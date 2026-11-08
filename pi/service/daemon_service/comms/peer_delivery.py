"""One peer's worker: bring the link up, retry its mail, probe, back off.

The worker never decides that mail arrived. It records the answer the remote
daemon gave, and keeps the delivery queued whenever the answer is missing.
"""

import threading

from ..transport.rpc_error import RpcError
from .error import CommsError
from .mailbox_store import ACCEPTED, BAD_REQUEST, QUEUED, UNDELIVERABLE
from .peer_link_error import PeerLinkError

DELIVER = "comms.peer.deliver"
PING = "comms.peer.ping"
BATCH = 8
UP = "up"
DOWN = "down"


class PeerDelivery:
    """It owns its peer's link, its tunnel and its retry schedule."""

    def __init__(self, config, store, peer, link, tunnel, credentials,
                 clock=None):
        self._config = config
        self._store = store
        self._peer = peer
        self._link = link
        self._tunnel = tunnel
        self._credentials = credentials
        self._generation = peer["generation"]
        self._node_id = peer["nodeId"]
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._state = DOWN
        self._last_error = None
        self._backoff = config.comms_peer_reconnect_initial

    def start(self):
        self._thread = threading.Thread(target=self._run,
                                        name=f"peer-{self._node_id}",
                                        daemon=True)
        self._thread.start()
        return self

    def wake(self):
        self._wake.set()

    def snapshot(self):
        with self._lock:
            return {"state": self._state, "lastError": self._last_error}

    def stop(self):
        """Stop the link, unblock the worker, then reap tunnel and thread."""
        self._stop.set()
        self._wake.set()
        self._link.close()
        self._tunnel.close()
        if self._thread is not None:
            self._thread.join(self._config.stop_grace)

    def generation(self):
        return self._generation

    # -- the worker --------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            if not self._connect():
                self._pause()
                continue
            self._serve()
            self._teardown()
            if not self._stop.is_set():
                self._mark(DOWN, "link-closed")
                self._pause()

    def _connect(self):
        try:
            address = self._tunnel.start(self._peer)
            credential = self._credentials.outbound_credential(self._peer)
            self._link.open(address, self._peer, credential,
                            self._config.comms_peer_request_timeout)
        except (OSError, ValueError, PeerLinkError) as failure:
            self._teardown()
            self._mark(DOWN, str(failure) or "link-down")
            return False
        except Exception as failure:                     # noqa: BLE001
            self._teardown()
            self._mark(DOWN, str(failure) or "link-down")
            return False
        with self._lock:
            self._backoff = self._config.comms_peer_reconnect_initial
        self._mark(UP, None)
        return True

    def _serve(self):
        while not self._stop.is_set() and self._link.alive():
            if not self._drain():
                return
            if self._wake.wait(self._config.comms_peer_probe_interval):
                self._wake.clear()
                continue
            if not self._probe():
                return

    def _drain(self):
        """Attempt every due record; stop at the first link failure."""
        try:
            records = self._store.due(self._node_id, self._generation, BATCH)
        except CommsError as failure:
            self._mark(DOWN, failure.code)
            return False
        for record in records:
            if self._stop.is_set():
                return False
            delivery = record.get("delivery")
            if not isinstance(delivery, dict):
                continue
            delivery_id = delivery.get("deliveryId")
            if not delivery_id:
                continue
            deadline = (self._store.now() +
                        self._config.comms_peer_request_timeout)
            if not self._store.mark_attempt(delivery_id, self._generation,
                                            deadline):
                continue
            try:
                answer = self._link.request(
                    DELIVER, {"delivery": delivery},
                    self._config.comms_peer_request_timeout)
            except RpcError as failure:
                # The hop itself was refused: the record stays queued.
                self._mark(UP, f"refused: {failure.code}")
                return True
            except PeerLinkError as failure:
                self._mark(DOWN, failure.reason)
                return False
            outcome = self._outcome(delivery_id, answer)
            if outcome is None:
                self._mark(DOWN, "unexpected-answer")
                return False
            try:
                self._store.settle_peer(delivery_id, self._generation,
                                        outcome)
            except CommsError as failure:
                self._mark(UP, f"settle-failed: {failure.code}")
                return True
        return True

    def _probe(self):
        try:
            answer = self._link.request(PING, {},
                                        self._config.comms_peer_request_timeout)
        except (PeerLinkError, RpcError) as failure:
            self._mark(DOWN, getattr(failure, "reason", None) or "probe-failed")
            return False
        if not isinstance(answer, dict) or answer.get("nodeId") != self._node_id:
            self._mark(DOWN, "wrong-node")
            return False
        return True

    def _teardown(self):
        self._link.close()
        self._tunnel.close()

    def _pause(self):
        delay = self._backoff
        with self._lock:
            self._backoff = min(self._backoff * 2,
                                self._config.comms_peer_reconnect_max)
        self._stop.wait(delay)

    def _mark(self, state, reason):
        with self._lock:
            self._state = state
            self._last_error = reason

    @staticmethod
    def _outcome(delivery_id, answer):
        """The remote daemon's answer, or None when it is not one."""
        if not isinstance(answer, dict) or \
                answer.get("deliveryId") != delivery_id:
            return None
        status = answer.get("status")
        if status == ACCEPTED:
            return {"status": ACCEPTED, "deliveryId": delivery_id,
                    "messageId": answer.get("messageId"),
                    "sequence": answer.get("sequence")}
        if status == UNDELIVERABLE:
            return {"status": UNDELIVERABLE, "deliveryId": delivery_id,
                    "reason": answer.get("reason") or BAD_REQUEST}
        if status == QUEUED:
            return None
        if status == "expired":
            return {"status": UNDELIVERABLE, "deliveryId": delivery_id,
                    "reason": "expired"}
        return None
