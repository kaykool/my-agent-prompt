#!/usr/bin/env python3
"""NAME: one-line purpose.

Status output (steps, spinner, errors) goes to stderr; results go to stdout,
so `NAME | other-tool` and `NAME > file` stay clean.

Exit codes: 0 ok, 1 failure, 2 bad usage, 75 already running,
124 timeout, 130 interrupted.
"""

from __future__ import annotations

import argparse
import contextlib
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


def work(args: argparse.Namespace) -> None:
    """Replace this body. Keep each unit of work inside step()."""
    with step("Read kernel version"):
        kernel = run(["uname", "-sr"]).stdout.strip()
    print(kernel)  # results go to stdout


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
