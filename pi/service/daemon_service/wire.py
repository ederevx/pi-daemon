"""The control contract: pi-pty-host/1 framing, greeting and errors."""

import hmac
import json


class WireCodec:
    """One owner of the published protocol and its vocabulary."""

    SERVER = "pi-pty-host"
    PROTOCOL = 1

    def __init__(self, token, commands):
        self._token = token
        self._commands = sorted(commands)

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

    def greet(self, request):
        """The existing handshake: op hello, then the shared token."""
        if request.get("op") != "hello":
            raise PermissionError("a hello is required first")
        supplied = str(request.get("token") or "")
        if not hmac.compare_digest(supplied, self._token):
            raise PermissionError("the token is not accepted")
        return {
            "ok": True,
            "srv": self.SERVER,
            "proto": self.PROTOCOL,
            "commands": list(self._commands),
        }

    def error(self, message):
        return {"ok": False, "error": message}
