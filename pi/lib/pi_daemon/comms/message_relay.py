"""Live notification delivery: which connections hear accepted mail.

Delivery is an enqueue into the connection's own outbound queue, so the
publisher never writes to a socket and never waits for a slow peer.
"""

import threading

# The one topic a connection can subscribe to; every envelope rides it.
MESSAGE_TOPIC = "comms.message"


class MessageRelay:
    """Subscriptions live as long as their connection; mail does not."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = []

    def attach(self, session):
        with self._lock:
            self._sessions.append(session)

    def detach(self, session):
        with self._lock:
            self._sessions = [existing for existing in self._sessions
                              if existing is not session]

    def publish(self, envelopes):
        """Offer accepted mail, in sequence order, to bound connections."""
        if not envelopes:
            return
        with self._lock:
            sessions = list(self._sessions)
        for envelope in envelopes:
            recipient = envelope["to"]["agentId"]
            for session in sessions:
                if session.agent_id() == recipient:
                    session.deliver(envelope)
