#!/usr/bin/env bats
# Unit tests for lib/decide.sh

setup() {
  source "${BATS_TEST_DIRNAME}/../../lib/decide.sh"
}

# ---------------------------------------------------------------------------
# should_poweroff_host
# ---------------------------------------------------------------------------

@test "host with all VMs stopped should be powered off" {
  declare -A vmid_host=([103]="pve3" [104]="pve3")
  run should_poweroff_host "pve3" vmid_host
  [[ "$output" == "yes" ]]
}

@test "host with a running VM should not be powered off" {
  declare -A vmid_host=([103]="pve3" [104]="pve3")
  run should_poweroff_host "pve3" vmid_host "103"
  [[ "$output" == "no" ]]
}

@test "host with no assigned VMs should be powered off" {
  declare -A vmid_host=([103]="pve2")
  run should_poweroff_host "pve3" vmid_host
  [[ "$output" == "yes" ]]
}

@test "running VM on different host does not block poweroff" {
  declare -A vmid_host=([103]="pve3" [104]="pve2")
  run should_poweroff_host "pve3" vmid_host "104"
  [[ "$output" == "yes" ]]
}

# ---------------------------------------------------------------------------
# should_disable_ha
# ---------------------------------------------------------------------------

@test "phase 1 skips HA disable" {
  run should_disable_ha 1
  [[ "$output" == "no" ]]
}

@test "phase 2 enables HA disable" {
  run should_disable_ha 2
  [[ "$output" == "yes" ]]
}

@test "phase 3 enables HA disable" {
  run should_disable_ha 3
  [[ "$output" == "yes" ]]
}

# ---------------------------------------------------------------------------
# should_run_polling
# ---------------------------------------------------------------------------

@test "phase 1 skips polling loop" {
  run should_run_polling 1
  [[ "$output" == "no" ]]
}

@test "phase 2 runs polling loop" {
  run should_run_polling 2
  [[ "$output" == "yes" ]]
}

@test "phase 3 runs polling loop" {
  run should_run_polling 3
  [[ "$output" == "yes" ]]
}

# ---------------------------------------------------------------------------
# should_poweroff_hosts
# ---------------------------------------------------------------------------

@test "phase 1 skips host poweroff" {
  run should_poweroff_hosts 1
  [[ "$output" == "no" ]]
}

@test "phase 2 skips host poweroff" {
  run should_poweroff_hosts 2
  [[ "$output" == "no" ]]
}

@test "phase 3 enables host poweroff" {
  run should_poweroff_hosts 3
  [[ "$output" == "yes" ]]
}

# ---------------------------------------------------------------------------
# should_set_ceph_flags
# ---------------------------------------------------------------------------

@test "phase 1 skips ceph flags" {
  run should_set_ceph_flags 1
  [[ "$output" == "no" ]]
}

@test "phase 2 skips ceph flags" {
  run should_set_ceph_flags 2
  [[ "$output" == "no" ]]
}

@test "phase 3 sets ceph flags" {
  run should_set_ceph_flags 3
  [[ "$output" == "yes" ]]
}
