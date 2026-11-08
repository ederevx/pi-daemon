"""The outbound half of one peer link: connect, greet, request, close.

It owns one socket and the frames on it. Whether a delivery succeeded is the
remote daemon's answer, never this class's judgement.
"""

import socket
import threading
import time

from ..transport.json_line_stream import JsonLineStream
from ..transport.rpc_codec import RpcCodec
from ..transport.rpc_error import RpcError
from .peer_link_error import PeerLinkError

HELLO = "rpc.hello"
SERVICE = "agent-comms"
VERSION = 1
REQUIRED = ("comms.peer.deliver",)


class PeerLink:
    """One direction, one outstanding request at a time."""

    def __init__(self, config, node_id, codec=None, clock=None):
        self._config = config
        self._node_id = node_id
        self._codec = codec or RpcCodec()
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._socket = None
        self._stream = None
        self._request_id = 0
        self._greeting = None

    def open(self, address, peer, credential, timeout=None):
        """Connect, authenticate as this node, and require the peer's node."""
        with self._lock:
            self._close_locked()
            deadline = timeout or self._config.comms_peer_request_timeout
            try:
                connection = socket.create_connection(
                    address, timeout=self._config.connect_timeout)
                connection.settimeout(deadline)
            except OSError as failure:
                raise PeerLinkError(f"connect-failed: {failure}") from None
            stream = JsonLineStream(connection,
                                    self._config.frame_bytes,
                                    self._config.read_chunk_bytes)
            hello = {"jsonrpc": "2.0", "id": "hello", "method": HELLO,
                     "params": {"version": VERSION,
                                "peer": {"nodeId": self._node_id,
                                         "credential": credential}}}
            try:
                stream.write(self._codec.encode(hello))
                greeting = self._result(
                    self._codec.decode(stream.read()), "hello")
            except (OSError, ValueError, PeerLinkError) as failure:
                stream.close()
                raise PeerLinkError(f"hello-failed: {failure}") from None
            try:
                self._validate(greeting, peer)
            except PeerLinkError:
                stream.close()
                raise
            self._socket, self._stream = connection, stream
            self._greeting = greeting
            return greeting

    def request(self, method, params, timeout=None):
        """One request, one matching reply; anything else is a dead link."""
        with self._lock:
            stream = self._stream
            if stream is None:
                raise PeerLinkError("link-down")
            self._request_id += 1
            request_id = f"p{self._request_id}"
            frame = self._codec.encode({"jsonrpc": "2.0", "id": request_id,
                                        "method": method, "params": params})
            if len(frame) > self._config.frame_bytes:
                raise PeerLinkError("frame-too-large")
            try:
                if timeout is not None:
                    self._socket.settimeout(timeout)
                stream.write(frame)
                message = self._codec.decode(stream.read())
            except socket.timeout:
                self._close_locked()
                raise PeerLinkError("request-timeout") from None
            except (OSError, ValueError) as failure:
                self._close_locked()
                raise PeerLinkError(f"link-broken: {failure}") from None
            if message.get("id") != request_id:
                self._close_locked()
                raise PeerLinkError("mismatched-reply")
            error = message.get("error")
            if isinstance(error, dict):
                raise RpcError(int(error.get("code") or 0),
                               str(error.get("message") or "refused"))
            return message.get("result")

    def alive(self):
        return self._stream is not None

    def close(self):
        with self._lock:
            self._close_locked()

    def _close_locked(self):
        stream, self._stream = self._stream, None
        connection, self._socket = self._socket, None
        if stream is not None:
            stream.close()
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        self._greeting = None

    def _result(self, message, request_id):
        if message.get("id") != request_id:
            raise PeerLinkError("mismatched-greeting")
        error = message.get("error")
        if isinstance(error, dict):
            raise PeerLinkError(str(error.get("message") or "refused"))
        result = message.get("result")
        if not isinstance(result, dict):
            raise PeerLinkError("empty-greeting")
        return result

    def _validate(self, greeting, peer):
        if greeting.get("protocol") != RpcCodec.PROTOCOL or \
                greeting.get("service") != SERVICE or \
                greeting.get("version") != VERSION:
            raise PeerLinkError("wrong-protocol")
        if greeting.get("nodeId") != peer["nodeId"]:
            raise PeerLinkError("wrong-node")
        methods = greeting.get("methods")
        if not isinstance(methods, list) or \
                any(method not in methods for method in REQUIRED):
            raise PeerLinkError("missing-method")
        frame = greeting.get("maxFrameBytes")
        if isinstance(frame, bool) or not isinstance(frame, int) or \
                frame > self._config.frame_bytes:
            raise PeerLinkError("frame-limit")
        server_time = greeting.get("serverTime")
        if isinstance(server_time, (int, float)) and \
                not isinstance(server_time, bool):
            if abs(float(server_time) - self._clock()) > \
                    self._config.comms_peer_clock_skew:
                raise PeerLinkError("clock-skew")
