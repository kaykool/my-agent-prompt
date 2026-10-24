#!/usr/bin/env python3
"""Refresh the model lists in your agent configs from the Nutaraline and GutsAI gateways.

Downloads each gateway's live model list and rewrites the harness config files
listed in catalog.md (opencode jsonc, pi/pig models.json),
so you never have to ask an AI to update them by hand.

Rules:
- GutsAI entries with status != "ok" are omitted (they return 404 offline).
- Fields the gateways do not provide (e.g. GutsAI context windows) fall back
  to the values already in the config file; per-model fields the script does
  not manage (e.g. pi "promptCache") are preserved as-is.
- Provider blocks are updated in place; a provider absent from a config file
  is reported and skipped, never added silently.

Status output (steps, spinner, errors) goes to stderr; results go to stdout.

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
import shutil
import signal
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

PROG = Path(sys.argv[0]).stem or "refetch-catalog"
DEFAULT_TIMEOUT = 60.0  # seconds per external call; overridden by --timeout

EXIT_FAIL, EXIT_USAGE, EXIT_LOCKED, EXIT_TIMEOUT, EXIT_INTERRUPT = 1, 2, 75, 124, 130

DRY_RUN = False
VERBOSE = False

HOME = Path.home()
DEFAULT_CATALOG = HOME / "1DEV/notes/my-agent-setup/catalog.md"
BACKUP_ROOT = HOME / ".local/state/refetch-catalog/backups"
USER_AGENT = "curl/8.0"  # the gateways 403 the default python-urllib User-Agent

REASONING_LEVELS = ["minimal", "low", "medium", "high", "xhigh", "max"]
GUTSAI_DEFAULT_CONTEXT = 1_000_000  # matches the values hand-written in models.json
GUTSAI_DEFAULT_OUTPUT = 32_000


# --- output (from stable-scripts template) -----------------------------------

_TTY = sys.stderr.isatty() and os.environ.get("TERM") != "dumb"
_COLOR = _TTY and "NO_COLOR" not in os.environ
_LOCK = threading.Lock()


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
                sys.stderr.write(f"\r\033[K{_paint('36', frame)} {self.label} {secs:.0f}s")
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


@contextlib.contextmanager
def single_instance(name: str = PROG) -> Iterator[None]:
    """Refuse to run twice at once; the lock dies with the process, even on a crash."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(tempfile.gettempdir())
    lock_name = f"{name}.lock" if runtime else f"{name}-{os.getuid()}.lock"
    fd = os.open(base / lock_name, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ScriptError(
                "another refetch-catalog is already running", code=EXIT_LOCKED
            ) from None
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


# --- gateways and normalized catalog models -----------------------------------

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
    vision: bool = False
    tools: bool = False
    inputs: list[str] = field(default_factory=lambda: ["text"])
    efforts: list[str] = field(default_factory=list)


def _nested(obj: dict, *path):
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
        efforts = list(_nested(m, "reasoning", "supported_efforts") or [])
        modal = (m.get("architecture") or {}).get("input_modalities") or ["text"]
        models.append(
            Model(
                id=m["id"],
                name=m.get("name") or m["id"],
                context=caps.get("contextWindow") or _nested(m, "top_provider", "context_length"),
                output=caps.get("maxOutput") or _nested(m, "top_provider", "max_completion_tokens"),
                reasoning=bool(caps.get("reasoning")),
                vision=bool(caps.get("vision")),
                tools=bool(caps.get("tools", True)),
                inputs=[x for x in modal if x in ("text", "image")] or ["text"],
                efforts=efforts,
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
                vision=bool(m.get("supports_vision")),
                tools=True,  # gutsai does not report tool support; configs assume true
                inputs=[x for x in modal if x in ("text", "image")] or ["text"],
                efforts=[],
            )
        )
    return models


NORMALIZERS = {"nutaraline": _normalize_nutaraline, "gutsai": _normalize_gutsai}


# --- merge helpers --------------------------------------------------------------

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


# --- jsonc (opencode) ------------------------------------------------------------

# v2 schema model keys the script manages. "capabilities" is v1-only and is
# replaced by modalities/tool_call/reasoning; "package" is v1 for "npm".
_OPENCODE_KNOWN = {
    "name", "limit", "variants", "modalities", "tool_call", "reasoning", "options",
    "capabilities", "package",
}
_OPENCODE_NPM = "@ai-sdk/openai-compatible"  # v2 SDK package for OpenAI-compatible gateways


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
    """opencode v2 model entry: modalities/tool_call/reasoning replace the v1
    `capabilities` block. `variants` stays a list of {id, settings} objects:
    the newest JSON schema documents it as an object, but opencode v2.0.27
    drops the whole provider when given the object form (verified: 0 of 18
    models load), so the list form is what this machine needs."""
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


def _migrate_opencode_provider(block: dict) -> None:
    """v1 -> v2: `package` becomes `npm` with the @ai-sdk package name."""
    if "package" in block:
        block.setdefault("npm", _OPENCODE_NPM)
        del block["package"]
    elif "npm" in block and "openai-compatible" in str(block["npm"]):
        block["npm"] = _OPENCODE_NPM


def _opencode_update(data: dict, catalog: dict[str, list[Model]]) -> tuple[dict, int, int, list[str]]:
    providers = data.get("providers") or {}
    added = removed = 0
    skipped: list[str] = []
    for prov_id, models in catalog.items():
        block = providers.get(prov_id)
        if block is None:
            skipped.append(prov_id)
            continue
        _migrate_opencode_provider(block)
        entries = block.setdefault("models", {})
        old_ids = set(entries)
        want_ids = {m.id for m in models}
        for m in models:
            entries[m.id] = _opencode_model(m, entries.get(m.id, {}))
        for stale in sorted(old_ids - want_ids):
            del entries[stale]
        added += len(want_ids - old_ids)
        removed += len(old_ids - want_ids)
    return data, added, removed, skipped


def _serialize_json(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# --- pi-models (pi, pig) -----------------------------------------------------------

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


def _pi_update(data: dict, catalog: dict[str, list[Model]]) -> tuple[dict, int, int]:
    providers = data.get("providers") or {}
    added = removed = 0
    for prov_id, models in catalog.items():
        block = providers.get(prov_id)
        if block is None:
            continue
        entries = block.get("models") or []
        old_by_id = {e.get("id"): e for e in entries}
        want = {m.id: m for m in models}
        block["models"] = [_pi_model(m, old_by_id.get(m.id, {})) for m in models]
        added += len(want.keys() - old_by_id.keys())
        removed += len(old_by_id.keys() - want.keys())
    return data, added, removed


# --- targets from catalog.md -----------------------------------------------------------

_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*(?:\|\s*)$")


def _expand_path(raw: str) -> Path:
    text = raw.strip().strip('"')

    def sub(m: re.Match) -> str:
        return os.environ.get(m.group(1)) or os.path.expandvars(m.group(2))

    text = re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}", sub, text
    )
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
        targets.append((harness, _expand_path(path_raw), fmt))
    return targets


# --- fetch -------------------------------------------------------------------------------


def _fetch_models(gateway: Gateway, timeout: float) -> list[Model]:
    key = os.environ.get(gateway.api_key_env)
    if not key:
        raise ScriptError(
            f"{gateway.api_key_env} is not set in this shell",
            hint="run: source ~/.config/agent-env.sh",
        )
    url = f"{gateway.base_url}/models"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as err:
        raise ScriptError(
            f"{gateway.id}: the gateway answered with HTTP {err.code} ({url})",
            hint="check that the API key is still valid",
        ) from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise ScriptError(
            f"{gateway.id}: could not reach {url}: {err}",
            hint="check the network, or raise the limit with --timeout",
        ) from err
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ScriptError(
            f"{gateway.id}: {url} did not return a model list "
            '(expected JSON with a "data" array)'
        )
    return NORMALIZERS[gateway.flavor](payload["data"])


# --- the job --------------------------------------------------------------------------------


def _plan_target(harness: str, path: Path, fmt: str, catalog: dict[str, list[Model]]) -> Plan:
    old_text = path.read_text(encoding="utf-8")
    skipped: list[str] = []
    if fmt == "opencode-jsonc":
        data = json.loads(strip_jsonc(old_text))
        new_data, added, removed, skipped = _opencode_update(data, catalog)
        new_text = _serialize_json(new_data)
    elif fmt == "pi-models":
        data = json.loads(old_text)
        new_data, added, removed = _pi_update(data, catalog)
        new_text = _serialize_json(new_data)
    else:
        raise ScriptError(
            f"{harness} uses an unknown config format {fmt!r}",
            hint="use one of: opencode-jsonc, pi-models",
        )
    for prov_id in skipped:
        emit(
            f"note: {prov_id} is not configured in {path.name}, "
            f"so its models were not added"
        )
    return Plan(harness, path, fmt, new_text, old_text, added, removed)


def _change_label(plan: Plan) -> str:
    if plan.added or plan.removed:
        return f"{plan.added} new models added, {plan.removed} removed"
    return "no models added or removed; details such as context windows were updated"


def work(args: argparse.Namespace) -> None:
    catalog_md = Path(args.catalog).expanduser()
    if not catalog_md.is_file():
        raise ScriptError(
            f"harness list not found: {catalog_md}",
            hint="pass the right path with --catalog",
        )

    with step(f"Read the harness list from {catalog_md.name}"):
        targets = _read_targets(catalog_md)
        if not targets:
            raise ScriptError(
                f"{catalog_md} lists no harnesses",
                hint="expected a markdown table: harness | config file | format",
            )
        for harness, path, _fmt in targets:
            if not path.is_file():
                raise ScriptError(f"{harness}: config file not found: {path}")

    catalogs: dict[str, list[Model]] = {}
    for gw in GATEWAYS:
        with step(f"Download the model list from {gw.id}"):
            catalogs[gw.id] = _fetch_models(gw, args.timeout)
        if not catalogs[gw.id]:
            raise ScriptError(
                f"{gw.id} returned no models; stopped so your configs are not emptied"
            )
    for gw in GATEWAYS:
        print(f"{gw.id}: {len(catalogs[gw.id])} models in its catalog")

    plans = []
    with step("Work out the new file contents (writes nothing)"):
        for harness, path, fmt in targets:
            plans.append(_plan_target(harness, path, fmt, catalogs))

    todo = [p for p in plans if p.new_text != p.old_text]
    if not todo:
        print(
            f"nothing to do: all {len(plans)} config files "
            f"already match the gateway catalogs"
        )
        return
    if DRY_RUN:
        for plan in todo:
            diff = "".join(
                difflib.unified_diff(
                    plan.old_text.splitlines(keepends=True),
                    plan.new_text.splitlines(keepends=True),
                    fromfile=f"{plan.path} (current)",
                    tofile=f"{plan.path} (new)",
                )
            )
            print(f"{plan.harness}: {_change_label(plan)} — {plan.path}")
            print(diff, end="")
        print("dry run finished: nothing was written")
        return

    stamped = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_dir = BACKUP_ROOT / stamped
    for plan in todo:
        with step(f"Save {plan.harness} (backup kept first)"):
            backup_dir.mkdir(parents=True, exist_ok=True)
            # pi and pig share the file name models.json: prefix with the harness
            # so one backup cannot overwrite the other.
            shutil.copy2(plan.path, backup_dir / f"{plan.harness}-{plan.path.name}")
            write_atomic(plan.path, plan.new_text)
        print(f"{plan.harness}: {_change_label(plan)} — {plan.path}")

    for old in sorted(BACKUP_ROOT.iterdir())[:-10] if BACKUP_ROOT.is_dir() else []:
        shutil.rmtree(old, ignore_errors=True)
    print(
        f"saved {len(todo)} of {len(plans)} config files; "
        f"previous versions are in {backup_dir}"
    )


# --- entry point (from stable-scripts template) -----------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=PROG, description=(__doc__ or "").splitlines()[0])
    parser.add_argument("-n", "--dry-run", action="store_true", help="show diffs without making them")
    parser.add_argument(
        "-t", "--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SEC",
        help=f"per-call time limit in seconds (default {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--catalog", default=str(DEFAULT_CATALOG), metavar="PATH",
        help=f"catalog.md holding the harness target table (default {DEFAULT_CATALOG})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show tracebacks")
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
        emit(f"{_paint('33', 'dry-run')} no files will be written")
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
