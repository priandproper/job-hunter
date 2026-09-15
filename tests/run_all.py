#!/usr/bin/env python3
"""Run the whole job-hunter test suite (Python + node) with one command.

    python3 tests/run_all.py

Zero-dependency: discovers tests/test_*.py (run with python3) and tests/test_*.cjs
(run with node, if node is installed) and reports a combined pass/fail total. Exit code
is non-zero if anything fails, so it works in CI or a pre-push hook.
"""

import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _run(cmd, path):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    except Exception as e:  # noqa: BLE001
        return None, None, f"ERROR launching: {e}"
    line = next((l for l in reversed(out.splitlines()) if "passed" in l), "")
    if "/" not in line:
        return None, None, "no result line"
    try:
        n, d = line.split()[0].split("/")
        return int(n), int(d), line.strip()
    except ValueError:
        return None, None, line.strip()


def main():
    ok = tot = 0
    fails = []
    py = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    cjs = sorted(glob.glob(os.path.join(HERE, "test_*.cjs")))
    node = shutil.which("node")

    print("== Python tests ==")
    for f in py:
        n, d, line = _run([sys.executable, f], f)
        name = os.path.basename(f)
        if n is None:
            fails.append(name); print(f"  ✗ {name}: {line}")
        else:
            ok += n; tot += d
            print(f"  {'✓' if n == d else '✗'} {name}: {line}")
            if n != d:
                fails.append(name)

    print("\n== JS tests (node) ==" if cjs else "")
    if cjs and not node:
        print("  ⚠ node not found — skipping JS tests:", ", ".join(os.path.basename(f) for f in cjs))
        fails.append("node-missing")
    for f in cjs if node else []:
        n, d, line = _run([node, f], f)
        name = os.path.basename(f)
        if n is None:
            fails.append(name); print(f"  ✗ {name}: {line}")
        else:
            ok += n; tot += d
            print(f"  {'✓' if n == d else '✗'} {name}: {line}")
            if n != d:
                fails.append(name)

    print(f"\n{'='*40}\nTOTAL: {ok}/{tot} passed"
          + (f" · FAILURES: {', '.join(fails)}" if fails else " · all green ✓"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
