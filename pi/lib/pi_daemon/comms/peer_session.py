"""One authenticated peer connection: mail delivery and a liveness probe.

It is not an agent session: it has no mailbox, no topics, no waits and no
spawn. Its methods are an explicit allowlist, whatever the daemon offers its
own agents.
"""

from ..transport.rpc_codec import INVALID_PARAMS, RpcCodec
from ..transport.rpc_error import RpcError

SERVICE = "agent-comms"
VERSION = 1
PROTOCOL = RpcCodec.PROTOCOL


class PeerSession:
    """The peer's identity and generation are fixed for the connection."""

    def __init__(self, config, operations, peer):
        self._config = config
        self._operations = operations
        self._handlers = operations.peer_handlers()
        self._peer = dict(peer)

    def greeting(self):
        return {
            "service": SERVICE,
            "version": VERSION,
            "protocol": PROTOCOL,
            "nodeId": self._operations.node_id(),
            "peer": {"nodeId": self._peer["nodeId"]},
            "methods": self.methods(),
            "topics": [],
            "maxFrameBytes": self._config.frame_bytes,
            "serverTime": self._operations.server_time(),
            "limits": {"maxTtlSeconds": self._config.comms_max_ttl,
                       "maxPayloadBytes": self._config.comms_payload_bytes},
        }

    def methods(self):
        return sorted(self._handlers)

    def handle(self, request):
        method = request.get("method")
        handler = self._handlers.get(method)
        if handler is None:
            raise LookupError(method)
        params = request.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params are required")
        return handler(self._peer, params)

    def cancel(self, _params):
        """Nothing here is cancellable: delivery is one request and answer."""
        return None

    def close(self):
        return None
