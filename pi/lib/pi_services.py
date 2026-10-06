"""Generic local service discovery for independently operating daemons.

A provider advertises itself so any client can find and activate it
without knowing a daemon name, path, or port. One `ServiceDirectory`
owns the whole well-known location: the root formula, the per-provider
descriptor files, and the GC of stale entries. A descriptor is
non-secret coordinates only; the per-provider token stays in the
endpoint file it points at.

Layout: <services_root>/<service>/<provider>.json with

    {"service": ..., "version": ..., "protocol": ...,
     "provider": ..., "pid": ..., "endpoint_file": ...,
     "activation": {"kind": "exec", "argv": [...]}}

A descriptor is stale when its JSON is malformed, its pid is not alive,
or its endpoint file is missing/unreachable; the sweep removes only
those. Nothing here is module-level mutable state.
"""

import os
import shutil
import sys
import tempfile
import threading

import pi_platform

# The hourly sweep cadence: the daemon passes its own constant, this is
# only the fallback so the owner is usable stand-alone.
DEFAULT_SWEEP_TICK = 3600.0
DEFAULT_SERVICE = "session-host"
DEFAULT_PROVIDER = "pi-daemon"


class ServiceDirectory:
    """Owner of the well-known service-descriptor location.

    One responsibility: publish, remove, and GC provider descriptors
    under the services root. The root formula lives here alone so a
    provider and a client resolve the same directory.
    """

    def __init__(self, root=None, environ=None, platform=None,
                 process=None):
        self.environ = os.environ if environ is None else environ
        self.platform = sys.platform if platform is None else platform
        self.process = process or pi_platform.ProcessControl()
        self.root = self._resolve_root() if root is None else root

    # -- the location --------------------------------------------------------

    def _resolve_root(self):
        # The single owner of the services-root formula: env override,
        # else the POSIX runtime dir, else the temp dir on every
        # platform. Windows always uses the env/tempdir branch.
        explicit = self.environ.get("PI_SERVICES_DIR")
        if explicit:
            return explicit
        if not self._is_windows():
            runtime = self.environ.get("XDG_RUNTIME_DIR")
            if runtime:
                return os.path.join(runtime, "pi-services")
        return os.path.join(tempfile.gettempdir(), "pi-services")

    def _is_windows(self):
        return str(self.platform).startswith("win") or os.name == "nt"

    def path(self, service=DEFAULT_SERVICE, provider=DEFAULT_PROVIDER):
        # The one place that composes a descriptor path; safe single
        # path components only, so a hostile name can never escape the
        # services root.
        return os.path.join(self.root, self._component(service),
                            self._component(provider) + ".json")

    @staticmethod
    def _component(name):
        if not name or name in (".", "..") or os.sep in name \
                or (os.altsep and os.altsep in name):
            raise ValueError("unsafe service path component: %r" % (name,))
        return name

    # -- activation entry ----------------------------------------------------

    @staticmethod
    def activation_argv(script, command):
        """Resolve the activation executable the way the repo locates
        helper scripts: the sibling of the installed daemon, else the
        repo's pi/bin, else PATH, else the bare name. `script` is the
        provider's own __file__."""
        here = os.path.dirname(os.path.realpath(script))
        candidates = [
            os.path.join(here, "pi-rc"),
            os.path.join(os.path.dirname(here), "bin", "pi-rc"),
        ]
        for cand in candidates:
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return [cand, command]
        found = shutil.which("pi-rc")
        return [found, command] if found else ["pi-rc", command]

    # -- publish / remove ----------------------------------------------------

    def publish(self, endpoint_file, activation_argv,
                service=DEFAULT_SERVICE, provider=DEFAULT_PROVIDER,
                protocol="pi-pty-host/1", version=1, pid=None):
        """Write the provider's descriptor atomically and return it.
        `endpoint_file` is the absolute path the daemon already
        publishes host/port/token into."""
        descriptor = {
            "service": service,
            "version": int(version),
            "protocol": protocol,
            "provider": provider,
            "pid": int(os.getpid() if pid is None else pid),
            "endpoint_file": os.path.abspath(endpoint_file),
            "activation": {"kind": "exec",
                           "argv": [str(a) for a in activation_argv]},
        }
        pi_platform.AtomicStateFile(self.path(service, provider)).write(
            descriptor)
        return descriptor

    def remove(self, service=DEFAULT_SERVICE, provider=DEFAULT_PROVIDER,
               pid=None):
        """Drop a descriptor. With `pid`, unlink it only while it still
        records that pid, so a dying daemon never deletes the entry a
        successor published over it."""
        path = self.path(service, provider)
        if pid is not None:
            data = pi_platform.AtomicStateFile(path).read()
            if not isinstance(data, dict) or data.get("pid") != int(pid):
                return False
        try:
            os.unlink(path)
        except OSError:
            return False
        return True

    # -- GC ------------------------------------------------------------------

    def start(self, shutdown_event, tick=DEFAULT_SWEEP_TICK):
        """Run the stale-descriptor sweep on an interval until shutdown;
        the caller owns the one-shot startup sweep."""
        t = threading.Thread(target=self._sweep_loop,
                             args=(shutdown_event, float(tick)), daemon=True)
        t.start()

    def _sweep_loop(self, shutdown_event, tick):
        while not shutdown_event.wait(tick):
            self.sweep()

    def sweep(self):
        """Remove every stale descriptor under the services root. One
        malformed service directory is logged nowhere and never stops
        the rest; per-daemon rules mean only descriptors are touched."""
        try:
            services = os.listdir(self.root)
        except OSError:
            return
        for service in services:
            path = os.path.join(self.root, service)
            if os.path.islink(path) or not os.path.isdir(path):
                continue
            try:
                self._sweep_service(path)
            except OSError:
                continue

    def _sweep_service(self, service_dir):
        for name in os.listdir(service_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(service_dir, name)
            if self._stale(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _stale(self, path):
        data = pi_platform.AtomicStateFile(path).read()
        if not isinstance(data, dict):
            return True
        pid = data.get("pid")
        if not isinstance(pid, int) or not self.process.pid_alive(pid):
            return True
        endpoint = data.get("endpoint_file")
        if not isinstance(endpoint, str) or not self._endpoint_reachable(
                endpoint):
            return True
        return False

    @staticmethod
    def _endpoint_reachable(path):
        # Missing, or present but not publishing dialable coordinates:
        # the provider's control endpoint cannot be reached through it.
        if not os.path.isfile(path):
            return False
        data = pi_platform.EndpointFile(path).read()
        return bool(data.get("host")) and bool(data.get("port"))
