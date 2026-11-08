"""The control contract: names, framing, greeting and errors."""

import json

BAD_HANDSHAKE = "bad-handshake"
BAD_REQUEST = "bad-request"
NO_TICKET = "no-ticket"
TICKET_RUNNING = "running"
TOO_BUSY = "too-busy"
SPAWN_FAILED = "spawn-failed"


class WireCodec:
    """One owner of the published protocol and its error vocabulary."""

    SERVER = "pi-daemon"
    PROTOCOL = 1
    INDEX = 1

    def decode(self, line):
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("malformed frame") from None
        if not isinstance(message, dict):
            raise ValueError("malformed frame")
        return message

    def encode(self, message):
        return (json.dumps(message) + "\n").encode("utf-8")

    def hello_request(self, token):
        return {"cmd": "hello", "token": token}

    def error(self, code):
        return {"ok": False, "error": code}
