#!/usr/bin/env bash
# Tests for sync-agent-rules.sh. Runs in a sandbox tmpdir; never touches real harness files.
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SUT="$SCRIPT_DIR/sync-agent-rules.sh"
fails=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1" >&2; fails=$((fails + 1)); }
eq()   { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (want [$3] got [$2])"; fi; }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

printf '# Rules\n\nline one\nline two\n' > "$tmp/src.md"

fixtures() {
  rm -rf "$tmp/oc" "$tmp/pi" "$tmp/maki"
  mkdir -p "$tmp/oc" "$tmp/pi/agent" "$tmp/maki/lua"
  printf 'stale\n' > "$tmp/oc/AGENTS.md"
  printf '# specpi\n' > "$tmp/pi/agent/APPEND_SYSTEM.md"
  cat > "$tmp/maki/lua/agent_rules.lua" <<'LUA'
local keep = true
maki.api.register_prompt_hint({
  content = [[
OLD RULES
]],
})
LUA
  cat > "$tmp/harness.md" <<EOF
| harness | base_dir | rules file | mode |
| --- | --- | --- | --- |
| oc | $tmp/oc | AGENTS.md | md |
| pi | $tmp/pi | agent/APPEND_SYSTEM.md | md |
| maki | $tmp/maki | lua/agent_rules.lua | lua |
EOF
}
run() { HARNESS="$1" SOURCE="$tmp/src.md" "$SUT" "${@:2}"; }

bash -n "$SUT" && pass "syntax (bash -n)" || fail "syntax (bash -n)"

# dry-run: lists every row, writes nothing
fixtures
out="$(run "$tmp/harness.md" --dry-run)"
eq "dry-run lists all rows" "$(printf '%s\n' "$out" | grep -c '^would')" 3
eq "dry-run writes nothing" "$(cat "$tmp/oc/AGENTS.md")" "stale"

# real run: md copied, lua body embedded with scaffold preserved
run "$tmp/harness.md" >/dev/null && pass "run exit 0" || fail "run exit 0"
cmp -s "$tmp/oc/AGENTS.md" "$tmp/src.md" && pass "md target == source" || fail "md target == source"
cmp -s "$tmp/pi/agent/APPEND_SYSTEM.md" "$tmp/src.md" && pass "pi md target == source" || fail "pi md target == source"
grep -q '^local keep = true' "$tmp/maki/lua/agent_rules.lua" && pass "lua scaffold preserved" || fail "lua scaffold preserved"
sed -n '/content = \[\[/,/^]]/p' "$tmp/maki/lua/agent_rules.lua" | sed '1d;$d' > "$tmp/body"
cmp -s "$tmp/body" "$tmp/src.md" && pass "lua body == source" || fail "lua body == source"
[ -e "$tmp/oc/AGENTS.md.bak" ] && pass "backup created" || fail "backup created"

# backup written once, then preserved across reruns
cp -p "$tmp/oc/AGENTS.md.bak" "$tmp/bak.keep"
run "$tmp/harness.md" >/dev/null
cmp -s "$tmp/oc/AGENTS.md.bak" "$tmp/bak.keep" && pass "backup not clobbered on rerun" || fail "backup not clobbered on rerun"

# $HOME in harness.md is expanded at run time
mkdir -p "$tmp/h/.pi/agent"; printf 'x\n' > "$tmp/h/.pi/agent/APPEND_SYSTEM.md"
printf '| harness | base_dir | rules file | mode |\n| --- | --- | --- | --- |\n| pi | $HOME/.pi | agent/APPEND_SYSTEM.md | md |\n' > "$tmp/harness2.md"
HOME="$tmp/h" run "$tmp/harness2.md" >/dev/null
cmp -s "$tmp/h/.pi/agent/APPEND_SYSTEM.md" "$tmp/src.md" && pass '$HOME expansion' || fail '$HOME expansion'

# an empty/garbled table must fail fast, not silently no-op
printf 'nothing here\n' > "$tmp/empty.md"
if run "$tmp/empty.md" >/dev/null 2>&1; then fail "empty table exits nonzero"; else pass "empty table exits nonzero"; fi

# last row without a trailing newline must still be parsed
fixtures
printf '| harness | base_dir | rules file | mode |\n| --- | --- | --- | --- |\n| oc | %s/oc | AGENTS.md | md |\n| pi | %s/pi | agent/APPEND_SYSTEM.md | md |' "$tmp" "$tmp" > "$tmp/nonl.md"
eq "no trailing newline: all rows" "$(run "$tmp/nonl.md" --dry-run | grep -c '^would')" 2

# CRLF line endings must not drop rows
printf '| harness | base_dir | rules file | mode |\r\n| --- | --- | --- | --- |\r\n| oc | %s/oc | AGENTS.md | md |\r\n| pi | %s/pi | agent/APPEND_SYSTEM.md | md |\r\n' "$tmp" "$tmp" > "$tmp/crlf.md"
eq "CRLF: all rows" "$(run "$tmp/crlf.md" --dry-run | grep -c '^would')" 2

# unknown mode must warn and fail, not silently skip
printf '| harness | base_dir | rules file | mode |\n| --- | --- | --- | --- |\n| oc | %s/oc | AGENTS.md | MD |\n' "$tmp" > "$tmp/badmode.md"
if run "$tmp/badmode.md" --dry-run >/dev/null 2>&1; then fail "unknown mode exits nonzero"; else pass "unknown mode exits nonzero"; fi

echo
if [ "$fails" -eq 0 ]; then echo "All tests passed."; else echo "$fails test(s) failed." >&2; fi
exit "$fails"