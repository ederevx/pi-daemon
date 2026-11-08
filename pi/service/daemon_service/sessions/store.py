"""Durable session names, generations and run outcomes: one owner."""

import json
import os
import threading
import time
import uuid

from ..atomic_state_file import AtomicStateFile
from .error import SessionError
from .model import SessionRecord
from .run_outcome import RunOutcome

LIVE = ("spawning", "running", "reviving")


class SessionStore:
    """Names and run records are durable; processes belong to the runner."""

    def __init__(self, path, run_state_dir, config):
        self._file = AtomicStateFile(path, config.file_mode,
                                     config.write_retries,
                                     config.write_retry_delay)
        self._run_dir = run_state_dir
        self._config = config
        self._lock = threading.RLock()
        self._sessions = {}
        self._files = {}
        self._run_keys = {}
        self._run_states = {}
        self._handoffs = {}

    # -- loading and saving ------------------------------------------------

    def load(self):
        with self._lock:
            document = self._read_document()
            self._sessions = {}
            self._files = dict(document.get("files") or {})
            self._handoffs = dict(document.get("handoffs") or {})
            self._run_keys = dict(document.get("run_keys") or {})
            for name, data in (document.get("sessions") or {}).items():
                record = SessionRecord.from_dict(data)
                record.name = name
                self._sessions[name] = record
            return self

    def save(self):
        with self._lock:
            self._write_document()

    def _read_document(self):
        text = self._file.read()
        if not text:
            return {}
        try:
            document = json.loads(text)
        except ValueError:
            raise ValueError("the session registry is unreadable") from None
        if not isinstance(document, dict) or document.get("version") != 1:
            raise ValueError("the session registry has an unknown version")
        return document

    def _write_document(self):
        self._file.write_dict({
            "version": 1,
            "sessions": {name: record.to_dict()
                         for name, record in self._sessions.items()},
            "files": dict(self._files),
            "handoffs": dict(self._handoffs),
            "run_keys": dict(self._run_keys),
        })

    # -- names -------------------------------------------------------------

    def get(self, name):
        with self._lock:
            return self._sessions.get(name)

    def resolve(self, name):
        """The stored spelling for a name: exact, then the pi- variant."""
        with self._lock:
            if name in self._sessions:
                return name
            alternate = name[3:] if name.startswith("pi-") else "pi-" + name
            if alternate in self._sessions:
                return alternate
            return None

    def live(self):
        with self._lock:
            return [record for record in self._sessions.values()
                    if record.live()]

    def snapshot(self):
        with self._lock:
            return [record.view() for record in self._sessions.values()
                    if record.live()]

    def file_of(self, name):
        with self._lock:
            record = self._sessions.get(name)
            if record is not None and record.file:
                return record.file
            return self._files.get(name)

    def holders(self, file):
        """Live sessions holding one conversation, attached ones first."""
        with self._lock:
            found = [record for record in self._sessions.values()
                     if record.live() and record.file == file]
            found.sort(key=lambda record: -int(record.child.get("clients") or 0))
            return [(record.name, record.state()) for record in found]

    def announced(self, file):
        with self._lock:
            return [record.name for record in self._sessions.values()
                    if record.live() and record.file == file]

    # -- lifecycle ---------------------------------------------------------

    def reserve(self, name, dir_, argv, cols, rows, env, file, resume_argv,
                owner, now=None):
        """Reserve a name for a spawn, or replay the live holder."""
        now = time.time() if now is None else now
        with self._lock:
            existing = self._sessions.get(name)
            if existing is not None and existing.live():
                return existing, False
            previous = self._run_state_for(self._run_keys.get(name))
            record = SessionRecord(
                id=uuid.uuid4().hex,
                generation=uuid.uuid4().hex,
                name=name,
                dir=dir_,
                argv=list(argv),
                resume_argv=list(resume_argv or argv),
                env=dict(env or {}),
                file=file or (previous or {}).get("file"),
                cols=cols,
                rows=rows,
                desired="running",
                phase="spawning",
                owner=dict(owner),
                child={},
                created=now,
                started=None,
            )
            self._sessions[name] = record
            self._run_keys[name] = record.id
            self._open_run_state(record, previous, now)
            self._write_document()
            return record, True

    def mark_started(self, key, pid, identity, now=None):
        now = time.time() if now is None else now
        with self._lock:
            record = self._by_id(key)
            if record is None or record.phase != "spawning":
                raise LookupError(key)
            record.phase = "running"
            record.started = now
            record.child = {"pid": int(pid), "identity": identity.to_dict(),
                            "clients": 0}
            state = self._run_states.get(key)
            if state is not None:
                state["child_pid"] = int(pid)
                state["changed"] = now
                self._write_run_state(key, state)
            self._write_document()
            return record

    def publish(self, name, state, seq=None, pid=None, aborted=None,
                now=None):
        """A run-state publish, accepted only for the current generation."""
        now = time.time() if now is None else now
        with self._lock:
            record = self._sessions.get(name)
            if record is None or not record.live():
                return False
            current = self._run_states.get(record.id)
            if current is None:
                return False
            if pid is not None and record.pid() and int(pid) != record.pid():
                return False
            if seq is not None and current.get("seq") is not None \
                    and int(seq) < int(current["seq"]):
                return False
            previous = current.get("state")
            if state == "busy" and previous not in ("busy", "waiting"):
                current["run"] = int(current.get("run") or 0) + 1
                current["aborted"] = False
            if aborted is True:
                current["aborted"] = True
            current["state"] = state
            current["announced"] = True
            if seq is not None:
                current["seq"] = int(seq)
            current["seen"] = now
            current["changed"] = now
            self._write_run_state(record.id, current)
            record.child["state"] = state
            self._write_document()
            return True

    def announce(self, name, file, argv=None):
        with self._lock:
            record = self._sessions.get(name)
            if record is None or not record.live():
                raise SessionError("no-session")
            record.file = file
            if argv:
                record.resume_argv = list(argv)
            self._files[name] = file
            state = self._run_states.get(record.id)
            if state is not None:
                state["file"] = file
                state["changed"] = time.time()
                self._write_run_state(record.id, state)
            self._write_document()

    def set_handoff(self, name, target):
        with self._lock:
            if self._sessions.get(name) is None:
                raise SessionError("no-session")
            self._handoffs[name] = {"name": target}
            self._write_document()

    def handoff(self, name):
        with self._lock:
            return self._handoffs.get(name)

    def clear_handoff(self, name):
        with self._lock:
            if self._handoffs.pop(name, None) is not None:
                self._write_document()

    def request_stop(self, key, reason="stop"):
        with self._lock:
            record = self._by_id(key)
            if record is None:
                raise SessionError("no-session")
            record.desired = "stopped"
            state = self._run_states.get(key)
            if state is not None:
                state["stop_reason"] = reason
                self._write_run_state(key, state)
            self._write_document()

    def stop_reason(self, key):
        state = self._run_states.get(key) or {}
        return state.get("stop_reason")

    def set_clients(self, name, clients):
        with self._lock:
            record = self._sessions.get(name)
            if record is None or not record.live():
                return
            record.child["clients"] = int(clients)
            self._write_document()

    def finish(self, key, reason, returncode, now=None):
        """Seal the run and mark the record exited, never deleted."""
        now = time.time() if now is None else now
        with self._lock:
            record = self._by_id(key)
            if record is None:
                return None
            state = self._run_states.get(key) \
                or self._run_state_for(key) or {}
            if record.phase == "exited" or state.get("ended"):
                return state.get("ended")
            state.setdefault("state", "idle")
            state.setdefault("seq", 0)
            verdict = RunOutcome.seal(
                {"aborted": state.get("aborted")}, state.get("state"),
                _announced(state), reason, returncode, now)
            # The verdict is `ended`; history only ever holds past runs, so
            # a reopen can carry the seal forward without repeating it.
            state["ended"] = verdict
            state["open"] = False
            state["changed"] = now
            self._write_run_state(key, state)
            record.phase = "exited"
            record.child = {"state": state.get("state"), "clients": 0}
            record.started = None
            self._write_document()
            return verdict

    def discard(self, key):
        """Drop a reservation that never became a run."""
        with self._lock:
            record = self._by_id(key)
            if record is None:
                return False
            self._sessions.pop(record.name, None)
            if self._run_keys.get(record.name) == key:
                self._run_keys.pop(record.name, None)
            self._run_states.pop(key, None)
            self._remove_run_state(key)
            self._write_document()
            return True

    def attachable(self, name):
        with self._lock:
            record = self._sessions.get(name)
            return record if record is not None and record.phase == "running" \
                else None

    def recover(self, instance, skip=(), now=None):
        """A new instance owns no child: seal what the last one left open.

        A record whose process cannot be proven gone is named in `skip`: its
        name is released, its run stays open as unsealed evidence.
        """
        now = time.time() if now is None else now
        with self._lock:
            settled = 0
            for record in self._sessions.values():
                if record.owner.get("instance") == instance:
                    continue
                if not record.live():
                    continue
                state = self._run_states.get(record.id) \
                    or self._run_state_for(record.id) or {}
                record.phase = "exited"
                record.child = {"state": state.get("state") or "idle",
                                "clients": 0}
                record.started = None
                if state.get("open") and record.id not in set(skip):
                    state["ended"] = RunOutcome.seal(
                        {"aborted": state.get("aborted")},
                        state.get("state"), _announced(state), "lost", None,
                        now)
                    state["open"] = False
                    state["changed"] = now
                    self._write_run_state(record.id, state)
                settled += 1
            if settled:
                self._write_document()
            return settled

    # -- lookups -----------------------------------------------------------

    def run_state(self, name):
        with self._lock:
            canonical = self.resolve(name)
            if canonical is None:
                return None
            record = self._sessions[canonical]
            state = self._run_states.get(record.id)
            if state is None:
                state = self._run_state_for(record.id)
            if state is None:
                return None
            projection = dict(state)
            projection["name"] = record.name
            return projection

    def name_of(self, key):
        record = self._by_id(key)
        return record.name if record is not None else None

    def _by_id(self, key):
        for record in self._sessions.values():
            if record.id == key:
                return record
        return None

    # -- run-state files ---------------------------------------------------

    def _path_for(self, key):
        return os.path.join(self._run_dir, f"{key}.json")

    def _run_state_for(self, key):
        if not key:
            return None
        if key in self._run_states:
            return self._run_states[key]
        try:
            with open(self._path_for(key), "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(state, dict):
            return None
        self._run_states[key] = state
        return state

    def _open_run_state(self, record, previous, now):
        history = list((previous or {}).get("history") or [])
        if previous and previous.get("ended"):
            history.append(previous["ended"])
        state = {
            "v": 1,
            "boot": (record.owner or {}).get("boot"),
            "owner_pid": (record.owner or {}).get("pid"),
            "owner_token": (record.owner or {}).get("start"),
            "child_pid": None,
            "file": record.file,
            "state": "idle",
            "aborted": False,
            "announced": False,
            "run": 0,
            "seq": 0,
            "seen": now,
            "changed": now,
            "opened": now,
            "open": True,
            "ended": None,
            "history": history[-int(self._config.history_limit):],
        }
        self._run_states[record.id] = state
        self._write_run_state(record.id, state)

    def _write_run_state(self, key, state):
        directory = self._run_dir
        os.makedirs(directory, exist_ok=True)
        temporary = os.path.join(directory,
                                 f"{key}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                             self._config.file_mode)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(state, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path_for(key))
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _remove_run_state(self, key):
        try:
            os.unlink(self._path_for(key))
        except OSError:
            pass


def _announced(state):
    return bool(state.get("announced")) or int(state.get("seq") or 0) > 0
