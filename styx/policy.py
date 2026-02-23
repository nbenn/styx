"""styx.policy — Execution policy.

Three concrete modes:
  DryRunPolicy      — log all planned actions, execute nothing.
  Policy            — emergency mode: execute automatically, warn and continue.
  MaintenancePolicy — maintenance mode: pre-flight + interactive gates.
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
    """Emergency mode: execute automatically, warn and continue, no gates."""

    @property
    def dry_run(self):
        return False

    def on_warning(self, msg):
        log(f'WARNING: {msg}')

    def phase_gate(self, summary):
        """Checkpoint between phases. Emergency: no-op. Maintenance: prompt."""

    def execute(self, description, fn, *args, **kwargs):
        """Run fn(*args, **kwargs)."""
        return fn(*args, **kwargs)


class DryRunPolicy(Policy):
    """Dry-run mode: log all planned actions, execute nothing."""

    @property
    def dry_run(self):
        return True

    def execute(self, description, fn, *args, **kwargs):
        log(f'[dry-run] {description}')
        return None


class MaintenancePolicy(Policy):
    """Maintenance mode: warnings prompt [skip/abort]; gates require confirmation.

    Pass _input=<callable> to substitute stdin for testing.
    """

    def __init__(self, _input=None):
        self._input = _input if _input is not None else input
        import threading
        self._lock = threading.Lock()

    def on_warning(self, msg):
        log(f'WARNING: {msg}')
        with self._lock:
            while True:
                try:
                    choice = self._input('  [s]kip  [a]bort: ').strip().lower()
                except EOFError:
                    choice = 's'
                if choice in ('s', 'skip', ''):
                    return
                if choice in ('a', 'abort'):
                    import sys
                    sys.exit(1)

    def phase_gate(self, summary):
        log(summary)
        while True:
            try:
                choice = self._input('  [y]es  [a]bort: ').strip().lower()
            except EOFError:
                choice = 'y'
            if choice in ('y', 'yes', ''):
                return
            if choice in ('a', 'abort', 'n', 'no'):
                import sys
                sys.exit(0)
