"""Unit tests for styx.local_shutdown."""

import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import io
from contextlib import redirect_stdout, redirect_stderr

from styx.local_shutdown import run, main, _parse_workload


class _Base(unittest.TestCase):
    """Patches _PID_FILE and _QMP_SOCKET to a temp directory for each test."""

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
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _spawn(self, vmid):
        """Start a sleep process and write its PID file."""
        proc = subprocess.Popen(
            ['sleep', '3600'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._procs.append(proc)
        Path(self._tmp, f'{vmid}.pid').write_text(str(proc.pid))
        return proc


class TestParseWorkload(unittest.TestCase):

    def test_bare_vmid_defaults_to_qemu(self):
        self.assertEqual(_parse_workload('101'), ('qemu', '101'))

    def test_qemu_prefix(self):
        self.assertEqual(_parse_workload('qemu:101'), ('qemu', '101'))

    def test_lxc_prefix(self):
        self.assertEqual(_parse_workload('lxc:200'), ('lxc', '200'))

    def test_unknown_type_preserved(self):
        self.assertEqual(_parse_workload('oci:300'), ('oci', '300'))

    def test_colon_in_vmid_preserved(self):
        # Edge case: only first colon is the separator
        self.assertEqual(_parse_workload('qemu:101:extra'), ('qemu', '101:extra'))


class TestRun(_Base):

    def test_empty_workloads(self):
        rc = run([], timeout_vm=5)
        self.assertEqual(rc, 0)

    def test_single_vm_shutdown(self):
        proc = self._spawn('101')
        rc = run([('qemu', '101')], timeout_vm=5)
        self.assertEqual(rc, 0)
        # Process should have exited (poll returns exit code, not None)
        proc.wait(timeout=2)
        self.assertIsNotNone(proc.poll())

    def test_parallel_shutdown(self):
        procs = {}
        for vmid in ['101', '102', '103']:
            procs[vmid] = self._spawn(vmid)
        rc = run([('qemu', '101'), ('qemu', '102'), ('qemu', '103')], timeout_vm=5)
        self.assertEqual(rc, 0)
        for vmid, proc in procs.items():
            proc.wait(timeout=2)
            self.assertIsNotNone(proc.poll())

    def test_dry_run_does_not_kill(self):
        proc = self._spawn('101')
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = run([('qemu', '101')], timeout_vm=5, dry_run=True)
        self.assertEqual(rc, 0)
        # Process should still be alive
        os.kill(proc.pid, 0)  # should not raise
        self.assertIn('101', buf.getvalue())

    def test_vm_not_running_returns_0(self):
        # No PID file for this VM
        rc = run([('qemu', '999')], timeout_vm=5)
        self.assertEqual(rc, 0)

    def test_unknown_type_returns_1(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = run([('lxc', '200')], timeout_vm=5)
        self.assertEqual(rc, 1)
        self.assertIn('unknown workload type', buf.getvalue())

    def test_unknown_type_dry_run_warns(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = run([('lxc', '200')], timeout_vm=5, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn('unknown workload type', buf.getvalue())

    def test_poweroff_deadline_calls_poweroff(self):
        # Set deadline in the past so it fires immediately
        with mock.patch('os.system') as mock_system:
            rc = run([], timeout_vm=5,
                     poweroff_deadline=time.monotonic() - 1)
        self.assertEqual(rc, 0)
        mock_system.assert_called_once_with('poweroff')

    def test_poweroff_deadline_future_sleeps(self):
        # Deadline 0.1s in the future — should sleep briefly then poweroff
        with mock.patch('os.system') as mock_system, \
             mock.patch('time.sleep') as mock_sleep:
            deadline = time.monotonic() + 0.1
            rc = run([], timeout_vm=5, poweroff_deadline=deadline)
        self.assertEqual(rc, 0)
        mock_system.assert_called_once_with('poweroff')
        # sleep was called with a positive remaining time
        self.assertTrue(mock_sleep.called)
        self.assertGreater(mock_sleep.call_args[0][0], 0)

    def test_no_poweroff_when_deadline_none(self):
        with mock.patch('os.system') as mock_system:
            rc = run([], timeout_vm=5, poweroff_deadline=None)
        self.assertEqual(rc, 0)
        mock_system.assert_not_called()


class TestMain(_Base):

    def test_basic_invocation(self):
        proc = self._spawn('101')
        with self.assertRaises(SystemExit) as cm:
            main(['101', '--timeout', '5'])
        self.assertEqual(cm.exception.code, 0)

    def test_type_prefixed_invocation(self):
        proc = self._spawn('101')
        with self.assertRaises(SystemExit) as cm:
            main(['qemu:101', '--timeout', '5'])
        self.assertEqual(cm.exception.code, 0)

    def test_dry_run_flag(self):
        proc = self._spawn('101')
        buf = io.StringIO()
        with redirect_stdout(buf), \
             self.assertRaises(SystemExit) as cm:
            main(['qemu:101', '--timeout', '5', '--dry-run'])
        self.assertEqual(cm.exception.code, 0)
        os.kill(proc.pid, 0)  # should still be alive

    def test_poweroff_delay_flag(self):
        with mock.patch('os.system') as mock_system, \
             mock.patch('time.sleep'), \
             self.assertRaises(SystemExit) as cm:
            main(['101', '--timeout', '5', '--poweroff-delay', '0'])
        self.assertEqual(cm.exception.code, 0)
        mock_system.assert_called_once_with('poweroff')


if __name__ == '__main__':
    unittest.main()
