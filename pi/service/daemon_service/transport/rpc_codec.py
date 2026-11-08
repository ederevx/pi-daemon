"""JSON-RPC 2.0 framing and the error vocabulary of pi-rpc/1."""

import json

from .rpc_error import RpcError

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHORIZED = -32001
CAPACITY = -32002
UNAVAILABLE = -32003
TIMEOUT = -32004
CANCELLED = -32800


def _reject_constant(name):
    raise ValueError(f"{name} is not a JSON number")


class RpcCodec:
    """One owner of the wire shape: decode a line, build a message."""

    PROTOCOL = "pi-rpc/1"

    def decode(self, line):
        """A frame, or the refusal the profile names for the failure."""
        try:
            message = json.loads(line.decode("utf-8"),
                                 parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError):
            raise RpcError(PARSE_ERROR, "parse error") from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise RpcError(INVALID_REQUEST,
                           "one json-rpc request object is required")
        return message

    def request_id(self, message):
        """A caller-owned id, or None when the frame has no usable one."""
        value = message.get("id")
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        return value

    def encode(self, message):
        return (json.dumps(message, separators=(",", ":")) + "\n").encode()

    def result(self, request_id, value):
        return {"jsonrpc": "2.0", "id": request_id, "result": value}

    def failure(self, request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": int(code), "message": str(message)}}

    def notification(self, method, params):
        return {"jsonrpc": "2.0", "method": method, "params": params}
