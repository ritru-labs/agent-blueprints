"""Synthetic head-SHA qualification check for Phase 1F."""

import pathlib
import subprocess
import sys

PROJECT = pathlib.Path(__file__).resolve().parents[1] / "agents-api-spike/sample-project"
APP = PROJECT / "sample/app.py"


def main():
    if "PHASE1F_REPAIR_REQUIRED" in APP.read_text():
        print("Synthetic qualification marker remains in sample/app.py", file=sys.stderr)
        return 1
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s",
                             "sample/tests", "-v"], cwd=PROJECT, timeout=60, check=False)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
