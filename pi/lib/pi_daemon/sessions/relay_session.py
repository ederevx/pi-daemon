"""One relayed conversation: its child's PTY and its attached viewers."""

import threading


class RelaySession:
    """Buffers belong to viewers; the child owns the terminal."""

    def __init__(self, key, generation, child, limit):
        self.key = key
        self.generation = generation
        self.child = child
        self.limit = int(limit)
        self.viewers = {}
        self.pending = {}
        self.inbox = bytearray()
        self.backlog = bytearray()
        self.closing = False
        self.deadline = None
        self.eof = False
        self.done = threading.Event()

    def viewer_count(self):
        return len(self.viewers)

    def add_viewer(self, viewer_id, connection, initial):
        """A new viewer first receives what it missed while nobody watched."""
        self.viewers[viewer_id] = connection
        self.pending[viewer_id] = bytearray(self.backlog)
        self.backlog.clear()
        if initial:
            self.inbox += initial

    def drop_viewer(self, viewer_id):
        connection = self.viewers.pop(viewer_id, None)
        self.pending.pop(viewer_id, None)
        if connection is not None:
            _close(connection)

    def broadcast(self, data):
        """Fan out one chunk, dropping viewers that cannot keep up."""
        if not self.viewers:
            self.backlog += data
            if len(self.backlog) > self.limit:
                del self.backlog[:len(self.backlog) - self.limit]
            return []
        dropped = []
        for viewer_id in list(self.viewers):
            pending = self.pending[viewer_id]
            pending += data
            if len(pending) > self.limit:
                dropped.append(viewer_id)
        for viewer_id in dropped:
            self.drop_viewer(viewer_id)
        return dropped

    def feed(self, data):
        """Queue bytes for the PTY; False when no child can take them."""
        if self.closing:
            return False
        self.inbox += data
        if len(self.inbox) > self.limit:
            excess = len(self.inbox) - self.limit
            del self.inbox[:excess]
        return True


def _close(connection):
    try:
        connection.close()
    except OSError:
        pass
