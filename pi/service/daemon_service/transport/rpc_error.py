"""A refusal with a published JSON-RPC code, raised where it is decided."""


class RpcError(Exception):
    """A request the provider refuses, carrying its code and message."""

    def __init__(self, code, message="request refused", request_id=None):
        super().__init__(str(message))
        self.code = int(code)
        self.message = str(message)
        self.request_id = request_id
