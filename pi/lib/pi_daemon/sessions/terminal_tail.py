"""The last bytes one run wrote, addressed by absolute offset."""

import threading


class TerminalTail:
    """A bounded ring of one run's output; readers name an offset.

    The tail belongs to the run, not to a viewer: it fills whether or not
    anyone watches, and a reader that falls behind is told how much it
    skipped instead of being dropped. `generation` names the run the bytes
    came from, so a reader can tell a restart from a gap.
    """

    def __init__(self, generation, limit):
        self.generation = generation
        self._limit = int(limit)
        self._data = bytearray()
        self._end = 0
        self._ended = None
        self._lock = threading.Lock()

    def append(self, data):
        with self._lock:
            self._data += data
            self._end += len(data)
            excess = len(self._data) - self._limit
            if excess > 0:
                del self._data[:excess]

    def end(self, reason):
        """Mark the run over; the bytes it wrote stay readable."""
        with self._lock:
            self._ended = reason

    def read(self, offset, limit):
        """From `offset`, or from the oldest byte still held."""
        with self._lock:
            start = self._end - len(self._data)
            begin = min(max(int(offset), start), self._end)
            end = min(begin + int(limit), self._end)
            return {"offset": begin,
                    "data": bytes(self._data[begin - start:end - start]),
                    "skipped": max(0, start - int(offset)),
                    "end": self._end,
                    "ended": self._ended}
