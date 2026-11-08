"""The durable record of directly configured peers: endpoints, no secrets.

A peer is a direction this node reaches out over; the file holds the SSH
destination, the port on both ends and the reference to the credential the
outbound link reads. It never holds a credential's contents.
"""

import json
import threading
import uuid

from ..atomic_state_file import AtomicStateFile
from .error import CommsError
from .mailbox_store import BAD_REQUEST, STORE_CORRUPT

SCHEMA = 1
ACTIVE = "active"
REMOVING = "removing"


class PeerConfigStore:
    """Configuration changes commit before any link or worker exists."""

    def __init__(self, path, config):
        self._file = AtomicStateFile(path, config.file_mode,
                                     config.write_retries,
                                     config.write_retry_delay)
        self._cap = config.comms_peer_cap
        self._lock = threading.RLock()
        self._peers = {}

    def load(self):
        with self._lock:
            text = self._file.read()
            if text is None:
                self._peers = {}
                return
            try:
                data = json.loads(text)
            except ValueError:
                raise CommsError(STORE_CORRUPT) from None
            self._peers = self._restore(data)

    def save(self):
        with self._lock:
            self._file.write_dict(self._snapshot_locked())

    def get(self, node_id):
        with self._lock:
            record = self._peers.get(node_id)
            return None if record is None else _detach(record)

    def list(self):
        with self._lock:
            return [_detach(self._peers[node_id])
                    for node_id in sorted(self._peers)]

    def active_generation(self, node_id):
        """The generation a delivery may reference, or None."""
        with self._lock:
            record = self._peers.get(node_id)
            if record is None or record["state"] != ACTIVE:
                return None
            return record["generation"]

    def add(self, spec):
        """Idempotent for an identical record; a change needs remove first."""
        with self._lock:
            node_id = spec["nodeId"]
            existing = self._peers.get(node_id)
            if existing is not None:
                if _same(existing, spec):
                    return {"peer": _detach(existing), "created": False,
                            "conflict": False}
                return {"peer": _detach(existing), "created": False,
                        "conflict": True}
            if len(self._peers) >= self._cap:
                raise CommsError(BAD_REQUEST)
            record = {"generation": uuid.uuid4().hex, "state": ACTIVE,
                      "nodeId": node_id,
                      "sshTarget": spec["sshTarget"],
                      "remotePort": int(spec["remotePort"]),
                      "localPort": int(spec.get("localPort") or 0),
                      "outboundCredentialFile":
                          spec["outboundCredentialFile"],
                      "inboundCredentialSha256":
                          spec["inboundCredentialSha256"]}
            candidate = dict(self._peers)
            candidate[node_id] = record
            self._commit_locked(candidate)
            return {"peer": _detach(record), "created": True,
                    "conflict": False}

    def begin_remove(self, node_id):
        """Mark one generation removing, so imports and attempts stop."""
        with self._lock:
            record = self._peers.get(node_id)
            if record is None:
                return None
            if record["state"] == REMOVING:
                return _detach(record)
            changed = _detach(record)
            changed["state"] = REMOVING
            candidate = dict(self._peers)
            candidate[node_id] = changed
            self._commit_locked(candidate)
            return _detach(changed)

    def finish_remove(self, node_id):
        with self._lock:
            record = self._peers.get(node_id)
            if record is None:
                return None
            candidate = {key: value for key, value in self._peers.items()
                         if key != node_id}
            self._commit_locked(candidate)
            return _detach(record)

    def removing(self):
        with self._lock:
            return [_detach(record) for record in self._peers.values()
                    if record["state"] == REMOVING]

    def _commit_locked(self, peers):
        previous = self._peers
        self._peers = peers
        try:
            self._file.write_dict(self._snapshot_locked())
        except BaseException:
            self._peers = previous
            raise

    def _snapshot_locked(self):
        return {"version": SCHEMA,
                "peers": {node_id: _detach(record)
                          for node_id, record in self._peers.items()}}

    def _restore(self, data):
        if not isinstance(data, dict) or data.get("version") != SCHEMA:
            raise CommsError(STORE_CORRUPT)
        raw = data.get("peers")
        if not isinstance(raw, dict):
            raise CommsError(STORE_CORRUPT)
        peers = {}
        for node_id, value in raw.items():
            try:
                if not isinstance(value, dict) or \
                        value["state"] not in (ACTIVE, REMOVING):
                    raise CommsError(STORE_CORRUPT)
                record = {
                    "generation": _text(value["generation"]),
                    "state": value["state"],
                    "nodeId": _text(value["nodeId"]),
                    "sshTarget": _text(value["sshTarget"]),
                    "remotePort": _port(value["remotePort"]),
                    "localPort": _port(value.get("localPort", 0)),
                    "outboundCredentialFile":
                        _text(value["outboundCredentialFile"]),
                    "inboundCredentialSha256":
                        _text(value["inboundCredentialSha256"]),
                }
            except (KeyError, TypeError, ValueError):
                raise CommsError(STORE_CORRUPT) from None
            if record["nodeId"] != node_id:
                raise CommsError(STORE_CORRUPT)
            peers[node_id] = record
        return peers


def _same(record, spec):
    return (record["sshTarget"] == spec["sshTarget"] and
            record["remotePort"] == int(spec["remotePort"]) and
            record["localPort"] == int(spec.get("localPort") or 0) and
            record["outboundCredentialFile"] ==
            spec["outboundCredentialFile"] and
            record["inboundCredentialSha256"] ==
            spec["inboundCredentialSha256"])


def _detach(value):
    return json.loads(json.dumps(value))


def _text(value):
    if not isinstance(value, str) or not value:
        raise CommsError(STORE_CORRUPT)
    return value


def _port(value):
    if isinstance(value, bool) or not isinstance(value, int) or \
            not 0 <= value <= 65535:
        raise CommsError(STORE_CORRUPT)
    return value
