"""The one-shot reload signal a hosted session's extension consumes."""

import json
import os
import time

SIGNAL_DIR = "extensions-reload"
# The extension sees the file on its own watcher's next event; a session
# that has not consumed it by then runs an extension without the watcher,
# and the caller types "/reload" into the PTY instead of losing the round.
GRACE = 2.0
POLL = 0.05


class SessionReload:
    """One round of requests under the state root, nothing durable.

    The file itself is the contract with the in-session extension: it is
    written whole (a reader never sees half a request), it carries a fresh
    token per round (a token already consumed can never fire twice), and
    its deletion by the extension is the only acknowledgement.
    """

    def __init__(self, state_root, grace=GRACE, poll=POLL,
                 clock=time.monotonic, sleep=time.sleep):
        self._directory = os.path.join(state_root, SIGNAL_DIR)
        self._grace = grace
        self._poll = poll
        self._clock = clock
        self._sleep = sleep

    def token(self):
        """A round token: unique per call, so no earlier round can re-fire."""
        return "%s-%d" % (os.urandom(8).hex(), time.time_ns())

    def path(self, name):
        return os.path.join(self._directory, "%s.json" % name)

    def signal(self, name, token):
        """Leave one request for a session; False when it cannot be written.

        A name with a separator is refused here rather than by the
        filesystem: the signal directory must never escape the state root.
        """
        if not name or name in (".", "..") or os.sep in name \
                or (os.altsep and os.altsep in name):
            return False
        path = self.path(name)
        temporary = path + ".tmp"
        try:
            os.makedirs(self._directory, exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"token": token, "written": time.time()}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            return False
        return True

    def consumed(self, name):
        """The extension deletes its file once it queued pi's own reload."""
        return not os.path.exists(self.path(name))

    def clear(self, name):
        try:
            os.remove(self.path(name))
        except OSError:
            pass

    def purge(self):
        """Drop what a previous run left: a stale request must never fire."""
        try:
            entries = os.listdir(self._directory)
        except OSError:
            return
        for entry in entries:
            if entry.endswith(".json"):
                self.clear(entry[:-len(".json")])

    def await_consumed(self, names):
        """Wait out the grace once, then answer who consumed their signal."""
        pending = list(names)
        deadline = self._clock() + self._grace
        while pending and self._clock() < deadline:
            self._sleep(self._poll)
            pending = [name for name in pending if not self.consumed(name)]
        return [name for name in names if self.consumed(name)]
