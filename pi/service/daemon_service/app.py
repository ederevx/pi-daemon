"""The unified service: composition root and lifecycle.

One process serves one state root. It composes every domain, publishes
the control endpoint, and owns the shutdown that stops each domain.
"""

import secrets
import threading

from .config import ServiceConfig
from .endpoint import Endpoint, probe
from .router import Router
from .state import ServiceState
from .tasks.control import TaskControl
from .tasks.service import TaskService

SERVICE_NAME = "pi-daemon"
WIRE_VERSION = 1


class Service:
    """Owns the process: state, config, domains, transport, and the tick."""

    def __init__(self, state=None, config=None, token=None):
        self.state = state or ServiceState()
        self.state.ensure()
        self.config = config or ServiceConfig(self.state)
        self.stopping = threading.Event()
        self.tasks = TaskService(self.state, self.config)
        control = TaskControl(self.tasks)
        self.router = Router(SERVICE_NAME, WIRE_VERSION, {
            "ticket-submit": control.submit,
            "ticket-wait": control.wait,
            "ticket-output": control.output,
            "ticket-list": control.list,
            "ticket-cancel": control.cancel,
            "ticket-remove": control.remove,
            "tickets-reset": control.reset,
        })
        self.token = token or secrets.token_hex(16)
        self.endpoint = Endpoint(self.state.endpoint_path(), self.router,
                                 self.token, state=self.state,
                                 read_timeout=self.config.request_timeout)

    def run(self):
        """Serve until stopped; a live instance on the same state root
        wins, so a second start is a no-op rather than a takeover."""
        if probe(self.state.endpoint_path()):
            return 1
        self.tasks.start()
        self.endpoint.start()
        try:
            while not self.stopping.wait(1.0):
                pass
        finally:
            self.stop()
        return 0

    def stop(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.endpoint.stop()
        self.tasks.stop()
