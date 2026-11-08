"""Composition of the unified service: start in order, stop in reverse."""

import os
import secrets
import signal
import threading

from .config import ServiceConfig
from .lifecycle.instance_coordinator import InstanceCoordinator
from .runtime_layout import RuntimeLayout
from .tasks.operations import TaskOperations
from .tasks.runner import TicketRunner
from .tasks.service import TaskService
from .tasks.store import TicketStore
from .transport.control_endpoint import ControlEndpoint
from .transport.control_handshake import ControlHandshake
from .transport.control_router import ControlRouter
from .wire import WireCodec


class DaemonApplication:
    """It composes collaborators and nothing else; domains own their state."""

    def __init__(self, layout, config):
        self._layout = layout
        self._config = config
        self._stopping = threading.Event()
        self._coordinator = InstanceCoordinator(layout.instance_lock_path())
        self._instance = None
        self._store = None
        self._runner = None
        self._tasks = None
        self._endpoint = None

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
            tasks = TaskService(store, runner)
            handlers = TaskOperations(tasks).handlers()
            token = secrets.token_hex(16)
            codec = WireCodec()
            endpoint = ControlEndpoint(
                self._layout, self._config, ControlHandshake(token),
                ControlRouter(handlers), codec, self._instance, token,
            )
            endpoint.start()
        except BaseException:
            self._unwind()
            raise
        self._store, self._runner = store, runner
        self._tasks, self._endpoint = tasks, endpoint
        return self

    def run(self):
        self.start()
        try:
            self._stopping.wait()
        finally:
            self.stop()
        return 0

    def request_stop(self):
        self._stopping.set()

    def stop(self):
        if self._tasks is not None:
            self._tasks.begin_shutdown()
        if self._endpoint is not None:
            self._endpoint.stop()
        if self._tasks is not None:
            self._tasks.stop()
        if self._store is not None:
            self._store.save()
        self._coordinator.release()
        self._endpoint = None
        self._tasks = None
        self._runner = None
        self._store = None

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
