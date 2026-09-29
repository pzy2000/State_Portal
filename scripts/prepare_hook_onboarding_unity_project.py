#!/usr/bin/env python3
"""Populate a clean Unity project's Library before hook-agent evaluation."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


UNITY_EXECUTABLES = {
    "jynew": Path("/Applications/Unity/Unity.app/Contents/MacOS/Unity"),
    "pal3": Path("/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity"),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True, choices=sorted(UNITY_EXECUTABLES))
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args()
    repo = args.repo.expanduser().resolve()
    report_dir = args.report_dir.expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    executable = UNITY_EXECUTABLES[args.app]
    if not executable.is_file():
        raise SystemExit(f"Unity executable is unavailable: {executable}")
    project = repo / "jyx2" if args.app == "jynew" else repo
    command = [
        str(executable),
        "-batchmode",
        "-nographics",
        "-quit",
        "-projectPath",
        str(project),
        "-logFile",
        str(report_dir / "unity-prepare.log"),
    ]
    completed = subprocess.run(command, cwd=repo, timeout=1800)
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
