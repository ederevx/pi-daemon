"""The session wire: reach, type into, stop and watch a hosted session.

Every method here is named `comms.session.*`, so `comms.remote` forwards it
unchanged and the node that hosts the session is the one that validates and
answers it. Nothing here keeps a session: the sessions domain owns the runs
and the transcripts domain owns the conversations, and this module only
shapes what a caller asked for into what those two answer.
"""

import json
import time

from ..sessions.error import SessionError
from ..transcripts.catalog import CatalogError
from ..transport.rpc_codec import INVALID_PARAMS, RpcError

FEED = "comms.session.feed"
CONTROL = ("comms.session.open", "comms.session.input",
           "comms.session.stop")
INCLUDE = ("output", "activity", "state")
# An output page reads this fraction of its budget as bytes: one non-ASCII
# byte can cost six in an escaped frame, and a page must fit one frame.
ESCAPE_FACTOR = 6
ACTIVITY_EVENTS = 20
# A page keeps at least this much room, so an activity entry that cannot fit
# in it still names itself: a caller's smaller maxBytes is raised to it.
PAGE_FLOOR = 256
PATH_LIMIT = 4096


def feed_key(key):
    """The registry key a feed waits under, apart from a mailbox name."""
    return ("feed", key)


def seal_reason(seal):
    """The word a sealed run's ending is reported as, or None."""
    if not isinstance(seal, dict):
        return None
    return seal.get("cause") or seal.get("verdict")


class SessionWire:
    """It validates the session wire and projects the two domains.

    `sessions` answers what a run is doing and prints, `transcripts`
    answers which conversations exist and what one did; this class owns no
    answer of its own and no state.
    """

    def __init__(self, sessions, transcripts, config, node_id):
        self._sessions = sessions
        self._transcripts = transcripts
        self._config = config
        self._node_id = node_id

    def handlers(self, control=True):
        handlers = {"comms.session.list": self.list,
                    "comms.session.conversations": self.conversations}
        if control:
            handlers.update({"comms.session.open": self.open,
                             "comms.session.input": self.input,
                             "comms.session.stop": self.stop})
        return handlers

    # -- what exists -------------------------------------------------------

    def list(self, _sender, params):
        include_stopped = params.get("all") is True
        views = self._sessions.sessions(include_stopped)
        return {"status": "ok", "nodeId": self._node_id,
                "sessions": [self._view(view) for view in views],
                "stopped": 0 if include_stopped
                else self._sessions.stopped_count()}

    def conversations(self, _sender, params):
        page = self._call(self._catalog().conversations,
                          self._path(params.get("dir")),
                          self._count(params.get("limit")),
                          self._text(params.get("after"), "after"))
        return dict(page, status="ok", nodeId=self._node_id)

    # -- reaching one ------------------------------------------------------

    def open(self, _sender, params):
        file, dir_ = self._target(params)
        answer = self._call(self._sessions.open, file, dir_)
        return dict(answer, nodeId=self._node_id)

    def input(self, _sender, params):
        name = self._name(params)
        text = self._text(params.get("text"), "text")
        if text is None:
            raise RpcError(INVALID_PARAMS, "text is required")
        answer = self._call(self._sessions.input, name, text)
        return dict(answer, status="ok", nodeId=self._node_id)

    def stop(self, _sender, params):
        name = self._name(params)
        answer = self._call(self._sessions.stop, name)
        return dict(answer, status="ok", nodeId=self._node_id)

    # -- watching one ------------------------------------------------------

    def feed_plan(self, _sender, params):
        """One validated feed, as the held registry needs it."""
        plan = self._feed_params(params)
        key = self._sessions.key(plan["name"])
        if key is None:
            raise RpcError(INVALID_PARAMS, "no-session")

        def probe(expired):
            return self._feed_probe(plan, expired)

        return {"key": feed_key(key), "probe": probe,
                "timeout": 0.0 if plan["cursor"] is None else plan["timeout"]}

    def _feed_probe(self, plan, expired):
        try:
            page = self._feed_page(plan)
        except SessionError:
            return self._gone(plan), None
        if page["events"] or page["status"] == "ended" or \
                plan["cursor"] is None:
            return page, None
        if expired:
            page["status"] = "timed-out"
            return page, None
        wait = (self._config.comms_feed_settle if "output" in plan["include"]
                else self._config.comms_feed_poll)
        return None, time.monotonic() + wait

    def _feed_page(self, plan):
        name = plan["name"]
        cursor = plan["cursor"] or {}
        include = plan["include"]
        budget = plan["max_bytes"]
        events = []
        state = self._sessions.run_state(name)
        record = state["record"]
        generation = cursor.get("generation")
        output = None
        next_output = cursor.get("output") or 0
        if "output" in include:
            output = self._sessions.output(name, next_output,
                                           max(1, budget // ESCAPE_FACTOR))
            if generation is not None and \
                    output["generation"] != generation:
                events.append({"type": "reset",
                               "generation": output["generation"]})
                output = self._sessions.output(name, 0,
                                               max(1, budget // ESCAPE_FACTOR))
            generation = output["generation"]
            next_output = output["next"]
            if output["text"] or output["skipped"]:
                events.append({"type": "output", "offset": output["offset"],
                               "text": output["text"],
                               "skipped": output["skipped"]})
        ended = None
        if output is not None:
            if output["ended"] is not None and output["next"] >= output["end"]:
                ended = output["ended"]
        else:
            # A run that has ended says so without being read for output:
            # a watcher reading only activity would otherwise hold until its
            # deadline on every page and never learn the run is over.
            ended = seal_reason(record.get("ended"))
        if "state" in include and cursor.get("stateSeq") != record.get("seq"):
            events.append({"type": "state", "state": record.get("state"),
                           "seq": record.get("seq")})
        next_activity, more = cursor.get("activity"), False
        if "activity" in include:
            next_activity, more = self._activity(
                name, cursor.get("activity"), events, budget)
        # Like a page that stops short of the tail, a page with activity
        # still to read never declares the run over: a watcher that stops
        # on `ended` would lose the rest. The next page says it.
        if more:
            ended = None
        if ended:
            events.append({"type": "ended", "reason": ended})
        return {"status": "ended" if ended else "ok",
                "nodeId": self._node_id, "name": name,
                "generation": generation, "state": record.get("state"),
                "events": events,
                "cursor": {"name": name, "generation": generation,
                           "output": next_output, "activity": next_activity,
                           "stateSeq": record.get("seq")},
                "more": more}

    def _activity(self, name, since, events, budget):
        """Activity events that fit the page; returns its cursor and `more`."""
        file = self._sessions.recorded_file(name)
        if not file:
            return since, False
        try:
            page = self._catalog().activity(
                file, since, self._config.comms_feed_text_chars,
                ACTIVITY_EVENTS)
        except CatalogError:
            events.append({"type": "activity", "kind": "gap"})
            page = self._catalog().activity(
                file, None, self._config.comms_feed_text_chars,
                ACTIVITY_EVENTS)
        used = len(json.dumps(events))
        cursor, admitted = since, 0
        last = len(page["events"])
        for index, event in enumerate(page["events"]):
            candidate = dict(event, type="activity")
            size = len(json.dumps(candidate))
            if used + size > budget:
                # An entry admitted already, or room taken by output or
                # state, leaves this one to a page of its own: it keeps its
                # text there, where eliding it would lose it for good.
                if admitted or len(json.dumps([candidate])) <= budget:
                    return cursor, True
                # Not even an empty page could carry it. Name it without
                # its text, so the cursor always moves: an entry no page
                # can carry must never hold a sealed run's ending back.
                candidate = {"type": "activity", "kind": event.get("kind"),
                             "entryId": event["entryId"], "elided": True}
                size = len(json.dumps(candidate))
            events.append(candidate)
            admitted += 1
            cursor = event["entryId"]
            used = len(json.dumps(events))
            if used >= budget:
                return cursor, index + 1 < last or page["hasMore"]
        return cursor, page["hasMore"]

    def _gone(self, plan):
        """A session that left no tail behind is over, not an error."""
        return {"status": "ended", "nodeId": self._node_id,
                "name": plan["name"], "generation": None, "state": None,
                "events": [{"type": "ended", "reason": "gone"}],
                "cursor": dict(plan["cursor"] or {}, name=plan["name"]),
                "more": False}

    # -- validation --------------------------------------------------------

    def _feed_params(self, params):
        name = self._name(params)
        include = params.get("include")
        if include is None:
            file = self._sessions.recorded_file(name)
            include = ("activity", "state") if file else ("output", "state")
        elif isinstance(include, list) and include:
            if not all(isinstance(item, str) and item in INCLUDE
                       for item in include):
                raise RpcError(INVALID_PARAMS,
                               "include names output, activity or state")
            include = tuple(dict.fromkeys(include))
        else:
            raise RpcError(INVALID_PARAMS, "include must be a list")
        timeout = self._millis(params.get("timeoutMs"))
        limit = self._bytes(params.get("maxBytes"))
        budget = self._config.feed_page_bytes
        return {"name": name,
                "cursor": self._cursor(params.get("cursor"), name),
                "include": include,
                "timeout": self._config.comms_wait_max if timeout is None
                else min(timeout, self._config.comms_wait_max),
                "max_bytes": budget if limit is None else
                min(max(limit, PAGE_FLOOR), budget)}

    def _cursor(self, value, name):
        if value is None:
            return None
        if not isinstance(value, dict) or value.get("name") != name:
            raise RpcError(INVALID_PARAMS, "a cursor belongs to one session")
        cursor = {"name": name}
        for field, kind in (("generation", int), ("output", int),
                            ("stateSeq", int)):
            entry = value.get(field)
            if entry is None:
                cursor[field] = None
                continue
            if isinstance(entry, bool) or not isinstance(entry, kind) or \
                    entry < 0:
                raise RpcError(INVALID_PARAMS,
                               f"cursor.{field} must be a number")
            cursor[field] = entry
        activity = value.get("activity")
        if activity is not None and not isinstance(activity, str):
            raise RpcError(INVALID_PARAMS, "cursor.activity must be an id")
        cursor["activity"] = activity
        return cursor

    def _target(self, params):
        """The conversation one open names, and the directory it runs in."""
        given = [key for key in ("name", "file", "sessionId")
                 if params.get(key) is not None]
        if len(given) != 1:
            raise RpcError(INVALID_PARAMS,
                           "exactly one of name, file or sessionId")
        dir_ = self._path(params.get("dir"))
        if given[0] == "name":
            recorded = self._sessions.recorded(self._name(params))
            if recorded is None:
                raise RpcError(INVALID_PARAMS, "no-session")
            if not recorded.get("file"):
                raise RpcError(INVALID_PARAMS, "no-file")
            return recorded["file"], dir_ or recorded.get("dir")
        if given[0] == "file":
            file = self._path(params.get("file"))
            if not file or not self._transcripts.owns(file):
                raise RpcError(INVALID_PARAMS, "bad-transcript")
            return file, dir_ or self._cwd(file)
        entry = self._call(self._catalog().resolve,
                           self._text(params.get("sessionId"), "sessionId"))
        return entry["file"], dir_ or entry.get("cwd")

    def _cwd(self, file):
        entry = self._catalog().entry(file)
        return entry.get("cwd") if entry else None

    def _catalog(self):
        catalog = self._transcripts.catalog()
        if catalog is None:
            raise RpcError(INVALID_PARAMS, "transcripts are not served here")
        return catalog

    def _name(self, params):
        name = self._text(params.get("name"), "name")
        if not name:
            raise RpcError(INVALID_PARAMS, "name is required")
        if len(name) > self._config.comms_agent_id_limit:
            raise RpcError(INVALID_PARAMS, "name must be short")
        return name

    @staticmethod
    def _text(value, label):
        if value is None:
            return None
        if not isinstance(value, str) or "\x00" in value:
            raise RpcError(INVALID_PARAMS, f"{label} must be text")
        return value

    @staticmethod
    def _path(value):
        if value is None:
            return None
        if not isinstance(value, str) or not value or \
                len(value) > PATH_LIMIT:
            raise RpcError(INVALID_PARAMS, "a path is required")
        return value

    @staticmethod
    def _count(value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RpcError(INVALID_PARAMS, "limit must be a positive integer")
        return value

    @staticmethod
    def _bytes(value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RpcError(INVALID_PARAMS,
                           "maxBytes must be a positive integer")
        return value

    @staticmethod
    def _millis(value):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or \
                value < 0:
            raise RpcError(INVALID_PARAMS,
                           "a number of milliseconds is required")
        return float(value) / 1000.0

    @staticmethod
    def _call(call, *args):
        try:
            return call(*args)
        except (SessionError, CatalogError) as failure:
            raise RpcError(INVALID_PARAMS, failure.code) from None

    @staticmethod
    def _view(view):
        return {field: view.get(field) for field in
                ("name", "state", "dir", "attached", "clients", "file",
                 "phase", "fileExists") if field in view}
