#!/usr/bin/env python3
"""pi-daemon validation runner: OOP/removal lint, unit tests, and the
real daemon+client integration test. Zero dependencies (stdlib only).

Usage:
    python3 tests/run.py
"""

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def step(name, cmd):
    print(f"== {name} ==")
    proc = subprocess.run(cmd, cwd=REPO)
    print()
    if proc.returncode != 0:
        print(f"{name}: FAILED ({proc.returncode})")
        return False
    print(f"{name}: ok")
    return True


def main():
    all_ok = True
    all_ok &= step("OOP + removal lint",
                   [sys.executable, "tests/oop_lint.py"])
    all_ok &= step("extension tests (TS)",
                   ["node", "--experimental-strip-types",
                    "--experimental-transform-types", "tests/ts/run.ts"])
    all_ok &= step("daemon unit tests",
                   [sys.executable, "tests/daemon_unit_test.py"])
    all_ok &= step("daemon integration test",
                   [sys.executable, "tests/daemon_integration_test.py"])
    all_ok &= step("ConPTY backend (skips off Windows)",
                   [sys.executable, "tests/conpty_test.py"])
    if not all_ok:
        return 1
    print("all pi-daemon tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())