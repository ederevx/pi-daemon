#!/usr/bin/env python3
"""Unit tests for the conversation index (pi/lib/pi_transcript.py).

Covers the build/tail/rebuild algorithm, the tolerant index reader, the
skip rules, the orphan sweep, concurrent syncs and the watch's live-file
gate. Scratch lives under ~/tmp, never /tmp; stdlib only.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi", "lib"))
import pi_transcript  # noqa: E402


SESSION_LINE = {
    "type": "session", "version": 3, "id": "sess-1",
    "timestamp": "2026-10-01T00:00:00Z", "cwd": "/tmp",
}


def entry(entry_id, parent, kind="message", **extra):
    obj = {"type": kind, "id": entry_id, "parentId": parent,
           "timestamp": "2026-10-01T00:00:01Z"}
    obj.update(extra)
    return obj


class Fixture:
    """A scratch session store with slug dirs and one conversation."""

    def __init__(self, testcase):
        self.root = tempfile.mkdtemp(
            prefix="pi-transcript-", dir=os.path.expanduser("~/tmp"))
        testcase.addCleanup(shutil.rmtree, self.root, True)
        self.slug = os.path.join(self.root, "slug")
        os.makedirs(self.slug, exist_ok=True)
        self.file = os.path.join(self.slug, "sess-1.jsonl")

    def write(self, entries, session=None):
        lines = [json.dumps(session if session is not None else SESSION_LINE)]
        for obj in entries:
            lines.append(json.dumps(obj))
        with open(self.file, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return self.file

    def append_raw(self, text):
        with open(self.file, "ab") as fh:
            fh.write(text.encode("utf-8"))

    def size(self):
        return os.path.getsize(self.file)


class IndexBuildTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(self)
        self.idx = pi_transcript.TranscriptIndex()
        self.path = pi_transcript.TranscriptIndex.index_path(self.fx.file)

    def load(self):
        return pi_transcript.TranscriptIndexFile(self.path).load()

    def test_build_indexes_every_entry_in_order(self):
        self.fx.write([
            entry("m1", None),
            entry("c1", "m1", kind="compaction",
                  firstKeptEntryId="m1"),
            entry("e1", "c1", kind="context_edit", targetId="m1"),
            entry("b1", "c1", kind="branch_summary", fromId="m1"),
        ])
        state = self.idx.sync(self.fx.file)
        self.assertEqual(state, "ok")
        header, records = self.load()
        self.assertEqual(header["v"], pi_transcript.INDEX_VERSION)
        self.assertEqual(header["sessionId"], "sess-1")
        self.assertEqual(header["headerV"], 3)
        self.assertEqual(header["entryCount"], 5)
        self.assertEqual(header["coveredEnd"], self.fx.size())
        self.assertEqual(header["fileSize"], self.fx.size())
        self.assertEqual(records[0]["o"], 0)
        self.assertEqual(records[0]["t"], "session")
        self.assertEqual(records[0]["id"], "sess-1")
        # Contiguity from byte zero through the covered end.
        offset = 0
        for rec in records:
            self.assertEqual(rec["o"], offset)
            self.assertGreater(rec["l"], 0)
            offset += rec["l"]
        self.assertEqual(offset, header["coveredEnd"])
        self.assertEqual(records[2]["firstKept"], "m1")
        self.assertEqual(records[3]["target"], "m1")
        self.assertEqual(records[4]["from"], "m1")

    def test_unchanged_file_is_a_cheap_ok(self):
        self.fx.write([entry("m1", None)])
        self.assertEqual(self.idx.sync(self.fx.file), "ok")
        self.assertEqual(self.idx.sync(self.fx.file), "ok")

    def test_tail_appends_only_new_records(self):
        self.fx.write([entry("m1", None)])
        self.idx.sync(self.fx.file)
        _, first = self.load()
        self.fx.append_raw(json.dumps(entry("m2", "m1")) + "\n")
        self.assertEqual(self.idx.sync(self.fx.file), "ok")
        header, second = self.load()
        self.assertEqual(header["entryCount"], 3)
        self.assertEqual(header["coveredEnd"], self.fx.size())
        self.assertEqual(second[:len(first)], first)

    def test_torn_tail_is_left_uncovered(self):
        self.fx.write([entry("m1", None)])
        self.idx.sync(self.fx.file)
        partial = json.dumps(entry("m2", "m1"))[:-1]
        self.fx.append_raw(partial)
        self.assertEqual(self.idx.sync(self.fx.file), "ok")
        header, records = self.load()
        self.assertEqual(header["entryCount"], 2)  # session + m1
        self.assertLess(header["coveredEnd"], self.fx.size())
        # Completing the line lets the next sync cover it.
        self.fx.append_raw("}\n")
        self.idx.sync(self.fx.file)
        header, records = self.load()
        self.assertEqual(header["entryCount"], 3)
        self.assertEqual(header["coveredEnd"], self.fx.size())

    def test_shrunk_file_rebuilds(self):
        self.fx.write([entry("m1", None), entry("m2", "m1")])
        self.idx.sync(self.fx.file)
        self.fx.write([entry("m9", None)])
        self.assertEqual(self.idx.sync(self.fx.file), "ok")
        header, records = self.load()
        self.assertEqual(header["entryCount"], 2)
        self.assertEqual([r["id"] for r in records], ["sess-1", "m9"])

    def test_replaced_file_rebuilds(self):
        self.fx.write([entry("m1", None)])
        self.idx.sync(self.fx.file)
        other = os.path.join(self.fx.slug, "other.jsonl")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(SESSION_LINE) + "\n")
            fh.write(json.dumps(entry("m2", None)) + "\n")
        os.replace(other, self.fx.file)
        self.idx.sync(self.fx.file)
        _, records = self.load()
        self.assertEqual([r["id"] for r in records], ["sess-1", "m2"])

    def test_tampered_session_id_is_not_fresh(self):
        self.fx.write([entry("m1", None)])
        self.idx.sync(self.fx.file)
        with open(self.path, encoding="utf-8") as fh:
            parsed = json.loads(fh.readline())
        parsed["sessionId"] = "wrong"
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(parsed) + "\n")
        self.assertEqual(self.idx.sync(self.fx.file), "ok")
        header, _ = self.load()
        self.assertEqual(header["sessionId"], "sess-1")

    def test_oversized_line_stops_coverage(self):
        # A line past the cap is left uncovered (the reader falls back to
        # a full parse) rather than truncated into a bogus record.
        idx = pi_transcript.TranscriptIndex(max_line=200)
        self.fx.write([
            entry("m1", None),
            entry("m2", "m1", text="x" * 400),
            entry("m3", "m2"),
        ])
        idx.sync(self.fx.file)
        header, records = self.load()
        self.assertEqual([r["id"] for r in records], ["sess-1", "m1"])
        self.assertLess(header["coveredEnd"], self.fx.size())

    def test_foreign_and_missing_are_skipped(self):
        foreign = os.path.join(self.fx.slug, "foreign.jsonl")
        with open(foreign, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "not-a-session"}) + "\n")
        self.assertEqual(self.idx.sync(foreign), "skip")
        self.assertFalse(os.path.exists(
            pi_transcript.TranscriptIndex.index_path(foreign)))
        self.assertEqual(
            self.idx.sync(os.path.join(self.fx.slug, "gone.jsonl")),
            "missing")
        link = os.path.join(self.fx.slug, "link.jsonl")
        if hasattr(os, "symlink"):
            os.symlink(self.fx.file, link)
            self.assertEqual(self.idx.sync(link), "skip")


class IndexReaderTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(self)
        self.path = pi_transcript.TranscriptIndex.index_path(self.fx.file)

    def write_index(self, lines):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def test_corrupt_middle_line_is_none(self):
        self.write_index([
            json.dumps({"v": 1, "sessionId": "sess-1"}),
            json.dumps({"o": 0}),
            "{not json",
            json.dumps({"o": 9}),
        ])
        self.assertIsNone(
            pi_transcript.TranscriptIndexFile(self.path).load())

    def test_torn_last_line_is_dropped(self):
        self.write_index([
            json.dumps({"v": 1, "sessionId": "sess-1"}),
            json.dumps({"o": 0}),
        ])
        with open(self.path, "ab") as fh:
            fh.write(b'{"o":9,"l":1')
        header, records = pi_transcript.TranscriptIndexFile(self.path).load()
        self.assertEqual(header["sessionId"], "sess-1")
        self.assertEqual(records, [{"o": 0}])

    def test_missing_index_is_none(self):
        self.assertIsNone(
            pi_transcript.TranscriptIndexFile(self.path).load())


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(self)
        self.idx = pi_transcript.TranscriptIndex()
        self.path = pi_transcript.TranscriptIndex.index_path(self.fx.file)

    def test_forget_unlinks_sidecar(self):
        self.fx.write([entry("m1", None)])
        self.idx.sync(self.fx.file)
        self.assertTrue(os.path.exists(self.path))
        self.idx.forget(self.fx.file)
        self.assertFalse(os.path.exists(self.path))

    def test_drop_orphans_removes_stale_sidecar(self):
        self.fx.write([entry("m1", None)])
        self.idx.sync(self.fx.file)
        self.assertTrue(os.path.exists(self.path))
        os.unlink(self.fx.file)
        self.idx.drop_orphans(self.fx.root)
        self.assertFalse(os.path.exists(self.path))


class ConcurrencyTests(unittest.TestCase):
    def test_two_threads_sync_one_file_consistently(self):
        fx = Fixture(self)
        fx.write([entry("m1", None)])
        idx = pi_transcript.TranscriptIndex()
        errors = []

        def worker(count):
            try:
                for i in range(count):
                    fx.append_raw(json.dumps(
                        entry("m%d-%d" % (count, i), "m1")) + "\n")
                    idx.sync(fx.file)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in (5, 7)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        idx.sync(fx.file)
        path = pi_transcript.TranscriptIndex.index_path(fx.file)
        header, records = pi_transcript.TranscriptIndexFile(path).load()
        offset = 0
        for rec in records:
            self.assertEqual(rec["o"], offset)
            offset += rec["l"]
        self.assertEqual(offset, header["coveredEnd"])
        self.assertLessEqual(header["coveredEnd"], fx.size())


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(self)
        self.other = os.path.join(self.fx.slug, "sess-2.jsonl")
        for path, sid in ((self.fx.file, "sess-1"), (self.other, "sess-2")):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(dict(SESSION_LINE, id=sid)) + "\n")
                fh.write(json.dumps(entry("m1", None)) + "\n")

    def make_watch(self, live):
        return pi_transcript.TranscriptIndexWatch(
            pi_transcript.TranscriptIndex(), self.fx.root,
            lambda: live, threading.Event(), interval=0)

    def test_live_file_is_not_indexed(self):
        watch = self.make_watch({self.fx.file})
        watch.sweep()
        self.assertFalse(os.path.exists(
            pi_transcript.TranscriptIndex.index_path(self.fx.file)))
        self.assertTrue(os.path.exists(
            pi_transcript.TranscriptIndex.index_path(self.other)))

    def test_sweep_removes_orphan_sidecar(self):
        watch = self.make_watch(set())
        watch.sweep()
        self.assertTrue(os.path.exists(
            pi_transcript.TranscriptIndex.index_path(self.other)))
        os.unlink(self.other)
        watch.sweep()
        self.assertFalse(os.path.exists(
            pi_transcript.TranscriptIndex.index_path(self.other)))


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(self)
        self.idx = pi_transcript.TranscriptIndex(root=self.fx.root)
        self.fx.write([entry("m1", None), entry("m2", "m1"),
                       entry("m3", "m2")])
        self.reader = pi_transcript.TranscriptReader(self.idx, self.fx.file)
        self.reader.refresh()

    def test_meta_and_path(self):
        meta = self.reader.meta()
        self.assertEqual(meta["sessionId"], "sess-1")
        self.assertEqual(meta["entryCount"], 3)
        self.assertEqual(meta["coveredEnd"], self.fx.size())
        path = self.reader.path()
        self.assertEqual([e["id"] for e in path["entries"]],
                         ["m3", "m2", "m1"])
        self.assertEqual(path["leafId"], "m3")

    def test_full_fields_parse_bodies(self):
        entries = self.reader.entries(fields="full")["entries"]
        self.assertEqual(entries[0]["type"], "message")
        self.assertEqual(entries[0]["id"], "m1")

    def test_paging_and_cursor(self):
        page = self.reader.entries(limit=2)
        self.assertEqual([e["id"] for e in page["entries"]], ["m1", "m2"])
        self.assertTrue(page["hasMore"])
        self.assertEqual(page["next"], "m2")
        rest = self.reader.entries(since="m2")
        self.assertEqual([e["id"] for e in rest["entries"]], ["m3"])
        with self.assertRaises(KeyError):
            self.reader.entries(since="nope")
        by_ids = self.reader.entries(ids=["m3", "m1"])
        self.assertEqual([e["id"] for e in by_ids["entries"]],
                         ["m3", "m1"])

    def test_byte_range_entry_aligned(self):
        rng = self.reader.byte_range(0, 64, align="entry")
        body = base64.b64decode(rng["bytes"])
        self.assertTrue(body.endswith(b"\n"))
        self.assertEqual(json.loads(body)["id"], "m1")

    def test_tree_roots(self):
        tree = self.reader.tree()
        self.assertEqual([n["id"] for n in tree["nodes"]],
                         ["m1", "m2", "m3"])
        self.assertEqual(tree["roots"], ["m1"])
        self.assertEqual(tree["leafId"], "m3")

    def test_owns_gates_the_store_root(self):
        self.assertTrue(self.idx.owns(self.fx.file))
        outside = os.path.join(os.path.dirname(self.fx.root), "x.jsonl")
        self.assertFalse(self.idx.owns(outside))

    def test_owns_rejects_a_symlinked_ancestor(self):
        outside = tempfile.mkdtemp(dir=os.path.expanduser("~/tmp"))
        self.addCleanup(shutil.rmtree, outside, True)
        open(os.path.join(outside, "secret.jsonl"), "w").close()
        link = os.path.join(self.fx.root, "linkdir")
        os.symlink(outside, link)
        self.assertFalse(self.idx.owns(os.path.join(link, "secret.jsonl")))

    def test_body_read_after_replacement_is_refused(self):
        other = os.path.join(self.fx.slug, "other.jsonl")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(SESSION_LINE) + "\n")
            fh.write(json.dumps(entry("m9", None)) + "\n")
        os.replace(other, self.fx.file)
        with self.assertRaises(ValueError):
            self.reader.entries(fields="full")


class OversizeTests(unittest.TestCase):
    def test_oversized_index_is_not_claimed_ok(self):
        fx = Fixture(self)
        idx = pi_transcript.TranscriptIndex(max_bytes=200)
        fx.write([entry("m%d" % i, None) for i in range(30)])
        self.assertEqual(idx.sync(fx.file), "oversized")
        self.assertFalse(os.path.exists(
            pi_transcript.TranscriptIndex.index_path(fx.file)))
        reader = pi_transcript.TranscriptReader(idx, fx.file)
        self.assertEqual(reader.refresh()["state"], "oversized")
        self.assertIsNone(reader.header)
        self.assertEqual(reader.meta()["entryCount"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
