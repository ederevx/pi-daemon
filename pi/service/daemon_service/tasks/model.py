"""One ticket's record; its execution environment is never persisted."""

from dataclasses import dataclass, fields

RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"


@dataclass
class Ticket:
    ticket_id: str
    session: str
    cwd: str
    command: str
    timeout: float = 0.0
    status: str = RUNNING
    created: float = 0.0
    pid: int = None
    started: float = None
    ended: float = None
    exit_code: int = None
    output_bytes: int = 0
    truncated: bool = False
    error: str = ""

    def to_dict(self):
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_dict(cls, data):
        known = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items()
                      if key in known and key != "ticket_id"},
                   ticket_id=str(data["ticket_id"]))
