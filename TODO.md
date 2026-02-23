# TODO

## 1. Robust unit testing with fixtures
Capture real `pvesh` and Kubernetes API output from a live cluster, anonymize it, and store
as fixture files under `test/fixtures/pvesh/` and `test/fixtures/k8s/`. Update bats and Python
tests to load from disk instead of inline strings. Add hand-crafted fixtures for edge cases not
present in the real cluster (offline nodes, mid-migration VMs, single-node k8s, dual-role nodes).

## 2. Convert bash to Python
Python is already a hard dependency, so the main argument for bash (no extra deps) no longer holds.
The architecture translates directly — layer separation maps cleanly to Python modules, INI parsing
becomes configparser, parallel tracks become concurrent.futures, background process bookkeeping
becomes straightforward. Fixtures from step 1 verify correctness of the rewrite. Pays off
compoundingly for every feature added after, and is a prerequisite for steps 3 and 4.

## 3. Distribution
Bundle as a single `styx.pyz` (Python zipapp, stdlib since 3.5) with two subcommands:
`orchestrate` and `vm-shutdown`, replacing the two current binaries. Place on shared Proxmox
snippets storage (NFS or CephFS with `content snippets`) — the file is immediately available at
the same path on every node, so no per-node install. GitHub Actions builds and publishes
`styx.pyz` as a release artifact on version tags; install is a single `curl` + `chmod +x`.
Depends on step 2 (Python rewrite) — bash cannot be bundled this way.

## 4. E2E tests
- **k8s track**: spin up a kind cluster in GitHub Actions, deploy test workloads (Deployments +
  DaemonSets), run `styx --phase 1`, assert nodes cordoned and pods evicted. Trigger on release
  tags, not every push. Ideally exercises the `.pyz` artifact from step 3.
- **Proxmox track**: GitHub-hosted runners have no KVM, so Proxmox E2E in CI is not feasible
  without a self-hosted runner on real hardware. Covered instead by realistic fixtures (step 1)
  + the existing bats integration tests. Document a manual pre-release checklist for testing
  against a real cluster.

## 5. Interactive / maintenance mode
Two modes over identical execution logic — the same operations, same order, same code paths:

- **Emergency mode** (default, current behaviour): failures log a warning and continue; no
  human in the loop.
- **Maintenance mode** (`--mode maintenance`): a thin gate layer wraps the same operations —
  pre-flight checks surface before anything is touched, failures pause and prompt
  (retry / skip / abort), and a confirmation gate sits between phases.

The gate is a policy object passed through the execution layer. `policy.on_warning(msg)` either
logs silently (emergency) or prompts the operator (maintenance). Execution code is identical.

Maintenance mode adds:
- Pre-flight: SSH reachability to all hosts, k8s API reachable, Ceph health, estimated drain
  load — all checked and displayed before any action is taken.
- Phase gates: summary after each phase with explicit "proceed?" confirmation.
- Failure handling: drain timeout or SSH error → prompt instead of warn-and-continue.

Primary motivation: maintenance mode is the only way the emergency path sees real-life testing.
Keeping the code paths maximally similar is a hard requirement — divergence defeats the purpose.
