from __future__ import annotations

from datetime import UTC, datetime, timedelta

import base64
import json
import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_lua_admission import (
    project_luanti_lua_regression_to_v2,
    reproject_qualified_luanti_lua_admission,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.source_atomicity import (
    load_source_atomicity_batch_context,
    write_source_atomicity_artifact,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-lua-admission/test",
        scope="preannotation-execution",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _screening_authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-lua-admission/source-atomicity-test",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _after_now() -> str:
    """Return a timestamp just after the projection's own real-clock stamp."""

    moment = datetime.now(UTC) + timedelta(seconds=1)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write(
    root: Path,
    *,
    artifact_id: str,
    artifact_kind: str,
    semantic_version: str,
    payload: dict[str, object],
    inputs: tuple[ArtifactReference, ...] = (),
    created_at: str = "2026-08-13T00:00:00Z",
) -> StableArtifact:
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        semantic_version=semantic_version,
        created_at=created_at,
        inputs=inputs,
        payload=payload,
        authorization=_authorization(),
    )
    return load_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        semantic_version=semantic_version,
    )


def _write_screening_and_manifest(
    root: Path, candidate: CandidateKey
) -> tuple[StableArtifact, StableArtifact]:
    patch_path = Path("raw/luanti") / str(candidate.change_number) / "pull.diff"
    manifest_path = Path("raw/luanti") / str(candidate.change_number) / "source.json"
    full_patch = root / patch_path
    full_patch.parent.mkdir(parents=True, exist_ok=True)
    full_patch.write_text(
        """diff --git a/builtin/common/misc_helpers.lua b/builtin/common/misc_helpers.lua
--- a/builtin/common/misc_helpers.lua
+++ b/builtin/common/misc_helpers.lua
@@ -1 +1 @@
-return old_value
+return new_value
diff --git a/builtin/common/tests/misc_helpers_spec.lua b/builtin/common/tests/misc_helpers_spec.lua
--- a/builtin/common/tests/misc_helpers_spec.lua
+++ b/builtin/common/tests/misc_helpers_spec.lua
@@ -1 +1,2 @@
 describe('regression', function()
+it('covers the defect', function() assert(true) end)
""",
        encoding="utf-8",
    )
    full_manifest = root / manifest_path
    full_manifest.write_text(
        json.dumps(
            {
                "candidate": candidate.to_value(),
                "provider": "github",
                "authority": {
                    "provider": "github",
                    "api_base": "https://api.github.com",
                    "repository": "luanti-org/luanti",
                },
                "pull": {
                    "number": candidate.change_number,
                    "merged": True,
                    "merged_at": "2026-08-01T00:00:00Z",
                    "title": "Repair one Lua regression",
                    "body": "Fixes #7.",
                },
                "relationships": [],
                "issue_numbers": [7],
                "issues": [
                    {
                        "number": 7,
                        "issue": {
                            "number": 7,
                            "title": "One Lua regression",
                            "body": "Public defect report.",
                            "created_at": "2026-07-01T00:00:00Z",
                        },
                        "comments": [],
                        "history": [],
                    }
                ],
                "raw_paths": [patch_path.as_posix()],
                "request_receipt_paths": [],
                "patch_raw_path": patch_path.as_posix(),
                "attachment_paths": [],
                "complete": True,
            }
        ),
        encoding="utf-8",
    )
    enumeration = _write(
        root,
        artifact_id="repository_enumeration/luanti/current/v2",
        artifact_kind="repository_enumeration",
        semantic_version="2.0",
        payload={
            "repository_key": "luanti",
            "merged_change_numbers": [candidate.change_number],
            "authoritative_issue_link_missing": [],
            "repair_not_in_pinned": [],
            "infrastructure_blocked": [],
            "candidates": [candidate.to_value()],
            "complete": True,
        },
    )
    source = _write(
        root,
        artifact_id=(
            f"candidate_source_packet/luanti/{candidate.change_number}/source/v2"
        ),
        artifact_kind="candidate_source_packet",
        semantic_version="2.0",
        inputs=(enumeration.reference,),
        payload={
            "candidate": candidate.to_value(),
            "raw_evidence_path": manifest_path.as_posix(),
            "complete": True,
        },
    )
    patch = _write(
        root,
        artifact_id=(
            f"candidate_test_patch/luanti/{candidate.change_number}/screening/v2"
        ),
        artifact_kind="candidate_test_patch",
        semantic_version="2.0",
        inputs=(enumeration.reference, source.reference),
        payload={
            "candidate": candidate.to_value(),
            "outcome": "patch_eligible",
            "production_paths": ["builtin/common/misc_helpers.lua"],
            "test_paths": ["builtin/common/tests/misc_helpers_spec.lua"],
            "source_manifest_path": manifest_path.as_posix(),
            "raw_patch_path": patch_path.as_posix(),
            "complete": True,
        },
    )
    _write(
        root,
        artifact_id=f"screening_checkpoint/luanti/{candidate.change_number}/current/v2",
        artifact_kind="screening_checkpoint",
        semantic_version="2.0",
        inputs=(enumeration.reference, patch.reference),
        payload={
            "candidate": candidate.to_value(),
            "state": "complete",
            "outcome": "patch_eligible",
            "stage_artifact_id": patch.reference.artifact_id,
        },
    )
    atomicity_context = load_source_atomicity_batch_context(
        root,
        repository_key="luanti",
    )
    atomicity = write_source_atomicity_artifact(
        batch_context=atomicity_context,
        candidate=candidate,
        created_at="2026-08-13T00:00:01Z",
        authorization=_screening_authorization(),
    )
    manifest = _write(
        root,
        artifact_id=f"candidate_stage/luanti/{candidate.change_number}/test_manifest/v2",
        artifact_kind="candidate_stage",
        semantic_version="2.0",
        inputs=(patch.reference, atomicity.artifact.reference),
        payload={
            "candidate": candidate.to_value(),
            "stage": "test_manifest",
            "state": "passed",
            "outcome": None,
            "details": {
                "executable_test_paths": ["builtin/common/tests/misc_helpers_spec.lua"],
                "test_count": 1,
                "execution_candidate_kind": "added_assertion",
            },
            "complete": True,
        },
    )
    checkpoint = _write(
        root,
        artifact_id=(
            f"preannotation_checkpoint/luanti/{candidate.change_number}/current/v2"
        ),
        artifact_kind="preannotation_checkpoint",
        semantic_version="2.0",
        inputs=(manifest.reference,),
        payload={
            "candidate": candidate.to_value(),
            "completed_stages": ["test_manifest"],
            "state": "active",
            "outcome": None,
            "current_artifact_id": manifest.reference.artifact_id,
        },
    )
    return source, checkpoint


def _write_regression_evidence(
    root: Path,
    candidate: CandidateKey,
    source: StableArtifact,
    *,
    qualified: bool = True,
    aggregate_inputs: tuple[ArtifactReference, ...] | None = None,
) -> tuple[StableArtifact, tuple[StableArtifact, ...], StableArtifact]:
    profile_id = f"luanti-{candidate.change_number}-profile-v1"
    relative_tree = (
        Path("private/fixed-capsules/luanti")
        / str(candidate.change_number)
        / "fixed-source-v2"
    )
    tree = root / relative_tree
    for relative, content in {
        "builtin/common/misc_helpers.lua": "return new_value\n",
        "builtin/common/tests/misc_helpers_spec.lua": "assert(true)\n",
        "builtin/common/vector.lua": "return {}\n",
    }.items():
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    capsule = _write(
        root,
        artifact_id=(f"fixed_source_capsule/luanti/{candidate.change_number}/fixed/v2"),
        artifact_kind="fixed_source_capsule",
        semantic_version="2.0",
        inputs=(source.reference,),
        payload={
            "candidate": candidate.to_value(),
            "profile_id": profile_id,
            "source_tree_path": relative_tree.as_posix(),
            "support_paths": ["builtin/common/vector.lua"],
            "test_paths": ["builtin/common/tests/misc_helpers_spec.lua"],
        },
    )
    attempts = []
    for phase in ("f2p", "p2p"):
        for variant in ("fixed", "buggy"):
            for repetition in (1, 2, 3):
                expected = "pass"
                if phase == "f2p" and variant == "buggy":
                    expected = (
                        "timed_out" if candidate.change_number == 13132 else "failure"
                    )
                observed = expected
                if not qualified and phase == "f2p" and variant == "buggy":
                    observed = "pass"
                if observed == "timed_out":
                    process_outcome = "timed_out"
                    returncode = -9
                    completed_test_count = None
                    summary = b""
                else:
                    process_outcome = "completed"
                    returncode = 0 if observed == "pass" else 1
                    completed_test_count = 1
                    summary = (
                        b"1 success / 0 failures / 0 errors / 0 pending\n"
                        if observed == "pass"
                        else b"0 successes / 1 failure / 0 errors / 0 pending\n"
                    )
                attempts.append(
                    _write(
                        root,
                        artifact_id=(
                            f"lua_regression_attempt/luanti/{candidate.change_number}/"
                            f"{phase}/{variant}/{repetition}/v3"
                        ),
                        artifact_kind="lua_regression_attempt",
                        semantic_version="3.0",
                        inputs=(capsule.reference,),
                        payload={
                            "candidate": candidate.to_value(),
                            "profile_id": profile_id,
                            "phase": phase,
                            "variant": variant,
                            "repetition": repetition,
                            "selector": "registered defect selector",
                            "test_path": "builtin/common/tests/misc_helpers_spec.lua",
                            "expected_outcome": expected,
                            "observed_outcome": observed,
                            "process_outcome": process_outcome,
                            "returncode": returncode,
                            "completed_test_count": completed_test_count,
                            "stdout_base64": base64.b64encode(summary).decode("ascii"),
                            "stderr_base64": "",
                            "stdout_truncated": False,
                            "stderr_truncated": False,
                            "output_encoding": "base64",
                            "matched_expected": observed == expected,
                        },
                    )
                )
    cells = []
    for phase in ("f2p", "p2p"):
        for variant in ("buggy", "fixed"):
            group = [
                item
                for item in attempts
                if item.payload["phase"] == phase and item.payload["variant"] == variant
            ]
            cells.append(
                {
                    "phase": phase,
                    "variant": variant,
                    "observed_outcomes": [
                        item.payload["observed_outcome"] for item in group
                    ],
                }
            )
    inputs = (
        tuple(item.reference for item in attempts)
        if aggregate_inputs is None
        else aggregate_inputs
    )
    aggregate = _write(
        root,
        artifact_id=(
            f"lua_regression_aggregate/luanti/{candidate.change_number}/fixed-buggy/v3"
        ),
        artifact_kind="lua_regression_aggregate",
        semantic_version="3.0",
        inputs=inputs,
        payload={
            "candidate": candidate.to_value(),
            "profile_id": profile_id,
            "repetitions_per_cell": 3,
            "attempt_count": 12,
            "stable_repetitions": True,
            "all_expected": qualified,
            "qualified": qualified,
            "cells": sorted(cells, key=lambda row: (row["phase"], row["variant"])),
        },
    )
    return capsule, tuple(attempts), aggregate


def test_qualified_v3_aggregate_projects_formal_v2_repetitions_and_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("luanti", 13132)
    source, _checkpoint = _write_screening_and_manifest(root, candidate)
    capsule, attempts, aggregate = _write_regression_evidence(root, candidate, source)
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {"may_start_build": True, "may_start_candidate": True, "may_write": True},
        )(),
    )

    result = project_luanti_lua_regression_to_v2(
        root,
        candidate=candidate,
        authorization=_authorization(),
    )

    assert result.qualified is True
    assert result.next_gate == "version_materialization"
    assert result.stage.payload["state"] == "passed"
    assert result.checkpoint.payload == {
        "candidate": candidate.to_value(),
        "completed_stages": ["test_manifest", "test_repetitions"],
        "state": "active",
        "outcome": None,
        "current_artifact_id": result.stage.reference.artifact_id,
    }
    assert result.transition.inputs == (
        result.manifest.reference,
        capsule.reference,
        *(item.reference for item in attempts),
        aggregate.reference,
    )
    assert result.stage.inputs == (
        result.manifest.reference,
        result.transition.reference,
    )
    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        load_stable_artifact(
            root,
            artifact_id=(
                f"candidate_stage/luanti/{candidate.change_number}/"
                "version_materialization/v2"
            ),
            artifact_kind="candidate_stage",
            semantic_version="2.0",
        )

    revision = result.transition.revision_path
    resumed = project_luanti_lua_regression_to_v2(
        root,
        candidate=candidate,
        authorization=_authorization(),
    )
    assert resumed.resumed is True
    assert resumed.transition.revision_path == revision


def test_unqualified_v3_aggregate_becomes_terminal_not_materialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("luanti", 14757)
    source, _checkpoint = _write_screening_and_manifest(root, candidate)
    _write_regression_evidence(root, candidate, source, qualified=False)
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {"may_start_build": True, "may_start_candidate": True, "may_write": True},
        )(),
    )

    result = project_luanti_lua_regression_to_v2(
        root,
        candidate=candidate,
        authorization=_authorization(),
    )

    assert result.qualified is False
    assert result.next_gate is None
    assert result.stage.payload["state"] == "terminal"
    assert result.stage.payload["outcome"] == "quality_rejected"
    assert result.checkpoint.payload["state"] == "terminal"


def test_projection_rejects_detached_or_missing_v3_attempts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("luanti", 13132)
    source, _checkpoint = _write_screening_and_manifest(root, candidate)
    capsule, _attempts, _aggregate = _write_regression_evidence(
        root,
        candidate,
        source,
        aggregate_inputs=(),
    )
    assert capsule.reference.semantic_version == "2.0"

    with pytest.raises(RuntimeError, match="aggregate attempt closure"):
        project_luanti_lua_regression_to_v2(
            root,
            candidate=candidate,
            authorization=_authorization(),
        )


def test_projection_rejects_capsule_detached_from_current_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("luanti", 13132)
    source, _checkpoint = _write_screening_and_manifest(root, candidate)
    capsule, _attempts, _aggregate = _write_regression_evidence(root, candidate, source)
    _write(
        root,
        artifact_id=capsule.reference.artifact_id,
        artifact_kind=capsule.reference.artifact_kind,
        semantic_version=capsule.reference.semantic_version,
        created_at="2026-08-14T00:00:01Z",
        inputs=(),
        payload=dict(capsule.payload),
    )

    with pytest.raises(RuntimeError, match="capsule source prerequisite"):
        project_luanti_lua_regression_to_v2(
            root,
            candidate=candidate,
            authorization=_authorization(),
        )


def test_projection_rejects_attempt_with_forged_process_truth(tmp_path: Path) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("luanti", 14757)
    source, _checkpoint = _write_screening_and_manifest(root, candidate)
    _capsule, attempts, _aggregate = _write_regression_evidence(root, candidate, source)
    forged = next(
        item
        for item in attempts
        if item.payload["phase"] == "f2p" and item.payload["variant"] == "buggy"
    )
    payload = dict(forged.payload)
    payload["returncode"] = 0
    _write(
        root,
        artifact_id=forged.reference.artifact_id,
        artifact_kind=forged.reference.artifact_kind,
        semantic_version=forged.reference.semantic_version,
        created_at="2026-08-13T00:00:01Z",
        inputs=forged.inputs,
        payload=payload,
    )

    with pytest.raises(RuntimeError, match="attempt process result"):
        project_luanti_lua_regression_to_v2(
            root,
            candidate=candidate,
            authorization=_authorization(),
        )


def test_projection_rejects_existing_transition_detached_from_current_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("luanti", 14757)
    source, _checkpoint = _write_screening_and_manifest(root, candidate)
    _write_regression_evidence(root, candidate, source)
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {"may_start_build": True, "may_start_candidate": True, "may_write": True},
        )(),
    )
    result = project_luanti_lua_regression_to_v2(
        root,
        candidate=candidate,
        authorization=_authorization(),
    )
    _write(
        root,
        artifact_id=result.transition.reference.artifact_id,
        artifact_kind=result.transition.reference.artifact_kind,
        semantic_version=result.transition.reference.semantic_version,
        # The projection stamps its transition with the real clock, so the detached
        # revision has to be newer than that to become the current head.
        created_at=_after_now(),
        inputs=result.transition.inputs[:-1],
        payload=dict(result.transition.payload),
    )

    with pytest.raises(RuntimeError, match="transition binding"):
        project_luanti_lua_regression_to_v2(
            root,
            candidate=candidate,
            authorization=_authorization(),
        )


def test_narrow_reprojection_replaces_downstream_terminal_with_active_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("luanti", 14757)
    source, _checkpoint = _write_screening_and_manifest(root, candidate)
    _capsule, _attempts, _aggregate = _write_regression_evidence(
        root, candidate, source
    )
    initial = project_luanti_lua_regression_to_v2(
        root,
        candidate=candidate,
        authorization=_authorization(),
    )
    _write(
        root,
        artifact_id=f"candidate_stage/luanti/{candidate.change_number}/version_materialization/v2",
        artifact_kind="candidate_stage",
        semantic_version="2.0",
        created_at="2026-08-14T00:00:00Z",
        inputs=(initial.stage.reference,),
        payload={
            "candidate": candidate.to_value(),
            "stage": "version_materialization",
            "state": "terminal",
            "outcome": "version_incompatible",
            "details": {"reason": "downstream_visual_closure_missing"},
            "complete": True,
        },
    )
    terminal = ArtifactReference(
        f"candidate_stage/luanti/{candidate.change_number}/version_materialization/v2",
        "candidate_stage",
        "2.0",
    )
    _write(
        root,
        artifact_id=f"preannotation_checkpoint/luanti/{candidate.change_number}/current/v2",
        artifact_kind="preannotation_checkpoint",
        semantic_version="2.0",
        created_at="2026-08-14T00:00:00Z",
        inputs=(terminal,),
        payload={
            "candidate": candidate.to_value(),
            "completed_stages": [
                "test_manifest",
                "test_repetitions",
                "version_materialization",
            ],
            "state": "terminal",
            "outcome": "version_incompatible",
            "current_artifact_id": terminal.artifact_id,
        },
    )
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.luanti_lua_admission._now",
        lambda: "2026-08-14T00:00:01Z",
    )

    restored = reproject_qualified_luanti_lua_admission(
        root,
        candidate=candidate,
        authorization=_authorization(),
    )

    assert restored.qualified is True
    assert restored.next_gate == "version_materialization"
    assert restored.stage.reference == initial.stage.reference
    assert restored.checkpoint.inputs == (restored.stage.reference,)
    assert restored.checkpoint.payload == {
        "candidate": candidate.to_value(),
        "completed_stages": ["test_manifest", "test_repetitions"],
        "state": "active",
        "outcome": None,
        "current_artifact_id": restored.stage.reference.artifact_id,
    }


def test_narrow_reprojection_rejects_unqualified_truth_table(tmp_path: Path) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("luanti", 14757)
    source, _checkpoint = _write_screening_and_manifest(root, candidate)
    _write_regression_evidence(root, candidate, source, qualified=False)

    with pytest.raises(RuntimeError, match="truth table is not qualified"):
        reproject_qualified_luanti_lua_admission(
            root,
            candidate=candidate,
            authorization=_authorization(),
        )
