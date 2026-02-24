# TODO

## 1. Parallel k8s drain

**Problem:** Worker nodes are drained sequentially. With a 120s drain timeout
per node, a 5-node cluster has a worst-case drain phase of 10 minutes —
exceeding the typical UPS window on its own.

**Goal:** Drain all nodes in parallel, bringing the worst-case drain phase
down to ~120s (a single shared timeout across all nodes).

**Approach:**
- Drain all nodes (workers + CP) concurrently with a single shared timeout.
- Worker VMs shut down immediately when their individual drain completes.
- CP VMs wait until all drains are done before shutting down — this preserves
  API server availability for the remaining drains (on k3s, the API server is
  the k3s process on the VM, not a pod).
- No pod-level filtering needed: on k3s there are no CP mirror pods; on
  kubeadm, mirror pods are already skipped by `_drainable()`.

## 2. Document worst-case runtime budget

**Problem:** An admin needs to look at their config and confidently say "my
worst case is X seconds" — without reading the source. Today the relationship
between individual timeouts and overall wall-clock runtime is not documented.

**Goal:** Clear documentation of every timeout that contributes to total
runtime, how they compose (parallel vs sequential), and a simple formula an
admin can use to calculate worst-case duration.

**Approach:**
- Enumerate all configurable timeouts (drain, vm shutdown, polling interval,
  HA disable wait, etc.) and any fixed internal delays (SIGTERM grace period,
  SIGKILL wait, etc.).
- Document the phase structure showing which timeouts run in parallel and
  which are sequential.
- Provide a worst-case formula, e.g.:
  `total = max(drain, vm_shutdown) + polling + ceph_flags + poweroff_overhead`
  (exact formula depends on phase structure after issue #1 is implemented).
- Add this to the README and/or a dedicated section in the design doc.
- Ensure every timeout that affects the formula is configurable and has a
  sensible default.
- Have the preflight routine calculate and display the worst-case runtime
  based on the active config, e.g. "Worst-case runtime: 4m 30s". This gives
  the admin a concrete number to compare against their UPS battery estimate
  before confirming.

## 3. ~~Split coordinated and independent shutdown phases~~ (done)

Implemented in v0.0.3. The orchestrator now splits the shutdown into two
phases:

1. **Coordinated phase** (leader required): cordon, disable HA, drain all
   k8s nodes.
2. **Independent phase** (each node autonomous): Ceph flags, then one
   `styx local-shutdown` dispatch per host. Each peer shuts down its own
   VMs via QMP. Peers have an autonomous poweroff deadline
   (`timeout_vm + 15s`) as a leader-dead fallback; the leader preempts
   this by powering off peers from the polling loop.

New subcommand: `styx local-shutdown <vmid>... --timeout N [--poweroff-delay S]`

## 4. Install script instead of shared storage

**Problem:** The current deployment model places `styx.pyz` on shared Proxmox
snippets storage (NFS or CephFS). This introduces a dependency on the storage
layer being healthy at the moment the tool is needed most. The `push_executable`
fallback adds latency and failure modes during an emergency.

**Goal:** Have the executable pre-installed at a fixed path on every node,
with no runtime dependency on shared storage or file distribution.

**Approach:**
- Provide an install script that copies `styx.pyz` to a fixed path on every
  cluster node via SSH. The path must be outside package-manager-managed
  directories to survive Proxmox upgrades (e.g. `/opt/styx/styx.pyz` or
  similar — needs investigation into what paths Proxmox upgrades leave
  untouched).
- The script should be re-runnable for upgrades (copy new version, done).
- Remove the `push_executable` mechanism from the shutdown path — the
  executable is already in place.
- Remove the shared snippets storage requirement from the README.
- The install script can discover cluster nodes the same way styx does
  (`pvesh get /cluster/status`) or accept a list of hosts as arguments.

## 5. Container support (LXC, OCI)

### 5a. Preflight warning (short-term)

**Problem:** LXC (and with Proxmox 9.x, OCI) containers are silently ignored.
They will be hard-killed on host poweroff with no warning to the operator.

**Goal:** Detect running containers during preflight and warn the operator.

**Approach:**
- `pvesh get /cluster/resources --type vm` already returns `type: lxc`
  alongside `type: qemu`. Use this to detect running containers.
- In preflight (and dry-run), emit a warning: "Found N LXC/OCI containers
  that will not be gracefully stopped."
- No new API calls needed — the data is already available from existing
  discovery.

### 5b. Graceful container shutdown (medium-term)

**Problem:** Containers are not gracefully stopped before host poweroff.

**Goal:** Extend styx to gracefully shut down LXC and OCI containers
alongside QEMU VMs, using quorum-free mechanisms.

**Approach:**
- LXC: `lxc-stop` operates locally without Proxmox quorum. Container init
  PID is tracked under `/var/run/lxc/<vmid>/`. Same pattern as VM shutdown:
  signal, poll PID, escalate. Needs investigation into exact PID file
  locations and signal behavior on Proxmox.
- OCI (Proxmox 9.x): depends on the container runtime (likely crun/runc).
  Needs investigation into the local stop mechanism, PID tracking, and
  whether a quorum-free path exists. Likely doable if the runtime provides
  a local CLI or socket interface.
- Discovery: extend `parse_cluster_resources` to include container workloads
  and classify them alongside VMs.
- The `local-shutdown` subcommand should be extended to handle both VMs and
  containers.
