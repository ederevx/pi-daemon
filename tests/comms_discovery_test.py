#!/usr/bin/env python3
"""Discovery: what the service publishes, and who may unpublish it."""

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
sys.path.insert(0, os.path.join(REPO, "pi", "lib"))

from daemon_service.application import DaemonApplication
from daemon_service.config import ServiceConfig
from daemon_service.runtime_layout import RuntimeLayout
from daemon_service.transport.control_endpoint import ControlEndpoint
from daemon_service.transport.endpoint_file import EndpointFile
from daemon_service.transport.rpc_codec import RpcCodec
from daemon_service.transport.service_descriptor import ServiceDescriptor

import pi_services

SCRATCH = tempfile.mkdtemp(prefix="pi-discovery-")

# A stop waits out its graces; the discovery tests never run a child.
FAST_STOP = {"cancel_grace": 0.05, "settle_grace": 0.05}


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)


class Fixture:
    """One application with its own roots and one shared services root."""

    def __init__(self, services_root=None):
        base = tempfile.mkdtemp(dir=SCRATCH)
        self.state = os.path.join(base, "state")
        self.runtime = os.path.join(base, "runtime")
        self.services = services_root or os.path.join(base, "services")
        self.environment = {"PI_DAEMON_STATE_DIR": self.state,
                            "PI_DAEMON_RUNTIME_DIR": self.runtime,
                            "PI_SERVICES_DIR": self.services}
        self.layout = RuntimeLayout(self.state, self.runtime)
        self.app = None

    def start(self):
        config = ServiceConfig.load(overrides=FAST_STOP)
        self.app = DaemonApplication(self.layout, config,
                                    self.services).start()
        return self.app

    def stop(self):
        if self.app is not None:
            self.app.stop()
            self.app = None

    def comms_file(self):
        return EndpointFile(self.layout.comms_endpoint_path())

    def descriptor(self, service="agent-comms"):
        return ServiceDescriptor(self.services, service, "pi-daemon")

    def comms_json(self):
        return self.comms_file().read()


def greeting_at(endpoint_path):
    """A real rpc.hello against whatever that endpoint file publishes."""
    coordinates = EndpointFile(endpoint_path).read()
    connection = socket.create_connection(
        (coordinates["host"], coordinates["port"]), 5.0)
    try:
        line = json.dumps({"jsonrpc": "2.0", "id": "hello-1",
                           "method": "rpc.hello",
                           "params": {"token": coordinates["token"],
                                      "version": 1,
                                      "agent": {"agentId": "probe"}}})
        connection.sendall(line.encode() + b"\n")
        return RpcCodec().decode(connection.makefile("rb").readline())
    finally:
        connection.close()


class DescribePublishedFilesTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.app = self.fixture.start()

    def tearDown(self):
        self.fixture.stop()

    def test_the_comms_endpoint_file_carries_the_coordinates_and_the_token(self):
        data = self.fixture.comms_json()
        self.assertEqual(data["service"], "agent-comms")
        self.assertEqual(data["proto"], "pi-rpc/1")
        self.assertEqual(data["host"], "127.0.0.1")
        self.assertGreater(data["port"], 0)
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["instance"], self.app._instance)
        self.assertTrue(data["nodeId"])
        control = EndpointFile(self.app.endpoint_path()).read()
        self.assertEqual(data["token"], control["token"])
        mode = os.stat(self.fixture.layout.comms_endpoint_path()).st_mode
        self.assertEqual(mode & 0o077, 0, "the token file is private")

    def test_the_descriptor_names_the_endpoint_file_and_the_activation(self):
        path = self.fixture.descriptor().path()
        with open(path, encoding="utf-8") as opened:
            descriptor = json.load(opened)
        self.assertEqual(descriptor["service"], "agent-comms")
        self.assertEqual(descriptor["provider"], "pi-daemon")
        self.assertEqual(descriptor["protocol"], "pi-rpc/1")
        self.assertEqual(descriptor["version"], 1)
        self.assertEqual(descriptor["pid"], os.getpid())
        self.assertEqual(descriptor["instance"], self.app._instance)
        self.assertEqual(descriptor["endpoint_file"],
                         os.path.abspath(self.fixture.layout.comms_endpoint_path()))
        self.assertEqual(descriptor["activation"]["kind"], "exec")
        self.assertEqual(descriptor["activation"]["argv"][-1], "daemon-start")

    def test_the_session_host_descriptor_points_at_the_control_endpoint(self):
        path = self.fixture.descriptor("session-host").path()
        with open(path, encoding="utf-8") as opened:
            descriptor = json.load(opened)
        self.assertEqual(descriptor["protocol"], "pi-pty-host/1")
        self.assertEqual(descriptor["endpoint_file"],
                         os.path.abspath(self.app.endpoint_path()))

    def test_the_comms_listener_answers_before_its_entry_publishes(self):
        """A published entry always names a port that already greets."""
        seen = {}
        original = ServiceDescriptor.publish

        def spy(descriptor, protocol, version, endpoint_file, argv, instance,
                pid):
            if endpoint_file.endswith("comms.json"):
                self.assertTrue(os.path.isfile(endpoint_file))
                seen["greeting"] = greeting_at(endpoint_file)
            return original(descriptor, protocol, version, endpoint_file,
                            argv, instance, pid)

        self.fixture.stop()
        with mock.patch.object(ServiceDescriptor, "publish", spy):
            self.app = self.fixture.start()
        self.assertEqual(seen["greeting"]["result"]["service"], "agent-comms")

    def test_the_client_directory_agrees_with_the_service(self):
        directory = pi_services.ServiceDirectory(root=self.fixture.services)
        found = directory.find("agent-comms", ("pi-rpc/1",))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["endpoint_file"],
                         os.path.abspath(self.fixture.layout.comms_endpoint_path()))
        session_host = directory.find("session-host", ("pi-pty-host/1",))
        self.assertEqual(len(session_host), 1)
        directory.sweep()
        self.assertEqual(len(directory.find("agent-comms", ("pi-rpc/1",))), 1)

    def test_a_dead_pid_entry_is_swept_by_the_client_directory(self):
        path = self.fixture.descriptor().path()
        with open(path, encoding="utf-8") as opened:
            descriptor = json.load(opened)
        descriptor["pid"] = 2 ** 22 + 7
        with open(path, "w", encoding="utf-8") as opened:
            json.dump(descriptor, opened)
        pi_services.ServiceDirectory(root=self.fixture.services).sweep()
        self.assertFalse(os.path.exists(path))


class StopTests(unittest.TestCase):
    def test_stop_removes_both_entries_and_the_endpoint_file(self):
        fixture = Fixture()
        app = fixture.start()
        comms_path = fixture.layout.comms_endpoint_path()
        descriptor_path = fixture.descriptor().path()
        control_descriptor = fixture.descriptor("session-host").path()
        app.stop()
        for path in (comms_path, descriptor_path, control_descriptor):
            self.assertFalse(os.path.exists(path), path)

    def test_a_failed_start_removes_what_it_published(self):
        fixture = Fixture()
        with mock.patch.object(ControlEndpoint, "start",
                               side_effect=RuntimeError("no listener")):
            with self.assertRaises(RuntimeError):
                fixture.start()
        for path in (fixture.layout.comms_endpoint_path(),
                     fixture.descriptor().path(),
                     fixture.descriptor("session-host").path(),
                     fixture.layout.endpoint_path()):
            self.assertFalse(os.path.exists(path), path)

    def test_a_foreign_instance_or_pid_is_never_unpublished(self):
        fixture = Fixture()
        fixture.start()
        app = fixture.app
        fixture.app = None
        app.stop()
        comms_path = fixture.layout.comms_endpoint_path()
        descriptor_path = fixture.descriptor().path()
        os.makedirs(os.path.dirname(comms_path), exist_ok=True)
        os.makedirs(os.path.dirname(descriptor_path), exist_ok=True)
        with open(comms_path, "w", encoding="utf-8") as opened:
            json.dump({"host": "127.0.0.1", "port": 1, "token": "t",
                       "instance": "someone-else", "pid": os.getpid()},
                      opened)
        self.assertFalse(EndpointFile(comms_path).remove_owned(app._instance))
        self.assertTrue(os.path.exists(comms_path))
        descriptor = fixture.descriptor()
        descriptor.publish("pi-rpc/1", 1, comms_path, ["pi-rc"],
                           "someone-else", os.getpid())
        self.assertFalse(descriptor.remove_owned(app._instance, os.getpid()))
        self.assertTrue(os.path.exists(descriptor_path))
        descriptor.publish("pi-rpc/1", 1, comms_path, ["pi-rc"],
                           app._instance, os.getpid() + 1)
        self.assertFalse(descriptor.remove_owned(app._instance, os.getpid()))
        self.assertTrue(os.path.exists(descriptor_path))
        self.assertTrue(descriptor.remove_owned(app._instance,
                                                os.getpid() + 1))
        self.assertFalse(os.path.exists(descriptor_path))

    def test_a_successor_keeps_its_entry_when_the_first_stops(self):
        first = Fixture()
        second = Fixture(services_root=first.services)
        first_app = first.start()
        second_app = second.start()
        second_descriptor = second.descriptor().path()
        with open(second_descriptor, encoding="utf-8") as opened:
            successor = json.load(opened)
        first_app.stop()
        first.app = None
        self.assertTrue(os.path.exists(second_descriptor))
        with open(second_descriptor, encoding="utf-8") as opened:
            self.assertEqual(json.load(opened)["instance"], successor["instance"])
        self.assertTrue(os.path.exists(second.layout.comms_endpoint_path()))
        second.stop()

    def test_an_embedded_application_publishes_nothing(self):
        base = tempfile.mkdtemp(dir=SCRATCH)
        layout = RuntimeLayout(os.path.join(base, "state"),
                               os.path.join(base, "runtime"))
        services = os.path.join(base, "services")
        app = DaemonApplication(layout, ServiceConfig.load(overrides=FAST_STOP))
        app.start()
        try:
            self.assertFalse(os.path.isdir(services))
            self.assertFalse(
                os.path.exists(os.path.join(services, "agent-comms")))
        finally:
            app.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
