"""What one translation would do, decided without writing anything."""

import os

from .error import LOCKED, SOURCES_MOVED, UNMARKED
from .marker import MigrationMarker
from .translator import LegacyTranslator

TICKETS = "tickets.json"
SESSIONS = "sessions.json"
RUN_STATE = "run-state"
LOGS = "tickets"
SESSION_FILES = "session-files.json"
REAP = "gc-reap"

TRANSLATE = "translate"
CURRENT = "current"
REFUSE = "refuse"

INSTANCE_LOCKED = "instance-locked"
SOURCES_CHANGED = "sources-changed"
UNMARKED_STATE = "unmarked-state"

REFUSALS = {
    INSTANCE_LOCKED: (LOCKED,
                      "the instance lock is held; stop the service first"),
    SOURCES_CHANGED: (
        SOURCES_MOVED, "the legacy state changed since the last migration; "
        "--force replaces the translated stores"),
    UNMARKED_STATE: (
        UNMARKED, "the state root holds untranslated state; --force "
        "replaces the translated stores"),
}


class MigrationPlan:
    """The reads, the translations and the decision, held as one object."""

    def __init__(self, layout, config, legacy, force, locked, clock):
        self._layout = layout
        self._config = config
        self._legacy = legacy
        self._force = bool(force)
        self._locked = bool(locked)
        self._translator = LegacyTranslator(config, clock())
        self._action = TRANSLATE
        self._refusal = None
        self._changed = []
        self._tickets = []
        self._ticket_legacy = {}
        self._next = 0
        self._logs = []
        self._sessions = []
        self._run_states = {}
        self._sealed = set()
        self._dropped = []
        self._read()
        self._decide()

    # -- the decision ------------------------------------------------------

    def _read(self):
        self._read_legacy()
        marker = MigrationMarker(self._layout.migration_path(),
                                 self._config).read()
        self._marker = marker

    def _decide(self):
        sources = self._legacy.digests()
        if self._locked:
            self._action, self._refusal = REFUSE, INSTANCE_LOCKED
            return
        if self._marker is None:
            if self._state_present() and not self._force:
                self._action, self._refusal = REFUSE, UNMARKED_STATE
            return
        self._changed = self._moved(self._marker, sources)
        if not self._changed:
            self._action, self._refusal = CURRENT, None
        elif not self._force:
            self._action, self._refusal = REFUSE, SOURCES_CHANGED

    def _moved(self, marker, sources):
        previous = marker.get("sources") or {}
        moved = sorted(key for key in set(previous) | set(sources)
                       if previous.get(key) != sources.get(key))
        if marker.get("legacy_root") != self._legacy.root():
            moved.append(self._legacy.root())
        return moved

    def _state_present(self):
        try:
            entries = os.listdir(self._layout.state_root())
        except (FileNotFoundError, NotADirectoryError):
            return False
        settings = os.path.basename(self._layout.settings_path())
        return any(entry != settings for entry in entries)

    # -- the translated state ---------------------------------------------

    def _read_legacy(self):
        self._legacy_read_tickets()
        self._legacy_read_sessions()
        if self._legacy.exists(SESSION_FILES):
            self._dropped.append({"source": SESSION_FILES,
                                  "reason": "transcript index"})
        for entry in self._legacy.names(REAP, ".json"):
            self._dropped.append({"source": f"{REAP}/{entry}",
                                  "reason": "no reap request"})

    def _legacy_read_tickets(self):
        translated = self._translator.tickets(
            self._legacy.read_object(TICKETS) or {})
        self._tickets = translated["tickets"]
        self._next = translated["next"]
        self._ticket_legacy = translated["legacy"]
        self._dropped.extend(translated["dropped"])
        kept = {ticket.ticket_id for ticket in self._tickets}
        for entry in self._legacy.names(LOGS, ".log"):
            ticket_id = entry[:-len(".log")]
            if ticket_id not in kept:
                continue
            data = self._legacy.read_bytes(f"{LOGS}/{entry}")
            if data is None:
                continue
            self._logs.append((ticket_id, f"{LOGS}/{entry}", len(data)))

    def _legacy_read_sessions(self):
        registry = self._legacy.read_object(SESSIONS) or {}
        runs = {}
        for entry in self._legacy.names(RUN_STATE, ".json"):
            name = entry[:-len(".json")]
            runs[name] = self._legacy.read_object(f"{RUN_STATE}/{entry}") or {}
        translated = self._translator.sessions(registry, runs)
        self._sessions = translated["records"]
        self._run_states = translated["states"]
        self._dropped.extend(translated["dropped"])
        self._sealed = {record.id for record in self._sessions
                        if (runs.get(record.name) or {}).get("open")}

    # -- published answers -------------------------------------------------

    def action(self):
        return self._action

    def refusal(self):
        return self._refusal

    def forced(self):
        return self._force

    def sources(self):
        return self._legacy.digests()

    def tickets(self):
        return list(self._tickets)

    def next_ticket(self):
        return self._next

    def logs(self):
        return [(ticket_id, relative) for ticket_id, relative, _size
                in self._logs]

    def sessions(self):
        return list(self._sessions)

    def run_states(self):
        return dict(self._run_states)

    def refusal_status(self):
        """The refusal's exit code and message, for the command to print."""
        return REFUSALS[self._refusal]

    def to_dict(self, dry_run):
        return {
            "dry_run": bool(dry_run),
            "action": self._action,
            "refusal": self._refusal,
            "force": self._force,
            "legacy_root": self._legacy.root(),
            "state_root": self._layout.state_root(),
            "sources": self.sources(),
            "changed": list(self._changed),
            "tickets": [self._ticket_dict(ticket)
                        for ticket in self._tickets],
            "sessions": [{"name": record.name,
                          "desired": record.desired,
                          "file": record.file,
                          "ended": self._ended(record),
                          "sealed": record.id in self._sealed}
                         for record in self._sessions],
            "dropped": list(self._dropped),
        }

    def _ticket_dict(self, ticket):
        return {"id": ticket.ticket_id,
                "status": ticket.status,
                "legacy_status": self._ticket_legacy.get(ticket.ticket_id),
                "log_bytes": self._log_size(ticket.ticket_id)}

    def _log_size(self, ticket_id):
        for kept_id, _relative, size in self._logs:
            if kept_id == ticket_id:
                return size
        return 0

    def _ended(self, record):
        state = self._run_states.get(record.id) or {}
        return (state.get("ended") or {}).get("verdict")
