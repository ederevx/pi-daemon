#!/usr/bin/env python3
"""Windows ConPTY smoke test for pi/lib/pi_conpty.py.

Spawns cmd.exe on a pseudoconsole, checks that its output reaches the
select-able output fd, exercises ResizePseudoConsole, and terminates the
child. On any non-Windows platform it prints a skip line and exits 0 so
the file stays loadable from a Linux checkout.
"""

import os
import select
import sys
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "lib"))


class WindowsConPtyTest(unittest.TestCase):
    """Smoke exercises for WindowsPtyBackend and WindowsPtyChild."""

    def setUp(self):
        from pi_conpty import WindowsPtyBackend
        self.backend = WindowsPtyBackend()

    def _spawn_echo(self):
        env = dict(os.environ)
        env.update({"TERM": "xterm-256color", "COLUMNS": "80",
                    "LINES": "24"})
        return self.backend.spawn(
            ["cmd.exe", "/c", "echo hello"], os.getcwd(), env, 80, 24)

    def _drain(self, child, want, deadline):
        collected = b""
        while time.time() < deadline:
            ready, _w, _e = select.select([child.output_fd], [], [], 0.2)
            if not ready:
                continue
            chunk = child.read_output(65536)
            if not chunk:
                break
            collected += chunk
            if want in collected:
                return collected
        return collected

    def test_child_output_arrives(self):
        child = self._spawn_echo()
        try:
            collected = self._drain(child, b"hello", time.time() + 10)
            self.assertIn(b"hello", collected)
        finally:
            child.terminate(2)
            child.close()

    def test_resize_then_reap(self):
        child = self._spawn_echo()
        try:
            self.assertTrue(child.resize(100, 30))
            self._drain(child, b"hello", time.time() + 10)
            reaped, code = child.wait_nohang()
            deadline = time.time() + 5
            while not reaped and time.time() < deadline:
                time.sleep(0.05)
                reaped, code = child.wait_nohang()
            self.assertTrue(reaped)
            self.assertEqual(code, 0)
        finally:
            child.terminate(2)
            child.close()

    def test_winsize_tracks_resize(self):
        child = self._spawn_echo()
        try:
            self.assertEqual(child.get_winsize(), (24, 80))
            self.assertTrue(child.resize(100, 30))
            self.assertEqual(child.get_winsize(), (30, 100))
        finally:
            child.terminate(2)
            child.close()

    def test_exit_abnormal_codes(self):
        child = self._spawn_echo()
        try:
            self.assertFalse(child.exit_abnormal(0))
            self.assertTrue(child.exit_abnormal(1))
        finally:
            child.terminate(2)
            child.close()

    def test_available(self):
        from pi_conpty import WindowsPtyBackend
        self.assertTrue(WindowsPtyBackend.available())


def main():
    if sys.platform != "win32":
        print("conpty: skipped (non-Windows)")
        return 0
    suite = unittest.TestLoader().loadTestsFromTestCase(WindowsConPtyTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())