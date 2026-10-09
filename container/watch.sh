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
# A status set with `simulate` replaces the real one while the real UPS is not
# on battery; triggers it causes always use dry-run mode.
#
# Environment:
#   STYX_UPS             NUT UPS name (default: ups@127.0.0.1)
#   STYX_CONTROLLERS     Space-separated node list for trigger.sh (required)
#   STYX_MODE            emergency or dry-run (default: dry-run)
#   STYX_ONBATT_MIN      Seconds of OB+LB before triggering (default: 60)
#   STYX_POLL_INTERVAL   Seconds between polls (default: 10)
#   STYX_CHECK_INTERVAL  Seconds between runs of check.sh, 0 to disable
#                        (default: 21600; the first run is STYX_CHECK_DELAY,
#                        default 15, seconds after startup)
#   STYX_SIMULATE_MAX    Seconds after which a simulation expires (default: 900)
#   STYX_SSH_KEY         SSH private key for trigger.sh (default: ~/.ssh/styx)
#   STYX_KNOWN_HOSTS     known_hosts for trigger.sh (default:
#                        /config/ssh/known_hosts if present, else no checking)
#   STYX_STATE_DIR       Runtime state: simulation, readiness (default: /run/styx-trigger)
#   STYX_TRIGGER         trigger command (default: trigger.sh)
#   STYX_UPSC            upsc command (default: upsc)
#   STYX_CHECK           check command (default: check.sh next to this script)

set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/common.sh"

MODE="${STYX_MODE:-dry-run}"
ONBATT_MIN="${STYX_ONBATT_MIN:-60}"
POLL="${STYX_POLL_INTERVAL:-10}"
CHECK_INTERVAL="${STYX_CHECK_INTERVAL:-21600}"
CHECK="${STYX_CHECK:-${LIB_DIR}/check.sh}"

require_controllers

case "$MODE" in
    emergency|dry-run) ;;
    *) log "ERROR: STYX_MODE must be 'emergency' or 'dry-run', got '${MODE}'"; exit 1 ;;
esac

mkdir -p "$STATE_DIR"

has_flag() {
    [[ " $1 " == *" $2 "* ]]
}

# simulated_status REAL_OK: set SIM_STATUS and succeed if a simulation is
# active. Ends the simulation when it has expired or the real UPS (REAL_OK
# true, status in UPS_STATUS) is on battery.
simulated_status() {
    local started status
    [[ -f "$SIMULATE_FILE" ]] || return 1
    read -r started status < "$SIMULATE_FILE" || true
    if $1 && has_flag "$UPS_STATUS" OB; then
        log "real UPS is on battery; ending simulation"
        rm -f "$SIMULATE_FILE"
        return 1
    fi
    if (( $(date +%s) - ${started:-0} > SIMULATE_MAX )); then
        log "simulation expired after ${SIMULATE_MAX}s"
        rm -f "$SIMULATE_FILE"
        return 1
    fi
    SIM_STATUS="$status"
}

log "watching ${UPS} every ${POLL}s; trigger after ${ONBATT_MIN}s of OB+LB" \
    "(mode ${MODE}, controllers ${controllers[*]})"

prev=""
since=""          # SECONDS value when OB+LB was first seen, empty when not OB+LB
fired=false
was_simulated=false
next_check="${STYX_CHECK_DELAY:-15}"

while true; do
    if (( CHECK_INTERVAL > 0 && SECONDS >= next_check )); then
        next_check=$(( SECONDS + CHECK_INTERVAL ))
        { "$CHECK" 2>&1 | while IFS= read -r line; do log "check: ${line}"; done; } &
    fi

    ok=true
    ups_status || ok=false
    status="$UPS_STATUS"
    $ok || status="unavailable (${status})"

    simulated=false
    if simulated_status "$ok"; then
        simulated=true
        ok=true
        status="$SIM_STATUS"
    fi

    # Never let a simulated trigger suppress a real one, or the other way round
    if [[ $simulated != "$was_simulated" ]]; then
        since=""
        fired=false
        was_simulated=$simulated
    fi

    shown="$status"
    $simulated && shown+=" (simulated)"
    if [[ "$shown" != "$prev" ]]; then
        log "status: ${prev:-<none>} -> ${shown}"
        prev="$shown"
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
                mode="$MODE"
                $simulated && mode=dry-run
                args=(--controllers "${controllers[@]}" ${ssh_args[@]+"${ssh_args[@]}"} --mode "$mode")
                log "OB+LB for $(( SECONDS - since ))s, running: ${TRIGGER} ${args[*]}"
                if "$TRIGGER" "${args[@]}" 2>&1 | while IFS= read -r line; do log "trigger: ${line}"; done; then
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
