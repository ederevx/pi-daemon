"""Command-name dispatch for the control listener."""

import inspect


class ControlRouter:
    """The router owns the command table and nothing else."""

    def __init__(self, handlers):
        self._handlers = dict(handlers)
        self._contextual = {
            name for name, handler in self._handlers.items()
            if len(inspect.signature(handler).parameters) > 1
        }

    def names(self):
        return sorted(self._handlers)

    def contextual(self):
        return sorted(self._contextual)

    def dispatch(self, request, connection=None):
        operation = request.get("cmd")
        if not isinstance(operation, str) or not operation:
            raise ValueError("an operation name is required")
        handler = self._handlers.get(operation)
        if handler is None:
            raise LookupError(f"unknown command: {operation}")
        if operation in self._contextual:
            return handler(request, connection)
        return handler(request)
