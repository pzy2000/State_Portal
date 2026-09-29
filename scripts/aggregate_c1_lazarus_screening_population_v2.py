#!/usr/bin/env python3
"""Close the final offline C1-Lazarus v2 screening population and coverage.

This command deliberately runs only after every v2 deep-screen candidate has a
complete source/patch checkpoint and every patch-eligible candidate has reached
its v2 pre-annotation terminal.  It performs no candidate work: it reads the
five fixed v2 enumeration heads and their exact checkpoint IDs, then delegates
the final typed closure to the existing population and coverage writers.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from warptest.c1_lazarus_selection.enumeration import (
    RepositoryEnumeration,
    load_repository_enumeration,
)
from warptest.c1_lazarus_selection.epoch import SELECTION_EPOCH_V2
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.population import (
    load_v2_enumeration_audit,
    write_complete_selection_coverage_v2,
    write_screening_population_v2,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
)


_REPOSITORY_ORDER = ("luanti", "blender", "jupyterlab", "openra", "libreoffice")
_ENUMERATION_PAYLOAD_FIELDS = frozenset(
    {
        "repository_key",
        "merged_change_numbers",
        "authoritative_issue_link_missing",
        "repair_not_in_pinned",
        "infrastructure_blocked",
        "candidates",
        "complete",
    }
)
_CHECKPOINT_PAYLOAD_FIELDS = frozenset(
    {"candidate", "state", "outcome", "stage_artifact_id"}
)
_CHECKPOINT_OUTCOMES = frozenset(
    {
        "source_rejected",
        "patch_rejected",
        "patch_eligible",
        "infrastructure_blocked",
    }
)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.execute:
        _parser().error("final v2 aggregation requires the explicit --execute flag")
    authorization = AuthorizationRecord(
        authorization_id=arguments.authorization_id,
        scope="candidate-screening",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )
    enumerations, expected_candidates = _load_final_v2_inputs(arguments.artifact_root)
    _load_v2_enumeration_audit_population(arguments.artifact_root, enumerations)
    population_result = write_screening_population_v2(
        arguments.artifact_root,
        repository_order=_REPOSITORY_ORDER,
        authorization=authorization,
    )
    coverage = write_complete_selection_coverage_v2(
        arguments.artifact_root,
        repository_order=_REPOSITORY_ORDER,
        expected_merged_count=sum(
            len(enumeration.merged_change_numbers)
            for enumeration in enumerations.values()
        ),
        authorization=authorization,
    )
    _reload_final_closure(
        arguments.artifact_root,
        expected_candidates=expected_candidates,
        population_reference=population_result.artifact.reference,
        coverage_reference=coverage.reference,
    )
    print(
        json.dumps(
            {
                "selection_epoch": SELECTION_EPOCH_V2.label,
                "repository_order": list(_REPOSITORY_ORDER),
                "merged_count": sum(
                    len(enumeration.merged_change_numbers)
                    for enumeration in enumerations.values()
                ),
                "deep_screen_count": len(expected_candidates),
                "outcome_counts": population_result.outcome_counts,
                "screening_population_artifact_id": (
                    population_result.artifact.reference.artifact_id
                ),
                "selection_coverage_artifact_id": coverage.reference.artifact_id,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--execute", action="store_true")
    return parser


def _load_final_v2_inputs(
    artifact_root: Path,
) -> tuple[dict[str, RepositoryEnumeration], tuple[CandidateKey, ...]]:
    """Load exactly one v2 head and one v2 checkpoint per deep candidate.

    This deliberately does not discover artifacts by walking directories.  The
    enumeration dictates the sole valid candidate set and the checkpoint path
    is mechanically derived from each candidate identity.
    """

    enumerations: dict[str, RepositoryEnumeration] = {}
    expected_candidates: list[CandidateKey] = []
    seen: set[CandidateKey] = set()
    for repository_key in _REPOSITORY_ORDER:
        enumeration = _load_v2_enumeration_head(artifact_root, repository_key)
        enumerations[repository_key] = enumeration
        for candidate in enumeration.candidates:
            if candidate in seen:
                raise RuntimeError("v2 screening candidate is duplicated")
            seen.add(candidate)
            _load_v2_screening_checkpoint(
                artifact_root,
                candidate=candidate,
                enumeration_reference=SELECTION_EPOCH_V2.enumeration_reference(
                    repository_key
                ),
            )
            expected_candidates.append(candidate)
    if tuple(expected_candidates) != tuple(
        sorted(expected_candidates, key=_candidate_order_key)
    ):
        raise RuntimeError("v2 screening candidate order is invalid")
    return enumerations, tuple(expected_candidates)


def _candidate_order_key(candidate: CandidateKey) -> tuple[int, int]:
    return (_REPOSITORY_ORDER.index(candidate.repository_key), candidate.change_number)


def _load_v2_enumeration_head(
    artifact_root: Path,
    repository_key: str,
) -> RepositoryEnumeration:
    reference = SELECTION_EPOCH_V2.enumeration_reference(repository_key)
    try:
        artifact = load_stable_artifact(
            artifact_root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
        enumeration = load_repository_enumeration(
            artifact_root,
            repository_key=repository_key,
            selection_epoch=SELECTION_EPOCH_V2,
        )
    except RuntimeError as error:
        raise RuntimeError(
            f"v2 repository enumeration current head is missing or invalid: {repository_key}"
        ) from error
    if artifact.inputs != () or set(artifact.payload) != _ENUMERATION_PAYLOAD_FIELDS:
        raise RuntimeError("v2 repository enumeration current head is invalid")
    if (
        artifact.payload.get("repository_key") != repository_key
        or artifact.payload.get("complete") is not True
        or enumeration.repository_key != repository_key
    ):
        raise RuntimeError("v2 repository enumeration current head is invalid")
    return enumeration


def _load_v2_screening_checkpoint(
    artifact_root: Path,
    *,
    candidate: CandidateKey,
    enumeration_reference: ArtifactReference,
) -> StableArtifact:
    expected = ArtifactReference(
        artifact_id=(
            f"screening_checkpoint/{candidate.repository_key}/"
            f"{candidate.change_number}/current/v2"
        ),
        artifact_kind="screening_checkpoint",
        semantic_version=SELECTION_EPOCH_V2.semantic_version,
    )
    try:
        checkpoint = load_stable_artifact(
            artifact_root,
            artifact_id=expected.artifact_id,
            artifact_kind=expected.artifact_kind,
            semantic_version=expected.semantic_version,
        )
    except RuntimeError as error:
        raise RuntimeError("v2 screening checkpoint is missing") from error
    payload = checkpoint.payload
    outcome = payload.get("outcome")
    if (
        set(payload) != _CHECKPOINT_PAYLOAD_FIELDS
        or payload.get("candidate") != candidate.to_value()
        or payload.get("state") != "complete"
        or outcome not in _CHECKPOINT_OUTCOMES
        or type(payload.get("stage_artifact_id")) is not str
        or checkpoint.inputs
        != (
            enumeration_reference,
            _stage_reference(candidate, outcome),
        )
        or payload["stage_artifact_id"] != checkpoint.inputs[1].artifact_id
    ):
        raise RuntimeError("v2 screening checkpoint binding is invalid")
    return checkpoint


def _stage_reference(candidate: CandidateKey, outcome: object) -> ArtifactReference:
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


def _load_v2_enumeration_audit_population(
    artifact_root: Path,
    enumerations: dict[str, RepositoryEnumeration],
) -> None:
    """Fail before writing if a v2 audit/terminal would break final coverage."""

    for repository_key in _REPOSITORY_ORDER:
        enumeration = enumerations[repository_key]
        reference = SELECTION_EPOCH_V2.enumeration_reference(repository_key)
        missing = set(enumeration.authoritative_issue_link_missing)
        not_pinned = set(enumeration.repair_not_in_pinned)
        blocked = set(enumeration.infrastructure_blocked)
        for change_number in enumeration.merged_change_numbers:
            candidate = CandidateKey(repository_key, change_number)
            try:
                load_v2_enumeration_audit(artifact_root, candidate)
            except RuntimeError as error:
                raise RuntimeError(
                    "v2 enumeration audit is missing or invalid"
                ) from error
            reason, stage = _enumeration_terminal_reason_and_stage(
                change_number,
                missing=missing,
                not_pinned=not_pinned,
                blocked=blocked,
            )
            if reason is None:
                continue
            try:
                terminal = load_stable_artifact(
                    artifact_root,
                    artifact_id=(
                        f"enumeration_terminal/{repository_key}/{change_number}/"
                        f"{stage}/v2"
                    ),
                    artifact_kind="enumeration_terminal",
                    semantic_version=SELECTION_EPOCH_V2.semantic_version,
                )
            except RuntimeError as error:
                raise RuntimeError("v2 enumeration terminal is missing") from error
            if terminal.inputs != (reference,) or terminal.payload != {
                "candidate": candidate.to_value(),
                "reason": reason,
                "complete": True,
            }:
                raise RuntimeError("v2 enumeration terminal binding is invalid")


def _enumeration_terminal_reason_and_stage(
    change_number: int,
    *,
    missing: set[int],
    not_pinned: set[int],
    blocked: set[int],
) -> tuple[str | None, str | None]:
    if change_number in missing:
        return "authoritative_issue_link_missing", "issue-link"
    if change_number in not_pinned:
        return "repair_not_in_pinned", "not-in-pinned"
    if change_number in blocked:
        return "infrastructure_blocked", "infrastructure-blocked"
    return None, None


def _reload_final_closure(
    artifact_root: Path,
    *,
    expected_candidates: tuple[CandidateKey, ...],
    population_reference: ArtifactReference,
    coverage_reference: ArtifactReference,
) -> None:
    """Verify that the writers produced exactly the ordered v2 final heads."""

    population = load_stable_artifact(
        artifact_root,
        artifact_id=population_reference.artifact_id,
        artifact_kind=population_reference.artifact_kind,
        semantic_version=population_reference.semantic_version,
    )
    coverage = load_stable_artifact(
        artifact_root,
        artifact_id=coverage_reference.artifact_id,
        artifact_kind=coverage_reference.artifact_kind,
        semantic_version=coverage_reference.semantic_version,
    )
    records = population.payload.get("records")
    if (
        population.reference != population_reference
        or population.payload.get("selection_epoch") != SELECTION_EPOCH_V2.label
        or population.payload.get("repository_order") != list(_REPOSITORY_ORDER)
        or population.payload.get("complete") is not True
        or type(records) is not list
        or tuple(
            CandidateKey.from_value(record.get("candidate"))
            if type(record) is dict
            else (_ for _ in ()).throw(ValueError())
            for record in records
        )
        != expected_candidates
    ):
        raise RuntimeError("v2 screening population reload is invalid")
    if (
        coverage.reference != coverage_reference
        or coverage.payload.get("selection_epoch") != SELECTION_EPOCH_V2.label
        or coverage.payload.get("repository_order") != list(_REPOSITORY_ORDER)
        or coverage.payload.get("screening_population_artifact_id")
        != population_reference.artifact_id
        or coverage.payload.get("complete") is not True
    ):
        raise RuntimeError("v2 selection coverage reload is invalid")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
