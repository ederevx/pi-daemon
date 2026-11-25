"""The conversation catalog: what /resume lists, and what one is doing.

It owns no state and runs no sweep: a listing is a walk over the session
store plus the sidecar headers the index already keeps, and an activity page
is the indexed entries after a cursor. A conversation the index has not
reached yet is still listable; only its name is missing until it is.
"""

import hashlib
import json
import os

import pi_transcript

from .error import CatalogError

HEADER_BYTES = 1 << 16
# What an activity event keeps of a long text or argument list.
TEXT_FIELDS = ("text",)
SKIPPED_KINDS = frozenset({"system", "thinking"})


class ConversationCatalog:
    """It answers which conversations exist and what one did since a cursor.

    `holders` names the live run of a conversation and `revivable` the
    stopped name it can be hosted under; both are the sessions domain's
    answers, asked here rather than guessed.
    """

    def __init__(self, index, root, holders, revivable, page=50):
        self._index = index
        self._root = root
        self._holders = holders
        self._revivable = revivable
        self._page = int(page)

    # -- listing -----------------------------------------------------------

    def conversations(self, dir_=None, limit=None, after=None):
        """Newest first; `dir_` narrows to one working directory."""
        size = max(1, min(int(limit or self._page), self._page))
        cursor = _cursor(after)
        items = []
        for file in self._files():
            entry = self.entry(file)
            if entry is None:
                continue
            if dir_ is not None and entry["cwd"] != dir_:
                continue
            items.append((entry["mtimeMs"], _digest(file), entry))
        items.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if cursor is not None:
            items = [item for item in items
                     if (item[0], item[1]) < cursor]
        page = items[:size]
        more = len(items) > size
        return {"conversations": [entry for _mtime, _digest_, entry in page],
                "next": _position(page[-1]) if more else None}

    def resolve(self, session_id):
        """The file one session id names, by full id or unique prefix."""
        wanted = (session_id or "").strip()
        if not wanted:
            raise CatalogError("bad-transcript")
        found = []
        for file in self._files():
            entry = self.entry(file)
            if entry is not None and entry["sessionId"] and \
                    entry["sessionId"].startswith(wanted):
                found.append(entry)
        if not found:
            raise CatalogError("no-file")
        if len(found) > 1 and all(entry["sessionId"] != wanted
                                  for entry in found):
            raise CatalogError("ambiguous-session")
        return next((entry for entry in found
                     if entry["sessionId"] == wanted), found[0])

    # -- activity ----------------------------------------------------------

    def head(self, file):
        """The newest entry's id: where a reader that wants only news starts."""
        reader = pi_transcript.TranscriptReader(self._index, file)
        reader.refresh()
        entries = reader.conversation()
        return entries[-1]["id"] if entries else None

    def activity(self, file, since=None, chars=2000, limit=50):
        """The events after `since`, one page, in append order."""
        reader = pi_transcript.TranscriptReader(self._index, file)
        reader.refresh()
        try:
            page = reader.entries(since=since or None, limit=limit,
                                  fields="full")
        except KeyError:
            raise CatalogError("bad-cursor") from None
        events = []
        for entry in page["entries"]:
            events.extend(_events(entry, chars))
        return {"events": events,
                "next": page["entries"][-1]["id"] if page["entries"] else None,
                "hasMore": page["hasMore"]}

    # -- internals ---------------------------------------------------------

    def _files(self):
        try:
            names = os.listdir(self._root) if self._root else []
        except OSError:
            return []
        files = []
        for name in names:
            directory = os.path.join(self._root, name)
            if os.path.islink(directory) or not os.path.isdir(directory):
                continue
            try:
                entries = os.listdir(directory)
            except OSError:
                continue
            files.extend(os.path.join(directory, entry)
                         for entry in entries
                         if entry.endswith(".jsonl")
                         and not os.path.islink(
                             os.path.join(directory, entry)))
        return files

    def entry(self, file):
        """One conversation's listing record, or None when it is gone."""
        try:
            stamp = os.stat(file)
        except OSError:
            return None
        header = _header(file)
        loaded = pi_transcript.TranscriptIndexFile(
            self._index.index_path(file), self._index.max_bytes).load()
        indexed = loaded[0] if loaded else {}
        records = loaded[1] if loaded else []
        name = None
        for record in records:
            if record.get("name"):
                name = record["name"]
        holders = list(self._holders(file) or ())
        return {"file": file,
                "sessionId": indexed.get("sessionId") or header.get("id"),
                "cwd": header.get("cwd"),
                "name": name,
                "mtimeMs": int(stamp.st_mtime * 1000),
                "size": stamp.st_size,
                "entryCount": len([record for record in records
                                   if record.get("t") != "session"]),
                "holder": ({"name": holders[0][0], "state": holders[0][1]}
                           if holders else None),
                "revivable": self._revivable(file)}


def _header(file):
    try:
        with open(file, "rb") as handle:
            raw = handle.readline(HEADER_BYTES)
        loaded = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _digest(file):
    return hashlib.sha256(file.encode("utf-8")).hexdigest()[:16]


def _cursor(after):
    if not isinstance(after, str) or ":" not in after:
        return None
    stamp, _, digest = after.partition(":")
    try:
        return (int(stamp), digest)
    except ValueError:
        raise CatalogError("bad-cursor") from None


def _position(item):
    mtime, digest, _entry = item
    return "%d:%s" % (mtime, digest)


def _events(entry, chars):
    """One entry as the events a reader sees, none for what it cannot use."""
    kind = entry.get("type")
    entry_id = entry.get("id")
    if kind == "message":
        return _message_events(entry_id, entry.get("message") or {}, chars)
    if kind == "compaction":
        return [{"entryId": entry_id, "kind": "compaction"}]
    if kind == "session_info":
        return [{"entryId": entry_id, "kind": "renamed",
                 "text": _clip(entry.get("name") or "", chars)}]
    return []


def _message_events(entry_id, message, chars):
    role = message.get("role")
    if role in SKIPPED_KINDS:
        return []
    if role == "user":
        return [{"entryId": entry_id, "kind": "user",
                 "text": _text(message.get("content"), chars)}]
    if role == "toolResult":
        return [{"entryId": entry_id, "kind": "tool_result",
                 "tool": message.get("toolName"),
                 "isError": bool(message.get("isError")),
                 "text": _text(message.get("content"), chars)}]
    if role != "assistant":
        return []
    events = [{"entryId": entry_id, "kind": "assistant",
               "text": _text(message.get("content"), chars)}]
    for part in _parts(message.get("content")):
        if part.get("type") != "toolCall":
            continue
        events.append({"entryId": entry_id, "kind": "tool_call",
                       "tool": part.get("name"),
                       "args": _clip(json.dumps(part.get("arguments") or {},
                                                separators=(",", ":")),
                                     chars)})
    return [event for event in events
            if event["kind"] != "assistant" or event["text"]]


def _parts(content):
    return [part for part in content if isinstance(part, dict)] \
        if isinstance(content, list) else []


def _text(content, chars):
    """The readable text of a message's content, clipped for a page."""
    if isinstance(content, str):
        return _clip(content, chars)
    return _clip("\n".join(part.get("text") or ""
                           for part in _parts(content)
                           if part.get("type") in TEXT_FIELDS), chars)


def _clip(text, chars):
    text = (text or "").strip()
    limit = int(chars)
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…"
    return text
