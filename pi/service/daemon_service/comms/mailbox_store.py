"""The sole owner of mailboxes, envelopes, sequences and send receipts.

There is no roster: a mailbox exists because an identity was presented, and
the durable store is the only record that it exists.
"""

import hashlib
import json
import threading
import time
import uuid

from ..atomic_state_file import AtomicStateFile
from .error import CommsError

BUSY = "busy"
REQUEST_CONFLICT = "request-conflict"
STORE_CORRUPT = "store-corrupt"
RESULT_KIND = "result"
ALL_TARGET = "all"
SCHEMA = 1


class MailboxStore:
    """Accepted mail is durable before anyone is told about it."""

    def __init__(self, path, config, node_id="", clock=None):
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
        self._node_id = node_id
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._recipients = {}
        self._receipts = {}

    # -- lifecycle ---------------------------------------------------------

    def load(self):
        """Read one snapshot, expire what is stale and republish it."""
        with self._lock:
            try:
                data = self._file.read_dict()
            except ValueError:
                raise CommsError(STORE_CORRUPT) from None
            if data is None:
                self._recipients = {}
                self._receipts = {}
                return
            if not isinstance(data, dict) or data.get("version") != SCHEMA:
                raise CommsError(STORE_CORRUPT)
            self._recipients = {
                agent_id: self._restore_recipient(state)
                for agent_id, state in (data.get("recipients") or {}).items()
            }
            self._receipts = {
                key: self._detach(receipt)
                for key, receipt in (data.get("send_receipts") or {}).items()
            }
            self._expire_locked(self._clock())

    def save(self):
        with self._lock:
            self._file.write_dict(self._snapshot_locked())

    def _restore_recipient(self, state):
        if not isinstance(state, dict):
            raise CommsError(STORE_CORRUPT)
        messages = []
        for item in state.get("messages") or []:
            try:
                messages.append(self._restore_envelope(item))
            except (KeyError, TypeError, ValueError):
                raise CommsError(STORE_CORRUPT) from None
        return {
            "next_sequence": int(state.get("next_sequence") or 0),
            "messages": messages,
            "acknowledged": [str(item) for item in state.get("acknowledged")
                             or []],
        }

    def _restore_envelope(self, item):
        return {
            "messageId": str(item["messageId"]),
            "from": self._address(item["from"]),
            "to": self._address(item["to"]),
            "sequence": int(item["sequence"]),
            "kind": str(item["kind"]),
            "payload": self._detach(item["payload"]),
            "acceptedAt": float(item["acceptedAt"]),
            "expiresAt": float(item["expiresAt"]),
        }

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

    def identities(self):
        with self._lock:
            return sorted(self._recipients)

    def head_sequence(self, agent_id):
        with self._lock:
            state = self._recipients.get(agent_id) or {}
            return int(state.get("next_sequence") or 0)

    def _mailbox_snapshot(self, agent_id, state):
        return {"agentId": agent_id,
                "nextSequence": state["next_sequence"],
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
            now = self._clock()
            stored = self._receipts.get(key)
            if stored is not None and now - float(stored["at"]) > \
                    self._dedupe_ttl:
                stored = None
                self._receipts = {existing: value
                                  for existing, value in self._receipts.items()
                                  if existing != key}
            if stored is not None:
                if stored["digest"] != digest:
                    raise CommsError(REQUEST_CONFLICT)
                return self._duplicate_result(stored)
            ttl = self._ttl(kind, ttl_seconds)
            addresses = self._resolve_locked(descriptor, sender_address)
            candidate = {agent_id: self._copy_state(state)
                         for agent_id, state in self._recipients.items()}
            accepted, undeliverable = [], []
            for address in addresses:
                agent_id = address["agentId"]
                state = candidate.get(agent_id)
                if state is None:
                    undeliverable.append({"to": address,
                                          "reason": "unknown-recipient"})
                    continue
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
                state["messages"].append(envelope)
                accepted.append({"to": address,
                                 "messageId": envelope["messageId"],
                                 "sequence": envelope["sequence"]})
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
            if undeliverable:
                result["undeliverable"] = self._detach(undeliverable)
            return result

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
        with self._lock:
            self._expire_if_due_locked()
            state = self._recipients.get(agent_id)
            if state is None:
                raise CommsError("unknown-recipient")
            oldest = self._oldest_locked(state)
            if cursor is not None and int(cursor) + 1 < oldest:
                return {"status": "cursor-expired",
                        "oldestAvailableSequence": oldest}
            position = 0 if cursor is None else int(cursor)
            page = min(int(limit or self._page), self._page)
            waiting = [message for message in state["messages"]
                       if message["sequence"] > position]
            window = waiting[:page]
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
                raise CommsError("unknown-recipient")
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
        candidate = {}
        dropped = 0
        horizon = now - self._dedupe_ttl
        for agent_id, state in self._recipients.items():
            copied = self._copy_state(state)
            fresh = []
            for message in copied["messages"]:
                if message["expiresAt"] > now:
                    fresh.append(message)
                    continue
                dropped += 1
            copied["messages"] = fresh
            candidate[agent_id] = copied
        receipts = {key: self._detach(value)
                    for key, value in self._receipts.items()
                    if float(value.get("at") or 0) > horizon}
        receipts_dropped = len(self._receipts) - len(receipts)
        if dropped or receipts_dropped:
            self._commit_locked(candidate, receipts)
        return dropped + receipts_dropped

    # -- internals ---------------------------------------------------------

    def _commit_locked(self, recipients, receipts):
        """Persist first, publish second: nobody sees unsaved mail."""
        previous = (self._recipients, self._receipts)
        self._recipients, self._receipts = recipients, receipts
        try:
            encoded = json.dumps(self._snapshot_locked(), sort_keys=True)
            if len(encoded.encode("utf-8")) > self._store_bytes:
                raise CommsError(BUSY)
            self._file.write(encoded)
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
            raise CommsError("bad-request")
        return min(wanted, self._max_ttl)

    def _payload_text(self, payload):
        try:
            encoded = json.dumps(payload, sort_keys=True)
        except (TypeError, ValueError):
            raise CommsError("bad-request") from None
        if len(encoded.encode("utf-8")) > self._payload_bytes:
            raise CommsError("payload-too-large")
        return encoded

    def _address(self, value):
        if not isinstance(value, dict):
            raise CommsError("bad-request")
        agent_id = str(value.get("agentId") or "")
        if not agent_id:
            raise CommsError("bad-request")
        return {"nodeId": str(value.get("nodeId") or ""),
                "agentId": agent_id}

    def _detach(self, value):
        """A copy that shares no container with store-owned state."""
        return json.loads(json.dumps(value))

    def _digest(self, kind, encoded_payload, descriptor, ttl):
        material = json.dumps({"kind": kind,
                               "payload": json.loads(encoded_payload),
                               "target": descriptor,
                               "ttl": ttl}, sort_keys=True)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
