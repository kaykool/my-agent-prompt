#!/usr/bin/env python3
"""rules-sync: propagate the canonical AGENTS.md rules to every AI harness.

The repo AGENTS.md is the source. harness-list.md maps each harness to its
rules file and mode: "md" installs the rules verbatim; "lua" embeds them in
maki's [[...]] prompt string, keeping the surrounding Lua wrapper. Only files
whose content actually changes are written; the first write to a file leaves a
one-time <file>.bak copy. --dry-run previews the unified diffs and writes
nothing.

Status output (steps, spinner, errors) goes to stderr; results go to stdout,
so `rules-sync | other-tool` and `rules-sync > file` stay clean.

Exit codes: 0 ok, 1 failure, 2 bad usage, 75 already running,
124 timeout, 130 interrupted.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import fcntl
import itertools
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Iterator, Sequence
from pathlib import Path

PROG = Path(sys.argv[0]).stem or "script"
DEFAULT_TIMEOUT = 60.0  # seconds per command; overridden by --timeout

EXIT_FAIL, EXIT_USAGE, EXIT_LOCKED, EXIT_TIMEOUT, EXIT_INTERRUPT = 1, 2, 75, 124, 130

DRY_RUN = False
VERBOSE = False

# --- output ----------------------------------------------------------------

_TTY = sys.stderr.isatty() and os.environ.get("TERM") != "dumb"
_COLOR = _TTY and "NO_COLOR" not in os.environ
_LOCK = threading.Lock()  # serialises writes so the spinner never garbles a line


def _paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


OK = _paint("32", "✓" if _TTY else "[ok]")
BAD = _paint("31", "✗" if _TTY else "[fail]")


def emit(text: str = "") -> None:
    """Print one status line to stderr without colliding with the spinner."""
    with _LOCK:
        if _TTY:
            sys.stderr.write("\r\033[K")
        print(text, file=sys.stderr, flush=True)


class Spinner:
    """Animated 'working' line on a terminal; silent when output is piped."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str) -> None:
        self.label = label
        self._start = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self) -> None:
        for frame in itertools.cycle(self.FRAMES):
            with _LOCK:
                secs = time.monotonic() - self._start
                sys.stderr.write(
                    f"\r\033[K{_paint('36', frame)} {self.label} {secs:.0f}s"
                )
                sys.stderr.flush()
            if self._stop.wait(0.1):
                return

    def __enter__(self) -> Spinner:
        if _TTY:
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if _TTY:
            self._stop.set()
            self._thread.join()
            with _LOCK:
                sys.stderr.write("\r\033[K")
                sys.stderr.flush()


@contextlib.contextmanager
def step(label: str) -> Iterator[None]:
    """Run a block under a spinner, then print exactly one result line."""
    start = time.monotonic()
    try:
        with Spinner(label):
            yield
    except BaseException:
        emit(f"{BAD} {label} ({time.monotonic() - start:.1f}s)")
        raise
    emit(f"{OK} {label} ({time.monotonic() - start:.1f}s)")


# --- errors ----------------------------------------------------------------


class ScriptError(Exception):
    """An expected failure: shown as a short message, never a traceback."""

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        code: int = EXIT_FAIL,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.hint, self.code, self.detail = hint, code, detail


def require(*commands: str) -> None:
    """Fail early and clearly if a needed program is not installed."""
    missing = [c for c in commands if shutil.which(c) is None]
    if missing:
        raise ScriptError(
            f"missing command: {', '.join(missing)}",
            hint="install the package that provides it, using your package manager",
        )


# --- running commands ------------------------------------------------------


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """Stop a child and everything it spawned: SIGTERM first, then SIGKILL."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
        try:
            proc.communicate(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue


def run(
    cmd: Sequence[str | Path],
    *,
    timeout: float | None = None,
    check: bool = True,
    mutates: bool = False,
    cwd: Path | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command with captured output, a hard timeout and no shell.

    mutates=True marks commands that change the system; --dry-run skips them.
    """
    if not cmd:
        raise ScriptError("empty command sequence")
    argv = [str(c) for c in cmd]
    timeout = DEFAULT_TIMEOUT if timeout is None else timeout
    if mutates and DRY_RUN:
        emit(f"{_paint('33', 'dry-run')} {shlex.join(argv)}")
        return subprocess.CompletedProcess(argv, 0, "", "")
    require(argv[0])
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            text=True,
            errors="replace",
            start_new_session=True,  # own process group, so a timeout can kill grandchildren
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as err:
        raise ScriptError(f"cannot run {argv[0]}: {err.strerror}") from err
    try:
        out, err_text = proc.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise ScriptError(
            f"timed out after {timeout:g}s: {shlex.join(argv)}",
            code=EXIT_TIMEOUT,
            hint="raise the limit with --timeout",
        ) from None
    except BaseException:
        _kill_group(proc)
        raise
    if check and proc.returncode != 0:
        diagnostic = (err_text.strip() or out.strip()).splitlines()
        last = diagnostic[-1] if diagnostic else f"exit status {proc.returncode}"
        detail = err_text.strip() or out.strip() or None
        raise ScriptError(f"{argv[0]} failed: {last}", detail=detail)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err_text)


def run_interactive(
    cmd: Sequence[str | Path], *, timeout: float | None = None, mutates: bool = False
) -> None:
    """Run a command that needs the terminal (sudo password, package-manager prompts).

    Do not wrap this in step(): the spinner would fight the prompt.
    """
    if not cmd:
        raise ScriptError("empty command sequence")
    argv = [str(c) for c in cmd]
    timeout = DEFAULT_TIMEOUT if timeout is None else timeout
    if mutates and DRY_RUN:
        emit(f"{_paint('33', 'dry-run')} {shlex.join(argv)}")
        return
    require(argv[0])
    try:
        status = subprocess.run(argv, timeout=timeout, check=False).returncode
    except subprocess.TimeoutExpired:
        raise ScriptError(
            f"timed out after {timeout:g}s: {shlex.join(argv)}",
            code=EXIT_TIMEOUT,
            hint="raise the limit with --timeout",
        ) from None
    except OSError as err:
        raise ScriptError(f"cannot run {argv[0]}: {err.strerror}") from err
    if status != 0:
        raise ScriptError(f"{argv[0]} exited with status {status}")


# --- safe file handling ----------------------------------------------------


@contextlib.contextmanager
def single_instance(name: str = PROG) -> Iterator[None]:
    """Refuse to run twice at once. The lock dies with the process, even on a crash."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(tempfile.gettempdir())
    lock_name = f"{name}.lock" if runtime else f"{name}-{os.getuid()}.lock"
    fd = os.open(base / lock_name, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ScriptError("already running", code=EXIT_LOCKED) from None
        yield
    finally:
        os.close(fd)


def write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    """Write via a temp file + rename, so readers never see a half-written file."""
    if DRY_RUN:
        emit(f"{_paint('33', 'dry-run')} write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


# --- the job ---------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO_ROOT / "AGENTS.md"
DEFAULT_LIST = REPO_ROOT / "harness-list.md"
LUA_MARKER = "content = [["
MODES = ("md", "lua")


def expand_base(raw: str) -> str:
    """Expand the forms harness-list.md uses: ~, $HOME and its XDG default."""
    home = str(Path.home())
    xdg = os.environ.get("XDG_CONFIG_HOME") or f"{home}/.config"
    value = raw.replace("${XDG_CONFIG_HOME:-$HOME/.config}", xdg)
    value = value.replace("$HOME", home)
    if value.startswith("~"):
        value = home + value[1:]
    if "$" in value:
        raise ScriptError(
            f"unsupported variable in base_dir: {raw}",
            hint="only ~, $HOME and ${XDG_CONFIG_HOME:-$HOME/.config} are understood",
        )
    return value


def parse_harness_list(text: str, origin: Path) -> list[tuple[str, Path, str]]:
    """Parse the markdown table: harness | base_dir | rules file | mode."""
    targets: list[tuple[str, Path, str]] = []
    problems: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.rstrip("\r").strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        label = cells[0]
        if label == "harness" or (label and set(label) <= {"-", ":"}):
            continue  # header row or --- separator
        if not any(cells):
            continue
        if len(cells) < 4 or not all(cells[:4]):
            problems.append(f"line {lineno}: need 4 filled columns: {line}")
            continue
        base, filename, mode = cells[1], cells[2], cells[3]
        if mode not in MODES:
            problems.append(f"line {lineno}: unknown mode {mode!r} (want md or lua)")
            continue
        try:
            target = Path(expand_base(base)) / filename
        except ScriptError as err:
            problems.append(f"line {lineno}: {err}")
            continue
        targets.append((label, target, mode))
    if problems:
        raise ScriptError(
            f"{len(problems)} bad row(s) in {origin}: " + "; ".join(problems[:3]),
            hint="each data row needs harness | base_dir | rules file | mode",
        )
    if not targets:
        raise ScriptError(f"no sync targets parsed from {origin}")
    return targets


def render_lua(path: Path, current: str, rules: str) -> str:
    """Embed rules in the existing [[...]] prompt string, keeping the Lua wrapper."""
    lines = current.splitlines(keepends=True)
    open_idx = next((i for i, ln in enumerate(lines) if LUA_MARKER in ln), None)
    close_idx = None
    if open_idx is not None:
        close_idx = next(
            (
                i
                for i, ln in enumerate(lines)
                if i > open_idx and ln.lstrip().startswith("]]")
            ),
            None,
        )
    if open_idx is None or close_idx is None:
        raise ScriptError(
            f"no '{LUA_MARKER} ... ]]' prompt block in {path}",
            hint="restore the maki wrapper (maki.api.register_prompt_hint) and rerun",
        )
    cut = lines[open_idx].index(LUA_MARKER) + len(LUA_MARKER)
    head = "".join(lines[:open_idx]) + lines[open_idx][:cut]
    return head + rules + "".join(lines[close_idx:])


def work(args: argparse.Namespace) -> None:
    """Sync the source rules into every harness rules file the map lists."""
    source = Path(args.source)
    with step(f"Read rules from {source}"):
        if not source.is_file():
            raise ScriptError(
                f"source missing: {source}",
                hint="pass the rules file as an argument, or restore AGENTS.md",
            )
        rules = source.read_text(encoding="utf-8")
        if not rules.strip():
            raise ScriptError(f"source is empty: {source}")
        if "]]" in rules:
            raise ScriptError(
                "source contains ']]', which would close maki's Lua string early",
                hint="remove the ']]' sequence from the rules text",
            )
        if not rules.endswith("\n"):
            rules += "\n"

    list_path = Path(args.harness_list)
    with step(f"Parse harness map {list_path}"):
        if not list_path.is_file():
            raise ScriptError(f"harness map missing or unreadable: {list_path}")
        targets = parse_harness_list(list_path.read_text(encoding="utf-8"), list_path)

    results: list[tuple[str, str, str]] = []
    diffs: list[str] = []
    failed: list[str] = []
    for label, path, mode in targets:
        name = f"{label} ({path})"
        if not path.is_file():
            emit(f"{BAD} Sync {name}: no such file")
            results.append((label, "missing", str(path)))
            failed.append(label)
            continue
        current = path.read_text(encoding="utf-8")
        expected = rules if mode == "md" else render_lua(path, current, rules)
        changed = current != expected
        with step(f"Sync {name}"):
            if changed:
                backup = path.with_name(path.name + ".bak")
                if not DRY_RUN and not backup.exists():
                    write_atomic(backup, current)
                write_atomic(path, expected)
                if not DRY_RUN and path.read_text(encoding="utf-8") != expected:
                    raise ScriptError(
                        f"verification failed: {path} does not match after write"
                    )
        if changed and DRY_RUN:
            diffs.append(
                "".join(
                    difflib.unified_diff(
                        current.splitlines(keepends=True),
                        expected.splitlines(keepends=True),
                        fromfile=str(path),
                        tofile=f"{path} (synced)",
                    )
                )
            )
        state = ("would update" if DRY_RUN else "updated") if changed else "up to date"
        results.append((label, state, str(path)))

    if results:
        width = max(len(label) for label, _, _ in results)
        for label, state, location in results:
            print(f"{label:<{width}}  {state:<12}  {location}")
    if diffs:
        print()
    for diff in diffs:
        print(diff)
    updated = sum(1 for _, state, _ in results if state == "updated")
    pending = sum(1 for _, state, _ in results if state == "would update")
    same = sum(1 for _, state, _ in results if state == "up to date")
    counts = []
    if updated:
        counts.append(f"{updated} updated")
    if pending:
        counts.append(f"{pending} would be updated")
    if same:
        counts.append(f"{same} up to date")
    if counts:
        print(", ".join(counts))

    if failed:
        raise ScriptError(
            f"{len(failed)} target(s) failed: {', '.join(failed)}",
            hint="fix the path in the harness map or install the harness",
        )


# --- entry point -----------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=PROG, description=(__doc__ or "").splitlines()[0]
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true", help="show changes without making them"
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SEC",
        help=f"per-command time limit in seconds (default {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show command output and tracebacks",
    )
    parser.add_argument(
        "source",
        nargs="?",
        default=str(DEFAULT_SOURCE),
        metavar="SOURCE",
        help="rules file to propagate (default: the repo AGENTS.md)",
    )
    parser.add_argument(
        "--harness-list",
        default=str(DEFAULT_LIST),
        metavar="PATH",
        help="markdown map of harness rules files (default: the repo harness-list.md)",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    return args


def _on_sigterm(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    global DRY_RUN, VERBOSE, DEFAULT_TIMEOUT
    args = parse_args(argv)
    DRY_RUN, VERBOSE, DEFAULT_TIMEOUT = args.dry_run, args.verbose, args.timeout
    signal.signal(signal.SIGTERM, _on_sigterm)
    if DRY_RUN:
        emit(f"{_paint('33', 'dry-run')} nothing will be changed")
    try:
        with single_instance():
            work(args)
    except ScriptError as err:
        emit(f"{_paint('31', 'error:')} {err}")
        if err.hint:
            emit(f"{_paint('2', 'hint:')}  {err.hint}")
        if VERBOSE and err.detail:
            emit(err.detail)
        return err.code
    except KeyboardInterrupt:
        emit(_paint("33", "interrupted"))
        return EXIT_INTERRUPT
    except BrokenPipeError:  # e.g. `NAME | head`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except Exception as err:  # a bug, not an expected failure
        emit(f"{_paint('31', 'error:')} unexpected {type(err).__name__}: {err}")
        emit(
            traceback.format_exc().rstrip()
            if VERBOSE
            else f"{_paint('2', 'hint:')}  rerun with -v for the traceback"
        )
        return EXIT_FAIL
    return 0


if __name__ == "__main__":
    sys.exit(main())
