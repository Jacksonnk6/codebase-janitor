#!/usr/bin/env python3
"""janitor - a tiny codebase janitor (MVP).

Scans a Python project for:
  1. unused imports            (auto-fixable)
  2. probably-dead code        (report only, a human decides)
  3. outdated pinned deps      (patch/minor auto-fixable, major = human)

Usage:
  janitor.py PATH                      report only
  janitor.py PATH --fix                fix on a fresh git branch and commit
  janitor.py PATH --fix --bump-deps    also bump patch/minor pins in requirements.txt
  janitor.py PATH --fix --verify "python -m unittest discover -q"
                                       run a check before committing; if it
                                       fails, everything is rolled back
"""
import argparse
import ast
import json
import re
import shlex
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "build", "dist", ".tox"}
REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9_.\-+!]+)")


@dataclass
class Finding:
    kind: str  # unused-import | dead-code | outdated-dep
    path: Path
    line: int
    message: str
    fixable: bool = False


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------
def iter_py_files(root: Path):
    for p in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        yield p


def parse_file(path: Path):
    try:
        src = path.read_text(encoding="utf-8")
        return src, ast.parse(src, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return None, None


def exported_names(tree: ast.Module) -> set:
    """Names listed in __all__ count as used."""
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                names |= {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    return names


def used_names(tree: ast.Module) -> set:
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    return names | exported_names(tree)


# --------------------------------------------------------------------------
# 1. Unused imports
# --------------------------------------------------------------------------
def find_unused_imports(path: Path, src: str, tree: ast.Module):
    """Returns a list of (node, alias, bound_name). Module-level imports only."""
    if path.name == "__init__.py":  # imports there are usually re-exports
        return []
    lines = src.splitlines()
    used = used_names(tree)
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if "noqa" in lines[node.lineno - 1]:
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name.split(".")[0]
            if bound not in used:
                out.append((node, alias, bound))
    return out


def render_import(node, aliases) -> str:
    parts = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in aliases)
    if isinstance(node, ast.Import):
        return f"import {parts}"
    return f"from {'.' * node.level}{node.module or ''} import {parts}"


def remove_unused_imports(src: str, unused) -> str:
    lines = src.splitlines(keepends=True)
    by_node = {}
    for node, alias, _ in unused:
        by_node.setdefault(node.lineno, (node, set()))[1].add(alias.name)
    for lineno in sorted(by_node, reverse=True):
        node, dead = by_node[lineno]
        if ";" in lines[node.lineno - 1]:  # several statements on one line: leave alone
            continue
        keep = [a for a in node.names if a.name not in dead]
        start, end = node.lineno - 1, node.end_lineno
        if keep:
            lines[start:end] = [render_import(node, keep) + "\n"]
        else:
            del lines[start:end]
    out = "".join(lines)
    # don't leave blank lines at the top when the removed imports opened the file
    return out if src.startswith("\n") else out.lstrip("\n")


# --------------------------------------------------------------------------
# 2. Probably-dead code
# --------------------------------------------------------------------------
def find_dead_code(root: Path, parsed: dict):
    refs = set()
    for path, (src, tree) in parsed.items():
        used_here = used_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                refs.add(node.id)
            elif isinstance(node, ast.Attribute):
                refs.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
                refs.add(node.value)  # getattr("name"), __all__, entry points...
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    # a re-export in __init__ counts; otherwise only if actually used
                    if path.name == "__init__.py" or (a.asname or a.name) in used_here:
                        refs.add(a.name)

    findings = []
    for path, (src, tree) in parsed.items():
        if path.name in {"__init__.py", "conftest.py", "setup.py"} or path.name.startswith("test_"):
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            name = node.name
            if node.decorator_list:  # routes, commands, fixtures... could be registered
                continue
            if name == "main" or name.startswith("test_") or (name.startswith("__") and name.endswith("__")):
                continue
            if name not in refs:
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                findings.append(
                    Finding("dead-code", path, node.lineno,
                            f"{kind} `{name}` is never referenced anywhere in the project")
                )
    return findings


# --------------------------------------------------------------------------
# 3. Outdated dependencies
# --------------------------------------------------------------------------
def parse_requirements(path: Path):
    out = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        m = REQ_RE.match(line.split("#")[0])
        if m:
            out.append((i, m.group(1), m.group(2)))
    return out


def latest_version(name: str, timeout: int = 6):
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=timeout) as r:
            return json.load(r)["info"]["version"]
    except Exception:
        return None


def bump_kind(current: str, latest: str):
    a = ([int(x) for x in re.findall(r"\d+", current)] + [0, 0, 0])[:3]
    b = ([int(x) for x in re.findall(r"\d+", latest)] + [0, 0, 0])[:3]
    if b <= a:
        return None
    if a[0] != b[0]:
        return "major"
    if a[1] != b[1]:
        return "minor"
    return "patch"


def find_outdated_deps(root: Path, offline: bool):
    req = root / "requirements.txt"
    if offline or not req.exists():
        return [], []
    findings, unchecked = [], []
    for lineno, name, current in parse_requirements(req):
        latest = latest_version(name)
        if latest is None:
            unchecked.append(name)
            continue
        kind = bump_kind(current, latest)
        if kind:
            findings.append(
                Finding("outdated-dep", req, lineno,
                        f"{name} {current} -> {latest} ({kind} bump)",
                        fixable=kind != "major")
            )
    return findings, unchecked


def apply_dep_bumps(root: Path, findings) -> int:
    req = root / "requirements.txt"
    lines = req.read_text().splitlines(keepends=True)
    count = 0
    for f in findings:
        if f.kind != "outdated-dep" or not f.fixable:
            continue
        name, rest = f.message.split(" ", 1)
        current, latest = rest.split(" -> ")
        latest = latest.split(" ")[0]
        lines[f.line - 1] = lines[f.line - 1].replace(f"=={current}", f"=={latest}", 1)
        count += 1
    req.write_text("".join(lines))
    return count


# --------------------------------------------------------------------------
# Git + fix flow
# --------------------------------------------------------------------------
def git(root: Path, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=check)


def run_fix(root: Path, parsed: dict, unused_by_file: dict, dep_findings, args) -> int:
    inside = git(root, "rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode != 0:
        print("error: --fix needs a git repository (so every change is reviewable and revertable)")
        return 2
    if git(root, "status", "--porcelain").stdout.strip():
        print("error: working tree is not clean; commit or stash first so the janitor's changes stay separate")
        return 2

    base = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    branch = "janitor/cleanup-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    git(root, "checkout", "-b", branch)

    removed = 0
    for path, unused in unused_by_file.items():
        src, _ = parsed[path]
        path.write_text(remove_unused_imports(src, unused), encoding="utf-8")
        removed += len(unused)

    bumped = apply_dep_bumps(root, dep_findings) if args.bump_deps else 0

    if args.verify:
        print(f"verifying: {args.verify}")
        result = subprocess.run(shlex.split(args.verify), cwd=root, capture_output=True, text=True)
        if result.returncode != 0:
            print("verification FAILED - rolling everything back")
            print((result.stdout + result.stderr).strip()[-800:])
            git(root, "checkout", "--", ".")
            git(root, "checkout", base)
            git(root, "branch", "-D", branch)
            return 1
        print("verification passed")

    git(root, "add", "-A")
    msg = f"chore: janitor cleanup\n\n- removed {removed} unused import(s)\n- bumped {bumped} dependency pin(s)\n"
    identity = []
    if not git(root, "config", "user.name", check=False).stdout.strip():
        identity = ["-c", "user.name=janitor-bot", "-c", "user.email=janitor@example.invalid"]
    git(root, *identity, "commit", "-m", msg)
    print(f"\ncommitted on branch {branch}: {removed} import(s) removed, {bumped} pin(s) bumped")
    print(f"review with: git -C {root} diff {base}...{branch}")
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="A tiny codebase janitor.")
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--fix", action="store_true", help="apply safe fixes on a new git branch")
    ap.add_argument("--bump-deps", action="store_true", help="with --fix, bump patch/minor pins")
    ap.add_argument("--verify", metavar="CMD", help="command that must pass before committing")
    ap.add_argument("--offline", action="store_true", help="skip the PyPI dependency check")
    args = ap.parse_args()

    root = Path(args.path).resolve()
    parsed = {}
    for p in iter_py_files(root):
        src, tree = parse_file(p)
        if tree is not None:
            parsed[p] = (src, tree)

    unused_by_file, findings = {}, []
    for path, (src, tree) in parsed.items():
        unused = find_unused_imports(path, src, tree)
        if unused:
            unused_by_file[path] = unused
            for node, alias, bound in unused:
                findings.append(Finding("unused-import", path, node.lineno, f"`{bound}` is imported but never used", True))

    findings += find_dead_code(root, parsed)
    dep_findings, unchecked = find_outdated_deps(root, args.offline)
    findings += dep_findings

    titles = {
        "unused-import": "Unused imports (auto-fixable)",
        "dead-code": "Probably dead code (needs a human)",
        "outdated-dep": "Outdated dependencies",
    }
    print(f"janitor scanned {len(parsed)} Python file(s) in {root}\n")
    for kind, title in titles.items():
        group = [f for f in findings if f.kind == kind]
        if not group:
            continue
        print(f"{title}: {len(group)}")
        for f in group:
            tag = "" if f.kind != "outdated-dep" else ("  [auto]" if f.fixable else "  [human]")
            print(f"  {f.path.relative_to(root)}:{f.line}  {f.message}{tag}")
        print()
    if unchecked:
        print(f"(could not reach PyPI for: {', '.join(unchecked)})\n")
    if not findings:
        print("Nothing to clean. Suspiciously tidy.")
        return 0

    if args.fix:
        return run_fix(root, parsed, unused_by_file, dep_findings, args)
    print("Run again with --fix to clean up on a new git branch.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
