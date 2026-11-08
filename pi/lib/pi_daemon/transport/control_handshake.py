"""The pi-daemon/1 authentication step of one accepted connection."""

import hmac

from ..wire import BAD_HANDSHAKE
from ..wire import WireCodec


class ControlHandshake:
    """Authorization and the greeting are decided once per connection.

    The greeting carries exactly the published keys; `caps` names only
    capabilities the composed router really serves, so a client can rely
    on one that it sees.
    """

    def __init__(self, token, capabilities=()):
        self._token = token
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
        }
