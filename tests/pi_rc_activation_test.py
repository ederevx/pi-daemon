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
import stat
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "lib"))

import pi_platform

SCRATCH = tempfile.mkdtemp(prefix="pi-rc-activation-",
                           dir=os.path.expanduser("~/tmp"))


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)


pi_rc = SourceFileLoader(
    "pi_rc_activation", os.path.join(REPO, "pi", "bin", "pi-rc")).load_module()


class FakeSystemctl:
    """A systemctl that records its argv and reports a declared unit."""

    def __init__(self, directory, load_state="loaded", environment=""):
        self.log = os.path.join(directory, "systemctl.log")
        self.path = os.path.join(directory, "systemctl")
        script = (
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "' + self.log + '"\n'
            'case "$1 $2 $3" in\n'
            '  "--user show -p")\n'
            '    printf "LoadState=' + load_state + '\\n"\n'
            '    printf "Environment=\\"' + environment + '\\"\\n"\n'
            "    ;;\n"
            "esac\n"
            "exit 0\n")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(script)
        os.chmod(self.path, os.stat(self.path).st_mode | stat.S_IEXEC)

    def calls(self):
        try:
            with open(self.log, encoding="utf-8") as handle:
                return [line.strip() for line in handle]
        except FileNotFoundError:
            return []


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(dir=SCRATCH)
        self.state = os.path.join(self.base, "state")
        self.runtime = os.path.join(self.base, "runtime")
        os.makedirs(self.runtime, exist_ok=True)
        self.environ = {"PI_DAEMON_STATE_DIR": self.state,
                        "PI_DAEMON_RUNTIME_DIR": self.runtime,
                        "PATH": os.path.join(self.base, "bin")}
        os.makedirs(self.environ["PATH"], exist_ok=True)
        self.spawns = []
        self.patchers = [
            mock.patch.dict(os.environ, self.environ, clear=False),
            mock.patch.object(pi_rc, "DAEMON_RESTART_WAIT", 0.2),
            mock.patch.object(
                pi_rc.PROCESS, "launch_detached",
                side_effect=lambda *args: self.spawns.append(args)),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def client(self, alive=False, after_spawn=False):
        # A detached spawn is the daemon's own start: the client only sees
        # it once it answers, which the spawn recorder stands in for.
        def answering():
            return alive or (after_spawn and bool(self.spawns))

        return mock.Mock(alive=mock.Mock(side_effect=answering),
                         sock_path=os.path.join(self.runtime, "control.json"))

    def test_a_unit_that_declares_the_root_starts_it_and_never_spawns(self):
        systemctl = FakeSystemctl(
            self.environ["PATH"],
            environment="PI_DAEMON_RUNTIME_DIR=" + self.runtime)
        client = self.client()
        with mock.patch.object(pi_platform, "ControlClient") as control:
            control.return_value.connect.return_value.close.return_value = None
            pi_rc.cmd_daemon_start([], client)
        self.assertIn("--user start pi-daemon.service", systemctl.calls())
        self.assertEqual(self.spawns, [])

    def test_a_unit_that_never_answers_fails_instead_of_spawning(self):
        systemctl = FakeSystemctl(
            self.environ["PATH"],
            environment="PI_DAEMON_RUNTIME_DIR=" + self.runtime)
        client = self.client()
        with mock.patch.object(
                pi_platform, "ControlClient") as control:
            control.return_value.connect.side_effect = OSError("nobody home")
            with self.assertRaises(SystemExit) as raised:
                pi_rc.cmd_daemon_start([], client)
        self.assertEqual(raised.exception.code, pi_rc.EXIT_NO_DAEMON)
        self.assertIn("--user start pi-daemon.service", systemctl.calls())
        self.assertEqual(self.spawns, [])

    def test_no_unit_spawns_the_detached_daemon(self):
        FakeSystemctl(self.environ["PATH"], load_state="not-found")
        pi_rc.cmd_daemon_start([], self.client(after_spawn=True))
        self.assertEqual(len(self.spawns), 1)

    def test_a_unit_for_another_root_spawns_the_detached_daemon(self):
        FakeSystemctl(self.environ["PATH"],
                      environment="PI_DAEMON_RUNTIME_DIR=/somewhere/else")
        pi_rc.cmd_daemon_start([], self.client(after_spawn=True))
        self.assertEqual(len(self.spawns), 1)

    def test_no_systemctl_spawns_the_detached_daemon(self):
        pi_rc.cmd_daemon_start([], self.client(after_spawn=True))
        self.assertEqual(len(self.spawns), 1)

    def test_a_serving_daemon_starts_nothing(self):
        systemctl = FakeSystemctl(
            self.environ["PATH"],
            environment="PI_DAEMON_RUNTIME_DIR=" + self.runtime)
        pi_rc.cmd_daemon_start([], self.client(alive=True))
        self.assertEqual(systemctl.calls(), [])
        self.assertEqual(self.spawns, [])

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
