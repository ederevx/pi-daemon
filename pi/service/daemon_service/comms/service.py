"""The comms domain's composition: mailbox binding and the live relay."""

import hmac

from ..transport.rpc_codec import INVALID_PARAMS, UNAUTHORIZED
from ..transport.rpc_error import RpcError
from .message_relay import MESSAGE_TOPIC, MessageRelay
from .operations import CommsOperations
from .session import CommsSession

SERVICE = "agent-comms"
VERSION = 1


class CommsService:
    """An identity exists once it is presented; nothing here lists agents."""

    def __init__(self, store, config, token, node_id=""):
        self._store = store
        self._config = config
        self._token = token
        self._node_id = node_id
        self._relay = MessageRelay(config)
        self._operations = CommsOperations(self)

    def start(self):
        self._store.load()
        self._relay.start()
        return self

    def stop(self):
        self._relay.stop()
        self._store.save()

    def store(self):
        return self._store

    # -- binding -----------------------------------------------------------

    def bind(self, params, writer):
        """Authenticate a hello, reserve its mailbox and bind the socket."""
        params = self._params(params)
        if params.get("version") != VERSION:
            raise RpcError(INVALID_PARAMS, "unsupported version")
        supplied = str(params.get("token") or "")
        if not hmac.compare_digest(supplied, self._token):
            raise RpcError(UNAUTHORIZED, "bad token")
        agent = self._agent(params.get("agent"))
        self._store.reserve(agent["agentId"])
        return CommsSession(self._config, self._store, self._relay, writer,
                            self._operations.handlers(), agent)

    @staticmethod
    def _params(params):
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params are required")
        return params

    @staticmethod
    def _agent(value):
        if not isinstance(value, dict):
            raise RpcError(INVALID_PARAMS, "an agent is required")
        agent_id = str(value.get("agentId") or "")
        if not agent_id:
            raise RpcError(INVALID_PARAMS, "an agent id is required")
        return {"nodeId": str(value.get("nodeId") or ""),
                "agentId": agent_id}

    # -- domain ------------------------------------------------------------

    def send(self, sender, request_id, target, kind, payload,
             ttl_seconds=None):
        result = self._store.accept(sender, request_id, target, kind, payload,
                                    ttl_seconds)
        self._relay.publish(result.get("envelopes") or [])
        return result

    def replay(self, agent_id, cursor, limit):
        return self._store.replay(agent_id, cursor, limit)

    def acknowledge(self, agent_id, message_ids):
        return self._store.acknowledge(agent_id, message_ids)

    def topic(self):
        return MESSAGE_TOPIC
