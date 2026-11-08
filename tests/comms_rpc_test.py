#!/usr/bin/env python3
"""Identity-bound agent messaging over real sockets (pi-rpc/1).
Zero dependencies (stdlib only)."""

import json
import os
import shutil
import socket
import sys
import tempfile
import unittest

from collections import deque

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "service"))

from daemon_service.comms.mailbox_store import MailboxStore
from daemon_service.comms.service import CommsService
from daemon_service.config import ServiceConfig
from daemon_service.runtime_layout import RuntimeLayout
from daemon_service.transport.json_line_stream import JsonLineStream
from daemon_service.transport.rpc_codec import (CAPACITY, INVALID_PARAMS,
                                                INVALID_REQUEST,
                                                METHOD_NOT_FOUND,
                                                PARSE_ERROR, UNAUTHORIZED,
                                                RpcCodec)
from daemon_service.transport.rpc_listener import RpcListener

TOKEN = "test-token"
NODE = "local"


class Client:
    """A test consumer: one socket, its ids and its notification queue."""

    def __init__(self, address, agent_id=None, token=TOKEN, version=1,
                 node_id=None, greeting=True):
        self._codec = RpcCodec()
        self._socket = socket.create_connection(address, timeout=5.0)
        self._socket.settimeout(5.0)
        self._stream = JsonLineStream(self._socket, 65536, 8192)
        self._next = 0
        self._pending = deque()
        self.greeting = {}
        if greeting:
            params = {"token": token, "version": version}
            if agent_id is not None:
                params["agent"] = {"agentId": agent_id}
            if node_id is not None:
                params["agent"]["nodeId"] = node_id
            self.greeting = self._exchange("rpc.hello", params)

    def call(self, method, params=None):
        return self._exchange(method, params or {})

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


class Harness:
    """One daemon-side comms stack on a scratch root."""

    def __init__(self, **overrides):
        self.base = tempfile.mkdtemp(prefix="pi-comms-rpc-")
        layout = RuntimeLayout(os.path.join(self.base, "state"),
                              os.path.join(self.base, "run"))
        self.config = ServiceConfig.load(overrides=overrides)
        self.store = MailboxStore(layout.comms_mailbox_path(), self.config,
                                  NODE)
        self.service = CommsService(self.store, self.config, TOKEN, NODE)
        self.service.start()
        self.listener = RpcListener(self.config, RpcCodec(), self.service)
        self.listener.start()
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

    def test_a_send_to_a_foreign_node_is_undeliverable(self):
        sender = self.harness.client("a")
        self.reserve("b")
        answer = sender.result("comms.send", {
            "requestId": "r1", "to": {"nodeId": "peer", "agentId": "b"},
            "kind": "text", "payload": {"n": 1}})
        self.assertEqual(answer["status"], "undeliverable")
        self.assertEqual(answer["reason"], "remote-node")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
