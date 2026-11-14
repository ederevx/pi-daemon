"""The mailboxes this node keeps for agents that live somewhere else.

A remote agent has no session here, so its mail is held in a mailbox of its
own. The id is derived from its origin, never chosen by it: one origin agent
has exactly one mailbox here, and no local identity may claim that id.
"""

import hashlib
import json
import os
import threading

from ..atomic_state_file import AtomicStateFile
from .error import CommsError

STORE_CORRUPT = "store-corrupt"
STORE_FAILED = "store-failed"
SCHEMA = 1
PREFIX = "peer-"


class PeerMailboxes:
    """It owns the origin-to-mailbox mapping; the store owns the mailbox."""

    def __init__(self, path, config):
        self._path = path
        self._config = config
        self._file = AtomicStateFile(path, config.file_mode,
                                     config.write_retries,
                                     config.write_retry_delay)
        self._lock = threading.Lock()
        # Two binds of the same origin would derive and claim the same id.
        self._bind_lock = threading.Lock()
        self._origins = {}

    def load(self):
        with self._lock:
            self._origins = self._restore(self._file.read())
        return self

    def save(self):
        """A node that never kept one for anybody writes no record."""
        with self._lock:
            if not self._origins and not os.path.exists(self._path):
                return
            self._write(self._origins)

    def bind(self, origin, store):
        """The mailbox this origin agent owns here, created for it alone."""
        with self._bind_lock:
            bound = self.mailbox(origin)
            if bound is not None and store.exists(bound):
                return {"nodeId": store.node_id(), "agentId": bound}
            return {"nodeId": store.node_id(),
                    "agentId": self._claim(origin, store)}

    def owns(self, agent_id):
        """Whether this id belongs to a remote agent rather than a local one."""
        with self._lock:
            return agent_id in self._origins.values()

    def permit(self, agent_id):
        """Whether a local identity may take this id.

        This is the rule a local reservation applies, and it is asked inside
        the store's own lock: asked before it, the answer can pass just as a
        peer creates the id, and the reservation would adopt that mailbox.
        """
        return not self.owns(agent_id)

    def mailbox(self, origin):
        """The mailbox already bound to this origin, or `None`."""
        with self._lock:
            return self._origins.get(self._key(origin))

    # -- allocation --------------------------------------------------------

    def _claim(self, origin, store):
        """Publish the claim, then create the mailbox it names.

        A local hello is refused any id this node has claimed for a remote
        agent, so the claim is written first: created-then-recorded would
        leave the new mailbox open to adoption for the width of two writes.
        A claim whose mailbox this call could not create is withdrawn, and
        the next bind repairs a record left by a process that died claiming.
        """
        key = self._key(origin)
        with self._lock:
            before = dict(self._origins)
        held = set(before.values())
        try:
            for attempt in range(self._config.comms_recipient_cap):
                proxy = self._derive(origin, attempt)
                if proxy in held or store.exists(proxy):
                    continue
                candidate = dict(before)
                candidate[key] = proxy
                with self._lock:
                    self._write(candidate)
                    self._origins = candidate
                if store.allocate(proxy):
                    return proxy
        except CommsError:
            self._withdraw(before)
            raise
        self._withdraw(before)
        raise CommsError(STORE_FAILED)

    def _withdraw(self, origins):
        """No claim outlives the mailbox it could not create."""
        with self._lock:
            if self._origins == origins:
                return
            self._write(origins)
            self._origins = dict(origins)
            if not origins:
                # An empty mapping is the absence of a record, not a record.
                try:
                    os.unlink(self._path)
                except OSError:
                    pass

    def _derive(self, origin, attempt):
        """A stable id: the same origin keeps the same mailbox after restart."""
        seed = "\x00".join([origin["nodeId"], origin["agentId"],
                            str(attempt)])
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        room = max(self._config.comms_agent_id_limit - len(PREFIX), 0)
        return PREFIX + digest[:room]

    # -- persistence -------------------------------------------------------

    def _restore(self, text):
        if text is None:
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            raise CommsError(STORE_CORRUPT) from None
        if not isinstance(data, dict) or data.get("version") != SCHEMA or \
                not isinstance(data.get("origins"), dict):
            raise CommsError(STORE_CORRUPT)
        restored = {}
        for key, value in data["origins"].items():
            if not isinstance(key, str) or not self._is_key(key) or \
                    not self._is_proxy(value) or value in restored.values():
                raise CommsError(STORE_CORRUPT)
            restored[key] = value
        return restored

    def _write(self, origins):
        try:
            self._file.write_dict({"version": SCHEMA, "origins": origins})
        except OSError:
            raise CommsError(STORE_FAILED) from None

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _key(origin):
        return json.dumps([origin["nodeId"], origin["agentId"]])

    @staticmethod
    def _is_key(key):
        try:
            value = json.loads(key)
        except ValueError:
            return False
        return isinstance(value, list) and len(value) == 2 and \
            all(isinstance(item, str) and item for item in value)

    def _is_proxy(self, value):
        """A record names one of this node's own derived ids, or nothing."""
        if not isinstance(value, str) or not value.startswith(PREFIX):
            return False
        if len(value) > self._config.comms_agent_id_limit:
            return False
        tail = value[len(PREFIX):]
        return bool(tail) and all(character in "0123456789abcdef"
                                  for character in tail)
