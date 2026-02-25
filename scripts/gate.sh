#!/bin/bash
set -euo pipefail
read -ra args <<< "${SSH_ORIGINAL_COMMAND:-}"
exec python3 /opt/styx/styx.pyz "${args[@]}"
