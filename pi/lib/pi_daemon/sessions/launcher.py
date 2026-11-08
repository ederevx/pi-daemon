"""The narrow surface through which another domain launches a session."""

from .error import SessionError
from .operations import (_argv, _dimension, _directory, _environment, _name)

INVALID_SESSION = "invalid-session"
SPAWN_FAILED = "spawn-failed"
NAME_TAKEN = "name-taken"


class SessionLauncher:
    """It validates a launch and reports every outcome as plain data."""

    def __init__(self, service, config):
        self._service = service
        self._config = config

    def check(self, spec):
        """Normalise a launch before anything is recorded or started."""
        request = {"dir": spec.get("dir"), "argv": spec.get("argv"),
                   "env": spec.get("env"), "env_once": spec.get("envOnce")}
        try:
            request["name"] = _name({"name": spec.get("name")})
            request["dir"] = _directory(request)
            request["argv"] = _argv(request)
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
