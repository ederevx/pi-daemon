"""Service state: the one owner of every path the service writes.

The endpoint and state-directory names are the published contract a
client and a migration depend on, not an implementation detail: they
keep their historical values so a rebuild is a restart, not a move.
"""

import json
import os
import secrets
import sys

# Published contract: the control endpoint file and the state directory.
ENDPOINT_FILE = "pi-pty-host.sock"
STATE_DIR = "pi-pty-host"
SERVICE_FILE = "service.json"
ENDPOINT_FILE_NAME = "endpoint"
TICKETS_FILE = "tickets.json"
TICKET_LOG_DIR = "tickets"


class ServiceState:
    """Every path the service owns, resolved once per platform."""

    def __init__(self, environ=None, platform=None):
        self.environ = os.environ if environ is None else environ
        self.platform = sys.platform if platform is None else platform
        self.tmp_dir = self.environ.get("TMPDIR") or None

    def is_windows(self):
        return str(self.platform).startswith("win")

    def runtime_dir(self):
        explicit = self.environ.get("XDG_RUNTIME_DIR")
        if explicit:
            return explicit
        if self.is_windows():
            return self.environ.get("TEMP") or self.environ.get("TMP") \
                or os.path.expanduser("~")
        return "/run/user/%d" % (os.getuid() if hasattr(os, "getuid") else 0)

    def state_home(self):
        explicit = self.environ.get("XDG_STATE_HOME")
        if explicit:
            return explicit
        if self.is_windows():
            return self.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.expanduser("~/.local/state")

    def agent_home(self):
        explicit = self.environ.get("PI_CODING_AGENT_DIR")
        return explicit if explicit else os.path.expanduser("~/.pi/agent")

    def endpoint_path(self):
        return os.path.join(self.runtime_dir(), ENDPOINT_FILE)

    def state_dir(self):
        return os.path.join(self.state_home(), STATE_DIR)

    def service_file(self):
        return os.path.join(self.state_dir(), SERVICE_FILE)

    def tickets_path(self):
        return os.path.join(self.state_dir(), TICKETS_FILE)

    def ticket_log_dir(self):
        return os.path.join(self.state_dir(), TICKET_LOG_DIR)

    def ensure(self):
        for path in (self.runtime_dir(), self.state_dir(),
                     self.ticket_log_dir()):
            os.makedirs(path, exist_ok=True)

    def read_json(self, path, default=None):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return default
        return data if isinstance(data, dict) else default

    def write_json(self, path, data, mode=0o644):
        atomic_write(path, json.dumps(data, separators=(",", ":")), mode)

    def write_text(self, path, text):
        atomic_write(path, text)


def atomic_write(path, text, mode=0o644):
    """Replace a file in one step: a reader sees the old or the new
    content, never a partial write."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = "%s.tmp.%d.%s" % (path, os.getpid(), secrets.token_hex(4))
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass  # Windows chmod only toggles the read-only bit.
    for attempt in range(11):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt >= 10:
                break
    try:
        os.unlink(tmp)
    except OSError:
        pass
