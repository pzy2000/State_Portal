#!/usr/bin/env python3
"""Write the explicitly relaxed 24-task C1-Lazarus selection."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path

from warptest.c1_lazarus_selection.curated import write_curated_selection
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
    arguments = parser.parse_args()
    now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    authorization = AuthorizationRecord(
        authorization_id="authorization/curated/local/v1",
        scope="screening",
        granted_at=now,
        local_user_id=os.getuid(),
    )
    artifact = write_curated_selection(
        arguments.artifact_root,
        catalog_path=arguments.catalog,
        workspace_root=arguments.workspace_root,
        strict_selection_artifact_id="population_selection/all/current/v1",
        authorization=authorization,
    )
    print(artifact.reference.artifact_id)
    print(artifact.revision_path)
    print(f"selected_task_count={artifact.payload['selected_task_count']}")
    print(f"selection_status={artifact.payload['selection_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
