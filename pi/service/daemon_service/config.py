"""The service's configuration: every tunable, resolved once.

Resolution order, identical for every value: an explicit environment
variable, then the `piDaemon` namespace of the pi settings file, then a
legacy environment name (one release of migration), then the built-in
default. A value that fails to parse falls through to the next source,
so a typo cannot mask a valid setting. Nothing else in the service
reads the settings file.
"""

import json
import os

HOUR = 3600.0
MEGABYTE = 1 << 20

# The one settings namespace the service owns.
SECTION = "piDaemon"


class ServiceConfig:
    """The resolved tunables of one service instance."""

    def __init__(self, state, environ=None):
        self.state = state
        self.environ = os.environ if environ is None else environ
        self.section = self._section()
        self.ticket_ttl = self._hours(
            "PI_DAEMON_TICKET_TTL_HOURS", "ticketTtlHours", 72.0,
            "PI_PTYD_TICKET_TTL_HOURS")
        self.ticket_gc_seconds = self._number(
            "PI_DAEMON_TICKET_GC", "ticketGcSeconds", 60.0,
            "PI_PTYD_TICKET_GC")
        self.max_running_tickets = int(self._number(
            "PI_DAEMON_MAX_TICKETS", "maxRunningTickets", 32.0))
        self.max_output_bytes = int(self._number(
            "PI_DAEMON_TICKET_OUTPUT_MB", "ticketOutputMb", 4.0) * MEGABYTE)
        self.request_timeout = self._number(
            "PI_DAEMON_REQUEST_TIMEOUT", "requestTimeoutSeconds", 30.0)
        self.shell = self.environ.get("PI_SHELL") or None

    def _section(self):
        path = os.path.join(self.state.agent_home(), "settings.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                top = json.load(f)
        except (OSError, ValueError):
            return {}
        section = top.get(SECTION) if isinstance(top, dict) else None
        return section if isinstance(section, dict) else {}

    def _candidates(self, env_name, key, legacy_env):
        """Every configured source, in resolution order."""
        values = [self.environ.get(env_name), self.section.get(key)]
        if legacy_env:
            values.append(self.environ.get(legacy_env))
        return [raw for raw in values if raw is not None and raw != ""]

    def _number(self, env_name, key, default, legacy_env=None):
        for raw in self._candidates(env_name, key, legacy_env):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue  # a malformed source never masks the next one
            if value >= 0 and value == value:
                return value
        return default

    def _hours(self, env_name, key, default, legacy_env=None):
        return self._number(env_name, key, default, legacy_env) * HOUR
