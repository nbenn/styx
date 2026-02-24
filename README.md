# Styx

[![CI](https://github.com/nbenn/styx/actions/workflows/test.yml/badge.svg)](https://github.com/nbenn/styx/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/nbenn/styx/graph/badge.svg)](https://codecov.io/gh/nbenn/styx)

Graceful cluster shutdown for Proxmox + Kubernetes + Ceph.

Styx orchestrates a safe, ordered shutdown of your entire infrastructure stack — Kubernetes nodes first, then all VMs, then Ceph flags, then Proxmox hosts — designed to complete within a UPS battery window (typically 5–10 minutes).

## How it works

Styx runs in three phases:

| Phase | What happens |
|-------|-------------|
| 1 | Drain Kubernetes nodes, issue VM shutdown for k8s VMs |
| 2 | Shut down all remaining VMs, wait for all to stop |
| 3 | Set Ceph OSD flags, power off Proxmox hosts |

Phases 1 and 2 run concurrently as two parallel tracks. VM shutdowns use the QEMU QMP socket directly — bypassing `qm shutdown` and the Proxmox API — so the script keeps working even after cluster quorum is lost.

## Requirements

- Proxmox cluster with SSH between all hosts (root, key-based)
- `python3` on all Proxmox hosts (standard on Proxmox)

## Installation

Download the latest `styx.pyz` release artifact and place it on a shared Proxmox snippets storage pool (NFS or CephFS configured with `content snippets`). Because the storage is shared, the file is available at the same path on every node without per-node installation.

First, find the path of your shared snippets storage:

```bash
# List storage pools that have snippets content enabled
pvesm status --content snippets
# Then find the mount path for the shared pool you want to use:
pvesm path <storage-name>
```

Then download the artifact into that directory:

```bash
SNIPPETS=/path/to/your/snippets   # e.g. /mnt/pve/cephfs/snippets
curl -L https://github.com/nbenn/styx/releases/latest/download/styx.pyz \
    -o "${SNIPPETS}/styx.pyz"
chmod +x "${SNIPPETS}/styx.pyz"

# Optional: copy config if you need to override auto-discovery
cp styx.conf.example /etc/styx/styx.conf
```

Both subcommands (`orchestrate` and `vm-shutdown`) are bundled in the single `styx.pyz` file and available on all nodes via the shared path.

## Usage

```
styx.pyz orchestrate [--mode <mode>] [--phase <1|2|3>] [--config <path>]
                     [--hosts HOST [HOST ...]] [--skip-poweroff]

Modes:
  emergency    Execute automatically, warn and continue on failures (default)
  maintenance  Pre-flight checks + interactive gates between phases
  dry-run      Log all planned actions without executing anything

Options:
  --phase <1|2|3>        Execute up to and including this phase (default: 3)
  --config <path>        Config file path (default: /etc/styx/styx.conf)
  --hosts HOST [HOST ...]  Restrict to these hosts only (orchestrator always included)
  --skip-poweroff        Shut down VMs but do not power off any host
```

Typical invocations:

```bash
# Full shutdown (all phases)
styx.pyz orchestrate

# Walk through pre-flight and confirm each phase interactively
styx.pyz orchestrate --mode maintenance

# See what would happen without doing anything
styx.pyz orchestrate --mode dry-run

# Drain k8s and shut down k8s VMs only
styx.pyz orchestrate --phase 1

# Re-run phase 3 after a partial shutdown (k8s already down)
styx.pyz orchestrate --phase 3

# Partial run: test shutdown sequence on one host without touching the rest
styx.pyz orchestrate --mode maintenance --hosts pve3 --skip-poweroff

# Same but also power off pve3 at the end
styx.pyz orchestrate --mode maintenance --hosts pve3
```

### Modes

**Emergency** (default) is designed for unattended UPS-triggered shutdowns: every step logs a warning on failure and moves on, with no human in the loop.

**Maintenance** is for planned shutdowns. Before touching anything it runs a pre-flight check — SSH reachability to all hosts, Kubernetes API status with per-node drain estimates, and Ceph health — and displays the results. It then prompts for confirmation before proceeding. Any warning during execution (drain timeout, stale VolumeAttachment, etc.) pauses and asks whether to skip or abort. A second confirmation gate sits before the final host powerdown.

Both modes execute identical code paths, making maintenance mode a reliable way to exercise the emergency path against a real cluster.

**Dry-run** logs every planned action with a `[dry-run]` prefix and skips execution entirely. It also runs preflight checks and invokes `vm-shutdown --dry-run` on each peer to report real VM running status — making it as close to a real run as possible without modifying any state.

### Testing on a live cluster

The recommended progression before a first real run:

| Step | Command | What it validates |
|------|---------|------------------|
| 1 | `--mode dry-run` | Discovery, sequencing, SSH reachability, real VM status on all peers |
| 2 | `--mode maintenance --hosts pve3 --skip-poweroff` | Full VM shutdown sequence on one host; nothing powered off |
| 3 | `--mode maintenance --hosts pve3` | Same, plus power off pve3 (reboot manually to restore) |
| 4 | Full run | The real thing |

Choose a host with no critical services for the partial test (avoid the sole control-plane node or the host running all Ceph MONs if possible). At the end of every `--hosts` run, styx logs a **revert checklist** with the exact commands needed to restore normal cluster state:

```
--- Partial run complete — revert checklist ---
  Ceph OSD flags set: noout
    → ceph osd unset noout
  k8s nodes cordoned: k8s-cp-1
    → kubectl uncordon k8s-cp-1
  VM(s) stopped (host NOT powered off): 301 302
    → qm start 301 302
```

Note: restarting VMs, re-enabling HA, and uncordoning nodes is the operator's responsibility. Styx does not auto-revert.

## Configuration

For standard setups, **no config file is needed**. Styx auto-discovers hosts, VMs, Kubernetes nodes, and Ceph from the cluster.

Override only what differs:

```ini
# /etc/styx/styx.conf

# If SSH IPs differ from corosync IPs
[hosts]
pve1 = 192.168.1.10
pve2 = 192.168.1.11
pve3 = 192.168.1.12

# If Kubernetes node names don't match Proxmox VM names
[kubernetes]
workers = 211, 212, 213
control_plane = 201, 202, 203

# Override Ceph flags for partial --hosts runs (default: noout only)
[ceph]
partial_flags = noout

# Adjust timeouts (seconds)
[timeouts]
drain = 60
vm = 90
```

See [`styx.conf.example`](styx.conf.example) for the full reference.

## Triggering

Styx is a command, not a daemon. Wire it to your trigger of choice:

- **NUT** (Network UPS Tools): add `SHUTDOWNCMD "/path/to/snippets/styx.pyz orchestrate"` to `upsmon.conf`
- **Manual**: run `styx.pyz orchestrate` directly on the orchestrator
- **Cron/systemd**: call from a shutdown script

## Logging

All actions are logged to both stdout and `/var/log/styx.log` with timestamps. Each run appends a separator header, making the log useful for post-mortem analysis after a UPS-triggered shutdown.

## Recovery

After power is restored:

> **Tip:** styx logs an exact startup checklist before powering off the orchestrator — check `/var/log/styx.log` for the `--- Shutdown complete — startup checklist ---` entry to get the precise commands for your run.

1. Boot Proxmox hosts (via IPMI/iLO or physically)
2. Unset Ceph OSD flags: `for f in noout norecover norebalance nobackfill nodown; do ceph osd unset $f; done`
3. Re-enable HA: `ha-manager set <sid> --state started`
4. Start VMs (infra → k8s control plane → workers)
5. Uncordon k8s nodes: `kubectl uncordon --all`

## Testing

```bash
python3 -m unittest discover -s test/unit -p 'test_*.py'
python3 -m unittest discover -s test -p 'test_*.py'
```

Unit tests cover pure decision logic and fixture-based parsing (no infrastructure needed). Integration tests run a full shutdown sequence using fake wrappers and simulated PID files.

## Scope and limitations

- **VMs only**: LXC and OCI containers are not supported
- **Ceph on Proxmox hosts only**: Ceph-in-VM is not supported
- **Multi-node clusters only**: single-node Proxmox is not supported in v1
- **No live migration safety**: do not run styx while a VM migration is in progress
