"""One authenticated peer connection: its node, its generation, its surface.

A peer may present an agent of its own, and that agent is served the mailbox
surface a local agent gets; a peer that presents none keeps the delivery-only
wire. Either way the generation that admitted the connection is checked on
every call, so a removed peer cannot keep using a socket it already opened.
"""

from ..transport.rpc_codec import (INVALID_PARAMS, UNAUTHORIZED, RpcCodec)
from ..transport.rpc_error import RpcError

SERVICE = "agent-comms"
VERSION = 1
PROTOCOL = RpcCodec.PROTOCOL


class PeerSession:
    """The peer's identity and generation are fixed for the connection."""

    def __init__(self, config, operations, peer, bound=None, writer=None,
                 on_close=None):
        self._config = config
        self._operations = operations
        self._handlers = operations.peer_handlers()
        self._peer = dict(peer)
        self._bound = bound
        self._writer = writer
        self._on_close = on_close
        self._closed = False

    def node_id(self):
        """The node this connection authenticated as."""
        return self._peer["nodeId"]

    def greeting(self):
        greeting = {
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
        if self._bound is not None:
            bound = self._bound.greeting()
            limits = dict(greeting["limits"])
            limits.update(bound.pop("limits", None) or {})
            greeting.update(bound)
            greeting["limits"] = limits
            # The wire keeps its own two methods: this connection can still
            # deliver and probe, whichever profile it presented.
            greeting["methods"] = self.methods()
            greeting["peer"] = {"nodeId": self._peer["nodeId"]}
        return greeting

    def methods(self):
        return sorted(set(self._handlers) | set(self._agent_methods()))

    def handle(self, request):
        self._admit()
        method = request.get("method")
        handler = self._handlers.get(method)
        if handler is not None:
            return handler(self._peer, self._params(request))
        if self._bound is None:
            raise LookupError(method)
        return self._bound.handle(request)

    def cancel(self, _params):
        """Nothing to cancel here: an agent's waits cancel in its session."""
        self._admit()
        if self._bound is None:
            return None
        return self._bound.cancel(_params)

    def close(self, wait=True):
        """End the connection, whichever profile presented it.

        A bound session closes the socket through the agent session it holds;
        a delivery-only one holds no agent session, so it closes the socket
        itself rather than leaving it open until the peer speaks again.
        """
        if self._closed:
            return
        self._closed = True
        if self._bound is not None:
            self._bound.close(wait)
        elif self._writer is not None:
            self._writer.close(wait=wait)
        if self._on_close is not None:
            self._on_close(self)

    def _agent_methods(self):
        """The mailbox surface, as offered to the peer that presented it."""
        return [] if self._bound is None else self._bound.methods(self._peer)

    def _admit(self):
        """A connection whose generation is gone could not be admitted."""
        if self._operations.generation(self._peer["nodeId"]) != \
                self._peer["generation"]:
            raise RpcError(UNAUTHORIZED, "peer-stale")

    @staticmethod
    def _params(request):
        params = request.get("params")
        if params is None:
            return {}
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params are required")
        return params
