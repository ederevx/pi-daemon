"""The provider's entry in the shared service directory (pi-services.md)."""

import os
import sys
import tempfile

from ..atomic_state_file import AtomicStateFile


class ServiceDescriptor:
    """Only the instance that published an entry may remove it."""

    def __init__(self, root, service, provider, mode=0o600, retries=5,
                 retry_delay=0.05):
        for part in (service, provider):
            if not part or part in (".", "..") or os.sep in part or \
                    (os.altsep and os.altsep in part):
                raise ValueError(f"unsafe service path component: {part!r}")
        self._file = AtomicStateFile(
            os.path.join(os.path.abspath(root), service, provider + ".json"),
            mode, retries, retry_delay)
        self._service = service
        self._provider = provider

    @staticmethod
    def root_for(environ, platform=sys.platform):
        """The one services-root formula, matching pi-services.md and
        the client's `pi_services.ServiceDirectory`."""
        explicit = environ.get("PI_SERVICES_DIR")
        if explicit:
            return explicit
        if not str(platform).startswith("win"):
            runtime = environ.get("XDG_RUNTIME_DIR")
            if runtime:
                return os.path.join(runtime, "pi-services")
        return os.path.join(tempfile.gettempdir(), "pi-services")

    def path(self):
        return self._file.path()

    def publish(self, protocol, version, endpoint_file, activation_argv,
                instance, pid):
        """A descriptor names the endpoint file, never a secret."""
        self._file.write_dict({
            "service": self._service,
            "version": int(version),
            "protocol": protocol,
            "provider": self._provider,
            "pid": int(pid),
            "instance": instance,
            "endpoint_file": os.path.abspath(endpoint_file),
            "activation": {"kind": "exec",
                           "argv": [str(arg) for arg in activation_argv]},
        })

    def remove_owned(self, instance, pid):
        """A successor that published over us keeps its entry."""
        try:
            current = self._file.read_dict()
        except ValueError:
            return False
        if not current or current.get("instance") != instance or \
                current.get("pid") != int(pid):
            return False
        try:
            os.unlink(self.path())
        except OSError:
            return False
        return True
