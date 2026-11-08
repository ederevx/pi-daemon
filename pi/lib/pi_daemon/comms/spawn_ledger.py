"""The launch ledger: a spawn record exists before any process starts."""

import json
import threading
import time
import uuid

from ..atomic_state_file import AtomicStateFile
from .error import CommsError

BUSY = "busy"
REQUEST_CONFLICT = "request-conflict"
STORE_CORRUPT = "store-corrupt"
STORE_FAILED = "store-failed"
LAUNCHING = "launching"
SPAWNED = "spawned"
FAILED = "failed"
INTERRUPTED = "interrupted"
SCHEMA = 1
ENV_PREFIX = "PI_COMMS_"
ENV_AGENT_ID = ENV_PREFIX + "AGENT_ID"
ENV_NODE_ID = ENV_PREFIX + "NODE_ID"
ENV_PARENT_AGENT_ID = ENV_PREFIX + "PARENT_AGENT_ID"
ENV_SPAWN_ID = ENV_PREFIX + "SPAWN_ID"
ENV_HOST = ENV_PREFIX + "HOST"
ENV_PORT = ENV_PREFIX + "PORT"


class SpawnLedger:
    """Keyed by the parent address and its request id, like a send receipt.

    A record names one launch, not whether the process is alive: liveness
    belongs to the sessions domain, and a record never expires while it is
    still `launching`.
    """

    def __init__(self, path, config, node_id, clock=None):
        self._file = AtomicStateFile(path, config.file_mode,
                                     config.write_retries,
                                     config.write_retry_delay)
        self._node_id = node_id
        self._cap = config.comms_spawn_records
        self._dedupe_ttl = config.comms_dedupe_ttl
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._spawns = {}

    def node_id(self):
        return self._node_id

    # -- persistence -------------------------------------------------------

    def load(self):
        """Anything still `launching` was interrupted by this restart.

        The settlement is written before it is published, so a recovery
        that cannot reach the disk leaves both memory and the file as the
        restart found them.
        """
        with self._lock:
            candidate = self._restore(self._file.read())
            interrupted = [record for record in candidate.values()
                           if record["status"] == LAUNCHING]
            for record in interrupted:
                record["status"] = FAILED
                record["reason"] = INTERRUPTED
                record["settled"] = self._clock()
            if interrupted:
                self._write(candidate)
            self._spawns = candidate
        return self

    def save(self):
        with self._lock:
            self._write(self._spawns)

    def _restore(self, text):
        if text is None:
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            raise CommsError(STORE_CORRUPT) from None
        if not isinstance(data, dict) or data.get("version") != SCHEMA:
            raise CommsError(STORE_CORRUPT)
        spawns = data.get("spawns")
        if not isinstance(spawns, dict):
            raise CommsError(STORE_CORRUPT)
        restored = {}
        for key, value in spawns.items():
            restored[key] = self._record(value)
        return restored

    def _record(self, value):
        if not isinstance(value, dict):
            raise CommsError(STORE_CORRUPT)
        record = {field: value.get(field) for field in (
            "spawnId", "requestId", "digest", "parent", "agent", "session",
            "status", "reason", "at", "settled")}
        if not record["spawnId"] or not record["digest"] or \
                not isinstance(record["parent"], dict) or \
                not isinstance(record["agent"], dict) or \
                not isinstance(record["session"], dict) or \
                record["status"] not in (LAUNCHING, SPAWNED, FAILED):
            raise CommsError(STORE_CORRUPT)
        return record

    def _write(self, spawns):
        """A write that fails is a domain failure, never a raw traceback.

        Memory changes only after a write succeeds, so a failed write leaves
        the record exactly as the last durable one describes it.
        """
        try:
            self._file.write_dict({"version": SCHEMA, "spawns": spawns})
        except OSError:
            raise CommsError(STORE_FAILED) from None

    # -- the ledger --------------------------------------------------------

    def find(self, parent, request_id, digest):
        """A recorded launch for this request, or `None` if it is new."""
        key = self._key(parent, request_id)
        with self._lock:
            self._expire_locked()
            stored = self._spawns.get(key)
            if stored is None:
                return None
            if stored["digest"] != digest:
                raise CommsError(REQUEST_CONFLICT)
            return self._detach(stored)

    def open(self, parent, request_id, digest, agent, name):
        key = self._key(parent, request_id)
        with self._lock:
            self._expire_locked()
            if len(self._spawns) >= self._cap:
                raise CommsError(BUSY)
            record = {
                "spawnId": uuid.uuid4().hex,
                "requestId": str(request_id),
                "digest": digest,
                "parent": dict(parent),
                "agent": dict(agent),
                "session": {"name": name},
                "status": LAUNCHING,
                "reason": None,
                "at": self._clock(),
                "settled": None,
            }
            candidate = dict(self._spawns)
            candidate[key] = record
            self._write(candidate)
            self._spawns = candidate
            return self._detach(record)

    def discard(self, parent, request_id):
        """Undo an `open` that never reached a launch."""
        key = self._key(parent, request_id)
        with self._lock:
            if key not in self._spawns:
                return
            candidate = {other: value for other, value in self._spawns.items()
                         if other != key}
            self._write(candidate)
            self._spawns = candidate

    def settle(self, parent, request_id, status, reason=None):
        """A started process cannot be unstarted: keep the fact in memory."""
        key = self._key(parent, request_id)
        with self._lock:
            stored = self._spawns.get(key)
            if stored is None:
                return None
            record = dict(stored)
            record["status"] = status
            record["reason"] = reason
            record["settled"] = self._clock()
            candidate = dict(self._spawns)
            candidate[key] = record
            self._write(candidate)
            self._spawns = candidate
            return self._detach(record)

    def _expire_locked(self):
        horizon = self._clock() - self._dedupe_ttl
        stale = [key for key, record in self._spawns.items()
                 if record["status"] != LAUNCHING and
                 float(record.get("settled") or 0) <= horizon]
        if not stale:
            return
        candidate = {key: value for key, value in self._spawns.items()
                     if key not in stale}
        self._write(candidate)
        self._spawns = candidate

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _key(parent, request_id):
        return json.dumps([parent, str(request_id)], sort_keys=True)

    @staticmethod
    def _detach(record):
        return {field: (dict(value) if isinstance(value, dict) else value)
                for field, value in record.items()}
