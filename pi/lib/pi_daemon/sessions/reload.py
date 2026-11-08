"""The one-shot reload signal a hosted session's extension consumes."""

import json
import os
import time

SIGNAL_DIR = "extensions-reload"


class SessionReload:
    """One round of requests under the state root, nothing durable.

    Each request is its own file, `<name>/<round>.json`: the filename
    carries the round, so two rounds can never overwrite each other's
    request and a delayed consumer can never delete a later one. The
    file is written whole (a reader never sees half a request), and its
    deletion - by the extension, or by the round that gives up on it -
    is the only acknowledgement: whoever removes it first owns the
    action.
    """

    def __init__(self, state_root, grace, poll, clock=time.monotonic,
                 sleep=time.sleep):
        self._directory = os.path.join(state_root, SIGNAL_DIR)
        self._grace = float(grace)
        self._poll = float(poll)
        self._clock = clock
        self._sleep = sleep

    def token(self):
        """A round name: unique per call, so no earlier round can re-fire."""
        return "%s-%d" % (os.urandom(8).hex(), time.time_ns())

    def directory(self, name):
        return os.path.join(self._directory, name)

    def path(self, name, token):
        return os.path.join(self.directory(name), "%s.json" % token)

    def signal(self, name, token, run, generation):
        """Leave one request for a run; False when it cannot be written.

        The request names the run it is for, not only the session: a
        successor that takes the name must never answer the previous
        run's reload, so the extension compares both before it acts.

        A name with a separator is refused here rather than by the
        filesystem: the signal directory must never escape the state root.
        """
        if not name or name in (".", "..") or os.sep in name \
                or (os.altsep and os.altsep in name):
            return False
        path = self.path(name, token)
        temporary = path + ".tmp"
        try:
            os.makedirs(self.directory(name), exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump({"token": token, "run": run,
                           "generation": generation,
                           "written": time.time()}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            self._discard(temporary)
            return False
        return True

    def consumed(self, name, token):
        """The extension deletes its file once it queued pi's own reload.

        Only a proven absence counts: an unreadable directory is not an
        acknowledgement, so the round keeps waiting and its failure is
        reported when the round claims it.
        """
        try:
            os.stat(self.path(name, token))
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    def claim(self, name, token):
        """Take a request back before the caller falls back to typing.

        True only when this call removed the round's own file. False means
        the extension removed it first, so the reload it queued is the
        acknowledgement; the two are exclusive because each round has its
        own pathname. A removal that fails for another reason is raised,
        never reported as a claim.
        """
        try:
            os.remove(self.path(name, token))
        except FileNotFoundError:
            return False
        return True

    def purge(self):
        """Drop what a previous run left: a stale request must never fire.

        A failure to remove one is raised, not swallowed: the caller is
        starting a daemon that promised no stale request can fire.
        """
        for entry in self._entries(self._directory):
            path = os.path.join(self._directory, entry)
            if not os.path.isdir(path):
                self._remove(path)
                continue
            for request in self._entries(path):
                self._remove(os.path.join(path, request))

    def await_consumed(self, names, tokens):
        """Wait out the grace once, then answer who consumed their signal."""
        pending = [name for name in names]
        deadline = self._clock() + self._grace
        while pending:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            self._sleep(min(self._poll, remaining))
            pending = [name for name in pending
                       if not self.consumed(name, tokens[name])]
        return [name for name in names
                if self.consumed(name, tokens[name])]

    def _entries(self, path):
        try:
            return os.listdir(path)
        except FileNotFoundError:
            return []
        except OSError:
            raise

    def _remove(self, path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def _discard(self, path):
        try:
            os.remove(path)
        except OSError:
            pass
