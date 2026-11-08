"""The ticket wire commands: validate a request, apply it through the
tasks domain, answer in the wire shape.
"""

import base64
import os

from .. import wire


class TaskControl:
    """The tasks domain's wire handlers."""

    def __init__(self, tasks):
        self.tasks = tasks

    def submit(self, request):
        session = request.get("session")
        cwd = request.get("cwd")
        command = request.get("command")
        if not (isinstance(session, str) and session
                and isinstance(cwd, str) and os.path.isabs(cwd)
                and os.path.isdir(cwd)
                and isinstance(command, str) and command):
            return wire.fail("bad-request")
        env = request.get("env")
        if env is not None and not (isinstance(env, dict) and all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in env.items())):
            return wire.fail("bad-request")
        timeout = self._timeout(request.get("timeout"))
        if timeout is False:
            return wire.fail("bad-request")
        existing = self.tasks.find_running(session, cwd, command)
        if existing is not None:
            return wire.ok(id=existing["id"], ticket=existing, reused=True)
        ticket = self.tasks.submit(session, cwd, command, env, timeout)
        if ticket is None:
            return wire.fail("too-busy")
        if ticket.status == "failed" and ticket.error:
            return wire.fail("spawn-failed")
        return wire.ok(id=ticket.id, ticket=ticket.view())

    @staticmethod
    def _timeout(raw):
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return False
        return value if value > 0 else False

    def wait(self, request):
        tid = request.get("id")
        timeout = request.get("timeout", 0)
        if not (isinstance(tid, str) and tid):
            return wire.fail("bad-request")
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            return wire.fail("bad-request")
        if timeout < 0:
            return wire.fail("bad-request")
        ticket = self.tasks.wait(tid, timeout)
        if ticket is None:
            return wire.fail("no-ticket")
        return wire.ok(ticket=ticket.view())

    def output(self, request):
        tid = request.get("id")
        if not (isinstance(tid, str) and tid):
            return wire.fail("bad-request")
        offset = request.get("offset")
        if offset is None:
            ticket = self.tasks.wait(tid, 0)
            if ticket is None:
                return wire.fail("no-ticket")
            return wire.ok(id=tid, status=ticket.status, exit=ticket.exit,
                           output=self.tasks.text_output(tid))
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            return wire.fail("bad-request")
        if offset < 0:
            return wire.fail("bad-request")
        chunk = self.tasks.output(tid, offset)
        if chunk is None:
            return wire.fail("no-ticket")
        return wire.ok(id=tid, offset=chunk["offset"], next=chunk["next"],
                       size=chunk["size"],
                       data=base64.b64encode(chunk["data"]).decode())

    def list(self, request):
        session = request.get("session")
        if session is not None and not isinstance(session, str):
            return wire.fail("bad-request")
        return wire.ok(tickets=self.tasks.list(session))

    def cancel(self, request):
        tid = request.get("id")
        if not (isinstance(tid, str) and tid):
            return wire.fail("bad-request")
        ticket = self.tasks.cancel(tid)
        if ticket is None:
            return wire.fail("no-ticket")
        return wire.ok(ticket=ticket.view())

    def remove(self, request):
        tid = request.get("id")
        if not (isinstance(tid, str) and tid):
            return wire.fail("bad-request")
        try:
            ticket = self.tasks.remove(tid)
        except ValueError:
            return wire.fail("running")
        if ticket is None:
            return wire.fail("no-ticket")
        return wire.ok(removed=tid)

    def reset(self, _request):
        return wire.ok(removed=self.tasks.reset())
