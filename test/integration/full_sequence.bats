#!/usr/bin/env bats
# Integration tests: full shutdown sequences with fake environment

STYX="${BATS_TEST_DIRNAME}/../../bin/styx"

# Default 3-node cluster topology used by most tests:
#   pve1 = orchestrator, has VM 101 (non-k8s)
#   pve2 = has VM 211 (k8s worker)
#   pve3 = has VM 201 (k8s CP)
_setup_default() {
  write_fake_config \
    "pve1=10.0.0.1 pve2=10.0.0.2 pve3=10.0.0.3" \
    "pve1" "211" "201" "false"

  write_fake_wrappers \
    "pve1=10.0.0.1 pve2=10.0.0.2 pve3=10.0.0.3" \
    "pve1" \
    "101=pve1 211=pve2 201=pve3" \
    "101=infra-vm 211=worker1 201=cp1" \
    "211" "201" "true" "false"

  start_fake_vm 101
  start_fake_vm 211
  start_fake_vm 201
}

setup() {
  source "${BATS_TEST_DIRNAME}/fake_env.sh"
  fake_env_setup
  _setup_default
}

teardown() { fake_env_teardown; }

# ---------------------------------------------------------------------------

@test "phase 3: all VMs shut down, hosts powered off, orchestrator last" {
  run "$STYX" --phase 3 --config "${FAKE_CONF_DIR}/styx.conf"
  [[ "$status" -eq 0 ]]

  grep -q "SHUTDOWN 101" "$SHUTDOWN_LOG"
  grep -q "SHUTDOWN 211" "$SHUTDOWN_LOG"
  grep -q "SHUTDOWN 201" "$SHUTDOWN_LOG"

  grep -qE "POWEROFF pve[23]" "$POWEROFF_LOG"
  [[ "$(tail -1 "$POWEROFF_LOG")" == "POWEROFF_SELF" ]]
}

@test "phase 2: VMs shut down but no host poweroff" {
  run "$STYX" --phase 2 --config "${FAKE_CONF_DIR}/styx.conf"
  [[ "$status" -eq 0 ]]

  grep -q "SHUTDOWN" "$SHUTDOWN_LOG"
  [[ ! -s "$POWEROFF_LOG" ]]
}

@test "phase 1: only k8s VMs drained and shut down, no poweroff" {
  run "$STYX" --phase 1 --config "${FAKE_CONF_DIR}/styx.conf"
  [[ "$status" -eq 0 ]]

  grep -qE "DRAIN (worker1|cp1)" "$DRAIN_LOG"
  [[ ! -s "$POWEROFF_LOG" ]]
}

@test "phase 1: non-k8s VMs are not shut down" {
  run "$STYX" --phase 1 --config "${FAKE_CONF_DIR}/styx.conf"
  [[ "$status" -eq 0 ]]
  ! grep -q "SHUTDOWN 101" "$SHUTDOWN_LOG"
}

@test "dry-run: no VMs stopped, no hosts powered off" {
  run "$STYX" --dry-run --phase 3 --config "${FAKE_CONF_DIR}/styx.conf"
  [[ "$status" -eq 0 ]]

  [[ ! -s "$SHUTDOWN_LOG" ]]
  [[ ! -s "$POWEROFF_LOG" ]]
  [[ "$output" == *"dry-run"* ]]
}

@test "k8s workers are drained before control-plane" {
  run "$STYX" --phase 3 --config "${FAKE_CONF_DIR}/styx.conf"
  [[ "$status" -eq 0 ]]

  local worker_line cp_line
  worker_line="$(grep -n "DRAIN worker1" "$DRAIN_LOG" | cut -d: -f1 | head -1)"
  cp_line="$(grep    -n "DRAIN cp1"     "$DRAIN_LOG" | cut -d: -f1 | head -1)"
  [[ -n "$worker_line" && -n "$cp_line" ]]
  [[ "$worker_line" -lt "$cp_line" ]]
}

@test "ceph flags are set when ceph is enabled" {
  # Re-setup with ceph enabled, no k8s
  write_fake_config "pve1=10.0.0.1 pve2=10.0.0.2" "pve1" "" "" "true"
  write_fake_wrappers \
    "pve1=10.0.0.1 pve2=10.0.0.2" \
    "pve1" \
    "101=pve1 102=pve2" \
    "101=infra 102=other" \
    "" "" "false" "true" "noout norebalance"
  start_fake_vm 102

  run "$STYX" --phase 3 --config "${FAKE_CONF_DIR}/styx.conf"
  [[ "$status" -eq 0 ]]
  grep -q "CEPH_FLAGS" "$CEPH_LOG"
}

@test "no ceph flags set when ceph is disabled" {
  run "$STYX" --phase 3 --config "${FAKE_CONF_DIR}/styx.conf"
  [[ "$status" -eq 0 ]]
  [[ ! -s "$CEPH_LOG" ]]
}
