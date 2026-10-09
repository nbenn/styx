"""Tests for container/check.sh — the no-trigger check in the styx-trigger image.

trigger.sh is replaced by a fake that answers -v per node: "good" with the
image version, "old" with an older one, "down" like an unreachable host and
"garbage" with no version at all.
"""

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_CHECK = os.path.join(_REPO_ROOT, 'container', 'check.sh')

_FAKE_UPSC = textwrap.dedent('''\
    #!/bin/bash
    if [[ -f "$FAKE_DIR/ups_down" ]]; then
        echo "Error: Driver not connected" >&2
        exit 1
    fi
    echo "OL"
''')

_FAKE_TRIGGER = textwrap.dedent('''\
    #!/bin/bash
    echo "$*" >> "$FAKE_DIR/calls"
    node="$2"
    echo "Trying ${node}..."
    case "$node" in
        good)    echo "0.4.4" ;;
        old)     echo "0.4.3" ;;
        garbage) echo "hello" ;;
        down)
            echo "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@"
            echo "ssh: connect to host down port 22: Connection timed out"
            echo "${node}: unreachable or failed, trying next..."
            echo "ERROR: all controllers unreachable." >&2
            exit 1 ;;
    esac
    echo "Done via ${node}."
''')


class TestCheck(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, 'state')
        for name, body in (('upsc', _FAKE_UPSC), ('trigger', _FAKE_TRIGGER)):
            path = os.path.join(self.dir, name)
            with open(path, 'w') as f:
                f.write(body)
            os.chmod(path, 0o755)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _check(self, controllers, **overrides):
        env = dict(os.environ)
        env.update({
            'FAKE_DIR': self.dir,
            'STYX_UPSC': os.path.join(self.dir, 'upsc'),
            'STYX_TRIGGER': os.path.join(self.dir, 'trigger'),
            'STYX_CONTROLLERS': controllers,
            'STYX_STATE_DIR': self.state,
            'STYX_IMAGE_VERSION': '0.4.4',
            'SSH_CONFIG': os.path.join(self.dir, 'no-ssh-config'),
        })
        env.update(overrides)
        return subprocess.run(['bash', _CHECK], env=env, capture_output=True,
                              text=True, timeout=30)

    def _ready(self):
        return os.path.exists(os.path.join(self.state, 'ready'))

    def _calls(self):
        with open(os.path.join(self.dir, 'calls')) as f:
            return f.read().splitlines()

    def test_all_ok(self):
        r = self._check('good good')
        self.assertEqual(r.returncode, 0, msg=r.stdout)
        self.assertIn('UPS ups@127.0.0.1: ok (OL)', r.stdout)
        self.assertEqual(r.stdout.count('controller good: ok (styx 0.4.4)'), 2)
        self.assertTrue(self._ready())

    def test_each_controller_checked_separately_with_version_flag(self):
        self._check('good old', STYX_SSH_KEY='/k/id', STYX_KNOWN_HOSTS='/k/kh')
        self.assertEqual(self._calls(), [
            '--controllers good --key /k/id --known-hosts /k/kh --timeout 5 -v',
            '--controllers old --key /k/id --known-hosts /k/kh --timeout 5 -v',
        ])

    def test_known_hosts_picked_up_from_ssh_config(self):
        ssh_config = os.path.join(self.dir, 'ssh')
        os.makedirs(ssh_config)
        open(os.path.join(ssh_config, 'known_hosts'), 'w').close()
        self._check('good', SSH_CONFIG=ssh_config)
        self.assertEqual(self._calls(), [
            f'--controllers good --known-hosts {ssh_config}/known_hosts --timeout 5 -v'])

    def test_unreachable_controller_fails_with_ssh_error(self):
        r = self._check('good down')
        self.assertEqual(r.returncode, 1)
        self.assertIn('controller down: FAILED (ssh: connect to host down port 22: '
                      'Connection timed out)', r.stdout)
        self.assertIn('controller good: ok', r.stdout)
        self.assertFalse(self._ready())

    def test_response_without_version_fails(self):
        r = self._check('garbage')
        self.assertEqual(r.returncode, 1)
        self.assertIn('controller garbage: FAILED (no styx version in response)', r.stdout)

    def test_version_mismatch_warns_but_passes(self):
        r = self._check('old')
        self.assertEqual(r.returncode, 0)
        self.assertIn('controller old: ok (styx 0.4.3), WARNING: image is 0.4.4', r.stdout)
        self.assertTrue(self._ready())

    def test_ups_failure_fails(self):
        open(os.path.join(self.dir, 'ups_down'), 'w').close()
        r = self._check('good')
        self.assertEqual(r.returncode, 1)
        self.assertIn('UPS ups@127.0.0.1: FAILED (Error: Driver not connected)', r.stdout)
        self.assertFalse(self._ready())

    def test_ready_removed_after_later_failure(self):
        self._check('good')
        self.assertTrue(self._ready())
        self._check('down')
        self.assertFalse(self._ready())

    def test_missing_controllers_fails(self):
        r = self._check('')
        self.assertEqual(r.returncode, 1)
        self.assertIn('STYX_CONTROLLERS', r.stdout)


if __name__ == '__main__':
    unittest.main()
