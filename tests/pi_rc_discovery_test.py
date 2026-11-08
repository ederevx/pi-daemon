#!/usr/bin/env python3
"""pi-rc endpoint discovery: explicit, descriptor-first, legacy fallback.

The descriptor cases use a real loopback listener's endpoint file, so a
resolved path is one a client could dial; discovery itself never
connects.
"""

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
