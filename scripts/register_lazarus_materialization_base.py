#!/usr/bin/env python3
"""Register the revision each formal C1-Lazarus row is materialized from.

A row's fixed tree is not always the pinned submodule revision.  Many frozen
rows reproduce a defect that predates it, and their published inverse patch is
anchored at the contemporaneous tree, so reverse-applying it to the pinned
checkout is rejected outright.

The base is recoverable from the patch itself: a unified diff records the
post-image blob of every file it rewrites, and that blob is the fixed content.
This tool resolves candidate revisions from those blobs, then *verifies* each
candidate by checking the whole patch reverse-applies to a throwaway worktree at
that revision.  Only a verified revision is registered.

Resolution is deliberately done here and not at preflight time.  Searching
history is a heuristic that can name several commits carrying the same blob, so
the answer has to be reviewable evidence rather than something a paid run
recomputes.  The sidecar it writes is that evidence.

Run with ``--check`` in CI to fail when a registered base no longer verifies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = "c1-lazarus-materialization-base-v1"
_INDEX_LINE = re.compile(r"^index ([0-9a-f]{7,40})\.\.([0-9a-f]{7,40})", re.M)
# A blob can appear in many commits; bound the search so one pathological row
# cannot stall the whole registration.
_MAX_CANDIDATES = 8


class BaseRegistrationError(RuntimeError):
    """Raised when a row's materialization base cannot be established."""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=ROOT / "benchmarks/p5/c1_lazarus_tasks.yaml",
    )
    parser.add_argument("--task-id", action="append", default=None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the registered bases without writing anything",
    )
    return parser.parse_args(argv)


def sidecar_path(workspace: Path, task_id: str) -> Path:
    return (
        workspace
        / "benchmarks/p5/lazarus/authoring"
        / f"{task_id}.materialization.json"
    )


def _git(repo: Path, *args: str, timeout: float = 240.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        shell=False,
        timeout=timeout,
    )


def post_image_blobs(patch: Path) -> tuple[str, ...]:
    """Return every post-image blob id the patch records, in order."""

    text = patch.read_text(encoding="utf-8", errors="replace")
    return tuple(dict.fromkeys(match.group(2) for match in _INDEX_LINE.finditer(text)))


def candidate_revisions(repo: Path, patch: Path) -> tuple[str, ...]:
    """Resolve revisions whose tree could be the patch's fixed base."""

    candidates: list[str] = []
    for blob in post_image_blobs(patch):
        if _git(repo, "cat-file", "-e", blob).returncode != 0:
            continue
        found = _git(
            repo,
            "log",
            "--all",
            "--format=%H",
            f"-{_MAX_CANDIDATES}",
            f"--find-object={blob}",
        )
        for revision in found.stdout.split():
            if revision not in candidates:
                candidates.append(revision)
    return tuple(candidates)


def verify_revision(repo: Path, patch: Path, revision: str) -> bool:
    """Whether the whole patch reverse-applies to the tree at one revision."""

    worktree = Path(
        tempfile.mkdtemp(prefix="lazarus-base-", dir=tempfile.gettempdir())
    ).resolve()
    # mkdtemp created it; git refuses an existing directory.
    shutil.rmtree(worktree, ignore_errors=True)
    added = _git(repo, "worktree", "add", "--detach", str(worktree), revision)
    if added.returncode != 0:
        return False
    try:
        return _git(worktree, "apply", "--reverse", "--check", str(patch)).returncode == 0
    finally:
        _git(repo, "worktree", "remove", "--force", str(worktree))
        shutil.rmtree(worktree, ignore_errors=True)
        _git(repo, "worktree", "prune")


def resolve_base(repo: Path, patch: Path, *, host_revision: str) -> dict:
    """Return the verified materialization base for one row."""

    # The pinned revision is preferred: a row that still applies there is a
    # current-base row and needs no historical staging.
    if verify_revision(repo, patch, host_revision):
        return {
            "fixed_revision": host_revision,
            "source": "pinned_host_revision",
            "candidates_considered": 1,
        }
    candidates = candidate_revisions(repo, patch)
    for revision in candidates:
        if verify_revision(repo, patch, revision):
            return {
                "fixed_revision": revision,
                "source": "patch_post_image_blob",
                "candidates_considered": len(candidates),
            }
    raise BaseRegistrationError(
        "no revision in this repository accepts the registered inverse patch; "
        f"{len(candidates)} candidate(s) considered"
    )


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    catalog = yaml.safe_load(args.catalog.read_text(encoding="utf-8"))
    hosts = catalog["benchmark_hosts"]
    selected = set(args.task_id or ())

    registered: dict[str, str] = {}
    unchanged: list[str] = []
    unresolved: dict[str, str] = {}
    for row in catalog["tasks"]:
        task_id = str(row["id"])
        if selected and task_id not in selected:
            continue
        repo = workspace / str(row["repo"])
        patch = workspace / str(row["defect"]["revert_patch"])
        host_revision = str(hosts[str(row["benchmark"])]["revision"])
        try:
            resolution = resolve_base(repo, patch, host_revision=host_revision)
        except BaseRegistrationError as error:
            unresolved[task_id] = str(error)
            continue
        payload = {
            "schema": SCHEMA,
            "schema_version": 1,
            "task_id": task_id,
            "benchmark": str(row["benchmark"]),
            "repo": str(row["repo"]),
            "patch_sha256": str(row["defect"]["patch_sha256"]),
            "pinned_host_revision": host_revision,
            **resolution,
            "verification": "reverse_apply_check_in_detached_worktree",
        }
        destination = sidecar_path(workspace, task_id)
        existing = (
            json.loads(destination.read_text(encoding="utf-8"))
            if destination.is_file()
            else None
        )
        if existing == payload:
            unchanged.append(task_id)
            continue
        registered[task_id] = str(resolution["fixed_revision"])
        if not args.check:
            _write_json(destination, payload)

    print(
        json.dumps(
            {
                "mode": "check" if args.check else "write",
                "unchanged": len(unchanged),
                "registered" if not args.check else "would_register": registered,
                "unresolved": unresolved,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    if unresolved:
        return 1
    if args.check and registered:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
