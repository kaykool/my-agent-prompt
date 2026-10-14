---
name: stable-scripts
description: Write stable, robust scripts (Python by default) with a spinner, clean step-by-step output, error handling, --dry-run and a hard timeout on every external call. Tailored specifically for CachyOS Linux (KDE Plasma 6 on Wayland, fish shell, btrfs root with snapper, systemd user timers, pacman). Use whenever the user asks for a script, tool, helper, installer, automation, cleanup, backup, maintenance or "something I can run from the terminal". Bash only for a small wrapper where a shell script is clearly the better fit. Also use to review or harden an existing script, Python or shell.
---

# Stable scripts

"Stable" means four things: the script never hangs, never leaves a half-done mess, says plainly what happened, and can be re-run safely. The templates in this skill already do all four, so start from one instead of writing from scratch. All paths below are relative to this skill's folder.

## Target Environment (CachyOS)

This skill is configured specifically for this machine:
- **OS**: CachyOS Linux x86_64 (Arch rolling)
- **Interactive Shell**: fish 4 (`/bin/fish`). Scripts use Python 3 or Bash (`#!/usr/bin/env bash`).
- **Desktop**: KDE Plasma 6 on Wayland (`wl-copy`/`wl-paste`, `notify-send`, `kdialog`, `kreadconfig6`/`kwriteconfig6`).
- **Package Manager**: `pacman` (`pacman -S --needed`, `pacman -Qq`), `sudo` interactive handling.
- **Filesystem**: btrfs root subvolume (`/@`) with active `snapper` root configuration.
- **Hardware**: Hybrid graphics (`prime-run` for NVIDIA GPU), battery laptop.
- **Service Manager**: systemd user session (`systemctl --user`).

## Workflow

1. **Target environment:** Refer to `references/environments.md` for specific tool paths, desktop integrations, and btrfs/snapper handling.
2. **Pin down the job** in at most one question, only if it is unclear: what it does, which inputs it takes, and whether it changes the system (installs, deletes, writes outside a temp dir). Otherwise proceed and state your assumption in one line.
3. **Copy `assets/template.py`** to the new script's name. Replace `work()`; keep the rest. Add options in `parse_args()`.
4. **Put each unit of work in `step("Label")`** so the user sees a spinner and one result line per step. Mark anything that changes the system with `mutates=True` (or use `write_atomic`) so `--dry-run` skips it. For destructive or bulk filesystem mutations on btrfs, trigger a snapper pre-snapshot first.
5. **Check it:** `python3 scripts/check.py NEW_SCRIPT`. Python files get timeout checks, `shell=True` bans, and bare `except:` checks (plus ruff if available); shell files get `bash -n` and shellcheck (`/usr/bin/shellcheck`). Fix every error. Then run the real script: `--dry-run`, a failure path (bad input or missing command) and, if it calls external commands, a timeout (`--timeout 1` against something slow). Report what you ran and what came out.
6. **Say what you could not test.** Package operations, systemd user timers, desktop tools, and hardware behavior count as untested unless you actually ran them. Tell the user to run `NAME --dry-run` first.
7. **Deliver:** install with the steps from `references/environments.md`. When the source file lives in a repo or any folder you will keep editing, install it as a symlink so the installed command runs the source and never goes stale; reserve the plain copy for a throwaway source.

## When a shell script is the better fit

Default to Python. Use Bash only for a small wrapper, hook or installer snippet where starting Python would be silly. Start from `assets/template.sh` and read `references/bash.md`. Needing the spinner, step output or per-command timeouts is the signal to use the Python template instead. Write scripts in Bash with `#!/usr/bin/env bash` even though the interactive shell is fish; shellcheck cannot lint fish. A fish function is right only for things that must change the interactive shell itself (such as `cd` or setting environment variables).

## Output contract

The template produces this on a terminal; keep the same shape in anything you add:

```
✓ Read kernel version (0.0s)        # one line per step, with duration
✗ Fetch package index (60.0s)       # failure: step line, then...
error: timed out after 60s: curl ...
hint:  raise the limit with --timeout
```

- Status goes to **stderr**, results to **stdout**. That keeps `NAME | grep x` and `NAME > out.txt` clean.
- The spinner appears only on a terminal. Under a systemd service/timer or a pipe the output is plain `[ok]` / `[fail]` lines. `NO_COLOR` is honoured.
- Do not `print()` inside a `step()`; use `emit()` for notes, or return data and print it after the step.
- Be specific and short: say what happened and what to do next, never "something went wrong".

## Stability rules (and why)

- **A timeout on every external call.** A hung network request or a stuck package manager otherwise freezes the script forever. `run()` enforces it and kills the whole process group, not just the child. The checker flags `subprocess.run`, `check_output`, `urlopen` and friends without `timeout=`.
- **No `shell=True`.** Pass a list. Quoting bugs and injection both come from strings.
- **Expected failures raise `ScriptError`** with a message, an optional hint and an exit code. Unexpected exceptions are caught once in `main()`, shown briefly, and the traceback appears with `-v`. Never use bare `except:`.
- **Check dependencies up front** with `require("tool")`, so the user hears "missing command: x" before anything has changed.
- **Atomic writes** with `write_atomic()`; a crash mid-write then leaves the old file intact.
- **One instance at a time** via `single_instance()` (a `flock` that the OS releases on crash).
- **Idempotent:** a second run must not break or duplicate anything. Check state before acting (file exists, package installed, line already present).
- **Interrupts are clean:** Ctrl-C and SIGTERM stop children, restore the terminal line and exit 130.
- **Exit codes mean something:** 0 ok, 1 failure, 2 usage, 75 already running, 124 timeout, 130 interrupted.
- **Prompts need the terminal:** anything that asks for input (a sudo password, package-manager confirmations) goes through `run_interactive()` outside a `step()`.
- **Standard library only**, because system Python is externally managed (PEP 668).
- **Keep scope tight:** build what was asked; do not add features, config files or abstractions on the side.
