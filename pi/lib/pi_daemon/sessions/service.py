"""The sessions domain's surface: one method per published operation."""

import threading
import time

from ..wire import NO_SESSION
from .error import SessionError
from .identity import ProcessIdentity

PROJECTED = ("state", "run", "seq", "opened", "changed", "ended", "history",
             "file", "child_pid", "open")
STATES = ("busy", "idle", "waiting")
# The command a pi older than the reload watcher still understands.
FALLBACK = "/reload\r"


def _reloadable(record):
    """A hosted pi that announced a conversation file reloads in place.

    A `--no-session` run has no TUI to reload, and a `start`ed session's
    live argv carries no conversation, so the announced file is the
    reliable marker of an interactive session.
    """
    return bool(record.file) and "--no-session" not in record.argv


class SessionService:
    """Operations speak names; the runner owns processes and the relay owns IO."""

    def __init__(self, store, runner, relay, config, carry=None,
                 handover=None, reload=None):
        self._store = store
        self._runner = runner
        self._relay = relay
        self._config = config
        self._carry = carry
        self._handover = handover
        self._reload = reload
        # One round at a time: two rounds would overwrite each other's
        # request and neither absence nor a token could tell them apart.
        self._reload_lock = threading.Lock()

    # -- hosting -----------------------------------------------------------

    def start(self, name, dir_, argv, cols, rows, env=None, env_once=None):
        record, created = self._runner.start(name, dir_, argv, cols, rows,
                                             env, env_once)
        return {"name": record.name, "created": created,
                "session": record.view()}

    def sessions(self):
        views = self._store.snapshot()
        for view in views:
            clients = self._relay.viewer_count(view["name"] and
                                               self._key(view["name"]))
            view["clients"] = clients
            view["attached"] = bool(clients)
        return views

    def which(self, file):
        holders = self._holders(file)
        return {"name": holders[0][0] if holders else None}

    def whichall(self, file):
        return [{"name": name, "state": state}
                for name, state in self._holders(file)]

    def live_files(self):
        return self._store.live_files()

    def recorded_file(self, name):
        canonical = self._store.resolve(name)
        return self._store.file_of(canonical) if canonical else None

    def attach_plan(self, name, cols, rows):
        """Resolve an attach without taking the socket yet."""
        record = self._record(name, live=True)
        handoff = self._store.handoff(record.name)
        if handoff is not None:
            target = self._store.resolve(handoff.get("name"))
            live = self._store.get(target) if target else None
            if live is not None and live.phase == "running":
                return {"name": record.name, "handoff": {
                    "name": live.name, "state": live.state()}}
            self._store.clear_handoff(record.name)
        identity = ProcessIdentity.from_dict(record.child.get("identity"))
        if record.pid() <= 0 or ProcessIdentity.current(record.pid()) \
                .classify(identity) == ProcessIdentity.GONE:
            raise SessionError(NO_SESSION)
        return {
            "name": record.name,
            "key": record.id,
            "generation": record.generation,
            "file": record.file,
            "state": record.state(),
            "clients": self._relay.viewer_count(record.id) + 1,
            "cols": cols,
            "rows": rows,
            "handoff": None,
        }

    def attach_now(self, plan, connection, initial=b""):
        attached = self._relay.attach(plan["key"], plan["generation"],
                                      connection, initial)
        if attached:
            self._relay.resize(plan["key"], plan["cols"], plan["rows"])
        self._store.set_clients(plan["name"],
                                self._relay.viewer_count(plan["key"]))
        return attached

    def carry(self, name, to=None, argv=None):
        """/new without termination: move the viewers to a new conversation
        and leave this one running headless."""
        return self._carry.carry(name, to, argv)

    def handover(self, dir_, file):
        """Where a conversation an exiting pi leaves behind would be hosted.

        The target is decided once, here: the name the client is promised
        is the name the exit hosts, whatever the table does meanwhile.
        """
        target = self._handover.target(dir_, file)
        if self._handover.taken(target, dir_):
            return {"target": target, "verdict": "hosted:" + target,
                    "hosted": True}
        return {"target": target, "verdict": "target:" + target,
                "hosted": False}

    def host_handover(self, target, dir_, file, message=None):
        return self._handover.host(target, dir_, file, message)

    def detach(self, name):
        record = self._record(name)
        self._relay.drop_viewers(record.id)
        self._store.set_clients(record.name, 0)
        return {"name": record.name, "detached": True}

    def stop(self, name):
        record = self._record(name)
        return {"name": record.name, **self._runner.stop(record.name)}

    def input(self, name, text):
        record = self._record(name)
        written = self._runner.send(record.name, text.encode("utf-8"))
        return {"name": record.name, "bytes": written}

    def resize(self, name, cols, rows):
        record = self._record(name)
        self._runner.resize(record.name, cols, rows)
        return {"name": record.name, "cols": cols, "rows": rows}

    # -- run state ---------------------------------------------------------

    def state(self, name, state, seq=None, pid=None, aborted=None):
        if state not in STATES:
            raise ValueError("an unknown run state")
        record = self._record(name, live=True)
        accepted = self._store.publish(record.name, state, seq=seq, pid=pid,
                                       aborted=aborted, now=time.time())
        return {"name": record.name, "accepted": accepted}

    def run_state(self, name):
        canonical = self._store.resolve(name)
        if canonical is None:
            raise SessionError(NO_SESSION)
        state = self._store.run_state(canonical)
        if state is None:
            raise SessionError(NO_SESSION)
        record = self._store.get(canonical)
        projection = {field: state.get(field) for field in PROJECTED}
        projection["name"] = canonical
        return {
            "name": canonical,
            "record": projection,
            "hosted": bool(record is not None and record.live()),
        }

    # -- conversations -----------------------------------------------------

    def announce(self, name, file, takeover=False):
        """Bind a name to its conversation, resolving duplicate holders."""
        record = self._record(name, live=True)
        self._store.announce(record.name, file)
        others = [holder for holder in self._holders(file)
                  if holder[0] != record.name]
        if not takeover or not others:
            return self._announced(record.name, None, [], others)
        primary, primary_state = others[0]
        mine = self._relay.viewer_count(record.id)
        theirs = self._relay.viewer_count(self._key(primary))
        if mine > theirs:
            self._store.set_handoff(primary, record.name)
            self._relay.drop_viewers(self._key(primary))
            self._store.set_clients(primary, 0)
            remaining = others[1:]
            return self._announced(record.name, None, [primary], remaining)
        self._store.set_handoff(record.name, primary)
        self._relay.move_viewers(record.id, self._key(primary))
        self._store.set_clients(primary,
                                self._relay.viewer_count(self._key(primary)))
        self._store.set_clients(record.name, 0)
        return self._announced(record.name, {"name": primary,
                                            "state": primary_state},
                               [], others[1:])

    def handoff_target(self, name):
        if not name:
            return None
        canonical = self._store.resolve(name)
        if canonical is None:
            return None
        handoff = self._store.handoff(canonical)
        if handoff is None:
            return None
        target = self._store.resolve(handoff.get("name"))
        live = self._store.get(target) if target else None
        if live is None or live.phase != "running":
            self._store.clear_handoff(canonical)
            return None
        return {"name": live.name, "state": live.state()}

    def resume(self, name, file):
        """Point a conversation's viewers at its live holder, or refuse."""
        record = self._record(name)
        holders = [holder for holder in self._holders(file)
                   if holder[0] != record.name]
        if not holders:
            raise SessionError(NO_SESSION)
        target, target_state = holders[0]
        self._store.set_handoff(record.name, target)
        self._relay.move_viewers(record.id, self._key(target))
        self._store.set_clients(record.name, 0)
        self._store.set_clients(target, self._relay.viewer_count(
            self._key(target)))
        return {"name": target, "state": target_state}

    # -- maintenance -------------------------------------------------------

    def purge(self):
        """Stop every idle session nobody is attached to."""
        purged = []
        for record in self._store.live():
            if record.state() != "idle":
                continue
            if self._relay.viewer_count(record.id) > 0:
                continue
            self._runner.stop(record.name)
            purged.append(record.name)
        return purged

    def acknowledge_reap(self, name):
        record = self._record(name)
        return {"name": record.name, "acknowledged": True}

    def reload(self, names=None):
        """Ask hosted sessions to reload their resources in place, once.

        One round, no memory: a session that is mid-turn or parked on a
        blocking daemon wait has its turn open, so pi would deny the
        reload - it is reported as skipped and nothing retries it here.
        Only sessions that already exist are touched; nothing is started,
        respawned or revived, so a broken update cannot become a spawn
        storm. The extension deletes its signal to accept; a session that
        has not acknowledged by then gets "/reload" typed into its PTY
        instead, but only if it is still the same live, idle run.
        """
        if self._reload is None:
            raise SessionError(NO_SESSION)
        with self._reload_lock:
            return self._round(names)

    def _round(self, names):
        targets, skipped = [], []
        live = {record.name: record for record in self._store.live()}
        for record in live.values():
            if names is not None and record.name not in names:
                continue
            if not _reloadable(record):
                skipped.append({"id": record.name, "reason": "not-reloadable"})
                continue
            if record.state() != "idle":
                skipped.append({"id": record.name, "reason": "not-idle"})
                continue
            targets.append((record.id, record.generation, record.name))
        if names is not None:
            # A requested name that is not live is answered, not dropped:
            # silence would read as a reload that happened.
            for name in names:
                if name not in live:
                    skipped.append({"id": name, "reason": "unknown"})
        token = self._reload.token()
        pending = []
        for key, generation, name in targets:
            if self._reload.signal(name, token):
                pending.append((key, generation, name))
            else:
                skipped.append({"id": name, "reason": "write-failed"})
        consumed = set(self._reload.await_consumed(
            [name for _key, _generation, name in pending]))
        reloaded = [name for _key, _generation, name in pending
                    if name in consumed]
        for key, generation, name in pending:
            if name in consumed:
                continue
            try:
                claimed = self._reload.claim(name, token)
            except OSError:
                skipped.append({"id": name, "reason": "write-failed"})
                continue
            if not claimed:
                # The extension consumed the request while this round was
                # deciding: the reload it queued is the acknowledgement.
                reloaded.append(name)
                continue
            record = self._store.get(name)
            if record is None or record.id != key \
                    or not record.live() \
                    or record.generation != generation \
                    or record.state() != "idle":
                skipped.append({"id": name, "reason": "not-idle"})
                continue
            try:
                self._runner.send(name, FALLBACK.encode("utf-8"))
            except (SessionError, OSError, TimeoutError):
                skipped.append({"id": name, "reason": "write-failed"})
            else:
                reloaded.append(name)
        deferred = any(entry["reason"] == "not-idle" for entry in skipped)
        return {"reloaded": reloaded, "skipped": skipped,
                "deferred": deferred}

    def begin_shutdown(self, mode):
        self._runner.begin_shutdown(mode)

    def stop_all(self):
        self._runner.shutdown_wait()

    # -- internals ---------------------------------------------------------

    def _record(self, name, live=False):
        canonical = self._store.resolve(name)
        record = self._store.get(canonical) if canonical else None
        if record is None or (live and not record.live()):
            raise SessionError(NO_SESSION)
        return record

    def _holders(self, file):
        """Live holders of a conversation, the busiest viewer count first."""
        holders = self._store.holders(file)
        return sorted(holders,
                      key=lambda holder: -self._relay.viewer_count(
                          self._key(holder[0])))

    def _key(self, name):
        record = self._store.get(name)
        return record.id if record is not None else None

    def _announced(self, name, handoff, taken, remaining):
        return {
            "name": name,
            "handoff": handoff,
            "tookOver": list(taken),
            "alsoLive": [{"name": other, "state": state}
                         for other, state in remaining if other != name],
        }
