#!/usr/bin/env bash
# NAME: one-line purpose.
# Exit codes: 0 ok, 1 failure, 2 bad usage, 124 command timed out.
set -Eeuo pipefail
shopt -s nullglob # a glob with no matches expands to nothing, not to the literal pattern

DRY_RUN=0
TIMEOUT=${TIMEOUT:-60} # seconds per mutate call; override with TIMEOUT=120 NAME

log() { printf '%s\n' "$*" >&2; }
die() {
  log "error: $1"
  exit "${2:-1}"
}
# Catches anything unexpected. -E (above) makes the trap fire inside functions too.
trap 'log "error: line $LINENO: $BASH_COMMAND (exit $?)"' ERR

need() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null || die "missing command: $cmd"
  done
}

# Run a command that changes the system: time-limited, skipped under --dry-run.
# timeout runs programs, not shell functions.
mutate() {
  if ((DRY_RUN)); then
    log "dry-run: $*"
    return 0
  fi
  local status=0
  timeout "$TIMEOUT" "$@" || status=$?
  ((status != 124)) || die "timed out after ${TIMEOUT}s: $*" 124
  ((status == 0)) || die "command failed (exit $status): $*" "$status"
}

usage() {
  cat <<EOF
Usage: ${0##*/} [-n|--dry-run] [-t|--timeout SEC] [-h|--help] [--] [ARGS...]
  -n, --dry-run      show changes without making them
  -t, --timeout SEC  per-command time limit in seconds (default $TIMEOUT)
  -h, --help         show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case $1 in
    -n | --dry-run) DRY_RUN=1 ;;
    -t | --timeout)
      shift
      [[ $# -gt 0 ]] || die "missing argument for $1" 2
      TIMEOUT="$1"
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*) die "unknown option: $1 (try --help)" 2 ;;
    *) break ;;
  esac
  shift
done

main() {
  need uname timeout
  log "reading kernel version"
  uname -sr # results go to stdout, messages to stderr
}

main "$@"
