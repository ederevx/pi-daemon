"""The argv that launches pi itself, resolved the same way everywhere."""

import pi_platform


class PiCommand:
    """A bare `pi` is a shell shim, so every launch goes through one seam."""

    def __init__(self, environment=None):
        self._process = pi_platform.ProcessControl(environ=dict(environment or {}))

    def fresh(self, argv=None):
        """A new conversation: an explicit argv, or a plain `pi`."""
        if not argv:
            return self._process.pi_argv()
        if argv[0] == "pi":
            return self._process.pi_argv(argv[1:])
        return list(argv)

    def resume(self, file, message=None):
        """The argv pair for hosting a conversation file, and its durable
        form: the message is a legacy compatibility hook and is never
        recorded, so a revive starts idle at the prompt."""
        arguments = ["--session", file]
        if message:
            arguments = arguments + [message]
        return self._process.pi_argv(arguments), \
            self._process.pi_argv(["--session", file])
