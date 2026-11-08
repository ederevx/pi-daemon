"""The one-shot reload signal a hosted session's extension consumes."""

import json
import os
import time

SIGNAL_DIR = "extensions-reload"


class SessionReload:
    """One round of requests under the state root, nothing durable.

    The file itself is the contract with the in-session extension: it is
    written whole (a reader never sees half a request), it carries a fresh
    token per round (a token already consumed can never fire twice, and a
    later round never inherits an earlier one), and its deletion - by the
    extension, or by the round that gives up on it - is the only
    acknowledgement.
    """

    def __init__(self, state_root, grace, poll, clock=time.monotonic,
                 sleep=time.sleep):
        self._directory = os.path.join(state_root, SIGNAL_DIR)
        self._grace = float(grace)
        self._poll = float(poll)
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
        The temporary file carries the token, so two rounds can never write
        over each other's half-written request.
        """
        if not name or name in (".", "..") or os.sep in name \
                or (os.altsep and os.altsep in name):
            return False
        path = self.path(name)
        temporary = "%s.%s.tmp" % (path, token)
        try:
            os.makedirs(self._directory, exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"token": token, "written": time.time()}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            self._remove(temporary)
            return False
        return True

    def consumed(self, name):
        """The extension deletes its file once it queued pi's own reload."""
        return not os.path.exists(self.path(name))

    def claim(self, name, token):
        """Take a request back before the caller falls back to typing.

        True only when the file still carried this round's token and this
        call removed it. False means the extension consumed the request
        after the grace expired (or in the instant before this call), so
        the reload it queued is the acknowledgement. A file carrying a
        later round's token is left alone: this round must never delete a
        request it did not write. A removal that fails for another reason
        is raised, never reported as a claim.
        """
        if self._read_token(name) != token:
            return False
        try:
            os.remove(self.path(name))
        except FileNotFoundError:
            return False
        return True

    def purge(self):
        """Drop what a previous run left: a stale request must never fire."""
        try:
            entries = os.listdir(self._directory)
        except OSError:
            return
        for entry in entries:
            if entry.endswith(".json"):
                self._remove(self.path(entry[:-len(".json")]))
            elif entry.endswith(".tmp"):
                self._remove(os.path.join(self._directory, entry))

    def await_consumed(self, names):
        """Wait out the grace once, then answer who consumed their signal."""
        pending = list(names)
        deadline = self._clock() + self._grace
        while pending and self._clock() < deadline:
            self._sleep(self._poll)
            pending = [name for name in pending if not self.consumed(name)]
        return [name for name in names if self.consumed(name)]

    def _read_token(self, name):
        try:
            with open(self.path(name), encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            return None
        token = record.get("token") if isinstance(record, dict) else None
        return token if isinstance(token, str) else None

    def _remove(self, path):
        try:
            os.remove(path)
        except OSError:
            pass
