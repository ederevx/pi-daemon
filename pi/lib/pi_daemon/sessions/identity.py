"""Process identity: a PID is only evidence with its boot and start tick."""

import os


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
        return cls(pid, _boot_id(), _start_tick(pid))

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
        if not _alive(self.pid):
            return self.GONE
        return self.UNKNOWN


def _alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _boot_id():
    """The kernel's boot identity, the one host fact a record can trust."""
    try:
        with open("/proc/sys/kernel/random/boot_id", "r",
                  encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def _start_tick(pid):
    """Field 22 of /proc/<pid>/stat, parsed after the final ')'."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    closing = text.rfind(")")
    if closing < 0:
        return None
    fields = text[closing + 2:].split()
    return fields[19] if len(fields) > 19 else None
