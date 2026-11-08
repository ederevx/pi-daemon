"""The sole owner of mailboxes, envelopes, sequences and send receipts.

There is no roster: a mailbox exists because an identity was presented, and
the durable store is the only record that it exists.
"""

import hashlib
import json
import math
import threading
import time
import uuid

from ..atomic_state_file import AtomicStateFile
from .error import CommsError

BUSY = "busy"
REQUEST_CONFLICT = "request-conflict"
STORE_CORRUPT = "store-corrupt"
STORE_FAILED = "store-failed"
BAD_REQUEST = "bad-request"
PAYLOAD_TOO_LARGE = "payload-too-large"
UNKNOWN_RECIPIENT = "unknown-recipient"
REMOTE_NODE = "remote-node"
RESULT_KIND = "result"
ALL_TARGET = "all"
QUEUED = "queued"
ACCEPTED = "accepted"
UNDELIVERABLE = "undeliverable"
UNKNOWN = "unknown"
NO_SUCH_PEER = "no-such-peer"
PEER_REMOVED = "peer-removed"
RETENTION_EXPIRED = "retention-expired"
UNCONFIRMED = "delivery-unconfirmed"
MAILBOX_FULL = "mailbox-full"
EXPIRED = "expired"
RECEIPT_NOT_FOUND = "receipt-not-found"
SCHEMA = 2
LEGACY_SCHEMA = 1
# The room a reply wrapper, an id and the result keys need.
OVERHEAD_BYTES = 1024


class MailboxStore:
    """Accepted mail is durable before anyone is told about it."""

    def __init__(self, path, config, node_id, clock=None):
        self._file = AtomicStateFile(path, config.file_mode,
                                     config.write_retries,
                                     config.write_retry_delay)
        self._message_ttl = config.comms_message_ttl
        self._result_ttl = config.comms_result_ttl
        self._max_ttl = config.comms_max_ttl
        self._dedupe_ttl = config.comms_dedupe_ttl
        self._payload_bytes = config.comms_payload_bytes
        self._store_bytes = config.comms_store_bytes
        self._recipient_cap = config.comms_recipient_cap
        self._per_recipient = config.comms_messages_per_recipient
        self._page = config.comms_page_messages
        self._reply_bytes = config.frame_bytes - config.comms_frame_margin
        self._peer_retention = config.comms_peer_retention
        self._outbox_records = config.comms_peer_outbox_records
        self._inbound_records = config.comms_peer_inbound_records
        self._skew = config.comms_peer_clock_skew
        self._agent_limit = config.comms_agent_id_limit
        self._kind_limit = config.comms_kind_limit
        self._node_id = node_id
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._recipients = {}
        self._receipts = {}
        self._outbox = {}
        self._peer_deliveries = {}

    def node_id(self):
        return self._node_id

    def now(self):
        return self._clock()

    def queued(self, peer_node_id, generation=None):
        with self._lock:
            return sum(1 for record in self._outbox.values()
                       if record["state"] == QUEUED and
                       record["peerNodeId"] == peer_node_id and
                       (generation is None or
                        record["peerGeneration"] == generation))

    # -- lifecycle ---------------------------------------------------------

    def load(self):
        """Read one snapshot, refuse a broken one, expire what is stale."""
        with self._lock:
            text = self._file.read()
            if text is None:
                self._recipients = {}
                self._receipts = {}
                self._outbox = {}
                self._peer_deliveries = {}
                return
            try:
                data = json.loads(text)
            except ValueError:
                raise CommsError(STORE_CORRUPT) from None
            restored = self._restore(data)
            (self._recipients, self._receipts, self._outbox,
             self._peer_deliveries) = restored
            self._expire_locked(self._clock())

    def save(self):
        with self._lock:
            self._file.write_dict(self._snapshot_locked())

    def _restore(self, data):
        if not isinstance(data, dict) or \
                data.get("version") not in (SCHEMA, LEGACY_SCHEMA):
            raise CommsError(STORE_CORRUPT)
        raw_recipients = data.get("recipients")
        raw_receipts = data.get("send_receipts")
        raw_outbox = data.get("outbox") or {}
        raw_inbound = data.get("peer_deliveries") or {}
        if not isinstance(raw_recipients, dict) or \
                not isinstance(raw_receipts, dict) or \
                not isinstance(raw_outbox, dict) or \
                not isinstance(raw_inbound, dict):
            raise CommsError(STORE_CORRUPT)
        return ({agent_id: self._restore_recipient(state)
                 for agent_id, state in raw_recipients.items()},
                {key: self._restore_receipt(value)
                 for key, value in raw_receipts.items()},
                {key: self._restore_outbox(value)
                 for key, value in raw_outbox.items()},
                {key: self._restore_inbound(value)
                 for key, value in raw_inbound.items()})

    def _restore_recipient(self, state):
        try:
            if not isinstance(state, dict):
                raise CommsError(STORE_CORRUPT)
            messages = [self._restore_envelope(item)
                        for item in self._list(state.get("messages"))]
            next_sequence = self._whole(state.get("next_sequence"))
            acknowledged = [self._text(item)
                            for item in self._list(state.get("acknowledged"))]
        except (KeyError, TypeError, ValueError):
            raise CommsError(STORE_CORRUPT) from None
        except CommsError as exc:
            if exc.code == STORE_CORRUPT:
                raise
            raise CommsError(STORE_CORRUPT) from None
        sequences = [message["sequence"] for message in messages]
        identifiers = [message["messageId"] for message in messages]
        if len(set(sequences)) != len(sequences) or \
                len(set(identifiers)) != len(identifiers):
            raise CommsError(STORE_CORRUPT)
        if sequences and next_sequence < max(sequences):
            raise CommsError(STORE_CORRUPT)
        return {"next_sequence": next_sequence,
                "messages": messages,
                "acknowledged": acknowledged,
                "provisional": self._owner(state.get("provisional"))}

    def _restore_envelope(self, item):
        accepted_at = self._finite(item["acceptedAt"])
        expires_at = self._finite(item["expiresAt"])
        return {
            "messageId": self._text(item["messageId"]),
            "from": self._address(item["from"]),
            "to": self._address(item["to"]),
            "sequence": self._whole(item["sequence"]),
            "kind": self._text(item["kind"]),
            "payload": self._detach(item["payload"]),
            "acceptedAt": accepted_at,
            "expiresAt": expires_at,
        }

    def _restore_receipt(self, value):
        try:
            if not isinstance(value, dict):
                raise CommsError(STORE_CORRUPT)
            at = self._finite(value["at"])
            return {
                "digest": self._text(value["digest"]),
                "accepted": [self._restore_accepted(item)
                             for item in self._list(value["accepted"])],
                "undeliverable": [self._restore_undeliverable(item)
                                  for item in self._list(
                                      value["undeliverable"])],
                "at": at,
                "retainUntil": self._finite(
                    value.get("retainUntil", at + self._dedupe_ttl)),
                "deliveryId": self._optional_text(value.get("deliveryId")),
                "initial": self._optional_map(value.get("initial")),
                "outcome": self._optional_map(value.get("outcome")),
            }
        except (KeyError, TypeError, ValueError):
            raise CommsError(STORE_CORRUPT) from None
        except CommsError as exc:
            if exc.code == STORE_CORRUPT:
                raise
            raise CommsError(STORE_CORRUPT) from None

    def _restore_outbox(self, value):
        try:
            if not isinstance(value, dict):
                raise CommsError(STORE_CORRUPT)
            state = self._text(value["state"])
            if state not in (QUEUED, ACCEPTED, UNDELIVERABLE, UNKNOWN):
                raise CommsError(STORE_CORRUPT)
            delivery = value.get("delivery")
            settled = value.get("settledAt")
            return {
                "receiptKey": self._text(value["receiptKey"]),
                "peerNodeId": self._text(value["peerNodeId"]),
                "peerGeneration": self._text(value["peerGeneration"]),
                "delivery": None if delivery is None
                else self._detach(delivery),
                "state": state,
                "attempts": self._whole(value.get("attempts", 0)),
                "nextAttemptAt": self._finite(value["nextAttemptAt"]),
                "expiresAt": self._finite(value["expiresAt"]),
                "outcome": self._optional_map(value.get("outcome")),
                "settledAt": None if settled is None else self._finite(settled),
                "retainUntil": self._finite(value["retainUntil"]),
            }
        except (KeyError, TypeError, ValueError):
            raise CommsError(STORE_CORRUPT) from None
        except CommsError as exc:
            if exc.code == STORE_CORRUPT:
                raise
            raise CommsError(STORE_CORRUPT) from None

    def _restore_inbound(self, value):
        try:
            if not isinstance(value, dict):
                raise CommsError(STORE_CORRUPT)
            return {"digest": self._text(value["digest"]),
                    "expiresAt": self._finite(value["expiresAt"]),
                    "retainUntil": self._finite(value["retainUntil"]),
                    "outcome": self._detach(value["outcome"])}
        except (KeyError, TypeError, ValueError):
            raise CommsError(STORE_CORRUPT) from None
        except CommsError as exc:
            if exc.code == STORE_CORRUPT:
                raise
            raise CommsError(STORE_CORRUPT) from None

    def _restore_accepted(self, item):
        return {"to": self._address(item["to"]),
                "messageId": self._text(item["messageId"]),
                "sequence": self._whole(item["sequence"])}

    def _restore_undeliverable(self, item):
        return {"to": self._address(item["to"]),
                "reason": self._text(item["reason"])}

    def _snapshot_locked(self):
        return {
            "version": SCHEMA,
            "recipients": {
                agent_id: {
                    "next_sequence": state["next_sequence"],
                    "messages": [self._detach(message)
                                 for message in state["messages"]],
                    "acknowledged": list(state["acknowledged"]),
                    "provisional": state.get("provisional") or False,
                }
                for agent_id, state in self._recipients.items()
            },
            "send_receipts": {key: self._detach(value)
                              for key, value in self._receipts.items()},
            "outbox": {key: self._detach(value)
                       for key, value in self._outbox.items()},
            "peer_deliveries": {key: self._detach(value)
                                for key, value in self._peer_deliveries.items()},
        }

    # -- mailboxes ---------------------------------------------------------

    def reserve(self, agent_id, provisional=False):
        """A mailbox exists as soon as its identity is presented.

        A provisional mailbox belongs to one launch, named by the value:
        only that launch may release it, and the first presentation of the
        identity, or the first message addressed to it, adopts it.
        """
        with self._lock:
            self._expire_if_due_locked()
            state = self._recipients.get(agent_id)
            if state is None:
                if len(self._recipients) >= self._recipient_cap:
                    raise CommsError(BUSY)
                state = {"next_sequence": 0, "messages": [],
                         "acknowledged": [], "provisional": provisional}
                candidate = dict(self._recipients)
                candidate[agent_id] = state
                self._commit_locked(candidate, self._receipts)
            elif state.get("provisional") and not provisional:
                adopted = self._copy_state(state)
                adopted["provisional"] = False
                candidate = dict(self._recipients)
                candidate[agent_id] = adopted
                self._commit_locked(candidate, self._receipts)
                state = adopted
            return self._mailbox_snapshot(agent_id, state)

    def release(self, agent_id, owner=None):
        """Drop the provisional mailbox one launch reserved.

        A mailbox that was adopted, holds mail or was reserved by another
        launch is never removed: a failed launch cannot delete a working
        agent's mailbox.
        """
        with self._lock:
            state = self._recipients.get(agent_id)
            if state is None or not state.get("provisional"):
                return False
            if state["messages"]:
                return False
            if owner is not None and state["provisional"] != owner:
                return False
            candidate = {name: value for name, value in
                         self._recipients.items() if name != agent_id}
            self._commit_locked(candidate, self._receipts)
            return True

    def exists(self, agent_id):
        with self._lock:
            return agent_id in self._recipients

    def head_sequence(self, agent_id):
        with self._lock:
            state = self._recipients.get(agent_id) or {}
            return int(state.get("next_sequence") or 0)

    def _owner(self, value):
        """A provisional mailbox carries its owner, not a bare flag."""
        if value in (None, False):
            return False
        if value is True:
            return True
        return self._text(value)

    def _mailbox_snapshot(self, agent_id, state):
        return {"agentId": agent_id,
                "nodeId": self._node_id,
                "headSequence": state["next_sequence"],
                "pending": len(state["messages"])}

    # -- acceptance --------------------------------------------------------

    def accept(self, sender, request_id, target, kind, payload,
               ttl_seconds=None, peer=None, route=None):
        """One durable commit: selection, sequences, envelopes and receipt.

        `target` is one address or ALL_TARGET for every existing mailbox.
        The fingerprint describes the request; the receipt records what it
        resolved to, so a retry replays the first expansion. A foreign
        address is queued for its peer in the same commit, and the peer is
        resolved under the same lock, so a removal cannot slip between the
        routing decision and the commit.
        """
        sender_address = self._address(sender)
        encoded = self._payload_text(payload)
        wanted_ttl = self._ttl(kind, ttl_seconds)
        descriptor = self._target_descriptor(target)
        digest = self._digest(kind, encoded, descriptor, wanted_ttl)
        key = json.dumps([sender_address, str(request_id)], sort_keys=True)
        with self._lock:
            self._expire_if_due_locked()
            now = self._clock()
            stored = self._receipts.get(key)
            if stored is not None:
                if stored["digest"] != digest:
                    raise CommsError(REQUEST_CONFLICT)
                return self._duplicate_result(stored)
            if descriptor["kind"] == "direct" and \
                    descriptor["to"]["nodeId"] and \
                    descriptor["to"]["nodeId"] != self._node_id:
                chosen = peer
                if chosen is None and route is not None:
                    chosen = route(descriptor["to"]["nodeId"])
                return self._peer_accept_locked(
                    key, digest, sender_address, descriptor["to"], kind,
                    encoded, wanted_ttl, str(request_id), now, chosen)
            addresses = self._resolve_locked(descriptor, sender_address)
            candidate = {agent_id: self._copy_state(state)
                         for agent_id, state in self._recipients.items()}
            accepted, undeliverable, envelopes = [], [], []
            for address in addresses:
                outcome = self._deliver_locked(
                    candidate, address, sender_address, kind, encoded,
                    wanted_ttl, now)
                if outcome["reason"] is not None:
                    undeliverable.append({"to": address,
                                          "reason": outcome["reason"]})
                    continue
                envelopes.append(outcome["envelope"])
                accepted.append({"to": address,
                                 "messageId": outcome["envelope"]["messageId"],
                                 "sequence": outcome["envelope"]["sequence"]})
            receipt = {"digest": digest,
                       "accepted": accepted,
                       "undeliverable": undeliverable,
                       "at": now,
                       "retainUntil": now + self._dedupe_ttl,
                       "deliveryId": None,
                       "initial": None,
                       "outcome": None}
            receipts = dict(self._receipts)
            receipts[key] = receipt
            self._commit_locked(candidate, receipts)
            result = {"status": "accepted" if accepted else "undeliverable",
                      "receipt": self._detach(receipt)}
            if accepted:
                result["messages"] = self._detach(accepted)
                result["envelopes"] = envelopes
            if undeliverable:
                result["undeliverable"] = self._detach(undeliverable)
            return result

    def _deliver_locked(self, candidate, address, sender_address, kind,
                        encoded, ttl, now):
        """One recipient's envelope, or the reason it cannot exist."""
        if address["nodeId"] and address["nodeId"] != self._node_id:
            return {"reason": REMOTE_NODE, "envelope": None}
        state = candidate.get(address["agentId"])
        if state is None:
            return {"reason": UNKNOWN_RECIPIENT, "envelope": None}
        if len(state["messages"]) >= self._per_recipient:
            raise CommsError(BUSY)
        state["next_sequence"] += 1
        envelope = {
            "messageId": uuid.uuid4().hex,
            "from": sender_address,
            "to": address,
            "sequence": state["next_sequence"],
            "kind": kind,
            "payload": self._detach(json.loads(encoded)),
            "acceptedAt": now,
            "expiresAt": now + ttl,
        }
        if len(self._encode(envelope)) > self._reply_bytes:
            raise CommsError(PAYLOAD_TOO_LARGE)
        state["messages"].append(envelope)
        # Mail makes the identity real: a failed launch never deletes it.
        state["provisional"] = False
        return {"reason": None, "envelope": self._detach(envelope)}

    def _duplicate_result(self, stored):
        if stored.get("initial") is not None:
            result = {"status": "duplicate",
                      "receipt": self._detach(stored),
                      "initial": self._detach(stored["initial"])}
            if stored.get("outcome") is not None:
                result["outcome"] = self._detach(stored["outcome"])
            return result
        result = {"status": "duplicate", "receipt": self._detach(stored)}
        if stored["accepted"]:
            result["messages"] = self._detach(stored["accepted"])
        if stored["undeliverable"]:
            result["undeliverable"] = self._detach(stored["undeliverable"])
        return result

    # -- peers -------------------------------------------------------------

    def _peer_accept_locked(self, key, digest, sender_address, address, kind,
                            encoded, ttl, request_id, now, peer):
        """A foreign address: one receipt and one queued delivery, together."""
        to = self._address(address)
        receipt = {"digest": digest, "accepted": [], "undeliverable": [],
                   "at": now, "retainUntil": now + self._dedupe_ttl,
                   "deliveryId": None, "initial": None, "outcome": None}
        if peer is None or not peer.get("generation"):
            receipt["undeliverable"] = [{"to": to, "reason": NO_SUCH_PEER}]
            receipt["initial"] = {"status": UNDELIVERABLE,
                                  "requestId": request_id,
                                  "reason": NO_SUCH_PEER}
            receipts = dict(self._receipts)
            receipts[key] = receipt
            self._commit_locked(self._recipients, receipts)
            return {"status": UNDELIVERABLE,
                    "receipt": self._detach(receipt),
                    "undeliverable": self._detach(receipt["undeliverable"])}
        if sum(1 for record in self._outbox.values()
               if record["state"] == QUEUED) >= self._outbox_records:
            # Settled records are only dedupe markers; they hold no room.
            raise CommsError(BUSY)
        delivery_id = uuid.uuid4().hex
        expires_at = now + min(ttl, self._peer_retention)
        retain_until = expires_at + self._dedupe_ttl
        delivery = {"deliveryId": delivery_id, "from": sender_address,
                    "to": to, "kind": kind,
                    "payload": json.loads(encoded),
                    "createdAt": now, "expiresAt": expires_at}
        record = {"receiptKey": key, "peerNodeId": peer["nodeId"],
                  "peerGeneration": peer["generation"],
                  "delivery": delivery, "state": QUEUED, "attempts": 0,
                  "nextAttemptAt": now, "expiresAt": expires_at,
                  "outcome": None, "settledAt": None,
                  "retainUntil": retain_until}
        receipt["deliveryId"] = delivery_id
        receipt["retainUntil"] = retain_until
        receipt["initial"] = {"status": QUEUED, "requestId": request_id,
                              "deliveryId": delivery_id,
                              "expiresAt": expires_at}
        receipts = dict(self._receipts)
        receipts[key] = receipt
        outbox = dict(self._outbox)
        outbox[delivery_id] = record
        self._commit_locked(self._recipients, receipts, outbox)
        return {"status": QUEUED, "deliveryId": delivery_id,
                "expiresAt": expires_at, "peerNodeId": peer["nodeId"],
                "receipt": self._detach(receipt)}

    def _inbound_count_locked(self, peer_node_id):
        """The markers one source node holds; the key names it first."""
        return sum(1 for key in self._peer_deliveries
                   if json.loads(key)[0] == peer_node_id)

    def accept_peer(self, peer_node_id, delivery):
        """The remote node's one envelope, or the refusal it must learn.

        Deduplication comes first: a replay returns the recorded outcome and
        publishes nothing, whatever the mailbox now holds.
        """
        if not isinstance(delivery, dict):
            raise CommsError(BAD_REQUEST)
        try:
            delivery_id = self._text(delivery.get("deliveryId"))
            source = self._address(delivery.get("from"))
            to = self._address(delivery.get("to"))
            kind = self._text(delivery.get("kind"))
            expires_at = self._finite(delivery.get("expiresAt"))
            self._finite(delivery.get("createdAt"))
        except CommsError:
            raise CommsError(BAD_REQUEST) from None
        if len(kind) > self._kind_limit or \
                len(source["agentId"]) > self._agent_limit or \
                len(to["agentId"]) > self._agent_limit:
            raise CommsError(BAD_REQUEST)
        if source["nodeId"] != peer_node_id or to["nodeId"] != self._node_id:
            raise CommsError(BAD_REQUEST)
        encoded = self._payload_text(delivery.get("payload"))
        digest = hashlib.sha256(json.dumps(
            {"delivery": self._detach(delivery)},
            sort_keys=True).encode("utf-8")).hexdigest()
        key = json.dumps([peer_node_id, delivery_id], sort_keys=True)
        with self._lock:
            self._expire_if_due_locked()
            now = self._clock()
            # The remote chose this expiry: never let it outlive our own.
            expires_at = min(expires_at, now + self._max_ttl)
            stored = self._peer_deliveries.get(key)
            if stored is not None:
                if stored["digest"] != digest:
                    raise CommsError(REQUEST_CONFLICT)
                return {"outcome": self._detach(stored["outcome"]),
                        "envelope": None}
            # The markers are per source node: one busy peer fills its own
            # share and is told capacity, never every peer's.
            if self._inbound_count_locked(peer_node_id) >= \
                    self._inbound_records:
                raise CommsError(BUSY)
            if now >= expires_at:
                return {"outcome": {"status": EXPIRED,
                                    "deliveryId": delivery_id},
                        "envelope": None}
            candidate = {agent_id: self._copy_state(state)
                         for agent_id, state in self._recipients.items()}
            state = candidate.get(to["agentId"])
            envelope = None
            if state is None:
                outcome = {"status": UNDELIVERABLE,
                           "deliveryId": delivery_id,
                           "reason": UNKNOWN_RECIPIENT}
            elif len(state["messages"]) >= self._per_recipient:
                outcome = {"status": UNDELIVERABLE,
                           "deliveryId": delivery_id,
                           "reason": MAILBOX_FULL}
            else:
                state["next_sequence"] += 1
                envelope = {"messageId": uuid.uuid4().hex, "from": source,
                            "to": to, "sequence": state["next_sequence"],
                            "kind": kind,
                            "payload": json.loads(encoded),
                            "acceptedAt": now, "expiresAt": expires_at}
                if len(self._encode(envelope)) > self._reply_bytes:
                    state["next_sequence"] -= 1
                    envelope = None
                    outcome = {"status": UNDELIVERABLE,
                               "deliveryId": delivery_id,
                               "reason": PAYLOAD_TOO_LARGE}
                else:
                    state["messages"].append(envelope)
                    state["provisional"] = False
                    outcome = {"status": ACCEPTED,
                               "deliveryId": delivery_id,
                               "messageId": envelope["messageId"],
                               "sequence": envelope["sequence"]}
            inbound = dict(self._peer_deliveries)
            # A marker only has to outlive the sender's retries, and those
            # stop at the expiry the sender chose: the clock skew past it is
            # all the margin a duplicate needs.
            inbound[key] = {"digest": digest, "expiresAt": expires_at,
                            "retainUntil": expires_at + self._skew,
                            "outcome": self._detach(outcome)}
            self._commit_locked(candidate, self._receipts, self._outbox,
                                inbound)
            return {"outcome": self._detach(outcome),
                    "envelope": None if envelope is None
                    else self._detach(envelope)}

    def receipt(self, sender, request_id):
        """What happened to one send, in the terms the sender asked in."""
        sender_address = self._address(sender)
        key = json.dumps([sender_address, str(request_id)], sort_keys=True)
        with self._lock:
            self._expire_if_due_locked()
            stored = self._receipts.get(key)
            if stored is None:
                return {"status": UNKNOWN, "reason": RECEIPT_NOT_FOUND}
            current = stored.get("outcome") or stored.get("initial")
            if current is not None:
                answer = {"status": current["status"],
                          "requestId": str(current.get("requestId") or
                                           request_id),
                          "deliveryId": current.get("deliveryId")}
                for field in ("expiresAt", "messageId", "sequence",
                              "reason"):
                    if current.get(field) is not None:
                        answer[field] = current[field]
                return answer
            if stored["accepted"]:
                first = stored["accepted"][0]
                return {"status": ACCEPTED, "requestId": str(request_id),
                        "to": first["to"],
                        "messageId": first["messageId"],
                        "sequence": first["sequence"]}
            if stored["undeliverable"]:
                return {"status": UNDELIVERABLE,
                        "requestId": str(request_id),
                        "reason": stored["undeliverable"][0]["reason"]}
            return {"status": UNKNOWN, "reason": RECEIPT_NOT_FOUND}

    def due(self, peer_node_id, generation, limit):
        """The queued deliveries one peer may attempt now, oldest first."""
        with self._lock:
            self._expire_if_due_locked()
            now = self._clock()
            ready = [record for record in self._outbox.values()
                     if record["state"] == QUEUED and
                     record["peerNodeId"] == peer_node_id and
                     record["peerGeneration"] == generation and
                     record["nextAttemptAt"] <= now and
                     record["expiresAt"] > now]
            ready.sort(key=lambda record: (record["nextAttemptAt"],
                                           record["expiresAt"]))
            return [self._detach(record) for record in ready[:int(limit)]]

    def mark_attempt(self, delivery_id, generation, next_attempt_at):
        """The attempt is durable before the link is allowed to see it."""
        with self._lock:
            record = self._outbox.get(delivery_id)
            if record is None or record["state"] != QUEUED or \
                    record["peerGeneration"] != generation:
                return False
            changed = self._detach(record)
            changed["attempts"] += 1
            changed["nextAttemptAt"] = float(next_attempt_at)
            outbox = dict(self._outbox)
            outbox[delivery_id] = changed
            self._commit_locked(self._recipients, self._receipts, outbox)
            return True

    def settle_peer(self, delivery_id, generation, outcome):
        """Record a terminal outcome; a repeated report changes nothing."""
        with self._lock:
            record = self._outbox.get(delivery_id)
            if record is None or record["peerGeneration"] != generation:
                return False
            if record["state"] != QUEUED:
                return True
            status = outcome.get("status") if isinstance(outcome, dict) \
                else None
            if status not in (ACCEPTED, UNDELIVERABLE, UNKNOWN):
                raise CommsError(BAD_REQUEST)
            changed = self._detach(record)
            changed["state"] = status
            changed["outcome"] = self._detach(outcome)
            changed["settledAt"] = self._clock()
            changed["delivery"] = None
            outbox = dict(self._outbox)
            outbox[delivery_id] = changed
            receipts = dict(self._receipts)
            receipt = receipts.get(record["receiptKey"])
            if receipt is not None:
                updated = self._detach(receipt)
                updated["outcome"] = self._detach(outcome)
                receipts[record["receiptKey"]] = updated
            self._commit_locked(self._recipients, receipts, outbox)
            return True

    def cancel_peer(self, peer_node_id, generation, reason):
        """Settle what one peer can no longer carry, truthfully."""

        def selected(record):
            return record["peerNodeId"] == peer_node_id and (
                generation is None or
                record["peerGeneration"] == generation)

        return self._cancel_locked(selected, reason)

    def cancel_orphans(self, active, reason):
        """Settle queued mail no configured peer generation can carry."""

        def selected(record):
            return (record["peerNodeId"], record["peerGeneration"]) \
                not in active

        return self._cancel_locked(selected, reason)

    def _cancel_locked(self, selected, reason):
        """Unattempted mail is undeliverable; attempted mail is unknown."""
        with self._lock:
            now = self._clock()
            outbox = dict(self._outbox)
            receipts = dict(self._receipts)
            undeliverable = unknown = 0
            for delivery_id, record in self._outbox.items():
                if record["state"] != QUEUED or not selected(record):
                    continue
                status = UNDELIVERABLE if record["attempts"] == 0 \
                    else UNKNOWN
                changed = self._detach(record)
                changed["state"] = status
                changed["settledAt"] = now
                changed["delivery"] = None
                changed["outcome"] = {"status": status,
                                      "deliveryId": delivery_id,
                                      "reason": reason}
                outbox[delivery_id] = changed
                receipt = receipts.get(record["receiptKey"])
                if receipt is not None:
                    updated = self._detach(receipt)
                    updated["outcome"] = self._detach(changed["outcome"])
                    receipts[record["receiptKey"]] = updated
                if status == UNDELIVERABLE:
                    undeliverable += 1
                else:
                    unknown += 1
            if undeliverable or unknown:
                self._commit_locked(self._recipients, receipts, outbox)
            return {"settled": undeliverable + unknown,
                    "undeliverable": undeliverable, "unknown": unknown}

    def _resolve_locked(self, descriptor, sender_address):
        if descriptor["kind"] == "all":
            return [{"nodeId": self._node_id, "agentId": agent_id}
                    for agent_id in sorted(self._recipients)
                    if agent_id != sender_address["agentId"]]
        return [self._address(descriptor["to"])]

    def _target_descriptor(self, target):
        if target == ALL_TARGET:
            return {"kind": "all"}
        return {"kind": "direct", "to": self._address(target)}

    # -- reading -----------------------------------------------------------

    def replay(self, agent_id, cursor=None, limit=None):
        """A page that fits one frame, oldest first, after `cursor`."""
        with self._lock:
            self._expire_if_due_locked()
            state = self._recipients.get(agent_id)
            if state is None:
                raise CommsError(UNKNOWN_RECIPIENT)
            oldest = self._oldest_locked(state)
            if cursor is not None and int(cursor) + 1 < oldest:
                return {"status": "cursor-expired",
                        "oldestAvailableSequence": oldest,
                        "cursor": int(cursor)}
            position = 0 if cursor is None else int(cursor)
            page = min(int(limit or self._page), self._page)
            waiting = [message for message in state["messages"]
                       if message["sequence"] > position]
            window = []
            for message in waiting[:page]:
                if window and len(self._encode_response(window + [message],
                                                        message["sequence"])) \
                        > self._reply_bytes:
                    break
                window.append(message)
            following = window[-1]["sequence"] if window else position
            return {"status": "ok",
                    "messages": [self._detach(message) for message in window],
                    "cursor": following,
                    "more": len(waiting) > len(window)}

    def _oldest_locked(self, state):
        sequences = [message["sequence"] for message in state["messages"]]
        if sequences:
            return min(sequences)
        return state["next_sequence"] + 1

    def acknowledge(self, agent_id, message_ids):
        wanted = [str(item) for item in message_ids]
        with self._lock:
            self._expire_if_due_locked()
            state = self._recipients.get(agent_id)
            if state is None:
                raise CommsError(UNKNOWN_RECIPIENT)
            present = {message["messageId"] for message in state["messages"]}
            seen = set(state["acknowledged"])
            fresh, repeated, unknown = [], [], []
            for item in wanted:
                if item in seen:
                    repeated.append(item)
                elif item in present:
                    seen.add(item)
                    fresh.append(item)
                else:
                    unknown.append(item)
            if fresh:
                candidate = {key: self._copy_state(value)
                             for key, value in self._recipients.items()}
                candidate[agent_id]["acknowledged"].extend(fresh)
                self._commit_locked(candidate, self._receipts)
            return {"acknowledged": fresh,
                    "alreadyAcknowledged": repeated,
                    "unknown": unknown}

    def expire(self, now=None):
        with self._lock:
            return self._expire_locked(self._clock() if now is None else now)

    def _expire_if_due_locked(self):
        now = self._clock()
        stale_message = any(message["expiresAt"] <= now
                            for state in self._recipients.values()
                            for message in state["messages"])
        stale_receipt = any(self._retention(value) <= now
                            for value in self._receipts.values())
        stale_outbox = any(record["state"] == QUEUED and
                           record["expiresAt"] <= now
                           for record in self._outbox.values())
        stale_inbound = any(value["retainUntil"] <= now
                            for value in self._peer_deliveries.values())
        if stale_message or stale_receipt or stale_outbox or stale_inbound:
            self._expire_locked(now)

    def _retention(self, value):
        """When a receipt's own metadata may be forgotten."""
        keep = value.get("retainUntil")
        if keep is None:
            keep = float(value.get("at") or 0) + self._dedupe_ttl
        return float(keep)

    def _expire_locked(self, now):
        """Drop expired mail and markers; settle what can no longer be sent."""
        candidate = {}
        dropped = 0
        pruned = 0
        for agent_id, state in self._recipients.items():
            copied = self._copy_state(state)
            fresh = [message for message in copied["messages"]
                     if message["expiresAt"] > now]
            dropped += len(copied["messages"]) - len(fresh)
            copied["messages"] = fresh
            remaining = {message["messageId"] for message in fresh}
            markers = [item for item in copied["acknowledged"]
                       if item in remaining]
            pruned += len(copied["acknowledged"]) - len(markers)
            copied["acknowledged"] = markers
            candidate[agent_id] = copied
        receipts = {key: self._detach(value)
                    for key, value in self._receipts.items()
                    if self._retention(value) > now}
        receipts_dropped = len(self._receipts) - len(receipts)
        outbox = {}
        settled = 0
        for delivery_id, record in self._outbox.items():
            changed = self._detach(record)
            if changed["state"] == QUEUED and changed["expiresAt"] <= now:
                status = UNDELIVERABLE if changed["attempts"] == 0 \
                    else UNKNOWN
                changed["state"] = status
                changed["settledAt"] = now
                changed["delivery"] = None
                changed["outcome"] = {
                    "status": status, "deliveryId": delivery_id,
                    "reason": RETENTION_EXPIRED if status == UNDELIVERABLE
                    else UNCONFIRMED}
                self._settle_receipt(receipts, record["receiptKey"],
                                     changed["outcome"])
                settled += 1
            if changed["retainUntil"] > now:
                outbox[delivery_id] = changed
            else:
                settled += 1
        inbound = {key: self._detach(value)
                   for key, value in self._peer_deliveries.items()
                   if value["retainUntil"] > now}
        inbound_dropped = len(self._peer_deliveries) - len(inbound)
        if dropped or pruned or receipts_dropped or settled or inbound_dropped:
            self._commit_locked(candidate, receipts, outbox, inbound)
        return (dropped + pruned + receipts_dropped + settled + inbound_dropped)

    @staticmethod
    def _settle_receipt(receipts, key, outcome):
        receipt = receipts.get(key)
        if receipt is None:
            return
        updated = json.loads(json.dumps(receipt))
        updated["outcome"] = json.loads(json.dumps(outcome))
        receipts[key] = updated

    # -- internals ---------------------------------------------------------

    def _commit_locked(self, recipients, receipts, outbox=None,
                       peer_deliveries=None):
        """Persist first, publish second: nobody sees unsaved mail."""
        previous = (self._recipients, self._receipts, self._outbox,
                    self._peer_deliveries)
        self._recipients = recipients
        self._receipts = receipts
        if outbox is not None:
            self._outbox = outbox
        if peer_deliveries is not None:
            self._peer_deliveries = peer_deliveries
        try:
            encoded = self._encode(self._snapshot_locked())
            if len(encoded) > self._store_bytes:
                raise CommsError(BUSY)
            self._file.write(encoded.decode("utf-8"))
        except BaseException:
            (self._recipients, self._receipts, self._outbox,
             self._peer_deliveries) = previous
            raise

    def _copy_state(self, state):
        return {"next_sequence": int(state["next_sequence"]),
                "messages": [self._detach(message)
                             for message in state["messages"]],
                "acknowledged": list(state["acknowledged"]),
                "provisional": self._owner(state.get("provisional"))}

    def _ttl(self, kind, ttl_seconds):
        base = self._result_ttl if kind == RESULT_KIND else self._message_ttl
        wanted = base if ttl_seconds is None else float(ttl_seconds)
        if wanted <= 0:
            raise CommsError(BAD_REQUEST)
        return min(wanted, self._max_ttl)

    def _payload_text(self, payload):
        try:
            encoded = json.dumps(payload, sort_keys=True)
        except (TypeError, ValueError):
            raise CommsError(BAD_REQUEST) from None
        if len(encoded.encode("utf-8")) > self._payload_bytes:
            raise CommsError(PAYLOAD_TOO_LARGE)
        return encoded

    def _address(self, value):
        if not isinstance(value, dict):
            raise CommsError(BAD_REQUEST)
        agent_id = str(value.get("agentId") or "")
        if not agent_id:
            raise CommsError(BAD_REQUEST)
        return {"nodeId": str(value.get("nodeId") or ""),
                "agentId": agent_id}

    def _encode(self, value):
        return json.dumps(value, sort_keys=True).encode("utf-8")

    def _encode_response(self, messages, cursor):
        return self._encode({"status": "ok",
                             "messages": messages,
                             "cursor": cursor,
                             "more": True})

    def _detach(self, value):
        """A copy that shares no container with store-owned state."""
        return json.loads(json.dumps(value))

    def _digest(self, kind, encoded_payload, descriptor, ttl):
        material = json.dumps({"kind": kind,
                               "payload": json.loads(encoded_payload),
                               "target": descriptor,
                               "ttl": ttl}, sort_keys=True)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _list(self, value):
        if not isinstance(value, list):
            raise CommsError(STORE_CORRUPT)
        return value

    def _text(self, value):
        if not isinstance(value, str) or not value:
            raise CommsError(STORE_CORRUPT)
        return value

    def _optional_text(self, value):
        if value is None:
            return None
        return self._text(value)

    def _optional_map(self, value):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise CommsError(STORE_CORRUPT)
        return self._detach(value)

    def _whole(self, value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CommsError(STORE_CORRUPT)
        return value

    def _finite(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CommsError(STORE_CORRUPT)
        number = float(value)
        if not math.isfinite(number):
            raise CommsError(STORE_CORRUPT)
        return number
