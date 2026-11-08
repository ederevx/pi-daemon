"""The control contract: names, framing, greeting and errors."""

import json

BAD_HANDSHAKE = "bad-handshake"
BAD_REQUEST = "bad-request"
NO_TICKET = "no-ticket"
TICKET_RUNNING = "running"
TOO_BUSY = "too-busy"
SPAWN_FAILED = "spawn-failed"
NO_SESSION = "no-session"
NO_DIR = "no-dir"
NO_FILE = "no-file"
NAME_TAKEN = "name-taken"
WRITE_FAILED = "write-failed"
NO_TRANSCRIPT = "no-such-transcript"
BAD_TRANSCRIPT = "bad-transcript"
NO_INDEX = "no-index"
BAD_CURSOR = "bad-cursor"

# Capabilities the greeting names. A client that sees transcript.v1 knows
# the transcript commands answer, so it needs no probe request.
CAPABILITIES = ("transcript.v1",)


def error_reply(code):
    """The one shape of every failure reply."""
    return {"ok": False, "error": code}


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


