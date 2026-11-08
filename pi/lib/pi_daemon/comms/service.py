"""The comms domain's composition: mailboxes, the relay, waits and launches."""

import hashlib
import hmac
import json
import threading
import uuid

from ..transport.rpc_codec import (CAPACITY, INVALID_PARAMS, UNAUTHORIZED,
                                   UNAVAILABLE)
from ..transport.rpc_error import RpcError
from .error import CommsError
from .mailbox_store import BAD_REQUEST, QUEUED, STORE_FAILED
from .message_relay import MessageRelay
from .operations import CommsOperations, SPAWN_FAILED
from .peer_operations import PeerOperations
from .peer_session import PeerSession
from .session import CommsSession
from .spawn_ledger import (ENV_AGENT_ID, ENV_HOST, ENV_NODE_ID,
                           ENV_PARENT_AGENT_ID, ENV_PORT, ENV_SPAWN_ID,
                           SPAWNED)
from .wait_registry import CLOSING, WaitRegistry

SERVICE = "agent-comms"
VERSION = 1


class CommsService:
    """An identity exists once it is presented; nothing here lists agents."""

    def __init__(self, store, config, token, node_id, spawns=None,
                 launcher=None):
        self._store = store
        self._config = config
        self._token = token
        self._node_id = node_id
        self._spawns = spawns
        self._launcher = launcher
        self._peers = None
        self._peer_operations = None
        self._relay = MessageRelay()
        self._waits = WaitRegistry(store, config)
        self._operations = CommsOperations(self, config)
        # Acceptance and its notifications share one order, or mail arrives
        # out of sequence.
        self._send_lock = threading.Lock()
        # A launch holds the ledger from the record to the settled verdict.
        self._spawn_lock = threading.Lock()
        self._endpoint = None
        self._closing = False

    def start(self):
        self._store.load()
        if self._spawns is not None:
            self._spawns.load()
        self._waits.start()
        if self._peers is not None:
            self._peers.start()
        return self

    def use_peers(self, manager, operations=None, credentials=None):
        """Attach the peer manager and the wire it serves, before start()."""
        self._peers = manager
        self._peer_operations = operations or PeerOperations(
            manager, self._config, self._node_id, directory=credentials)
        return self

    def peer_handlers(self):
        if self._peer_operations is None:
            return {}
        return self._peer_operations.handlers()

    def begin_shutdown(self):
        """No new wait is registered while the pending ones are answered."""
        self._closing = True
        if self._peers is not None:
            self._peers.begin_shutdown()
        self._waits.stop()

    def stop(self):
        if not self._closing:
            self.begin_shutdown()
        if self._peers is not None:
            self._peers.stop()
        self._store.save()
        if self._spawns is not None:
            self._spawns.save()

    def node_id(self):
        return self._node_id

    def serve_at(self, host, port):
        """The address a spawned agent is told to connect back to."""
        self._endpoint = {"host": host, "port": port}
        return self

    # -- binding -----------------------------------------------------------

    def bind(self, params, writer):
        """Authenticate a hello, reserve its mailbox and bind the socket."""
        params = self._params(params)
        if params.get("peer") is not None:
            return self._bind_peer(params)
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
        return CommsSession(self._config, self._store, self._relay,
                            self._waits, writer, self._operations, agent)

    def _bind_peer(self, params):
        """A peer presents its own credential, never the daemon token."""
        if self._peers is None or self._peer_operations is None:
            raise RpcError(UNAUTHORIZED, "peers are not served here")
        if params.get("agent") is not None or params.get("token") is not None:
            raise RpcError(INVALID_PARAMS, "one identity per connection")
        if params.get("version") != VERSION:
            raise RpcError(INVALID_PARAMS, "unsupported version")
        peer = params.get("peer")
        if not isinstance(peer, dict):
            raise RpcError(UNAUTHORIZED, "a peer identity is required")
        node_id = peer.get("nodeId")
        credential = peer.get("credential")
        if not isinstance(node_id, str) or not isinstance(credential, str) \
                or not self._peers.verify(node_id, credential):
            raise RpcError(UNAUTHORIZED, "bad peer credential")
        generation = self._peers.generation(node_id)
        if generation is None:
            raise RpcError(UNAUTHORIZED, "the peer is not configured")
        return PeerSession(self._config, self._peer_operations,
                           {"nodeId": node_id, "generation": generation})

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
            route = self._peers.route if self._peers is not None else None
            result = self._store.accept(sender, request_id, target, kind,
                                        payload, ttl_seconds, route=route)
            envelopes = result.get("envelopes") or []
            self._relay.publish(envelopes)
            if result.get("status") == QUEUED:
                self._peers.wake(result["peerNodeId"])
        self._waits.signal({envelope["to"]["agentId"] for envelope
                            in envelopes})
        return result

    def receipt(self, sender, request_id):
        return self._store.receipt(sender, request_id)

    def deliver_peer(self, peer_node_id, generation, delivery):
        """One authenticated peer's mail, committed before it is published."""
        with self._send_lock:
            result = self._store.accept_peer(peer_node_id, delivery)
            envelope = result.get("envelope")
            if envelope is not None:
                self._relay.publish([envelope])
        if envelope is not None:
            self._waits.signal({envelope["to"]["agentId"]})
        return result["outcome"]

    def replay(self, agent_id, cursor, limit):
        return self._store.replay(agent_id, cursor, limit)

    def acknowledge(self, agent_id, message_ids):
        return self._store.acknowledge(agent_id, message_ids)

    # -- launching another agent -------------------------------------------

    def spawns_enabled(self):
        return self._spawns is not None and self._launcher is not None

    def spawn(self, sender, request_id, spec):
        """A recorded verdict answers a retry. A new request launches once."""
        if not self.spawns_enabled():
            raise CommsError(UNAVAILABLE)
        with self._spawn_lock:
            if self._closing:
                raise CommsError(CLOSING)
            if self._endpoint is None:
                raise CommsError(CLOSING)
            digest = self._digest(spec)
            found = self._spawns.find(sender, request_id, digest)
            if found is not None:
                return self._spawn_result(found, created=False)
            agent_id = spec.get("agentId") or uuid.uuid4().hex
            if agent_id == sender["agentId"]:
                raise CommsError(BAD_REQUEST)
            agent = {"nodeId": self._node_id, "agentId": agent_id}
            name = spec.get("name") or agent_id
            check = dict(spec, name=name)
            verdict = self._launcher.check(check)
            if not verdict["ok"]:
                raise CommsError(verdict["reason"])
            launch = verdict["spec"]
            record = self._spawns.open(sender, request_id, digest, agent,
                                       launch["name"])
            spawn_id = record["spawnId"]
            try:
                self._store.reserve(agent_id, provisional=spawn_id)
            except (CommsError, OSError) as failure:
                if not self._forget(sender, request_id, agent_id, spawn_id):
                    raise CommsError(STORE_FAILED) from None
                if isinstance(failure, CommsError):
                    raise
                raise CommsError(STORE_FAILED) from None
            launch["env"] = dict(launch["env"] or {},
                                 **self._identity_env(sender, agent,
                                                      record["spawnId"]))
            launch["env_once"] = dict(launch["env_once"] or {},
                                      **self._endpoint_env())
            try:
                outcome = self._launcher.launch(launch)
            except (CommsError, ValueError):
                outcome = {"ok": False, "reason": SPAWN_FAILED}
            if not outcome["ok"]:
                reason = outcome.get("reason") or SPAWN_FAILED
                if not self._forget(sender, request_id, agent_id, spawn_id):
                    raise CommsError(STORE_FAILED)
                raise CommsError(reason)
            try:
                record = self._spawns.settle(sender, request_id, SPAWNED)
            except (CommsError, OSError):
                # The launch is uncertain, so its record stays `launching`
                # and a retry replays that uncertainty instead of relaunching.
                raise CommsError(STORE_FAILED) from None
            return self._spawn_result(record, created=True)

    def _forget(self, sender, request_id, agent_id, spawn_id):
        """A launch that started nothing leaves neither record nor mailbox.

        The mailbox goes first: if it cannot be released the record stays,
        so the retry still replays a launch that may have started. A record
        that cannot be discarded also stays, and the caller is told
        `store-failed` instead of the ordinary free retry.
        """
        try:
            self._store.release(agent_id, spawn_id)
        except (CommsError, OSError):
            return False
        try:
            self._spawns.discard(sender, request_id)
        except (CommsError, OSError):
            return False
        return True

    def _identity_env(self, sender, agent, spawn_id):
        """A child knows its parent, and that knowledge is all it gets."""
        return {ENV_AGENT_ID: agent["agentId"],
                ENV_NODE_ID: agent["nodeId"],
                ENV_PARENT_AGENT_ID: sender["agentId"],
                ENV_SPAWN_ID: spawn_id}

    def _endpoint_env(self):
        return {ENV_HOST: self._endpoint["host"],
                ENV_PORT: str(self._endpoint["port"])}

    @staticmethod
    def _digest(spec):
        fields = ("agentId", "name", "dir", "argv", "cols", "rows", "env",
                  "envOnce")
        encoded = json.dumps({field: spec.get(field) for field in fields},
                             sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _spawn_result(record, created):
        return {"status": record["status"], "spawnId": record["spawnId"],
                "requestId": record["requestId"],
                "agent": dict(record["agent"]),
                "parent": dict(record["parent"]),
                "session": dict(record["session"]),
                "created": created, "reason": record.get("reason")}
