"""The record of a completed translation: who read what, and when."""

from ..atomic_state_file import AtomicStateFile
from .error import UNREADABLE, MigrationError

VERSION = 1


class MigrationMarker:
    """`migration.json` names the legacy files a translation consumed."""

    def __init__(self, path, config):
        self._file = AtomicStateFile(path, config.file_mode,
                                     config.write_retries,
                                     config.write_retry_delay)

    def read(self):
        try:
            document = self._file.read_dict()
        except ValueError:
            raise MigrationError(UNREADABLE, "migration.json: unreadable") \
                from None
        if document is None:
            return None
        if not isinstance(document, dict) or document.get("version") != VERSION:
            raise MigrationError(UNREADABLE, "migration.json: unreadable")
        return document

    def record(self, legacy_root, sources, forced, now):
        self._file.write_dict({
            "version": VERSION,
            "legacy_root": legacy_root,
            "migrated": now,
            "forced": bool(forced),
            "sources": dict(sources),
        })
