"""The `migrate` verb: argparse, the plan as JSON, the exit code."""

import argparse
import json
import sys

from ..config import ServiceConfig
from ..runtime_layout import RuntimeLayout
from .error import MigrationError
from .legacy_root import LegacyRoot
from .migration import Migration


class MigrateCommand:
    """One verb of the entry, composed from the owners that do the work."""

    def __init__(self, layout, config, legacy):
        self._layout = layout
        self._config = config
        self._legacy = legacy

    @classmethod
    def from_environment(cls, environ):
        layout = RuntimeLayout(environ.get("PI_DAEMON_STATE_DIR"),
                               environ.get("PI_DAEMON_RUNTIME_DIR"))
        config = ServiceConfig.load(settings_path=layout.settings_path(),
                                    environ=environ)
        return cls(layout, config, LegacyRoot.from_environment(environ))

    def run(self, argv, stdout=None, stderr=None):
        stdout = sys.stdout if stdout is None else stdout
        stderr = sys.stderr if stderr is None else stderr
        parser = argparse.ArgumentParser(prog="pi-daemon migrate")
        parser.add_argument("--dry-run", action="store_true",
                            help="print the plan as JSON, write nothing")
        parser.add_argument("--force", action="store_true",
                            help="replace the translated stores")
        options = parser.parse_args(argv)
        migration = Migration(self._layout, self._config, self._legacy)
        try:
            if options.dry_run:
                plan = migration.preview(options.force)
            else:
                plan = migration.apply(options.force)
        except MigrationError as failure:
            print(f"pi-daemon: migrate: {failure.message}", file=stderr)
            return failure.code
        print(json.dumps(plan.to_dict(options.dry_run), sort_keys=True,
                         indent=2), file=stdout)
        if plan.refusal() is not None:
            code, message = plan.refunds()
            print(f"pi-daemon: migrate: {message}", file=stderr)
            return code
        return 0
