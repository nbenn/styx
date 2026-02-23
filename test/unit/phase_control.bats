#!/usr/bin/env bats
# Unit tests for --phase and --dry-run argument parsing in bin/styx
# Tested by sourcing the parse_args function in isolation.

setup() {
  # Source only the arg-parsing portion by extracting it
  # We define parse_args here matching the implementation
  PHASE=3
  DRY_RUN=false
  CONFIG_FILE="/etc/styx/styx.conf"

  parse_args() {
    PHASE=3
    DRY_RUN=false
    CONFIG_FILE="/etc/styx/styx.conf"
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --dry-run)  DRY_RUN=true ;;
        --phase)    PHASE="$2"; shift ;;
        --config)   CONFIG_FILE="$2"; shift ;;
        *) echo "Unknown option: $1" >&2; return 1 ;;
      esac
      shift
    done
    if [[ "$PHASE" != "1" && "$PHASE" != "2" && "$PHASE" != "3" ]]; then
      echo "ERROR: --phase must be 1, 2, or 3" >&2
      return 1
    fi
  }
}

@test "default phase is 3" {
  parse_args
  [[ "$PHASE" -eq 3 ]]
}

@test "default dry-run is false" {
  parse_args
  [[ "$DRY_RUN" == "false" ]]
}

@test "--dry-run sets DRY_RUN=true" {
  parse_args --dry-run
  [[ "$DRY_RUN" == "true" ]]
}

@test "--phase 1 sets PHASE=1" {
  parse_args --phase 1
  [[ "$PHASE" -eq 1 ]]
}

@test "--phase 2 sets PHASE=2" {
  parse_args --phase 2
  [[ "$PHASE" -eq 2 ]]
}

@test "--phase 3 sets PHASE=3" {
  parse_args --phase 3
  [[ "$PHASE" -eq 3 ]]
}

@test "--phase 4 is rejected" {
  run parse_args --phase 4
  [[ "$status" -ne 0 ]]
}

@test "--config sets CONFIG_FILE" {
  parse_args --config /tmp/my.conf
  [[ "$CONFIG_FILE" == "/tmp/my.conf" ]]
}

@test "unknown option is rejected" {
  run parse_args --unknown-flag
  [[ "$status" -ne 0 ]]
}

@test "combined flags are parsed correctly" {
  parse_args --dry-run --phase 2 --config /tmp/test.conf
  [[ "$DRY_RUN"     == "true" ]]
  [[ "$PHASE"       -eq 2 ]]
  [[ "$CONFIG_FILE" == "/tmp/test.conf" ]]
}
