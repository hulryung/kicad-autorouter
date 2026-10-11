#!/usr/bin/env python3
"""Run every tests/test_*.py as its own process, with this interpreter.

    python3 scripts/run-tests.py            # the offline tests pass with any Python 3.8+
    <KiCad's python3> scripts/run-tests.py  # also the pcbnew/wx ones (others skip themselves)
"""
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent / "tests"


def main():
    words = sys.argv[1:]
    failed = []
    for test in sorted(TESTS.glob("test_*.py")):
        if words and not any(w in test.stem for w in words):
            continue
        result = subprocess.run([sys.executable, str(test)])
        if result.returncode != 0:
            failed.append(test.name)
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
