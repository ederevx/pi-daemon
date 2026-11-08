"""The one failure a translation raises, carrying its exit code."""

UNREADABLE = 1
SOURCES_MOVED = 4


class MigrationError(Exception):
    """A refused input: its code is the exit status the command returns."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
