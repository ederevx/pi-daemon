"""The ticket wire: existing command names, validated requests."""


class TaskOperations:
    """Request validation and response shapes for the control contract."""

    def __init__(self, service):
        self._service = service

    def handlers(self):
        return {
            "ticket-submit": self._submit,
            "ticket-wait": self._wait,
            "ticket-output": self._output,
            "ticket-list": self._list,
            "ticket-cancel": self._cancel,
            "ticket-remove": self._remove,
            "tickets-reset": self._reset,
        }

    def _submit(self, request):
        command = str(request.get("command") or "").strip()
        if not command:
            raise ValueError("a command is required")
        session = str(request.get("session") or "").strip()
        if not session:
            raise ValueError("a session is required")
        cwd = str(request.get("cwd") or "").strip()
        if not cwd:
            raise ValueError("a working directory is required")
        ticket = self._service.submit(session, cwd, command,
                                      _seconds(request, "timeout"))
        return {"ok": True, "ticket": ticket}

    def _wait(self, request):
        ticket = self._service.wait(_ticket(request), _seconds(request, "timeout"))
        return {"ok": True, "ticket": ticket}

    def _output(self, request):
        offset = _whole(request, "offset")
        result = self._service.output(_ticket(request), offset)
        return {"ok": True, "ticket": result["ticket"],
                "output": result["output"], "offset": result["offset"]}

    def _list(self, request):
        session = request.get("session")
        return {"ok": True,
                "tickets": self._service.tickets(str(session) if session else None)}

    def _cancel(self, request):
        return {"ok": True, "ticket": self._service.cancel(_ticket(request))}

    def _remove(self, request):
        self._service.remove(_ticket(request))
        return {"ok": True}

    def _reset(self, request):
        return {"ok": True, "removed": self._service.reset()}


def _ticket(request):
    ticket_id = str(request.get("ticket_id") or request.get("id") or "").strip()
    if not ticket_id:
        raise ValueError("a ticket id is required")
    return ticket_id


def _seconds(request, name):
    value = request.get(name)
    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise ValueError(f"{name}: a number is required")
    number = float(value)
    if number < 0 or number != number:
        raise ValueError(f"{name}: a non-negative number is required")
    return number


def _whole(request, name):
    value = _seconds(request, name)
    if value != int(value):
        raise ValueError(f"{name}: a whole number is required")
    return int(value)
