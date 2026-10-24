#!/usr/bin/env python3
"""rules-sync: propagate the canonical AGENTS.md rules to every AI harness.

The repo AGENTS.md is the source. harness-list.md maps each harness to its
base directory and rules file. Only files whose content actually changes are
written; the first write to an existing file leaves a one-time <file>.bak
copy. --dry-run previews the unified diffs and writes nothing.

Status output (notes, errors) goes to stderr; results go to stdout,
so `rules-sync | other-tool` and `rules-sync > file` stay clean.

Exit codes: 0 ok, 1 failure, 2 bad usage, 75 already running, 130 interrupted.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import fcntl
import os
import signal
import sys
import tempfile
import traceback
from collections.abc import Iterator, Sequence
from pathlib import Path

PROG = Path(sys.argv[0]).stem or "rules-sync"
EXIT_FAIL, EXIT_LOCKED, EXIT_INTERRUPT = 1, 75, 130

DRY_RUN = False
VERBOSE = False

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO_ROOT / "AGENTS.md"
DEFAULT_LIST = REPO_ROOT / "harness-list.md"

_TTY = sys.stderr.isatty() and os.environ.get("TERM") != "dumb"
_COLOR = _TTY and "NO_COLOR" not in os.environ


def _paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


OK = _paint("32", "✓" if _TTY else "[ok]")
BAD = _paint("31", "✗" if _TTY else "[fail]")


def emit(text: str = "") -> None:
    print(text, file=sys.stderr, flush=True)


@contextlib.contextmanager
def step(label: str) -> Iterator[list[str]]:
    """Emit one status line with outcome details when a step succeeds or fails."""
    detail: list[str] = []
    try:
        yield detail
    except BaseException:
        suffix = f": {', '.join(detail)}" if detail else ""
        emit(f"{BAD} {label}{suffix}")
        raise
    suffix = f": {', '.join(detail)}" if detail else ""
    emit(f"{OK} {label}{suffix}")


def _pretty(path: Path) -> str:
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


class ScriptError(Exception):
    """An expected failure: shown as a short message, never a traceback."""

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        code: int = EXIT_FAIL,
    ) -> None:
        super().__init__(message)
        self.hint, self.code = hint, code


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
        emit(f"{_paint('33', 'dry-run')} write {_pretty(path)}")
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


def expand_base(raw: str) -> str:
    """Expand the forms harness-list.md uses: ~, $HOME and its XDG default."""
    home = str(Path.home())
    xdg = os.environ.get("XDG_CONFIG_HOME") or f"{home}/.config"
    value = raw.replace("${XDG_CONFIG_HOME:-$HOME/.config}", xdg).replace("$HOME", home)
    if value.startswith("~"):
        value = home + value[1:]
    if "$" in value:
        raise ScriptError(
            f"unsupported variable in base_dir: {raw}",
            hint="only ~, $HOME and ${XDG_CONFIG_HOME:-$HOME/.config} are understood",
        )
    return value


def parse_harness_list(text: str, origin: Path) -> list[tuple[str, Path, Path]]:
    """Parse the markdown table: harness | base_dir | rules file."""
    targets: list[tuple[str, Path, Path]] = []
    problems: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.rstrip("\r").strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        label = cells[0]
        if label == "harness" or (label and set(label) <= {"-", ":"}) or not any(cells):
            continue
        if len(cells) < 3 or not all(cells[:3]):
            problems.append(f"line {lineno}: need 3 filled columns: {line}")
            continue
        base, filename = cells[1], cells[2]
        try:
            base_dir = Path(expand_base(base))
        except ScriptError as err:
            problems.append(f"line {lineno}: {err}")
            continue
        targets.append((label, base_dir, base_dir / filename))
    if problems:
        raise ScriptError(
            f"{len(problems)} bad row(s) in {origin}: " + "; ".join(problems[:3]),
            hint="each data row needs harness | base_dir | rules file",
        )
    if not targets:
        raise ScriptError(f"no sync targets parsed from {origin}")
    return targets


def work(args: argparse.Namespace) -> None:
    """Sync the source rules into every harness rules file the map lists."""
    source = Path(args.source)
    with step("Load rules") as info:
        if not source.is_file():
            raise ScriptError(
                f"source missing: {_pretty(source)}",
                hint="pass the rules file as an argument, or restore AGENTS.md",
            )
        rules = source.read_text(encoding="utf-8")
        if not rules.strip():
            raise ScriptError(
                f"source is empty: {_pretty(source)}",
                hint="restore AGENTS.md before syncing",
            )
        if not rules.endswith("\n"):
            rules += "\n"
        info.append(f"{_pretty(source)} ({len(rules.splitlines())} lines)")

    list_path = Path(args.harness_list)
    with step("Load harness map") as info:
        if not list_path.is_file():
            raise ScriptError(
                f"harness map missing or unreadable: {_pretty(list_path)}",
                hint="pass --harness-list PATH or restore harness-list.md",
            )
        targets = parse_harness_list(list_path.read_text(encoding="utf-8"), list_path)
        info.append(f"{len(targets)} target(s) from {_pretty(list_path)}")

    results: list[tuple[str, str, str]] = []
    diffs: list[str] = []
    failed: list[str] = []
    summary = ""
    try:
        with step("Sync harness rules") as info:
            for label, base_dir, path in targets:
                existed = path.is_file()
                if path.exists() and not existed:
                    emit(f"  {BAD} {label}: not a regular file ({_pretty(path)})")
                    results.append((label, "missing", _pretty(path)))
                    failed.append(label)
                    continue
                if not base_dir.is_dir():
                    emit(f"  {BAD} {label}: base directory missing ({_pretty(base_dir)})")
                    results.append((label, "missing", _pretty(path)))
                    failed.append(label)
                    continue
                current = path.read_text(encoding="utf-8") if existed else ""
                changed = current != rules
                if changed:
                    backup = path.with_name(path.name + ".bak")
                    if existed and not DRY_RUN and not backup.exists():
                        write_atomic(backup, current)
                    write_atomic(path, rules)
                    if not DRY_RUN and path.read_text(encoding="utf-8") != rules:
                        raise ScriptError(
                            f"verification failed: {_pretty(path)} does not match after write"
                        )
                if changed and DRY_RUN:
                    diffs.append(
                        "".join(
                            difflib.unified_diff(
                                current.splitlines(keepends=True),
                                rules.splitlines(keepends=True),
                                fromfile=_pretty(path),
                                tofile=f"{_pretty(path)} (synced)",
                            )
                        )
                    )
                if not changed:
                    state = "up to date"
                elif DRY_RUN:
                    state = "would update" if existed else "would create"
                else:
                    state = "updated" if existed else "created"
                results.append((label, state, _pretty(path)))

            same = sum(1 for _, s, _ in results if s == "up to date")
            if results and same == len(results):
                summary = f"{same}/{len(results)} up to date"
            else:
                counts = [
                    f"{sum(1 for _, s, _ in results if s == kind)} {kind}"
                    for kind in (
                        "created",
                        "updated",
                        "would create",
                        "would update",
                        "up to date",
                        "missing",
                    )
                    if any(s == kind for _, s, _ in results)
                ]
                summary = ", ".join(counts)
            if summary:
                info.append(summary)
            if failed:
                raise ScriptError(
                    f"{len(failed)} target(s) failed: {', '.join(failed)}",
                    hint="fix the path in harness-list.md or install the harness",
                )
    finally:
        if results:
            w_label = max(len(label) for label, _, _ in results)
            w_state = max(len(state) for _, state, _ in results)
            for label, state, location in results:
                print(f"{label:<{w_label}}  {state:<{w_state}}  {location}")
        if diffs:
            print()
        for diff in diffs:
            print(diff, end="" if diff.endswith("\n") else "\n")
        if summary:
            print(summary)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=PROG, description=(__doc__ or "").splitlines()[0]
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true", help="show changes without making them"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show tracebacks on unexpected errors"
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
    return parser.parse_args(argv)


def _on_sigterm(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    global DRY_RUN, VERBOSE
    args = parse_args(argv)
    DRY_RUN, VERBOSE = args.dry_run, args.verbose
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
        return err.code
    except KeyboardInterrupt:
        emit(_paint("33", "interrupted"))
        return EXIT_INTERRUPT
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except Exception as err:
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
