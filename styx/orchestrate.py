"""styx.orchestrate — Main shutdown sequence."""

import concurrent.futures
import json
import os
import socket
import subprocess
import time

from styx.classify import other_vmids
from styx.config import StyxConfig, load_config
from styx.decide import (
    should_disable_ha, should_run_polling,
    should_poweroff_hosts, should_set_ceph_flags,
)
from styx.discover import (
    ClusterTopology, parse_cluster_status, parse_cluster_resources,
    match_nodes_to_vms,
)
from styx import __version__
from styx.policy import Policy, DryRunPolicy, MaintenancePolicy, log, setup_log_file
from styx.wrappers import Operations, _local_pyz


# ── external CLI helpers ──────────────────────────────────────────────────────

def _pvesh(*args):
    r = subprocess.run(
        ['pvesh', 'get'] + list(args) + ['--output-format', 'json'],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(r.stdout)


def _pveceph_available():
    try:
        subprocess.run(
            ['pveceph', 'status'], capture_output=True, timeout=5,
        ).check_returncode()
        return True
    except Exception:
        return False


def _make_k8s_client(config):
    from styx.k8s import K8sClient
    with open(config.k8s_token) as f:
        token = f.read().strip()
    return K8sClient(config.k8s_server, token, config.k8s_ca_cert or None)


# ── discovery ─────────────────────────────────────────────────────────────────

def discover(config, *, _pvesh_fn=None, _pveceph_fn=None, _on_warning=None):
    """Build ClusterTopology from config + live cluster API calls.

    _pvesh_fn and _pveceph_fn are injectable for testing.
    """
    pvesh     = _pvesh_fn     or _pvesh
    pveceph   = _pveceph_fn   or _pveceph_available
    topo      = ClusterTopology()

    # Hosts
    if config.hosts:
        topo.host_ips    = dict(config.hosts)
        topo.orchestrator = config.orchestrator or socket.gethostname().split('.')[0]
        log('Hosts: using config override')
    else:
        try:
            topo.host_ips, topo.orchestrator = parse_cluster_status(pvesh('/cluster/status'))
        except Exception as e:
            raise RuntimeError(f'Failed to discover hosts via pvesh /cluster/status: {e}') from e
    if config.orchestrator:
        topo.orchestrator = config.orchestrator
    log(f'Orchestrator: {topo.orchestrator}')
    host_lines = [f'  {h} ({ip})' for h, ip in sorted(topo.host_ips.items())]
    log('Hosts:\n' + '\n'.join(host_lines))

    # VMs
    try:
        topo.vm_host, topo.vm_name, topo.vm_type, topo.vm_lock = parse_cluster_resources(
            pvesh('/cluster/resources', '--type', 'vm')
        )
        by_host = {}
        for vmid, host in topo.vm_host.items():
            by_host.setdefault(host, []).append(vmid)
        vm_lines = [f'  {h}: {" ".join(vms)}' for h, vms in sorted(by_host.items())]
        log('Running VMs:\n' + '\n'.join(vm_lines))
    except Exception as e:
        (_on_warning or log)(f'VM discovery failed ({e}) — proceeding with empty VM list')
        topo.vm_host, topo.vm_name, topo.vm_type, topo.vm_lock = {}, {}, {}, {}

    # Kubernetes — config override, then API auto-discovery
    if config.workers or config.control_plane:
        topo.k8s_workers  = list(config.workers)
        topo.k8s_cp       = list(config.control_plane)
        topo.k8s_enabled  = True
        log('Kubernetes: config override\n'
            f'  workers: {" ".join(topo.k8s_workers)}\n'
            f'  control-plane: {" ".join(topo.k8s_cp)}')
    elif config.k8s_server and config.k8s_token:
        try:
            k8s        = _make_k8s_client(config)
            node_roles = k8s.get_node_roles()
            topo.k8s_workers, topo.k8s_cp = match_nodes_to_vms(topo.vm_name, node_roles)
            topo.k8s_enabled = True
            log('Kubernetes: API discovery\n'
                f'  workers: {" ".join(topo.k8s_workers)}\n'
                f'  control-plane: {" ".join(topo.k8s_cp)}')
        except ValueError as e:
            (_on_warning or log)(f'Kubernetes node/VM name mismatch: {e}')
            topo.k8s_enabled = False
        except Exception as e:
            log(f'Kubernetes API unreachable ({e}) — skipping k8s')
            topo.k8s_enabled = False
    else:
        log('No [kubernetes] credentials configured — skipping k8s')
        topo.k8s_enabled = False

    # Ceph
    if config.ceph_enabled is not None:
        topo.ceph_enabled = config.ceph_enabled
    else:
        topo.ceph_enabled = pveceph()
    log(f'Ceph enabled: {topo.ceph_enabled}')

    return topo


def _refresh_vm_topology(topo, *, _pvesh_fn=None):
    """Re-fetch VM placement from Proxmox API and update topo in-place.

    Returns True on success, False on failure (stale data kept).
    """
    pvesh = _pvesh_fn or _pvesh
    try:
        vm_host, vm_name, vm_type, vm_lock = parse_cluster_resources(
            pvesh('/cluster/resources', '--type', 'vm')
        )
        topo.vm_host = vm_host
        topo.vm_name = vm_name
        topo.vm_type = vm_type
        topo.vm_lock = vm_lock
        return True
    except Exception as e:
        log(f'VM topology refresh failed ({e}) — using stale data')
        return False


def _try_refresh(topo, args, *, _pvesh_fn=None):
    """Refresh VM topology; re-apply hosts filter if needed."""
    if _refresh_vm_topology(topo, _pvesh_fn=_pvesh_fn):
        if args.hosts:
            _apply_hosts_filter(topo, args.hosts, quiet=True)
        by_host = {}
        for vmid, host in topo.vm_host.items():
            by_host.setdefault(host, []).append(vmid)
        lines = [f'  {h}: {" ".join(vms)}' for h, vms in sorted(by_host.items())]
        log('Refreshed VM topology:\n' + '\n'.join(lines))
    else:
        log('Proceeding with stale VM topology')


# Fixed escalation overhead in vm_shutdown.py: SIGTERM(10s) + SIGKILL(5s)
_VM_ESCALATION_OVERHEAD = 15


# ── hosts filter ─────────────────────────────────────────────────────────────

def _apply_hosts_filter(topo, hosts, quiet=False):
    """Restrict topo to only the given hosts.

    Orchestrator is always kept in host_ips (needed for SSH/polling) but its
    VMs are only scheduled for shutdown if it is explicitly listed in hosts.
    """
    shutdown_hosts = set(hosts)
    unknown = shutdown_hosts - set(topo.host_ips)
    if unknown:
        log(f'WARNING: --hosts filter references unknown host(s): '
            f'{", ".join(sorted(unknown))}')
    reachable = shutdown_hosts | {topo.orchestrator}
    topo.host_ips    = {h: ip for h, ip in topo.host_ips.items() if h in reachable}
    topo.vm_host     = {v: h  for v, h  in topo.vm_host.items()  if h in shutdown_hosts}
    topo.vm_name     = {v: n  for v, n  in topo.vm_name.items()  if v in topo.vm_host}
    topo.vm_type     = {v: t  for v, t  in topo.vm_type.items()  if v in topo.vm_host}
    topo.vm_lock     = {v: l  for v, l  in topo.vm_lock.items()  if v in topo.vm_host}
    topo.k8s_workers = [v for v in topo.k8s_workers if v in topo.vm_host]
    topo.k8s_cp      = [v for v in topo.k8s_cp      if v in topo.vm_host]
    if not topo.k8s_workers and not topo.k8s_cp:
        topo.k8s_enabled = False
    if not quiet:
        log(f'--hosts filter: shutting down {" ".join(sorted(shutdown_hosts))} '
            f'({len(topo.vm_host)} VM(s))')
    return topo


# ── pre-flight (maintenance mode) ────────────────────────────────────────────

def preflight(topo, config, policy):
    """Check SSH reachability, styx version, k8s API, Ceph health, and quorum.

    Collects all failures and calls policy.on_preflight_failure() once at the
    end if any exist.  Emergency mode warns and continues; dry-run and
    maintenance modes abort.
    """
    log('--- Pre-flight ---')
    failures = []

    # ── SSH reachability ──────────────────────────────────────────────────
    reachable = set()
    for host, ip in topo.host_ips.items():
        if host == topo.orchestrator:
            continue
        try:
            subprocess.run(
                ['ssh', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes',
                 f'root@{ip}', 'exit'],
                capture_output=True, timeout=10, check=True,
            )
            log(f'SSH {host} ({ip}): OK')
            reachable.add((host, ip))
        except Exception as e:
            log(f'SSH {host} ({ip}): UNREACHABLE ({e})')
            failures.append(f'SSH unreachable: {host}')

    # ── styx version check — only when running as a zipapp ────────────────
    if _local_pyz():
        for host, ip in reachable:
            cmd = f'python3 {_local_pyz()} --version'
            try:
                r = subprocess.run(
                    ['ssh', '-o', 'ConnectTimeout=5', '-o', 'BatchMode=yes',
                     f'root@{ip}', cmd],
                    capture_output=True, text=True, timeout=30, check=True,
                )
                remote_version = r.stdout.strip()
                if remote_version == __version__:
                    log(f'styx {host}: OK (v{remote_version})')
                else:
                    log(f'styx {host}: VERSION MISMATCH '
                        f'(local={__version__}, remote={remote_version})')
                    failures.append(f'styx version mismatch: {host}')
            except Exception as e:
                log(f'styx {host}: NOT AVAILABLE ({e})')
                failures.append(f'styx not available: {host}')

    # ── VM migration ───────────────────────────────────────────────────
    migrating = [f'{vmid} ({topo.vm_name.get(vmid, vmid)})'
                 for vmid, lock in topo.vm_lock.items() if lock == 'migrate']
    if migrating:
        log(f'VMs migrating: {" ".join(migrating)}')
        failures.append(f'VM migration in progress: {" ".join(migrating)}')

    # ── Kubernetes health ─────────────────────────────────────────────────
    if topo.k8s_enabled and config.k8s_server and config.k8s_token:
        try:
            k8s    = _make_k8s_client(config)
            nodes  = k8s.list_nodes()
            items  = nodes.get('items', [])
            n      = len(items)
            log(f'k8s API: OK ({n} nodes)')
            not_ready = []
            for item in items:
                name = item.get('metadata', {}).get('name', '<unknown>')
                conditions = item.get('status', {}).get('conditions', [])
                ready = any(
                    c.get('type') == 'Ready' and c.get('status') == 'True'
                    for c in conditions
                )
                if not ready:
                    not_ready.append(name)
            if not_ready:
                log(f'k8s NotReady nodes: {" ".join(not_ready)}')
                failures.append(f'k8s NotReady nodes: {" ".join(not_ready)}')
            for vmid in topo.k8s_workers + topo.k8s_cp:
                node = topo.vm_name.get(vmid, vmid)
                try:
                    pods      = k8s.list_pods_on_node(node)['items']
                    drainable = sum(1 for p in pods if k8s._drainable(p))
                    log(f'  {node}: {drainable} pod(s) to evict')
                except Exception:
                    pass
        except Exception as e:
            log(f'k8s API: UNREACHABLE ({e})')
            failures.append(f'k8s API unreachable: {e}')

    # ── Ceph health ───────────────────────────────────────────────────────
    if topo.ceph_enabled:
        try:
            r = subprocess.run(
                ['ceph', 'health'], capture_output=True, text=True, timeout=10,
            )
            status = r.stdout.strip() or r.stderr.strip()
            log(f'Ceph: {status}')
            if not status.startswith('HEALTH_OK'):
                failures.append(f'Ceph not healthy: {status}')
        except Exception as e:
            log(f'Ceph: unavailable ({e})')
            failures.append(f'Ceph unavailable: {e}')

    # ── Proxmox quorum ────────────────────────────────────────────────────
    try:
        r = subprocess.run(
            ['pvecm', 'status'], capture_output=True, text=True, timeout=10,
        )
        quorate = None
        for line in r.stdout.splitlines():
            if line.strip().startswith('Quorate:'):
                quorate = line.split(':', 1)[1].strip()
                break
        if quorate is None:
            log('Quorum: could not parse pvecm status')
            failures.append('Quorum: could not parse pvecm status')
        elif quorate.lower().startswith('yes'):
            log('Quorum: OK')
        else:
            log(f'Quorum: NOT quorate ({quorate})')
            failures.append(f'Quorum lost: {quorate}')
    except Exception as e:
        log(f'Quorum: pvecm unavailable ({e})')
        failures.append(f'Quorum check failed: {e}')

    # ── Report ────────────────────────────────────────────────────────────
    if failures:
        summary = '; '.join(failures)
        policy.on_preflight_failure(
            f'{len(failures)} preflight failure(s): {summary}'
        )


def _log_runtime_budget(topo, config, phase):
    """Calculate and display worst-case runtime budget."""
    log('--- Runtime budget (worst case) ---')

    total = 0
    has_k8s = topo.k8s_enabled and (topo.k8s_workers or topo.k8s_cp)
    has_vms = bool(topo.vm_host)

    if has_k8s:
        log(f'  k8s drain (all nodes parallel): {config.timeout_drain}s')
        total += config.timeout_drain

    if has_vms and phase >= 2:
        vm_s = config.timeout_vm + _VM_ESCALATION_OVERHEAD
        log(f'  VM shutdown + escalation: {vm_s}s')
        total += vm_s
        poll_s = int(os.environ.get('STYX_POLL_INTERVAL', '10'))
        log(f'  Polling detection: {poll_s}s')
        total += poll_s
    elif has_vms:
        log(f'  VM shutdown: fire-and-forget (background, not awaited in phase 1)')

    mins, secs = divmod(total, 60)
    log(f'  Total: {mins}m {secs:02d}s')


# ── HA ────────────────────────────────────────────────────────────────────────

def _disable_ha(topo, ops, policy, scope):
    target = (
        set(topo.k8s_workers + topo.k8s_cp) if scope == 'k8s'
        else set(topo.vm_host)   # 'all' — only VMs we're actually shutting down
    )
    sids = [sid for sid in ops.get_ha_started_sids()
            if (sid.split(':', 1)[-1] if ':' in sid else sid) in target]
    if not sids:
        log('No HA resources to disable')
        return
    log(f'--- Disabling HA: {" ".join(sids)} ---')
    for sid in sids:
        try:
            policy.execute(f'disable_ha_sid {sid}', ops.disable_ha_sid, sid)
            if not policy.dry_run:
                if not ops.wait_ha_disabled(sid):
                    policy.on_warning(f'HA transition timed out for {sid}')
        except Exception as e:
            policy.on_warning(f'failed to disable HA for {sid}: {e}')


# ── per-VM actions ────────────────────────────────────────────────────────────

def _drain_only(vmid, node, host, config, ops, policy):
    log(f'Draining: {node} (VM {vmid} on {host})')
    ok = policy.execute(f'drain {node}', ops.drain_node, node, config.timeout_drain)
    if ok is None:        # dry-run
        ops.check_vm(host, vmid)
        return
    if not ok:
        policy.on_warning(f'drain timed out or failed for {node}')
    else:
        log(f'Drained: {node}')
        stale = ops.list_volume_attachments_for_node(node)
        if stale:
            policy.on_warning(
                f'stale VolumeAttachments after drain of {node}: {", ".join(stale)}'
            )


# ── coordinated phase helpers ────────────────────────────────────────────────

def _drain_all_k8s(topo, config, ops, policy):
    """Drain all k8s nodes (workers + CP) in parallel. No VM shutdown."""
    if not topo.k8s_enabled or (not topo.k8s_workers and not topo.k8s_cp):
        return

    log('--- Draining k8s nodes ---')

    cp_set = set(topo.k8s_cp)

    with concurrent.futures.ThreadPoolExecutor() as ex:
        futs = {}
        for vmid in topo.k8s_workers + topo.k8s_cp:
            futs[ex.submit(
                _drain_only,
                vmid, topo.vm_name.get(vmid, vmid), topo.vm_host[vmid],
                config, ops, policy,
            )] = vmid

        for fut in concurrent.futures.as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                vmid = futs[fut]
                label = 'cp' if vmid in cp_set else 'worker'
                policy.on_warning(f'{label} {vmid}: {e}')

    log('All drains complete')


# ── independent phase ────────────────────────────────────────────────────────

def _dispatch_independent_phase(topo, config, ops, policy, do_poweroff,
                                vm_filter=None):
    """Dispatch local-shutdown to each host.

    Groups VMIDs by host, sends one local-shutdown command per host.
    Peers get an autonomous poweroff deadline as a leader-dead fallback.
    The orchestrator gets no deadline — it powers off after the polling loop.

    vm_filter: if set, only include these VMIDs (e.g. k8s-only for phase 1).
    """
    # Group workloads by host as (type, vmid) tuples
    by_host = {}
    for vmid, host in topo.vm_host.items():
        if vm_filter is not None and vmid not in vm_filter:
            continue
        wtype = topo.vm_type.get(vmid, 'qemu')
        by_host.setdefault(host, []).append((wtype, vmid))

    vm_lines = [f'  {h}: {" ".join(vid for _, vid in wl)}'
                for h, wl in sorted(by_host.items())]
    log('--- Shutting down VMs ---\n' + '\n'.join(vm_lines))

    # Calculate poweroff_delay for peers (autonomous fallback)
    poweroff_delay = None
    if do_poweroff:
        poweroff_delay = config.timeout_vm + _VM_ESCALATION_OVERHEAD

    # Dispatch to all hosts (peers get poweroff_delay, orchestrator does not)
    for host, workloads in by_host.items():
        is_orch = (host == topo.orchestrator)
        delay = None if is_orch else poweroff_delay
        if policy.dry_run:
            for _wtype, vmid in workloads:
                ops.check_vm(host, vmid)
        else:
            ops.dispatch_local_shutdown(
                host, workloads, config.timeout_vm,
                poweroff_delay=delay, dry_run=policy.dry_run,
            )


# ── polling loop ──────────────────────────────────────────────────────────────

_SSH_MAX_FAILURES = 3


def run_polling_loop(topo, ops, policy, do_poweroff, poll_interval=None,
                     timeout=None):
    if poll_interval is None:
        poll_interval = int(os.environ.get('STYX_POLL_INTERVAL', '10'))

    log(f'--- Polling loop (poweroff={do_poweroff}) ---')

    if policy.dry_run:
        for host in topo.host_ips:
            if host != topo.orchestrator:
                log(f'[dry-run] would poweroff_host {host}')
        return

    deadline = (time.monotonic() + timeout) if timeout is not None else None
    ssh_failures = {h: 0 for h in topo.host_ips}
    powered_off = {topo.orchestrator}
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            log('WARNING: polling loop global timeout expired — breaking')
            break

        all_done = True
        for host in topo.host_ips:
            if host in powered_off and host != topo.orchestrator:
                continue
            try:
                running = set(ops.get_running_vmids(host))
                ssh_failures[host] = 0
            except Exception as e:
                ssh_failures[host] += 1
                if ssh_failures[host] >= _SSH_MAX_FAILURES:
                    log(f'WARNING: {host}: {ssh_failures[host]} consecutive SSH failures '
                        f'— treating as stopped')
                    if host == topo.orchestrator:
                        log('WARNING: orchestrator SSH exhausted — breaking')
                        break
                    if host not in powered_off:
                        if do_poweroff:
                            log(f'Host {host}: SSH unreachable — powering off')
                            ops.poweroff_host(host)
                        powered_off.add(host)
                else:
                    log(f'WARNING: {host}: SSH failure ({e}) — retry '
                        f'{ssh_failures[host]}/{_SSH_MAX_FAILURES}')
                    all_done = False
                continue
            host_vms = {v for v, h in topo.vm_host.items() if h == host}
            still    = host_vms & running
            if still:
                all_done = False
                log(f'Host {host}: VMs still running ({" ".join(still)})')
            elif host != topo.orchestrator and host not in powered_off:
                if do_poweroff:
                    log(f'Host {host}: all VMs stopped — powering off')
                    ops.poweroff_host(host)
                powered_off.add(host)
        else:
            # for-loop completed without break — check orchestrator
            peers_done = all(h in powered_off for h in topo.host_ips
                             if h != topo.orchestrator)
            if all_done or peers_done:
                try:
                    orch_running = set(ops.get_running_vmids(topo.orchestrator))
                    ssh_failures[topo.orchestrator] = 0
                except Exception as e:
                    ssh_failures[topo.orchestrator] += 1
                    if ssh_failures[topo.orchestrator] >= _SSH_MAX_FAILURES:
                        log('WARNING: orchestrator SSH exhausted — breaking')
                        break
                    log(f'WARNING: {topo.orchestrator}: SSH failure ({e}) — retry '
                        f'{ssh_failures[topo.orchestrator]}/{_SSH_MAX_FAILURES}')
                    time.sleep(poll_interval)
                    continue
                orch_vms = {v for v, h in topo.vm_host.items()
                            if h == topo.orchestrator}
                if not (orch_vms & orch_running):
                    log('All VMs stopped (including orchestrator)')
                    break
                log(f'Waiting for orchestrator VMs: {orch_vms & orch_running}')
                all_done = False

            time.sleep(poll_interval)
            continue

        # for-loop hit break (orchestrator SSH exhausted) — exit polling
        break


# ── revert summary (partial runs) ────────────────────────────────────────────

def _log_startup_checklist(topo, ceph_flags_set, osd_noout_ids=None):
    """Log steps to run after bringing a fully-shutdown cluster back up."""
    if osd_noout_ids is None:
        osd_noout_ids = []
    items = []

    if osd_noout_ids:
        osd_list = ' '.join(f'osd.{i}' for i in osd_noout_ids)
        cmds = '  '.join(f'ceph osd rm-noout osd.{i}' for i in osd_noout_ids)
        items.append((f'Per-OSD noout set: {osd_list}',
                      cmds))
    elif ceph_flags_set:
        flags = ' '.join(ceph_flags_set)
        items.append((f'Ceph OSD flags set: {flags}',
                      f'(after Ceph healthy) ceph osd unset {flags}'))

    k8s_nodes = [topo.vm_name.get(v, v) for v in topo.k8s_workers + topo.k8s_cp]
    if k8s_nodes:
        items.append((f'k8s nodes cordoned: {" ".join(k8s_nodes)}',
                      f'(after k8s API up) kubectl uncordon {" ".join(k8s_nodes)}'))

    if not items:
        return

    log('--- Shutdown complete — startup checklist ---')
    for what, cmd in items:
        log(f'  {what}')
        log(f'    → {cmd}')


def _log_revert_summary(topo, args, ceph_flags_set, osd_noout_ids=None):
    """Log a checklist of manual steps needed to restore normal cluster state.

    Called at the end of every --hosts run (skip in dry-run: nothing changed).
    """
    if osd_noout_ids is None:
        osd_noout_ids = []
    log('--- Partial run complete — revert checklist ---')

    if osd_noout_ids:
        osd_list = ' '.join(f'osd.{i}' for i in osd_noout_ids)
        log(f'  Per-OSD noout set: {osd_list}')
        for i in osd_noout_ids:
            log(f'    → ceph osd rm-noout osd.{i}')
    elif ceph_flags_set:
        flags = ' '.join(ceph_flags_set)
        log(f'  Ceph OSD flags set: {flags}')
        log(f'    → ceph osd unset {flags}')

    k8s_nodes = [topo.vm_name.get(v, v) for v in topo.k8s_workers + topo.k8s_cp]
    if k8s_nodes:
        log(f'  k8s nodes cordoned: {" ".join(k8s_nodes)}')
        log(f'    → kubectl uncordon {" ".join(k8s_nodes)}')

    vmids = sorted(topo.vm_host)
    if vmids:
        qemu_ids = sorted(v for v in vmids if topo.vm_type.get(v, 'qemu') == 'qemu')
        start_cmds = []
        if qemu_ids:
            start_cmds.append(f'qm start {" ".join(qemu_ids)}')
        if args.skip_poweroff:
            log(f'  VM(s) stopped (host NOT powered off): {" ".join(vmids)}')
            for cmd in start_cmds:
                log(f'    → {cmd}')
        else:
            peers = [h for h in topo.host_ips if h != topo.orchestrator]
            log(f'  Host(s) powered off: {" ".join(peers)}')
            for cmd in start_cmds:
                log(f'    → power on host(s), then {cmd}  (if not HA-managed)')


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv=None, *, _discover_fn=None, _ops_factory=None, _preflight_fn=None):
    """Entry point for `styx orchestrate`.

    _discover_fn(config) -> ClusterTopology  — injectable for testing
    _ops_factory(topo, config) -> Operations — injectable for testing
    _preflight_fn(topo, config, policy)      — injectable for testing
    """
    import argparse
    import sys as _sys

    p = argparse.ArgumentParser(description='styx — graceful cluster shutdown')
    p.add_argument('--phase',  type=int, choices=[1, 2, 3], default=3)
    p.add_argument('--config', default=None)
    p.add_argument('--mode',   choices=['dry-run', 'emergency', 'maintenance'],
                   default='emergency')
    p.add_argument('--hosts', metavar='HOST', nargs='+',
                   help='Restrict to these hosts only (orchestrator always included)')
    p.add_argument('--skip-poweroff', action='store_true',
                   help='Shut down VMs but do not power off any host')
    args = p.parse_args(argv)

    setup_log_file(os.environ.get('LOG_FILE', '/var/log/styx.log'))

    if args.mode == 'dry-run':
        policy = DryRunPolicy()
    elif args.mode == 'maintenance':
        policy = MaintenancePolicy()
    else:
        policy = Policy()

    # ── Config path resolution ────────────────────────────────────────────
    config_explicit = args.config is not None
    if config_explicit:
        config_path = args.config
    else:
        pyz = _local_pyz()
        if pyz:
            config_path = os.path.join(os.path.dirname(pyz), 'styx.conf')
        else:
            config_path = '/etc/styx/styx.conf'

    if config_explicit and not os.path.isfile(config_path):
        policy.on_preflight_failure(f'Config file not found: {config_path}')
    elif not config_explicit and not os.path.isfile(config_path):
        log(f'No config file at {config_path} — using defaults')

    config = load_config(config_path)

    log('=' * 40)
    log('styx run started')
    log('=' * 40)
    log(f'Mode: {args.mode}, Phase: {args.phase}')

    if _discover_fn is not None:
        topo = _discover_fn(config)
    else:
        try:
            topo = discover(config, _on_warning=policy.on_warning)
        except Exception as e:
            log(f'FATAL: {e}')
            _sys.exit(1)

    if args.hosts:
        _apply_hosts_filter(topo, args.hosts)

    pf = _preflight_fn if _preflight_fn is not None else preflight
    pf(topo, config, policy)
    _log_runtime_budget(topo, config, args.phase)
    type_counts = {}
    for vt in topo.vm_type.values():
        type_counts[vt] = type_counts.get(vt, 0) + 1
    workload_summary = ', '.join(f'{n} {t}' for t, n in sorted(type_counts.items()))
    policy.phase_gate(
        f'{len(topo.host_ips)} host(s), {len(topo.vm_host)} workload(s)'
        + (f' ({workload_summary})' if workload_summary else '')
        + (f', k8s workers={topo.k8s_workers} cp={topo.k8s_cp}' if topo.k8s_enabled else '')
        + ' — proceed with shutdown?'
    )

    if _ops_factory is not None:
        ops = _ops_factory(topo, config)
    else:
        k8s = None
        if config.k8s_server and config.k8s_token:
            try:
                k8s = _make_k8s_client(config)
            except Exception as e:
                policy.on_warning(f'Failed to create k8s client: {e}')
        ops = Operations(topo.host_ips, topo.orchestrator, k8s)

    # ── COORDINATED PHASE ─────────────────────────────────────────────────

    # HA — disable before cordon to close the window where HA could
    # migrate a VM onto the target host between preflight and disable.
    if should_disable_ha(args.phase):
        _disable_ha(topo, ops, policy, 'all')
    elif topo.k8s_enabled:
        _disable_ha(topo, ops, policy, 'k8s')

    # Cordon all k8s nodes (idempotent)
    if topo.k8s_enabled:
        log('--- Cordoning k8s nodes ---')
        for vmid in topo.k8s_workers + topo.k8s_cp:
            node = topo.vm_name.get(vmid, vmid)
            log(f'Cordoning: {node}')
            try:
                policy.execute(f'cordon {node}', ops.cordon_node, node)
            except Exception as e:
                policy.on_warning(f'cordon failed for {node}: {e}')

    # Drain all k8s nodes (workers + CP) in parallel — no VM shutdown
    _drain_all_k8s(topo, config, ops, policy)

    # Phase 1: dispatch for k8s VMs only, no poll, return
    if not should_run_polling(args.phase):
        _try_refresh(topo, args)
        k8s_vmids = set(topo.k8s_workers + topo.k8s_cp)
        _dispatch_independent_phase(topo, config, ops, policy,
                                    do_poweroff=False, vm_filter=k8s_vmids)
        log(f'Phase {args.phase} complete')
        return

    # ── PHASE GATE ────────────────────────────────────────────────────────

    do_poweroff = should_poweroff_hosts(args.phase) and not args.skip_poweroff
    if do_poweroff:
        ceph_note = ', set Ceph flags' if topo.ceph_enabled else ''
        policy.phase_gate(
            f'Drains complete — about to{ceph_note} dispatch shutdown'
            f' with autonomous poweroff. Proceed?'
        )

    # ── INDEPENDENT PHASE ─────────────────────────────────────────────────

    # Ceph flags (phase 3 only, before dispatch).
    # Partial runs set per-OSD noout on only the target hosts' OSDs;
    # full-cluster runs set global flags (noout + norebalance etc.).
    ceph_flags = []
    osd_noout_ids = []
    if should_set_ceph_flags(args.phase) and topo.ceph_enabled:
        if args.hosts:
            osd_noout_ids = ops.get_osds_for_hosts(args.hosts)
            if osd_noout_ids:
                osd_list = ' '.join(f'osd.{i}' for i in osd_noout_ids)
                log(f'--- Setting per-OSD noout: {osd_list} ---')
                policy.execute('set_osd_noout', ops.set_osd_noout, osd_noout_ids)
            else:
                log('WARNING: no OSDs found for target hosts — skipping per-OSD noout')
        else:
            ceph_flags = config.ceph_flags
            log(f'--- Setting Ceph OSD flags: {" ".join(ceph_flags)} ---')
            policy.execute('set_ceph_flags', ops.set_ceph_flags, ceph_flags)

    # Refresh VM topology right before dispatch (VMs may have migrated during drains)
    _try_refresh(topo, args)

    # Dispatch local-shutdown to each host (one SSH per peer)
    _dispatch_independent_phase(topo, config, ops, policy, do_poweroff)

    poll_timeout = config.timeout_vm + _VM_ESCALATION_OVERHEAD + 30
    run_polling_loop(topo, ops, policy, do_poweroff, timeout=poll_timeout)

    # Log completion notes before potentially going dark (poweroff flushes nothing).
    if not policy.dry_run:
        if args.hosts:
            _log_revert_summary(topo, args, ceph_flags, osd_noout_ids=osd_noout_ids)
        else:
            _log_startup_checklist(topo, ceph_flags, osd_noout_ids=osd_noout_ids)

    # Power off orchestrator only on full runs or when explicitly targeted.
    poweroff_self = do_poweroff and (not args.hosts or topo.orchestrator in args.hosts)
    if poweroff_self:
        log('Powering off orchestrator (self)')
        policy.execute('poweroff_self', ops.poweroff_self)
    elif args.skip_poweroff:
        log('All VMs stopped — --skip-poweroff: hosts not powered off')
    else:
        log(f'Phase {args.phase} complete')
