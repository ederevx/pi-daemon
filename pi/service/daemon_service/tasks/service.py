"""The tasks domain: the one owner of command tickets.

It composes the durable store and the process runner, and it is the only
object another domain or the wire layer talks to about tickets.
"""

import threading
import time

from .model import FINISHED, RUNNING
from .runner import TicketRunner
from .store import TicketStore


class TaskService:
    """Owns tickets: their records, their execution, and their expiry."""

    def __init__(self, state, config):
        self.config = config
        self.store = TicketStore(state,
                                 max_output_bytes=config.max_output_bytes)
        self.runner = TicketRunner(self.store, config)
        self.stopping = threading.Event()
        self.sweeper = None

    def start(self):
        if self.config.ticket_gc_seconds > 0:
            self.sweeper = threading.Thread(target=self._sweep_loop,
                                            daemon=True)
            self.sweeper.start()

    def stop(self):
        self.stopping.set()
        self.runner.stop()

    def submit(self, session, cwd, command, env=None, timeout=None):
        return self.runner.submit(session, cwd, command, env, timeout)

    def find_running(self, session, cwd, command):
        for ticket in self.store.snapshot(session):
            if ticket["status"] == RUNNING and ticket["cwd"] == cwd \
                    and ticket["command"] == command:
                return ticket
        return None

    def running(self):
        return self.runner.running()

    def wait(self, tid, timeout):
        return self.runner.wait(tid, timeout)

    def output(self, tid, offset):
        return self.runner.output(tid, offset)

    def list(self, session=None):
        return self.store.snapshot(session)

    def cancel(self, tid):
        return self.runner.cancel(tid)

    def remove(self, tid):
        return self.store.remove(tid)

    def reset(self):
        for ticket in self.store.snapshot():
            if ticket["status"] not in FINISHED:
                self.runner.cancel(ticket["id"])
        return self.store.reset()

    def text_output(self, tid):
        return self.store.text_output(tid)

    def _sweep_loop(self):
        while not self.stopping.wait(self.config.ticket_gc_seconds):
            self.sweep()

    def sweep(self):
        """Forget finished tickets past their retention window."""
        now = time.time()
        for ticket in self.store.snapshot():
            if ticket["status"] not in FINISHED:
                continue
            finished = ticket.get("finished") or ticket.get("created")
            if now - finished < self.config.ticket_ttl:
                continue
            try:
                self.store.remove(ticket["id"])
            except ValueError:
                continue
