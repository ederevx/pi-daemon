#!/usr/bin/env python3
"""Independent wire-level test of the M2 transcript commands.

Runs the REAL pi-daemon on isolated XDG dirs and drives it over the
actual loopback control socket with the mandatory hello+token JSONL
handshake - never by calling Control methods directly. Covers the hello
ack capabilities, the legacy hello, open->path, entries paging and the
`since` cursor, an entry-aligned byte range, the tree, stat, rebuild,
and the two rejection paths (unknown handle, path outside the store).

Scratch and the session store live under ~/tmp; stdlib only.
"""

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMON_PATH = os.path.join(REPO, "pi", "daemon", "pi-daemon")

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


class WireDaemon:
    """Owns one isolated daemon process and the raw JSONL client."""

    def __init__(self):
        self.scratch = tempfile.mkdtemp(
            prefix="pi-daemon-twire-", dir=os.path.expanduser("~/tmp"))
        self.runtime = os.path.join(self.scratch, "runtime")
        self.state = os.path.join(self.scratch, "state")
        self.agent = os.path.join(self.scratch, "agent")
        for path in (self.runtime, self.state, self.agent):
            os.makedirs(path, exist_ok=True)
        self.sock_path = os.path.join(self.runtime, "pi-pty-host.sock")
        self.store = os.path.join(self.agent, "sessions")
        self.env = dict(os.environ)
        self.env.update({
            "XDG_RUNTIME_DIR": self.runtime,
            "XDG_STATE_HOME": self.state,
            "PI_CODING_AGENT_DIR": self.agent,
            "PI_PTYD_TRANSCRIPT_INDEX_INTERVAL": "0",
        })
        self.proc = None
        self.file = None

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, DAEMON_PATH], env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                sock, _ = self._connect()
                sock.close()
                return
            except (OSError, ValueError):
                time.sleep(0.1)
        raise AssertionError("daemon control socket never came up")

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        shutil.rmtree(self.scratch, ignore_errors=True)

    def conversation(self):
        """Create the scratch conversation inside the store and return
        its path. A fresh slug directory each call keeps one test's file
        from racing another's index."""
        self.file = os.path.join(self.store, "--home-wire--", "wire.jsonl")
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        with open(self.file, "w", encoding="utf-8") as fh:
            fh.write(HEADER)
            fh.writelines(ENTRIES)
        return self.file

    # -- control transport --------------------------------------------------

    def endpoint(self):
        with open(self.sock_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _read_line(self, sock):
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        if not buf:
            raise OSError("control connection closed")
        return json.loads(buf.split(b"\n", 1)[0])

    def _connect(self, hello=None):
        ep = self.endpoint()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((ep["host"], ep["port"]))
        msg = ({"cmd": "hello", "token": ep["token"]}
               if hello is None else hello)
        sock.sendall(json.dumps(msg).encode() + b"\n")
        return sock, self._read_line(sock)

    def wire(self, req):
        """One request on its own authenticated connection."""
        sock, reply = self._connect()
        if not reply.get("ok"):
            sock.close()
            raise AssertionError("handshake rejected: %r" % (reply,))
        try:
            sock.sendall(json.dumps(req).encode() + b"\n")
            return self._read_line(sock)
        finally:
            sock.close()


class TranscriptWireTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.daemon = WireDaemon()
        cls.daemon.start()
        cls.daemon.conversation()

    @classmethod
    def tearDownClass(cls):
        cls.daemon.stop()

    # -- helpers ------------------------------------------------------------

    def open_handle(self):
        resp = self.daemon.wire({"id": "open", "cmd": "transcript-open",
                                 "file": self.daemon.file})
        self.assertTrue(resp.get("ok"), resp)
        return resp["transcriptId"]

    # -- handshake ----------------------------------------------------------

    def test_ack_advertises_proto_and_caps(self):
        sock, reply = self.daemon._connect()
        sock.close()
        self.assertTrue(reply.get("ok"), reply)
        self.assertEqual(reply.get("proto"), 1)
        self.assertIn("transcript.v1", reply.get("caps", []))
        self.assertEqual(reply.get("index"), 1)

    def test_legacy_hello_with_only_token_succeeds(self):
        ep = self.daemon.endpoint()
        sock, reply = self.daemon._connect(
            hello={"cmd": "hello", "token": ep["token"]})
        sock.close()
        self.assertTrue(reply.get("ok"), reply)

    # -- transcript commands -------------------------------------------------

    def test_open_then_path_leaf_to_root(self):
        tid = self.open_handle()
        resp = self.daemon.wire({"id": "p1", "cmd": "transcript-path",
                                 "transcriptId": tid})
        self.assertEqual(resp["id"], "p1")
        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual(resp["leafId"], "m3")
        self.assertEqual([e["id"] for e in resp["entries"]],
                         ["m3", "m2", "m1"])

    def test_entries_paging_cursor_and_bad_cursor(self):
        tid = self.open_handle()
        page = self.daemon.wire({"id": "e1", "cmd": "transcript-entries",
                                 "transcriptId": tid, "limit": 2})
        self.assertEqual([e["id"] for e in page["entries"]], ["m1", "m2"])
        self.assertTrue(page["hasMore"])
        self.assertEqual(page["next"], "m2")
        rest = self.daemon.wire({"id": "e2", "cmd": "transcript-entries",
                                 "transcriptId": tid, "since": "m2"})
        self.assertEqual([e["id"] for e in rest["entries"]], ["m3"])
        self.assertFalse(rest["hasMore"])
        bad = self.daemon.wire({"id": "e3", "cmd": "transcript-entries",
                                "transcriptId": tid, "since": "nope"})
        self.assertEqual(bad["id"], "e3")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "bad-cursor")

    def test_entry_aligned_range_round_trips_a_real_line(self):
        tid = self.open_handle()
        resp = self.daemon.wire({"id": "r1", "cmd": "transcript-range",
                                 "transcriptId": tid, "offset": 0,
                                 "length": 64, "align": "entry"})
        self.assertTrue(resp.get("ok"), resp)
        body = base64.b64decode(resp["bytes"])
        self.assertTrue(body.endswith(b"\n"), body)
        self.assertEqual(json.loads(body.decode("utf-8"))["id"], "m1")
        with open(self.daemon.file, "rb") as fh:
            fh.seek(resp["offset"])
            self.assertEqual(fh.read(resp["length"]), body)

    def test_tree_nodes_and_leaf(self):
        tid = self.open_handle()
        resp = self.daemon.wire({"id": "t1", "cmd": "transcript-tree",
                                 "transcriptId": tid})
        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual([n["id"] for n in resp["nodes"]],
                         ["m1", "m2", "m3"])
        self.assertEqual(resp["roots"], ["m1"])
        self.assertEqual(resp["leafId"], "m3")

    def test_stat(self):
        tid = self.open_handle()
        resp = self.daemon.wire({"id": "s1", "cmd": "transcript-stat",
                                 "transcriptId": tid})
        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual(resp["sessionId"], "wire-sess")
        self.assertEqual(resp["entryCount"], 3)
        self.assertNotIn("file", resp)

    def test_rebuild(self):
        tid = self.open_handle()
        resp = self.daemon.wire({"id": "b1", "cmd": "transcript-rebuild",
                                 "transcriptId": tid})
        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual(resp["index"], {"state": "ok"})

    # -- rejection paths -----------------------------------------------------

    def test_unknown_transcript_id_is_no_such_transcript(self):
        resp = self.daemon.wire({"id": "u1", "cmd": "transcript-stat",
                                 "transcriptId": "t-nope"})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp["error"], "no-such-transcript")
        self.assertEqual(resp["id"], "u1")

    def test_file_outside_store_root_is_bad_transcript(self):
        outside = os.path.join(self.daemon.scratch, "outside.jsonl")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write(HEADER)
            fh.writelines(ENTRIES)
        resp = self.daemon.wire({"id": "o1", "cmd": "transcript-open",
                                 "file": outside})
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp["error"], "bad-transcript")


if __name__ == "__main__":
    unittest.main(verbosity=2)
