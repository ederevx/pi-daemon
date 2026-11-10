"""The comms wire: published methods, validated params, result shapes."""

from ..transport.rpc_codec import (CAPACITY, INVALID_PARAMS, UNAVAILABLE)
from ..transport.rpc_error import RpcError
from .error import CommsError
from .mailbox_store import (ALL_TARGET, BAD_REQUEST, BUSY, PAYLOAD_TOO_LARGE,
                            QUEUED, REQUEST_CONFLICT, STORE_CORRUPT,
                            STORE_FAILED, UNDELIVERABLE, UNKNOWN_RECIPIENT)
from .spawn_ledger import ENV_PREFIX, SPAWNED
from .wait_registry import CLOSING, WAIT_PENDING

LOCAL_SCOPE = "local"
NO_LOCAL_RECIPIENT = "no-local-recipient"
CODES = {
    BUSY: CAPACITY,
    REQUEST_CONFLICT: INVALID_PARAMS,
    BAD_REQUEST: INVALID_PARAMS,
    PAYLOAD_TOO_LARGE: INVALID_PARAMS,
    UNKNOWN_RECIPIENT: INVALID_PARAMS,
    STORE_CORRUPT: UNAVAILABLE,
    STORE_FAILED: UNAVAILABLE,
    CLOSING: UNAVAILABLE,
    WAIT_PENDING: INVALID_PARAMS,
    # Session launch verdicts, carried as data by the sessions domain.
    "invalid-session": INVALID_PARAMS,
    "no-dir": INVALID_PARAMS,
    "name-taken": INVALID_PARAMS,
    "spawn-failed": UNAVAILABLE,
}
SPAWN_FAILED = "spawn-failed"


class CommsOperations:
    """It validates the wire and projects the domain; it owns no mail."""

    def __init__(self, service, config):
        self._service = service
        self._config = config

    def handlers(self):
        handlers = {
            "comms.send": self.send,
            "comms.broadcast": self.broadcast,
            "comms.replay": self.replay,
            "comms.ack": self.ack,
            "comms.receipt": self.receipt,
        }
        if self._service.spawns_enabled():
            handlers["comms.spawn"] = self.spawn
        handlers.update(self._service.peer_handlers())
        return handlers

    def send(self, sender, params):
        request_id = self._identifier(params, "requestId")
        target = self._agent(params.get("to"), "to")
        kind = self._kind(params)
        payload = self._payload(params)
        ttl = self._ttl(params)
        result = self._project(self._service.send, sender, request_id, target,
                               kind, payload, ttl)
        initial = self._initial(result)
        if initial is not None:
            return initial
        accepted = self._accepted(result)
        if accepted:
            return {"status": "accepted",
                    "messageId": accepted[0]["messageId"]}
        return {"status": "undeliverable",
                "reason": self._undeliverable(result)[0]["reason"]}

    def receipt(self, sender, params):
        request_id = self._identifier(params, "requestId")
        return self._project(self._service.receipt, sender, request_id)

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

    def spawn(self, sender, params):
        request_id = self._identifier(params, "requestId")
        spec = self._spawn_spec(sender, params)
        result = self._project(self._service.spawn, sender, request_id, spec)
        answer = {"status": result["status"], "spawnId": result["spawnId"],
                  "requestId": result["requestId"],
                  "agent": result["agent"], "parent": result["parent"],
                  "session": result["session"], "created": result["created"]}
        if result["status"] != SPAWNED and result.get("reason"):
            answer["reason"] = result["reason"]
        return answer

    def ack(self, agent, params):
        message_ids = params.get("messageIds")
        if not isinstance(message_ids, list) or not all(
                isinstance(item, str) and item for item in message_ids):
            raise RpcError(INVALID_PARAMS, "messageIds must be strings")
        return self._project(self._service.acknowledge, agent["agentId"],
                             message_ids)

    def wait_request(self, agent, params):
        """What the registry needs to answer a wait, already validated."""
        cursor = self._cursor(params, agent)
        limit = self._limit(params)
        value = params.get("timeoutMs")
        if value is None:
            timeout = self._config.comms_wait_max
        elif isinstance(value, bool) or not isinstance(value, (int, float)) \
                or value < 0:
            raise RpcError(INVALID_PARAMS, "timeoutMs must be a number")
        else:
            timeout = min(float(value) / 1000.0,
                          self._config.comms_wait_max)
        return {"cursor": cursor, "limit": limit, "timeout": timeout}

    def wait_result(self, agent, result):
        """A completed wait, in the same shape `comms.replay` returns."""
        if result["status"] == "cursor-expired":
            return {"status": result["status"],
                    "oldestAvailableSequence":
                        result["oldestAvailableSequence"]}
        cursor = result["cursor"]
        return {"status": result["status"],
                "messages": result["messages"],
                "cursor": self._position(
                    agent, 0 if cursor is None else cursor),
                "more": bool(result.get("more"))}

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

    def _spawn_spec(self, sender, params):
        spec = {"agentId": params.get("agentId"),
                "name": params.get("name"),
                "dir": params.get("dir"),
                "argv": params.get("argv"),
                "task": params.get("task"),
                "cols": params.get("cols"),
                "rows": params.get("rows"),
                "env": self._spawn_env(params.get("env"), "env"),
                "envOnce": self._spawn_env(params.get("envOnce"), "envOnce")}
        agent_id = spec["agentId"]
        if agent_id is not None:
            if not isinstance(agent_id, str) or not agent_id or \
                    len(agent_id) > self._config.comms_agent_id_limit:
                raise RpcError(INVALID_PARAMS, "agentId must be a short name")
            if agent_id == sender["agentId"]:
                raise RpcError(INVALID_PARAMS, "an agent cannot spawn itself")
        if spec["name"] is not None and (not isinstance(spec["name"], str) or
                                         not spec["name"]):
            raise RpcError(INVALID_PARAMS, "name must be a string")
        if not isinstance(spec["dir"], str) or not spec["dir"]:
            raise RpcError(INVALID_PARAMS, "dir is required")
        argv = spec["argv"]
        task = spec["task"]
        if (argv is None) == (task is None):
            raise RpcError(INVALID_PARAMS,
                           "exactly one of argv or task is required")
        if task is not None:
            if not isinstance(task, str) or not task.strip() or "\x00" in task:
                raise RpcError(INVALID_PARAMS,
                               "task must be a non-empty string")
        elif not isinstance(argv, list) or not argv or not all(
                isinstance(item, str) and item for item in argv):
            raise RpcError(INVALID_PARAMS, "argv must be command words")
        for key in ("cols", "rows"):
            value = spec[key]
            if value is not None and (isinstance(value, bool) or
                                      not isinstance(value, int)):
                raise RpcError(INVALID_PARAMS, f"{key} must be a number")
        return spec

    def _spawn_env(self, value, name):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RpcError(INVALID_PARAMS, f"{name} must be an object")
        environment = {}
        for variable, entry in value.items():
            if not isinstance(variable, str) or not variable or \
                    not isinstance(entry, str):
                raise RpcError(INVALID_PARAMS,
                               f"{name} must map names to strings")
            if variable.startswith(ENV_PREFIX):
                raise RpcError(INVALID_PARAMS,
                               f"{name} may not set {variable}")
            environment[variable] = entry
        return environment

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
    def _initial(result):
        """A remote send's first answer, replayed as it was first given."""
        initial = None
        if result["status"] == QUEUED:
            initial = result
        elif result["status"] == "duplicate":
            initial = result.get("initial")
        if initial is None:
            return None
        if initial["status"] == QUEUED:
            return {"status": QUEUED,
                    "deliveryId": initial["deliveryId"],
                    "expiresAt": initial["expiresAt"]}
        if initial["status"] == UNDELIVERABLE:
            return {"status": UNDELIVERABLE,
                    "reason": initial.get("reason")}
        return None

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
