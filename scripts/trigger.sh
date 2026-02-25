#!/bin/bash
#
# ups-trigger.sh — Trigger styx cluster shutdown via SSH.
#
# Tries each node in order until one responds. Any node can act as
# orchestrator, so the first reachable node wins.
#
# Usage: ups-trigger.sh [--mode MODE] [--key PATH] [--timeout SECS] [-v] NODES...
#   --mode MODE     Styx mode: dry-run, emergency, maintenance (default: emergency)
#   --key PATH      SSH private key (default: ~/.ssh/styx)
#   --timeout SECS  SSH connect timeout per node (default: 5)
#   -v, --version   Show remote styx version
#   -h, --help      Show this help message
#   NODES           Ordered list of node IPs/hostnames to try
#
# Exit codes:
#   0  Shutdown triggered successfully
#   1  All nodes unreachable

set -euo pipefail

MODE="emergency"
KEY="${HOME}/.ssh/styx"
TIMEOUT=5
NODES=()
styx_cmd=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)       MODE="$2"; shift 2 ;;
        --key)        KEY="$2"; shift 2 ;;
        --timeout)    TIMEOUT="$2"; shift 2 ;;
        -v|--version) styx_cmd="--version"; shift ;;
        -h|--help)    sed -n '3,16s/^# //p' "$0"; exit 0 ;;
        -*)           echo "Unknown option: $1" >&2; exit 1 ;;
        *)            NODES+=("$1"); shift ;;
    esac
done

if [[ ${#NODES[@]} -eq 0 ]]; then
    echo "No nodes specified." >&2
    exit 1
fi

if [[ -z "$styx_cmd" ]]; then
    styx_cmd="orchestrate --mode ${MODE}"
fi

for node in "${NODES[@]}"; do
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

echo "ERROR: all nodes unreachable." >&2
exit 1
