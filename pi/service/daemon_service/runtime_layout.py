"""Every path the service derives, resolved once, with no cleanup."""

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
        if not name or os.sep in name or name in (".", ".."):
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

    def settings_path(self):
        return os.path.join(self._state_root, "settings.json")

    def instance_lock_path(self):
        os.makedirs(self._runtime_root, exist_ok=True)
        return os.path.join(self._runtime_root, "instance.lock")

    def endpoint_path(self):
        os.makedirs(self._runtime_root, exist_ok=True)
        return os.path.join(self._runtime_root, "control.json")
