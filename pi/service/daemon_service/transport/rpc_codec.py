"""JSON-RPC 2.0 framing and the error vocabulary of pi-rpc/1."""

import json

INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHORIZED = -32001
CAPACITY = -32002
UNAVAILABLE = -32003
TIMEOUT = -32004
CANCELLED = -32800


class RpcCodec:
    """One owner of the wire shape: decode a line, build a message."""

    PROTOCOL = "pi-rpc/1"

    def decode(self, line):
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("malformed frame") from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise ValueError("malformed frame")
        return message

    def encode(self, message):
        return (json.dumps(message, separators=(",", ":")) + "\n").encode()

    def result(self, request_id, value):
        return {"jsonrpc": "2.0", "id": request_id, "result": value}

    def failure(self, request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": int(code), "message": str(message)}}

    def notification(self, method, params):
        return {"jsonrpc": "2.0", "method": method, "params": params}
