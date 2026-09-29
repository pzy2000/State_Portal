#!/usr/bin/env python3
"""Write or audit stable C1 repository enumerations."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from warptest.c1_lazarus_selection.enumeration import (
    RepositoryEnumeration,
    load_repository_enumeration,
    write_enumeration_audit_population,
    write_repository_enumeration,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import AuthorizationRecord


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--population-json", type=Path)
    parser.add_argument("--write-audits", action="store_true")
    parser.add_argument("--authorization-id", required=True)
    arguments = parser.parse_args()
    authorization = AuthorizationRecord(
        authorization_id=arguments.authorization_id,
        scope="repository-enumeration",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )
    if arguments.population_json is not None:
        value = json.loads(arguments.population_json.read_text(encoding="utf-8"))
        if (
            type(value) is not dict
            or value.get("repository_key") != arguments.repository
        ):
            raise RuntimeError("enumeration population input is invalid")
        enumeration = RepositoryEnumeration(
            repository_key=arguments.repository,
            merged_change_numbers=_numbers(value, "merged_change_numbers"),
            authoritative_issue_link_missing=_numbers(
                value, "authoritative_issue_link_missing"
            ),
            repair_not_in_pinned=_numbers(value, "repair_not_in_pinned"),
            candidates=tuple(
                CandidateKey(arguments.repository, number)
                for number in _numbers(value, "candidate_numbers")
            ),
        )
        write_repository_enumeration(
            arguments.artifact_root,
            enumeration,
            authorization=authorization,
        )
    enumeration = load_repository_enumeration(
        arguments.artifact_root,
        repository_key=arguments.repository,
    )
    result: dict[str, object] = {
        "repository_key": enumeration.repository_key,
        "merged_count": len(enumeration.merged_change_numbers),
        "missing_relationship_count": len(enumeration.authoritative_issue_link_missing),
        "absent_repair_count": len(enumeration.repair_not_in_pinned),
        "deep_screen_count": len(enumeration.candidates),
    }
    if arguments.write_audits:
        audit = write_enumeration_audit_population(
            arguments.artifact_root,
            enumeration,
            authorization=authorization,
            persistence_workers=2,
        )
        result["audit_count"] = audit.audit_count
        result["terminal_count"] = audit.terminal_count
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _numbers(value: dict[str, object], key: str) -> tuple[int, ...]:
    raw = value.get(key)
    if type(raw) is not list or any(type(number) is not int for number in raw):
        raise RuntimeError(f"enumeration population {key} is invalid")
    return tuple(raw)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
