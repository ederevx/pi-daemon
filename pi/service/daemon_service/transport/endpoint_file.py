"""Publication of the live control coordinates and their token."""

import os

from ..atomic_state_file import AtomicStateFile


class EndpointFile:
    """Only the instance that published a descriptor may remove it."""

    def __init__(self, path, mode=0o600, retries=5, retry_delay=0.05):
        self._file = AtomicStateFile(path, mode, retries, retry_delay)

    def publish(self, coordinates):
        self._file.write_dict(coordinates)

    def read(self):
        try:
            return self._file.read_dict()
        except ValueError:
            return None

    def remove_owned(self, instance):
        current = self.read()
        if not current or current.get("instance") != instance:
            return False
        try:
            os.unlink(self._file.path())
        except OSError:
            return False
        return True
