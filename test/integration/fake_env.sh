#!/bin/bash
# fake_env.sh — Sets up a fake environment for integration tests.

FAKE_ROOT="${BATS_TMPDIR}/fake_root"
FAKE_RUN_DIR="${FAKE_ROOT}/var/run/qemu-server"
FAKE_CONF_DIR="${FAKE_ROOT}/etc/styx"

POWEROFF_LOG="${FAKE_ROOT}/poweroff.log"
DRAIN_LOG="${FAKE_ROOT}/drain.log"
SHUTDOWN_LOG="${FAKE_ROOT}/shutdown.log"
HA_LOG="${FAKE_ROOT}/ha.log"
CEPH_LOG="${FAKE_ROOT}/ceph.log"
CORDON_LOG="${FAKE_ROOT}/cordon.log"

fake_env_setup() {
  mkdir -p "$FAKE_RUN_DIR" "${FAKE_ROOT}/var/log" "$FAKE_CONF_DIR"
  touch "$POWEROFF_LOG" "$DRAIN_LOG" "$SHUTDOWN_LOG" "$HA_LOG" "$CEPH_LOG" "$CORDON_LOG"
}

fake_env_teardown() {
  local pidfile
  for pidfile in "${FAKE_RUN_DIR}"/*.pid; do
    [[ -f "$pidfile" ]] || continue
    local pid; pid="$(cat "$pidfile" 2>/dev/null)" || continue
    kill "$pid" 2>/dev/null || true
  done
  rm -rf "$FAKE_ROOT"
}

# start_fake_vm VMID
start_fake_vm() {
  local vmid="$1"
  sleep 3600 &
  echo $! > "${FAKE_RUN_DIR}/${vmid}.pid"
}

# stop_fake_vm VMID
stop_fake_vm() {
  local pidfile="${FAKE_RUN_DIR}/${1}.pid"
  [[ -f "$pidfile" ]] || return 0
  kill "$(cat "$pidfile")" 2>/dev/null || true
  rm -f "$pidfile"
}

# write_fake_config HOSTS ORCHESTRATOR WORKERS CP CEPH_ENABLED
# HOSTS: space-separated "name=ip" pairs
write_fake_config() {
  local hosts="$1" orchestrator="$2" workers="$3" cp="$4" ceph="${5:-false}"
  {
    echo "[hosts]"
    local pair; for pair in $hosts; do echo "${pair/=/ = }"; done
    echo ""
    echo "[orchestrator]"
    echo "host = ${orchestrator}"
    echo ""
    if [[ -n "$workers" || -n "$cp" ]]; then
      echo "[kubernetes]"
      [[ -n "$workers" ]] && echo "workers = $(echo "$workers" | tr ' ' ',')"
      [[ -n "$cp"      ]] && echo "control_plane = $(echo "$cp" | tr ' ' ',')"
      echo ""
    fi
    echo "[ceph]"
    echo "enabled = ${ceph}"
    echo ""
    echo "[timeouts]"
    echo "drain = 5"
    echo "vm = 5"
  } > "${FAKE_CONF_DIR}/styx.conf"
}

# write_fake_wrappers HOST_IPS_DECL ORCHESTRATOR VMID_HOST_DECL VMID_NAME_DECL
#                     WORKERS CP K8S_ENABLED CEPH_ENABLED [CEPH_FLAGS]
#
# Generates ${FAKE_ROOT}/fake_wrappers.sh and exports STYX_WRAPPERS_FILE.
# bin/styx sources this file last, so it overrides all default functions.
#
# HOSTS / VMID_HOST / VMID_NAME: space-separated "key=value" pairs
# WORKERS / CP: space-separated VMIDs or ""
write_fake_wrappers() {
  local hosts_pairs="$1"
  local orchestrator="$2"
  local vmid_host_pairs="$3"
  local vmid_name_pairs="$4"
  local workers="$5"
  local cp_nodes="$6"
  local k8s_enabled="$7"
  local ceph_enabled="$8"
  local ceph_flags="${9:-noout norecover norebalance nobackfill nodown noup}"

  local _fr="$FAKE_ROOT"
  local _rd="$FAKE_RUN_DIR"

  # Build bash associative-array initialiser strings
  local host_ips_init="" pair name val
  for pair in $hosts_pairs; do
    name="${pair%%=*}"; val="${pair##*=}"
    host_ips_init+=" [${name}]=\"${val}\""
  done

  local vmid_host_init=""
  for pair in $vmid_host_pairs; do
    name="${pair%%=*}"; val="${pair##*=}"
    vmid_host_init+=" [${name}]=\"${val}\""
  done

  local vmid_name_init=""
  for pair in $vmid_name_pairs; do
    name="${pair%%=*}"; val="${pair##*=}"
    vmid_name_init+=" [${name}]=\"${val}\""
  done

  # Variables we want expanded NOW (at write time) use $var.
  # Variables that should be evaluated at runtime inside bin/styx use \$var.
  cat > "${_fr}/fake_wrappers.sh" <<ENDOFWRAPPERS
#!/bin/bash
# Auto-generated fake wrappers — do not edit

LOG_FILE="${_fr}/var/log/styx.log"
STYX_POLL_INTERVAL=1

run_discovery() {
  parse_config "${_fr}/etc/styx/styx.conf"
  declare -gA HOST_IPS=(${host_ips_init})
  ORCHESTRATOR="${orchestrator}"
  declare -gA VMID_HOST=(${vmid_host_init})
  declare -gA VMID_NAME=(${vmid_name_init})
  K8S_WORKERS=(${workers})
  K8S_CP=(${cp_nodes})
  K8S_ENABLED="${k8s_enabled}"
  CEPH_ENABLED="${ceph_enabled}"
  STYX_CEPH_FLAGS="${ceph_flags}"
  TIMEOUT_DRAIN="\${STYX_TIMEOUT_DRAIN:-5}"
  TIMEOUT_VM="\${STYX_TIMEOUT_VM:-5}"
}

run_on_host() { local _h="\$1"; shift; bash -c "\$*"; }

get_running_vmids() {
  local pidfile vmid pid
  for pidfile in "${_rd}"/*.pid; do
    [[ -f "\$pidfile" ]] || continue
    vmid="\${pidfile##*/}"; vmid="\${vmid%.pid}"
    pid="\$(cat "\$pidfile" 2>/dev/null)" || continue
    kill -0 "\$pid" 2>/dev/null && echo "\$vmid"
  done
}

is_api_reachable() { return 1; }

cordon_node()        { echo "CORDON \$1"      >> "${_fr}/cordon.log"; }
drain_node()         { echo "DRAIN \$1"       >> "${_fr}/drain.log"; }
set_ceph_flags()     { echo "CEPH_FLAGS \$*"  >> "${_fr}/ceph.log"; }
disable_ha_sid()     { echo "DISABLE_HA \$1"  >> "${_fr}/ha.log"; }
get_ha_started_sids() { echo ""; }

shutdown_vm() {
  local _host="\$1" _vmid="\$2"
  echo "SHUTDOWN \${_vmid} on \${_host}" >> "${_fr}/shutdown.log"
  local _pf="${_rd}/\${_vmid}.pid"
  if [[ -f "\$_pf" ]]; then
    kill "\$(cat "\$_pf")" 2>/dev/null || true
    rm -f "\$_pf"
  fi
}

poweroff_host() { echo "POWEROFF \$1"   >> "${_fr}/poweroff.log"; }
poweroff_self() { echo "POWEROFF_SELF"  >> "${_fr}/poweroff.log"; }
ENDOFWRAPPERS

  export STYX_WRAPPERS_FILE="${_fr}/fake_wrappers.sh"
}
