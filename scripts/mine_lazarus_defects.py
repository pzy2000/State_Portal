#!/usr/bin/env python3
"""Scan and freeze real C1-Lazarus defect provenance."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_mining import (  # noqa: E402
    FORMAL_UPSTREAM_REPOSITORIES,
    REPOSITORY_PATHS,
    UPSTREAM_REPOSITORIES,
    fetch_authoritative_conversation,
    freeze_upstream_bundle,
    scan_repository,
)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_tests(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("tests")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("test definition file must contain a JSON tests list")
    return value


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="scan six local histories")
    scan.add_argument(
        "--benchmark",
        action="append",
        choices=tuple(UPSTREAM_REPOSITORIES),
        help="repeat to restrict the scan; default is all six",
    )
    scan.add_argument("--limit", type=int, default=2_000)
    scan.add_argument(
        "--output", type=Path, default=Path("reports/lazarus/mining_candidates.json")
    )

    freeze = subparsers.add_parser("freeze", help="freeze one qualified candidate")
    freeze.add_argument("--task-id", required=True)
    freeze.add_argument("--benchmark", required=True, choices=tuple(UPSTREAM_REPOSITORIES))
    freeze.add_argument("--patch-base", required=True)
    freeze.add_argument("--patch-head", required=True)
    freeze.add_argument("--issue-number", type=int)
    freeze.add_argument("--pr-number", type=int)
    freeze.add_argument("--tests", required=True, type=Path)
    freeze.add_argument("--observable", action="store_true", required=True)
    freeze.add_argument("--destination", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    if args.command == "scan":
        benchmarks = args.benchmark or list(FORMAL_UPSTREAM_REPOSITORIES)
        payload = {
            "schema": "c1-lazarus-mining-candidates-v1",
            "schema_version": 1,
            "workspace": str(workspace),
            "repositories": {
                benchmark: {
                    "authority": UPSTREAM_REPOSITORIES[benchmark][0],
                    "upstream": UPSTREAM_REPOSITORIES[benchmark][1],
                    "path": REPOSITORY_PATHS[benchmark],
                    "candidates": scan_repository(
                        benchmark,
                        workspace / REPOSITORY_PATHS[benchmark],
                        limit=args.limit,
                    ),
                }
                for benchmark in benchmarks
            },
        }
        output = args.output
        if not output.is_absolute():
            output = workspace / output
        _atomic_json(output.resolve(), payload)
        print(json.dumps({"output": str(output.resolve()), "benchmarks": benchmarks}))
        return 0

    conversation = fetch_authoritative_conversation(
        args.benchmark,
        issue_number=args.issue_number,
        pr_number=args.pr_number,
    )
    tests_path = args.tests if args.tests.is_absolute() else workspace / args.tests
    destination = args.destination or Path(
        f"benchmarks/p5/lazarus/defects/{args.task_id}"
    )
    if not destination.is_absolute():
        destination = workspace / destination
    result = freeze_upstream_bundle(
        task_id=args.task_id,
        benchmark=args.benchmark,
        repo_path=workspace / REPOSITORY_PATHS[args.benchmark],
        patch_base=args.patch_base,
        patch_head=args.patch_head,
        issue=conversation["issue"],
        pull_request=conversation["pull_request"],
        comments=conversation["comments"],
        reviews=conversation["reviews"],
        tests=_load_tests(tests_path.resolve()),
        retrieved_at=conversation["retrieved_at"],
        destination=destination,
        observable=args.observable,
        upstream_commits=conversation.get("upstream_commits", []),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
