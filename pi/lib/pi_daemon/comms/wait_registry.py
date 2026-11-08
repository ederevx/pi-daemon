"""The wait registry: a pending wait never holds its connection."""

import threading
import time

from ..transport.rpc_codec import CANCELLED, UNAVAILABLE
from .error import CommsError

BUSY = "busy"
CLOSING = "closing"
WAIT_PENDING = "wait-pending"
UNAVAILABLE_REASON = "unavailable"


class WaitRegistry:
    """One thread answers waits from the mailbox, so a socket keeps reading."""

    def __init__(self, store, config, clock=None):
        self._store = store
        self._cap = config.comms_wait_cap
        self._per_connection = config.comms_waits_per_connection
        self._grace = config.stop_grace
        self._clock = clock or time.monotonic
        self._condition = threading.Condition()
        self._waits = {}
        self._closing = False
        self._thread = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._run, name="comms-wait",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        """Refuse every pending wait before the owner closes its sockets.

        The refusals are taken here rather than left to the answering
        thread, so a wait the thread is still answering is refused and
        never completed after shutdown began. A store write that outlives
        the grace ends with the process: the thread is a daemon.
        """
        with self._condition:
            self._closing = True
            pending = list(self._waits)
            self._waits.clear()
            self._condition.notify_all()
        for session, request_id in pending:
            session.refuse(request_id, UNAVAILABLE, CLOSING)
        if self._thread is not None:
            self._thread.join(self._grace)

    def closing(self):
        with self._condition:
            return self._closing

    def pending(self):
        with self._condition:
            return len(self._waits)

    # -- registration ------------------------------------------------------

    def begin(self, session, request_id, agent_id, cursor, limit, timeout):
        """Register a wait. The caller answers nothing until it completes."""
        key = (session, request_id)
        with self._condition:
            if self._closing:
                raise CommsError(CLOSING)
            if key in self._waits:
                raise CommsError(WAIT_PENDING)
            if len(self._waits) >= self._cap or \
                    self._mine(session) >= self._per_connection:
                raise CommsError(BUSY)
            self._waits[key] = {
                "agent_id": agent_id,
                "cursor": cursor,
                "limit": limit,
                "deadline": self._clock() + float(timeout),
                "ready": True,
                "busy": False,
            }
            self._condition.notify_all()

    def signal(self, agent_ids):
        """Mail was committed for these mailboxes. Their waits may answer."""
        if not agent_ids:
            return
        with self._condition:
            changed = False
            for entry in self._waits.values():
                if entry["agent_id"] in agent_ids:
                    entry["ready"] = True
                    changed = True
            if changed:
                self._condition.notify_all()

    def cancel(self, session, request_id):
        """Answer one pending wait `-32800`. Nothing happens if it is gone."""
        key = (session, request_id)
        with self._condition:
            entry = self._waits.pop(key, None)
            self._condition.notify_all()
        if entry is None:
            return False
        session.refuse(request_id, CANCELLED, "cancelled")
        return True

    def drop(self, session):
        """A closed connection's waits vanish unanswered."""
        with self._condition:
            for key in [key for key in self._waits if key[0] is session]:
                self._waits.pop(key, None)
            self._condition.notify_all()

    def _mine(self, session):
        return sum(1 for key in self._waits if key[0] is session)

    # -- the answering thread ----------------------------------------------

    def _run(self):
        while True:
            batch, closing = self._collect()
            if closing:
                for key, entry in batch:
                    self._refuse(key, entry, CLOSING)
                return
            for key, entry in batch:
                try:
                    self._evaluate(key, entry)
                except Exception:
                    # One entry must never disarm every other wait.
                    self._refuse(key, entry, UNAVAILABLE_REASON)

    def _collect(self):
        """Take the entries to look at, or all of them when closing."""
        with self._condition:
            while True:
                if self._closing:
                    return list(self._waits.items()), True
                ready = [(key, entry)
                         for key, entry in self._waits.items()
                         if not entry["busy"] and
                         (entry["ready"] or self._clock() >= entry["deadline"])]
                if ready:
                    for _, entry in ready:
                        entry["busy"] = True
                        entry["ready"] = False
                    return ready, False
                self._condition.wait(self._next_deadline())

    def _next_deadline(self):
        waiting = [entry["deadline"] for entry in self._waits.values()
                   if not entry["busy"]]
        if not waiting:
            return None
        return max(0.0, min(waiting) - self._clock())

    def _evaluate(self, key, entry):
        try:
            result = self._store.replay(entry["agent_id"], entry["cursor"],
                                        entry["limit"])
        except (CommsError, OSError, ValueError):
            self._refuse(key, entry, UNAVAILABLE_REASON)
            return
        if result["status"] == "cursor-expired" or result["messages"]:
            if self._take(key, entry):
                self._answer(key, entry, result)
            return
        if self._clock() >= entry["deadline"]:
            if self._take(key, entry):
                self._answer(key, entry, {
                    "status": "timed-out", "messages": [],
                    "cursor": entry["cursor"], "more": False})
            return
        with self._condition:
            if self._waits.get(key) is entry:
                entry["busy"] = False
                self._condition.notify_all()

    def _take(self, key, entry, refusal=False):
        """The one owner of an answer: whoever removes the entry sends it.

        A normal answer may not begin after shutdown started. The entry
        then stays for the closing pass, which refuses every remaining wait.
        """
        with self._condition:
            if self._waits.get(key) is not entry:
                return False
            if self._closing and not refusal:
                return False
            del self._waits[key]
            self._condition.notify_all()
            return True

    def _refuse(self, key, entry, message):
        if self._take(key, entry, refusal=True):
            session, request_id = key
            session.refuse(request_id, UNAVAILABLE, message)

    @staticmethod
    def _answer(key, entry, result):
        session, request_id = key
        session.complete(request_id, result)
