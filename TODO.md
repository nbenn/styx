# TODO

## 1. Convert bash to Python
Python is already a hard dependency, so the main argument for bash (no extra deps) no longer
holds. The architecture maps directly: `lib/` → modules, INI parsing → `configparser`, parallel
tracks → `concurrent.futures`, background process bookkeeping becomes straightforward.

**Operational fixes to implement during the rewrite** (identified from research against prior art
and the full-cluster-shutdown literature — do not add new bash code for these):

- **Kubernetes**: drain CP nodes the same as workers, but extend `_drainable()` in `k8s.py` to
  also skip mirror pods (annotation `kubernetes.io/config.mirror`) — these are the API-side
  reflection of static pods (etcd, kube-apiserver, etc.) managed by the kubelet's
  `staticPodPath`; they are not evictable and attempting to evict them errors or no-ops. With
  this filter, draining a CP node correctly evicts any user workloads while silently skipping
  system pods. After draining each node, check for stale `VolumeAttachment` objects still
  referencing it — a clean drain triggers the CSI external-attacher to remove them; leftovers
  mean a volume didn't detach and will cause `ContainerCreating` hangs on restart. This check
  is a natural `policy.on_warning()` site: emergency mode logs and continues, maintenance mode
  surfaces the list and prompts. Same pattern applies to PDB bypass: emergency mode
  force-deletes after drain timeout, maintenance mode prompts. Document kubelet
  `GracefulNodeShutdown` (`shutdownGracePeriod`) as a node-side prerequisite, not a styx
  concern.
- **Proxmox HA**: `disabled` is the correct state for a full cluster shutdown — all VMs are
  being stopped anyway, so HA has nothing to relocate and the watchdog concern is moot (no
  surviving cluster to do the fencing). Do not stop `pve-ha-lrm` manually; that fires the
  watchdog immediately. After setting a resource to `disabled`, wait for the `active` →
  `disabled` transition before issuing VM shutdown — but with an explicit timeout and a
  warn-and-continue on expiry, same as every other wait in the sequence. Document
  `shutdown_policy = freeze` in `datacenter.cfg` as a cluster-side prerequisite (prevents HA
  from trying to relocate VMs to surviving nodes during the shutdown window).
- **Ceph**: run `ceph osd ok-to-stop <ids>` (and `mon`, `mds`) as a pre-flight check before
  setting flags — for a full shutdown everything goes down anyway, but a degraded cluster before
  we even start is worth surfacing; `policy.on_warning()` site. Fix the default flag set: drop
  `noup` — it prevents OSDs from marking themselves up on restart, which is a post-boot concern
  (avoid rebalancing storms before all OSDs are back), not a shutdown concern; make it an
  explicit opt-in for the startup side. Ensure `nodown + norecover` are in the default shutdown
  set. Add optional CephFS teardown (`ceph fs fail`, `ceph fs set cluster_down true`) for
  clusters that run CephFS; reverse on startup. MON-last Proxmox host ordering is the right
  goal but requires Ceph topology awareness (which hosts run MONs) that styx does not currently
  have; note as a future improvement.
- **proxmox-guardian patterns** (https://github.com/Guilhem-Bonnet/proxmox-guardian): per-action
  error policy is covered by the `Policy` pattern in step 5 (emergency = warn-and-continue,
  maintenance = prompt); no additional per-operation configuration matrix needed. Persistent
  state is unnecessary as long as all actions remain idempotent. Startup/recovery is a manual
  procedure with clear documentation, not automated. Tag-based VM discovery was considered and
  rejected: it adds a third discovery mechanism (alongside config and API auto-discovery) with
  no added value — if the k8s API is unreachable, drain cannot run anyway.

**Design constraints for later steps — don't box in:**

- **Maintenance mode (step 5)**: thread a `Policy` object through the execution layer instead of
  hardcoding warn-and-continue. `policy.on_warning(msg)` logs silently in emergency mode and
  prompts interactively in maintenance mode. Execution code must be identical between modes —
  divergence defeats the testing purpose.
- **Distribution as zipapp (step 4)**: structure modules so the package works cleanly under
  `python -m zipapp` — a `__main__.py` entry point, no `__file__`-relative path tricks at
  runtime, no dynamic imports that break zip packaging.
- **E2E testability (step 5)**: keep I/O (pvesh calls, SSH, Kubernetes API, Ceph CLI) behind
  thin interfaces so tests can substitute fakes without monkey-patching internals.

## 2. Robust unit testing with fixtures
Capture real `pvesh` and Kubernetes API output from a live cluster, anonymize it, and store
as fixture files under `test/fixtures/pvesh/` and `test/fixtures/k8s/`. Update Python tests to
load from disk instead of inline strings. Add hand-crafted fixtures for edge cases not present in
the real cluster (offline nodes, mid-migration VMs, single-node k8s, dual-role nodes). Feeds
directly into the E2E and Proxmox test strategy in step 5.

## 3. Distribution
Bundle as a single `styx.pyz` (Python zipapp, stdlib since 3.5) with two subcommands:
`orchestrate` and `vm-shutdown`, replacing the two current binaries. Place on shared Proxmox
snippets storage (NFS or CephFS with `content snippets`) — the file is immediately available at
the same path on every node, so no per-node install. GitHub Actions builds and publishes
`styx.pyz` as a release artifact on version tags; install is a single `curl` + `chmod +x`.
Depends on step 1 (Python rewrite) — bash cannot be bundled this way.

## 4. E2E tests
- **k8s track**: spin up a kind cluster in GitHub Actions, deploy test workloads (Deployments +
  DaemonSets), run `styx --phase 1`, assert nodes cordoned and pods evicted. Trigger on release
  tags, not every push. Ideally exercises the `.pyz` artifact from step 3.
- **Proxmox track**: GitHub-hosted runners have no KVM, so Proxmox E2E in CI is not feasible
  without a self-hosted runner on real hardware. Covered instead by realistic fixtures (step 2)
  + the existing integration tests. Document a manual pre-release checklist for testing against
  a real cluster.

## 5. Interactive / maintenance mode
Two modes over identical execution logic — the same operations, same order, same code paths:

- **Emergency mode** (default, current behaviour): failures log a warning and continue; no
  human in the loop.
- **Maintenance mode** (`--mode maintenance`): a thin gate layer wraps the same operations —
  pre-flight checks surface before anything is touched, failures pause and prompt
  (retry / skip / abort), and a confirmation gate sits between phases.

The gate is a `Policy` object passed through the execution layer (see step 1). `policy.on_warning(msg)`
either logs silently (emergency) or prompts the operator (maintenance). Execution code is identical.

Maintenance mode adds:
- Pre-flight: SSH reachability to all hosts, k8s API reachable, Ceph health, estimated drain
  load — all checked and displayed before any action is taken.
- Phase gates: summary after each phase with explicit "proceed?" confirmation.
- Failure handling: drain timeout or SSH error → prompt instead of warn-and-continue.

Primary motivation: maintenance mode is the only way the emergency path sees real-life testing.
Keeping the code paths maximally similar is a hard requirement — divergence defeats the purpose.
