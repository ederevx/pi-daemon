"""One connection's ordered queue of live notifications."""

import threading

from collections import deque


class CommsSubscription:
    """Bounded and ordered, and honest about falling behind.

    A subscriber whose queue is full is disconnected rather than served a
    gap: its durable mailbox, replayed from its cursor, is the truth.
    """

    def __init__(self, agent_id, capacity, on_overflow):
        self._agent_id = agent_id
        self._capacity = max(1, int(capacity))
        self._on_overflow = on_overflow
        self._topics = set()
        self._queue = deque()
        self._condition = threading.Condition()
        self._closed = False

    def agent_id(self):
        return self._agent_id

    def subscribe(self, topics):
        with self._condition:
            self._topics.update(str(topic) for topic in topics)
            return sorted(self._topics)

    def unsubscribe(self, topics):
        with self._condition:
            self._topics.difference_update(str(topic) for topic in topics)
            return sorted(self._topics)

    def topics(self):
        with self._condition:
            return sorted(self._topics)

    def wants(self, topic):
        with self._condition:
            return topic in self._topics

    def offer(self, topic, params):
        """Enqueue one notification; False means the subscriber fell behind."""
        with self._condition:
            if self._closed or topic not in self._topics:
                return True
            if len(self._queue) >= self._capacity:
                self._closed = True
                overflowed = True
            else:
                self._queue.append((topic, params))
                self._condition.notify()
                overflowed = False
        if overflowed:
            self._on_overflow()
            return False
        return True

    def take(self):
        with self._condition:
            if not self._queue:
                return None
            return self._queue.popleft()

    def close(self):
        with self._condition:
            self._closed = True
            self._queue.clear()
            self._condition.notify_all()
