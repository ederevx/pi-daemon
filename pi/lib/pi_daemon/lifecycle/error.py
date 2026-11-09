"""The lifecycle domain's refusal: another instance owns the root."""


class InstanceHeldError(RuntimeError):
    """The runtime root's instance lock is held by another process.

    A subclass of RuntimeError, so the migration's existing handler keeps
    reading it as a held root."""
