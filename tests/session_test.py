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
sys.path.insert(0, os.path.join(REPO, "pi", "service"))

from daemon_service.config import ServiceConfig
from daemon_service.sessions.error import SessionError
from daemon_service.sessions.identity import ProcessIdentity
from daemon_service.sessions.model import SessionRecord
from daemon_service.sessions.operations import SessionOperations
from daemon_service.sessions.pty_backend import PosixPtyBackend, service_root
from daemon_service.sessions.relay import SessionRelay
from daemon_service.sessions.runner import SessionRunner
from daemon_service.sessions.run_outcome import RunOutcome
from daemon_service.sessions.store import SessionStore
from daemon_service.transport.control_action import ControlAction

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

    def test_an_attach_with_a_handoff_never_upgrades(self):
        service = StubService()
        service.attach_plan = lambda name, cols, rows: {
            "name": name, "state": "busy", "file": None,
            "handoff": {"name": "pi-b", "state": "busy"}}
        reply = self.operations(service).attach(
            {"cmd": "attach", "name": "pi-a"}, None)
        self.assertEqual(reply["handoff"]["name"], "pi-b")
        self.assertNotIsInstance(reply, ControlAction)

    def test_an_unknown_run_state_is_refused(self):
        with self.assertRaises(ValueError):
            self.operations().state({"cmd": "state", "name": "pi-a",
                                     "state": "sleeping"})

    def test_a_shutdown_answers_before_it_stops(self):
        action = self.operations().shutdown({"cmd": "shutdown"})
        self.assertFalse(self.stopped)
        action.after_send()
        self.assertEqual(self.stopped, [("shutdown",)])

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

    def spawn_second(self, script="sleep 5"):
        second = self.backend.spawn(["sh", "-c", script], REPO,
                                    dict(os.environ), 80, 24)
        second.release()
        self.addCleanup(lambda: (second.terminate(grace=0.2), second.close()))
        self.relay.register("k2", "g2", second.disown_master())
        return "k2"


class DaemonTests(unittest.TestCase):
    """The staging daemon, driven over its own control contract."""

    def setUp(self):
        self.state, self.runtime = roots()
        self.environment = dict(os.environ)
        self.environment.update({
            "PI_DAEMON_STATE_DIR": self.state,
            "PI_DAEMON_RUNTIME_DIR": self.runtime,
        })
        self.process = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "pi", "service", "pi-daemon")],
            env=self.environment, cwd=REPO)
        self.addCleanup(self._stop)
        self.endpoint = self._endpoint()

    def _stop(self):
        if self.process.poll() is None:
            try:
                self.control({"cmd": "shutdown"})
            except OSError:
                pass
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(5)

    def _endpoint(self):
        path = os.path.join(self.runtime, "control.json")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if data.get("token"):
                    return data
            except (OSError, ValueError):
                time.sleep(0.05)
        self.fail("the daemon never published its endpoint")

    def connect(self):
        connection = socket.create_connection(
            (self.endpoint["host"], self.endpoint["port"]), timeout=10)
        connection.sendall(json.dumps(
            {"cmd": "hello", "token": self.endpoint["token"]}).encode()
            + b"\n")
        self.read_frame(connection)
        return connection

    @staticmethod
    def read_frame(connection):
        buffer = b""
        while b"\n" not in buffer:
            chunk = connection.recv(65536)
            if not chunk:
                raise ConnectionError("the daemon closed the connection")
            buffer += chunk
        return json.loads(buffer.split(b"\n", 1)[0])

    def control(self, request):
        connection = self.connect()
        try:
            connection.sendall(json.dumps(request).encode() + b"\n")
            return self.read_frame(connection)
        finally:
            connection.close()

    def attach(self, name, cols=100, rows=30):
        connection = self.connect()
        connection.sendall(json.dumps({
            "cmd": "attach", "name": name, "cols": cols, "rows": rows,
        }).encode() + b"\n")
        reply = self.read_frame(connection)
        return connection, reply

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        return None

    def read_until(self, connection, marker, timeout=10):
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

    def test_a_hosted_session_runs_announces_and_settles(self):
        started = self.control({
            "cmd": "start", "name": "pi-live", "dir": REPO,
            "argv": ["sh", "-c",
                     "printf 'READY\\n'; read line; printf 'GOT:%s\\n' \"$line\""],
            "cols": 100, "rows": 30})
        self.assertTrue(started["ok"], started)
        self.assertTrue(started["created"])
        session, reply = self.attach("pi-live")
        self.assertTrue(reply["ok"], reply)
        self.assertIn(b"READY", self.read_until(session, b"READY"))
        announce = self.control({"cmd": "announce", "name": "pi-live",
                                 "file": "/conv/a.jsonl"})
        self.assertTrue(announce["ok"], announce)
        self.assertEqual(self.control({"cmd": "which", "file": "/conv/a.jsonl"}),
                         {"ok": True, "name": "pi-live"})
        self.assertEqual(self.control({"cmd": "whichall",
                                       "file": "/conv/a.jsonl"})["names"],
                         [{"name": "pi-live", "state": "idle"}])
        self.assertEqual(self.control({"cmd": "state", "name": "pi-live",
                                       "state": "busy"})["accepted"], True)
        listed = self.control({"cmd": "list"})["sessions"]
        self.assertEqual(listed[0]["state"], "busy")
        self.assertTrue(listed[0]["attached"])
        self.assertEqual(listed[0]["clients"], 1)
        typed = self.control({"cmd": "input", "name": "pi-live",
                              "text": "hello\n"})
        self.assertEqual(typed["bytes"], 6)
        self.assertIn(b"GOT:hello", self.read_until(session, b"GOT:hello"))
        self.assertTrue(self.wait_for(
            lambda: self.control({"cmd": "run-state", "name": "pi-live"})
            ["record"]["ended"]))
        state = self.control({"cmd": "run-state", "name": "pi-live"})
        self.assertEqual(state["record"]["ended"]["verdict"], "exited-mid-run")
        session.close()

    def test_a_shutdown_seals_the_runs_it_ends(self):
        self.control({"cmd": "start", "name": "pi-live", "dir": REPO,
                      "argv": ["sh", "-c", "sleep 30"]})
        self.control({"cmd": "announce", "name": "pi-live",
                      "file": "/conv/a.jsonl"})
        self.control({"cmd": "state", "name": "pi-live", "state": "busy"})
        self.control({"cmd": "shutdown"})
        self.process.wait(20)
        runs = os.path.join(self.state, "sessions", "runs")
        sealed = []
        for name in os.listdir(runs):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(runs, name), encoding="utf-8") as handle:
                sealed.append(json.load(handle))
        self.assertEqual(len(sealed), 1, sealed)
        self.assertEqual(sealed[0]["ended"]["verdict"], "daemon-stopped")
        self.assertFalse(sealed[0]["open"])

    def test_an_attach_failure_names_the_recorded_conversation(self):
        self.control({"cmd": "start", "name": "pi-gone", "dir": REPO,
                      "argv": ["sh", "-c", "sleep 30"]})
        self.control({"cmd": "announce", "name": "pi-gone",
                      "file": "/conv/gone.jsonl"})
        self.control({"cmd": "stop", "name": "pi-gone"})
        self.assertTrue(self.wait_for(
            lambda: not self.control({"cmd": "list"})["sessions"]))
        connection, reply = self.attach("pi-gone")
        connection.close()
        self.assertEqual(reply["error"], "no-session")
        self.assertEqual(reply["file"], "/conv/gone.jsonl")

    def test_detach_keeps_the_session_running(self):
        self.control({"cmd": "start", "name": "pi-live", "dir": REPO,
                      "argv": ["sh", "-c", "sleep 30"]})
        session, reply = self.attach("pi-live")
        self.assertTrue(reply["ok"], reply)
        self.control({"cmd": "detach", "name": "pi-live"})
        session.settimeout(10)
        self.assertEqual(session.recv(65536), b"")
        session.close()
        listed = self.control({"cmd": "list"})["sessions"]
        self.assertEqual(listed[0]["clients"], 0)
        self.control({"cmd": "stop", "name": "pi-live"})
        self.assertTrue(self.wait_for(
            lambda: not self.control({"cmd": "list"})["sessions"]))

    def test_an_announce_takeover_hands_the_duplicate_over(self):
        for name in ("pi-a", "pi-b"):
            self.control({"cmd": "start", "name": name, "dir": REPO,
                          "argv": ["sh", "-c", "sleep 30"]})
        first, _ = self.attach("pi-a")
        second, _ = self.attach("pi-b")
        self.control({"cmd": "announce", "name": "pi-a",
                      "file": "/conv/dup.jsonl"})
        reply = self.control({"cmd": "announce", "name": "pi-b",
                              "file": "/conv/dup.jsonl", "takeover": True})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["handoff"], {"name": "pi-a", "state": "idle"})
        self.assertEqual(self.control({"cmd": "handoff-target",
                                       "name": "pi-b"})["target"],
                         {"name": "pi-a", "state": "idle"})
        self.assertEqual(self.control({"cmd": "list"})["sessions"][0]["clients"],
                         2)
        first.close()
        second.close()

    def test_unknown_and_malformed_requests_are_refused_not_fatal(self):
        self.assertEqual(self.control({"cmd": "nope"})["error"], "bad-request")
        self.assertEqual(self.control({"cmd": "start", "name": ""})["error"],
                         "bad-request")
        self.assertTrue(self.control({"cmd": "list"})["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
