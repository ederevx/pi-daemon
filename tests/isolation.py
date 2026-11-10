#!/usr/bin/env python3
"""One scratch tree and environment for every pi-daemon test process.

A test process must never read, write or signal the live host's daemon
roots: its daemon, tickets, tunnels, discovery entries and service-manager
answers all belong to this scratch tree. One class owns that tree and the
environment derived from it, so the suite runner and a module run on its
own isolate the same way. A module builds one in `setUpModule()` before
anything resolves a root from the environment.
"""

import os
import shutil
import tempfile

# Roots the suite owns. An inherited value names the live host's tree.
ROOT_VARIABLES = (
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "TEMP", "TMP", "TMPDIR",
    "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    "PI_CODING_AGENT_DIR", "PI_DAEMON_STATE_DIR", "PI_DAEMON_RUNTIME_DIR",
    "PI_SERVICES_DIR",
)

# Names of a live service, a hosted session or an agent: an inherited value
# makes a test dial, signal or impersonate the host's daemon.
CLEARED_VARIABLES = (
    "PI_DAEMON_ENDPOINT",
    "PI_HOSTED", "PI_HOSTED_SESSION",
    "PI_DAEMON_RUN_ID", "PI_DAEMON_RUN_GENERATION",
    "PI_COMMS_AGENT_ID",
)

# The tree layout the environment names, keyed by the root it serves.
ROOTS = {
    "home": ("HOME", "USERPROFILE"),
    "appdata": ("APPDATA",),
    "localappdata": ("LOCALAPPDATA",),
    "temp": ("TEMP", "TMP", "TMPDIR"),
    "runtime": ("XDG_RUNTIME_DIR",),
    "state": ("XDG_STATE_HOME",),
    "config": ("XDG_CONFIG_HOME",),
    "data": ("XDG_DATA_HOME",),
    "agent": ("PI_CODING_AGENT_DIR",),
    "daemon-state": ("PI_DAEMON_STATE_DIR",),
    "daemon-runtime": ("PI_DAEMON_RUNTIME_DIR",),
    "services": ("PI_SERVICES_DIR",),
}


class ScratchEnvironment:
    """The suite's own equivalent of a host, and the environment for it."""

    def __init__(self, root=None):
        self.root = root or tempfile.mkdtemp(
            prefix="pi-daemon-tests-", dir=host_scratch())
        self._saved = None

    def path(self, name):
        return os.path.join(self.root, name)

    def environment(self):
        """The inherited environment with every owned root redirected."""
        environment = dict(os.environ)
        for name in CLEARED_VARIABLES:
            environment.pop(name, None)
        for directory, names in ROOTS.items():
            target = self.path(directory)
            os.makedirs(target, exist_ok=True)
            for name in names:
                environment[name] = target
        # Modules keep their scratch under ~/tmp, which now names this tree.
        os.makedirs(os.path.join(self.path("home"), "tmp"), exist_ok=True)
        return environment

    def apply(self):
        """Redo this process's own environment, for a module run alone."""
        if self._saved is None:
            self._saved = dict(os.environ)
        environment = self.environment()
        for name in ROOT_VARIABLES:
            if name in environment:
                os.environ[name] = environment[name]
            else:
                os.environ.pop(name, None)
        for name in CLEARED_VARIABLES:
            os.environ.pop(name, None)

    def restore(self):
        if self._saved is None:
            return
        os.environ.clear()
        os.environ.update(self._saved)
        self._saved = None

    def cleanup(self):
        self.restore()
        shutil.rmtree(self.root, ignore_errors=True)


def host_scratch():
    """The host's scratch directory, created on first use (never /tmp)."""
    scratch = os.path.expanduser("~/tmp")
    os.makedirs(scratch, exist_ok=True)
    return scratch
