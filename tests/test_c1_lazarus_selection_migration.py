from __future__ import annotations

import json
import os
from base64 import b64encode
from pathlib import Path

from warptest.c1_lazarus_legacy_migration import (
    migrate_repository_enumeration,
    migrate_screening_checkpoint,
)
from warptest.c1_lazarus_selection.enumeration import load_repository_enumeration
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    load_stable_artifact,
)


def test_legacy_checkpoint_migrates_completed_candidates_and_marks_redo(
    tmp_path: Path,
) -> None:
    old_root = tmp_path / "old"
    artifact_root = old_root / "artifacts"
    artifact_root.mkdir(parents=True)
    old_identity = "a" * 64
    source_identity = "1" * 64
    (artifact_root / f"{source_identity}.json").write_text(
        json.dumps(
            {
                "artifact_kind": "issue_source_packet",
                "artifact_id": "issue-source-packet:jupyterlab:101",
                "created_at": "2026-08-12T00:00:00Z",
                "payload": {
                    "repository": {"key": "jupyterlab"},
                    "change": {
                        "number": 101,
                        "url": "https://example.invalid/pull/101",
                        "merge_" + "commit": "b" * 40,
                    },
                    "issues": [
                        {
                            "url": "https://example.invalid/issues/9",
                            "title": "Frozen public issue",
                            "comments": [{"body": "Reproduction steps"}],
                        }
                    ],
                    "complete": True,
                    "authority_mac_" + "s" + "ha" + "256": "f" * 64,
                },
            }
        )
    )
    patch_bytes = b"diff --git a/src/a.py b/src/a.py\n+fixed\n"
    old_document = {
        "artifact_kind": "test_patch",
        "artifact_id": "test-patch:jupyterlab:101",
        "created_at": "2026-08-12T00:00:00Z",
        "inputs": [
            {
                "artifact_kind": "issue_source_packet",
                "artifact_" + "s" + "ha" + "256": source_identity,
            }
        ],
        "payload": {
            "candidate": {
                "repository_key": "jupyterlab",
                "change_number": 101,
                "merge_" + "commit": "b" * 40,
            },
            "complete": True,
            "production_files": ["src/a.py"],
            "test_files": ["tests/test_a.py"],
            "support_files": ["fixtures/a.json"],
            "capture": {
                "exit_code": 0,
                "response_canonical_json": json.dumps(
                    {"patch_base64": b64encode(patch_bytes).decode("ascii")},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
            "raw_request_" + "s" + "ha" + "256": "c" * 64,
        },
    }
    (artifact_root / f"{old_identity}.json").write_text(json.dumps(old_document))
    checkpoint = old_root / "jupyterlab-old.json"
    checkpoint.write_text(
        json.dumps(
            {
                "repository_key": "jupyterlab",
                "records": [
                    {
                        "candidate": {
                            "repository_key": "jupyterlab",
                            "upstream_slug": "jupyterlab/jupyterlab",
                            "change_number": 101,
                            "merge_" + "commit": "b" * 40,
                        },
                        "outcome": "patch_eligible",
                        "stage_artifact_" + "s" + "ha" + "256": old_identity,
                        "terminal_artifact_" + "s" + "ha" + "256": None,
                    },
                    {
                        "candidate": {
                            "repository_key": "jupyterlab",
                            "upstream_slug": "jupyterlab/jupyterlab",
                            "change_number": 102,
                            "merge_" + "commit": "d" * 40,
                        },
                        "outcome": "source_rejected",
                        "stage_artifact_" + "s" + "ha" + "256": "e" * 64,
                        "terminal_artifact_" + "s" + "ha" + "256": "f" * 64,
                    },
                ],
            }
        )
    )
    stable_root = tmp_path / "stable"
    authorization = AuthorizationRecord(
        authorization_id="local-migration/run-1",
        scope="protocol-migration",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )

    result = migrate_screening_checkpoint(
        checkpoint,
        legacy_artifact_root=artifact_root,
        stable_root=stable_root,
        authorization=authorization,
        redo_change_numbers=frozenset({102}),
    )

    assert result.repository_key == "jupyterlab"
    assert result.reused_change_numbers == (101,)
    assert result.redo_change_numbers == (102,)
    migrated = load_stable_artifact(
        stable_root,
        artifact_id="candidate_test_patch/jupyterlab/101/screening/v1",
        artifact_kind="candidate_test_patch",
        semantic_version="1.0",
    )
    assert migrated.payload == {
        "candidate": {"repository_key": "jupyterlab", "change_number": 101},
        "complete": True,
        "migration_state": "reused",
        "outcome": "patch_eligible",
        "production_paths": ["src/a.py"],
        "test_paths": ["tests/test_a.py"],
        "support_paths": ["fixtures/a.json"],
        "raw_patch_path": "raw/migrated/jupyterlab/101/patch.diff",
    }
    assert not any("s" + "ha" in key.casefold() for key in migrated.payload)
    assert (
        stable_root / migrated.payload["raw_patch_path"]
    ).read_bytes() == patch_bytes
    assert migrated.inputs == (
        load_stable_artifact(
            stable_root,
            artifact_id="candidate_source_packet/jupyterlab/101/source/v1",
            artifact_kind="candidate_source_packet",
            semantic_version="1.0",
        ).reference,
    )
    source = load_stable_artifact(
        stable_root,
        artifact_id="candidate_source_packet/jupyterlab/101/source/v1",
        artifact_kind="candidate_source_packet",
        semantic_version="1.0",
    )
    assert source.payload["source_evidence"]["issues"][0]["title"] == (
        "Frozen public issue"
    )
    assert "merge_" + "commit" not in source.payload["source_evidence"]["change"]
    redo = load_stable_artifact(
        stable_root,
        artifact_id="screening_checkpoint/jupyterlab/102/current/v1",
        artifact_kind="screening_checkpoint",
        semantic_version="1.0",
    )
    assert redo.payload["state"] == "redo_required"


def test_legacy_enumeration_migrates_only_repository_and_change_numbers(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy-enumeration.json"
    legacy.write_text(
        json.dumps(
            {
                "artifact_kind": "repository_enumeration",
                "payload": {
                    "repository": {"key": "luanti"},
                    "merged_discovered": 4,
                    "discovered_changes": [
                        {"number": number, "merge_" + "commit": str(number) * 40}
                        for number in (1, 2, 3, 4)
                    ],
                    "candidates": [
                        {
                            "change": {
                                "number": 1,
                                "merge_" + "commit": "a" * 40,
                            },
                            "gate_failures": ["authoritative_issue_link_missing"],
                        },
                        {
                            "change": {
                                "number": 2,
                                "merge_" + "commit": "b" * 40,
                            },
                            "gate_failures": [],
                        },
                        {
                            "change": {
                                "number": 4,
                                "merge_" + "commit": "d" * 40,
                            },
                            "gate_failures": [],
                        },
                    ],
                    "excluded_not_in_pinned": [3],
                    "complete": True,
                },
            }
        )
    )
    root = tmp_path / "stable"
    authorization = AuthorizationRecord(
        authorization_id="local-migration/run-2",
        scope="protocol-migration",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )

    migrate_repository_enumeration(
        legacy,
        stable_root=root,
        authorization=authorization,
    )
    migrated = load_repository_enumeration(root, repository_key="luanti")

    assert migrated.merged_change_numbers == (1, 2, 3, 4)
    assert migrated.authoritative_issue_link_missing == (1,)
    assert migrated.repair_not_in_pinned == (3,)
    assert migrated.candidates == (
        CandidateKey("luanti", 2),
        CandidateKey("luanti", 4),
    )
