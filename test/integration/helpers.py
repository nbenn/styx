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
        self._vmid_errors = {}    # host -> Exception; when set, get_running_vmids raises

        self.cordon_log   = []
        self.drain_log    = []
        self.drain_ignore_pdb = {}   # node -> ignore_pdb flag passed to drain
        self.mount_checks = []       # hosts asked for release-mounts --dry-run
        self.shutdown_log = []
        self.ha_log       = []
        self.ceph_log     = []
        self.poweroff_log = []
        self.sequence_log = []   # (seq, action) for ordering assertions

        self._seq  = itertools.count()
        self._lock = threading.Lock()

    def get_running_vmids(self, host):
        if host in self._vmid_errors:
            raise self._vmid_errors[host]
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

    def drain_node(self, node, timeout, ignore_pdb=False):
        with self._lock:
            self.drain_ignore_pdb[node] = ignore_pdb
            self.drain_log.append(f'DRAIN {node}')
            self.sequence_log.append((next(self._seq), f'DRAIN {node}'))
        return True

    def check_vm(self, host, vmid):
        pass  # dry-run only: report live VM status

    def check_network_mounts(self, host):
        self.mount_checks.append(host)   # dry-run only

    def list_volume_attachments_for_node(self, node):
        return []

    def get_ha_started_sids(self):
        return getattr(self, '_ha_started', [])

    def get_ha_resources(self):
        return getattr(self, '_ha_resources', [])

    def get_ha_groups(self):
        return getattr(self, '_ha_groups', {})

    def enable_node_maintenance(self, node):
        self.ha_log.append(f'NODE_MAINTENANCE {node}')

    def wait_ha_migrations_done(self, node, timeout):
        self.ha_log.append(f'WAIT_MIGRATIONS {node}')
        return True

    def release_ha_sid(self, sid):
        entry = f'RELEASE_HA {sid}'
        with self._lock:
            self.ha_log.append(entry)
            self.sequence_log.append((next(self._seq), entry))

    def wait_ha_released(self, sids, timeout=30):
        entry = f'WAIT_HA_RELEASED {" ".join(sids)}'
        with self._lock:
            self.ha_log.append(entry)
            self.sequence_log.append((next(self._seq), entry))
        return []

    def set_ceph_flags(self, flags):
        with self._lock:
            self.ceph_log.append(f'CEPH_FLAGS {" ".join(flags)}')
            self.sequence_log.append((next(self._seq), f'CEPH_FLAGS {" ".join(flags)}'))

    def get_osds_for_hosts(self, hosts):
        # Fixed mapping for tests: pve1 → [0,1], pve2 → [2,5], pve3 → [3,4]
        osd_map = {'pve1': ['0', '1'], 'pve2': ['2', '5'], 'pve3': ['3', '4']}
        osd_ids = []
        for h in hosts:
            osd_ids.extend(osd_map.get(h, []))
        return osd_ids

    def set_osd_noout(self, osd_ids):
        entry = f'OSD_NOOUT {" ".join(osd_ids)}'
        with self._lock:
            self.ceph_log.append(entry)
            self.sequence_log.append((next(self._seq), entry))

    def dispatch_local_shutdown(self, host, workloads, timeout_vm,
                                poweroff_delay=None, dry_run=False):
        vmids = [vmid for _, vmid in workloads]
        entry = f'LOCAL_SHUTDOWN {host} vmids={",".join(sorted(vmids))}'
        with self._lock:
            self.shutdown_log.append(entry)
            self.sequence_log.append((next(self._seq), entry))
        # Kill the fake VM processes so the polling loop sees them as stopped
        for vmid in vmids:
            pid_file = self._run_dir / f'{vmid}.pid'
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
                except (ValueError, ProcessLookupError):
                    pass
                pid_file.unlink()

    def release_network_mounts(self, host):
        entry = f'RELEASE_MOUNTS {host}'
        with self._lock:
            self.sequence_log.append((next(self._seq), entry))

    def poweroff_host(self, host):
        self.poweroff_log.append(f'POWEROFF {host}')

    def poweroff_self(self):
        self.poweroff_log.append('POWEROFF_SELF')
