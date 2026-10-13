#!/usr/bin/env bash
# Sync the canonical AGENTS.md to every harness listed in harness.md.
# harness.md is a markdown table with columns: harness | base_dir | rules file | mode,
# where mode is "md" (whole file) or "lua" (rules embedded in a [[...]] string).
# Paths may use shell expansions ($HOME, ${XDG_CONFIG_HOME:-$HOME/.config}), evaluated at run time.
# Usage: sync-agent-rules.sh [--dry-run] [SOURCE_AGENTS_MD]
# Backups: a "<file>.bak" is written on the first run only, so it is never clobbered later.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
SOURCE="${SOURCE:-}"
for arg in "$@"; do
  case "$arg" in
    -n|--dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) SOURCE="$arg" ;;
  esac
done
SOURCE="${SOURCE:-$SCRIPT_DIR/AGENTS.md}"

[ -s "$SOURCE" ] || { echo "source missing or empty: $SOURCE" >&2; exit 1; }
if grep -q ']]' "$SOURCE"; then
  echo "source contains ']]', which would close the maki Lua string early" >&2
  exit 1
fi

HARNESS="${HARNESS:-$SCRIPT_DIR/harness.md}"
[ -r "$HARNESS" ] || { echo "harness file missing or unreadable: $HARNESS" >&2; exit 1; }

trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; printf '%s' "${s%"${s##*[![:space:]]}"}"; }

# parse_harness -> "label|path|kind" per data row (kind: md = whole file, lua = [[...]] body)
# Bad rows (missing fields, unknown mode, unreachable path) are reported and set PARSE_ERR.
parse_harness() {
  local line label base theFile kind lineno=0
  local -a f
  while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno + 1))
    line="${line%$'\r'}"                     # tolerate CRLF tables
    line="${line#"${line%%[![:space:]]*}"}"  # strip leading blanks
    [[ "$line" == \|* ]] || continue
    IFS='|' read -r -a f <<<"$line"
    # leading empty field from the row's first '|': label|base|file|mode -> f[1..4]
    label="${f[1]:-}"; base="${f[2]:-}"; theFile="${f[3]:-}"; kind="${f[4]:-}"
    label="$(trim "$label")"; base="$(trim "$base")"; theFile="$(trim "$theFile")"; kind="$(trim "$kind")"
    [[ "$label" == "harness" ]] && continue
    [[ "$label" == ---* ]] && continue          # separator row
    if [ -z "$label" ] && [ -z "$base" ] && [ -z "$theFile" ]; then continue; fi  # blank row
    if [ -z "$label" ] || [ -z "$base" ] || [ -z "$theFile" ]; then
      echo "warn   line $lineno: missing column in '${line}'" >&2; PARSE_ERR=1; continue
    fi
    if [[ "$kind" != "md" && "$kind" != "lua" ]]; then
      echo "warn   line $lineno: unknown mode '${kind:-<empty>}' (want md or lua)" >&2; PARSE_ERR=1; continue
    fi
    # expand ~ and shell variables ($HOME, ${XDG_CONFIG_HOME:-...}) like the shell would
    base="${base/#\~/$HOME}"
    base="$(eval "printf '%s' \"$base\"")" 2>/dev/null || base=""
    if [ -z "$base" ]; then
      echo "warn   line $lineno: base_dir expands to empty in '${line}'" >&2; PARSE_ERR=1; continue
    fi
    printf '%s|%s|%s\n' "$label" "$base/$theFile" "$kind"
  done < "$HARNESS"
}
PARSE_ERR=0
mapfile -t TARGETS < <(parse_harness)
[ "${#TARGETS[@]}" -gt 0 ] || { echo "no sync targets parsed from $HARNESS" >&2; exit 1; }
mapfile -t TARGETS < <(parse_harness)
[ "${#TARGETS[@]}" -gt 0 ] || { echo "no sync targets parsed from $HARNESS" >&2; exit 1; }

bounds() { # bounds PATH -> "open close" line numbers of the [[...]] block
  local o c
  o="$(grep -n 'content = \[\[' "$1" | head -n1 | cut -d: -f1 || true)"
  c="$(grep -n '^]]' "$1" | head -n1 | cut -d: -f1 || true)"
  [ -n "$o" ] && [ -n "$c" ] && [ "$c" -gt "$o" ] || return 1
  echo "$o $c"
}

body() { # body KIND PATH -> the rules text currently installed
  case "$1" in
    md) cat -- "$2" ;;
    lua)
      read -r o c < <(bounds "$2") || return 1
      sed -n "$((o + 1)),$((c - 1))p" "$2"
      ;;
  esac
}

install_lua() { # install_lua PATH -- replace the [[...]] block body with SOURCE
  local o c tmp
  read -r o c < <(bounds "$1") || { echo "no [[...]] block in $1" >&2; return 1; }
  tmp="$(mktemp)"
  { sed -n "1,${o}p" "$1"; cat -- "$SOURCE"; sed -n "${c},\$p" "$1"; } >"$tmp"
  cat -- "$tmp" >"$1"
  rm -f "$tmp"
}

fail=$PARSE_ERR
for t in "${TARGETS[@]}"; do
  IFS='|' read -r label path kind <<<"$t"
  if [ ! -e "$path" ]; then echo "skip   $label: no $path" >&2; fail=1; continue; fi
  if [ "$DRY_RUN" = 1 ]; then echo "would  $label -> $path"; continue; fi

  [ -e "$path.bak" ] || cp -- "$path" "$path.bak"
  if [ "$kind" = md ]; then
    cp -- "$SOURCE" "$path"
  else
    install_lua "$path" || { echo "FAIL   $label: $path" >&2; fail=1; continue; }
  fi

  if cmp -s <(body "$kind" "$path") "$SOURCE"; then
    echo "ok     $label: $path"
  else
    echo "FAIL   $label: $path" >&2
    fail=1
  fi
done
exit "$fail"