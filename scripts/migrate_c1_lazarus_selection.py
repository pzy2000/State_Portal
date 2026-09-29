#!/usr/bin/env python3
"""Explicit read-only migration entry point for retired C1 artifacts."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from warptest.c1_lazarus_legacy_migration import (
    migrate_repository_enumeration,
    migrate_screening_checkpoint,
)
from warptest.c1_lazarus_selection.stable_protocol import AuthorizationRecord


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--legacy-artifact-root", type=Path, required=True)
    parser.add_argument("--legacy-enumeration", type=Path, required=True)
    parser.add_argument("--legacy-checkpoint", type=Path)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--redo-number", type=int, action="append", default=[])
    arguments = parser.parse_args()
    authorization = AuthorizationRecord(
        authorization_id=arguments.authorization_id,
        scope="protocol-migration",
        granted_at=datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        local_user_id=os.getuid(),
    )
    enumeration_path = migrate_repository_enumeration(
        arguments.legacy_enumeration,
        stable_root=arguments.artifact_root,
        authorization=authorization,
    )
    result: dict[str, object] = {"enumeration_path": str(enumeration_path)}
    if arguments.legacy_checkpoint is not None:
        migration = migrate_screening_checkpoint(
            arguments.legacy_checkpoint,
            legacy_artifact_root=arguments.legacy_artifact_root,
            stable_root=arguments.artifact_root,
            authorization=authorization,
            redo_change_numbers=frozenset(arguments.redo_number),
        )
        result.update(
            {
                "repository_key": migration.repository_key,
                "reused_count": len(migration.reused_change_numbers),
                "redo_count": len(migration.redo_change_numbers),
                "batch_artifact_path": str(migration.batch_artifact_path),
            }
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
