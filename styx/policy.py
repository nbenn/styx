"""styx.policy — Execution policy (dry-run + warning handling).

Emergency mode (default): warn and continue.
Future MaintenancePolicy will prompt the operator instead.
"""

import datetime

_log_fh = None


def _now():
    return datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def setup_log_file(path):
    """Open path in append mode; subsequent log() calls tee there."""
    global _log_fh
    import atexit, os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if _log_fh is not None:
        _log_fh.close()
    _log_fh = open(path, 'a', buffering=1)   # line-buffered
    atexit.register(_log_fh.close)


def log(msg):
    line = f'[{_now()}] {msg}'
    print(line, flush=True)
    if _log_fh is not None:
        print(line, file=_log_fh)


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
