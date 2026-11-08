"""Live notification delivery: subscriptions, order, and overflow policy."""

import threading

from .subscription import CommsSubscription

MESSAGE_TOPIC = "comms.message"


class MessageRelay:
    """Subscriptions live as long as their connection; mail does not."""

    def __init__(self, config):
        self._capacity = config.comms_notification_queue
        self._tick = config.relay_tick
        self._lock = threading.Lock()
        self._subscriptions = []
        self._thread = None
        self._stopping = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="comms-relay",
                                        daemon=True)
        self._thread.start()
        return self

    def attach(self, agent_id, sink, on_overflow):
        """One connection's queue; `sink(topic, params)` writes the frame."""
        subscription = CommsSubscription(agent_id, self._capacity,
                                         on_overflow)
        with self._lock:
            self._subscriptions.append((subscription, sink))
        return subscription

    def detach(self, subscription):
        with self._lock:
            self._subscriptions = [
                (existing, sink) for existing, sink in self._subscriptions
                if existing is not subscription
            ]
        subscription.close()

    def publish(self, envelopes):
        """Offer accepted mail to every subscribed connection, in order."""
        if not envelopes:
            return
        with self._lock:
            targets = list(self._subscriptions)
        for envelope in envelopes:
            recipient = envelope["to"]["agentId"]
            for subscription, sink in targets:
                if subscription.agent_id() != recipient:
                    continue
                try:
                    subscription.offer(MESSAGE_TOPIC, envelope)
                except OSError:
                    pass

    def _run(self):
        while not self._stopping.is_set():
            delivered = False
            with self._lock:
                targets = list(self._subscriptions)
            for subscription, sink in targets:
                item = subscription.take()
                while item is not None:
                    topic, params = item
                    try:
                        sink(topic, params)
                    except OSError:
                        self.detach(subscription)
                        break
                    delivered = True
                    item = subscription.take()
            if not delivered:
                self._stopping.wait(self._tick)

    def stop(self):
        self._stopping.set()
        with self._lock:
            targets = list(self._subscriptions)
            self._subscriptions = []
        for subscription, _ in targets:
            subscription.close()
        if self._thread is not None:
            self._thread.join(self._tick * 10)
            self._thread = None
