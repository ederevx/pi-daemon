#!/usr/bin/env python3
"""Identity-bound agent messaging over real sockets (pi-rpc/1).
Zero dependencies (stdlib only)."""

import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

from collections import deque

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "service"))

from daemon_service.comms.mailbox_store import MailboxStore
from daemon_service.comms.peer_auth import PeerAuthenticator
from daemon_service.comms.peer_config_store import PeerConfigStore
from daemon_service.comms.peer_manager import PeerManager
from daemon_service.comms.service import CommsService
from daemon_service.comms.spawn_ledger import SpawnLedger
from daemon_service.config import ServiceConfig
from daemon_service.runtime_layout import RuntimeLayout
from daemon_service.sessions.error import SessionError
from daemon_service.sessions.launcher import SessionLauncher
from daemon_service.transport.json_line_stream import JsonLineStream
from daemon_service.transport.rpc_codec import (CAPACITY, INVALID_PARAMS,
                                                INVALID_REQUEST,
                                                METHOD_NOT_FOUND,
                                                PARSE_ERROR, UNAUTHORIZED,
                                                UNAVAILABLE, RpcCodec)
from daemon_service.transport.rpc_listener import RpcListener

TOKEN = "test-token"
NODE = "local"


class Client:
    """A test consumer: one socket, its ids and its notification queue."""

    def __init__(self, address, agent_id=None, token=TOKEN, version=1,
                 node_id=None, greeting=True, peer=None):
        self._codec = RpcCodec()
        self._socket = socket.create_connection(address, timeout=5.0)
        self._socket.settimeout(5.0)
        self._stream = JsonLineStream(self._socket, 65536, 8192)
        self._next = 0
        self._pending = deque()
        self.greeting = {}
        if greeting:
            params = {"token": token, "version": version}
            if peer is not None:
                params = {"version": version, "peer": peer}
            elif agent_id is not None:
                params["agent"] = {"agentId": agent_id}
                if node_id is not None:
                    params["agent"]["nodeId"] = node_id
            self.greeting = self._exchange("rpc.hello", params)

    def call(self, method, params=None):
        return self._exchange(method, params or {})

    def send_request(self, method, params=None):
        """Write one request and leave its reply to `receive`."""
        self._next += 1
        request_id = f"c{self._next}"
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method,
                     "params": params or {}})
        return request_id

    def notify(self, method, params=None):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def result(self, method, params=None):
        answer = self.call(method, params)
        if "error" in answer:
            raise AssertionError(f"{method} failed: {answer['error']}")
        return answer["result"]

    def notification(self):
        message = self._read()
        return message["method"], message["params"]

    def receive(self):
        return self._read()

    def frame(self, message):
        return self._codec.encode(message)

    def close(self):
        try:
            self._socket.close()
        except OSError:
            pass

    def _exchange(self, method, params):
        self._next += 1
        request_id = f"c{self._next}"
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method,
                     "params": params})
        while True:
            message = self._read()
            if message.get("id") == request_id:
                return message
            self._pending.append(message)

    def _read(self):
        if self._pending:
            return self._pending.popleft()
        return self._codec.decode(self._stream.read())

    def _write(self, message):
        self._socket.sendall(self._codec.encode(message))


class StubSessions:
    """The sessions domain, reduced to what the launcher asks of it."""

    def __init__(self):
        self.starts = []
        self.created = True
        self.failure = None

    def start(self, name, dir_, argv, cols, rows, env=None, env_once=None):
        self.starts.append({"name": name, "dir": dir_, "argv": list(argv),
                            "cols": cols, "rows": rows,
                            "env": dict(env or {}),
                            "env_once": dict(env_once or {})})
        if self.failure is not None:
            raise SessionError(self.failure)
        return {"name": name, "created": self.created,
                "session": {"name": name, "pid": 4242}}


class FakeTunnel:
    """A tunnel that is up at a port nothing listens on: no ssh in tests."""

    def __init__(self):
        self.started = 0
        self.closed = 0
        self._address = ("127.0.0.1", 1)

    def start(self, _peer, cancel=None):
        self.started += 1
        return self._address

    def address(self):
        return self._address

    def close(self):
        self.closed += 1
        return True


class Harness:
    """One daemon-side comms stack on a scratch root."""

    def __init__(self, spawns=False, peers=None, **overrides):
        self.base = tempfile.mkdtemp(prefix="pi-comms-rpc-")
        layout = RuntimeLayout(os.path.join(self.base, "state"),
                              os.path.join(self.base, "run"))
        self.config = ServiceConfig.load(overrides=overrides)
        self.store = MailboxStore(layout.comms_mailbox_path(), self.config,
                                  NODE)
        self.sessions = StubSessions() if spawns else None
        ledger = None
        launcher = None
        if spawns:
            ledger = SpawnLedger(layout.comms_spawns_path(), self.config, NODE)
            launcher = SessionLauncher(self.sessions, self.config)
        self.service = CommsService(self.store, self.config, TOKEN, NODE,
                                    ledger, launcher)
        self.peer_store = None
        self.credentials = layout.comms_peer_credentials_path()
        if peers is not None:
            self.peer_store = PeerConfigStore(layout.comms_peers_path(),
                                             self.config)
            self.service.use_peers(PeerManager(
                self.config, self.store, self.peer_store,
                PeerAuthenticator(self.config, self.peer_store,
                                  reader=lambda _path: peers),
                self.service.deliver_peer, NODE,
                tunnel_factory=FakeTunnel), credentials=self.credentials)
        self.service.start()
        self.listener = RpcListener(self.config, RpcCodec(), self.service)
        self.listener.start()
        host, port = self.listener.address()
        self.service.serve_at(host, port)
        self.clients = []

    def address(self):
        return self.listener.address()

    def client(self, agent_id=None, **options):
        client = Client(self.address(), agent_id, **options)
        self.clients.append(client)
        return client

    def close(self):
        for client in self.clients:
            client.close()
        self.listener.stop()
        self.service.stop()
        shutil.rmtree(self.base, ignore_errors=True)


class SocketCase(unittest.TestCase):

    def setUp(self):
        self.harness = self.make_harness()
        self.addCleanup(self.harness.close)

    def make_harness(self, **overrides):
        return Harness(**overrides)

    def agent(self, agent_id):
        return {"agentId": agent_id}

    def reserve(self, *names):
        """A mailbox exists once its identity has been presented."""
        for name in names:
            self.harness.client(name).close()

    def raw(self):
        """A socket the test drives frame by frame."""
        return Client(self.harness.address(), greeting=False)


class BindingTests(SocketCase):

    def test_hello_binds_an_identity_and_its_head(self):
        client = self.harness.client("a")
        self.assertNotIn("error", client.greeting)
        greeting = client.greeting["result"]
        self.assertEqual(greeting["service"], "agent-comms")
        self.assertEqual(greeting["agent"], {"nodeId": NODE, "agentId": "a"})
        self.assertEqual(greeting["headSequence"], 0)
        self.assertIn("comms.send", greeting["methods"])
        self.assertNotIn("comms.list", greeting["methods"])
        self.assertNotIn("comms.register", greeting["methods"])
        self.assertTrue(self.harness.store.exists("a"))

    def test_a_wrong_token_is_refused_and_the_socket_closes(self):
        client = self.harness.client("a", token="wrong")
        self.assertEqual(client.greeting["error"]["code"], UNAUTHORIZED)
        self.assertFalse(self.harness.store.exists("a"))

    def test_an_unknown_version_is_refused_after_the_token(self):
        client = self.harness.client("a", version=99)
        self.assertEqual(client.greeting["error"]["code"], INVALID_PARAMS)

    def test_a_foreign_node_cannot_bind(self):
        client = self.harness.client("a", node_id="peer")
        self.assertEqual(client.greeting["error"]["code"], INVALID_PARAMS)

    def test_a_hello_over_the_mailbox_cap_names_the_capacity(self):
        self.harness = self.make_harness(comms_recipient_cap=1)
        self.addCleanup(self.harness.close)
        self.harness.client("a")
        client = self.harness.client("b")
        self.assertEqual(client.greeting["error"]["code"], CAPACITY)
        self.assertEqual(client.greeting["id"], "c1")

    def test_hello_without_an_identity_is_refused(self):
        client = self.harness.client(None)
        self.assertIn("error", client.greeting)

    def test_a_first_request_that_is_not_a_hello_is_unauthorized(self):
        client = self.raw()
        answer = client.call("comms.replay", {})
        self.assertEqual(answer["error"]["code"], UNAUTHORIZED)
        self.assertEqual(answer["id"], "c1")
        with self.assertRaises(ConnectionError):
            client._read()

    def test_an_unknown_method_is_method_not_found(self):
        client = self.harness.client("a")
        answer = client.call("comms.list", {})
        self.assertEqual(answer["error"]["code"], METHOD_NOT_FOUND)
        self.assertEqual(answer["id"], "c2")

    def test_two_connections_share_one_mailbox(self):
        first = self.harness.client("a")
        second = self.harness.client("a")
        self.assertEqual(first.greeting["result"]["agent"],
                         second.greeting["result"]["agent"])
        client_b = self.harness.client("b")
        client_b.result("rpc.subscribe", {"topics": ["comms.message"]})
        first.result("comms.send", {"requestId": "r1", "to": self.agent("b"),
                                    "kind": "text", "payload": {"n": 1}})
        self.assertEqual(client_b.notification()[1]["payload"], {"n": 1})


class ProfileTests(SocketCase):

    def test_a_malformed_frame_answers_parse_error_and_keeps_the_connection(
            self):
        client = self.harness.client("a")
        client._socket.sendall(b"{not json\n")
        answer = client._read()
        self.assertEqual(answer["error"]["code"], PARSE_ERROR)
        self.assertIn("comms.replay", client.greeting["result"]["methods"])
        self.assertEqual(client.result("comms.replay", {})["status"], "ok")

    def test_a_batch_is_an_invalid_request(self):
        client = self.harness.client("a")
        client._socket.sendall(b"[{\"jsonrpc\":\"2.0\",\"id\":\"x\"}]\n")
        answer = client._read()
        self.assertEqual(answer["error"]["code"], INVALID_REQUEST)
        self.assertEqual(client.result("comms.replay", {})["status"], "ok")

    def test_a_request_id_of_the_wrong_type_is_refused(self):
        client = self.harness.client("a")
        client._write({"jsonrpc": "2.0", "id": {"bad": True},
                       "method": "comms.replay", "params": {}})
        answer = client._read()
        self.assertEqual(answer["error"]["code"], INVALID_REQUEST)
        self.assertIsNone(answer["id"])


class MessagingTests(SocketCase):

    def test_mail_reaches_an_agent_that_was_offline(self):
        sender = self.harness.client("a")
        self.reserve("b")
        sender.result("comms.send", {"requestId": "r1",
                                     "to": self.agent("b"), "kind": "text",
                                     "payload": {"body": "hi"}})
        receiver = self.harness.client("b")
        page = receiver.result("comms.replay", {})
        self.assertEqual([m["payload"] for m in page["messages"]],
                         [{"body": "hi"}])
        self.assertEqual(page["cursor"],
                         {"agent": {"nodeId": NODE, "agentId": "b"},
                          "sequence": 1})

    def test_a_cursor_from_another_mailbox_is_refused(self):
        sender = self.harness.client("a")
        self.reserve("b")
        sender.result("comms.send", {"requestId": "r1", "to": self.agent("b"),
                                     "kind": "text", "payload": {"n": 1}})
        receiver = self.harness.client("b")
        page = receiver.result("comms.replay", {})
        page["cursor"]["agent"]["agentId"] = "a"
        answer = receiver.call("comms.replay", {"cursor": page["cursor"]})
        self.assertEqual(answer["error"]["code"], INVALID_PARAMS)

    def test_a_subscribed_agent_receives_mail_in_order(self):
        sender = self.harness.client("a")
        receiver = self.harness.client("b")
        self.reserve("c")
        receiver.result("rpc.subscribe", {"topics": ["comms.message"]})
        for index in range(3):
            sender.result("comms.send", {"requestId": f"r{index}",
                                         "to": self.agent("b"),
                                         "kind": "text", "payload": {"n": index}})
        received = [receiver.notification()[1]["payload"]["n"]
                    for _ in range(3)]
        self.assertEqual(received, [0, 1, 2])

    def test_an_unsubscribed_connection_gets_no_notifications(self):
        sender = self.harness.client("a")
        receiver = self.harness.client("b")
        sender.result("comms.send", {"requestId": "r1",
                                     "to": self.agent("b"), "kind": "text",
                                     "payload": {"n": 1}})
        self.assertEqual(len(receiver.result("comms.replay", {})["messages"]),
                         1)
        answer = receiver.result("rpc.unsubscribe",
                                 {"topics": ["comms.message"]})
        self.assertEqual(answer["topics"], [])

    def test_a_retried_request_id_does_not_deliver_twice(self):
        sender = self.harness.client("a")
        self.reserve("b")
        params = {"requestId": "r1", "to": self.agent("b"), "kind": "text",
                  "payload": {"n": 1}}
        first = sender.result("comms.send", params)
        again = sender.result("comms.send", params)
        self.assertEqual(first["messageId"], again["messageId"])
        receiver = self.harness.client("b")
        self.assertEqual(len(receiver.result("comms.replay", {})["messages"]),
                         1)

    def test_a_retried_undeliverable_send_answers_its_first_outcome(self):
        sender = self.harness.client("a")
        params = {"requestId": "r1", "to": self.agent("nobody"),
                  "kind": "text", "payload": {"n": 1}}
        first = sender.result("comms.send", params)
        again = sender.result("comms.send", params)
        self.assertEqual(first, again)
        self.assertEqual(again["status"], "undeliverable")

    def test_a_request_id_with_new_content_is_invalid_params(self):
        sender = self.harness.client("a")
        self.reserve("b")
        sender.result("comms.send", {"requestId": "r1", "to": self.agent("b"),
                                     "kind": "text", "payload": {"n": 1}})
        answer = sender.call("comms.send", {"requestId": "r1",
                                            "to": self.agent("b"),
                                            "kind": "text",
                                            "payload": {"n": 2}})
        self.assertEqual(answer["error"]["code"], INVALID_PARAMS)

    def test_a_zero_ttl_is_invalid_params_and_keeps_the_connection(self):
        sender = self.harness.client("a")
        self.reserve("b")
        answer = sender.call("comms.send", {"requestId": "r1",
                                            "to": self.agent("b"),
                                            "kind": "text",
                                            "payload": {"n": 1},
                                            "ttlSeconds": 0})
        self.assertEqual(answer["error"]["code"], INVALID_PARAMS)
        self.assertEqual(sender.result("comms.replay", {})["status"], "ok")

    def test_a_send_to_an_unconfigured_peer_is_undeliverable(self):
        sender = self.harness.client("a")
        self.reserve("b")
        answer = sender.result("comms.send", {
            "requestId": "r1", "to": {"nodeId": "peer", "agentId": "b"},
            "kind": "text", "payload": {"n": 1}})
        self.assertEqual(answer["status"], "undeliverable")
        self.assertEqual(answer["reason"], "no-such-peer")

    def test_acknowledgement_is_shared_between_connections(self):
        sender = self.harness.client("a")
        self.reserve("b")
        sender.result("comms.send", {"requestId": "r1", "to": self.agent("b"),
                                    "kind": "result", "payload": {"ok": 1}})
        first = self.harness.client("b")
        second = self.harness.client("b")
        message_id = first.result("comms.replay", {})["messages"][0][
            "messageId"]
        self.assertEqual(first.result("comms.ack", {"messageIds": [message_id]}
                                      )["acknowledged"], [message_id])
        self.assertEqual(second.result("comms.ack", {"messageIds": [message_id]}
                                       )["alreadyAcknowledged"], [message_id])

    def test_a_reconnect_replays_from_the_saved_cursor(self):
        sender = self.harness.client("a")
        sender.result("comms.send", {"requestId": "r1", "to": self.agent("b"),
                                     "kind": "text", "payload": {"n": 1}})
        first = self.harness.client("b")
        page = first.result("comms.replay", {})
        first.close()
        sender.result("comms.send", {"requestId": "r2", "to": self.agent("b"),
                                     "kind": "text", "payload": {"n": 2}})
        second = self.harness.client("b")
        replayed = second.result("comms.replay", {"cursor": page["cursor"]})
        self.assertEqual([m["payload"]["n"] for m in replayed["messages"]],
                         [2])

    def test_a_broadcast_reaches_connected_and_offline_mailboxes(self):
        sender = self.harness.client("a")
        connected = self.harness.client("b")
        connected.result("rpc.subscribe", {"topics": ["comms.message"]})
        answer = sender.result("comms.broadcast", {
            "requestId": "b1", "scope": "local", "kind": "text",
            "payload": {"announcement": True}})
        self.assertEqual(answer["status"], "accepted")
        self.assertEqual(answer["localRecipientCount"], 1)
        self.assertEqual(connected.notification()[1]["payload"],
                         {"announcement": True})
        offline = self.harness.client("c")
        self.assertEqual(len(offline.result("comms.replay", {})["messages"]),
                         0)

    def test_an_undeliverable_send_is_a_result_not_an_error(self):
        sender = self.harness.client("a")
        answer = sender.result("comms.send", {"requestId": "r1",
                                              "to": self.agent("nobody"),
                                              "kind": "text",
                                              "payload": {}})
        self.assertEqual(answer["status"], "undeliverable")
        self.assertEqual(answer["reason"], "unknown-recipient")

    def test_a_bad_request_is_invalid_params(self):
        sender = self.harness.client("a")
        answer = sender.call("comms.send", {"to": self.agent("b"),
                                            "kind": "text"})
        self.assertEqual(answer["error"]["code"], INVALID_PARAMS)

    def test_a_unicode_payload_round_trips(self):
        sender = self.harness.client("a")
        self.reserve("b")
        payload = {"body": "héllo — 世界"}
        sender.result("comms.send", {"requestId": "r1", "to": self.agent("b"),
                                     "kind": "text", "payload": payload})
        receiver = self.harness.client("b")
        self.assertEqual(receiver.result("comms.replay", {})["messages"][0]
                         ["payload"], payload)


class CapacityTests(SocketCase):

    def make_harness(self, **overrides):
        overrides.setdefault("comms_messages_per_recipient", 1)
        return Harness(**overrides)

    def test_a_full_mailbox_answers_busy_and_keeps_the_connection(self):
        sender = self.harness.client("a")
        self.reserve("b")
        sender.result("comms.send", {"requestId": "r1", "to": self.agent("b"),
                                     "kind": "text", "payload": {"n": 1}})
        answer = sender.call("comms.send", {"requestId": "r2",
                                            "to": self.agent("b"),
                                            "kind": "text",
                                            "payload": {"n": 2}})
        self.assertEqual(answer["error"]["code"], CAPACITY)
        self.assertEqual(sender.result("comms.replay", {})["status"], "ok")
        receiver = self.harness.client("b")
        self.assertEqual(len(receiver.result("comms.replay", {})["messages"]),
                         1)


class FrameTests(SocketCase):

    def make_harness(self, **overrides):
        overrides.setdefault("frame_bytes", 2048)
        overrides.setdefault("comms_frame_margin", 1024)
        overrides.setdefault("comms_payload_bytes", 1024)
        return Harness(**overrides)

    def test_replay_paginates_within_the_frame_limit(self):
        sender = self.harness.client("a")
        self.reserve("b")
        for index in range(8):
            sender.result("comms.send", {"requestId": f"r{index}",
                                         "to": self.agent("b"),
                                         "kind": "text",
                                         "payload": {"body": "x" * 400}})
        receiver = self.harness.client("b")
        seen = []
        cursor = None
        for _ in range(20):
            answer = receiver.call("comms.replay",
                                   {} if cursor is None else {"cursor": cursor})
            self.assertLessEqual(len(receiver.frame(answer)), 2048)
            self.assertEqual(answer["result"]["status"], "ok")
            seen.extend(message["payload"]["body"]
                        for message in answer["result"]["messages"])
            cursor = answer["result"]["cursor"]
            if not answer["result"]["more"]:
                break
        self.assertEqual(len(seen), 8)

    def test_a_payload_that_cannot_fit_a_notification_is_refused(self):
        sender = self.harness.client("a")
        self.reserve("b")
        answer = sender.call("comms.send", {"requestId": "r1",
                                            "to": self.agent("b"),
                                            "kind": "text",
                                            "payload": {"body": "x" * 1000}})
        self.assertEqual(answer["error"]["code"], INVALID_PARAMS)


class SlowPeerTests(SocketCase):

    def make_harness(self, **overrides):
        overrides.setdefault("comms_outbound_queue", 2)
        return Harness(**overrides)

    def test_a_peer_that_stops_reading_cannot_stall_the_others(self):
        sender = self.harness.client("a")
        slow = self.harness.client("slow")
        slow.result("rpc.subscribe", {"topics": ["comms.message"]})
        fast = self.harness.client("fast")
        fast.result("rpc.subscribe", {"topics": ["comms.message"]})
        sender.result("comms.send", {"requestId": "s0", "to": self.agent("slow"),
                                     "kind": "text", "payload": {"n": 0}})
        self.assertEqual(slow.notification()[1]["payload"], {"n": 0})
        for index in range(1, 8):
            sender.result("comms.send", {"requestId": f"s{index}",
                                         "to": self.agent("slow"),
                                         "kind": "text",
                                         "payload": {"body": "x" * 600}})
        sender.result("comms.send", {"requestId": "f1", "to": self.agent("fast"),
                                     "kind": "text", "payload": {"n": 7}})
        self.assertEqual(fast.notification()[1]["payload"], {"n": 7})

class WaitTests(SocketCase):

    def test_wait_is_a_published_method(self):
        client = self.harness.client("a")
        self.assertIn("comms.wait", client.greeting["result"]["methods"])

    def test_a_wait_returns_mail_the_mailbox_already_holds(self):
        self.reserve("b")
        sender = self.harness.client("a")
        sender.result("comms.send", {"requestId": "r1",
                                     "to": self.agent("b"), "kind": "chat",
                                     "payload": {"text": "hi"}})
        client = self.harness.client("b")
        answer = client.result("comms.wait", {"timeoutMs": 1000})
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(len(answer["messages"]), 1)
        self.assertEqual(answer["messages"][0]["sequence"], 1)
        self.assertEqual(answer["cursor"]["sequence"], 1)
        self.assertEqual(answer["cursor"]["agent"]["agentId"], "b")
        self.assertFalse(answer["more"])

    def test_a_pending_wait_is_answered_by_arriving_mail(self):
        receiver = self.harness.client("b")
        sender = self.harness.client("a")
        request_id = receiver.send_request("comms.wait", {"timeoutMs": 4000})
        answers = []

        def read():
            answers.append(receiver.receive())

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        time.sleep(0.1)
        sender.result("comms.send", {"requestId": "r1",
                                     "to": self.agent("b"), "kind": "chat",
                                     "payload": {"text": "hello"}})
        reader.join(5.0)
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["id"], request_id)
        self.assertEqual(answers[0]["result"]["status"], "ok")
        self.assertEqual(answers[0]["result"]["messages"][0]["sequence"], 1)

    def test_a_wait_without_mail_times_out(self):
        client = self.harness.client("b")
        answer = client.result("comms.wait", {"timeoutMs": 50})
        self.assertEqual(answer["status"], "timed-out")
        self.assertEqual(answer["messages"], [])
        self.assertEqual(answer["cursor"], {"agent": {"nodeId": NODE,
                                                      "agentId": "b"},
                                            "sequence": 0})

    def test_cancel_answers_a_pending_wait(self):
        client = self.harness.client("b")
        request_id = client.send_request("comms.wait", {"timeoutMs": 4000})
        client.notify("rpc.cancel", {"id": request_id})
        answer = client.receive()
        self.assertEqual(answer["id"], request_id)
        self.assertEqual(answer["error"]["code"], -32800)

    def test_a_malformed_cancel_keeps_the_connection(self):
        client = self.harness.client("b")
        for request_id in ([1], {"id": 1}, True, 1.5, None):
            client.notify("rpc.cancel", {"id": request_id})
        self.assertEqual(client.result("comms.wait", {"timeoutMs": 50})
                         ["status"], "timed-out")

    def test_cancel_as_a_request_is_not_in_the_profile(self):
        client = self.harness.client("b")
        answer = client.call("rpc.cancel", {"id": "c1"})
        self.assertEqual(answer["error"]["code"], METHOD_NOT_FOUND)

    def test_shutdown_answers_a_pending_wait(self):
        client = self.harness.client("b")
        request_id = client.send_request("comms.wait", {"timeoutMs": 4000})
        self.harness.service.begin_shutdown()
        self.harness.listener.stop()
        answer = client.receive()
        self.assertEqual(answer["id"], request_id)
        self.assertEqual(answer["error"]["code"], UNAVAILABLE)

    def test_a_wait_answers_a_cursor_the_mailbox_no_longer_holds(self):
        self.reserve("b")
        sender = self.harness.client("a")
        sender.result("comms.send", {"requestId": "r1",
                                     "to": self.agent("b"), "kind": "chat",
                                     "payload": {"text": "hi"},
                                     "ttlSeconds": 1})
        self.harness.store.expire(now=time.time() + 5)
        client = self.harness.client("b")
        answer = client.result("comms.wait", {
            "timeoutMs": 50,
            "cursor": {"agent": self.agent("b"), "sequence": 0}})
        self.assertIn(answer["status"], ("ok", "cursor-expired"))
        if answer["status"] == "cursor-expired":
            self.assertEqual(answer["oldestAvailableSequence"], 2)

    def test_a_second_wait_on_one_connection_is_capacity(self):
        client = self.harness.client("b")
        client.send_request("comms.wait", {"timeoutMs": 4000})
        second = client.send_request("comms.wait", {"timeoutMs": 4000})
        answer = client.receive()
        self.assertEqual(answer["id"], second)
        self.assertEqual(answer["error"]["code"], CAPACITY)

    def test_a_reused_pending_request_id_is_wait_pending(self):
        client = self.harness.client("b")
        for _ in range(2):
            client._write({"jsonrpc": "2.0", "id": "dup",
                           "method": "comms.wait",
                           "params": {"timeoutMs": 4000}})
        answer = client.receive()
        self.assertEqual(answer["id"], "dup")
        self.assertEqual(answer["error"]["code"], INVALID_PARAMS)
        self.assertEqual(answer["error"]["message"], "wait-pending")
        # The first wait is untouched and still answers on its own.
        client.notify("rpc.cancel", {"id": "dup"})
        self.assertEqual(client.receive()["error"]["code"], -32800)

    def test_a_wait_with_a_foreign_cursor_is_refused(self):
        client = self.harness.client("b")
        answer = client.call("comms.wait", {
            "timeoutMs": 50,
            "cursor": {"agent": self.agent("a"), "sequence": 0}})
        self.assertEqual(answer["error"]["code"], INVALID_PARAMS)

    def test_a_wait_with_a_broken_timeout_is_refused(self):
        client = self.harness.client("b")
        for timeout in (-1, "soon", True, [1]):
            answer = client.call("comms.wait", {"timeoutMs": timeout})
            self.assertEqual(answer["error"]["code"], INVALID_PARAMS)

    def test_a_wait_is_clamped_to_the_configured_maximum(self):
        self.harness = self.make_harness(comms_wait_max=0.05)
        self.addCleanup(self.harness.close)
        client = self.harness.client("b")
        started = time.monotonic()
        answer = client.result("comms.wait", {"timeoutMs": 60000})
        self.assertEqual(answer["status"], "timed-out")
        self.assertLess(time.monotonic() - started, 2.0)


class SpawnTests(SocketCase):

    def make_harness(self, **overrides):
        return Harness(spawns=True, **overrides)

    def spawn(self, client, **params):
        request = {"requestId": "r1", "dir": self.harness.base,
                   "argv": ["/bin/true"], "agentId": "kid"}
        request.update(params)
        return client.call("comms.spawn", request)

    def test_a_spawn_names_the_child_and_its_parent(self):
        client = self.harness.client("a")
        answer = self.spawn(client)
        self.assertNotIn("error", answer)
        result = answer["result"]
        self.assertEqual(result["status"], "spawned")
        self.assertTrue(result["created"])
        self.assertEqual(result["agent"], {"nodeId": NODE, "agentId": "kid"})
        self.assertEqual(result["parent"], {"nodeId": NODE, "agentId": "a"})
        self.assertEqual(result["session"], {"name": "kid"})
        self.assertEqual(len(result["spawnId"]), 32)
        self.assertTrue(self.harness.store.exists("kid"))

    def test_the_child_is_told_where_it_came_from(self):
        client = self.harness.client("a")
        result = self.spawn(client)["result"]
        launched = self.harness.sessions.starts[0]
        self.assertEqual(launched["name"], "kid")
        self.assertEqual(launched["env"]["PI_COMMS_AGENT_ID"], "kid")
        self.assertEqual(launched["env"]["PI_COMMS_NODE_ID"], NODE)
        self.assertEqual(launched["env"]["PI_COMMS_PARENT_AGENT_ID"], "a")
        self.assertEqual(launched["env"]["PI_COMMS_SPAWN_ID"],
                         result["spawnId"])
        host, port = self.harness.address()
        self.assertEqual(launched["env_once"]["PI_COMMS_HOST"], host)
        self.assertEqual(launched["env_once"]["PI_COMMS_PORT"], str(port))

    def test_a_spawn_defaults_its_session_name_to_the_agent(self):
        client = self.harness.client("a")
        self.spawn(client, agentId=None, name="worker")
        result = self.harness.sessions.starts[0]
        self.assertEqual(result["name"], "worker")

    def test_a_retried_spawn_replays_its_verdict(self):
        client = self.harness.client("a")
        first = self.spawn(client)["result"]
        second = self.spawn(client)["result"]
        self.assertEqual(second["spawnId"], first["spawnId"])
        self.assertFalse(second["created"])
        self.assertEqual(second["status"], "spawned")
        self.assertEqual(len(self.harness.sessions.starts), 1)

    def test_one_request_id_with_another_body_is_refused(self):
        client = self.harness.client("a")
        self.spawn(client)
        answer = self.spawn(client, argv=["/bin/false"])
        self.assertEqual(answer["error"]["code"], INVALID_PARAMS)
        self.assertEqual(len(self.harness.sessions.starts), 1)

    def test_a_taken_session_name_leaves_no_mailbox(self):
        self.harness.sessions.created = False
        client = self.harness.client("a")
        refused = self.spawn(client)
        self.assertEqual(refused["error"]["code"], INVALID_PARAMS)
        self.assertEqual(refused["error"]["message"], "name-taken")
        self.assertFalse(self.harness.store.exists("kid"))
        self.harness.sessions.created = True
        retried = self.spawn(client)
        self.assertEqual(retried["result"]["status"], "spawned")
        self.assertEqual(len(self.harness.sessions.starts), 2)

    def test_a_failed_launch_leaves_no_record_or_mailbox(self):
        self.harness.sessions.failure = "spawn-failed"
        client = self.harness.client("a")
        answer = self.spawn(client)
        self.assertEqual(answer["error"]["code"], UNAVAILABLE)
        self.assertEqual(answer["error"]["message"], "spawn-failed")
        self.assertFalse(self.harness.store.exists("kid"))
        self.harness.sessions.failure = None
        retried = self.spawn(client)["result"]
        self.assertEqual(retried["status"], "spawned")
        self.assertTrue(retried["created"])
        self.assertTrue(self.harness.store.exists("kid"))
        self.assertEqual(len(self.harness.sessions.starts), 2)

    def test_a_client_may_not_set_identity_variables(self):
        client = self.harness.client("a")
        answer = self.spawn(client, env={"PI_COMMS_AGENT_ID": "x"})
        self.assertEqual(answer["error"]["code"], INVALID_PARAMS)
        self.assertEqual(self.harness.sessions.starts, [])

    def test_an_agent_cannot_spawn_itself(self):
        client = self.harness.client("a")
        answer = self.spawn(client, agentId="a")
        self.assertEqual(answer["error"]["code"], INVALID_PARAMS)

    def test_a_missing_directory_is_refused(self):
        client = self.harness.client("a")
        answer = self.spawn(client, dir=os.path.join(self.harness.base,
                                                     "missing"))
        self.assertEqual(answer["error"]["code"], INVALID_PARAMS)
        self.assertEqual(answer["error"]["message"], "no-dir")

    def test_a_broken_argv_is_refused(self):
        client = self.harness.client("a")
        for argv in ([], "true", [1], [""]):
            answer = self.spawn(client, argv=argv)
            self.assertEqual(answer["error"]["code"], INVALID_PARAMS)

    def test_spawn_is_not_offered_without_a_launcher(self):
        self.harness = Harness()
        self.addCleanup(self.harness.close)
        client = self.harness.client("a")
        self.assertNotIn("comms.spawn", client.greeting["result"]["methods"])
        answer = client.call("comms.spawn", {"requestId": "r1",
                                             "dir": self.harness.base,
                                             "argv": ["/bin/true"]})
        self.assertEqual(answer["error"]["code"], METHOD_NOT_FOUND)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class PeerWireTests(SocketCase):
    """The peer wire: its own credential, its own methods, its own mail."""

    NODE_B = "b" * 32
    CREDENTIAL = "shared"

    def make_harness(self, **overrides):
        values = {"comms_peer_reconnect_initial": 3600.0,
                  "comms_peer_reconnect_max": 3600.0}
        values.update(overrides)
        return Harness(peers=self.CREDENTIAL, **values)

    def spec(self, **overrides):
        value = {"nodeId": self.NODE_B, "sshTarget": "peer.example",
                 "remotePort": 5000, "localPort": 0,
                 "outboundCredentialFile": os.path.join(
                     self.harness.credentials, "cred"),
                 "inboundCredentialSha256": hashlib.sha256(
                     self.CREDENTIAL.encode("utf-8")).hexdigest()}
        value.update(overrides)
        return value

    def configure(self, **overrides):
        return self.harness.client("op").result("comms.peer.add",
                                                self.spec(**overrides))

    def remote(self, agent_id="b"):
        return {"nodeId": self.NODE_B, "agentId": agent_id}

    def peer_client(self, node_id=None, credential=CREDENTIAL, **options):
        client = Client(self.harness.address(),
                        peer={"nodeId": node_id or self.NODE_B,
                              "credential": credential}, **options)
        self.harness.clients.append(client)
        return client

    def delivery(self, delivery_id="d1", to="b"):
        now = time.time()
        return {"deliveryId": delivery_id,
                "from": {"nodeId": self.NODE_B, "agentId": "a"},
                "to": {"nodeId": NODE, "agentId": to},
                "kind": "text",
                "payload": {"text": "hi"},
                "createdAt": now,
                "expiresAt": now + 60}

    def test_a_peer_gets_its_own_greeting_and_never_an_agent_session(self):
        self.configure()
        peer = self.peer_client()
        self.assertNotIn("error", peer.greeting)
        greeting = peer.greeting["result"]
        self.assertEqual(greeting["nodeId"], NODE)
        self.assertEqual(greeting["peer"], {"nodeId": self.NODE_B})
        self.assertEqual(greeting["topics"], [])
        self.assertIn("comms.peer.deliver", greeting["methods"])
        self.assertIn("comms.peer.ping", greeting["methods"])
        self.assertNotIn("comms.send", greeting["methods"])
        self.assertNotIn("comms.peer.add", greeting["methods"])
        self.assertNotIn("agent", greeting)

    def test_an_agent_is_offered_the_peer_administration_not_the_peer_wire(self):
        self.configure()
        greeting = self.harness.client("op").greeting["result"]
        self.assertIn("comms.peer.add", greeting["methods"])
        self.assertIn("comms.peer.list", greeting["methods"])
        self.assertNotIn("comms.peer.deliver", greeting["methods"])

    def test_a_peer_without_a_configured_verifier_is_refused(self):
        peer = self.peer_client()
        self.assertEqual(peer.greeting["error"]["code"], UNAUTHORIZED)
        self.assertIsNone(self.harness.peer_store.get(self.NODE_B))

    def test_a_peer_with_the_wrong_credential_is_refused(self):
        self.configure()
        peer = self.peer_client(credential="other")
        self.assertEqual(peer.greeting["error"]["code"], UNAUTHORIZED)

    def test_a_peer_hello_cannot_carry_the_daemon_token(self):
        self.configure()
        client = Client(self.harness.address(), agent_id="a")
        client.close()
        answer = Client(self.harness.address(), greeting=False)
        self.harness.clients.append(answer)
        answer._write({"jsonrpc": "2.0", "id": "c1", "method": "rpc.hello",
                       "params": {"version": 1, "token": TOKEN,
                                  "peer": {"nodeId": self.NODE_B,
                                           "credential": self.CREDENTIAL}}})
        self.assertEqual(answer._read()["error"]["code"], INVALID_PARAMS)

    def test_a_peer_hello_cannot_claim_an_agent_identity(self):
        self.configure()
        answer = Client(self.harness.address(), greeting=False)
        self.harness.clients.append(answer)
        answer._write({"jsonrpc": "2.0", "id": "c1", "method": "rpc.hello",
                       "params": {"version": 1, "agent": {"agentId": "a"},
                                  "peer": {"nodeId": self.NODE_B,
                                           "credential": self.CREDENTIAL}}})
        self.assertEqual(answer._read()["error"]["code"], INVALID_PARAMS)

    def test_a_peer_delivers_mail_into_the_local_mailbox(self):
        self.configure()
        self.reserve("b")
        peer = self.peer_client()
        answer = peer.result("comms.peer.deliver",
                             {"delivery": self.delivery()})
        self.assertEqual(answer["status"], "accepted")
        self.assertEqual(answer["sequence"], 1)
        self.assertEqual(answer["deliveryId"], "d1")
        replayed = self.harness.client("b").result("comms.replay", {})
        self.assertEqual(len(replayed["messages"]), 1)
        self.assertEqual(replayed["messages"][0]["from"],
                         {"nodeId": self.NODE_B, "agentId": "a"})

    def test_a_repeated_delivery_answers_the_same_outcome_once(self):
        self.configure()
        self.reserve("b")
        peer = self.peer_client()
        delivery = self.delivery()
        first = peer.result("comms.peer.deliver", {"delivery": delivery})
        again = peer.result("comms.peer.deliver", {"delivery": delivery})
        self.assertEqual(again, first)
        self.assertEqual(len(self.harness.client("b")
                             .result("comms.replay", {})["messages"]), 1)

    def test_a_peer_cannot_reach_an_agent_method(self):
        self.configure()
        peer = self.peer_client()
        answer = peer.call("comms.send", {"requestId": "r1",
                                          "to": self.agent("a"),
                                          "kind": "text",
                                          "payload": {"n": 1}})
        self.assertEqual(answer["error"]["code"], METHOD_NOT_FOUND)

    def test_an_agent_cannot_deliver_as_a_peer(self):
        self.configure()
        answer = self.harness.client("a").call(
            "comms.peer.deliver", {"delivery": self.delivery()})
        self.assertEqual(answer["error"]["code"], METHOD_NOT_FOUND)

    def test_a_removed_peer_cannot_deliver_under_its_old_generation(self):
        self.configure()
        self.reserve("b")
        peer = self.peer_client()
        removed = self.harness.client("op").result("comms.peer.remove",
                                                   {"nodeId": self.NODE_B})
        self.assertEqual(removed["status"], "removed")
        answer = peer.call("comms.peer.deliver", {"delivery": self.delivery()})
        # Gone generation means greet again, not a bad request the worker
        # would retry forever against a link that can never succeed.
        self.assertEqual(answer["error"]["code"], UNAUTHORIZED)
        self.assertEqual(self.harness.client("b").result(
            "comms.replay", {})["messages"], [])

    def test_the_operator_configures_lists_and_removes_a_peer(self):
        operator = self.harness.client("op")
        added = operator.result("comms.peer.add", self.spec())
        self.assertEqual(added["status"], "configured")
        self.assertTrue(added["created"])
        self.assertNotIn("inboundCredentialSha256", added["peer"])
        self.assertNotIn("outboundCredentialFile", added["peer"])
        listed = operator.result("comms.peer.list", {})
        self.assertEqual([peer["nodeId"] for peer in listed["peers"]],
                         [self.NODE_B])
        self.assertEqual(listed["peers"][0]["state"], "down")
        operator.result("comms.peer.remove", {"nodeId": self.NODE_B})
        self.assertEqual(operator.result("comms.peer.list", {})["peers"], [])

    def test_a_peer_specification_is_validated_at_the_wire(self):
        operator = self.harness.client("op")
        for field, value in (("nodeId", "not-a-node"),
                             ("sshTarget", "-oProxyCommand=evil"),
                             ("sshTarget", "two words"),
                             ("sshTarget", "t" * 256),
                             ("remotePort", 0),
                             ("remotePort", 65536),
                             ("localPort", -1),
                             ("localPort", 65536),
                             ("outboundCredentialFile", "relative"),
                             ("outboundCredentialFile", "/" + "c" * 5000),
                             ("outboundCredentialFile", "/run/cred"),
                             ("inboundCredentialSha256", "short")):
            answer = operator.call("comms.peer.add", self.spec(**{field: value}))
            self.assertEqual(answer["error"]["code"], INVALID_PARAMS, field)
        answer = operator.call("comms.peer.add", self.spec(nodeId=NODE))
        self.assertEqual(answer["error"]["code"], INVALID_PARAMS)

    def test_a_remote_send_is_queued_for_its_peer(self):
        self.configure()
        answer = self.harness.client("a").result(
            "comms.send", {"requestId": "r1", "to": self.remote(),
                           "kind": "text", "payload": {"n": 1}})
        self.assertEqual(answer["status"], "queued")
        self.assertTrue(answer["deliveryId"])
        listed = self.harness.client("op").result("comms.peer.list", {})
        self.assertEqual(listed["peers"][0]["queued"], 1)

    def test_a_send_to_a_removed_peer_is_undeliverable(self):
        self.configure()
        self.harness.client("op").result("comms.peer.remove",
                                         {"nodeId": self.NODE_B})
        answer = self.harness.client("a").result(
            "comms.send", {"requestId": "r1", "to": self.remote(),
                           "kind": "text", "payload": {"n": 1}})
        self.assertEqual(answer, {"status": "undeliverable",
                                  "reason": "no-such-peer"})

    def test_a_delivery_on_a_stale_connection_also_says_greet_again(self):
        self.configure()
        peer = self.peer_client()
        self.harness.client("op").result("comms.peer.remove",
                                        {"nodeId": self.NODE_B})
        self.configure()
        answer = peer.call("comms.peer.deliver",
                           {"delivery": self.delivery()})
        # The same answer ping gives, so a worker's steady sends cannot keep
        # a removed generation's link wedged.
        self.assertEqual(answer["error"]["code"], UNAUTHORIZED)
        self.assertIn("peer-stale", answer["error"]["message"])

    def test_a_connection_whose_generation_is_gone_must_greet_again(self):
        self.configure()
        peer = self.peer_client()
        self.assertEqual(peer.result("comms.peer.ping", {})["nodeId"], NODE)
        self.harness.client("op").result("comms.peer.remove",
                                         {"nodeId": self.NODE_B})
        self.configure()
        answer = peer.call("comms.peer.ping", {})
        self.assertEqual(answer["error"]["code"], UNAUTHORIZED)
        self.assertIn("peer-stale", answer["error"]["message"])
