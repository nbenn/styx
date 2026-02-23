#!/bin/bash
# lib/classify.sh — VMID classification into k8s-worker, k8s-cp, other
# Pure functions, no external calls.
#
# Expects globals: K8S_WORKERS (array), K8S_CP (array)

# classify_vmid VMID
# Outputs: "k8s-worker", "k8s-cp", or "other"
classify_vmid() {
  local vmid="$1"
  local id
  for id in "${K8S_WORKERS[@]+"${K8S_WORKERS[@]}"}"; do
    [[ "$id" == "$vmid" ]] && echo "k8s-worker" && return 0
  done
  for id in "${K8S_CP[@]+"${K8S_CP[@]}"}"; do
    [[ "$id" == "$vmid" ]] && echo "k8s-cp" && return 0
  done
  echo "other"
}

# get_k8s_workers VMID...
# Filters the given VMIDs, outputs only those classified as k8s-worker.
get_k8s_workers() {
  local vmid
  for vmid in "$@"; do
    [[ "$(classify_vmid "$vmid")" == "k8s-worker" ]] && echo "$vmid"
  done
}

# get_k8s_cp VMID...
# Filters the given VMIDs, outputs only those classified as k8s-cp.
get_k8s_cp() {
  local vmid
  for vmid in "$@"; do
    [[ "$(classify_vmid "$vmid")" == "k8s-cp" ]] && echo "$vmid"
  done
}

# get_other_vms VMID...
# Filters the given VMIDs, outputs only those classified as other.
get_other_vms() {
  local vmid
  for vmid in "$@"; do
    [[ "$(classify_vmid "$vmid")" == "other" ]] && echo "$vmid"
  done
}
