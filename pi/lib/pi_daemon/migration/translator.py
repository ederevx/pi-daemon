"""Legacy data becoming new model objects; no legacy code is imported."""

import uuid

from ..sessions.model import SessionRecord
from ..sessions.run_outcome import RunOutcome
from ..tasks.model import DONE, FAILED, RUNNING, CANCELLED, Ticket

STATUSES = (RUNNING, DONE, FAILED, CANCELLED, "lost")
SHELL = "shell"
KNOWN_STATES = ("busy", "idle", "waiting")
LOSSES = ("lost-mid-run", "lost-at-prompt")


class LegacyTranslator:
    """Pure conversion: the input is a decoded document, the output a model.

    It knows the legacy shapes and nothing about paths or writes, so a
    translation can be previewed without touching a store.
    """

    def __init__(self, config, now):
        self._config = config
        self._now = now

    def tickets(self, document):
        raw = document.get("tickets")
        raw = raw if isinstance(raw, dict) else {}
        tickets, dropped, legacy = [], [], {}
        for ticket_id in sorted(raw, key=lambda key: self._order(key, raw[key])):
            record = raw[ticket_id]
            if not isinstance(record, dict):
                dropped.append(self._drop(ticket_id, "record"))
                continue
            legacy[str(ticket_id)] = str(record.get("status") or "")
            if record.get("kind", SHELL) != SHELL:
                dropped.append(self._drop(ticket_id, "kind"))
                continue
            if record.get("status") not in STATUSES:
                dropped.append(self._drop(ticket_id, "status"))
                continue
            tickets.append(self._ticket(str(ticket_id), record))
        return {"tickets": tickets, "next": self._next(document, raw),
                "dropped": dropped, "legacy": legacy}

    def sessions(self, registry, run_states):
        records, states, dropped = [], {}, []
        for name in sorted(set(registry) | set(run_states)):
            entry = registry.get(name)
            valid = self._valid(entry)
            if entry is not None and not valid:
                dropped.append(self._drop(name, "invalid entry"))
            run = run_states.get(name) or {}
            argv = list(entry["argv"]) if valid else []
            state, _sealed = self.run_state(run)
            record = SessionRecord(
                id=uuid.uuid4().hex,
                generation=uuid.uuid4().hex,
                name=name,
                dir=entry["dir"] if valid else None,
                argv=argv,
                resume_argv=list(argv),
                env=dict(entry.get("env") or {}) if valid else {},
                file=run.get("file") or self._session_file(argv),
                cols=None,
                rows=None,
                desired="running" if valid else "stopped",
                phase="exited",
                owner={},
                child={"state": state["state"], "clients": 0},
                created=run.get("opened") or self._now,
                started=None,
            )
            records.append(record)
            states[record.id] = state
        return {"records": records, "states": states, "dropped": dropped}

    def run_state(self, run):
        if not isinstance(run, dict):
            run = {}
        announced = bool(run.get("seen")) or int(run.get("seq") or 0) > 0
        state = run.get("state") if run.get("state") in KNOWN_STATES else "idle"
        open_ = bool(run.get("open"))
        if open_:
            ended = RunOutcome.seal({"aborted": bool(run.get("aborted"))},
                                    state, announced, "lost", None, self._now)
        else:
            ended = self.ended(run.get("ended"))
        history = [self.ended(item) for item in run.get("history") or []
                   if isinstance(item, dict)]
        history = [item for item in history if item is not None]
        return {
            "v": 1,
            "boot": run.get("boot"),
            "owner_pid": run.get("owner_pid"),
            "owner_token": run.get("owner_token"),
            "child_pid": run.get("child_pid"),
            "file": run.get("file"),
            "state": state,
            "aborted": bool(run.get("aborted")),
            "announced": announced,
            "run": int(run.get("run") or 0),
            "seq": int(run.get("seq") or 0),
            "seen": run.get("changed"),
            "changed": run.get("changed"),
            "opened": run.get("opened"),
            "open": False,
            "ended": ended,
            "history": history[-int(self._config.history_limit):],
        }, open_

    @staticmethod
    def ended(verdict):
        """One sealed verdict in the new shape; the old signal was a flag."""
        if not isinstance(verdict, dict) or not verdict.get("verdict"):
            return None
        cause = verdict.get("cause")
        if verdict["verdict"] in LOSSES:
            cause = "lost"
        elif cause == "never-announced":
            cause = "unknown"
        return {"verdict": verdict["verdict"], "cause": cause, "signal": None,
                "exit": verdict.get("exit"), "state": verdict.get("state"),
                "at": verdict.get("at")}

    @staticmethod
    def _ticket(ticket_id, record):
        return Ticket(
            ticket_id=ticket_id,
            session=str(record.get("session") or ""),
            cwd=str(record.get("cwd") or ""),
            command=str(record.get("command") or ""),
            timeout=float(record.get("timeout") or 0.0),
            status=FAILED if record["status"] == "lost" else record["status"],
            created=float(record.get("created") or 0.0),
            pid=record.get("pid"),
            started=record.get("started"),
            ended=record.get("finished"),
            exit_code=record.get("exit"),
            output_bytes=0,
            truncated=bool(record.get("truncated")),
            error=str(record.get("error") or ""),
        )

    @staticmethod
    def _valid(entry):
        if not isinstance(entry, dict) or not isinstance(entry.get("dir"), str):
            return False
        argv = entry.get("argv")
        return isinstance(argv, list) and bool(argv) \
            and all(isinstance(item, str) for item in argv) \
            and isinstance(entry.get("env", {}), dict)

    @staticmethod
    def _session_file(argv):
        """The conversation a legacy argv names, after its `--session`."""
        for index, item in enumerate(argv):
            if item == "--session" and index + 1 < len(argv):
                return argv[index + 1]
        return None

    @staticmethod
    def _next(document, raw):
        stored = document.get("next")
        try:
            stored = int(stored or 0)
        except (TypeError, ValueError):
            stored = 0
        highest = 0
        for ticket_id in raw:
            try:
                highest = max(highest, int(str(ticket_id).rsplit("-", 1)[-1]))
            except (TypeError, ValueError):
                continue
        return max(stored, highest + 1)

    @staticmethod
    def _order(ticket_id, record):
        created = record.get("created") if isinstance(record, dict) else 0
        try:
            created = float(created or 0)
        except (TypeError, ValueError):
            created = 0.0
        return created, str(ticket_id)

    @staticmethod
    def _drop(source, reason):
        return {"source": source, "reason": reason}
