#!/bin/bash
#
# ups-trigger.sh — Trigger styx cluster shutdown via SSH.
#
# Tries each node in order until one responds. Any node can act as
# orchestrator, so the first reachable node wins.
#
# Usage: ups-trigger.sh [--mode MODE] [--hosts HOST ...] [--key PATH]
#                       [--timeout SECS] [NODES...]
#   --mode MODE     Styx mode: dry-run, emergency, maintenance (default: emergency)
#   --key PATH      SSH private key (default: ~/.ssh/styx-trigger)
#   --timeout SECS  SSH connect timeout per node (default: 5)
#   NODES           Ordered list of node IPs/hostnames to try
#
# Exit codes:
#   0  Shutdown triggered successfully
#   1  All nodes unreachable

set -euo pipefail

MODE="emergency"
KEY="${HOME}/.ssh/styx-trigger"
TIMEOUT=5
NODES=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)    MODE="$2"; shift 2 ;;
        --key)     KEY="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        -h|--help) sed -n '3,13s/^# //p' "$0"; exit 0 ;;
        -*)        echo "Unknown option: $1" >&2; exit 1 ;;
        *)         NODES+=("$1"); shift ;;
    esac
done

if [[ ${#NODES[@]} -eq 0 ]]; then
    echo "No nodes specified." >&2
    exit 1
fi

for node in "${NODES[@]}"; do
    echo "Trying ${node}..."
    if ssh -o ConnectTimeout="$TIMEOUT" \
           -o BatchMode=yes \
           -i "$KEY" \
           "root@${node}" \
           "orchestrate --mode ${MODE}" 2>&1; then
        echo "Shutdown triggered via ${node}."
        exit 0
    else
        echo "${node}: unreachable or failed, trying next..."
    fi
done

echo "ERROR: all nodes unreachable." >&2
exit 1
