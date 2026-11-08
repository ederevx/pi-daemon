"""The ticket wire: published command names, validated requests, results."""

import base64
import math
import os

from ..wire import BAD_REQUEST, NO_TICKET, error_reply
from .error import TaskError


class TaskOperations:
    """Request validation and the exact result shapes of the contract."""

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
        try:
            session = _text(request, "session")
            cwd = _text(request, "cwd")
            command = _text(request, "command")
            if not os.path.isabs(cwd) or not os.path.isdir(cwd):
                raise TaskError(BAD_REQUEST)
            environment = _environment(request)
            timeout = _timeout(request)
        except TaskError as exc:
            return error_reply(exc.code)
        try:
            ticket, reused = self._service.submit(
                session, cwd, command, timeout, environment)
        except TaskError as exc:
            return error_reply(exc.code)
        result = {"ok": True, "id": ticket["id"], "ticket": ticket}
        if reused:
            result["reused"] = True
        return result

    def _wait(self, request):
        try:
            ticket_id = _text(request, "id")
            timeout = _wait_seconds(request)
        except TaskError as exc:
            return error_reply(exc.code)
        try:
            return {"ok": True, "ticket": self._service.wait(ticket_id,
                                                             timeout)}
        except LookupError:
            return error_reply(NO_TICKET)

    def _output(self, request):
        try:
            ticket_id = _text(request, "id")
            paged = request.get("offset") is not None
            offset = _whole(request["offset"]) if paged else 0
        except TaskError as exc:
            return error_reply(exc.code)
        if paged:
            try:
                result = self._service.output(ticket_id, offset)
            except LookupError:
                return error_reply(NO_TICKET)
            return {"ok": True, "offset": result["offset"],
                    "next": result["next"], "size": result["size"],
                    "data": base64.b64encode(result["data"]).decode("ascii")}
        try:
            result = self._service.text(ticket_id)
        except LookupError:
            return error_reply(NO_TICKET)
        return {"ok": True, "id": result["id"], "status": result["status"],
                "exit": result["exit"], "output": result["output"],
                "usage": result["usage"]}

    def _list(self, request):
        try:
            session = _optional_text(request.get("session"))
            kind = _optional_text(request.get("kind"))
            if kind is not None and kind != "shell":
                raise TaskError(BAD_REQUEST)
        except TaskError as exc:
            return error_reply(exc.code)
        return {"ok": True, "tickets": self._service.tickets(session)}

    def _cancel(self, request):
        try:
            ticket_id = _text(request, "id")
        except TaskError as exc:
            return error_reply(exc.code)
        try:
            return {"ok": True, "ticket": self._service.cancel(ticket_id)}
        except LookupError:
            return error_reply(NO_TICKET)

    def _remove(self, request):
        try:
            ticket_id = _text(request, "id")
        except TaskError as exc:
            return error_reply(exc.code)
        try:
            self._service.remove(ticket_id)
        except LookupError:
            return error_reply(NO_TICKET)
        except TaskError as exc:
            return error_reply(exc.code)
        return {"ok": True, "removed": ticket_id}

    def _reset(self, request):
        result = self._service.reset()
        return {"ok": True, "cancelled": result["cancelled"],
                "removed": result["removed"]}


def _text(request, name):
    value = request.get(name)
    if not isinstance(value, str) or not value:
        raise TaskError(BAD_REQUEST)
    return value


def _optional_text(value):
    """The published filter check is a type check, never an emptiness one."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TaskError(BAD_REQUEST)
    return value


def _environment(request):
    supplied = request.get("env")
    if supplied is None:
        return None
    if not isinstance(supplied, dict):
        raise TaskError(BAD_REQUEST)
    for key, value in supplied.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TaskError(BAD_REQUEST)
    return dict(supplied)


def _timeout(request):
    """A submission timeout is absent or a positive number."""
    if request.get("timeout") is None:
        return 0.0
    number = _number(request["timeout"])
    if number <= 0:
        raise TaskError(BAD_REQUEST)
    return number


def _wait_seconds(request):
    """A wait timeout must be present and numeric when the key is sent."""
    if "timeout" not in request:
        return 0.0
    number = _number(request["timeout"])
    if number < 0:
        raise TaskError(BAD_REQUEST)
    return number


def _number(value):
    """The wire accepts numbers and their string forms, coercing once."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TaskError(BAD_REQUEST)
    try:
        number = float(value)
    except ValueError:
        raise TaskError(BAD_REQUEST) from None
    if not math.isfinite(number):
        raise TaskError(BAD_REQUEST)
    return number


def _whole(value):
    """An offset is parsed the way the published server parses it."""
    if isinstance(value, bool):
        raise TaskError(BAD_REQUEST)
    try:
        whole = int(value)
    except (TypeError, ValueError):
        raise TaskError(BAD_REQUEST) from None
    if whole < 0:
        raise TaskError(BAD_REQUEST)
    return whole
