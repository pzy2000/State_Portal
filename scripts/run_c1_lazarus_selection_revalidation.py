#!/usr/bin/env python3
"""Run resumable curated fixed/buggy construction revalidation."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from warptest.c1_lazarus_selection.curated import load_curated_catalog
from warptest.c1_lazarus_selection.revalidation import (
    run_curated_construction_revalidation,
)
from warptest.c1_lazarus_selection.stable_protocol import AuthorizationRecord


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path(".warptest-cache/c1_lazarus_selection_v1"),
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("benchmarks/p5/lazarus/curated.yaml"),
    )
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    parser.add_argument("--task-id", action="append")
    parser.add_argument("--timeout-s", type=float, default=600.0)
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args()
    tasks = load_curated_catalog(
        arguments.catalog,
        workspace_root=arguments.workspace_root,
    )
    selected = set(arguments.task_id or (task.task_id for task in tasks))
    unknown = selected - {task.task_id for task in tasks}
    if unknown:
        parser.error("unknown task IDs: " + ", ".join(sorted(unknown)))
    now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    authorization = AuthorizationRecord(
        authorization_id="authorization/curated-revalidation/local/v1",
        scope="candidate-screening",
        granted_at=now,
        local_user_id=os.getuid(),
    )
    rows = []
    for task in tasks:
        if task.task_id not in selected:
            continue
        try:
            artifact = run_curated_construction_revalidation(
                arguments.artifact_root,
                task_id=task.task_id,
                catalog_path=arguments.catalog,
                workspace_root=arguments.workspace_root,
                curated_selection_artifact_id=(
                    "curated_selection/all/current/v1"
                ),
                authorization=authorization,
                total_timeout_s=arguments.timeout_s,
                resume_existing=not arguments.force,
            )
            rows.append(
                {
                    "task_id": task.task_id,
                    "outcome": artifact.payload["outcome"],
                    "complete": artifact.payload["complete"],
                    "artifact_id": artifact.reference.artifact_id,
                }
            )
        except Exception as error:
            rows.append(
                {
                    "task_id": task.task_id,
                    "outcome": "infrastructure_blocked",
                    "complete": False,
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
    print(json.dumps({"results": rows}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(row["complete"] for row in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
