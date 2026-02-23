#!/usr/bin/env bats
# Unit tests for lib/discover.sh

setup() {
  source "${BATS_TEST_DIRNAME}/../../lib/discover.sh"
}

# ---------------------------------------------------------------------------
# parse_cluster_status
# ---------------------------------------------------------------------------

@test "parse_cluster_status extracts hosts and IPs" {
  local json='[
    {"type":"cluster","name":"mycluster"},
    {"type":"node","name":"pve1","ip":"10.0.0.1","local":1,"online":1},
    {"type":"node","name":"pve2","ip":"10.0.0.2","local":0,"online":1}
  ]'
  parse_cluster_status "$json"
  [[ "${HOST_IPS[pve1]}" == "10.0.0.1" ]]
  [[ "${HOST_IPS[pve2]}" == "10.0.0.2" ]]
}

@test "parse_cluster_status identifies orchestrator via local==1" {
  local json='[
    {"type":"node","name":"pve1","ip":"10.0.0.1","local":1,"online":1},
    {"type":"node","name":"pve2","ip":"10.0.0.2","local":0,"online":1}
  ]'
  parse_cluster_status "$json"
  [[ "$ORCHESTRATOR" == "pve1" ]]
}

@test "parse_cluster_status ignores cluster-type entry" {
  local json='[
    {"type":"cluster","name":"mycluster"},
    {"type":"node","name":"pve1","ip":"10.0.0.1","local":1,"online":1}
  ]'
  parse_cluster_status "$json"
  [[ ${#HOST_IPS[@]} -eq 1 ]]
  [[ -v HOST_IPS[pve1] ]]
  [[ ! -v HOST_IPS[mycluster] ]]
}

# ---------------------------------------------------------------------------
# parse_cluster_resources
# ---------------------------------------------------------------------------

@test "parse_cluster_resources extracts running QEMU VMs" {
  local json='[
    {"type":"qemu","vmid":101,"name":"vm1","node":"pve1","status":"running","template":0},
    {"type":"qemu","vmid":102,"name":"vm2","node":"pve2","status":"running","template":0}
  ]'
  parse_cluster_resources "$json"
  [[ "${VMID_HOST[101]}" == "pve1" ]]
  [[ "${VMID_NAME[101]}" == "vm1"  ]]
  [[ "${VMID_HOST[102]}" == "pve2" ]]
}

@test "parse_cluster_resources excludes LXC containers" {
  local json='[
    {"type":"qemu","vmid":101,"name":"vm1","node":"pve1","status":"running","template":0},
    {"type":"lxc","vmid":200,"name":"ct1","node":"pve1","status":"running","template":0}
  ]'
  parse_cluster_resources "$json"
  [[ -v  VMID_HOST[101] ]]
  [[ ! -v VMID_HOST[200] ]]
}

@test "parse_cluster_resources excludes templates" {
  local json='[
    {"type":"qemu","vmid":101,"name":"vm1","node":"pve1","status":"running","template":0},
    {"type":"qemu","vmid":999,"name":"tpl","node":"pve1","status":"stopped","template":1}
  ]'
  parse_cluster_resources "$json"
  [[ -v  VMID_HOST[101] ]]
  [[ ! -v VMID_HOST[999] ]]
}

@test "parse_cluster_resources excludes stopped VMs" {
  local json='[
    {"type":"qemu","vmid":101,"name":"running","node":"pve1","status":"running","template":0},
    {"type":"qemu","vmid":102,"name":"stopped","node":"pve1","status":"stopped","template":0}
  ]'
  parse_cluster_resources "$json"
  [[ -v  VMID_HOST[101] ]]
  [[ ! -v VMID_HOST[102] ]]
}

# ---------------------------------------------------------------------------
# match_nodes_to_vms
# ---------------------------------------------------------------------------

@test "match_nodes_to_vms classifies workers and CP correctly" {
  declare -A vmid_names=([201]="cp1" [211]="worker1")
  local node_roles=$'cp1 control-plane\nworker1 worker'
  match_nodes_to_vms vmid_names "$node_roles"
  [[ "${K8S_CP[*]}"      == *"201"* ]]
  [[ "${K8S_WORKERS[*]}" == *"211"* ]]
}

@test "match_nodes_to_vms fails when no names match" {
  declare -A vmid_names=([201]="my-vm-1")
  local node_roles=$'cp1 control-plane'
  run match_nodes_to_vms vmid_names "$node_roles"
  [[ "$status" -ne 0 ]]
}

@test "match_nodes_to_vms succeeds with partial match" {
  declare -A vmid_names=([201]="cp1" [202]="unrelated-vm")
  local node_roles=$'cp1 control-plane'
  run match_nodes_to_vms vmid_names "$node_roles"
  [[ "$status" -eq 0 ]]
}
