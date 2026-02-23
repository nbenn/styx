#!/bin/bash
# lib/wrappers.sh — Thin wrappers around external commands.
# Override these functions in tests with fakes.

# run_on_host HOSTNAME CMD
# Runs CMD on HOSTNAME via SSH, or locally if it's the orchestrator.
run_on_host() {
  local host="$1"
  shift
  if [[ "$host" == "$ORCHESTRATOR" ]]; then
    bash -c "$*"
  else
    local ip="${HOST_IPS[$host]}"
    ssh -o ConnectTimeout=5 -o BatchMode=yes "root@${ip}" "$*"
  fi
}

# get_running_vmids HOSTNAME
# Outputs VMIDs whose PID files exist and whose processes are alive on HOSTNAME.
get_running_vmids() {
  local host="$1"
  run_on_host "$host" '
    for pidfile in /var/run/qemu-server/*.pid; do
      [[ -f "$pidfile" ]] || continue
      vmid="${pidfile##*/}"
      vmid="${vmid%.pid}"
      pid=$(cat "$pidfile" 2>/dev/null) || continue
      kill -0 "$pid" 2>/dev/null && echo "$vmid"
    done
  '
}

# _k8s COMMAND [ARGS...]
# Internal helper: invoke lib/k8s.py with credentials from config variables.
# Requires STYX_K8S_SERVER and STYX_K8S_TOKEN to be set.
_k8s() {
  local extra_args=()
  [[ -n "${STYX_K8S_CA_CERT:-}" ]] && extra_args=(--ca-cert="${STYX_K8S_CA_CERT}")
  python3 "${STYX_DIR}/lib/k8s.py" \
    --server="${STYX_K8S_SERVER}" \
    --token-file="${STYX_K8S_TOKEN}" \
    "${extra_args[@]}" \
    "$@"
}

# is_api_reachable
# Returns 0 if the Kubernetes API server is reachable, non-zero otherwise.
is_api_reachable() {
  _k8s reachable >/dev/null 2>&1
}

# get_k8s_nodes
# Outputs "name role" pairs for all nodes (role: worker | control-plane).
get_k8s_nodes() {
  _k8s get-nodes
}

# cordon_node NODE_NAME
cordon_node() {
  _k8s cordon "$1"
}

# drain_node NODE_NAME TIMEOUT_SECONDS
drain_node() {
  _k8s drain "$1" --timeout="${2:-120}"
}

# shutdown_vm HOSTNAME VMID TIMEOUT_SECONDS
# Runs styx-vm-shutdown on the target host.
shutdown_vm() {
  local host="$1"
  local vmid="$2"
  local timeout="${3:-120}"
  run_on_host "$host" "styx-vm-shutdown ${vmid} ${timeout}"
}

# set_ceph_flags FLAG...
set_ceph_flags() {
  local flag
  for flag in "$@"; do
    ceph osd set "$flag"
  done
}

# get_ha_started_sids
# Outputs SIDs (e.g. "vm:100") for HA resources currently in "started" state.
get_ha_started_sids() {
  ha-manager status 2>/dev/null | awk 'NR>1 && $2=="started" {print $1}'
}

# disable_ha_sid SID
disable_ha_sid() {
  local sid="$1"
  ha-manager set "$sid" --state disabled
}

# poweroff_host HOSTNAME
poweroff_host() {
  local host="$1"
  local ip="${HOST_IPS[$host]}"
  ssh -o ConnectTimeout=5 -o BatchMode=yes "root@${ip}" poweroff
}

# poweroff_self
poweroff_self() {
  poweroff
}
