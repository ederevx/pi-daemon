"""Command-name dispatch for the control listener."""


class ControlRouter:
    """The router owns the command table and nothing else."""

    def __init__(self, handlers):
        self._handlers = dict(handlers)

    def names(self):
        return sorted(self._handlers)

    def dispatch(self, request):
        operation = request.get("cmd")
        if not isinstance(operation, str) or not operation:
            raise ValueError("an operation name is required")
        handler = self._handlers.get(operation)
        if handler is None:
            raise LookupError(f"unknown command: {operation}")
        result = handler(request)
        return result if isinstance(result, dict) else {"ok": True,
                                                        "result": result}
