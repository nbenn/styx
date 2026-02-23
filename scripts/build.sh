#!/bin/bash
# Build styx.pyz — a self-contained Python zipapp.
#
# Usage: bash scripts/build.sh [output]
# Default output: styx.pyz in the repo root.
#
# The zip must contain styx/ as a subdirectory so that
# "from styx.xxx import ..." works inside __main__.py.

set -euo pipefail

OUTPUT="${1:-styx.pyz}"
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT

# Package as a subdirectory
cp -r styx "$STAGING/styx"
# Remove __pycache__ — not needed and bloats the archive
find "$STAGING" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Root entry point — same content as styx/__main__.py;
# "from styx.orchestrate import main" works because styx/ is
# a subdirectory inside the zip.
cp styx/__main__.py "$STAGING/__main__.py"

python3 -m zipapp "$STAGING" \
    --output "$OUTPUT" \
    --python '/usr/bin/env python3' \
    --compress

echo "Built $OUTPUT ($(du -sh "$OUTPUT" | cut -f1))"
