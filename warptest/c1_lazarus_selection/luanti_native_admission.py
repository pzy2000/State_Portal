"""Project closed Luanti native regression evidence into the formal v2 DAG.

The projection is deliberately preparation-only.  It never builds, starts a
candidate process, or invents a runtime/visual exception.  It validates the
current v2 source chain, atomicity receipt, fixed capsule, the twelve v4
receipts and their aggregate before writing the formal test-repetitions stage.
"""

from __future__ import annotations

import base64
import binascii
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Mapping

from .epoch import SELECTION_EPOCH_V2
from .luanti_native_executor import completed_native_test_count
from .luanti_native_regression import (
    NativeRegressionProfile,
    NativeRegressionRun,
    profile_for_candidate,
)
from .models import CandidateKey
from .operations import evaluate_test_manifest
from .pipeline import (
    StageDecision,
    V2PreannotationBatchContext,
    run_preannotation_pipeline,
)
from .source_atomicity import (
    SourceAtomicityReceipt,
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


_V2 = "2.0"
_V3 = "3.0"
_V4 = "4.0"
_ATTEMPT_FIELDS = frozenset(
    {
        "candidate",
        "profile_id",
        "phase",
        "variant",
        "repetition",
        "selector",
        "expected_outcome",
        "observed_outcome",
        "process_outcome",
        "returncode",
        "completed_test_count",
        "stdout_base64",
        "stderr_base64",
        "stdout_truncated",
        "stderr_truncated",
        "output_encoding",
        "matched_expected",
    }
)
_AGGREGATE_FIELDS = frozenset(
    {
        "candidate",
        "profile_id",
        "repetitions_per_cell",
        "attempt_count",
        "stable_repetitions",
        "all_expected",
        "qualified",
        "cells",
    }
)
_TRANSITION_FIELDS = frozenset(
    {
        "candidate",
        "profile_id",
        "repetitions_per_cell",
        "attempt_count",
        "stable_repetitions",
        "all_expected",
        "qualified",
        "truth_table",
        "complete",
    }
)
_CELL_ORDER = tuple(
    (phase, variant) for phase in ("f2p", "p2p") for variant in ("fixed", "buggy")
)

REQUIRED_RUNTIME_VISUAL_EVIDENCE = (
    "registered_lazarus_task_mapping",
    "complete_fixed_buggy_application_version_pair",
    "ordered_reference_replay_with_actions_and_waits",
    "raw_fixed_and_buggy_start_observations",
    "semantic_goal_defect_and_invariant_observations",
    "common_before_fixed_after_and_buggy_after_goldens",
    "pairwise_before_visual_equivalence",
    "two_by_two_after_visual_cross_matrix",
    "same_lineage_construction_receipt",
)

DOWNSTREAM_REPAIRABLE_ACTIONS = (
    "register_issue_15518_task_mapping",
    "freeze_minimal_russian_po_game_fixture",
    "materialize_complete_fixed_buggy_application_pair",
    "launch_isolated_russian_locale_world_pair",
    "capture_ordered_replay_and_raw_start_observations",
    "define_semantic_error_and_invariant_oracles",
    "capture_common_before_and_cross_matrix_goldens",
    "persist_same_lineage_construction_receipt",
)

_TASK_FIXTURE_ROOT = "private/downstream-fixtures/luanti/15519/russian-po-game-v1"
_TASK_FIXTURE_FILES = {
    "game.conf": "name = c1_plural\ntitle = C1 Russian Plural Fixture\n",
    "minetest.conf": "language = ru\nenable_console = true\n",
    "mods/c1_plural/mod.conf": "name = c1_plural\ndescription = C1 Russian plural parser fixture\n",
    "mods/c1_plural/init.lua": (
        'local S = minetest.get_translator("c1_plural")\n'
        'local translated = core.get_translated_string("ru", S("apple"))\n'
        'minetest.log("action", "C1 plural fixture loaded: " .. translated)\n'
    ),
    "mods/c1_plural/locale/c1_plural.ru.po": (
        'msgid ""\n'
        'msgstr ""\n'
        '"Project-Id-Version: c1_plural\\n"\n'
        '"Language: ru\\n"\n'
        '"Content-Type: text/plain; charset=UTF-8\\n"\n'
        '"Plural-Forms: nplurals=3; plural=n%10==1 && n%100!=11 ? 0 : '
        'n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2;\\n"\n'
        "\n"
        'msgid "apple"\n'
        'msgid_plural "apples"\n'
        'msgstr[0] "яблоко"\n'
        'msgstr[1] "яблока"\n'
        'msgstr[2] "яблок"\n'
    ),
}


@dataclass(frozen=True)
class LuantiNativeAdmissionProjection:
    """The exact formal boundary reached by native regression evidence."""

    candidate: CandidateKey
    manifest: StableArtifact
    transition: StableArtifact
    stage: StableArtifact
    checkpoint: StableArtifact
    qualified: bool
    next_gate: Literal["version_materialization"] | None
    manifest_rebound: bool
    resumed: bool


@dataclass(frozen=True)
class NativeRuntimeVisualClosureBlocker:
    """Read-only blocker; this is intentionally not a pipeline terminal."""

    candidate: CandidateKey
    unit_transition: ArtifactReference
    reason: str
    missing_required_evidence: tuple[str, ...]
    golden_disposition: Literal["required_no_not_applicable"]
    registered_asset_paths: tuple[str, ...]


def write_luanti_native_downstream_preflight(
    artifact_root: str | Path,
    *,
    workspace_root: str | Path,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist a resumable pending receipt without demoting admission.

    The admission projector replays every current typed prerequisite and all
    twelve native attempts, but performs no candidate process execution.  A
    missing visual task mapping is therefore recorded as downstream work, not
    as a source/patch/test terminal.
    """

    _require_authorization(authorization)
    root = Path(artifact_root).resolve(strict=True)
    projection = project_luanti_native_regression_to_v2(
        root,
        candidate=candidate,
        authorization=authorization,
    )
    if (
        not projection.qualified
        or projection.checkpoint.payload.get("state") != "active"
    ):
        raise RuntimeError("native candidate admission is not active and qualified")
    blocker = audit_luanti_native_runtime_visual_closure(
        root,
        workspace_root=workspace_root,
        candidate=candidate,
    )
    reference = _downstream_preflight_reference(projection.candidate)
    payload = _downstream_preflight_payload(
        profile_id=projection.transition.payload["profile_id"],
        candidate=projection.candidate,
        blocker=blocker,
    )
    inputs = (
        projection.transition.reference,
        projection.stage.reference,
        projection.checkpoint.reference,
    )
    try:
        return _load_downstream_preflight(
            root,
            workspace_root=workspace_root,
            candidate=candidate,
            reference=reference,
        )
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
            "native downstream asset inventory changed",
        }:
            raise
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
    return _load_downstream_preflight(
        root,
        workspace_root=workspace_root,
        candidate=candidate,
        reference=reference,
    )


def load_luanti_native_downstream_preflight(
    artifact_root: str | Path,
    *,
    workspace_root: str | Path,
    candidate: CandidateKey,
) -> StableArtifact:
    """Strictly reload the current downstream-pending receipt."""

    root = Path(artifact_root).resolve(strict=True)
    return _load_downstream_preflight(
        root,
        workspace_root=workspace_root,
        candidate=candidate,
        reference=_downstream_preflight_reference(candidate),
    )


def write_luanti_native_task_mapping(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Freeze the issue-derived replay mapping and minimal Russian PO fixture."""

    _require_authorization(authorization)
    root = Path(artifact_root).resolve(strict=True)
    projection = project_luanti_native_regression_to_v2(
        root,
        candidate=candidate,
        authorization=authorization,
    )
    if not projection.qualified:
        raise RuntimeError("native candidate admission is not active and qualified")
    reference = _task_mapping_reference(candidate)
    try:
        return load_luanti_native_task_mapping(root, candidate=candidate)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
    _write_task_fixture(root)
    payload = _task_mapping_payload(
        candidate, projection.transition.payload["profile_id"]
    )
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=(
            projection.transition.reference,
            projection.stage.reference,
            projection.checkpoint.reference,
        ),
        payload=payload,
        authorization=authorization,
    )
    return load_luanti_native_task_mapping(root, candidate=candidate)


def load_luanti_native_task_mapping(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
) -> StableArtifact:
    """Strictly load the stable task mapping and its exact fixture files."""

    root = Path(artifact_root).resolve(strict=True)
    profile = profile_for_candidate(candidate)
    transition, stage, checkpoint = _load_active_admission_boundary(root, profile)
    mapping = _load(root, _task_mapping_reference(candidate))
    expected = _task_mapping_payload(candidate, profile.profile_id)
    if (
        mapping.inputs
        != (
            transition.reference,
            stage.reference,
            checkpoint.reference,
        )
        or mapping.payload != expected
    ):
        raise RuntimeError("native task mapping is invalid")
    _validate_task_fixture(root)
    return mapping


def project_luanti_native_regression_to_v2(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
) -> LuantiNativeAdmissionProjection:
    """Project current native v4 receipts through formal test repetitions.

    A legacy manifest head that has the exact mechanically recomputed payload
    but predates the v2 atomicity input is rebound once to that accepted input.
    This is a dependency repair, not reuse of an old verdict.  No process is
    available from this module.
    """

    profile = profile_for_candidate(candidate)
    _require_authorization(authorization)
    root = Path(artifact_root).resolve(strict=True)
    atomicity_context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )
    batch_context = V2PreannotationBatchContext.from_source_atomicity(atomicity_context)
    source, patch = _load_screening_prefix(root, profile, batch_context)
    atomicity = load_source_atomicity_artifact(
        batch_context=atomicity_context,
        candidate=candidate,
    )
    if atomicity.outcome != "accepted" or atomicity.primary_issue_number is None:
        raise RuntimeError("current v2 source atomicity is not accepted")
    manifest, manifest_rebound = _load_or_rebind_manifest(
        root,
        profile=profile,
        patch=patch,
        atomicity=atomicity,
        batch_context=batch_context,
        authorization=authorization,
    )
    _load_manifest_checkpoint(root, profile, manifest)
    capsule = _load_capsule(root, profile, source, patch, manifest)
    attempts = _load_attempts(root, profile, capsule)
    aggregate = _load_aggregate(root, profile, attempts)
    truth_table = _truth_table(attempts)
    transition, transition_resumed = _load_or_write_transition(
        root,
        profile=profile,
        manifest=manifest,
        capsule=capsule,
        attempts=attempts,
        aggregate=aggregate,
        truth_table=truth_table,
        authorization=authorization,
    )
    details = _stage_details(transition, attempts)

    def unexpected_manifest(_candidate: CandidateKey) -> StageDecision:
        raise RuntimeError("current v2 test manifest checkpoint is missing")

    def project_repetitions(_candidate: CandidateKey) -> StageDecision:
        if transition.payload["qualified"] is True:
            return StageDecision.passed(details)
        outcome = (
            "flake_observed"
            if transition.payload["stable_repetitions"] is False
            else "quality_rejected"
        )
        return StageDecision.terminal(outcome, details)

    pipeline_result = run_preannotation_pipeline(
        root,
        candidates=(candidate,),
        operations={
            "test_manifest": unexpected_manifest,
            "test_repetitions": project_repetitions,
        },
        authorization=authorization,
        selection_epoch=SELECTION_EPOCH_V2,
        through_stage="test_repetitions",
        batch_contexts={candidate.repository_key: batch_context},
        test_workers=1,
        build_workers=1,
        replay_workers=1,
        persistence_workers=1,
        reload_workers=1,
        queue_limit=1,
    )
    qualified = transition.payload["qualified"] is True
    if qualified:
        if pipeline_result.active != 1 or pipeline_result.terminal_counts:
            raise RuntimeError("qualified native admission did not remain active")
    elif pipeline_result.terminal_counts not in (
        {"quality_rejected": 1},
        {"flake_observed": 1},
    ):
        raise RuntimeError("unqualified native admission did not terminate")
    stage, checkpoint = _load_formal_projection(
        root,
        profile=profile,
        manifest=manifest,
        transition=transition,
        details=details,
        qualified=qualified,
    )
    return LuantiNativeAdmissionProjection(
        candidate=candidate,
        manifest=manifest,
        transition=transition,
        stage=stage,
        checkpoint=checkpoint,
        qualified=qualified,
        next_gate="version_materialization" if qualified else None,
        manifest_rebound=manifest_rebound,
        resumed=transition_resumed and pipeline_result.resumed == 1,
    )


def audit_luanti_native_runtime_visual_closure(
    artifact_root: str | Path,
    *,
    workspace_root: str | Path,
    candidate: CandidateKey,
) -> NativeRuntimeVisualClosureBlocker:
    """Return the exact unclosed runtime/visual contract without writing.

    Existing candidate-specific Lazarus assets are inventoried, but their mere
    presence cannot satisfy any gate.  A future positive path must use a
    dedicated typed runtime closure loader before materialization is allowed.
    """

    profile = profile_for_candidate(candidate)
    root = Path(artifact_root).resolve(strict=True)
    workspace = Path(workspace_root).resolve(strict=True)
    transition = _load(
        root,
        _transition_reference(profile),
    )
    _load_test_repetitions_stage(
        root,
        profile,
        transition=transition.reference,
    )
    asset_root = workspace / "benchmarks" / "p5" / "lazarus"
    registered: list[str] = []
    if asset_root.is_dir() and not asset_root.is_symlink():
        token = f"lz-luanti-{candidate.change_number}-"
        for directory in (asset_root / "defects", asset_root / "authoring"):
            if directory.is_dir() and not directory.is_symlink():
                for path in sorted(directory.glob(f"{token}*")):
                    registered.append(path.relative_to(workspace).as_posix())
    missing = list(REQUIRED_RUNTIME_VISUAL_EVIDENCE)
    try:
        mapping = load_luanti_native_task_mapping(root, candidate=candidate)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
    else:
        if mapping.payload["closure_state"] != "mapping_registered_fixture_frozen":
            raise RuntimeError("native task mapping closure state is invalid")
        missing.remove("registered_lazarus_task_mapping")
        registered.append(mapping.payload["fixture_root"])
    return NativeRuntimeVisualClosureBlocker(
        candidate=profile.candidate,
        unit_transition=transition.reference,
        reason="registered_runtime_world_visual_closure_missing",
        missing_required_evidence=tuple(missing),
        golden_disposition="required_no_not_applicable",
        registered_asset_paths=tuple(registered),
    )


def _task_mapping_reference(candidate: CandidateKey) -> ArtifactReference:
    if candidate != CandidateKey("luanti", 15519):
        raise ValueError("native task mapping candidate is unsupported")
    return ArtifactReference(
        "lazarus_task_mapping/luanti/15519/issue-15518/v2",
        "lazarus_task_mapping",
        _V2,
    )


def _task_mapping_payload(
    candidate: CandidateKey, profile_id: object
) -> dict[str, object]:
    if type(profile_id) is not str or not profile_id:
        raise RuntimeError("native task mapping profile identity is invalid")
    return {
        "candidate": candidate.to_value(),
        "profile_id": profile_id,
        "task_id": "lz-luanti-15519-russian-plural-form",
        "issue_number": 15518,
        "defect_locus": "at_goal",
        "goal": (
            "start the frozen Russian game fixture without an Invalid Plural-Forms error"
        ),
        "fixture_root": _TASK_FIXTURE_ROOT,
        "fixture_paths": sorted(_TASK_FIXTURE_FILES),
        "replay_actions": [
            "launch_registered_version_with_frozen_config_and_isolated_world",
            "start_c1_plural_game",
            "wait_for_world_start_and_translation_load",
            "observe_startup_log_and_application_frame",
        ],
        "semantic_oracles": {
            "goal": "Invalid Plural-Forms line is absent after fixture translation load",
            "defect": "Invalid Plural-Forms line is present after fixture translation load",
            "invariant": "the isolated world starts and emits C1 plural fixture loaded",
        },
        "closure_state": "mapping_registered_fixture_frozen",
        "visual_closure_state": "pending",
        "golden_disposition": "required_no_not_applicable",
        "complete": True,
    }


def _write_task_fixture(root: Path) -> None:
    fixture = root / PurePosixPath(_TASK_FIXTURE_ROOT)
    for relative, content in _TASK_FIXTURE_FILES.items():
        path = fixture / PurePosixPath(relative)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or path.read_text() != content:
                raise RuntimeError("native task fixture conflicts with its stable path")
            continue
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
    _validate_task_fixture(root)


def _validate_task_fixture(root: Path) -> None:
    fixture = _ordinary_directory(root / PurePosixPath(_TASK_FIXTURE_ROOT), root)
    actual: list[str] = []
    for path in fixture.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
        ):
            raise RuntimeError("native task fixture contains an unsafe entry")
        if stat.S_ISREG(info.st_mode):
            actual.append(path.relative_to(fixture).as_posix())
    if sorted(actual) != sorted(_TASK_FIXTURE_FILES):
        raise RuntimeError("native task fixture inventory is invalid")
    for relative, expected in _TASK_FIXTURE_FILES.items():
        path = _ordinary_file(fixture / PurePosixPath(relative), fixture)
        if path.read_text(encoding="utf-8") != expected:
            raise RuntimeError("native task fixture content is invalid")


def _downstream_preflight_reference(candidate: CandidateKey) -> ArtifactReference:
    if candidate.repository_key != "luanti" or candidate.change_number != 15519:
        raise ValueError("native downstream preflight candidate is unsupported")
    return ArtifactReference(
        "downstream_preflight/luanti/15519/runtime-visual/v2",
        "downstream_preflight",
        _V2,
    )


def _downstream_preflight_payload(
    *,
    profile_id: object,
    candidate: CandidateKey,
    blocker: NativeRuntimeVisualClosureBlocker,
) -> dict[str, object]:
    if type(profile_id) is not str or not profile_id:
        raise RuntimeError("native downstream profile identity is invalid")
    return {
        "candidate": candidate.to_value(),
        "profile_id": profile_id,
        "admission_state": "active_qualified",
        "downstream_state": "pending",
        "outcome": None,
        "reason": blocker.reason,
        "missing_required_evidence": list(blocker.missing_required_evidence),
        "golden_disposition": blocker.golden_disposition,
        "registered_asset_paths": list(blocker.registered_asset_paths),
        "repairable_actions": list(DOWNSTREAM_REPAIRABLE_ACTIONS),
        "complete": True,
    }


def _load_downstream_preflight(
    root: Path,
    *,
    workspace_root: str | Path,
    candidate: CandidateKey,
    reference: ArtifactReference,
) -> StableArtifact:
    profile = profile_for_candidate(candidate)
    transition, stage, checkpoint = _load_active_admission_boundary(root, profile)
    blocker = audit_luanti_native_runtime_visual_closure(
        root,
        workspace_root=workspace_root,
        candidate=candidate,
    )
    artifact = _load(root, reference)
    expected_payload = _downstream_preflight_payload(
        profile_id=profile.profile_id,
        candidate=candidate,
        blocker=blocker,
    )
    if artifact.inputs != (
        transition.reference,
        stage.reference,
        checkpoint.reference,
    ):
        raise RuntimeError("native downstream preflight binding is invalid")
    if artifact.payload != expected_payload:
        if (
            artifact.payload.get("registered_asset_paths")
            != expected_payload["registered_asset_paths"]
        ):
            raise RuntimeError("native downstream asset inventory changed")
        raise RuntimeError("native downstream preflight is invalid")
    return artifact


def _load_active_admission_boundary(
    root: Path,
    profile: NativeRegressionProfile,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    candidate = profile.candidate
    transition = _load(root, _transition_reference(profile))
    _validate_qualified_transition(profile, transition)
    stage = _load_test_repetitions_stage(
        root,
        profile,
        transition=transition.reference,
    )
    checkpoint = _load(
        root,
        ArtifactReference(
            f"preannotation_checkpoint/luanti/{candidate.change_number}/current/v2",
            "preannotation_checkpoint",
            _V2,
        ),
    )
    expected_checkpoint = {
        "candidate": candidate.to_value(),
        "completed_stages": ["test_manifest", "test_repetitions"],
        "state": "active",
        "outcome": None,
        "current_artifact_id": stage.reference.artifact_id,
    }
    if (
        checkpoint.inputs != (stage.reference,)
        or checkpoint.payload != expected_checkpoint
    ):
        raise RuntimeError("native downstream admission checkpoint is invalid")
    return transition, stage, checkpoint


def _validate_qualified_transition(
    profile: NativeRegressionProfile,
    transition: StableArtifact,
) -> None:
    expected_inputs = (
        profile.test_manifest_reference,
        profile.fixed_capsule_reference,
        *(
            ArtifactReference(
                "native_regression_attempt/luanti/"
                f"{profile.candidate.change_number}/{phase}/{variant}/{repetition}/v4",
                "native_regression_attempt",
                _V4,
            )
            for phase, variant in _CELL_ORDER
            for repetition in (1, 2, 3)
        ),
        ArtifactReference(
            "native_regression_aggregate/luanti/"
            f"{profile.candidate.change_number}/fixed-buggy/v4",
            "native_regression_aggregate",
            _V4,
        ),
    )
    expected_payload = {
        "candidate": profile.candidate.to_value(),
        "profile_id": profile.profile_id,
        "repetitions_per_cell": 3,
        "attempt_count": 12,
        "stable_repetitions": True,
        "all_expected": True,
        "qualified": True,
        "truth_table": {
            "fixed": {"f2p": True, "p2p": True},
            "buggy": {"f2p": False, "p2p": True},
        },
        "complete": True,
    }
    if transition.inputs != expected_inputs or transition.payload != expected_payload:
        raise RuntimeError("qualified native transition is invalid")


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("native admission authorization is invalid")
    if (
        authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("native admission authorization is inactive")


def _load_screening_prefix(
    root: Path,
    profile: NativeRegressionProfile,
    batch_context: V2PreannotationBatchContext,
) -> tuple[StableArtifact, StableArtifact]:
    if profile.candidate.change_number not in batch_context.candidate_numbers:
        raise RuntimeError("native candidate is outside the current v2 enumeration")
    enumeration = batch_context.enumeration_reference
    source = _load(root, profile.source_packet_reference)
    patch = _load(root, profile.patch_reference)
    if source.inputs != (enumeration,) or source.payload != {
        "candidate": profile.candidate.to_value(),
        "raw_evidence_path": source.payload.get("raw_evidence_path"),
        "complete": True,
    }:
        raise RuntimeError("native source packet is invalid")
    if (
        type(source.payload["raw_evidence_path"]) is not str
        or not source.payload["raw_evidence_path"]
    ):
        raise RuntimeError("native source packet is invalid")
    expected_patch = {
        "candidate": profile.candidate.to_value(),
        "outcome": "patch_eligible",
        "production_paths": list(profile.production_paths),
        "test_paths": list(profile.test_paths),
        "source_manifest_path": source.payload["raw_evidence_path"],
        "raw_patch_path": patch.payload.get("raw_patch_path"),
        "complete": True,
    }
    if (
        patch.inputs != (enumeration, source.reference)
        or patch.payload != expected_patch
        or type(patch.payload["raw_patch_path"]) is not str
        or not patch.payload["raw_patch_path"]
    ):
        raise RuntimeError("native test patch is invalid")
    checkpoint = _load(
        root,
        ArtifactReference(
            f"screening_checkpoint/luanti/{profile.candidate.change_number}/current/v2",
            "screening_checkpoint",
            _V2,
        ),
    )
    if checkpoint.inputs != (enumeration, patch.reference) or checkpoint.payload != {
        "candidate": profile.candidate.to_value(),
        "state": "complete",
        "outcome": "patch_eligible",
        "stage_artifact_id": patch.reference.artifact_id,
    }:
        raise RuntimeError("native screening checkpoint is invalid")
    return source, patch


def _load_or_rebind_manifest(
    root: Path,
    *,
    profile: NativeRegressionProfile,
    patch: StableArtifact,
    atomicity: SourceAtomicityReceipt,
    batch_context: V2PreannotationBatchContext,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, bool]:
    recomputed = evaluate_test_manifest(
        root,
        profile.candidate,
        selection_epoch=SELECTION_EPOCH_V2,
        batch_context=batch_context,
    )
    if recomputed.state != "passed":
        raise RuntimeError("current v2 native test manifest no longer passes")
    manifest = _load(root, profile.test_manifest_reference)
    expected_payload = {
        "candidate": profile.candidate.to_value(),
        "stage": "test_manifest",
        "state": "passed",
        "outcome": None,
        "details": dict(recomputed.details),
        "complete": True,
    }
    if manifest.payload != expected_payload:
        raise RuntimeError("current v2 native test manifest payload is invalid")
    formal_inputs = (patch.reference, atomicity.artifact.reference)
    rebound = False
    if manifest.inputs == (patch.reference,):
        write_stable_artifact(
            root,
            artifact_id=manifest.reference.artifact_id,
            artifact_kind=manifest.reference.artifact_kind,
            semantic_version=manifest.reference.semantic_version,
            created_at=_now(),
            inputs=formal_inputs,
            payload=expected_payload,
            authorization=authorization,
        )
        manifest = _load(root, profile.test_manifest_reference)
        rebound = True
    if manifest.inputs != formal_inputs or manifest.payload != expected_payload:
        raise RuntimeError("current v2 native test manifest binding is invalid")
    return manifest, rebound


def _load_manifest_checkpoint(
    root: Path,
    profile: NativeRegressionProfile,
    manifest: StableArtifact,
) -> StableArtifact:
    checkpoint = _load(
        root,
        ArtifactReference(
            f"preannotation_checkpoint/luanti/{profile.candidate.change_number}/current/v2",
            "preannotation_checkpoint",
            _V2,
        ),
    )
    if (
        set(checkpoint.payload)
        != {
            "candidate",
            "completed_stages",
            "state",
            "outcome",
            "current_artifact_id",
        }
        or checkpoint.payload.get("candidate") != profile.candidate.to_value()
    ):
        raise RuntimeError("current v2 native checkpoint schema is invalid")
    completed = checkpoint.payload.get("completed_stages")
    if completed == ["test_manifest"]:
        expected = {
            "candidate": profile.candidate.to_value(),
            "completed_stages": ["test_manifest"],
            "state": "active",
            "outcome": None,
            "current_artifact_id": manifest.reference.artifact_id,
        }
        if checkpoint.inputs != (manifest.reference,) or checkpoint.payload != expected:
            raise RuntimeError("current v2 native manifest checkpoint is invalid")
    elif completed == ["test_manifest", "test_repetitions"]:
        stage_reference = ArtifactReference(
            f"candidate_stage/luanti/{profile.candidate.change_number}/test_repetitions/v2",
            "candidate_stage",
            _V2,
        )
        if (
            checkpoint.inputs != (stage_reference,)
            or checkpoint.payload.get("state") not in {"active", "terminal"}
            or checkpoint.payload.get("current_artifact_id")
            != stage_reference.artifact_id
        ):
            raise RuntimeError("current v2 native repetitions checkpoint is invalid")
    else:
        raise RuntimeError("native admission checkpoint is outside its boundary")
    return checkpoint


def _load_capsule(
    root: Path,
    profile: NativeRegressionProfile,
    source: StableArtifact,
    patch: StableArtifact,
    manifest: StableArtifact,
) -> StableArtifact:
    capsule = _load(root, profile.fixed_capsule_reference)
    expected = {
        "candidate": profile.candidate.to_value(),
        "profile_id": profile.profile_id,
        "source_tree_path": profile.fixed_source_tree_path,
        "source_manifest_path": source.payload["raw_evidence_path"],
        "test_paths": list(profile.test_paths),
        "p2p_test_paths": list(profile.p2p_test_paths),
        "support_paths": list(profile.support_paths),
    }
    if (
        capsule.inputs != (source.reference, patch.reference, manifest.reference)
        or capsule.payload != expected
    ):
        raise RuntimeError("native fixed capsule binding is invalid")
    tree = _ordinary_directory(
        root / PurePosixPath(profile.fixed_source_tree_path),
        root,
    )
    for relative in (
        *profile.production_paths,
        *profile.support_paths,
        *profile.test_paths,
        *profile.p2p_test_paths,
    ):
        _ordinary_file(tree / PurePosixPath(relative), tree)
    return capsule


def _run(profile: NativeRegressionProfile, phase: str) -> NativeRegressionRun:
    selector = profile.f2p_selector if phase == "f2p" else profile.p2p_selector
    return NativeRegressionRun(
        phase=phase,
        selector=selector,
        executable_path="bin/luantiserver",
        argv=("bin/luantiserver", "--run-unittests", "--test-module", selector),
        repetitions=3,
        fixed_expected_outcome="pass",
        buggy_expected_outcome="failure" if phase == "f2p" else "pass",
    )


def _load_attempts(
    root: Path,
    profile: NativeRegressionProfile,
    capsule: StableArtifact,
) -> tuple[StableArtifact, ...]:
    attempts: list[StableArtifact] = []
    builds: dict[str, StableArtifact] = {}
    for phase, variant in _CELL_ORDER:
        run = _run(profile, phase)
        build_reference = ArtifactReference(
            f"native_regression_build/luanti/{profile.candidate.change_number}/{variant}/build/v3",
            "native_regression_build",
            _V3,
        )
        build = builds.get(variant)
        if build is None:
            build = _load(root, build_reference)
            builds[variant] = build
        _validate_build(build, profile=profile, capsule=capsule, variant=variant)
        for repetition in (1, 2, 3):
            source_reference = ArtifactReference(
                "native_regression_attempt/luanti/"
                f"{profile.candidate.change_number}/{phase}/{variant}/{repetition}/v3",
                "native_regression_attempt",
                _V3,
            )
            source = _load(root, source_reference)
            attempt = _load(
                root,
                ArtifactReference(
                    "native_regression_attempt/luanti/"
                    f"{profile.candidate.change_number}/{phase}/{variant}/{repetition}/v4",
                    "native_regression_attempt",
                    _V4,
                ),
            )
            _validate_attempt(
                profile,
                run=run,
                variant=variant,
                repetition=repetition,
                capsule=capsule,
                build=build,
                source=source,
                attempt=attempt,
            )
            attempts.append(attempt)
    return tuple(attempts)


def _validate_build(
    build: StableArtifact,
    *,
    profile: NativeRegressionProfile,
    capsule: StableArtifact,
    variant: str,
) -> None:
    payload = build.payload
    if (
        build.inputs != (capsule.reference,)
        or payload.get("candidate") != profile.candidate.to_value()
        or payload.get("profile_id") != profile.profile_id
        or payload.get("variant") != variant
        or payload.get("step") != "build"
        or payload.get("process_outcome") != "completed"
        or payload.get("returncode") != 0
        or payload.get("output_encoding") != "base64"
        or type(payload.get("argv")) is not list
        or not payload["argv"]
    ):
        raise RuntimeError("native v3 build receipt is invalid")
    _decode_streams(payload, label="native v3 build")


def _validate_attempt(
    profile: NativeRegressionProfile,
    *,
    run: NativeRegressionRun,
    variant: str,
    repetition: int,
    capsule: StableArtifact,
    build: StableArtifact,
    source: StableArtifact,
    attempt: StableArtifact,
) -> None:
    expected_outcome = (
        run.fixed_expected_outcome if variant == "fixed" else run.buggy_expected_outcome
    )
    identity = {
        "candidate": profile.candidate.to_value(),
        "profile_id": profile.profile_id,
        "phase": run.phase,
        "variant": variant,
        "repetition": repetition,
        "selector": run.selector,
        "expected_outcome": expected_outcome,
    }
    if (
        source.inputs != (capsule.reference, build.reference)
        or set(source.payload) != _ATTEMPT_FIELDS
        or any(source.payload.get(key) != value for key, value in identity.items())
        or attempt.inputs != (source.reference,)
        or set(attempt.payload) != _ATTEMPT_FIELDS
        or any(attempt.payload.get(key) != value for key, value in identity.items())
    ):
        raise RuntimeError("native regression attempt identity is invalid")
    source_stdout, source_stderr = _decode_streams(
        source.payload,
        label="native v3 attempt",
    )
    attempt_stdout, attempt_stderr = _decode_streams(
        attempt.payload,
        label="native v4 attempt",
    )
    count = completed_native_test_count(
        run,
        source.payload["process_outcome"],
        source_stdout,
        source_stderr,
    )
    observed = _observed_outcome(
        source.payload["process_outcome"],
        source.payload["returncode"],
        count,
    )
    projected_fields = {
        "observed_outcome": observed,
        "process_outcome": source.payload["process_outcome"],
        "returncode": source.payload["returncode"],
        "completed_test_count": count,
        "stdout_base64": source.payload["stdout_base64"],
        "stderr_base64": source.payload["stderr_base64"],
        "stdout_truncated": source.payload["stdout_truncated"],
        "stderr_truncated": source.payload["stderr_truncated"],
        "output_encoding": "base64",
        "matched_expected": observed == expected_outcome,
    }
    if (
        any(
            attempt.payload.get(key) != value for key, value in projected_fields.items()
        )
        or attempt_stdout != source_stdout
        or attempt_stderr != source_stderr
        or count != 1
        or observed != expected_outcome
        or attempt.payload["matched_expected"] is not True
    ):
        raise RuntimeError("native v4 attempt reprojection is invalid")


def _decode_streams(
    payload: Mapping[str, object],
    *,
    label: str,
) -> tuple[bytes, bytes]:
    if (
        payload.get("output_encoding") != "base64"
        or type(payload.get("stdout_truncated")) is not bool
        or type(payload.get("stderr_truncated")) is not bool
    ):
        raise RuntimeError(f"{label} output metadata is invalid")
    result: list[bytes] = []
    for field in ("stdout_base64", "stderr_base64"):
        value = payload.get(field)
        if type(value) is not str:
            raise RuntimeError(f"{label} output is invalid")
        try:
            result.append(base64.b64decode(value, validate=True))
        except (ValueError, binascii.Error) as error:
            raise RuntimeError(f"{label} output is invalid") from error
    return result[0], result[1]


def _observed_outcome(
    process_outcome: object,
    returncode: object,
    completed_test_count: object,
) -> str:
    if process_outcome == "timed_out":
        return "timed_out"
    if process_outcome == "launch_failed":
        return "launch_failed"
    if process_outcome != "completed" or type(returncode) is not int:
        raise RuntimeError("native v3 process result is invalid")
    if completed_test_count != 1:
        return "invalid_test_count"
    return "pass" if returncode == 0 else "failure"


def _load_aggregate(
    root: Path,
    profile: NativeRegressionProfile,
    attempts: tuple[StableArtifact, ...],
) -> StableArtifact:
    aggregate = _load(
        root,
        ArtifactReference(
            f"native_regression_aggregate/luanti/{profile.candidate.change_number}/fixed-buggy/v4",
            "native_regression_aggregate",
            _V4,
        ),
    )
    if aggregate.inputs != tuple(item.reference for item in attempts):
        raise RuntimeError("native v4 aggregate attempt closure is invalid")
    groups = _attempt_groups(attempts)
    stable = all(
        len({item.payload["observed_outcome"] for item in group}) == 1
        for group in groups.values()
    )
    all_expected = all(item.payload["matched_expected"] is True for item in attempts)
    cells = [
        {
            "phase": phase,
            "variant": variant,
            "observed_outcomes": [
                item.payload["observed_outcome"] for item in groups[(phase, variant)]
            ],
        }
        for phase, variant in sorted(_CELL_ORDER)
    ]
    payload = aggregate.payload
    if (
        set(payload) != _AGGREGATE_FIELDS
        or payload["candidate"] != profile.candidate.to_value()
        or payload["profile_id"] != profile.profile_id
        or payload["repetitions_per_cell"] != 3
        or payload["attempt_count"] != 12
        or payload["stable_repetitions"] is not stable
        or payload["all_expected"] is not all_expected
        or payload["qualified"] is not (stable and all_expected)
        or payload["cells"] != cells
    ):
        raise RuntimeError("native v4 aggregate binding is invalid")
    return aggregate


def _attempt_groups(
    attempts: tuple[StableArtifact, ...],
) -> dict[tuple[str, str], tuple[StableArtifact, ...]]:
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for attempt in attempts:
        key = (attempt.payload["phase"], attempt.payload["variant"])
        groups.setdefault(key, []).append(attempt)
    if set(groups) != set(_CELL_ORDER) or any(
        len(group) != 3 for group in groups.values()
    ):
        raise RuntimeError("native regression attempt coverage is invalid")
    return {
        key: tuple(sorted(group, key=lambda item: item.payload["repetition"]))
        for key, group in groups.items()
    }


def _truth_table(
    attempts: tuple[StableArtifact, ...],
) -> dict[str, dict[str, bool]]:
    groups = _attempt_groups(attempts)
    return {
        variant: {
            phase: all(
                item.payload["observed_outcome"] == "pass"
                for item in groups[(phase, variant)]
            )
            for phase in ("f2p", "p2p")
        }
        for variant in ("fixed", "buggy")
    }


def _transition_reference(profile: NativeRegressionProfile) -> ArtifactReference:
    return ArtifactReference(
        "luanti_native_regression_transition/luanti/"
        f"{profile.candidate.change_number}/test-repetitions/v2",
        "luanti_native_regression_transition",
        _V2,
    )


def _load_or_write_transition(
    root: Path,
    *,
    profile: NativeRegressionProfile,
    manifest: StableArtifact,
    capsule: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    aggregate: StableArtifact,
    truth_table: dict[str, dict[str, bool]],
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, bool]:
    reference = _transition_reference(profile)
    inputs = (
        manifest.reference,
        capsule.reference,
        *(attempt.reference for attempt in attempts),
        aggregate.reference,
    )
    payload = {
        "candidate": profile.candidate.to_value(),
        "profile_id": profile.profile_id,
        "repetitions_per_cell": 3,
        "attempt_count": 12,
        "stable_repetitions": aggregate.payload["stable_repetitions"],
        "all_expected": aggregate.payload["all_expected"],
        "qualified": aggregate.payload["qualified"],
        "truth_table": truth_table,
        "complete": True,
    }
    try:
        transition = _load(root, reference)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
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
        resumed = False
    else:
        resumed = True
    if (
        set(transition.payload) != _TRANSITION_FIELDS
        or transition.inputs != inputs
        or transition.payload != payload
    ):
        raise RuntimeError("native regression v2 transition binding is invalid")
    return transition, resumed


def _stage_details(
    transition: StableArtifact,
    attempts: tuple[StableArtifact, ...],
) -> dict[str, object]:
    groups = _attempt_groups(attempts)
    pass_count = lambda phase, variant: sum(  # noqa: E731
        item.payload["observed_outcome"] == "pass" for item in groups[(phase, variant)]
    )
    return {
        "profile_id": transition.payload["profile_id"],
        "repetitions_per_cell": 3,
        "fixed_f2p_passes": pass_count("f2p", "fixed"),
        "buggy_f2p_failures": 3 - pass_count("f2p", "buggy"),
        "fixed_p2p_passes": pass_count("p2p", "fixed"),
        "buggy_p2p_passes": pass_count("p2p", "buggy"),
        "next_gate": (
            "version_materialization"
            if transition.payload["qualified"] is True
            else None
        ),
        "evidence_artifacts": [
            {
                "artifact_id": transition.reference.artifact_id,
                "artifact_kind": transition.reference.artifact_kind,
                "semantic_version": transition.reference.semantic_version,
            }
        ],
    }


def _load_formal_projection(
    root: Path,
    *,
    profile: NativeRegressionProfile,
    manifest: StableArtifact,
    transition: StableArtifact,
    details: Mapping[str, object],
    qualified: bool,
) -> tuple[StableArtifact, StableArtifact]:
    stage = _load(
        root,
        ArtifactReference(
            f"candidate_stage/luanti/{profile.candidate.change_number}/test_repetitions/v2",
            "candidate_stage",
            _V2,
        ),
    )
    expected_state = "passed" if qualified else "terminal"
    expected_outcome = (
        None
        if qualified
        else (
            "flake_observed"
            if transition.payload["stable_repetitions"] is False
            else "quality_rejected"
        )
    )
    if stage.inputs != (manifest.reference, transition.reference) or stage.payload != {
        "candidate": profile.candidate.to_value(),
        "stage": "test_repetitions",
        "state": expected_state,
        "outcome": expected_outcome,
        "details": dict(details),
        "complete": True,
    }:
        raise RuntimeError("formal v2 native test repetitions projection is invalid")
    checkpoint = _load(
        root,
        ArtifactReference(
            f"preannotation_checkpoint/luanti/{profile.candidate.change_number}/current/v2",
            "preannotation_checkpoint",
            _V2,
        ),
    )
    expected_checkpoint = {
        "candidate": profile.candidate.to_value(),
        "completed_stages": ["test_manifest", "test_repetitions"],
        "state": "active" if qualified else "terminal",
        "outcome": expected_outcome,
        "current_artifact_id": stage.reference.artifact_id,
    }
    if (
        checkpoint.inputs != (stage.reference,)
        or checkpoint.payload != expected_checkpoint
    ):
        raise RuntimeError("formal v2 native test repetitions checkpoint is invalid")
    return stage, checkpoint


def _load_test_repetitions_stage(
    root: Path,
    profile: NativeRegressionProfile,
    *,
    transition: ArtifactReference,
) -> StableArtifact:
    manifest = profile.test_manifest_reference
    stage = _load(
        root,
        ArtifactReference(
            f"candidate_stage/luanti/{profile.candidate.change_number}/test_repetitions/v2",
            "candidate_stage",
            _V2,
        ),
    )
    if (
        stage.inputs != (manifest, transition)
        or stage.payload.get("candidate") != profile.candidate.to_value()
        or stage.payload.get("stage") != "test_repetitions"
        or stage.payload.get("state") != "passed"
        or stage.payload.get("outcome") is not None
        or stage.payload.get("complete") is not True
    ):
        raise RuntimeError("formal native test repetitions stage is invalid")
    return stage


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _ordinary_directory(path: Path, parent: Path) -> Path:
    parent_resolved = parent.resolve(strict=True)
    info = path.lstat()
    resolved = path.resolve(strict=True)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or parent_resolved not in resolved.parents
    ):
        raise RuntimeError("native fixed capsule tree is unsafe")
    return resolved


def _ordinary_file(path: Path, parent: Path) -> Path:
    parent_resolved = parent.resolve(strict=True)
    info = path.lstat()
    resolved = path.resolve(strict=True)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or parent_resolved not in resolved.parents
    ):
        raise RuntimeError("native fixed capsule file is unsafe")
    return resolved


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "DOWNSTREAM_REPAIRABLE_ACTIONS",
    "LuantiNativeAdmissionProjection",
    "NativeRuntimeVisualClosureBlocker",
    "REQUIRED_RUNTIME_VISUAL_EVIDENCE",
    "audit_luanti_native_runtime_visual_closure",
    "load_luanti_native_downstream_preflight",
    "load_luanti_native_task_mapping",
    "project_luanti_native_regression_to_v2",
    "write_luanti_native_downstream_preflight",
    "write_luanti_native_task_mapping",
]
