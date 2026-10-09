#!/bin/bash
#
# simulate.sh — Make watch.sh see a simulated UPS status.
#
# Usage: simulate STATUS    e.g. simulate "OB LB"
#        simulate --clear   end the simulation
#        simulate           show the current simulation
#
# A trigger caused by a simulated status always runs in dry-run mode,
# whatever STYX_MODE says. The simulation ends by itself when the real UPS
# goes on battery, and after STYX_SIMULATE_MAX seconds (default 900).

set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/common.sh"

case "${1:-}" in
    -h|--help)
        sed -n '5,11s/^# \{0,1\}//p' "$0"
        ;;
    --clear)
        rm -f "$SIMULATE_FILE"
        echo "simulation cleared"
        ;;
    "")
        if [[ -f "$SIMULATE_FILE" ]]; then
            read -r started status < "$SIMULATE_FILE"
            echo "simulating '${status}' for $(( $(date +%s) - started ))s" \
                 "(expires after ${SIMULATE_MAX}s)"
        else
            echo "no simulation active"
        fi
        ;;
    *)
        mkdir -p "$STATE_DIR"
        echo "$(date +%s) $*" > "$SIMULATE_FILE"
        echo "simulating '$*' for up to ${SIMULATE_MAX}s; triggers run in dry-run mode"
        echo "follow with: kubectl logs -f deploy/styx-trigger"
        ;;
esac
