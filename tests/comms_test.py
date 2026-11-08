#!/usr/bin/env python3
"""The agent comms domain: durable mailboxes, channels and receipts.
Zero dependencies (stdlib only)."""

import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "service"))

from daemon_service.comms.error import CommsError
from daemon_service.comms.peer_auth import PeerAuthenticator
from daemon_service.comms.peer_config_store import PeerConfigStore
from daemon_service.comms.peer_delivery import PeerDelivery
from daemon_service.comms.peer_link import PeerLink
from daemon_service.comms.peer_link_error import PeerLinkError
from daemon_service.comms.peer_manager import PeerManager
from daemon_service.comms.peer_operations import PeerOperations
from daemon_service.comms.peer_tunnel import PeerTunnel
from daemon_service.comms.peer_tunnel_error import TunnelError
from daemon_service.atomic_state_file import AtomicStateFile
from daemon_service.comms.mailbox_store import ALL_TARGET, MailboxStore
from daemon_service.comms.service import CommsService
from daemon_service.comms.node_identity import NodeIdentity
from daemon_service.comms.spawn_ledger import SpawnLedger
from daemon_service.comms.wait_registry import WaitRegistry
from daemon_service.config import ServiceConfig
from daemon_service.runtime_layout import RuntimeLayout
from daemon_service.transport.json_line_stream import JsonLineStream
from daemon_service.transport.rpc_codec import (INVALID_PARAMS,
                                                 UNAUTHORIZED, RpcCodec)
from daemon_service.transport.rpc_error import RpcError


class StoreCase(unittest.TestCase):
    """A mailbox store over a scratch state root and a movable clock."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pi-comms-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.layout = RuntimeLayout(os.path.join(self.base, "state"),
                                    os.path.join(self.base, "run"))
        self.now = 1000.0
        self.store = self._store()

    def _store(self, **overrides):
        self.config = ServiceConfig.load(overrides=overrides)
        store = MailboxStore(self.layout.comms_mailbox_path(), self.config,
                             "local", clock=lambda: self.now)
        store.load()
        return store

    def _agent(self, agent_id):
        return {"nodeId": "local", "agentId": agent_id}


class MailboxTests(StoreCase):

    def test_a_reserved_mailbox_is_durable(self):
        self.store.reserve("a")
        self.assertTrue(self.store.exists("a"))
        self.assertTrue(self._store().exists("a"))

    def test_reserving_twice_keeps_the_original_mailbox(self):
        first = self.store.reserve("a")
        second = self.store.reserve("a")
        self.assertEqual(first, second)

    def test_a_provisional_mailbox_is_released(self):
        self.store.reserve("a", provisional=True)
        self.assertTrue(self.store.release("a"))
        self.assertFalse(self.store.exists("a"))
        self.assertFalse(self._store().exists("a"))
        self.assertFalse(self.store.release("a"))

    def test_a_provisional_mailbox_belongs_to_one_launch(self):
        self.store.reserve("a", provisional="s1")
        self.assertFalse(self.store.release("a", "s2"))
        self.assertTrue(self.store.exists("a"))
        self.assertTrue(self.store.release("a", "s1"))
        self.assertFalse(self._store().exists("a"))

    def test_a_launch_never_adopts_another_launch_mailbox(self):
        self.store.reserve("a", provisional="s1")
        self.store.reserve("a", provisional="s2")
        self.assertFalse(self.store.release("a", "s2"))
        self.assertTrue(self.store.exists("a"))

    def test_a_reservation_survives_a_restart(self):
        self.store.reserve("a", provisional="s1")
        restarted = self._store()
        self.assertFalse(restarted.release("a", "s2"))
        self.assertTrue(restarted.exists("a"))
        self.assertTrue(restarted.release("a", "s1"))
        self.assertFalse(self._store().exists("a"))

    def test_mail_for_another_mailbox_keeps_the_reservation(self):
        self.store.reserve("a", provisional="s1")
        self.store.reserve("b")
        self.store.accept(self._agent("b"), "r1", self._agent("b"), "chat",
                          {"text": "hi"}, ttl_seconds=1)
        self.now += 10.0
        self.assertEqual(self.store.expire(), 1)
        self.assertTrue(self._store().release("a", "s1"))
        self.assertFalse(self._store().exists("a"))

    def test_expiry_keeps_the_reservation(self):
        self.store.reserve("a", provisional="s1")
        self.store.reserve("b")
        self.store.accept(self._agent("a"), "r1", self._agent("b"), "chat",
                          {"text": "hi"}, ttl_seconds=1)
        self.now += 10.0
        self.assertEqual(self.store.expire(), 1)
        self.assertTrue(self._store().release("a", "s1"))
        self.assertFalse(self._store().exists("a"))

    def test_a_release_that_cannot_be_written_keeps_the_mailbox(self):
        self.store.reserve("a", provisional="s1")
        with failing_mailbox_writes(after=0):
            with self.assertRaises(OSError):
                self.store.release("a", "s1")
        self.assertTrue(self._store().exists("a"))
        self.assertTrue(self._store().release("a", "s1"))

    def test_a_presented_mailbox_is_never_released(self):
        self.store.reserve("a")
        self.assertFalse(self.store.release("a"))
        self.assertTrue(self.store.exists("a"))

    def test_a_provisional_mailbox_is_adopted_when_presented(self):
        self.store.reserve("a", provisional=True)
        self.store.reserve("a")
        self.assertFalse(self.store.release("a"))
        self.assertTrue(self._store().exists("a"))

    def test_a_provisional_mailbox_that_received_mail_stays(self):
        self.store.reserve("a")
        self.store.reserve("b", provisional=True)
        self.store.accept(self._agent("a"), "r1", self._agent("b"), "chat",
                          {"text": "hi"})
        self.assertFalse(self.store.release("b"))
        self.assertTrue(self.store.exists("b"))
        self.assertTrue(self._store().exists("b"))

    def test_the_mailbox_cap_refuses_rather_than_evicts(self):
        self.store = self._store(comms_recipient_cap=2)
        self.store.reserve("a")
        self.store.reserve("b")
        with self.assertRaises(CommsError) as caught:
            self.store.reserve("c")
        self.assertEqual(caught.exception.code, "busy")
        self.assertFalse(self.store.exists("c"))


class DeliveryTests(StoreCase):

    def setUp(self):
        super().setUp()
        self.store.reserve("a")
        self.store.reserve("b")

    def test_acceptance_is_durable_before_it_is_reported(self):
        result = self.store.accept(self._agent("a"), "r1", self._agent("b"),
                                   "text", {"body": "hello"})
        self.assertEqual(result["status"], "accepted")
        replayed = self._store().replay("b")
        self.assertEqual([m["payload"] for m in replayed["messages"]],
                         [{"body": "hello"}])
        self.assertEqual(replayed["messages"][0]["sequence"], 1)

    def test_sequences_follow_acceptance_order_per_mailbox(self):
        for index in range(3):
            self.store.accept(self._agent("a"), f"r{index}",
                              self._agent("b"), "text", {"n": index})
        messages = self.store.replay("b")["messages"]
        self.assertEqual([m["sequence"] for m in messages], [1, 2, 3])
        self.assertEqual([m["payload"]["n"] for m in messages], [0, 1, 2])

    def test_an_unknown_recipient_is_undeliverable_not_accepted(self):
        result = self.store.accept(self._agent("a"), "r1", self._agent("z"),
                                   "text", {"body": "hi"})
        self.assertEqual(result["status"], "undeliverable")
        self.assertEqual(result["undeliverable"][0]["reason"],
                         "unknown-recipient")

    def test_a_returned_payload_shares_nothing_with_the_store(self):
        payload = {"body": {"nested": [1, 2]}}
        accepted = self.store.accept(self._agent("a"), "r1", self._agent("b"),
                                     "text", payload)
        payload["body"]["nested"].append(3)
        message = self.store.replay("b")["messages"][0]
        message["payload"]["body"]["nested"].append(4)
        stored = self._store().replay("b")["messages"][0]
        self.assertEqual(stored["payload"]["body"]["nested"], [1, 2])
        self.assertEqual(accepted["messages"][0]["to"]["agentId"], "b")

    def test_a_replayed_request_returns_its_first_receipt(self):
        first = self.store.accept(self._agent("a"), "r1", self._agent("b"),
                                  "text", {"body": "hello"})
        again = self.store.accept(self._agent("a"), "r1", self._agent("b"),
                                  "text", {"body": "hello"})
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["receipt"]["accepted"],
                         first["receipt"]["accepted"])
        self.assertEqual(len(self.store.replay("b")["messages"]), 1)

    def test_a_request_id_with_new_content_is_refused(self):
        self.store.accept(self._agent("a"), "r1", self._agent("b"),
                          "text", {"body": "hello"})
        with self.assertRaises(CommsError) as caught:
            self.store.accept(self._agent("a"), "r1", self._agent("b"),
                              "text", {"body": "changed"})
        self.assertEqual(caught.exception.code, "request-conflict")

    def test_the_same_request_id_on_another_node_is_a_different_request(self):
        self.store.accept(self._agent("a"), "r1", self._agent("b"),
                          "text", {"body": "hello"})
        result = self.store.accept({"nodeId": "peer", "agentId": "a"}, "r1",
                                   self._agent("b"), "text",
                                   {"body": "hello"})
        self.assertEqual(result["status"], "accepted")

    def test_changed_options_are_a_different_request(self):
        self.store.accept(self._agent("a"), "r1", self._agent("b"),
                          "text", {"body": "hello"})
        with self.assertRaises(CommsError):
            self.store.accept(self._agent("a"), "r1", self._agent("b"),
                              "text", {"body": "hello"}, ttl_seconds=5)

    def test_a_full_mailbox_refuses_new_mail_and_keeps_the_old(self):
        self.store = self._store(comms_messages_per_recipient=1)
        self.store.reserve("a")
        self.store.reserve("b")
        self.store.accept(self._agent("a"), "r1", self._agent("b"),
                          "text", {"n": 1})
        with self.assertRaises(CommsError) as caught:
            self.store.accept(self._agent("a"), "r2", self._agent("b"),
                              "text", {"n": 2})
        self.assertEqual(caught.exception.code, "busy")
        self.assertEqual(len(self._store().replay("b")["messages"]), 1)

    def test_a_foreign_node_has_no_route_without_a_peer(self):
        self.store.reserve("b")
        result = self.store.accept(self._agent("a"), "r1",
                                   {"nodeId": "peer", "agentId": "b"},
                                   "text", {"body": "hi"})
        self.assertEqual(result["status"], "undeliverable")
        self.assertEqual(result["undeliverable"][0]["reason"], "no-such-peer")
        self.assertEqual(self.store.replay("b")["messages"], [])

    def test_an_oversized_payload_is_refused(self):
        self.store = self._store(comms_payload_bytes=16)
        self.store.reserve("a")
        self.store.reserve("b")
        with self.assertRaises(CommsError) as caught:
            self.store.accept(self._agent("a"), "r1", self._agent("b"),
                              "text", {"body": "x" * 64})
        self.assertEqual(caught.exception.code, "payload-too-large")


class NodeTests(StoreCase):

    def test_mail_addressed_to_this_node_is_local(self):
        node = NodeIdentity(self.layout.comms_node_path(), self.config).load()
        store = MailboxStore(self.layout.comms_mailbox_path(), self.config,
                             node.id(), clock=lambda: self.now)
        store.load()
        store.reserve("a")
        store.reserve("b")
        address = {"nodeId": node.id(), "agentId": "b"}
        result = store.accept({"nodeId": node.id(), "agentId": "a"}, "r1",
                              address, "text", {"n": 1})
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(store.replay("b")["messages"][0]["to"], address)


class EnvelopeBoundsTests(StoreCase):

    def test_mail_that_cannot_fit_a_notification_is_refused(self):
        self.store = self._store(frame_bytes=2048, comms_frame_margin=1024,
                                 comms_payload_bytes=1024)
        self.store.reserve("a")
        self.store.reserve("b")
        with self.assertRaises(CommsError) as caught:
            self.store.accept(self._agent("a"), "r1", self._agent("b"),
                              "text", {"body": "x" * 1200})
        self.assertEqual(caught.exception.code, "payload-too-large")

    def test_a_page_fits_the_frame_whatever_the_message_count(self):
        self.store = self._store(frame_bytes=2048, comms_frame_margin=512,
                                 comms_payload_bytes=1024,
                                 comms_page_messages=64)
        self.store.reserve("a")
        self.store.reserve("b")
        for index in range(10):
            self.store.accept(self._agent("a"), f"r{index}",
                              self._agent("b"), "text",
                              {"body": "x" * 400})
        page = self.store.replay("b")
        self.assertLess(len(page["messages"]), 10)
        self.assertTrue(page["more"])


class BroadcastTests(StoreCase):

    def setUp(self):
        super().setUp()
        for name in ("a", "b", "c"):
            self.store.reserve(name)

    def test_a_broadcast_reaches_every_mailbox_but_the_sender(self):
        result = self.store.accept(self._agent("a"), "b1", ALL_TARGET,
                                   "text", {"n": 1})
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(result["messages"]), 2)
        self.assertEqual(self.store.replay("a")["messages"], [])
        for name in ("b", "c"):
            self.assertEqual(len(self.store.replay(name)["messages"]), 1)

    def test_a_broadcast_retry_keeps_its_first_audience(self):
        first = self.store.accept(self._agent("a"), "b1", ALL_TARGET,
                                  "text", {"n": 1})
        self.store.reserve("d")
        again = self.store.accept(self._agent("a"), "b1", ALL_TARGET,
                                  "text", {"n": 1})
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["receipt"]["accepted"],
                         first["receipt"]["accepted"])
        self.assertEqual(self.store.replay("d")["messages"], [])

    def test_a_broadcast_with_no_audience_is_undeliverable(self):
        self.layout = RuntimeLayout(os.path.join(self.base, "alone", "state"),
                                    os.path.join(self.base, "alone", "run"))
        self.store = self._store()
        self.store.reserve("a")
        result = self.store.accept(self._agent("a"), "b1", ALL_TARGET,
                                   "text", {"n": 1})
        self.assertEqual(result["status"], "undeliverable")


class ReplayTests(StoreCase):

    def setUp(self):
        super().setUp()
        self.store.reserve("a")
        self.store.reserve("b")
        for index in range(5):
            self.store.accept(self._agent("a"), f"r{index}",
                              self._agent("b"), "text", {"n": index})

    def test_a_page_reports_where_to_continue(self):
        page = self.store.replay("b", limit=2)
        self.assertEqual([m["sequence"] for m in page["messages"]], [1, 2])
        self.assertTrue(page["more"])
        following = self.store.replay("b", cursor=page["cursor"], limit=2)
        self.assertEqual([m["sequence"] for m in following["messages"]],
                         [3, 4])

    def test_the_page_size_is_bounded_by_configuration(self):
        self.assertEqual(len(self.store.replay("b", limit=99)["messages"]), 5)
        self.store = self._store(comms_page_messages=3)
        self.store.reserve("a")
        self.store.reserve("b")
        for index in range(5):
            self.store.accept(self._agent("a"), f"x{index}",
                              self._agent("b"), "text", {"n": index})
        self.assertEqual(len(self.store.replay("b", limit=99)["messages"]), 3)

    def test_replay_only_returns_mail_after_the_cursor(self):
        page = self.store.replay("b", cursor=3)
        self.assertEqual([m["sequence"] for m in page["messages"]], [4, 5])
        self.assertFalse(page["more"])


class AcknowledgementTests(StoreCase):

    def setUp(self):
        super().setUp()
        self.store.reserve("a")
        self.store.reserve("b")
        self.store.accept(self._agent("a"), "r1", self._agent("b"),
                          "text", {"n": 1})

    def test_acknowledgement_is_idempotent(self):
        message_id = self.store.replay("b")["messages"][0]["messageId"]
        first = self.store.acknowledge("b", [message_id])
        self.assertEqual(first["acknowledged"], [message_id])
        second = self.store.acknowledge("b", [message_id])
        self.assertEqual(second["acknowledged"], [])
        self.assertEqual(second["alreadyAcknowledged"], [message_id])

    def test_acknowledgement_separates_unknown_from_repeated(self):
        message_id = self.store.replay("b")["messages"][0]["messageId"]
        self.store.acknowledge("b", [message_id])
        result = self.store.acknowledge("b", ["nope", message_id])
        self.assertEqual(result["unknown"], ["nope"])
        self.assertEqual(result["alreadyAcknowledged"], [message_id])

    def test_an_acknowledgement_does_not_hide_an_unexpired_message(self):
        message_id = self.store.replay("b")["messages"][0]["messageId"]
        self.store.acknowledge("b", [message_id])
        self.assertEqual(len(self.store.replay("b")["messages"]), 1)


class ExpiryTests(StoreCase):

    def test_expiry_happens_on_read_not_only_on_a_sweep(self):
        self.store = self._store(comms_message_ttl=10.0)
        self.store.reserve("a")
        self.store.reserve("b")
        self.store.accept(self._agent("a"), "r1", self._agent("b"),
                          "text", {"n": 1})
        self.now += 11.0
        self.assertEqual(self.store.replay("b")["messages"], [])
        self.assertEqual(self._store().replay("b")["messages"], [])

    def test_a_cursorless_read_still_returns_a_retained_result(self):
        self.store.reserve("a")
        self.store.reserve("b")
        self.store.accept(self._agent("a"), "q1", self._agent("b"),
                          "result", {"answer": 42})
        self.store.accept(self._agent("a"), "q2", self._agent("b"),
                          "text", {"n": 2}, ttl_seconds=10)
        self.now += 60.0
        messages = self.store.replay("b")["messages"]
        self.assertEqual([m["payload"] for m in messages], [{"answer": 42}])

    def test_a_cursor_before_the_retained_history_is_expired(self):
        self.store = self._store(comms_message_ttl=5.0)
        self.store.reserve("a")
        self.store.reserve("b")
        self.store.accept(self._agent("a"), "r1", self._agent("b"),
                          "text", {"n": 1})
        self.now += 6.0
        self.store.reserve("a")
        self.store.reserve("b")
        self.store = self._store(comms_message_ttl=5.0)
        self.store.accept(self._agent("a"), "r2", self._agent("b"),
                          "text", {"n": 2})
        stale = self.store.replay("b", cursor=0)
        self.assertEqual(stale["status"], "cursor-expired")
        self.assertEqual(stale["oldestAvailableSequence"], 2)

    def test_results_outlive_messages(self):
        self.store.reserve("a")
        self.store.reserve("b")
        self.store.accept(self._agent("a"), "r1", self._agent("b"),
                          "result", {"ok": True})
        self.now += 200.0
        self.assertEqual(len(self.store.replay("b")["messages"]), 1)

    def test_expired_mail_frees_capacity_for_new_mail(self):
        self.store = self._store(comms_messages_per_recipient=1,
                                 comms_message_ttl=5.0)
        self.store.reserve("a")
        self.store.reserve("b")
        self.store.accept(self._agent("a"), "r1", self._agent("b"),
                          "text", {"n": 1})
        self.now += 6.0
        result = self.store.accept(self._agent("a"), "r2", self._agent("b"),
                                   "text", {"n": 2})
        self.assertEqual(result["status"], "accepted")

    def test_an_acknowledgement_marker_ends_with_its_message(self):
        self.store = self._store(comms_message_ttl=5.0)
        self.store.reserve("a")
        self.store.reserve("b")
        self.store.accept(self._agent("a"), "r1", self._agent("b"),
                          "text", {"n": 1})
        message_id = self.store.replay("b")["messages"][0]["messageId"]
        self.store.acknowledge("b", [message_id])
        self.now += 6.0
        self.assertEqual(self.store.acknowledge("b", [message_id])["unknown"],
                         [message_id])
        with open(self.layout.comms_mailbox_path(),
                  encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored["recipients"]["b"]["acknowledged"], [])

    def test_an_expired_receipt_no_longer_deduplicates(self):
        self.store = self._store(comms_dedupe_ttl=30.0)
        self.store.reserve("a")
        self.store.reserve("b")
        self.store.accept(self._agent("a"), "r1", self._agent("b"),
                          "text", {"n": 1})
        self.now += 31.0
        again = self.store.accept(self._agent("a"), "r1", self._agent("b"),
                                  "text", {"n": 1})
        self.assertEqual(again["status"], "accepted")


class RecoveryTests(StoreCase):

    def test_sequences_and_receipts_survive_a_restart(self):
        self.store.reserve("a")
        self.store.reserve("b")
        self.store.accept(self._agent("a"), "r1", self._agent("b"),
                          "text", {"n": 1})
        reopened = self._store()
        self.assertEqual(reopened.head_sequence("b"), 1)
        again = reopened.accept(self._agent("a"), "r1", self._agent("b"),
                                "text", {"n": 1})
        self.assertEqual(again["status"], "duplicate")

    def test_a_corrupt_store_refuses_to_open(self):
        with open(self.layout.comms_mailbox_path(), "w",
                  encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(CommsError) as caught:
            self._store()
        self.assertEqual(caught.exception.code, "store-corrupt")

    def test_a_broken_sequence_invariant_refuses_to_open(self):
        self.store.reserve("b")
        self.store.accept(self._agent("a"), "r1", self._agent("b"),
                          "text", {"n": 1})
        with open(self.layout.comms_mailbox_path(),
                  encoding="utf-8") as handle:
            document = json.load(handle)
        document["recipients"]["b"]["next_sequence"] = 0
        with open(self.layout.comms_mailbox_path(), "w",
                  encoding="utf-8") as handle:
            json.dump(document, handle)
        with self.assertRaises(CommsError) as caught:
            self._store()
        self.assertEqual(caught.exception.code, "store-corrupt")

    def test_a_broken_receipt_refuses_to_open(self):
        self.store.reserve("a")
        self.store.reserve("b")
        self.store.accept(self._agent("a"), "r1", self._agent("b"),
                          "text", {"n": 1})
        with open(self.layout.comms_mailbox_path(),
                  encoding="utf-8") as handle:
            document = json.load(handle)
        key = next(iter(document["send_receipts"]))
        document["send_receipts"][key]["accepted"] = {"not": "a list"}
        with open(self.layout.comms_mailbox_path(), "w",
                  encoding="utf-8") as handle:
            json.dump(document, handle)
        with self.assertRaises(CommsError):
            self._store()

    def test_an_empty_store_file_is_corruption_not_a_fresh_start(self):
        with open(self.layout.comms_mailbox_path(), "w",
                  encoding="utf-8"):
            pass
        with self.assertRaises(CommsError) as caught:
            self._store()
        self.assertEqual(caught.exception.code, "store-corrupt")

    def test_an_unknown_schema_refuses_to_open(self):
        with open(self.layout.comms_mailbox_path(), "w",
                  encoding="utf-8") as handle:
            json.dump({"version": 99}, handle)
        with self.assertRaises(CommsError):
            self._store()


class NodeIdentityTests(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pi-node-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.config = ServiceConfig.load()
        self.path = os.path.join(self.base, "comms", "node.json")

    def _identity(self):
        return NodeIdentity(self.path, self.config).load()

    def test_an_identity_is_minted_once_and_reused(self):
        first = self._identity().id()
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertEqual(self._identity().id(), first)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_broken_identity_refuses_to_load(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for text in ("", "{", "{}", '{"version": 9, "nodeId": "%s"}'
                     % ("a" * 32),
                     '{"version": 1, "nodeId": "short"}',
                     '{"version": 1, "nodeId": 7}'):
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write(text)
            with self.assertRaises(CommsError) as caught:
                self._identity()
            self.assertEqual(caught.exception.code, "store-corrupt")

    def test_a_missing_file_is_a_fresh_node(self):
        self.assertTrue(self._identity().id())

class GatedStore:
    """A mailbox store whose replay the test can hold, break or answer."""

    def __init__(self):
        self.gate = threading.Event()
        self.gate.set()
        self.calls = []
        self.by_agent = {}
        self.fail = False
        self.explode = False
        self.answer = {"status": "ok", "messages": [], "cursor": 0,
                       "more": False}

    def replay(self, agent_id, cursor=None, limit=None):
        self.calls.append((agent_id, cursor, limit))
        if self.fail:
            raise OSError("disk")
        if self.explode:
            raise RuntimeError("a store bug")
        self.gate.wait(5.0)
        answer = self.by_agent.get(agent_id, self.answer)
        return {"status": answer["status"],
                "messages": [dict(message)
                             for message in answer.get("messages", [])],
                "cursor": answer.get("cursor"),
                "more": bool(answer.get("more")),
                "oldestAvailableSequence":
                    answer.get("oldestAvailableSequence")}


def failing_writes(after=0):
    """The production failure: the disk refuses writes past the first ones."""
    real = AtomicStateFile.write_dict
    state = {"calls": 0}

    def write(store, data):
        state["calls"] += 1
        if state["calls"] > after:
            raise OSError("disk")
        return real(store, data)

    return mock.patch("daemon_service.atomic_state_file.AtomicStateFile"
                      ".write_dict", write)


def failing_mailbox_writes(after=0):
    """Mailboxes persist through the text path, not `write_dict`."""
    real = AtomicStateFile.write
    state = {"calls": 0}

    def write(store, text):
        state["calls"] += 1
        if state["calls"] > after:
            raise OSError("disk")
        return real(store, text)

    return mock.patch("daemon_service.atomic_state_file.AtomicStateFile"
                      ".write", write)


class StubStore:
    """Mailbox ownership, small enough to break on purpose."""

    def __init__(self):
        self.mailboxes = {}
        self.provisional = set()
        self.owners = {}
        self.fail_reserve = False
        self.fail_release = False
        self.released = []

    def exists(self, agent_id):
        return agent_id in self.mailboxes

    def reserve(self, agent_id, provisional=False):
        if self.fail_reserve:
            raise OSError("disk")
        if agent_id not in self.mailboxes:
            self.mailboxes[agent_id] = 0
            if provisional:
                self.provisional.add(agent_id)
                self.owners[agent_id] = provisional
        elif not provisional:
            self.provisional.discard(agent_id)
            self.owners.pop(agent_id, None)
        return {"agentId": agent_id, "nodeId": "local", "headSequence": 0,
                "pending": 0}

    def release(self, agent_id, owner=None):
        if self.fail_release:
            raise OSError("disk")
        if agent_id not in self.provisional:
            return False
        if owner is not None and self.owners.get(agent_id) != owner:
            return False
        self.provisional.discard(agent_id)
        self.owners.pop(agent_id, None)
        self.mailboxes.pop(agent_id, None)
        self.released.append(agent_id)
        return True


class StubLauncher:
    """A launcher that records the spec and answers on demand."""

    def __init__(self):
        self.launched = []
        self.reason = None

    def check(self, spec):
        """The launcher contract: a normalised launch spec, or a refusal."""
        name = (spec.get("name") or spec.get("agentId")).strip()
        return {"ok": True, "spec": {
            "name": name, "dir": spec.get("dir"),
            "argv": list(spec.get("argv") or []),
            "cols": spec.get("cols"), "rows": spec.get("rows"),
            "env": dict(spec.get("env") or {}),
            "env_once": dict(spec.get("envOnce") or {})}}

    def launch(self, spec):
        self.launched.append(dict(spec))
        if self.reason is not None:
            return {"ok": False, "reason": self.reason}
        return {"ok": True, "name": spec["name"], "pid": 4242}


class SpawnLedgerTests(StoreCase):
    """A launch record exists before the launch, and a retry finds it."""

    def setUp(self):
        super().setUp()
        self.ledger = self._ledger()

    def _ledger(self, **overrides):
        self.config = ServiceConfig.load(overrides=overrides)
        ledger = SpawnLedger(self.layout.comms_spawns_path(), self.config,
                             "local", clock=lambda: self.now)
        ledger.load()
        return ledger

    def _parent(self, agent_id="a"):
        return {"nodeId": "local", "agentId": agent_id}

    def _agent(self, agent_id):
        return {"nodeId": "local", "agentId": agent_id}

    def test_an_open_record_is_found_by_its_request(self):
        opened = self.ledger.open(self._parent(), "r1", "d1",
                                  self._agent("kid"), "kid")
        found = self.ledger.find(self._parent(), "r1", "d1")
        self.assertEqual(found["spawnId"], opened["spawnId"])
        self.assertEqual(found["status"], "launching")
        self.assertEqual(found["agent"], self._agent("kid"))
        self.assertEqual(found["session"], {"name": "kid"})

    def test_a_new_request_has_no_record(self):
        self.assertIsNone(self.ledger.find(self._parent(), "r1", "d1"))

    def test_one_request_id_with_another_body_conflicts(self):
        self.ledger.open(self._parent(), "r1", "d1", self._agent("kid"),
                         "kid")
        with self.assertRaises(CommsError) as caught:
            self.ledger.find(self._parent(), "r1", "d2")
        self.assertEqual(caught.exception.code, "request-conflict")

    def test_the_ledger_cap_refuses_rather_than_evicts(self):
        self.ledger = self._ledger(comms_spawn_records=1)
        self.ledger.open(self._parent(), "r1", "d1", self._agent("kid"), "kid")
        with self.assertRaises(CommsError) as caught:
            self.ledger.open(self._parent(), "r2", "d2", self._agent("kid2"),
                             "kid2")
        self.assertEqual(caught.exception.code, "busy")
        self.assertIsNone(self.ledger.find(self._parent(), "r2", "d2"))

    def test_a_launching_record_never_expires(self):
        self.ledger.open(self._parent(), "r1", "d1", self._agent("kid"), "kid")
        self.now += 10 * self.config.comms_dedupe_ttl
        self.assertIsNotNone(self._ledger().find(self._parent(), "r1", "d1"))

    def test_a_settled_record_expires_after_the_ttl(self):
        self.ledger.open(self._parent(), "r1", "d1", self._agent("kid"), "kid")
        self.ledger.settle(self._parent(), "r1", "spawned")
        self.assertIsNotNone(self.ledger.find(self._parent(), "r1", "d1"))
        self.now += 2 * self.config.comms_dedupe_ttl
        self.assertIsNone(self.ledger.find(self._parent(), "r1", "d1"))

    def test_a_settled_record_survives_a_restart(self):
        self.ledger.open(self._parent(), "r1", "d1", self._agent("kid"), "kid")
        self.ledger.settle(self._parent(), "r1", "spawned")
        found = self._ledger().find(self._parent(), "r1", "d1")
        self.assertEqual(found["status"], "spawned")
        self.assertIsNotNone(found["settled"])

    def test_a_restart_settles_an_interrupted_launch(self):
        self.ledger.open(self._parent(), "r1", "d1", self._agent("kid"), "kid")
        self.now += 5.0
        found = self._ledger().find(self._parent(), "r1", "d1")
        self.assertEqual(found["status"], "failed")
        self.assertEqual(found["reason"], "interrupted")
        self.assertEqual(found["settled"], 1005.0)

    def test_a_recovery_that_cannot_be_written_keeps_the_record(self):
        self.ledger.open(self._parent(), "r1", "d1", self._agent("kid"), "kid")
        with failing_writes(after=0):
            with self.assertRaises(CommsError) as caught:
                self._ledger()
        self.assertEqual(caught.exception.code, "store-failed")
        with open(self.layout.comms_spawns_path(), encoding="utf-8") as handle:
            records = json.load(handle)["spawns"]
        self.assertEqual([r["status"] for r in records.values()],
                         ["launching"])
        self.assertEqual(self._ledger().find(self._parent(), "r1", "d1")
                         ["status"], "failed")

    def test_a_discarded_record_is_gone(self):
        self.ledger.open(self._parent(), "r1", "d1", self._agent("kid"), "kid")
        self.ledger.discard(self._parent(), "r1")
        self.assertIsNone(self.ledger.find(self._parent(), "r1", "d1"))
        self.assertIsNone(self._ledger().find(self._parent(), "r1", "d1"))

    def test_the_record_file_is_private(self):
        self.ledger.open(self._parent(), "r1", "d1", self._agent("kid"), "kid")
        mode = os.stat(self.layout.comms_spawns_path()).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_a_settle_that_cannot_be_written_keeps_the_launch_certainty(self):
        ledger = SpawnLedger(self.layout.comms_spawns_path(), self.config,
                             "local", clock=lambda: self.now)
        ledger.load()
        ledger.open(self._parent(), "r1", "d1", self._agent("kid"), "kid")
        with failing_writes():
            with self.assertRaises(CommsError) as caught:
                ledger.settle(self._parent(), "r1", "spawned")
        self.assertEqual(caught.exception.code, "store-failed")
        self.assertEqual(ledger.find(self._parent(), "r1", "d1")["status"],
                         "launching")
        self.assertEqual(self._ledger().find(self._parent(), "r1", "d1")
                         ["status"], "failed")

    def test_a_broken_ledger_refuses_to_load(self):
        with open(self.layout.comms_spawns_path(), "w",
                  encoding="utf-8") as handle:
            handle.write('{"version": 99, "spawns": {}}')
        with self.assertRaises(CommsError) as caught:
            self._ledger()
        self.assertEqual(caught.exception.code, "store-corrupt")


class RecordingSession:
    """A connection stand-in that records how its wait ended."""

    def __init__(self):
        self._lock = threading.Lock()
        self.completed = []
        self.refused = []

    def complete(self, request_id, result):
        with self._lock:
            self.completed.append((request_id, result))

    def refuse(self, request_id, code, message):
        with self._lock:
            self.refused.append((request_id, code, message))


class WaitRegistryTests(StoreCase):

    def setUp(self):
        super().setUp()
        self.store.reserve("a")
        self.store.reserve("b")
        self.session = RecordingSession()

    def _registry(self, store=None, **overrides):
        config = ServiceConfig.load(overrides=overrides)
        registry = WaitRegistry(store or self.store, config)
        registry.start()
        self.addCleanup(registry.stop)
        return registry

    def _until(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def _send(self, request_id="s1", agent_id="b"):
        return self.store.accept(self._agent("a"), request_id,
                                 self._agent(agent_id), "chat", {"text": "hi"})

    def test_mail_already_in_the_mailbox_answers_at_once(self):
        self._send()
        registry = self._registry()
        registry.begin(self.session, "w1", "b", None, None, 5.0)
        self.assertTrue(self._until(lambda: self.session.completed))
        request_id, result = self.session.completed[0]
        self.assertEqual(request_id, "w1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["messages"]), 1)

    def test_a_signal_answers_a_pending_wait(self):
        registry = self._registry()
        registry.begin(self.session, "w1", "b", None, None, 5.0)
        self.assertEqual(registry.pending(), 1)
        self._send()
        registry.signal({"b"})
        self.assertTrue(self._until(lambda: self.session.completed))
        self.assertEqual(self.session.completed[0][1]["status"], "ok")

    def test_a_wait_times_out_and_names_its_position(self):
        registry = self._registry()
        registry.begin(self.session, "w1", "b", 3, None, 0.05)
        self.assertTrue(self._until(lambda: self.session.completed))
        result = self.session.completed[0][1]
        self.assertEqual(result["status"], "timed-out")
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["cursor"], 3)

    def test_a_cancelled_wait_is_answered_cancelled(self):
        registry = self._registry()
        registry.begin(self.session, "w1", "b", None, None, 5.0)
        self.assertTrue(registry.cancel(self.session, "w1"))
        self.assertFalse(registry.cancel(self.session, "w1"))
        self.assertEqual(self.session.refused, [("w1", -32800, "cancelled")])
        self.assertEqual(self.session.completed, [])
        self.assertEqual(registry.pending(), 0)

    def test_one_wait_at_a_time_per_connection(self):
        registry = self._registry(comms_waits_per_connection=1)
        registry.begin(self.session, "w1", "b", None, None, 5.0)
        with self.assertRaises(CommsError) as caught:
            registry.begin(self.session, "w2", "b", None, None, 5.0)
        self.assertEqual(caught.exception.code, "busy")

    def test_the_registry_cap_refuses_a_second_connection(self):
        registry = self._registry(comms_wait_cap=1,
                                  comms_waits_per_connection=1)
        registry.begin(self.session, "w1", "b", None, None, 5.0)
        with self.assertRaises(CommsError) as caught:
            registry.begin(RecordingSession(), "w2", "b", None, None, 5.0)
        self.assertEqual(caught.exception.code, "busy")

    def test_a_dropped_connection_leaves_no_pending_wait(self):
        registry = self._registry()
        registry.begin(self.session, "w1", "b", None, None, 5.0)
        registry.drop(self.session)
        self.assertEqual(registry.pending(), 0)
        self._send()
        registry.signal({"b"})
        time.sleep(0.05)
        self.assertEqual(self.session.completed, [])

    def test_a_pending_wait_is_refused_at_shutdown(self):
        registry = self._registry(comms_wait_max=5.0)
        registry.begin(self.session, "w1", "b", None, None, 5.0)
        registry.stop()
        self.assertEqual(self.session.refused, [("w1", -32003, "closing")])
        self.assertEqual(registry.pending(), 0)

    def test_a_store_failure_refuses_one_wait_and_keeps_answering(self):
        store = GatedStore()
        store.fail = True
        registry = self._registry(store=store)
        registry.begin(self.session, "w1", "b", None, None, 5.0)
        self.assertTrue(self._until(lambda: self.session.refused))
        self.assertEqual(self.session.refused,
                         [("w1", -32003, "unavailable")])
        store.fail = False
        store.answer = {"status": "ok", "messages": [{"sequence": 1}],
                        "cursor": 1, "more": False}
        registry.begin(self.session, "w2", "b", None, None, 5.0)
        self.assertTrue(self._until(lambda: self.session.completed))
        self.assertEqual(self.session.completed[0][1]["messages"],
                         [{"sequence": 1}])

    def test_a_reused_request_id_never_steals_the_replacement(self):
        store = GatedStore()
        store.gate.clear()
        store.answer = {"status": "ok", "messages": [{"sequence": 1}],
                        "cursor": 1, "more": False}
        registry = self._registry(store=store)
        store.by_agent["c"] = {"status": "ok", "messages": [{"sequence": 9}],
                               "cursor": 9, "more": False}
        registry.begin(self.session, "w1", "b", None, None, 5.0)
        self.assertTrue(self._until(lambda: store.calls))
        self.assertTrue(registry.cancel(self.session, "w1"))
        registry.begin(self.session, "w1", "c", None, None, 5.0)
        store.gate.set()
        # The old evaluator finished replay and found a replacement: only the
        # replacement itself may answer, so the thread loops for its own look.
        self.assertTrue(self._until(lambda: len(store.calls) == 2))
        self.assertTrue(self._until(lambda: self.session.completed))
        # The replacement answered with its own mailbox, not the one the
        # cancelled wait was still holding in replay.
        self.assertEqual(self.session.completed,
                         [("w1", {"status": "ok",
                                  "messages": [{"sequence": 9}],
                                  "cursor": 9, "more": False,
                                  "oldestAvailableSequence": None})])
        self.assertEqual(registry.pending(), 0)
        self.assertEqual([refused[1] for refused in self.session.refused],
                         [-32800])

    def test_shutdown_refuses_a_wait_the_thread_is_still_answering(self):
        store = GatedStore()
        store.gate.clear()
        store.answer = {"status": "ok", "messages": [{"sequence": 1}],
                        "cursor": 1, "more": False}
        registry = self._registry(store=store, stop_grace=0.05)
        registry.begin(self.session, "w1", "b", None, None, 5.0)
        self.assertTrue(self._until(lambda: store.calls))
        registry.stop()
        # The replay is still held, and shutdown has already answered.
        self.assertEqual(self.session.refused,
                         [("w1", -32003, "closing")])
        self.assertEqual(registry.pending(), 0)
        self.assertFalse(registry.cancel(self.session, "w1"))
        store.gate.set()
        registry.stop()
        self.assertEqual(self.session.completed, [])
        self.assertEqual(len(self.session.refused), 1)

    def test_an_unexpected_store_bug_refuses_only_its_wait(self):
        store = GatedStore()
        store.explode = True
        registry = self._registry(store=store)
        registry.begin(self.session, "w1", "b", None, None, 5.0)
        self.assertTrue(self._until(lambda: self.session.refused))
        self.assertEqual(self.session.refused,
                         [("w1", -32003, "unavailable")])
        store.explode = False
        store.answer = {"status": "ok", "messages": [{"sequence": 7}],
                        "cursor": 7, "more": False}
        registry.begin(self.session, "w2", "b", None, None, 5.0)
        self.assertTrue(self._until(lambda: self.session.completed))
        self.assertEqual(self.session.completed[0][1]["messages"],
                         [{"sequence": 7}])

    def test_a_reused_pending_request_id_is_refused(self):
        registry = self._registry(comms_waits_per_connection=2)
        registry.begin(self.session, "w1", "b", None, None, 5.0)
        with self.assertRaises(CommsError) as caught:
            registry.begin(self.session, "w1", "b", None, None, 5.0)
        self.assertEqual(caught.exception.code, "wait-pending")

    def test_a_cursor_behind_the_retained_history_is_reported(self):
        store = GatedStore()
        store.answer = {"status": "cursor-expired",
                        "oldestAvailableSequence": 4, "cursor": 0}
        registry = self._registry(store=store)
        registry.begin(self.session, "w1", "b", 0, None, 5.0)
        self.assertTrue(self._until(lambda: self.session.completed))
        result = self.session.completed[0][1]
        self.assertEqual(result["status"], "cursor-expired")
        self.assertEqual(result["oldestAvailableSequence"], 4)

    def test_a_wait_after_shutdown_is_refused(self):
        registry = self._registry()
        registry.stop()
        with self.assertRaises(CommsError) as caught:
            registry.begin(self.session, "w1", "b", None, None, 5.0)
        self.assertEqual(caught.exception.code, "closing")


class SpawnServiceTests(unittest.TestCase):
    """The spawn path over stub collaborators, so failures are reachable."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pi-comms-spawn-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.config = ServiceConfig.load()
        self.store = StubStore()
        self.launcher = StubLauncher()
        self.ledger = SpawnLedger(os.path.join(self.base, "spawns.json"),
                                  self.config, "local")
        self.ledger.load()
        self.service = CommsService(self.store, self.config, "token", "local",
                                    self.ledger, self.launcher)
        self.service.serve_at("127.0.0.1", 1234)
        self.sender = {"nodeId": "local", "agentId": "parent"}
        self.spec = {"agentId": "kid", "name": None, "dir": self.base,
                     "argv": ["/bin/true"], "cols": None, "rows": None,
                     "env": None, "envOnce": None}

    def _spawn(self, **changes):
        spec = dict(self.spec, **changes)
        return self.service.spawn(self.sender, "r1", spec)

    def test_a_ledger_that_cannot_open_answers_a_store_failure(self):
        with failing_writes():
            with self.assertRaises(CommsError) as caught:
                self._spawn()
        self.assertEqual(caught.exception.code, "store-failed")
        self.assertEqual(self.launcher.launched, [])
        self.assertIsNone(self.ledger.find(self.sender, "r1", "digest"))

    def test_a_failed_reservation_leaves_no_record_or_mailbox(self):
        self.store.fail_reserve = True
        with self.assertRaises(CommsError) as caught:
            self._spawn()
        self.assertEqual(caught.exception.code, "store-failed")
        self.assertIsNone(self.ledger.find(self.sender, "r1", "digest"))
        self.assertEqual(self.launcher.launched, [])

    def test_an_uncertain_settlement_is_replayed_not_relaunched(self):
        with failing_writes(after=1):
            with self.assertRaises(CommsError) as caught:
                self._spawn()
        self.assertEqual(caught.exception.code, "store-failed")
        self.assertEqual(len(self.launcher.launched), 1)
        retried = self._spawn()
        self.assertEqual(retried["status"], "launching")
        self.assertFalse(retried["created"])
        self.assertEqual(len(self.launcher.launched), 1)

    def test_a_refused_launch_never_releases_an_earlier_launch_mailbox(self):
        self.assertEqual(self._spawn()["status"], "spawned")
        self.launcher.reason = "name-taken"
        with self.assertRaises(CommsError) as caught:
            self.service.spawn(self.sender, "r2", dict(self.spec))
        self.assertEqual(caught.exception.code, "name-taken")
        self.assertEqual(list(self.store.mailboxes), ["kid"])
        self.assertEqual(self.store.released, [])
        self.launcher.reason = None
        self.assertEqual(self.service.spawn(self.sender, "r3",
                                            dict(self.spec))["status"],
                         "spawned")

    def test_a_rollback_that_cannot_be_written_is_a_store_failure(self):
        self.launcher.reason = "name-taken"
        with failing_writes(after=1):
            with self.assertRaises(CommsError) as caught:
                self._spawn()
        self.assertEqual(caught.exception.code, "store-failed")
        self.assertEqual(self.store.released, ["kid"])
        self.launcher.reason = None
        self.assertEqual(self._spawn()["status"], "launching")
        self.assertFalse(self._spawn()["created"])

    def test_a_release_that_cannot_be_written_keeps_the_retry_record(self):
        self.launcher.reason = "name-taken"
        self.store.fail_release = True
        with self.assertRaises(CommsError) as caught:
            self._spawn()
        self.assertEqual(caught.exception.code, "store-failed")
        self.store.fail_release = False
        self.launcher.reason = None
        replayed = self._spawn()
        self.assertEqual(replayed["status"], "launching")
        self.assertFalse(replayed["created"])
        self.assertEqual(len(self.launcher.launched), 1)

    def test_a_refused_launch_releases_its_provisional_mailbox(self):
        self.launcher.reason = "name-taken"
        with self.assertRaises(CommsError) as caught:
            self._spawn()
        self.assertEqual(caught.exception.code, "name-taken")
        self.assertEqual(self.store.mailboxes, {})
        self.assertEqual(self.store.released, ["kid"])
        self.launcher.reason = None
        self.assertEqual(self._spawn()["status"], "spawned")
        self.assertEqual(len(self.launcher.launched), 2)

    def test_a_padded_session_name_is_recorded_trimmed(self):
        verdict = self._spawn(agentId="kid2", name=" worker ")
        self.assertEqual(verdict["session"], {"name": "worker"})
        self.assertEqual(self.launcher.launched[0]["name"], "worker")
        self.assertEqual(self._spawn(agentId="kid2", name=" worker ")
                         ["session"], {"name": "worker"})

    def test_a_caller_one_shot_environment_reaches_the_launch(self):
        self._spawn(envOnce={"TERM": "xterm"})
        launched = self.launcher.launched[0]
        self.assertEqual(launched["env_once"]["TERM"], "xterm")
        self.assertEqual(launched["env_once"]["PI_COMMS_PORT"], "1234")
        self.assertEqual(launched["env"]["PI_COMMS_AGENT_ID"], "kid")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# -- peers -----------------------------------------------------------------

NODE_A = "a" * 32
NODE_B = "b" * 32
NODE_C = "c" * 32


def _await(predicate, timeout=2.0):
    """Poll a worker's effect; the worker owns its own thread."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class FakeTunnel:
    """A tunnel that is already up; the test owns its address."""

    def __init__(self, failure=None):
        self._failure = failure
        self.started = 0
        self.closed = 0
        self._address = ("127.0.0.1", 4242)

    def start(self, peer, cancel=None):
        self.started += 1
        if self._failure is not None:
            raise self._failure
        return self._address

    def address(self):
        return self._address

    def close(self):
        self.closed += 1
        return True


class FakeLink:
    """A link whose answers the test dictates, and whose requests it records."""

    def __init__(self, answers=None, failure=None):
        self.answers = list(answers or [])
        self.failure = failure
        self.requests = []
        self.opened = 0
        self.closed = 0
        self.node_id = ""
        self._alive = False

    def open(self, address, peer, credential, timeout=None):
        self.opened += 1
        self.node_id = peer["nodeId"]
        self._alive = True
        return {"nodeId": self.node_id}

    def request(self, method, params, timeout=None):
        self.requests.append((method, params))
        if method == "comms.peer.ping":
            return {"nodeId": self.node_id, "serverTime": 0}
        if self.failure is not None:
            raise self.failure
        if self.answers:
            return self.answers.pop(0)
        delivery_id = params.get("delivery", {}).get("deliveryId")
        return {"status": "accepted", "deliveryId": delivery_id,
                "messageId": "m", "sequence": 1}

    def alive(self):
        return self._alive

    def interrupt(self):
        self.interrupted = getattr(self, "interrupted", 0) + 1

    def close(self):
        self._alive = False
        self.closed += 1


class StubCredentials:
    def outbound_credential(self, peer):
        return "shared"


class PeerCase(unittest.TestCase):
    """A peer-capable store with a movable clock and one configured peer."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="pi-comms-peer-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.layout = RuntimeLayout(os.path.join(self.base, "state"),
                                    os.path.join(self.base, "run"))
        self.now = 1000.0
        self.store = self._peer_store()
        self.store.reserve("a")
        self.store.reserve("b")

    def _peer_store(self, **overrides):
        values = {"comms_peer_reconnect_initial": 0.01,
                  "comms_peer_reconnect_max": 0.02,
                  "comms_peer_probe_interval": 0.05,
                  "comms_peer_request_timeout": 0.2,
                  "comms_peer_ready_timeout": 0.2}
        values.update(overrides)
        self.config = ServiceConfig.load(overrides=values)
        store = MailboxStore(self.layout.comms_mailbox_path(), self.config,
                             NODE_A, clock=lambda: self.now)
        store.load()
        return store

    def _agent(self, agent_id):
        return {"nodeId": NODE_A, "agentId": agent_id}

    def remote(self, agent_id="b"):
        return {"nodeId": NODE_B, "agentId": agent_id}

    def route(self, generation="g1"):
        return {"nodeId": NODE_B, "generation": generation}

    def send(self, request_id="r1", peer=None, ttl=None, text="hi"):
        return self.store.accept(self._agent("a"), request_id,
                                 self.remote(), "text", {"text": text},
                                 ttl, peer=self.route() if peer is None
                                 else peer)

    def delivery(self, delivery_id="d1", to="b", expires=None, text="hi"):
        return {"deliveryId": delivery_id,
                "from": {"nodeId": NODE_B, "agentId": "a"},
                "to": {"nodeId": NODE_A, "agentId": to},
                "kind": "text",
                "payload": {"text": text},
                "createdAt": self.now,
                "expiresAt": self.now + 60 if expires is None else expires}


class PeerStoreTests(PeerCase):

    def test_a_remote_send_commits_a_receipt_and_a_queued_delivery(self):
        result = self.send()
        self.assertEqual(result["status"], "queued")
        receipt = self.store.receipt(self._agent("a"), "r1")
        self.assertEqual(receipt["status"], "queued")
        self.assertEqual(receipt["deliveryId"], result["deliveryId"])
        reloaded = self._peer_store()
        due = reloaded.due(NODE_B, "g1", 10)
        self.assertEqual([record["delivery"]["deliveryId"] for record in due],
                         [result["deliveryId"]])
        self.assertEqual(reloaded.queued(NODE_B), 1)

    def test_a_remote_request_id_cannot_be_reused_for_a_local_send(self):
        self.send()
        with self.assertRaises(CommsError) as caught:
            self.store.accept(self._agent("a"), "r1", self.remote(), "text",
                              {"text": "other"})
        self.assertEqual(caught.exception.code, "request-conflict")

    def test_a_remote_send_replays_its_first_answer(self):
        first = self.send()
        self.store.settle_peer(first["deliveryId"], "g1",
                               {"status": "accepted",
                                "deliveryId": first["deliveryId"],
                                "messageId": "m", "sequence": 3})
        again = self.send()
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["initial"]["status"], "queued")
        self.assertEqual(again["initial"]["deliveryId"],
                         first["deliveryId"])

    def test_an_attempt_is_recorded_before_the_link_sees_it(self):
        first = self.send()
        self.assertTrue(self.store.mark_attempt(first["deliveryId"], "g1",
                                                self.now))
        reloaded = self._peer_store()
        due = reloaded.due(NODE_B, "g1", 5)
        self.assertEqual(due[0]["attempts"], 1)
        self.assertFalse(reloaded.mark_attempt("missing", "g1", self.now))

    def test_a_settled_delivery_is_not_attempted_again(self):
        first = self.send()
        self.store.settle_peer(first["deliveryId"], "g1",
                               {"status": "accepted",
                                "deliveryId": first["deliveryId"],
                                "messageId": "m", "sequence": 1})
        self.assertEqual(self.store.due(NODE_B, "g1", 5), [])
        self.assertEqual(self.store.receipt(self._agent("a"), "r1")
                         ["status"], "accepted")

    def test_an_unattempted_delivery_that_expires_is_undeliverable(self):
        self.send(ttl=10)
        self.now += 11
        receipt = self.store.receipt(self._agent("a"), "r1")
        self.assertEqual(receipt["status"], "undeliverable")
        self.assertEqual(receipt["reason"], "retention-expired")
        self.assertEqual(self.store.due(NODE_B, "g1", 5), [])

    def test_an_attempted_delivery_that_expires_is_unknown(self):
        first = self.send(ttl=10)
        self.store.mark_attempt(first["deliveryId"], "g1", self.now)
        self.now += 11
        receipt = self.store.receipt(self._agent("a"), "r1")
        self.assertEqual(receipt["status"], "unknown")
        self.assertEqual(receipt["reason"], "delivery-unconfirmed")

    def test_an_unconfigured_peer_is_recorded_truthfully(self):
        result = self.store.accept(self._agent("a"), "r1", self.remote(),
                                   "text", {"text": "hi"}, None, peer=None)
        self.assertEqual(result["status"], "undeliverable")
        self.assertEqual(result["undeliverable"][0]["reason"], "no-such-peer")
        again = self.store.accept(self._agent("a"), "r1", self.remote(),
                                  "text", {"text": "hi"}, None, peer=None)
        self.assertEqual(again["status"], "duplicate")

    def test_removal_settles_attempted_and_unattempted_mail_truthfully(self):
        first = self.send("r1")
        second = self.send("r2")
        self.store.mark_attempt(second["deliveryId"], "g1", self.now)
        counts = self.store.cancel_peer(NODE_B, "g1", "peer-removed")
        self.assertEqual(counts, {"settled": 2, "undeliverable": 1,
                                  "unknown": 1})
        self.assertEqual(self.store.receipt(self._agent("a"), "r1")
                         ["reason"], "peer-removed")
        self.assertEqual(self.store.receipt(self._agent("a"), "r2")
                         ["status"], "unknown")
        self.assertEqual(self.store.due(NODE_B, "g1", 5), [])

    def test_an_import_commits_one_envelope_and_one_outcome(self):
        first = self.store.accept_peer(NODE_B, self.delivery())
        self.assertEqual(first["outcome"]["status"], "accepted")
        self.assertIsNotNone(first["envelope"])
        again = self.store.accept_peer(NODE_B, self.delivery())
        self.assertEqual(again["outcome"], first["outcome"])
        self.assertIsNone(again["envelope"])
        messages = self.store.replay("b")["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["from"], {"nodeId": NODE_B,
                                               "agentId": "a"})
        self.assertEqual(messages[0]["sequence"], 1)

    def test_an_import_conflict_is_refused(self):
        self.store.accept_peer(NODE_B, self.delivery())
        with self.assertRaises(CommsError) as caught:
            self.store.accept_peer(NODE_B, self.delivery(text="different"))
        self.assertEqual(caught.exception.code, "request-conflict")

    def test_an_expired_import_cannot_reappear(self):
        delivery = self.delivery(expires=self.now - 1)
        result = self.store.accept_peer(NODE_B, delivery)
        self.assertEqual(result["outcome"]["status"], "expired")
        self.assertIsNone(result["envelope"])
        self.assertEqual(self.store.replay("b")["messages"], [])

    def test_an_unknown_recipient_refusal_is_durable(self):
        first = self.store.accept_peer(NODE_B, self.delivery(to="nobody"))
        self.assertEqual(first["outcome"]["reason"], "unknown-recipient")
        again = self.store.accept_peer(NODE_B, self.delivery(to="nobody"))
        self.assertEqual(again["outcome"], first["outcome"])

    def test_an_import_for_a_third_node_is_refused(self):
        delivery = self.delivery()
        delivery["to"]["nodeId"] = NODE_C
        with self.assertRaises(CommsError) as caught:
            self.store.accept_peer(NODE_B, delivery)
        self.assertEqual(caught.exception.code, "bad-request")

    def test_a_forged_source_is_refused(self):
        delivery = self.delivery()
        delivery["from"]["nodeId"] = NODE_C
        with self.assertRaises(CommsError) as caught:
            self.store.accept_peer(NODE_B, delivery)
        self.assertEqual(caught.exception.code, "bad-request")

    def test_a_schema_one_snapshot_still_loads(self):
        path = self.layout.comms_mailbox_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        AtomicStateFile(path, 0o600, 5, 0.01).write_dict({
            "version": 1,
            "recipients": {"b": {"next_sequence": 2, "messages": [],
                                 "acknowledged": [], "provisional": False}},
            "send_receipts": {}})
        store = self._peer_store()
        self.assertTrue(store.exists("b"))
        self.assertEqual(store.head_sequence("b"), 2)
        self.assertEqual(store.queued(NODE_B), 0)
        self.assertEqual(store.accept_peer(NODE_B, self.delivery())
                         ["outcome"]["sequence"], 3)

    def test_a_corrupt_mailbox_snapshot_fails_closed(self):
        path = self.layout.comms_mailbox_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{")
        with self.assertRaises(CommsError) as caught:
            self._peer_store()
        self.assertEqual(caught.exception.code, "store-corrupt")

    def test_the_outbox_is_bounded(self):
        store = self._peer_store(comms_peer_outbox_records=1)
        store.reserve("a")
        store.accept(self._agent("a"), "r1", self.remote(), "text",
                     {"text": "hi"}, None, peer=self.route())
        with self.assertRaises(CommsError) as caught:
            store.accept(self._agent("a"), "r2", self.remote(), "text",
                         {"text": "hi"}, None, peer=self.route())
        self.assertEqual(caught.exception.code, "busy")


class PeerBoundTests(PeerCase):
    """The bounds and the ordering the store owes a peer it cannot reach."""

    def test_the_route_is_resolved_under_the_store_lock(self):
        done = threading.Event()

        def cancel():
            self.store.cancel_peer(NODE_B, "g1", "peer-removed")
            done.set()

        def route(node_id):
            thread = threading.Thread(target=cancel)
            thread.start()
            # A removal cannot commit between the routing and the delivery.
            self.assertFalse(done.wait(0.2))
            return {"nodeId": node_id, "generation": "g1"}

        result = self.store.accept(self._agent("a"), "r1", self.remote(),
                                   "text", {"text": "hi"}, None, route=route)
        self.assertEqual(result["status"], "queued")
        self.assertTrue(done.wait(1.0))
        self.assertEqual(self.store.receipt(self._agent("a"), "r1")
                         ["reason"], "peer-removed")

    def test_the_outbox_cap_counts_only_mail_still_queued(self):
        store = self._peer_store(comms_peer_outbox_records=1)
        store.reserve("a")
        first = store.accept(self._agent("a"), "r1", self.remote(), "text",
                             {"text": "hi"}, None, peer=self.route())
        store.settle_peer(first["deliveryId"], "g1",
                          {"status": "accepted",
                           "deliveryId": first["deliveryId"],
                           "messageId": "m", "sequence": 1})
        again = store.accept(self._agent("a"), "r2", self.remote(), "text",
                             {"text": "hi"}, None, peer=self.route())
        self.assertEqual(again["status"], "queued")
        with self.assertRaises(CommsError) as caught:
            store.accept(self._agent("a"), "r3", self.remote(), "text",
                         {"text": "hi"}, None, peer=self.route())
        self.assertEqual(caught.exception.code, "busy")

    def test_removal_reports_attempted_mail_as_unknown_and_late_settles_do_nothing(self):
        first = self.send()
        self.store.mark_attempt(first["deliveryId"], "g1", self.now)
        counts = self.store.cancel_peer(NODE_B, "g1", "peer-removed")
        self.assertEqual(counts, {"settled": 1, "undeliverable": 0,
                                  "unknown": 1})
        self.assertEqual(self.store.receipt(self._agent("a"), "r1")
                         ["status"], "unknown")
        self.assertTrue(self.store.settle_peer(
            first["deliveryId"], "g1",
            {"status": "accepted", "deliveryId": first["deliveryId"],
             "messageId": "m", "sequence": 1}))
        self.assertEqual(self.store.receipt(self._agent("a"), "r1")
                         ["status"], "unknown")

    def test_an_orphaned_generation_is_settled_rather_than_retained(self):
        self.send()
        counts = self.store.cancel_orphans({(NODE_B, "g2")}, "peer-removed")
        self.assertEqual(counts["undeliverable"], 1)
        self.assertEqual(self.store.queued(NODE_B), 0)
        self.assertEqual(self.store.receipt(self._agent("a"), "r1")
                         ["status"], "undeliverable")

    def test_an_inbound_delivery_cannot_outlive_our_own_ttl(self):
        delivery = self.delivery(expires=self.now + 10 * self.config.comms_max_ttl)
        accepted = self.store.accept_peer(NODE_B, delivery)
        self.assertEqual(accepted["outcome"]["status"], "accepted")
        self.assertEqual(accepted["envelope"]["expiresAt"],
                         self.now + self.config.comms_max_ttl)

    def test_inbound_deliveries_are_bounded_in_count(self):
        store = self._peer_store(comms_peer_inbound_records=1)
        store.reserve("b")
        store.accept_peer(NODE_B, self.delivery("d1", text="one"))
        with self.assertRaises(CommsError) as caught:
            store.accept_peer(NODE_B, self.delivery("d2", text="two"))
        self.assertEqual(caught.exception.code, "busy")
        # The replay of the accepted one is still answered, not refused.
        self.assertEqual(store.accept_peer(NODE_B, self.delivery("d1", text="one"))
                         ["outcome"]["status"], "accepted")

    def test_the_inbound_marker_cap_is_per_source_node(self):
        store = self._peer_store(comms_peer_inbound_records=1)
        store.reserve("b")
        store.accept_peer(NODE_B, self.delivery("d1", text="one"))
        other = self.delivery("d2", text="two")
        other["from"]["nodeId"] = NODE_C
        accepted = store.accept_peer(NODE_C, other)
        self.assertEqual(accepted["outcome"]["status"], "accepted")
        # One node's full table is its own: a second peer still gets in.
        with self.assertRaises(CommsError) as caught:
            store.accept_peer(NODE_B, self.delivery("d3", text="three"))
        self.assertEqual(caught.exception.code, "busy")

    def test_a_marker_outlives_the_retries_it_must_absorb_and_no_more(self):
        self.store.reserve("b")
        delivery = self.delivery("d1", expires=self.now + 60)
        self.store.accept_peer(NODE_B, delivery)
        skew = self.config.comms_peer_clock_skew
        flight = self.config.comms_peer_request_timeout
        self.assertGreater(min(skew, flight), 0.1)
        self.now += 60 + skew + flight - 0.1
        # Past the skew alone but inside a retry still in flight: a replay,
        # so dropping either margin fails here.
        self.assertEqual(self.store.accept_peer(NODE_B, delivery)
                         ["outcome"]["status"], "accepted")
        self.now += 1
        # Past both margins the marker is gone, so the same body is stale.
        self.assertEqual(self.store.accept_peer(NODE_B, delivery)
                         ["outcome"]["status"], "expired")

    def test_an_inbound_delivery_is_bounded_by_our_own_limits(self):
        self.store.reserve("b")
        long_kind = self.delivery("d1")
        long_kind["kind"] = "k" * (self.config.comms_kind_limit + 1)
        with self.assertRaises(CommsError) as caught:
            self.store.accept_peer(NODE_B, long_kind)
        self.assertEqual(caught.exception.code, "bad-request")
        long_agent = self.delivery("d2")
        long_agent["from"]["agentId"] = "a" * (
            self.config.comms_agent_id_limit + 1)
        with self.assertRaises(CommsError) as caught:
            self.store.accept_peer(NODE_B, long_agent)
        self.assertEqual(caught.exception.code, "bad-request")


class PeerCredentialTests(PeerCase):
    """The credential file a peer link reads is ours, and only ours."""

    def peers(self):
        return PeerConfigStore(self.layout.comms_peers_path(), self.config)

    def write(self, name, text="shared\n", mode=0o600):
        directory = os.path.join(self.base, "credentials")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(path, mode)
        return directory, path

    def test_a_credential_is_read_from_the_credentials_directory(self):
        directory, path = self.write("cred")
        auth = PeerAuthenticator(self.config, self.peers(),
                                 directory=directory)
        self.assertEqual(auth.outbound_credential(
            {"outboundCredentialFile": path}), "shared")

    def test_a_credential_outside_the_directory_is_refused(self):
        directory, _ = self.write("cred")
        elsewhere = os.path.join(self.base, "elsewhere")
        os.makedirs(elsewhere, exist_ok=True)
        path = os.path.join(elsewhere, "cred")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("shared")
        os.chmod(path, 0o600)
        auth = PeerAuthenticator(self.config, self.peers(),
                                 directory=directory)
        with self.assertRaises(ValueError) as caught:
            auth.outbound_credential({"outboundCredentialFile": path})
        self.assertEqual(str(caught.exception), "credential-unreadable")

    def test_a_symlinked_credential_is_refused(self):
        directory, path = self.write("cred")
        link = os.path.join(directory, "link")
        os.symlink(path, link)
        auth = PeerAuthenticator(self.config, self.peers(),
                                 directory=directory)
        with self.assertRaises(ValueError):
            auth.outbound_credential({"outboundCredentialFile": link})

    def test_a_credential_readable_by_others_is_refused(self):
        directory, path = self.write("cred", mode=0o644)
        auth = PeerAuthenticator(self.config, self.peers(),
                                 directory=directory)
        with self.assertRaises(ValueError):
            auth.outbound_credential({"outboundCredentialFile": path})


class PeerConfigStoreTests(PeerCase):

    def spec(self, **overrides):
        value = {"nodeId": NODE_B, "sshTarget": "peer.example",
                 "remotePort": 5000, "localPort": 0,
                 "outboundCredentialFile": "/run/cred",
                 "inboundCredentialSha256": "c" * 64}
        value.update(overrides)
        return value

    def test_configuration_round_trips(self):
        peers = PeerConfigStore(self.layout.comms_peers_path(), self.config)
        added = peers.add(self.spec())
        self.assertTrue(added["created"])
        reloaded = PeerConfigStore(self.layout.comms_peers_path(),
                                   self.config)
        reloaded.load()
        stored = reloaded.get(NODE_B)
        self.assertEqual(stored["sshTarget"], "peer.example")
        self.assertEqual(stored["state"], "active")
        self.assertEqual(reloaded.active_generation(NODE_B),
                         stored["generation"])

    def test_a_corrupt_peer_file_fails_closed(self):
        path = self.layout.comms_peers_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{")
        peers = PeerConfigStore(path, self.config)
        with self.assertRaises(CommsError) as caught:
            peers.load()
        self.assertEqual(caught.exception.code, "store-corrupt")


class TunnelTests(PeerCase):

    class ScriptedProcess:
        def __init__(self, out=b"", err=b"", returncode=None):
            self.stdout = io.BytesIO(out)
            self.stderr = io.BytesIO(err)
            self.returncode = returncode
            self.pid = 999999

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    def spawner(self, marker=True, error=b"", returncode=None):
        def spawn(argv, **_kwargs):
            out = b""
            for item in argv:
                if item.startswith("LocalCommand=echo "):
                    out = item.split("echo ", 1)[1].encode() + b"\n"
            return self.ScriptedProcess(out if marker else b"", error,
                                        returncode)
        return spawn

    def test_the_readiness_marker_opens_the_tunnel(self):
        tunnel = PeerTunnel(self.config, spawn=self.spawner())
        self.addCleanup(tunnel.close)
        host, port = tunnel.start({"nodeId": NODE_B, "sshTarget": "t",
                                   "remotePort": 5000, "localPort": 0})
        self.assertEqual(host, "127.0.0.1")
        self.assertTrue(port > 0)
        self.assertEqual(tunnel.address(), (host, port))

    def test_a_tunnel_without_its_marker_never_opens(self):
        tunnel = PeerTunnel(self.config, spawn=self.spawner(marker=False,
                                                            returncode=0))
        with self.assertRaises(TunnelError):
            tunnel.start({"nodeId": NODE_B, "sshTarget": "t",
                          "remotePort": 5000, "localPort": 0})

    def test_a_port_collision_reports_itself(self):
        tunnel = PeerTunnel(self.config, spawn=self.spawner(
            marker=False, error=b"bind: Address already in use\n",
            returncode=255))
        with self.assertRaises(TunnelError) as caught:
            tunnel.start({"nodeId": NODE_B, "sshTarget": "t",
                          "remotePort": 5000, "localPort": 6000})
        self.assertTrue(caught.exception.reason.startswith(
            "local-port-in-use"))


class GreetingServer:
    """One loopback peer that answers a hello with a chosen greeting."""

    def __init__(self, greeting, answer=None):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._greeting = greeting
        self._answer = answer
        self._codec = RpcCodec()
        self._stopping = False
        self.address = self._listener.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self):
        self._stopping = True
        try:
            self._listener.close()
        except OSError:
            pass

    def _serve(self):
        while not self._stopping:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(connection,),
                             daemon=True).start()

    def _one(self, connection):
        codec, stream = self._codec, JsonLineStream(connection, 65536, 8192)
        try:
            hello = codec.decode(stream.read())
            stream.write(codec.encode(codec.result(hello["id"],
                                                   self._greeting)))
            if self._answer is not None:
                request = codec.decode(stream.read())
                stream.write(codec.encode(codec.result(request["id"],
                                                       self._answer)))
        except (OSError, ValueError):
            pass
        finally:
            connection.close()


class PeerLinkTests(PeerCase):

    def greeting(self, **overrides):
        value = {"service": "agent-comms", "version": 1,
                 "protocol": "pi-rpc/1", "nodeId": NODE_B,
                 "peer": {"nodeId": NODE_A},
                 "methods": ["comms.peer.deliver", "comms.peer.ping"],
                 "topics": [], "maxFrameBytes": 65536,
                 "serverTime": self.now,
                 "limits": {"maxTtlSeconds": 86400.0,
                            "maxPayloadBytes": 32768}}
        value.update(overrides)
        return value

    def link(self):
        return PeerLink(self.config, NODE_A, clock=lambda: self.now)

    def peer(self):
        return {"nodeId": NODE_B}

    def test_a_link_requires_the_expected_node(self):
        server = GreetingServer(self.greeting(nodeId=NODE_C))
        self.addCleanup(server.close)
        with self.assertRaises(PeerLinkError) as caught:
            self.link().open(server.address, self.peer(), "credential")
        self.assertEqual(caught.exception.reason, "wrong-node")

    def test_a_link_requires_the_expected_protocol(self):
        server = GreetingServer(self.greeting(protocol="pi-rpc/2"))
        self.addCleanup(server.close)
        with self.assertRaises(PeerLinkError) as caught:
            self.link().open(server.address, self.peer(), "credential")
        self.assertEqual(caught.exception.reason, "wrong-protocol")

    def test_a_link_speaks_one_request_at_a_time(self):
        server = GreetingServer(self.greeting(),
                                answer={"nodeId": NODE_B, "serverTime": 7.0})
        self.addCleanup(server.close)
        link = self.link()
        self.addCleanup(link.close)
        link.open(server.address, self.peer(), "credential")
        self.assertTrue(link.alive())
        self.assertEqual(link.request("comms.peer.ping", {}),
                         {"nodeId": NODE_B, "serverTime": 7.0})
        link.close()
        self.assertFalse(link.alive())
        with self.assertRaises(PeerLinkError):
            link.request("comms.peer.ping", {})


class PeerWorkerTests(PeerCase):

    def worker(self, link, tunnel=None):
        record = {"nodeId": NODE_B, "generation": "g1",
                  "sshTarget": "peer.example", "remotePort": 5000,
                  "localPort": 0}
        worker = PeerDelivery(self.config, self.store, record, link,
                              tunnel or FakeTunnel(),
                              StubCredentials(), clock=lambda: self.now)
        self.addCleanup(worker.stop)
        return worker

    def test_an_accepted_answer_settles_the_receipt(self):
        first = self.send()
        link = FakeLink(answers=[{"status": "accepted",
                                  "deliveryId": first["deliveryId"],
                                  "messageId": "m1", "sequence": 4}])
        self.worker(link).start()
        self.assertTrue(_await(lambda: self.store.receipt(
            self._agent("a"), "r1")["status"] == "accepted"))
        receipt = self.store.receipt(self._agent("a"), "r1")
        self.assertEqual(receipt["messageId"], "m1")
        self.assertEqual(receipt["sequence"], 4)
        self.assertEqual(self.store.queued(NODE_B), 0)

    def test_an_answer_for_another_delivery_is_not_accepted(self):
        first = self.send()
        link = FakeLink(answers=[{"status": "accepted",
                                  "deliveryId": "someone-else",
                                  "messageId": "m1", "sequence": 4}])
        self.worker(link).start()
        self.assertTrue(_await(lambda: link.requests))
        self.assertEqual(self.store.receipt(self._agent("a"), "r1")
                         ["status"], "queued")

    def test_a_refusal_settles_with_the_remote_reason(self):
        first = self.send()
        link = FakeLink(answers=[{"status": "undeliverable",
                                  "deliveryId": first["deliveryId"],
                                  "reason": "mailbox-full"}])
        self.worker(link).start()
        self.assertTrue(_await(lambda: self.store.receipt(
            self._agent("a"), "r1")["status"] == "undeliverable"))
        self.assertEqual(self.store.receipt(self._agent("a"), "r1")
                         ["reason"], "mailbox-full")

    def test_a_broken_link_keeps_the_delivery_queued(self):
        self.send()
        link = FakeLink(failure=PeerLinkError("link-down"))
        self.worker(link).start()
        self.assertTrue(_await(lambda: any(method == "comms.peer.deliver"
                                           for method, _ in link.requests)))
        self.assertEqual(self.store.receipt(self._agent("a"), "r1")
                         ["status"], "queued")

    def test_stopping_closes_the_link_and_the_tunnel(self):
        link, tunnel = FakeLink(), FakeTunnel()
        worker = self.worker(link, tunnel)
        worker.start()
        self.assertTrue(_await(lambda: link.opened > 0))
        worker.stop()
        self.assertGreaterEqual(link.interrupted, 1)
        self.assertGreaterEqual(link.closed, 1)
        self.assertGreaterEqual(tunnel.closed, 1)
        self.assertEqual(worker.snapshot()["state"], "down")
        # A stopped worker never reconnects: no further open is recorded.
        opened = link.opened
        self.assertFalse(_await(lambda: link.opened > opened, timeout=0.3))


    def test_a_stale_generation_sends_the_worker_back_to_its_hello(self):
        self.send()
        link = FakeLink(failure=RpcError(UNAUTHORIZED, "peer-stale"))
        worker = self.worker(link)
        worker.start()
        self.assertTrue(_await(lambda: link.opened >= 2))
        self.assertEqual(self.store.receipt(self._agent("a"), "r1")
                         ["status"], "queued")

    def test_an_oversized_delivery_is_settled_rather_than_retried(self):
        self.send()
        link = FakeLink(failure=PeerLinkError("frame-too-large"))
        self.worker(link).start()
        self.assertTrue(_await(lambda: self.store.receipt(
            self._agent("a"), "r1")["status"] == "undeliverable"))
        self.assertEqual(self.store.receipt(self._agent("a"), "r1")
                         ["reason"], "payload-too-large")

    def test_a_remote_payload_refusal_settles_the_delivery(self):
        self.send()
        link = FakeLink(failure=RpcError(INVALID_PARAMS, "payload-too-large"))
        self.worker(link).start()
        self.assertTrue(_await(lambda: self.store.receipt(
            self._agent("a"), "r1")["status"] == "undeliverable"))
        self.assertEqual(self.store.receipt(self._agent("a"), "r1")
                         ["reason"], "payload-too-large")

    def test_a_failed_link_reports_a_bounded_reason(self):
        self.send()
        link = FakeLink(failure=PeerLinkError("x" * 10000))
        worker = self.worker(link)
        worker.start()
        self.assertTrue(_await(lambda: worker.snapshot()["lastError"]))
        self.assertLessEqual(len(worker.snapshot()["lastError"]), 256)


class PeerOperationsTests(PeerCase):

    def setUp(self):
        super().setUp()
        self.peers = PeerConfigStore(self.layout.comms_peers_path(),
                                     self.config)
        self.auth = PeerAuthenticator(self.config, self.peers,
                                      reader=lambda _path: "shared")
        self.accepted = []
        self.manager = PeerManager(self.config, self.store, self.peers,
                                   self.auth, self._accept, NODE_A,
                                   clock=lambda: self.now,
                                   tunnel_factory=lambda: FakeTunnel(),
                                   link_factory=lambda: FakeLink())
        self.addCleanup(self.manager.stop)
        self.manager.start()
        self.operations = PeerOperations(self.manager, self.config, NODE_A,
                                         clock=lambda: self.now)

    def _accept(self, node_id, generation, delivery):
        self.accepted.append((node_id, generation, delivery))
        return {"status": "accepted", "deliveryId": delivery["deliveryId"],
                "messageId": "m", "sequence": 1}

    def spec(self, **overrides):
        value = {"nodeId": NODE_B, "sshTarget": "peer.example",
                 "remotePort": 5000, "localPort": 0,
                 "outboundCredentialFile": "/run/cred",
                 "inboundCredentialSha256": hashlib.sha256(
                     b"shared").hexdigest()}
        value.update(overrides)
        return value

    def test_a_peer_can_be_configured_while_it_is_down(self):
        answer = self.operations.add(None, self.spec())
        self.assertEqual(answer["status"], "configured")
        self.assertTrue(answer["created"])
        listed = self.operations.list(None, {})
        self.assertEqual([peer["nodeId"] for peer in listed["peers"]],
                         [NODE_B])
        self.assertIn(listed["peers"][0]["state"], ("down", "up"))

    def test_a_configuration_is_idempotent_and_a_change_conflicts(self):
        self.operations.add(None, self.spec())
        again = self.operations.add(None, self.spec())
        self.assertFalse(again["created"])
        with self.assertRaises(RpcError) as caught:
            self.operations.add(None, self.spec(remotePort=5001))
        self.assertEqual(caught.exception.code, -32602)

    def test_a_peer_cannot_peer_with_itself(self):
        with self.assertRaises(RpcError) as caught:
            self.operations.add(None, self.spec(nodeId=NODE_A))
        self.assertEqual(caught.exception.code, -32602)

    def test_a_credential_is_verified_and_never_listed(self):
        self.operations.add(None, self.spec())
        self.assertTrue(self.manager.verify(NODE_B, "shared"))
        self.assertFalse(self.manager.verify(NODE_B, "other"))
        self.assertFalse(self.manager.verify(NODE_C, "shared"))
        listed = self.operations.list(None, {})["peers"][0]
        self.assertNotIn("inboundCredentialSha256", listed)
        self.assertNotIn("outboundCredentialFile", listed)

    def test_an_inbound_delivery_is_admitted_only_for_the_live_generation(self):
        self.operations.add(None, self.spec())
        generation = self.manager.generation(NODE_B)
        delivery = self.delivery()
        self.manager.deliver(NODE_B, generation, delivery)
        self.assertEqual(self.accepted[0][0], NODE_B)
        with self.assertRaises(CommsError) as caught:
            self.manager.deliver(NODE_B, "stale", delivery)
        self.assertEqual(caught.exception.code, "peer-stale")

    def offline(self):
        """A manager whose tunnel never comes up, so mail stays queued."""
        peers = PeerConfigStore(os.path.join(self.base, "offline-peers.json"),
                                self.config)
        manager = PeerManager(self.config, self.store, peers,
                              PeerAuthenticator(self.config, peers,
                                                reader=lambda _path: "shared"),
                              self._accept, NODE_A, clock=lambda: self.now,
                              tunnel_factory=lambda: FakeTunnel(
                                  failure=TunnelError("spawn-failed")),
                              link_factory=lambda: FakeLink())
        self.addCleanup(manager.stop)
        manager.start()
        return manager, PeerOperations(manager, self.config, NODE_A,
                                       clock=lambda: self.now)

    def test_removal_reports_what_it_settled_and_a_readd_is_a_new_generation(self):
        manager, operations = self.offline()
        operations.add(None, self.spec())
        first = manager.generation(NODE_B)
        self.store.accept(self._agent("a"), "r1", self.remote(), "text",
                          {"text": "hi"}, None,
                          peer={"nodeId": NODE_B, "generation": first})
        removed = operations.remove(None, {"nodeId": NODE_B})
        self.assertEqual(removed["status"], "removed")
        self.assertEqual(removed["undeliverableCount"], 1)
        self.assertEqual(self.store.receipt(self._agent("a"), "r1")
                         ["reason"], "peer-removed")
        operations.add(None, self.spec())
        self.assertNotEqual(manager.generation(NODE_B), first)
        self.assertEqual(operations.remove(None, {"nodeId": NODE_C}),
                         {"status": "not-found", "nodeId": NODE_C})

    def test_an_interrupted_removal_is_completed_on_start(self):
        self.peers.add(self.spec())
        self.peers.begin_remove(NODE_B)
        manager = PeerManager(self.config, self.store, self.peers, self.auth,
                              self._accept, NODE_A, clock=lambda: self.now,
                              tunnel_factory=lambda: FakeTunnel(),
                              link_factory=lambda: FakeLink())
        self.addCleanup(manager.stop)
        manager.start()
        self.assertIsNone(self.peers.get(NODE_B))

    def test_removal_stops_the_worker_even_when_settling_fails(self):
        tunnels = []

        def factory():
            tunnels.append(FakeTunnel())
            return tunnels[-1]

        peers = PeerConfigStore(os.path.join(self.base, "failing-peers.json"),
                                self.config)
        manager = PeerManager(self.config, self.store, peers,
                              PeerAuthenticator(self.config, peers,
                                                reader=lambda _path: "shared"),
                              self._accept, NODE_A, clock=lambda: self.now,
                              tunnel_factory=factory,
                              link_factory=lambda: FakeLink())
        self.addCleanup(manager.stop)
        manager.start()
        operations = PeerOperations(manager, self.config, NODE_A,
                                    clock=lambda: self.now)
        operations.add(None, self.spec())
        original = self.store.cancel_peer

        def failing(*_args, **_kwargs):
            raise CommsError("store-failed")

        self.store.cancel_peer = failing
        self.addCleanup(setattr, self.store, "cancel_peer", original)
        with self.assertRaises(RpcError) as caught:
            operations.remove(None, {"nodeId": NODE_B})
        self.assertEqual(str(caught.exception), "store-failed")
        self.assertGreaterEqual(tunnels[0].closed, 1)
        # Nothing was settled, so the removal stays visible as removing.
        self.assertEqual(operations.list(None, {})["peers"][0]["state"],
                         "removing")

    def test_a_credential_outside_the_directory_is_refused_at_the_wire(self):
        directory = os.path.join(self.base, "credentials")
        operations = PeerOperations(self.manager, self.config, NODE_A,
                                    clock=lambda: self.now,
                                    directory=directory)
        with self.assertRaises(RpcError) as caught:
            operations.add(None, self.spec(outboundCredentialFile="/run/cred"))
        self.assertEqual(caught.exception.code, -32602)
        accepted = operations.add(None, self.spec(
            outboundCredentialFile=os.path.join(directory, "cred")))
        self.assertTrue(accepted["created"])

    def test_ping_tells_a_stale_generation_to_greet_again(self):
        self.operations.add(None, self.spec())
        first = self.manager.generation(NODE_B)
        self.assertEqual(self.operations.ping(
            {"nodeId": NODE_B, "generation": first}, {}), {
                "nodeId": NODE_A, "serverTime": self.now})
        self.operations.remove(None, {"nodeId": NODE_B})
        self.operations.add(None, self.spec())
        self.assertNotEqual(self.manager.generation(NODE_B), first)
        with self.assertRaises(RpcError) as caught:
            self.operations.ping({"nodeId": NODE_B, "generation": first}, {})
        self.assertEqual(caught.exception.code, -32001)


class TunnelArgvTests(PeerCase):

    def capture(self, marker=True, returncode=None):
        calls = []

        def spawn(argv, **options):
            calls.append((argv, options))
            out = b""
            if marker:
                for item in argv:
                    if item.startswith("LocalCommand=echo "):
                        out = item.split("echo ", 1)[1].encode() + b"\n"
            return TunnelTests.ScriptedProcess(out, b"", returncode)

        return calls, spawn

    def peer(self, local_port=0):
        return {"nodeId": NODE_B, "sshTarget": "peer.example",
                "remotePort": 5000, "localPort": local_port}

    def test_the_forward_runs_in_the_foreground_and_never_forks(self):
        calls, spawn = self.capture()
        tunnel = PeerTunnel(self.config, spawn=spawn)
        self.addCleanup(tunnel.close)
        tunnel.start(self.peer(local_port=6000))
        argv, options = calls[0]
        self.assertNotIn("-f", argv)
        self.assertFalse(any("ForkAfterAuthentication" in item for item in argv))
        self.assertEqual(options.get("shell"), False)
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ExitOnForwardFailure=yes", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("ForwardAgent=no", argv)
        self.assertIn("127.0.0.1:6000:127.0.0.1:5000", argv)
        self.assertTrue(any(item.startswith("LocalCommand=echo pi-peer-ready-")
                            for item in argv))
        self.assertEqual(argv[-1], "peer.example")

    def test_a_child_that_dies_before_its_marker_ends_the_wait(self):
        _calls, spawn = self.capture(marker=False, returncode=255)
        tunnel = PeerTunnel(self.config, spawn=spawn)
        with self.assertRaises(TunnelError) as caught:
            tunnel.start(self.peer())
        self.assertTrue(caught.exception.reason.startswith("tunnel-exited"))

    def test_close_ends_a_start_that_is_still_waiting(self):
        self._peer_store(comms_peer_ready_timeout=2.0)
        _calls, spawn = self.capture(marker=False, returncode=None)
        tunnel = PeerTunnel(self.config, spawn=spawn)
        caught = []

        def run():
            try:
                tunnel.start(self.peer())
            except TunnelError as failure:
                caught.append(failure)

        thread = threading.Thread(target=run)
        started = time.time()
        thread.start()
        self.assertTrue(_await(lambda: tunnel.address() != ("127.0.0.1", 0),
                              timeout=0.5))
        tunnel.close()
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.time() - started, 1.5)
        self.assertEqual(len(caught), 1)
        self.assertTrue(caught[0].reason.startswith("tunnel-stopped"))
