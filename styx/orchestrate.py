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
from styx.config import DEFAULT_CEPH_FLAGS_PARTIAL
from styx import __version__
from styx.policy import Policy, DryRunPolicy, MaintenancePolicy, log, setup_log_file
from styx.wrappers import Operations, _local_pyz, installed_pyz_path


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
        topo.host_ips, topo.orchestrator = parse_cluster_status(pvesh('/cluster/status'))
    if config.orchestrator:
        topo.orchestrator = config.orchestrator
    log(f'Orchestrator: {topo.orchestrator}')
    log(f'Hosts: {" ".join(f"{h}({ip})" for h, ip in topo.host_ips.items())}')

    # VMs
    topo.vm_host, topo.vm_name, topo.vm_type = parse_cluster_resources(
        pvesh('/cluster/resources', '--type', 'vm')
    )
    log(f'Running VMs: {" ".join(topo.vm_host)}')

    # Kubernetes — config override, then API auto-discovery
    if config.workers or config.control_plane:
        topo.k8s_workers  = list(config.workers)
        topo.k8s_cp       = list(config.control_plane)
        topo.k8s_enabled  = True
        log(f'Kubernetes: config override '
            f'(workers={topo.k8s_workers} cp={topo.k8s_cp})')
    elif config.k8s_server and config.k8s_token:
        try:
            k8s        = _make_k8s_client(config)
            node_roles = k8s.get_node_roles()
            topo.k8s_workers, topo.k8s_cp = match_nodes_to_vms(topo.vm_name, node_roles)
            topo.k8s_enabled = True
            log(f'Kubernetes: API discovery '
                f'(workers={topo.k8s_workers} cp={topo.k8s_cp})')
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


# Fixed escalation overhead in vm_shutdown.py: SIGTERM(10s) + SIGKILL(5s)
_VM_ESCALATION_OVERHEAD = 15


# ── hosts filter ─────────────────────────────────────────────────────────────

def _apply_hosts_filter(topo, hosts):
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
    topo.k8s_workers = [v for v in topo.k8s_workers if v in topo.vm_host]
    topo.k8s_cp      = [v for v in topo.k8s_cp      if v in topo.vm_host]
    if not topo.k8s_workers and not topo.k8s_cp:
        topo.k8s_enabled = False
    log(f'--hosts filter: shutting down {" ".join(sorted(shutdown_hosts))} '
        f'({len(topo.vm_host)} VM(s))')
    return topo


# ── pre-flight (maintenance mode) ────────────────────────────────────────────

def preflight(topo, config):
    """Check SSH reachability, styx version, k8s API, and Ceph health.

    Results are logged. A styx version mismatch or unreachable peer is fatal —
    sys.exit(1) is called after logging all failures.
    """
    log('--- Pre-flight ---')

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

    # styx version check — only when running as a zipapp
    if _local_pyz():
        styx_failures = []
        unreachable_count = (
            len(topo.host_ips) - 1 - len(reachable)   # -1 for orchestrator
        )
        for host, ip in reachable:
            cmd = f'python3 {installed_pyz_path()} --version'
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
                    styx_failures.append(host)
            except Exception as e:
                log(f'styx {host}: NOT AVAILABLE ({e})')
                styx_failures.append(host)

        total_failures = len(styx_failures) + unreachable_count
        if total_failures:
            import sys as _sys
            _sys.exit(
                f'FATAL: styx version check failed on {total_failures} host(s) '
                f'— cannot proceed'
            )

    if topo.k8s_enabled and config.k8s_server and config.k8s_token:
        try:
            k8s    = _make_k8s_client(config)
            nodes  = k8s.list_nodes()
            n      = len(nodes.get('items', []))
            log(f'k8s API: OK ({n} nodes)')
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

    if topo.ceph_enabled:
        try:
            r = subprocess.run(
                ['ceph', 'health'], capture_output=True, text=True, timeout=10,
            )
            log(f'Ceph: {r.stdout.strip() or r.stderr.strip()}')
        except Exception as e:
            log(f'Ceph: unavailable ({e})')


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
    log(f'Disabling HA resources (scope: {scope})')
    for sid in ops.get_ha_started_sids():
        vmid = sid.split(':', 1)[-1] if ':' in sid else sid
        if vmid not in target:
            continue
        log(f'Disabling HA: {sid}')
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

    log('--- Draining all k8s nodes ---')

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
    log('--- Dispatching local-shutdown ---')

    # Group workloads by host as (type, vmid) tuples
    by_host = {}
    for vmid, host in topo.vm_host.items():
        if vm_filter is not None and vmid not in vm_filter:
            continue
        wtype = topo.vm_type.get(vmid, 'qemu')
        by_host.setdefault(host, []).append((wtype, vmid))

    # Calculate poweroff_delay for peers (autonomous fallback)
    poweroff_delay = None
    if do_poweroff:
        poweroff_delay = config.timeout_vm + _VM_ESCALATION_OVERHEAD

    # Dispatch to all hosts (peers get poweroff_delay, orchestrator does not)
    for host, workloads in by_host.items():
        is_orch = (host == topo.orchestrator)
        delay = None if is_orch else poweroff_delay
        labels = ' '.join(f'{wt}:{vid}' for wt, vid in workloads)
        log(f'Dispatching local-shutdown to {host}: {labels}')
        if policy.dry_run:
            for _wtype, vmid in workloads:
                ops.check_vm(host, vmid)
        else:
            ops.dispatch_local_shutdown(
                host, workloads, config.timeout_vm,
                poweroff_delay=delay, dry_run=policy.dry_run,
            )


# ── polling loop ──────────────────────────────────────────────────────────────

def run_polling_loop(topo, ops, policy, do_poweroff, poll_interval=None):
    if poll_interval is None:
        poll_interval = int(os.environ.get('STYX_POLL_INTERVAL', '10'))

    log(f'--- Polling loop (poweroff={do_poweroff}) ---')

    if policy.dry_run:
        for host in topo.host_ips:
            if host != topo.orchestrator:
                log(f'[dry-run] would poweroff_host {host}')
        return

    powered_off = {topo.orchestrator}
    while True:
        all_done = True
        for host in topo.host_ips:
            if host in powered_off and host != topo.orchestrator:
                continue
            running  = set(ops.get_running_vmids(host))
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

        peers_done = all(h in powered_off for h in topo.host_ips if h != topo.orchestrator)
        if all_done or peers_done:
            orch_running = set(ops.get_running_vmids(topo.orchestrator))
            orch_vms     = {v for v, h in topo.vm_host.items() if h == topo.orchestrator}
            if not (orch_vms & orch_running):
                log('All VMs stopped (including orchestrator)')
                break
            log(f'Waiting for orchestrator VMs: {orch_vms & orch_running}')
            all_done = False

        time.sleep(poll_interval)


# ── revert summary (partial runs) ────────────────────────────────────────────

def _log_startup_checklist(topo, ceph_flags_set):
    """Log steps to run after bringing a fully-shutdown cluster back up."""
    items = []

    if ceph_flags_set:
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


def _log_revert_summary(topo, args, ceph_flags_set):
    """Log a checklist of manual steps needed to restore normal cluster state.

    Called at the end of every --hosts run (skip in dry-run: nothing changed).
    """
    log('--- Partial run complete — revert checklist ---')

    if ceph_flags_set:
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

def main(argv=None, *, _discover_fn=None, _ops_factory=None):
    """Entry point for `styx orchestrate`.

    _discover_fn(config) -> ClusterTopology  — injectable for testing
    _ops_factory(topo, config) -> Operations — injectable for testing
    """
    import argparse

    p = argparse.ArgumentParser(description='styx — graceful cluster shutdown')
    p.add_argument('--phase',  type=int, choices=[1, 2, 3], default=3)
    p.add_argument('--config', default='/etc/styx/styx.conf')
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
    config = load_config(args.config)

    log('=' * 40)
    log('styx run started')
    log('=' * 40)
    log(f'Mode: {args.mode}, Phase: {args.phase}')

    if _discover_fn is not None:
        topo = _discover_fn(config)
    else:
        topo = discover(config, _on_warning=policy.on_warning)

    if args.hosts:
        _apply_hosts_filter(topo, args.hosts)

    if args.mode in ('maintenance', 'dry-run'):
        preflight(topo, config)
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

    # Cordon all k8s nodes (idempotent)
    if topo.k8s_enabled:
        log('--- Cordoning all k8s nodes ---')
        for vmid in topo.k8s_workers + topo.k8s_cp:
            node = topo.vm_name.get(vmid, vmid)
            log(f'Cordoning: {node}')
            try:
                policy.execute(f'cordon {node}', ops.cordon_node, node)
            except Exception as e:
                policy.on_warning(f'cordon failed for {node}: {e}')

    # HA
    if should_disable_ha(args.phase):
        _disable_ha(topo, ops, policy, 'all')
    elif topo.k8s_enabled:
        _disable_ha(topo, ops, policy, 'k8s')

    # Drain all k8s nodes (workers + CP) in parallel — no VM shutdown
    _drain_all_k8s(topo, config, ops, policy)

    # Phase 1: dispatch for k8s VMs only, no poll, return
    if not should_run_polling(args.phase):
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
    # Partial runs use a reduced set: noout only (prevents mark-out timer);
    # the recovery/rebalance/backfill/nodown flags are full-shutdown-only.
    ceph_flags = (
        config.ceph_flags_partial if args.hosts else config.ceph_flags
    )
    if should_set_ceph_flags(args.phase) and topo.ceph_enabled:
        log('--- Setting Ceph OSD flags ---')
        policy.execute('set_ceph_flags', ops.set_ceph_flags, ceph_flags)

    # Dispatch local-shutdown to each host (one SSH per peer)
    _dispatch_independent_phase(topo, config, ops, policy, do_poweroff)

    run_polling_loop(topo, ops, policy, do_poweroff)

    # Log completion notes before potentially going dark (poweroff flushes nothing).
    if not policy.dry_run:
        applied_ceph = (
            ceph_flags
            if should_set_ceph_flags(args.phase) and topo.ceph_enabled
            else []
        )
        if args.hosts:
            _log_revert_summary(topo, args, applied_ceph)
        else:
            _log_startup_checklist(topo, applied_ceph)

    # Power off orchestrator only on full runs or when explicitly targeted.
    poweroff_self = do_poweroff and (not args.hosts or topo.orchestrator in args.hosts)
    if poweroff_self:
        log('Powering off orchestrator (self)')
        policy.execute('poweroff_self', ops.poweroff_self)
    elif args.skip_poweroff:
        log('All VMs stopped — --skip-poweroff: hosts not powered off')
    else:
        log(f'Phase {args.phase} complete')
