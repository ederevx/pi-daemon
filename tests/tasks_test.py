#!/usr/bin/env python3
"""The tasks domain: ticket records, their output logs and the runs they
name. Zero dependencies (stdlib only).

Every path this file writes lives under one scratch directory, and the
settings a config snapshot could read are pinned there too, so no test can
read or write the installed daemon's state. No daemon is ever started.
"""

import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "lib"))

SCRATCH = tempfile.mkdtemp(
    prefix="pi-tasks-", dir=os.path.expanduser("~/tmp"))
os.environ["PI_DAEMON_STATE_DIR"] = os.path.join(SCRATCH, "state")
os.environ["XDG_STATE_HOME"] = os.path.join(SCRATCH, "xdg-state")
os.environ["XDG_RUNTIME_DIR"] = os.path.join(SCRATCH, "run")
os.environ["PI_CODING_AGENT_DIR"] = os.path.join(SCRATCH, "agent")
for _directory in ("state", "xdg-state", "run", "agent"):
    os.makedirs(os.path.join(SCRATCH, _directory), exist_ok=True)

from pi_daemon.config import ServiceConfig
from pi_daemon.tasks.error import TaskError
from pi_daemon.tasks.model import CANCELLED, DONE, FAILED, RUNNING
from pi_daemon.tasks.runner import TicketRunner
from pi_daemon.tasks.store import TicketStore
from pi_daemon.wire import SPAWN_FAILED, TICKET_RUNNING, TOO_BUSY


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)


def base_dir():
    """One isolated state directory of this module's own scratch tree."""
    return tempfile.mkdtemp(dir=SCRATCH)


def store_for(base, **overrides):
    """The store over `base`'s tickets.json and its ticket log directory."""
    config = ServiceConfig(**overrides)
    logs = os.path.join(base, "tickets")
    os.makedirs(logs, exist_ok=True)
    store = TicketStore(os.path.join(base, "tickets.json"), logs, config)
    store.load()
    return store, config


def reserve(store, session="s-1", command="sleep 5", capacity=None):
    """A reservation in the repo directory, at the default capacity."""
    return store.reserve(
        session, REPO, command, 0.0,
        ServiceConfig().max_running if capacity is None else capacity)


def counter(ticket_id):
    """The id's monotonic tail, which is what a reload must never repeat."""
    return int(ticket_id.rsplit("-", 1)[1])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.base = base_dir()
        self.store = store_for(self.base)[0]

    def test_a_reservation_is_a_running_record(self):
        snapshot, created = reserve(self.store)
        self.assertTrue(created, "a fresh command creates a ticket")
        self.assertEqual(snapshot["status"], RUNNING,
                         "a reservation is a run in progress")
        self.assertEqual((snapshot["session"], snapshot["cwd"]),
                         ("s-1", REPO), "the request is what is recorded")
        self.assertTrue(snapshot["id"].startswith(f"t-{os.getpid()}-"),
                        snapshot["id"])

    def test_a_resend_of_a_running_command_replays_it(self):
        first, _created = reserve(self.store)
        second, created = reserve(self.store)
        self.assertFalse(created, "an identical live command is a replay")
        self.assertEqual(second["id"], first["id"],
                         "a replay names the running ticket, not a new one")

    def test_a_resend_after_the_run_ended_is_a_new_ticket(self):
        first, _created = reserve(self.store)
        self.store.finish(first["id"], DONE, 0)
        second, created = reserve(self.store)
        self.assertTrue(created, "a finished command may run again")
        self.assertNotEqual(second["id"], first["id"],
                            "the second run is its own ticket")

    def test_capacity_counts_running_tickets(self):
        reserve(self.store, command="sleep 1")
        with self.assertRaises(TaskError) as caught:
            reserve(self.store, command="sleep 2", capacity=1)
        self.assertEqual(caught.exception.code, TOO_BUSY,
                         "a full daemon refuses, it does not queue")

    def test_a_replay_is_decided_before_capacity(self):
        first, _created = reserve(self.store)
        second, created = reserve(self.store, capacity=1)
        self.assertFalse(created, "the replay is decided before capacity")
        self.assertEqual(second["id"], first["id"],
                         "a re-sent live command is never too busy")

    def test_mark_running_records_the_process(self):
        snapshot, _created = reserve(self.store)
        marked = self.store.mark_running(snapshot["id"], 4242, 1000.5)
        self.assertEqual((marked["pid"], marked["started"]), (4242, 1000.5),
                         "the process and its start are published")
        self.assertEqual(self.store.snapshot_one(snapshot["id"])["pid"], 4242,
                         "and they are the durable record")

    def test_finish_seals_the_record(self):
        snapshot, _created = reserve(self.store)
        sealed = self.store.finish(snapshot["id"], FAILED, 3, "exit code 3")
        self.assertEqual((sealed["status"], sealed["exit"]), (FAILED, 3),
                         "the verdict and the exit code are the seal")
        self.assertEqual(sealed["error"], "exit code 3",
                         "the failure keeps its reason")
        self.assertIsNotNone(sealed["finished"], "a sealed run has an end")
        self.assertEqual(self.store.running_count(), 0,
                         "a sealed run is no longer running")

    def test_an_unknown_ticket_is_an_error(self):
        with self.assertRaises(LookupError):
            self.store.mark_running("t-absent", 1, 1.0)
        with self.assertRaises(LookupError):
            self.store.finish("t-absent", DONE)
        self.assertIsNone(self.store.snapshot_one("t-absent"),
                          "an unknown id has no record")

    def test_a_reload_turns_a_running_ticket_into_a_failed_one(self):
        snapshot, _created = reserve(self.store)
        reloaded = store_for(self.base)[0]
        record = reloaded.snapshot_one(snapshot["id"])
        self.assertEqual(record["status"], FAILED,
                         "no child survives the daemon that spawned it")
        self.assertEqual(record["error"], "interrupted by a restart",
                         "the recovered run says why it failed")
        self.assertIsNotNone(record["finished"],
                             "the recovered run carries an end")

    def test_a_reload_keeps_the_counter_moving(self):
        first, _created = reserve(self.store)
        reloaded = store_for(self.base)[0]
        second, created = reserve(reloaded)
        self.assertTrue(created,
                        "the recovered run is sealed, so this one is new")
        self.assertNotEqual(second["id"], first["id"],
                            "a reload must never reissue an id")
        self.assertGreater(counter(second["id"]), counter(first["id"]),
                           "the counter only rises")

    def test_a_finished_ticket_survives_a_reload_whole(self):
        """output() reads only as far as the persisted size, so a reload
        that lost a field would hide the log that is still on disk."""
        snapshot, _created = reserve(self.store)
        self.store.append_output(snapshot["id"], b"kept\n")
        sealed = self.store.finish(snapshot["id"], DONE, 0)
        self.store.save()
        reloaded = store_for(self.base)[0]
        self.assertEqual(reloaded.snapshot_one(snapshot["id"]), sealed,
                         "a sealed record reloads unchanged")
        self.assertEqual(reloaded.output(snapshot["id"])[0], b"kept\n",
                         "and its output is still readable")

    def test_snapshots_are_oldest_first_and_session_scoped(self):
        first, _created = reserve(self.store, session="s-1")
        second, _created = reserve(self.store, session="s-2")
        self.assertEqual([ticket["id"] for ticket in self.store.snapshot()],
                         [first["id"], second["id"]],
                         "the whole list keeps reservation order")
        self.assertEqual(
            [ticket["id"] for ticket in self.store.snapshot("s-2")],
            [second["id"]], "one session sees only its own tickets")

    def test_running_ids_name_the_live_tickets(self):
        first, _created = reserve(self.store, command="sleep 1")
        second, _created = reserve(self.store, command="sleep 2")
        self.assertEqual(self.store.running_ids(), [first["id"], second["id"]],
                         "both reservations are live")
        self.store.finish(first["id"], DONE, 0)
        self.assertEqual(self.store.running_ids(), [second["id"]],
                         "a sealed run leaves the live list")
        self.assertEqual(self.store.running_count(), 1,
                         "the count follows the live list")

    def test_output_is_paged_from_a_clamped_offset(self):
        snapshot, _created = reserve(self.store)
        self.assertEqual(self.store.append_output(snapshot["id"],
                                                  b"0123456789"), 10,
                         "the whole chunk is captured")
        self.assertEqual(self.store.output(snapshot["id"]),
                         (b"0123456789", 0, 10, 10),
                         "an unpaged read starts at the beginning")
        self.assertEqual(self.store.output(snapshot["id"], 3),
                         (b"3456789", 3, 10, 10),
                         "a paged read continues from its offset")
        self.assertEqual(self.store.output(snapshot["id"], 99),
                         (b"", 10, 10, 10),
                         "an offset past the end reads nothing at the end")

    def test_a_ticket_without_output_reads_empty(self):
        snapshot, _created = reserve(self.store)
        self.assertEqual(self.store.output(snapshot["id"]), (b"", 0, 0, 0),
                         "a run that printed nothing has a zero-length log")

    def test_output_of_an_unknown_ticket_is_an_error(self):
        with self.assertRaises(LookupError):
            self.store.output("t-absent")

    def test_the_output_cap_truncates_and_flags_the_record(self):
        capped = store_for(base_dir(), output_cap=4)[0]
        snapshot, _created = reserve(capped)
        self.assertEqual(capped.append_output(snapshot["id"], b"abcdefgh"), 4,
                         "the cap holds back the rest of the chunk")
        self.assertTrue(capped.snapshot_one(snapshot["id"])["truncated"],
                        "a clipped append is recorded, not silent")
        self.assertEqual(capped.append_output(snapshot["id"], b"x"), 0,
                         "a full log accepts nothing more")
        self.assertEqual(capped.output(snapshot["id"])[0], b"abcd",
                         "the log keeps exactly what the cap allowed")

    def test_the_record_cap_drops_the_oldest_finished_ticket(self):
        capped = store_for(base_dir(), record_cap=2)[0]
        ids = []
        for _index in range(3):
            snapshot, _created = reserve(capped, command="true")
            ids.append(snapshot["id"])
            capped.append_output(snapshot["id"], b"log\n")
            capped.finish(snapshot["id"], DONE, 0)
        self.assertIsNone(capped.snapshot_one(ids[0]),
                          "the oldest finished ticket is evicted")
        self.assertFalse(os.path.exists(capped.log_path(ids[0])),
                         "an evicted ticket's output goes with it")
        self.assertIsNotNone(capped.snapshot_one(ids[2]),
                             "the newest finished ticket stays")

    def test_discard_drops_a_reservation_and_its_log(self):
        snapshot, _created = reserve(self.store)
        self.store.append_output(snapshot["id"], b"x")
        self.assertTrue(self.store.discard(snapshot["id"]),
                        "a live reservation is discarded")
        self.assertIsNone(self.store.snapshot_one(snapshot["id"]),
                          "a discarded reservation is gone")
        self.assertFalse(os.path.exists(self.store.log_path(snapshot["id"])),
                         "and so is its log")
        self.assertFalse(self.store.discard(snapshot["id"]),
                         "an already dropped reservation is not an error")

    def test_remove_drops_a_finished_ticket_and_its_log(self):
        snapshot, _created = reserve(self.store)
        self.store.append_output(snapshot["id"], b"bye\n")
        self.store.finish(snapshot["id"], DONE, 0)
        self.assertTrue(self.store.remove(snapshot["id"]),
                        "a finished ticket is removable")
        self.assertIsNone(self.store.snapshot_one(snapshot["id"]),
                          "a removed ticket is gone")
        self.assertFalse(os.path.exists(self.store.log_path(snapshot["id"])),
                         "and its output goes with it")

    def test_remove_refuses_a_running_ticket(self):
        snapshot, _created = reserve(self.store)
        with self.assertRaises(TaskError) as caught:
            self.store.remove(snapshot["id"])
        self.assertEqual(caught.exception.code, TICKET_RUNNING,
                         "a live run is refused, not removed")
        self.assertIsNotNone(self.store.snapshot_one(snapshot["id"]),
                             "the refused ticket is still there")

    def test_remove_of_an_unknown_ticket_is_an_error(self):
        with self.assertRaises(LookupError):
            self.store.remove("t-absent")

    def test_reset_all_empties_the_store_but_not_the_counter(self):
        first, _created = reserve(self.store, command="sleep 1")
        second, _created = reserve(self.store, command="sleep 2")
        self.store.append_output(first["id"], b"x")
        self.assertEqual(self.store.reset_all(), 2,
                         "the reset reports what it removed")
        self.assertEqual(self.store.snapshot(), [], "the store is empty")
        self.assertFalse(os.path.exists(self.store.log_path(first["id"])),
                         "no log outlives the reset")
        third, created = reserve(self.store)
        self.assertTrue(created, "an emptied store still reserves")
        self.assertGreater(counter(third["id"]), counter(second["id"]),
                           "a reset never hands an id out twice")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.base = base_dir()
        self.store, config = store_for(self.base, cancel_grace=0.2)
        self.runner = TicketRunner(self.store, config)
        self.addCleanup(self.settle)

    def settle(self):
        """Every child ends and every record is sealed before the scratch
        tree goes: a late seal would write the tree back after teardown."""
        ticket_ids = self.store.running_ids()
        self.runner.stop()
        for ticket_id in ticket_ids:
            self.runner.wait(ticket_id, 10)

    def submit(self, command, timeout=0.0, env=None):
        return self.runner.submit("s-1", REPO, command, timeout, env)

    def test_a_ticket_captures_its_output_and_exit(self):
        snapshot, reused = self.submit(
            "printf 'out-marker'; printf 'err-marker' >&2")
        self.assertFalse(reused, "a new command is not a replay")
        settled = self.runner.wait(snapshot["id"], 10)
        self.assertEqual((settled["status"], settled["exit"]), (DONE, 0),
                         "a clean exit is a done ticket")
        output = self.store.output(snapshot["id"])[0]
        self.assertIn(b"out-marker", output, "the log holds stdout")
        self.assertIn(b"err-marker", output,
                      "stderr is merged into the ticket's one log")

    def test_a_non_zero_exit_is_a_failure_with_its_code(self):
        snapshot, _reused = self.submit("exit 3")
        settled = self.runner.wait(snapshot["id"], 10)
        self.assertEqual(settled["status"], FAILED,
                         "a non-zero exit is a failure")
        self.assertEqual(settled["error"], "exit code 3",
                         "the failure names its exit code")
        self.assertEqual(settled["exit"], 3, "and reports it as the exit")

    def test_an_environment_value_reaches_the_child(self):
        snapshot, _reused = self.submit(
            "printf '%s' \"$PI_DAEMON_TASKS_TEST\"",
            env={"PI_DAEMON_TASKS_TEST": "seen"})
        self.runner.wait(snapshot["id"], 10)
        self.assertEqual(self.store.output(snapshot["id"])[0], b"seen",
                         "a submit-time variable reaches the run")

    @unittest.skipIf(os.name == "nt", "a shell script is not executable here")
    def test_a_ticket_runs_in_the_shell_it_was_handed(self):
        # The owner of an offloaded task resolves the shell its settings
        # chose and passes it as PI_SHELL; the ticket runs there, not in
        # whatever shell the service happens to find.
        marker = os.path.join(self.base, "shell-used")
        shell = os.path.join(self.base, "a-shell")
        with open(shell, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nprintf '%s' \"$*\" > " + marker + "\n")
        os.chmod(shell, 0o755)
        snapshot, _reused = self.submit("echo ticket-command",
                                        env={"PI_SHELL": shell})
        settled = self.runner.wait(snapshot["id"], 10)
        self.assertEqual(settled["status"], DONE, settled.get("error"))
        with open(marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "-c echo ticket-command",
                             "the command ran in the handed shell")

    def test_a_timeout_ends_the_child_and_marks_it_failed(self):
        snapshot, _reused = self.submit("sleep 30", timeout=0.3)
        settled = self.runner.wait(snapshot["id"], 10)
        self.assertEqual(settled["status"], FAILED,
                         "a timed out run is a failure")
        self.assertEqual(settled["error"], "timed out",
                         "and says why it ended")
        self.assertEqual(settled["timeout"], 0.3,
                         "the enforced timeout is on the record")
        self.assertEqual(self.store.running_count(), 0,
                         "no child outlives the timeout that killed it")

    def test_a_resend_of_a_running_command_is_reused(self):
        first, _reused = self.submit("sleep 5")
        second, reused = self.submit("sleep 5")
        self.assertTrue(reused, "a re-sent live command is one ticket")
        self.assertEqual(second["id"], first["id"],
                         "the replay is the same ticket")
        self.runner.cancel(first["id"])

    def test_cancel_ends_the_child_and_names_it_cancelled(self):
        snapshot, _reused = self.submit("sleep 30")
        self.runner.cancel(snapshot["id"])
        self.assertEqual(self.runner.wait(snapshot["id"], 10)["status"],
                         CANCELLED, "a cancelled run is named, not failed")

    def test_cancel_of_an_unknown_ticket_is_an_error(self):
        with self.assertRaises(LookupError):
            self.runner.cancel("t-absent")

    def test_a_missing_directory_never_becomes_a_record(self):
        with self.assertRaises(TaskError) as caught:
            self.runner.submit("s-1", os.path.join(self.base, "gone"),
                               "true")
        self.assertEqual(caught.exception.code, SPAWN_FAILED,
                         "a spawn that cannot land is refused, not retried")
        self.assertEqual(self.store.snapshot(), [],
                         "a refused spawn leaves no ticket behind")

    def test_a_shutdown_refuses_new_tickets(self):
        self.runner.begin_shutdown()
        with self.assertRaises(TaskError) as caught:
            self.submit("true")
        self.assertEqual(caught.exception.code, TOO_BUSY,
                         "a closing runner accepts no new run")

    def test_stop_ends_every_live_ticket(self):
        first, _reused = self.submit("sleep 30")
        second, _reused = self.submit("sleep 31")
        self.assertEqual(self.store.running_count(), 2,
                         "both runs are live before the stop")
        self.runner.stop()
        for ticket_id in (first["id"], second["id"]):
            self.assertEqual(self.runner.wait(ticket_id, 10)["status"],
                             CANCELLED, ticket_id)
        self.assertEqual(self.store.running_count(), 0,
                         "no child outlives the stop that ended it")


if __name__ == "__main__":
    unittest.main()
