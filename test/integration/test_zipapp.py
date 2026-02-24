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
import textwrap
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

    def test_process_continues_after_executable_deleted(self):
        """Imported styx modules keep working after the .pyz is deleted from disk.

        Simulates the scenario where CephFS loses quorum mid-run, making the
        shared .pyz inaccessible to new processes.  Because Python caches all
        imported code in memory (zipimport does not keep the file open after
        reading each module), every styx function should continue to work
        normally once startup imports are complete.
        """
        pyz = os.path.join(self._tmpdir, 'styx-deletion-test.pyz')
        shutil.copy(self._zipapp, pyz)

        script = textwrap.dedent(f"""\
            import sys, os
            sys.path.insert(0, {pyz!r})
            sys.argv = [{pyz!r}]

            # Mirror what happens during startup / discover():
            # import every styx module, including the one lazy import (k8s).
            from styx.policy import Policy, log
            from styx.classify import other_vmids
            from styx.decide import (
                should_disable_ha, should_run_polling, should_poweroff_hosts,
            )
            from styx.config import load_config
            from styx.discover import ClusterTopology
            from styx.wrappers import Operations, _styx_cmd, _local_pyz
            from styx.k8s import K8sClient        # the one lazy import in real code
            from styx.orchestrate import discover, preflight

            # Simulate CephFS going away.
            os.unlink({pyz!r})
            assert not os.path.exists({pyz!r})

            # Every function must still work from in-memory code objects.
            assert _styx_cmd() == f'python3 {pyz}', _styx_cmd()
            assert other_vmids(['101', '201', '211'], ['211'], []) == ['101', '201']
            assert should_disable_ha(3) is True
            assert should_run_polling(3) is True
            assert should_poweroff_hosts(3) is True
            Policy().on_warning('still works after deletion')

            print('OK')
        """)

        result = subprocess.run(
            [sys.executable, '-c', script],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn('OK', result.stdout)


if __name__ == '__main__':
    unittest.main()
