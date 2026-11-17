#!/usr/bin/env python3
"""Windows ConPTY smoke test for pi/lib/pi_conpty.py.

Spawns cmd.exe on a pseudoconsole, checks that its output reaches the
select-able output fd, exercises ResizePseudoConsole, and terminates the
child. On any non-Windows platform it prints a skip line and exits 0 so
the file stays loadable from a Linux checkout.
"""

import ctypes
import os
import select
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "lib"))
import isolation

import pi_conpty

# Every root this module probes belongs to its own scratch tree, never
# the host's live daemon.
ISOLATION = isolation.ScratchEnvironment()
ISOLATION.apply()

def tearDownModule():
    ISOLATION.cleanup()


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
            ready, _w, _e = select.select([child.output_handle()], [], [], 0.2)
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
            child.release()
            collected = self._drain(child, b"hello", time.time() + 20)
            self.assertIn(b"hello", collected)
        finally:
            child.terminate(2)
            child.close()

    def test_a_child_runs_nothing_before_it_is_released(self):
        child = self._spawn_echo()
        try:
            # Suspended at creation: the host records the identity first.
            self.assertIsNone(child.poll())
            self.assertEqual(self._drain(child, b"hello", time.time() + 1),
                             b"")
            child.release()
            self.assertIn(b"hello", self._drain(child, b"hello",
                                                time.time() + 20))
        finally:
            child.terminate(2)
            child.close()

    def test_resize_then_reap(self):
        child = self._spawn_echo()
        try:
            child.release()
            child.set_winsize(100, 30)
            self._drain(child, b"hello", time.time() + 20)
            self.assertEqual(child.wait(20), 0)
            self.assertEqual(child.poll(), 0)
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


class Win32InputModeFilterTest(unittest.TestCase):
    """Unit coverage for the ConPTY Win32 Input Mode request filter."""

    def setUp(self):
        from pi_conpty import _Win32InputModeFilter
        self.filter = _Win32InputModeFilter()

    def test_strips_enable_in_one_chunk(self):
        data = b"before\x1b[?9001hafter"
        self.assertEqual(self.filter.feed(data), b"beforeafter")

    def test_strips_disable_in_one_chunk(self):
        data = b"before\x1b[?9001lafter"
        self.assertEqual(self.filter.feed(data), b"beforeafter")

    def test_strips_split_at_every_offset(self):
        for seq in (b"\x1b[?9001h", b"\x1b[?9001l"):
            for cut in range(len(seq) + 1):
                filt = self._new_filter()
                out = filt.feed(seq[:cut]) + filt.feed(seq[cut:])
                self.assertEqual(out, b"",
                                 "split at %d leaked %r" % (cut, out))

    def test_forwards_other_sequences(self):
        data = b"\x1b[?25l\x1b[2J\x1b[?2004h"
        self.assertEqual(self.filter.feed(data), data)

    def test_forwards_unfinished_non_match(self):
        self.assertEqual(self.filter.feed(b"\x1b[?900"), b"")
        self.assertEqual(self.filter.feed(b"x"), b"\x1b[?900x")

    def _new_filter(self):
        from pi_conpty import _Win32InputModeFilter
        return _Win32InputModeFilter()


class WindowsTerminateBudgetTests(unittest.TestCase):
    """Two budgets: taskkill's own, then grace for the reap.

    The cases drive the seam with mocks, except the already reaped child,
    which is a real exited process; no case signals a live one.
    """

    def setUp(self):
        import pi_platform
        self.control = pi_platform.ProcessControl()

    def test_an_already_reaped_child_is_not_killed_again(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(30)
        with mock.patch.object(self.control, "taskkill_tree") as killed:
            self.control._windows_terminate(child.pid, 0.2, child)
        # There is no root left to walk an ancestry from.
        killed.assert_not_called()

    def test_the_tree_kill_keeps_its_own_bound_and_grace_bounds_the_reap(self):
        waited = []

        class Child:
            def poll(self):
                return None

            def wait(self, timeout):
                waited.append(timeout)

        with mock.patch.object(self.control, "taskkill_tree",
                               return_value=True) as killed, \
                mock.patch.object(self.control, "pid_alive",
                                  return_value=False):
            self.control._windows_terminate(4242, 0.75, Child())
        self.assertEqual(killed.call_args, mock.call(4242),
                         "taskkill keeps its own command bound")
        self.assertEqual(waited, [0.75], "grace bounds the reap")

    def test_a_taskkill_timeout_is_not_a_kill(self):
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("taskkill", 5)):
            self.assertFalse(self.control.taskkill_tree(4242),
                             "a command cut short ended no tree")

    def test_a_root_that_outlives_its_grace_is_an_error(self):
        class Child:
            def poll(self):
                return None

            def wait(self, timeout):
                raise subprocess.TimeoutExpired("child", timeout)

        with mock.patch.object(self.control, "taskkill_tree",
                               return_value=True):
            with self.assertRaises(OSError) as raised:
                self.control._windows_terminate(4242, 0.01, Child())
        self.assertEqual(str(raised.exception),
                         "process 4242 survived termination")

    def test_a_failed_tree_kill_whose_root_ended_is_reported_as_ended(self):
        class Child:
            def poll(self):
                return None

            def wait(self, timeout):
                pass

        with mock.patch.object(self.control, "taskkill_tree",
                               return_value=False), \
                mock.patch.object(self.control, "pid_alive",
                                  return_value=False):
            self.control._windows_terminate(4242, 0.2, Child())

    def test_a_failed_tree_kill_with_a_live_root_is_an_error(self):
        class Child:
            def poll(self):
                return None

            def wait(self, timeout):
                pass

        with mock.patch.object(self.control, "taskkill_tree",
                               return_value=False), \
                mock.patch.object(self.control, "pid_alive",
                                  return_value=True):
            with self.assertRaises(OSError) as raised:
                self.control._windows_terminate(4242, 0.2, Child())
        self.assertEqual(str(raised.exception),
                         "could not terminate process 4242")


class WindowsConsoleOutputPumpTest(unittest.TestCase):
    """The attach client's CONOUT$ writer seams behind a socket front.

    The relay selects the front; the writer thread owns the blocking
    console writes, drains a queued burst through stop-time EOF, and a
    failed console write surfaces at the relay as a None return.
    """

    def setUp(self):
        from pi_conpty import WindowsConsole
        self.console = WindowsConsole(drain_timeout=0.05)

    def _stub_writer(self, writes):
        api = mock.Mock()

        def write_file(_handle, buf, count, written_ref, _overlap):
            chunk = ctypes.cast(
                buf, ctypes.POINTER(ctypes.c_char * count)).contents.raw
            writes.append(chunk)
            written_ref._obj.value = count
            return 1

        api.lib.WriteFile.side_effect = write_file
        self.console._api = api
        self.console._out_handle = 4242

    def _wait_for(self, writes, want, deadline=2.0):
        end = time.monotonic() + deadline
        while time.monotonic() < end and b"".join(writes) != want:
            time.sleep(0.02)

    def test_output_reaches_the_console_through_the_socket_front(self):
        writes = []
        self._stub_writer(writes)
        self.console._start_output_pump()
        try:
            self.assertIsNotNone(self.console.writable_fd())
            self.assertEqual(self.console.write_output(b"hello"), 5)
            self._wait_for(writes, b"hello")
            self.assertEqual(b"".join(writes), b"hello")
        finally:
            self.console._stop_output_pump()
            self.console._out_handle = None
        self.assertIsNone(self.console.writable_fd())

    def test_a_pending_burst_drains_when_the_bridge_stops(self):
        writes = []
        self._stub_writer(writes)
        self.console._start_output_pump()
        payload = b"x" * (256 * 1024)
        try:
            data = payload
            accepted = b""
            deadline = time.monotonic() + 4.0
            while data and time.monotonic() < deadline:
                sent = self.console.write_output(data)
                self.assertIsNotNone(sent)
                if sent:
                    accepted += data[:sent]
                    data = data[sent:]
                time.sleep(0.005)
            self.assertEqual(accepted, payload)
            self.console._stop_output_pump()
            self.assertEqual(b"".join(writes), payload)
        finally:
            self.console._stop_output_pump()
            self.console._out_handle = None

    def test_a_short_console_write_still_writes_the_whole_chunk(self):
        writes = []
        api = mock.Mock()

        def write_file(_handle, buf, count, written_ref, _overlap):
            written = max(1, count // 2)
            chunk = ctypes.cast(
                buf, ctypes.POINTER(ctypes.c_char * count)).contents.raw
            writes.append(chunk[:written])
            written_ref._obj.value = written
            return 1

        api.lib.WriteFile.side_effect = write_file
        self.console._api = api
        self.console._out_handle = 4242
        self.console._start_output_pump()
        try:
            self.console.write_output(b"short write test")
            self._wait_for(writes, b"short write test")
            self.assertEqual(b"".join(writes), b"short write test")
        finally:
            self.console._stop_output_pump()
            self.console._out_handle = None

    def _chunked_write(self, steps, want):
        writes = []
        self._stub_writer(writes)
        self.console._start_output_pump()
        try:
            for data, midline in steps:
                self.console.write_output(data)
                self._wait_for(writes, midline)
            self.assertEqual(b"".join(writes), want)
        finally:
            self.console._stop_output_pump()
            self.console._out_handle = None

    def test_a_split_utf8_sequence_is_held_until_it_completes(self):
        self._chunked_write(
            [(b"OK\xe4\xbd", b"OK"),
             (b"\xa0\xe5\xa5\xbd", b"OK\xe4\xbd\xa0\xe5\xa5\xbd")],
            b"OK\xe4\xbd\xa0\xe5\xa5\xbd")

    def test_a_split_win32_request_is_held_until_it_completes(self):
        # Each step's console line-up is awaited, so the request really
        # crosses the writer's recv boundary instead of racing into one
        # receive.
        self._chunked_write([(b"A\x1b[?9", b"A"), (b"001hB", b"AB")],
                            b"AB")

    def test_a_parked_writer_is_cancelled_and_reports_success(self):
        write_entered = threading.Event()
        cancelled = threading.Event()
        api = mock.Mock()

        def write_file(_handle, _buf, count, written_ref, _overlap):
            # A write the cancellation aborts: the writer sits in it
            # until the mocked CancelSynchronousIo lands, then fails
            # like CONOUT$ does with ERROR_OPERATION_ABORTED.
            write_entered.set()
            cancelled.wait(0.5)
            raise OSError("operation aborted")

        def cancel_synchronous_io(_handle):
            cancelled.set()
            return 1

        api.lib.WriteFile.side_effect = write_file
        api.lib.CancelSynchronousIo.side_effect = cancel_synchronous_io
        self.console._api = api
        self.console._out_handle = 4242
        self.console._start_output_pump()
        tid = self.console._output_thread.native_id
        try:
            self.console.write_output(b"slow chunk")
            self.assertTrue(write_entered.wait(2.0))
            self.assertTrue(self.console._stop_output_pump())
            api.lib.OpenThread.assert_called_once()
            mask, inherit, opened = api.lib.OpenThread.call_args.args
            self.assertEqual(mask, pi_conpty._THREAD_TERMINATE)
            self.assertFalse(inherit)
            self.assertEqual(opened, tid)
            # A successfully opened handle is closed even when the
            # cancellation request itself fails.
            api.lib.CloseHandle.assert_called_once_with(
                api.lib.OpenThread.return_value)
            self.assertTrue(cancelled.is_set())
            self.assertIsNone(self.console._output_thread)
            self.assertIsNone(self.console.writable_fd())
        finally:
            cancelled.set()
            self.console._stop_output_pump()
            self.console._out_handle = None

    def test_a_cancel_without_a_thread_handle_only_waits_the_join(self):
        # OpenThread can return no handle; the stop then refuses to lean
        # on the request and confirms the writer through the join.
        write_entered = threading.Event()
        release = threading.Event()
        api = mock.Mock()
        api.lib.OpenThread.return_value = None

        def write_file(_handle, _buf, count, written_ref, _overlap):
            write_entered.set()
            release.wait(2.0)
            written_ref._obj.value = count
            return 1

        api.lib.WriteFile.side_effect = write_file
        self.console._api = api
        self.console._out_handle = 4242
        self.console._start_output_pump()
        try:
            self.console.write_output(b"parked")
            self.assertTrue(write_entered.wait(2.0))
            self.assertFalse(self.console._stop_output_pump())
            api.lib.CancelSynchronousIo.assert_not_called()
        finally:
            release.set()
            self.console._stop_output_pump()
            self.console._out_handle = None

    def test_a_cancellation_request_landing_early_still_refuses_the_stop(
            self):
        # CancelSynchronousIo is a request, never a completion: it can
        # land after the abort check and before WriteFile begins; the
        # writer then survives and the stop reports it instead of
        # assuming the request completed the write.
        write_entered = threading.Event()
        completes = threading.Event()
        api = mock.Mock()

        def write_file(_handle, _buf, count, written_ref, _overlap):
            # The write completes later regardless of the request; a
            # later loop turn sees the abort flag and exits.
            write_entered.set()
            completes.wait(2.0)
            if self.console._output_aborted:
                raise OSError("output aborted")
            written_ref._obj.value = count
            return 1

        def cancel_synchronous_io(_handle):
            return 1

        api.lib.WriteFile.side_effect = write_file
        api.lib.CancelSynchronousIo.side_effect = cancel_synchronous_io
        self.console._api = api
        self.console._out_handle = 4242
        self.console._start_output_pump()
        try:
            self.console.write_output(b"parked")
            self.assertTrue(write_entered.wait(2.0))
            self.assertFalse(self.console._stop_output_pump())
            self.assertTrue(self.console._output_thread.is_alive())
        finally:
            completes.set()
            self.console._stop_output_pump()
            self.console._out_handle = None

    def test_a_writer_that_survives_cancellation_refuses_the_stop(self):
        write_entered = threading.Event()
        gate = threading.Event()
        api = mock.Mock()

        def write_file(_handle, _buf, count, written_ref, _overlap):
            # A write that cancellation never lands on: the writer stays
            # parked past both joins.
            write_entered.set()
            gate.wait(2.0)
            written_ref._obj.value = count
            return 1

        api.lib.WriteFile.side_effect = write_file
        self.console._api = api
        self.console._out_handle = 4242
        self.console._start_output_pump()
        try:
            self.console.write_output(b"parked")
            self.assertTrue(write_entered.wait(2.0))
            self.assertFalse(self.console._stop_output_pump())
            self.assertTrue(self.console._output_thread.is_alive())
        finally:
            gate.set()
            self.console._stop_output_pump()
            self.console._out_handle = None

    def test_a_failed_console_write_surfaces_as_none(self):
        write_entered = threading.Event()
        api = mock.Mock()

        def write_file(_handle, _buf, count, written_ref, _overlap):
            # Win32 failure: the call reports success with zero bytes
            # written, which the writer treats as a failed write.
            write_entered.set()
            written_ref._obj.value = 0
            return 1

        api.lib.WriteFile.side_effect = write_file
        self.console._api = api
        self.console._out_handle = 4242
        self.console._start_output_pump()
        try:
            self.console.write_output(b"abc")
            self.assertTrue(write_entered.wait(2.0))
            result = None
            end = time.monotonic() + 2.0
            while time.monotonic() < end:
                result = self.console.write_output(b"abc")
                if result is None:
                    break
                time.sleep(0.02)
            self.assertIsNone(result)
        finally:
            self.console._stop_output_pump()
            self.console._out_handle = None

    def test_a_pump_whose_writer_cannot_start_leaves_nothing_behind(self):
        self.console._api = mock.Mock()
        self.console._out_handle = 4242
        with mock.patch.object(threading.Thread, "start",
                               side_effect=RuntimeError("no threads")):
            with self.assertRaises(RuntimeError):
                self.console._start_output_pump()
        self.assertIsNone(self.console._output_thread)
        self.assertIsNone(self.console._output_sock)
        self.assertIsNone(self.console.writable_fd())


def main():
    if sys.platform != "win32":
        print("conpty: skipped (non-Windows)")
        return 0
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        loader.loadTestsFromTestCase(WindowsConPtyTest),
        loader.loadTestsFromTestCase(Win32InputModeFilterTest),
        loader.loadTestsFromTestCase(WindowsTerminateBudgetTests),
        loader.loadTestsFromTestCase(WindowsConsoleOutputPumpTest),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())