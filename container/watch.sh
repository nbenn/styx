#!/bin/bash
#
# watch.sh — Poll a NUT UPS and trigger styx when it runs low on battery.
#
# Triggers once when the UPS reports both OB (on battery) and LB (low battery)
# for at least STYX_ONBATT_MIN seconds. The low-battery threshold itself is
# best configured on the UPS (APC: "Low Battery Duration"); the minimum time
# on battery guards against an aged battery whose runtime is already below
# that threshold when the outage starts. When OB+LB clears, the state resets.
#
# Environment:
#   STYX_UPS            NUT UPS name (default: ups@127.0.0.1)
#   STYX_CONTROLLERS    Space-separated node list for trigger.sh (required)
#   STYX_MODE           emergency or dry-run (default: dry-run)
#   STYX_ONBATT_MIN     Seconds of OB+LB before triggering (default: 60)
#   STYX_POLL_INTERVAL  Seconds between polls (default: 10)
#   STYX_SSH_KEY        SSH private key for trigger.sh (default: ~/.ssh/styx)
#   STYX_KNOWN_HOSTS    known_hosts file for trigger.sh (default: none, no checking)
#   STYX_TRIGGER        trigger command (default: trigger.sh)
#   STYX_UPSC           upsc command (default: upsc)

set -uo pipefail

UPS="${STYX_UPS:-ups@127.0.0.1}"
MODE="${STYX_MODE:-dry-run}"
ONBATT_MIN="${STYX_ONBATT_MIN:-60}"
POLL="${STYX_POLL_INTERVAL:-10}"
TRIGGER="${STYX_TRIGGER:-trigger.sh}"
UPSC="${STYX_UPSC:-upsc}"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

read -ra controllers <<< "${STYX_CONTROLLERS:-}"
if [[ ${#controllers[@]} -eq 0 ]]; then
    log "ERROR: STYX_CONTROLLERS is not set"
    exit 1
fi

case "$MODE" in
    emergency|dry-run) ;;
    *) log "ERROR: STYX_MODE must be 'emergency' or 'dry-run', got '${MODE}'"; exit 1 ;;
esac

trigger_args=(--controllers "${controllers[@]}")
[[ -n "${STYX_SSH_KEY:-}" ]] && trigger_args+=(--key "$STYX_SSH_KEY")
[[ -n "${STYX_KNOWN_HOSTS:-}" ]] && trigger_args+=(--known-hosts "$STYX_KNOWN_HOSTS")
trigger_args+=(--mode "$MODE")

has_flag() {
    [[ " $1 " == *" $2 "* ]]
}

log "watching ${UPS} every ${POLL}s; trigger after ${ONBATT_MIN}s of OB+LB" \
    "(mode ${MODE}, controllers ${controllers[*]})"

prev=""
since=""      # SECONDS value when OB+LB was first seen, empty when not OB+LB
fired=false

errfile="$(mktemp)"
trap 'rm -f "$errfile"' EXIT

while true; do
    # upsc prints notices like "Init SSL without certificate database" on
    # stderr, so only look at stderr when it fails.
    if status="$("$UPSC" "$UPS" ups.status 2>"$errfile")"; then
        ok=true
    else
        ok=false
        err="$(<"$errfile")"
        status="unavailable (${err//$'\n'/ })"
    fi

    if [[ "$status" != "$prev" ]]; then
        log "status: ${prev:-<none>} -> ${status}"
        prev="$status"
    fi

    # A failed poll says nothing about the UPS: neither start nor reset the
    # timer, and never trigger on stale information.
    if $ok; then
        if has_flag "$status" OB && has_flag "$status" LB; then
            if [[ -z "$since" ]]; then
                since=$SECONDS
                log "on battery with low battery; triggering in ${ONBATT_MIN}s unless it clears"
            fi
            if ! $fired && (( SECONDS - since >= ONBATT_MIN )); then
                log "OB+LB for $(( SECONDS - since ))s, running: ${TRIGGER} ${trigger_args[*]}"
                if "$TRIGGER" "${trigger_args[@]}" 2>&1 | while IFS= read -r line; do log "trigger: ${line}"; done; then
                    fired=true
                    log "trigger succeeded; not triggering again until OB+LB clears"
                else
                    log "trigger failed; retrying on next poll"
                fi
            fi
        elif [[ -n "$since" ]]; then
            log "OB+LB cleared; reset"
            since=""
            fired=false
        fi
    fi

    sleep "$POLL"
done
