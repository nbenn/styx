#!/usr/bin/env bats
# Integration tests: idempotency and re-run scenarios

STYX="${BATS_TEST_DIRNAME}/../../bin/styx"

setup() {
  source "${BATS_TEST_DIRNAME}/fake_env.sh"
  fake_env_setup

  write_fake_config \
    "pve1=10.0.0.1 pve2=10.0.0.2" \
    "pve1" "211" "201" "false"

  write_fake_wrappers \
    "pve1=10.0.0.1 pve2=10.0.0.2" \
    "pve1" \
    "101=pve1 211=pve2 201=pve2" \
    "101=infra-vm 211=worker1 201=cp1" \
    "211" "201" "true" "false"

  start_fake_vm 101
  start_fake_vm 211
  start_fake_vm 201
}

teardown() { fake_env_teardown; }

@test "phase 1 then phase 3: full sequence completes successfully" {
  run "$STYX" --phase 1 --config "${FAKE_CONF_DIR}/styx.conf"
  [[ "$status" -eq 0 ]]

  # Restart any VMs that phase 1 stopped so phase 3 has something to clean up
  start_fake_vm 101

  run "$STYX" --phase 3 --config "${FAKE_CONF_DIR}/styx.conf"
  [[ "$status" -eq 0 ]]
  [[ "$(tail -1 "$POWEROFF_LOG")" == "POWEROFF_SELF" ]]
}

@test "shutdown_vm on already-stopped VM exits 0 (idempotent)" {
  # VM 999 has no PID file — styx-vm-shutdown should be a no-op
  run "${BATS_TEST_DIRNAME}/../../bin/styx-vm-shutdown" 999
  [[ "$status" -eq 0 ]]
  [[ "$output" == *"not running"* ]]
}

@test "phase 3 re-run with no VMs running: no errors" {
  stop_fake_vm 101
  stop_fake_vm 211
  stop_fake_vm 201

  run "$STYX" --phase 3 --config "${FAKE_CONF_DIR}/styx.conf"
  [[ "$status" -eq 0 ]]
}

@test "HA disable is a no-op when no started resources exist" {
  run "$STYX" --phase 2 --config "${FAKE_CONF_DIR}/styx.conf"
  [[ "$status" -eq 0 ]]
  [[ ! -s "$HA_LOG" ]]
}

@test "SSH failure on poweroff is logged and does not abort" {
  # Append a failing poweroff_host override after the generated defaults
  # Unquoted heredoc so ${POWEROFF_LOG} expands at write time;
  # \$1 is escaped so it evaluates at function-call time inside bin/styx.
  cat >> "${STYX_WRAPPERS_FILE}" <<EOF
poweroff_host() {
  echo "POWEROFF_ATTEMPT \$1" >> "${POWEROFF_LOG}"
  return 1
}
EOF
  run "$STYX" --phase 3 --config "${FAKE_CONF_DIR}/styx.conf"
  [[ "$status" -eq 0 ]]
  grep -qiE "warning|failed|already down" <(echo "$output")
}
