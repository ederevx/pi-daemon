"""One translation: hold the lock, decide, write through the owners."""

import time

from ..lifecycle.instance_coordinator import InstanceCoordinator
from ..sessions.store import SessionStore
from ..tasks.store import TicketStore
from .error import SOURCES_MOVED, MigrationError
from .marker import MigrationMarker
from .plan import TRANSLATE, MigrationPlan


class Migration:
    """Preview never writes; apply holds the instance lock throughout."""

    def __init__(self, layout, config, legacy, clock=time.time):
        self._layout = layout
        self._config = config
        self._legacy = legacy
        self._clock = clock

    def preview(self, force):
        locked = InstanceCoordinator(self._layout.instance_lock_path()).held()
        return MigrationPlan(self._layout, self._config, self._legacy,
                             force, locked, self._clock)

    def apply(self, force):
        coordinator = InstanceCoordinator(self._layout.instance_lock_path())
        try:
            coordinator.acquire()
        except RuntimeError:
            return MigrationPlan(self._layout, self._config, self._legacy,
                                 force, True, self._clock)
        try:
            plan = MigrationPlan(self._layout, self._config, self._legacy,
                                 force, False, self._clock)
            if plan.action() == TRANSLATE:
                self._write(plan)
            return plan
        finally:
            coordinator.release()

    def _write(self, plan):
        tickets = TicketStore(self._layout.tickets_path(),
                              self._layout.logs_dir(), self._config)
        tickets.replace_all(plan.tickets(), plan.next_ticket())
        for ticket_id, relative in plan.logs():
            data = self._legacy.read_bytes(relative)
            if data:
                tickets.append_output(ticket_id, data)
        sessions = SessionStore(self._layout.sessions_path(),
                                self._layout.run_state_dir(), self._config)
        sessions.replace_all(plan.sessions(), plan.run_states())
        sources = plan.sources()
        moved = self._legacy.verify(sources)
        if moved:
            # The marker would name digests the stores no longer hold.
            raise MigrationError(SOURCES_MOVED,
                                 "the legacy state moved during "
                                 "translation: " + ", ".join(moved))
        MigrationMarker(self._layout.migration_path(),
                        self._config).record(self._legacy.root(),
                                             sources,
                                             plan.forced(), self._clock())
