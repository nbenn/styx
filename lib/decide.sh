#!/bin/bash
# lib/decide.sh — Decision logic (pure functions, no external calls)

# should_poweroff_host HOSTNAME VMID_HOST_ASSOC_NAME RUNNING_VMIDS...
# Outputs "yes" if all VMIDs assigned to HOSTNAME are absent from RUNNING_VMIDS.
# Args:
#   $1 — hostname to evaluate
#   $2 — name of assoc array: vmid -> hostname
#   $3+ — VMIDs currently running (may be empty)
should_poweroff_host() {
  local hostname="$1"
  local -n _vmid_host="$2"
  shift 2
  local -a running=("$@")

  local vmid
  for vmid in "${!_vmid_host[@]}"; do
    [[ "${_vmid_host[$vmid]}" != "$hostname" ]] && continue
    # This vmid belongs to hostname — check if it's still running
    local r
    for r in "${running[@]+"${running[@]}"}"; do
      [[ "$r" == "$vmid" ]] && echo "no" && return 0
    done
  done
  echo "yes"
}

# should_disable_ha PHASE
# Outputs "yes" if HA should be disabled for all resources (phase >= 2).
should_disable_ha() {
  local phase="$1"
  [[ "$phase" -ge 2 ]] && echo "yes" || echo "no"
}

# should_disable_ha_for_k8s PHASE
# Outputs "yes" if HA should be disabled for k8s VMIDs (always, any phase).
should_disable_ha_for_k8s() {
  echo "yes"
}

# should_run_polling PHASE
# Outputs "yes" if the unified polling loop should run (phase >= 2).
should_run_polling() {
  local phase="$1"
  [[ "$phase" -ge 2 ]] && echo "yes" || echo "no"
}

# should_poweroff_hosts PHASE
# Outputs "yes" if hosts should be powered off in the polling loop (phase >= 3).
should_poweroff_hosts() {
  local phase="$1"
  [[ "$phase" -ge 3 ]] && echo "yes" || echo "no"
}

# should_set_ceph_flags PHASE
# Outputs "yes" if Ceph OSD flags should be set (phase >= 3).
should_set_ceph_flags() {
  local phase="$1"
  [[ "$phase" -ge 3 ]] && echo "yes" || echo "no"
}
