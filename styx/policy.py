"""styx.policy — Execution policy (dry-run + warning handling).

Emergency mode (default): warn and continue.
Future MaintenancePolicy will prompt the operator instead.
"""

import datetime


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def log(msg):
    print(f'[{_now()}] {msg}', flush=True)


class Policy:
    """Controls dry-run behaviour and non-fatal failure handling."""

    def __init__(self, dry_run=False):
        self._dry_run = dry_run

    @property
    def dry_run(self):
        return self._dry_run

    def on_warning(self, msg):
        log(f'WARNING: {msg}')

    def execute(self, description, fn, *args, **kwargs):
        """Run fn(*args, **kwargs), or log and skip in dry-run mode."""
        if self._dry_run:
            log(f'[dry-run] {description}')
            return None
        return fn(*args, **kwargs)
