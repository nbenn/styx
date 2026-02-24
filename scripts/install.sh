#!/usr/bin/env bash
#
# install.sh — Install styx.pyz on all Proxmox cluster nodes.
#
# Usage: install.sh [--hosts HOST ...] [--pyz PATH] [--update-self]
#   --pyz PATH      Use a local .pyz file instead of downloading from GitHub
#   --hosts HOST    Explicit host list (repeatable); default: auto-discover via pvesh
#   --update-self   Replace this script with the latest release from GitHub, then exit
#
# Downloads the latest styx.pyz from GitHub releases (unless --pyz is given),
# then copies it to /opt/styx/styx.pyz on every node. Re-runnable for upgrades.

set -euo pipefail

INSTALL_DIR="/opt/styx"
INSTALL_PATH="${INSTALL_DIR}/styx.pyz"
RELEASE_BASE="https://github.com/nbenn/styx/releases/latest/download"

pyz=""
hosts=()
update_self=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pyz)
            pyz="$2"; shift 2 ;;
        --hosts)
            shift
            while [[ $# -gt 0 && ! "$1" =~ ^-- ]]; do
                hosts+=("$1"); shift
            done
            ;;
        --update-self)
            update_self=true; shift ;;
        -h|--help)
            sed -n '3,10s/^# //p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ── Self-update ──────────────────────────────────────────────────────────────

if $update_self; then
    self="$(readlink -f "$0")"
    echo "Downloading latest install.sh from GitHub releases..."
    curl -fSL "${RELEASE_BASE}/install.sh" -o "${self}.tmp"
    chmod +x "${self}.tmp"
    mv "${self}.tmp" "$self"
    echo "Updated ${self}"
    exit 0
fi

# ── Obtain .pyz ───────────────────────────────────────────────────────────────

cleanup_tmp=""
if [[ -z "$pyz" ]]; then
    tmp="$(mktemp)"
    cleanup_tmp="$tmp"
    echo "Downloading styx.pyz from GitHub releases..."
    curl -fSL "${RELEASE_BASE}/styx.pyz" -o "$tmp"
    pyz="$tmp"
fi

trap '[ -n "$cleanup_tmp" ] && rm -f "$cleanup_tmp"' EXIT

# ── Report version ────────────────────────────────────────────────────────────

version="$(python3 "$pyz" --version 2>/dev/null)" || version="unknown"
echo "Installing styx ${version}"

# ── Discover hosts ────────────────────────────────────────────────────────────

if [[ ${#hosts[@]} -eq 0 ]]; then
    echo "Discovering cluster nodes via pvesh..."
    mapfile -t hosts < <(
        pvesh get /cluster/status --output-format json \
        | python3 -c "
import json, sys
for e in json.load(sys.stdin):
    if e.get('type') == 'node' and e.get('ip'):
        print(e['name'] + '=' + e['ip'])
"
    )
fi

# ── Resolve host IPs ─────────────────────────────────────────────────────────
# hosts entries are either "name=ip" (from discovery) or bare hostnames (from
# --hosts). For bare hostnames, re-discover IPs from pvesh.

declare -A host_ips
needs_resolve=()
for entry in "${hosts[@]}"; do
    if [[ "$entry" == *=* ]]; then
        name="${entry%%=*}"
        ip="${entry#*=}"
        host_ips["$name"]="$ip"
    else
        needs_resolve+=("$entry")
    fi
done

if [[ ${#needs_resolve[@]} -gt 0 ]]; then
    # Build a lookup table from pvesh, then resolve requested hosts
    declare -A all_ips
    while IFS='=' read -r name ip; do
        all_ips["$name"]="$ip"
    done < <(
        pvesh get /cluster/status --output-format json \
        | python3 -c "
import json, sys
for e in json.load(sys.stdin):
    if e.get('type') == 'node' and e.get('ip'):
        print(e['name'] + '=' + e['ip'])
"
    )
    for name in "${needs_resolve[@]}"; do
        if [[ -n "${all_ips[$name]+x}" ]]; then
            host_ips["$name"]="${all_ips[$name]}"
        else
            echo "ERROR: cannot resolve IP for host '$name'" >&2
            exit 1
        fi
    done
fi

# ── Detect local hostname ────────────────────────────────────────────────────

local_hostname="$(hostname -s)"

# ── Install on each node ──────────────────────────────────────────────────────

fail=0
for name in "${!host_ips[@]}"; do
    ip="${host_ips[$name]}"

    if [[ "$name" == "$local_hostname" ]]; then
        echo "Installing on ${name} (local)..."
        mkdir -p "$INSTALL_DIR"
        cp "$pyz" "$INSTALL_PATH"
        chmod +x "$INSTALL_PATH"
    else
        echo "Installing on ${name} (${ip})..."
        ssh -o ConnectTimeout=5 -o BatchMode=yes "root@${ip}" \
            "mkdir -p ${INSTALL_DIR} && cat > ${INSTALL_PATH} && chmod +x ${INSTALL_PATH}" \
            < "$pyz"
    fi

    # Verify
    if [[ "$name" == "$local_hostname" ]]; then
        if python3 "$INSTALL_PATH" vm-shutdown --help >/dev/null 2>&1; then
            echo "  OK"
        else
            echo "  FAILED verification on ${name}" >&2
            fail=1
        fi
    else
        if ssh -o ConnectTimeout=5 -o BatchMode=yes "root@${ip}" \
                "python3 ${INSTALL_PATH} vm-shutdown --help" >/dev/null 2>&1; then
            echo "  OK"
        else
            echo "  FAILED verification on ${name}" >&2
            fail=1
        fi
    fi
done

if [[ $fail -ne 0 ]]; then
    echo "Some nodes failed verification." >&2
    exit 1
fi

echo "Done. styx installed at ${INSTALL_PATH} on all nodes."
