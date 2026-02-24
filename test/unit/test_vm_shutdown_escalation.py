"""Unit tests for vm_shutdown escalation paths and _alive edge cases."""

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from styx.vm_shutdown import _alive, shutdown, check


class TestAliveEdgeCases(unittest.TestCase):
    """Edge cases in _alive: PermissionError, zombie detection."""

    def test_permission_error_returns_true(self):
        """Process exists but we can't signal it → treat as alive."""
        with mock.patch('os.kill', side_effect=PermissionError):
            self.assertTrue(_alive(99999))

    def test_zombie_detected_as_dead(self):
        """A zombie process (state Z) should be treated as dead."""
        with mock.patch('os.kill'):   # no error → pid exists
            with mock.patch('builtins.open',
                            mock.mock_open(read_data='12345 (proc) Z 1 ...')):
                self.assertFalse(_alive(12345))

    def test_normal_running_state(self):
        """A running process (state S) should be treated as alive."""
        with mock.patch('os.kill'):
            with mock.patch('builtins.open',
                            mock.mock_open(read_data='12345 (proc) S 1 ...')):
                self.assertTrue(_alive(12345))

    def test_stat_read_fails_returns_true(self):
        """If /proc/<pid>/stat can't be read, assume alive (safe default)."""
        with mock.patch('os.kill'):
            with mock.patch('builtins.open', side_effect=OSError):
                self.assertTrue(_alive(12345))


class _Base(unittest.TestCase):
    """Patches _PID_FILE and _QMP_SOCKET to a temp directory."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._procs = []
        pid_tpl = os.path.join(self._tmp, '{vmid}.pid')
        qmp_tpl = os.path.join(self._tmp, '{vmid}.qmp')
        self._p_pid = mock.patch('styx.vm_shutdown._PID_FILE', pid_tpl)
        self._p_qmp = mock.patch('styx.vm_shutdown._QMP_SOCKET', qmp_tpl)
        self._p_pid.start()
        self._p_qmp.start()

    def tearDown(self):
        self._p_pid.stop()
        self._p_qmp.stop()
        for pid_file in Path(self._tmp).glob('*.pid'):
            try:
                os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
            except (ValueError, ProcessLookupError, OSError):
                pass
        for proc in self._procs:
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _spawn(self, vmid='101'):
        proc = subprocess.Popen(
            ['sleep', '3600'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._procs.append(proc)
        Path(self._tmp, f'{vmid}.pid').write_text(str(proc.pid))
        return proc


class TestShutdownSIGKILLEscalation(_Base):
    """Test the full SIGTERM → SIGKILL escalation path."""

    def test_sigkill_after_sigterm_timeout(self):
        """When SIGTERM doesn't kill the process, SIGKILL is sent."""
        with mock.patch('styx.vm_shutdown._read_pid', return_value=12345), \
             mock.patch('styx.vm_shutdown._alive', side_effect=[True, False]), \
             mock.patch('styx.vm_shutdown._qmp_powerdown', return_value=False), \
             mock.patch('styx.vm_shutdown._poll_dead', return_value=False), \
             mock.patch('os.kill') as mock_kill:
            result = shutdown('101', timeout=0)
        self.assertEqual(result, 0)
        signals_sent = [c.args[1] for c in mock_kill.call_args_list]
        self.assertIn(signal.SIGTERM, signals_sent)
        self.assertIn(signal.SIGKILL, signals_sent)

    def test_sigkill_process_lookup_error_returns_0(self):
        """Process dies between SIGTERM timeout and SIGKILL → return 0."""
        with mock.patch('styx.vm_shutdown._read_pid', return_value=12345), \
             mock.patch('styx.vm_shutdown._alive', return_value=True), \
             mock.patch('styx.vm_shutdown._qmp_powerdown', return_value=False), \
             mock.patch('styx.vm_shutdown._poll_dead', return_value=False), \
             mock.patch('os.kill') as mock_kill:
            # First kill (SIGTERM) succeeds, second (SIGKILL) → ProcessLookupError
            mock_kill.side_effect = [None, ProcessLookupError]
            result = shutdown('101', timeout=0)
        self.assertEqual(result, 0)

    def test_unkillable_process_returns_1(self):
        """If even SIGKILL can't kill the process, return 1."""
        with mock.patch('styx.vm_shutdown._read_pid', return_value=12345), \
             mock.patch('styx.vm_shutdown._alive', return_value=True), \
             mock.patch('styx.vm_shutdown._qmp_powerdown', return_value=False), \
             mock.patch('styx.vm_shutdown._poll_dead', return_value=False), \
             mock.patch('os.kill'):
            result = shutdown('101', timeout=0)
        self.assertEqual(result, 1)

    def test_qmp_succeeds_but_timeout_falls_to_sigterm(self):
        """QMP powerdown sent but VM doesn't stop → escalate to SIGTERM."""
        proc = self._spawn()
        # No QMP server → _qmp_powerdown returns False → goes to SIGTERM directly
        result = shutdown('101', timeout=0)
        self.assertEqual(result, 0)
        self.assertFalse(_alive(proc.pid))

    def test_sigterm_sufficient_no_sigkill(self):
        """When SIGTERM kills the process, SIGKILL is not needed."""
        with mock.patch('styx.vm_shutdown._read_pid', return_value=12345), \
             mock.patch('styx.vm_shutdown._alive', side_effect=[True, True]), \
             mock.patch('styx.vm_shutdown._qmp_powerdown', return_value=False), \
             mock.patch('styx.vm_shutdown._poll_dead') as mock_poll, \
             mock.patch('os.kill') as mock_kill:
            # First poll (after SIGTERM) returns True → process died
            mock_poll.return_value = True
            result = shutdown('101', timeout=0)
        self.assertEqual(result, 0)
        signals_sent = [c.args[1] for c in mock_kill.call_args_list]
        self.assertIn(signal.SIGTERM, signals_sent)
        self.assertNotIn(signal.SIGKILL, signals_sent)


class TestShutdownForceKillOutput(_Base):
    """Verify output messages during escalation."""

    def test_force_killed_message(self):
        """SIGKILL success prints 'force-killed' message."""
        import io
        from contextlib import redirect_stdout
        with mock.patch('styx.vm_shutdown._read_pid', return_value=12345), \
             mock.patch('styx.vm_shutdown._alive') as mock_alive, \
             mock.patch('styx.vm_shutdown._qmp_powerdown', return_value=False), \
             mock.patch('styx.vm_shutdown._poll_dead', return_value=False), \
             mock.patch('os.kill'):
            # _alive: True at line 73 (initial check), False at line 98 (after SIGKILL)
            mock_alive.side_effect = [True, False]
            buf = io.StringIO()
            with redirect_stdout(buf):
                result = shutdown('101', timeout=0)
        self.assertEqual(result, 0)
        self.assertIn('force-killed', buf.getvalue())

    def test_could_not_be_killed_message(self):
        """When nothing works, error message goes to stderr."""
        import io
        from contextlib import redirect_stderr, redirect_stdout
        with mock.patch('styx.vm_shutdown._read_pid', return_value=12345), \
             mock.patch('styx.vm_shutdown._alive', return_value=True), \
             mock.patch('styx.vm_shutdown._qmp_powerdown', return_value=False), \
             mock.patch('styx.vm_shutdown._poll_dead', return_value=False), \
             mock.patch('os.kill'):
            err_buf = io.StringIO()
            out_buf = io.StringIO()
            with redirect_stderr(err_buf), redirect_stdout(out_buf):
                result = shutdown('101', timeout=0)
        self.assertEqual(result, 1)
        self.assertIn('could not be killed', err_buf.getvalue())


if __name__ == '__main__':
    unittest.main()
