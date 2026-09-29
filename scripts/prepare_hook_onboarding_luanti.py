#!/usr/bin/env python3
"""Build the frozen Luanti clean source into a reusable headless server."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.expanduser().resolve()
    report_dir = args.report_dir.expanduser().resolve()
    build_dir = report_dir / "luanti-agent-build"
    sdk = subprocess.run(
        ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()
    configure = [
        "cmake",
        "-S",
        str(repo),
        "-B",
        str(build_dir),
        "-DBUILD_CLIENT=FALSE",
        "-DBUILD_SERVER=TRUE",
        "-DRUN_IN_PLACE=TRUE",
        "-DBUILD_UNITTESTS=FALSE",
        "-DBUILD_DOCUMENTATION=FALSE",
        "-DENABLE_GETTEXT=FALSE",
        "-DENABLE_SOUND=FALSE",
        "-DCMAKE_CXX_FLAGS=-DLIBICONV_PLUG",
        f"-DICONV_LIBRARY={sdk}/usr/lib/libiconv.tbd",
    ]
    subprocess.run(configure, cwd=repo, check=True, timeout=900)
    subprocess.run(
        ["cmake", "--build", str(build_dir), "-j2"],
        cwd=repo,
        check=True,
        timeout=1_800,
    )
    candidates = (
        repo / "bin" / "luantiserver",
        build_dir / "bin" / "luantiserver",
        build_dir / "luantiserver",
    )
    if not any(path.is_file() for path in candidates):
        raise SystemExit("Luanti build completed without a luantiserver executable")


if __name__ == "__main__":
    main()
