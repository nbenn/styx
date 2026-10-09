"""Tests for container/watch.sh — the NUT watcher in the styx-trigger image.

upsc and trigger.sh are replaced by fakes: upsc returns one status per poll
from a list (a line "FAIL" makes it fail), trigger.sh records its arguments.
"""

import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_WATCH = os.path.join(_REPO_ROOT, 'container', 'watch.sh')

_FAKE_UPSC = textwrap.dedent('''\
    #!/bin/bash
    n=$(cat "$FAKE_DIR/count" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$FAKE_DIR/count"
    line=$(sed -n "$((n + 1))p" "$FAKE_DIR/statuses")
    if [[ -z "$line" ]]; then
        touch "$FAKE_DIR/done"
        echo "Error: exhausted" >&2
        exit 1
    fi
    if [[ "$line" == FAIL ]]; then
        echo "Error: Driver not connected" >&2
        exit 1
    fi
    echo "Init SSL without certificate database" >&2
    echo "$line"
''')

_FAKE_TRIGGER = textwrap.dedent('''\
    #!/bin/bash
    echo "$*" >> "$FAKE_DIR/calls"
    echo "Trying somewhere..."
    exit "$(cat "$FAKE_DIR/trigger_rc" 2>/dev/null || echo 0)"
''')


class TestWatch(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for name, body in (('upsc', _FAKE_UPSC), ('trigger', _FAKE_TRIGGER)):
            path = os.path.join(self.dir, name)
            with open(path, 'w') as f:
                f.write(body)
            os.chmod(path, 0o755)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _env(self, **overrides):
        env = dict(os.environ)
        env.update({
            'FAKE_DIR': self.dir,
            'STYX_UPSC': os.path.join(self.dir, 'upsc'),
            'STYX_TRIGGER': os.path.join(self.dir, 'trigger'),
            'STYX_CONTROLLERS': '10.0.0.1 10.0.0.2',
            'STYX_POLL_INTERVAL': '0.05',
            'STYX_ONBATT_MIN': '0',
        })
        env.update(overrides)
        return env

    def _run(self, statuses, trigger_rc=0, **env):
        """Run watch.sh until all statuses are consumed; return (calls, log)."""
        with open(os.path.join(self.dir, 'statuses'), 'w') as f:
            f.write(''.join(s + '\n' for s in statuses))
        with open(os.path.join(self.dir, 'trigger_rc'), 'w') as f:
            f.write(str(trigger_rc))
        proc = subprocess.Popen(['bash', _WATCH], env=self._env(**env),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        deadline = time.monotonic() + 20
        while not os.path.exists(os.path.join(self.dir, 'done')):
            if proc.poll() is not None or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        proc.terminate()
        out, _ = proc.communicate(timeout=5)
        calls_path = os.path.join(self.dir, 'calls')
        calls = []
        if os.path.exists(calls_path):
            with open(calls_path) as f:
                calls = f.read().splitlines()
        return calls, out

    def test_ob_lb_triggers_once(self):
        calls, out = self._run(['OL', 'OB', 'OB LB', 'OB LB', 'OB LB'])
        self.assertEqual(calls, ['--controllers 10.0.0.1 10.0.0.2 --mode dry-run'])
        self.assertIn('trigger: Trying somewhere...', out)

    def test_ob_alone_does_not_trigger(self):
        calls, _ = self._run(['OL', 'OB', 'OB', 'OB CHRG', 'OL'])
        self.assertEqual(calls, [])

    def test_lb_while_online_does_not_trigger(self):
        calls, _ = self._run(['OL LB', 'OL LB CHRG'])
        self.assertEqual(calls, [])

    def test_minimum_time_on_battery(self):
        calls, _ = self._run(['OB LB'] * 5, STYX_ONBATT_MIN='3600')
        self.assertEqual(calls, [])

    def test_status_changes_are_logged_and_stderr_ignored(self):
        _, out = self._run(['OL', 'OB', 'OB'])
        self.assertIn('status: <none> -> OL', out)
        self.assertIn('status: OL -> OB', out)
        self.assertEqual(out.count('-> OB\n'), 1)
        self.assertNotIn('-> Init SSL', out)

    def test_reset_after_clear_allows_new_trigger(self):
        calls, out = self._run(['OB LB', 'OB LB', 'OL', 'OB LB'])
        self.assertEqual(len(calls), 2)
        self.assertIn('OB+LB cleared; reset', out)

    def test_failed_trigger_is_retried(self):
        calls, out = self._run(['OB LB'] * 3, trigger_rc=1)
        self.assertEqual(len(calls), 3)
        self.assertIn('trigger failed; retrying on next poll', out)

    def test_failed_poll_does_not_reset_timer(self):
        # 2 s of OB+LB required; the OB+LB polls are separated by ~2.5 s of
        # failed polls. If those reset the timer, the last poll cannot trigger.
        statuses = ['OB LB'] + ['FAIL'] * 50 + ['OB LB']
        calls, out = self._run(statuses, STYX_ONBATT_MIN='2')
        self.assertEqual(len(calls), 1)
        self.assertIn('unavailable (Error: Driver not connected)', out)
        self.assertNotIn('reset', out)

    def test_failed_poll_never_triggers(self):
        calls, _ = self._run(['FAIL'] * 5)
        self.assertEqual(calls, [])

    def test_emergency_mode_and_ssh_options_are_forwarded(self):
        calls, _ = self._run(['OB LB'], STYX_MODE='emergency',
                             STYX_SSH_KEY='/k/id', STYX_KNOWN_HOSTS='/k/known_hosts')
        self.assertEqual(calls, ['--controllers 10.0.0.1 10.0.0.2 --key /k/id '
                                 '--known-hosts /k/known_hosts --mode emergency'])

    def test_missing_controllers_fails(self):
        r = subprocess.run(['bash', _WATCH], env=self._env(STYX_CONTROLLERS=''),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 1)
        self.assertIn('STYX_CONTROLLERS', r.stdout)

    def test_invalid_mode_fails(self):
        r = subprocess.run(['bash', _WATCH], env=self._env(STYX_MODE='maintenance'),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 1)
        self.assertIn('STYX_MODE', r.stdout)


if __name__ == '__main__':
    unittest.main()
