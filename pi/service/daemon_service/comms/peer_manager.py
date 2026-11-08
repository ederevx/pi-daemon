"""The configured peers and their workers: admission, routing, removal.

Configuration commits before anything connects, so a peer can be added while
it is down. Admission of an inbound delivery checks the generation that
authorised it, and removal settles what that generation still owed before
the worker's sockets are closed.
"""

import threading

from .error import CommsError
from .mailbox_store import PEER_REMOVED
from .peer_auth import PeerAuthenticator
from .peer_config_store import ACTIVE, PeerConfigStore
from .peer_delivery import PeerDelivery
from .peer_link import PeerLink
from .peer_tunnel import PeerTunnel

CLOSING = "closing"
STALE_PEER = "peer-stale"
CONFIG_CONFLICT = "peer-config-conflict"


class PeerManager:
    """It owns the peer records and one worker per configured peer."""

    def __init__(self, config, store, peers, authenticator, accept, node_id,
                 clock=None, tunnel_factory=None, link_factory=None):
        self._config = config
        self._store = store
        self._peers = peers
        self._auth = authenticator or PeerAuthenticator(config, peers)
        self._accept = accept
        self._node_id = node_id
        self._clock = clock
        self._tunnel_factory = tunnel_factory or (
            lambda: PeerTunnel(config))
        self._link_factory = link_factory or (
            lambda: PeerLink(config, node_id, clock=clock))
        self._lock = threading.Lock()
        self._workers = {}
        self._closing = False

    def start(self):
        self._peers.load()
        for record in self._peers.removing():
            self._store.cancel_peer(record["nodeId"], record["generation"],
                                    PEER_REMOVED)
            self._peers.finish_remove(record["nodeId"])
        active = {(record["nodeId"], record["generation"])
                  for record in self._peers.list() if record["state"] == ACTIVE}
        # Mail a previous process queued for a generation this one no longer
        # reaches is settled now, or no worker would ever look at it.
        self._store.cancel_orphans(active, PEER_REMOVED)
        for record in self._peers.list():
            if record["state"] == ACTIVE:
                self._spawn(record)
        return self

    def begin_shutdown(self):
        """Every worker is signalled first; none waits on another's socket."""
        with self._lock:
            self._closing = True
            workers = list(self._workers.values())
        for worker in workers:
            worker.signal()
        for worker in workers:
            worker.stop()
        with self._lock:
            self._workers = {}

    def stop(self):
        self.begin_shutdown()
        self._peers.save()

    # -- admission ---------------------------------------------------------

    def route(self, node_id):
        """Where a foreign address goes, or None when no peer is configured."""
        generation = self._peers.active_generation(node_id)
        if generation is None:
            return None
        return {"nodeId": node_id, "generation": generation}

    def deliver(self, peer_node_id, generation, delivery):
        """An authenticated peer's mail, admitted only for its own generation."""
        if self._closing:
            raise CommsError(CLOSING)
        record = self._peers.get(peer_node_id)
        if record is None or record["state"] != ACTIVE or \
                record["generation"] != generation:
            raise CommsError(STALE_PEER)
        return self._accept(peer_node_id, generation, delivery)

    def verify(self, node_id, credential):
        return self._auth.verify(node_id, credential)

    def generation(self, node_id):
        return self._peers.active_generation(node_id)

    def wake(self, node_id):
        with self._lock:
            worker = self._workers.get(node_id)
        if worker is not None:
            worker.wake()

    # -- administration ----------------------------------------------------

    def configure(self, spec):
        with self._lock:
            if self._closing:
                raise CommsError(CLOSING)
            added = self._peers.add(spec)
            record = added["peer"]
            created = added["created"]
            conflict = added["conflict"]
            if created:
                self._spawn(record)
        if conflict:
            raise CommsError(CONFIG_CONFLICT)
        if created:
            self.wake(record["nodeId"])
        return {"status": "configured", "created": created,
                "peer": self._view(record)}

    def remove(self, node_id):
        with self._lock:
            record = self._peers.get(node_id)
            if record is None:
                return {"status": "not-found", "nodeId": node_id}
            removing = self._peers.begin_remove(node_id)
            worker = self._workers.pop(node_id, None)
        try:
            # The generation is already removing, so no import is admitted;
            # stopping the worker first means no attempt starts behind us.
            if worker is not None:
                worker.stop()
            counts = self._store.cancel_peer(node_id,
                                             removing["generation"],
                                             PEER_REMOVED)
            self._peers.finish_remove(node_id)
        finally:
            if worker is not None:
                worker.stop()
        return {"status": "removed", "nodeId": node_id,
                "undeliverableCount": counts["undeliverable"],
                "unknownCount": counts["unknown"]}

    def list(self):
        return {"peers": [self._view(record) for record in self._peers.list()]}

    # -- internals ---------------------------------------------------------

    def _spawn(self, record):
        worker = PeerDelivery(
            self._config, self._store, dict(record),
            self._link_factory(), self._tunnel_factory(), self._auth,
            clock=self._clock)
        self._workers[record["nodeId"]] = worker
        worker.start()
        return worker

    def _view(self, record):
        with self._lock:
            worker = self._workers.get(record["nodeId"])
        state = worker.snapshot() if worker is not None else {
            "state": "down", "lastError": None}
        queued = self._store.queued(record["nodeId"], record["generation"])
        return {"nodeId": record["nodeId"],
                "sshTarget": record["sshTarget"],
                "remotePort": record["remotePort"],
                "localPort": record["localPort"],
                "state": record["state"] if record["state"] != ACTIVE
                else state["state"],
                "queued": queued,
                "lastError": state["lastError"]}
