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
        topo.host_ips, topo.orchestrator = parse_cluster_status(pvesh('/cluster/status'))
    if config.orchestrator:
        topo.orchestrator = config.orchestrator
    log(f'Orchestrator: {topo.orchestrator}')
    log(f'Hosts: {" ".join(f"{h}({ip})" for h, ip in topo.host_ips.items())}')

    # VMs
    topo.vm_host, topo.vm_name = parse_cluster_resources(
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


# ── pre-flight (maintenance mode) ────────────────────────────────────────────

def preflight(topo, config):
    """Check SSH reachability, k8s API, and Ceph health before any action.

    Results are logged; the caller is responsible for any gate prompt.
    """
    log('--- Pre-flight ---')

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
        except Exception as e:
            log(f'SSH {host} ({ip}): UNREACHABLE ({e})')

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


# ── HA ────────────────────────────────────────────────────────────────────────

def _disable_ha(topo, ops, policy, scope):
    target = set(topo.k8s_workers + topo.k8s_cp) if scope == 'k8s' else None
    log(f'Disabling HA resources (scope: {scope})')
    for sid in ops.get_ha_started_sids():
        if target is not None:
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

def _drain_and_shutdown(vmid, node, host, config, ops, policy):
    log(f'Draining: {node} (VM {vmid} on {host})')
    ok = policy.execute(f'drain {node}', ops.drain_node, node, config.timeout_drain)
    if ok is None:        # dry-run
        pass
    elif not ok:
        policy.on_warning(f'drain timed out or failed for {node}')
    else:
        log(f'Drained: {node}')
        stale = ops.list_volume_attachments_for_node(node)
        if stale:
            policy.on_warning(
                f'stale VolumeAttachments after drain of {node}: {", ".join(stale)}'
            )
    policy.execute(f'shutdown_vm {vmid}', ops.shutdown_vm, host, vmid, config.timeout_vm)


def _shutdown_only(vmid, host, config, ops, policy):
    log(f'Shutting down VM: {vmid} on {host}')
    policy.execute(f'shutdown_vm {vmid}', ops.shutdown_vm, host, vmid, config.timeout_vm)


# ── tracks ────────────────────────────────────────────────────────────────────

def run_k8s_track(topo, config, ops, policy):
    if not topo.k8s_enabled or (not topo.k8s_workers and not topo.k8s_cp):
        return

    log('--- Track A: Kubernetes ---')

    def _run_parallel(vmids, label):
        with concurrent.futures.ThreadPoolExecutor() as ex:
            futs = {
                ex.submit(
                    _drain_and_shutdown,
                    vmid, topo.vm_name.get(vmid, vmid), topo.vm_host[vmid],
                    config, ops, policy,
                ): vmid
                for vmid in vmids
            }
            for fut in concurrent.futures.as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    policy.on_warning(f'{label} {futs[fut]}: {e}')

    _run_parallel(topo.k8s_workers, 'worker')
    log('All worker nodes done')
    _run_parallel(topo.k8s_cp, 'cp')
    log('All control-plane nodes done')


def run_other_vm_track(topo, config, ops, policy):
    log('--- Track B: Non-k8s VMs ---')
    others = other_vmids(list(topo.vm_host), topo.k8s_workers, topo.k8s_cp)
    with concurrent.futures.ThreadPoolExecutor() as ex:
        futs = {
            ex.submit(_shutdown_only, vmid, topo.vm_host[vmid], config, ops, policy): vmid
            for vmid in others
        }
        for fut in concurrent.futures.as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                policy.on_warning(f'VM {futs[fut]}: {e}')


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

    if args.mode in ('maintenance', 'dry-run'):
        preflight(topo, config)
    policy.phase_gate(
        f'{len(topo.host_ips)} host(s), {len(topo.vm_host)} VM(s)'
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

    # Deploy executable to peer hosts so vm-shutdown doesn't depend on CephFS
    if _local_pyz():
        log('--- Deploying styx to peer hosts ---')
        for host in topo.host_ips:
            if host != topo.orchestrator:
                try:
                    policy.execute(f'push_executable {host}', ops.push_executable, host)
                    if not policy.dry_run:
                        log(f'Deployed styx to {host}')
                except Exception as e:
                    policy.on_warning(f'Failed to deploy styx to {host}: {e}')

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

    # Track A (k8s) + Track B (other VMs) run concurrently
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fut_a = ex.submit(run_k8s_track, topo, config, ops, policy)
        fut_b = (
            ex.submit(run_other_vm_track, topo, config, ops, policy)
            if args.phase >= 2
            else None
        )
        fut_a.result()
        if fut_b:
            fut_b.result()

    if not should_run_polling(args.phase):
        log(f'Phase {args.phase} complete')
        return

    do_poweroff = should_poweroff_hosts(args.phase)
    if do_poweroff:
        ceph_note = ', set Ceph flags' if topo.ceph_enabled else ''
        policy.phase_gate(f'VM shutdown tracks complete — about to{ceph_note} power off all hosts. Proceed?')

    # Ceph flags (phase 3 only, before polling loop)
    if should_set_ceph_flags(args.phase) and topo.ceph_enabled:
        log('--- Setting Ceph OSD flags ---')
        policy.execute('set_ceph_flags', ops.set_ceph_flags, config.ceph_flags)

    run_polling_loop(topo, ops, policy, do_poweroff)

    if do_poweroff:
        log('Powering off orchestrator (self)')
        policy.execute('poweroff_self', ops.poweroff_self)
    else:
        log(f'Phase {args.phase} complete')
