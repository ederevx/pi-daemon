"""One bound connection: its identity, its topics, its requests."""

import threading

from ..transport.rpc_codec import INVALID_PARAMS, UNAVAILABLE, RpcCodec
from ..transport.rpc_error import RpcError
from .error import CommsError
from .message_relay import MESSAGE_TOPIC as TOPIC_MESSAGE
from .operations import CODES

# A mailbox answers for the mail it holds; everything else answers as the
# agent that asked, so a remote caller keeps its own address.
MAILBOX_METHODS = frozenset({"comms.replay", "comms.ack"})


class CommsSession:
    """A connection is bound to one identity for its whole life."""

    def __init__(self, config, store, relay, waits, writer, operations, agent,
                 mailbox=None):
        self._config = config
        self._store = store
        self._relay = relay
        self._waits = waits
        self._writer = writer
        self._operations = operations
        self._handlers = operations.handlers(agent)
        self._agent = agent
        self._mailbox = mailbox or agent
        self._codec = RpcCodec()
        self._subscribed = set()
        self._topics_lock = threading.Lock()
        relay.attach(self)

    def agent_id(self):
        """The mailbox this connection reads, and is waited on through."""
        return self._mailbox["agentId"]

    def greeting(self):
        return {
            "service": "agent-comms",
            "version": 1,
            "protocol": RpcCodec.PROTOCOL,
            "methods": self.methods(),
            "topics": [TOPIC_MESSAGE],
            "maxFrameBytes": self._config.frame_bytes,
            "agent": dict(self._agent),
            "mailbox": dict(self._mailbox),
            "headSequence": self._store.head_sequence(self.agent_id()),
            "limits": {"maxWaitMs": int(self._config.comms_wait_max * 1000)},
        }

    def methods(self, sender=None):
        """What this connection answers, as the caller it was admitted for."""
        return self._operations.methods(sender or self._agent)

    def handle(self, request):
        method = request.get("method")
        if method == "rpc.subscribe":
            return {"topics": self._change_topics(self._topics(request), True)}
        if method == "rpc.unsubscribe":
            return {"topics": self._change_topics(self._topics(request),
                                                  False)}
        if method == "comms.wait":
            return self._wait(request)
        handler = self._handlers.get(method)
        if handler is None:
            raise LookupError(method)
        identity = self._mailbox if method in MAILBOX_METHODS else self._agent
        return handler(identity, self._params(request))

    def deliver(self, envelope):
        """Offer one envelope; unsent mail is replayed from the mailbox."""
        if not self._wants():
            return
        try:
            self._writer.send(self._codec.notification(TOPIC_MESSAGE, envelope))
        except OSError:
            self._end(False)
        except RpcError:
            # The envelope cannot fit a frame; the stored mail is replayed.
            return

    def cancel(self, params):
        """`rpc.cancel` is a notification naming one request id.

        A malformed id is ignored: the profile makes cancellation advisory,
        and the caller's own deadline decides.
        """
        if not isinstance(params, dict):
            return None
        request_id = params.get("id")
        if isinstance(request_id, bool) or \
                not isinstance(request_id, (str, int)):
            return None
        self._waits.cancel(self, request_id)
        return None

    def complete(self, request_id, result):
        """Answer a wait the registry finished. Nobody else sends it."""
        try:
            reply = self._operations.wait_result(self._mailbox, result)
        except RpcError as exc:
            self._send(self._codec.failure(request_id, exc.code, exc.message))
            return
        try:
            self._writer.send(self._codec.result(request_id, reply))
        except RpcError as exc:
            self._send(self._codec.failure(request_id, exc.code, exc.message))
        except OSError:
            self._end(False)

    def refuse(self, request_id, code, message):
        self._send(self._codec.failure(request_id, code, message))

    def close(self, wait=True):
        """A session is its connection: closing one ends both.

        A local connection's loop closes it on the way out, and removal ends
        a peer's while it is still held: the wait it was holding, the topics
        it took and the socket itself go together. `wait=False` is for a
        caller that already knows the peer is gone and will not be drained.
        """
        self._end(wait)

    def _wait(self, request):
        """Register a wait. The registry answers it from another thread."""
        wait = self._operations.wait_request(self._mailbox,
                                            self._params(request))
        try:
            self._waits.begin(self, request.get("id"), self.agent_id(),
                              wait["cursor"], wait["limit"], wait["timeout"])
        except CommsError as exc:
            raise RpcError(CODES.get(exc.code, UNAVAILABLE),
                           str(exc.code)) from None
        return None

    def _send(self, message):
        try:
            self._writer.send(message)
        except RpcError:
            pass
        except OSError:
            self._end(False)

    def detach(self):
        """Release what this connection took, without touching its socket.

        A greeting that is refused is answered on the socket it arrived on,
        and that answer is the connection loop's to write: the domain state
        goes here, the socket there.
        """
        self._waits.drop(self)
        self._relay.detach(self)

    def _end(self, wait=True):
        self.detach()
        self._writer.close(wait=wait)

    def _wants(self):
        with self._topics_lock:
            return TOPIC_MESSAGE in self._subscribed

    def _change_topics(self, topics, subscribed):
        with self._topics_lock:
            if subscribed:
                self._subscribed.update(topics)
            else:
                self._subscribed.difference_update(topics)
            return sorted(self._subscribed)

    def _topics(self, request):
        params = self._params(request)
        topics = params.get("topics")
        if not isinstance(topics, list) or not all(
                isinstance(topic, str) for topic in topics):
            raise RpcError(INVALID_PARAMS, "topics must be strings")
        return [topic for topic in topics if topic == TOPIC_MESSAGE]

    @staticmethod
    def _params(request):
        params = request.get("params")
        if params is None:
            return {}
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        return params
