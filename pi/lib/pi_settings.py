"""Package settings: one owner for reading a package's namespace out of
the pi settings file and resolving each configurable value.

pi keeps user configuration in `<agent-dir>/settings.json`; a pi package
adds its own top-level namespace object there (for pi-daemon: "piDaemon").
This module reads that section through the guarded JSON reader and
resolves a value from, in order: an explicit environment variable (kept
for tests and one-off runs), the settings section, then the caller's
built-in default. Callers keep their own injected parameters as the
highest-precedence source, so tests can still override a value directly.
"""

import os


class PackageSettings:
    """A read-only view of one top-level settings namespace."""

    def __init__(self, section, environ=None):
        self._section = section if isinstance(section, dict) else {}
        self._environ = os.environ if environ is None else environ

    @classmethod
    def from_file(cls, path, namespace, environ=None):
        # Imported lazily: pi_platform is loaded from the same lib dir
        # the daemon puts on sys.path, and keeping this module importable
        # on its own avoids a circular import at daemon load time.
        from pi_platform import AtomicStateFile
        top = AtomicStateFile(path).read_dict()
        return cls(top.get(namespace), environ)

    def resolve(self, env_name, key, default, kind="number"):
        """The effective value: env var, then settings key, then default.

        `kind` selects the value shape and parser: "number" (int/float,
        booleans rejected), "flag" (bool; env words on/yes/1 and
        off/no/0), "text" (non-empty string), "paths" (list of strings;
        an env value is split on the platform path separator). A value
        that fails to parse at one level falls through to the next, so a
        malformed env var never masks a valid setting or default.
        """
        for raw in (self._environ.get(env_name), self._section.get(key)):
            if raw is None or raw == "":
                continue
            parsed = self._parse(raw, kind)
            if parsed is not None:
                return parsed
        return default

    @staticmethod
    def _parse(raw, kind):
        if kind == "number":
            if isinstance(raw, bool):
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
        if kind == "flag":
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in ("1", "true", "yes", "on"):
                return True
            if text in ("0", "false", "no", "off"):
                return False
            return None
        if kind == "text":
            return raw if isinstance(raw, str) and raw else None
        if kind == "paths":
            if isinstance(raw, str):
                return [p for p in raw.split(os.pathsep) if p] or None
            if isinstance(raw, (list, tuple)):
                items = [p for p in raw if isinstance(p, str) and p]
                return items or None
            return None
        return None