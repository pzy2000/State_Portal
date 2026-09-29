#!/usr/bin/env python3
"""Offline-reproject persisted Luanti native v3 receipts into v4 artifacts."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from warptest.c1_lazarus_selection.luanti_native_receipt_reprojection import (
    reproject_luanti_native_receipts,
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
        scope="preannotation-reprojection",
        granted_at=datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        local_user_id=os.getuid(),
    )
    result = reproject_luanti_native_receipts(
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
                "created_attempts": result.created_attempts,
                "qualified": result.qualified,
                "resumed_attempts": result.resumed_attempts,
            },
            sort_keys=True,
        )
    )
    return 0 if result.qualified else 2


if __name__ == "__main__":
    raise SystemExit(main())
