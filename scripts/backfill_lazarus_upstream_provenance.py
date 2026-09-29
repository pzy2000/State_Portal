#!/usr/bin/env python3
"""Bind each formal C1-Lazarus upstream test to the bytes a trial will execute.

The frozen rows' ``formal-v1/upstream.json`` is a projection of freeze-sprint
evidence, and its projector emits each test's id, kind, command and path but not
the per-test provenance the experiment's trial-strength contract requires: a
content digest of the test source and a URL naming it upstream.  Without those
two fields ``validate_lazarus_trial_provenance`` refuses the row, so no formal
preflight can authorize it.

This backfill takes both facts from the pinned submodule checkout rather than
from the fix commit.  That is deliberate: the digest then describes the exact
test bytes the graded run executes, and the URL points at that same revision, so
the record cannot claim a test whose content has moved on.  It is also precisely
what the validator checks for a row that ships no frozen test sources.

A row is rewritten only if the whole record validates afterwards; a row whose
test source is absent from the pinned tree is reported instead, because it needs
frozen test bytes published beside its record and this tool must not invent them.

Run with ``--check`` in CI to fail when a committed record has drifted.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_mining import (  # noqa: E402
    _canonical_sha256,
    validate_lazarus_trial_provenance,
)


# How each authority addresses one file at one revision.
_BLOB_URL = {
    "github": "https://github.com/{repository}/blob/{revision}/{path}",
    "gitea": "https://projects.blender.org/{repository}/src/commit/{revision}/{path}",
}


class BackfillError(RuntimeError):
    """Raised when a row cannot be bound without inventing evidence."""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=ROOT / "benchmarks/p5/c1_lazarus_tasks.yaml",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what would change without writing anything",
    )
    return parser.parse_args(argv)


def _blob_url(*, authority: str, repository: str, revision: str, path: str) -> str:
    template = _BLOB_URL.get(authority)
    if template is None:
        raise BackfillError(f"no blob URL convention for authority {authority!r}")
    return template.format(repository=repository, revision=revision, path=path)


def bind_record(
    record: dict,
    *,
    repo: Path,
    revision: str,
) -> tuple[dict, list[str]]:
    """Return the record with per-test provenance bound, plus unresolved paths."""

    bound = copy.deepcopy(record)
    authority = str(bound.get("authority") or "")
    repository = str(bound.get("repository") or "")
    unresolved: list[str] = []
    for test in bound.get("tests") or []:
        if not isinstance(test, dict):
            continue
        # The OpenRA GUI-differential exception carries its own labelled
        # provenance and is deliberately not an upstream test file.
        if str(test.get("origin") or "") != "upstream":
            continue
        # A row that ships frozen test bytes is already bound by them.
        if test.get("frozen_test_sources"):
            continue
        path = str(test.get("upstream_test_path") or "")
        if not path:
            unresolved.append(f"{test.get('id')!r} declares no test path")
            continue
        source = (repo / path).resolve()
        if not source.is_file():
            unresolved.append(path)
            continue
        test["upstream_test_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        test["source_url"] = _blob_url(
            authority=authority,
            repository=repository,
            revision=revision,
            path=path,
        )
    bound.pop("record_sha256", None)
    bound["record_sha256"] = _canonical_sha256(bound)
    return bound, unresolved


def _write_json(path: Path, payload: dict) -> None:
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

    changed: list[str] = []
    already: list[str] = []
    blocked: dict[str, list[str]] = {}
    rejected: dict[str, list[str]] = {}
    for row in catalog["tasks"]:
        task_id = str(row["id"])
        repo = workspace / str(row["repo"])
        revision = str(hosts[str(row["benchmark"])]["revision"])
        record_path = workspace / str(row["defect"]["upstream_record"])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        frozen_root = record_path.parent.parent

        bound, unresolved = bind_record(record, repo=repo, revision=revision)
        if unresolved:
            blocked[task_id] = unresolved
            continue
        errors = validate_lazarus_trial_provenance(
            bound,
            source_root=repo,
            harness_root=workspace,
            frozen_test_root=frozen_root,
        )
        if errors:
            rejected[task_id] = errors
            continue
        if bound == record:
            already.append(task_id)
            continue
        changed.append(task_id)
        if not args.check:
            _write_json(record_path, bound)

    print(
        json.dumps(
            {
                "mode": "check" if args.check else "write",
                "already_bound": len(already),
                "rewritten" if not args.check else "would_rewrite": changed,
                "blocked_on_absent_test_source": blocked,
                "rejected_after_binding": rejected,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    if blocked or rejected:
        return 1
    if args.check and changed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
