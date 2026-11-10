#!/usr/bin/env python3
"""pi-comms: the operator CLI against a live service."""

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

import isolation

# Every root this module probes belongs to its own scratch tree, never
# the host's live daemon.
ISOLATION = isolation.ScratchEnvironment()
ISOLATION.apply()

from pi_daemon.application import DaemonApplication
from pi_daemon.config import ServiceConfig
from pi_daemon.runtime_layout import RuntimeLayout
from pi_daemon.transport.endpoint_file import EndpointFile
from pi_daemon.transport.rpc_codec import RpcCodec
from pi_daemon.transport.rpc_listener import RpcListener
from pi_daemon.transport.service_descriptor import ServiceDescriptor

from pi_daemon.comms.mailbox_store import MailboxStore
from pi_daemon.comms.service import CommsService

CLI = os.path.join(REPO, "pi", "bin", "pi-comms")

SCRATCH = tempfile.mkdtemp(prefix="pi-comms-cli-")

# A stop waits out its graces; these tests run no child.
FAST_STOP = {"cancel_grace": 0.05, "settle_grace": 0.05}


def tearDownModule():
    ISOLATION.cleanup()
    shutil.rmtree(SCRATCH, ignore_errors=True)


class Fixture:
    """One live application with a private services root."""

    def __init__(self):
        base = tempfile.mkdtemp(dir=SCRATCH)
        self.state = os.path.join(base, "state")
        self.runtime = os.path.join(base, "runtime")
        self.services = os.path.join(base, "services")
        self.sessions = os.path.join(base, "sessions")
        self.layout = RuntimeLayout(self.state, self.runtime)
        self.app = None

    def start(self):
        self.app = DaemonApplication(
            self.layout, ServiceConfig.load(overrides=FAST_STOP),
            self.services,
            session_store_root=self.sessions).start()
        return self

    def stop(self):
        if self.app is not None:
            self.app.stop()
            self.app = None

    def environment(self, agent="alice", **extra):
        environment = dict(os.environ)
        for name in ("PI_COMMS_ENDPOINT", "PI_SERVICES_DIR",
                     "PI_COMMS_AGENT_ID", "PI_HOSTED_SESSION", "PI_SESSION_FILE",
                     "PI_DAEMON_STATE_DIR"):
            environment.pop(name, None)
        environment["PI_SERVICES_DIR"] = self.services
        if agent is not None:
            environment["PI_COMMS_AGENT_ID"] = agent
        environment.update({key: str(value) for key, value in extra.items()})
        return environment

    def run(self, *arguments, agent="alice", environment=None):
        command = [sys.executable, CLI]
        if agent is not None:
            command += ["--agent", agent]
        command += list(arguments)
        return subprocess.run(
            command, capture_output=True, text=True, timeout=60,
            env=environment or self.environment(agent=agent))


def output(completed):
    """The last stdout line as JSON, for one-result commands."""
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else None


class MailTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture().start()
        # Binding an identity creates its mailbox: a recipient must exist.
        self.fixture.run("node", agent="bob")

    def tearDown(self):
        self.fixture.stop()

    def send(self, payload="hello", *extra, agent="alice"):
        return self.fixture.run("send", "bob", payload, *extra, agent=agent)

    def test_a_send_is_accepted_and_the_mailbox_answers_it(self):
        sent = self.send()
        self.assertEqual(sent.returncode, 0, sent.stderr)
        result = output(sent)
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["messageId"])
        waited = self.fixture.run("wait", "--timeout-ms", "0", agent="bob")
        self.assertEqual(waited.returncode, 0, waited.stderr)
        page = output(waited)
        self.assertEqual(page["status"], "ok")
        self.assertEqual(page["messages"][0]["payload"], "hello")
        self.assertEqual(page["messages"][0]["from"]["agentId"], "alice")
        self.assertEqual(page["cursor"]["sequence"], 1)

    def test_an_empty_wait_times_out_without_moving_the_cursor(self):
        waited = self.fixture.run("wait", "--timeout-ms", "100", agent="bob")
        self.assertEqual(waited.returncode, 3, waited.stderr)
        self.assertEqual(output(waited)["status"], "timed-out")

    def test_replay_reads_a_page_from_a_cursor(self):
        self.send("first")
        self.send("second")
        page = output(self.fixture.run("replay", "--after", "1", agent="bob"))
        self.assertEqual([message["payload"] for message in page["messages"]],
                         ["second"])
        self.assertEqual(page["cursor"]["sequence"], 2)

    def test_replay_all_follows_every_page(self):
        for index in range(3):
            self.send("message-%d" % index)
        completed = self.fixture.run("replay", "--all", "--limit", "1",
                                     agent="bob")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payloads = []
        for line in completed.stdout.splitlines():
            if line.strip():
                payloads.extend(message["payload"]
                                for message in json.loads(line)["messages"])
        self.assertEqual(payloads, ["message-0", "message-1", "message-2"])

    def test_ack_acknowledges_what_it_was_given(self):
        sent = output(self.send())
        acked = self.fixture.run("ack", sent["messageId"], agent="bob")
        self.assertEqual(acked.returncode, 0, acked.stderr)
        self.assertEqual(output(acked)["acknowledged"], [sent["messageId"]])
        again = self.fixture.run("ack", sent["messageId"], agent="bob")
        self.assertEqual(output(again)["alreadyAcknowledged"],
                         [sent["messageId"]])

    def test_a_receipt_answers_the_request_id_of_its_send(self):
        request_id = "req-receipt-1"
        sent = output(self.send("hi", "--request-id", request_id))
        self.assertEqual(sent["status"], "accepted")
        receipt = self.fixture.run("receipt", request_id)
        self.assertEqual(receipt.returncode, 0, receipt.stderr)
        self.assertEqual(output(receipt)["messageId"], sent["messageId"])
        missing = self.fixture.run("receipt", "no-such-request")
        self.assertEqual(missing.returncode, 2, missing.stderr)

    def test_a_retried_request_id_reuses_the_message(self):
        first = output(self.send("once", "--request-id", "req-retry-1"))
        second = output(self.send("once", "--request-id", "req-retry-1"))
        self.assertEqual(first["messageId"], second["messageId"])

    def test_a_send_to_an_unknown_node_is_declined(self):
        completed = self.send("far", "--node", "node-elsewhere")
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(output(completed)["status"], "undeliverable")
        self.assertIn("no-such-peer", completed.stderr)

    def test_a_broadcast_reaches_every_local_mailbox(self):
        completed = self.fixture.run("broadcast", "--text", "all hands")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = output(completed)
        self.assertEqual(result["status"], "accepted")
        self.assertGreaterEqual(result["localRecipientCount"], 1)

    def test_text_format_renders_one_line_per_message(self):
        self.send("rendered")
        completed = self.fixture.run("--format", "text", "wait",
                                     "--timeout-ms", "0", agent="bob")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("#1 alice@", completed.stdout)
        self.assertIn('"rendered"', completed.stdout)
        self.assertTrue(completed.stdout.strip().endswith("cursor 1"))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture().start()

    def tearDown(self):
        self.fixture.stop()

    def test_an_explicit_endpoint_is_used_as_given(self):
        environment = self.fixture.environment(agent="alice")
        environment.pop("PI_SERVICES_DIR")
        completed = self.fixture.run(
            "--endpoint", self.fixture.layout.comms_endpoint_path(), "node",
            agent=None, environment=environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(output(completed)["service"], "agent-comms")

    def test_a_wrong_token_is_refused(self):
        path = os.path.join(self.fixture.runtime, "wrong.json")
        data = EndpointFile(self.fixture.layout.comms_endpoint_path()).read()
        data["token"] = "not-the-token"
        EndpointFile(path).publish(data)
        completed = self.fixture.run("--endpoint", path, "node",
                                     agent="alice")
        self.assertEqual(completed.returncode, 6, completed.stderr)
        self.assertIn("bad token", completed.stderr)

    def test_a_missing_service_exits_before_sending(self):
        self.fixture.stop()
        completed = self.fixture.run("send", "bob", "hi")
        self.assertEqual(completed.returncode, 4, completed.stderr)
        self.assertEqual(completed.stdout, "")

    def test_a_stale_entry_exits_before_sending(self):
        path = ServiceDescriptor(self.fixture.services, "agent-comms",
                                 "pi-daemon").path()
        with open(path, encoding="utf-8") as opened:
            data = json.load(opened)
        data["pid"] = 2 ** 22 + 7
        with open(path, "w", encoding="utf-8") as opened:
            json.dump(data, opened)
        completed = self.fixture.run("send", "bob", "hi")
        self.assertEqual(completed.returncode, 4, completed.stderr)

    def test_no_identity_is_a_usage_error(self):
        completed = self.fixture.run("node", agent=None,
                                     environment=self.fixture.environment(
                                         agent=None))
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("agent id", completed.stderr)

    def test_a_hosted_session_names_itself(self):
        environment = self.fixture.environment(agent=None,
                                               PI_HOSTED_SESSION="pi-hosted")
        completed = self.fixture.run("node", agent=None, environment=environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(output(completed)["agent"]["agentId"], "pi-hosted")

    def test_a_registry_that_is_not_an_object_is_refused(self):
        # A malformed registry must not crash: the refusal stays a usage error.
        state = os.path.join(self.fixture.runtime, "state")
        os.makedirs(os.path.join(state, "sessions"), exist_ok=True)
        session = os.path.join(self.fixture.runtime, "session.jsonl")
        with open(os.path.join(state, "sessions", "sessions.json"), "w",
                  encoding="utf-8") as opened:
            json.dump(["not", "a", "registry"], opened)
        environment = self.fixture.environment(
            agent=None, PI_DAEMON_STATE_DIR=state, PI_SESSION_FILE=session)
        completed = self.fixture.run("node", agent=None, environment=environment)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("agent id", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_a_session_the_registry_names_uses_that_name(self):
        state = os.path.join(self.fixture.runtime, "state")
        os.makedirs(os.path.join(state, "sessions"), exist_ok=True)
        session = os.path.join(self.fixture.runtime, "session.jsonl")
        with open(os.path.join(state, "sessions", "sessions.json"), "w",
                  encoding="utf-8") as opened:
            # The non-matching entry comes first, so a lookup that returns
            # the first name blindly cannot pass this.
            json.dump({"files": {"other": session + ".other",
                                 "pi-registered": session}}, opened)
        environment = self.fixture.environment(
            agent=None, PI_DAEMON_STATE_DIR=state, PI_SESSION_FILE=session)
        completed = self.fixture.run("node", agent=None, environment=environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(output(completed)["agent"]["agentId"], "pi-registered")

    def test_an_unknown_option_is_a_usage_error(self):
        # Exit 2 is the service declining, so a typo must not borrow it.
        completed = self.fixture.run("send", "--to", "bob", "hi")
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("error", completed.stderr)

    def test_peer_list_speaks_json(self):
        completed = self.fixture.run("peer", "list")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIsInstance(output(completed), (dict, list))


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture().start()

    def tearDown(self):
        self.fixture.stop()

    def test_spawn_launches_an_agent_and_reports_its_identity(self):
        directory = tempfile.mkdtemp(dir=SCRATCH)
        completed = self.fixture.run(
            "spawn", "--agent-id", "kid", directory, "--",
            sys.executable, "-c", "pass")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = output(completed)
        self.assertEqual(result["status"], "spawned")
        self.assertEqual(result["agent"]["agentId"], "kid")

    def test_a_service_without_a_launcher_answers_method_not_found(self):
        bare = BareComms().start()
        try:
            completed = bare.run("spawn", "--agent-id", "kid", SCRATCH, "--",
                                 sys.executable, "-c", "pass")
            self.assertEqual(completed.returncode, 5, completed.stderr)
            self.assertIn("-32601", completed.stderr)
        finally:
            bare.stop()


class BareComms:
    """Comms alone, with no launcher: the method is simply not published."""

    def __init__(self):
        base = tempfile.mkdtemp(dir=SCRATCH)
        self.runtime = os.path.join(base, "runtime")
        self.services = os.path.join(base, "services")
        self.layout = RuntimeLayout(os.path.join(base, "state"), self.runtime)
        self.config = ServiceConfig.load()
        self.listener = None
        self.service = None

    def start(self):
        store = MailboxStore(self.layout.comms_mailbox_path(), self.config,
                             "node-bare")
        self.service = CommsService(store, self.config, "tok", "node-bare")
        self.service.start()
        self.listener = RpcListener(self.config, RpcCodec(), self.service)
        self.listener.start()
        host, port = self.listener.address()
        self.service.serve_at(host, port)
        EndpointFile(self.layout.comms_endpoint_path()).publish(
            {"host": host, "port": port, "token": "tok",
             "instance": "bare", "pid": os.getpid()})
        ServiceDescriptor(self.services, "agent-comms", "pi-daemon").publish(
            "pi-rpc/1", 1, self.layout.comms_endpoint_path(),
            ["pi-rc", "daemon-start"], "bare", os.getpid())
        return self

    def run(self, *arguments):
        environment = dict(os.environ)
        environment.pop("PI_COMMS_ENDPOINT", None)
        environment["PI_SERVICES_DIR"] = self.services
        environment["PI_COMMS_AGENT_ID"] = "alice"
        return subprocess.run([sys.executable, CLI, *arguments],
                              capture_output=True, text=True, timeout=60,
                              env=environment)

    def stop(self):
        if self.listener is not None:
            self.listener.stop()
        if self.service is not None:
            self.service.stop()


class AmbiguityTests(unittest.TestCase):
    """A request written and then cut off is not a plain failure."""

    def test_a_dropped_send_exits_ambiguous_with_the_request_id(self):
        server = DroppingServer()
        try:
            base = tempfile.mkdtemp(dir=SCRATCH)
            path = os.path.join(base, "comms.json")
            EndpointFile(path).publish({"host": "127.0.0.1",
                                        "port": server.port,
                                        "token": "tok"})
            environment = dict(os.environ)
            environment["PI_COMMS_AGENT_ID"] = "alice"
            environment["PI_COMMS_ENDPOINT"] = path
            completed = subprocess.run(
                [sys.executable, CLI, "send", "bob", "hi",
                 "--request-id", "req-lost"],
                capture_output=True, text=True, timeout=60,
                env=environment)
        finally:
            server.stop()
        self.assertEqual(completed.returncode, 7, completed.stderr)
        self.assertIn("req-lost", completed.stderr)


class DroppingServer(threading.Thread):
    """Answers the greeting, then closes on the next request."""

    def __init__(self):
        super().__init__(daemon=True)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.socket.settimeout(0.2)
        self.port = self.socket.getsockname()[1]
        self.running = True
        self.start()

    def run(self):
        codec = RpcCodec()
        while self.running:
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                stream = connection.makefile("rb")
                hello = codec.decode(stream.readline())
                connection.sendall(codec.encode(codec.result(
                    hello["id"], {"service": "agent-comms", "version": 1,
                                  "maxFrameBytes": 65536,
                                  "agent": {"nodeId": "n",
                                            "agentId": "alice"},
                                  "headSequence": 0})))
                codec.decode(stream.readline())
            except (OSError, ValueError):
                pass
            finally:
                connection.close()

    def stop(self):
        self.running = False
        try:
            self.socket.close()
        except OSError:
            pass
        self.join(5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
