#!/usr/bin/env python3
"""Tests for the unified service: configuration, the ticket store and
runner, and the control endpoint end to end.

Scratch lives under ~/tmp, never /tmp; stdlib only. Shell-specific
cases skip on Windows, where the platform interpreter differs.
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "service"))

import daemon_service.config as config_module  # noqa: E402
import daemon_service.endpoint as endpoint_module  # noqa: E402
import daemon_service.wire as wire  # noqa: E402
from daemon_service.app import Service  # noqa: E402
from daemon_service.state import ServiceState  # noqa: E402
from daemon_service.tasks.model import Ticket  # noqa: E402
from daemon_service.tasks.service import TaskService  # noqa: E402

SCRATCH = tempfile.mkdtemp(
    prefix="pi-service-", dir=os.path.expanduser("~/tmp"))
POSIX = os.name != "nt"


def scratch_state(name, settings=None):
    base = os.path.join(SCRATCH, name)
    shutil.rmtree(base, ignore_errors=True)
    environ = {
        "XDG_STATE_HOME": os.path.join(base, "state"),
        "XDG_RUNTIME_DIR": os.path.join(base, "runtime"),
        "PI_CODING_AGENT_DIR": os.path.join(base, "agent"),
    }
    for path in environ.values():
        os.makedirs(path, exist_ok=True)
    if settings is not None:
        with open(os.path.join(environ["PI_CODING_AGENT_DIR"],
                               "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"piDaemon": settings}, f)
    state = ServiceState(environ=environ)
    state.ensure()
    return state


class ConfigTests(unittest.TestCase):
    def config(self, name, settings=None, environ=None):
        state = scratch_state(name, settings)
        return config_module.ServiceConfig(state, environ=environ or {})

    def test_defaults(self):
        cfg = self.config("cfg-default")
        self.assertEqual(cfg.ticket_ttl, 72 * 3600)
        self.assertEqual(cfg.ticket_gc_seconds, 60.0)
        self.assertEqual(cfg.max_running_tickets, 32)
        self.assertEqual(cfg.max_output_bytes, 4 << 20)

    def test_settings_apply(self):
        cfg = self.config("cfg-settings", {"ticketTtlHours": 2,
                                           "maxRunningTickets": 3})
        self.assertEqual(cfg.ticket_ttl, 7200.0)
        self.assertEqual(cfg.max_running_tickets, 3)

    def test_environment_wins_and_legacy_is_honored(self):
        cfg = self.config("cfg-env", {"ticketTtlHours": 2},
                          {"PI_DAEMON_TICKET_TTL_HOURS": "1"})
        self.assertEqual(cfg.ticket_ttl, 3600.0)
        legacy = self.config("cfg-legacy", {},
                             {"PI_PTYD_TICKET_TTL_HOURS": "4"})
        self.assertEqual(legacy.ticket_ttl, 4 * 3600)

    def test_malformed_value_falls_through(self):
        cfg = self.config("cfg-bad", {"ticketTtlHours": 3},
                          {"PI_DAEMON_TICKET_TTL_HOURS": "soon"})
        self.assertEqual(cfg.ticket_ttl, 3 * 3600)


class TicketStoreTests(unittest.TestCase):
    def setUp(self):
        self.state = scratch_state("store")
        self.config = config_module.ServiceConfig(self.state, environ={})
        self.store = self.store_with()

    def store_with(self, max_records=200):
        from daemon_service.tasks.store import TicketStore
        return TicketStore(self.state, max_records=max_records)

    def ticket(self, tid="t-1", session="pi-1"):
        return Ticket(tid, session, SCRATCH, "true")

    def test_records_survive_a_reload(self):
        self.store.put(self.ticket())
        reloaded = self.store_with()
        self.assertEqual(reloaded.get("t-1").command, "true")

    def test_ids_are_monotonic_across_a_reload(self):
        self.store.alloc("pi-1")
        reloaded = self.store_with()
        self.assertEqual(reloaded.alloc("pi-1"), "t-2")

    def test_finished_records_are_capped_and_logs_removed(self):
        store = self.store_with(max_records=1)
        for index, tid in enumerate(("t-1", "t-2")):
            ticket = self.ticket(tid)
            ticket.finish("done", 0)
            ticket.finished = float(index)
            store.put(ticket)
        self.assertIsNone(store.get("t-1"))
        self.assertIsNotNone(store.get("t-2"))

    def test_removing_a_running_ticket_is_refused(self):
        ticket = self.ticket()
        ticket.status = "running"
        self.store.put(ticket)
        with self.assertRaises(ValueError):
            self.store.remove("t-1")


class TaskServiceTests(unittest.TestCase):
    def setUp(self):
        self.state = scratch_state("tasks")
        self.config = config_module.ServiceConfig(self.state, environ={})
        self.tasks = TaskService(self.state, self.config)

    def tearDown(self):
        self.tasks.stop()

    @unittest.skipUnless(POSIX, "POSIX shell")
    def test_a_ticket_runs_and_keeps_its_output(self):
        ticket = self.tasks.submit("pi-1", SCRATCH, "printf hello")
        done = self.tasks.wait(ticket.id, 10)
        self.assertEqual(done.status, "done")
        self.assertEqual(done.exit, 0)
        self.assertEqual(self.tasks.text_output(ticket.id), "hello")

    @unittest.skipUnless(POSIX, "POSIX shell")
    def test_a_failing_command_is_failed_not_lost(self):
        ticket = self.tasks.submit("pi-1", SCRATCH, "exit 3")
        done = self.tasks.wait(ticket.id, 10)
        self.assertEqual(done.status, "failed")
        self.assertEqual(done.exit, 3)

    @unittest.skipUnless(POSIX, "POSIX shell")
    def test_cancel_stops_the_child(self):
        ticket = self.tasks.submit("pi-1", SCRATCH, "sleep 30")
        time.sleep(0.2)
        cancelled = self.tasks.cancel(ticket.id)
        self.assertEqual(cancelled.status, "cancelled")

    @unittest.skipUnless(POSIX, "POSIX shell")
    def test_timeout_is_its_own_outcome(self):
        ticket = self.tasks.submit("pi-1", SCRATCH, "sleep 5", timeout=0.3)
        done = self.tasks.wait(ticket.id, 10)
        self.assertEqual(done.status, "timeout")

    @unittest.skipUnless(POSIX, "POSIX shell")
    def test_a_reset_forgets_every_ticket(self):
        self.tasks.submit("pi-1", SCRATCH, "true")
        self.assertGreater(self.tasks.reset(), 0)
        self.assertEqual(self.tasks.list(), [])

    def test_output_offsets_page_a_whole_log(self):
        ticket = Ticket(self.tasks.store.alloc("pi-1"), "pi-1", SCRATCH, "x")
        self.tasks.store.put(ticket)
        self.tasks.store.append_output(ticket.id, b"abcdef")
        first = self.tasks.output(ticket.id, 0)
        self.assertEqual(first["data"], b"abcdef")
        self.assertEqual(first["next"], 6)
        self.assertEqual(self.tasks.output("t-404", 0), None)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.state = scratch_state("endpoint")
        self.service = Service(state=self.state, token="secret-token")

    def tearDown(self):
        self.service.stop()

    def connect(self, token="secret-token", version=1):
        with open(self.state.endpoint_path(), "r", encoding="utf-8") as f:
            where = json.load(f)
        sock = socket.create_connection((where["host"], where["port"]),
                                        timeout=5)
        self.reply = self.exchange(sock, {"cmd": "hello", "token": token,
                                          "version": version})
        return sock

    def exchange(self, sock, message):
        sock.sendall(wire.encode(message))
        data = b""
        while b"\n" not in data:
            piece = sock.recv(65536)
            if not piece:
                break
            data += piece
        return wire.decode(data.split(b"\n", 1)[0].decode())

    def start(self):
        self.service.tasks.start()
        self.service.endpoint.start()

    def test_wrong_token_and_version_are_refused(self):
        self.start()
        self.connect(token="nope").close()
        self.assertEqual(self.reply["error"], "unauthorized")
        self.connect(version=2).close()
        self.assertEqual(self.reply["error"], "protocol-mismatch")

    def test_hello_advertises_the_command_table(self):
        self.start()
        self.connect()
        self.assertTrue(self.reply["ok"])
        self.assertEqual(self.reply["service"], "pi-daemon")
        self.assertIn("ticket-submit", self.reply["commands"])

    def test_an_unknown_command_is_answered_not_fatal(self):
        self.start()
        sock = self.connect()
        self.assertEqual(self.exchange(sock, {"cmd": "nope"})["error"],
                         "bad-cmd")
        self.assertTrue(self.exchange(sock, {"cmd": "ticket-list"})["ok"])
        sock.close()

    @unittest.skipUnless(POSIX, "POSIX shell")
    def test_a_ticket_round_trip_over_the_wire(self):
        self.start()
        sock = self.connect()
        submitted = self.exchange(sock, {
            "cmd": "ticket-submit", "session": "pi-1", "cwd": SCRATCH,
            "command": "printf wired"})
        self.assertTrue(submitted["ok"], submitted)
        tid = submitted["id"]
        waited = self.exchange(sock, {"cmd": "ticket-wait", "id": tid,
                                      "timeout": 10})
        self.assertEqual(waited["ticket"]["status"], "done")
        output = self.exchange(sock, {"cmd": "ticket-output", "id": tid})
        self.assertEqual(output["output"], "wired")
        sock.close()

    def test_probe_finds_a_live_service_and_gone_after_stop(self):
        self.start()
        self.assertTrue(endpoint_module.probe(self.state.endpoint_path()))
        self.service.stop()
        self.assertFalse(endpoint_module.probe(self.state.endpoint_path()))
        self.assertFalse(os.path.exists(self.state.endpoint_path()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
