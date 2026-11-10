"""Process identity: a PID is only evidence with its boot and start token."""

import pi_platform


class ProcessIdentity:
    """A recycled PID is indistinguishable from its predecessor alone."""

    SAME = "same"
    GONE = "gone"
    UNKNOWN = "unknown"

    def __init__(self, pid, boot=None, start=None):
        self.pid = int(pid or 0)
        self.boot = boot
        self.start = start

    @classmethod
    def current(cls, pid):
        """The identity of the process this PID names right now."""
        pid = int(pid or 0)
        if pid <= 0:
            return cls(0)
        return cls(pid, pi_platform.boot_identity(),
                   pi_platform.ProcessControl().process_start_token(pid))

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            return cls(0)
        return cls(data.get("pid"), data.get("boot"), data.get("start"))

    def to_dict(self):
        return {"pid": self.pid, "boot": self.boot, "start": self.start}

    def classify(self, expected):
        """Compare this fresh reading against the recorded identity."""
        if not isinstance(expected, ProcessIdentity) or expected.pid <= 0:
            return self.GONE
        if self.pid != expected.pid:
            return self.GONE
        if self.boot is not None and expected.boot is not None \
                and self.boot != expected.boot:
            return self.GONE
        if self.start is not None and expected.start is not None:
            return self.SAME if self.start == expected.start else self.GONE
        # Without a token to compare, a portable liveness probe is all
        # that is left, and it never reports a pid it cannot prove gone.
        if not pi_platform.ProcessControl().pid_alive(self.pid):
            return self.GONE
        return self.UNKNOWN
