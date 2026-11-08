"""The transcripts domain's control operations and error projection."""

from ..wire import (BAD_CURSOR, BAD_REQUEST, BAD_TRANSCRIPT, NO_INDEX,
                    NO_TRANSCRIPT, error_reply)

FIELDS = ("ref", "full")


class TranscriptOperations:
    """It validates the wire and shapes the reply; the service owns state."""

    def __init__(self, service):
        self._service = service

    def handlers(self):
        return {
            "transcript-open": self.open,
            "transcript-path": self.path,
            "transcript-entries": self.entries,
            "transcript-range": self.range,
            "transcript-tree": self.tree,
            "transcript-stat": self.stat,
            "transcript-rebuild": self.rebuild,
        }

    def open(self, request):
        """A handle plus the index state, so a reader can name the file
        it means without re-announcing a session."""
        reader, failure = self._reader(request)
        if failure is not None:
            return _tag(request, failure)
        state = reader.refresh()
        if reader.header is None:
            return _tag(request, error_reply(NO_INDEX))
        try:
            meta = reader.meta()
        except OSError:
            return _tag(request, error_reply(BAD_TRANSCRIPT))
        meta["ok"] = True
        meta["transcriptId"] = self._service.issue(reader.file)
        meta["index"] = state
        return _tag(request, meta)

    def path(self, request):
        return self._view(request, lambda reader: reader.path(
            leaf_id=request.get("leafId"), fields=_fields(request)))

    def entries(self, request):
        return self._view(request, lambda reader: reader.entries(
            since=request.get("since"), ids=request.get("ids"),
            offset=request.get("offset"), limit=request.get("limit", 200),
            fields=_fields(request)))

    def range(self, request):
        return self._view(request, lambda reader: reader.byte_range(
            request.get("offset", 0), request.get("length", 65536),
            align=request.get("align", "none")))

    def tree(self, request):
        return self._view(request, lambda reader: reader.tree())

    def stat(self, request):
        """Every metadata field but the path itself: a client that asks
        what a conversation is need not learn where it lives."""
        def run(reader):
            meta = reader.meta()
            meta.pop("file", None)
            return meta
        return self._view(request, run)

    def rebuild(self, request):
        """Drop a sidecar and build it again, so a corrupt index is
        repaired through the same writer that made it."""
        file = self._target(request)
        if file is None:
            return _tag(request, error_reply(NO_TRANSCRIPT))
        if not self._service.owns(file):
            return _tag(request, error_reply(BAD_TRANSCRIPT))
        self._service.forget(file)
        reader = self._service.reader(file)
        state = reader.refresh()
        if reader.header is None:
            return _tag(request, error_reply(NO_INDEX))
        return _tag(request, {"ok": True, "index": state})

    # -- the one read path -------------------------------------------------

    def _view(self, request, action):
        reader, failure = self._reader(request)
        if failure is not None:
            return _tag(request, failure)
        reader.refresh()
        if reader.header is None:
            return _tag(request, error_reply(NO_INDEX))
        try:
            payload = action(reader)
        except KeyError:
            return _tag(request, error_reply(BAD_CURSOR))
        except (TypeError, ValueError, OSError):
            return _tag(request, error_reply(BAD_REQUEST))
        payload["ok"] = True
        return _tag(request, payload)

    def _reader(self, request):
        file = self._target(request)
        if file is None:
            return None, error_reply(NO_TRANSCRIPT)
        if not self._service.owns(file):
            return None, error_reply(BAD_TRANSCRIPT)
        return self._service.reader(file), None

    def _target(self, request):
        """A handle first, then an explicit file, then the session's own
        announced conversation - the order the client relies on."""
        tid = request.get("transcriptId")
        if isinstance(tid, str) and tid:
            file = self._service.handle(tid)
            if file:
                return file
        file = request.get("file")
        if isinstance(file, str) and file:
            return file
        return self._service.announced(request.get("session"))


def _tag(request, payload):
    """A reply echoes the request id, so a client can match a stale one."""
    rid = request.get("id")
    if isinstance(rid, str) and rid and isinstance(payload, dict):
        payload = dict(payload)
        payload["id"] = rid
    return payload


def _fields(request):
    fields = request.get("fields", "ref")
    return fields if fields in FIELDS else "ref"
