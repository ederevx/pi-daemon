"""Conversation index for pi's session store: one owner for the on-disk
`.jsonl.idx` sidecar and the daemon's tail-and-rebuild pass over it.

pi resumes a conversation by parsing the whole `.jsonl` eagerly. This
module builds a tiny positional index beside each conversation so a
reader can resolve the entry metadata (id, parent, type, kind-specific
fields) and then read only the bodies it needs, without a full parse.

The daemon is the sole writer of an index; pi only reads it. An index is
never authoritative for correctness: a malformed, stale or absent index
is ignored by the reader, which falls back to the full file. Every write
is an atomic replace of the whole (bounded) index, so a reader sees a
complete file or the previous one; the expensive part - reading the
conversation - is incremental, resuming from the last covered byte.

The `.idx` suffix keeps the sidecar out of pi's `/resume` picker, the
session-store GC and every other `.jsonl` walk.
"""

import base64
import collections
import hashlib
import json
import os
import threading
import time

INDEX_VERSION = 1
INDEX_SUFFIX = ".idx"
DEFAULT_MAX_BYTES = 4 << 20
DEFAULT_MAX_LINE = 16 << 20

# Kind-specific fields an index record must carry so a reader can
# resolve the model-visible context without fetching bodies: the
# compaction's retained-entry marker, a context edit's target and a
# branch summary's source. Each is (entry type, JSON field, record
# field); a field is copied only when present.
CONTROL_FIELDS = (
    ("compaction", "firstKeptEntryId", "firstKept"),
    ("context_edit", "targetId", "target"),
    ("branch_summary", "fromId", "from"),
    ("session_info", "name", "name"),
    ("custom", "customType", "customType"),
    ("custom_message", "customType", "customType"),
)


class TranscriptIndexFile:
    """The on-disk JSONL index: the only code that opens a `.idx`.

    Line 0 is the header object; every later line is one entry record in
    append order. `load` tolerates a torn trailing line (a crash between
    a full rebuild's temp-write and its replace cannot produce one, but a
    truncated copy can); a torn line in the middle is corruption and
    returns None so the caller rebuilds.
    """

    def __init__(self, path, max_bytes=DEFAULT_MAX_BYTES):
        self.path = path
        self.max_bytes = int(max_bytes)

    def load_header(self):
        """The parsed header line, or None when the file is missing,
        empty, truncated or not an object. Cheap: reads one line."""
        try:
            with open(self.path, "rb") as fh:
                raw = fh.readline(DEFAULT_MAX_LINE + 1)
        except OSError:
            return None
        if not raw.endswith(b"\n"):
            return None
        try:
            header = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return header if isinstance(header, dict) else None

    def load(self):
        """(header, records) or None. Records whose lines parse and end
        in a newline; a torn final line is dropped, any other bad line is
        treated as corruption."""
        try:
            if os.path.getsize(self.path) > self.max_bytes:
                return None
            with open(self.path, "rb") as fh:
                raw_lines = fh.readlines()
        except OSError:
            return None
        header = None
        records = []
        last = len(raw_lines) - 1
        for i, raw in enumerate(raw_lines):
            if not raw.endswith(b"\n"):
                break  # a torn trailing line is dropped
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                if i == last:
                    break
                return None
            if i == 0:
                if not isinstance(obj, dict):
                    return None
                header = obj
            elif isinstance(obj, dict):
                records.append(obj)
            else:
                return None
        if header is None:
            return None
        return header, records

    def publish(self, header, records):
        """Atomically replace the whole index: write a pid+thread-unique
        temp in the same directory, fsync, os.replace. Returns False
        (and leaves no index) when the payload would exceed the byte cap,
        so a reader never silently sees an empty transcript from a
        too-large sidecar. A crash leaves the old index intact."""
        lines = [json.dumps(header, separators=(",", ":"))]
        for rec in records:
            lines.append(json.dumps(rec, separators=(",", ":")))
        payload = "\n".join(lines) + "\n"
        if len(payload.encode("utf-8")) > self.max_bytes:
            self.unlink()
            return False
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = "%s.tmp.%d.%d" % (self.path, os.getpid(),
                                threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.replace(tmp, self.path)
        except OSError:
            # Windows cannot rename over a file a reader holds open;
            # drop it and retry so the index keeps refreshing.
            try:
                os.unlink(self.path)
            except OSError:
                pass
            os.replace(tmp, self.path)
        return True

    def unlink(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass


class TranscriptIndex:
    """Build, validate and refresh `.idx` sidecars.

    `sync(file)` is the one entry point: it skips a foreign/held file,
    accepts an up-to-date index, tails a grown conversation from its
    covered end, or rebuilds from zero. One lock per conversation path
    keeps two callers from publishing concurrently; the lock map is
    instance-owned.
    """

    def __init__(self, max_bytes=DEFAULT_MAX_BYTES,
                 max_line=DEFAULT_MAX_LINE, now=None, root=None):
        self.max_bytes = int(max_bytes)
        self.max_line = int(max_line)
        self.now = now if now is not None else time.time
        self.root = os.path.abspath(root) if root else None
        self._now = self.now
        self._locks = {}
        self._guard = threading.Lock()

    def owns(self, file):
        """The daemon may serve this file: an absolute, non-symlink
        regular file inside the session store (when a root was given).
        The daemon never reads an arbitrary client-supplied path."""
        if not (isinstance(file, str) and file and os.path.isabs(file)):
            return False
        if self.root is None:
            return os.path.isfile(file) and not os.path.islink(file)
        # realpath both sides: a symlinked slug directory must not let a
        # request escape the store, and the real root may itself be
        # reached through a symlinked ancestor.
        try:
            real = os.path.realpath(file)
            root = os.path.realpath(self.root)
            common = os.path.commonpath([real, root])
        except (OSError, ValueError):
            return False
        return common == root and os.path.isfile(real)

    @staticmethod
    def index_path(file):
        return file + INDEX_SUFFIX

    def sync(self, file):
        """Bring the sidecar for `file` up to date. Returns a state word
        ("ok", "skip", "missing", "rebuild"); never raises for a bad
        file, so one conversation can never stop a sweep."""
        if not isinstance(file, str) or not file.endswith(".jsonl"):
            return "skip"
        if not os.path.isabs(file) or os.path.islink(file):
            return "skip"
        # Key the lock on the resolved path so two spellings of one file
        # cannot scan and publish concurrently.
        with self._lock_for(os.path.realpath(file)):
            try:
                return self._sync_locked(file)
            except OSError:
                return "skip"

    def forget(self, file):
        """Drop a conversation's sidecar (it is being discarded or gone)."""
        if isinstance(file, str) and file:
            TranscriptIndexFile(self.index_path(file)).unlink()

    def drop_orphans(self, root):
        """Unlink sidecars whose conversation no longer exists: a file
        deleted out of band, or a leftover from a crash."""
        try:
            sluds = os.listdir(root)
        except OSError:
            return
        for slug in sluds:
            directory = os.path.join(root, slug)
            if os.path.islink(directory) or not os.path.isdir(directory):
                continue
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".jsonl" + INDEX_SUFFIX):
                    continue
                base = os.path.join(directory, name[:-len(INDEX_SUFFIX)])
                if not os.path.isfile(base):
                    TranscriptIndexFile(
                        os.path.join(directory, name)).unlink()

    # -- internals -----------------------------------------------------------

    def _lock_for(self, file):
        with self._guard:
            lock = self._locks.get(file)
            if lock is None:
                lock = threading.Lock()
                self._locks[file] = lock
            return lock

    def _sync_locked(self, file):
        if os.path.islink(file) or not os.path.isfile(file):
            return "missing"
        st = os.stat(file)
        session_id, header_v = self._header_identity(file)
        if session_id is None:
            return "skip"  # not a pi conversation
        idx = TranscriptIndexFile(self.index_path(file), self.max_bytes)
        loaded = idx.load()
        if loaded is not None:
            header, records = loaded
            if self._fresh(header, st, session_id):
                covered = header.get("coveredEnd")
                if covered == st.st_size:
                    return "ok"
                if isinstance(covered, int) and 0 <= covered <= st.st_size:
                    tail = self._scan(file, covered)
                    if tail is not None:
                        return self._publish(
                            idx, st, session_id, header_v,
                            records + tail, header.get("created"))
        records = self._scan(file, 0)
        if records is None:
            return "skip"
        return self._publish(idx, st, session_id, header_v, records, None)

    def _fresh(self, header, st, session_id):
        """The index provably belongs to this exact file state: same
        version, same conversation, same inode and a covered end within
        the current size."""
        if header.get("v") != INDEX_VERSION:
            return False
        if header.get("sessionId") != session_id:
            return False
        if header.get("ino") != st.st_ino or header.get("dev") != st.st_dev:
            return False
        covered = header.get("coveredEnd")
        return isinstance(covered, int) and 0 <= covered <= st.st_size

    def _header_identity(self, file):
        """(session id, header version) of a real pi conversation, or
        (None, None) for anything else. Reads only the first line."""
        try:
            with open(file, "rb") as fh:
                raw = fh.readline(self.max_line + 1)
        except OSError:
            return None, None
        if not raw.endswith(b"\n") or len(raw) > self.max_line:
            return None, None
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, None
        if not isinstance(obj, dict) or obj.get("type") != "session":
            return None, None
        session_id = obj.get("id")
        if not isinstance(session_id, str) or not session_id:
            return None, None
        version = obj.get("version")
        return session_id, version if isinstance(version, int) else None

    def _scan(self, file, start):
        """Entry records for every complete line from byte `start` to
        EOF. An unterminated or oversized final line, a JSON error or a
        missing id stops the scan there and leaves the rest uncovered, so
        the next sync retries; the caller never indexes a partial line.
        Returns None when the file cannot be read at all."""
        records = []
        offset = start
        try:
            with open(file, "rb") as fh:
                fh.seek(start)
                while True:
                    raw = fh.readline(self.max_line + 1)
                    if not raw or not raw.endswith(b"\n"):
                        break
                    if len(raw) > self.max_line:
                        break
                    try:
                        obj = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        break
                    record = self._record(obj, offset, len(raw))
                    if record is None:
                        break
                    records.append(record)
                    offset += len(raw)
        except OSError:
            return None
        return records

    @staticmethod
    def _record(obj, offset, length):
        if not isinstance(obj, dict):
            return None
        entry_id = obj.get("id")
        entry_type = obj.get("type")
        if not isinstance(entry_id, str) or not isinstance(entry_type, str):
            return None
        record = {
            "o": offset,
            "l": length,
            "id": entry_id,
            "p": obj.get("parentId"),
            "t": entry_type,
            "ts": obj.get("timestamp"),
        }
        for type_name, source_key, record_key in CONTROL_FIELDS:
            if entry_type == type_name:
                value = obj.get(source_key)
                if value is not None:
                    record[record_key] = value
        return record

    def _publish(self, idx, st, session_id, header_v, records, created):
        covered = records[-1]["o"] + records[-1]["l"] if records else 0
        header = {
            "v": INDEX_VERSION,
            "sessionId": session_id,
            "headerV": header_v,
            "fileSize": st.st_size,
            "mtimeMs": int(st.st_mtime * 1000),
            "ino": st.st_ino,
            "dev": st.st_dev,
            "coveredEnd": covered,
            "entryCount": len(records),
            "created": created or time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._now())),
        }
        if not idx.publish(header, records):
            return "oversized"
        return "ok"


class TranscriptIndexWatch:
    """One poll thread that keeps the store's sidecars fresh.

    It walks the session store's slug directories and syncs every
    conversation no live session backs: the daemon is the index's only
    writer, and a live pi owns its own file, so the index pass leaves it
    alone until it goes cold. The live set comes from a caller-supplied
    snapshot so the sweep never holds the session table lock while it
    reads a file.
    """

    def __init__(self, index, root, live_files, shutdown_event,
                 interval=3.0):
        self.index = index
        self.root = root
        self.live_files = live_files
        self.shutdown_event = shutdown_event
        self.interval = float(interval)

    def start(self):
        if self.interval <= 0:
            return
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        # Index once immediately, in this thread, so booting the daemon
        # never blocks on a large store; then poll.
        self.sweep()
        while not self.shutdown_event.wait(self.interval):
            self.sweep()

    def sweep(self):
        try:
            live = set(self.live_files() or ())
        except Exception:
            live = set()
        for slug in self._slug_dirs():
            self._sweep_dir(slug, live)
        self.index.drop_orphans(self.root)

    def _slug_dirs(self):
        try:
            names = os.listdir(self.root)
        except OSError:
            return []
        dirs = []
        for name in names:
            path = os.path.join(self.root, name)
            if os.path.islink(path) or not os.path.isdir(path):
                continue
            dirs.append(path)
        return dirs

    def _sweep_dir(self, directory, live):
        try:
            names = os.listdir(directory)
        except OSError:
            return
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            full = os.path.join(directory, name)
            if full in live:
                continue
            self.index.sync(full)


class TranscriptHandles:
    """A bounded registry of open transcripts: transcriptId -> file.

    The daemon is stateless per request, so a client may always pass the
    conversation `file`/`session` directly; the handle is a convenience
    for a client that opens once and reuses. The oldest handle is evicted
    past the cap, which the client sees as a stale handle.
    """

    def __init__(self, cap=256):
        self.cap = int(cap)
        self._handles = collections.OrderedDict()
        self._lock = threading.Lock()

    def issue(self, file):
        tid = "t-" + hashlib.sha256(file.encode("utf-8")).hexdigest()[:16]
        with self._lock:
            self._handles[tid] = file
            self._handles.move_to_end(tid)
            while len(self._handles) > self.cap:
                self._handles.popitem(last=False)
        return tid

    def resolve(self, transcript_id):
        if not isinstance(transcript_id, str) or not transcript_id:
            return None
        with self._lock:
            return self._handles.get(transcript_id)

    def forget(self, file):
        with self._lock:
            stale = [tid for tid, path in self._handles.items()
                     if path == file]
            for tid in stale:
                del self._handles[tid]


class TranscriptReader:
    """Read-only serving of one conversation from its index.

    Metadata comes from the sidecar; every body is read by
    (offset,length) from the committed file, so a concurrent appender
    never yields a torn entry. `refresh()` builds or tails the index
    through the shared owner, so a live conversation's complete lines
    become readable without a full parse.
    """

    def __init__(self, index, file, max_page=500, max_range=1 << 20):
        self.index = index
        self.file = file
        self.max_page = int(max_page)
        self.max_range = int(max_range)
        self.header = None
        self.records = []
        self._by_id = {}
        self._identity = None

    def refresh(self):
        state = self.index.sync(self.file)
        loaded = TranscriptIndexFile(
            self.index.index_path(self.file), self.index.max_bytes).load()
        if loaded is None:
            self.header, self.records, self._by_id = None, [], {}
        else:
            self.header, self.records = loaded
            self._by_id = {r["id"]: r for r in self.records
                           if isinstance(r.get("id"), str)}
        try:
            st = os.stat(self.file)
            self._identity = (st.st_ino, st.st_dev)
        except OSError:
            self._identity = None
        return {"state": state}

    def conversation(self):
        """Index records that are real entries, in append order (the
        session header is not part of the tree)."""
        return [r for r in self.records if r.get("t") != "session"]

    def meta(self):
        st = os.stat(self.file)
        header = self.header or {}
        return {
            "sessionId": header.get("sessionId"),
            "headerV": header.get("headerV"),
            "file": self.file,
            "size": st.st_size,
            "mtimeMs": int(st.st_mtime * 1000),
            "ino": st.st_ino,
            "dev": st.st_dev,
            "coveredEnd": header.get("coveredEnd", 0),
            "entryCount": len(self.conversation()),
            "recordCount": len(self.records),
        }

    def read_body(self, offset, length):
        # Bound by the size and identity captured at refresh: a shrunk,
        # rotated or replaced file never yields another file's bytes at
        # stale offsets.
        try:
            st = os.stat(self.file)
            if self._identity is not None and \
                    (st.st_ino, st.st_dev) != self._identity:
                return None
            if offset < 0 or length <= 0 or offset + length > st.st_size:
                return None
            with open(self.file, "rb") as fh:
                fh.seek(offset)
                return fh.read(length)
        except OSError:
            return None

    def _view(self, record, fields):
        if fields != "full":
            return dict(record)
        body = self.read_body(record["o"], record["l"])
        if body is None:
            raise ValueError("body")
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("body")

    def path(self, leaf_id=None, fields="ref"):
        leaf = leaf_id if leaf_id in self._by_id else None
        if leaf is None:
            entries = self.conversation()
            leaf = entries[-1]["id"] if entries else None
        chain = []
        seen = set()
        while leaf is not None and leaf in self._by_id and leaf not in seen:
            seen.add(leaf)
            record = self._by_id[leaf]
            chain.append(self._view(record, fields))
            leaf = record.get("p")
        return {"leafId": chain[0]["id"] if chain else None,
                "entries": chain}

    def entries(self, since=None, ids=None, offset=None, limit=200,
                fields="ref"):
        if isinstance(ids, list) and ids:
            out = [self._view(self._by_id[i], fields)
                   for i in ids if i in self._by_id]
            return {"entries": out, "next": None, "hasMore": False}
        entries = self.conversation()
        start = 0
        if isinstance(since, str) and since:
            found = next((k for k, r in enumerate(entries)
                          if r["id"] == since), None)
            if found is None:
                raise KeyError("since")
            start = found + 1
        elif isinstance(offset, int) and offset > 0:
            start = next((k for k, r in enumerate(entries)
                          if r["o"] >= offset), len(entries))
        page_size = max(1, min(int(limit or 200), self.max_page))
        page = entries[start:start + page_size]
        has_more = start + page_size < len(entries)
        return {
            "entries": [self._view(r, fields) for r in page],
            "next": page[-1]["id"] if page and has_more else None,
            "hasMore": has_more,
        }

    def byte_range(self, offset, length, align="none"):
        try:
            offset = int(offset)
            length = int(length)
        except (TypeError, ValueError):
            raise ValueError("range")
        if offset < 0 or length <= 0:
            raise ValueError("range")
        length = min(length, self.max_range)
        if align == "entry":
            region = self._entry_range(offset, offset + length)
            if region is not None:
                offset, length = region
        size = os.path.getsize(self.file)
        end = min(offset + length, size)
        body = self.read_body(offset, end - offset)
        if body is None:
            raise ValueError("range")
        return {"offset": offset, "length": len(body), "eof": end >= size,
                "bytes": base64.b64encode(body).decode("ascii")}

    def _entry_range(self, start, end):
        records = [r for r in self.conversation() if r["o"] >= start]
        if not records:
            return None
        first = records[0]
        last = first
        for record in records:
            if record["o"] + record["l"] <= end:
                last = record
            else:
                break
        return first["o"], last["o"] + last["l"] - first["o"]

    def tree(self):
        entries = self.conversation()
        known = {r["id"] for r in entries}
        nodes = [{"id": r["id"], "parentId": r.get("p"), "t": r.get("t"),
                  "ts": r.get("ts")} for r in entries]
        roots = [r["id"] for r in entries if r.get("p") not in known]
        return {"nodes": nodes, "roots": roots,
                "leafId": entries[-1]["id"] if entries else None}
