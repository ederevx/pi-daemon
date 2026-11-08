"""One command ticket: the record the store persists and the runner
advances through its states.
"""

import time

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
TIMEOUT = "timeout"
CANCELLED = "cancelled"
FINISHED = (DONE, FAILED, TIMEOUT, CANCELLED)


class Ticket:
    """The record of one offloaded command."""

    def __init__(self, id, session, cwd, command, env=None, timeout=None):
        self.id = id
        self.session = session
        self.cwd = cwd
        self.command = command
        self.env = env or {}
        self.timeout = timeout
        self.status = QUEUED
        self.pid = None
        self.exit = None
        self.error = None
        self.truncated = False
        self.created = time.time()
        self.started = None
        self.finished = None

    def to_dict(self):
        return {
            "id": self.id, "session": self.session, "cwd": self.cwd,
            "command": self.command, "env": self.env, "timeout": self.timeout,
            "status": self.status, "pid": self.pid, "exit": self.exit,
            "error": self.error, "truncated": self.truncated,
            "created": self.created, "started": self.started,
            "finished": self.finished,
        }

    @classmethod
    def from_dict(cls, data):
        ticket = cls(data["id"], data["session"], data["cwd"],
                     data["command"], data.get("env"), data.get("timeout"))
        for field in ("status", "pid", "exit", "error", "truncated",
                      "created", "started", "finished"):
            if field in data:
                setattr(ticket, field, data[field])
        return ticket

    def view(self):
        """The client-facing shape: no internal bookkeeping."""
        return self.to_dict()

    def finish(self, status, exit_code=None, error=None):
        self.status = status
        self.exit = exit_code
        self.error = error
        self.finished = time.time()
