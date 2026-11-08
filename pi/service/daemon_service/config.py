"""Validated configuration snapshots; one immutable record per owner."""

import json
import math
import os
from dataclasses import dataclass

ENV_PREFIX = "PI_DAEMON_"


@dataclass(frozen=True)
class ServiceConfig:
    """Every operational default the service reads, resolved once."""

    state_dir: str = ""
    runtime_dir: str = ""
    settings_path: str = ""
    frame_bytes: int = 65536
    read_chunk_bytes: int = 8192
    backlog: int = 16
    connect_timeout: float = 2.0
    probe_timeout: float = 1.0
    drain_grace: float = 2.0
    cancel_grace: float = 2.0
    stop_grace: float = 5.0
    record_cap: int = 200
    output_cap: int = 1048576
    file_mode: int = 0o600
    write_retries: int = 5
    write_retry_delay: float = 0.05
    shell: str = ""
    revision: int = 0

    @classmethod
    def load(cls, settings_path=None, environ=None, overrides=None):
        environment = dict(os.environ if environ is None else environ)
        settings = _read_settings(settings_path)
        values = {}
        for name, default in cls().items():
            for supplied in (environment.get(ENV_PREFIX + name.upper()),
                             settings.get(name)):
                if supplied is None:
                    continue
                try:
                    values[name] = _coerce(name, supplied, default)
                except ValueError:
                    continue
                break
        if settings_path is not None:
            values["settings_path"] = str(settings_path)
        for name, value in (overrides or {}).items():
            values[name] = _coerce(name, value, getattr(cls(), name))
        return cls(**values)

    def items(self):
        for name in self.__dataclass_fields__:
            yield name, getattr(self, name)

    def with_revision(self, revision):
        return ServiceConfig(revision=revision, **{
            name: value for name, value in self.items() if name != "revision"
        })


def _read_settings(path):
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    section = loaded.get("daemon") if isinstance(loaded, dict) else None
    if isinstance(section, dict):
        return section
    return loaded if isinstance(loaded, dict) else {}


def _coerce(name, value, default):
    if isinstance(value, bool):
        raise ValueError(f"{name}: a boolean is not a value")
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, int):
        number = _finite(value, name)
        if number != int(number):
            raise ValueError(f"{name}: an integer is required")
        return int(number)
    if isinstance(default, float):
        return float(_finite(value, name))
    return str(value)


def _finite(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: a number is required") from None
    if not math.isfinite(number):
        raise ValueError(f"{name}: a finite number is required")
    return number
