"""The narrow surface through which another domain launches a session."""

from .error import SessionError
from .operations import (_argv, _dimension, _directory, _environment, _name)

INVALID_SESSION = "invalid-session"
SPAWN_FAILED = "spawn-failed"
NAME_TAKEN = "name-taken"


class SessionLauncher:
    """It validates a launch and reports every outcome as plain data."""

    def __init__(self, service, config, command):
        self._service = service
        self._config = config
        self._command = command

    def check(self, spec):
        """Normalise a launch before anything is recorded or started."""
        request = {"dir": spec.get("dir"), "argv": spec.get("argv"),
                   "task": spec.get("task"),
                   "env": spec.get("env"), "env_once": spec.get("envOnce")}
        try:
            request["name"] = _name({"name": spec.get("name")})
            request["dir"] = _directory(request)
            request["argv"] = self._launch_argv(request)
            request["cols"] = _dimension(spec.get("cols"),
                                         self._config.default_cols)
            request["rows"] = _dimension(spec.get("rows"),
                                         self._config.default_rows)
            request["env"] = _environment(request, "env")
            request["env_once"] = _environment(request, "env_once")
        except SessionError as failure:
            return {"ok": False, "reason": failure.code}
        except ValueError:
            return {"ok": False, "reason": INVALID_SESSION}
        return {"ok": True, "spec": request}

    def launch(self, spec):
        """Start exactly one session and never adopt one this call did not."""
        try:
            result = self._service.start(
                spec["name"], spec["dir"], spec["argv"], spec["cols"],
                spec["rows"], spec["env"], spec["env_once"])
        except SessionError as failure:
            return {"ok": False, "reason": failure.code}
        except (OSError, ValueError, LookupError):
            return {"ok": False, "reason": SPAWN_FAILED}
        if not result.get("created"):
            return {"ok": False, "reason": NAME_TAKEN}
        return {"ok": True, "name": result["name"],
                "pid": result["session"].get("pid")}

    def _launch_argv(self, request):
        """A launch runs an explicit argv, or a fresh `pi` on one task.

        The task goes in as pi's positional prompt after `--`, so the
        child opens its own conversation and reads the task as text; its
        provider, model and thinking level are its own configuration, not
        this caller's.
        """
        task = request.get("task")
        if task is None:
            return _argv(request)
        if request.get("argv") is not None:
            raise ValueError("a launch takes an argv or a task, never both")
        if not isinstance(task, str) or not task.strip() or "\x00" in task:
            raise ValueError("a task must be a non-empty string")
        return self._command.task(task)
