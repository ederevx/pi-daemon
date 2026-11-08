"""Validated configuration snapshots; one immutable record per owner."""

import json
import math
import os
from dataclasses import dataclass

ENV_PREFIX = "PI_DAEMON_"

# Comms fields whose zero would stop mail rather than disable a feature;
# every other value keeps its own semantics, including zero.
RANGES = {
    "comms_message_ttl": 0.0,
    "comms_result_ttl": 0.0,
    "comms_max_ttl": 0.0,
    "comms_payload_bytes": 0,
    "comms_store_bytes": 0,
    "comms_recipient_cap": 0,
    "comms_messages_per_recipient": 0,
    "comms_page_messages": 0,
    "comms_outbound_queue": 0,
    "comms_request_id_limit": 0,
    "comms_kind_limit": 0,
    "comms_agent_id_limit": 0,
    "comms_frame_margin": 255,
}


@dataclass(frozen=True)
class ServiceConfig:
    """Every operational default the service reads, resolved once."""

    state_dir: str = ""
    runtime_dir: str = ""
    settings_path: str = ""
    frame_bytes: int = 65536
    read_chunk_bytes: int = 8192
    backlog: int = 16
    accept_poll: float = 0.25
    connect_timeout: float = 2.0
    probe_timeout: float = 1.0
    drain_grace: float = 2.0
    cancel_grace: float = 2.0
    stop_grace: float = 5.0
    max_running: int = 32
    max_wait: float = 600.0
    record_cap: int = 200
    output_cap: int = 1048576
    file_mode: int = 0o600
    write_retries: int = 5
    write_retry_delay: float = 0.05
    shell: str = ""
    default_cols: int = 120
    default_rows: int = 40
    master_chunk_bytes: int = 65536
    relay_buffer_bytes: int = 262144
    relay_tick: float = 0.05
    settle_grace: float = 1.0
    history_limit: int = 4
    comms_message_ttl: float = 120.0
    comms_result_ttl: float = 3600.0
    comms_max_ttl: float = 86400.0
    comms_dedupe_ttl: float = 86400.0
    comms_payload_bytes: int = 32768
    comms_store_bytes: int = 16777216
    comms_recipient_cap: int = 256
    comms_messages_per_recipient: int = 256
    comms_page_messages: int = 64
    comms_outbound_queue: int = 256
    comms_request_id_limit: int = 200
    comms_kind_limit: int = 64
    comms_agent_id_limit: int = 128
    comms_frame_margin: int = 1024
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
        config = cls(**values)
        _relationships(config)
        return config

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
        return _ranged(name, int(number))
    if isinstance(default, float):
        return _ranged(name, float(_finite(value, name)))
    return str(value)


def _ranged(name, value):
    """A field's own bound, so no owner has to defend itself."""
    bound = RANGES.get(name)
    if bound is not None and value <= bound:
        raise ValueError(f"{name}: must be greater than {bound}")
    return value


def _relationships(config):
    """Bounds that only hold between fields, checked once per snapshot."""
    if config.comms_message_ttl > config.comms_max_ttl:
        raise ValueError("comms_message_ttl: exceeds comms_max_ttl")
    if config.comms_result_ttl > config.comms_max_ttl:
        raise ValueError("comms_result_ttl: exceeds comms_max_ttl")
    if config.comms_payload_bytes >= config.frame_bytes:
        raise ValueError("comms_payload_bytes: must fit in a frame")
    if config.comms_frame_margin >= config.frame_bytes:
        raise ValueError("comms_frame_margin: must fit in a frame")
    if (config.comms_payload_bytes + config.comms_frame_margin
            > config.frame_bytes):
        raise ValueError("comms_payload_bytes: leaves no frame for the reply")
    if config.comms_frame_margin < config.comms_agent_id_limit + 256:
        raise ValueError("comms_frame_margin: leaves no room for a reply")


def _finite(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: a number is required") from None
    if not math.isfinite(number):
        raise ValueError(f"{name}: a finite number is required")
    return number
