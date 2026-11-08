"""The sessions domain's control operations, validation and error projection."""

import os
import threading

from ..wire import NO_DIR, error_reply
from ..transport.control_action import ControlAction
from .error import SessionError
from .service import STATES

NAME_LIMIT = 200
# The legacy daemon's handover wire limit, measured in characters.
MESSAGE_LIMIT = 8192
DIMENSION_LIMIT = 65535


class SessionOperations:
    """It validates the wire and projects the domain; it owns no process."""

    def __init__(self, service, request_stop, config):
        self._service = service
        self._request_stop = request_stop
        self._config = config

    def handlers(self):
        return {
            "list": self.list,
            "which": self.which,
            "whichall": self.whichall,
            "start": self.start,
            "attach": self.attach,
            "carry": self.carry,
            "detach": self.detach,
            "stop": self.stop,
            "input": self.input,
            "resize": self.resize,
            "state": self.state,
            "run-state": self.run_state,
            "announce": self.announce,
            "resume": self.resume,
            "handoff-target": self.handoff_target,
            "handover": self.handover,
            "gc-reap-ack": self.gc_reap_ack,
            "extensions-reload": self.extensions_reload,
            "daemon-purge": self.daemon_purge,
            "daemon-restart": self.daemon_restart,
            "shutdown": self.shutdown,
        }

    # -- reads -------------------------------------------------------------

    def list(self, request):
        include = request.get("all", False)
        if not isinstance(include, bool):
            raise ValueError("all must be a boolean")
        reply = {"ok": True, "sessions": self._service.sessions(include)}
        if not include:
            reply["stopped"] = self._service.stopped_count()
        return reply

    def which(self, request):
        return {"ok": True, **self._service.which(_file(request))}

    def whichall(self, request):
        return {"ok": True, "names": self._service.whichall(_file(request))}

    def run_state(self, request):
        return self._project(self._service.run_state, _name(request))

    def handoff_target(self, request):
        name = request.get("name")
        return {"ok": True,
                "target": self._service.handoff_target(name)
                if isinstance(name, str) and name.strip() else None}

    # -- hosting -----------------------------------------------------------

    def start(self, request):
        try:
            name, dir_ = _name(request), _directory(request)
            argv, cols, rows = _argv(request), self._cols(request), \
                self._rows(request)
            environment = _environment(request, "env")
            once = _environment(request, "env_once")
        except SessionError as failure:
            return error_reply(failure.code)
        return self._project(self._service.start, name, dir_, argv, cols, rows,
                             environment, once)

    def attach(self, request, connection):
        name = _name(request)
        cols, rows = self._cols(request), self._rows(request)
        try:
            plan = self._service.attach_plan(name, cols, rows)
        except SessionError as failure:
            return _attach_failure(self._service, name, failure)
        recorded = plan.get("file")
        if plan["handoff"] is not None:
            reply = {"ok": True, "name": plan["name"],
                     "handoff": plan["handoff"], "clients": 0,
                     "state": plan["state"]}
            if recorded:
                reply["file"] = recorded
            return reply

        def upgrade(socket_, initial):
            self._service.attach_now(plan, socket_, initial)

        reply = {"ok": True, "name": plan["name"],
                 "state": plan["state"], "clients": plan["clients"]}
        if recorded:
            reply["file"] = recorded
        return ControlAction(reply=reply, upgrade=upgrade)

    def carry(self, request):
        to = request.get("to")
        if to is not None and not isinstance(to, str):
            raise ValueError("to must be a session name")
        argv = request.get("argv")
        if argv is not None:
            # A carry argv is passed to pi as it stands: unlike a start,
            # the protocol has always allowed an empty entry here.
            if not isinstance(argv, list) or not argv:
                raise ValueError("an argv list is required")
            if not all(isinstance(argument, str) for argument in argv):
                raise ValueError("every argv entry must be a string")
            argv = list(argv)
        return self._project(self._service.carry, _name(request), to, argv)

    def handover(self, request, connection):
        """Host a conversation a pi hands over as it exits.

        A holding client simply keeps its connection open until that pi
        exits; the close - never a pid probe - is the signal to host, so
        the connection is parked by registering its close, not by reading
        it: the endpoint keeps answering it as usual.
        """
        file_ = _given(request, "file")
        dir_ = _given(request, "dir")
        after_pid = request.get("afterPid")
        if after_pid is not None and not isinstance(after_pid, int):
            raise ValueError("afterPid must be an integer")
        hold = bool(request.get("hold"))
        message = request.get("message")
        if message is not None and not (isinstance(message, str)
                                        and 0 < len(message) <= MESSAGE_LIMIT):
            raise ValueError("a message of 1 to %d characters is allowed"
                             % MESSAGE_LIMIT)
        if not os.path.isdir(dir_):
            return error_reply(NO_DIR)
        plan = self._service.handover(dir_, file_)
        reply = {"ok": True, "verdict": plan["verdict"]}
        if plan["hosted"]:
            return reply
        target = plan["target"]

        def host():
            # Off the connection's thread: the settle and the spawn must
            # not hold the worker that is closing.
            threading.Thread(
                target=self._service.host_handover,
                args=(target, dir_, file_, message),
                name="handover-host", daemon=True).start()

        if connection is not None and (hold or after_pid is not None):
            return ControlAction(reply=reply, on_close=host)
        host()
        return reply

    def detach(self, request):
        return self._project(self._service.detach, _name(request))

    def stop(self, request):
        return self._project(self._service.stop, _name(request))

    def input(self, request):
        return self._project(self._service.input, _name(request),
                             _text(request))

    def resize(self, request):
        return self._project(self._service.resize, _name(request),
                             self._cols(request), self._rows(request))

    # -- run state ---------------------------------------------------------

    def state(self, request):
        value = request.get("state")
        if value not in STATES:
            raise ValueError("a run state of busy, idle or waiting is required")
        seq = _optional_int(request.get("seq"), "seq")
        pid = _optional_int(request.get("pid"), "pid")
        aborted = request.get("aborted")
        if aborted is not None and not isinstance(aborted, bool):
            raise ValueError("aborted must be a boolean")
        return self._project(self._service.state, _name(request), value,
                             seq, pid, aborted)

    # -- conversations -----------------------------------------------------

    def announce(self, request):
        takeover = request.get("takeover", False)
        if not isinstance(takeover, bool):
            raise ValueError("takeover must be a boolean")
        return self._project(self._service.announce, _name(request),
                             _file(request), takeover)

    def resume(self, request):
        return self._project(self._service.resume, _name(request),
                             _file(request))

    # -- maintenance -------------------------------------------------------

    def gc_reap_ack(self, request):
        return self._project(self._service.acknowledge_reap, _name(request))

    def daemon_purge(self, request):
        return {"ok": True, "purged": self._service.purge()}

    def extensions_reload(self, request):
        """One manual reload round: every hosted session that can take it.

        `sessions` narrows the round; `force` is accepted because the
        client has always sent it and a manual round reloads regardless of
        whether anything on disk appears to have changed.
        """
        force = request.get("force", False)
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        only = request.get("sessions")
        if only is not None:
            if not isinstance(only, list) \
                    or not all(isinstance(name, str) for name in only):
                raise ValueError("sessions must be a list of names")
            only = list(only)
        reply = self._service.reload(only)
        return {"ok": True, "reloaded": reply["reloaded"],
                "skipped": reply["skipped"],
                "deferred": reply["deferred"]}

    def daemon_restart(self, request):
        # Deferred: a restart hands the endpoint to a successor; without
        # one the reply must not promise a daemon that never comes back.
        return error_reply("bad-request")

    def shutdown(self, request):
        mode = _mode(request)
        return ControlAction(reply={"ok": True},
                             after_send=lambda: self._request_stop(mode))

    # -- internals ---------------------------------------------------------

    def _cols(self, request):
        return _dimension(request.get("cols"), self._config.default_cols)

    def _rows(self, request):
        return _dimension(request.get("rows"), self._config.default_rows)

    def _project(self, operation, *arguments):
        try:
            return {"ok": True, **operation(*arguments)}
        except SessionError as failure:
            return error_reply(failure.code)


def _attach_failure(service, name, failure):
    reply = error_reply(failure.code)
    recorded = service.recorded(name)
    if recorded is not None:
        reply["recorded"] = recorded["name"]
        if recorded["file"]:
            reply["file"] = recorded["file"]
        if recorded["dir"]:
            reply["dir"] = recorded["dir"]
    return reply


def _name(request):
    name = request.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a session name is required")
    name = name.strip()
    if len(name) > NAME_LIMIT or name in (".", "..") \
            or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("an unusable session name")
    return name


def _directory(request):
    value = request.get("dir")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a directory is required")
    path = os.path.abspath(os.path.expanduser(value))
    if not os.path.isdir(path):
        raise SessionError(NO_DIR)
    return path


def _argv(request):
    argv = request.get("argv")
    if not isinstance(argv, list) or not argv:
        raise ValueError("an argv list is required")
    for argument in argv:
        if not isinstance(argument, str) or not argument or "\x00" in argument:
            raise ValueError("every argv entry must be a non-empty string")
    return list(argv)


def _dimension(value, default):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("a terminal dimension must be an integer")
    if not 1 <= value <= DIMENSION_LIMIT:
        raise ValueError("a terminal dimension is out of range")
    return value


def _given(request, key):
    """A path exactly as the client spelled it: the target name and the
    conversation digest are derived from it, so normalizing it here would
    change the names a client is promised."""
    value = request.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("a %s is required" % key)
    return value


def _optional_int(value, label):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _environment(request, key):
    value = request.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    environment = {}
    for name, entry in value.items():
        if not isinstance(name, str) or not name or "=" in name:
            raise ValueError("an environment name is unusable")
        if not isinstance(entry, str) or "\x00" in entry:
            raise ValueError("an environment value must be a string")
        environment[name] = entry
    return environment


def _text(request):
    value = request.get("text", request.get("data"))
    if not isinstance(value, str):
        raise ValueError("input text is required")
    return value


def _file(request):
    value = request.get("file")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a conversation file is required")
    return value


def _mode(request):
    value = request.get("mode", "shutdown")
    if value not in ("shutdown", "update", "reboot"):
        raise ValueError("an unknown stop mode")
    return value
