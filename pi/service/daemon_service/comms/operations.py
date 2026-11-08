"""The comms wire: published methods, validated params, result shapes."""

from ..transport.rpc_codec import INVALID_PARAMS
from ..transport.rpc_error import RpcError
from .mailbox_store import ALL_TARGET

REQUEST_ID_LIMIT = 200
KIND_LIMIT = 64
LOCAL_SCOPE = "local"


class CommsOperations:
    """It validates the wire and projects the domain; it owns no mail."""

    def __init__(self, service):
        self._service = service

    def handlers(self):
        return {
            "comms.send": self.send,
            "comms.broadcast": self.broadcast,
            "comms.replay": self.replay,
            "comms.ack": self.ack,
        }

    def send(self, sender, params):
        request_id = _identifier(params, "requestId")
        target = _agent(params.get("to"), "to")
        kind = _kind(params)
        payload = _payload(params)
        ttl = _ttl(params)
        result = self._service.send(sender, request_id, target, kind, payload,
                                    ttl)
        if result["status"] in ("accepted", "duplicate"):
            return {"status": "accepted",
                    "messageId": result["messages"][0]["messageId"]}
        return {"status": "undeliverable",
                "reason": result["undeliverable"][0]["reason"]}

    def broadcast(self, sender, params):
        request_id = _identifier(params, "requestId")
        if params.get("scope", LOCAL_SCOPE) != LOCAL_SCOPE:
            raise RpcError(INVALID_PARAMS, "only the local scope is served")
        kind = _kind(params)
        payload = _payload(params)
        ttl = _ttl(params)
        result = self._service.send(sender, request_id, ALL_TARGET, kind,
                                    payload, ttl)
        if result["status"] in ("accepted", "duplicate"):
            return {"status": "accepted",
                    "broadcastId": request_id,
                    "localRecipientCount": len(result["messages"])}
        return {"status": "undeliverable", "reason": "no-local-recipient"}

    def replay(self, agent, params):
        return self._service.replay(agent["agentId"], _cursor(params),
                                    _limit(params))

    def ack(self, agent, params):
        message_ids = params.get("messageIds")
        if not isinstance(message_ids, list) or not all(
                isinstance(item, str) for item in message_ids):
            raise RpcError(INVALID_PARAMS, "messageIds must be strings")
        return self._service.acknowledge(agent["agentId"], message_ids)


def _identifier(params, name):
    value = params.get(name)
    if not isinstance(value, str) or not value or len(value) > REQUEST_ID_LIMIT:
        raise RpcError(INVALID_PARAMS, f"{name} must be a short string")
    return value


def _agent(value, name):
    if not isinstance(value, dict):
        raise RpcError(INVALID_PARAMS, f"{name} must be an agent reference")
    agent_id = value.get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
        raise RpcError(INVALID_PARAMS, f"{name}.agentId is required")
    node_id = value.get("nodeId")
    if node_id is not None and not isinstance(node_id, str):
        raise RpcError(INVALID_PARAMS, f"{name}.nodeId must be a string")
    return {"nodeId": node_id or "", "agentId": agent_id}


def _kind(params):
    kind = params.get("kind")
    if not isinstance(kind, str) or not kind or len(kind) > KIND_LIMIT:
        raise RpcError(INVALID_PARAMS, "kind must be a short string")
    return kind


def _cursor(params):
    value = params.get("cursor")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RpcError(INVALID_PARAMS, "cursor must be a sequence number")
    return value


def _limit(params):
    value = params.get("limit")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RpcError(INVALID_PARAMS, "limit must be a positive integer")
    return value


def _payload(params):
    if "payload" not in params:
        raise RpcError(INVALID_PARAMS, "payload is required")
    return params["payload"]


def _ttl(params):
    value = params.get("ttlSeconds")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RpcError(INVALID_PARAMS, "ttlSeconds must be a number")
    return float(value)
