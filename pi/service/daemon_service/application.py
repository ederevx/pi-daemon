"""Composition of the unified service: start in order, stop in reverse."""

import os
import secrets
import signal
import threading

from .comms.mailbox_store import MailboxStore
from .comms.node_identity import NodeIdentity
from .comms.peer_auth import PeerAuthenticator
from .comms.peer_config_store import PeerConfigStore
from .comms.peer_manager import PeerManager
from .comms.service import CommsService
from .comms.spawn_ledger import SpawnLedger
from .config import ServiceConfig
from .lifecycle.instance_coordinator import InstanceCoordinator
from .runtime_layout import RuntimeLayout
from .sessions.identity import ProcessIdentity
from .sessions.launcher import SessionLauncher
from .sessions.operations import SessionOperations
from .sessions.pty_backend import PosixPtyBackend, service_root
from .sessions.relay import SessionRelay
from .sessions.runner import SessionRunner
from .sessions.service import SessionService
from .sessions.store import SessionStore
from .tasks.operations import TaskOperations
from .tasks.runner import TicketRunner
from .tasks.service import TaskService
from .tasks.store import TicketStore
from .transport.control_endpoint import ControlEndpoint
from .transport.control_handshake import ControlHandshake
from .transport.control_router import ControlRouter
from .transport.rpc_codec import RpcCodec
from .transport.rpc_listener import RpcListener
from .wire import WireCodec


class DaemonApplication:
    """It composes collaborators and nothing else; domains own their state."""

    def __init__(self, layout, config):
        self._layout = layout
        self._config = config
        self._stopping = threading.Event()
        self._mode = "shutdown"
        self._coordinator = InstanceCoordinator(layout.instance_lock_path())
        self._instance = None
        self._store = None
        self._runner = None
        self._tasks = None
        self._sessions = None
        self._session_store = None
        self._session_runner = None
        self._relay = None
        self._endpoint = None
        self._comms = None
        self._comms_store = None
        self._comms_listener = None
        self._comms_spawns = None
        self._comms_peers = None
        self._node_identity = None

    @classmethod
    def from_environment(cls, environ=None):
        environment = dict(os.environ if environ is None else environ)
        layout = RuntimeLayout(environment.get("PI_DAEMON_STATE_DIR"),
                               environment.get("PI_DAEMON_RUNTIME_DIR"))
        config = ServiceConfig.load(settings_path=layout.settings_path(),
                                    environ=environment)
        return cls(layout, config)

    def config(self):
        return self._config

    def endpoint_path(self):
        return self._layout.endpoint_path()

    def start(self):
        self._install_signals()
        self._instance = self._coordinator.acquire()
        try:
            store = TicketStore(self._layout.tickets_path(),
                                self._layout.logs_dir(), self._config)
            store.load()
            runner = TicketRunner(store, self._config)
            self._store, self._runner = store, runner
            tasks = TaskService(store, runner)
            self._tasks = tasks
            sessions = self._compose_sessions()
            # Comms composes its launcher over the sessions service, so the
            # owner must be visible before comms is built.
            self._sessions = sessions
            handlers = TaskOperations(tasks).handlers()
            handlers.update(SessionOperations(
                sessions, self.request_stop, self._config).handlers())
            token = secrets.token_hex(16)
            self._compose_comms(token)
            codec = WireCodec()
            endpoint = ControlEndpoint(
                self._layout, self._config, ControlHandshake(token),
                ControlRouter(handlers), codec, self._instance, token,
            )
            self._endpoint = endpoint
            endpoint.start()
        except BaseException:
            self._unwind()
            raise
        return self

    def _compose_sessions(self):
        store = SessionStore(self._layout.sessions_path(),
                             self._layout.run_state_dir(), self._config)
        store.load()
        backend = PosixPtyBackend(self._config, service_root())
        relay = SessionRelay(self._config, backend)
        relay.start()
        self._session_store, self._relay = store, relay
        owner = ProcessIdentity.current(os.getpid()).to_dict()
        owner["instance"] = self._instance
        runner = SessionRunner(store, relay, self._config, backend, owner)
        runner.recover()
        self._session_runner = runner
        return SessionService(store, runner, relay, self._config)

    def _compose_comms(self, token):
        """Every owner is held before it starts, so unwind can stop it."""
        identity = NodeIdentity(self._layout.comms_node_path(),
                                self._config).load()
        self._node_identity = identity
        store = MailboxStore(self._layout.comms_mailbox_path(), self._config,
                             identity.id())
        self._comms_store = store
        spawns = SpawnLedger(self._layout.comms_spawns_path(), self._config,
                             identity.id())
        self._comms_spawns = spawns
        launcher = SessionLauncher(self._sessions, self._config)
        service = CommsService(store, self._config, token, identity.id(),
                               spawns, launcher)
        peers = PeerConfigStore(self._layout.comms_peers_path(), self._config)
        self._comms_peers = peers
        manager = PeerManager(self._config, store, peers,
                              PeerAuthenticator(self._config, peers),
                              service.deliver_peer, identity.id())
        service.use_peers(manager)
        self._comms = service
        service.start()
        listener = RpcListener(self._config, RpcCodec(), service,
                               port=self._config.comms_port)
        self._comms_listener = listener
        listener.start()
        host, port = listener.address()
        service.serve_at(host, port)
        return service, listener

    def run(self):
        self.start()
        try:
            self._stopping.wait()
        finally:
            self.stop()
        return 0

    def request_stop(self, mode="shutdown"):
        """Ask the run to end; the mode names what the sessions become."""
        self._mode = mode
        self._stopping.set()

    def stop(self):
        if self._tasks is not None:
            self._tasks.begin_shutdown()
        if self._sessions is not None:
            self._sessions.begin_shutdown(self._mode)
        if self._comms is not None:
            self._comms.begin_shutdown()
        if self._comms_listener is not None:
            self._comms_listener.stop()
        if self._endpoint is not None:
            self._endpoint.stop()
        if self._sessions is not None:
            self._sessions.stop_all()
        if self._tasks is not None:
            self._tasks.stop()
        if self._relay is not None:
            self._relay.stop()
        if self._store is not None:
            self._store.save()
        if self._comms is not None:
            self._comms.stop()
        if self._session_store is not None:
            self._session_store.save()
        self._coordinator.release()
        self._endpoint = None
        self._tasks = None
        self._runner = None
        self._store = None
        self._sessions = None
        self._session_store = None
        self._session_runner = None
        self._relay = None
        self._comms = None
        self._comms_store = None
        self._comms_listener = None
        self._comms_peers = None
        self._node_identity = None

    def _unwind(self):
        try:
            self.stop()
        except (OSError, RuntimeError):
            pass

    def _install_signals(self):
        if threading.current_thread() is not threading.main_thread():
            return
        for number in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(number, self._on_signal)
            except (OSError, ValueError):
                pass

    def _on_signal(self, number, frame):
        self.request_stop()
