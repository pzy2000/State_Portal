from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import warptest.c1_lazarus_selection.operations as operations_module
from warptest.c1_lazarus_selection.epoch import SELECTION_EPOCH_V2
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.operations import (
    ProcessCapture,
    V2PreannotationBatchContext,
    evaluate_test_manifest,
    evaluate_test_repetitions,
)
from warptest.c1_lazarus_selection.pipeline import (
    PREANNOTATION_STAGES,
    StageDecision,
    run_preannotation_pipeline,
)
from warptest.c1_lazarus_selection.source_atomicity import (
    load_source_atomicity_artifact,
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
        authorization_id="local-v2-preannotation/test",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _write(
    root: Path,
    *,
    artifact_id: str,
    artifact_kind: str,
    payload: dict[str, object],
    inputs: tuple[ArtifactReference, ...] = (),
    semantic_version: str = "2.0",
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


def _write_v2_screening_root(
    root: Path,
    candidate: CandidateKey,
    *,
    source_inputs: tuple[ArtifactReference, ...] | None = None,
    write_atomicity: bool = True,
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    source_manifest_path = (
        f"raw/source/{candidate.repository_key}/{candidate.change_number}.json"
    )
    raw_patch_path = (
        f"raw/patch/{candidate.repository_key}/{candidate.change_number}.diff"
    )
    raw_patch = root / raw_patch_path
    raw_patch.parent.mkdir(parents=True, exist_ok=True)
    raw_patch.write_bytes(
        b"""\\
diff --git a/packages/example/src/index.ts b/packages/example/src/index.ts
--- a/packages/example/src/index.ts
+++ b/packages/example/src/index.ts
@@ -1 +1 @@
-export const value = 1;
+export const value = 2;
diff --git a/packages/example/test/index.spec.ts b/packages/example/test/index.spec.ts
--- a/packages/example/test/index.spec.ts
+++ b/packages/example/test/index.spec.ts
@@ -1 +1 @@
-test('old', () => expect(value).toBe(1));
+test('new', () => expect(value).toBe(2));
"""
    )
    source_manifest = root / source_manifest_path
    source_manifest.parent.mkdir(parents=True, exist_ok=True)
    issue_number = candidate.change_number + 100_000
    repository_names = {
        "jupyterlab": "jupyterlab/jupyterlab",
        "luanti": "luanti-org/luanti",
        "openra": "OpenRA/OpenRA",
    }
    repository_name = repository_names[candidate.repository_key]
    source_manifest.write_text(
        json.dumps(
            {
                "candidate": candidate.to_value(),
                "provider": "github",
                "authority": {
                    "provider": "github",
                    "api_base": "https://api.github.com",
                    "repository": repository_name,
                },
                "pull": {
                    "number": candidate.change_number,
                    "merged": True,
                    "merged_at": "2026-08-01T00:00:00Z",
                    "title": "Repair regression",
                    "body": f"Fixes #{issue_number}.",
                },
                "relationships": [],
                "issue_numbers": [issue_number],
                "issues": [
                    {
                        "number": issue_number,
                        "issue": {
                            "number": issue_number,
                            "title": "Regression",
                            "body": "Authoritative defect report.",
                            "created_at": "2026-07-01T00:00:00Z",
                        },
                        "comments": [],
                        "history": [],
                    }
                ],
                "raw_paths": [raw_patch_path],
                "request_receipt_paths": [],
                "patch_raw_path": raw_patch_path,
                "attachment_paths": [],
                "complete": True,
            }
        ),
        encoding="utf-8",
    )
    enumeration = _write(
        root,
        artifact_id=(f"repository_enumeration/{candidate.repository_key}/current/v2"),
        artifact_kind="repository_enumeration",
        payload={
            "repository_key": candidate.repository_key,
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
            f"candidate_source_packet/{candidate.repository_key}/"
            f"{candidate.change_number}/source/v2"
        ),
        artifact_kind="candidate_source_packet",
        inputs=(enumeration.reference,) if source_inputs is None else source_inputs,
        payload={
            "candidate": candidate.to_value(),
            "raw_evidence_path": source_manifest_path,
            "complete": True,
        },
    )
    patch = _write(
        root,
        artifact_id=(
            f"candidate_test_patch/{candidate.repository_key}/"
            f"{candidate.change_number}/screening/v2"
        ),
        artifact_kind="candidate_test_patch",
        inputs=(enumeration.reference, source.reference),
        payload={
            "candidate": candidate.to_value(),
            "outcome": "patch_eligible",
            "production_paths": ["packages/example/src/index.ts"],
            "test_paths": ["packages/example/test/index.spec.ts"],
            "source_manifest_path": source_manifest_path,
            "raw_patch_path": raw_patch_path,
            "complete": True,
        },
    )
    checkpoint = _write(
        root,
        artifact_id=(
            f"screening_checkpoint/{candidate.repository_key}/"
            f"{candidate.change_number}/current/v2"
        ),
        artifact_kind="screening_checkpoint",
        inputs=(enumeration.reference, patch.reference),
        payload={
            "candidate": candidate.to_value(),
            "state": "complete",
            "outcome": "patch_eligible",
            "stage_artifact_id": patch.reference.artifact_id,
        },
    )
    if write_atomicity:
        context = load_source_atomicity_batch_context(
            root,
            repository_key=candidate.repository_key,
        )
        write_source_atomicity_artifact(
            batch_context=context,
            candidate=candidate,
            created_at="2026-08-13T00:00:01Z",
            authorization=_authorization(),
        )
    return enumeration, source, patch, checkpoint


def _passing_operations(calls: list[str]):
    def operation(stage: str):
        def execute(_candidate: CandidateKey) -> StageDecision:
            calls.append(stage)
            if stage == "annotation_eligibility":
                return StageDecision.eligible()
            return StageDecision.passed({})

        return execute

    return {stage: operation(stage) for stage in PREANNOTATION_STAGES}


def test_v2_pipeline_writes_and_resumes_only_the_v2_preannotation_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 41)
    _enumeration, _source, patch, _screening = _write_v2_screening_root(root, candidate)
    _write(
        root,
        artifact_id="preannotation_checkpoint/jupyterlab/41/current/v1",
        artifact_kind="preannotation_checkpoint",
        semantic_version="1.0",
        payload={"legacy": True},
    )
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {
                "may_start_build": True,
                "may_start_candidate": True,
                "may_write": True,
            },
        )(),
    )
    calls: list[str] = []

    first = run_preannotation_pipeline(
        root,
        candidates=(candidate,),
        operations=_passing_operations(calls),
        authorization=_authorization(),
        selection_epoch=SELECTION_EPOCH_V2,
        test_workers=1,
        build_workers=1,
        replay_workers=1,
        persistence_workers=1,
        reload_workers=1,
        queue_limit=1,
    )

    assert first.scheduled == 1
    assert first.eligible == 1
    assert calls == list(PREANNOTATION_STAGES)
    first_stage = load_stable_artifact(
        root,
        artifact_id="candidate_stage/jupyterlab/41/test_manifest/v2",
        artifact_kind="candidate_stage",
        semantic_version="2.0",
    )
    atomicity = load_source_atomicity_artifact(
        batch_context=load_source_atomicity_batch_context(
            root,
            repository_key=candidate.repository_key,
        ),
        candidate=candidate,
    )
    assert first_stage.inputs == (patch.reference, atomicity.artifact.reference)
    checkpoint = load_stable_artifact(
        root,
        artifact_id="preannotation_checkpoint/jupyterlab/41/current/v2",
        artifact_kind="preannotation_checkpoint",
        semantic_version="2.0",
    )
    assert checkpoint.inputs[0].semantic_version == "2.0"
    assert checkpoint.inputs[0].artifact_id.endswith("annotation_eligibility/v2")

    resumed = run_preannotation_pipeline(
        root,
        candidates=(candidate,),
        operations={
            stage: lambda _candidate: (_ for _ in ()).throw(
                AssertionError("valid v2 stage was re-executed")
            )
            for stage in PREANNOTATION_STAGES
        },
        authorization=_authorization(),
        selection_epoch=SELECTION_EPOCH_V2,
    )

    assert resumed.resumed == 1
    assert resumed.eligible == 1


def test_v2_manifest_boundary_persists_active_checkpoint_without_later_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 45)
    _enumeration, _source, patch, _screening = _write_v2_screening_root(root, candidate)
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {
                "may_start_build": True,
                "may_start_candidate": True,
                "may_write": True,
            },
        )(),
    )
    calls: list[str] = []

    first = run_preannotation_pipeline(
        root,
        candidates=(candidate,),
        operations={
            "test_manifest": lambda _candidate: (
                calls.append("test_manifest") or StageDecision.passed({})
            )
        },
        authorization=_authorization(),
        selection_epoch=SELECTION_EPOCH_V2,
        through_stage="test_manifest",
        test_workers=1,
        build_workers=1,
        replay_workers=1,
        persistence_workers=1,
        reload_workers=1,
        queue_limit=1,
    )

    assert calls == ["test_manifest"]
    assert first.active == 1
    assert first.scheduled == 1
    manifest = load_stable_artifact(
        root,
        artifact_id="candidate_stage/jupyterlab/45/test_manifest/v2",
        artifact_kind="candidate_stage",
        semantic_version="2.0",
    )
    atomicity = load_source_atomicity_artifact(
        batch_context=load_source_atomicity_batch_context(
            root,
            repository_key=candidate.repository_key,
        ),
        candidate=candidate,
    )
    assert manifest.inputs == (patch.reference, atomicity.artifact.reference)
    checkpoint = load_stable_artifact(
        root,
        artifact_id="preannotation_checkpoint/jupyterlab/45/current/v2",
        artifact_kind="preannotation_checkpoint",
        semantic_version="2.0",
    )
    assert checkpoint.payload["completed_stages"] == ["test_manifest"]
    assert checkpoint.payload["state"] == "active"

    resumed = run_preannotation_pipeline(
        root,
        candidates=(candidate,),
        operations={
            "test_manifest": lambda _candidate: (_ for _ in ()).throw(
                AssertionError("manifest-only active checkpoint was re-executed")
            )
        },
        authorization=_authorization(),
        selection_epoch=SELECTION_EPOCH_V2,
        through_stage="test_manifest",
    )

    assert resumed.active == 1
    assert resumed.resumed == 1


def test_v2_manifest_boundary_resumes_terminal_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 46)
    _write_v2_screening_root(root, candidate)
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.pipeline.disk_capacity",
        lambda _root: type(
            "Capacity",
            (),
            {
                "may_start_build": True,
                "may_start_candidate": True,
                "may_write": True,
            },
        )(),
    )

    first = run_preannotation_pipeline(
        root,
        candidates=(candidate,),
        operations={
            "test_manifest": lambda _candidate: StageDecision.terminal(
                "quality_rejected", {"reason": "no_regression_assertion"}
            )
        },
        authorization=_authorization(),
        selection_epoch=SELECTION_EPOCH_V2,
        through_stage="test_manifest",
    )
    resumed = run_preannotation_pipeline(
        root,
        candidates=(candidate,),
        operations={
            "test_manifest": lambda _candidate: (_ for _ in ()).throw(
                AssertionError("manifest terminal was re-executed")
            )
        },
        authorization=_authorization(),
        selection_epoch=SELECTION_EPOCH_V2,
        through_stage="test_manifest",
    )

    assert first.terminal_counts == {"quality_rejected": 1}
    assert resumed.terminal_counts == {"quality_rejected": 1}
    assert resumed.resumed == 1


def test_v2_manifest_fails_closed_before_operation_when_atomicity_is_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 48)
    _write_v2_screening_root(root, candidate, write_atomicity=False)
    calls: list[str] = []

    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        run_preannotation_pipeline(
            root,
            candidates=(candidate,),
            operations={
                "test_manifest": lambda _candidate: (
                    calls.append("test_manifest") or StageDecision.passed({})
                )
            },
            authorization=_authorization(),
            selection_epoch=SELECTION_EPOCH_V2,
            through_stage="test_manifest",
        )

    assert calls == []
    _assert_no_v2_preannotation_head(root, candidate)


def test_v2_manifest_fails_closed_before_operation_when_atomicity_is_rejected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 49)
    _write_v2_screening_root(root, candidate)
    manifest_path = root / "raw/source/jupyterlab/49.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pull"]["body"] = "No authoritative closing relationship."
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )
    receipt = write_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
        created_at="2026-08-13T00:00:02Z",
        authorization=_authorization(),
    )
    assert receipt.outcome == "rejected"
    with pytest.raises(RuntimeError, match="source atomicity is not accepted"):
        evaluate_test_manifest(
            root,
            candidate,
            selection_epoch=SELECTION_EPOCH_V2,
        )
    calls: list[str] = []

    with pytest.raises(RuntimeError, match="source atomicity is not accepted"):
        run_preannotation_pipeline(
            root,
            candidates=(candidate,),
            operations={
                "test_manifest": lambda _candidate: (
                    calls.append("test_manifest") or StageDecision.passed({})
                )
            },
            authorization=_authorization(),
            selection_epoch=SELECTION_EPOCH_V2,
            through_stage="test_manifest",
        )

    assert calls == []
    _assert_no_v2_preannotation_head(root, candidate)


def test_v2_manifest_fails_closed_before_operation_when_atomicity_is_tampered(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 50)
    _write_v2_screening_root(root, candidate)
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )
    receipt = load_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
    )
    _write(
        root,
        artifact_id=receipt.artifact.reference.artifact_id,
        artifact_kind=receipt.artifact.reference.artifact_kind,
        inputs=receipt.artifact.inputs,
        created_at="2026-08-13T00:00:02Z",
        payload={**receipt.artifact.payload, "primary_issue_number": 999_999},
    )
    calls: list[str] = []

    with pytest.raises(RuntimeError, match="mechanical replay"):
        run_preannotation_pipeline(
            root,
            candidates=(candidate,),
            operations={
                "test_manifest": lambda _candidate: (
                    calls.append("test_manifest") or StageDecision.passed({})
                )
            },
            authorization=_authorization(),
            selection_epoch=SELECTION_EPOCH_V2,
            through_stage="test_manifest",
        )

    assert calls == []
    _assert_no_v2_preannotation_head(root, candidate)


def _assert_no_v2_preannotation_head(root: Path, candidate: CandidateKey) -> None:
    for artifact_id, artifact_kind in (
        (
            f"candidate_stage/{candidate.repository_key}/"
            f"{candidate.change_number}/test_manifest/v2",
            "candidate_stage",
        ),
        (
            f"preannotation_checkpoint/{candidate.repository_key}/"
            f"{candidate.change_number}/current/v2",
            "preannotation_checkpoint",
        ),
    ):
        with pytest.raises(RuntimeError, match="artifact directory is missing"):
            load_stable_artifact(
                root,
                artifact_id=artifact_id,
                artifact_kind=artifact_kind,
                semantic_version="2.0",
            )


def test_v2_pipeline_rejects_a_source_packet_not_rooted_in_v2_enumeration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("luanti", 42)
    _write_v2_screening_root(
        root,
        candidate,
        source_inputs=(),
        write_atomicity=False,
    )

    with pytest.raises(RuntimeError, match="v2 source packet enumeration input"):
        run_preannotation_pipeline(
            root,
            candidates=(candidate,),
            operations=_passing_operations([]),
            authorization=_authorization(),
            selection_epoch=SELECTION_EPOCH_V2,
        )


def test_v2_manifest_batch_context_rejects_wrong_repository_or_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 47)
    enumeration, _source, _patch, _screening = _write_v2_screening_root(root, candidate)

    with pytest.raises(ValueError, match="candidate repository"):
        V2PreannotationBatchContext.from_enumeration(
            artifact_root=root,
            repository_key="luanti",
            enumeration_reference=enumeration.reference,
            candidate_numbers=frozenset({candidate.change_number}),
        )

    context = V2PreannotationBatchContext.from_enumeration(
        artifact_root=root,
        repository_key=candidate.repository_key,
        enumeration_reference=enumeration.reference,
        candidate_numbers=frozenset(),
    )
    with pytest.raises(RuntimeError, match="outside the batch enumeration"):
        evaluate_test_manifest(
            root,
            candidate,
            selection_epoch=SELECTION_EPOCH_V2,
            batch_context=context,
        )

    valid_context = V2PreannotationBatchContext.from_enumeration(
        artifact_root=root,
        repository_key=candidate.repository_key,
        enumeration_reference=enumeration.reference,
        candidate_numbers=frozenset({candidate.change_number}),
    )
    real_load = operations_module.load_stable_artifact

    def no_enumeration_reload(*args: object, **kwargs: object) -> object:
        if kwargs.get("artifact_kind") == "repository_enumeration":
            raise AssertionError("manifest operation reloaded the batch enumeration")
        return real_load(*args, **kwargs)

    monkeypatch.setattr(
        operations_module,
        "load_stable_artifact",
        no_enumeration_reload,
    )
    assert (
        evaluate_test_manifest(
            root,
            candidate,
            selection_epoch=SELECTION_EPOCH_V2,
            batch_context=valid_context,
        ).state
        == "passed"
    )


def test_v2_pipeline_rejects_v1_stage_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("luanti", 43)
    _write_v2_screening_root(root, candidate)
    evidence = _write(
        root,
        artifact_id="legacy_evidence/luanti/43/current/v1",
        artifact_kind="legacy_evidence",
        semantic_version="1.0",
        payload={"complete": True},
    )
    evidence_value = {
        "artifact_id": evidence.reference.artifact_id,
        "artifact_kind": evidence.reference.artifact_kind,
        "semantic_version": evidence.reference.semantic_version,
    }
    operations = _passing_operations([])
    operations["test_manifest"] = lambda _candidate: StageDecision.terminal(
        "quality_rejected", {"evidence_artifacts": [evidence_value]}
    )

    with pytest.raises(RuntimeError, match="v2 pre-annotation evidence is not v2"):
        run_preannotation_pipeline(
            root,
            candidates=(candidate,),
            operations=operations,
            authorization=_authorization(),
            selection_epoch=SELECTION_EPOCH_V2,
        )


def test_v2_pipeline_rejects_test_patch_with_mismatched_raw_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("luanti", 43)
    _enumeration, _source, patch, _screening = _write_v2_screening_root(root, candidate)
    _write(
        root,
        artifact_id=patch.reference.artifact_id,
        artifact_kind=patch.reference.artifact_kind,
        inputs=patch.inputs,
        created_at="2026-08-13T00:00:01Z",
        payload={
            **patch.payload,
            "raw_patch_path": "raw/patch/luanti/not-the-source-patch.diff",
        },
    )

    with pytest.raises(RuntimeError, match="v2 test patch raw evidence binding"):
        run_preannotation_pipeline(
            root,
            candidates=(candidate,),
            operations=_passing_operations([]),
            authorization=_authorization(),
            selection_epoch=SELECTION_EPOCH_V2,
        )


def test_v2_operations_read_v2_prerequisites_and_write_v2_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 44)
    _enumeration, _source, patch, _screening = _write_v2_screening_root(root, candidate)
    raw_patch = root / "raw" / "patch" / "jupyterlab" / "44.diff"
    raw_patch.parent.mkdir(parents=True, exist_ok=True)
    raw_patch.write_bytes(
        b"""\\
diff --git a/packages/example/src/index.ts b/packages/example/src/index.ts
--- a/packages/example/src/index.ts
+++ b/packages/example/src/index.ts
@@ -1 +1 @@
-export const value = 1;
+export const value = 2;
diff --git a/packages/example/test/index.spec.ts b/packages/example/test/index.spec.ts
--- a/packages/example/test/index.spec.ts
+++ b/packages/example/test/index.spec.ts
@@ -1 +1,2 @@
 test('old', () => expect(value).toBe(1));
+test('new', () => expect(value).toBe(2));
"""
    )
    manifest = _write(
        root,
        artifact_id="candidate_stage/jupyterlab/44/test_manifest/v2",
        artifact_kind="candidate_stage",
        inputs=(
            patch.reference,
            load_source_atomicity_artifact(
                batch_context=load_source_atomicity_batch_context(
                    root,
                    repository_key=candidate.repository_key,
                ),
                candidate=candidate,
            ).artifact.reference,
        ),
        payload={
            "candidate": candidate.to_value(),
            "stage": "test_manifest",
            "state": "passed",
            "outcome": None,
            "details": {
                "executable_test_paths": ["packages/example/test/index.spec.ts"],
                "test_count": 1,
            },
            "complete": True,
        },
    )

    decision = evaluate_test_manifest(
        root, candidate, selection_epoch=SELECTION_EPOCH_V2
    )
    assert decision.state == "passed"
    assert manifest.inputs[0] == patch.reference
    assert manifest.inputs[1].artifact_kind == "source_atomicity"

    workspace = tmp_path / "workspace"
    (workspace / "games" / "jupyterlab").mkdir(parents=True)
    calls: list[str] = []

    def clone(_source: Path, destination: Path) -> None:
        destination.mkdir()

    def execute(
        *, version: str, root: Path, argv: tuple[str, ...], cwd: str, timeout_s: float
    ) -> ProcessCapture:
        del root, argv, cwd, timeout_s
        calls.append(version)
        return ProcessCapture(
            status="completed",
            exit_code=0 if version == "fixed" else 1,
            stdout=version.encode(),
            stderr=b"",
            elapsed_ms=1.0,
        )

    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.operations._clone_repository", clone
    )
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.operations._apply_buggy_overlay",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.operations._execute_process", execute
    )

    first = evaluate_test_repetitions(
        root,
        workspace_root=workspace,
        candidate=candidate,
        authorization=_authorization(),
        selection_epoch=SELECTION_EPOCH_V2,
    )
    assert first.state == "passed"
    assert calls == ["fixed", "buggy"] * 3
    assert all(
        attempt_id.endswith("/v2")
        for attempt_id in first.details["attempt_artifact_ids"]
    )
    attempt = load_stable_artifact(
        root,
        artifact_id="candidate_test_attempt/jupyterlab/44/fixed/1/v2",
        artifact_kind="candidate_test_attempt",
        semantic_version="2.0",
    )
    assert "/v2/" in attempt.payload["stdout_path"]

    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.operations._execute_process",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("v2 attempt was executed again")
        ),
    )
    resumed = evaluate_test_repetitions(
        root,
        workspace_root=workspace,
        candidate=candidate,
        authorization=_authorization(),
        selection_epoch=SELECTION_EPOCH_V2,
    )
    assert resumed.details == first.details
