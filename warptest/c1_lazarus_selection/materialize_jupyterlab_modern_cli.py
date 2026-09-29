"""Materialize a modern JupyterLab fixed/buggy pair for strict Gate 1--7 work.

The tool is deliberately mechanical.  It archives the tracked tree of the merged
fix revision, clones the pinned workspace dependencies per variant, reverses only
the production hunks in the buggy variant, and compiles both variants with the
candidate-local TypeScript.  It writes no typed artifact: the per-candidate Gate
1--7 adapter validates and records the result.

``git apply`` is unusable here because the artifact root lives inside another git
work tree, where git silently skips patch paths outside the current prefix, so
the inverse is applied with ``patch``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

_MIRROR = "private/repository-enumeration-git/jupyterlab/mirror.git"
_MATERIALIZED = "private/materialized/jupyterlab"
_GIT = "/usr/bin/git"
_PATCH = "/usr/bin/patch"


def materialize(
    *,
    artifact_root: Path,
    workspace: Path,
    change_number: int,
    revision: str,
    production_paths: tuple[str, ...],
    package_directory: str,
    node: Path,
    epoch: str = "current-v1",
) -> Path:
    """Create ``<artifact_root>/private/materialized/jupyterlab/<n>/<epoch>``."""

    root = artifact_root.resolve(strict=True)
    mirror = (root / _MIRROR).resolve(strict=True)
    modules = (workspace / "node_modules").resolve(strict=True)
    destination = root / _MATERIALIZED / str(change_number) / epoch
    if destination.exists():
        raise SystemExit(f"destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    staging = destination.parent / f".staging-{epoch}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    archive = subprocess.run(
        (_GIT, f"--git-dir={mirror}", "archive", revision),
        capture_output=True,
        check=True,
    )
    subprocess.run(("/usr/bin/tar", "-x", "-C", str(staging)), input=archive.stdout, check=True)
    print(f"archived {revision} into {staging}")
    subprocess.run(("/bin/cp", "-c", "-R", str(modules), str(staging / "node_modules")), check=True)
    print("cloned pinned workspace dependencies")
    os.replace(staging, destination)

    fixed = destination / "fixed"
    buggy = destination / "buggy"
    fixed.mkdir()
    for entry in sorted(destination.iterdir()):
        if entry.name in {"fixed", "buggy"}:
            continue
        os.replace(entry, fixed / entry.name)
    subprocess.run(("/bin/cp", "-c", "-R", str(fixed), str(buggy)), check=True)
    print("cloned the fixed tree into the buggy variant")

    inverse = subprocess.run(
        (
            _GIT,
            f"--git-dir={mirror}",
            "diff",
            f"{revision}^",
            revision,
            "--",
            *production_paths,
        ),
        capture_output=True,
        check=True,
    )
    if not inverse.stdout:
        raise SystemExit("production diff is empty")
    result = subprocess.run(
        (_PATCH, "-p1", "-R", "--batch", "--forward"),
        cwd=buggy,
        input=inverse.stdout,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stdout.decode(errors="replace"))
        print(result.stderr.decode(errors="replace"))
        raise SystemExit("production inverse failed")
    print("applied the production-only inverse")

    changed = _tracked_differences(fixed, buggy)
    if changed != sorted(production_paths):
        raise SystemExit(f"inverse is not production-only: {changed}")

    for variant in (fixed, buggy):
        build = subprocess.run(
            (
                str(node),
                str(variant / "node_modules/typescript/bin/tsc"),
                "--build",
                "tsconfig.json",
                "tsconfig.test.json",
                "--force",
            ),
            cwd=variant / package_directory,
            capture_output=True,
            check=False,
        )
        if build.returncode != 0:
            print(build.stdout.decode(errors="replace")[-4000:])
            print(build.stderr.decode(errors="replace")[-4000:])
            raise SystemExit(f"{variant.name} TypeScript build failed")
        print(f"compiled {variant.name}")
    return destination


def _tracked_differences(fixed: Path, buggy: Path) -> list[str]:
    result = subprocess.run(
        ("/usr/bin/diff", "-rq", "--exclude", "node_modules", str(fixed), str(buggy)),
        capture_output=True,
        check=False,
    )
    differences: list[str] = []
    for line in result.stdout.decode(errors="replace").splitlines():
        if not line.startswith("Files "):
            raise SystemExit(f"unexpected tree difference: {line}")
        left = line.split(" ", 2)[1]
        differences.append(str(Path(left).relative_to(fixed)))
    return sorted(differences)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--change-number", type=int, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--production", action="append", required=True)
    parser.add_argument("--package-directory", required=True)
    parser.add_argument(
        "--node", type=Path, default=Path("/opt/homebrew/Cellar/node@22/22.22.0_1/bin/node")
    )
    parser.add_argument("--epoch", default="current-v1")
    arguments = parser.parse_args(argv)
    destination = materialize(
        artifact_root=arguments.artifact_root,
        workspace=arguments.workspace,
        change_number=arguments.change_number,
        revision=arguments.revision,
        production_paths=tuple(arguments.production),
        package_directory=arguments.package_directory,
        node=arguments.node,
        epoch=arguments.epoch,
    )
    print(f"materialized {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
