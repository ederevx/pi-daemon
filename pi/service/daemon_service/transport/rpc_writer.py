"""One connection's outbound frames: a bounded queue and its writer.

Replies and notifications share this queue so their order is the order the
provider decided, and a peer that stops reading cannot stall a sender: the
queue bound ends the connection instead.
"""

import socket
import threading

from collections import deque

from .rpc_codec import CAPACITY
from .rpc_error import RpcError


class RpcWriter:
    """The only place that writes to one socket, from its own thread."""

    def __init__(self, connection, config, codec):
        self._connection = connection
        self._codec = codec
        self._max_frame = config.frame_bytes
        self._capacity = config.comms_outbound_queue
        self._grace = config.stop_grace
        self._queue = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="rpc-writer",
                                        daemon=True)
        self._thread.start()

    def send(self, message):
        """Offer one frame; a full queue ends the connection."""
        frame = self._frame(message)
        with self._condition:
            if self._closed:
                raise OSError("connection closed")
            if len(self._queue) >= self._capacity:
                self._closed = True
                self._queue.clear()
                self._condition.notify_all()
                overflowed = True
            else:
                self._queue.append(frame)
                self._condition.notify()
                overflowed = False
        if overflowed:
            self._interrupt()
            raise OSError("outbound queue full")

    def close(self, wait=True):
        """Stop accepting frames; the writer sends what is already queued."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if not wait or self._thread is threading.current_thread():
            self._interrupt()
            return
        self._thread.join(self._grace)
        self._interrupt()

    def _frame(self, message):
        frame = self._codec.encode(message)
        if len(frame) > self._max_frame:
            raise RpcError(CAPACITY, "the reply exceeds the frame limit")
        return frame

    def _run(self):
        while True:
            with self._condition:
                while not self._queue and not self._closed:
                    self._condition.wait()
                if not self._queue:
                    break
                frame = self._queue.popleft()
            try:
                self._connection.sendall(frame)
            except OSError:
                self.close(wait=False)
                return
        self._interrupt()

    def _interrupt(self):
        """Wake a sendall that a peer will never satisfy."""
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._connection.close()
        except OSError:
            pass
