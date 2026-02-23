#!/usr/bin/env bats
# Unit tests for lib/config.sh

setup() {
  source "${BATS_TEST_DIRNAME}/../../lib/config.sh"
  TMPDIR="$(mktemp -d)"
}

teardown() {
  rm -rf "$TMPDIR"
}

write_conf() {
  cat > "${TMPDIR}/styx.conf"
}

@test "no config file: defaults are applied" {
  parse_config "/nonexistent/path"
  [[ "$STYX_TIMEOUT_DRAIN" -eq 120 ]]
  [[ "$STYX_TIMEOUT_VM"    -eq 120 ]]
  [[ "$STYX_CEPH_FLAGS" == *"noout"* ]]
}

@test "parse [hosts] section" {
  write_conf <<'EOF'
[hosts]
pve1 = 10.0.0.1
pve2 = 10.0.0.2
EOF
  parse_config "${TMPDIR}/styx.conf"
  [[ "${STYX_HOSTS[pve1]}" == "10.0.0.1" ]]
  [[ "${STYX_HOSTS[pve2]}" == "10.0.0.2" ]]
}

@test "parse [orchestrator] section" {
  write_conf <<'EOF'
[orchestrator]
host = pve1
EOF
  parse_config "${TMPDIR}/styx.conf"
  [[ "$STYX_ORCHESTRATOR" == "pve1" ]]
}

@test "parse [kubernetes] workers and control_plane" {
  write_conf <<'EOF'
[kubernetes]
workers = 211, 212, 213
control_plane = 201, 202
EOF
  parse_config "${TMPDIR}/styx.conf"
  [[ "$STYX_WORKERS" == "211 212 213" ]]
  [[ "$STYX_CP"      == "201 202" ]]
}

@test "parse [kubernetes] server, token, and ca_cert" {
  write_conf <<'EOF'
[kubernetes]
server = https://10.0.0.100:6443
token = /etc/styx/k8s-token
ca_cert = /etc/styx/k8s-ca.crt
EOF
  parse_config "${TMPDIR}/styx.conf"
  [[ "$STYX_K8S_SERVER"  == "https://10.0.0.100:6443" ]]
  [[ "$STYX_K8S_TOKEN"   == "/etc/styx/k8s-token"     ]]
  [[ "$STYX_K8S_CA_CERT" == "/etc/styx/k8s-ca.crt"    ]]
}

@test "parse [ceph] enabled and flags" {
  write_conf <<'EOF'
[ceph]
enabled = true
flags = noout, norebalance
EOF
  parse_config "${TMPDIR}/styx.conf"
  [[ "$STYX_CEPH_ENABLED" == "true" ]]
  [[ "$STYX_CEPH_FLAGS"   == "noout norebalance" ]]
}

@test "parse [timeouts] section" {
  write_conf <<'EOF'
[timeouts]
drain = 60
vm = 90
EOF
  parse_config "${TMPDIR}/styx.conf"
  [[ "$STYX_TIMEOUT_DRAIN" -eq 60 ]]
  [[ "$STYX_TIMEOUT_VM"    -eq 90 ]]
}

@test "inline comments are stripped" {
  write_conf <<'EOF'
[timeouts]
drain = 60 # fast shutdown
vm = 90    # generous
EOF
  parse_config "${TMPDIR}/styx.conf"
  [[ "$STYX_TIMEOUT_DRAIN" -eq 60 ]]
  [[ "$STYX_TIMEOUT_VM"    -eq 90 ]]
}

@test "blank lines and comment-only lines are ignored" {
  write_conf <<'EOF'

# This is a comment

[timeouts]
drain = 45
EOF
  parse_config "${TMPDIR}/styx.conf"
  [[ "$STYX_TIMEOUT_DRAIN" -eq 45 ]]
}
