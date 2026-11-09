#!/usr/bin/env python3
"""The sessions domain: run verdicts, durable records, the relay and a real
hosted session end to end. Zero dependencies (stdlib only)."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "lib"))

from pi_daemon.config import ServiceConfig
from pi_daemon.sessions.carry import SessionCarry
from pi_daemon.sessions.error import SessionError
from pi_daemon.sessions.handover import SessionHandover
from pi_daemon.sessions.identity import ProcessIdentity
from pi_daemon.sessions.model import SessionRecord
from pi_daemon.sessions.operations import SessionOperations
from pi_daemon.sessions.pi_command import PiCommand
from pi_daemon.sessions.pty_backend import PosixPtyBackend, service_root
from pi_daemon.sessions.reload import SessionReload
from pi_daemon.sessions.relay import SessionRelay
from pi_daemon.sessions.runner import SessionRunner
from pi_daemon.sessions.run_outcome import RunOutcome
from pi_daemon.sessions.service import SessionService
from pi_daemon.sessions.store import SessionStore
from pi_daemon.transport.control_action import ControlAction

SCRATCH = tempfile.mkdtemp(prefix="pi-sessions-")


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)


def roots():
    base = tempfile.mkdtemp(dir=SCRATCH)
    state, runtime = os.path.join(base, "state"), os.path.join(base, "run")
    os.makedirs(state)
    os.makedirs(runtime)
    return state, runtime


def store_for(base):
    state, runtime = base
    config = ServiceConfig()
    store = SessionStore(os.path.join(state, "sessions.json"),
                         os.path.join(state, "runs"), config)
    os.makedirs(os.path.join(state, "runs"), exist_ok=True)
    return store.load(), config


def owner(instance="i-1"):
    return {"instance": instance, "pid": os.getpid(), "boot": "b",
            "start": "s"}


def reserve(store, name="pi-a", file=None):
    record, created = store.reserve(
        name, "/tmp", ["sh", "-c", "true"], 80, 24, None, file, ["sh"], owner())
    return record, created


class OutcomeTests(unittest.TestCase):
    def seal(self, state="idle", published=True, reason="exit", code=0,
             aborted=False):
        record = {"aborted": aborted}
        return RunOutcome.seal(record, state, published, reason, code, 1.0)

    def test_the_daemon_stopping_outranks_a_signal(self):
        sealed = self.seal("busy", True, "shutdown", -9)
        self.assertEqual(sealed["verdict"], "daemon-stopped")
        self.assertEqual(sealed["cause"], "daemon")

    def test_a_stop_is_a_stop(self):
        self.assertEqual(self.seal("busy", True, "stop", None)["verdict"],
                         "stopped")

    def test_a_reload_death_is_named(self):
        self.assertEqual(self.seal("busy", True, "reload", -15)["verdict"],
                         "reload-death")

    def test_a_signal_mid_run_names_the_signal(self):
        sealed = self.seal("busy", True, "exit", -9)
        self.assertEqual((sealed["verdict"], sealed["signal"]),
                         ("died-mid-run", "SIGKILL"))

    def test_a_clean_exit_mid_run_is_still_a_death(self):
        self.assertEqual(self.seal("waiting", True)["verdict"],
                         "exited-mid-run")

    def test_a_crash_at_the_prompt(self):
        sealed = self.seal("idle", True, "exit", -11)
        self.assertEqual((sealed["verdict"], sealed["signal"]),
                         ("crashed-at-prompt", "SIGSEGV"))

    def test_an_aborted_run(self):
        self.assertEqual(self.seal(aborted=True)["verdict"], "run-aborted")

    def test_a_settled_run_has_no_cause(self):
        sealed = self.seal()
        self.assertEqual((sealed["verdict"], sealed["cause"]),
                         ("settled", None))

    def test_a_run_that_never_announced_is_unknown(self):
        self.assertEqual(self.seal(published=False)["verdict"], "unknown")

    def test_a_lost_run(self):
        self.assertEqual(self.seal("busy", True, "lost", None)["verdict"],
                         "lost-mid-run")
        self.assertEqual(self.seal("idle", True, "lost", None)["verdict"],
                         "lost-at-prompt")


class IdentityTests(unittest.TestCase):
    def test_a_live_process_matches_itself(self):
        identity = ProcessIdentity.current(os.getpid())
        self.assertEqual(identity.classify(identity), ProcessIdentity.SAME)

    def test_a_boot_change_is_a_different_process(self):
        expected = ProcessIdentity(os.getpid(), "b1", "1")
        self.assertEqual(ProcessIdentity(os.getpid(), "b2", "1")
                         .classify(expected), ProcessIdentity.GONE)

    def test_a_start_tick_change_is_a_recycled_pid(self):
        expected = ProcessIdentity(os.getpid(), "b", "1")
        self.assertEqual(ProcessIdentity(os.getpid(), "b", "2")
                         .classify(expected), ProcessIdentity.GONE)

    def test_an_unrecorded_start_tick_cannot_be_proven(self):
        boot = ProcessIdentity.current(os.getpid()).boot
        expected = ProcessIdentity(os.getpid(), boot, None)
        self.assertEqual(ProcessIdentity.current(os.getpid())
                         .classify(expected), ProcessIdentity.UNKNOWN)

    def test_a_missing_pid_is_gone(self):
        self.assertEqual(ProcessIdentity(0).classify(ProcessIdentity(0)),
                         ProcessIdentity.GONE)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.base = roots()
        self.store, self.config = store_for(self.base)

    def test_a_live_name_replays_instead_of_reserving(self):
        first, created = reserve(self.store)
        self.assertTrue(created)
        second, again = reserve(self.store)
        self.assertFalse(again)
        self.assertEqual(first.id, second.id)

    def test_an_exited_name_is_reserved_again_with_a_new_generation(self):
        first, _ = reserve(self.store)
        self.store.finish(first.id, "stop", 0)
        second, created = reserve(self.store)
        self.assertTrue(created)
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.generation, second.generation)

    def test_a_publish_needs_a_live_session(self):
        record, _ = reserve(self.store)
        self.store.finish(record.id, "stop", 0)
        self.assertFalse(self.store.publish("pi-a", "busy"))

    def test_a_snapshot_leaves_stopped_records_out_unless_asked(self):
        live, _ = reserve(self.store, "pi-a")
        gone, _ = reserve(self.store, "pi-b")
        self.store.finish(gone.id, "exit", 0)
        self.assertEqual([view["name"] for view in self.store.snapshot()],
                         ["pi-a"])
        self.assertEqual(sorted(view["name"]
                                for view in self.store.snapshot(True)),
                         ["pi-a", "pi-b"])
        self.assertEqual(self.store.stopped_count(), 1)
        self.assertEqual(live.name, "pi-a")

    def test_a_view_says_what_the_record_is_meant_to_be_doing(self):
        reserve(self.store, "pi-a")
        view = [v for v in self.store.snapshot(True)
                if v["name"] == "pi-a"][0]
        self.assertIn(view["phase"], ("spawning", "running"))
        self.assertEqual(view["desired"], "running")
        self.assertIn("file", view)

    def test_replace_all_removes_unnamed_runs_and_answers_run_state(self):
        record, _ = reserve(self.store, "pi-a")
        self.store.finish(record.id, "stop", 0)
        reserve(self.store, "pi-b")
        kept = SessionRecord(id="key-c", generation="g", name="pi-c",
                             desired="running", phase="exited",
                             child={"state": "idle"}, created=1.0)
        state = {"v": 1, "state": "busy", "seq": 7, "announced": True,
                 "open": True, "ended": None, "history": []}
        self.store.replace_all([kept], {"key-c": state})
        reloaded, _ = store_for(self.base)
        self.assertIsNone(reloaded.get("pi-a"))
        self.assertIsNone(reloaded.run_state("pi-b"))
        self.assertEqual(reloaded.run_state("pi-c")["seq"], 7)
        self.assertEqual(reloaded.run_state("pi-c")["name"], "pi-c")
        self.assertFalse(os.path.exists(
            os.path.join(self.base[0], "runs", record.id + ".json")))

    def test_an_older_sequence_is_ignored(self):
        reserve(self.store)
        self.assertTrue(self.store.publish("pi-a", "busy", seq=5))
        self.assertFalse(self.store.publish("pi-a", "idle", seq=4))
        self.assertEqual(self.store.run_state("pi-a")["state"], "busy")

    def test_a_publish_from_another_pid_is_ignored(self):
        record, _ = reserve(self.store)
        self.store.mark_started(record.id, os.getpid(),
                                ProcessIdentity.current(os.getpid()))
        self.assertFalse(self.store.publish("pi-a", "busy", pid=os.getpid() + 1))
        self.assertTrue(self.store.publish("pi-a", "busy", pid=os.getpid()))

    def test_a_second_finish_keeps_the_first_seal(self):
        record, _ = reserve(self.store)
        first = self.store.finish(record.id, "stop", 0)
        second = self.store.finish(record.id, "exit", 1)
        self.assertEqual(second, first)
        state = self.store.run_state("pi-a")
        self.assertEqual(state["ended"], first)
        self.assertEqual(state["history"], [])

    def test_recovery_leaves_an_unknown_identity_open(self):
        record, _ = reserve(self.store)
        other = SessionStore(os.path.join(self.base[0], "sessions.json"),
                             os.path.join(self.base[0], "runs"), self.config)
        other.load()
        self.assertEqual(other.recover("i-2", skip=[record.id]), 1)
        self.assertFalse(other.get("pi-a").live())
        self.assertIsNone(other.run_state("pi-a")["ended"])

    def test_a_busy_transition_counts_a_run_once(self):
        reserve(self.store)
        self.store.publish("pi-a", "busy")
        self.store.publish("pi-a", "waiting")
        self.store.publish("pi-a", "busy")
        self.assertEqual(self.store.run_state("pi-a")["run"], 1)

    def test_a_finish_seals_the_run_and_keeps_the_record(self):
        record, _ = reserve(self.store)
        self.store.publish("pi-a", "busy")
        sealed = self.store.finish(record.id, "exit", 0)
        self.assertEqual(sealed["verdict"], "exited-mid-run")
        state = self.store.run_state("pi-a")
        self.assertFalse(state["open"])
        self.assertEqual(state["ended"]["verdict"], "exited-mid-run")
        self.assertEqual(self.store.get("pi-a").pid(), 0)

    def test_a_reopen_carries_the_history_forward(self):
        first, _ = reserve(self.store)
        self.store.finish(first.id, "stop", 0)
        reserve(self.store, "pi-a")
        state = self.store.run_state("pi-a")
        self.assertTrue(state["open"])
        self.assertEqual(state["history"][-1]["verdict"], "stopped")
        self.assertIsNone(state["ended"])

    def test_recovery_seals_another_instances_open_run_as_lost(self):
        record, _ = reserve(self.store)
        self.store.publish("pi-a", "busy")
        other = SessionStore(os.path.join(self.base[0], "sessions.json"),
                             os.path.join(self.base[0], "runs"), self.config)
        other.load()
        self.assertEqual(other.recover("i-2"), 1)
        sealed = other.run_state("pi-a")["ended"]
        self.assertEqual(sealed["verdict"], "lost-mid-run")
        self.assertFalse(other.get("pi-a").live())

    def test_a_handoff_survives_a_reload(self):
        reserve(self.store, "pi-a")
        reserve(self.store, "pi-b")
        self.store.set_handoff("pi-a", "pi-b")
        reloaded = SessionStore(os.path.join(self.base[0], "sessions.json"),
                                os.path.join(self.base[0], "runs"), self.config)
        reloaded.load()
        self.assertEqual(reloaded.handoff("pi-a"), {"name": "pi-b"})
        reloaded.clear_handoff("pi-a")
        self.assertIsNone(reloaded.handoff("pi-a"))

    def test_holders_are_ordered_by_attachment(self):
        first, _ = reserve(self.store, "pi-a")
        second, _ = reserve(self.store, "pi-b")
        self.store.announce("pi-a", "/conv.jsonl")
        self.store.announce("pi-b", "/conv.jsonl")
        self.store.set_clients("pi-b", 2)
        self.assertEqual([name for name, _ in self.store.holders("/conv.jsonl")],
                         ["pi-b", "pi-a"])
        self.assertEqual(self.store.holders("/other.jsonl"), [])
        del first, second


class ViewerRelay:
    """A relay stub that counts viewers per session id and records moves."""

    def __init__(self, viewers=None):
        self.viewers = dict(viewers or {})
        self.dropped = []
        self.moved = []

    def viewer_count(self, key):
        return self.viewers.get(key, 0)

    def drop_viewers(self, key):
        self.dropped.append(key)
        self.viewers[key] = 0

    def move_viewers(self, key, target):
        self.moved.append((key, target))
        self.viewers[target] = (self.viewers.get(target, 0)
                                + self.viewers.pop(key, 0))


class ListingTests(unittest.TestCase):
    """The listing a revive is chosen from: what is live, what is left."""

    def setUp(self):
        self.base = roots()
        self.addCleanup(shutil.rmtree, self.base[0], True)
        self.store, self.config = store_for(self.base)
        self.saved = os.path.join(self.base[0], "conv.jsonl")
        open(self.saved, "w").close()

    def service(self):
        return SessionService(self.store, None, ViewerRelay({}), self.config)

    def stop(self, name, file):
        record, _ = reserve(self.store, name, file)
        self.store.finish(record.id, "exit", 0)
        return record

    def test_a_stopped_record_says_whether_it_can_be_resumed(self):
        self.stop("pi-a", self.saved)
        self.stop("pi-b", os.path.join(self.base[0], "gone.jsonl"))
        self.stop("pi-c", None)
        rows = {view["name"]: view
                for view in self.service().sessions(include_stopped=True)}
        self.assertTrue(rows["pi-a"]["fileExists"])
        self.assertFalse(rows["pi-b"]["fileExists"], "the file is gone")
        self.assertFalse(rows["pi-c"]["fileExists"], "nothing was recorded")
        self.assertEqual(rows["pi-a"]["phase"], "exited")
        self.assertEqual(self.service().sessions(), [],
                         "a live-only listing leaves them out")
        self.assertEqual(self.service().stopped_count(), 3)

    def test_a_recorded_name_answers_its_stored_spelling_and_directory(self):
        self.stop("pi-a", self.saved)
        self.assertEqual(self.service().recorded("a"),
                         {"name": "pi-a", "dir": "/tmp",
                          "file": self.saved})
        self.assertIsNone(self.service().recorded("nobody"),
                          "a name with no record promises nothing")


class AnnounceTests(unittest.TestCase):
    """A takeover gives the conversation to whichever holder is busiest."""

    def setUp(self):
        self.base = roots()
        self.addCleanup(shutil.rmtree, self.base[0], True)
        self.store, self.config = store_for(self.base)
        self.first, _ = reserve(self.store, "pi-a", "/conv.jsonl")
        self.second, _ = reserve(self.store, "pi-b", "/conv.jsonl")

    def service(self, viewers):
        self.relay = ViewerRelay(viewers)
        return SessionService(self.store, None, self.relay, self.config)

    def test_a_takeover_with_more_viewers_keeps_the_conversation(self):
        service = self.service({self.first.id: 2, self.second.id: 1})
        reply = service.announce("pi-a", "/conv.jsonl", takeover=True)
        self.assertIsNone(reply["handoff"], "the busier holder keeps it")
        self.assertEqual(reply["tookOver"], ["pi-b"],
                         "the duplicate holder hands over")
        self.assertEqual(self.store.handoff("pi-b"), {"name": "pi-a"})
        self.assertEqual(self.relay.dropped, [self.second.id],
                         "the other holder's viewers are dropped")
        self.assertEqual(self.store.get("pi-b").child["clients"], 0)

    def test_a_takeover_with_fewer_viewers_hands_the_conversation_over(self):
        service = self.service({self.first.id: 0, self.second.id: 1})
        reply = service.announce("pi-a", "/conv.jsonl", takeover=True)
        self.assertEqual(reply["handoff"], {"name": "pi-b",
                                            "state": self.second.state()})
        self.assertEqual(reply["tookOver"], [])
        self.assertEqual(self.store.handoff("pi-a"), {"name": "pi-b"})
        self.assertEqual(self.relay.moved, [(self.first.id, self.second.id)],
                         "its viewers follow the conversation")
        self.assertEqual(self.store.get("pi-b").child["clients"], 1)
        self.assertEqual(self.store.get("pi-a").child["clients"], 0)

    def test_a_takeover_without_a_duplicate_holder_changes_nothing(self):
        service = self.service({})
        reply = service.announce("pi-a", "/other.jsonl", takeover=True)
        self.assertIsNone(reply["handoff"])
        self.assertEqual(reply["tookOver"], [])
        self.assertIsNone(self.store.handoff("pi-a"))


class SpawnGateBackend:
    """A backend whose spawn blocks, so a stop can land mid-reservation."""

    def __init__(self):
        self.entered = threading.Event()
        self.opened = threading.Event()

    def spawn(self, argv, cwd, env, cols, rows):
        self.entered.set()
        self.opened.wait(5)
        self.child = GatedChild()
        return self.child


class GatedChild:
    """It is the daemon's own pid, so identity is provable without a process."""

    def __init__(self):
        self.pid = os.getpid()
        self.identity = None
        self.terminated = False
        self.closed = False

    def release(self):
        return None

    def terminate(self, grace=None):
        self.terminated = True

    def close(self):
        self.closed = True

    def disown_master(self):
        return -1


class StubRelay:
    def __init__(self):
        self.closed = []

    def register(self, key, generation, master):
        return True

    def close(self, key):
        self.closed.append(key)

    def finish(self, key, reason="exit"):
        return None


class RecordingRelay:
    """A relay that records what reached a run's master, or refuses it."""

    def __init__(self, accepting=True):
        self.written = []
        self.accepting = accepting

    def input(self, key, data):
        if not self.accepting:
            return False
        self.written.append((key, data))
        return True


class SendRunTests(unittest.TestCase):
    """The fallback types into the run it selected, never into its heir."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pi-sendrun-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.config = ServiceConfig(settle_grace=0.5, cancel_grace=0.2)
        self.store = SessionStore(os.path.join(self.base, "sessions.json"),
                                  os.path.join(self.base, "runs"),
                                  self.config)
        self.relay = RecordingRelay()
        self.runner = SessionRunner(self.store, self.relay, self.config,
                                    SpawnGateBackend(),
                                    {"instance": "i-1"})

    def test_a_child_is_told_which_run_it_is(self):
        record, _created = reserve(self.store)
        environment = self.runner._environment(
            "pi-a", record, {"PI_DAEMON_RUN_ID": "a-caller-remembered"})
        self.assertEqual(environment["PI_DAEMON_RUN_ID"], record.id,
                         "the record's identity wins over the caller's")
        self.assertEqual(environment["PI_DAEMON_RUN_GENERATION"],
                         record.generation)

    def test_a_send_run_reaches_exactly_the_run_it_names(self):
        record, _created = reserve(self.store)
        sent = self.runner.send_run(record.id, record.generation,
                                    b"/reload\r")
        self.assertEqual(sent, len(b"/reload\r"))
        self.assertEqual(self.relay.written, [(record.id, b"/reload\r")])

    def test_a_successor_that_took_the_name_is_never_written(self):
        old, _created = reserve(self.store)
        self.store.finish(old.id, "stop", 0)
        heir, _created = reserve(self.store)
        self.assertNotEqual(heir.id, old.id)
        with self.assertRaises(SessionError):
            self.runner.send_run(old.id, old.generation, b"/reload\r")
        self.assertEqual(self.relay.written, [],
                         "the successor received nothing")

    def test_a_stale_generation_of_the_same_run_is_refused(self):
        record, _created = reserve(self.store)
        with self.assertRaises(SessionError):
            self.runner.send_run(record.id, "an-earlier-round",
                                 b"/reload\r")
        self.assertEqual(self.relay.written, [])

    def test_an_unknown_run_is_refused(self):
        with self.assertRaises(SessionError):
            self.runner.send_run("no-such-run", "no-such-round",
                                 b"/reload\r")
        self.assertEqual(self.relay.written, [])

    def test_a_relay_that_dropped_the_run_is_reported(self):
        record, _created = reserve(self.store)
        self.relay.accepting = False
        with self.assertRaises(SessionError):
            self.runner.send_run(record.id, record.generation,
                                 b"/reload\r")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pi-runner-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.config = ServiceConfig(settle_grace=0.5, cancel_grace=0.2)
        self.store = SessionStore(os.path.join(self.base, "sessions.json"),
                                  os.path.join(self.base, "runs"), self.config)
        self.backend = SpawnGateBackend()
        self.relay = StubRelay()
        self.runner = SessionRunner(self.store, self.relay, self.config,
                                    self.backend, {"instance": "i-1"})

    def start_in_background(self):
        errors = []

        def start():
            try:
                self.runner.start("pi-a", self.base, ["sh"], 80, 24)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=start)
        thread.start()
        self.assertTrue(self.backend.entered.wait(5))
        return thread, errors

    def test_a_stop_during_a_spawn_still_ends_the_child(self):
        thread, errors = self.start_in_background()
        reply = self.runner.stop("pi-a")
        self.assertTrue(reply["queued"], reply)
        self.backend.opened.set()
        thread.join(5)
        self.assertEqual(errors, [])
        self.assertTrue(self.backend.child.terminated)
        record = self.store.get("pi-a")
        self.assertEqual(self.store.stop_reason(record.id), "stop")

    def test_a_shutdown_during_a_spawn_names_the_run(self):
        thread, errors = self.start_in_background()
        self.runner.begin_shutdown("shutdown")
        self.backend.opened.set()
        thread.join(5)
        self.assertEqual(errors, [])
        self.assertTrue(self.backend.child.terminated)
        record = self.store.get("pi-a")
        self.assertEqual(self.store.stop_reason(record.id), "shutdown")


class StubService:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def __getattr__(self, name):
        def call(*arguments):
            self.calls.append((name, arguments))
            if self.failure is not None:
                raise self.failure
            return {"name": arguments[0] if arguments else None}
        return call

    def recorded_file(self, name):
        return "/conv.jsonl"

    def recorded(self, name):
        return {"name": name, "dir": "/s", "file": "/conv.jsonl"}


class OperationsTests(unittest.TestCase):
    def operations(self, service=None):
        self.service = service or StubService()
        self.stopped = []
        return SessionOperations(
            self.service,
            lambda *mode: self.stopped.append(mode),
            ServiceConfig())

    def test_a_start_needs_an_existing_directory(self):
        reply = self.operations().start(
            {"cmd": "start", "name": "pi-a", "dir": "/does/not/exist",
             "argv": ["sh"]})
        self.assertEqual(reply["error"], "no-dir")

    def test_an_attach_takes_the_connection_only_after_the_reply(self):
        service = StubService()
        service.attach_plan = lambda name, cols, rows: {
            "name": name, "key": "k", "generation": "g", "file": None,
            "state": "idle", "clients": 1, "cols": cols, "rows": rows,
            "handoff": None}
        operations = self.operations(service)
        action = operations.attach({"cmd": "attach", "name": "pi-a"}, None)
        self.assertIsInstance(action, ControlAction)
        self.assertEqual(action.reply["clients"], 1)
        action.upgrade(socket.socketpair()[0], b"")
        self.assertEqual(service.calls[-1][0], "attach_now")

    def test_an_attach_failure_hands_back_the_recorded_file(self):
        reply = self.operations(StubService(SessionError("no-session"))) \
            .attach({"cmd": "attach", "name": "pi-a"}, None)
        self.assertEqual(reply["error"], "no-session")
        self.assertEqual(reply["file"], "/conv.jsonl")
        self.assertEqual(reply["recorded"], "pi-a")
        self.assertEqual(reply["dir"], "/s")

    def test_an_attach_failure_with_a_recorded_name_answers_no_file(self):
        service = StubService(SessionError("no-session"))
        service.recorded = lambda name: {"name": "pi-a", "dir": "/s",
                                         "file": None}
        reply = self.operations(service).attach(
            {"cmd": "attach", "name": "pi-a"}, None)
        self.assertEqual(reply["error"], "no-session")
        self.assertEqual(reply["recorded"], "pi-a")
        self.assertNotIn("file", reply,
                         "a record with no conversation promises none")

    def test_an_attach_failure_without_a_record_answers_no_record(self):
        service = StubService(SessionError("no-session"))
        service.recorded = lambda name: None
        reply = self.operations(service).attach(
            {"cmd": "attach", "name": "pi-a"}, None)
        self.assertEqual(reply, {"ok": False, "error": "no-session"})

    def test_a_listing_counts_the_stopped_records_it_leaves_out(self):
        service = StubService()
        service.sessions = lambda include_stopped=False: [
            {"name": "pi-a", "phase": "running"}]
        service.stopped_count = lambda: 3
        reply = self.operations(service).list({"cmd": "list"})
        self.assertEqual(reply["stopped"], 3)
        self.assertEqual(len(reply["sessions"]), 1)

    def test_an_all_listing_carries_no_stopped_count(self):
        service = StubService()
        service.sessions = lambda include_stopped=False: [
            {"name": "pi-a", "phase": "running"},
            {"name": "pi-b", "phase": "exited"}]
        service.stopped_count = lambda: 1
        reply = self.operations(service).list({"cmd": "list", "all": True})
        self.assertNotIn("stopped", reply)
        self.assertEqual(len(reply["sessions"]), 2)

    def test_an_all_listing_needs_a_boolean(self):
        with self.assertRaises(ValueError):
            self.operations().list({"cmd": "list", "all": "yes"})

    def test_an_attach_with_a_handoff_never_upgrades(self):
        service = StubService()
        service.attach_plan = lambda name, cols, rows: {
            "name": name, "state": "busy", "file": None,
            "handoff": {"name": "pi-b", "state": "busy"}}
        reply = self.operations(service).attach(
            {"cmd": "attach", "name": "pi-a"}, None)
        self.assertEqual(reply["handoff"]["name"], "pi-b")
        self.assertNotIsInstance(reply, ControlAction)

    def test_an_announce_passes_takeover_through(self):
        operations = self.operations()
        operations.announce({"cmd": "announce", "name": "pi-a",
                             "file": "/conv/a.jsonl", "takeover": True})
        self.assertEqual(self.service.calls[-1],
                         ("announce", ("pi-a", "/conv/a.jsonl", True)))

    def test_a_non_boolean_takeover_is_refused(self):
        with self.assertRaises(ValueError):
            self.operations().announce(
                {"cmd": "announce", "name": "pi-a", "file": "/conv/a.jsonl",
                 "takeover": "yes"})

    def test_a_carry_is_projected_and_named(self):
        reply = self.operations().carry(
            {"cmd": "carry", "name": "pi-a", "to": "pi-b"})
        self.assertEqual(reply["name"], "pi-a")
        self.assertTrue(reply["ok"])
        self.assertEqual(self.service.calls,
                         [("carry", ("pi-a", "pi-b", None))])

    def test_a_carry_to_a_non_name_is_refused(self):
        with self.assertRaises(ValueError):
            self.operations().carry(
                {"cmd": "carry", "name": "pi-a", "to": 7})

    def test_a_held_handover_hosts_only_after_the_client_leaves(self):
        service = StubService()
        hosted = []
        service.handover = lambda dir_, file: {"verdict": "target:pi-x",
                                               "hosted": False,
                                               "target": "pi-x"}
        service.host_handover = lambda *arguments: hosted.append(arguments)
        operations = self.operations(service)
        client, server = socket.socketpair()
        directory = tempfile.mkdtemp(prefix="handover-")
        self.addCleanup(shutil.rmtree, directory, True)
        action = operations.handover(
            {"cmd": "handover", "dir": directory, "file": "/conv.jsonl",
             "hold": True}, server)
        self.assertIsInstance(action, ControlAction)
        self.assertEqual(action.reply, {"ok": True, "verdict": "target:pi-x"})
        self.assertEqual(hosted, [])
        self.assertIsNone(action.after_send)
        client.close()
        action.on_close()
        deadline = time.monotonic() + 5
        while not hosted and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertEqual(hosted, [("pi-x", directory, "/conv.jsonl", None)])
        server.close()

    def test_a_hosted_conversation_is_not_hosted_again(self):
        service = StubService()
        hosted = []
        service.handover = lambda dir_, file: {"verdict": "hosted:pi-x",
                                               "hosted": True,
                                               "target": "pi-x"}
        service.host_handover = lambda *arguments: hosted.append(arguments)
        directory = tempfile.mkdtemp(prefix="handover-")
        self.addCleanup(shutil.rmtree, directory, True)
        reply = self.operations(service).handover(
            {"cmd": "handover", "dir": directory, "file": "/conv.jsonl",
             "hold": True}, None)
        self.assertEqual(reply, {"ok": True, "verdict": "hosted:pi-x"})
        self.assertEqual(hosted, [])

    def test_a_handover_outside_a_directory_is_no_dir(self):
        reply = self.operations().handover(
            {"cmd": "handover", "dir": "/does/not/exist",
             "file": "/conv.jsonl"}, None)
        self.assertEqual(reply["error"], "no-dir")

    def test_an_unknown_run_state_is_refused(self):
        with self.assertRaises(ValueError):
            self.operations().state({"cmd": "state", "name": "pi-a",
                                     "state": "sleeping"})

    def test_a_shutdown_answers_before_it_stops(self):
        action = self.operations().shutdown({"cmd": "shutdown"})
        self.assertFalse(self.stopped)
        action.after_send()
        self.assertEqual(self.stopped, [("shutdown", "control shutdown")])

    def test_input_is_projected_with_its_byte_count(self):
        service = StubService()
        service.input = lambda name, text: {"name": name, "bytes": len(text)}
        reply = self.operations(service).input(
            {"cmd": "input", "name": "pi-a", "text": "hi"})
        self.assertEqual(reply["bytes"], 2)


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.child = None
        self.relay = None
        self.config = ServiceConfig(settle_grace=1.0)
        self.backend = PosixPtyBackend(self.config, service_root())

    def tearDown(self):
        if self.relay is not None:
            self.relay.stop()
        if self.child is not None:
            self.child.terminate(grace=0.2)
            self.child.close()

    def spawn(self, script):
        self.child = self.backend.spawn(["sh", "-c", script], REPO,
                                        dict(os.environ), 80, 24)
        self.child.release()
        self.relay = SessionRelay(self.config, self.backend)
        self.relay.start()
        self.relay.register("k", "g", self.child.disown_master())

    def attach(self):
        viewer, peer = socket.socketpair()
        self.relay.attach("k", "g", viewer)
        return peer

    def read_until(self, connection, marker, timeout=5.0):
        buffer = b""
        deadline = time.monotonic() + timeout
        connection.settimeout(0.5)
        while marker not in buffer and time.monotonic() < deadline:
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer += chunk
        return buffer

    def test_output_reaches_a_viewer_and_input_reaches_the_child(self):
        self.spawn("printf 'READY\\n'; read line; printf 'GOT:%s\\n' \"$line\"")
        viewer = self.attach()
        self.assertIn(b"READY", self.read_until(viewer, b"READY"))
        self.relay.input("k", b"hello\n")
        self.assertIn(b"GOT:hello", self.read_until(viewer, b"GOT:hello"))
        viewer.close()

    def test_a_finished_session_releases_its_viewers(self):
        self.spawn("read line; printf 'BYE\\n'")
        viewer = self.attach()
        self.relay.input("k", b"go\n")
        self.assertIn(b"BYE", self.read_until(viewer, b"BYE"))
        self.child.wait(5)
        self.relay.finish("k")
        viewer.settimeout(5)
        self.assertEqual(viewer.recv(65536), b"")

    def test_final_output_reaches_a_viewer_before_release(self):
        self.spawn("read line; printf 'LAST\\n'; exit 3")
        viewer = self.attach()
        self.relay.input("k", b"go\n")
        self.child.wait(5)
        self.relay.finish("k")
        self.assertIn(b"LAST", self.read_until(viewer, b"LAST"))
        viewer.settimeout(5)
        self.assertEqual(viewer.recv(65536), b"")
        viewer.close()

    def test_a_moved_viewer_receives_the_target_backlog(self):
        self.spawn("printf 'X\\n'; sleep 5")
        other = self.spawn_second("printf 'T\\n'; sleep 5")
        time.sleep(0.3)
        viewer = self.attach()
        self.assertIn(b"X", self.read_until(viewer, b"X"))
        self.assertEqual(self.relay.move_viewers("k", other), 1)
        self.assertIn(b"T", self.read_until(viewer, b"T"))
        viewer.close()

    def test_a_second_viewer_shares_the_stream(self):
        self.spawn("read line; printf 'SHARED\\n'")
        first, second = self.attach(), self.attach()
        self.relay.input("k", b"go\n")
        self.assertIn(b"SHARED", self.read_until(first, b"SHARED"))
        self.assertIn(b"SHARED", self.read_until(second, b"SHARED"))
        first.close()
        second.close()

    def test_moving_viewers_leaves_the_source_headless(self):
        self.spawn("printf 'X\\n'; sleep 5")
        other = self.spawn_second()
        viewer = self.attach()
        self.assertIn(b"X", self.read_until(viewer, b"X"))
        self.assertEqual(self.relay.move_viewers("k", other), 1)
        self.assertEqual(self.relay.viewer_count("k"), 0)
        viewer.close()

    def test_a_carried_viewer_repaints_a_cleared_screen(self):
        self.spawn("printf 'X\\n'; sleep 5")
        other = self.spawn_second("printf 'T\\n'; sleep 5")
        time.sleep(0.3)
        viewer = self.attach()
        self.assertIn(b"X", self.read_until(viewer, b"X"))
        moved = self.relay.carry_viewers("k", "g", other, "g2", 100, 40)
        self.assertEqual(moved, {"moved": 1})
        seen = self.read_until(viewer, b"T")
        self.assertIn(b"\x1b[2J", seen)
        self.assertEqual(self.relay.viewer_count("k"), 0)
        # The nudge is restored by the loop, not by a sleeping caller.
        deadline = time.monotonic() + 5
        while self.relay._repaints and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.relay._repaints, {})
        viewer.close()

    def test_a_resize_cancels_a_pending_repaint_restore(self):
        import fcntl
        import struct
        import termios

        self.spawn("printf 'X\\n'; read line")
        other = self.spawn_second()
        time.sleep(0.3)
        viewer = self.attach()
        self.assertIn(b"X", self.read_until(viewer, b"X"))
        self.relay.carry_viewers("k", "g", other, "g2", 100, 40)
        self.relay.resize(other, 120, 50)
        # The restore the carry still owed must not undo the new size.
        time.sleep(self.config.repaint_tick * 4)
        master = self.relay._sessions[other].master
        packed = fcntl.ioctl(master, termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols = struct.unpack("HH", packed[:4])
        self.assertEqual((cols, rows), (120, 50))
        self.assertEqual(self.relay._repaints, {})
        viewer.close()

    def test_a_carry_across_a_replaced_generation_is_refused(self):
        self.spawn("printf 'X\\n'; sleep 5")
        other = self.spawn_second()
        time.sleep(0.3)
        viewer = self.attach()
        self.assertIn(b"X", self.read_until(viewer, b"X"))
        self.assertIsNone(
            self.relay.carry_viewers("k", "g-stale", other, "g2", 80, 24))
        self.assertEqual(self.relay.viewer_count("k"), 1)
        viewer.close()

    def spawn_second(self, script="sleep 5"):
        second = self.backend.spawn(["sh", "-c", script], REPO,
                                    dict(os.environ), 80, 24)
        second.release()
        self.addCleanup(lambda: (second.terminate(grace=0.2), second.close()))
        self.relay.register("k2", "g2", second.disown_master())
        return "k2"


class NudgeTests(unittest.TestCase):
    """A repaint nudge always steps to a size the child has never had."""

    def setUp(self):
        self.backend = PosixPtyBackend(ServiceConfig(), service_root())
        self.master, self.slave = os.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)

    def size(self):
        import fcntl
        import struct
        import termios

        packed = fcntl.ioctl(self.master, termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols = struct.unpack("HH", packed[:4])
        return cols, rows

    def test_every_terminal_steps_away_and_returns(self):
        for cols, rows in ((80, 24), (80, 1), (1, 1)):
            self.backend.resize(self.master, cols, rows)
            temporary = self.backend.nudge(self.master, cols, rows)
            self.assertNotEqual(temporary, (cols, rows))
            self.assertEqual(self.size(), temporary)
            self.backend.resize(self.master, cols, rows)
            self.assertEqual(self.size(), (cols, rows))


class CarryRelay:
    """A relay that records the transfer instead of moving sockets."""

    def __init__(self, result=None):
        self.result = {"moved": 1} if result is None else result
        self.calls = []

    def carry_viewers(self, *arguments):
        self.calls.append(arguments)
        return self.result


class CarryRunner:
    """A runner that reserves the target instead of spawning a process."""

    def __init__(self, store, created=True):
        self.store = store
        self.created = created
        self.started = []
        self.stopped = []

    def start(self, name, dir_, argv, cols, rows, env=None, env_once=None,
              file=None, durable_argv=None):
        self.started.append((name, dir_, argv, cols, rows, file))
        if not self.created:
            record, _ = self.store.reserve(name, dir_, argv, cols, rows, None,
                                           None, argv, owner())
            self.store.finish(record.id, "done", None)
            return self.store.get(name), False
        durable = durable_argv or argv
        record, _ = self.store.reserve(name, dir_, durable, cols, rows, None,
                                       file, durable, owner())
        return record, True

    def stop_run(self, key, generation, reason="stop"):
        self.stopped.append((key, generation))
        return True


class CarryTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pi-carry-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.store, self.config = store_for(roots())
        self.source, _ = reserve(self.store, "pi-a")
        self.relay = CarryRelay()
        self.runner = CarryRunner(self.store)
        self.carry = SessionCarry(self.store, self.runner, self.relay,
                                  PiCommand({}))

    def test_carry_to_an_existing_session_moves_the_viewers(self):
        target, _ = reserve(self.store, "pi-b")
        reply = self.carry.carry("pi-a", to="pi-b")
        self.assertEqual(reply, {"name": "pi-b"})
        self.assertEqual(self.runner.started, [])
        self.assertEqual(self.relay.calls, [(
            self.source.id, self.source.generation, target.id,
            target.generation, self.source.cols, self.source.rows)])
        self.assertTrue(self.store.get("pi-a").live())

    def test_a_fresh_target_is_spawned_beside_the_source(self):
        reply = self.carry.carry("pi-a", argv=["pi", "--continue"])
        name = reply["name"]
        self.assertTrue(name.startswith("pi-"), name)
        self.assertNotEqual(name, "pi-a")
        spawned = self.runner.started[0]
        self.assertEqual(spawned[0], name)
        self.assertEqual(spawned[1], self.source.dir)
        self.assertEqual((spawned[3], spawned[4]),
                         (self.source.cols, self.source.rows))
        self.assertEqual(spawned[2][-1], "--continue")
        self.assertIn("pi", spawned[2])
        self.assertTrue(self.store.get("pi-a").live())

    def test_a_rejected_transfer_drops_only_the_target_it_created(self):
        self.relay.result = None
        with self.assertRaises(SessionError) as caught:
            self.carry.carry("pi-a")
        self.assertEqual(caught.exception.code, "no-session")
        self.assertEqual(len(self.runner.stopped), 1)
        self.assertFalse(self.store.get("pi-a").generation ==
                         self.runner.stopped[0][1])

    def test_a_rejected_transfer_never_stops_an_existing_target(self):
        reserve(self.store, "pi-b")
        self.relay.result = None
        with self.assertRaises(SessionError):
            self.carry.carry("pi-a", to="pi-b")
        self.assertEqual(self.runner.stopped, [])
        self.assertTrue(self.store.get("pi-b").live())

    def test_a_lost_name_race_is_name_taken(self):
        self.runner.created = False
        with self.assertRaises(SessionError) as caught:
            self.carry.carry("pi-a")
        self.assertEqual(caught.exception.code, "name-taken")

    def test_a_name_cannot_carry_into_itself(self):
        with self.assertRaises(SessionError) as caught:
            self.carry.carry("pi-a", to="pi-a")
        self.assertEqual(caught.exception.code, "no-session")

    def test_an_unknown_source_is_no_session(self):
        with self.assertRaises(SessionError) as caught:
            self.carry.carry("pi-absent")
        self.assertEqual(caught.exception.code, "no-session")

    def test_the_pi_seam_keeps_an_explicit_argv_and_shims_a_bare_pi(self):
        command = PiCommand({})
        self.assertEqual(command.fresh(["sh", "-c", "true"]),
                         ["sh", "-c", "true"])
        self.assertEqual(command.fresh([])[-1], "pi")
        self.assertEqual(command.fresh(["pi", "--x"])[-1], "--x")
        self.assertIn("pi", command.fresh(["pi", "--x"]))
        hosted, durable = command.resume("/conv.jsonl", message="carry on")
        self.assertIn("/conv.jsonl", hosted)
        self.assertIn("carry on", hosted)
        self.assertNotIn("carry on", durable)


class HandoverTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="work:", dir=SCRATCH)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.file = os.path.join(self.dir, "conversation.jsonl")
        with open(self.file, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        self.store, self.config = store_for(roots())
        self.runner = CarryRunner(self.store)
        self.handover = SessionHandover(self.store, self.runner,
                                       PiCommand({}), self.config,
                                       settle=0)
        self.base = os.path.basename(self.dir).replace(":", "_")

    def test_a_free_directory_names_the_primary_target(self):
        self.assertEqual(self.handover.target(self.dir, self.file),
                         "pi-" + self.base)

    def test_another_directory_holding_the_name_gets_a_digest(self):
        self.store.reserve("pi-" + self.base, "/elsewhere", ["sh"], 80, 24,
                           None, None, ["sh"], owner())
        target = self.handover.target(self.dir, self.file)
        self.assertTrue(target.startswith("pi-%s-" % self.base), target)
        self.assertEqual(len(target.rsplit("-", 1)[1]), 6)
        self.assertTrue(self.handover.taken("pi-" + self.base, self.dir))
        self.assertFalse(self.handover.taken(target, self.dir))

    def test_the_same_directory_keeps_the_primary_target(self):
        self.store.reserve("pi-" + self.base, self.dir, ["sh"], 80, 24,
                           None, self.file, ["sh"], owner())
        self.assertEqual(self.handover.target(self.dir, self.file),
                         "pi-" + self.base)
        self.assertFalse(self.handover.taken("pi-" + self.base, self.dir))

    def test_hosting_resumes_the_file_under_its_promised_target(self):
        name = self.handover.host("pi-" + self.base, self.dir, self.file)
        self.assertEqual(name, "pi-" + self.base)
        spawned = self.runner.started[0]
        self.assertEqual(spawned[0], name)
        self.assertEqual(spawned[1], self.dir)
        self.assertIn("--session", spawned[2])
        self.assertIn(self.file, spawned[2])
        self.assertEqual(spawned[5], self.file)
        self.assertTrue(self.store.get(name).live())

    def test_a_gone_conversation_is_not_hosted(self):
        os.unlink(self.file)
        self.assertIsNone(
            self.handover.host("pi-" + self.base, self.dir, self.file))
        self.assertEqual(self.runner.started, [])

    def test_a_name_hosted_meanwhile_is_not_hosted_twice(self):
        self.store.reserve("pi-" + self.base, self.dir, ["sh"], 80, 24,
                           None, self.file, ["sh"], owner())
        self.assertIsNone(
            self.handover.host("pi-" + self.base, self.dir, self.file))
        self.assertEqual(self.runner.started, [])

    def test_the_promised_name_wins_over_a_newly_busy_primary(self):
        self.store.reserve("pi-" + self.base, "/elsewhere", ["sh"], 80, 24,
                           None, None, ["sh"], owner())
        name = self.handover.host("pi-elsewhere", self.dir, self.file)
        self.assertEqual(name, "pi-elsewhere")

    def test_the_message_is_launched_but_never_remembered(self):
        self.handover.host("pi-" + self.base, self.dir, self.file, "carry on")
        spawned = self.runner.started[0]
        self.assertEqual(spawned[5], self.file)
        record = self.store.get("pi-" + self.base)
        self.assertIn("carry on", spawned[2])
        self.assertNotIn("carry on", record.argv)
        self.assertNotIn("carry on", record.resume_argv)


class StubReload:
    """A round whose grace decides: names in `consumed` ack, the rest wait."""

    def __init__(self, consumed=(), write_failed=(), claimed=False,
                 late_consumed=()):
        self.signals = []
        self.runs = []
        self.claimed = []
        self.entries = 0
        self.inside = 0
        self.overlapped = False
        self.consumed = list(consumed)
        self.late_consumed = list(late_consumed)
        self.write_failed = set(write_failed)
        self.claimable = claimed

    def token(self):
        return "tok"

    def signal(self, name, token, run=None, generation=None):
        if name in self.write_failed:
            return False
        self.signals.append((name, token))
        self.runs.append((name, run, generation))
        return True

    def await_consumed(self, names, tokens=None):
        self.entries += 1
        self.inside += 1
        if self.inside > 1:
            self.overlapped = True
        try:
            return [name for name in names
                    if name in self.consumed]
        finally:
            self.inside -= 1

    def claim(self, name, token):
        self.claimed.append((name, token))
        if name in self.late_consumed:
            return False
        return self.claimable


class StubSender:
    def __init__(self, failing=(), raising=(), store=None):
        self.sent = []
        self.failing = set(failing)
        self.raising = set(raising)
        self.store = store

    def send(self, name, data):
        if name in self.raising:
            raise TimeoutError("the relay did not answer")
        if name in self.failing:
            raise SessionError("write-failed")
        self.sent.append((name, data))
        return len(data)

    def send_run(self, key, generation, data):
        """The identity-scoped delivery: resolve the key for the report."""
        name = self.store.name_of(key) if self.store is not None else None
        if name is None:
            raise SessionError("write-failed")
        return self.send(name, data)


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.state, _runtime = roots()
        self.reload = SessionReload(self.state, grace=0.0, poll=0.0,
                                    sleep=lambda _s: None)

    def test_a_signal_carries_its_round_in_the_filename_and_the_body(self):
        self.assertTrue(self.reload.signal("pi-a", "round-1", "run-1", "gen-1"))
        path = self.reload.path("pi-a", "round-1")
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["token"], "round-1")
        self.assertEqual((payload["run"], payload["generation"]),
                         ("run-1", "gen-1"), "the request names its run")
        self.assertEqual(os.listdir(self.reload.directory("pi-a")),
                         ["round-1.json"])

    def test_each_round_carries_a_fresh_name(self):
        self.assertNotEqual(self.reload.token(), self.reload.token())

    def test_consumption_is_the_deletion_of_that_rounds_file(self):
        self.reload.signal("pi-a", "round-1", "run-1", "gen-1")
        self.assertFalse(self.reload.consumed("pi-a", "round-1"))
        self.assertEqual(
            self.reload.await_consumed(["pi-a"], {"pi-a": "round-1"}), [])
        self.assertTrue(self.reload.claim("pi-a", "round-1"))
        self.assertTrue(self.reload.consumed("pi-a", "round-1"))
        self.assertEqual(
            self.reload.await_consumed(["pi-a"], {"pi-a": "round-1"}),
            ["pi-a"])

    def test_a_claim_never_deletes_another_rounds_request(self):
        self.reload.signal("pi-a", "round-2", "run-1", "gen-1")
        self.assertFalse(self.reload.claim("pi-a", "round-1"))
        self.assertFalse(self.reload.consumed("pi-a", "round-2"))
        self.assertTrue(self.reload.claim("pi-a", "round-2"))

    def test_a_claim_of_an_absent_request_answers_so(self):
        self.assertFalse(self.reload.claim("pi-a", "round-1"))

    def test_an_unreadable_request_is_not_an_acknowledgement(self):
        self.assertTrue(self.reload.signal("pi-a", "round-1", "run-1",
                                           "gen-1"))
        directory = self.reload.directory("pi-a")
        if os.name != "posix" or os.geteuid() == 0:
            self.skipTest("permissions are not enforced here")
        os.chmod(directory, 0o000)
        try:
            self.assertFalse(self.reload.consumed("pi-a", "round-1"))
        finally:
            os.chmod(directory, 0o700)
        self.assertFalse(self.reload.consumed("pi-a", "round-1"))
        self.assertTrue(self.reload.claim("pi-a", "round-1"))

    def test_a_name_the_directory_cannot_hold_is_refused(self):
        self.assertFalse(self.reload.signal("../pi-a", "round-1", "run-1", "gen-1"))
        self.assertFalse(self.reload.signal("", "round-1", "run-1", "gen-1"))

    def test_a_purge_drops_what_a_previous_run_left(self):
        self.reload.signal("pi-a", "round-1", "run-1", "gen-1")
        self.reload.signal("pi-a", "round-2", "run-1", "gen-1")
        self.reload.signal("pi-b", "round-3", "run-2", "gen-2")
        self.reload.purge()
        self.assertEqual(
            self.reload.await_consumed(["pi-a", "pi-b"],
                                       {"pi-a": "round-1",
                                        "pi-b": "round-3"}),
            ["pi-a", "pi-b"])

    def test_a_purge_drops_a_half_written_request(self):
        directory = self.reload.directory("pi-a")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "round-1.json.tmp"), "w",
                  encoding="utf-8") as handle:
            handle.write("{")
        self.reload.purge()
        self.assertEqual(os.listdir(directory), [])

    def test_a_signal_that_cannot_be_written_leaves_no_temporary(self):
        blocked = os.path.join(self.state, "not-a-directory")
        with open(blocked, "w", encoding="utf-8") as handle:
            handle.write("x")
        reload = SessionReload(os.path.join(blocked, "state"), grace=0.0,
                               poll=0.0)
        self.assertFalse(reload.signal("pi-a", "round-1", "run-1", "gen-1"))

    def test_the_poll_never_overshoots_the_grace(self):
        now = [0.0]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds

        reload = SessionReload(self.state, grace=0.05, poll=60.0,
                               clock=lambda: now[0], sleep=sleep)
        reload.signal("pi-a", "round-1", "run-1", "gen-1")
        reload.await_consumed(["pi-a"], {"pi-a": "round-1"})
        self.assertEqual(slept, [0.05], "one wait, capped at the grace")


class ReloadRoundTests(unittest.TestCase):
    def setUp(self):
        self.base = roots()
        self.store, self.config = store_for(self.base)
        reserve(self.store, "pi-idle", file="/conv/idle.jsonl")
        reserve(self.store, "pi-busy", file="/conv/busy.jsonl")
        busier, _ = reserve(self.store, "pi-waiting", file="/conv/wait.jsonl")
        self.store.mark_started(busier.id, os.getpid(),
                               ProcessIdentity.current(os.getpid()))
        self.store.publish("pi-busy", "busy")
        self.store.publish("pi-waiting", "waiting")
        headless, _ = reserve(self.store, "pi-headless")
        shell, _ = reserve(self.store, "pi-shell")
        self.store.finish(shell.id, "stop", 0)

    def service(self, reload, sender=None):
        return SessionService(self.store, sender or StubSender(), None,
                              self.config, None, None, reload)

    def test_an_idle_session_that_consumes_its_signal_is_reloaded(self):
        reload = StubReload(consumed=["pi-idle"])
        reply = self.service(reload).reload()
        self.assertEqual(reply["reloaded"], ["pi-idle"])
        self.assertNotIn({"id": "pi-idle", "reason": "not-idle"},
                         reply["skipped"])
        self.assertEqual(reload.signals, [("pi-idle", "tok")])
        record = self.store.get("pi-idle")
        self.assertEqual(reload.runs, [("pi-idle", record.id,
                                       record.generation)],
                         "the request names the run, not only the name")

    def test_a_busy_session_is_skipped_and_nothing_retries_it(self):
        reload = StubReload(consumed=["pi-idle"])
        reply = self.service(reload).reload()
        self.assertTrue(reply["deferred"])
        self.assertIn({"id": "pi-busy", "reason": "not-idle"},
                      reply["skipped"])
        self.assertIn({"id": "pi-waiting", "reason": "not-idle"},
                      reply["skipped"])
        self.assertEqual(reply["reloaded"], ["pi-idle"])
        self.assertEqual([name for name, _token in reload.signals],
                         ["pi-idle"])

    def test_a_session_without_a_conversation_is_not_reloadable(self):
        reply = self.service(StubReload(consumed=["pi-idle"])).reload()
        self.assertIn({"id": "pi-headless", "reason": "not-reloadable"},
                      reply["skipped"])

    def test_an_unconsumed_signal_falls_back_to_typing_the_command(self):
        reload = StubReload(claimed=True)
        sender = StubSender(store=self.store)
        reply = self.service(reload, sender).reload()
        self.assertEqual(reload.claimed, [("pi-idle", "tok")])
        self.assertEqual(sender.sent, [("pi-idle", b"/reload\r")])
        self.assertEqual(reply["reloaded"], ["pi-idle"])

    def test_a_request_consumed_at_the_deadline_is_not_typed_into(self):
        reload = StubReload(late_consumed=["pi-idle"])
        sender = StubSender(store=self.store)
        reply = self.service(reload, sender).reload()
        self.assertEqual(sender.sent, [])
        self.assertEqual(reply["reloaded"], ["pi-idle"])
        self.assertNotIn("pi-idle",
                         [entry["id"] for entry in reply["skipped"]])

    def test_a_session_that_went_busy_during_the_grace_is_not_typed_into(self):
        reload = StubReload(claimed=True)

        class Busying(StubSender):
            def __init__(self, store):
                super().__init__(store=store)

        sender = Busying(self.store)
        original = reload.await_consumed

        def await_and_busy(names, tokens):
            self.store.publish("pi-idle", "busy")
            return original(names, tokens)

        reload.await_consumed = await_and_busy
        reply = self.service(reload, sender).reload()
        self.assertEqual(sender.sent, [])
        self.assertIn({"id": "pi-idle", "reason": "not-idle"},
                      reply["skipped"])
        self.assertEqual(reply["reloaded"], [])
        self.assertTrue(reply["deferred"])

    def test_a_failed_fallback_is_reported_not_raised(self):
        reply = self.service(StubReload(claimed=True),
                             StubSender(["pi-idle"], store=self.store)).reload()
        self.assertEqual(reply["reloaded"], [])
        self.assertIn({"id": "pi-idle", "reason": "write-failed"},
                      reply["skipped"])

    def test_a_relay_that_never_answers_is_reported_not_raised(self):
        reply = self.service(StubReload(claimed=True),
                             StubSender(raising=["pi-idle"], store=self.store)).reload()
        self.assertEqual(reply["reloaded"], [])
        self.assertIn({"id": "pi-idle", "reason": "write-failed"},
                      reply["skipped"])

    def test_an_unwritable_signal_is_reported_and_never_typed_into(self):
        sender = StubSender(store=self.store)
        reply = self.service(StubReload(write_failed=["pi-idle"]),
                             sender).reload()
        self.assertEqual(sender.sent, [])
        self.assertIn({"id": "pi-idle", "reason": "write-failed"},
                      reply["skipped"])

    def test_a_named_round_touches_only_the_names_given(self):
        reserve(self.store, "pi-other", file="/conv/other.jsonl")
        reply = self.service(StubReload(consumed=["pi-other"])).reload(
            ["pi-other"])
        self.assertEqual(reply["reloaded"], ["pi-other"])
        self.assertEqual(reply["skipped"], [])
        self.assertFalse(reply["deferred"])

    def test_an_unknown_requested_name_is_answered_not_dropped(self):
        reply = self.service(StubReload()).reload(["pi-nowhere"])
        self.assertEqual(reply["reloaded"], [])
        self.assertEqual(reply["skipped"], [{"id": "pi-nowhere",
                                             "reason": "unknown"}])

    def test_two_rounds_never_overlap(self):
        import threading
        reload = StubReload(consumed=["pi-idle"])
        service = self.service(reload)
        first = threading.Thread(target=service.reload)
        second = threading.Thread(target=service.reload)
        first.start()
        first.join()
        second.start()
        second.join()
        self.assertFalse(reload.overlapped)
        self.assertEqual(reload.entries, 2)

    def test_a_round_that_holds_the_lock_keeps_the_other_out(self):
        import threading
        reload = StubReload(consumed=["pi-idle"])
        entered = threading.Event()
        release = threading.Event()
        original = reload.await_consumed

        failures = []

        def slow(names, tokens):
            entered.set()
            release.wait(2.0)
            return original(names, tokens)

        def round_():
            try:
                service.reload()
            except Exception as exc:  # a stub mismatch must fail the test
                failures.append(exc)

        reload.await_consumed = slow
        service = self.service(reload)
        first = threading.Thread(target=round_)
        second = threading.Thread(target=round_)
        first.start()
        self.assertTrue(entered.wait(2.0), "the first round is inside")
        second.start()
        release.set()
        first.join()
        second.join()
        self.assertEqual(failures, [])
        self.assertFalse(reload.overlapped)
        self.assertEqual(reload.entries, 2, "both rounds ran")


if __name__ == "__main__":
    unittest.main(verbosity=2)
