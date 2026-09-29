#!/usr/bin/env python3
"""Run one explicitly confirmed closed Luanti native regression profile."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from warptest.c1_lazarus_selection.luanti_native_executor import (
    execute_luanti_native_regression,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import AuthorizationRecord


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument(
        "--change-number", type=int, choices=(15519, 15495), required=True
    )
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    authorization = AuthorizationRecord(
        authorization_id=args.authorization_id,
        scope="preannotation-execution",
        granted_at=datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        local_user_id=os.getuid(),
    )
    result = execute_luanti_native_regression(
        args.artifact_root,
        candidate=CandidateKey("luanti", args.change_number),
        authorization=authorization,
        confirmation=args.confirm,
    )
    print(
        json.dumps(
            {
                "aggregate_id": result.aggregate.reference.artifact_id,
                "candidate": result.candidate.to_value(),
                "executed_attempts": result.executed_attempts,
                "executed_build_steps": result.executed_build_steps,
                "qualified": result.qualified,
                "resumed_attempts": result.resumed_attempts,
            },
            sort_keys=True,
        )
    )
    return 0 if result.qualified else 2


if __name__ == "__main__":
    raise SystemExit(main())
