#!/usr/bin/env python3
"""The unified service: configuration, tasks, framing and the endpoint."""

import base64
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
from daemon_service.tasks.error import TaskError
from daemon_service.tasks.runner import TicketRunner
from daemon_service.tasks.service import TaskService
from daemon_service.tasks.store import TicketStore
from daemon_service.transport.control_handshake import ControlHandshake
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
    def test_hello_uses_the_published_contract(self):
        handshake = ControlHandshake("secret")
        greeting = handshake.authorize({"cmd": "hello", "token": "secret"})
        self.assertEqual(greeting["srv"], "pi-daemon")
        self.assertEqual(greeting["proto"], 1)
        self.assertEqual(greeting["caps"], [])
        self.assertEqual(greeting["index"], 1)
        self.assertEqual(set(greeting),
                         {"ok", "srv", "proto", "caps", "index"})
        with self.assertRaises(PermissionError):
            handshake.authorize({"cmd": "hello", "token": "wrong"})
        with self.assertRaises(PermissionError):
            handshake.authorize({"op": "hello", "token": "secret"})

    def test_the_codec_encodes_and_decodes_one_frame(self):
        codec = WireCodec()
        frame = codec.encode(codec.hello_request("secret"))
        self.assertTrue(frame.endswith(b"\n"))
        self.assertEqual(codec.decode(frame),
                         {"cmd": "hello", "token": "secret"})
        with self.assertRaises(ValueError):
            codec.decode(b"not json")

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
        snapshot, _ = service.submit("pi-1", SCRATCH, "printf 'a'")
        snapshot["status"] = "tampered"
        self.assertEqual(store.snapshot_one(snapshot["id"])["status"],
                         "running")

    def test_a_running_match_is_replayed_not_duplicated(self):
        service, _, _ = tasks_service()
        first, _ = service.submit("pi-1", SCRATCH, "sleep 1")
        second, _ = service.submit("pi-1", SCRATCH, "sleep 1")
        self.assertEqual(first["id"], second["id"])
        service.cancel(first["id"])

    def test_ids_are_monotonic_across_a_reload(self):
        service, store, config = tasks_service()
        first, _ = service.submit("pi-1", SCRATCH, "true")
        service.wait(first["id"], 5)
        reloaded = TicketStore(store.log_path(first["id"]).replace(
            "logs" + os.sep + first["id"] + ".log", "tickets.json"),
            os.path.dirname(store.log_path(first["id"])), config)
        reloaded.load()
        second = reloaded.reserve("pi-1", SCRATCH, "true", 0, 32)[0]
        self.assertNotEqual(first["id"], second["id"])

    def test_running_records_become_interrupted_after_a_reload(self):
        service, store, config = tasks_service()
        ticket, _ = service.submit("pi-1", SCRATCH, "sleep 2")
        reloaded = TicketStore(store.log_path(ticket["id"]).replace(
            "logs" + os.sep + ticket["id"] + ".log", "tickets.json"),
            os.path.dirname(store.log_path(ticket["id"])), config)
        reloaded.load()
        record = reloaded.snapshot_one(ticket["id"])
        self.assertEqual(record["status"], "failed")
        self.assertIn("interrupted", record["error"])
        service.cancel(ticket["id"])

    def test_finished_records_are_capped_with_their_logs(self):
        service, store, _ = tasks_service(record_cap=2)
        ids = []
        for _ in range(4):
            ticket, _ = service.submit("s", SCRATCH, f"printf '{time.time()}'")
            service.wait(ticket["id"], 5)
            ids.append(ticket["id"])
        self.assertEqual(len(service.tickets()), 2)
        self.assertIsNone(store.snapshot_one(ids[0]))
        self.assertFalse(os.path.exists(store.log_path(ids[0])))

    def test_a_running_ticket_cannot_be_removed(self):
        service, _, _ = tasks_service()
        ticket, _ = service.submit("pi-1", SCRATCH, "sleep 2")
        with self.assertRaises(TaskError):
            service.remove(ticket["id"])
        service.cancel(ticket["id"])


class RunnerTests(unittest.TestCase):
    def test_a_ticket_runs_and_keeps_its_output(self):
        service, _, _ = tasks_service()
        ticket, _ = service.submit("pi-1", SCRATCH, "printf 'hello'")
        done = service.wait(ticket["id"], 10)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["exit"], 0)
        result = service.output(ticket["id"])
        self.assertEqual(result["data"], b"hello")
        self.assertEqual(result["size"], 5)

    def test_capacity_is_decided_after_the_replay_guard(self):
        service, _, _ = tasks_service(max_running=1)
        first, _ = service.submit("pi-1", SCRATCH, "sleep 30")
        replay, reused = service.submit("pi-1", SCRATCH, "sleep 30")
        self.assertTrue(reused)
        self.assertEqual(replay["id"], first["id"])
        with self.assertRaises(TaskError) as caught:
            service.submit("pi-1", SCRATCH, "sleep 31")
        self.assertEqual(caught.exception.code, "too-busy")
        service.cancel(first["id"])

    def test_a_reset_race_ends_the_child_and_the_submission(self):
        service, store, _ = tasks_service()
        runner = service._runner
        spawned = []
        real_spawn = runner._spawn

        def spawn(snapshot, env):
            child = real_spawn(snapshot, env)
            spawned.append(child)
            return child

        def gone(ticket_id, pid, started):
            raise LookupError(ticket_id)

        runner._spawn = spawn
        store.mark_running = gone
        with self.assertRaises(TaskError) as caught:
            service.submit("pi-1", SCRATCH, "sleep 30")
        self.assertEqual(caught.exception.code, "spawn-failed")
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll())
        self.assertEqual(store.running_ids(), [])

    def test_a_paged_offset_past_the_end_is_clamped(self):
        service, _, _ = tasks_service()
        ticket, _ = service.submit("pi-1", SCRATCH, "printf 'abc'")
        service.wait(ticket["id"], 10)
        paged = service.output(ticket["id"], 100)
        self.assertEqual(paged["offset"], 3)
        self.assertEqual(paged["next"], 3)
        self.assertEqual(paged["data"], b"")
        self.assertEqual(paged["size"], 3)

    def test_a_failure_is_kept_not_lost(self):
        service, _, _ = tasks_service()
        ticket, _ = service.submit("pi-1", SCRATCH, "exit 3")
        done = service.wait(ticket["id"], 10)
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["exit"], 3)

    def test_a_timeout_is_a_failure_that_says_timed_out(self):
        service, _, _ = tasks_service()
        ticket, _ = service.submit("pi-1", SCRATCH, "sleep 5", 0.3)
        done = service.wait(ticket["id"], 10)
        self.assertEqual(done["status"], "failed")
        self.assertIn("timed out", done["error"])

    def test_cancel_stops_the_child(self):
        service, _, _ = tasks_service()
        ticket, _ = service.submit("pi-1", SCRATCH, "sleep 5")
        cancelled = service.cancel(ticket["id"])
        self.assertEqual(cancelled["status"], "cancelled")

    def test_zero_timeout_polls_instead_of_waiting(self):
        service, _, _ = tasks_service()
        ticket, _ = service.submit("pi-1", SCRATCH, "sleep 2")
        started = time.time()
        record = service.wait(ticket["id"], 0)
        self.assertLess(time.time() - started, 1.0)
        self.assertEqual(record["status"], "running")
        service.cancel(ticket["id"])

    def test_output_offsets_page_raw_bytes(self):
        service, _, _ = tasks_service()
        ticket, _ = service.submit("pi-1", SCRATCH, "printf 'abcdef'")
        service.wait(ticket["id"], 10)
        first = service.output(ticket["id"], 0)
        second = service.output(ticket["id"], 3)
        self.assertEqual(first["data"], b"abcdef")
        self.assertEqual(second["data"], b"def")
        self.assertEqual(second["next"], 6)
        self.assertEqual(second["size"], 6)

    def test_a_multibyte_output_is_paged_without_decoding(self):
        service, _, _ = tasks_service()
        script = "import sys;sys.stdout.buffer.write(bytes([99,97,102,195,169]))"
        ticket, _ = service.submit("pi-1", SCRATCH,
                                f'{sys.executable} -c "{script}"')
        service.wait(ticket["id"], 10)
        whole = service.output(ticket["id"])
        self.assertEqual(whole["data"], b"caf\xc3\xa9")
        paged = service.output(ticket["id"], 3)
        self.assertEqual(paged["data"], b"\xc3\xa9")
        self.assertEqual(paged["size"], 5)

    def test_the_environment_is_applied_to_the_child(self):
        service, _, _ = tasks_service()
        ticket, _ = service.submit("pi-1", SCRATCH, "printf '%s' \"$PI_SMOKE\"",
                                0.0, {"PI_SMOKE": "transient"})
        service.wait(ticket["id"], 10)
        self.assertEqual(service.output(ticket["id"])["data"], b"transient")

    def test_a_missing_directory_is_a_spawn_failure(self):
        service, _, _ = tasks_service()
        with self.assertRaises(TaskError) as caught:
            service.submit("pi-1", os.path.join(SCRATCH, "absent"), "true")
        self.assertEqual(caught.exception.code, "spawn-failed")
        self.assertEqual(service.tickets(), [])


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = DaemonApplication.from_environment({
            "PI_DAEMON_STATE_DIR": roots()[0],
            "PI_DAEMON_RUNTIME_DIR": roots()[1],
        }).start()
        self.coordinates = EndpointFile(self.app.endpoint_path()).read()
        self.codec = WireCodec()
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
            self.stream.write(self.codec.encode(
                self.codec.hello_request(self.coordinates["token"])))
            self.greeting = json.loads(self.stream.read())
        self.stream.write(self.codec.encode(request))
        return json.loads(self.stream.read())

    def test_the_descriptor_names_its_instance_and_is_restricted(self):
        self.assertEqual(self.coordinates["instance"], self.app._instance)
        self.assertEqual(self.coordinates["srv"], "pi-daemon")
        if os.name != "nt":
            mode = os.stat(self.app.endpoint_path()).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_a_ticket_round_trip_over_the_wire(self):
        submitted = self.talk({"cmd": "ticket-submit", "session": "pi-1",
                               "cwd": SCRATCH, "command": "printf 'wire'"})
        self.assertTrue(submitted["ok"])
        self.assertTrue(submitted["id"].startswith("t-"))
        ticket_id = submitted["id"]
        waited = self.talk({"cmd": "ticket-wait", "id": ticket_id,
                            "timeout": 10})
        self.assertEqual(waited["ticket"]["status"], "done")
        self.assertEqual(waited["ticket"]["exit"], 0)
        self.assertIsNotNone(waited["ticket"]["finished"])
        output = self.talk({"cmd": "ticket-output", "id": ticket_id})
        self.assertEqual(output["output"], "wire")
        self.assertEqual(output["exit"], 0)
        self.assertEqual(output["status"], "done")
        paged = self.talk({"cmd": "ticket-output", "id": ticket_id,
                           "offset": 2})
        self.assertEqual(base64.b64decode(paged["data"]), b"re")
        self.assertEqual(paged["offset"], 2)
        self.assertEqual(paged["next"], 4)
        self.assertEqual(paged["size"], 4)

    def test_the_environment_is_transient_over_the_wire(self):
        submitted = self.talk({"cmd": "ticket-submit", "session": "pi-1",
                               "cwd": SCRATCH, "env": {"PI_WIRE": "yes"},
                               "command": "printf '%s' \"$PI_WIRE\""})
        self.talk({"cmd": "ticket-wait", "id": submitted["id"], "timeout": 10})
        output = self.talk({"cmd": "ticket-output", "id": submitted["id"]})
        self.assertEqual(output["output"], "yes")
        listed = self.talk({"cmd": "ticket-list"})
        self.assertNotIn("env", listed["tickets"][0])

    def test_an_unknown_command_is_answered_not_fatal(self):
        reply = self.talk({"cmd": "ticket-nope"})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "bad-request")

    def test_the_greeting_announces_the_published_capabilities(self):
        self.talk({"cmd": "ticket-list"})
        self.assertTrue(self.greeting["ok"])
        self.assertEqual(self.greeting["srv"], "pi-daemon")
        self.assertEqual(self.greeting["proto"], 1)
        self.assertEqual(self.greeting["index"], 1)
        self.assertEqual(self.greeting["caps"], [])

    def test_a_malformed_hello_is_bad_handshake(self):
        connection = socket.create_connection(
            (self.coordinates["host"], self.coordinates["port"]), 2)
        stream = JsonLineStream(connection, 65536, 8192)
        try:
            stream.write(b"nope\n")
            reply = json.loads(stream.read())
            self.assertFalse(reply["ok"])
            self.assertEqual(reply["error"], "bad-handshake")
        finally:
            stream.close()

    def test_a_malformed_request_is_bad_request(self):
        self.talk({"cmd": "ticket-list"})
        self.assertEqual(self.talk({"cmd": "ticket-submit"})["error"],
                         "bad-request")
        self.assertEqual(self.talk({"cmd": "ticket-submit", "session": "pi-1",
                                    "cwd": "relative", "command": "true"})
                         ["error"], "bad-request")
        self.assertEqual(self.talk({"cmd": "ticket-submit", "session": "pi-1",
                                    "cwd": SCRATCH, "command": "true",
                                    "timeout": 0})["error"], "bad-request")
        self.assertEqual(self.talk({"cmd": "ticket-submit", "session": "pi-1",
                                    "cwd": SCRATCH, "command": "true",
                                    "env": {"A": 1}})["error"],
                         "bad-request")

    def test_an_unknown_ticket_is_no_ticket(self):
        self.talk({"cmd": "ticket-list"})
        self.assertEqual(self.talk({"cmd": "ticket-wait", "id": "t-nope"})
                         ["error"], "no-ticket")
        self.assertEqual(self.talk({"cmd": "ticket-output", "id": "t-nope"})
                         ["error"], "no-ticket")
        self.assertEqual(self.talk({"cmd": "ticket-cancel", "id": "t-nope"})
                         ["error"], "no-ticket")
        self.assertEqual(self.talk({"cmd": "ticket-remove", "id": "t-nope"})
                         ["error"], "no-ticket")

    def test_numeric_strings_are_accepted_like_the_published_server(self):
        self.talk({"cmd": "ticket-list"})
        submitted = self.talk({"cmd": "ticket-submit", "session": "pi-1",
                               "cwd": SCRATCH, "command": "printf 'abc'",
                               "timeout": "5"})
        self.assertTrue(submitted["ok"])
        self.talk({"cmd": "ticket-wait", "id": submitted["id"],
                   "timeout": "10"})
        paged = self.talk({"cmd": "ticket-output", "id": submitted["id"],
                           "offset": "1"})
        self.assertEqual(base64.b64decode(paged["data"]), b"bc")
        self.assertEqual(paged["offset"], 1)

    def test_an_empty_session_filter_is_listed_not_refused(self):
        self.talk({"cmd": "ticket-list"})
        self.talk({"cmd": "ticket-submit", "session": "pi-1",
                   "cwd": SCRATCH, "command": "printf 'x'"})
        self.assertTrue(self.talk({"cmd": "ticket-list", "session": ""})["ok"])

    def test_a_null_wait_timeout_is_bad_request(self):
        self.talk({"cmd": "ticket-list"})
        submitted = self.talk({"cmd": "ticket-submit", "session": "pi-1",
                               "cwd": SCRATCH, "command": "printf 'x'"})
        self.assertEqual(self.talk({"cmd": "ticket-wait",
                                    "id": submitted["id"],
                                    "timeout": None})["error"],
                         "bad-request")

    def test_removing_a_running_ticket_is_refused(self):
        self.talk({"cmd": "ticket-list"})
        submitted = self.talk({"cmd": "ticket-submit", "session": "pi-1",
                               "cwd": SCRATCH, "command": "sleep 30"})
        self.assertEqual(self.talk({"cmd": "ticket-remove",
                                    "id": submitted["id"]})["error"],
                         "running")
        self.assertTrue(self.talk({"cmd": "ticket-cancel",
                                   "id": submitted["id"]})["ok"])

    def test_an_identical_running_submission_is_reused(self):
        self.talk({"cmd": "ticket-list"})
        request = {"cmd": "ticket-submit", "session": "pi-1",
                   "cwd": SCRATCH, "command": "sleep 30"}
        first = self.talk(request)
        second = self.talk(request)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["reused"])
        self.talk({"cmd": "ticket-cancel", "id": first["id"]})

    def test_tickets_reset_reports_its_counts(self):
        self.talk({"cmd": "ticket-list"})
        self.talk({"cmd": "ticket-submit", "session": "pi-1",
                   "cwd": SCRATCH, "command": "printf 'x'"})
        reset = self.talk({"cmd": "tickets-reset"})
        self.assertEqual(reset["removed"], 1)
        self.assertIn("cancelled", reset)

    def test_a_wrong_token_is_refused(self):
        connection = socket.create_connection(
            (self.coordinates["host"], self.coordinates["port"]), 2)
        stream = JsonLineStream(connection, 65536, 8192)
        try:
            stream.write(self.codec.encode({"cmd": "hello", "token": "wrong"}))
            self.assertFalse(json.loads(stream.read())["ok"])
        finally:
            stream.close()

    def test_the_probe_finds_a_live_service(self):
        probe = EndpointProbe(WireCodec(), 2, 2, 65536, 8192)
        self.assertEqual(probe.probe(self.coordinates)["srv"], "pi-daemon")
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
