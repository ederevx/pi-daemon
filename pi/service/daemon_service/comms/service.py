"""The comms domain's composition: mailbox binding and the live relay."""

import hmac
import threading

from ..transport.rpc_codec import (CAPACITY, INVALID_PARAMS, UNAUTHORIZED,
                                   UNAVAILABLE)
from ..transport.rpc_error import RpcError
from .error import CommsError
from .message_relay import MessageRelay
from .operations import CommsOperations
from .session import CommsSession

SERVICE = "agent-comms"
VERSION = 1


class CommsService:
    """An identity exists once it is presented; nothing here lists agents."""

    def __init__(self, store, config, token, node_id):
        self._store = store
        self._config = config
        self._token = token
        self._node_id = node_id
        self._relay = MessageRelay()
        self._operations = CommsOperations(self, config)
        # Acceptance and its notifications share one order, or mail arrives
        # out of sequence.
        self._send_lock = threading.Lock()

    def start(self):
        self._store.load()
        return self

    def stop(self):
        self._store.save()

    def node_id(self):
        return self._node_id

    # -- binding -----------------------------------------------------------

    def bind(self, params, writer):
        """Authenticate a hello, reserve its mailbox and bind the socket."""
        params = self._params(params)
        supplied = str(params.get("token") or "")
        if not hmac.compare_digest(supplied, self._token):
            raise RpcError(UNAUTHORIZED, "bad token")
        if params.get("version") != VERSION:
            raise RpcError(INVALID_PARAMS, "unsupported version")
        agent = self._agent(params.get("agent"))
        try:
            self._store.reserve(agent["agentId"])
        except CommsError as exc:
            raise RpcError(CAPACITY if exc.code == "busy" else UNAVAILABLE,
                           str(exc.code)) from None
        return CommsSession(self._config, self._store, self._relay, writer,
                            self._operations.handlers(), agent)

    def _agent(self, value):
        if not isinstance(value, dict):
            raise RpcError(INVALID_PARAMS, "an agent is required")
        agent_id = str(value.get("agentId") or "")
        if not agent_id:
            raise RpcError(INVALID_PARAMS, "an agent id is required")
        if len(agent_id) > self._config.comms_agent_id_limit:
            raise RpcError(INVALID_PARAMS, "an agent id must be short")
        if str(value.get("nodeId") or self._node_id) != self._node_id:
            raise RpcError(INVALID_PARAMS, "this node is the one served")
        # A sender is stamped with this node, whatever the caller claimed.
        return {"nodeId": self._node_id, "agentId": agent_id}

    @staticmethod
    def _params(params):
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params are required")
        return params

    # -- domain ------------------------------------------------------------

    def send(self, sender, request_id, target, kind, payload,
             ttl_seconds=None):
        with self._send_lock:
            result = self._store.accept(sender, request_id, target, kind,
                                        payload, ttl_seconds)
            self._relay.publish(result.get("envelopes") or [])
        return result

    def replay(self, agent_id, cursor, limit):
        return self._store.replay(agent_id, cursor, limit)

    def acknowledge(self, agent_id, message_ids):
        return self._store.acknowledge(agent_id, message_ids)
