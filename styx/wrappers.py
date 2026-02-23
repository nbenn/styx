"""styx.wrappers — Operations: thin wrappers around all external commands.

Inject a subclass or mock in tests to avoid real SSH / CLI calls.
"""

import subprocess
import sys
import time

from styx.policy import log

# How long to wait for an HA resource to transition to 'disabled' after
# calling ha-manager set. Always times out with a warning rather than
# stalling the sequence.
_HA_TRANSITION_TIMEOUT = 30


_REMOTE_PYZ = '/tmp/styx-deploy.pyz'


def _local_pyz():
    """Return sys.argv[0] if running as a zipapp, else None."""
    argv0 = sys.argv[0] if sys.argv else ''
    return argv0 if argv0.endswith('.pyz') else None


def _styx_cmd():
    """Return the invocation prefix for styx subcommands run in subprocesses.

    When running as a zipapp (sys.argv[0] ends with .pyz), the zipapp path is
    passed directly to python3.  Falls back to 'python3 -m styx' for
    development / source installs.
    """
    pyz = _local_pyz()
    return f'python3 {pyz}' if pyz else 'python3 -m styx'


def _parse_ha_status(output):
    """Return SIDs in 'started' state from ha-manager status output."""
    return [
        parts[0]
        for line in output.splitlines()
        for parts in [line.split()]
        if len(parts) >= 2 and parts[1] == 'started'
    ]


def _parse_running_vmids(output):
    """Return non-empty stripped lines from get_running_vmids shell output."""
    return [line.strip() for line in output.splitlines() if line.strip()]


class Operations:
    def __init__(self, host_ips, orchestrator, k8s=None):
        self._host_ips    = host_ips
        self._orchestrator = orchestrator
        self._k8s         = k8s

    # ── host execution ────────────────────────────────────────────────────────

    def run_on_host(self, host, cmd):
        if host == self._orchestrator:
            r = subprocess.run(['bash', '-c', cmd], capture_output=True, text=True, timeout=30)
        else:
            ip = self._host_ips[host]
            r  = subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes',
                 f'root@{ip}', cmd],
                capture_output=True, text=True, timeout=30,
            )
        r.check_returncode()
        return r.stdout

    def get_running_vmids(self, host):
        try:
            out = self.run_on_host(host, (
                'for f in /var/run/qemu-server/*.pid; do '
                '  [ -f "$f" ] || continue; '
                '  pid=$(cat "$f"); '
                '  kill -0 "$pid" 2>/dev/null && basename "$f" .pid; '
                'done'
            ))
            return _parse_running_vmids(out)
        except Exception:
            return []

    # ── VM lifecycle ──────────────────────────────────────────────────────────

    def push_executable(self, host):
        """Copy the running .pyz to host via SSH stdin pipe.

        No-op in dev/source mode (when not running as a zipapp).
        """
        pyz = _local_pyz()
        if pyz is None:
            return
        ip = self._host_ips[host]
        with open(pyz, 'rb') as f:
            subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes',
                 f'root@{ip}', f'cat > {_REMOTE_PYZ}'],
                stdin=f, check=True, timeout=60,
            )

    def shutdown_vm(self, host, vmid, timeout):
        # Peers get the pre-deployed local copy; orchestrator uses _styx_cmd()
        # (which points at shared storage, safe since it runs first and imports
        # everything before CephFS can lose quorum).
        if _local_pyz() and host != self._orchestrator:
            prefix = f'python3 {_REMOTE_PYZ}'
        else:
            prefix = _styx_cmd()
        cmd = f'{prefix} vm-shutdown {vmid} {timeout}'
        try:
            self.run_on_host(host, f'nohup {cmd} </dev/null >/dev/null 2>&1 &')
        except Exception as e:
            log(f'WARNING: shutdown_vm {vmid} on {host}: {e}')

    # ── Kubernetes ────────────────────────────────────────────────────────────

    def cordon_node(self, node):
        if self._k8s is None:
            log(f'WARNING: no k8s client configured, cannot cordon {node}')
            return
        self._k8s.cordon(node)

    def drain_node(self, node, timeout):
        if self._k8s is None:
            log(f'WARNING: no k8s client configured, cannot drain {node}')
            return False
        return self._k8s.drain(node, timeout)

    def list_volume_attachments_for_node(self, node):
        if self._k8s is None:
            return []
        try:
            return [name for name, n in self._k8s.list_volume_attachments() if n == node]
        except Exception:
            return []

    # ── Proxmox HA ────────────────────────────────────────────────────────────

    def get_ha_started_sids(self):
        try:
            r = subprocess.run(
                ['ha-manager', 'status'],
                capture_output=True, text=True, timeout=10,
            )
            return _parse_ha_status(r.stdout)
        except Exception:
            return []

    def disable_ha_sid(self, sid):
        subprocess.run(
            ['ha-manager', 'set', sid, '--state', 'disabled'],
            check=True, timeout=10,
        )

    def wait_ha_disabled(self, sid, timeout=_HA_TRANSITION_TIMEOUT):
        """Wait for HA resource to reach 'disabled' state. Returns True on success."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                r = subprocess.run(
                    ['ha-manager', 'status'],
                    capture_output=True, text=True, timeout=10,
                )
                for line in r.stdout.splitlines():
                    parts = line.split()
                    if parts and parts[0] == sid and len(parts) >= 2 and parts[1] == 'disabled':
                        return True
            except Exception:
                pass
            time.sleep(2)
        return False

    # ── Ceph ──────────────────────────────────────────────────────────────────

    def set_ceph_flags(self, flags):
        for flag in flags:
            subprocess.run(['ceph', 'osd', 'set', flag], check=True, timeout=10)

    # ── host power ────────────────────────────────────────────────────────────

    def poweroff_host(self, host):
        ip = self._host_ips[host]
        try:
            subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes',
                 f'root@{ip}', 'poweroff'],
                timeout=10,
            )
        except Exception as e:
            log(f'WARNING: poweroff {host}: {e}')

    def poweroff_self(self):
        subprocess.run(['poweroff'])
