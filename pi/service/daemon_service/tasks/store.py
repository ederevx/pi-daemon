"""The sole owner of ticket records, their IDs and their output logs."""

import os
import threading
import time

from ..atomic_state_file import AtomicStateFile
from ..wire import TICKET_RUNNING, TOO_BUSY
from .error import TaskError
from .model import FAILED, RUNNING, Ticket


class TicketStore:
    """Records leave this object as detached snapshots, never as state."""

    def __init__(self, path, logs_dir, config):
        self._file = AtomicStateFile(path, config.file_mode,
                                     config.write_retries,
                                     config.write_retry_delay)
        self._logs = logs_dir
        self._record_cap = config.record_cap
        self._output_cap = config.output_cap
        self._lock = threading.RLock()
        self._records = {}
        self._order = []
        self._next = 0

    def load(self):
        with self._lock:
            data = self._file.read_dict() or {}
            records = {}
            for item in data.get("tickets", []):
                try:
                    ticket = Ticket.from_dict(item)
                except (TypeError, KeyError):
                    continue
                records[ticket.ticket_id] = ticket
            order = [tid for tid in data.get("order", []) if tid in records]
            order += [tid for tid in records if tid not in order]
            for ticket in records.values():
                if ticket.status == RUNNING:
                    ticket.status = FAILED
                    ticket.error = "interrupted by a restart"
                    ticket.ended = time.time()
            self._records = records
            self._order = order
            self._next = int(data.get("next", 0) or 0)
            self._save_locked()

    def save(self):
        with self._lock:
            self._save_locked()

    def _save_locked(self):
        self._file.write_dict({
            "next": self._next,
            "order": list(self._order),
            "tickets": [self._records[tid].to_dict() for tid in self._order
                        if tid in self._records],
        })

    def replace_all(self, tickets, next_id):
        """Replace every record and drop every log: a translated store is
        not a merge. Output is appended afterwards through append_output.
        """
        with self._lock:
            entries = os.listdir(self._logs) if os.path.isdir(self._logs) \
                else []
            for entry in entries:
                if entry.endswith(".log"):
                    self._discard_log(entry[:-len(".log")])
            self._records = {ticket.ticket_id: ticket for ticket in tickets}
            self._order = [ticket.ticket_id for ticket in tickets]
            self._next = int(next_id)
            self._save_locked()

    def reserve(self, session, cwd, command, timeout, capacity):
        """One atomic operation: replay a running match or create a ticket.

        The replay is decided before capacity, because a re-sent identical
        command is a replay, never a new load.
        """
        with self._lock:
            for ticket_id in self._order:
                ticket = self._records[ticket_id]
                if (ticket.status == RUNNING and ticket.session == session
                        and ticket.cwd == cwd and ticket.command == command):
                    return ticket.to_dict(), False
            if self.running_count() >= capacity:
                raise TaskError(TOO_BUSY)
            self._next += 1
            ticket = Ticket(
                ticket_id=f"t-{os.getpid()}-{self._next}",
                session=session,
                cwd=cwd,
                command=command,
                timeout=float(timeout or 0.0),
                created=time.time(),
            )
            self._records[ticket.ticket_id] = ticket
            self._order.append(ticket.ticket_id)
            self._save_locked()
            return ticket.to_dict(), True

    def mark_running(self, ticket_id, pid, started):
        with self._lock:
            ticket = self._owned(ticket_id)
            ticket.pid = pid
            ticket.started = started
            self._save_locked()
            return ticket.to_dict()

    def mark_truncated(self, ticket_id):
        with self._lock:
            ticket = self._owned(ticket_id)
            ticket.truncated = True
            self._save_locked()

    def finish(self, ticket_id, status, exit_code=None, error=""):
        with self._lock:
            ticket = self._owned(ticket_id)
            ticket.status = status
            ticket.exit_code = exit_code
            ticket.error = error
            ticket.ended = time.time()
            self._save_locked()
            snapshot = ticket.to_dict()
            self._cap_locked()
            return snapshot

    def snapshot_one(self, ticket_id):
        with self._lock:
            ticket = self._records.get(ticket_id)
            return ticket.to_dict() if ticket else None

    def snapshot(self, session=None):
        with self._lock:
            return [self._records[tid].to_dict() for tid in self._order
                    if self._records[tid].session == session] if session \
                else [self._records[tid].to_dict() for tid in self._order]

    def running_count(self):
        with self._lock:
            return sum(1 for ticket in self._records.values()
                       if ticket.status == RUNNING)

    def running_ids(self):
        with self._lock:
            return [tid for tid in self._order
                    if self._records[tid].status == RUNNING]

    def log_path(self, ticket_id):
        return os.path.join(self._logs, f"{ticket_id}.log")

    def append_output(self, ticket_id, data):
        with self._lock:
            ticket = self._records.get(ticket_id)
            if ticket is None:
                return 0
            remaining = self._output_cap - ticket.output_bytes
            if remaining <= 0:
                if not ticket.truncated:
                    ticket.truncated = True
                    self._save_locked()
                return 0
            chunk = data[:remaining]
            with open(self.log_path(ticket_id), "ab") as handle:
                handle.write(chunk)
            ticket.output_bytes += len(chunk)
            if len(chunk) < len(data):
                ticket.truncated = True
            self._save_locked()
            return len(chunk)

    def output(self, ticket_id, offset=0):
        """Raw bytes from one offset, with the clamped offset and the size."""
        with self._lock:
            ticket = self._records.get(ticket_id)
            if ticket is None:
                raise LookupError(f"unknown ticket: {ticket_id}")
            size = ticket.output_bytes
        offset = max(0, min(int(offset or 0), size))
        try:
            with open(self.log_path(ticket_id), "rb") as handle:
                handle.seek(offset)
                data = handle.read(size - offset)
        except FileNotFoundError:
            data = b""
        return data, offset, offset + len(data), size

    def discard(self, ticket_id):
        """Drop a reservation that never became a run."""
        with self._lock:
            ticket = self._records.pop(ticket_id, None)
            if ticket is None:
                return False
            if ticket_id in self._order:
                self._order.remove(ticket_id)
            self._save_locked()
            self._discard_log(ticket_id)
            return True

    def remove(self, ticket_id):
        with self._lock:
            ticket = self._records.get(ticket_id)
            if ticket is None:
                raise LookupError(f"unknown ticket: {ticket_id}")
            if ticket.status == RUNNING:
                raise TaskError(TICKET_RUNNING)
            self._records.pop(ticket_id, None)
            if ticket_id in self._order:
                self._order.remove(ticket_id)
            self._save_locked()
            self._discard_log(ticket_id)
            return True

    def reset_all(self):
        with self._lock:
            removed = list(self._order)
            self._records.clear()
            self._order.clear()
            self._save_locked()
            for ticket_id in removed:
                self._discard_log(ticket_id)
            return len(removed)

    def _cap_locked(self):
        finished = [tid for tid in self._order
                    if self._records[tid].status != RUNNING]
        for ticket_id in finished[:max(0, len(finished) - self._record_cap)]:
            self._records.pop(ticket_id, None)
            self._order.remove(ticket_id)
            self._discard_log(ticket_id)
        self._save_locked()

    def _discard_log(self, ticket_id):
        try:
            os.unlink(self.log_path(ticket_id))
        except OSError:
            pass

    def _owned(self, ticket_id):
        ticket = self._records.get(ticket_id)
        if ticket is None:
            raise LookupError(f"unknown ticket: {ticket_id}")
        return ticket
