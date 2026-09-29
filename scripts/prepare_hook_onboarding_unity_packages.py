#!/usr/bin/env python3
"""Prepare local Git mirrors for jynew's unavailable Gitee UPM packages.

The source directories are Unity's already-resolved package caches from the
handwritten reference checkout.  They contain third-party dependencies only;
no product source or WarpTest hook is copied into the clean onboarding repo.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


PACKAGE_SOURCES = {
    "assetbundlebrowser": Path(
        "games/jynew/jyx2/Library/PackageCache/com.unity.assetbundlebrowser@b7c279278d"
    ),
    "sharp-zip-lib": Path(
        "games/jynew/jyx2/Library/PackageCache/com.unity.sharp-zip-lib@a82316d2ab"
    ),
    "simplefilebrowser": Path(
        "games/jynew/jyx2/Library/PackageCache/com.yasirkula.simplefilebrowser@1e8a795d15"
    ),
}
GIT_PACKAGE_IDS = tuple(
    (
        "com.unity.assetbundlebrowser",
        "com.unity.sharp-zip-lib",
        "com.yasirkula.simplefilebrowser",
    )
)


def run(argv: list[str], cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)


def prepare(repository_root: Path, project_repo: Path, report_dir: Path) -> None:
    mirrors = report_dir / "unity-package-mirrors"
    mirrors.mkdir(parents=True, exist_ok=True)
    for name, relative in PACKAGE_SOURCES.items():
        source = (repository_root / relative).resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"Unity package cache is unavailable: {source}")
        destination = mirrors / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(".git", "Tests", "Tests~", "Documentation~"),
        )
        run(["git", "init"], destination)
        run(["git", "config", "user.email", "onboarding-reference@example.invalid"], destination)
        run(["git", "config", "user.name", "Onboarding Dependency Mirror"], destination)
        run(["git", "add", "."], destination)
        run(["git", "commit", "-m", "Cached dependency snapshot"], destination)
    lock_path = project_repo / "jyx2" / "Packages" / "packages-lock.json"
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, dict):
        raise ValueError(f"Unity package lock is malformed: {lock_path}")
    for package_id in GIT_PACKAGE_IDS:
        dependencies.pop(package_id, None)
    lock_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args()
    prepare(
        Path(__file__).resolve().parents[1],
        args.repo.expanduser().resolve(),
        args.report_dir.expanduser().resolve(),
    )


if __name__ == "__main__":
    main()
