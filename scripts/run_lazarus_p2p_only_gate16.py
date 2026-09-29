#!/usr/bin/env python3
"""Run Gate 1--6 for the registered P2P-only candidates.

Each task recomputes Gate 1--5 from its frozen captures, materializes the
fixed/buggy pair from the pinned checkout, and runs the registered repository
control test three times on each version.  Six passing cells establish that both
products run; they do not establish a defect, which is why the admission this
writes authorizes frame capture and nothing further.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from warptest.c1_lazarus_selection.p2p_only_gate16 import (  # noqa: E402
    CONFIRMATION,
    Gate16Error,
    execute_gate_1_6,
)
from warptest.c1_lazarus_selection.p2p_only_profiles import (  # noqa: E402
    load_profile_registry,
)
from warptest.c1_lazarus_selection.stable_protocol import (  # noqa: E402
    AuthorizationRecord,
)

ARTIFACT_ROOT = Path(".warptest-cache/lazarus-p2p-only/artifacts")
REPORT_PATH = Path("reports/lazarus-p2p-only-gate16.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append", help="restrict to one task id")
    parser.add_argument("--artifact-root", type=Path, default=ARTIFACT_ROOT)
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    parser.add_argument(
        "--keep-trees",
        action="store_true",
        help="keep the materialized buggy tree for frame capture",
    )
    args = parser.parse_args()

    artifact_root = args.artifact_root
    if not artifact_root.is_absolute():
        artifact_root = REPOSITORY_ROOT / artifact_root
    artifact_root.mkdir(parents=True, exist_ok=True)

    authorization = AuthorizationRecord(
        authorization_id="lazarus-p2p-only/gate16",
        scope="candidate-screening",
        granted_at=datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        local_user_id=os.getuid(),
    )

    wanted = set(args.task or ())
    profiles = [
        profile
        for profile in load_profile_registry(REPOSITORY_ROOT)
        if (
            profile.task_id in wanted
            if wanted
            else profile.catalog_admission
        )
    ]
    if not profiles:
        print("no registered profile matched", file=sys.stderr)
        return 1

    results: list[dict[str, object]] = []
    for profile in profiles:
        print(f"== {profile.task_id} ==")
        try:
            result = execute_gate_1_6(
                artifact_root,
                repository_root=REPOSITORY_ROOT,
                profile=profile,
                authorization=authorization,
                confirmation=CONFIRMATION,
                keep_trees=args.keep_trees,
            )
        except (Gate16Error, PermissionError) as error:
            print(f"  BLOCKED {error}")
            results.append(
                {
                    "task_id": profile.task_id,
                    "strict_gate_1_6": False,
                    "error": str(error),
                }
            )
            continue
        status = "PASS" if result.gate_1_6_complete else "BLOCKED"
        cells = result.aggregate.payload.get("cells")
        print(f"  {status} {result.executed_cells} cells")
        for cell in cells if isinstance(cells, list) else ():
            print(f"    {cell['variant']:>5}: {cell['observed_outcomes']}")
        results.append(
            {
                "task_id": profile.task_id,
                "strict_gate_1_6": result.gate_1_6_complete,
                "outcome": result.transition.payload.get("outcome"),
                "gate_1_6_status": result.transition.payload.get("gate_1_6_status"),
                "aggregate": result.aggregate.reference.artifact_id,
                "transition": result.transition.reference.artifact_id,
                "admission": (
                    result.admission.reference.artifact_id
                    if result.admission is not None
                    else None
                ),
                "cells": cells,
            }
        )

    output = args.output
    if not output.is_absolute():
        output = REPOSITORY_ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "c1-lazarus-p2p-only-gate16-v1",
        "schema_version": 1,
        "passed": sum(1 for item in results if item.get("strict_gate_1_6")),
        "total": len(results),
        "results": results,
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"\n{payload['passed']}/{payload['total']} passed Gate 1--6")
    print(f"wrote {output.relative_to(REPOSITORY_ROOT)}")
    return 0 if payload["passed"] == payload["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
