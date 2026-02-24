"""Integration test helpers: FakeOperations and fake VM lifecycle."""

import itertools
import os
import signal
import subprocess
import tempfile
import threading
from pathlib import Path


def start_fake_vm(vmid, run_dir):
    """Spawn a sleep process and write its PID file. Returns PID."""
    proc = subprocess.Popen(
        ['sleep', '3600'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proc.fake_vmid = vmid   # keep reference so caller can .wait() if needed
    Path(run_dir, f'{vmid}.pid').write_text(str(proc.pid))
    return proc.pid


def stop_fake_vm(vmid, run_dir):
    """Kill the fake VM process and remove its PID file."""
    pid_file = Path(run_dir, f'{vmid}.pid')
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
        except (ValueError, ProcessLookupError):
            pass
        pid_file.unlink()


def kill_all_fake_vms(run_dir):
    for pid_file in Path(run_dir).glob('*.pid'):
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
        except (ValueError, ProcessLookupError):
            pass
        pid_file.unlink()


class FakeOperations:
    """Test double for styx.wrappers.Operations.

    Tracks all operations in lists. shutdown_vm actually kills the fake VM
    process so get_running_vmids() returns correct results in the polling loop.
    """

    def __init__(self, run_dir, vm_host):
        self._run_dir = Path(run_dir)
        self._vm_host = vm_host   # vmid -> host

        self.cordon_log   = []
        self.drain_log    = []
        self.shutdown_log = []
        self.ha_log       = []
        self.ceph_log     = []
        self.poweroff_log = []
        self.sequence_log = []   # (seq, action) for ordering assertions

        self._seq  = itertools.count()
        self._lock = threading.Lock()

    def get_running_vmids(self, host):
        result = []
        for pid_file in self._run_dir.glob('*.pid'):
            vmid = pid_file.stem
            if self._vm_host.get(vmid) != host:
                continue
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                result.append(vmid)
            except (ValueError, ProcessLookupError, OSError):
                pass
        return result

    def shutdown_vm(self, host, vmid, timeout):
        entry = f'SHUTDOWN {vmid} on {host}'
        with self._lock:
            self.shutdown_log.append(entry)
            self.sequence_log.append((next(self._seq), entry))
        pid_file = self._run_dir / f'{vmid}.pid'
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
            except (ValueError, ProcessLookupError):
                pass
            pid_file.unlink()

    def cordon_node(self, node):
        self.cordon_log.append(f'CORDON {node}')

    def drain_node(self, node, timeout):
        with self._lock:
            self.drain_log.append(f'DRAIN {node}')
            self.sequence_log.append((next(self._seq), f'DRAIN {node}'))
        return True

    def check_vm(self, host, vmid):
        pass  # dry-run only: report live VM status

    def list_volume_attachments_for_node(self, node):
        return []

    def get_ha_started_sids(self):
        return []

    def disable_ha_sid(self, sid):
        self.ha_log.append(f'DISABLE_HA {sid}')

    def wait_ha_disabled(self, sid, timeout=30):
        return True

    def set_ceph_flags(self, flags):
        self.ceph_log.append(f'CEPH_FLAGS {" ".join(flags)}')

    def poweroff_host(self, host):
        self.poweroff_log.append(f'POWEROFF {host}')

    def poweroff_self(self):
        self.poweroff_log.append('POWEROFF_SELF')
