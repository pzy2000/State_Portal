#!/usr/bin/env python3
"""Resume the unpaid C1-Lazarus candidate-level pre-annotation pipeline."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from warptest.c1_lazarus_selection.epoch import SELECTION_EPOCH_V2
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.operations import (
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
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
)


_REPOSITORY_ORDER = ("luanti", "blender", "jupyterlab", "openra", "libreoffice")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument(
        "--through-stage",
        choices=("test_manifest",),
    )
    parser.add_argument("--max-candidates", type=_positive_int)
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.execute:
        raise RuntimeError("pre-annotation execution requires explicit --execute")
    if arguments.through_stage is None and arguments.workspace_root is None:
        raise RuntimeError("full pre-annotation execution requires --workspace-root")
    authorization = AuthorizationRecord(
        authorization_id=arguments.authorization_id,
        scope="candidate-screening",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )
    candidates, batch_contexts = _patch_eligible_candidates(
        arguments.artifact_root,
        max_candidates=arguments.max_candidates,
    )

    def unavailable(stage: str):
        def execute(_candidate) -> StageDecision:
            raise RuntimeError(
                f"{stage} has no registered production operation; execution is blocked"
            )

        return execute

    def manifest_operation(candidate: CandidateKey) -> StageDecision:
        return evaluate_test_manifest(
            arguments.artifact_root,
            candidate,
            selection_epoch=SELECTION_EPOCH_V2,
            batch_context=batch_contexts[candidate.repository_key],
        )

    if arguments.through_stage == "test_manifest":
        operations = {"test_manifest": manifest_operation}
    else:
        if arguments.workspace_root is None:
            raise RuntimeError(
                "full pre-annotation execution requires --workspace-root"
            )
        operations = {stage: unavailable(stage) for stage in PREANNOTATION_STAGES}
        operations["test_manifest"] = manifest_operation
        operations["test_repetitions"] = lambda candidate: evaluate_test_repetitions(
            arguments.artifact_root,
            workspace_root=arguments.workspace_root,
            candidate=candidate,
            authorization=authorization,
            selection_epoch=SELECTION_EPOCH_V2,
        )
    result = run_preannotation_pipeline(
        arguments.artifact_root,
        candidates=candidates,
        operations=operations,
        authorization=authorization,
        selection_epoch=SELECTION_EPOCH_V2,
        through_stage=arguments.through_stage,
        batch_contexts=batch_contexts,
        test_workers=3,
        build_workers=1,
        replay_workers=2,
        persistence_workers=2,
        reload_workers=2,
        queue_limit=16,
    )
    print(
        json.dumps(
            {
                "total": result.total,
                "scheduled": result.scheduled,
                "resumed": result.resumed,
                "active": result.active,
                "eligible": result.eligible,
                "terminal_counts": result.terminal_counts,
                "paused_for_disk": result.paused_for_disk,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _patch_eligible_candidates(
    artifact_root: Path,
    *,
    max_candidates: int | None = None,
) -> tuple[tuple[CandidateKey, ...], dict[str, V2PreannotationBatchContext]]:
    """Load only v2 enumeration candidates with a complete v2 checkpoint.

    This is intentionally upstream of the eventual all-candidate aggregation:
    the aggregate is written only after pre-annotation concludes, so consuming
    it here would form a dependency cycle.  Every selected candidate is rooted
    directly in its typed v2 repository enumeration and screening chain.
    """

    candidates: list[CandidateKey] = []
    batch_contexts: dict[str, V2PreannotationBatchContext] = {}
    seen: set[CandidateKey] = set()
    for repository_key in _REPOSITORY_ORDER:
        atomicity_context = load_source_atomicity_batch_context(
            artifact_root,
            repository_key=repository_key,
        )
        enumeration_reference = atomicity_context.enumeration_reference
        batch_contexts[repository_key] = (
            V2PreannotationBatchContext.from_source_atomicity(
                atomicity_context,
            )
        )
        for change_number in sorted(atomicity_context.candidate_numbers):
            candidate = CandidateKey(repository_key, change_number)
            if candidate in seen:
                raise RuntimeError("v2 enumeration candidate is duplicated")
            seen.add(candidate)
            outcome = _load_v2_screening_checkpoint(
                artifact_root,
                candidate=candidate,
                enumeration_reference=enumeration_reference,
            )
            if outcome == "patch_eligible":
                try:
                    atomicity = load_source_atomicity_artifact(
                        batch_context=atomicity_context,
                        candidate=candidate,
                    )
                except RuntimeError as error:
                    if str(error) not in {
                        "artifact directory is missing",
                        "artifact current head is missing",
                    }:
                        raise
                    continue
                if atomicity.outcome != "accepted":
                    continue
                candidates.append(candidate)
                if max_candidates is not None and len(candidates) == max_candidates:
                    return tuple(candidates), batch_contexts
    return tuple(candidates), batch_contexts


def _load_v2_screening_checkpoint(
    artifact_root: Path,
    *,
    candidate: CandidateKey,
    enumeration_reference: ArtifactReference,
) -> str:
    checkpoint_reference = ArtifactReference(
        artifact_id=(
            f"screening_checkpoint/{candidate.repository_key}/"
            f"{candidate.change_number}/current/v2"
        ),
        artifact_kind="screening_checkpoint",
        semantic_version=SELECTION_EPOCH_V2.semantic_version,
    )
    checkpoint = load_stable_artifact(
        artifact_root,
        artifact_id=checkpoint_reference.artifact_id,
        artifact_kind=checkpoint_reference.artifact_kind,
        semantic_version=checkpoint_reference.semantic_version,
    )
    payload = checkpoint.payload
    if set(payload) != {"candidate", "state", "outcome", "stage_artifact_id"}:
        raise RuntimeError("v2 screening checkpoint schema is invalid")
    outcome = payload["outcome"]
    if type(outcome) is not str or outcome not in {
        "source_rejected",
        "patch_rejected",
        "patch_eligible",
        "infrastructure_blocked",
    }:
        raise RuntimeError("v2 screening checkpoint outcome is invalid")
    stage_reference = _screening_stage_reference(candidate, outcome)
    if (
        payload["candidate"] != candidate.to_value()
        or payload["state"] != "complete"
        or payload["stage_artifact_id"] != stage_reference.artifact_id
        or checkpoint.inputs != (enumeration_reference, stage_reference)
    ):
        raise RuntimeError("v2 screening checkpoint binding is invalid")
    _validate_v2_screening_stage(
        artifact_root,
        candidate=candidate,
        outcome=outcome,
        enumeration_reference=enumeration_reference,
        stage_reference=stage_reference,
    )
    return outcome


def _screening_stage_reference(
    candidate: CandidateKey,
    outcome: str,
) -> ArtifactReference:
    if outcome in {"source_rejected", "infrastructure_blocked"}:
        return ArtifactReference(
            artifact_id=(
                f"candidate_source_terminal/{candidate.repository_key}/"
                f"{candidate.change_number}/source/v2"
            ),
            artifact_kind="candidate_source_terminal",
            semantic_version=SELECTION_EPOCH_V2.semantic_version,
        )
    if outcome == "patch_rejected":
        return ArtifactReference(
            artifact_id=(
                f"candidate_patch_terminal/{candidate.repository_key}/"
                f"{candidate.change_number}/patch/v2"
            ),
            artifact_kind="candidate_patch_terminal",
            semantic_version=SELECTION_EPOCH_V2.semantic_version,
        )
    if outcome == "patch_eligible":
        return ArtifactReference(
            artifact_id=(
                f"candidate_test_patch/{candidate.repository_key}/"
                f"{candidate.change_number}/screening/v2"
            ),
            artifact_kind="candidate_test_patch",
            semantic_version=SELECTION_EPOCH_V2.semantic_version,
        )
    raise RuntimeError("v2 screening checkpoint outcome is invalid")


def _validate_v2_screening_stage(
    artifact_root: Path,
    *,
    candidate: CandidateKey,
    outcome: str,
    enumeration_reference: ArtifactReference,
    stage_reference: ArtifactReference,
) -> None:
    stage = load_stable_artifact(
        artifact_root,
        artifact_id=stage_reference.artifact_id,
        artifact_kind=stage_reference.artifact_kind,
        semantic_version=stage_reference.semantic_version,
    )
    payload = stage.payload
    if (
        payload.get("candidate") != candidate.to_value()
        or payload.get("outcome") != outcome
        or payload.get("complete") is not True
    ):
        raise RuntimeError("v2 screening stage binding is invalid")
    if outcome in {"source_rejected", "infrastructure_blocked"}:
        if set(payload) != {
            "candidate",
            "outcome",
            "reason",
            "complete",
        } or stage.inputs != (enumeration_reference,):
            raise RuntimeError("v2 screening source terminal is invalid")
        return
    source_reference = ArtifactReference(
        artifact_id=(
            f"candidate_source_packet/{candidate.repository_key}/"
            f"{candidate.change_number}/source/v2"
        ),
        artifact_kind="candidate_source_packet",
        semantic_version=SELECTION_EPOCH_V2.semantic_version,
    )
    if stage.inputs != (enumeration_reference, source_reference):
        raise RuntimeError("v2 screening patch source prerequisite is invalid")
    _validate_v2_source_packet(
        artifact_root,
        candidate=candidate,
        enumeration_reference=enumeration_reference,
        source_reference=source_reference,
    )
    expected_fields = (
        {"candidate", "outcome", "reason", "complete"}
        if outcome == "patch_rejected"
        else {
            "candidate",
            "outcome",
            "production_paths",
            "test_paths",
            "source_manifest_path",
            "raw_patch_path",
            "complete",
        }
    )
    if set(payload) != expected_fields:
        raise RuntimeError("v2 screening patch stage schema is invalid")
    if outcome == "patch_eligible" and not (
        _nonempty_string_list(payload["production_paths"])
        and _nonempty_string_list(payload["test_paths"])
    ):
        raise RuntimeError("v2 screening patch inventory is invalid")


def _validate_v2_source_packet(
    artifact_root: Path,
    *,
    candidate: CandidateKey,
    enumeration_reference: ArtifactReference,
    source_reference: ArtifactReference,
) -> None:
    source = load_stable_artifact(
        artifact_root,
        artifact_id=source_reference.artifact_id,
        artifact_kind=source_reference.artifact_kind,
        semantic_version=source_reference.semantic_version,
    )
    if (
        set(source.payload) != {"candidate", "raw_evidence_path", "complete"}
        or source.inputs != (enumeration_reference,)
        or source.payload["candidate"] != candidate.to_value()
        or type(source.payload["raw_evidence_path"]) is not str
        or not source.payload["raw_evidence_path"]
        or source.payload["complete"] is not True
    ):
        raise RuntimeError("v2 screening source packet is invalid")


def _nonempty_string_list(value: object) -> bool:
    return (
        type(value) is list
        and bool(value)
        and all(type(item) is str and item for item in value)
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
