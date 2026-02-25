#!/bin/bash
set -euo pipefail
read -ra args <<< "${SSH_ORIGINAL_COMMAND:-}"

case "${args[0]:-}" in
    orchestrate|-v|--version) ;;
    *) echo "ERROR: only 'orchestrate' and '-v/--version' allowed" >&2; exit 1 ;;
esac

exec python3 /opt/styx/styx.pyz "${args[@]}"
