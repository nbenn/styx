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
| K8s worker/CP VMIDs | `lib/k8s.py get-nodes` — match node names to VM names, classify by `node-role.kubernetes.io/control-plane` label | `[kubernetes] workers, control_plane` |
| K8s credentials | `[kubernetes] server` + `token` (required for any k8s integration) | — |
| Ceph enabled | `pveceph status` exits 0 | `[ceph] enabled` |
| Ceph flags | defaults: `noout, norecover, norebalance, nobackfill, nodown, noup` | `[ceph] flags` |
| Timeouts | defaults: drain=120, vm=120 | `[timeouts]` |

### Startup Logic

1. **Hosts**: `pvesh get /cluster/status --output-format json` → filter `type == "node"` → extract `name` and `ip`. The entry with `local == 1` is the orchestrator. If `[hosts]` is in config, use that instead.
2. **VMs**: `pvesh get /cluster/resources --type vm --output-format json` → filter `type == "qemu"` (excludes LXC containers), build VMID-to-host and VMID-to-name maps. Filters out templates (`template == 1`) and stopped VMs.
3. **Kubernetes**: if `[kubernetes] server` and `token` are configured, try `lib/k8s.py get-nodes`.
   - If reachable: extract node names and roles. Match node names against VM names from step 2. Workers = nodes without `control-plane` role. CP = nodes with it.
   - If name matching fails (no VM name matches any node name) → **abort with error**, ask user to provide `workers` and `control_plane` in config.
   - If `server`/`token` not configured and no `workers`/`control_plane` in config → skip k8s entirely (Proxmox-only mode).
   - If `server`/`token` configured but API unreachable → skip k8s (re-run scenario where k8s VMs are already off).
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
# Override auto-discovered k8s node classification
# Default kubeconfig: /root/.kube/config
kubeconfig = /etc/styx/kubeconfig
workers = 211, 212, 213, 214, 215
control_plane = 201, 202, 203

[ceph]
# Override auto-detection (pveceph status)
enabled = true
# Override default flags
flags = noout, norecover, norebalance, nobackfill, nodown, noup

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
- **socat** installed on all Proxmox hosts (standard on Proxmox)
- **python3** on the orchestrator (standard on Proxmox; used for JSON parsing and the Kubernetes API client)
- **ceph** CLI on the orchestrator or a Ceph node (if using Ceph)

### File Layout

```
/usr/local/bin/styx                # Main shutdown script (orchestrator only)
/usr/local/bin/styx-vm-shutdown    # VM shutdown helper (all Proxmox hosts)
/etc/styx/styx.conf                # Configuration (optional, overrides auto-discovery)
```

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
styx-vm-shutdown <vmid> [timeout]    # default timeout: 120s
```

**Implementation:**
```bash
#!/bin/bash
# styx-vm-shutdown — Quorum-free VM shutdown via QMP + PID
set -euo pipefail

VMID=${1:?Usage: styx-vm-shutdown <vmid> [timeout]}
TIMEOUT=${2:-120}

QMP="/var/run/qemu-server/${VMID}.qmp"
PIDFILE="/var/run/qemu-server/${VMID}.pid"

# Check if VM is running
if [[ ! -f "$PIDFILE" ]] || ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "VM ${VMID} is not running"
  exit 0
fi

# Send ACPI power button via QMP (system_powerdown).
# QMP requires qmp_capabilities before any command. Sleep gives QEMU time
# to process capabilities before the command arrives.
echo "VM ${VMID}: sending ACPI powerdown via QMP"
{ printf '{"execute":"qmp_capabilities"}\n'
  sleep 0.3
  printf '{"execute":"system_powerdown"}\n'
} | socat -t 3 - UNIX-CONNECT:"${QMP}" >/dev/null 2>&1 || true

# Poll PID until stopped or timeout
count=0
while [[ $count -lt $TIMEOUT ]]; do
  if ! kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
    echo "VM ${VMID} stopped gracefully"
    exit 0
  fi
  sleep 1
  count=$((count + 1))
done

# Escalate: SIGTERM
echo "VM ${VMID} timeout after ${TIMEOUT}s, sending SIGTERM"
kill -15 "$(cat "$PIDFILE")" 2>/dev/null || true
count=0
while [[ $count -lt 10 ]]; do
  if ! kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
    echo "VM ${VMID} stopped after SIGTERM"
    exit 0
  fi
  sleep 1
  count=$((count + 1))
done

# Last resort: SIGKILL
echo "VM ${VMID} still running, sending SIGKILL"
kill -9 "$(cat "$PIDFILE")" 2>/dev/null || true
echo "VM ${VMID} force-killed"
```

**Dependencies**: `socat` (standard on Proxmox).

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
styx [options]

Options:
  --dry-run        Log all actions without executing them
  --phase <1|2|3>  Execute up to and including this phase (default: 3)
  --config <path>  Config file path (default: /etc/styx/styx.conf)
```

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
- Phase 1 issues `styx-vm-shutdown` for k8s VMs (fire-and-forget). Does **not** wait for them to stop. HA is **not** disabled — only k8s VMs are affected, and k8s VMs are typically not HA-managed.
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

Implementation: `exec > >(tee -a /var/log/styx.log)` at script start with a timestamped prefix on each line. Each run starts with a separator header for readability in the log file.

The primary use case is post-mortem analysis after a UPS-triggered shutdown. When called interactively, stdout provides the same output.

## Startup Recovery (Manual)

After power is restored:

1. **Power on Proxmox hosts** (manually or via IPMI/iLO)

2. **Unset Ceph OSD flags** (if Ceph is enabled):
   ```bash
   for flag in noout norecover norebalance nobackfill nodown noup; do
     ceph osd unset "$flag"
   done
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

Tests use [bats](https://github.com/bats-core/bats-core) (Bash Automated Testing System).

### Code Structure

```
styx/
├── bin/
│   ├── styx                          # main orchestrator (sources lib/)
│   └── styx-vm-shutdown              # VM shutdown helper
├── lib/
│   ├── config.sh                     # INI parser (optional config overrides)
│   ├── discover.sh                   # auto-discovery (hosts, VMs, k8s, ceph)
│   ├── classify.sh                   # VMID classification, node-to-VM matching
│   ├── decide.sh                     # decision logic (should_poweroff, etc.)
│   └── wrappers.sh                   # thin wrappers around external commands
├── test/
│   ├── unit/
│   │   ├── config.bats               # INI parsing
│   │   ├── discover.bats             # pvesh/kubectl JSON parsing, name matching
│   │   ├── classify.bats             # VMID classification
│   │   ├── decide.bats               # host poweroff, shutdown decisions
│   │   ├── phase_control.bats        # --phase and --dry-run logic
│   │   └── vm_shutdown.bats          # styx-vm-shutdown PID/signal logic
│   ├── integration/
│   │   ├── fake_env.sh               # sets up fake PID files, mock wrappers
│   │   ├── full_sequence.bats        # end-to-end with fake env
│   │   └── idempotency.bats          # re-run scenarios
│   └── test_helper/
│       └── bats-support/             # bats assertion libs (git submodule)
├── .github/workflows/
│   └── test.yml                      # CI: install bats, run tests
├── styx.conf.example                 # Example config (all sections optional)
└── README.md
```

### Layer Separation

**Layer 1 — Pure functions** (`lib/discover.sh`, `lib/classify.sh`, `lib/decide.sh`):

No side effects. Take data in, return decisions. Directly testable.

```bash
# lib/config.sh
parse_config()              # INI file -> shell variables (all optional overrides)

# lib/discover.sh
parse_cluster_status()      # pvesh cluster/status JSON -> host names, IPs, local flag
parse_cluster_resources()   # pvesh cluster/resources JSON -> VMID-to-host, VMID-to-name (filter type=="qemu")
parse_kubectl_nodes()       # kubectl get nodes JSON -> node names + roles
match_nodes_to_vms()        # node names + VM names -> VMID classification (worker/CP)

# lib/classify.sh
classify_vmid()             # vmid + worker/CP sets -> "k8s-worker", "k8s-cp", "other"
get_k8s_workers()           # from vmid list -> worker VMIDs
get_k8s_cp()                # from vmid list -> CP VMIDs
get_other_vms()             # from vmid list -> non-k8s VMIDs

# lib/decide.sh
should_poweroff_host()      # host, its VMIDs, running set -> yes/no
should_disable_ha()         # phase -> yes/no
should_run_polling()        # phase -> yes/no
should_poweroff_hosts()     # phase -> yes/no
```

**Layer 2 — Thin wrappers** (`lib/wrappers.sh`):

One-line functions calling external commands. Overridden with fakes in tests.

```bash
run_on_host()          # ssh -o ConnectTimeout=5 root@$host "$cmd" (or local if self)
get_running_vms()      # scan PID files on a host
drain_node()           # lib/k8s.py drain <node> --timeout=...
shutdown_vm()          # styx-vm-shutdown (local or via SSH)
cordon_node()          # lib/k8s.py cordon <node>
is_api_reachable()     # lib/k8s.py reachable
get_k8s_nodes()        # lib/k8s.py get-nodes → "name role" pairs
set_ceph_flags()       # ceph osd set <flags>
disable_ha()           # ha-manager status + set --state disabled
poweroff_host()        # ssh root@$host poweroff
```

**Layer 3 — Orchestration** (`bin/styx`):

Sources `lib/` and wires everything together. Tested via integration tests with fake wrappers.

### Unit Tests

Test layer-1 functions with synthetic data — no mocking needed.

```bash
# test/unit/discover.bats
@test "parse_cluster_status extracts hosts and IPs" {
  source lib/discover.sh
  local json='[
    {"type":"cluster","name":"mycluster"},
    {"type":"node","name":"pve1","ip":"10.0.0.1","local":1,"online":1},
    {"type":"node","name":"pve2","ip":"10.0.0.2","local":0,"online":1}
  ]'
  parse_cluster_status "$json"
  [[ "${HOST_IPS[pve1]}" == "10.0.0.1" ]]
  [[ "${HOST_IPS[pve2]}" == "10.0.0.2" ]]
  [[ "$ORCHESTRATOR" == "pve1" ]]
}

@test "match_nodes_to_vms classifies by role" {
  source lib/discover.sh
  # Simulate: kubectl nodes with names matching VM names
  local -A vm_names=([201]="cp1" [211]="worker1")
  local -A node_roles=([cp1]="control-plane" [worker1]="")
  match_nodes_to_vms vm_names node_roles
  [[ "${K8S_CP[*]}" == *"201"* ]]
  [[ "${K8S_WORKERS[*]}" == *"211"* ]]
}

@test "match_nodes_to_vms fails when no names match" {
  source lib/discover.sh
  local -A vm_names=([201]="my-vm-1")
  local -A node_roles=([cp1]="control-plane")
  run match_nodes_to_vms vm_names node_roles
  [[ "$status" -ne 0 ]]
}

# test/unit/classify.bats
@test "VMID in workers list classified as k8s-worker" {
  source lib/classify.sh
  STYX_WORKERS="211 212 213"
  run classify_vmid 211
  [[ "$output" == "k8s-worker" ]]
}

# test/unit/decide.bats
@test "host with all VMs stopped should be powered off" {
  source lib/decide.sh
  local -A host_vms=([pve3]="103 104")
  local running=()
  run should_poweroff_host "pve3" host_vms running
  [[ "$output" == "yes" ]]
}

@test "phase 1 skips HA disable" {
  source lib/decide.sh
  run should_disable_ha 1
  [[ "$output" == "no" ]]
}
```

### Integration Tests

Full orchestration with fake wrappers — no real SSH, QMP, or kubectl. VMs simulated as backgrounded `sleep` processes with PID files.

```bash
@test "phase 3: all VMs stop, hosts powered off, orchestrator last" {
  run bin/styx --phase 3 --config "$FAKE_CONFIG"
  local last_line=$(tail -1 "$FAKE_ROOT/poweroff.log")
  [[ "$last_line" == "POWEROFF pve1" ]]
}

@test "dry-run: no VMs stopped, no hosts powered off" {
  run bin/styx --dry-run --phase 3 --config "$FAKE_CONFIG"
  for pid in "${FAKE_PIDS[@]}"; do
    kill -0 "$pid" 2>/dev/null
  done
}

@test "phase 1 then phase 3: full sequence completes" {
  bin/styx --phase 1 --config "$FAKE_CONFIG"
  is_api_reachable() { return 1; }
  export -f is_api_reachable
  run bin/styx --phase 3 --config "$FAKE_CONFIG"
  [[ "$status" -eq 0 ]]
}
```

### CI

```yaml
name: Test
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install bats
        run: |
          git clone https://github.com/bats-core/bats-core.git
          cd bats-core && sudo ./install.sh /usr/local
      - name: Run unit tests
        run: bats test/unit/
      - name: Run integration tests
        run: bats test/integration/
```

### What's NOT Tested (requires real infrastructure)

| Concern | Mitigation |
|---------|------------|
| QMP socket communication | Manual test on Proxmox, `--dry-run` |
| kubectl drain behavior | `--dry-run --phase 1` on real cluster |
| SSH connectivity | `--dry-run` shows SSH commands |
| Ceph OSD flag behavior | Idempotent, safe to test live |
| ha-manager interaction | `--dry-run`, verify manually |
| Actual VM shutdown timing | Tune timeouts based on observation |

## Open Issues

No blocking issues remain. All open items have been resolved — see design decisions below.

### Resolved: QGA vs QMP

Investigated both QGA (`guest-shutdown`) and QMP (`system_powerdown`). Decision: **QMP only**.

QGA requires the guest agent to be installed and running inside the VM, uses a separate socket and protocol (`guest-sync-delimited` handshake), and the socket's existence doesn't guarantee agent availability. QMP `system_powerdown` sends an ACPI power button event at the hypervisor level — works regardless of guest agent status. All modern Linux and Windows guests handle ACPI shutdown. Any VM that doesn't will be killed when the host powers off.

QMP-only is simpler (one socket, one protocol, one code path) and more reliable (no dependency on guest-side software).

### Resolved: QMP handshake reliability

QMP requires `qmp_capabilities` before any command. The greeting is sent by QEMU immediately on connect and sits in the socket send buffer — it does not need to be read before sending commands.

**Pipelining works in practice:** QEMU buffers socket input and processes commands sequentially from the buffer. The `printf | socat` one-shot pattern is widely used on Proxmox forums. However, Proxmox itself uses strict request-response (via `IO::Multiplex`) because its QMP client is general-purpose.

**For safety on loaded systems**, a short `sleep 0.3` between `qmp_capabilities` and the actual command ensures QEMU has processed capabilities before the command arrives:

```bash
{ printf '{"execute":"qmp_capabilities"}\n'; sleep 0.3; printf '{"execute":"system_powerdown"}\n'; } | \
  socat -t 3 - UNIX-CONNECT:"${QMP}"
```

Source: Proxmox `PVE/QMPClient.pm`, QEMU QMP specification, Proxmox forum patterns.

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

## Known Limitations

- **VMs only**: LXC containers and Proxmox 9 OCI containers are not supported. The architecture allows adding a `styx-ct-shutdown` helper later.
- **Ceph on hosts only**: Ceph-in-VM topologies are not supported. Ceph OSD flags are set after VM shutdown commands are issued (before any host goes down), which is correct for on-host Ceph.
- **No single-node support (v1)**: Styx assumes a multi-node Proxmox cluster. Single-node is a simpler problem and could be a stretch goal for v2.
- **VM migration**: Do not run styx while a VM live migration is in progress. The VMID-to-host mapping is captured once at startup and not refreshed. A migrating VM may receive shutdown commands on the wrong host. The `pvesh` resource data includes a `status` field that could potentially detect migrations — this is a future enhancement if needed.
- **Orphaned shutdown processes**: If the main `styx` script is killed, backgrounded `styx-vm-shutdown` processes continue running. This is intentional — they will complete their VM shutdowns independently.
