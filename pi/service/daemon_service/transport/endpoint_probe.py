"""Readiness probing of a published endpoint, over the real handshake."""

import socket

from .json_line_stream import JsonLineStream


class EndpointProbe:
    """A probe establishes readiness; it never establishes ownership."""

    def __init__(self, codec, connect_timeout, probe_timeout, frame_bytes,
                 read_chunk_bytes):
        self._codec = codec
        self._connect_timeout = connect_timeout
        self._probe_timeout = probe_timeout
        self._frame_bytes = frame_bytes
        self._chunk = read_chunk_bytes

    def probe(self, coordinates):
        if not coordinates:
            return None
        try:
            connection = socket.create_connection(
                (coordinates["host"], coordinates["port"]),
                timeout=self._connect_timeout,
            )
        except (OSError, KeyError):
            return None
        connection.settimeout(self._probe_timeout)
        stream = JsonLineStream(connection, self._frame_bytes, self._chunk)
        try:
            stream.write(self._codec.encode(
                self._codec.hello_request(coordinates.get("token", ""))))
            return self._codec.decode(stream.read())
        except (OSError, ValueError, ConnectionError):
            return None
        finally:
            stream.close()
