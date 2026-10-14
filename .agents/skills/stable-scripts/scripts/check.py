#!/usr/bin/env python3
"""Check scripts against the stable-scripts rules.

Usage: check.py FILE [FILE ...]

Python files. Errors (exit 1): syntax errors, subprocess/urlopen calls without
timeout=, shell=True, bare `except:`, ruff findings (rules E4,E7,E9,F,B,
ignoring local ruff config). Warnings: no shebang, not executable, ruff format
drift. ruff is optional and skipped if not installed.

Shell files (.sh/.bash or an sh/bash shebang). Errors: syntax errors (sh -n /
bash -n) and shellcheck findings. Warnings: no shebang, not executable.
shellcheck is optional and skipped if not installed. fish scripts are not
supported by shellcheck and are not checked.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

NEEDS_TIMEOUT = {
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
}


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    return ""


class Rules(ast.NodeVisitor):
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.imported_subprocess: set[str] = set()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "subprocess":
            for alias in node.names:
                if alias.name in {"run", "call", "check_call", "check_output"}:
                    self.imported_subprocess.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = dotted(node.func)
        keys = {k.arg for k in node.keywords}
        forwards_kwargs = None in keys  # f(**opts): cannot tell, stay quiet
        is_sub = (
            name in NEEDS_TIMEOUT
            or name in self.imported_subprocess
            or name.endswith("urlopen")
        )
        if is_sub and "timeout" not in keys and not forwards_kwargs:
            self.problems.append(f"line {node.lineno}: {name}() has no timeout=")
        for kw in node.keywords:
            if (
                kw.arg == "shell"
                and isinstance(kw.value, ast.Constant)
                and bool(kw.value.value)
            ):
                self.problems.append(
                    f"line {node.lineno}: shell=True (pass a list instead)"
                )
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self.problems.append(
                f"line {node.lineno}: bare 'except:' (name the exception)"
            )
        self.generic_visit(node)


def ruff(path: Path, *args: str) -> str | None:
    """Return ruff's output if it reports problems, else None."""
    # --isolated ignores any ruff config on the system, so results are the same everywhere;
    # --no-cache keeps a .ruff_cache folder from appearing next to the user's scripts
    cmd = ["ruff", args[0], "--isolated", "--no-cache", *args[1:], str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    return (proc.stdout + proc.stderr).strip() if proc.returncode else None


SHELL_SHEBANG = re.compile(r"^#!.*\b(ba)?sh\b")  # matches sh/bash, not fish/zsh


def run_tool(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def check_shell(path: Path, first: str) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    if not first.startswith("#!"):
        warnings.append("no shebang on line 1 (assuming bash)")
    if not os.access(path, os.X_OK):
        warnings.append("not executable (chmod +x)")
    shell = "bash" if "bash" in first or not first.startswith("#!") else "sh"
    if shutil.which(shell):
        code, out = run_tool([shell, "-n", str(path)])
        if code:
            return [f"{shell} -n: {out}"], warnings
    errors: list[str] = []
    if shutil.which("shellcheck"):
        code, out = run_tool(["shellcheck", "-x", "-s", shell, str(path)])
        if code:
            errors.append(f"shellcheck:\n{out}")
    else:
        warnings.append(
            "shellcheck not installed: shell lint skipped (install shellcheck to enable it)"
        )
    return errors, warnings


def check(path: Path) -> tuple[list[str], list[str]]:
    try:
        source = path.read_text()
    except (OSError, UnicodeDecodeError) as err:
        return [f"cannot read: {err}"], []
    first = source.splitlines()[0] if source else ""
    if path.suffix in {".sh", ".bash"} or SHELL_SHEBANG.match(first):
        return check_shell(path, first)
    if path.suffix == ".py" or "python" in first:
        return check_python(path, source, first)
    fish = path.suffix == ".fish" or "fish" in first
    kind = "fish script" if fish else "unknown file type"
    return [], [f"{kind}: not checked (only Python and sh/bash are supported)"]


def check_python(path: Path, source: str, first: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        tree = ast.parse(source, str(path))
        compile(source, str(path), "exec")
    except SyntaxError as err:
        return [f"syntax error at line {err.lineno}: {err.msg}"], warnings

    if not (first.startswith("#!") and "python" in first):
        warnings.append("no python shebang on line 1")
    if not os.access(path, os.X_OK):
        warnings.append("not executable (chmod +x)")

    rules = Rules()
    rules.visit(tree)
    errors += rules.problems

    if shutil.which("ruff"):
        if found := ruff(path, "check", "--select", "E4,E7,E9,F,B"):
            errors.append(f"ruff check:\n{found}")
        if found := ruff(path, "format", "--check"):
            warnings.append("ruff format would change this file")
    else:
        warnings.append("ruff not installed: lint skipped (install ruff to enable it)")
    return errors, warnings


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__, file=sys.stderr)
        return 2
    failed = False
    for name in paths:
        errors, warnings = check(Path(name))
        failed |= bool(errors)
        print(f"{'✗' if errors else '✓'} {name}")
        for msg in errors:
            print(f"    error: {msg}")
        for msg in warnings:
            print(f"    warn:  {msg}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
