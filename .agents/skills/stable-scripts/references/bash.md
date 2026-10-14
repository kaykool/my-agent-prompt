# Bash scripts

Python is the default. Use Bash only for small glue where starting Python would be silly. If the job needs the spinner, per-step output or per-command timeouts, use `assets/template.py` instead; `assets/template.sh` has only plain log lines and a `timeout` wrapper.

The checklist below follows [10 cool tips for writing robust Bash scripts](https://medium.com/better-programming/10-cool-tips-for-writing-robust-bash-scripts-7333531ca5fb) (Better Programming), with the changes noted.

| Tip from the article | In `assets/template.sh` | Notes |
|---|---|---|
| Stable shebang | `#!/usr/bin/env bash` | Not `/bin/sh`: its target varies, and the script uses Bash features. |
| Relocatable script | not included | Add `SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)` only if the script reads files shipped beside it. An unused variable would fail shellcheck. |
| Stop on errors, trap them | `set -Eeuo pipefail`, `trap ... ERR` | Added `pipefail` (a failure in the middle of a pipe otherwise passes) and `-E` (so the ERR trap also fires inside functions). |
| Fail on unset variables | `-u`, defaults like `${TIMEOUT:-60}` | Give every optional input a default. |
| Quote paths | quote every expansion: `"$file"` | Most important before `rm`, `mv`, `cp`. shellcheck flags the misses. |
| Arrays for long commands | build `opts=()`, `opts+=(--flag "$v")`, call `cmd "${opts[@]}"` | Not in the template; use when a call takes many optional arguments. |
| Safe globbing | `shopt -s nullglob` | A pattern with no match then loops zero times. |
| Argument parsing | `while` + `case`, with `--` handling | Unknown options exit 2 with a message. |
| Built-in string functions | `${x,,}`, `${x^^}` | Supported: CachyOS runs Bash 5.2+, so modern Bash features (case conversion, associative arrays) are available. |
| shellcheck | `scripts/check.py` runs it | See below. |

Added beyond the article: a `timeout` on every `mutate` call (a hung command otherwise hangs the script), `--dry-run`, `-t/--timeout`, `need` for upfront dependency checks, and errors on stderr with results on stdout.

## shellcheck

The checker runs `bash -n` (or `sh -n`), then `shellcheck -x -s <shell>`; the shell comes from the shebang. shellcheck is installed on this machine at `/usr/bin/shellcheck`. Note that the interactive shell is fish, but scripts must use `#!/usr/bin/env bash` because shellcheck checks sh/bash only.

Do not silence a finding with `# shellcheck disable=` unless you can state in one line why the code is right; put that reason in the comment beside it.
