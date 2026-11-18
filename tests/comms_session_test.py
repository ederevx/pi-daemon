#!/usr/bin/env python3
"""The session wire over stub domains: what it asks, and what it answers.

The wire owns no state, so every question here goes to a stub that records
it: a sessions stub for what a run is doing and printing, and a transcripts
stub for what a catalog lists and what one conversation did. The tests hold
the projection (list, conversations, open), the feed's page shape (output,
activity, state, reset, ended, budget), and the refusals a caller must get
before anything is read.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "pi", "lib"))

from pi_daemon.comms.session_wire import (  # noqa: E402
    ACTIVITY_EVENTS, SessionWire, feed_key)
from pi_daemon.config import ServiceConfig  # noqa: E402
from pi_daemon.sessions.error import SessionError  # noqa: E402
from pi_daemon.transcripts.error import CatalogError  # noqa: E402
from pi_daemon.transport.rpc_codec import INVALID_PARAMS, RpcError  # noqa: E402

FILE = "/home/a/.pi/agent/sessions/-home-a/2026-01-01T00-00-00_s1.jsonl"


def config(**overrides):
    return ServiceConfig.load(overrides=overrides)


class FakeSessions:
    """The sessions domain as the wire asks it."""

    def __init__(self):
        self.live = {"pi-a": {"id": "k-a", "file": FILE}}
        self.views = [{"name": "pi-a", "state": "busy", "dir": "/home/a",
                       "attached": True, "clients": 1, "file": FILE,
                       "phase": None}]
        self.outputs = {}
        self.states = {}
        self.seals = {}
        self.opened = []
        self.inputs = []
        self.stopped = []

    def sessions(self, include_stopped=False):
        return list(self.views)

    def stopped_count(self):
        return 2

    def key(self, name):
        return self.live[name]["id"] if name in self.live else None

    def recorded_file(self, name):
        return self.live.get(name, {}).get("file")

    def recorded(self, name):
        if name not in self.live:
            return None
        return {"name": name, "dir": "/home/a", "file": self.live[name]["file"]}

    def run_state(self, name):
        if name not in self.live:
            raise SessionError("no-session")
        return {"name": name,
                "record": {"state": "busy", "seq": self.states.get(name, 3),
                           "ended": self.seals.get(name), "history": []},
                "hosted": True}

    def output(self, name, offset, limit):
        if name not in self.outputs:
            raise SessionError("no-session")
        tail = self.outputs[name]
        chunk = tail["text"][offset:offset + limit]
        return {"generation": tail.get("generation", 1),
                "offset": offset, "next": offset + len(chunk),
                "text": chunk, "skipped": 0,
                "end": len(tail["text"]), "ended": tail.get("ended")}

    def open(self, file, dir_=None):
        self.opened.append((file, dir_))
        return {"status": "hosted", "name": "pi-new", "state": "busy",
                "dir": dir_ or "/tmp", "file": file, "created": True}

    def input(self, name, text):
        self.inputs.append((name, text))
        return {"name": name, "bytes": len(text)}

    def stop(self, name):
        self.stopped.append(name)
        return {"name": name, "stopped": True}


class FakeCatalog:
    """The conversation catalog as the wire asks it."""

    def __init__(self):
        self.asked = []
        self.activities = {}
        self.resolved = {FILE: {"sessionId": "sess-1", "file": FILE,
                               "cwd": "/home/a"}}

    def conversations(self, dir_, limit=None, after=None):
        self.asked.append(("conversations", dir_, limit, after))
        if after == "expired":
            raise CatalogError("bad-cursor")
        return {"conversations": [{"sessionId": "sess-1", "file": FILE}],
                "next": None, "total": 1}

    def resolve(self, session_id):
        for entry in self.resolved.values():
            if entry["sessionId"] == session_id:
                return entry
        raise CatalogError("ambiguous-session")

    def entry(self, file):
        return self.resolved.get(file)

    def activity(self, file, cursor, text_chars, page):
        self.asked.append(("activity", file, cursor, text_chars, page))
        events = self.activities.get(file, [])
        if cursor is not None:
            events = [event for event in events
                      if event["entryId"] > cursor]
        return {"events": events[:page], "next":
                events[0]["entryId"] if events else cursor,
                "hasMore": len(events) > page}


class FakeTranscripts:
    """The transcripts domain: ownership and the catalog it serves."""

    def __init__(self, owned=(FILE,), catalog=None):
        self.owned = set(owned)
        self._catalog = catalog or FakeCatalog()

    def owns(self, file):
        return file in self.owned

    def catalog(self):
        return self._catalog


class WireCase(unittest.TestCase):

    def wire(self, sessions=None, transcripts=None, **overrides):
        self.sessions = sessions or FakeSessions()
        self.transcripts = transcripts or FakeTranscripts()
        return SessionWire(self.sessions, self.transcripts,
                           config(**overrides), "node-a")

    def refused(self, call, *args):
        with self.assertRaises(RpcError) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, INVALID_PARAMS)
        return str(caught.exception.message)


class ListTests(WireCase):

    def test_a_live_listing_projects_the_domain_fields(self):
        answer = self.wire().list(None, {})
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(answer["nodeId"], "node-a")
        self.assertEqual(answer["stopped"], 2)
        self.assertEqual(answer["sessions"][0]["name"], "pi-a")
        self.assertEqual(answer["sessions"][0]["state"], "busy")
        self.assertNotIn("fileExists", answer["sessions"][0])

    def test_all_lists_the_ended_records_too(self):
        sessions = FakeSessions()
        sessions.views.append({"name": "pi-b", "state": None,
                               "dir": "/home/b", "attached": False,
                               "clients": 0, "file": None, "phase": "exit",
                               "fileExists": False})
        answer = self.wire(sessions).list(None, {"all": True})
        self.assertEqual(answer["stopped"], 0)
        self.assertEqual(len(answer["sessions"]), 2)
        self.assertEqual(answer["sessions"][1]["phase"], "exit")

    def test_conversations_passes_the_page_through(self):
        wire = self.wire()
        answer = wire.conversations(None, {"dir": "/home/a", "limit": 5,
                                           "after": None})
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(answer["total"], 1)
        self.assertEqual(self.transcripts._catalog.asked,
                         [("conversations", "/home/a", 5, None)])

    def test_a_bad_cursor_is_the_catalogs_refusal(self):
        wire = self.wire()
        self.assertEqual(self.refused(wire.conversations, None,
                                      {"dir": "/home/a", "after": "expired"}),
                         "bad-cursor")

    def test_a_listing_without_a_catalog_is_refused(self):
        wire = self.wire(transcripts=FakeTranscripts())
        self.transcripts._catalog = None
        self.refused(wire.conversations, None, {"dir": "/home/a"})


class OpenTests(WireCase):

    def test_a_name_reaches_the_run_that_holds_it(self):
        answer = self.wire().open(None, {"name": "pi-a"})
        self.assertEqual(answer["status"], "hosted")
        self.assertEqual(self.sessions.opened, [(FILE, "/home/a")])

    def test_an_unknown_name_is_refused(self):
        wire = self.wire()
        self.assertEqual(self.refused(wire.open, None, {"name": "nope"}),
                         "no-session")

    def test_a_name_without_a_file_is_refused(self):
        sessions = FakeSessions()
        sessions.live["pi-z"] = {"id": "k-z", "file": None}
        wire = self.wire(sessions)
        self.assertEqual(self.refused(wire.open, None, {"name": "pi-z"}),
                         "no-file")

    def test_a_file_outside_the_store_is_refused(self):
        wire = self.wire()
        self.assertEqual(self.refused(wire.open, None, {"file": "/etc/x"}),
                         "bad-transcript")

    def test_a_file_uses_the_conversations_own_directory(self):
        answer = self.wire().open(None, {"file": FILE})
        self.assertEqual(answer["name"], "pi-new")
        self.assertEqual(self.sessions.opened, [(FILE, "/home/a")])

    def test_an_explicit_directory_wins_over_the_header(self):
        self.wire().open(None, {"file": FILE, "dir": "/srv/x"})
        self.assertEqual(self.sessions.opened, [(FILE, "/srv/x")])

    def test_a_session_id_resolves_through_the_catalog(self):
        self.wire().open(None, {"sessionId": "sess-1"})
        self.assertEqual(self.sessions.opened, [(FILE, "/home/a")])

    def test_exactly_one_target_is_required(self):
        wire = self.wire()
        self.refused(wire.open, None, {})
        self.refused(wire.open, None, {"name": "pi-a", "file": FILE})

    def test_a_non_transcript_file_is_refused_without_a_catalog(self):
        self.transcripts = None
        wire = self.wire()
        self.transcripts._catalog = None
        self.refused(wire.open, None, {"sessionId": "sess-1"})


class ControlTests(WireCase):

    def test_input_writes_the_text_and_reports_the_bytes(self):
        answer = self.wire().input(None, {"name": "pi-a", "text": "hi\r"})
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(self.sessions.inputs, [("pi-a", "hi\r")])
        self.assertEqual(answer["bytes"], 3)

    def test_input_needs_text(self):
        self.refused(self.wire().input, None, {"name": "pi-a"})

    def test_stop_names_the_session(self):
        answer = self.wire().stop(None, {"name": "pi-a"})
        self.assertEqual(self.sessions.stopped, ["pi-a"])
        self.assertEqual(answer["stopped"], True)

    def test_a_session_error_is_a_refusal(self):
        sessions = FakeSessions()
        wire = self.wire(sessions)

        def refuse(name, text):
            raise SessionError("no-session")

        sessions.input = refuse
        self.assertEqual(self.refused(wire.input, None,
                                      {"name": "pi-a", "text": "x"}),
                         "no-session")

    def test_control_methods_are_absent_when_they_are_off(self):
        wire = self.wire()
        self.assertEqual(sorted(wire.handlers(False)),
                         ["comms.session.conversations",
                          "comms.session.list"])


class FeedTests(WireCase):

    def page(self, wire, params):
        plan = wire.feed_plan(None, params)
        reply, wake = plan["probe"](False)
        return plan, reply, wake

    def test_the_first_page_needs_no_cursor_and_answers_at_once(self):
        sessions = self.sessions_for({"text": "hello"})
        wire = self.wire(sessions)
        plan, reply, wake = self.page(
            wire, {"name": "pi-a", "include": ["output", "state"]})
        self.assertEqual(plan["timeout"], 0.0)
        self.assertIsNone(wake)
        self.assertEqual(plan["key"], feed_key("k-a"))
        self.assertEqual(reply["status"], "ok")
        types = [event["type"] for event in reply["events"]]
        self.assertEqual(types, ["output", "state"])
        self.assertEqual(reply["events"][0]["text"], "hello")
        self.assertEqual(reply["cursor"]["output"], 5)
        self.assertEqual(reply["cursor"]["stateSeq"], 3)

    def test_a_page_with_nothing_new_waits_for_a_signal(self):
        wire = self.wire(self.sessions_for({"text": "hello"}))
        plan = wire.feed_plan(None, {"name": "pi-a"})
        cursor = {"name": "pi-a", "generation": 1, "output": 5,
                  "activity": None, "stateSeq": 3}
        reply, wake = plan["probe"](False)
        self.assertIsNotNone(reply)
        plan = wire.feed_plan(None, {"name": "pi-a", "cursor": cursor,
                                     "include": ["output"]})
        reply, wake = plan["probe"](False)
        self.assertIsNone(reply)
        self.assertGreater(wake, 0.0)
        # The settle window bounds a fresh-looking tail, the poll window a
        # state-only one.
        plan = wire.feed_plan(None, {"name": "pi-a", "cursor": cursor,
                                     "include": ["state"]})
        _reply, poll = plan["probe"](False)
        self.assertGreater(poll, wake)

    def test_a_deadline_answers_a_page_that_found_nothing(self):
        wire = self.wire(self.sessions_for({"text": "hello"}))
        cursor = {"name": "pi-a", "generation": 1, "output": 5,
                  "activity": None, "stateSeq": 3}
        plan = wire.feed_plan(None, {"name": "pi-a", "cursor": cursor,
                                     "include": ["state"]})
        reply, wake = plan["probe"](True)
        self.assertIsNone(wake)
        self.assertEqual(reply["status"], "timed-out")
        self.assertEqual(reply["events"], [])

    def test_a_restarted_run_is_announced_and_read_from_its_start(self):
        wire = self.wire(self.sessions_for({"text": "again",
                                            "generation": 2}))
        cursor = {"name": "pi-a", "generation": 1, "output": 9,
                  "activity": None, "stateSeq": 3}
        plan = wire.feed_plan(None, {"name": "pi-a", "cursor": cursor,
                                     "include": ["output"]})
        reply, _wake = plan["probe"](False)
        self.assertEqual(reply["events"][0]["type"], "reset")
        self.assertEqual(reply["events"][0]["generation"], 2)
        self.assertEqual(reply["events"][1]["text"], "again")

    def test_an_ended_run_says_so_once_its_tail_is_read(self):
        wire = self.wire(self.sessions_for({"text": "bye", "ended": "exit"}))
        plan = wire.feed_plan(None, {"name": "pi-a",
                                     "include": ["output"]})
        reply, _wake = plan["probe"](False)
        self.assertEqual(reply["status"], "ended")
        self.assertEqual(reply["events"][-1],
                         {"type": "ended", "reason": "exit"})

    def test_an_activity_only_watch_still_learns_the_run_ended(self):
        sessions = self.sessions_for({"text": "bye"})
        sessions.seals["pi-a"] = {"verdict": "exited", "cause": "exit"}
        wire = self.wire(sessions)
        plan = wire.feed_plan(None, {"name": "pi-a",
                                     "include": ["activity"]})
        reply, wake = plan["probe"](False)
        self.assertIsNone(wake)
        self.assertEqual(reply["status"], "ended")
        self.assertEqual(reply["events"], [{"type": "ended",
                                             "reason": "exit"}])

    def test_an_activity_only_watch_never_ends_before_its_activity(self):
        sessions = self.sessions_for({"text": "bye"})
        sessions.seals["pi-a"] = {"verdict": "exited", "cause": "exit"}
        wire = self.wire(sessions)
        self.transcripts._catalog.activities[FILE] = [
            {"entryId": f"m{index:02d}", "kind": "tool_call", "text": "ls"}
            for index in range(ACTIVITY_EVENTS + 1)]
        plan = wire.feed_plan(None, {"name": "pi-a",
                                     "include": ["activity"]})
        reply, _wake = plan["probe"](False)
        self.assertEqual(reply["status"], "ok")
        self.assertTrue(reply["more"])
        self.assertNotIn("ended", [event["type"] for event in reply["events"]])
        plan = wire.feed_plan(None, {"name": "pi-a", "cursor": reply["cursor"],
                                     "include": ["activity"]})
        page, _wake = plan["probe"](False)
        self.assertEqual(page["status"], "ended")
        self.assertEqual(page["events"][-1], {"type": "ended",
                                               "reason": "exit"})

    def test_an_entry_too_large_for_the_page_never_hides_the_ending(self):
        sessions = self.sessions_for({"text": "bye"})
        sessions.seals["pi-a"] = {"verdict": "exited", "cause": "exit"}
        wire = self.wire(sessions)
        self.transcripts._catalog.activities[FILE] = [
            {"entryId": "m00", "kind": "tool_call", "text": "x" * 500},
            {"entryId": "m01", "kind": "tool_call", "text": "ls"}]
        plan = wire.feed_plan(None, {"name": "pi-a", "maxBytes": 120,
                                     "include": ["activity"]})
        reply, _wake = plan["probe"](False)
        # The entry no page could carry still names itself and moves the
        # cursor, so the page after it can report the run's ending.
        self.assertEqual(reply["events"][0]["entryId"], "m00")
        self.assertTrue(reply["events"][0].get("elided"))
        self.assertNotEqual(reply["cursor"]["activity"], None)
        cursor = reply["cursor"]
        for _page in range(3):
            plan = wire.feed_plan(None, {"name": "pi-a", "cursor": cursor,
                                         "include": ["activity"],
                                         "maxBytes": 120})
            reply, _wake = plan["probe"](False)
            if reply is None or reply["status"] == "ended":
                break
            cursor = reply["cursor"]
        self.assertIsNotNone(reply)
        self.assertEqual(reply["status"], "ended")

    def test_a_partial_page_never_declares_the_run_ended(self):
        sessions = self.sessions_for({"text": "z" * 400, "ended": "exit"})
        wire = self.wire(sessions)
        plan = wire.feed_plan(None, {"name": "pi-a", "maxBytes": 120,
                                     "include": ["output"]})
        reply, _wake = plan["probe"](False)
        self.assertEqual(reply["status"], "ok")
        self.assertEqual([event["type"] for event in reply["events"]],
                         ["output"])
        self.assertLess(reply["cursor"]["output"], 400)

    def test_activity_is_read_from_the_conversation(self):
        sessions = self.sessions_for({"text": ""})
        wire = self.wire(sessions)
        self.transcripts._catalog.activities[FILE] = [
            {"entryId": "m1", "kind": "tool_call", "text": "ls"},
        ]
        plan = wire.feed_plan(None, {"name": "pi-a",
                                     "include": ["activity"]})
        reply, _wake = plan["probe"](False)
        self.assertEqual(reply["events"], [
            {"type": "activity", "entryId": "m1", "kind": "tool_call",
             "text": "ls"}])
        self.assertEqual(reply["cursor"]["activity"], "m1")

    def test_a_page_never_outgrows_its_byte_budget(self):
        sessions = self.sessions_for({"text": "x" * 400})
        wire = self.wire(sessions)
        self.transcripts._catalog.activities[FILE] = [
            {"entryId": f"m{index}", "kind": "tool_call", "text": "y" * 200}
            for index in range(10)]
        plan = wire.feed_plan(None, {"name": "pi-a", "maxBytes": 900,
                                     "include": ["activity"]})
        reply, _wake = plan["probe"](False)
        self.assertLessEqual(len(reply["events"]), 4)
        self.assertTrue(reply["more"])
        self.assertLess(reply["cursor"]["activity"], "m9")

    def test_a_feed_for_a_session_that_left_ends_instead_of_failing(self):
        wire = self.wire(FakeSessions())
        plan = wire.feed_plan(None, {"name": "pi-a", "cursor":
                                     {"name": "pi-a"}})
        self.sessions.live.clear()
        reply, _wake = plan["probe"](False)
        self.assertEqual(reply["status"], "ended")
        self.assertEqual(reply["events"], [{"type": "ended",
                                           "reason": "gone"}])

    def test_an_unknown_session_is_refused_before_any_hold(self):
        self.refused(self.wire().feed_plan, None, {"name": "nope"})

    def test_a_cursor_belongs_to_one_session(self):
        wire = self.wire(self.sessions_for({"text": "x"}))
        self.refused(wire.feed_plan, None, {
            "name": "pi-a", "cursor": {"name": "pi-b"}})

    def test_a_malformed_cursor_is_refused(self):
        wire = self.wire(self.sessions_for({"text": "x"}))
        self.refused(wire.feed_plan, None, {
            "name": "pi-a", "cursor": {"name": "pi-a", "output": -1}})
        self.refused(wire.feed_plan, None, {
            "name": "pi-a", "cursor": {"name": "pi-a", "activity": 4}})

    def test_include_names_the_three_sources(self):
        wire = self.wire(self.sessions_for({"text": "x"}))
        self.refused(wire.feed_plan, None, {"name": "pi-a",
                                            "include": ["stdout"]})
        self.refused(wire.feed_plan, None, {"name": "pi-a", "include": []})

    def test_a_hold_is_clamped_to_the_advertised_maximum(self):
        wire = self.wire(self.sessions_for({"text": "x"}),
                         comms_wait_max=0.5)
        cursor = {"name": "pi-a"}
        plan = wire.feed_plan(None, {"name": "pi-a", "cursor": cursor,
                                     "timeoutMs": 600000})
        self.assertEqual(plan["timeout"], 0.5)

    def test_the_page_budget_never_outgrows_the_reply_frame(self):
        self.assertEqual(config(frame_bytes=2048, comms_payload_bytes=256,
                                comms_frame_margin=512).feed_page_bytes,
                         1536)
        self.assertEqual(config().feed_page_bytes, 16384)

    def sessions_for(self, output):
        sessions = FakeSessions()
        sessions.outputs["pi-a"] = output
        return sessions


if __name__ == "__main__":
    unittest.main(verbosity=2)
