#!/bin/bash
#
# trigger.sh — Trigger styx on a remote node via SSH.
#
# Tries each controller in order until one responds. Any node can act as
# orchestrator, so the first reachable controller wins.
#
# Usage: trigger.sh --controllers HOST... [--key PATH] [--timeout SECS] [STYX_ARGS...]
#   --controllers HOST...  Ordered list of node IPs/hostnames to try
#   --key PATH             SSH private key (default: ~/.ssh/styx)
#   --timeout SECS         SSH connect timeout per node (default: 5)
#   -v, --version          Show remote styx version
#   -h, --help             Show this help message
#   STYX_ARGS              All other flags are forwarded to styx orchestrate
#
# Default remote command: orchestrate --mode emergency
#
# Examples:
#   trigger.sh --controllers 10.0.0.1 10.0.0.2
#   trigger.sh --controllers 10.0.0.1 --mode dry-run
#   trigger.sh --controllers 10.0.0.1 --phase 2 --mode emergency
#   trigger.sh --controllers 10.0.0.1 --config /etc/styx/custom.conf
#   trigger.sh --controllers 10.0.0.1 -v
#
# Exit codes:
#   0  Shutdown triggered successfully
#   1  All controllers unreachable

set -euo pipefail

KEY="${HOME}/.ssh/styx"
TIMEOUT=5
CONTROLLERS=()
STYX_ARGS=()
styx_cmd=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --controllers)
            shift
            while [[ $# -gt 0 && ! "$1" =~ ^- ]]; do
                CONTROLLERS+=("$1"); shift
            done
            ;;
        --key)        KEY="$2"; shift 2 ;;
        --timeout)    TIMEOUT="$2"; shift 2 ;;
        -v|--version) styx_cmd="--version"; shift ;;
        -h|--help)    sed -n '3,27s/^# //p' "$0"; exit 0 ;;
        -*)           STYX_ARGS+=("$1"); shift ;;
        *)            STYX_ARGS+=("$1"); shift ;;
    esac
done

if [[ ${#CONTROLLERS[@]} -eq 0 ]]; then
    echo "No controllers specified. Use --controllers HOST..." >&2
    exit 1
fi

if [[ -z "$styx_cmd" ]]; then
    if [[ ${#STYX_ARGS[@]} -gt 0 ]]; then
        styx_cmd="orchestrate ${STYX_ARGS[*]}"
    else
        styx_cmd="orchestrate --mode emergency"
    fi
fi

for node in "${CONTROLLERS[@]}"; do
    echo "Trying ${node}..."
    if ssh -o ConnectTimeout="$TIMEOUT" \
           -o BatchMode=yes \
           -i "$KEY" \
           "root@${node}" \
           "$styx_cmd" 2>&1; then
        echo "Done via ${node}."
        exit 0
    else
        echo "${node}: unreachable or failed, trying next..."
    fi
done

echo "ERROR: all controllers unreachable." >&2
exit 1
