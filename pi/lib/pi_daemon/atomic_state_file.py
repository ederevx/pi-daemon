"""Atomic persistence of exactly one file, with its mode at creation."""

import json
import os
import time
import uuid


class AtomicStateFile:
    """A replacement is either fully visible or not at all."""

    def __init__(self, path, mode=0o600, retries=5, retry_delay=0.05):
        self._path = os.path.abspath(path)
        self._mode = mode
        self._retries = max(1, int(retries))
        self._retry_delay = retry_delay

    def path(self):
        return self._path

    def read(self):
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def read_dict(self):
        text = self.read()
        if not text:
            return None
        return json.loads(text)

    def write(self, text):
        directory = os.path.dirname(self._path)
        os.makedirs(directory, exist_ok=True)
        temporary = f"{self._path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, self._mode
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            self._replace(temporary)
        except BaseException:
            self._discard(temporary)
            raise

    def write_dict(self, value):
        self.write(json.dumps(value, sort_keys=True))

    def _replace(self, temporary):
        failure = None
        for _ in range(self._retries):
            try:
                os.replace(temporary, self._path)
                return
            except PermissionError as exc:
                failure = exc
                time.sleep(self._retry_delay)
            except FileNotFoundError as exc:
                failure = exc
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
        raise failure

    def _discard(self, temporary):
        try:
            os.unlink(temporary)
        except OSError:
            pass
