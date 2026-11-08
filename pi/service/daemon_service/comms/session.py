"""One bound connection: its identity, its subscription, its requests."""

from ..transport.rpc_codec import INVALID_PARAMS, RpcCodec
from ..transport.rpc_error import RpcError

TOPIC_MESSAGE = "comms.message"


class CommsSession:
    """A connection is bound to one identity for its whole life."""

    def __init__(self, config, store, relay, writer, handlers, agent):
        self._config = config
        self._store = store
        self._relay = relay
        self._writer = writer
        self._handlers = dict(handlers)
        self._agent = agent
        self._codec = RpcCodec()
        self._subscription = relay.attach(agent["agentId"], self._notify,
                                          self._disconnect)

    def greeting(self):
        return {
            "service": "agent-comms",
            "version": 1,
            "protocol": RpcCodec.PROTOCOL,
            "methods": self.methods(),
            "topics": [TOPIC_MESSAGE],
            "maxFrameBytes": self._config.frame_bytes,
            "agent": dict(self._agent),
            "cursor": self._store.head_sequence(self._agent["agentId"]),
        }

    def methods(self):
        return sorted(set(self._handlers) | {"rpc.hello", "rpc.cancel",
                                             "rpc.subscribe", "rpc.unsubscribe"})

    def handle(self, request):
        method = request.get("method")
        if method == "rpc.subscribe":
            return {"topics": self._subscription.subscribe(
                self._topics(request))}
        if method == "rpc.unsubscribe":
            return {"topics": self._subscription.unsubscribe(
                self._topics(request))}
        handler = self._handlers.get(method)
        if handler is None:
            raise LookupError(method)
        return handler(self._agent, self._params(request))

    def cancel(self, params):
        """No wait is pending in this build, so nothing is cancelled."""
        return None

    def close(self):
        self._relay.detach(self._subscription)

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

    def _notify(self, topic, params):
        self._writer.send(self._codec.notification(topic, params))

    def _disconnect(self):
        self._writer.close()
