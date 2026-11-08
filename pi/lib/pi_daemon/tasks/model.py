"""One ticket's record; its execution environment is never persisted."""

from dataclasses import dataclass

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
        """The published ticket fields; execution environment is absent."""
        return {
            "id": self.ticket_id,
            "session": self.session,
            "cwd": self.cwd,
            "command": self.command,
            "timeout": self.timeout,
            "status": self.status,
            "created": self.created,
            "pid": self.pid,
            "started": self.started,
            "finished": self.ended,
            "exit": self.exit_code,
            "output_bytes": self.output_bytes,
            "truncated": self.truncated,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            ticket_id=str(data["id"]),
            session=str(data.get("session", "")),
            cwd=str(data.get("cwd", "")),
            command=str(data.get("command", "")),
            timeout=float(data.get("timeout", 0.0) or 0.0),
            status=str(data.get("status", RUNNING)),
            created=float(data.get("created", 0.0) or 0.0),
            pid=data.get("pid"),
            started=data.get("started"),
            ended=data.get("finished"),
            exit_code=data.get("exit"),
            output_bytes=int(data.get("output_bytes", 0) or 0),
            truncated=bool(data.get("truncated", False)),
            error=str(data.get("error", "")),
        )
