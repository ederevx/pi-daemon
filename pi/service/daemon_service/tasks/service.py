"""The tasks domain's public surface: tickets, never internals."""

from .store import TicketStore


class TaskService:
    """Every task operation reaches the store and the runner through here."""

    def __init__(self, store, runner):
        self._store = store
        self._runner = runner

    def submit(self, session, cwd, command, timeout=0.0):
        return self._runner.submit(session, cwd, command, timeout)

    def wait(self, ticket_id, timeout=0.0):
        ticket = self._runner.wait(ticket_id, timeout)
        if ticket is None:
            raise LookupError(f"unknown ticket: {ticket_id}")
        return ticket

    def tickets(self, session=None):
        return self._store.snapshot(session)

    def output(self, ticket_id, offset=0):
        text, next_offset = self._store.output(ticket_id, offset)
        return {"ticket": self._store.snapshot_one(ticket_id),
                "output": text, "offset": next_offset}

    def cancel(self, ticket_id):
        return self._runner.cancel(ticket_id)

    def remove(self, ticket_id):
        return self._store.remove(ticket_id)

    def reset(self):
        for ticket_id in self._store.running_ids():
            try:
                self._runner.cancel(ticket_id)
            except LookupError:
                pass
        return self._store.reset_all()

    def begin_shutdown(self):
        self._runner.begin_shutdown()

    def stop(self):
        self._runner.stop()
        self._store.save()
