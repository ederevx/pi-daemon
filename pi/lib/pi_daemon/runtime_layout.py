"""Every path the service derives, resolved once, with no cleanup."""

import ntpath
import os


class RuntimeLayout:
    """State is durable; runtime holds live coordinates only."""

    def __init__(self, state_root, runtime_root):
        self._state_root = self._resolve(state_root, "state root")
        self._runtime_root = self._resolve(runtime_root, "runtime root")

    def _resolve(self, root, label):
        if not root:
            raise ValueError(f"an explicit {label} is required")
        return os.path.abspath(os.path.expanduser(str(root)))

    def state_root(self):
        return self._state_root

    def runtime_root(self):
        return self._runtime_root

    def domain_root(self, name):
        # A component is checked by its meaning, not by the host's
        # separator: Windows accepts both, so either one is traversal.
        if (not name or name in (".", "..")
                or "/" in name or "\\" in name
                or ntpath.splitdrive(name)[0]):
            raise ValueError(f"unsafe domain name: {name!r}")
        root = os.path.join(self._state_root, name)
        os.makedirs(root, exist_ok=True)
        return root

    def tickets_path(self):
        return os.path.join(self.domain_root("tasks"), "tickets.json")

    def logs_dir(self):
        path = os.path.join(self.domain_root("tasks"), "logs")
        os.makedirs(path, exist_ok=True)
        return path

    def sessions_path(self):
        return os.path.join(self.domain_root("sessions"), "sessions.json")

    def run_state_dir(self):
        path = os.path.join(self.domain_root("sessions"), "runs")
        os.makedirs(path, exist_ok=True)
        return path

    def session_state_path(self, key):
        return os.path.join(self.run_state_dir(), f"{key}.json")

    def comms_mailbox_path(self):
        return os.path.join(self.domain_root("comms"), "mailbox.json")

    def comms_node_path(self):
        return os.path.join(self.domain_root("comms"), "node.json")

    def comms_spawns_path(self):
        return os.path.join(self.domain_root("comms"), "spawns.json")

    def comms_peers_path(self):
        return os.path.join(self.domain_root("comms"), "peers.json")

    def comms_peer_credentials_path(self):
        """The one directory a peer's outbound credential may live in."""
        return os.path.join(self.domain_root("comms"), "peer-credentials")

    def settings_path(self):
        return os.path.join(self._state_root, "settings.json")

    def migration_path(self):
        """The record of a completed translation; absent until one runs."""
        return os.path.join(self._state_root, "migration.json")

    def daemon_log_path(self):
        """Where a daemon whose stderr is closed still records why it died."""
        os.makedirs(self._state_root, exist_ok=True)
        return os.path.join(self._state_root, "daemon.log")

    def instance_lock_path(self):
        return os.path.join(self._runtime_root, "instance.lock")

    def endpoint_path(self):
        os.makedirs(self._runtime_root, exist_ok=True)
        return os.path.join(self._runtime_root, "control.json")

    def comms_endpoint_path(self):
        """The comms listener's coordinates, beside the control endpoint."""
        os.makedirs(self._runtime_root, exist_ok=True)
        return os.path.join(self._runtime_root, "comms.json")
