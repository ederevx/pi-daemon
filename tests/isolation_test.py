#!/usr/bin/env python3
"""The suite's own environment: what it neutralizes, proven adversarially.

The gate's environment is only worth trusting if an inherited name that
would reach the host's daemon does not survive it. These cases plant such
a name - a published service descriptor behind a listener that counts its
connections - show that a process inheriting it does reach that service,
and then show that a process given the suite's environment does not.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "lib"))

import isolation

# This module's own tree: built before anything resolves a root.
ISOLATION = isolation.ScratchEnvironment()
ISOLATION.apply()

from pi_platform import AtomicStateFile

PROBE = """
import json, os, socket, sys
sys.path.insert(0, %(library)r)
import pi_platform, pi_services
found = pi_services.ServiceDirectory().find()
dialed = 0
for entry in found:
    data = pi_platform.EndpointFile(entry["endpoint_file"]).read()
    try:
        socket.create_connection((data["host"], data["port"]),
                                 timeout=2).close()
        dialed += 1
    except OSError:
        pass
print(json.dumps({
    "found": [entry.get("endpoint_file") for entry in found],
    "dialed": dialed,
    "endpoint": "PI_DAEMON_ENDPOINT" in os.environ,
    "services": os.environ.get("PI_SERVICES_DIR"),
}))
"""


class Counter:
    """A listener that stands in for a service outside the suite's tree."""

    def __init__(self):
        self.connections = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.2)
        self._running = True
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def address(self):
        return self._listener.getsockname()

    def _accept(self):
        while self._running:
            try:
                connection, peer = self._listener.accept()
            except OSError:
                continue
            self.connections.append(peer)
            connection.close()

    def wait_for_connections(self, count, timeout=5.0):
        """Wait until `count` connections have arrived, then report them."""
        import time
        deadline = time.monotonic() + timeout
        while len(self.connections) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        return list(self.connections)

    def close(self):
        self._running = False
        self._thread.join(2.0)
        self._listener.close()


class HostLike:
    """A published descriptor in a tree this suite does not own."""

    def __init__(self, counter):
        self.root = tempfile.mkdtemp(prefix="pi-isolation-host-")
        self.services = os.path.join(self.root, "services")
        self.endpoint = os.path.join(self.root, "endpoint.json")
        host, port = counter.address
        AtomicStateFile(self.endpoint).write(
            {"host": host, "port": port, "token": "host-not-a-real-token"})
        sys.path.insert(0, os.path.join(REPO, "pi", "lib"))
        import pi_services
        pi_services.ServiceDirectory(root=self.services).publish(
            self.endpoint, ["host-like-provider"], pid=os.getpid())

    def environment(self):
        """The environment a process would inherit from this host."""
        environment = dict(os.environ)
        environment.update({
            "PI_SERVICES_DIR": self.services,
            "PI_DAEMON_ENDPOINT": self.endpoint,
            "PI_HOSTED": "pi-isolation-planted",
        })
        return environment

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def probe(environment):
    """Resolve the service directory in a fresh process and report it."""
    script = PROBE % {"library": os.path.join(REPO, "pi", "lib")}
    result = subprocess.run([sys.executable, "-c", script], env=environment,
                            capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise AssertionError("probe failed: %s" % result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class AdversarialTests(unittest.TestCase):
    """The fixture first, so the neutralization has something to defeat."""

    def setUp(self):
        self.counter = Counter()
        self.host = HostLike(self.counter)

    def tearDown(self):
        self.counter.close()
        self.host.cleanup()

    def test_the_fixture_reaches_its_service_when_it_is_inherited(self):
        inheriting = probe(self.host.environment())
        self.assertIn(self.host.endpoint, inheriting["found"])
        self.assertEqual(1, inheriting["dialed"])
        self.assertTrue(inheriting["endpoint"])
        self.assertEqual(self.host.services, inheriting["services"])
        self.assertTrue(
            self.counter.wait_for_connections(1),
            "the fixture's descriptor was not reached, so it is no test")

    def test_the_suite_environment_drops_an_inherited_endpoint(self):
        saved = dict(os.environ)
        try:
            os.environ.update(self.host.environment())
            environment = ISOLATION.environment()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertNotIn("PI_DAEMON_ENDPOINT", environment)
        self.assertNotIn("PI_HOSTED", environment)
        self.assertNotEqual(self.host.services, environment["PI_SERVICES_DIR"])
        self.assertIn(ISOLATION.root, environment["PI_SERVICES_DIR"])

    def test_the_suite_environment_never_resolves_the_hosts_service(self):
        saved = dict(os.environ)
        try:
            os.environ.update(self.host.environment())
            environment = ISOLATION.environment()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        isolated = probe(environment)
        self.assertEqual([], isolated["found"])
        self.assertEqual(0, isolated["dialed"])
        self.assertFalse(isolated["endpoint"])
        # Nothing arrived after the isolated probe read its own tree.
        self.assertEqual([], self.counter.wait_for_connections(1, 1.0),
                         "the isolated process connected to the service")

    def test_every_owned_root_names_the_suite_tree(self):
        environment = ISOLATION.environment()
        for name in isolation.ROOT_VARIABLES:
            self.assertIn(name, environment)
            self.assertTrue(
                os.path.commonpath([ISOLATION.root, environment[name]])
                == ISOLATION.root,
                "%s names %r, outside the suite's tree"
                % (name, environment[name]))
        for name in isolation.CLEARED_VARIABLES:
            self.assertNotIn(name, environment)


if __name__ == "__main__":
    unittest.main()
