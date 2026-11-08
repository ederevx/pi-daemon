"""A pi-rpc/1 client for one published endpoint (docs/pi-services.md)."""

import json
import socket
import uuid

import pi_platform

VERSION = 1
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
UNAVAILABLE = -32003
TIMEOUT = -32004


class Unreachable(Exception):
    """The service is not there: nothing was sent."""


class RpcFailure(Exception):
    """The service answered with a JSON-RPC error."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class Ambiguous(Exception):
    """A request was written and its answer never arrived."""


class RpcClient:
    """One connection, one identity, requests answered by their own id."""

    def __init__(self, endpoint_path, timeout=10.0, connect_timeout=2.0):
        self._endpoint_path = endpoint_path
        self._timeout = float(timeout)
        self._connect_timeout = float(connect_timeout)
        self._socket = None
        self._reader = None
        self._token = None
        self._agent = None
        self._max_frame = 65536

    def connect(self):
        data = self._coordinates()
        try:
            connection = socket.create_connection(
                (data["host"], data["port"]), self._connect_timeout)
        except OSError as exc:
            raise Unreachable(str(exc)) from None
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._socket = connection
        self._reader = connection.makefile("rb")
        self._token = data["token"]
        return self

    def _coordinates(self):
        try:
            data = pi_platform.EndpointFile(self._endpoint_path).read()
        except (OSError, ValueError):
            raise Unreachable("the endpoint file is unreadable") from None
        if not isinstance(data, dict):
            raise Unreachable("the endpoint file is missing or empty")
        host, port, token = data.get("host"), data.get("port"), data.get("token")
        if not isinstance(host, str) or not host:
            raise Unreachable("the endpoint file publishes no host")
        if isinstance(port, bool) or not isinstance(port, int) or port <= 0:
            raise Unreachable("the endpoint file publishes no port")
        if not isinstance(token, str) or not token:
            raise Unreachable("the endpoint file publishes no token")
        return data

    def hello(self, agent_id):
        """Bind the connection to one identity; the greeting is adopted."""
        self._agent = {"agentId": str(agent_id)}
        request_id = uuid.uuid4().hex
        self._write({"jsonrpc": "2.0", "id": request_id,
                     "method": "rpc.hello",
                     "params": {"token": self._token, "version": VERSION,
                                "agent": self._agent}})
        greeting = self._await(request_id, self._timeout)["result"]
        frame = greeting.get("maxFrameBytes")
        if isinstance(frame, int) and not isinstance(frame, bool) and \
                frame > 0:
            self._max_frame = frame
        return greeting

    def call(self, method, params, deadline=None, mutating=False):
        """One request; the deadline bounds the wait for its answer."""
        request_id = uuid.uuid4().hex
        wait = self._timeout if deadline is None else float(deadline)
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method,
                     "params": params})
        try:
            reply = self._await(request_id, wait)
        except socket.timeout:
            self._cancel(request_id)
            if mutating:
                raise Ambiguous("the answer did not arrive") from None
            raise RpcFailure(TIMEOUT, "the answer did not arrive") from None
        except (ConnectionError, OSError) as exc:
            if mutating:
                raise Ambiguous(str(exc)) from None
            raise RpcFailure(UNAVAILABLE, "the connection was lost") \
                from None
        return reply["result"]

    def close(self):
        for handle in (self._reader, self._socket):
            try:
                handle.close()
            except (AttributeError, OSError):
                pass
        self._reader = None
        self._socket = None

    # -- internals -----------------------------------------------------------

    def _write(self, message):
        line = json.dumps(message, separators=(",", ":")) + "\n"
        try:
            self._socket.sendall(line.encode("utf-8"))
        except (AttributeError, OSError) as exc:
            raise ConnectionError(str(exc)) from None

    def _cancel(self, request_id):
        try:
            self._write({"jsonrpc": "2.0", "method": "rpc.cancel",
                         "params": {"id": request_id}})
        except ConnectionError:
            pass

    def _await(self, request_id, timeout):
        self._socket.settimeout(max(0.001, float(timeout)))
        while True:
            line = self._reader.readline()
            if not line:
                raise ConnectionError("the service closed the connection")
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise RpcFailure(PARSE_ERROR, "the answer is not one frame") \
                    from None
            if not isinstance(message, dict):
                raise RpcFailure(INVALID_REQUEST, "the answer is not an object")
            if message.get("id") != request_id:
                continue
            error = message.get("error")
            if isinstance(error, dict):
                raise RpcFailure(error.get("code"), error.get("message"))
            if "result" not in message:
                raise RpcFailure(INVALID_REQUEST, "the answer carries no result")
            return message
