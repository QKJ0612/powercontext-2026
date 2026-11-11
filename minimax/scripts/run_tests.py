"""Run the demo project's tests and print a compact result."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1] / "sample_project"


def main() -> int:
    completed = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests"], cwd=PROJECT)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
