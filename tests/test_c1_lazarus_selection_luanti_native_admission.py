from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_native_admission import (
    REQUIRED_RUNTIME_VISUAL_EVIDENCE,
    audit_luanti_native_runtime_visual_closure,
    load_luanti_native_downstream_preflight,
    load_luanti_native_task_mapping,
    project_luanti_native_regression_to_v2,
    write_luanti_native_downstream_preflight,
    write_luanti_native_task_mapping,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.source_atomicity import (
    load_source_atomicity_batch_context,
    write_source_atomicity_artifact,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("luanti", 15519)
_PROFILE_ID = "luanti-15519-gettext-plural-trim-v2"
_PRODUCTION = ("src/gettext_plural_form.cpp", "src/gettext_plural_form.h")
_TEST = ("src/unittest/test_translations.cpp",)
_P2P = ("src/unittest/test_settings.cpp",)


def _authorization(scope: str = "preannotation-execution") -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=f"local-test/native-admission-{scope}",
        scope=scope,
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    *,
    created_at: str,
    inputs: tuple[ArtifactReference, ...] = (),
    payload: dict[str, object],
    authorization: AuthorizationRecord | None = None,
) -> None:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=created_at,
        inputs=inputs,
        payload=payload,
        authorization=authorization or _authorization(),
    )


def _ref(artifact_id: str, kind: str, version: str) -> ArtifactReference:
    return ArtifactReference(artifact_id, kind, version)


def _write_source_prefix(root: Path) -> tuple[ArtifactReference, ...]:
    enumeration = _ref(
        "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
    )
    source = _ref(
        "candidate_source_packet/luanti/15519/source/v2",
        "candidate_source_packet",
        "2.0",
    )
    patch = _ref(
        "candidate_test_patch/luanti/15519/screening/v2",
        "candidate_test_patch",
        "2.0",
    )
    screening = _ref(
        "screening_checkpoint/luanti/15519/current/v2",
        "screening_checkpoint",
        "2.0",
    )
    screening_authorization = _authorization("candidate-screening")
    _write(
        root,
        enumeration,
        created_at="2026-08-13T00:00:00Z",
        payload={
            "repository_key": "luanti",
            "merged_change_numbers": [15519],
            "authoritative_issue_link_missing": [],
            "repair_not_in_pinned": [],
            "candidates": [_CANDIDATE.to_value()],
            "infrastructure_blocked": [],
            "complete": True,
        },
        authorization=screening_authorization,
    )
    manifest_relative = "raw/luanti/15519/source-manifest.json"
    patch_relative = "raw/luanti/15519/repair.diff"
    manifest_path = root / manifest_relative
    patch_path = root / patch_relative
    manifest_path.parent.mkdir(parents=True)
    patch_path.write_text(
        """diff --git a/src/gettext_plural_form.cpp b/src/gettext_plural_form.cpp
--- a/src/gettext_plural_form.cpp
+++ b/src/gettext_plural_form.cpp
@@ -1 +1 @@
-return old_value;
+return fixed_value;
diff --git a/src/gettext_plural_form.h b/src/gettext_plural_form.h
--- a/src/gettext_plural_form.h
+++ b/src/gettext_plural_form.h
@@ -1 +1 @@
-int old_value();
+int fixed_value();
diff --git a/src/unittest/test_translations.cpp b/src/unittest/test_translations.cpp
--- a/src/unittest/test_translations.cpp
+++ b/src/unittest/test_translations.cpp
@@ -1 +1,2 @@
 TEST_CASE("test translations") {
+CHECK((*form)(122) == 1);
""",
        encoding="utf-8",
    )
    repository_url = "https://api.github.com/repos/luanti-org/luanti"
    manifest_path.write_text(
        json.dumps(
            {
                "candidate": _CANDIDATE.to_value(),
                "provider": "github",
                "authority": {
                    "provider": "github",
                    "api_base": "https://api.github.com",
                    "repository": "luanti-org/luanti",
                },
                "pull": {
                    "number": 15519,
                    "merged": True,
                    "merged_at": "2026-08-01T00:00:00Z",
                    "title": "Repair plural forms",
                    "body": "Fixes #15518.",
                },
                "relationships": [
                    {
                        "event": "cross-referenced",
                        "created_at": "2026-07-02T00:00:00Z",
                        "source": {
                            "issue": {
                                "number": 15518,
                                "repository_url": repository_url,
                                "created_at": "2026-07-01T00:00:00Z",
                            }
                        },
                    }
                ],
                "issue_numbers": [15518],
                "issues": [
                    {
                        "number": 15518,
                        "issue": {
                            "number": 15518,
                            "title": "Plural form defect",
                            "body": "The parser retains an invalid trailing token.",
                            "created_at": "2026-07-01T00:00:00Z",
                        },
                        "comments": [],
                        "history": [],
                    }
                ],
                "raw_paths": [patch_relative],
                "request_receipt_paths": [],
                "patch_raw_path": patch_relative,
                "attachment_paths": [],
                "complete": True,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _write(
        root,
        source,
        created_at="2026-08-13T00:00:01Z",
        inputs=(enumeration,),
        payload={
            "candidate": _CANDIDATE.to_value(),
            "raw_evidence_path": manifest_relative,
            "complete": True,
        },
        authorization=screening_authorization,
    )
    _write(
        root,
        patch,
        created_at="2026-08-13T00:00:02Z",
        inputs=(enumeration, source),
        payload={
            "candidate": _CANDIDATE.to_value(),
            "outcome": "patch_eligible",
            "production_paths": list(_PRODUCTION),
            "test_paths": list(_TEST),
            "source_manifest_path": manifest_relative,
            "raw_patch_path": patch_relative,
            "complete": True,
        },
        authorization=screening_authorization,
    )
    _write(
        root,
        screening,
        created_at="2026-08-13T00:00:03Z",
        inputs=(enumeration, patch),
        payload={
            "candidate": _CANDIDATE.to_value(),
            "state": "complete",
            "outcome": "patch_eligible",
            "stage_artifact_id": patch.artifact_id,
        },
        authorization=screening_authorization,
    )
    context = load_source_atomicity_batch_context(root, repository_key="luanti")
    atomicity = write_source_atomicity_artifact(
        batch_context=context,
        candidate=_CANDIDATE,
        created_at="2026-08-13T00:00:04Z",
        authorization=screening_authorization,
    )
    return enumeration, source, patch, screening, atomicity.artifact.reference


def _write_manifest_capsule_and_receipts(root: Path) -> None:
    _enumeration, source, patch, _screening, _atomicity = _write_source_prefix(root)
    manifest = _ref(
        "candidate_stage/luanti/15519/test_manifest/v2", "candidate_stage", "2.0"
    )
    _write(
        root,
        manifest,
        created_at="2026-08-13T00:00:05Z",
        inputs=(patch,),
        payload={
            "candidate": _CANDIDATE.to_value(),
            "stage": "test_manifest",
            "state": "passed",
            "outcome": None,
            "details": {
                "executable_test_paths": list(_TEST),
                "test_count": 1,
                "execution_candidate_kind": "added_assertion",
            },
            "complete": True,
        },
    )
    checkpoint = _ref(
        "preannotation_checkpoint/luanti/15519/current/v2",
        "preannotation_checkpoint",
        "2.0",
    )
    _write(
        root,
        checkpoint,
        created_at="2026-08-13T00:00:06Z",
        inputs=(manifest,),
        payload={
            "candidate": _CANDIDATE.to_value(),
            "completed_stages": ["test_manifest"],
            "state": "active",
            "outcome": None,
            "current_artifact_id": manifest.artifact_id,
        },
    )
    tree_relative = "private/fixed-capsules/luanti/15519/fixed-source"
    tree = root / tree_relative
    for relative in (*_PRODUCTION, *_TEST, *_P2P):
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("registered fixed content\n", encoding="utf-8")
    capsule = _ref(
        "fixed_source_capsule/luanti/15519/fixed/v2",
        "fixed_source_capsule",
        "2.0",
    )
    _write(
        root,
        capsule,
        created_at="2026-08-13T00:00:07Z",
        inputs=(source, patch, manifest),
        payload={
            "candidate": _CANDIDATE.to_value(),
            "profile_id": _PROFILE_ID,
            "source_tree_path": tree_relative,
            "source_manifest_path": "raw/luanti/15519/source-manifest.json",
            "test_paths": list(_TEST),
            "p2p_test_paths": list(_P2P),
            "support_paths": [],
        },
    )
    attempts: list[ArtifactReference] = []
    sequence = 8
    for phase in ("f2p", "p2p"):
        selector = "test translations" if phase == "f2p" else "TestSettings"
        summary = (
            b"All tests passed (45 assertions in 1 test case)\n"
            if phase == "f2p"
            else b"Unit Test Results: PASSED\n    0 / 3 failed tests.\n"
        )
        for variant in ("fixed", "buggy"):
            build = _ref(
                f"native_regression_build/luanti/15519/{variant}/build/v3",
                "native_regression_build",
                "3.0",
            )
            build_head = (
                root
                / "artifacts"
                / Path(*build.artifact_id.split("/"))
                / build.semantic_version
                / "head.json"
            )
            if not build_head.exists():
                _write(
                    root,
                    build,
                    created_at=f"2026-08-13T00:00:{sequence:02d}Z",
                    inputs=(capsule,),
                    payload={
                        "candidate": _CANDIDATE.to_value(),
                        "profile_id": _PROFILE_ID,
                        "variant": variant,
                        "step": "build",
                        "process_outcome": "completed",
                        "returncode": 0,
                        "argv": ["cmake", "--build", "build-luanti-15519"],
                        "stdout_base64": "",
                        "stderr_base64": "",
                        "stdout_truncated": False,
                        "stderr_truncated": False,
                        "output_encoding": "base64",
                    },
                )
                sequence += 1
            expected = "failure" if phase == "f2p" and variant == "buggy" else "pass"
            returncode = 1 if expected == "failure" else 0
            raw_summary = (
                b"test cases: 1 | 0 passed | 1 failed\n"
                if expected == "failure"
                else summary
            )
            for repetition in (1, 2, 3):
                v3 = _ref(
                    f"native_regression_attempt/luanti/15519/{phase}/{variant}/{repetition}/v3",
                    "native_regression_attempt",
                    "3.0",
                )
                identity = {
                    "candidate": _CANDIDATE.to_value(),
                    "profile_id": _PROFILE_ID,
                    "phase": phase,
                    "variant": variant,
                    "repetition": repetition,
                    "selector": selector,
                    "expected_outcome": expected,
                }
                streams = {
                    "stdout_base64": "",
                    "stderr_base64": base64.b64encode(raw_summary).decode("ascii"),
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "output_encoding": "base64",
                }
                _write(
                    root,
                    v3,
                    created_at=f"2026-08-13T00:00:{sequence:02d}Z",
                    inputs=(capsule, build),
                    payload={
                        **identity,
                        "observed_outcome": "invalid_test_count",
                        "process_outcome": "completed",
                        "returncode": returncode,
                        "completed_test_count": 0,
                        **streams,
                        "matched_expected": False,
                    },
                )
                sequence += 1
                v4 = _ref(
                    f"native_regression_attempt/luanti/15519/{phase}/{variant}/{repetition}/v4",
                    "native_regression_attempt",
                    "4.0",
                )
                _write(
                    root,
                    v4,
                    created_at=f"2026-08-13T00:00:{sequence:02d}Z",
                    inputs=(v3,),
                    payload={
                        **identity,
                        "observed_outcome": expected,
                        "process_outcome": "completed",
                        "returncode": returncode,
                        "completed_test_count": 1,
                        **streams,
                        "matched_expected": True,
                    },
                )
                sequence += 1
                attempts.append(v4)
    cells = []
    for phase, variant in sorted(
        (phase, variant) for phase in ("f2p", "p2p") for variant in ("fixed", "buggy")
    ):
        outcome = "failure" if phase == "f2p" and variant == "buggy" else "pass"
        cells.append(
            {
                "phase": phase,
                "variant": variant,
                "observed_outcomes": [outcome, outcome, outcome],
            }
        )
    aggregate = _ref(
        "native_regression_aggregate/luanti/15519/fixed-buggy/v4",
        "native_regression_aggregate",
        "4.0",
    )
    _write(
        root,
        aggregate,
        created_at="2026-08-13T00:01:00Z",
        inputs=tuple(attempts),
        payload={
            "candidate": _CANDIDATE.to_value(),
            "profile_id": _PROFILE_ID,
            "repetitions_per_cell": 3,
            "attempt_count": 12,
            "stable_repetitions": True,
            "all_expected": True,
            "qualified": True,
            "cells": cells,
        },
    )


def test_projects_v4_native_receipts_and_repairs_atomicity_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    _write_manifest_capsule_and_receipts(root)
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {"may_start_build": True, "may_start_candidate": True, "may_write": True},
        )(),
    )

    result = project_luanti_native_regression_to_v2(
        root,
        candidate=_CANDIDATE,
        authorization=_authorization(),
    )

    assert result.qualified is True
    assert result.next_gate == "version_materialization"
    assert result.manifest_rebound is True
    assert result.stage.payload["state"] == "passed"
    assert result.checkpoint.payload["completed_stages"] == [
        "test_manifest",
        "test_repetitions",
    ]
    atomicity = _ref(
        "source_atomicity/luanti/15519/preannotation/v2", "source_atomicity", "2.0"
    )
    assert result.manifest.inputs == (
        _ref(
            "candidate_test_patch/luanti/15519/screening/v2",
            "candidate_test_patch",
            "2.0",
        ),
        atomicity,
    )
    assert result.transition.payload["truth_table"] == {
        "fixed": {"f2p": True, "p2p": True},
        "buggy": {"f2p": False, "p2p": True},
    }


def test_tampered_v4_receipt_fails_before_formal_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    _write_manifest_capsule_and_receipts(root)
    attempt = _ref(
        "native_regression_attempt/luanti/15519/f2p/fixed/1/v4",
        "native_regression_attempt",
        "4.0",
    )
    loaded = load_stable_artifact(
        root,
        artifact_id=attempt.artifact_id,
        artifact_kind=attempt.artifact_kind,
        semantic_version=attempt.semantic_version,
    )
    payload = dict(loaded.payload)
    payload["completed_test_count"] = 0
    _write(
        root,
        attempt,
        created_at="2026-08-13T00:02:00Z",
        inputs=loaded.inputs,
        payload=payload,
    )
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {"may_start_build": True, "may_start_candidate": True, "may_write": True},
        )(),
    )

    with pytest.raises(RuntimeError, match="v4 attempt reprojection"):
        project_luanti_native_regression_to_v2(
            root,
            candidate=_CANDIDATE,
            authorization=_authorization(),
        )

    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        load_stable_artifact(
            root,
            artifact_id=(
                "luanti_native_regression_transition/luanti/15519/test-repetitions/v2"
            ),
            artifact_kind="luanti_native_regression_transition",
            semantic_version="2.0",
        )


def test_runtime_visual_audit_is_read_only_and_requires_goldens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    workspace = tmp_path / "workspace"
    (workspace / "benchmarks/p5/lazarus/defects").mkdir(parents=True)
    (workspace / "benchmarks/p5/lazarus/authoring").mkdir(parents=True)
    _write_manifest_capsule_and_receipts(root)
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {"may_start_build": True, "may_start_candidate": True, "may_write": True},
        )(),
    )
    project_luanti_native_regression_to_v2(
        root,
        candidate=_CANDIDATE,
        authorization=_authorization(),
    )
    before = tuple(sorted(path for path in root.rglob("head.json")))

    blocker = audit_luanti_native_runtime_visual_closure(
        root,
        workspace_root=workspace,
        candidate=_CANDIDATE,
    )

    after = tuple(sorted(path for path in root.rglob("head.json")))
    assert after == before
    assert blocker.reason == "registered_runtime_world_visual_closure_missing"
    assert blocker.missing_required_evidence == REQUIRED_RUNTIME_VISUAL_EVIDENCE
    assert blocker.golden_disposition == "required_no_not_applicable"
    assert blocker.registered_asset_paths == ()
    assert not any("version_materialization" in path.as_posix() for path in after)


def test_downstream_preflight_preserves_active_admission_and_is_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    workspace = tmp_path / "workspace"
    (workspace / "benchmarks/p5/lazarus/defects").mkdir(parents=True)
    (workspace / "benchmarks/p5/lazarus/authoring").mkdir(parents=True)
    _write_manifest_capsule_and_receipts(root)
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {"may_start_build": True, "may_start_candidate": True, "may_write": True},
        )(),
    )

    first = write_luanti_native_downstream_preflight(
        root,
        workspace_root=workspace,
        candidate=_CANDIDATE,
        authorization=_authorization(),
    )
    resumed = write_luanti_native_downstream_preflight(
        root,
        workspace_root=workspace,
        candidate=_CANDIDATE,
        authorization=_authorization(),
    )
    loaded = load_luanti_native_downstream_preflight(
        root,
        workspace_root=workspace,
        candidate=_CANDIDATE,
    )

    assert first.reference == resumed.reference == loaded.reference
    assert first.payload == resumed.payload == loaded.payload
    assert loaded.payload["admission_state"] == "active_qualified"
    assert loaded.payload["downstream_state"] == "pending"
    assert loaded.payload["outcome"] is None
    assert loaded.payload["reason"] == "registered_runtime_world_visual_closure_missing"
    assert loaded.payload["missing_required_evidence"] == list(
        REQUIRED_RUNTIME_VISUAL_EVIDENCE
    )
    assert loaded.payload["registered_asset_paths"] == []
    assert loaded.payload["golden_disposition"] == "required_no_not_applicable"
    assert loaded.payload["repairable_actions"] == [
        "register_issue_15518_task_mapping",
        "freeze_minimal_russian_po_game_fixture",
        "materialize_complete_fixed_buggy_application_pair",
        "launch_isolated_russian_locale_world_pair",
        "capture_ordered_replay_and_raw_start_observations",
        "define_semantic_error_and_invariant_oracles",
        "capture_common_before_and_cross_matrix_goldens",
        "persist_same_lineage_construction_receipt",
    ]
    assert not any(
        path.parent.name in {"version_materialization", "construction_gate"}
        for path in root.rglob("head.json")
    )


def test_downstream_preflight_rejects_asset_inventory_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    workspace = tmp_path / "workspace"
    defects = workspace / "benchmarks/p5/lazarus/defects"
    defects.mkdir(parents=True)
    (workspace / "benchmarks/p5/lazarus/authoring").mkdir(parents=True)
    _write_manifest_capsule_and_receipts(root)
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {"may_start_build": True, "may_start_candidate": True, "may_write": True},
        )(),
    )
    write_luanti_native_downstream_preflight(
        root,
        workspace_root=workspace,
        candidate=_CANDIDATE,
        authorization=_authorization(),
    )
    (defects / "lz-luanti-15519-plural-form").mkdir()

    with pytest.raises(RuntimeError, match="asset inventory changed"):
        load_luanti_native_downstream_preflight(
            root,
            workspace_root=workspace,
            candidate=_CANDIDATE,
        )


def test_task_mapping_freezes_issue_15518_fixture_without_claiming_visual_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    workspace = tmp_path / "workspace"
    (workspace / "benchmarks/p5/lazarus/defects").mkdir(parents=True)
    (workspace / "benchmarks/p5/lazarus/authoring").mkdir(parents=True)
    _write_manifest_capsule_and_receipts(root)
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {"may_start_build": True, "may_start_candidate": True, "may_write": True},
        )(),
    )
    project_luanti_native_regression_to_v2(
        root,
        candidate=_CANDIDATE,
        authorization=_authorization(),
    )

    mapping = write_luanti_native_task_mapping(
        root,
        candidate=_CANDIDATE,
        authorization=_authorization(),
    )
    loaded = load_luanti_native_task_mapping(root, candidate=_CANDIDATE)

    assert mapping.payload == loaded.payload
    assert mapping.payload["issue_number"] == 15518
    assert mapping.payload["defect_locus"] == "at_goal"
    assert mapping.payload["closure_state"] == "mapping_registered_fixture_frozen"
    assert mapping.payload["visual_closure_state"] == "pending"
    assert mapping.payload["golden_disposition"] == "required_no_not_applicable"
    fixture = root / mapping.payload["fixture_root"]
    po = fixture / "mods/c1_plural/locale/c1_plural.ru.po"
    assert '"Plural-Forms: nplurals=3; plural=n%10==1' in po.read_text()
    assert (fixture / "game.conf").is_file()
    assert (fixture / "mods/c1_plural/init.lua").is_file()
    assert not any("sha" in key.lower() for key in mapping.payload)

    pending = write_luanti_native_downstream_preflight(
        root,
        workspace_root=workspace,
        candidate=_CANDIDATE,
        authorization=_authorization(),
    )
    assert (
        "registered_lazarus_task_mapping"
        not in pending.payload["missing_required_evidence"]
    )
    assert pending.payload["downstream_state"] == "pending"
    assert pending.payload["outcome"] is None
