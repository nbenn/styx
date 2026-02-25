"""Unit tests for styx.vm_shutdown."""

import os
import shutil
import signal
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import io
from contextlib import redirect_stdout

from styx.vm_shutdown import _read_pid, _alive, _poll_dead, _qmp_powerdown, shutdown, check

_VMID = '101'


# ── QMP mock server ───────────────────────────────────────────────────────────

class _QmpHandler(socketserver.BaseRequestHandler):
    """Handles one QMP connection: greeting → qmp_capabilities → system_powerdown.

    If server.kill_pid is set, sends SIGTERM to that process after responding,
    simulating the guest actually powering down.
    """
    def handle(self):
        self.request.sendall(b'{"QMP": {"version": {}, "capabilities": []}}\n')
        self.request.recv(4096)                          # qmp_capabilities
        self.request.sendall(b'{"return": {}}\n')
        self.request.recv(4096)                          # system_powerdown
        self.request.sendall(b'{"return": {}}\n')
        if self.server.kill_pid:
            try:
                os.kill(self.server.kill_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


class _FakeQmpServer:
    """Context manager that runs a QMP mock server in a daemon thread."""

    def __init__(self, path, kill_pid=None):
        self._server = socketserver.UnixStreamServer(path, _QmpHandler)
        self._server.kill_pid = kill_pid
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.daemon = True

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._server.shutdown()
        self._server.server_close()


# ── Base class ────────────────────────────────────────────────────────────────

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

    def _spawn(self, vmid=_VMID):
        """Start a sleep process and write its PID file."""
        proc = subprocess.Popen(
            ['sleep', '3600'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._procs.append(proc)
        Path(self._tmp, f'{vmid}.pid').write_text(str(proc.pid))
        return proc

    def _pid_path(self, vmid=_VMID):
        return Path(self._tmp, f'{vmid}.pid')

    def _qmp_path(self, vmid=_VMID):
        return str(Path(self._tmp, f'{vmid}.qmp'))


# ── TestReadPid ───────────────────────────────────────────────────────────────

class TestReadPid(_Base):

    def test_missing_file_returns_none(self):
        self.assertIsNone(_read_pid(_VMID))

    def test_valid_pid_file(self):
        self._pid_path().write_text('12345')
        self.assertEqual(_read_pid(_VMID), 12345)

    def test_malformed_pid_file_returns_none(self):
        self._pid_path().write_text('not-a-number')
        self.assertIsNone(_read_pid(_VMID))


# ── TestAlive ─────────────────────────────────────────────────────────────────

class TestAlive(unittest.TestCase):

    def test_running_process(self):
        self.assertTrue(_alive(os.getpid()))

    def test_dead_process(self):
        proc = subprocess.Popen(['true'], stdout=subprocess.DEVNULL)
        proc.wait()
        self.assertFalse(_alive(proc.pid))


# ── TestPollDead ──────────────────────────────────────────────────────────────

class TestPollDead(unittest.TestCase):

    def test_already_dead_returns_true(self):
        proc = subprocess.Popen(['true'], stdout=subprocess.DEVNULL)
        proc.wait()
        self.assertTrue(_poll_dead(proc.pid, time.monotonic() + 1, interval=0.05))

    def test_dies_during_poll_returns_true(self):
        proc = subprocess.Popen(
            ['sleep', '3600'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        def _kill():
            time.sleep(0.05)
            proc.terminate()
        threading.Thread(target=_kill, daemon=True).start()
        self.assertTrue(_poll_dead(proc.pid, time.monotonic() + 2, interval=0.05))
        proc.wait()

    def test_deadline_exceeded_returns_false(self):
        proc = subprocess.Popen(
            ['sleep', '3600'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            result = _poll_dead(proc.pid, time.monotonic() + 0.1, interval=0.05)
        finally:
            proc.terminate()
            proc.wait()
        self.assertFalse(result)


# ── TestQmpPowerdown ──────────────────────────────────────────────────────────

class TestQmpPowerdown(_Base):

    def test_no_socket_returns_false(self):
        self.assertFalse(_qmp_powerdown(_VMID))

    def test_valid_socket_returns_true(self):
        with _FakeQmpServer(self._qmp_path()):
            self.assertTrue(_qmp_powerdown(_VMID))


# ── TestShutdown ──────────────────────────────────────────────────────────────

class TestShutdown(_Base):

    def test_no_pid_file_returns_0(self):
        self.assertEqual(shutdown(_VMID, timeout=1), 0)

    def test_process_already_dead_returns_0(self):
        proc = subprocess.Popen(['true'], stdout=subprocess.DEVNULL)
        proc.wait()
        self._pid_path().write_text(str(proc.pid))
        self.assertEqual(shutdown(_VMID, timeout=1), 0)

    def test_no_qmp_sigterm_sufficient(self):
        # No socket → falls through directly to SIGTERM; sleep is killed
        proc = self._spawn()
        self.assertEqual(shutdown(_VMID, timeout=1), 0)
        self.assertFalse(_alive(proc.pid))

    def test_qmp_present_vm_stops_gracefully(self):
        # QMP server responds and kills the process, simulating a clean guest shutdown
        proc = self._spawn()
        with _FakeQmpServer(self._qmp_path(), kill_pid=proc.pid):
            self.assertEqual(shutdown(_VMID, timeout=5), 0)
        self.assertFalse(_alive(proc.pid))

    def test_qmp_timeout_falls_through_to_sigterm(self):
        # QMP responds but process doesn't stop within timeout=0 → SIGTERM
        proc = self._spawn()
        with _FakeQmpServer(self._qmp_path()):   # no kill_pid
            self.assertEqual(shutdown(_VMID, timeout=0), 0)
        self.assertFalse(_alive(proc.pid))

    def test_sigkill_sent_when_sigterm_ignored(self):
        # Verify that SIGTERM is tried first and SIGKILL follows when it fails.
        # Use mocked internals to avoid the hardcoded 10s SIGTERM poll wait.
        with mock.patch('styx.vm_shutdown._read_pid', return_value=12345), \
             mock.patch('styx.vm_shutdown._alive', side_effect=[True, False]), \
             mock.patch('styx.vm_shutdown._qmp_powerdown', return_value=False), \
             mock.patch('styx.vm_shutdown._poll_dead', return_value=False), \
             mock.patch('os.kill') as mock_kill:
            result = shutdown(_VMID, timeout=0)
        self.assertEqual(result, 0)
        sent = [call.args[1] for call in mock_kill.call_args_list]
        self.assertIn(signal.SIGTERM, sent)
        self.assertIn(signal.SIGKILL, sent)
        self.assertLess(sent.index(signal.SIGTERM), sent.index(signal.SIGKILL))

    def test_process_dies_between_sigterm_and_poll(self):
        # ProcessLookupError on os.kill(SIGTERM) → process already gone → return 0
        with mock.patch('styx.vm_shutdown._read_pid', return_value=12345), \
             mock.patch('styx.vm_shutdown._alive', return_value=True), \
             mock.patch('styx.vm_shutdown._qmp_powerdown', return_value=False), \
             mock.patch('os.kill', side_effect=ProcessLookupError):
            self.assertEqual(shutdown(_VMID, timeout=0), 0)


# ── TestCheck ─────────────────────────────────────────────────────────────────

class TestCheck(_Base):

    def _capture(self, vmid=_VMID):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = check(vmid)
        return rc, buf.getvalue()

    def test_no_pid_file_reports_not_running(self):
        rc, out = self._capture()
        self.assertEqual(rc, 0)
        self.assertIn('not running', out)

    def test_running_process_reports_pid_and_would_shut_down(self):
        proc = self._spawn()
        rc, out = self._capture()
        self.assertEqual(rc, 0)
        self.assertIn('running', out)
        self.assertIn(str(proc.pid), out)
        self.assertIn('[dry-run]', out)

    def test_dead_process_reports_not_running(self):
        proc = subprocess.Popen(['true'], stdout=subprocess.DEVNULL)
        proc.wait()
        self._pid_path().write_text(str(proc.pid))
        rc, out = self._capture()
        self.assertEqual(rc, 0)
        self.assertIn('not running', out)


if __name__ == '__main__':
    unittest.main()
