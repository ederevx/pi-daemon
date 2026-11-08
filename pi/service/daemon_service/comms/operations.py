"""The comms wire: published methods, validated params, result shapes."""

from ..transport.rpc_codec import (CAPACITY, INVALID_PARAMS, UNAVAILABLE)
from ..transport.rpc_error import RpcError
from .error import CommsError
from .mailbox_store import (ALL_TARGET, BAD_REQUEST, BUSY, PAYLOAD_TOO_LARGE,
                            REQUEST_CONFLICT, STORE_CORRUPT, UNKNOWN_RECIPIENT)

LOCAL_SCOPE = "local"
NO_LOCAL_RECIPIENT = "no-local-recipient"
CODES = {
    BUSY: CAPACITY,
    REQUEST_CONFLICT: INVALID_PARAMS,
    BAD_REQUEST: INVALID_PARAMS,
    PAYLOAD_TOO_LARGE: INVALID_PARAMS,
    UNKNOWN_RECIPIENT: INVALID_PARAMS,
    STORE_CORRUPT: UNAVAILABLE,
}


class CommsOperations:
    """It validates the wire and projects the domain; it owns no mail."""

    def __init__(self, service, config):
        self._service = service
        self._config = config

    def handlers(self):
        return {
            "comms.send": self.send,
            "comms.broadcast": self.broadcast,
            "comms.replay": self.replay,
            "comms.ack": self.ack,
        }

    def send(self, sender, params):
        request_id = self._identifier(params, "requestId")
        target = self._agent(params.get("to"), "to")
        kind = self._kind(params)
        payload = self._payload(params)
        ttl = self._ttl(params)
        result = self._project(self._service.send, sender, request_id, target,
                               kind, payload, ttl)
        accepted = self._accepted(result)
        if accepted:
            return {"status": "accepted",
                    "messageId": accepted[0]["messageId"]}
        return {"status": "undeliverable",
                "reason": self._undeliverable(result)[0]["reason"]}

    def broadcast(self, sender, params):
        request_id = self._identifier(params, "requestId")
        if params.get("scope", LOCAL_SCOPE) != LOCAL_SCOPE:
            raise RpcError(INVALID_PARAMS, "only the local scope is served")
        kind = self._kind(params)
        payload = self._payload(params)
        ttl = self._ttl(params)
        result = self._project(self._service.send, sender, request_id,
                               ALL_TARGET, kind, payload, ttl)
        accepted = self._accepted(result)
        if accepted:
            return {"status": "accepted",
                    "broadcastId": request_id,
                    "localRecipientCount": len(accepted)}
        declines = self._undeliverable(result) or [{"reason":
                                                    NO_LOCAL_RECIPIENT}]
        return {"status": "undeliverable", "reason": declines[0]["reason"]}

    def replay(self, agent, params):
        cursor = self._cursor(params, agent)
        limit = self._limit(params)
        result = self._project(self._service.replay, agent["agentId"], cursor,
                               limit)
        if result["status"] == "cursor-expired":
            return {"status": result["status"],
                    "oldestAvailableSequence":
                        result["oldestAvailableSequence"]}
        return {"status": result["status"],
                "messages": result["messages"],
                "cursor": self._position(agent, result["cursor"]),
                "more": result["more"]}

    def ack(self, agent, params):
        message_ids = params.get("messageIds")
        if not isinstance(message_ids, list) or not all(
                isinstance(item, str) and item for item in message_ids):
            raise RpcError(INVALID_PARAMS, "messageIds must be strings")
        return self._project(self._service.acknowledge, agent["agentId"],
                             message_ids)

    # -- validation --------------------------------------------------------

    def _identifier(self, params, name):
        value = params.get(name)
        if not isinstance(value, str) or not value or \
                len(value) > self._config.comms_request_id_limit:
            raise RpcError(INVALID_PARAMS, f"{name} must be a short string")
        return value

    def _agent(self, value, name):
        if not isinstance(value, dict):
            raise RpcError(INVALID_PARAMS, f"{name} must be an agent reference")
        agent_id = value.get("agentId")
        node_id = value.get("nodeId")
        if not isinstance(agent_id, str) or not agent_id:
            raise RpcError(INVALID_PARAMS, f"{name}.agentId is required")
        if len(agent_id) > self._config.comms_agent_id_limit:
            raise RpcError(INVALID_PARAMS, f"{name}.agentId must be short")
        if node_id is not None and (not isinstance(node_id, str) or
                                    not node_id):
            raise RpcError(INVALID_PARAMS, f"{name}.nodeId must be a name")
        return {"nodeId": node_id or "", "agentId": agent_id}

    def _kind(self, params):
        value = params.get("kind")
        if not isinstance(value, str) or not value or \
                len(value) > self._config.comms_kind_limit:
            raise RpcError(INVALID_PARAMS, "kind must be a short string")
        return value

    def _cursor(self, params, agent):
        value = params.get("cursor")
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RpcError(INVALID_PARAMS, "cursor must be a mailbox cursor")
        sequence = value.get("sequence")
        owner = value.get("agent")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or \
                sequence < 0:
            raise RpcError(INVALID_PARAMS, "cursor.sequence must be a number")
        if not isinstance(owner, dict) or \
                owner.get("agentId") != agent["agentId"]:
            raise RpcError(INVALID_PARAMS, "a cursor belongs to one mailbox")
        return sequence

    def _limit(self, params):
        value = params.get("limit")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RpcError(INVALID_PARAMS, "limit must be a positive integer")
        return value

    def _payload(self, params):
        if "payload" not in params:
            raise RpcError(INVALID_PARAMS, "payload is required")
        return params["payload"]

    def _ttl(self, params):
        value = params.get("ttlSeconds")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RpcError(INVALID_PARAMS, "ttlSeconds must be a number")
        return float(value)

    # -- projection --------------------------------------------------------

    @staticmethod
    def _project(call, *args):
        try:
            return call(*args)
        except CommsError as exc:
            raise RpcError(CODES.get(exc.code, UNAVAILABLE),
                           str(exc.code)) from None

    @staticmethod
    def _position(agent, sequence):
        return {"agent": {"nodeId": agent["nodeId"],
                          "agentId": agent["agentId"]},
                "sequence": sequence}

    @staticmethod
    def _accepted(result):
        if result["status"] == "accepted":
            return result["messages"]
        if result["status"] == "duplicate":
            return result["receipt"]["accepted"]
        return []

    @staticmethod
    def _undeliverable(result):
        if result["status"] == "undeliverable":
            return result["undeliverable"]
        if result["status"] == "duplicate":
            return result["receipt"]["undeliverable"]
        return []
