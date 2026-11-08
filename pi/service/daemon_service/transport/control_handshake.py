"""The pi-pty-host/1 authentication step of one accepted connection."""


class ControlHandshake:
    """Authorization is decided once per connection, by the codec."""

    def __init__(self, codec):
        self._codec = codec

    def authorize(self, request):
        return self._codec.greet(request)
