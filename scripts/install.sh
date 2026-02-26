#!/usr/bin/env bash
#
# install.sh — Install styx.pyz on all Proxmox cluster nodes.
#
# Usage: install.sh [--hosts HOST ...] [--pyz PATH] [--install-dir DIR] [--update-self]
#                   [--sync-config] [--include-gate]
#   --pyz PATH        Use a local .pyz file instead of downloading from GitHub
#   --hosts HOST      Explicit host list (repeatable); default: auto-discover via pvesh
#   --install-dir DIR Install to DIR/styx.pyz (default: /opt/styx)
#   --update-self     Replace this script with the latest release from GitHub, then exit
#   --sync-config     Sync all config files in INSTALL_DIR (except styx.pyz) to peers
#   --include-gate            Also install gate.sh alongside styx.pyz
#
# Downloads the latest styx.pyz from GitHub releases (unless --pyz is given),
# then copies it to /opt/styx/styx.pyz on every node. Re-runnable for upgrades.

set -euo pipefail

VERSION="0.2.1"
INSTALL_DIR="/opt/styx"
INSTALL_PATH="${INSTALL_DIR}/styx.pyz"
RELEASE_BASE="https://github.com/nbenn/styx/releases/latest/download"

pyz=""
hosts=()
update_self=false
install_dir=""
sync_config=false
include_gate=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pyz)
            pyz="$2"; shift 2 ;;
        --install-dir)
            install_dir="$2"; shift 2 ;;
        --hosts)
            shift
            while [[ $# -gt 0 && ! "$1" =~ ^-- ]]; do
                hosts+=("$1"); shift
            done
            ;;
        --update-self)
            update_self=true; shift ;;
        --sync-config)
            sync_config=true; shift ;;
        --include-gate)
            include_gate=true; shift ;;
        -h|--help)
            sed -n '3,12s/^# //p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ -n "$install_dir" ]]; then
    INSTALL_DIR="$install_dir"
    INSTALL_PATH="${INSTALL_DIR}/styx.pyz"
fi

# ── Self-update ──────────────────────────────────────────────────────────────

if $update_self; then
    if $sync_config || $include_gate || [[ -n "$pyz" ]] || [[ ${#hosts[@]} -gt 0 ]] || [[ -n "$install_dir" ]]; then
        echo "WARNING: --update-self exits after updating; other flags are ignored. Re-run without --update-self to apply them."
    fi
    self="$(readlink -f "$0")"
    echo "Downloading latest install.sh from GitHub releases..."
    curl -fSL "${RELEASE_BASE}/install.sh" -o "${self}.tmp"
    chmod +x "${self}.tmp"
    new_version="$(grep -m1 '^VERSION=' "${self}.tmp" | cut -d'"' -f2)"
    mv "${self}.tmp" "$self"
    echo "Updated ${self}: ${VERSION} -> ${new_version:-unknown}"
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

# ── Generate gate.sh ─────────────────────────────────────────────────────────

write_gate() {
    cat <<'GATE'
#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
read -ra args <<< "${SSH_ORIGINAL_COMMAND:-}"

case "${args[0]:-}" in
    orchestrate|-v|--version) ;;
    *) echo "ERROR: only 'orchestrate' and '-v/--version' allowed" >&2; exit 1 ;;
esac

exec python3 "$DIR/styx.pyz" "${args[@]}"
GATE
}

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

    # Install gate.sh
    if $include_gate; then
        gate_dst="${INSTALL_DIR}/gate.sh"
        if [[ "$name" == "$local_hostname" ]]; then
            write_gate > "$gate_dst"
            chmod +x "$gate_dst"
        else
            write_gate | ssh -o ConnectTimeout=5 -o BatchMode=yes "root@${ip}" \
                "cat > ${gate_dst} && chmod +x ${gate_dst}"
        fi
        echo "  gate.sh installed"
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

# ── Sync config files ────────────────────────────────────────────────────────

if $sync_config; then
    mapfile -t config_files < <(
        find "$INSTALL_DIR" -maxdepth 1 -type f ! -name 'styx.pyz' ! -name 'gate.sh' ! -name 'install.sh' -printf '%f\n'
    )

    if [[ ${#config_files[@]} -eq 0 ]]; then
        echo "No config files found in ${INSTALL_DIR} to sync."
    else
        echo "Syncing config files: ${config_files[*]}"
        for name in "${!host_ips[@]}"; do
            ip="${host_ips[$name]}"

            if [[ "$name" == "$local_hostname" ]]; then
                echo "  ${name} (local): skipping, files already in place"
                continue
            fi

            for cfg in "${config_files[@]}"; do
                src="${INSTALL_DIR}/${cfg}"
                perms="$(stat -c '%a' "$src")"
                echo "  ${name}: syncing ${cfg}..."
                ssh -o ConnectTimeout=5 -o BatchMode=yes "root@${ip}" \
                    "cat > ${INSTALL_DIR}/${cfg} && chmod ${perms} ${INSTALL_DIR}/${cfg}" \
                    < "$src"
            done
        done
    fi
fi

echo "Done. styx installed at ${INSTALL_PATH} on all nodes."
