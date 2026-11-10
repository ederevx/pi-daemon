#!/usr/bin/env python3
"""The migration: one legacy root translated into the new layout, once."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "lib"))

import isolation

# Every root this module probes belongs to its own scratch tree, never
# the host's live daemon.
ISOLATION = isolation.ScratchEnvironment()
ISOLATION.apply()

from pi_daemon.config import ServiceConfig
from pi_daemon.lifecycle.instance_coordinator import InstanceCoordinator
from pi_daemon.migration.error import MigrationError
from pi_daemon.migration.legacy_root import LegacyRoot
from pi_daemon.migration.migration import Migration
from pi_daemon.runtime_layout import RuntimeLayout
from pi_daemon.sessions.store import SessionStore
from pi_daemon.tasks.store import TicketStore

ENTRY = os.path.join(REPO, "pi", "bin", "pi-daemon-migrate")
SCRATCH = tempfile.mkdtemp(prefix="pi-migration-")


def tearDownModule():
    ISOLATION.cleanup()
    shutil.rmtree(SCRATCH, ignore_errors=True)


def snapshot(root):
    """Every path under a tree with its bytes and mtime: write evidence."""
    seen = {}
    for directory, _dirs, files in os.walk(root):
        for name in sorted(files):
            path = os.path.join(directory, name)
            stat = os.stat(path)
            with open(path, "rb") as handle:
                seen[os.path.relpath(path, root)] = (handle.read(),
                                                     stat.st_mtime_ns)
    return seen


class LegacyFixture:
    """One legacy state root in the shape the old daemon wrote it."""

    def __init__(self, base, log_bytes=b"hello log\n"):
        self.base = base
        self.state_home = os.path.join(base, "xdg")
        self.legacy = os.path.join(self.state_home, "pi-pty-host")
        self.state = os.path.join(base, "state")
        self.runtime = os.path.join(base, "run")
        os.makedirs(os.path.join(self.legacy, "tickets"))
        os.makedirs(os.path.join(self.legacy, "run-state"))
        os.makedirs(os.path.join(self.legacy, "gc-reap"))
        self.write("tickets.json", {
            "next": 14,
            "tickets": {
                "t-aa-12": self.ticket("t-aa-12", "done", 100.0, exit=0),
                "t-aa-13": self.ticket("t-aa-13", "lost", 110.0,
                                       error="interrupted by daemon restart"),
                "t-aa-14": self.ticket("t-aa-14", "running", 120.0),
                "t-aa-9": {"id": "t-aa-9", "kind": "agent", "status": "done",
                           "created": 90.0},
            },
        })
        self.write_bytes("tickets/t-aa-12.log", log_bytes)
        self.write_bytes("tickets/t-aa-13.log", b"lost log\n")
        self.write("sessions.json", {
            "pi-a": {"dir": "/tmp", "argv": ["pi", "--session", "/tmp/x.jsonl"]},
            "pi-bad": {"dir": 7},
        })
        self.write("run-state/pi-a.json", {
            "v": 1, "boot": "b1", "owner_pid": 1, "owner_token": "t",
            "child_pid": None, "file": "/tmp/x.jsonl", "state": "busy",
            "aborted": False, "run": 2, "seq": 5, "seen": True,
            "changed": 130.0, "opened": 100.0, "open": True, "ended": None,
            "history": [{"verdict": "settled", "cause": None, "signal": False,
                         "exit": 0, "state": None, "at": 90.0}],
        })
        self.write("gc-reap/pi-a.json", {})
        self.write("session-files.json", {})

    @staticmethod
    def ticket(ticket_id, status, created, exit=None, error=None):
        return {"id": ticket_id, "kind": "shell", "session": "pi-a",
                "cwd": "/tmp", "command": "true", "status": status,
                "created": created, "started": created + 1,
                "finished": created + 2, "exit": exit, "term": None,
                "truncated": False, "error": error, "timeout": None}

    def path(self, relative):
        return os.path.join(self.legacy, relative)

    def write(self, relative, value):
        self.write_bytes(relative, json.dumps(value).encode("utf-8"))

    def write_bytes(self, relative, data):
        with open(self.path(relative), "wb") as handle:
            handle.write(data)

    def environment(self, **extra):
        environment = dict(os.environ)
        environment.update({
            "PI_DAEMON_STATE_DIR": self.state,
            "PI_DAEMON_RUNTIME_DIR": self.runtime,
            "XDG_STATE_HOME": self.state_home,
        })
        environment.update({key: str(value) for key, value in extra.items()})
        return environment

    def run(self, *args, **extra):
        completed = subprocess.run(
            [sys.executable, ENTRY] + list(args),
            env=self.environment(**extra), capture_output=True, text=True)
        plan = json.loads(completed.stdout) if completed.stdout.strip() else None
        return completed.returncode, plan, completed.stderr

    def layout(self):
        return RuntimeLayout(self.state, self.runtime)

    def config(self):
        return ServiceConfig()


class MovingLegacy(LegacyRoot):
    """A legacy root the old daemon writes to during a translation."""

    def verify(self, expected):
        return ["tickets.json"]


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LegacyFixture(tempfile.mkdtemp(dir=SCRATCH))

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        before = snapshot(self.fixture.legacy)
        code, plan, _ = self.fixture.run("--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["action"], "translate")
        self.assertEqual(sorted(plan["sources"]), [
            "run-state/pi-a.json", "sessions.json", "tickets.json",
            "tickets/t-aa-12.log", "tickets/t-aa-13.log"])
        self.assertEqual(snapshot(self.fixture.legacy), before)
        self.assertFalse(os.path.exists(self.fixture.state))
        self.assertFalse(os.path.exists(self.fixture.runtime))

    def test_the_plan_names_what_it_would_translate_and_drop(self):
        _code, plan, _ = self.fixture.run("--dry-run")
        self.assertEqual([(item["id"], item["status"], item["log_bytes"])
                          for item in plan["tickets"]],
                         [("t-aa-12", "done", 10),
                          ("t-aa-13", "failed", 9),
                          ("t-aa-14", "running", 0)])
        self.assertEqual(sorted(item["source"] for item in plan["dropped"]),
                         ["gc-reap/pi-a.json", "pi-bad",
                          "session-files.json", "t-aa-9"])
        session = [item for item in plan["sessions"]
                   if item["name"] == "pi-a"][0]
        self.assertEqual(session["desired"], "running")
        self.assertEqual(session["file"], "/tmp/x.jsonl")
        self.assertEqual(session["ended"], "lost-mid-run")
        self.assertTrue(session["sealed"])


class TranslateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LegacyFixture(tempfile.mkdtemp(dir=SCRATCH))

    def test_translation_writes_through_the_owners_and_records_a_marker(self):
        before = snapshot(self.fixture.legacy)
        code, plan, stderr = self.fixture.run(PI_DAEMON_OUTPUT_CAP=32)
        self.assertEqual((code, plan["action"], stderr), (0, "translate", ""))
        marker_path = os.path.join(self.fixture.state, "migration.json")
        with open(marker_path, encoding="utf-8") as handle:
            marker = json.load(handle)
        self.assertEqual(marker["version"], 1)
        self.assertEqual(marker["legacy_root"], self.fixture.legacy)
        self.assertFalse(marker["forced"])
        self.assertEqual(marker["sources"], plan["sources"])
        for relative, digest in marker["sources"].items():
            with open(self.fixture.path(relative), "rb") as handle:
                self.assertEqual(hashlib.sha256(handle.read()).hexdigest(),
                                 digest)
        self.assertEqual(snapshot(self.fixture.legacy), before)

    def test_the_ticket_store_keeps_ids_and_appends_the_capped_log(self):
        self.fixture.run(PI_DAEMON_OUTPUT_CAP=32)
        layout = self.fixture.layout()
        store = TicketStore(layout.tickets_path(), layout.logs_dir(),
                            self.fixture.config())
        store.load()
        self.assertEqual([ticket["id"] for ticket in store.snapshot()],
                         ["t-aa-12", "t-aa-13", "t-aa-14"])
        lost = store.snapshot_one("t-aa-13")
        self.assertEqual(lost["status"], "failed")
        self.assertIn("interrupted", lost["error"])
        self.assertTrue(store.snapshot_one("t-aa-14")["status"] != "running")
        data, _offset, _next, size = store.output("t-aa-12")
        self.assertEqual(data, b"hello log\n")
        self.assertEqual(size, 10)
        oversized, _offset, _next, capped = store.output("t-aa-13")
        self.assertEqual(capped, 9)
        self.assertEqual(oversized, b"lost log\n")

    def test_the_translated_log_is_capped_by_configuration(self):
        self.fixture.write_bytes("tickets/t-aa-13.log", b"x" * 100)
        code, _plan, _ = self.fixture.run(PI_DAEMON_OUTPUT_CAP=32)
        self.assertEqual(code, 0)
        layout = self.fixture.layout()
        store = TicketStore(layout.tickets_path(), layout.logs_dir(),
                            self.fixture.config())
        store.load()
        ticket = store.snapshot_one("t-aa-13")
        self.assertEqual(ticket["output_bytes"], 32)
        self.assertTrue(ticket["truncated"])

    def test_the_session_store_carries_the_run_verdict(self):
        self.fixture.run()
        layout = self.fixture.layout()
        store = SessionStore(layout.sessions_path(), layout.run_state_dir(),
                             self.fixture.config())
        store.load()
        state = store.run_state("pi-a")
        self.assertEqual(state["ended"]["verdict"], "lost-mid-run")
        self.assertEqual(state["ended"]["cause"], "lost")
        self.assertIsNone(state["ended"]["signal"])
        self.assertEqual(len(state["history"]), 1)
        self.assertFalse(state["open"])
        self.assertEqual(state["seq"], 5)
        record = store.get("pi-a")
        self.assertEqual((record.desired, record.file), ("running",
                                                         "/tmp/x.jsonl"))
        self.assertEqual(store.resolve("pi-bad"), "pi-bad")

    def test_the_plan_freezes_the_sources_it_read(self):
        legacy = LegacyRoot(self.fixture.legacy)
        plan = Migration(self.fixture.layout(), self.fixture.config(),
                         legacy).preview(False)
        read = plan.sources()
        self.assertEqual(sorted(read), [
            "run-state/pi-a.json", "sessions.json", "tickets.json",
            "tickets/t-aa-12.log", "tickets/t-aa-13.log"])
        # A writer at work after the read must not rewrite the plan's own
        # account of what it read.
        self.fixture.write_bytes("tickets/t-aa-13.log", b"rewritten\n")
        os.remove(self.fixture.path("sessions.json"))
        self.assertEqual(plan.sources(), read)
        self.assertEqual(legacy.verify(read),
                         ["sessions.json", "tickets/t-aa-13.log"])
        self.assertEqual(legacy.digests(), read)

    def test_a_translated_root_is_current_on_a_rerun(self):
        code, _plan, _ = self.fixture.run()
        self.assertEqual(code, 0)
        marker_path = os.path.join(self.fixture.state, "migration.json")
        store_path = os.path.join(self.fixture.state, "tasks", "tickets.json")
        marker_before = snapshot(self.fixture.state)
        code, plan, stderr = self.fixture.run()
        self.assertEqual((code, plan["action"], plan["changed"], stderr),
                         (0, "current", [], ""))
        self.assertEqual(snapshot(self.fixture.state), marker_before)
        self.assertTrue(os.path.exists(marker_path))
        self.assertTrue(os.path.exists(store_path))


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LegacyFixture(tempfile.mkdtemp(dir=SCRATCH))

    def test_apply_refuses_when_the_legacy_moves_during_translation(self):
        before = snapshot(self.fixture.legacy)
        with self.assertRaises(MigrationError) as raised:
            Migration(self.fixture.layout(), self.fixture.config(),
                      MovingLegacy(self.fixture.legacy)).apply(False)
        self.assertEqual(raised.exception.code, 4)
        self.assertIn("during translation", raised.exception.message)
        self.assertFalse(os.path.exists(
            os.path.join(self.fixture.state, "migration.json")))
        self.assertEqual(snapshot(self.fixture.legacy), before)

    def test_moved_sources_refuse_until_forced(self):
        self.assertEqual(self.fixture.run()[0], 0)
        self.fixture.write("tickets.json", {"next": 2, "tickets": {}})
        translated = snapshot(self.fixture.state)
        code, plan, stderr = self.fixture.run()
        self.assertEqual(code, 4)
        # The logs of removed tickets are no longer read, so their
        # digests leave the sources too.
        self.assertEqual(plan["refusal"], "sources-changed")
        self.assertEqual(plan["changed"], [
            "tickets.json", "tickets/t-aa-12.log", "tickets/t-aa-13.log"])
        self.assertIn("--force", stderr)
        self.assertEqual(snapshot(self.fixture.state), translated)
        dry_code, dry_plan, _ = self.fixture.run("--dry-run")
        self.assertEqual((dry_code, dry_plan["refusal"]),
                         (4, "sources-changed"))
        self.assertEqual(snapshot(self.fixture.state), translated)

    def test_force_replaces_the_translated_stores_only(self):
        self.assertEqual(self.fixture.run()[0], 0)
        comms = os.path.join(self.fixture.state, "comms")
        os.makedirs(comms)
        with open(os.path.join(comms, "node.json"), "w",
                  encoding="utf-8") as handle:
            handle.write('{"node": "n"}')
        self.fixture.write("tickets.json", {
            "next": 15,
            "tickets": {"t-aa-12": self.fixture.ticket("t-aa-12", "done",
                                                       100.0, exit=0)},
        })
        code, plan, _ = self.fixture.run("--force")
        self.assertEqual((code, plan["action"], plan["force"]),
                         (0, "translate", True))
        with open(os.path.join(self.fixture.state, "migration.json"),
                  encoding="utf-8") as handle:
            marker = json.load(handle)
        self.assertTrue(marker["forced"])
        with open(os.path.join(self.fixture.state, "tasks", "tickets.json"),
                  encoding="utf-8") as handle:
            store = json.load(handle)
        self.assertEqual([ticket["id"] for ticket in store["tickets"]],
                         ["t-aa-12"])
        self.assertFalse(os.path.exists(os.path.join(
            self.fixture.state, "tasks", "logs", "t-aa-13.log")))
        with open(os.path.join(comms, "node.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"node": "n"}')

    def test_a_held_instance_lock_refuses_outright(self):
        layout = self.fixture.layout()
        holder = InstanceCoordinator(layout.instance_lock_path())
        holder.acquire()
        try:
            for args in ((), ("--force",), ("--dry-run",)):
                code, plan, stderr = self.fixture.run(*args)
                self.assertEqual(code, 3)
                self.assertEqual(plan["refusal"], "instance-locked")
                self.assertIn("instance lock", stderr)
            self.assertEqual(os.path.exists(self.fixture.state), False)
        finally:
            holder.release()

    def test_a_held_lock_still_reports_what_the_run_would_do(self):
        holder = InstanceCoordinator(self.fixture.layout().instance_lock_path())
        holder.acquire()
        try:
            code, plan, _ = self.fixture.run("--dry-run")
            self.assertEqual((code, plan["action"], plan["intent"]),
                             (3, "refuse", "translate"))
        finally:
            holder.release()
        self.assertEqual(self.fixture.run()[0], 0)
        holder.acquire()
        try:
            code, plan, _ = self.fixture.run("--dry-run")
            self.assertEqual((code, plan["refusal"], plan["intent"],
                              plan["changed"]),
                             (3, "instance-locked", "current", []))
        finally:
            holder.release()

    def test_untranslated_state_refuses_until_forced(self):
        os.makedirs(os.path.join(self.fixture.state, "tasks"))
        with open(os.path.join(self.fixture.state, "tasks", "tickets.json"),
                  "w", encoding="utf-8") as handle:
            handle.write('{"next": 0, "order": [], "tickets": []}')
        code, plan, stderr = self.fixture.run()
        self.assertEqual(code, 5)
        self.assertEqual(plan["refusal"], "unmarked-state")
        self.assertIn("--force", stderr)
        self.assertEqual(self.fixture.run("--force")[0], 0)

    def test_an_unreadable_legacy_file_refuses_without_a_marker(self):
        self.fixture.write_bytes("sessions.json", b"{not json")
        code, plan, stderr = self.fixture.run()
        self.assertEqual((code, plan), (1, None))
        self.assertIn("sessions.json: unreadable", stderr)
        self.assertFalse(os.path.exists(os.path.join(self.fixture.state,
                                                     "migration.json")))

    def test_an_absent_legacy_root_records_an_empty_marker(self):
        shutil.rmtree(self.fixture.legacy)
        code, plan, _ = self.fixture.run()
        self.assertEqual((code, plan["action"], plan["sources"]),
                         (0, "translate", {}))
        with open(os.path.join(self.fixture.state, "migration.json"),
                  encoding="utf-8") as handle:
            marker = json.load(handle)
        self.assertEqual(marker["sources"], {})

    def test_both_roots_are_required(self):
        environment = dict(os.environ)
        environment.pop("PI_DAEMON_STATE_DIR", None)
        environment["PI_DAEMON_RUNTIME_DIR"] = self.fixture.runtime
        completed = subprocess.run([sys.executable, ENTRY, "migrate"],
                                   env=environment, capture_output=True,
                                   text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("PI_DAEMON_STATE_DIR", completed.stderr)


class LegacyRootResolutionTests(unittest.TestCase):
    """The legacy root is the one the old daemon actually wrote.

    The old daemon resolved its state home through pi_platform, so on a
    Windows host that root is %LOCALAPPDATA%\\pi-pty-host, not the POSIX
    default no Windows daemon ever wrote.
    """

    def test_the_legacy_root_is_the_old_daemons_registry_dir(self):
        environ = {}
        if sys.platform.startswith("win"):
            local = tempfile.mkdtemp(dir=SCRATCH)
            environ["LOCALAPPDATA"] = local
            expected = os.path.join(local, "pi-pty-host")
        else:
            expected = os.path.expanduser("~/.local/state/pi-pty-host")
        self.assertEqual(LegacyRoot.from_environment(environ).root(),
                         os.path.abspath(expected))

    def test_an_explicit_state_home_wins_on_every_platform(self):
        home = tempfile.mkdtemp(dir=SCRATCH)
        root = LegacyRoot.from_environment({"XDG_STATE_HOME": home})
        self.assertEqual(root.root(),
                         os.path.abspath(os.path.join(home, "pi-pty-host")))


class LongPathTests(unittest.TestCase):
    """A legacy root whose own path is past the classic Windows limit.

    The translated layout is used where it is given, so a root the host
    serves only with long paths enabled still translates. Where the host
    policy refuses the path, the prerequisite is blocked: the property is
    untested here, not unsupported, and the path is never shortened to
    make the case fit.
    """

    def test_a_legacy_root_past_the_legacy_path_limit_translates(self):
        deep = SCRATCH
        while len(os.path.join(deep, "xdg", "pi-pty-host")) <= 300:
            deep = os.path.join(deep, "deepening-%02d" % (len(deep) % 100,))
        try:
            os.makedirs(deep)
        except OSError as failure:
            self.skipTest("blocked: this host refuses a path past the "
                          "legacy limit: %s" % failure)
        fixture = LegacyFixture(deep)
        self.assertGreater(len(fixture.legacy), 260)
        code, plan, stderr = fixture.run()
        self.assertEqual((code, plan["action"], stderr), (0, "translate", ""))
        layout = fixture.layout()
        store = TicketStore(layout.tickets_path(), layout.logs_dir(),
                            fixture.config())
        store.load()
        self.assertEqual([ticket["id"] for ticket in store.snapshot()],
                         ["t-aa-12", "t-aa-13", "t-aa-14"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
