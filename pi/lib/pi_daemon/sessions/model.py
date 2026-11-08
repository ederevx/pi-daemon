"""The durable shape of one hosted session, detached from every owner."""


class SessionRecord:
    """One name's durable record: who it is, not who is running it."""

    FIELDS = ("id", "generation", "name", "dir", "argv", "resume_argv",
              "env", "file", "cols", "rows", "desired", "phase", "owner",
              "child", "created", "started")

    def __init__(self, **values):
        for name in self.FIELDS:
            setattr(self, name, values.get(name))
        self.argv = list(self.argv or [])
        self.resume_argv = list(self.resume_argv or [])
        self.env = dict(self.env or {})
        self.owner = dict(self.owner or {})
        self.child = dict(self.child or {})

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("a session record must be an object")
        values = {name: data.get(name) for name in cls.FIELDS}
        if not values.get("name") or not values.get("id"):
            raise ValueError("a session record needs a name and an id")
        return cls(**values)

    def to_dict(self):
        return {name: getattr(self, name) for name in self.FIELDS}

    def live(self):
        return self.phase in ("spawning", "running", "reviving")

    def pid(self):
        return self.child.get("pid") or 0

    def state(self):
        return self.child.get("state") or "idle"

    def view(self):
        """The published projection of one hosted session."""
        return {
            "name": self.name,
            "dir": self.dir,
            "pid": self.pid(),
            "attached": bool(self.child.get("clients")),
            "clients": int(self.child.get("clients") or 0),
            "file": self.file,
            "state": self.state(),
            "phase": self.phase,
            "desired": self.desired,
            "argv": list(self.argv),
        }

    def summary(self):
        return {"name": self.name, "state": self.state()}
