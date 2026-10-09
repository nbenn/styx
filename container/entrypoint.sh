#!/bin/bash
#
# entrypoint.sh — Run the NUT driver, upsd on 127.0.0.1 and watch.sh.
#
# Mounted configuration:
#   /config/nut/ups.conf      NUT driver config (required); any other files in
#                             /config/nut (e.g. a dummy-ups .dev file) are
#                             copied next to it
#   /config/ssh/id            SSH private key for the styx gate (required
#                             unless STYX_SSH_KEY points elsewhere)
#   /config/ssh/known_hosts   Host keys of the controllers (optional; without
#                             it, host keys are not checked)

set -euo pipefail

NUT_CONFIG="${NUT_CONFIG:-/config/nut}"
SSH_CONFIG="${SSH_CONFIG:-/config/ssh}"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

if [[ ! -f "${NUT_CONFIG}/ups.conf" ]]; then
    log "ERROR: ${NUT_CONFIG}/ups.conf not found"
    exit 1
fi

# Copy rather than use in place: mounted secrets often have modes or owners
# that the NUT daemons (running as user nut) or ssh refuse.
cp -L "${NUT_CONFIG}"/* /etc/nut/
cat > /etc/nut/nut.conf <<'EOF'
MODE=standalone
EOF
cat > /etc/nut/upsd.conf <<'EOF'
LISTEN 127.0.0.1 3493
EOF
: > /etc/nut/upsd.users
chown root:nut /etc/nut/*
chmod 0640 /etc/nut/*

mkdir -p /run/nut
chown nut:nut /run/nut
chmod 0770 /run/nut

if [[ -z "${STYX_SSH_KEY:-}" ]]; then
    if [[ ! -f "${SSH_CONFIG}/id" ]]; then
        log "ERROR: ${SSH_CONFIG}/id not found (or set STYX_SSH_KEY)"
        exit 1
    fi
    # trigger.sh's default key, so check and simulate run through
    # `kubectl exec` use it as well
    install -d -m 0700 /root/.ssh
    install -m 0600 "${SSH_CONFIG}/id" /root/.ssh/styx
fi

# Run everything in the foreground and exit as soon as any of them does, so
# the pod restarts instead of watching a dead driver.
log "starting NUT driver"
upsdrvctl -F start &
log "starting upsd"
upsd -F &
"$(dirname "$(readlink -f "$0")")/watch.sh" &

rc=0
wait -n || rc=$?
log "a NUT daemon or watch.sh exited (status ${rc}); stopping"
exit $(( rc == 0 ? 1 : rc ))
