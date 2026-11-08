"""The ticket store: durable records and the output log of each ticket.

One file holds every record; each ticket's output is its own log file so
a large command never rewrites the index. Both are written atomically
from this owner alone.
"""

import os
import threading

from .model import FINISHED, Ticket

CHUNK = 256 << 10


class TicketStore:
    """Owns the ticket records and their output logs."""

    def __init__(self, state, max_records=200, max_output_bytes=4 << 20):
        self.state = state
        self.path = state.tickets_path()
        self.log_dir = state.ticket_log_dir()
        self.max_records = max_records
        self.max_output_bytes = max_output_bytes
        self.lock = threading.RLock()
        self.records = {}
        self.counter = 0
        self._load()

    def _load(self):
        data = self.state.read_json(self.path, {}) or {}
        for tid, raw in (data.get("tickets") or {}).items():
            try:
                self.records[tid] = Ticket.from_dict(raw)
            except (KeyError, TypeError):
                continue
        try:
            self.counter = int(data.get("counter") or 0)
        except (TypeError, ValueError):
            self.counter = 0

    def _persist(self):
        self.state.write_json(self.path, {
            "counter": self.counter,
            "tickets": {t.id: t.to_dict() for t in self.records.values()},
        })

    def alloc(self, session):
        with self.lock:
            self.counter += 1
            self._persist()
            return "t-%d" % self.counter

    def put(self, ticket):
        with self.lock:
            self.records[ticket.id] = ticket
            self._evict()
            self._persist()

    def _evict(self):
        finished = [t for t in self.records.values()
                    if t.status in FINISHED]
        overflow = len(finished) - self.max_records
        if overflow <= 0:
            return
        finished.sort(key=lambda t: t.finished or 0.0)
        for ticket in finished[:overflow]:
            self.records.pop(ticket.id, None)
            self._unlink_log(ticket.id)

    def get(self, tid):
        with self.lock:
            return self.records.get(tid)

    def snapshot(self, session=None):
        with self.lock:
            tickets = [t for t in self.records.values()
                       if session is None or t.session == session]
        tickets.sort(key=lambda t: t.created)
        return [t.view() for t in tickets]

    def remove(self, tid):
        with self.lock:
            ticket = self.records.get(tid)
            if ticket is None:
                return None
            if ticket.status not in FINISHED:
                raise ValueError("running")
            self.records.pop(tid, None)
            self._unlink_log(tid)
            self._persist()
            return ticket

    def reset(self):
        with self.lock:
            removed = len(self.records)
            self.records.clear()
            for name in os.listdir(self.log_dir):
                try:
                    os.unlink(os.path.join(self.log_dir, name))
                except OSError:
                    pass
            self._persist()
            return removed

    def log_path(self, tid):
        return os.path.join(self.log_dir, tid + ".log")

    def _unlink_log(self, tid):
        try:
            os.unlink(self.log_path(tid))
        except OSError:
            pass

    def append_output(self, tid, data):
        """Append to a ticket's log, bounded; returns True once truncated."""
        path = self.log_path(tid)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        room = self.max_output_bytes - size
        if room <= 0:
            return True
        with open(path, "ab") as f:
            f.write(data[:room])
        return len(data) > room

    def read_output(self, tid, offset):
        path = self.log_path(tid)
        try:
            size = os.path.getsize(path)
        except OSError:
            return {"offset": offset, "next": offset, "size": 0,
                    "data": b""}
        with open(path, "rb") as f:
            f.seek(min(offset, size))
            data = f.read(CHUNK)
        return {"offset": offset, "next": offset + len(data), "size": size,
                "data": data}

    def text_output(self, tid):
        try:
            with open(self.log_path(tid), "r", encoding="utf-8",
                      errors="replace") as f:
                return f.read()
        except OSError:
            return ""
