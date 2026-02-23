# TODO

## 1. `vm_shutdown.py` — unit tests

Zero coverage today. `FakeOperations.shutdown_vm` bypasses the module entirely (it
SIGTERMs the fake process directly), so the integration tests give no signal here.

The escalation path — ACPI `system_powerdown` → SIGTERM → SIGKILL — has multiple
branches, all untested:
- VM already stopped before we start (PID file missing or process dead) → early exit
- QMP powerdown succeeds, VM stops within timeout → clean return
- QMP powerdown succeeds, VM does not stop within timeout → fall through to SIGTERM
- QMP socket absent or refused → fall through to SIGTERM immediately
- SIGTERM sufficient → clean return
- SIGTERM insufficient → SIGKILL
- Process disappears between signal and poll (race) → handled by `ProcessLookupError`

Test approach mirrors what integration tests already do: spawn real `sleep` processes,
write temp PID files, assert on exit code and process liveness. For the QMP path, a
`socketserver.UnixStreamServer` in a background thread can respond with the three QMP
frames (greeting / ack / response) and then close, simulating a cooperative guest.
Testing the QMP-absent branch is even simpler: just point at a non-existent socket path.

Individual helpers (`_read_pid`, `_alive`, `_poll_dead`) are pure enough to test in
isolation before testing `shutdown()` end-to-end.

## 2. `orchestrate.py::discover()` — unit tests

`discover()` has `_pvesh_fn` and `_pveceph_fn` injectable parameters added specifically
for testing, but no test uses them. The integration tests inject `_discover_fn` at a
higher level, bypassing `discover()` entirely.

Branches worth covering:
- Hosts from config override vs. from `pvesh`
- Orchestrator from config override vs. from `pvesh`
- Kubernetes: config override (`workers`/`control_plane` set)
- Kubernetes: API auto-discovery (inject a fake `K8sClient` or mock `_make_k8s_client`)
- Kubernetes: API unreachable → `k8s_enabled = False`, log and continue
- Kubernetes: no credentials configured → skip
- Ceph: from config override vs. from `pveceph`

These are all straightforward to write — inject small dicts/lists for pvesh responses
and booleans for pveceph, assert on the returned `ClusterTopology`.

## 3. `wrappers.py` — parsing logic

`get_ha_started_sids()` and `get_running_vmids()` contain non-trivial parsing that is
never directly tested. The integration tests exercise the flow but use `FakeOperations`,
so a bug in the real parsing would go undetected.

`get_ha_started_sids()` splits `ha-manager status` output line-by-line and looks for
`parts[1] == 'started'`. Edge cases: empty output, resources in states other than
`started`, malformed lines.

`get_running_vmids()` runs a shell command that scans `/var/run/qemu-server/*.pid` files
and checks liveness via `kill -0`. The output is split on newlines.

Cleanest fix: extract the parsing into module-level pure functions
(`_parse_ha_status(output)`, `_parse_running_vmids(output)`) and test those directly.
The methods themselves then just call `subprocess.run` and pass stdout to the parser —
thin enough not to need mocking.

## 4. `k8s.py` — HTTP method coverage

`_drainable()` is well-tested. The HTTP methods are not:

- `drain()` polling loop: evict all drainable pods, then poll until none remain or
  timeout. Branches: all pods clear immediately, pods clear after retry, timeout with
  pods still present.
- `evict()` return values: `evicted`, `gone` (404), `retry` (422/429), re-raise on
  other errors.
- `cordon()`: PATCH sent with correct body.

Approach: patch `K8sClient._request` with `unittest.mock.patch.object` and return
controlled responses. No network required. The `drain()` loop in particular has enough
branches (immediate clear, multi-poll, timeout) to warrant explicit tests.
