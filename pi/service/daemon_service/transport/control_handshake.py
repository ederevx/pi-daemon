"""The pi-daemon/1 authentication step of one accepted connection."""

import hmac

from ..wire import BAD_HANDSHAKE
from ..wire import WireCodec


class ControlHandshake:
    """Authorization and the greeting are decided once per connection.

    The greeting advertises only capabilities this build really serves:
    the command table it dispatches and the transcript index it keeps.
    """

    def __init__(self, token, commands, capabilities=()):
        self._token = token
        self._commands = sorted(commands)
        self._capabilities = list(capabilities)

    def authorize(self, request):
        if request.get("cmd") != "hello":
            raise PermissionError(BAD_HANDSHAKE)
        supplied = str(request.get("token") or "")
        if not hmac.compare_digest(supplied, self._token):
            raise PermissionError(BAD_HANDSHAKE)
        return {
            "ok": True,
            "srv": WireCodec.SERVER,
            "proto": WireCodec.PROTOCOL,
            "caps": list(self._capabilities),
            "index": WireCodec.INDEX,
            "commands": list(self._commands),
        }
