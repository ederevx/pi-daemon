#!/usr/bin/env python3
"""Identity-bound agent messaging over real sockets (pi-rpc/1).
Zero dependencies (stdlib only)."""

import os
import shutil
import socket
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "service"))

from daemon_service.comms.mailbox_store import MailboxStore
from daemon_service.comms.service import CommsService
from daemon_service.config import ServiceConfig
from daemon_service.runtime_layout import RuntimeLayout
from daemon_service.transport.json_line_stream import JsonLineStream
from daemon_service.transport.rpc_codec import (METHOD_NOT_FOUND, UNAUTHORIZED,
                                                RpcCodec)
from daemon_service.transport.rpc_listener import RpcListener

TOKEN = "test-token"


class Client:
    """A test consumer: one socket, its ids and its notification queue."""

    def __init__(self, address, agent_id=None, token=TOKEN, version=1):
        self._codec = RpcCodec()
        self._socket = socket.create_connection(address, timeout=2.0)
        self._socket.settimeout(2.0)
        self._stream = JsonLineStream(self._socket, 65536, 8192)
        self._next = 0
        params = {"token": token, "version": version}
        if agent_id is not None:
            params["agent"] = {"agentId": agent_id}
        self.greeting = self._exchange("rpc.hello", params)
        self.hello_error = self.greeting.get("error")

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
        message = self._codec.decode(self._stream.read())
        return message["method"], message["params"]

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
            message = self._codec.decode(self._stream.read())
            if message.get("id") == request_id:
                return message

    def _write(self, message):
        self._socket.sendall(self._codec.encode(message))


class Harness:
    """One daemon-side comms stack on a scratch root."""

    def __init__(self):
        self.base = tempfile.mkdtemp(prefix="pi-comms-rpc-")
        layout = RuntimeLayout(os.path.join(self.base, "state"),
                              os.path.join(self.base, "run"))
        self.config = ServiceConfig.load(overrides={"relay_tick": 0.01})
        self.store = MailboxStore(layout.comms_mailbox_path(), self.config)
        self.service = CommsService(self.store, self.config, TOKEN)
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
        self.harness = Harness()
        self.addCleanup(self.harness.close)

    def agent(self, agent_id):
        return {"agentId": agent_id}

    def reserve(self, *names):
        """A mailbox exists once its identity has been presented."""
        for name in names:
            self.harness.client(name).close()


class BindingTests(SocketCase):

    def test_hello_binds_an_identity_and_its_cursor(self):
        client = self.harness.client("a")
        self.assertNotIn("error", client.greeting)
        greeting = client.greeting["result"]
        self.assertEqual(greeting["service"], "agent-comms")
        self.assertEqual(greeting["agent"], {"nodeId": "", "agentId": "a"})
        self.assertEqual(greeting["cursor"], 0)
        self.assertIn("comms.send", greeting["methods"])
        self.assertNotIn("comms.list", greeting["methods"])
        self.assertNotIn("comms.register", greeting["methods"])
        self.assertTrue(self.harness.store.exists("a"))

    def test_a_wrong_token_is_refused_and_the_socket_closes(self):
        client = self.harness.client("a", token="wrong")
        self.assertEqual(client.greeting["error"]["code"], UNAUTHORIZED)
        self.assertEqual(self.harness.store.identities(), [])

    def test_hello_without_an_identity_is_refused(self):
        client = self.harness.client(None)
        self.assertIn("error", client.greeting)

    def test_an_unknown_method_is_method_not_found(self):
        client = self.harness.client("a")
        answer = client.call("comms.list", {})
        self.assertEqual(answer["error"]["code"], METHOD_NOT_FOUND)

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
        self.assertEqual(page["cursor"], 1)

    def test_a_subscribed_agent_receives_mail_in_order(self):
        sender = self.harness.client("a")
        receiver = self.harness.client("b")
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
        self.assertEqual(answer["error"]["code"], -32602)

    def test_a_unicode_payload_round_trips(self):
        sender = self.harness.client("a")
        self.reserve("b")
        payload = {"body": "héllo — 世界"}
        sender.result("comms.send", {"requestId": "r1", "to": self.agent("b"),
                                     "kind": "text", "payload": payload})
        receiver = self.harness.client("b")
        self.assertEqual(receiver.result("comms.replay", {})["messages"][0]
                         ["payload"], payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
