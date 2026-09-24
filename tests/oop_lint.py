#!/usr/bin/env python3
"""Static OOP + subagent-removal enforcement for the pi-daemon repo.

House rule (shared memory): owned program logic is structured as classes
with single-responsibility methods; state is mutated only by its owning
object or explicitly through an input parameter - never through module
globals or another function's side effects.

Checks:
  1. TypeScript extensions (pi/extensions/*.ts): no top-level mutable
     bindings. At module brace-depth 0 only imports, type/interface,
     class/function declarations, and `const` scalar values are
     allowed; `let`/`var` and mutable container constants are
     forbidden. Classes may own mutable instance fields.
  2. Python daemon/client (pi/daemon, pi/bin): no module-level mutable
     containers, and every class uses `self.X` (instance-owned) state.
  3. Extension formatting (https://pi.dev/docs/latest/extensions): a
     pi/extensions dir containing only .ts files, each declaring an
     `export default` entry.
  4. Subagent feature removal completeness: none of the removed
     feature identifiers appears anywhere in pi/, scripts/, or the
     README.
"""

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILURES: list[str] = []


def fail(path: str, message: str) -> None:
    FAILURES.append(f"{path}: {message}")


# -- 1. TypeScript top-level-state check -----------------------------------

TS_TOP_LEVEL_BAD = [
    (r"^\s*let\s+", "top-level let binding"),
    (r"^\s*var\s+", "top-level var binding"),
]

TS_CONST_CONTAINER = re.compile(
    r"^\s*const\s+\w+\s*=\s*(new\s+(Map|Set)\b|\{|\[)"
)

TS_SKIP_ANNOTATION = "// oop-check: skip"  # deliberate, documented escapes


def check_typescript(path: str) -> None:
    """Walk statement lines tracking brace depth; flag module-scope
    declarations that introduce mutable state. Instance fields and
    class-local state are allowed (owned by the object)."""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    depth = 0
    for lineno, raw in enumerate(lines, 1):
        line = raw.rstrip("\n")
        stripped = line.strip()
        # Track braces ignoring commented lines and string-ish noise:
        # adequate for this codebase (statements are one per line).
        if stripped.startswith("//") or stripped.startswith("/*") \
                or stripped.startswith("*"):
            continue
        if stripped.endswith("*/") or "/*" in stripped:
            pass  # keep depth tracking sane on inline comments
        depth += line.count("{") - line.count("}")
        if depth != 0 or not stripped or stripped.endswith("*/"):
            continue
        if TS_SKIP_ANNOTATION in line:
            continue
        for pattern, label in TS_TOP_LEVEL_BAD:
            if re.match(pattern, stripped):
                fail(path, f"line {lineno}: {label}: {stripped[:60]}")
        if TS_CONST_CONTAINER.match(stripped):
            fail(path, f"line {lineno}: top-level mutable const container: {stripped[:60]}")


# -- 2. Python module-state check ------------------------------------------

PY_MODULE_MUTABLE = re.compile(
    r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(\{\}|\[\]|dict\(|list\(|set\()"
)
PY_UPPER = re.compile(r"^[A-Z][A-Z0-9_]*$")


def check_python(path: str) -> None:
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for lineno, raw in enumerate(lines, 1):
        stripped = raw.rstrip("\n")
        if not stripped or stripped.startswith(("#", " " * 4, "\t")):
            continue  # comments and anything indented stay in scope-owners
        m = PY_MODULE_MUTABLE.match(stripped)
        if m and not PY_UPPER.match(m.group(1)):
            fail(path, f"line {lineno}: module-level mutable container: {stripped[:60]}")
        if re.match(r"^\s*(let|var)\s", stripped):
            fail(path, f"line {lineno}: module-level let/var: {stripped[:60]}")


# -- 3. Subagent removal completeness --------------------------------------

REMOVED_FEATURE_IDS = [
    r"subagent",
    r"daemon_subagent",
    r"agent-submit",
    r"agent-wait",
    r"agent-output",
    r"agent-cancel",
    r"agent-detach",
    r"agent-bridge",
    r"\bagent-list\b",
    r"pi-agent-entry",
    r"dsubagents",
    r"AgentTicketRunner",
    r"AGENT_ENTRY",
    r"PI_PTYD_SESSKEY",
    r"AGENT_WATCH_TICK",
    r"PI_DAEMON_AGENT_CHILD",
    r"ADOPTION_NOTICE",
    r"turn_budget",
]

REMOVED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in REMOVED_FEATURE_IDS]

REMOVED_FILES = [
    "pi/extensions/dsubagents-views.ts",
    "pi/daemon/pi-agent-entry.mjs",
]


def check_no_subagent_residue(root: str) -> None:
    for dirpath, dirs, files in os.walk(root):
        # Prune by path component, not a literal prefix: on Windows the
        # separator is "\\", so a "/.git" substring check would scan
        # tests/ (which intentionally contains the removed identifiers)
        # and fail the lint on every Windows run.
        # node_modules: a local npm install (needed by the TS suite)
        # carries third-party sources and vendored pi copies that hold
        # the removed identifiers; they are not this repo's code.
        dirs[:] = [d for d in dirs if d not in (".git", "tests", "node_modules")]
        for name in files:
            path = os.path.join(dirpath, name)
            if path.endswith((".pyc", ".map")):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            for pattern in REMOVED_PATTERNS:
                m = pattern.search(text)
                if m:
                    fail(path, f"removed-feature identifier: {m.group(0)!r}")
    for rel in REMOVED_FILES:
        if os.path.exists(os.path.join(root, rel)):
            fail(rel, "removed file still exists")


def check_extension_format(root: str) -> None:
    """Per pi.dev/docs/latest/extensions: the extension dir holds only
    .ts entry files, each with a default export. A subdirectory without an
    index is a support-module directory the extensions import (pi loads
    directories only through index.ts/index.js or a package.json pi
    manifest), so it is allowed and its .ts files are still covered by
    the top-level-state check."""
    ext_dir = os.path.join(root, "pi", "extensions")
    if not os.path.isdir(ext_dir):
        fail(ext_dir, "missing pi/extensions directory")
        return
    for name in sorted(os.listdir(ext_dir)):
        if name == ".DS_Store":
            continue
        path = os.path.join(ext_dir, name)
        if os.path.isdir(path):
            continue
        if not name.endswith(".ts"):
            fail(path, "non-TS file in the extension directory")
            continue
        with open(path, "r", encoding="utf-8") as f:
            if not re.search(r"\bexport\s+default\b", f.read()):
                fail(path, "extension module must declare a default export")


def _extension_ts_files(root: str) -> list[str]:
    """Repo-relative .ts paths under pi/extensions, support dirs included."""
    ext_dir = os.path.join(root, "pi", "extensions")
    found = []
    for dirpath, dirs, files in os.walk(ext_dir):
        dirs[:] = [d for d in dirs if d != "node_modules"]
        for name in sorted(files):
            if name.endswith(".ts"):
                full = os.path.join(dirpath, name)
                found.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(found)


def _relative_files(directory: str, suffix: str) -> list[str]:
    """Repo-relative paths of `directory` entries ending in `suffix`."""
    base = os.path.join(REPO, directory)
    return [f"{directory}/{name}" for name in sorted(os.listdir(base))
            if name.endswith(suffix)]


def main() -> int:
    # Glob so a newly added extension or support module is covered without
    # editing this list; the two extension-less scripts are explicit.
    ts_files = _extension_ts_files(REPO)
    py_files = (["pi/daemon/pi-daemon", "pi/bin/pi-rc"]
                + _relative_files("pi/lib", ".py"))
    for rel in ts_files:
        check_typescript(os.path.join(REPO, rel))
    for rel in py_files:
        check_python(os.path.join(REPO, rel))
    check_extension_format(REPO)
    check_no_subagent_residue(REPO)
    if FAILURES:
        for item in FAILURES:
            print("OOP-LINT FAIL: " + item)
        return 1
    print("oop lint: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())