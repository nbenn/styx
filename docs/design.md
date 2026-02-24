# Styx — Graceful Cluster Shutdown for Proxmox + Kubernetes + Ceph

A general-purpose tool for automated graceful shutdown of infrastructure stacks running Kubernetes on Proxmox VMs with optional Ceph storage, triggered by UPS power failure or manual invocation.

## Scope

- **VMs only** (QEMU/KVM) today. Workload type infrastructure is in place (`vm_type` plumbed through discovery, dispatch, and CLI) for future LXC and Proxmox 9 OCI support — see Future Work.
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
| Ceph flags (partial runs) | default: `noout` | `[ceph] partial_flags` |
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
# Flags set during partial --hosts runs (default: noout only).
# nodown and recovery/rebalance/backfill flags are full-cluster-shutdown precautions.
# noout alone is the standard single-node maintenance flag.
partial_flags = noout

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
- **styx installed** on all nodes via `scripts/install.sh` (installs to `/opt/styx/styx.pyz`)
- **ceph** CLI on the orchestrator or a Ceph node (if using Ceph)
- **`shutdown_policy = freeze`** in Proxmox `datacenter.cfg` (cluster-side): prevents HA from attempting to relocate VMs to surviving nodes during the shutdown window. Without this, HA may fight the shutdown sequence.
- **kubelet `GracefulNodeShutdown`** (`shutdownGracePeriod` in kubelet config, node-side): ensures the kubelet participates in ACPI shutdown and terminates pods cleanly before the node powers off. This is a node-side prerequisite; styx has no visibility into it and does not configure it.

### File Layout

```
/opt/styx/styx.pyz                 # Self-contained executable (installed on every node)
/etc/styx/styx.conf                # Configuration (optional, overrides auto-discovery)
```

`styx.pyz` is a Python zipapp (stdlib `zipapp` module, Python 3.6+). It bundles the entire `styx/` package in a single executable file. The install script (`scripts/install.sh`) copies it to `/opt/styx/styx.pyz` on every cluster node via SSH.

All subcommands are available from the single file:
- `styx.pyz orchestrate` — main shutdown sequence (orchestrator only)
- `styx.pyz vm-shutdown <vmid> [timeout]` — single VM shutdown helper (all hosts)
- `styx.pyz local-shutdown [TYPE:]VMID... --timeout N [--poweroff-delay S]` — per-host workload shutdown with autonomous poweroff

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

### Local Shutdown (`styx local-shutdown`)

Dispatched by the orchestrator after the coordinated phase. Each host shuts down its own VMs autonomously.

**Usage:**
```bash
styx.pyz local-shutdown [TYPE:]VMID... --timeout 120 [--poweroff-delay 135] [--dry-run]
```

TYPE defaults to `qemu` if omitted. Examples: `qemu:101`, `lxc:200`, `301` (bare = qemu).

**How it works:**
1. Parse each argument as `(type, vmid)` and dispatch to the appropriate handler via `_SHUTDOWN`/`_CHECK` maps
2. Shut down all workloads in parallel using `ThreadPoolExecutor`
2. Collect results and log failures
3. If `--poweroff-delay` is set: sleep until the deadline, then `poweroff`

**Autonomous poweroff deadline:**
- Peers receive `--poweroff-delay` = `timeout_vm + 15` (ACPI wait + SIGTERM + SIGKILL)
- This is relative (seconds from dispatch), avoiding clock-sync issues
- In normal operation, the leader's polling loop powers off the peer *before* the deadline expires
- If the leader dies, the peer powers itself off after the deadline — safe because all VMs everywhere are guaranteed stopped by then (all were dispatched before the leader could die)

**The orchestrator** receives no `--poweroff-delay` — it powers off after the polling loop confirms all VMs are stopped.

The implementation lives in `styx/local_shutdown.py`. Logs to `/tmp/styx-local-shutdown.log` (collected by `poweroff_host()` before the host is powered off).

**Design note — why host poweroff is centrally coordinated, not autonomous:**

An earlier design had each host autonomously power itself off as soon as its own VMs were stopped — fully independent, no polling loop, no leader. This does not work when VMs run on Ceph storage. Ceph availability depends on a minimum number of OSDs (and therefore hosts) being up. If hosts that finish their VM shutdowns first power themselves off immediately, they take their OSDs down while VMs on slower hosts are still doing I/O during graceful shutdown. Those VMs stall on blocked storage, time out, and get force-killed — defeating the purpose of graceful shutdown.

The fix is the current design: VM shutdown is dispatched independently (each host shuts down its own VMs), but host poweroff is coordinated by the leader's polling loop, which waits until *all* VMs across the cluster are confirmed stopped before powering off *any* host. This guarantees that no Ceph OSD goes away while a VM anywhere is still running. The autonomous poweroff deadline on peers exists only as a leader-dead fallback — it is deliberately set long enough (`timeout_vm + 15s`) that all VMs everywhere are guaranteed stopped before any peer self-terminates.

Do not revisit the fully-autonomous approach without solving the Ceph storage dependency. Any design where a host can power off while VMs elsewhere are still running risks storage stalls and data loss.

### Kubernetes RBAC

A dedicated ServiceAccount with minimal permissions for drain operations. A long-lived token is placed on the orchestrator as a kubeconfig.

**ClusterRole permissions**:
- `nodes`: `get`, `list`, `patch` (for cordon/uncordon)
- `pods`: `get`, `list` (to discover pods on a node)
- `pods/eviction`: `create` (to evict pods during drain)
- `volumeattachments` (storage.k8s.io/v1): `get`, `list` (to detect stale CSI attachments post-drain)

## Shutdown Sequence

### Command Line

```
styx.pyz orchestrate [--mode <mode>] [--phase <1|2|3>] [--config <path>]
                     [--hosts HOST [HOST ...]] [--skip-poweroff]

  --mode <mode>           dry-run | emergency | maintenance  (default: emergency)
  --phase <1|2|3>         Execute up to and including this phase (default: 3)
  --config <path>         Config file path (default: /etc/styx/styx.conf)
  --hosts HOST [HOST ...]  Restrict to these hosts only (orchestrator always included)
  --skip-poweroff         Shut down VMs but do not power off any host
```

### Modes

Three mutually exclusive modes, implemented as three `Policy` subclasses in `styx/policy.py`:

| Mode | Class | Behaviour |
|------|-------|-----------|
| `emergency` | `Policy` | Execute automatically; `on_warning()` logs and continues; `phase_gate()` is a no-op. Default — designed for unattended UPS-triggered shutdowns. |
| `maintenance` | `MaintenancePolicy` | Pre-flight checks before any action; `on_warning()` prompts `[skip/abort]`; `phase_gate()` requires explicit confirmation before proceeding. Designed for planned maintenance. |
| `dry-run` | `DryRunPolicy` | `execute()` logs `[dry-run] <description>` and returns `None` without calling the function. Preflight checks run; `vm-shutdown --dry-run` is invoked synchronously on each peer to report real VM status without touching anything. |

**Maintenance mode detail:**

Before touching anything, `preflight()` runs and logs:
- SSH reachability to every non-orchestrator host
- Kubernetes API status + per-node drainable pod count (drain load estimate)
- Ceph health (`ceph health`)

`preflight()` also runs in **dry-run** mode.

Two phase gates prompt for confirmation:
1. After discovery + pre-flight: "N hosts, M VMs … proceed with shutdown?"
2. Before phase-3 powerdown: "about to set Ceph flags and power off all hosts — proceed?"

Any `on_warning()` call during execution (drain timeout, stale VolumeAttachment, SSH error) pauses and prompts `[skip/abort]`. A `threading.Lock` serialises concurrent prompts from parallel drain threads.

Both modes run **identical code paths**. `Policy.phase_gate()` and `Policy.on_warning()` are no-ops in emergency mode. This is intentional — maintenance mode is the primary way to exercise the emergency path against a real cluster.

### Phases

The shutdown is split into a **coordinated phase** (requires cluster APIs and quorum) and an **independent phase** (each host acts autonomously):

| Phase | Scope | What it does |
|-------|-------|-------------|
| 1     | Kubernetes | Drain k8s nodes, dispatch `local-shutdown` for k8s VMs |
| 2     | All VMs | Dispatch `local-shutdown` for all VMs, wait for all to stop |
| 3     | Hosts | Set Ceph OSD flags, dispatch with autonomous poweroff, power off hosts |

With `--phase N`, the script executes up to and including phase N.

### Pipeline

```
Time ->

STARTUP:
  auto-discover:      [pvesh cluster/status + cluster/resources, kubectl get nodes, pveceph status]
  hosts filter:       [_apply_hosts_filter(topo, --hosts)] (if --hosts specified; restricts topology)

COORDINATED PHASE (leader, requires quorum/API):
  cordon all k8s:     [kubectl cordon] (instant, prevents rescheduling before HA is disabled)
  disable HA:         [ha-manager set ... --state disabled]  (k8s scope for phase 1; all for phase 2+)
  drain all k8s:      [drain all workers + CP in parallel, no VM shutdown]

PHASE GATE (phase 3): "Drains complete — about to set Ceph flags, dispatch shutdown
  with autonomous poweroff. Proceed?"

INDEPENDENT PHASE:
  set Ceph flags:     [ceph osd set <flags>] (phase 3 only, moved after gate)
  dispatch shutdown:  [styx local-shutdown <vmids> per host, one SSH per peer]
                      peers get --poweroff-delay (timeout_vm + 15s) as leader-dead fallback
                      orchestrator gets no delay (powers off after polling loop)
  polling loop:       every 10s check PID files, poweroff hosts as VMs stop (phase 3)
  post-loop:          poweroff orchestrator (self, always last)

PEER (normal case):
  local-shutdown → shut down VMs → [leader sends poweroff before deadline]

PEER (leader-dead fallback):
  local-shutdown → shut down VMs → sleep until deadline → power off self
```

### Phase Control

| Flag | Coordinated | Dispatch | Ceph flags | Polling | Poweroff |
|------|-------------|----------|------------|---------|----------|
| `--phase 1` | HA (k8s), cordon, drain | k8s VMs only, no poweroff delay | skip | skip | skip |
| `--phase 2` | HA (all), cordon, drain | all VMs, no poweroff delay | skip | poll, wait for all VMs | skip |
| `--phase 3` (default) | HA (all), cordon, drain | all VMs, with poweroff delay | set flags | poll, **poweroff hosts** | poweroff orchestrator |

Notes:
- Phase 1 dispatches `local-shutdown` for k8s VMs only (fire-and-forget). Does **not** wait for them to stop. HA is disabled for k8s VMIDs only.
- Phase 2 widens HA disable to all resources, dispatches all VMs, and runs the polling loop.
- Phase 3 adds Ceph flags (after the phase gate, before dispatch) and host poweroff with autonomous fallback.
- Cordon always runs regardless of phase (idempotent prerequisite).
- If `[kubernetes]` is not configured, drain is skipped entirely.

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
- Phase 1: scoped to k8s VMIDs only (`ha-manager set vm:<vmid> --state disabled` for each k8s worker/CP)
- Phase 2+: all HA-managed resources disabled regardless of VM type
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

### Runtime Budget

Many operations run in parallel, so the worst-case formula is simpler than the number of individual timeouts suggests.

#### Phase structure

```
sequential:  HA disable → drain → VM shutdown wait → polling
parallel:    all k8s drains run concurrently
             all VM shutdowns run concurrently (fire-and-forget)
             Track A (k8s) and Track B (non-k8s) run concurrently
```

#### Worst-case formula (phase 3)

| Step | Duration | Notes |
|------|----------|-------|
| HA disable | N × 30s | Sequential per resource; usually completes in < 5s each |
| k8s drain | `timeout_drain` | All nodes drain in parallel; single shared timeout |
| VM shutdown + escalation | `timeout_vm` + 15s | ACPI wait + SIGTERM 10s + SIGKILL 5s |
| Polling detection | `poll_interval` | One cycle to detect completion |

```
worst_case = timeout_drain + timeout_vm + 15 + poll_interval
```

With defaults (drain=120, vm=120, poll=10): **4m 25s** (excluding HA disable, which is topology-dependent).

Without Kubernetes (no drain phase): `timeout_vm + 15 + poll_interval` = **2m 25s**.

Why drain and VM shutdown are sequential: CP VM shutdowns are deferred until after all drains complete (the API server must remain available for drain evictions). The last VM shutdown is therefore dispatched at time `timeout_drain`, and the polling loop waits up to `timeout_vm + 15` seconds after that.

Worker VMs and non-k8s VMs begin shutting down earlier (during or before the drain phase), so they overlap with the drain time and are never the bottleneck.

#### Preflight display

In `maintenance` and `dry-run` modes, styx calculates and displays the worst-case runtime before the confirmation prompt:

```
--- Runtime budget (worst case) ---
  k8s drain (all nodes parallel): 120s
  VM shutdown + escalation: 135s
  Polling detection: 10s
  Total: 4m 25s
```

This gives the admin a concrete number to compare against their UPS battery estimate before confirming.

#### All timeouts

| Timeout | Default | Configurable | Purpose |
|---------|---------|-------------|---------|
| `timeout_drain` | 120s | `[timeouts] drain` | Max time for `kubectl drain` per node (all nodes drain in parallel) |
| `timeout_vm` | 120s | `[timeouts] vm` | ACPI graceful shutdown wait per VM |
| SIGTERM grace | 10s | No | Grace period after SIGTERM before SIGKILL |
| SIGKILL grace | 5s | No | Final check after SIGKILL |
| HA transition | 30s | No | Wait for `ha-manager set --state disabled` per resource |
| Poll interval | 10s | `STYX_POLL_INTERVAL` env var | Polling loop sleep between VM status checks |
| K8s drain poll | 2s | No | Sleep between pod eviction checks during drain |
| K8s API timeout | 10s | No | HTTP timeout for Kubernetes API calls |
| SSH command timeout | 30s | No | Subprocess timeout for SSH commands |
| QMP socket timeout | 5s | No | QMP socket connect/recv timeout |

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

> **Tip:** styx logs an exact startup checklist before powering off the orchestrator — check `/var/log/styx.log` for the `--- Shutdown complete — startup checklist ---` entry. For `--hosts` partial runs, a revert checklist is logged at the end of the run instead.

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
├── __main__.py                       # CLI dispatch (orchestrate | vm-shutdown | local-shutdown)
├── policy.py                         # DryRunPolicy, Policy, MaintenancePolicy + log()
├── config.py                         # StyxConfig dataclass + INI parser
├── discover.py                       # pure parsing functions + ClusterTopology
├── classify.py                       # VMID classification (k8s-worker/cp/other)
├── decide.py                         # phase predicates (should_disable_ha, etc.)
├── k8s.py                            # K8sClient (cordon, drain, list nodes, etc.)
├── vm_shutdown.py                    # QMP + PID escalation (no socat)
├── local_shutdown.py                 # per-host VM shutdown + autonomous poweroff
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
│   ├── test_config.py                    # INI parsing
│   ├── test_discover.py                  # pvesh/kubectl JSON parsing, name matching
│   ├── test_classify.py                  # VMID classification
│   ├── test_decide.py                    # phase predicates
│   ├── test_k8s.py                       # K8sClient (cordon, drain, mirror pods, etc.)
│   ├── test_policy.py                    # DryRunPolicy, Policy, MaintenancePolicy
│   ├── test_vm_shutdown.py               # QMP mock server, PID escalation, check()
│   ├── test_local_shutdown.py            # parallel shutdown, dry-run, poweroff deadline
│   ├── test_wrappers_parsing.py          # _parse_ha_status, _parse_running_vmids, Operations commands
│   ├── test_orchestrate_discover.py      # discover() with injected pvesh/pveceph fns
│   ├── test_orchestrate_preflight.py     # preflight() SSH, k8s, Ceph checks
│   └── test_orchestrate_hosts_filter.py  # _apply_hosts_filter()
└── integration/
    ├── helpers.py                    # FakeOperations + fake VM (sleep + PID files)
    └── test_full_sequence.py         # end-to-end with injected fakes
scripts/
├── build.sh                          # builds styx.pyz zipapp
└── install.sh                        # installs styx.pyz on all cluster nodes
.github/workflows/
├── test.yml                          # CI: unittest + coverage upload to Codecov
└── release.yml                       # publishes styx.pyz + install.sh on v* tags
```

### Layer Separation

**Layer 1 — Pure functions** (`styx/discover.py`, `styx/classify.py`, `styx/decide.py`):

No side effects. Take data in, return decisions. Directly testable.

```python
# styx/config.py
load_config(path) -> StyxConfig       # INI file -> dataclass (all sections optional)

# styx/discover.py
parse_cluster_status(data)            # pvesh cluster/status JSON -> (host_ips, orchestrator)
parse_cluster_resources(data)         # pvesh cluster/resources JSON -> (vm_host, vm_name, vm_type)
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
ops.check_vm(host, vmid)                       # vm-shutdown --dry-run (sync); used in dry-run mode
ops.shutdown_vm(host, vmid, timeout)           # nohup styx vm-shutdown (fire-and-forget)
ops.dispatch_local_shutdown(host, workloads, ...)  # nohup styx local-shutdown (one per host); workloads=[(type, vmid), ...]
ops.cordon_node(node)                          # kubectl cordon via K8sClient
ops.drain_node(node, timeout) -> bool          # kubectl drain via K8sClient
ops.list_volume_attachments_for_node(node)     # CSI VolumeAttachment check post-drain
ops.get_ha_started_sids()                      # ha-manager status -> started SIDs
ops.disable_ha_sid(sid)                        # ha-manager set --state disabled
ops.wait_ha_disabled(sid, timeout) -> bool     # poll until disabled or timeout
ops.set_ceph_flags(flags)                      # ceph osd set <flag> for each flag
ops.poweroff_host(host)                        # ssh: collect shutdown logs then poweroff
ops.poweroff_self()                            # poweroff (orchestrator self)
```

**Layer 3 — Orchestration** (`styx/orchestrate.py`):

`main()` accepts `_discover_fn` and `_ops_factory` as keyword-only parameters for test injection.

Key helpers:
- `_drain_all_k8s(topo, config, ops, policy)` — drain all k8s nodes in parallel (no VM shutdown)
- `_dispatch_independent_phase(topo, config, ops, policy, do_poweroff, vm_filter)` — dispatch `local-shutdown` to each host
- `_apply_hosts_filter(topo, hosts)` — restricts topology to targeted hosts; orchestrator always kept reachable but its VMs only included if explicitly listed
- `_log_revert_summary(topo, args, ceph_flags)` — logged at end of `--hosts` runs with exact commands to restore cluster state
- `_log_startup_checklist(topo, ceph_flags)` — logged before `poweroff_self` in full runs with post-restart commands

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
| QMP socket communication | Covered by `test_vm_shutdown.py` with a Unix-socket mock server; also test on Proxmox |
| kubectl drain behavior | `--mode dry-run --phase 1` on real cluster |
| SSH connectivity | `--mode maintenance` pre-flight checks SSH reachability; `--hosts` partial run exercises real SSH |
| Ceph OSD flag behavior | Idempotent, safe to test live; `--hosts` partial run uses `noout` only |
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

- **VMs only** (for now): LXC containers and Proxmox 9 OCI containers are not gracefully stopped. The workload type infrastructure is in place (`vm_type` threaded through discovery, dispatch, CLI, and polling; `local_shutdown.py` uses dispatch maps keyed by type), so adding a new workload requires implementing a handler, registering it, and widening the discovery filter. See Future Work for concrete next steps.
- **Ceph on hosts only**: Ceph-in-VM topologies are not supported. Ceph OSD flags are set after VM shutdown commands are issued (before any host goes down), which is correct for on-host Ceph.
- **No single-node support (v1)**: Styx assumes a multi-node Proxmox cluster. Single-node is a simpler problem and could be a stretch goal for v2.
- **VM migration**: Do not run styx while a VM live migration is in progress. The VMID-to-host mapping is captured once at startup and not refreshed. A migrating VM may receive shutdown commands on the wrong host. The `pvesh` resource data includes a `status` field that could potentially detect migrations — this is a future enhancement if needed.
- **Orphaned shutdown processes**: If the main `styx` script is killed, backgrounded `styx-vm-shutdown` processes continue running. This is intentional — they will complete their VM shutdowns independently.
- **CephFS teardown**: Clusters running CephFS could benefit from an explicit `ceph fs fail` + `ceph fs set cluster_down true` before shutdown, reversed on startup. Not implemented; CephFS clusters should verify filesystem health after recovery.
- **MON-last host ordering**: Powering off the Proxmox host running the Ceph MON last would be ideal to maintain Ceph quorum as long as possible. This requires Ceph topology awareness (which hosts run MONs) that styx does not currently have. The polling loop powers off hosts as their VMs stop, which is correct but not MON-aware.

## Future Work

### LXC Container Support

**What's known:**
- `lxc-stop -n <CTID> -t <timeout>` is quorum-free and handles the full graceful→force lifecycle.
- `lxc-ls --running` detects running containers without quorum.
- `pct shutdown`/`pct stop` require Proxmox quorum — unusable during shutdown when quorum may be lost.

**What needs hardware verification:**
- PID file paths: container init PID is tracked under `/var/run/lxc/<CTID>/` but the exact layout needs verification on real Proxmox hardware to confirm PID-based status checks (analogous to `/var/run/qemu-server/<VMID>.pid` for QEMU).
- Signal behavior: whether `lxc-stop` signal escalation matches the ACPI→SIGTERM→SIGKILL pattern used for VMs.

**Implementation steps:**
1. Implement `ct_shutdown.py` with `shutdown(ctid, timeout)` and `check(ctid)` using `lxc-stop` and PID-based polling.
2. Register the handler in `local_shutdown._SHUTDOWN` / `_CHECK` dispatch maps.
3. Widen `parse_cluster_resources()` filter to include `type == "lxc"`.
4. Add `lxc-ls --running` parsing to `get_running_vmids()` for PID-free status checks.

### OCI Container Support (Proxmox 9)

Proxmox 9 adds OCI container support. The runtime is `crun`/`runc`. Quorum-free termination path is unclear — needs investigation on Proxmox 9 hardware to determine whether a local CLI or socket interface can stop containers without Proxmox API calls. Same implementation pattern as LXC once the quorum-free mechanism is identified.

### Preflight Container Warning (Short-Term)

Detect LXC and OCI containers in the `pvesh get /cluster/resources --type vm` output during preflight and emit a warning: "Found N LXC/OCI containers that will not be gracefully stopped." No new API calls needed — the data is already available from the existing discovery call (`parse_cluster_resources()` currently filters to `type == "qemu"` and discards the rest).

### Other

- **Single-node support**: Out of scope for v1; simpler problem — stretch goal for v2 (see Known Limitations).
- **CephFS teardown**: Explicit `ceph fs fail` + `ceph fs set cluster_down true` before shutdown, reversed on startup (see Known Limitations).
- **MON-aware host ordering**: Power off Ceph MON hosts last to maintain Ceph quorum longer; requires Ceph topology awareness (see Known Limitations).
