#!/usr/bin/env python3
"""The unified service: configuration, tasks, framing and the endpoint."""

import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "service"))

from daemon_service.application import DaemonApplication
from daemon_service.atomic_state_file import AtomicStateFile
from daemon_service.config import ServiceConfig
from daemon_service.lifecycle.instance_coordinator import InstanceCoordinator
from daemon_service.runtime_layout import RuntimeLayout
from daemon_service.tasks.runner import TicketRunner
from daemon_service.tasks.service import TaskService
from daemon_service.tasks.store import TicketStore
from daemon_service.transport.endpoint_file import EndpointFile
from daemon_service.transport.endpoint_probe import EndpointProbe
from daemon_service.transport.json_line_stream import JsonLineStream
from daemon_service.wire import WireCodec

SCRATCH = tempfile.mkdtemp(prefix="pi-service-")


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)


def roots():
    base = tempfile.mkdtemp(dir=SCRATCH)
    return os.path.join(base, "state"), os.path.join(base, "runtime")


def tasks_service(**overrides):
    state, runtime = roots()
    config = ServiceConfig(os.path.join(state, "x"), runtime, **overrides)
    layout = RuntimeLayout(state, runtime)
    store = TicketStore(layout.tickets_path(), layout.logs_dir(), config)
    store.load()
    runner = TicketRunner(store, config)
    return TaskService(store, runner), store, config


class ConfigTests(unittest.TestCase):
    def test_environment_beats_settings_beats_default(self):
        directory = tempfile.mkdtemp(dir=SCRATCH)
        settings = os.path.join(directory, "settings.json")
        with open(settings, "w", encoding="utf-8") as handle:
            json.dump({"daemon": {"record_cap": 9, "output_cap": 4096}}, handle)
        loaded = ServiceConfig.load(settings, environ={
            "PI_DAEMON_RECORD_CAP": "3"})
        self.assertEqual(loaded.record_cap, 3)
        self.assertEqual(loaded.output_cap, 4096)
        self.assertEqual(loaded.frame_bytes, ServiceConfig().frame_bytes)

    def test_invalid_values_fall_through_and_zero_is_kept(self):
        loaded = ServiceConfig.load(environ={
            "PI_DAEMON_RECORD_CAP": "many",
            "PI_DAEMON_OUTPUT_CAP": "inf",
            "PI_DAEMON_BACKLOG": "0",
        })
        self.assertEqual(loaded.record_cap, ServiceConfig().record_cap)
        self.assertEqual(loaded.output_cap, ServiceConfig().output_cap)
        self.assertEqual(loaded.backlog, 0)

    def test_fractions_and_booleans_are_refused(self):
        self.assertEqual(
            ServiceConfig.load(environ={"PI_DAEMON_RECORD_CAP": "2.5"}).record_cap,
            ServiceConfig().record_cap)
        with self.assertRaises(ValueError):
            ServiceConfig.load(overrides={"record_cap": True})
        with self.assertRaises(ValueError):
            ServiceConfig.load(overrides={"record_cap": "2.5"})

    def test_no_legacy_team_or_ptyd_names_are_honored(self):
        loaded = ServiceConfig.load(environ={
            "PI_PTYD_STATE_DIR": "/tmp/legacy", "PI_TEAMS_STATE_DIR": "/x"})
        self.assertEqual(loaded.state_dir, "")


class LayoutTests(unittest.TestCase):
    def test_roots_are_required_and_resolved_once(self):
        with self.assertRaises(ValueError):
            RuntimeLayout("", "/tmp/runtime")
        layout = RuntimeLayout("~/state-a", "/tmp/run-a")
        self.assertTrue(os.path.isabs(layout.state_root()))
        self.assertTrue(layout.endpoint_path().startswith(layout.runtime_root()))
        self.assertTrue(layout.tickets_path().startswith(layout.state_root()))

    def test_unsafe_domain_names_are_refused(self):
        layout = RuntimeLayout(tempfile.mkdtemp(dir=SCRATCH),
                               tempfile.mkdtemp(dir=SCRATCH))
        for name in ("", "..", "a/b"):
            with self.assertRaises(ValueError):
                layout.domain_root(name)


class AtomicFileTests(unittest.TestCase):
    def test_write_read_and_mode_at_creation(self):
        directory = tempfile.mkdtemp(dir=SCRATCH)
        path = os.path.join(directory, "state.json")
        state = AtomicStateFile(path)
        state.write_dict({"a": 1})
        self.assertEqual(state.read_dict(), {"a": 1})
        if os.name != "nt":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_replacement_failure_is_raised_and_temp_removed(self):
        directory = tempfile.mkdtemp(dir=SCRATCH)
        state = AtomicStateFile(os.path.join(directory, "state.json"))
        with mock.patch("os.replace", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                state.write("payload")
        self.assertEqual(os.listdir(directory), [])


class InstanceTests(unittest.TestCase):
    def test_one_owner_per_runtime_root(self):
        path = os.path.join(tempfile.mkdtemp(dir=SCRATCH), "instance.lock")
        first, second = InstanceCoordinator(path), InstanceCoordinator(path)
        first.acquire()
        with self.assertRaises(RuntimeError):
            second.acquire()
        first.release()
        second.acquire()
        second.release()


class WireTests(unittest.TestCase):
    def test_hello_uses_the_existing_contract(self):
        codec = WireCodec("secret", ["ticket-submit"])
        greeting = codec.greet({"op": "hello", "token": "secret"})
        self.assertEqual(greeting["srv"], "pi-pty-host")
        self.assertEqual(greeting["proto"], 1)
        self.assertEqual(greeting["commands"], ["ticket-submit"])
        with self.assertRaises(PermissionError):
            codec.greet({"op": "hello", "token": "wrong"})
        with self.assertRaises(PermissionError):
            codec.greet({"op": "ticket-list"})

    def test_frames_are_kept_across_one_packet(self):
        left, right = socket.socketpair()
        stream = JsonLineStream(left, 1024, 4)
        try:
            right.sendall(b'{"a": 1}\n{"b": 2}\n')
            self.assertEqual(json.loads(stream.read()), {"a": 1})
            self.assertEqual(json.loads(stream.read()), {"b": 2})
        finally:
            stream.close()
            right.close()

    def test_an_oversized_frame_is_refused(self):
        left, right = socket.socketpair()
        stream = JsonLineStream(left, 16, 8)
        try:
            right.sendall(b"x" * 32)
            with self.assertRaises(ValueError):
                stream.read()
        finally:
            stream.close()
            right.close()


class StoreTests(unittest.TestCase):
    def test_snapshots_are_detached(self):
        service, store, _ = tasks_service()
        snapshot = service.submit("pi-1", SCRATCH, "printf 'a'")
        snapshot["status"] = "tampered"
        self.assertEqual(store.snapshot_one(snapshot["ticket_id"])["status"],
                         "running")

    def test_a_running_match_is_replayed_not_duplicated(self):
        service, _, _ = tasks_service()
        first = service.submit("pi-1", SCRATCH, "sleep 1")
        second = service.submit("pi-1", SCRATCH, "sleep 1")
        self.assertEqual(first["ticket_id"], second["ticket_id"])
        service.cancel(first["ticket_id"])

    def test_ids_are_monotonic_across_a_reload(self):
        service, store, config = tasks_service()
        first = service.submit("pi-1", SCRATCH, "true")
        service.wait(first["ticket_id"], 5)
        reloaded = TicketStore(store.log_path(first["ticket_id"]).replace(
            "logs" + os.sep + first["ticket_id"] + ".log", "tickets.json"),
            os.path.dirname(store.log_path(first["ticket_id"])), config)
        reloaded.load()
        second = reloaded.reserve("pi-1", SCRATCH, "true", 0)[0]
        self.assertNotEqual(first["ticket_id"], second["ticket_id"])

    def test_running_records_become_interrupted_after_a_reload(self):
        service, store, config = tasks_service()
        ticket = service.submit("pi-1", SCRATCH, "sleep 2")
        reloaded = TicketStore(store.log_path(ticket["ticket_id"]).replace(
            "logs" + os.sep + ticket["ticket_id"] + ".log", "tickets.json"),
            os.path.dirname(store.log_path(ticket["ticket_id"])), config)
        reloaded.load()
        record = reloaded.snapshot_one(ticket["ticket_id"])
        self.assertEqual(record["status"], "failed")
        self.assertIn("interrupted", record["error"])
        service.cancel(ticket["ticket_id"])

    def test_finished_records_are_capped_with_their_logs(self):
        service, store, _ = tasks_service(record_cap=2)
        ids = []
        for _ in range(4):
            ticket = service.submit("s", SCRATCH, f"printf '{time.time()}'")
            service.wait(ticket["ticket_id"], 5)
            ids.append(ticket["ticket_id"])
        self.assertEqual(len(service.tickets()), 2)
        self.assertIsNone(store.snapshot_one(ids[0]))
        self.assertFalse(os.path.exists(store.log_path(ids[0])))

    def test_a_running_ticket_cannot_be_removed(self):
        service, _, _ = tasks_service()
        ticket = service.submit("pi-1", SCRATCH, "sleep 2")
        with self.assertRaises(RuntimeError):
            service.remove(ticket["ticket_id"])
        service.cancel(ticket["ticket_id"])


class RunnerTests(unittest.TestCase):
    def test_a_ticket_runs_and_keeps_its_output(self):
        service, _, _ = tasks_service()
        ticket = service.submit("pi-1", SCRATCH, "printf 'hello'")
        done = service.wait(ticket["ticket_id"], 10)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["exit_code"], 0)
        result = service.output(ticket["ticket_id"])
        self.assertEqual(result["output"], "hello")

    def test_a_failure_is_kept_not_lost(self):
        service, _, _ = tasks_service()
        ticket = service.submit("pi-1", SCRATCH, "exit 3")
        done = service.wait(ticket["ticket_id"], 10)
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["exit_code"], 3)

    def test_a_timeout_is_a_failure_that_says_timed_out(self):
        service, _, _ = tasks_service()
        ticket = service.submit("pi-1", SCRATCH, "sleep 5", 0.3)
        done = service.wait(ticket["ticket_id"], 10)
        self.assertEqual(done["status"], "failed")
        self.assertIn("timed out", done["error"])

    def test_cancel_stops_the_child(self):
        service, _, _ = tasks_service()
        ticket = service.submit("pi-1", SCRATCH, "sleep 5")
        cancelled = service.cancel(ticket["ticket_id"])
        self.assertEqual(cancelled["status"], "cancelled")

    def test_zero_timeout_polls_instead_of_waiting(self):
        service, _, _ = tasks_service()
        ticket = service.submit("pi-1", SCRATCH, "sleep 2")
        started = time.time()
        record = service.wait(ticket["ticket_id"], 0)
        self.assertLess(time.time() - started, 1.0)
        self.assertEqual(record["status"], "running")
        service.cancel(ticket["ticket_id"])

    def test_output_offsets_page_the_log(self):
        service, _, _ = tasks_service()
        ticket = service.submit("pi-1", SCRATCH, "printf 'abcdef'")
        service.wait(ticket["ticket_id"], 10)
        first = service.output(ticket["ticket_id"], 0)
        second = service.output(ticket["ticket_id"], 3)
        self.assertEqual(first["output"], "abcdef")
        self.assertEqual(second["output"], "def")
        self.assertEqual(second["offset"], 6)

    def test_a_missing_directory_fails_the_ticket(self):
        service, _, _ = tasks_service()
        ticket = service.submit("pi-1", os.path.join(SCRATCH, "absent"), "true")
        self.assertEqual(ticket["status"], "failed")


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = DaemonApplication.from_environment({
            "PI_DAEMON_STATE_DIR": roots()[0],
            "PI_DAEMON_RUNTIME_DIR": roots()[1],
        }).start()
        self.coordinates = EndpointFile(self.app.endpoint_path()).read()
        self.connection = None

    def tearDown(self):
        if self.connection is not None:
            self.connection.close()
        self.app.stop()

    def talk(self, request):
        if self.connection is None:
            self.connection = socket.create_connection(
                (self.coordinates["host"], self.coordinates["port"]), 2)
            self.stream = JsonLineStream(self.connection, 65536, 8192)
            self.stream.write({"op": "hello", "token": self.coordinates["token"]})
            self.greeting = json.loads(self.stream.read())
        self.stream.write(request)
        return json.loads(self.stream.read())

    def test_the_descriptor_names_its_instance_and_is_restricted(self):
        self.assertEqual(self.coordinates["instance"], self.app._instance)
        self.assertEqual(self.coordinates["srv"], "pi-pty-host")
        if os.name != "nt":
            mode = os.stat(self.app.endpoint_path()).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_a_ticket_round_trip_over_the_wire(self):
        greeting = self.talk({"op": "hello", "token": self.coordinates["token"]}) \
            if False else None
        submitted = self.talk({"op": "ticket-submit", "session": "pi-1",
                               "cwd": SCRATCH, "command": "printf 'wire'"})
        self.assertTrue(submitted["ok"])
        ticket_id = submitted["ticket"]["ticket_id"]
        waited = self.talk({"op": "ticket-wait", "ticket_id": ticket_id,
                            "timeout": 10})
        self.assertEqual(waited["ticket"]["status"], "done")
        output = self.talk({"op": "ticket-output", "ticket_id": ticket_id})
        self.assertEqual(output["output"], "wire")
        self.assertIsNone(greeting)

    def test_an_unknown_command_is_answered_not_fatal(self):
        self.talk({"op": "hello", "token": self.coordinates["token"]})
        reply = self.talk({"op": "ticket-nope"})
        self.assertFalse(reply["ok"])
        self.assertIn("unknown", reply["error"])

    def test_a_wrong_token_is_refused(self):
        connection = socket.create_connection(
            (self.coordinates["host"], self.coordinates["port"]), 2)
        stream = JsonLineStream(connection, 65536, 8192)
        try:
            stream.write({"op": "hello", "token": "wrong"})
            self.assertFalse(json.loads(stream.read())["ok"])
        finally:
            stream.close()

    def test_the_probe_finds_a_live_service(self):
        probe = EndpointProbe(2, 2, 65536, 8192)
        self.assertEqual(probe.probe(self.coordinates)["srv"], "pi-pty-host")
        self.assertIsNone(probe.probe({"host": "127.0.0.1", "port": 1,
                                       "token": "x"}))

    def test_a_second_instance_is_refused(self):
        other = DaemonApplication.from_environment({
            "PI_DAEMON_STATE_DIR": roots()[0],
            "PI_DAEMON_RUNTIME_DIR": os.path.dirname(self.app.endpoint_path()),
        })
        with self.assertRaises(RuntimeError):
            other.start()


class StopTests(unittest.TestCase):
    def test_stop_removes_the_descriptor_and_releases_ownership(self):
        state, runtime = roots()
        environment = {"PI_DAEMON_STATE_DIR": state,
                       "PI_DAEMON_RUNTIME_DIR": runtime}
        first = DaemonApplication.from_environment(environment).start()
        path = first.endpoint_path()
        file_path = EndpointFile(path)
        self.assertIsNotNone(file_path.read())
        first.stop()
        self.assertIsNone(file_path.read())
        second = DaemonApplication.from_environment(environment).start()
        second.stop()

    def test_a_foreign_descriptor_is_not_removed(self):
        state, runtime = roots()
        file_path = EndpointFile(os.path.join(runtime, "control.json"))
        os.makedirs(runtime, exist_ok=True)
        file_path.publish({"instance": "other", "host": "h", "port": 1})
        self.assertFalse(file_path.remove_owned("mine"))
        self.assertIsNotNone(file_path.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
