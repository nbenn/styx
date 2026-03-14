"""styx.wrappers — Operations: thin wrappers around all external commands.

Inject a subclass or mock in tests to avoid real SSH / CLI calls.
"""

import json
import subprocess
import sys
import time

from styx.policy import log

# How long to wait for an HA resource to transition to 'disabled' after
# calling ha-manager set. Always times out with a warning rather than
# stalling the sequence.
_HA_TRANSITION_TIMEOUT = 30


_VM_LOG              = '/var/log/styx-vm-{vmid}.log'
_VM_LOG_GLOB         = '/var/log/styx-vm-*.log'
_LOCAL_SHUTDOWN_LOG   = '/var/log/styx-local-shutdown.log'


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


def _parse_osd_tree(data):
    """Parse ``ceph osd tree --format json`` into hostname → OSD-ID list.

    Returns dict[str, list[str]], e.g. ``{'pve2': ['2', '5', '8']}``.
    """
    nodes_by_id = {n['id']: n for n in data.get('nodes', [])}
    result = {}
    for node in data.get('nodes', []):
        if node.get('type') != 'host':
            continue
        hostname = node.get('name', '')
        osd_ids = [str(cid) for cid in node.get('children', [])
                   if nodes_by_id.get(cid, {}).get('type') == 'osd']
        result[hostname] = osd_ids
    return result


def _parse_ha_status(data):
    """Return SIDs in 'started' state from /cluster/ha/status/current JSON.

    Each entry has 'sid' (e.g. 'vm:100') and 'state' (e.g. 'started').
    """
    return [
        entry['sid']
        for entry in data
        if entry.get('state') == 'started' and 'sid' in entry
    ]


def _parse_ha_resources(data):
    """Parse /cluster/ha/resources JSON into list of resource dicts.

    Returns list of dicts with keys: sid, group (or ''), state, type.
    Only includes entries with state='started'.
    """
    return [
        {
            'sid': entry['sid'],
            'group': entry.get('group', ''),
            'state': entry['state'],
            'type': entry.get('type', 'vm'),
        }
        for entry in data
        if entry.get('state') == 'started' and 'sid' in entry
    ]


def _parse_ha_groups(data):
    """Parse /cluster/ha/groups JSON into {name: {nodes: set, restricted: bool}}.

    Proxmox returns nodes as a comma-separated string and restricted as 0/1.
    """
    return {
        entry['group']: {
            'nodes': set(entry.get('nodes', '').split(',')),
            'restricted': bool(entry.get('restricted', 0)),
        }
        for entry in data
        if 'group' in entry
    }


def _parse_ha_services_on_nodes(data, target_nodes):
    """Return SIDs of started HA services currently running on target_nodes.

    Uses the richer /cluster/ha/status/current entries (type='service')
    which include a 'node' field.
    """
    return [
        entry['sid']
        for entry in data
        if entry.get('type') == 'service'
        and entry.get('state') == 'started'
        and entry.get('node') in target_nodes
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
        out = self.run_on_host(host, (
            'for f in /var/run/qemu-server/*.pid; do '
            '  [ -f "$f" ] || continue; '
            '  pid=$(cat "$f"); '
            '  kill -0 "$pid" 2>/dev/null && basename "$f" .pid; '
            'done'
        ))
        return _parse_running_vmids(out)

    # ── VM lifecycle ──────────────────────────────────────────────────────────

    def _vm_prefix(self, host):
        """Command prefix for running styx on host."""
        return _styx_cmd()

    def check_vm(self, host, vmid):
        """Synchronously check VM status and log result. Used in dry-run mode."""
        cmd = f'{self._vm_prefix(host)} vm-shutdown {vmid} --dry-run'
        try:
            out = self.run_on_host(host, cmd)
            if out.strip():
                log(out.rstrip())
        except Exception as e:
            log(f'WARNING: check_vm {vmid} on {host}: {e}')

    def shutdown_vm(self, host, vmid, timeout):
        # Output goes to a per-VM log file on the host; collected by
        # poweroff_host() before the host is powered off.
        log_file = _VM_LOG.format(vmid=vmid)
        cmd = f'{self._vm_prefix(host)} vm-shutdown {vmid} {timeout}'
        try:
            self.run_on_host(host, f'nohup {cmd} </dev/null >{log_file} 2>&1 &')
        except Exception as e:
            log(f'WARNING: shutdown_vm {vmid} on {host}: {e}')

    def dispatch_local_shutdown(self, host, workloads, timeout_vm,
                                poweroff_delay=None, dry_run=False):
        """Dispatch a local-shutdown command to a peer host via SSH (nohup).

        workloads: list of (type, vmid) tuples, e.g. [('qemu', '101')].
        The peer will shut down all listed workloads in parallel and optionally
        power itself off after poweroff_delay seconds.
        """
        args = ' '.join(f'{wtype}:{vmid}' for wtype, vmid in workloads)
        cmd = f'{self._vm_prefix(host)} local-shutdown {args} --timeout {timeout_vm}'
        if poweroff_delay is not None:
            cmd += f' --poweroff-delay {poweroff_delay}'
        if dry_run:
            cmd += ' --dry-run'
        try:
            self.run_on_host(
                host,
                f'nohup {cmd} </dev/null >{_LOCAL_SHUTDOWN_LOG} 2>&1 &',
            )
        except Exception as e:
            log(f'WARNING: dispatch_local_shutdown to {host}: {e}')

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
                ['pvesh', 'get', '/cluster/ha/status/current',
                 '--output-format', 'json'],
                capture_output=True, text=True, check=True, timeout=10,
            )
            return _parse_ha_status(json.loads(r.stdout))
        except Exception:
            return []

    def get_ha_resources(self):
        """Return parsed HA resource list from /cluster/ha/resources."""
        r = subprocess.run(
            ['pvesh', 'get', '/cluster/ha/resources',
             '--output-format', 'json'],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return _parse_ha_resources(json.loads(r.stdout))

    def get_ha_groups(self):
        """Return parsed HA groups dict from /cluster/ha/groups."""
        r = subprocess.run(
            ['pvesh', 'get', '/cluster/ha/groups',
             '--output-format', 'json'],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return _parse_ha_groups(json.loads(r.stdout))

    def enable_node_maintenance(self, node):
        """Enable HA maintenance mode on a node."""
        subprocess.run(
            ['ha-manager', 'crm-command', 'node-maintenance', 'enable', node],
            check=True, timeout=10,
        )

    def wait_ha_migrations_done(self, node, timeout):
        """Poll until no started HA services remain on node. Returns True on success."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                r = subprocess.run(
                    ['pvesh', 'get', '/cluster/ha/status/current',
                     '--output-format', 'json'],
                    capture_output=True, text=True, check=True, timeout=10,
                )
                remaining = _parse_ha_services_on_nodes(
                    json.loads(r.stdout), {node},
                )
                if not remaining:
                    return True
                log(f'HA migrations pending on {node}: {" ".join(remaining)}')
            except Exception:
                pass
            time.sleep(5)
        return False

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
                    ['pvesh', 'get', '/cluster/ha/status/current',
                     '--output-format', 'json'],
                    capture_output=True, text=True, check=True, timeout=10,
                )
                for entry in json.loads(r.stdout):
                    if entry.get('sid') == sid and entry.get('state') == 'disabled':
                        return True
            except Exception:
                pass
            time.sleep(2)
        return False

    # ── Ceph ──────────────────────────────────────────────────────────────────

    def set_ceph_flags(self, flags):
        for flag in flags:
            subprocess.run(['ceph', 'osd', 'set', flag], check=True, timeout=10)

    def get_osds_for_hosts(self, hosts):
        """Return list of OSD ID strings for the given hostnames."""
        try:
            r = subprocess.run(
                ['ceph', 'osd', 'tree', '--format', 'json'],
                capture_output=True, text=True, check=True, timeout=30,
            )
            tree = _parse_osd_tree(json.loads(r.stdout))
            osd_ids = []
            for host in hosts:
                osd_ids.extend(tree.get(host, []))
            return osd_ids
        except Exception as e:
            log(f'WARNING: failed to get OSDs for hosts: {e}')
            return []

    def set_osd_noout(self, osd_ids):
        """Set noout flag on individual OSDs."""
        for osd_id in osd_ids:
            subprocess.run(
                ['ceph', 'osd', 'add-noout', f'osd.{osd_id}'],
                check=True, timeout=10,
            )

    # ── host power ────────────────────────────────────────────────────────────

    def poweroff_host(self, host):
        ip = self._host_ips[host]
        ssh_base = ['ssh', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes',
                    f'root@{ip}']
        # Schedule poweroff first so it happens even if log collection
        # fails or the SSH connection drops.
        try:
            subprocess.run(
                ssh_base + ['nohup sh -c "sleep 5; poweroff" </dev/null >/dev/null 2>&1 &'],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as e:
            log(f'WARNING: poweroff {host}: {e}')
            return
        # Collect vm-shutdown and local-shutdown logs before the host
        # goes down (best-effort, 25s budget before the poweroff fires).
        try:
            r = subprocess.run(
                ssh_base + [f'cat {_VM_LOG_GLOB} {_LOCAL_SHUTDOWN_LOG} 2>/dev/null'],
                capture_output=True, text=True, timeout=25,
            )
            if r.stdout.strip():
                log(f'shutdown log from {host}:\n{r.stdout.rstrip()}')
        except Exception as e:
            log(f'WARNING: log collection from {host}: {e}')

    def poweroff_self(self):
        subprocess.run(['poweroff'])
