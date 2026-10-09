# common.sh — Settings and helpers shared by the styx-trigger scripts.
#
# Sourced, not executed. Reads the same environment as watch.sh, so check and
# simulate run through `kubectl exec` see the pod's configuration.

LIB_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

UPS="${STYX_UPS:-ups@127.0.0.1}"
TRIGGER="${STYX_TRIGGER:-trigger.sh}"
UPSC="${STYX_UPSC:-upsc}"
STATE_DIR="${STYX_STATE_DIR:-/run/styx-trigger}"
SIMULATE_FILE="${STATE_DIR}/simulate"
SIMULATE_MAX="${STYX_SIMULATE_MAX:-900}"

read -ra controllers <<< "${STYX_CONTROLLERS:-}"

# SSH options for trigger.sh. Without STYX_SSH_KEY, trigger.sh uses
# ~/.ssh/styx, where the entrypoint installs the mounted key.
ssh_args=()
if [[ -n "${STYX_SSH_KEY:-}" ]]; then
    ssh_args+=(--key "$STYX_SSH_KEY")
fi
known_hosts="${STYX_KNOWN_HOSTS:-${SSH_CONFIG:-/config/ssh}/known_hosts}"
if [[ -n "${STYX_KNOWN_HOSTS:-}" || -f "$known_hosts" ]]; then
    ssh_args+=(--known-hosts "$known_hosts")
fi

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

require_controllers() {
    if [[ ${#controllers[@]} -eq 0 ]]; then
        log "ERROR: STYX_CONTROLLERS is not set"
        exit 1
    fi
}

# ups_status: set UPS_STATUS to the UPS's ups.status, or to the error message
# and return 1. upsc prints notices like "Init SSL without certificate
# database" on stderr, so stderr is only looked at when it fails.
ups_status() {
    local err rc=0
    err="$(mktemp)"
    UPS_STATUS="$("$UPSC" "$UPS" ups.status 2>"$err")" || rc=$?
    if [[ $rc -ne 0 ]]; then
        UPS_STATUS="$(<"$err")"
        UPS_STATUS="${UPS_STATUS//$'\n'/ }"
    fi
    rm -f "$err"
    return $rc
}
