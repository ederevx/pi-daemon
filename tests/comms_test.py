#!/usr/bin/env python3
"""The agent comms domain: durable mailboxes, channels and receipts.
Zero dependencies (stdlib only)."""

import json
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "service"))

from daemon_service.comms.error import CommsError
from daemon_service.comms.mailbox_store import ALL_TARGET, MailboxStore
from daemon_service.comms.node_identity import NodeIdentity
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
