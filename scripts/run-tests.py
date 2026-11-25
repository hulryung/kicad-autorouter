#!/usr/bin/env python3
"""Run every tests/test_*.py as its own process, with this interpreter.

    python3 scripts/run-tests.py            # the offline tests pass with any Python 3.8+
    <KiCad's python3> scripts/run-tests.py  # also the pcbnew/wx ones (others skip themselves)
"""
import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent / "tests"


def main():
    words = sys.argv[1:]
    tests = [test for test in sorted(TESTS.glob("test_*.py"))
             if not words or any(w in test.stem for w in words)]
    if not tests:
        print("No tests matched:", " ".join(words), file=sys.stderr)
        return 2
    failed = []
    env = dict(os.environ, KICAD_AUTOROUTER_NO_REGISTER="1")
    timeout = float(os.environ.get("KICAD_AUTOROUTER_TEST_TIMEOUT", "300"))
    for test in tests:
        print("RUN:", test.name, flush=True)
        try:
            result = subprocess.run([sys.executable, str(test)], env=env, timeout=timeout)
            if result.returncode != 0:
                failed.append(test.name)
        except subprocess.TimeoutExpired:
            print(f"TIMEOUT: {test.name} exceeded {timeout:g} seconds", flush=True)
            failed.append(test.name)
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
