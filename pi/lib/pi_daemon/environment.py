"""The installed defaults an executable entry point resolves over."""

import json
import os


class EntryEnvironment:
    """Resolve explicit environment values over installation defaults.

    Only an installed generation carries `defaults.json`, written beside
    its `lib/` at install time, so a checkout keeps failing when a root is
    missing: nothing here guesses a machine's paths. The process
    environment always wins, which is what lets an operator point one run
    at other roots without reinstalling."""

    ROOTS = ("PI_DAEMON_STATE_DIR", "PI_DAEMON_RUNTIME_DIR")
    KEYS = ROOTS + ("PI_CODING_AGENT_DIR",)

    def __init__(self, environ=None, defaults_path=None):
        self._environ = dict(os.environ if environ is None else environ)
        if defaults_path is None:
            # lib/pi_daemon/environment.py -> <generation>/defaults.json
            root = os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.realpath(__file__))))
            defaults_path = os.path.join(root, "defaults.json")
        self._defaults_path = defaults_path

    def resolve(self):
        defaults = self._read_defaults()
        environment = dict(self._environ)
        for name in self.KEYS:
            value = defaults.get(name)
            if not environment.get(name) and isinstance(value, str):
                environment[name] = value
        missing = [name for name in self.ROOTS
                   if not environment.get(name)]
        if missing:
            raise ValueError(
                "explicit roots required: " + ", ".join(missing))
        return environment

    def _read_defaults(self):
        try:
            with open(self._defaults_path, encoding="utf-8") as handle:
                document = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise ValueError("installed defaults are unreadable") from None
        if not isinstance(document, dict):
            raise ValueError("installed defaults are unreadable")
        return document
