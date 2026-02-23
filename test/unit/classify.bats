#!/usr/bin/env bats
# Unit tests for lib/classify.sh

setup() {
  source "${BATS_TEST_DIRNAME}/../../lib/classify.sh"
  K8S_WORKERS=("211" "212" "213")
  K8S_CP=("201" "202")
}

@test "VMID in workers list classified as k8s-worker" {
  run classify_vmid 211
  [[ "$output" == "k8s-worker" ]]
}

@test "VMID in CP list classified as k8s-cp" {
  run classify_vmid 201
  [[ "$output" == "k8s-cp" ]]
}

@test "VMID in neither list classified as other" {
  run classify_vmid 300
  [[ "$output" == "other" ]]
}

@test "get_k8s_workers filters correctly" {
  run get_k8s_workers 101 211 201 212 300
  [[ "$output" == $'211\n212' ]]
}

@test "get_k8s_cp filters correctly" {
  run get_k8s_cp 101 211 201 202 300
  [[ "$output" == $'201\n202' ]]
}

@test "get_other_vms filters correctly" {
  run get_other_vms 101 211 201 300
  [[ "$output" == $'101\n300' ]]
}

@test "classify_vmid works with empty worker list" {
  K8S_WORKERS=()
  K8S_CP=("201")
  run classify_vmid 201
  [[ "$output" == "k8s-cp" ]]
}

@test "classify_vmid works with empty lists" {
  K8S_WORKERS=()
  K8S_CP=()
  run classify_vmid 999
  [[ "$output" == "other" ]]
}
