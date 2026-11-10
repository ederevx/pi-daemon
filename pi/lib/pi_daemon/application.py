"""Composition of the unified service: start in order, stop in reverse."""

import os
import secrets
import shutil
import signal
import sys
import threading

import pi_platform

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
from .sessions.carry import SessionCarry
from .sessions.handover import SessionHandover
from .sessions.launcher import SessionLauncher
from .sessions.operations import SessionOperations
from .sessions.pi_command import PiCommand
from .sessions.reload import SessionReload
from .sessions.relay import SessionRelay
from .sessions.runner import SessionRunner
from .sessions.service import SessionService
from .sessions.store import SessionStore
from .tasks.operations import TaskOperations
from .tasks.runner import TicketRunner
from .tasks.service import TaskService
from .tasks.store import TicketStore
from .transcripts.operations import TranscriptOperations
from .transcripts.service import TranscriptService
from .transport.control_endpoint import ControlEndpoint
from .transport.control_handshake import ControlHandshake
from .transport.control_router import ControlRouter
from .transport.endpoint_file import EndpointFile
from .transport.rpc_codec import RpcCodec
from .transport.rpc_listener import RpcListener
from .transport.service_descriptor import ServiceDescriptor
from .wire import CAPABILITIES, WireCodec


class DaemonApplication:
    """It composes collaborators and nothing else; domains own their state."""

    def __init__(self, layout, config, services_root=None,
                 session_store_root=None, environment=None):
        # An empty root is not "no store": pi_transcript reads it as no
        # containment at all, so an unresolvable root is refused here
        # rather than served from anywhere on the disk.
        root = session_store_root or config.session_store_root
        if not root:
            raise ValueError("session_store_root is required")
        self._layout = layout
        self._config = config
        self._services_root = services_root
        self._session_store_root = os.path.abspath(root)
        self._environment = dict(environment or {})
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
        self._transcripts = None
        self._endpoint = None
        self._comms = None
        self._comms_store = None
        self._comms_listener = None
        self._comms_spawns = None
        self._comms_peers = None
        self._node_identity = None
        self._comms_file = None
        self._comms_descriptor = None
        self._control_descriptor = None

    @classmethod
    def from_environment(cls, environ=None, services_root=None):
        """A services root is published only when one is given, in
        argument or in PI_SERVICES_DIR, so an embedder or a test never
        writes into the directory a real client reads."""
        environment = dict(os.environ if environ is None else environ)
        layout = RuntimeLayout(environment.get("PI_DAEMON_STATE_DIR"),
                               environment.get("PI_DAEMON_RUNTIME_DIR"))
        config = ServiceConfig.load(settings_path=layout.settings_path(),
                                    environ=environment)
        if services_root is None:
            services_root = environment.get("PI_SERVICES_DIR")
        # The conversation store is resolved from the environment this
        # application was given, never from the process's own, so an
        # embedder's agent home is the one it reads.
        store_root = config.session_store_root or os.path.join(
            pi_platform.RuntimeLayout(environment).agent_home(), "sessions")
        return cls(layout, config, services_root, store_root, environment)

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
            self._transcripts = self._compose_transcripts(sessions)
            handlers.update(
                TranscriptOperations(self._transcripts).handlers())
            token = secrets.token_hex(16)
            self._compose_comms(token)
            codec = WireCodec()
            endpoint = ControlEndpoint(
                self._layout, self._config,
                ControlHandshake(token, CAPABILITIES),
                ControlRouter(handlers), codec, self._instance, token,
            )
            self._endpoint = endpoint
            endpoint.start()
            self._publish_control()
        except BaseException:
            self._unwind()
            raise
        return self

    def _compose_sessions(self):
        store = SessionStore(self._layout.sessions_path(),
                             self._layout.run_state_dir(), self._config)
        store.load()
        backend = pi_platform.select_pty_backend(self._config)
        relay = SessionRelay(self._config)
        relay.start()
        self._session_store, self._relay = store, relay
        owner = ProcessIdentity.current(os.getpid()).to_dict()
        owner["instance"] = self._instance
        runner = SessionRunner(store, relay, self._config, backend, owner,
                               state_root=self._layout.state_root())
        runner.recover()
        self._session_runner = runner
        command = PiCommand(self._environment)
        carry = SessionCarry(store, runner, relay, command)
        handover = SessionHandover(store, runner, command, self._config)
        reload = SessionReload(self._layout.state_root(),
                               grace=self._config.reload_grace,
                               poll=self._config.reload_poll)
        # A request a previous run left behind names a session that is gone.
        reload.purge()
        return SessionService(store, runner, relay, self._config, carry,
                              handover, reload)

    def _compose_transcripts(self, sessions):
        """The conversation index reads pi's own store; the root was
        resolved once, from the environment this application was given."""
        service = TranscriptService(self._config, self._session_store_root,
                                    sessions.live_files,
                                    sessions.recorded_file)
        service.start()
        return service

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
        credentials = self._layout.comms_peer_credentials_path()
        manager = PeerManager(
            self._config, store, peers,
            PeerAuthenticator(self._config, peers, directory=credentials),
            service.deliver_peer, identity.id())
        service.use_peers(manager, credentials=credentials)
        self._comms = service
        service.start()
        listener = RpcListener(self._config, RpcCodec(), service,
                               port=self._config.comms_port)
        self._comms_listener = listener
        listener.start()
        host, port = listener.address()
        service.serve_at(host, port)
        self._publish_comms(host, port, token, identity)
        return service, listener

    # -- discovery ---------------------------------------------------------

    def _publish_control(self):
        """The session-host entry: clients find the control wire here."""
        if self._services_root is None:
            return
        descriptor = ServiceDescriptor(
            self._services_root, "session-host", "pi-daemon",
            self._config.file_mode, self._config.write_retries,
            self._config.write_retry_delay)
        self._control_descriptor = descriptor
        descriptor.publish("pi-pty-host/1", 1, self._layout.endpoint_path(),
                           self._activation_argv(), self._instance,
                           os.getpid())

    def _publish_comms(self, host, port, token, identity):
        """The comms endpoint file first, then its directory entry, so a
        published entry always names a port that answers."""
        endpoint = EndpointFile(
            self._layout.comms_endpoint_path(), self._config.file_mode,
            self._config.write_retries, self._config.write_retry_delay)
        self._comms_file = endpoint
        endpoint.publish({"service": "agent-comms", "proto": "pi-rpc/1",
                          "host": host, "port": port, "token": token,
                          "nodeId": identity.id(),
                          "instance": self._instance, "pid": os.getpid()})
        if self._services_root is None:
            return
        descriptor = ServiceDescriptor(
            self._services_root, "agent-comms", "pi-daemon",
            self._config.file_mode, self._config.write_retries,
            self._config.write_retry_delay)
        self._comms_descriptor = descriptor
        descriptor.publish("pi-rpc/1", 1, endpoint.path(),
                           self._activation_argv("daemon-start"),
                           self._instance, os.getpid())

    @staticmethod
    def _activation_argv(command="daemon-start"):
        """The client that starts a stopped service, resolved the way the
        repository locates helper scripts: beside the entry, else PATH."""
        entry = os.path.dirname(os.path.realpath(sys.argv[0]))
        candidates = [os.path.join(entry, "pi-rc"),
                      os.path.join(os.path.dirname(entry), "bin", "pi-rc")]
        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return [candidate, command]
        found = shutil.which("pi-rc")
        return [found, command] if found else ["pi-rc", command]

    def _unpublish(self):
        """Discovery goes dark first: a client never dials a closing port."""
        for descriptor in (self._control_descriptor,
                           self._comms_descriptor):
            if descriptor is not None:
                descriptor.remove_owned(self._instance, os.getpid())
        if self._comms_file is not None:
            self._comms_file.remove_owned(self._instance)
        self._control_descriptor = None
        self._comms_descriptor = None
        self._comms_file = None

    def run(self):
        self.start()
        try:
            self._stopping.wait()
        finally:
            self.stop()
        return 0

    def request_stop(self, mode="shutdown", cause="request"):
        """Ask the run to end; the mode names what the sessions become.

        The cause goes to stderr, which is the unit's journal, so a stop no
        supervisor asked for still names its origin.
        """
        self._note_stop(mode, cause)
        self._mode = mode
        self._stopping.set()

    @staticmethod
    def _note_stop(mode, cause):
        try:
            sys.stderr.write("pi-daemon: stopping (mode=%s, cause=%s)\n"
                             % (mode, cause))
            sys.stderr.flush()
        except Exception:
            pass

    def stop(self):
        self._unpublish()
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
        if self._transcripts is not None:
            self._transcripts.stop()
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
        self._transcripts = None
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
        self.request_stop(cause="signal %s" % signal.Signals(number).name)
