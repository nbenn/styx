#!/bin/bash
# lib/discover.sh — Auto-discovery: hosts, VMs, k8s nodes, Ceph
# Pure parsing functions — no side effects, no external calls.
#
# Populated globals (set by run_discovery in bin/styx):
#   HOST_IPS        — assoc array: hostname -> ip
#   ORCHESTRATOR    — string: hostname of this node
#   VMID_HOST       — assoc array: vmid -> hostname
#   VMID_NAME       — assoc array: vmid -> vm name
#   K8S_WORKERS     — array of VMIDs
#   K8S_CP          — array of VMIDs
#   CEPH_ENABLED    — "true" / "false"

# parse_cluster_status JSON
# Sets HOST_IPS (assoc) and ORCHESTRATOR.
parse_cluster_status() {
  local json="$1"
  declare -gA HOST_IPS=()

  local name ip local_flag
  while read -r name ip local_flag; do
    HOST_IPS["$name"]="$ip"
    if [[ "$local_flag" == "1" ]]; then ORCHESTRATOR="$name"; fi
  done < <(python3 -c "
import json, sys
for n in json.load(sys.stdin):
    if n.get('type') == 'node':
        print(n.get('name',''), n.get('ip',''), n.get('local', 0))
" <<< "$json")
}

# parse_cluster_resources JSON
# Sets VMID_HOST and VMID_NAME (assoc). Filters type=="qemu", excludes templates
# and stopped VMs.
parse_cluster_resources() {
  local json="$1"
  declare -gA VMID_HOST=()
  declare -gA VMID_NAME=()

  local vmid name host
  while read -r vmid name host; do
    VMID_HOST["$vmid"]="$host"
    VMID_NAME["$vmid"]="$name"
  done < <(python3 -c "
import json, sys
for v in json.load(sys.stdin):
    if v.get('type') != 'qemu': continue
    if v.get('template', 0): continue
    if v.get('status') != 'running': continue
    print(v['vmid'], v.get('name',''), v.get('node',''))
" <<< "$json")
}

# match_nodes_to_vms node_role_lines vmid_name_assoc_name
# Sets K8S_WORKERS and K8S_CP arrays.
# Aborts with error if no node names match any VM name.
# Args:
#   $1 — variable name of assoc array: vmid -> vm_name
#   $2 — newline-separated "nodename role" pairs (from parse_kubectl_nodes)
match_nodes_to_vms() {
  local -n _vmid_name="$1"
  local node_role_lines="$2"

  declare -gA _node_roles=()
  local name role
  while read -r name role; do
    [[ -z "$name" ]] && continue
    _node_roles["$name"]="$role"
  done <<< "$node_role_lines"

  K8S_WORKERS=()
  K8S_CP=()
  local matched=0

  local vmid vm_name
  for vmid in "${!_vmid_name[@]}"; do
    vm_name="${_vmid_name[$vmid]}"
    if [[ -v _node_roles["$vm_name"] ]]; then
      matched=1
      if [[ "${_node_roles[$vm_name]}" == "control-plane" ]]; then
        K8S_CP+=("$vmid")
      else
        K8S_WORKERS+=("$vmid")
      fi
    fi
  done

  unset _node_roles

  if [[ $matched -eq 0 ]]; then
    echo "ERROR: No Kubernetes node names match any Proxmox VM name." >&2
    echo "       Provide 'workers' and 'control_plane' VMIDs in [kubernetes] config." >&2
    return 1
  fi
}
