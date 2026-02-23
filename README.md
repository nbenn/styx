# Styx

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
- `socat` on all Proxmox hosts (standard on Proxmox)
- `jq` on the orchestrator
- `kubectl` on the orchestrator (if using Kubernetes)
- `ceph` CLI on the orchestrator (if using Ceph)

## Installation

```bash
cp bin/styx           /usr/local/bin/styx
cp bin/styx-vm-shutdown /usr/local/bin/styx-vm-shutdown
chmod +x /usr/local/bin/styx /usr/local/bin/styx-vm-shutdown

# Optional: copy config if you need to override auto-discovery
cp styx.conf.example /etc/styx/styx.conf
```

`styx` runs on the orchestrator only. `styx-vm-shutdown` must be on **all** Proxmox hosts.

## Usage

```
styx [options]

Options:
  --dry-run        Log all actions without executing them
  --phase <1|2|3>  Execute up to and including this phase (default: 3)
  --config <path>  Config file path (default: /etc/styx/styx.conf)
```

Typical invocations:

```bash
# Full shutdown (all phases)
styx

# Test what would happen without doing anything
styx --dry-run

# Drain k8s and shut down k8s VMs only
styx --phase 1

# Re-run phase 3 after a partial shutdown (k8s already down)
styx --phase 3
```

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

# Adjust timeouts (seconds)
[timeouts]
drain = 60
vm = 90
```

See [`styx.conf.example`](styx.conf.example) for the full reference.

## Triggering

Styx is a command, not a daemon. Wire it to your trigger of choice:

- **NUT** (Network UPS Tools): add `SHUTDOWNCMD "/usr/local/bin/styx"` to `upsmon.conf`
- **Manual**: run `styx` directly on the orchestrator
- **Cron/systemd**: call from a shutdown script

## Logging

All actions are logged to both stdout and `/var/log/styx.log` with timestamps. Each run appends a separator header, making the log useful for post-mortem analysis after a UPS-triggered shutdown.

## Recovery

After power is restored:

1. Boot Proxmox hosts (via IPMI/iLO or physically)
2. Unset Ceph OSD flags: `for f in noout norecover norebalance nobackfill nodown noup; do ceph osd unset $f; done`
3. Re-enable HA: `ha-manager set <sid> --state started`
4. Start VMs (infra → k8s control plane → workers)
5. Uncordon k8s nodes: `kubectl uncordon --all`

## Testing

Tests use [bats](https://github.com/bats-core/bats-core).

```bash
bats test/unit/
bats test/integration/
```

Unit tests cover pure decision logic (no infrastructure needed). Integration tests run a full shutdown sequence using fake wrappers and simulated PID files.

## Scope and limitations

- **VMs only**: LXC and OCI containers are not supported
- **Ceph on Proxmox hosts only**: Ceph-in-VM is not supported
- **Multi-node clusters only**: single-node Proxmox is not supported in v1
- **No live migration safety**: do not run styx while a VM migration is in progress
