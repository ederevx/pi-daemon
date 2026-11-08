#!/usr/bin/env python3
"""The transcripts domain: the seven control verbs over a real indexed
conversation, the handle/session/file precedence, and every rejection.

The legacy wire suite drove these verbs through the monolith's control
port; this suite drives the new domain's operations, which is where the
same promises now live. Zero dependencies (stdlib only).
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "lib"))

from pi_daemon.config import ServiceConfig
from pi_daemon.transcripts.operations import TranscriptOperations
from pi_daemon.transcripts.service import TranscriptService

HEADER = ('{"type":"session","version":3,"id":"wire-sess",'
          '"timestamp":"2026-01-01T00:00:00.000Z","cwd":"/home/wire"}\n')
ENTRIES = [
    '{"type":"message","id":"m1","parentId":null,'
    '"timestamp":"2026-01-01T00:00:01.000Z",'
    '"message":{"role":"user","content":"first"}}\n',
    '{"type":"message","id":"m2","parentId":"m1",'
    '"timestamp":"2026-01-01T00:00:02.000Z",'
    '"message":{"role":"assistant","content":"second"}}\n',
    '{"type":"message","id":"m3","parentId":"m2",'
    '"timestamp":"2026-01-01T00:00:03.000Z",'
    '"message":{"role":"user","content":"third"}}\n',
]

SCRATCH = tempfile.mkdtemp(prefix="pi-transcripts-")


def tearDownModule():
    shutil.rmtree(SCRATCH, ignore_errors=True)


def root_for():
    """One conversation store per test, so no sidecar is shared."""
    root = tempfile.mkdtemp(dir=SCRATCH)
    os.makedirs(os.path.join(root, "--home-wire--"))
    return root


def conversation(root, name="wire"):
    path = os.path.join(root, "--home-wire--", name + ".jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(HEADER)
        handle.writelines(ENTRIES)
    return path


def config():
    return ServiceConfig(transcript_index=True, transcript_index_interval=0.0,
                         session_store_root="")


def service_for(root, recorded=None, live=None):
    service = TranscriptService(config(), root, live or (lambda: []),
                                recorded or (lambda name: None))
    service.start()
    return service


class TranscriptsTest(unittest.TestCase):
    def setUp(self):
        self.root = root_for()
        self.file = conversation(self.root)
        self.announced = {"pi-a": self.file}
        self.service = service_for(self.root,
                                   recorded=self.announced.get)
        self.ops = TranscriptOperations(self.service)

    def tearDown(self):
        self.service.stop()

    def call(self, cmd, **request):
        request["cmd"] = cmd
        return self.ops.handlers()[cmd](request)

    def open_handle(self):
        reply = self.call("transcript-open", **{"file": self.file})
        self.assertTrue(reply.get("ok"), reply)
        return reply["transcriptId"]

    # -- reads -------------------------------------------------------------

    def test_open_reports_meta_and_a_handle(self):
        reply = self.call("transcript-open", **{"file": self.file})
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(reply["transcriptId"])
        self.assertEqual(reply["index"]["state"], "ok")
        self.assertEqual(reply["sessionId"], "wire-sess")

    def test_path_follows_the_handle_leaf_to_root(self):
        handle = self.open_handle()
        reply = self.call("transcript-path", transcriptId=handle)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["leafId"], "m3")
        self.assertEqual([entry["id"] for entry in reply["entries"]],
                         ["m3", "m2", "m1"])

    def test_entries_page_then_the_since_cursor(self):
        handle = self.open_handle()
        page = self.call("transcript-entries", transcriptId=handle, limit=2)
        self.assertTrue(page["ok"], page)
        self.assertEqual([entry["id"] for entry in page["entries"]],
                         ["m1", "m2"])
        self.assertTrue(page["hasMore"])
        rest = self.call("transcript-entries", transcriptId=handle,
                         since=page["next"])
        self.assertEqual([entry["id"] for entry in rest["entries"]], ["m3"])
        self.assertFalse(rest["hasMore"])

    def test_a_cursor_that_names_no_entry_is_a_bad_cursor(self):
        handle = self.open_handle()
        reply = self.call("transcript-entries", transcriptId=handle,
                          since="nope")
        self.assertEqual(reply, {"ok": False, "error": "bad-cursor"})

    def test_an_entry_aligned_range_round_trips_a_real_line(self):
        handle = self.open_handle()
        entries = self.call("transcript-entries", transcriptId=handle,
                            ids=["m2"])["entries"]
        record = entries[0]
        reply = self.call("transcript-range", transcriptId=handle,
                          offset=record["o"], length=record["l"],
                          align="entry")
        self.assertTrue(reply["ok"], reply)
        line = base64.b64decode(reply["bytes"])
        self.assertEqual(json.loads(line)["id"], "m2")

    def test_the_tree_names_nodes_and_roots(self):
        handle = self.open_handle()
        reply = self.call("transcript-tree", transcriptId=handle)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual([node["id"] for node in reply["nodes"]],
                         ["m1", "m2", "m3"])
        self.assertEqual(reply["roots"], ["m1"])

    def test_stat_never_names_the_file(self):
        handle = self.open_handle()
        reply = self.call("transcript-stat", transcriptId=handle)
        self.assertTrue(reply["ok"], reply)
        self.assertNotIn("file", reply)
        self.assertEqual(reply["sessionId"], "wire-sess")

    def test_a_session_name_resolves_to_its_announced_file(self):
        reply = self.call("transcript-path", session="pi-a")
        self.assertTrue(reply.get("ok"), reply)
        self.assertEqual(reply["leafId"], "m3")

    def test_a_handle_outranks_the_file_and_the_session(self):
        other = conversation(self.root, "other")
        handle = self.open_handle()
        reply = self.call("transcript-stat", transcriptId=handle,
                          file=other, session="pi-a")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["sessionId"], "wire-sess")

    def test_an_id_echoes_on_every_reply(self):
        reply = self.call("transcript-stat", file=self.file, id="r-1")
        self.assertEqual(reply["id"], "r-1")
        failure = self.call("transcript-stat", file="/nope", id="r-2")
        self.assertEqual(failure["id"], "r-2")
        self.assertFalse(failure["ok"])

    # -- rebuild -----------------------------------------------------------

    def test_rebuild_replaces_the_sidecar(self):
        handle = self.open_handle()
        sidecar = self.service.reader(self.file).index.index_path(self.file)
        self.assertTrue(os.path.isfile(sidecar))
        os.remove(sidecar)
        reply = self.call("transcript-rebuild", transcriptId=handle)
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(os.path.isfile(sidecar))

    # -- rejections --------------------------------------------------------

    def test_an_unknown_handle_is_no_such_transcript(self):
        reply = self.call("transcript-stat", transcriptId="t-nope")
        self.assertEqual(reply, {"ok": False, "error": "no-such-transcript"})

    def test_a_file_outside_the_store_is_a_bad_transcript(self):
        outside = os.path.join(SCRATCH, "outside.jsonl")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write(HEADER)
        reply = self.call("transcript-open", **{"file": outside})
        self.assertEqual(reply, {"ok": False, "error": "bad-transcript"})

    def test_a_directory_that_is_not_a_conversation_is_no_index(self):
        empty = os.path.join(self.root, "--home-wire--", "empty.jsonl")
        open(empty, "w", encoding="utf-8").close()
        reply = self.call("transcript-stat", **{"file": empty})
        self.assertEqual(reply, {"ok": False, "error": "no-index"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
