#!/usr/bin/env python3
"""The attach relay's seams: the bounded control round-trip and the
backpressure-preserving output pump.

These are platform-neutral: the socket fronts are plain socketpairs, so
the relay logic runs the same way Windows and POSIX run it.
"""

import os
import select
import socket
import sys
import threading
import time
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "lib"))

pi_rc = SourceFileLoader(
    "pi_rc_relay", os.path.join(REPO, "pi", "bin", "pi-rc")).load_module()


class DaemonClientControlTest(unittest.TestCase):
    """A single control round-trip cannot wait forever for a reply."""

    def setUp(self):
        self.client = pi_rc.DaemonClient()
        self.relay, peer = socket.socketpair()
        self.client.connect = mock.Mock(return_value=self.relay)
        self.peer = peer

    def tearDown(self):
        for sock in (self.relay, self.peer):
            try:
                sock.close()
            except OSError:
                pass

    def test_a_reply_within_the_timeout_is_returned(self):
        def reply():
            time.sleep(0.02)
            self.peer.sendall(b'{"ok": true}\n')

        threading.Thread(target=reply).start()
        resp = self.client.control({"cmd": "state"})
        self.assertEqual(resp, {"ok": True})
        # The response recv rides the deadline's remaining budget.
        self.assertGreater(self.relay.gettimeout(), 0)
        self.assertLessEqual(self.relay.gettimeout(), pi_rc.CONTROL_TIMEOUT)

    def test_a_silent_daemon_ends_the_round_trip_bounded(self):
        with self.assertRaises(SystemExit) as raised:
            self.client.control({"cmd": "state"}, timeout=0.2)
        self.assertEqual(raised.exception.code, pi_rc.EXIT_AMBIGUOUS)
        # The caller passed the wait's own timeout, not the default.
        self.assertEqual(self.client.connect.call_args.args[0], 0.2)

    def test_a_trickling_reply_cannot_run_past_the_deadline(self):
        # One drip per interval keeps every per-op timeout quiet, so only
        # the round-trip's deadline can end this connection.
        def trickle():
            while True:
                self.peer.sendall(b" ")
                time.sleep(0.1)

        threading.Thread(target=trickle, daemon=True).start()
        start = time.monotonic()
        with self.assertRaises(SystemExit) as raised:
            self.client.control({"cmd": "state"}, timeout=0.4)
        self.assertEqual(raised.exception.code, pi_rc.EXIT_AMBIGUOUS)
        self.assertLess(time.monotonic() - start, pi_rc.CONTROL_TIMEOUT)

    def test_a_trickling_line_refuses_to_read_past_its_deadline(self):
        self.peer.sendall(b"{")
        with self.assertRaises(TimeoutError):
            pi_rc.read_json_line(self.relay, deadline=time.monotonic() + 0.2)


class BridgePumpSocketTest(unittest.TestCase):
    """A full to_tty queue applies backpressure instead of dropping."""

    def setUp(self):
        self.relay, peer = socket.socketpair()
        self.peer = peer
        self.bridge = pi_rc.Bridge(object(), self.relay, "name",
                                    terminal=mock.Mock())

    def tearDown(self):
        self.bridge.resizer.close()
        for sock in (self.relay, self.peer):
            try:
                sock.close()
            except OSError:
                pass

    def _ready(self):
        return select.select([self.relay], [], [], 1.0)[0]

    def test_the_queue_bounded_recv_keeps_every_arriving_byte(self):
        to_tty = bytearray(b"x" * (pi_rc.MAX_BUF - 10))
        self.peer.sendall(b"y" * 200)
        ready = self._ready()
        while self.bridge._pump_socket(ready, to_tty):
            if len(to_tty) >= pi_rc.MAX_BUF:
                break
            ready = self._ready()
        self.assertEqual(bytes(to_tty),
                         b"x" * (pi_rc.MAX_BUF - 10) + b"y" * 10)
        # Everything past the bound stays queued on the connection, not
        # dropped at the seam.
        self.assertEqual(self.relay.recv(200), b"y" * 190)

    def test_a_full_queue_receives_nothing_more(self):
        to_tty = bytearray(b"x" * pi_rc.MAX_BUF)
        self.peer.sendall(b"y" * 200)
        ready = self._ready()
        self.assertTrue(self.bridge._pump_socket(ready, to_tty))
        self.assertEqual(bytes(to_tty), b"x" * pi_rc.MAX_BUF)


class BridgeResizeTest(unittest.TestCase):
    """Resizes coalesce onto one bounded control worker."""

    def setUp(self):
        self.controls = []
        self.client = mock.Mock()

        def control(req, timeout=None):
            self.controls.append((req, timeout))
            time.sleep(0.02)
            return {"ok": True}

        self.client.control.side_effect = control
        self.resizer = pi_rc.BridgeResize(self.client, "name")

    def tearDown(self):
        self.resizer.close()

    def test_a_submit_is_delivered_with_the_default_timeout(self):
        self.resizer.submit(100, 40)
        deadline = time.monotonic() + 2.0
        while not self.controls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.controls[0][0],
                         {"cmd": "resize", "name": "name",
                          "cols": 100, "rows": 40})
        self.assertEqual(self.controls[0][1], pi_rc.CONTROL_TIMEOUT)

    def test_quick_submits_coalesce_to_the_newest_size(self):
        self.resizer.submit(100, 40)
        self.resizer.submit(101, 41)
        deadline = time.monotonic() + 2.0
        while len(self.controls) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        deadline = time.monotonic() + 2.0
        while len(self.controls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.controls[-1][0]["cols"], 101)

    def test_close_stops_the_worker(self):
        self.resizer.close()
        self.assertFalse(self.resizer._thread.is_alive())

    def test_a_submit_after_close_is_refused(self):
        self.resizer.close()
        self.assertFalse(self.resizer.submit(100, 40))

    def test_the_worker_survives_a_failed_resize_and_serves_the_next(self):
        # The recording stub raises for the first round trip and serves
        # the next, like a daemon that refuses one resize then recovers.
        attempts = {"n": 0}

        def control(req, timeout=None):
            self.controls.append((req, timeout))
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("refused")
            return {"ok": True}

        self.client.control.side_effect = control
        self.resizer.submit(100, 40)
        deadline = time.monotonic() + 2.0
        while len(self.controls) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.resizer.submit(101, 41)
        deadline = time.monotonic() + 2.0
        while len(self.controls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.controls[1][0]["cols"], 101)

    def test_close_joins_an_in_flight_round_trip(self):
        entered = threading.Event()
        release = threading.Event()

        def control(req, timeout=None):
            self.controls.append((req, timeout))
            entered.set()
            release.wait(2.0)
            return {"ok": True}

        self.client.control.side_effect = control
        resizer = pi_rc.BridgeResize(self.client, "name", timeout=1.0)
        self.addCleanup(release.set)
        resizer.submit(90, 30)
        self.assertTrue(entered.wait(2.0))
        # The round trip is mid-flight when close begins (the release is
        # only now handed to the worker); close waits it out instead of
        # leaving the connection half-served.
        release.set()
        resizer.close()
        self.assertFalse(resizer._thread.is_alive())

    def test_a_worker_past_its_join_fails_the_close(self):
        entered = threading.Event()
        release = threading.Event()

        def control(req, timeout=None):
            self.controls.append((req, timeout))
            entered.set()
            release.wait(5.0)
            return {"ok": True}

        self.client.control.side_effect = control
        resizer = pi_rc.BridgeResize(self.client, "name", timeout=0.1)
        self.addCleanup(resizer._thread.join, 2.0)
        self.addCleanup(release.set)
        resizer.submit(90, 30)
        self.assertTrue(entered.wait(2.0))
        with self.assertRaises(OSError):
            resizer.close()
        self.assertTrue(resizer._thread.is_alive())

    def test_sizes_blocked_behind_one_round_trip_coalesce(self):
        entered = threading.Event()
        release = threading.Event()

        def control(req, timeout=None):
            self.controls.append((req, timeout))
            entered.set()
            release.wait(2.0)
            return {"ok": True}

        self.client.control.side_effect = control
        resizer = pi_rc.BridgeResize(self.client, "name")
        self.addCleanup(release.set)
        self.addCleanup(resizer._thread.join, 2.0)
        resizer.submit(100, 40)
        self.assertTrue(entered.wait(2.0))
        resizer.submit(101, 41)
        resizer.submit(102, 42)
        release.set()
        deadline = time.monotonic() + 2.0
        while len(self.controls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        resizer.close()
        self.assertEqual(self.controls[1][0]["cols"], 102)


def main():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        loader.loadTestsFromTestCase(DaemonClientControlTest),
        loader.loadTestsFromTestCase(BridgePumpSocketTest),
        loader.loadTestsFromTestCase(BridgeResizeTest),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
