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

# is_api_reachable
# Returns 0 if kubectl can reach the API server, non-zero otherwise.
is_api_reachable() {
  local kubeconfig_arg=""
  [[ -n "${STYX_KUBECONFIG:-}" ]] && kubeconfig_arg="--kubeconfig=${STYX_KUBECONFIG}"
  kubectl ${kubeconfig_arg} get nodes --request-timeout=5s >/dev/null 2>&1
}

# cordon_node NODE_NAME
cordon_node() {
  local node="$1"
  local kubeconfig_arg=""
  [[ -n "${STYX_KUBECONFIG:-}" ]] && kubeconfig_arg="--kubeconfig=${STYX_KUBECONFIG}"
  kubectl ${kubeconfig_arg} cordon "$node"
}

# drain_node NODE_NAME TIMEOUT_SECONDS
drain_node() {
  local node="$1"
  local timeout="${2:-120}"
  local kubeconfig_arg=""
  [[ -n "${STYX_KUBECONFIG:-}" ]] && kubeconfig_arg="--kubeconfig=${STYX_KUBECONFIG}"
  kubectl ${kubeconfig_arg} drain "$node" \
    --ignore-daemonsets \
    --delete-emptydir-data \
    --force \
    --timeout="${timeout}s"
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
