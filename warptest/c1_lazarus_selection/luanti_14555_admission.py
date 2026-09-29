"""Resolve and project the narrowly-scoped Luanti PR 14555 test evidence.

This adapter exists because the generic Luanti manifest classifier originally
treated an added boundary-value corpus as a compile-only API probe.  It writes
no synthetic runtime evidence: the projection ends at test repetitions and
leaves version, replay, oracle, golden, visual, and construction-lineage gates
for their registered executors.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .epoch import SELECTION_EPOCH_V2
from .models import CandidateKey
from .operations import evaluate_test_manifest
from .pipeline import (
    StageDecision,
    V2PreannotationBatchContext,
    run_preannotation_pipeline,
)
from .source_atomicity import (
    load_source_atomicity_artifact,
    load_source_atomicity_batch_context,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATE = CandidateKey("luanti", 14555)
TASK_ID = "lz-luanti-14555-negative-half-pointing"
F2P_ID = "luanti-pr14555-voxel-line-iterator-f2p"
P2P_ID = "luanti-vector-zero-construction-p2p"
TEST_PATH = "src/unittest/test_voxelalgorithms.cpp"
PRODUCTION_PATH = "src/voxelalgorithms.cpp"
_V2 = "2.0"
_PREPARE_IDS = (
    "luanti-unit-test-remove-stale-cache",
    "luanti-unit-test-remove-stale-cmake-files",
    "luanti-unit-test-reconfigure",
    "luanti-unit-test-rebuild",
)

REQUIRED_RUNTIME_VISUAL_EVIDENCE = (
    "complete_fixed_buggy_application_version_pair",
    "registered_world_task_and_start",
    "ordered_reference_replay_with_actions_and_waits",
    "raw_fixed_and_buggy_start_observations",
    "semantic_pointing_goal_defect_and_invariant_observations",
    "common_before_fixed_after_and_buggy_after_goldens",
    "pairwise_before_visual_equivalence",
    "two_by_two_after_visual_cross_matrix",
    "same_lineage_construction_receipt",
)


@dataclass(frozen=True)
class Luanti14555RuntimeClosureBlocker:
    """Read-only boundary after the qualified unit-test projection."""

    candidate: CandidateKey
    unit_transition: ArtifactReference
    reason: str
    missing_required_evidence: tuple[str, ...]
    registered_asset_paths: tuple[str, ...]
    golden_disposition: str = "required_no_not_applicable"

    @property
    def ready(self) -> bool:
        return False


def project_luanti_14555_tests_to_v2(
    artifact_root: str | Path,
    *,
    workspace_root: str | Path,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    """Replace the false terminal and bind the real 3x matrix into v2.

    Returns the corrected manifest, the transition receipt, and the formal
    test-repetitions stage.  Every old attempt is reloaded and checked; no old
    verdict is accepted merely because its aggregate says ``qualified``.
    """

    _require_authorization(authorization)
    root = Path(artifact_root).resolve(strict=True)
    workspace = Path(workspace_root).resolve(strict=True)
    atomicity_context = load_source_atomicity_batch_context(
        root,
        repository_key=CANDIDATE.repository_key,
    )
    batch = V2PreannotationBatchContext.from_source_atomicity(atomicity_context)
    if CANDIDATE.change_number not in batch.candidate_numbers:
        raise RuntimeError("Luanti 14555 is outside the current v2 population")
    decision = evaluate_test_manifest(
        root,
        CANDIDATE,
        selection_epoch=SELECTION_EPOCH_V2,
        batch_context=batch,
    )
    expected_details = {
        "executable_test_paths": [TEST_PATH],
        "test_count": 1,
        "execution_candidate_kind": "expanded_regression_corpus",
    }
    if decision != StageDecision.passed(expected_details):
        raise RuntimeError(
            "Luanti 14555 manifest conflict is not mechanically resolved"
        )

    patch = _load(
        root,
        ArtifactReference(
            "candidate_test_patch/luanti/14555/screening/v2",
            "candidate_test_patch",
            _V2,
        ),
    )
    if patch.payload.get("production_paths") != [PRODUCTION_PATH] or patch.payload.get(
        "test_paths"
    ) != [TEST_PATH]:
        raise RuntimeError("Luanti 14555 patch inventory drifted")
    atomicity = load_source_atomicity_artifact(
        batch_context=atomicity_context,
        candidate=CANDIDATE,
    )
    if (
        atomicity.outcome != "accepted"
        or atomicity.primary_issue_number != 14554
        or atomicity.authoritative_issue_numbers != (14554,)
    ):
        raise RuntimeError("Luanti 14555 source atomicity is not uniquely accepted")

    manifest = _write_manifest(
        root,
        patch=patch,
        atomicity=atomicity.artifact,
        details=expected_details,
        authorization=authorization,
    )
    selection, aggregate, attempts = _load_real_three_by_two_matrix(root)
    _validate_strict_inverse_and_frozen_test(
        root,
        workspace,
        patch=patch,
    )
    transition = _write_transition(
        root,
        manifest=manifest,
        selection=selection,
        aggregate=aggregate,
        attempts=attempts,
        authorization=authorization,
    )

    details = {
        "fixed_f2p_passes": 3,
        "buggy_f2p_failures": 3,
        "fixed_p2p_passes": 3,
        "buggy_p2p_passes": 3,
        "next_gate": "version_materialization",
        "attempt_artifact_ids": [value.reference.artifact_id for value in attempts],
        "evidence_artifacts": [
            {
                "artifact_id": transition.reference.artifact_id,
                "artifact_kind": transition.reference.artifact_kind,
                "semantic_version": transition.reference.semantic_version,
            }
        ],
    }

    def should_not_rerun_manifest(_candidate: CandidateKey) -> StageDecision:
        raise RuntimeError("corrected Luanti 14555 manifest was not resumed")

    def project_repetitions(_candidate: CandidateKey) -> StageDecision:
        return StageDecision.passed(details)

    result = run_preannotation_pipeline(
        root,
        candidates=(CANDIDATE,),
        operations={
            "test_manifest": should_not_rerun_manifest,
            "test_repetitions": project_repetitions,
        },
        authorization=authorization,
        selection_epoch=SELECTION_EPOCH_V2,
        through_stage="test_repetitions",
        batch_contexts={"luanti": batch},
        test_workers=1,
        build_workers=1,
        replay_workers=1,
        persistence_workers=1,
        reload_workers=1,
        queue_limit=1,
    )
    if result.active != 1 or result.terminal_counts:
        raise RuntimeError("Luanti 14555 test projection did not remain active")
    stage = _load(
        root,
        ArtifactReference(
            "candidate_stage/luanti/14555/test_repetitions/v2",
            "candidate_stage",
            _V2,
        ),
    )
    if stage.inputs != (manifest.reference, transition.reference) or stage.payload != {
        "candidate": CANDIDATE.to_value(),
        "stage": "test_repetitions",
        "state": "passed",
        "outcome": None,
        "details": details,
        "complete": True,
    }:
        raise RuntimeError("Luanti 14555 formal test stage reload drifted")
    return manifest, transition, stage


def audit_luanti_14555_runtime_closure(
    artifact_root: str | Path,
    *,
    workspace_root: str | Path,
) -> Luanti14555RuntimeClosureBlocker:
    """Reload the passed test gate and report the unclosed runtime contract."""

    root = Path(artifact_root).resolve(strict=True)
    workspace = Path(workspace_root).resolve(strict=True)
    manifest = _load(
        root,
        ArtifactReference(
            "candidate_stage/luanti/14555/test_manifest/v2",
            "candidate_stage",
            _V2,
        ),
    )
    transition = _load(
        root,
        ArtifactReference(
            "candidate_test_transition/luanti/14555/current/v2",
            "candidate_test_transition",
            _V2,
        ),
    )
    stage = _load(
        root,
        ArtifactReference(
            "candidate_stage/luanti/14555/test_repetitions/v2",
            "candidate_stage",
            _V2,
        ),
    )
    details = stage.payload.get("details")
    if (
        stage.inputs != (manifest.reference, transition.reference)
        or stage.payload.get("candidate") != CANDIDATE.to_value()
        or stage.payload.get("stage") != "test_repetitions"
        or stage.payload.get("state") != "passed"
        or stage.payload.get("outcome") is not None
        or stage.payload.get("complete") is not True
        or type(details) is not dict
        or details.get("fixed_f2p_passes") != 3
        or details.get("buggy_f2p_failures") != 3
        or details.get("fixed_p2p_passes") != 3
        or details.get("buggy_p2p_passes") != 3
        or details.get("next_gate") != "version_materialization"
    ):
        raise RuntimeError("Luanti 14555 formal test gate is invalid")
    registered: list[str] = []
    asset_root = workspace / "benchmarks/p5/lazarus"
    for path in (
        asset_root / "defects" / TASK_ID,
        asset_root / "authoring" / f"{TASK_ID}.tests.json",
        asset_root / "authoring" / f"{TASK_ID}.version-request.json",
    ):
        if path.exists() and not path.is_symlink():
            registered.append(path.relative_to(workspace).as_posix())
    return Luanti14555RuntimeClosureBlocker(
        candidate=CANDIDATE,
        unit_transition=transition.reference,
        reason="registered_runtime_world_visual_closure_missing",
        missing_required_evidence=REQUIRED_RUNTIME_VISUAL_EVIDENCE,
        registered_asset_paths=tuple(registered),
    )


def _write_manifest(
    root: Path,
    *,
    patch: StableArtifact,
    atomicity: StableArtifact,
    details: dict[str, Any],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = ArtifactReference(
        "candidate_stage/luanti/14555/test_manifest/v2",
        "candidate_stage",
        _V2,
    )
    payload = {
        "candidate": CANDIDATE.to_value(),
        "stage": "test_manifest",
        "state": "passed",
        "outcome": None,
        "details": details,
        "complete": True,
    }
    created_at = _now()
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=created_at,
        inputs=(patch.reference, atomicity.reference),
        payload=payload,
        authorization=authorization,
    )
    manifest = _load(root, reference)
    if (
        manifest.inputs != (patch.reference, atomicity.reference)
        or manifest.payload != payload
    ):
        raise RuntimeError("Luanti 14555 corrected manifest reload drifted")
    checkpoint_reference = ArtifactReference(
        "preannotation_checkpoint/luanti/14555/current/v2",
        "preannotation_checkpoint",
        _V2,
    )
    checkpoint_payload = {
        "candidate": CANDIDATE.to_value(),
        "completed_stages": ["test_manifest"],
        "state": "active",
        "outcome": None,
        "current_artifact_id": reference.artifact_id,
    }
    write_stable_artifact(
        root,
        artifact_id=checkpoint_reference.artifact_id,
        artifact_kind=checkpoint_reference.artifact_kind,
        semantic_version=checkpoint_reference.semantic_version,
        created_at=created_at,
        inputs=(reference,),
        payload=checkpoint_payload,
        authorization=authorization,
    )
    checkpoint = _load(root, checkpoint_reference)
    if checkpoint.inputs != (reference,) or checkpoint.payload != checkpoint_payload:
        raise RuntimeError("Luanti 14555 corrected checkpoint reload drifted")
    return manifest


def _load_real_three_by_two_matrix(
    root: Path,
) -> tuple[StableArtifact, StableArtifact, tuple[StableArtifact, ...]]:
    selection = _load(
        root,
        ArtifactReference(
            "curated_selection/all/current/v1",
            "curated_selection",
            "1.0",
        ),
    )
    rows = selection.payload.get("tasks")
    matches = (
        [row for row in rows if type(row) is dict and row.get("task_id") == TASK_ID]
        if type(rows) is list
        else []
    )
    if len(matches) != 1 or matches[0].get("candidate") != CANDIDATE.to_value():
        raise RuntimeError("Luanti 14555 legacy selection binding is invalid")
    attempts = tuple(
        _load(
            root,
            ArtifactReference(
                f"curated_task_revalidation/luanti/14555/construction/repetition-{index}",
                "curated_construction_repetition",
                "1.0",
            ),
        )
        for index in range(1, 4)
    )
    for index, attempt in enumerate(attempts, start=1):
        if attempt.inputs != (selection.reference,):
            raise RuntimeError("Luanti 14555 repetition selection binding drifted")
        _validate_attempt_payload(attempt.payload, index)
    aggregate = _load(
        root,
        ArtifactReference(
            "curated_task_revalidation/luanti/14555/construction",
            "curated_task_revalidation",
            _V2,
        ),
    )
    expected_inputs = (selection.reference, *(value.reference for value in attempts))
    payload = aggregate.payload
    if (
        aggregate.inputs != expected_inputs
        or payload.get("task_id") != TASK_ID
        or payload.get("candidate") != CANDIDATE.to_value()
        or payload.get("defect_locus") != "at_goal"
        or payload.get("outcome") != "qualified"
        or payload.get("qualified") is not True
        or payload.get("complete") is not True
        or payload.get("fresh_revalidation") is not True
        or payload.get("historical_result_inherited") is not False
        or payload.get("required_repetitions") != 3
        or payload.get("completed_repetitions") != 3
        or payload.get("fixed_tests") != {F2P_ID: True, P2P_ID: True}
        or payload.get("buggy_tests") != {F2P_ID: False, P2P_ID: True}
    ):
        raise RuntimeError("Luanti 14555 revalidation aggregate is invalid")
    repetitions = payload.get("repetitions")
    if type(repetitions) is not list or len(repetitions) != 3:
        raise RuntimeError("Luanti 14555 aggregate repetition table is invalid")
    for index, row in enumerate(repetitions, start=1):
        _validate_attempt_summary(row, index)
    return selection, aggregate, attempts


def _validate_attempt_payload(payload: Any, repetition: int) -> None:
    if type(payload) is not dict:
        raise RuntimeError("Luanti 14555 repetition payload is invalid")
    if (
        payload.get("task_id") != TASK_ID
        or payload.get("candidate") != CANDIDATE.to_value()
        or payload.get("defect_locus") != "at_goal"
        or payload.get("repetition") != repetition
        or payload.get("outcome") != "qualified"
        or payload.get("complete") is not True
        or payload.get("fresh_revalidation") is not True
        or payload.get("historical_result_inherited") is not False
        or payload.get("runner_error") is not None
        or payload.get("fixed_tests") != {F2P_ID: True, P2P_ID: True}
        or payload.get("buggy_tests") != {F2P_ID: False, P2P_ID: True}
    ):
        raise RuntimeError("Luanti 14555 repetition verdict is invalid")
    executions = payload.get("executions")
    if type(executions) is not list or len(executions) != 2:
        raise RuntimeError("Luanti 14555 repetition execution table is invalid")
    by_version = {
        value.get("version"): value for value in executions if type(value) is dict
    }
    if set(by_version) != {"fixed", "buggy"}:
        raise RuntimeError("Luanti 14555 repetition versions are invalid")
    for version, expected in (
        ("fixed", {F2P_ID: True, P2P_ID: True}),
        ("buggy", {F2P_ID: False, P2P_ID: True}),
    ):
        execution = by_version[version]
        prepare = execution.get("prepare_results")
        if (
            type(prepare) is not list
            or [value.get("test_id") for value in prepare] != list(_PREPARE_IDS)
            or any(
                type(value) is not dict
                or value.get("passed") is not True
                or value.get("exit_code") != 0
                or value.get("timed_out") is not False
                for value in prepare
            )
        ):
            raise RuntimeError("Luanti 14555 frozen-test rebuild was incomplete")
        results = execution.get("results")
        if type(results) is not list or len(results) != 2:
            raise RuntimeError("Luanti 14555 test result table is invalid")
        observed = {
            value.get("test_id"): value.get("passed")
            for value in results
            if type(value) is dict
            and value.get("timed_out") is False
            and type(value.get("exit_code")) is int
        }
        if observed != expected:
            raise RuntimeError("Luanti 14555 test truth table is invalid")
        for value in results:
            expected_exit = (
                1 if version == "buggy" and value.get("test_id") == F2P_ID else 0
            )
            if value.get("exit_code") != expected_exit:
                raise RuntimeError("Luanti 14555 test process outcome is invalid")


def _validate_attempt_summary(value: Any, repetition: int) -> None:
    if (
        type(value) is not dict
        or value.get("repetition") != repetition
        or value.get("outcome") != "qualified"
        or value.get("complete") is not True
        or value.get("runner_error") is not None
        or value.get("fixed_tests") != {F2P_ID: True, P2P_ID: True}
        or value.get("buggy_tests") != {F2P_ID: False, P2P_ID: True}
    ):
        raise RuntimeError("Luanti 14555 aggregate repetition summary is invalid")


def _validate_strict_inverse_and_frozen_test(
    root: Path,
    workspace: Path,
    *,
    patch: StableArtifact,
) -> None:
    raw_relative = patch.payload.get("raw_patch_path")
    if type(raw_relative) is not str or not raw_relative:
        raise RuntimeError("Luanti 14555 raw patch path is invalid")
    raw = (root / raw_relative).resolve(strict=True)
    if root != raw and root not in raw.parents:
        raise RuntimeError("Luanti 14555 raw patch escapes the artifact root")
    if not raw.is_file() or raw.is_symlink():
        raise RuntimeError("Luanti 14555 raw patch is not an ordinary file")
    fixed_root = workspace / ".warptest-cache/lazarus/prebuilt" / TASK_ID / "fixed"
    buggy_root = workspace / ".warptest-cache/lazarus/prebuilt" / TASK_ID / "buggy"
    evidence_root = workspace / "benchmarks/p5/lazarus/defects" / TASK_ID
    fixed_production = _ordinary_file(fixed_root / PRODUCTION_PATH, fixed_root)
    buggy_production = _ordinary_file(buggy_root / PRODUCTION_PATH, buggy_root)
    frozen_test = _ordinary_file(
        evidence_root / "upstream-tests" / TEST_PATH, evidence_root
    )
    fixed_test = _ordinary_file(fixed_root / TEST_PATH, fixed_root)
    if _voxel_iterator_test(frozen_test.read_bytes()) != _voxel_iterator_test(
        fixed_test.read_bytes()
    ):
        raise RuntimeError(
            "Luanti 14555 frozen regression body drifted from fixed version"
        )
    production_patch = _single_file_patch(raw.read_bytes(), PRODUCTION_PATH)
    # Keep this scratch tree outside the parent Git worktree.  ``git apply``
    # otherwise discovers the workspace repository and interprets paths from
    # its root instead of from the intended disposable directory.
    with tempfile.TemporaryDirectory(prefix="luanti-14555-inverse-") as temporary:
        temporary_root = Path(temporary)
        destination = temporary_root / PRODUCTION_PATH
        destination.parent.mkdir(parents=True)
        destination.write_bytes(fixed_production.read_bytes())
        result = subprocess.run(
            ["/usr/bin/git", "apply", "--reverse", "--whitespace=nowarn", "-"],
            cwd=temporary_root,
            input=production_patch,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if result.returncode != 0 or result.stdout:
            raise RuntimeError(
                "Luanti 14555 production inverse patch did not apply cleanly"
            )
        if destination.read_bytes() != buggy_production.read_bytes():
            raise RuntimeError(
                "Luanti 14555 production inverse does not match buggy version"
            )


def _single_file_patch(content: bytes, path: str) -> bytes:
    marker = f"diff --git a/{path} b/{path}".encode()
    chunks = content.split(b"diff --git ")
    matches = [
        b"diff --git " + chunk
        for chunk in chunks[1:]
        if (b"diff --git " + chunk).startswith(marker)
    ]
    if len(matches) != 1:
        raise RuntimeError("Luanti 14555 production patch section is not unique")
    return matches[0]


def _voxel_iterator_test(content: bytes) -> bytes:
    start = b"void TestVoxelAlgorithms::testVoxelLineIterator()"
    end = b"void TestVoxelAlgorithms::testLighting(IGameDef *gamedef)"
    if content.count(start) != 1 or content.count(end) != 1:
        raise RuntimeError("Luanti 14555 regression function boundary is invalid")
    body = content.split(start, 1)[1].split(end, 1)[0]
    if not body.strip():
        raise RuntimeError("Luanti 14555 regression function is empty")
    return body


def _write_transition(
    root: Path,
    *,
    manifest: StableArtifact,
    selection: StableArtifact,
    aggregate: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = ArtifactReference(
        "candidate_test_transition/luanti/14555/current/v2",
        "candidate_test_transition",
        _V2,
    )
    inputs = (
        manifest.reference,
        selection.reference,
        aggregate.reference,
        *(value.reference for value in attempts),
    )
    payload = {
        "candidate": CANDIDATE.to_value(),
        "task_id": TASK_ID,
        "source": "fresh_isolated_construction_repetitions",
        "historical_result_inherited": False,
        "required_repetitions": 3,
        "completed_repetitions": 3,
        "truth_table": {
            "fixed": {"f2p": True, "p2p": True},
            "buggy": {"f2p": False, "p2p": True},
        },
        "strict_inverse_paths": [PRODUCTION_PATH],
        "frozen_test_paths": [TEST_PATH],
        "complete": True,
    }
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    transition = _load(root, reference)
    if transition.inputs != inputs or transition.payload != payload:
        raise RuntimeError("Luanti 14555 transition reload drifted")
    return transition


def _ordinary_file(path: Path, root: Path) -> Path:
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    if resolved_root != resolved and resolved_root not in resolved.parents:
        raise RuntimeError("Luanti 14555 evidence path escapes its root")
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError("Luanti 14555 evidence is not an ordinary file")
    return resolved


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("Luanti 14555 admission authorization is inactive")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "Luanti14555RuntimeClosureBlocker",
    "REQUIRED_RUNTIME_VISUAL_EVIDENCE",
    "audit_luanti_14555_runtime_closure",
    "project_luanti_14555_tests_to_v2",
]
