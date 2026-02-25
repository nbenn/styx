# Styx

[![CI](https://github.com/nbenn/styx/actions/workflows/test.yml/badge.svg)](https://github.com/nbenn/styx/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/nbenn/styx/graph/badge.svg)](https://codecov.io/gh/nbenn/styx)

Graceful cluster shutdown for Proxmox + Kubernetes + Ceph.

Styx orchestrates a safe, ordered shutdown of your entire infrastructure stack — Kubernetes nodes first, then all VMs, then Ceph flags, then Proxmox hosts — designed to complete within a UPS battery window (typically 5–10 minutes).

## How it works

Styx splits the shutdown into a **coordinated phase** (requires cluster APIs) and an **independent phase** (each host acts autonomously):

| Phase | What happens |
|-------|-------------|
| Coordinated | Cordon k8s nodes, disable HA, drain all k8s nodes in parallel |
| Independent | Set Ceph OSD flags, dispatch `local-shutdown` to each host (one SSH per peer), poll + power off |

After the coordinated phase, each peer shuts down its own VMs via QMP and has an autonomous poweroff deadline as a leader-dead fallback — if the orchestrator dies, peers power themselves off after `timeout_vm + 15s`. VM shutdowns bypass `qm shutdown` and the Proxmox API, so the script keeps working even after cluster quorum is lost.

Phase control (`--phase`):

| Phase | Scope |
|-------|-------|
| 1 | Coordinated phase only + dispatch k8s VM shutdown |
| 2 | + dispatch all VM shutdown + polling loop |
| 3 | + Ceph flags + host poweroff (default) |

## Requirements

- Proxmox cluster with SSH between all hosts (root, key-based)
- `python3` on all Proxmox hosts (standard on Proxmox)

## Installation

Run the install script on any cluster node to install `styx.pyz` at `/opt/styx/styx.pyz` on all nodes:

```bash
curl -fSL https://github.com/nbenn/styx/releases/latest/download/install.sh | bash
```

Or download and run manually:

```bash
# Auto-discover nodes and install
bash install.sh

# Use a local .pyz instead of downloading
bash install.sh --pyz /path/to/styx.pyz

# Explicit host list (skip auto-discovery)
bash install.sh --hosts pve1 pve2 pve3
```

The script downloads the latest `styx.pyz` from GitHub releases, copies it to `/opt/styx/styx.pyz` on every node via SSH, and verifies each install. Re-run to upgrade.

Optionally, copy a config file if you need to override auto-discovery:

```bash
cp styx.conf.example /opt/styx/styx.conf
```

All subcommands (`orchestrate`, `vm-shutdown`, `local-shutdown`) are bundled in the single `styx.pyz` file.

## Usage

```
styx.pyz orchestrate [--mode <mode>] [--phase <1|2|3>] [--config <path>]
                     [--hosts HOST [HOST ...]] [--skip-poweroff]

Modes:
  emergency    Pre-flight warns, execute automatically, continue on failures (default)
  maintenance  Pre-flight aborts on failure + interactive gates between phases
  dry-run      Pre-flight aborts on failure, log all planned actions, execute nothing

Options:
  --phase <1|2|3>        Execute up to and including this phase (default: 3)
  --config <path>        Config file path (default: next to styx.pyz, else /etc/styx/styx.conf)
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

All three modes run preflight checks — SSH reachability, styx version on peers, Kubernetes API + node readiness, Ceph health, Proxmox quorum, and a worst-case runtime budget. The difference is what happens when a check fails:

**Emergency** (default) is designed for unattended UPS-triggered shutdowns: preflight failures are logged as warnings and execution continues. Every step during the shutdown sequence also logs a warning on failure and moves on, with no human in the loop.

**Maintenance** is for planned shutdowns. Preflight failures are fatal — styx aborts before touching anything. If preflight passes, it then prompts for confirmation before proceeding. Any warning during execution (drain timeout, stale VolumeAttachment, etc.) pauses and asks whether to skip or abort. A second confirmation gate sits before the final host powerdown.

All modes execute identical code paths, making maintenance mode a reliable way to exercise the emergency path against a real cluster.

**Dry-run** logs every planned action with a `[dry-run]` prefix and skips execution entirely. Preflight failures are fatal, same as maintenance. It also invokes `vm-shutdown --dry-run` on each peer to report real VM running status — making it as close to a real run as possible without modifying any state.

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
# /opt/styx/styx.conf (or /etc/styx/styx.conf when running from source)

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

## Remote Triggering

Styx ships two scripts for remote triggering (e.g. from a UPS monitoring host):

### Setup

**1. Generate a dedicated SSH key** on the trigger host (the machine monitoring the UPS):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/styx-trigger -N "" -C "styx-trigger"
```

**2. Install `gate.sh`** on every Proxmox node and add the public key to `authorized_keys`:

```bash
# On each node:
cp scripts/gate.sh /opt/styx/gate.sh
chmod +x /opt/styx/gate.sh

# In /root/.ssh/authorized_keys:
command="/opt/styx/gate.sh",restrict ssh-ed25519 AAAA... styx-trigger
```

The `restrict` keyword disables all SSH features (pty, forwarding, tunnels) by default. The `command=` directive ensures the key can only invoke styx — regardless of what the SSH client requests, `gate.sh` passes arguments to `styx.pyz` and pins `--config` to `/etc/styx/styx.conf`.

**3. Install `trigger.sh`** on the UPS monitoring host and configure your UPS software to call it:

```bash
cp scripts/trigger.sh /usr/local/bin/styx-trigger
chmod +x /usr/local/bin/styx-trigger
```

### Usage

```bash
# Trigger emergency shutdown, trying each node until one responds
styx-trigger 192.168.1.10 192.168.1.11 192.168.1.12

# Dry-run (verify connectivity and plan without executing)
styx-trigger --mode dry-run 192.168.1.10 192.168.1.11 192.168.1.12

# Custom SSH key path
styx-trigger --key /path/to/key --mode emergency 192.168.1.10 192.168.1.11
```

The trigger script tries each node in order and stops at the first one that responds. Any node can act as orchestrator, so if the primary is down, the next reachable node takes over. If a connection drops mid-run and the script falls through to another node, both runs can proceed safely — all styx operations are idempotent.

### NUT integration

In `upsmon.conf` on the UPS monitoring host:

```
SHUTDOWNCMD "/usr/local/bin/styx-trigger 192.168.1.10 192.168.1.11 192.168.1.12"
```

### Other triggers

Styx can also be triggered directly on any cluster node:

- **Manual**: `styx.pyz orchestrate` on any node
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

- **VMs only** (for now): LXC and OCI containers are not gracefully stopped. Workload type infrastructure is in place for future support — see [Future Work](docs/design.md#future-work)
- **Ceph on Proxmox hosts only**: Ceph-in-VM is not supported
- **Multi-node clusters only**: single-node Proxmox is not supported in v1
- **Live migration aware**: in-progress migrations are detected during preflight, and VM→host mappings are refreshed right before dispatch
