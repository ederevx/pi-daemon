"""The command table: which owner answers a wire command, and the
identity a client sees after the handshake.
"""

from . import wire


class Router:
    """Owns the command table. A domain never learns another domain's
    command names; this table is the only place that knows them."""

    def __init__(self, service, version, commands):
        self.service = service
        self.version = version
        self.commands = dict(commands)

    def identity(self):
        return {"service": self.service, "version": self.version,
                "commands": sorted(self.commands)}

    def serve(self, request):
        command = request.get("cmd")
        handler = self.commands.get(command) \
            if isinstance(command, str) else None
        if handler is None:
            return wire.fail("bad-cmd")
        try:
            return handler(request)
        except Exception as exc:  # one boundary: a handler bug is a reply
            return wire.fail("internal-error: %s" % (exc,))
