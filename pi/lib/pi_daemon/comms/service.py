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
from .mailbox_store import (BAD_REQUEST, IDENTITY_CLAIMED, QUEUED,
                            STORE_FAILED)
from .message_relay import MessageRelay
from .operations import (CommsOperations, METHOD_UNAVAILABLE,
                         PEER_LOST_ANSWER, PEER_UNREACHABLE, SPAWN_FAILED,
                         UNKNOWN_PEER)
from .peer_operations import PeerOperations
from .peer_link_error import PeerLinkError
from .peer_session import PeerSession
from .session import CommsSession
from .spawn_ledger import (ENV_AGENT_ID, ENV_HOST, ENV_NODE_ID,
                           ENV_PARENT_AGENT_ID, ENV_PARENT_NODE_ID, ENV_PORT,
                           ENV_SPAWN_ID, SPAWNED)
from .wait_registry import CLOSING, WaitRegistry

SERVICE = "agent-comms"
# Link failures that follow a frame the peer did read: the answer is lost.
LOST_ANSWERS = frozenset({"request-timeout", "link-broken",
                          "mismatched-reply", "bad-error-code"})
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
        self._mailboxes = None
        self._peer_operations = None
        self._peer_sessions = []
        self._peer_lock = threading.Lock()
        self._relay = MessageRelay()
        self._waits = WaitRegistry(config)
        self._operations = CommsOperations(self, config)
        self._sessions_wire = None
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
        if self._mailboxes is not None:
            self._mailboxes.load()
        self._waits.start()
        if self._peers is not None:
            self._peers.start()
        return self

    def use_peers(self, manager, operations=None, credentials=None,
                  mailboxes=None):
        """Attach the peer manager and the wire it serves, before start()."""
        self._peers = manager
        self._mailboxes = mailboxes
        self._peer_operations = operations or PeerOperations(
            manager, self._config, self._node_id, directory=credentials)
        manager.on_peer_gone(self.close_peer_sessions)
        return self

    def peer_handlers(self):
        if self._peer_operations is None:
            return {}
        return self._peer_operations.handlers()

    def use_sessions(self, wire):
        """Attach the session wire another domain composed, before start()."""
        self._sessions_wire = wire
        return self

    def session_handlers(self):
        """The session methods this node serves, control only when allowed."""
        if self._sessions_wire is None:
            return {}
        return self._sessions_wire.handlers(
            self._config.comms_session_control)

    def feed_plan(self, sender, params):
        """One validated held feed, ready for the wait registry."""
        if self._sessions_wire is None:
            raise CommsError(METHOD_UNAVAILABLE)
        return self._sessions_wire.feed_plan(sender, params)

    def capabilities(self):
        """The additive capability names a greeting may carry."""
        if self._sessions_wire is None:
            return []
        return ["session.reach.v1", "session.feed.v1"]

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
        if self._mailboxes is not None:
            self._mailboxes.save()

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
            return self._bind_peer(params, writer)
        supplied = str(params.get("token") or "")
        if not hmac.compare_digest(supplied, self._token):
            raise RpcError(UNAUTHORIZED, "bad token")
        if params.get("version") != VERSION:
            raise RpcError(INVALID_PARAMS, "unsupported version")
        agent = self._agent(params.get("agent"))
        try:
            self._store.reserve(agent["agentId"], permitted=self._permit())
        except CommsError as exc:
            if exc.code == IDENTITY_CLAIMED:
                raise RpcError(INVALID_PARAMS,
                               "that id belongs to a peer") from None
            raise RpcError(CAPACITY if exc.code == "busy" else UNAVAILABLE,
                           str(exc.code)) from None
        return CommsSession(self._config, self._store, self._relay,
                            self._waits, writer, self._operations, agent)

    def _bind_peer(self, params, writer):
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
        identity = {"nodeId": node_id, "generation": generation}
        bound = None
        agent_id = peer.get("agentId")
        if agent_id is not None:
            bound = self._bind_peer_agent(node_id, agent_id, writer)
        session = PeerSession(self._config, self._peer_operations, identity,
                              bound, writer, on_close=self._forget_peer_session)
        # A removal or a shutdown may have swept the connections while this
        # one was binding: registering behind the sweep would leave a
        # connection nothing can close. The sweep runs under this lock. A
        # connection refused here is answered on its socket like any other
        # greeting error, and the loop closes it.
        with self._peer_lock:
            admitted = not self._closing and \
                self._peers.admitting() and \
                self._peers.generation(node_id) == generation
            if admitted:
                self._peer_sessions.append(session)
        if not admitted:
            # The socket still owes this greeting its refusal, which the loop
            # writes; what the session took from the relay is released here,
            # or the refused connection would stay attached forever.
            if bound is not None:
                bound.detach()
            raise RpcError(UNAUTHORIZED, "peer-stale")
        return session

    def _forget_peer_session(self, session):
        """A connection that is over stops being one removal can close."""
        with self._peer_lock:
            if session in self._peer_sessions:
                self._peer_sessions.remove(session)

    def _permit(self):
        """The ownership rule a local reservation applies, if peers exist."""
        if self._mailboxes is None:
            return None
        return self._mailboxes.permit

    def close_peer_sessions(self, node_id=None):
        """A peer that is gone keeps no connection here, open or bound.

        The generation check refuses a removed peer's calls, but a held wait
        would otherwise answer later and an unread mailbox would stay
        readable by a node this daemon no longer trusts. The connection is
        cut rather than drained: nothing a removed peer holds is owed an
        answer.
        """
        with self._peer_lock:
            closing = [session for session in self._peer_sessions
                       if node_id is None or session.node_id() == node_id]
        for session in closing:
            session.close(wait=False)
        return len(closing)

    def _bind_peer_agent(self, node_id, agent_id, writer):
        """A peer's agent is served the surface a local agent is served."""
        if self._mailboxes is None:
            raise RpcError(UNAUTHORIZED, "peers are not served here")
        if not isinstance(agent_id, str) or not agent_id or \
                len(agent_id) > self._config.comms_agent_id_limit:
            raise RpcError(INVALID_PARAMS, "agentId must be a short name")
        try:
            mailbox = self._mailboxes.bind(
                {"nodeId": node_id, "agentId": agent_id}, self._store)
        except CommsError as exc:
            raise RpcError(CAPACITY if exc.code == "busy" else UNAVAILABLE,
                           str(exc.code)) from None
        return CommsSession(self._config, self._store, self._relay,
                            self._waits, writer, self._operations,
                            {"nodeId": node_id, "agentId": agent_id},
                            mailbox)

    def _agent(self, value):
        if not isinstance(value, dict):
            raise RpcError(INVALID_PARAMS, "an agent is required")
        agent_id = str(value.get("agentId") or "")
        if not agent_id:
            raise RpcError(INVALID_PARAMS, "an agent id is required")
        if len(agent_id) > self._config.comms_agent_id_limit:
            raise RpcError(INVALID_PARAMS, "an agent id must be short")
        if self._mailboxes is not None and self._mailboxes.owns(agent_id):
            raise RpcError(INVALID_PARAMS, "that id belongs to a peer")
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

    def peers_enabled(self):
        return self._peers is not None

    def describe(self, sender):
        """What this node offers the caller, and the mailbox it reads here."""
        mailbox = {"nodeId": self._node_id, "agentId": sender["agentId"]}
        if self._mailboxes is not None:
            bound = self._mailboxes.mailbox(sender)
            if bound is not None:
                mailbox["agentId"] = bound
        return {"nodeId": self._node_id,
                "agent": {"nodeId": sender["nodeId"],
                          "agentId": sender["agentId"]},
                "mailbox": mailbox,
                "headSequence": self._store.head_sequence(
                    mailbox["agentId"]),
                "methods": self._operations.methods(sender),
                "capabilities": self.capabilities(),
                "limits": {"maxWaitMs": int(self._config.comms_wait_max
                                            * 1000),
                           "feedPageBytes":
                               self._config.feed_page_bytes}}

    def forward(self, node_id, agent_id, method, params, timeout=None):
        """One call over a connection opened as the calling agent."""
        if node_id == self._node_id:
            # Mailboxes here hold the mail, and a node does not peer with
            # itself: the same handlers answer as if a bound socket had
            # asked, so locally-held mail is never a peer lookup.
            return self._serve(agent_id, method, params)
        if self._peers is None:
            raise CommsError(BAD_REQUEST)
        channel = None
        try:
            channel = self._peers.open_channel(node_id, agent_id)
            if channel is None:
                raise CommsError(UNKNOWN_PEER)
            return channel.request(method, params, timeout)
        except PeerLinkError as failure:
            # Opening fails with the same reasons a request does: a refused
            # hello and an interrupted call must read alike to the agent.
            raise CommsError(self._forward_failure(failure)) from None
        finally:
            if channel is not None:
                channel.close()

    def _serve(self, agent_id, method, params):
        """One agent method answered here, without opening a connection."""
        sender = {"nodeId": self._node_id, "agentId": agent_id}
        handler = self._operations.handlers(sender).get(method)
        if handler is None:
            raise CommsError(METHOD_UNAVAILABLE)
        return handler(sender, params)

    @staticmethod
    def _forward_failure(failure):
        """An unserved method stays apart from a peer that could not answer."""
        reason = failure.reason.split(":", 1)[0]
        if reason == "method-unavailable":
            return METHOD_UNAVAILABLE
        if reason in LOST_ANSWERS:
            return PEER_LOST_ANSWER
        return PEER_UNREACHABLE

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
            if self._mailboxes is not None and \
                    self._mailboxes.owns(agent_id):
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
                self._store.reserve(agent_id, provisional=spawn_id,
                                    permitted=self._permit())
            except (CommsError, OSError) as failure:
                if not self._forget(sender, request_id, agent_id, spawn_id):
                    raise CommsError(STORE_FAILED) from None
                if isinstance(failure, CommsError):
                    if failure.code == IDENTITY_CLAIMED:
                        raise CommsError(BAD_REQUEST) from None
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
                ENV_PARENT_NODE_ID: sender["nodeId"],
                ENV_SPAWN_ID: spawn_id}

    def _endpoint_env(self):
        return {ENV_HOST: self._endpoint["host"],
                ENV_PORT: str(self._endpoint["port"])}

    @staticmethod
    def _digest(spec):
        fields = ("agentId", "name", "dir", "argv", "task", "cols",
                  "rows", "env", "envOnce")
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
