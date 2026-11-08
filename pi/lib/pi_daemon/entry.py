"""The daemon process entry contract: validate, prepare stderr, run."""

import sys

from .application import DaemonApplication
from .environment import EntryEnvironment
from .runtime_layout import RuntimeLayout
from .transport.service_descriptor import ServiceDescriptor


class DaemonEntry:
    """The one way a daemon process starts, whatever launched it.

    A daemon takes no arguments: control commands belong to the client, so
    anything in argv is a mistake and exits 2 before a root is created, a
    default is read, or an application is composed. The stderr repair keeps
    the monolith's behaviour: a unit whose stderr is closed still records
    why the process died, appending to the state root's `daemon.log`, and a
    repair that fails is not fatal."""

    def __init__(self, environ=None, application_factory=None):
        self._environment = EntryEnvironment(environ)
        self._application_factory = application_factory

    def main(self, argv=None):
        argv = sys.argv[1:] if argv is None else list(argv)
        if argv:
            self._error("pi-daemon: takes no arguments (got %r); "
                        "control commands live in pi-rc\n" % (argv,))
            return 2
        try:
            environment = self._environment.resolve()
            layout = RuntimeLayout(environment["PI_DAEMON_STATE_DIR"],
                                   environment["PI_DAEMON_RUNTIME_DIR"])
        except ValueError as failure:
            self._error("pi-daemon: %s\n" % failure)
            return 2
        self._ensure_stderr(layout)
        factory = self._application_factory or \
            DaemonApplication.from_environment
        application = factory(
            environ=environment,
            services_root=ServiceDescriptor.root_for(environment))
        return application.run()

    @staticmethod
    def _error(message):
        try:
            sys.stderr.write(message)
            sys.stderr.flush()
        except Exception:
            pass

    @staticmethod
    def _ensure_stderr(layout):
        try:
            sys.stderr.write("")
            sys.stderr.flush()
            return
        except Exception:
            pass
        try:
            sys.stderr = open(layout.daemon_log_path(), "a", buffering=1,
                              encoding="utf-8", errors="replace")
        except OSError:
            pass
