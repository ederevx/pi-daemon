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
BAD_REQUEST = "bad-request"
PAYLOAD_TOO_LARGE = "payload-too-large"
UNKNOWN_RECIPIENT = "unknown-recipient"
REMOTE_NODE = "remote-node"
RESULT_KIND = "result"
ALL_TARGET = "all"
SCHEMA = 1
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
        self._node_id = node_id
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._recipients = {}
        self._receipts = {}

    def node_id(self):
        return self._node_id

    # -- lifecycle ---------------------------------------------------------

    def load(self):
        """Read one snapshot, refuse a broken one, expire what is stale."""
        with self._lock:
            text = self._file.read()
            if text is None:
                self._recipients = {}
                self._receipts = {}
                return
            try:
                data = json.loads(text)
            except ValueError:
                raise CommsError(STORE_CORRUPT) from None
            recipients, receipts = self._restore(data)
            self._recipients, self._receipts = recipients, receipts
            self._expire_locked(self._clock())

    def save(self):
        with self._lock:
            self._file.write_dict(self._snapshot_locked())

    def _restore(self, data):
        if not isinstance(data, dict) or data.get("version") != SCHEMA:
            raise CommsError(STORE_CORRUPT)
        raw_recipients = data.get("recipients")
        raw_receipts = data.get("send_receipts")
        if not isinstance(raw_recipients, dict) or \
                not isinstance(raw_receipts, dict):
            raise CommsError(STORE_CORRUPT)
        return ({agent_id: self._restore_recipient(state)
                 for agent_id, state in raw_recipients.items()},
                {key: self._restore_receipt(value)
                 for key, value in raw_receipts.items()})

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
                "acknowledged": acknowledged}

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
            return {
                "digest": self._text(value["digest"]),
                "accepted": [self._restore_accepted(item)
                             for item in self._list(value["accepted"])],
                "undeliverable": [self._restore_undeliverable(item)
                                  for item in self._list(
                                      value["undeliverable"])],
                "at": self._finite(value["at"]),
            }
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
                }
                for agent_id, state in self._recipients.items()
            },
            "send_receipts": {key: self._detach(value)
                              for key, value in self._receipts.items()},
        }

    # -- mailboxes ---------------------------------------------------------

    def reserve(self, agent_id):
        """A mailbox exists as soon as its identity is presented."""
        with self._lock:
            self._expire_if_due_locked()
            state = self._recipients.get(agent_id)
            if state is None:
                if len(self._recipients) >= self._recipient_cap:
                    raise CommsError(BUSY)
                state = {"next_sequence": 0, "messages": [],
                         "acknowledged": []}
                candidate = dict(self._recipients)
                candidate[agent_id] = state
                self._commit_locked(candidate, self._receipts)
            return self._mailbox_snapshot(agent_id, state)

    def exists(self, agent_id):
        with self._lock:
            return agent_id in self._recipients

    def head_sequence(self, agent_id):
        with self._lock:
            state = self._recipients.get(agent_id) or {}
            return int(state.get("next_sequence") or 0)

    def _mailbox_snapshot(self, agent_id, state):
        return {"agentId": agent_id,
                "nodeId": self._node_id,
                "headSequence": state["next_sequence"],
                "pending": len(state["messages"])}

    # -- acceptance --------------------------------------------------------

    def accept(self, sender, request_id, target, kind, payload,
               ttl_seconds=None):
        """One durable commit: selection, sequences, envelopes and receipt.

        `target` is one address or ALL_TARGET for every existing mailbox.
        The fingerprint describes the request; the receipt records what it
        resolved to, so a retry replays the first expansion.
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
                       "at": now}
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
        return {"reason": None, "envelope": self._detach(envelope)}

    def _duplicate_result(self, stored):
        result = {"status": "duplicate", "receipt": self._detach(stored)}
        if stored["accepted"]:
            result["messages"] = self._detach(stored["accepted"])
        if stored["undeliverable"]:
            result["undeliverable"] = self._detach(stored["undeliverable"])
        return result

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
        stale_receipt = any(float(receipt.get("at") or 0) <=
                            now - self._dedupe_ttl
                            for receipt in self._receipts.values())
        if stale_message or stale_receipt:
            self._expire_locked(now)

    def _expire_locked(self, now):
        """Drop expired mail and the acknowledgement markers that named it."""
        candidate = {}
        dropped = 0
        pruned = 0
        horizon = now - self._dedupe_ttl
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
                    if float(value.get("at") or 0) > horizon}
        receipts_dropped = len(self._receipts) - len(receipts)
        if dropped or pruned or receipts_dropped:
            self._commit_locked(candidate, receipts)
        return dropped + pruned + receipts_dropped

    # -- internals ---------------------------------------------------------

    def _commit_locked(self, recipients, receipts):
        """Persist first, publish second: nobody sees unsaved mail."""
        previous = (self._recipients, self._receipts)
        self._recipients, self._receipts = recipients, receipts
        try:
            encoded = self._encode(self._snapshot_locked())
            if len(encoded) > self._store_bytes:
                raise CommsError(BUSY)
            self._file.write(encoded.decode("utf-8"))
        except BaseException:
            self._recipients, self._receipts = previous
            raise

    def _copy_state(self, state):
        return {"next_sequence": int(state["next_sequence"]),
                "messages": [self._detach(message)
                             for message in state["messages"]],
                "acknowledged": list(state["acknowledged"])}

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
