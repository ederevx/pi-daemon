#!/usr/bin/env python3
"""Activation: the unit that declares the runtime root starts the daemon.

The choice is `pi-rc daemon-start`'s alone. When the systemd user unit
declares this runtime root, a start is systemd's and the wait dials the
endpoint the unit publishes; only a host without such a unit falls back
to the detached spawn.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "lib"))

import isolation

# Every root this module probes belongs to its own scratch tree, never
# the host's live daemon.
ISOLATION = isolation.ScratchEnvironment()
ISOLATION.apply()

import pi_platform
from pi_daemon.runtime_layout import RuntimeLayout as InstalledLayout

SCRATCH = tempfile.mkdtemp(prefix="pi-rc-activation-",
                           dir=os.path.expanduser("~/tmp"))


def tearDownModule():
    ISOLATION.cleanup()
    shutil.rmtree(SCRATCH, ignore_errors=True)


pi_rc = SourceFileLoader(
    "pi_rc_activation", os.path.join(REPO, "pi", "bin", "pi-rc")).load_module()


class FakeSystemd:
    """The systemd answers pi-rc asks for, at its call boundary.

    A POSIX shell script cannot be launched by a native Windows process,
    and what the policy under test decides is the choice pi-rc makes from
    systemctl's answer, not systemd itself.
    """

    def __init__(self, load_state="loaded", environment="",
                 missing=False):
        self.load_state = load_state
        self.environment = environment
        self.missing = missing
        self.calls = []

    def run(self, argv, **_kwargs):
        if self.missing:
            raise FileNotFoundError(argv[0])
        self.calls.append(" ".join(argv))
        if argv[1:3] == ["--user", "show"]:
            return subprocess.CompletedProcess(
                argv, 0, "LoadState=" + self.load_state + "\n"
                "Environment=\"" + self.environment + "\"\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(dir=SCRATCH)
        self.state = os.path.join(self.base, "state")
        self.runtime = os.path.join(self.base, "runtime")
        os.makedirs(self.runtime, exist_ok=True)
        # The class pi-rc reads: it asks the layout for its roots, and those
        # are the roots `installed_layout` answers with.
        self.layout = InstalledLayout(self.state, self.runtime)
        self.environ = {"PI_DAEMON_STATE_DIR": self.state,
                        "PI_DAEMON_RUNTIME_DIR": self.runtime,
                        "PATH": os.path.join(self.base, "bin")}
        os.makedirs(self.environ["PATH"], exist_ok=True)
        self.spawns = []
        self.patchers = [
            mock.patch.dict(os.environ, self.environ, clear=False),
            # Long enough for the fake systemd answer on a loaded host.
            mock.patch.object(pi_rc, "DAEMON_RESTART_WAIT", 2.0),
            mock.patch.object(
                pi_rc.PROCESS, "launch_detached",
                side_effect=lambda *args: self.spawns.append(args)),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def unit(self, systemd):
        """This checkout's installed layout, answered by a fake systemd."""
        patcher = mock.patch.object(pi_rc, "installed_layout",
                                    return_value=self.layout)
        patcher.start()
        self.addCleanup(patcher.stop)
        run = mock.patch.object(pi_rc.subprocess, "run",
                                side_effect=systemd.run)
        run.start()
        self.addCleanup(run.stop)
        return systemd

    def client(self, alive=False, after_spawn=False):
        # A detached spawn is the daemon's own start: the client only sees
        # it once it answers, which the spawn recorder stands in for.
        def answering():
            return alive or (after_spawn and bool(self.spawns))

        return mock.Mock(alive=mock.Mock(side_effect=answering),
                         sock_path=os.path.join(self.runtime, "control.json"))

    def test_a_unit_that_declares_the_root_starts_it_and_never_spawns(self):
        systemd = self.unit(FakeSystemd(
            environment="PI_DAEMON_RUNTIME_DIR=" + self.runtime))
        client = self.client()
        with mock.patch.object(pi_platform, "ControlClient") as control:
            control.return_value.connect.return_value.close.return_value = None
            pi_rc.cmd_daemon_start([], client)
        self.assertIn("systemctl --user start pi-daemon.service", systemd.calls)
        self.assertEqual(self.spawns, [])

    def test_a_unit_that_never_answers_fails_instead_of_spawning(self):
        systemd = self.unit(FakeSystemd(
            environment="PI_DAEMON_RUNTIME_DIR=" + self.runtime))
        client = self.client()
        with mock.patch.object(
                pi_platform, "ControlClient") as control:
            control.return_value.connect.side_effect = OSError("nobody home")
            with self.assertRaises(SystemExit) as raised:
                pi_rc.cmd_daemon_start([], client)
        self.assertEqual(raised.exception.code, pi_rc.EXIT_NO_DAEMON)
        self.assertIn("systemctl --user start pi-daemon.service", systemd.calls)
        self.assertEqual(self.spawns, [])

    def test_no_unit_spawns_the_detached_daemon(self):
        self.unit(FakeSystemd(load_state="not-found"))
        pi_rc.cmd_daemon_start([], self.client(after_spawn=True))
        self.assertEqual(len(self.spawns), 1)

    def test_a_unit_for_another_root_spawns_the_detached_daemon(self):
        self.unit(FakeSystemd(
            environment="PI_DAEMON_RUNTIME_DIR=/somewhere/else"))
        pi_rc.cmd_daemon_start([], self.client(after_spawn=True))
        self.assertEqual(len(self.spawns), 1)

    def test_no_systemctl_spawns_the_detached_daemon(self):
        self.unit(FakeSystemd(missing=True))
        pi_rc.cmd_daemon_start([], self.client(after_spawn=True))
        self.assertEqual(len(self.spawns), 1)

    def test_a_serving_daemon_starts_nothing(self):
        systemd = self.unit(FakeSystemd(
            environment="PI_DAEMON_RUNTIME_DIR=" + self.runtime))
        pi_rc.cmd_daemon_start([], self.client(alive=True))
        self.assertEqual(systemd.calls, [])
        self.assertEqual(self.spawns, [])

    @unittest.skipIf(sys.platform == "win32",
                     "real systemd user-unit behavior is unsupported on "
                     "Windows")
    def test_the_real_unit_answers_for_its_own_root(self):
        layout = pi_rc.unit_layout()
        if layout is not None:
            self.assertEqual(layout.runtime_root(),
                             self.layout.runtime_root())

    def test_the_wrapper_leaves_the_choice_to_daemon_start(self):
        # One owner for the choice: the wrapper calls daemon-start and never
        # starts a unit itself, so the two cannot disagree about the root.
        with open(os.path.join(REPO, "pi", "bin", "pi-wrapper"),
                  encoding="utf-8") as handle:
            body = handle.read().split("ensure_daemon() {", 1)[1]
        body = body.split("\n}", 1)[0]
        self.assertIn('"$PI_RC" daemon-start', body)
        self.assertNotIn("systemctl", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
