#!/bin/bash
#
# check.sh — Verify that a trigger would get through, without triggering.
#
# Polls the UPS once, then runs `trigger.sh -v` against each controller on its
# own. gate.sh answers -v with the styx version, so this exercises the SSH
# key, host key, authorized_keys entry, gate.sh and styx.pyz on every node
# without running orchestrate. A styx version that differs from this image is
# reported but not treated as a failure.
#
# Creates $STYX_STATE_DIR/ready when everything passes and removes it
# otherwise; the readiness probe tests for that file. watch.sh runs this at
# startup and every STYX_CHECK_INTERVAL seconds; run it any time with
# `kubectl exec deploy/styx-trigger -- check`.
#
# Exit codes:
#   0  UPS and all controllers OK
#   1  at least one check failed

set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/common.sh"

require_controllers

IMAGE_VERSION="${STYX_IMAGE_VERSION:-$(cat "${LIB_DIR}/VERSION" 2>/dev/null || echo unknown)}"
TIMEOUT="${STYX_CHECK_TIMEOUT:-5}"

fail=0

if ups_status; then
    echo "UPS ${UPS}: ok (${UPS_STATUS})"
else
    echo "UPS ${UPS}: FAILED (${UPS_STATUS})"
    fail=1
fi

for node in "${controllers[@]}"; do
    rc=0
    out="$("$TRIGGER" --controllers "$node" ${ssh_args[@]+"${ssh_args[@]}"} \
           --timeout "$TIMEOUT" -v 2>&1)" || rc=$?
    if [[ $rc -ne 0 ]]; then
        # Drop trigger.sh's own progress lines and keep the last thing ssh or
        # gate.sh said (ssh's host key warning is a whole banner)
        detail="$(grep -v -e '^Trying ' -e ': unreachable or failed' \
                          -e '^ERROR: all controllers' -e '^[[:space:]]*$' <<< "$out" \
                  | tail -n1)"
        echo "controller ${node}: FAILED (${detail:-exit status ${rc}})"
        fail=1
        continue
    fi
    version="$(grep -Em1 '^[0-9]+\.[0-9]+' <<< "$out")"
    if [[ -z "$version" ]]; then
        echo "controller ${node}: FAILED (no styx version in response)"
        fail=1
    elif [[ "$IMAGE_VERSION" != unknown && "$version" != "$IMAGE_VERSION" ]]; then
        echo "controller ${node}: ok (styx ${version}), WARNING: image is ${IMAGE_VERSION}"
    else
        echo "controller ${node}: ok (styx ${version})"
    fi
done

mkdir -p "$STATE_DIR"
if [[ $fail -eq 0 ]]; then
    touch "${STATE_DIR}/ready"
    echo "all checks passed"
else
    rm -f "${STATE_DIR}/ready"
    echo "some checks FAILED"
fi
exit $fail
