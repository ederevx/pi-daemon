#!/usr/bin/env python3
"""The agent comms domain: durable mailboxes, channels and receipts.
Zero dependencies (stdlib only)."""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "service"))

from daemon_service.comms.error import CommsError
from daemon_service.atomic_state_file import AtomicStateFile
from daemon_service.comms.mailbox_store import ALL_TARGET, MailboxStore
from daemon_service.comms.service import CommsService
from daemon_service.comms.node_identity import NodeIdentity
from daemon_service.comms.spawn_ledger import SpawnLedger
from daemon_service.comms.wait_registry import WaitRegistry
from daemon_service.config import ServiceConfig
from daemon_service.runtime_layout import RuntimeLayout


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

    def test_a_foreign_node_is_undeliverable(self):
        result = self.store.accept(self._agent("a"), "r1",
                                   {"nodeId": "peer", "agentId": "b"},
                                   "text", {"body": "hi"})
        self.assertEqual(result["status"], "undeliverable")
        self.assertEqual(result["undeliverable"][0]["reason"], "remote-node")
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
