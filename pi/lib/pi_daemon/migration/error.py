"""The exit statuses a translation returns, and the failure it raises."""

UNREADABLE = 1
LOCKED = 3
SOURCES_MOVED = 4
UNMARKED = 5


class MigrationError(Exception):
    """A refused input: its code is the exit status the command returns."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
