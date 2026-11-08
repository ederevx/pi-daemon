"""The peer wire: administration for the local operator, delivery for peers.

Validation lives here so neither the manager nor the store parses params.
`comms.peer.deliver` is never offered to an agent's connection.
"""

import re
import time

from ..transport.rpc_codec import (CAPACITY, INVALID_PARAMS, UNAUTHORIZED,
                                   UNAVAILABLE)
from ..transport.rpc_error import RpcError
from .error import CommsError
from .mailbox_store import (BAD_REQUEST, BUSY, PAYLOAD_TOO_LARGE,
                            REQUEST_CONFLICT, STORE_CORRUPT, STORE_FAILED,
                            UNKNOWN_RECIPIENT)
from .node_identity import NODE_ID
from .peer_manager import CLOSING, CONFIG_CONFLICT, STALE_PEER

VERIFIER = re.compile(r"[0-9a-f]{64}")
TARGET_LIMIT = 255
CODES = {
    BUSY: CAPACITY,
    REQUEST_CONFLICT: INVALID_PARAMS,
    BAD_REQUEST: INVALID_PARAMS,
    PAYLOAD_TOO_LARGE: INVALID_PARAMS,
    UNKNOWN_RECIPIENT: INVALID_PARAMS,
    STORE_CORRUPT: UNAVAILABLE,
    STORE_FAILED: UNAVAILABLE,
    CLOSING: UNAVAILABLE,
    STALE_PEER: INVALID_PARAMS,
    CONFIG_CONFLICT: INVALID_PARAMS,
    UNAUTHORIZED: UNAUTHORIZED,
}


class PeerOperations:
    """It validates the peer wire and projects the manager; it owns no state."""

    def __init__(self, manager, config, node_id, clock=None):
        self._manager = manager
        self._config = config
        self._node_id = node_id
        self._clock = clock or time.time

    def handlers(self):
        return {"comms.peer.add": self.add,
                "comms.peer.remove": self.remove,
                "comms.peer.list": self.list}

    def peer_handlers(self):
        return {"comms.peer.deliver": self.deliver,
                "comms.peer.ping": self.ping}

    def add(self, _sender, params):
        return self._call(self._manager.configure, self._spec(params))

    def remove(self, _sender, params):
        node_id = self._node(params, "nodeId")
        return self._call(self._manager.remove, node_id)

    def list(self, _sender, _params):
        return self._manager.list()

    def deliver(self, peer, params):
        if not isinstance(params, dict) or \
                not isinstance(params.get("delivery"), dict):
            raise RpcError(INVALID_PARAMS, "a delivery object is required")
        return self._call(self._manager.deliver, peer["nodeId"],
                          peer["generation"], params["delivery"])

    def ping(self, _peer, _params):
        return {"nodeId": self._node_id,
                "serverTime": self.server_time()}

    def node_id(self):
        return self._node_id

    def server_time(self):
        return self._clock()

    # -- validation --------------------------------------------------------

    def _spec(self, params):
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params are required")
        node_id = self._node(params, "nodeId")
        target = params.get("sshTarget")
        if not isinstance(target, str) or not target or \
                len(target) > TARGET_LIMIT or target.startswith("-") or \
                any(character.isspace() for character in target):
            raise RpcError(INVALID_PARAMS, "sshTarget must be a destination")
        remote = params.get("remotePort")
        if not self._port(remote) or remote == 0:
            raise RpcError(INVALID_PARAMS, "remotePort must be a tcp port")
        local = params.get("localPort", 0)
        if not self._port(local):
            raise RpcError(INVALID_PARAMS, "localPort must be a tcp port")
        credential = params.get("outboundCredentialFile")
        if not isinstance(credential, str) or \
                not credential.startswith("/") or len(credential) > 4096:
            raise RpcError(INVALID_PARAMS,
                           "outboundCredentialFile must be an absolute path")
        verifier = params.get("inboundCredentialSha256")
        if not isinstance(verifier, str) or \
                not VERIFIER.fullmatch(verifier):
            raise RpcError(INVALID_PARAMS,
                           "inboundCredentialSha256 must be a sha256 digest")
        return {"nodeId": node_id, "sshTarget": target,
                "remotePort": remote, "localPort": local,
                "outboundCredentialFile": credential,
                "inboundCredentialSha256": verifier}

    def _node(self, params, name):
        value = params.get("nodeId")
        if not isinstance(value, str) or not NODE_ID.fullmatch(value):
            raise RpcError(INVALID_PARAMS, f"{name} must be a node id")
        if value == self._node_id:
            raise RpcError(INVALID_PARAMS, "a node cannot peer with itself")
        return value

    @staticmethod
    def _port(value):
        return not isinstance(value, bool) and isinstance(value, int) and \
            0 <= value <= 65535

    @staticmethod
    def _call(call, *args):
        try:
            return call(*args)
        except CommsError as exc:
            raise RpcError(CODES.get(exc.code, UNAVAILABLE),
                           str(exc.code)) from None
        except (OSError, ValueError):
            raise RpcError(UNAVAILABLE, "the peer state is unavailable") \
                from None
