# Styx

[![CI](https://github.com/nbenn/styx/actions/workflows/test.yml/badge.svg)](https://github.com/nbenn/styx/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/nbenn/styx/graph/badge.svg)](https://codecov.io/gh/nbenn/styx)

Graceful cluster shutdown for Proxmox + Kubernetes + Ceph.

Styx orchestrates a safe, ordered shutdown of your entire infrastructure stack — Kubernetes nodes first, then all VMs, then Ceph flags, then Proxmox hosts — designed to complete within a UPS battery window (typically 5–10 minutes).

> **QEMU VMs only.** LXC and OCI containers are not gracefully stopped — they will be killed when the host powers off. Workload type infrastructure is in place for future support (see [design doc](docs/design.md#future-work)).

## How it works

Styx splits the shutdown into a **coordinated phase** (requires cluster APIs) and an **independent phase** (each host acts autonomously):

| Phase | What happens |
|-------|-------------|
| Coordinated | Release HA (VMs keep running), cordon k8s nodes, drain all k8s nodes in parallel |
| Independent | Set Ceph OSD flags, unmount network storage (NFS/CIFS/CephFS under `/mnt/pve`) on hosts being powered off, dispatch `local-shutdown` to each host (one SSH per peer), poll + power off |

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

All subcommands (`orchestrate`, `vm-shutdown`, `local-shutdown`, `release-mounts`) are bundled in the single `styx.pyz` file.

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

**Dry-run** (default) logs every action styx would take without executing anything. Preflight failures are fatal. Use this to verify connectivity and inspect the shutdown plan before a real run.

**Emergency** is designed for unattended UPS-triggered shutdowns: preflight failures are logged as warnings and execution continues. Every step during the shutdown sequence also logs a warning on failure and moves on, with no human in the loop. Must be requested explicitly with `--mode emergency`.

**Maintenance** is for planned shutdowns. Preflight failures are fatal — styx aborts before touching anything. If preflight passes, it then prompts for confirmation before proceeding. Any warning during execution (drain timeout, stale VolumeAttachment, etc.) pauses and asks whether to skip or abort. A second confirmation gate sits before the final host powerdown.

All modes execute identical code paths, making maintenance mode a reliable way to exercise the emergency path against a real cluster.

**Dry-run** logs every planned action with a `[dry-run]` prefix and skips execution entirely. Preflight failures are fatal, same as maintenance. It also invokes `vm-shutdown --dry-run` on each peer to report real VM running status, and `release-mounts --dry-run` on each host to list the network storage mounts it would unmount — making it as close to a real run as possible without modifying any state.

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
  Per-OSD noout set: osd.2 osd.5 osd.8
    → ceph osd rm-noout osd.2
    → ceph osd rm-noout osd.5
    → ceph osd rm-noout osd.8
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
ssh-keygen -t ed25519 -f ~/.ssh/styx -N "" -C "styx-trigger"
```

`~/.ssh/styx` is the key `trigger.sh` uses by default (override with `--key`).

**2. Install `gate.sh`** on every Proxmox node and add the public key to `authorized_keys`:

```bash
# On any node — installs styx.pyz and generates /opt/styx/gate.sh on all nodes:
bash install.sh --include-gate

# In /root/.ssh/authorized_keys on each node:
command="/opt/styx/gate.sh",restrict ssh-ed25519 AAAA... styx-trigger
```

The `restrict` keyword disables all SSH features (pty, forwarding, tunnels) by default. The `command=` directive ensures the key can only invoke styx — regardless of what the SSH client requests, `gate.sh` only allows `orchestrate` and `-v`/`--version` and passes the arguments to the `styx.pyz` next to it. The config is therefore the default for a zipapp install: `/opt/styx/styx.conf`.

`gate.sh` starts `--mode emergency` runs detached from the SSH session: it prints the PID and returns right away, and styx keeps running even if the connection drops (output goes to the styx log). This matters when the trigger runs inside the cluster being shut down, where the drain evicts it mid-run. If an emergency run is already in progress on that node, `gate.sh` does not start a second one. Dry-run and maintenance runs stay in the foreground, so their output and exit status reach the caller.

**3. Install `trigger.sh`** on the UPS monitoring host and configure your UPS software to call it:

```bash
cp scripts/trigger.sh /usr/local/bin/trigger
chmod +x /usr/local/bin/trigger
```

### Usage

```bash
# Trigger emergency shutdown, trying each node until one responds
trigger --controllers 192.168.1.10 192.168.1.11 192.168.1.12 --mode emergency

# Dry-run (verify connectivity and plan without executing)
trigger --controllers 192.168.1.10 192.168.1.11 192.168.1.12

# Custom SSH key path
trigger --key /path/to/key --controllers 192.168.1.10 192.168.1.11 --mode emergency

# Verify host keys instead of accepting any
trigger --known-hosts /path/to/known_hosts --controllers 192.168.1.10 --mode emergency
```

The trigger script tries each node in order and stops at the first one that responds. Any node can act as orchestrator, so if the primary is down, the next reachable node takes over. Emergency runs return as soon as styx has started (see above); for dry-run and maintenance, if a connection drops mid-run and the script falls through to another node, both runs can proceed safely — all styx operations are idempotent.

### NUT integration

In `upsmon.conf` on the UPS monitoring host:

```
SHUTDOWNCMD "/usr/local/bin/trigger --controllers 192.168.1.10 192.168.1.11 192.168.1.12 --mode emergency"
```

### In-cluster UPS watcher (container image)

Instead of `upsmon` on a separate host, a single pod in the cluster can watch the UPS. Each release publishes `ghcr.io/nbenn/styx-trigger:<version>` (and `latest`), whose `trigger.sh` matches the `gate.sh` of the same styx version. The image runs the NUT driver and `upsd` on 127.0.0.1 and polls `ups.status` every 10 s. When the UPS reports both `OB` (on battery) and `LB` (low battery) for `STYX_ONBATT_MIN` seconds, it runs `trigger.sh` once. When `OB`+`LB` clears, it resets. Every status change is logged.

Set the runtime threshold on the UPS itself (APC: "Low Battery Duration"), so NUT reports the UPS's own `LB`. The minimum time on battery guards against an aged battery whose full-charge runtime is already below that threshold. `upsmon` is not used because it calls `SHUTDOWNCMD` immediately on `OB`+`LB` and then exits, expecting its own host to power off.

| Variable | Default | |
|---|---|---|
| `STYX_CONTROLLERS` | (required) | space-separated node list for `trigger.sh` |
| `STYX_MODE` | `dry-run` | `emergency` or `dry-run` |
| `STYX_UPS` | `ups@127.0.0.1` | NUT UPS name |
| `STYX_ONBATT_MIN` | `60` | seconds of `OB`+`LB` before triggering |
| `STYX_POLL_INTERVAL` | `10` | seconds between polls |

Mounted files:

| Path | |
|---|---|
| `/config/nut/ups.conf` | NUT driver config, including SNMP credentials (required) |
| `/config/ssh/id` | private key for `gate.sh` (required) |
| `/config/ssh/known_hosts` | controller host keys (optional; without it, host keys are not checked) |

The pod exits if the driver, `upsd` or the watcher dies, so Kubernetes restarts it. Example deployment, with one replica, `Recreate` so there is never a second watcher, and short `not-ready`/`unreachable` tolerations so it moves quickly off a failed node:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: styx-trigger-nut
  namespace: styx
stringData:
  ups.conf: |
    [ups]
        driver = snmp-ups
        port = 192.168.1.5
        mibs = apcc
        snmp_version = v3
        secLevel = authPriv
        secName = nut
        authProtocol = SHA
        authPassword = changeme
        privProtocol = AES
        privPassword = changeme
---
apiVersion: v1
kind: Secret
metadata:
  name: styx-trigger-ssh
  namespace: styx
stringData:
  id: |
    -----BEGIN OPENSSH PRIVATE KEY-----
    ...
    -----END OPENSSH PRIVATE KEY-----
  known_hosts: |
    192.168.1.10 ssh-ed25519 AAAA...
    192.168.1.11 ssh-ed25519 AAAA...
    192.168.1.12 ssh-ed25519 AAAA...
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: styx-trigger
  namespace: styx
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: styx-trigger
  template:
    metadata:
      labels:
        app: styx-trigger
    spec:
      tolerations:
        - key: node.kubernetes.io/not-ready
          operator: Exists
          effect: NoExecute
          tolerationSeconds: 30
        - key: node.kubernetes.io/unreachable
          operator: Exists
          effect: NoExecute
          tolerationSeconds: 30
      containers:
        - name: styx-trigger
          image: ghcr.io/nbenn/styx-trigger:0.4.4
          env:
            - name: STYX_CONTROLLERS
              value: "192.168.1.10 192.168.1.11 192.168.1.12"
            - name: STYX_MODE
              value: emergency
          volumeMounts:
            - name: nut
              mountPath: /config/nut
              readOnly: true
            - name: ssh
              mountPath: /config/ssh
              readOnly: true
      volumes:
        - name: nut
          secret:
            secretName: styx-trigger-nut
        - name: ssh
          secret:
            secretName: styx-trigger-ssh
```

Start with `STYX_MODE=dry-run` and check the pod log and `/var/log/styx.log` on the controller before switching to `emergency`.

To try the image locally without a UPS, use NUT's `dummy-ups` driver with the files in [`test/fixtures/nut`](test/fixtures/nut), and change the simulated status inside the container:

```bash
docker build -f container/Dockerfile -t styx-trigger .
docker run -d --name styx-trigger -v "$PWD/test/fixtures/nut:/config/nut:ro" -v ~/.ssh/styx:/config/ssh/id:ro -e STYX_CONTROLLERS=192.168.1.10 -e STYX_MODE=dry-run styx-trigger
docker exec styx-trigger sh -c 'echo "ups.status: OB LB" > /etc/nut/ups.dev'
docker logs -f styx-trigger
```

`OB LB` for 60 s triggers one dry run; `OB` alone or `OL` does not.

### Other triggers

Styx can also be triggered directly on any cluster node:

- **Manual**: `styx.pyz orchestrate` on any node (dry-run by default; add `--mode maintenance` or `--mode emergency`)
- **Cron/systemd**: `styx.pyz orchestrate --mode emergency` from a shutdown script

## Logging

All actions are logged to both stdout and `/var/log/styx.log` with timestamps. Each run appends a separator header, making the log useful for post-mortem analysis after a UPS-triggered shutdown.

## Recovery

After power is restored, bring the cluster back up in the reverse order that styx shut it down. The exact commands depend on your run — check `/var/log/styx.log` for the `--- Shutdown complete — startup checklist ---` entry, which lists the precise revert commands for your specific shutdown.

### Full cluster restart

**1. Boot Proxmox hosts**

Power on all nodes via IPMI/iLO or physically. Wait until all hosts are online and Proxmox cluster quorum is established:

```bash
pvecm status   # Quorate: Yes
```

**2. Clear Ceph OSD flags**

Wait for all OSDs to come up — `ceph osd tree down` lists any that didn't — then unset the flags styx applied:

```bash
ceph osd unset noout
ceph osd unset norecover
ceph osd unset norebalance
ceph osd unset nobackfill
```

If `nodown` is set (an older styx version or a manual `[ceph] flags` override), unset it **first**, before checking the OSDs: while it is set, an OSD that didn't come back still counts as up, and I/O to its placement groups hangs. Keep the other flags until every OSD is up again (or deliberately marked out), so Ceph doesn't start moving data for a disk that is only temporarily missing.

Wait for Ceph to settle before starting VMs — active PGs should reach a healthy state:

```bash
ceph status   # look for "HEALTH_OK" or "HEALTH_WARN" with only expected warnings
```

**3. Re-enable HA**

Styx took HA-managed VMs out of HA control (`--state ignored`) before shutting them down, so that HA neither stopped them early nor restarted them. The startup checklist lists the SIDs. Re-enable each service ID:

```bash
ha-manager set <sid> --state started   # e.g. ha-manager set vm:201 --state started
```

To find which SIDs need re-enabling:

```bash
ha-manager status   # look for services in "ignored" state
```

If HA is configured for your VMs, this step also starts them — skip to step 5.

**4. Start VMs (if not HA-managed)**

Start VMs in dependency order — infrastructure first, then Kubernetes control plane, then workers:

```bash
qm start <infra-vmids>
qm start <control-plane-vmids>
qm start <worker-vmids>
```

**5. Uncordon Kubernetes nodes**

Once the k8s API is reachable:

```bash
kubectl uncordon <node1> <node2> ...
# or to uncordon all nodes at once:
kubectl get nodes -o name | xargs kubectl uncordon
```

Verify pods are scheduling and running:

```bash
kubectl get nodes        # all nodes should be Ready, no SchedulingDisabled
kubectl get pods -A      # check for stuck pods
```

### Partial run (`--hosts`) restart

For partial runs, styx prints a `--- Partial run complete — revert checklist ---` with the exact revert commands. The key differences from a full restart:

- **Per-OSD noout** instead of global flags — clear with `ceph osd rm-noout osd.<id>` for each OSD on the affected hosts (do *not* use `ceph osd unset noout`, which would clear a cluster-wide flag that was never set)
- **HA maintenance mode** may have been enabled per-host — disable with `ha-manager crm-command node-maintenance disable <host>`
- **Only the targeted VMs** need restarting — `qm start <vmids>` as listed in the checklist

## Testing

```bash
python3 -m unittest discover -s test/unit -p 'test_*.py'
python3 -m unittest discover -s test -p 'test_*.py'
```

Unit tests cover pure decision logic and fixture-based parsing (no infrastructure needed). Integration tests run a full shutdown sequence using fake wrappers and simulated PID files.
