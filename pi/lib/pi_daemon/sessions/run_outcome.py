"""The one place a run's ending is named, and the order that decides it."""

import signal

# How a run ends, in the order the reasons outrank one another.
DAEMON_STOPPED = "daemon-stopped"
STOPPED = "stopped"
RELOAD_DEATH = "reload-death"
LOST_MID_RUN = "lost-mid-run"
LOST_AT_PROMPT = "lost-at-prompt"
DIED_MID_RUN = "died-mid-run"
EXITED_MID_RUN = "exited-mid-run"
CRASHED_AT_PROMPT = "crashed-at-prompt"
RUN_ABORTED = "run-aborted"
UNKNOWN_RUN = "unknown"
SETTLED = "settled"

CAUSES = ("daemon", "reload", "signal", "exit", "unknown", "abort", "lost")


class RunOutcome:
    """A seal is a pure projection of the record and how the process ended."""

    @classmethod
    def seal(cls, record, state, published, reason, returncode, now):
        """Return the one verdict a run's ending earns."""
        abnormal, signal_name = _abnormal(returncode)
        verdict, cause = cls._verdict(record, state, published, reason,
                                      abnormal, signal_name)
        return {
            "verdict": verdict,
            "cause": cause,
            "signal": signal_name,
            "exit": returncode,
            "state": state if published else None,
            "at": now,
        }

    @classmethod
    def _verdict(cls, record, state, published, reason, abnormal, signal_name):
        if reason in ("shutdown", "restart"):
            return DAEMON_STOPPED, "daemon"
        if reason in ("stop", "purge"):
            return STOPPED, "daemon"
        if reason == "reload":
            return RELOAD_DEATH, "reload"
        if reason == "lost":
            return (LOST_MID_RUN if state in ("busy", "waiting")
                    else LOST_AT_PROMPT), "lost"
        if state in ("busy", "waiting"):
            if abnormal:
                return DIED_MID_RUN, "signal" if signal_name else "exit"
            return EXITED_MID_RUN, "unknown"
        if abnormal:
            return CRASHED_AT_PROMPT, "signal" if signal_name else "exit"
        if record.get("aborted"):
            return RUN_ABORTED, "abort"
        if not published:
            return UNKNOWN_RUN, "unknown"
        return SETTLED, None


def _abnormal(returncode):
    """A non-zero or signalled exit, named when the signal is known."""
    if returncode is None:
        return True, None
    if returncode < 0:
        try:
            return True, signal.Signals(-returncode).name
        except ValueError:
            return True, None
    return returncode != 0, None
