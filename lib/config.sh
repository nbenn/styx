#!/bin/bash
# lib/config.sh — INI config parser (all sections optional)
# Populates STYX_* variables from the config file.
# Only overrides auto-discovery when a section/key is present.

# Populated by parse_config:
#   STYX_HOSTS          — associative array: hostname -> ip (from [hosts])
#   STYX_ORCHESTRATOR   — string (from [orchestrator] host)
#   STYX_WORKERS        — space-separated VMIDs (from [kubernetes] workers)
#   STYX_CP             — space-separated VMIDs (from [kubernetes] control_plane)
#   STYX_K8S_SERVER     — API server URL (from [kubernetes] server)
#   STYX_K8S_TOKEN      — path to bearer token file (from [kubernetes] token)
#   STYX_K8S_CA_CERT    — path to CA certificate (from [kubernetes] ca_cert, optional)
#   STYX_CEPH_ENABLED   — "true"/"false" (from [ceph] enabled)
#   STYX_CEPH_FLAGS     — space-separated flags (from [ceph] flags)
#   STYX_TIMEOUT_DRAIN  — integer seconds (from [timeouts] drain)
#   STYX_TIMEOUT_VM     — integer seconds (from [timeouts] vm)

parse_config() {
  local config_file="${1:-/etc/styx/styx.conf}"

  # Defaults
  STYX_TIMEOUT_DRAIN=${STYX_TIMEOUT_DRAIN:-120}
  STYX_TIMEOUT_VM=${STYX_TIMEOUT_VM:-120}
  STYX_CEPH_FLAGS=${STYX_CEPH_FLAGS:-"noout norecover norebalance nobackfill nodown noup"}

  [[ ! -f "$config_file" ]] && return 0

  declare -gA STYX_HOSTS=()
  local section=""

  while IFS= read -r line || [[ -n "$line" ]]; do
    # Strip inline comments and trim whitespace
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" ]] && continue

    # Section header
    if [[ "$line" =~ ^\[([a-zA-Z0-9_]+)\]$ ]]; then
      section="${BASH_REMATCH[1]}"
      continue
    fi

    # Key = value
    if [[ "$line" =~ ^([a-zA-Z0-9_]+)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
      local key="${BASH_REMATCH[1]}"
      local val="${BASH_REMATCH[2]}"
      # Strip trailing whitespace from value
      val="${val%"${val##*[![:space:]]}"}"

      case "$section" in
        hosts)
          STYX_HOSTS["$key"]="$val"
          ;;
        orchestrator)
          [[ "$key" == "host" ]] && STYX_ORCHESTRATOR="$val"
          ;;
        kubernetes)
          case "$key" in
            workers)
              # Normalise: strip commas, collapse spaces
              STYX_WORKERS="$(echo "$val" | tr ',' ' ' | tr -s ' ')"
              ;;
            control_plane)
              STYX_CP="$(echo "$val" | tr ',' ' ' | tr -s ' ')"
              ;;
            server)   STYX_K8S_SERVER="$val"  ;;
            token)    STYX_K8S_TOKEN="$val"   ;;
            ca_cert)  STYX_K8S_CA_CERT="$val" ;;
          esac
          ;;
        ceph)
          case "$key" in
            enabled)  STYX_CEPH_ENABLED="$val" ;;
            flags)    STYX_CEPH_FLAGS="$(echo "$val" | tr ',' ' ' | tr -s ' ')" ;;
          esac
          ;;
        timeouts)
          case "$key" in
            drain) STYX_TIMEOUT_DRAIN="$val" ;;
            vm)    STYX_TIMEOUT_VM="$val"    ;;
          esac
          ;;
      esac
    fi
  done < "$config_file"
}
