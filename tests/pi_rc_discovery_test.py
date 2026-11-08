#!/usr/bin/env python3
"""pi-rc endpoint discovery: explicit, descriptor-first, legacy fallback.

The descriptor cases use a real loopback listener's endpoint file, so a
resolved path is one a client could dial; discovery itself never
connects.
"""

import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "lib"))

import pi_platform
import pi_services

SCRATCH = tempfile.mkdtemp(prefix="pi-rc-discovery-",
                           dir=os.path.expanduser("~/tmp"))


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)


pi_rc = SourceFileLoader(
    "pi_rc_discovery", os.path.join(REPO, "pi", "bin", "pi-rc")).load_module()


class LoopbackEndpoint:
    """A listening loopback socket plus the endpoint file naming it."""

    def __init__(self, path):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.path = path
        host, port = self.socket.getsockname()
        pi_platform.EndpointFile(path).write(host, port, "token")

    def close(self):
        self.socket.close()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(dir=SCRATCH)
        self.root = os.path.join(self.base, "services")
        self.listener = LoopbackEndpoint(
            os.path.join(self.base, "endpoint.json"))
        self.environ = {"PI_SERVICES_DIR": self.root}
        self.services = pi_services.ServiceDirectory(root=self.root)

    def tearDown(self):
        self.listener.close()

    def publish(self, provider, version=1, endpoint=None, pid=None,
                protocol="pi-pty-host/1"):
        descriptor = {
            "service": "session-host", "version": version,
            "protocol": protocol, "provider": provider,
            "pid": os.getpid() if pid is None else pid,
            "endpoint_file": endpoint or self.listener.path,
            "activation": {"kind": "exec", "argv": ["pi-rc", "daemon-start"]},
        }
        directory = os.path.join(self.root, "session-host")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, provider + ".json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(descriptor, handle)
        return path

    def resolver(self, environ=None):
        return pi_rc.EndpointResolver(
            environ=self.environ if environ is None else environ,
            services=self.services)


class ResolutionTests(Fixture):
    def test_discovery_takes_the_highest_live_version(self):
        older = LoopbackEndpoint(os.path.join(self.base, "older.json"))
        newer = LoopbackEndpoint(os.path.join(self.base, "newer.json"))
        try:
            self.publish("older", version=1, endpoint=older.path)
            self.publish("newer", version=3, endpoint=newer.path)
            self.assertEqual(self.resolver().resolve(), newer.path)
        finally:
            older.close()
            newer.close()

    def test_stale_descriptors_are_skipped(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        missing = os.path.join(self.base, "absent.json")
        no_coordinates = os.path.join(self.base, "empty-endpoint.json")
        with open(no_coordinates, "w", encoding="utf-8") as handle:
            handle.write("{}")
        dead_endpoint = os.path.join(self.base, "dead-endpoint.json")
        pi_platform.EndpointFile(dead_endpoint).write(
            "127.0.0.1", 1, "token")
        protocol_endpoint = os.path.join(self.base, "protocol-endpoint.json")
        pi_platform.EndpointFile(protocol_endpoint).write(
            "127.0.0.1", 1, "token")
        self.publish("dead", version=9, endpoint=dead_endpoint, pid=dead.pid)
        self.publish("missing", version=8, endpoint=missing)
        self.publish("no-coordinates", version=7, endpoint=no_coordinates)
        malformed = self.publish("malformed", version=6)
        with open(malformed, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.publish("other-protocol", version=5,
                     endpoint=protocol_endpoint, protocol="pi-rpc/1")
        self.publish("live", version=4)
        self.assertEqual(self.resolver().resolve(), self.listener.path)
        # Skipping is a read: only the provider's own sweep removes them.
        for provider in ("dead", "missing", "no-coordinates", "malformed",
                         "other-protocol"):
            self.assertTrue(os.path.exists(os.path.join(
                self.root, "session-host", provider + ".json")))

    def test_empty_root_falls_back_to_the_legacy_path(self):
        runtime = os.path.join(self.base, "runtime")
        environ = {"XDG_RUNTIME_DIR": runtime}
        expected = os.path.join(runtime, "pi-pty-host.sock")
        with mock.patch.dict(os.environ, environ, clear=False):
            resolved = pi_rc.EndpointResolver(
                environ=environ, services=self.services).resolve()
        self.assertEqual(resolved, expected)

    def test_explicit_path_wins_over_environment_and_discovery(self):
        self.publish("live")
        environ = dict(self.environ, PI_DAEMON_ENDPOINT="/env/endpoint.json")
        resolver = self.resolver(environ)
        self.assertEqual(resolver.resolve("/explicit/endpoint.json"),
                         "/explicit/endpoint.json")
        self.assertEqual(resolver.resolve(), "/env/endpoint.json")

    def test_client_resolves_at_use(self):
        client = pi_rc.DaemonClient(resolver=self.resolver())
        self.assertIsNone(client._sock_path, "nothing probed at construction")
        self.publish("late")
        self.assertEqual(client.sock_path, self.listener.path)

    def test_client_honours_an_explicit_path(self):
        client = pi_rc.DaemonClient(
            sock_path="/explicit/endpoint.json", resolver=self.resolver())
        self.assertEqual(client.sock_path, "/explicit/endpoint.json")


class EndpointOptionTests(unittest.TestCase):
    def test_leading_options(self):
        self.assertEqual(
            pi_rc.endpoint_option(["--endpoint", "/x", "ls"]),
            ("/x", ["ls"]))
        self.assertEqual(
            pi_rc.endpoint_option(["--endpoint=/x", "ls"]),
            ("/x", ["ls"]))
        self.assertEqual(
            pi_rc.endpoint_option(["--endpoint=a", "--endpoint", "b", "ls"]),
            ("b", ["ls"]))

    def test_command_arguments_are_never_scanned(self):
        self.assertEqual(
            pi_rc.endpoint_option(["ls", "--endpoint", "/x"]),
            (None, ["ls", "--endpoint", "/x"]))
        self.assertEqual(
            pi_rc.endpoint_option(
                ["ticket-submit", "--", "tool", "--endpoint", "child"]),
            (None, ["ticket-submit", "--", "tool", "--endpoint", "child"]))

    def test_no_arguments(self):
        self.assertEqual(pi_rc.endpoint_option([]), (None, []))

    def test_missing_value_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as raised:
            pi_rc.endpoint_option(["--endpoint"])
        self.assertEqual(raised.exception.code, pi_rc.EXIT_USAGE)


class SessionNameTests(unittest.TestCase):
    def test_a_prefixed_name_is_not_prefixed_again(self):
        self.assertEqual(pi_rc.session_name("pi-ederevx-9580"),
                         "pi-ederevx-9580")
        self.assertEqual(pi_rc.session_name("ederevx-9580"),
                         "pi-ederevx-9580")
        self.assertEqual(pi_rc.session_name("sfworkload-owner"),
                         "pi-sfworkload-owner")

    def test_a_revive_reopens_the_record_the_daemon_resolved(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        stored = os.path.join(base, "legacy.jsonl")
        open(stored, "w").close()
        replies = iter([
            {"ok": False, "error": "no-session", "file": stored},
            {"ok": True, "name": "sfworkload-owner"},
            {"ok": True, "created": True},
            {"ok": True, "name": "sfworkload-owner", "state": "idle"},
        ])
        sent = []
        flow = pi_rc.AttachFlow(None, pi_rc.session_name("sfworkload-owner"),
                                80, 24, dir_="/tmp")
        with mock.patch.object(pi_rc, "send_json",
                               lambda sock, obj: sent.append(obj)), \
                mock.patch.object(pi_rc, "read_json_line",
                                  lambda sock: (next(replies), b"")), \
                mock.patch.object(pi_rc.PROCESS, "pi_argv",
                                  lambda args=(): ["pi"] + list(args)), \
                contextlib.redirect_stdout(io.StringIO()):
            flow.handshake(None)
        start = [request for request in sent
                 if request["cmd"] == "start"][0]
        self.assertEqual(start["name"], "sfworkload-owner")
        self.assertEqual(start["argv"], ["pi", "--session", stored])

    def test_a_revive_stores_the_typed_name_under_the_daemon_spelling(self):
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        stored = os.path.join(base, "legacy.jsonl")
        open(stored, "w").close()
        replies = iter([
            {"ok": False, "error": "no-session", "file": stored,
             "recorded": "pi-a", "dir": base},
            {"ok": True, "created": True},
            {"ok": True, "name": "pi-a", "state": "idle"},
        ])
        sent = []
        flow = pi_rc.AttachFlow(None, "pi-a", 80, 24, dir_="/tmp")
        with mock.patch.object(pi_rc, "send_json",
                               lambda sock, obj: sent.append(obj)), \
                mock.patch.object(pi_rc, "read_json_line",
                                  lambda sock: (next(replies), b"")), \
                mock.patch.object(pi_rc.PROCESS, "pi_argv",
                                  lambda args=(): ["pi"] + list(args)), \
                contextlib.redirect_stdout(io.StringIO()):
            flow.handshake(None)
        start = [request for request in sent
                 if request["cmd"] == "start"][0]
        # The record's own spelling and directory win over what was typed.
        self.assertEqual(start["name"], "pi-a")
        self.assertEqual(start["dir"], base)

    def test_a_revive_without_a_conversation_refuses_to_start_fresh(self):
        replies = iter([{"ok": False, "error": "no-session",
                         "recorded": "pi-a"}])
        sent = []
        flow = pi_rc.AttachFlow(None, "pi-a", 80, 24, dir_="/tmp")
        errors = io.StringIO()
        with mock.patch.object(pi_rc, "send_json",
                               lambda sock, obj: sent.append(obj)), \
                mock.patch.object(pi_rc, "read_json_line",
                                  lambda sock: (next(replies), b"")), \
                contextlib.redirect_stderr(errors), \
                self.assertRaises(SystemExit) as caught:
            flow.handshake(None)
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual([request for request in sent
                          if request["cmd"] == "start"], [],
                         "nothing is started")
        self.assertIn("never recorded a conversation", errors.getvalue())

    def test_a_revive_whose_conversation_is_gone_refuses_to_start_fresh(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-conv.jsonl")
        replies = iter([{"ok": False, "error": "no-session",
                         "file": missing, "recorded": "pi-a"}])
        sent = []
        flow = pi_rc.AttachFlow(None, "pi-a", 80, 24, dir_="/tmp")
        errors = io.StringIO()
        with mock.patch.object(pi_rc, "send_json",
                               lambda sock, obj: sent.append(obj)), \
                mock.patch.object(pi_rc, "read_json_line",
                                  lambda sock: (next(replies), b"")), \
                contextlib.redirect_stderr(errors), \
                self.assertRaises(SystemExit) as caught:
            flow.handshake(None)
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual([request for request in sent
                          if request["cmd"] == "start"], [],
                         "nothing is started")
        self.assertIn("nothing to resume", errors.getvalue())

    def test_stopping_a_session_that_is_not_running_says_so(self):
        client = mock.Mock()
        client.control.return_value = {"ok": True, "name": "pi-x",
                                       "queued": False, "alive": False,
                                       "state": "idle"}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pi_rc.cmd_stop(["pi-x"], client)
        self.assertIn("'pi-x' was not running", out.getvalue())
        self.assertNotIn("stopped", out.getvalue())
        client.control.assert_called_once_with({"cmd": "stop",
                                                "name": "pi-x"})


class ListingTests(unittest.TestCase):
    def listing(self, reply, args=()):
        client = mock.Mock()
        client.control.return_value = reply
        out, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(errors):
            pi_rc.cmd_ls(list(args), client)
        return out.getvalue(), errors.getvalue(), client

    def test_the_default_listing_prints_live_rows_and_mentions_the_rest(self):
        out, errors, client = self.listing({
            "ok": True, "stopped": 2,
            "sessions": [{"name": "pi-workload", "dir": "/s", "pid": 7,
                          "phase": "running", "state": "idle",
                          "attached": True, "clients": 1,
                          "file": "/s/conv.jsonl"}]})
        client.control.assert_called_once_with({"cmd": "list",
                                                "all": False})
        # The listing prints the short name a caller can type back.
        self.assertIn("workload\t/s\tpid 7\tattached\tidle\t/s/conv.jsonl",
                      out)
        self.assertIn("2 stopped session(s) not shown", errors)

    def test_all_asks_the_daemon_for_the_stopped_records(self):
        out, errors, client = self.listing(normal_all_reply(), ["--all"])
        client.control.assert_called_once_with({"cmd": "list", "all": True})
        self.assertIn("stopped:resumable", out)
        self.assertIn("stopped:no-conversation", out)
        self.assertIn("stopped:file-missing", out)
        self.assertIn("stopped:resumable:unfinished", out)
        self.assertEqual(errors, "", "nothing is left out to mention")

    def test_a_listing_rejects_a_flag_it_does_not_know(self):
        with self.assertRaises(SystemExit):
            self.listing({"ok": True, "sessions": []}, ["--nope"])


def normal_all_reply():
    # One of each state a stopped record can be in, in the daemon's own
    # words: a conversation to resume, none ever recorded, or one the
    # directory no longer holds.
    stopped = {"pid": None, "attached": False, "clients": 0,
               "phase": "exited", "state": None}
    return {"ok": True, "sessions": [
        dict(stopped, name="pi-a", desired="stopped", dir="/s",
             file="/s/conv.jsonl", fileExists=True),
        dict(stopped, name="pi-b", desired="stopped", dir="/s",
             file=None, fileExists=False),
        dict(stopped, name="pi-c", desired="stopped", dir="/s",
             file="/s/gone.jsonl", fileExists=False),
        dict(stopped, name="pi-d", desired="running", dir="/s",
             file="/s/conv.jsonl", fileExists=True),
    ]}


if __name__ == "__main__":
    unittest.main(verbosity=2)
