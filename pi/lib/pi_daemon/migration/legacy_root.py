"""The legacy daemon's state root: read once, hashed, never written."""

import hashlib
import json
import os

from .error import UNREADABLE, MigrationError


class LegacyRoot:
    """Every read is recorded by digest; nothing here opens for writing."""

    def __init__(self, root):
        self._root = os.path.abspath(root)
        self._digests = {}

    @classmethod
    def from_environment(cls, environ):
        state_home = environ.get("XDG_STATE_HOME") \
            or os.path.expanduser("~/.local/state")
        return cls(os.path.join(state_home, "pi-pty-host"))

    def root(self):
        return self._root

    def digests(self):
        return dict(self._digests)

    def exists(self, relative):
        return os.path.exists(os.path.join(self._root, relative))

    def names(self, relative, suffix):
        """Entries of one directory; a listing is not a read."""
        directory = os.path.join(self._root, relative)
        try:
            entries = sorted(os.listdir(directory))
        except (FileNotFoundError, NotADirectoryError):
            return []
        return [entry for entry in entries if entry.endswith(suffix)
                and os.path.isfile(os.path.join(directory, entry))]

    def read_bytes(self, relative):
        try:
            with open(os.path.join(self._root, relative), "rb") as handle:
                data = handle.read()
        except FileNotFoundError:
            return None
        self._digests[relative.replace(os.sep, "/")] = \
            hashlib.sha256(data).hexdigest()
        return data

    def read_object(self, relative):
        data = self.read_bytes(relative)
        if data is None:
            return None
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            value = None
        if not isinstance(value, dict):
            raise MigrationError(UNREADABLE, f"{relative}: unreadable")
        return value
