"""The single relay thread: select, pump, and never block on one peer."""

import os
import select
import threading
import time
import uuid

from .relay_session import RelaySession

# A carried viewer repaints from a cleared screen: the old conversation's
# queued output is dropped and the clear sequence leads the new one.
CLEAR_SEQ = b"\x1b[H\x1b[2J\x1b[m"


class SessionRelay:
    """All PTY traffic crosses one thread; every request is a ticket."""

    def __init__(self, config, backend=None):
        self._config = config
        self._backend = backend
        self._lock = threading.Lock()
        self._commands = []
        self._wake_read = -1
        self._wake_write = -1
        self._sessions = {}
        # A nudged size still owes its restore: keyed by session, so a
        # session that ends drops the debt with it.
        self._repaints = {}
        self._thread = None
        self._stopping = threading.Event()

    # -- public surface (called from request and watcher threads) ----------

    def start(self):
        self._wake_read, self._wake_write = os.pipe()
        os.set_blocking(self._wake_read, False)
        os.set_blocking(self._wake_write, False)
        self._thread = threading.Thread(target=self._loop, name="session-relay",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping.set()
        self._wake()
        if self._thread is not None:
            self._thread.join(timeout=self._config.cancel_grace + 1.0)
        self._close_wake()

    def register(self, key, generation, master):
        self._call(lambda: self._register(key, generation, master))

    def attach(self, key, generation, connection, initial=b""):
        viewer_id = uuid.uuid4().hex
        connection.setblocking(False)
        self._call(lambda: self._attach(key, generation, viewer_id,
                                        connection, initial))
        return viewer_id

    def detach(self, key, viewer_id):
        self._call(lambda: self._detach(key, viewer_id))

    def input(self, key, data):
        self._call(lambda: self._input(key, data))

    def resize(self, key, cols, rows):
        self._call(lambda: self._resize(key, cols, rows))

    def finish(self, key, reason="exit"):
        """Drain what the child wrote, then release its viewers."""
        session = self._call(lambda: self._begin_finish(key, reason))
        if session is not None:
            session.done.wait(timeout=self._config.settle_grace + 2.0)

    def viewer_count(self, key):
        return int(self._call(lambda: self._count(key)) or 0)

    def close(self, key):
        self._call(lambda: self._complete(key))

    def drop_viewers(self, key):
        self._call(lambda: self._drop_viewers(key))

    def move_viewers(self, source_key, target_key):
        return self._call(
            lambda: self._move_viewers(source_key, target_key))

    def carry_viewers(self, source_key, source_generation, target_key,
                      target_generation, cols, rows):
        """Move every viewer to another conversation, repainting it.

        Both generations are checked on the relay thread, so a name
        reused under the same key is never carried into.
        """
        return self._call(
            lambda: self._carry_viewers(source_key, source_generation,
                                        target_key, target_generation,
                                        cols, rows))

    # -- tickets -----------------------------------------------------------

    def _call(self, operation, timeout=5.0):
        if self._stopping.is_set():
            return None
        ticket = [None, None, threading.Event()]
        with self._lock:
            self._commands.append((ticket, operation))
        self._wake()
        if not ticket[2].wait(timeout):
            raise TimeoutError("the relay did not answer")
        result, failure = ticket[0], ticket[1]
        if failure is not None:
            raise failure
        return result

    def _wake(self):
        if self._wake_write < 0:
            return
        try:
            os.write(self._wake_write, b"x")
        except OSError:
            pass

    def _drain(self):
        with self._lock:
            commands, self._commands = self._commands, []
        for ticket, operation in commands:
            self._run(ticket, operation)

    def _run(self, ticket, operation):
        try:
            ticket[0] = operation()
        except Exception as failure:
            ticket[1] = failure
        ticket[2].set()

    # -- commands (run on the relay thread) --------------------------------

    def _register(self, key, generation, master):
        self._sessions[key] = RelaySession(key, generation, master,
                                          self._config.relay_buffer_bytes)
        return True

    def _attach(self, key, generation, viewer_id, connection, initial):
        session = self._sessions.get(key)
        if session is None or session.generation != generation \
                or session.closing:
            _close(connection)
            return False
        session.add_viewer(viewer_id, connection, initial)
        return True

    def _detach(self, key, viewer_id):
        session = self._sessions.get(key)
        if session is not None:
            session.drop_viewer(viewer_id)
        return True

    def _input(self, key, data):
        session = self._sessions.get(key)
        if session is not None:
            session.feed(data)
        return True

    def _resize(self, key, cols, rows):
        session = self._sessions.get(key)
        if session is not None and self._backend is not None:
            # A resize settles the size the client wants: a nudge still
            # owing its restore would put back the size it stepped from.
            self._repaints.pop(key, None)
            self._backend.resize(session.master, cols, rows)
        return True

    def _begin_finish(self, key, reason):
        session = self._sessions.get(key)
        if session is None:
            return None
        session.closing = True
        session.deadline = time.monotonic() + self._config.settle_grace
        return session

    def _count(self, key):
        session = self._sessions.get(key)
        return session.viewer_count() if session is not None else 0

    def _drop_viewers(self, key):
        session = self._sessions.get(key)
        if session is None:
            return False
        for viewer_id in list(session.viewers):
            session.drop_viewer(viewer_id)
        return True

    def _move_viewers(self, source_key, target_key):
        source = self._sessions.get(source_key)
        target = self._sessions.get(target_key)
        if source is None or target is None or target.closing:
            return 0
        moved = 0
        for viewer_id in list(source.viewers):
            connection = source.viewers.pop(viewer_id)
            pending = source.pending.pop(viewer_id)
            if not target.viewers:
                # The target's own backlog belongs in front of what moves.
                pending = target.backlog + pending
                target.backlog.clear()
            target.viewers[viewer_id] = connection
            target.pending[viewer_id] = pending
            moved += 1
        return moved

    def _carry_viewers(self, source_key, source_generation, target_key,
                       target_generation, cols, rows):
        source = self._sessions.get(source_key)
        target = self._sessions.get(target_key)
        if source is None or target is None or target.closing:
            return None
        if source.generation != source_generation \
                or target.generation != target_generation:
            return None
        moved = 0
        for viewer_id in list(source.viewers):
            connection = source.viewers.pop(viewer_id)
            source.pending.pop(viewer_id, None)
            target.viewers[viewer_id] = connection
            target.pending[viewer_id] = bytearray(CLEAR_SEQ)
            moved += 1
        if moved and self._backend is not None:
            # pi redraws only on a real size change, so the carried
            # viewer would otherwise stare at the cleared screen until
            # the target happens to print. The size is stepped here and
            # restored from the loop, which owns the pause: this thread
            # serves every conversation and must not sleep.
            if self._backend.nudge(target.master, cols, rows) is not None:
                self._repaints[target_key] = (
                    target.generation, cols, rows,
                    time.monotonic() + self._config.repaint_tick)
        return {"moved": moved}

    def _restore_repaints(self):
        """Give back every size a nudge stepped away from, once the child
        has had time to see the change."""
        now = time.monotonic()
        for key, (generation, cols, rows, deadline) in list(
                self._repaints.items()):
            if deadline > now:
                continue
            del self._repaints[key]
            session = self._sessions.get(key)
            if session is None or session.generation != generation:
                # The run that was nudged has already gone.
                continue
            self._backend.resize(session.master, cols, rows)

    def _timeout(self):
        """The nearest repaint restore bounds the wait, so it is never
        late by a whole tick."""
        timeout = self._config.relay_tick
        if self._repaints:
            soonest = min(deadline for _, _, _, deadline
                          in self._repaints.values())
            timeout = min(timeout, max(0.0, soonest - time.monotonic()))
        return timeout

    def _complete(self, key):
        self._repaints.pop(key, None)
        session = self._sessions.pop(key, None)
        if session is None:
            return False
        for viewer_id in list(session.viewers):
            session.drop_viewer(viewer_id)
        _close_master(session.master)
        session.done.set()
        return True

    # -- the loop ----------------------------------------------------------

    def _loop(self):
        while not self._stopping.is_set():
            self._drain()
            self._pump()
        for key in list(self._sessions):
            self._complete(key)

    def _pump(self):
        self._restore_repaints()
        readable = [self._wake_read]
        writable = []
        watched = list(self._sessions.values())
        for session in watched:
            if not session.eof:
                readable.append(session.master)
            if session.inbox:
                writable.append(session.master)
            for viewer_id, connection in session.viewers.items():
                readable.append(connection)
                if session.pending.get(viewer_id):
                    writable.append(connection)
        try:
            ready_read, ready_write, _ = select.select(readable, writable, [],
                                                       self._timeout())
        except (OSError, ValueError):
            # One closed descriptor must not stall every conversation.
            self._prune()
            time.sleep(self._config.relay_tick)
            return
        for source in ready_read:
            self._readable(source)
        for source in ready_write:
            self._writable(source)
        for session in watched:
            if not session.closing:
                continue
            drained = session.eof and not any(session.pending.values())
            if drained or time.monotonic() > session.deadline:
                self._complete(session.key)

    def _readable(self, source):
        if source == self._wake_read:
            try:
                os.read(self._wake_read, 4096)
            except OSError:
                pass
            return
        for session in list(self._sessions.values()):
            if source == session.master:
                self._from_master(session)
                return
            for viewer_id, connection in session.viewers.items():
                if source == connection:
                    self._from_viewer(session, viewer_id, connection)
                    return

    def _from_master(self, session):
        try:
            data = os.read(session.master, self._config.master_chunk_bytes)
        except OSError:
            data = b""
        if not data:
            session.eof = True
            if not session.closing:
                # The child stopped writing: drain what it wrote, then let
                # the viewers go.
                session.closing = True
                session.deadline = time.monotonic() + \
                    self._config.settle_grace
            return
        session.broadcast(data)
        for viewer_id in list(session.viewers):
            self._flush(session, viewer_id)

    def _from_viewer(self, session, viewer_id, connection):
        try:
            data = connection.recv(self._config.master_chunk_bytes)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if not data:
            session.drop_viewer(viewer_id)
            return
        session.feed(data)
        self._flush_input(session)

    def _writable(self, source):
        for session in list(self._sessions.values()):
            if source == session.master:
                self._flush_input(session)
                return
            for viewer_id, connection in session.viewers.items():
                if source == connection:
                    self._flush(session, viewer_id)
                    return

    def _flush_input(self, session):
        if not session.inbox:
            return
        try:
            written = os.write(session.master, session.inbox)
        except OSError:
            session.inbox.clear()
            return
        del session.inbox[:written]

    def _flush(self, session, viewer_id):
        pending = session.pending.get(viewer_id)
        connection = session.viewers.get(viewer_id)
        if not pending or connection is None:
            return
        try:
            sent = connection.send(pending)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            session.drop_viewer(viewer_id)
            return
        del pending[:sent]

    # -- helpers -----------------------------------------------------------

    def _prune(self):
        """Drop the conversations and viewers whose descriptor is gone."""
        for session in list(self._sessions.values()):
            if not _open_fd(session.master):
                self._complete(session.key)
                continue
            for viewer_id, connection in list(session.viewers.items()):
                if connection.fileno() < 0:
                    session.drop_viewer(viewer_id)

    def _close_wake(self):
        for descriptor in (self._wake_read, self._wake_write):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        self._wake_read = self._wake_write = -1


def _close(connection):
    try:
        connection.close()
    except OSError:
        pass


def _open_fd(descriptor):
    if descriptor is None or descriptor < 0:
        return False
    try:
        os.fstat(descriptor)
    except OSError:
        return False
    return True


def _close_master(master):
    try:
        os.close(master)
    except OSError:
        pass
