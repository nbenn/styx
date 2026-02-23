# Styx — Graceful Cluster Shutdown for Proxmox + Kubernetes + Ceph

A general-purpose tool for automated graceful shutdown of infrastructure stacks running Kubernetes on Proxmox VMs with optional Ceph storage, triggered by UPS power failure or manual invocation.

## Scope

- **VMs only** (QEMU/KVM). LXC containers and Proxmox 9 OCI containers are not supported.
- **Ceph on Proxmox hosts** (native Proxmox Ceph integration). Ceph-in-VM is not supported.
- **Proxmox clusters** (multi-node). Single-node support is a stretch goal (see Known Limitations).
- Styx is a **command**, not a daemon. The trigger mechanism (NUT, cron, manual) is external and out of scope.

## Overview

Styx orchestrates the shutdown of an entire stack in the correct order:

1. **Drain** Kubernetes nodes (graceful pod eviction)
2. **Shut down** all VMs (quorum-free, via QMP)
3. **Set Ceph flags** (prevent rebalancing)
4. **Power off** Proxmox hosts

The tool is designed to complete within a UPS battery window (typically 5-10 minutes) and handles quorum loss, partial failures, and idempotent re-runs.

## Auto-Discovery

Styx auto-discovers the entire environment at startup. For standard setups, **no configuration file is needed**.

### Discovery Chain

| What | How | Fallback (config override) |
|------|-----|---------------------------|
| Hosts + IPs | `pvesh get /cluster/status` — extract `type=node` entries | `[hosts]` section |
| Orchestrator | `local == 1` from cluster status | `[orchestrator]` section |
| VM-to-host mapping | `pvesh get /cluster/resources --type vm`, filter `type == "qemu"` | — (always needed) |
| K8s worker/CP VMIDs | Priority: (1) `[kubernetes] workers/control_plane` config, (2) API: match node names to VM names via `node-role.kubernetes.io/control-plane` label | `[kubernetes] workers, control_plane` |
| K8s credentials | `[kubernetes] server` + `token` + optional `ca_cert` (required for API-based discovery) | — |
| Ceph enabled | `pveceph status` exits 0 | `[ceph] enabled` |
| Ceph flags | defaults: `noout, norecover, norebalance, nobackfill, nodown` | `[ceph] flags` |
| Timeouts | defaults: drain=120, vm=120 | `[timeouts]` |

### Startup Logic

1. **Hosts**: `pvesh get /cluster/status --output-format json` → filter `type == "node"` → extract `name` and `ip`. The entry with `local == 1` is the orchestrator. If `[hosts]` is in config, use that instead.
2. **VMs**: `pvesh get /cluster/resources --type vm --output-format json` → filter `type == "qemu"` (excludes LXC containers), build VMID-to-host and VMID-to-name maps. Filters out templates (`template == 1`) and stopped VMs.
3. **Kubernetes**: worker/CP VMIDs are resolved in priority order:
   - **Config override**: if `workers` or `control_plane` are set in `[kubernetes]`, use them directly.
   - **API auto-discovery**: if `server` and `token` are set, call the Kubernetes API, extract node names and roles, match against Proxmox VM names. Workers = nodes without `control-plane` label; CP = nodes with it. If name matching yields zero matches → **abort with error**, ask user to configure `workers`/`control_plane` in config.
   - If neither applies → skip k8s entirely (Proxmox-only mode).
   - If API is configured but unreachable → skip k8s (re-run scenario where k8s VMs are already off).
4. **Ceph**: `pveceph status >/dev/null 2>&1` — exit 0 means Ceph is configured. If `[ceph] enabled` is explicitly set in config, that takes precedence.
5. **HA**: `ha-manager status` → auto-detect HA-managed resources (phase >= 2 only).

All discovery uses `pvesh`/`ha-manager` which require quorum — but discovery runs at startup before any host is powered off, so quorum is guaranteed.

## Configuration

Optional INI config file (default: `/etc/styx/styx.conf`). Only needed to override auto-discovery or set non-default values.

### Zero-Config (standard setup)

If your Kubernetes node names match Proxmox VM names and kubectl is configured at `/root/.kube/config`, no config file is needed at all. Styx discovers everything.

### Override Examples

Override only what differs from auto-discovery:

```ini
# Only needed if Ceph flags differ from defaults
[ceph]
flags = noout, norebalance

# Only needed if timeouts differ from defaults
[timeouts]
drain = 60
vm = 90
```

When node names don't match VM names:

```ini
[kubernetes]
workers = 211, 212, 213, 214, 215
control_plane = 201, 202, 203
```

When SSH IPs differ from corosync IPs:

```ini
[hosts]
pve1 = 192.168.1.10
pve2 = 192.168.1.11
pve3 = 192.168.1.12
```

### Full Config Reference

```ini
[hosts]
# Override auto-discovered hosts. Format: hostname = ip_address
# Default: auto-discovered from pvesh get /cluster/status
pve1 = 10.0.0.1
pve2 = 10.0.0.2
pve3 = 10.0.0.3

[orchestrator]
# Override auto-discovered orchestrator (local == 1 from pvesh)
# Default: the host where Styx is running
host = pve1

[kubernetes]
# Kubernetes API endpoint and credentials (required for API-based node discovery)
server = https://10.0.0.10:6443
token = /etc/styx/k8s-token          # path to a file containing the bearer token
ca_cert = /etc/styx/k8s-ca.crt       # optional; skip TLS verify if omitted
# Override auto-discovered k8s node classification (VMIDs)
workers = 211, 212, 213, 214, 215
control_plane = 201, 202, 203

[ceph]
# Override auto-detection (pveceph status)
enabled = true
# Override default flags (default: noout, norecover, norebalance, nobackfill, nodown)
# noup is NOT set by default: it prevents OSDs coming back up after restart,
# which is a post-boot concern. Add it here only if you want to delay OSD start
# on the next boot (e.g., to allow manual verification before OSDs come online).
flags = noout, norecover, norebalance, nobackfill, nodown

[timeouts]
# All values in seconds
drain = 120    # Max time for kubectl drain per node (default: 120)
vm = 120       # Max time for VM graceful shutdown before force-kill (default: 120)
```

All sections are optional. SSH must be set up between all Proxmox hosts (root, key-based) regardless of configuration method.

## Architecture

```
┌──────────────┐         ┌──────────────────────────┐
│ UPS / NUT /  │ trigger │ styx                     │
│ manual       │────────→│ on orchestrator host      │
└──────────────┘         └──────────┬───────────────┘
                                    │
                          Interleaved pipeline
                          (see Shutdown Sequence)
```

### Prerequisites

- **Proxmox cluster** with SSH between all hosts (root, key-based)
- **python3** on **all** Proxmox hosts (standard on Proxmox 8+; used for orchestration and VM shutdown)
- **ceph** CLI on the orchestrator or a Ceph node (if using Ceph)
- **`shutdown_policy = freeze`** in Proxmox `datacenter.cfg` (cluster-side): prevents HA from attempting to relocate VMs to surviving nodes during the shutdown window. Without this, HA may fight the shutdown sequence.
- **kubelet `GracefulNodeShutdown`** (`shutdownGracePeriod` in kubelet config, node-side): ensures the kubelet participates in ACPI shutdown and terminates pods cleanly before the node powers off. This is a node-side prerequisite; styx has no visibility into it and does not configure it.

### File Layout

```
/var/lib/vz/snippets/styx.pyz      # Self-contained executable (all Proxmox hosts, via shared storage)
/etc/styx/styx.conf                # Configuration (optional, overrides auto-discovery)
```

`styx.pyz` is a Python zipapp (stdlib `zipapp` module, Python 3.5+). It bundles the entire `styx/` package in a single executable file and is placed on shared Proxmox snippets storage (NFS or CephFS with `content snippets`) so every node can run it from the same path without per-node installation.

Both subcommands are available from the single file:
- `styx.pyz orchestrate` — main shutdown sequence (orchestrator only)
- `styx.pyz vm-shutdown <vmid> [timeout]` — VM shutdown helper (all hosts)

Built with `bash scripts/build.sh`. Published as a GitHub release artifact on version tags via `.github/workflows/release.yml`.

## Components

### VM Shutdown Helper (`styx-vm-shutdown`)

Deployed on **all** Proxmox hosts. Shuts down a single VM using direct QMP socket and PID file, with no Proxmox API (quorum) dependency.

**Why not `qm shutdown`?** During the shutdown sequence, Proxmox cluster quorum may be lost as hosts are powered off. `qm shutdown` requires quorum (it reads VM config from pmxcfs). This helper bypasses the Proxmox API entirely — it talks directly to the QEMU process via the QMP socket and monitors the PID file.

**How it works:**
1. Check PID file — if VM not running, exit 0 (idempotent)
2. Send `system_powerdown` via QMP socket (`/var/run/qemu-server/<vmid>.qmp`) — ACPI power button
3. Poll the PID file every second up to the timeout
4. If still running after timeout: SIGTERM → wait 10s → SIGKILL

**Why QMP, not QGA?** QGA (`guest-shutdown`) requires the QEMU guest agent to be installed and running inside the VM. QMP `system_powerdown` sends an ACPI power button event directly to the hypervisor — it works regardless of what's running inside the guest. All modern Linux and Windows guests handle ACPI shutdown correctly. Any VM that doesn't will be killed when the host shuts down anyway.

**Usage:**
```bash
styx.pyz vm-shutdown <vmid> [timeout]    # default timeout: 120s
```

The implementation lives in `styx/vm_shutdown.py`. It uses Python's `socket.AF_UNIX`
directly — no `socat` dependency.

**Dependencies**: `python3` (standard on Proxmox 8+).

**Checking if a VM is running** (quorum-free):
```bash
# Local
[[ -f /var/run/qemu-server/${VMID}.pid ]] && kill -0 "$(cat /var/run/qemu-server/${VMID}.pid)" 2>/dev/null

# Remote
ssh root@<host-ip> 'kill -0 $(cat /var/run/qemu-server/'"${VMID}"'.pid 2>/dev/null) 2>/dev/null'
```

### Kubernetes RBAC

A dedicated ServiceAccount with minimal permissions for drain operations. A long-lived token is placed on the orchestrator as a kubeconfig.

**ClusterRole permissions**:
- `nodes`: `get`, `list`, `patch` (for cordon/uncordon)
- `pods`: `get`, `list` (to discover pods on a node)
- `pods/eviction`: `create` (to evict pods during drain)
- `daemonsets`: `get`, `list` (for `--ignore-daemonsets`)

## Shutdown Sequence

### Command Line

```
styx.pyz orchestrate [--mode <mode>] [--phase <1|2|3>] [--config <path>]

  --mode <mode>    dry-run | emergency | maintenance  (default: emergency)
  --phase <1|2|3>  Execute up to and including this phase (default: 3)
  --config <path>  Config file path (default: /etc/styx/styx.conf)
```

### Modes

Three mutually exclusive modes, implemented as three `Policy` subclasses in `styx/policy.py`:

| Mode | Class | Behaviour |
|------|-------|-----------|
| `emergency` | `Policy` | Execute automatically; `on_warning()` logs and continues; `phase_gate()` is a no-op. Default — designed for unattended UPS-triggered shutdowns. |
| `maintenance` | `MaintenancePolicy` | Pre-flight checks before any action; `on_warning()` prompts `[skip/abort]`; `phase_gate()` requires explicit confirmation before proceeding. Designed for planned maintenance. |
| `dry-run` | `DryRunPolicy` | `execute()` logs `[dry-run] <description>` and returns `None` without calling the function. All other behaviour is identical to emergency. |

**Maintenance mode detail:**

Before touching anything, `preflight()` runs and logs:
- SSH reachability to every non-orchestrator host
- Kubernetes API status + per-node drainable pod count (drain load estimate)
- Ceph health (`ceph health`)

Two phase gates prompt for confirmation:
1. After discovery + pre-flight: "N hosts, M VMs … proceed with shutdown?"
2. Before phase-3 powerdown: "about to set Ceph flags and power off all hosts — proceed?"

Any `on_warning()` call during execution (drain timeout, stale VolumeAttachment, SSH error) pauses and prompts `[skip/abort]`. A `threading.Lock` serialises concurrent prompts from parallel drain threads.

Both modes run **identical code paths**. `Policy.phase_gate()` and `Policy.on_warning()` are no-ops in emergency mode. This is intentional — maintenance mode is the primary way to exercise the emergency path against a real cluster.

### Phases

| Phase | Scope | What it does |
|-------|-------|-------------|
| 1     | Kubernetes | Drain nodes, issue VM shutdown for k8s VMs |
| 2     | All VMs | Shut down non-k8s VMs, wait for all VMs to stop |
| 3     | Hosts | Set Ceph OSD flags, power off Proxmox hosts |

With `--phase N`, the script executes up to and including phase N.

### Interleaved Pipeline

Steps don't wait for an entire phase to complete. As each resource finishes its current step, it immediately progresses to the next:

```
Time ->

STARTUP:
  auto-discover:      [pvesh cluster/status + cluster/resources, kubectl get nodes, pveceph status]
  disable HA:         [ha-manager set ... --state disabled]  (phase >= 2 only)
  cordon all k8s:     [kubectl cordon] (instant, prevents rescheduling)

PARALLEL TRACKS (phases 1+2 run concurrently):
  Track A (k8s):
    worker VMs:       [drain all workers in parallel] -> [styx-vm-shutdown <vmid> &] (parallel per worker)
    ...all workers drained...
    CP VMs:           [drain all CP in parallel] -> [styx-vm-shutdown <vmid> &] (parallel per CP node)

  Track B (non-k8s VMs, starts at the same time as Track A):
    non-k8s VMs:      [ssh <host> styx-vm-shutdown <vmid> &] (parallel)

SET CEPH FLAGS (phase 3 only, before any host goes down):
  ceph osd set <flags>

UNIFIED POLLING LOOP (after all shutdown commands issued):
  Every 10s:
    - skip hosts already marked as powered off
    - check PID files for running VMs on each live host (local or SSH -o ConnectTimeout=5)
    - if all VMs on a peer host are stopped -> poweroff that host, mark as powered off (phase 3 only)
  After loop (phase 3 only):
    - poweroff orchestrator (self, always last)
```

### Phase Control

| Flag | Startup | Track A (k8s) | Track B (non-k8s) | Ceph flags | Polling loop | Post-loop |
|------|---------|---------------|-------------------|------------|--------------|-----------|
| `--phase 1` | cordon | drain + shutdown k8s VMs | skip | skip | skip | skip |
| `--phase 2` | disable HA, cordon | drain + shutdown k8s VMs | shutdown non-k8s VMs | skip | poll, wait for all VMs | skip |
| `--phase 3` (default) | disable HA, cordon | drain + shutdown k8s VMs | shutdown non-k8s VMs | set flags | poll, **poweroff hosts** | poweroff orchestrator |

Notes:
- Phase 1 issues `styx-vm-shutdown` for k8s VMs (fire-and-forget). Does **not** wait for them to stop. HA is disabled only for k8s VMIDs (scoped, since phase 1 doesn't touch other VMs).
- Phase 2 adds HA disable, non-k8s VMs, and the polling loop.
- Phase 3 adds Ceph flags and host poweroff.
- Cordon always runs regardless of phase (idempotent prerequisite).
- If `[kubernetes]` is not configured, track A is skipped entirely.

### State Tracking

| State | Source | When |
|-------|--------|------|
| Host list + IPs | `pvesh get /cluster/status` (or config) | Once at startup |
| Orchestrator | `local == 1` from cluster status (or config) | Once at startup |
| VMID -> host mapping | `pvesh get /cluster/resources --type vm` | Once at startup |
| VMID -> VM name | Same `pvesh` query | Once at startup |
| K8s worker/CP classification | `kubectl get nodes` + name matching (or config) | Once at startup |
| Ceph enabled | `pveceph status` (or config) | Once at startup |
| VM running status | PID files: `/var/run/qemu-server/<vmid>.pid` | Each poll iteration (no quorum needed) |
| Hosts powered off | Local array variable | Updated in loop |

All startup queries (`pvesh`, `ha-manager`, `kubectl`) require quorum / API access but run before any host is powered off, so availability is guaranteed. All subsequent VM status checks use PID files.

### HA Handling

Some VMs may have Proxmox HA enabled. HA must be disabled before shutdown to prevent Proxmox from restarting VMs on surviving hosts.

- Auto-detected at startup via `ha-manager status`
- Each HA-managed resource disabled individually: `ha-manager set <sid> --state disabled`
- Only runs for phase >= 2 (phase 1 only affects k8s VMs, which are typically not HA-managed)
- Both `ha-manager` and `pvesh` require quorum, but run at startup before any host is powered off

### Quorum Considerations

Proxmox cluster quorum (corosync/pmxcfs) requires a majority of nodes. As hosts are powered off during phase 3, quorum is eventually lost.

**Quorum-dependent** (run at startup only):
- `pvesh` — host discovery, VM discovery
- `pveceph` — Ceph detection
- `ha-manager` — HA disable
- `qm` — NOT used (replaced by `styx-vm-shutdown`)

**Quorum-independent** (work throughout):
- QMP socket (`/var/run/qemu-server/<vmid>.qmp`)
- PID files (`/var/run/qemu-server/<vmid>.pid`)
- SSH between hosts
- `ceph` commands (Ceph has its own quorum, independent of Proxmox)

Note: Proxmox does **not** use libvirt — it manages QEMU processes directly.

### Idempotency

The script is safe to re-run (e.g., `--phase 1` followed by `--phase 3`):

| Command | Already-done state | Behaviour |
|---------|-------------------|-----------|
| `kubectl get nodes` | API unreachable (VMs off) | Skip drain, go straight to VM shutdown |
| `kubectl drain` | Node already cordoned, no pods | Succeeds (no-op) |
| `styx-vm-shutdown` | VM already stopped (no PID) | Exits 0 |
| `ha-manager set --state disabled` | Already disabled | No-op |
| `ceph osd set noout` | Already set | No-op |
| `ssh root@<ip> poweroff` | Host already off | SSH refused, logged, continues |

### Timeouts

| Timeout | Default | Purpose |
|---------|---------|---------|
| Drain | 120s per node | Max time for `kubectl drain` |
| VM shutdown | 120s per VM | Graceful wait before SIGTERM -> SIGKILL |

Each backgrounded `styx-vm-shutdown` handles its own timeout and force-kill escalation. The polling loop only observes status — it doesn't manage timeouts.

### Logging

All significant actions are logged with timestamps to both stdout and `/var/log/styx.log` (append mode). This includes:

- Discovery results (hosts, VMs, k8s nodes, Ceph status)
- Every action taken (drain, shutdown, poweroff, flag set)
- Errors and fallbacks (QGA unavailable, SSH timeout, drain timeout)
- Phase transitions and completion

Implementation: `styx/policy.py` provides a module-level `log()` function that writes `[ISO-timestamp] msg` to both stdout and the log file in append mode. The log file is opened once at startup via `setup_log_file(path)`. Path defaults to `/var/log/styx.log`; override with the `LOG_FILE` environment variable. All three policy classes share the same `log()` function.

The primary use case is post-mortem analysis after a UPS-triggered shutdown. When called interactively, stdout provides the same output.

## Startup Recovery (Manual)

After power is restored:

1. **Power on Proxmox hosts** (manually or via IPMI/iLO)

2. **Unset Ceph OSD flags** (if Ceph is enabled):
   ```bash
   for flag in noout norecover norebalance nobackfill nodown; do
     ceph osd unset "$flag"
   done
   # Also unset noup if you set it manually at shutdown time:
   # ceph osd unset noup
   ```

3. **Verify Ceph health**:
   ```bash
   ceph status
   ceph osd tree
   ```

4. **Re-enable HA** for VMs that had it:
   ```bash
   ha-manager set <sid> --state started
   ```

5. **Start VMs** (networking/infrastructure VMs first, then k8s):
   ```bash
   qm start <infra-vmid>         # networking, DNS, etc.
   qm start <cp-vmids>           # k8s control plane
   qm start <worker-vmids>       # k8s workers
   ```

6. **Uncordon k8s nodes**:
   ```bash
   kubectl uncordon --all
   ```

7. **Unseal Vault** (if applicable):
   ```bash
   vault operator unseal
   ```

## Testing

### Design Principles

The scripts are structured for testability by separating **decision logic** (pure functions, testable without infrastructure) from **external actions** (SSH, QMP, kubectl — thin wrappers that are trivially mockable). This gives meaningful test coverage without needing real Proxmox, Kubernetes, or Ceph clusters.

Tests are written in Python `unittest` — no external test frameworks required.

### Code Structure

```
styx/
├── __main__.py                       # CLI dispatch (orchestrate | vm-shutdown)
├── policy.py                         # DryRunPolicy, Policy, MaintenancePolicy + log()
├── config.py                         # StyxConfig dataclass + INI parser
├── discover.py                       # pure parsing functions + ClusterTopology
├── classify.py                       # VMID classification (k8s-worker/cp/other)
├── decide.py                         # phase predicates (should_disable_ha, etc.)
├── k8s.py                            # K8sClient (cordon, drain, list nodes, etc.)
├── vm_shutdown.py                    # QMP + PID escalation (no socat)
├── wrappers.py                       # Operations class (all external calls)
└── orchestrate.py                    # main shutdown sequence
test/
├── fixtures/
│   ├── pvesh/                        # anonymised pvesh JSON responses
│   │   ├── cluster_status.json
│   │   ├── cluster_status_offline_node.json
│   │   ├── cluster_resources.json
│   │   └── cluster_resources_migration.json
│   └── k8s/                          # anonymised kubectl JSON responses
│       ├── nodes.json
│       ├── nodes_single_node.json
│       ├── nodes_dual_role.json
│       ├── volume_attachments.json
│       └── volume_attachments_stale.json
├── unit/
│   ├── test_config.py                # INI parsing
│   ├── test_discover.py              # pvesh/kubectl JSON parsing, name matching
│   ├── test_classify.py              # VMID classification
│   ├── test_decide.py                # phase predicates
│   ├── test_k8s.py                   # K8sClient (cordon, drain, mirror pods, etc.)
│   └── test_policy.py                # DryRunPolicy, Policy, MaintenancePolicy
└── integration/
    ├── helpers.py                    # FakeOperations + fake VM (sleep + PID files)
    └── test_full_sequence.py         # end-to-end with injected fakes
scripts/
└── build.sh                          # builds styx.pyz zipapp
.github/workflows/
├── test.yml                          # CI: unittest + coverage upload to Codecov
└── release.yml                       # publishes styx.pyz on v* tags
```

### Layer Separation

**Layer 1 — Pure functions** (`styx/discover.py`, `styx/classify.py`, `styx/decide.py`):

No side effects. Take data in, return decisions. Directly testable.

```python
# styx/config.py
load_config(path) -> StyxConfig       # INI file -> dataclass (all sections optional)

# styx/discover.py
parse_cluster_status(data)            # pvesh cluster/status JSON -> (host_ips, orchestrator)
parse_cluster_resources(data)         # pvesh cluster/resources JSON -> (vm_host, vm_name)
match_nodes_to_vms(vm_name, roles)    # k8s node names + VM names -> (workers, cp); raises on no match

# styx/classify.py
classify_vmid(vmid, workers, cp)      # -> "k8s-worker" | "k8s-cp" | "other"
other_vmids(all_vmids, workers, cp)   # -> non-k8s VMID list

# styx/decide.py
should_disable_ha(phase)              # -> bool
should_run_polling(phase)             # -> bool
should_poweroff_hosts(phase)          # -> bool
should_set_ceph_flags(phase)          # -> bool
```

**Layer 2 — Thin wrappers** (`styx/wrappers.py`):

`Operations` class; injected as a fake in tests.

```python
ops.run_on_host(host, cmd)                     # SSH or local bash
ops.get_running_vmids(host)                    # scan PID files on a host
ops.shutdown_vm(host, vmid, timeout)           # python3 -m styx vm-shutdown (local or SSH)
ops.cordon_node(node)                          # kubectl cordon via K8sClient
ops.drain_node(node, timeout) -> bool          # kubectl drain via K8sClient
ops.list_volume_attachments_for_node(node)     # CSI VolumeAttachment check post-drain
ops.get_ha_started_sids()                      # ha-manager status -> started SIDs
ops.disable_ha_sid(sid)                        # ha-manager set --state disabled
ops.wait_ha_disabled(sid, timeout) -> bool     # poll until disabled or timeout
ops.set_ceph_flags(flags)                      # ceph osd set <flag> for each flag
ops.poweroff_host(host)                        # ssh root@<ip> poweroff
ops.poweroff_self()                            # poweroff (orchestrator self)
```

**Layer 3 — Orchestration** (`styx/orchestrate.py`):

`main()` accepts `_discover_fn` and `_ops_factory` as keyword-only parameters for test injection.

**Policy** (`styx/policy.py`):

Three concrete classes implement the three modes. All share the module-level `log()` function.

```python
Policy()               # emergency: execute, warn-and-continue, no gates
DryRunPolicy()         # dry-run:   log planned actions, skip execution (dry_run=True)
MaintenancePolicy()    # maintenance: pre-flight, on_warning prompts, phase gates prompt
```

`policy.execute(description, fn, *args)` — calls `fn` in emergency/maintenance, skips in dry-run.
`policy.on_warning(msg)` — logs in emergency/dry-run, prompts `[skip/abort]` in maintenance.
`policy.phase_gate(summary)` — no-op in emergency/dry-run, prompts `[yes/abort]` in maintenance.
`policy.dry_run` — `True` only for `DryRunPolicy`; used by `run_polling_loop` to skip poweroffs.

### Unit Tests

Test layer-1 functions with synthetic data — no mocking needed. Fixture-based tests load real anonymised API responses from `test/fixtures/` to guard against schema assumptions.

```python
# test/unit/test_discover.py
def test_parse_cluster_status_extracts_hosts_and_ips(self):
    data = [
        {'type': 'cluster', 'name': 'mycluster'},
        {'type': 'node', 'name': 'pve1', 'ip': '10.0.0.1', 'local': 1},
        {'type': 'node', 'name': 'pve2', 'ip': '10.0.0.2', 'local': 0},
    ]
    host_ips, orchestrator = parse_cluster_status(data)
    self.assertEqual(host_ips['pve1'], '10.0.0.1')
    self.assertEqual(orchestrator, 'pve1')

def test_match_nodes_to_vms_raises_on_no_match(self):
    with self.assertRaises(ValueError):
        match_nodes_to_vms({'201': 'my-vm-1'}, [('cp1', 'control-plane')])

# test/unit/test_k8s.py
def test_mirror_pod_not_drainable(self):
    pod = _pod('kube-apiserver', mirror=True)
    self.assertFalse(K8sClient._drainable(pod))

# test/unit/test_policy.py
def test_dry_run_skips_execution(self):
    called = []
    DryRunPolicy().execute('op', called.append, 'x')
    self.assertEqual(called, [])

def test_maintenance_on_warning_abort_exits_1(self):
    p = MaintenancePolicy(_input=lambda _: 'a')
    with self.assertRaises(SystemExit) as cm:
        p.on_warning('something failed')
    self.assertEqual(cm.exception.code, 1)
```

### Integration Tests

Full orchestration with `FakeOperations` — no real SSH, QMP, or kubectl. VMs simulated as `sleep` processes with PID files in a temp directory. `main()` receives injected `_discover_fn` and `_ops_factory`.

```python
# test/integration/test_full_sequence.py
def test_phase3_orchestrator_powers_off_last(self):
    main(['--phase', '3', '--config', '/dev/null'],
         _discover_fn=self._fake_discover,
         _ops_factory=self._fake_ops)
    self.assertEqual(self.ops.poweroff_log[-1], 'POWEROFF_SELF')

def test_dry_run_no_side_effects(self):
    main(['--mode', 'dry-run', '--phase', '3', '--config', '/dev/null'],
         _discover_fn=self._fake_discover,
         _ops_factory=self._fake_ops)
    self.assertEqual(self.ops.shutdown_log, [])
    self.assertEqual(self.ops.poweroff_log, [])
```

### CI

`.github/workflows/test.yml` runs on every push and pull request:

1. Run unit tests (fast feedback)
2. Run full suite under `coverage run --source=styx`
3. Generate `coverage.xml` and upload to Codecov via `codecov/codecov-action@v5`

`.github/workflows/release.yml` triggers on `v*` tags, builds `styx.pyz` via `scripts/build.sh`, and publishes it as a GitHub release artifact.

### What's NOT Tested (requires real infrastructure)

| Concern | Mitigation |
|---------|------------|
| QMP socket communication | Manual test on Proxmox; `--mode dry-run` shows planned actions |
| kubectl drain behavior | `--mode dry-run --phase 1` on real cluster |
| SSH connectivity | `--mode maintenance` pre-flight checks SSH reachability |
| Ceph OSD flag behavior | Idempotent, safe to test live |
| ha-manager interaction | `--mode dry-run`, verify manually |
| Actual VM shutdown timing | Tune timeouts based on observation |

### E2E Coverage

**Proxmox track**: GitHub-hosted runners have no KVM, so Proxmox E2E in CI is not feasible without a self-hosted runner on real hardware. Covered instead by realistic fixtures (validated against a real cluster) and the integration tests with `FakeOperations`.

**Kubernetes track**: A kind-cluster E2E (spin up cluster, deploy workloads, run `styx --phase 1`, assert nodes cordoned and pods evicted) would give high confidence but requires significant CI infrastructure. The unit tests for `K8sClient` (cordon, drain, mirror pod filtering, VolumeAttachment checks) and the integration tests with injected fakes cover the logic. The gap is end-to-end API interaction against a real cluster.

## Open Issues

No blocking issues remain. All open items have been resolved — see design decisions below.

### Resolved: QGA vs QMP

Investigated both QGA (`guest-shutdown`) and QMP (`system_powerdown`). Decision: **QMP only**.

QGA requires the guest agent to be installed and running inside the VM, uses a separate socket and protocol (`guest-sync-delimited` handshake), and the socket's existence doesn't guarantee agent availability. QMP `system_powerdown` sends an ACPI power button event at the hypervisor level — works regardless of guest agent status. All modern Linux and Windows guests handle ACPI shutdown. Any VM that doesn't will be killed when the host powers off.

QMP-only is simpler (one socket, one protocol, one code path) and more reliable (no dependency on guest-side software).

### Resolved: QMP handshake reliability

QMP requires `qmp_capabilities` before any command. The greeting is sent by QEMU immediately on connect and sits in the socket send buffer — it does not need to be read before sending commands.

**Pipelining works in practice:** QEMU buffers socket input and processes commands sequentially from the buffer. Proxmox itself uses strict request-response (via `IO::Multiplex`) because its QMP client is general-purpose. The implementation uses Python `socket.AF_UNIX` with explicit `recv()` between each send, which is the correct request-response pattern.

Source: Proxmox `PVE/QMPClient.pm`, QEMU QMP specification.

### Resolved: kubectl drain flags

Default flags for `kubectl drain`:
```
--ignore-daemonsets --delete-emptydir-data --force --timeout=<drain_timeout>s
```

Rationale: this is an emergency shutdown tool. Daemonsets can't be evicted, emptyDir data is lost anyway, and bare pods can't be allowed to block drain during a UPS event. Pod `terminationGracePeriodSeconds` is respected (no `--grace-period` override). Not configurable — these are the right choices for all styx use cases.

### Resolved: Discovery resource type filtering

`pvesh get /cluster/resources --type vm` returns both QEMU VMs and LXC containers. Each entry has a `type` field (`"qemu"` or `"lxc"`). Filter by `type == "qemu"` in the JSON parsing step. Confirmed from live `pvesh` output.

### Resolved: Orphaned background processes

If the main `styx` script is killed, backgrounded `styx-vm-shutdown` processes continue running. This is **intentional and desirable** — better to have orphans finishing VM shutdowns than leaving VMs running during a power failure. No process group management or trap handlers needed.

### Resolved: VM migration race

Documented as a known limitation. Live migration during a power failure is extremely unlikely. Worst case: shutdown command hits the wrong host, fails to find PID file, exits 0 (idempotent). The `pvesh` resource data includes a `status` field that could potentially detect migrations — future enhancement if needed.

### Resolved: PID file cleanup after SIGKILL

Non-issue. The polling loop uses `kill -0 $(cat pidfile)` which checks if the **process** is alive, not just if the file exists. After SIGKILL, the process is dead, `kill -0` returns false, and the polling loop correctly sees the VM as stopped. PID recycling within the poll interval is astronomically unlikely.

### Resolved: Single-node Proxmox

Out of scope for v1. Stretch goal for v2.

### Resolved: Tag-based VM discovery

Considered adding a third discovery mechanism: tag Proxmox VMs with `styx.k8s-worker` / `styx.k8s-cp` and classify from those tags. Rejected. Two discovery mechanisms already exist (API auto-discovery via node name matching, config override). A third adds operational burden — a node can be tagged but config forgotten, or vice versa — with no added value: if the Kubernetes API is unreachable, drain cannot run regardless of how the nodes were classified.

### Resolved: proxmox-guardian patterns

Reviewed [proxmox-guardian](https://github.com/Guilhem-Bonnet/proxmox-guardian) as prior art. Outcomes:
- **Per-action error policy**: covered by the `Policy` class pattern — `emergency` warns and continues, `maintenance` prompts. No additional per-operation configuration matrix needed.
- **Persistent state**: unnecessary as long as all actions remain idempotent (they do).
- **Startup/recovery automation**: manual procedure with clear documentation is the right trade-off; automating recovery risks acting on incomplete state.
- **Tag-based VM discovery**: considered and rejected (see above).

## Known Limitations

- **VMs only**: LXC containers and Proxmox 9 OCI containers are not supported. The architecture allows adding a `styx-ct-shutdown` helper later.
- **Ceph on hosts only**: Ceph-in-VM topologies are not supported. Ceph OSD flags are set after VM shutdown commands are issued (before any host goes down), which is correct for on-host Ceph.
- **No single-node support (v1)**: Styx assumes a multi-node Proxmox cluster. Single-node is a simpler problem and could be a stretch goal for v2.
- **VM migration**: Do not run styx while a VM live migration is in progress. The VMID-to-host mapping is captured once at startup and not refreshed. A migrating VM may receive shutdown commands on the wrong host. The `pvesh` resource data includes a `status` field that could potentially detect migrations — this is a future enhancement if needed.
- **Orphaned shutdown processes**: If the main `styx` script is killed, backgrounded `styx-vm-shutdown` processes continue running. This is intentional — they will complete their VM shutdowns independently.
- **CephFS teardown**: Clusters running CephFS could benefit from an explicit `ceph fs fail` + `ceph fs set cluster_down true` before shutdown, reversed on startup. Not implemented; CephFS clusters should verify filesystem health after recovery.
- **MON-last host ordering**: Powering off the Proxmox host running the Ceph MON last would be ideal to maintain Ceph quorum as long as possible. This requires Ceph topology awareness (which hosts run MONs) that styx does not currently have. The polling loop powers off hosts as their VMs stop, which is correct but not MON-aware.
