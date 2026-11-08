"""Bounded JSON-line framing that never loses bytes between frames."""

import json
import socket


class JsonLineStream:
    """One connection's receive buffer and its frame limit."""

    def __init__(self, connection, max_frame_bytes, read_chunk_bytes):
        self._connection = connection
        self._max = max_frame_bytes
        self._chunk = read_chunk_bytes
        self._buffer = b""

    def read(self):
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                frame = self._buffer[:newline]
                self._buffer = self._buffer[newline + 1:]
                if len(frame) > self._max:
                    raise ValueError("frame too large")
                return frame
            if len(self._buffer) > self._max:
                raise ValueError("frame too large")
            chunk = self._connection.recv(self._chunk)
            if not chunk:
                if not self._buffer.strip():
                    raise ConnectionError("connection closed")
                frame, self._buffer = self._buffer, b""
                return frame
            self._buffer += chunk

    def write(self, message):
        self._connection.sendall(json.dumps(message).encode("utf-8") + b"\n")

    def close(self):
        try:
            self._connection.close()
        except OSError:
            pass
