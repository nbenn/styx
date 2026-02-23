#!/usr/bin/env bats
# Unit tests for bin/styx-vm-shutdown PID/signal logic
# Uses fake PID files and sleep processes to simulate VMs.

SCRIPT="${BATS_TEST_DIRNAME}/../../bin/styx-vm-shutdown"

setup() {
  TMPDIR="$(mktemp -d)"
  # Override the PID/QMP paths used by the script
  # We patch the paths by setting env vars and using a wrapper
  FAKE_RUN_DIR="${TMPDIR}/qemu-server"
  mkdir -p "$FAKE_RUN_DIR"

  # Create a patched copy of the script that uses our fake paths
  PATCHED_SCRIPT="${TMPDIR}/styx-vm-shutdown"
  sed "s|/var/run/qemu-server|${FAKE_RUN_DIR}|g" "$SCRIPT" > "$PATCHED_SCRIPT"
  chmod +x "$PATCHED_SCRIPT"
}

teardown() {
  # Kill any lingering sleep processes
  local pidfile
  for pidfile in "${FAKE_RUN_DIR}"/*.pid; do
    [[ -f "$pidfile" ]] || continue
    kill "$(cat "$pidfile")" 2>/dev/null || true
  done
  rm -rf "$TMPDIR"
}

start_fake_vm() {
  local vmid="$1"
  sleep 3600 &
  local pid=$!
  echo "$pid" > "${FAKE_RUN_DIR}/${vmid}.pid"
  # Create a fake QMP socket (socat will fail gracefully since it's not real)
  touch "${FAKE_RUN_DIR}/${vmid}.qmp"
}

@test "exits 0 when VM is not running (no PID file)" {
  run "$PATCHED_SCRIPT" 999
  [[ "$status" -eq 0 ]]
  [[ "$output" == *"not running"* ]]
}

@test "exits 0 when PID file exists but process is dead" {
  echo "999999" > "${FAKE_RUN_DIR}/998.pid"
  run "$PATCHED_SCRIPT" 998
  [[ "$status" -eq 0 ]]
  [[ "$output" == *"not running"* ]]
}

@test "sends SIGKILL after timeout and SIGTERM" {
  start_fake_vm 100
  local vm_pid
  vm_pid="$(cat "${FAKE_RUN_DIR}/100.pid")"

  # Run with timeout=0 to trigger immediate escalation
  run "$PATCHED_SCRIPT" 100 0
  # Process should be dead
  ! kill -0 "$vm_pid" 2>/dev/null
}

@test "exits 0 when process stops within timeout (simulate fast shutdown)" {
  # Start a short-lived process
  sleep 2 &
  local pid=$!
  echo "$pid" > "${FAKE_RUN_DIR}/101.pid"
  touch "${FAKE_RUN_DIR}/101.qmp"

  # Timeout=10 — process will die on its own before timeout
  run "$PATCHED_SCRIPT" 101 10
  [[ "$status" -eq 0 ]]
}

@test "requires VMID argument" {
  run "$PATCHED_SCRIPT"
  [[ "$status" -ne 0 ]]
}
