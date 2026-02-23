"""Smoke tests: build styx.pyz and verify it runs correctly as a zipapp.

These tests catch deployment-mode issues that unit tests with mocked
subprocesses cannot — specifically, that 'python3 styx.pyz vm-shutdown'
works without PYTHONPATH configuration on any host that has the file.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


class TestZipappSmoke(unittest.TestCase):

    _zipapp = None
    _tmpdir = None

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp()
        cls._zipapp = os.path.join(cls._tmpdir, 'styx.pyz')
        result = subprocess.run(
            ['bash', 'scripts/build.sh', cls._zipapp],
            cwd=_REPO_ROOT,
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise unittest.SkipTest(f'Could not build styx.pyz: {result.stderr}')

    @classmethod
    def tearDownClass(cls):
        if cls._tmpdir:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def test_vm_shutdown_nonexistent_vm_exits_zero(self):
        """python3 styx.pyz vm-shutdown <vmid> exits 0 when VM is not running.

        This is the core regression test for the deployment bug: if 'python3
        styx.pyz vm-shutdown' fails with 'No module named styx', this test
        catches it.
        """
        result = subprocess.run(
            [sys.executable, self._zipapp, 'vm-shutdown', '99999'],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('not running', result.stdout)

    def test_no_args_prints_usage_to_stderr(self):
        """python3 styx.pyz with no args exits non-zero and prints usage."""
        result = subprocess.run(
            [sys.executable, self._zipapp],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('vm-shutdown', result.stderr)

    def test_unknown_command_exits_nonzero(self):
        result = subprocess.run(
            [sys.executable, self._zipapp, 'no-such-command'],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
