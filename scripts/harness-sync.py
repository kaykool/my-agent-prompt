#!/usr/bin/env python3
"""harness-sync: refresh AI harness model lists and update harness binaries.

Fetches the live model catalogs from the Nutaraline and GutsAI gateways and
rewrites the model sections of opencode, pi and pig configs (agy
auto-updates itself on start, so it is skipped). It also updates harness
binaries via each harness's own updater whenever an update is pending
(opencode via pacman, which may ask for your sudo password).

It speaks plainly: `Models are up to date.` or `Updated models in:
opencode (+34).`, plus one line for programs. Rerun with -v for the
full tables (versions, file paths, backup locations).

Status output (steps, spinner, errors) goes to stderr; results go to stdout,
so `harness-sync | other-tool` and `harness-sync > file` stay clean.

Exit codes: 0 ok, 1 failure, 2 bad usage, 75 already running,
124 timeout, 130 interrupted.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import fcntl
import itertools
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

PROG = Path(sys.argv[0]).stem or "script"
DEFAULT_TIMEOUT = 60.0  # seconds per command; overridden by --timeout
# Binary upgrades download and unpack releases, and `pacman -S` can run for
# minutes. They get their own budget: killing an updater at --timeout could
# leave pacman part-way through a transaction.
UPDATE_TIMEOUT = 900.0

EXIT_FAIL, EXIT_USAGE, EXIT_LOCKED, EXIT_TIMEOUT, EXIT_INTERRUPT = 1, 2, 75, 124, 130

DRY_RUN = False
VERBOSE = False

HOME = Path.home()
DEFAULT_CATALOG = HOME / "1DEV/notes/my-agent-setup/catalog.md"
BACKUP_ROOT = HOME / ".local/state/harness-sync/backups"
USER_AGENT = "curl/8.0"  # the gateways 403 the default python-urllib User-Agent

REASONING_LEVELS = ["minimal", "low", "medium", "high", "xhigh", "max"]
GUTSAI_DEFAULT_CONTEXT = 1_000_000  # matches the values hand-written in models.json
GUTSAI_DEFAULT_OUTPUT = 32_000

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


# OSC, CSI and other escape sequences, plus stray C0 controls. Newline and tab
# survive so multi-line output still reads properly.
_CONTROL = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[@-Z\\-_]"
    r"|[\x00-\x08\x0b-\x1f\x7f]"
)


def _safe(text: str) -> str:
    """Strip terminal control sequences from output this script did not write.

    Harness updaters run third-party code and their output reaches this
    terminal. A release note carrying an OSC sequence could otherwise
    retitle the window or push text onto the clipboard.
    """
    return _CONTROL.sub("", text)


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


# --- gateways and normalized catalog models ----------------------------------


@dataclass
class Gateway:
    id: str
    base_url: str
    api_key_env: str
    flavor: str  # key into NORMALIZERS


GATEWAYS = [
    Gateway("nutaraline", "https://nutaraline.co.uk/v1", "NUTARALINE_API_KEY", "nutaraline"),
    Gateway("gutsai", "https://api.gutsai.id/v1", "GUTSAI_API_KEY", "gutsai"),
]


@dataclass
class Model:
    id: str
    name: str
    context: int | None = None
    output: int | None = None
    reasoning: bool = False
    tools: bool = False
    inputs: list[str] = field(default_factory=lambda: ["text"])
    efforts: list[str] = field(default_factory=list)


def _nested(obj: dict, *path: str) -> object:
    for key in path:
        if not isinstance(obj, dict) or key not in obj:
            return None
        obj = obj[key]
    return obj


def _pretty_name(model_id: str) -> str:
    """GutsAI models carry no display name; derive one from the id."""
    upper_words = {"gpt", "glm", "llm", "ai", "mcp"}
    parts = []
    for token in model_id.split("-"):
        if token[:1].isdigit():
            parts.append(token)
        elif token in upper_words:
            parts.append(token.upper())
        else:
            parts.append(token[:1].upper() + token[1:])
    return " ".join(parts) or model_id


def _normalize_nutaraline(raw: list[dict]) -> list[Model]:
    models = []
    for m in raw:
        caps = m.get("caps") or {}
        raw_efforts = _nested(m, "reasoning", "supported_efforts")
        efforts = list(raw_efforts) if isinstance(raw_efforts, list) else []
        modal = (m.get("architecture") or {}).get("input_modalities") or ["text"]
        context = caps.get("contextWindow") or _nested(m, "top_provider", "context_length")
        output = caps.get("maxOutput") or _nested(m, "top_provider", "max_completion_tokens")
        models.append(
            Model(
                id=m["id"],
                name=m.get("name") or m["id"],
                context=context if isinstance(context, int) else None,
                output=output if isinstance(output, int) else None,
                reasoning=bool(caps.get("reasoning")),
                tools=bool(caps.get("tools", True)),
                inputs=[x for x in modal if x in ("text", "image")] or ["text"],
                efforts=[str(e) for e in efforts],
            )
        )
    return models


def _normalize_gutsai(raw: list[dict]) -> list[Model]:
    models = []
    for m in raw:
        if m.get("status") != "ok":
            continue  # offline entries 404 on request; excluded everywhere
        modal = m.get("input_modalities") or ["text"]
        models.append(
            Model(
                id=m["id"],
                name=_pretty_name(m["id"]),
                context=None,  # gutsai does not report context windows
                output=None,
                reasoning=bool(m.get("supports_reasoning")),
                tools=True,  # gutsai does not report tool support; configs assume true
                inputs=[x for x in modal if x in ("text", "image")] or ["text"],
                efforts=[],
            )
        )
    return models


NORMALIZERS = {"nutaraline": _normalize_nutaraline, "gutsai": _normalize_gutsai}


def _http_json(url: str, timeout: float, desc: str, api_key: str | None = None) -> object:
    """Download a JSON document with a hard timeout. Failures raise ScriptError."""
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        if err.code >= 500:
            hint = "the gateway itself is failing, nothing to fix on your machine"
        elif err.code in (401, 403):
            hint = "its API key looks rejected; check ~/.config/agent-env.sh"
        else:
            hint = "rerun with -v to see the response body"
        raise ScriptError(
            f"could not download {desc}: HTTP {err.code}", hint=hint,
            detail=f"{url} answered HTTP {err.code}",
        ) from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise ScriptError(
            f"could not download {desc}",
            hint="check the internet connection and rerun",
            detail=str(err),
        ) from err


def _fetch_models(gateway: Gateway, timeout: float) -> list[Model]:
    key = os.environ.get(gateway.api_key_env)
    if not key:
        raise ScriptError(
            f"{gateway.api_key_env} is not set",
            hint="add it to ~/.config/agent-env.sh and rerun",
        )
    payload = _http_json(f"{gateway.base_url}/models", timeout, f"{gateway.id} model list", api_key=key)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ScriptError(
            f"{gateway.id} sent something unexpected",
            detail='expected a JSON document with a "data" list',
        )
    return NORMALIZERS[gateway.flavor](payload["data"])


# --- merge helpers -----------------------------------------------------------


def _kept(existing: dict, known: set[str]) -> dict:
    """Fields we do not manage (e.g. pi promptCache) survive regeneration."""
    return {k: v for k, v in existing.items() if k not in known}


def _thinking_map(model: Model, existing: dict | None) -> dict:
    """Map harness reasoning levels onto the efforts the gateway supports."""
    if len(model.efforts) == 1:
        return {lvl: model.efforts[0] for lvl in REASONING_LEVELS}
    if model.efforts:
        return {e: e for e in model.efforts}
    if existing and existing.get("thinkingLevelMap"):
        return existing["thinkingLevelMap"]
    return {"xhigh": "xhigh", "max": "max"}  # gutsai convention used in models.json


@dataclass
class Plan:
    harness: str
    path: Path
    fmt: str
    new_text: str
    old_text: str
    added: int
    removed: int
    note: str = ""
    added_ids: list[str] = field(default_factory=list)
    removed_ids: list[str] = field(default_factory=list)


# --- provider defaults for blocks missing from a config file -------------------

_OPENCODE_NPM = "@ai-sdk/openai-compatible"  # SDK package for OpenAI-compatible gateways


def _new_opencode_provider(prov_id: str) -> dict:
    base = {
        "nutaraline": "https://nutaraline.co.uk/v1",
        "gutsai": "https://api.gutsai.id/v1",
    }[prov_id]
    names = {"nutaraline": "Nutaraline", "gutsai": "GutsAI"}
    return {
        "name": names[prov_id],
        "env": [f"{prov_id.upper()}_API_KEY"],
        "settings": {"baseURL": base},
        "models": {},
        "npm": _OPENCODE_NPM,
    }


def _new_pi_provider(prov_id: str) -> dict:
    meta = {
        "nutaraline": ("Nutaraline", "https://nutaraline.co.uk/v1", "${NUTARALINE_API_KEY}"),
        "gutsai": ("GutsAI", "https://api.gutsai.id/v1", "${GUTSAI_API_KEY}"),
    }[prov_id]
    return {
        "name": meta[0],
        "baseUrl": meta[1],
        "apiKey": meta[2],
        "api": "openai-completions",
        "models": [],
    }


# --- jsonc (opencode) ----------------------------------------------------------


# Model keys the script manages. v1-only "capabilities" and "package" are
# dropped; v2 uses modalities/tool_call/reasoning and "npm".
_OPENCODE_KNOWN = {
    "name",
    "limit",
    "variants",
    "modalities",
    "tool_call",
    "reasoning",
    "options",
    "capabilities",
    "package",
}


def _drop_trailing_comma(out: list[str]) -> None:
    """Remove the last non-space element if it is a comma (JSONC allows trailing)."""
    j = len(out) - 1
    while j >= 0 and out[j] in " \t\r\n":
        j -= 1
    if j >= 0 and out[j] == ",":
        del out[j]


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas outside strings."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            if nl == -1:
                break
            i = nl
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            if c in "}]":
                _drop_trailing_comma(out)
            out.append(c)
            i += 1
    return "".join(out)


def _opencode_model(model: Model, existing: dict) -> dict:
    """opencode v2 model entry: variants stay a list of {id, settings} objects
    (opencode v2.0.27 drops the whole provider given the object form)."""
    entry: dict = {"name": model.name}
    limit = existing.get("limit") or {}
    ctx = model.context or limit.get("context")
    out = model.output or limit.get("output")
    if ctx or out:
        entry["limit"] = {k: v for k, v in (("context", ctx), ("output", out)) if v}
    if model.efforts:
        entry["variants"] = [
            {"id": e, "settings": {"reasoningEffort": e}} for e in model.efforts
        ]
    entry["reasoning"] = model.reasoning
    entry["tool_call"] = model.tools
    entry["modalities"] = {"input": model.inputs, "output": ["text"]}
    entry.update(_kept(existing, _OPENCODE_KNOWN))
    return entry


def _opencode_update(
    data: dict, catalog: dict[str, list[Model]]
) -> tuple[dict, int, int, list[str], list[str], list[str]]:
    providers = data.setdefault("providers", {})
    added = removed = 0
    created: list[str] = []
    added_ids: list[str] = []
    removed_ids: list[str] = []
    for prov_id, models in catalog.items():
        block = providers.get(prov_id)
        if block is None:
            block = _new_opencode_provider(prov_id)
            providers[prov_id] = block
            created.append(prov_id)
        entries = block.setdefault("models", {})
        old_ids = set(entries)
        want_ids = {m.id for m in models}
        for m in models:
            entries[m.id] = _opencode_model(m, entries.get(m.id, {}))
        for stale in sorted(old_ids - want_ids):
            del entries[stale]
        added += len(want_ids - old_ids)
        removed += len(old_ids - want_ids)
        added_ids.extend(f"{prov_id}/{m}" for m in sorted(want_ids - old_ids))
        removed_ids.extend(f"{prov_id}/{m}" for m in sorted(old_ids - want_ids))
    return data, added, removed, created, added_ids, removed_ids


def _serialize_json(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# --- pi-models (pi, pig) -------------------------------------------------------

_PI_KNOWN = {"id", "name", "contextWindow", "maxTokens", "reasoning", "input", "thinkingLevelMap"}


def _pi_model(model: Model, existing: dict) -> dict:
    entry: dict = {
        "id": model.id,
        "name": model.name,
        "contextWindow": model.context or existing.get("contextWindow") or GUTSAI_DEFAULT_CONTEXT,
        "maxTokens": model.output or existing.get("maxTokens") or GUTSAI_DEFAULT_OUTPUT,
        "reasoning": model.reasoning,
        "input": model.inputs,
    }
    if model.reasoning:
        entry["thinkingLevelMap"] = _thinking_map(model, existing)
    entry.update(_kept(existing, _PI_KNOWN))
    return entry


def _pi_update(
    data: dict, catalog: dict[str, list[Model]]
) -> tuple[dict, int, int, list[str], list[str], list[str]]:
    providers = data.setdefault("providers", {})
    added = removed = 0
    created: list[str] = []
    added_ids: list[str] = []
    removed_ids: list[str] = []
    for prov_id, models in catalog.items():
        block = providers.get(prov_id)
        if block is None:
            block = _new_pi_provider(prov_id)
            providers[prov_id] = block
            created.append(prov_id)
        entries = block.get("models") or []
        old_by_id = {e.get("id"): e for e in entries}
        want = {m.id: m for m in models}
        block["models"] = [_pi_model(m, old_by_id.get(m.id, {})) for m in models]
        added += len(want.keys() - old_by_id.keys())
        removed += len(old_by_id.keys() - want.keys())
        added_ids.extend(f"{prov_id}/{m}" for m in sorted(want.keys() - old_by_id.keys()))
        removed_ids.extend(f"{prov_id}/{m}" for m in sorted(old_by_id.keys() - want.keys()))
    return data, added, removed, created, added_ids, removed_ids


def _model_ref_alive(ref: str, avail: dict[str, set[str]]) -> bool:
    """Check an enabledModels entry of the form "provider/id"."""
    if "/" not in ref:
        return False
    prov, mid = ref.split("/", 1)
    return mid in avail.get(prov, set())


def _fix_pi_settings(harness: str, settings_text: str, models_data: dict) -> tuple[str | None, list[str]]:
    """Prune outdated enabledModels and repair a dangling defaultModel.

    Returns (new_text or None when unchanged, notes).
    """
    try:
        settings = json.loads(settings_text)
    except json.JSONDecodeError as err:
        raise ScriptError(f"{harness} settings file does not parse: {err}") from err
    providers = models_data.get("providers") or {}
    order: dict[str, list[str]] = {}
    for prov_id, block in providers.items():
        order[prov_id] = [m.get("id") for m in (block.get("models") or []) if m.get("id")]
    avail = {prov: set(ids) for prov, ids in order.items()}
    notes: list[str] = []
    changed = False

    enabled = settings.get("enabledModels")
    if isinstance(enabled, list):
        kept = [m for m in enabled if isinstance(m, str) and _model_ref_alive(m, avail)]
        dropped = [m for m in enabled if m not in kept]
        if dropped:
            settings["enabledModels"] = kept
            changed = True
            notes.append(f"removed {len(dropped)} outdated models: {', '.join(str(m) for m in dropped)}")

    prov = settings.get("defaultProvider")
    model = settings.get("defaultModel")
    if isinstance(prov, str) and isinstance(model, str):
        mid = model.split("/", 1)[1] if "/" in model else model
        if mid not in avail.get(prov, set()):
            ids = order.get(prov, [])
            if not ids:
                fallback_prov = next((p for p, lst in order.items() if lst), None)
                if fallback_prov is None:
                    raise ScriptError("no models left to choose a default from")
                settings["defaultProvider"] = fallback_prov
                settings["defaultModel"] = order[fallback_prov][0]
                notes.append(
                    f"default {prov}/{model} is gone; "
                    f"switched to {fallback_prov}/{order[fallback_prov][0]}"
                )
            else:
                settings["defaultModel"] = ids[0]
                notes.append(f"default {prov}/{model} is gone; switched to {prov}/{ids[0]}")
            changed = True

    if not changed:
        return None, []
    return _serialize_json(settings), notes


# --- targets from catalog.md -----------------------------------------------------


_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*(?:\|\s*)$")


def _expand_path(raw: str) -> Path:
    text = raw.strip().strip('"')

    def sub(m: re.Match) -> str:
        return os.environ.get(m.group(1)) or os.path.expandvars(m.group(2))

    text = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}", sub, text)
    return Path(os.path.expandvars(text)).expanduser()


def _read_targets(catalog_md: Path) -> list[tuple[str, Path, str]]:
    targets: list[tuple[str, Path, str]] = []
    for line in catalog_md.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _ROW_RE.match(line)
        if not m:
            continue
        harness, path_raw, fmt = (g.strip() for g in m.groups())
        if harness in ("harness",) or harness.startswith("-"):
            continue
        target = (harness, _expand_path(path_raw), fmt)
        if target not in targets:
            targets.append(target)
    return targets


# --- binary version checks -------------------------------------------------------


@dataclass
class BinaryStatus:
    harness: str
    current: str
    latest: str  # "" when the latest version cannot be checked
    update: bool  # True when an update is available
    note: str = ""


def _opencode_status(timeout: float) -> BinaryStatus:
    out = run(["pacman", "-Q", "opencode"], timeout=timeout, check=False)
    current = out.stdout.strip().split()[-1] if out.returncode == 0 else "not installed"
    pending = run(["pacman", "-Qu", "opencode"], timeout=timeout, check=False)
    lines = [ln for ln in pending.stdout.splitlines() if ln.startswith("opencode ")]
    if lines:
        return BinaryStatus("opencode", current, lines[0].split()[-1], True)
    return BinaryStatus("opencode", current, current, False, "no pending pacman upgrade")


def _pi_status(timeout: float) -> BinaryStatus:
    binary = shutil.which("pi")
    if binary is None:
        return BinaryStatus("pi", "not installed", "", False, "pi not on PATH")
    out = run([binary, "--version"], timeout=timeout, check=False)
    current = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else "unknown"
    try:
        payload = _http_json(
            "https://registry.npmjs.org/@earendil-works%2fpi-coding-agent/latest",
            timeout,
            "pi version info",
        )
        latest = payload.get("version") if isinstance(payload, dict) else None
    except ScriptError as err:
        return BinaryStatus("pi", current, "", False, f"{err}")
    if not latest:
        return BinaryStatus("pi", current, "", False, "npm gave no version")
    return BinaryStatus("pi", current, str(latest), str(latest) != current)


def _pig_status(timeout: float) -> BinaryStatus:
    receipt = HOME / ".pig/install-receipt"
    current = "unknown"
    if receipt.is_file():
        for line in receipt.read_text(encoding="utf-8").splitlines():
            if line.startswith("pig-version="):
                current = line.split("=", 1)[1].strip()
    binary = shutil.which("pig")
    if binary is None and current == "unknown":
        return BinaryStatus("pig", "not installed", "", False, "pig not on PATH")
    try:
        payload = _http_json(
            "https://github.com/MichaelKinsy/PiG/releases/latest/download/update.json",
            timeout,
            "pig version info",
        )
    except ScriptError as err:
        return BinaryStatus("pig", current, "", False, f"{err}")
    latest: str | None = None
    if isinstance(payload, dict):
        for key in ("version", "tag", "latest"):
            if payload.get(key):
                latest = str(payload[key]).lstrip("v")
                break
        if latest is None:
            binaries = payload.get("binaries") or {}
            entry = binaries.get("linux/amd64") or {}
            url = str(entry.get("url") or "")
            m = re.search(r"/v?(\d+\.\d+\.\d+)/", url) or re.search(
                r"pig-(\d+\.\d+\.\d+)-", url
            )
            if m:
                latest = m.group(1)
    if latest is None:
        return BinaryStatus("pig", current, "", False, "release feed gave no version")
    return BinaryStatus("pig", current, latest, latest != current)


def _check_binaries(timeout: float) -> list[BinaryStatus]:
    """Collect versions. Per-harness failures become notes, never aborts."""
    statuses: list[BinaryStatus] = []
    for name, func in (
        ("opencode", _opencode_status),
        ("pi", _pi_status),
        ("pig", _pig_status),
    ):
        try:
            statuses.append(func(timeout))
        except (ScriptError, OSError, IndexError, ValueError) as err:
            statuses.append(BinaryStatus(name, "unknown", "", False, f"check failed: {err}"))
    return statuses


def _read_version(harness: str, timeout: float) -> str:
    """Best-effort current version string; '' when it cannot be read."""
    try:
        if harness == "opencode":
            out = run(["pacman", "-Q", "opencode"], timeout=timeout, check=False)
            return out.stdout.strip().split()[-1] if out.returncode == 0 else ""
        binary = shutil.which(harness)
        if binary is None:
            return ""
        if harness == "pig":
            out = run([binary, "version"], timeout=timeout, check=False)
        else:
            out = run([binary, "--version"], timeout=timeout, check=False)
        # stdout only: a stderr fallback would treat an error message as a
        # version, and the per-harness parsers would scrape a number out of it.
        text = out.stdout.strip().splitlines()
        if not text:
            return ""
        if harness == "pig":
            return text[0].split(":")[-1].strip()  # first line is "pig: 0.4.1"
        return text[0].strip()  # pi prints the bare version
    except (ScriptError, OSError, IndexError, ValueError):
        return ""


def _snapper_presnapshot(timeout: float) -> None:
    """Best-effort btrfs pre-snapshot before the pacman upgrade."""
    if shutil.which("snapper") is None:
        emit("note: snapper is not installed; skipping the safety snapshot")
        return
    configs = run(["snapper", "list-configs"], timeout=timeout, check=False)
    if configs.returncode != 0 or "root" not in configs.stdout:
        emit("note: no snapper backup set up; skipping the safety snapshot")
        return
    result = run(
        ["snapper", "create", "-t", "pre", "-c", "number", "-d", "Pre-harness-sync snapshot"],
        timeout=timeout,
        check=False,
        mutates=True,
    )
    if result.returncode != 0:
        emit("note: safety snapshot failed; continuing anyway")
    else:
        emit("safety snapshot taken")


def _upgrade_binaries(statuses: list[BinaryStatus], timeout: float) -> dict[str, str]:
    """Run each harness's own updater interactively. Must be called outside step().

    Returns {harness: outcome}. Updaters run only when an update is pending.
    """
    outcomes: dict[str, str] = {}
    for st in statuses:
        if st.current in ("not installed", "unknown"):
            outcomes[st.harness] = f"skipped ({st.note or st.current})"
            continue
        if not st.update:
            outcomes[st.harness] = "current"
            continue
        try:
            before = _read_version(st.harness, timeout)
            if st.harness == "opencode":
                _snapper_presnapshot(timeout)
                run_interactive(
                    ["sudo", "pacman", "-S", "--needed", "opencode"],
                    timeout=UPDATE_TIMEOUT,
                    mutates=True,
                )
            elif st.harness == "pi":
                binary = shutil.which("pi") or "pi"
                run_interactive(
                    [binary, "update", "--self"], timeout=UPDATE_TIMEOUT, mutates=True
                )
            elif st.harness == "pig":
                binary = shutil.which("pig") or "pig"
                run_interactive([binary, "update"], timeout=UPDATE_TIMEOUT, mutates=True)
            after = _read_version(st.harness, timeout)
            if DRY_RUN:
                outcomes[st.harness] = "dry-run"
            elif not after:
                # It ran, but now it cannot report a version. An updater that
                # breaks its own binary must not read as a successful update.
                outcomes[st.harness] = f"FAILED: no version after upgrading ({st.harness} looks broken)"
            elif before and before != after:
                outcomes[st.harness] = f"updated to {after}"
            elif before:
                outcomes[st.harness] = "current"
            else:
                outcomes[st.harness] = f"updated (version now {after})"
        except ScriptError as err:
            outcomes[st.harness] = f"FAILED: {err}"
    return outcomes


# --- planning --------------------------------------------------------------------


def _config_parse_error(harness: str, path: Path, old_text: str, err: Exception) -> ScriptError:
    """Turn a parse failure into advice: what is wrong and where a copy lives."""
    backup = f"{BACKUP_ROOT} (named {harness}-{path.name})"
    if not old_text.strip():
        return ScriptError(
            f"{harness} settings file is empty: {path}",
            hint=f"restore a copy from {backup}",
        )
    return ScriptError(
        f"{harness} settings file does not parse: {err}",
        hint=f"fix the file by hand, or restore a copy from {backup}",
    )


def _plan_target(
    harness: str, path: Path, fmt: str, catalog: dict[str, list[Model]]
) -> list[Plan]:
    """Build write plans for one catalog row (models file plus settings fixes)."""
    old_text = path.read_text(encoding="utf-8")
    plans: list[Plan] = []
    if fmt == "opencode-jsonc":
        try:
            data = json.loads(strip_jsonc(old_text))
        except json.JSONDecodeError as err:
            raise _config_parse_error(harness, path, old_text, err) from err
        new_data, added, removed, created, added_ids, removed_ids = _opencode_update(data, catalog)
        for prov_id in created:
            emit(f"note: {prov_id} was not in {path.name}; added it")
        plans.append(
            Plan(harness, path, fmt, _serialize_json(new_data), old_text,
                 added, removed, added_ids=added_ids, removed_ids=removed_ids)
        )
    elif fmt == "pi-models":
        try:
            data = json.loads(old_text)
        except json.JSONDecodeError as err:
            raise _config_parse_error(harness, path, old_text, err) from err
        new_data, added, removed, created, added_ids, removed_ids = _pi_update(data, catalog)
        for prov_id in created:
            emit(f"note: {prov_id} was not in {path.name}; added it")
        plans.append(
            Plan(harness, path, fmt, _serialize_json(new_data), old_text,
                 added, removed, added_ids=added_ids, removed_ids=removed_ids)
        )
        settings_path = path.parent / "settings.json"
        if settings_path.is_file():
            fixed, notes = _fix_pi_settings(
                harness, settings_path.read_text(encoding="utf-8"), new_data
            )
            for note in notes:
                emit(f"note: {harness} settings: {note}")
            if fixed is not None:
                old_settings = settings_path.read_text(encoding="utf-8")
                plans.append(
                    Plan(harness, settings_path, "pi-settings", fixed, old_settings, 0, 0)
                )
    else:
        raise ScriptError(
            f"{harness} has a settings type I don't know: {fmt}",
            hint="use one of: opencode-jsonc, pi-models",
        )
    return plans


def _short_label(plan: Plan) -> str:
    if plan.fmt == "pi-settings":
        return "settings fixed"
    bits = []
    if plan.added:
        bits.append(f"+{plan.added}")
    if plan.removed:
        bits.append(f"-{plan.removed}")
    return " ".join(bits) if bits else "refreshed"


# --- the job ---------------------------------------------------------------


def work(args: argparse.Namespace) -> None:
    """Replace this body. Keep each unit of work inside step()."""
    catalog_md = Path(args.catalog).expanduser()
    if not catalog_md.is_file():
        raise ScriptError(
            f"program list not found: {catalog_md}",
            hint="pass the right path with --catalog",
        )

    with step("Read the program list"):
        targets = _read_targets(catalog_md)
        if not targets:
            raise ScriptError(
                f"{catalog_md} lists no programs",
                hint="it should hold a table: harness | config file | format",
            )
        for harness, path, _fmt in targets:
            if not path.is_file():
                raise ScriptError(
                    f"{harness}: settings file not found: {path}",
                    hint=f"a previous copy may be in {BACKUP_ROOT} "
                    f"(named {harness}-{path.name})",
                )

    catalogs: dict[str, list[Model]] = {}
    down: list[str] = []
    first_error: ScriptError | None = None
    with step("Check the online model lists"):
        for gw in GATEWAYS:
            try:
                models = _fetch_models(gw, args.timeout)
            except ScriptError as err:
                down.append(gw.id)
                first_error = first_error or err
                msg = f"{err} - {err.hint}" if err.hint else f"{err}"
                emit(f"note: {msg}; {gw.id}'s models were left untouched")
                continue
            if not models:
                raise ScriptError(
                    f"{gw.id} sent an empty list; stopped so none of your files are emptied"
                )
            catalogs[gw.id] = models
        if not catalogs and first_error:
            raise first_error  # every gateway failed; report the first failure
    detail: list[str] = []
    counts = ", ".join(f"{gid} {len(models)}" for gid, models in catalogs.items())
    detail.append(f"{'catalog':<8}  {counts}")
    if down:
        detail.append(f"{'skipped':<8}  {', '.join(down)} (gateway unreachable; models left as-is)")

    with step("Compare online lists with your files"):
        plans: list[Plan] = []
        for harness, path, fmt in targets:
            plans.extend(_plan_target(harness, path, fmt, catalogs))

    with step("Check program versions"):
        statuses = _check_binaries(args.timeout)
    vw = max(len(st.current) for st in statuses)
    for st in statuses:
        if st.current in ("not installed", "unknown"):
            state = st.note or "missing"
        elif st.note and not st.latest and not st.update:
            state = "check failed"
        elif st.update and st.latest:
            state = f"-> {st.latest}"
        else:
            state = "current"
        detail.append(f"{st.harness:<8}  {st.current:<{vw}}  {state}")

    todo = [p for p in plans if p.new_text != p.old_text]
    changed = sorted({p.harness for p in todo})
    delta_bits = []
    if sum(p.added for p in todo):
        delta_bits.append(f"+{sum(p.added for p in todo)}")
    if sum(p.removed for p in todo):
        delta_bits.append(f"-{sum(p.removed for p in todo)}")
    delta = f" ({' '.join(delta_bits)})" if delta_bits else ""
    detail.append(f"{'files':<8}  {len(todo)}/{len(plans)} changed")
    for plan in todo:
        detail.append(f"{plan.harness:<8}  {_short_label(plan)}  {plan.path}")

    if not todo:
        print("Models are up to date.")
    elif DRY_RUN:
        print(f"Would update models in: {', '.join(changed)}{delta}.")
        print("Rerun without --dry-run to apply.")
        for plan in todo:
            for mid in plan.added_ids:
                print(f"  + {mid}")
            for mid in plan.removed_ids:
                print(f"  - {mid}")
        if args.diff:
            for plan in todo:
                diff = "".join(
                    difflib.unified_diff(
                        plan.old_text.splitlines(keepends=True),
                        plan.new_text.splitlines(keepends=True),
                        fromfile=f"{plan.path} (current)",
                        tofile=f"{plan.path} (new)",
                    )
                )
                print(diff, end="")
        print("Nothing was changed.")
    else:
        stamped = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup_dir = BACKUP_ROOT / stamped
        for plan in todo:
            with step(f"Save {plan.harness} settings"):
                backup_dir.mkdir(parents=True, exist_ok=True)
                # pi and pig share the file name models.json: prefix with the
                # harness so one backup cannot overwrite the other.
                shutil.copy2(plan.path, backup_dir / f"{plan.harness}-{plan.path.name}")
                write_atomic(plan.path, plan.new_text)
        for old in sorted(BACKUP_ROOT.iterdir())[:-10] if BACKUP_ROOT.is_dir() else []:
            shutil.rmtree(old, ignore_errors=True)
        detail.append(f"{'backups':<8}  {backup_dir}")
        print(f"Updated models in: {', '.join(changed)}{delta}.")
        for plan in todo:
            for mid in plan.added_ids:
                print(f"  + {mid}")
            for mid in plan.removed_ids:
                print(f"  - {mid}")

    # Interactive updaters need the terminal; never run them under step().
    outcomes = _upgrade_binaries(statuses, args.timeout)
    failed = {h: o for h, o in outcomes.items() if o.startswith("FAILED")}
    if failed:
        raise ScriptError(
            f"could not update: {', '.join(failed)}",
            detail="; ".join(f"{h}: {o}" for h, o in failed.items()),
        )
    updated = sorted(h for h, o in outcomes.items() if o.startswith("updated"))
    detail.append(f"{'upgrades':<8}  " + ", ".join(f"{h} {o}" for h, o in outcomes.items()))
    if DRY_RUN and any(o == "dry-run" for o in outcomes.values()):
        would = sorted(h for h, o in outcomes.items() if o == "dry-run")
        print(f"Would also update programs: {', '.join(would)}.")
    elif updated:
        print(f"Updated programs: {', '.join(updated)}.")
    else:
        print("Programs are up to date.")

    if VERBOSE:
        print("--- details ---")
        for line in detail:
            print(line)


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
        "--catalog",
        default=str(DEFAULT_CATALOG),
        metavar="PATH",
        help=f"catalog.md holding the harness target table (default {DEFAULT_CATALOG})",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="with --dry-run, show full unified diffs per file",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show full details, command output and tracebacks",
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
        emit(f"{_paint('33', 'preview')} nothing will change")
    try:
        with single_instance():
            work(args)
    except ScriptError as err:
        emit(f"{_paint('31', 'error:')} {err}")
        if err.hint:
            emit(f"{_paint('2', 'hint:')}  {err.hint}")
        if VERBOSE and err.detail:
            emit(_safe(err.detail))
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
