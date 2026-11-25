"""The session wire: reach, type into, stop and wait on a hosted session.

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

ACTIVITY_EVENTS = 20
PATH_LIMIT = 4096


def wait_key(key):
    """The registry key a session wait holds under, apart from a mailbox."""
    return ("session", key)


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
        # Taken before the text is typed, so a wait from it sees the turn
        # this input starts however soon the run answers.
        cursor = self._start(name)
        answer = self._call(self._sessions.input, name, text)
        return dict(answer, status="ok", nodeId=self._node_id, cursor=cursor)

    def stop(self, _sender, params):
        name = self._name(params)
        answer = self._call(self._sessions.stop, name)
        return dict(answer, status="ok", nodeId=self._node_id)

    # -- waiting on one ----------------------------------------------------

    def wait_plan(self, _sender, params):
        """One validated session wait, as the held registry needs it."""
        plan = self._wait_params(params)
        key = self._sessions.key(plan["name"])
        if key is None:
            raise RpcError(INVALID_PARAMS, "no-session")

        def probe(expired):
            return self._wait_probe(plan, expired)

        return {"key": wait_key(key), "probe": probe,
                "timeout": 0.0 if plan["cursor"] is None else plan["timeout"]}

    def _wait_probe(self, plan, expired):
        try:
            page = self._wait_page(plan)
        except SessionError:
            return self._gone(plan), None
        if page["events"] or page["status"] == "ended" or \
                plan["cursor"] is None:
            return page, None
        if expired:
            page["status"] = "timed-out"
            return page, None
        return None, time.monotonic() + self._config.comms_feed_poll

    def _wait_page(self, plan):
        name = plan["name"]
        # The state is read first, so activity is at least as fresh as it.
        record = self._sessions.run_state(name)["record"]
        cursor = plan["cursor"]
        events, more = [], False
        if cursor is None:
            # A wait starts now: what the run did before it is not news.
            since = self._start(name)["activity"]
        else:
            if cursor["stateSeq"] != record.get("seq"):
                events.append({"type": "state", "state": record.get("state"),
                               "seq": record.get("seq")})
            since, more = self._activity(name, cursor["activity"], events)
        # A page with activity still to read never declares the run over: a
        # waiter that stops on `ended` would lose the rest. The next page
        # says it.
        ended = None if more else seal_reason(record.get("ended"))
        if ended:
            events.append({"type": "ended", "reason": ended})
        return {"status": "ended" if ended else "ok",
                "nodeId": self._node_id, "name": name,
                "state": record.get("state"), "events": events,
                "cursor": {"name": name, "activity": since,
                           "stateSeq": record.get("seq")},
                "more": more,
                "maxWaitMs": int(self._config.comms_wait_max * 1000)}

    def _start(self, name):
        """A cursor at the newest entry: a wait from it sees only news."""
        file = self._sessions.recorded_file(name)
        catalog = self._transcripts.catalog()
        return {"name": name, "stateSeq": None,
                "activity": catalog.head(file) if file and catalog else None}

    def _activity(self, name, since, events):
        """Activity events that fit the page; returns its cursor and `more`."""
        file = self._sessions.recorded_file(name)
        if not file:
            return since, False
        budget = self._config.feed_page_bytes
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
                # An entry admitted already, or room taken by a state event,
                # leaves this one to a page of its own: it keeps its text
                # there, where eliding it would lose it for good.
                if admitted or len(json.dumps([candidate])) <= budget:
                    return cursor, True
                # Not even an empty page could carry it. Name it without
                # its text, so the cursor always moves: an entry no page
                # can carry must never hold a sealed run's ending back.
                candidate = {"type": "activity", "kind": event.get("kind"),
                             "entryId": event["entryId"], "elided": True}
            events.append(candidate)
            admitted += 1
            cursor = event["entryId"]
            used = len(json.dumps(events))
            if used >= budget:
                return cursor, index + 1 < last or page["hasMore"]
        return cursor, page["hasMore"]

    def _gone(self, plan):
        """A session that left nothing behind is over, not an error."""
        return {"status": "ended", "nodeId": self._node_id,
                "name": plan["name"], "state": None,
                "events": [{"type": "ended", "reason": "gone"}],
                "cursor": dict(plan["cursor"] or {}, name=plan["name"]),
                "more": False}

    # -- validation --------------------------------------------------------

    def _wait_params(self, params):
        name = self._name(params)
        timeout = self._millis(params.get("timeoutMs"))
        return {"name": name,
                "cursor": self._cursor(params.get("cursor"), name),
                "timeout": self._config.comms_wait_max if timeout is None
                else min(timeout, self._config.comms_wait_max)}

    def _cursor(self, value, name):
        if value is None:
            return None
        if not isinstance(value, dict) or value.get("name") != name:
            raise RpcError(INVALID_PARAMS, "a cursor belongs to one session")
        cursor = {"name": name}
        entry = value.get("stateSeq")
        if entry is None:
            cursor["stateSeq"] = None
        elif isinstance(entry, bool) or not isinstance(entry, int) or \
                entry < 0:
            raise RpcError(INVALID_PARAMS, "cursor.stateSeq must be a number")
        else:
            cursor["stateSeq"] = entry
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
