"""Exact terminal population and whole-enumeration coverage closure."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

from .enumeration import RepositoryEnumeration, load_repository_enumeration
from .epoch import SELECTION_EPOCH_V2
from .models import CandidateKey
from .screening import resolve_raw_patch_evidence
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_SCREENING_TERMINALS = frozenset(
    {"source_rejected", "patch_rejected", "infrastructure_blocked"}
)
_PREANNOTATION_TERMINALS = frozenset(
    {
        "version_incompatible",
        "flake_observed",
        "infrastructure_blocked",
        "replay_invalid",
        "quality_rejected",
    }
)


@dataclass(frozen=True)
class ScreeningPopulationResult:
    artifact: StableArtifact
    deep_screen_count: int
    outcome_counts: Mapping[str, int]


@dataclass(frozen=True)
class V2CandidateEligibility:
    """The complete normal v2 source-to-eligibility chain for one candidate."""

    candidate: CandidateKey
    enumeration: StableArtifact
    source_packet: StableArtifact
    test_patch: StableArtifact
    screening_checkpoint: StableArtifact
    stages: tuple[StableArtifact, ...]
    preannotation_checkpoint: StableArtifact


@dataclass(frozen=True)
class V2RepositoryEnumeration:
    """A strictly parsed v2 enumeration together with its typed artifact."""

    artifact: StableArtifact
    enumeration: RepositoryEnumeration


def write_screening_population(
    artifact_root: str | Path,
    *,
    repository_order: tuple[str, ...],
    authorization: AuthorizationRecord,
) -> ScreeningPopulationResult:
    """Require one exact terminal for every complete-enumeration candidate."""

    if (
        type(repository_order) is not tuple
        or not repository_order
        or len(set(repository_order)) != len(repository_order)
    ):
        raise ValueError("screening repository order is invalid")
    root = Path(artifact_root)
    records: list[dict[str, object]] = []
    inputs: list[ArtifactReference] = []
    outcomes: Counter[str] = Counter()
    seen: set[CandidateKey] = set()
    for repository_key in repository_order:
        enumeration = load_repository_enumeration(
            root,
            repository_key=repository_key,
        )
        enumeration_reference = ArtifactReference(
            f"repository_enumeration/{repository_key}/current/v1",
            "repository_enumeration",
            "1.0",
        )
        inputs.append(enumeration_reference)
        for candidate in enumeration.candidates:
            if candidate in seen:
                raise RuntimeError("screening candidate is duplicated")
            seen.add(candidate)
            checkpoint = _load_terminal_checkpoint(root, candidate)
            outcome = checkpoint.payload["outcome"]
            outcomes[outcome] += 1
            inputs.append(checkpoint.reference)
            records.append(
                {
                    "candidate": candidate.to_value(),
                    "outcome": outcome,
                    "terminal_artifact_id": checkpoint.reference.artifact_id,
                    "terminal_artifact_kind": checkpoint.reference.artifact_kind,
                }
            )
    artifact_id = "screening_population/all/current/v1"
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="screening_population",
        semantic_version="1.0",
        created_at=_now(),
        inputs=tuple(inputs),
        payload={
            "repository_order": list(repository_order),
            "records": records,
            "deep_screen_count": len(records),
            "outcome_counts": dict(sorted(outcomes.items())),
            "complete": True,
        },
        authorization=authorization,
    )
    artifact = load_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="screening_population",
        semantic_version="1.0",
    )
    if artifact.inputs != tuple(inputs):
        raise RuntimeError("screening population dependencies are invalid")
    return ScreeningPopulationResult(
        artifact=artifact,
        deep_screen_count=len(records),
        outcome_counts=dict(sorted(outcomes.items())),
    )


def write_complete_selection_coverage(
    artifact_root: str | Path,
    *,
    repository_order: tuple[str, ...],
    expected_merged_count: int,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Reload every merged-change audit and bind the exact terminal partition."""

    if type(expected_merged_count) is not int or expected_merged_count <= 0:
        raise ValueError("selection expected merged count is invalid")
    root = Path(artifact_root)
    population = load_stable_artifact(
        root,
        artifact_id="screening_population/all/current/v1",
        artifact_kind="screening_population",
        semantic_version="1.0",
    )
    inputs: list[ArtifactReference] = []
    audits: list[ArtifactReference] = []
    enumeration_terminals: list[ArtifactReference] = []
    merged_count = 0
    deep_count = 0
    expected_candidates: list[dict[str, object]] = []
    for repository_key in repository_order:
        enumeration = load_repository_enumeration(
            root,
            repository_key=repository_key,
        )
        enumeration_reference = ArtifactReference(
            f"repository_enumeration/{repository_key}/current/v1",
            "repository_enumeration",
            "1.0",
        )
        inputs.append(enumeration_reference)
        merged_count += len(enumeration.merged_change_numbers)
        deep_count += len(enumeration.candidates)
        expected_candidates.extend(
            candidate.to_value() for candidate in enumeration.candidates
        )
        missing = set(enumeration.authoritative_issue_link_missing)
        not_pinned = set(enumeration.repair_not_in_pinned)
        for number in enumeration.merged_change_numbers:
            candidate = CandidateKey(repository_key, number)
            audit = load_stable_artifact(
                root,
                artifact_id=(f"enumeration_audit/{repository_key}/{number}/current/v1"),
                artifact_kind="enumeration_audit",
                semantic_version="1.0",
            )
            if (
                audit.inputs != (enumeration_reference,)
                or audit.payload.get("candidate") != candidate.to_value()
                or audit.payload.get("complete") is not True
            ):
                raise RuntimeError("enumeration audit binding is invalid")
            audits.append(audit.reference)
            reason = (
                "authoritative_issue_link_missing"
                if number in missing
                else "repair_not_in_pinned"
                if number in not_pinned
                else None
            )
            if reason is None:
                continue
            stage = (
                "issue-link"
                if reason == "authoritative_issue_link_missing"
                else "not-in-pinned"
            )
            terminal = load_stable_artifact(
                root,
                artifact_id=(
                    f"enumeration_terminal/{repository_key}/{number}/{stage}/v1"
                ),
                artifact_kind="enumeration_terminal",
                semantic_version="1.0",
            )
            if (
                terminal.inputs != (enumeration_reference,)
                or terminal.payload.get("candidate") != candidate.to_value()
                or terminal.payload.get("reason") != reason
                or terminal.payload.get("complete") is not True
            ):
                raise RuntimeError("enumeration terminal binding is invalid")
            enumeration_terminals.append(terminal.reference)
    if merged_count != expected_merged_count:
        raise RuntimeError("complete selection merged coverage is invalid")
    records = population.payload.get("records")
    if type(records) is not list or [record.get("candidate") for record in records] != (
        expected_candidates
    ):
        raise RuntimeError("screening population candidate coverage is invalid")
    if population.payload.get("deep_screen_count") != deep_count:
        raise RuntimeError("screening population count is invalid")
    artifact_id = "selection_coverage/all/current/v1"
    all_inputs = (
        *inputs,
        population.reference,
        *audits,
        *enumeration_terminals,
    )
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="selection_coverage",
        semantic_version="1.0",
        created_at=_now(),
        inputs=all_inputs,
        payload={
            "repository_order": list(repository_order),
            "merged_count": merged_count,
            "audit_count": len(audits),
            "enumeration_terminal_count": len(enumeration_terminals),
            "deep_screen_count": deep_count,
            "screening_population_artifact_id": population.reference.artifact_id,
            "complete": True,
        },
        authorization=authorization,
    )
    artifact = load_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="selection_coverage",
        semantic_version="1.0",
    )
    if artifact.inputs != all_inputs:
        raise RuntimeError("complete selection coverage dependencies are invalid")
    return artifact


def write_screening_population_v2(
    artifact_root: str | Path,
    *,
    repository_order: tuple[str, ...],
    authorization: AuthorizationRecord,
) -> ScreeningPopulationResult:
    """Write the v2 terminal population without consulting any v1 head.

    V2 is deliberately a new selection epoch.  A syntactically valid v1
    checkpoint is not a resume input: every candidate must have a v2
    checkpoint rooted in its v2 repository enumeration.
    """

    _validate_repository_order(repository_order, label="v2 screening")
    root = Path(artifact_root)
    records: list[dict[str, object]] = []
    inputs: list[ArtifactReference] = []
    outcomes: Counter[str] = Counter()
    seen: set[CandidateKey] = set()
    for repository_key in repository_order:
        enumeration = _load_v2_repository_enumeration(root, repository_key)
        inputs.append(enumeration.artifact.reference)
        for candidate in enumeration.enumeration.candidates:
            if candidate in seen:
                raise RuntimeError("v2 screening candidate is duplicated")
            seen.add(candidate)
            terminal = _load_v2_terminal_checkpoint(root, candidate)
            outcome = terminal.payload["outcome"]
            outcomes[outcome] += 1
            inputs.append(terminal.reference)
            records.append(
                {
                    "candidate": candidate.to_value(),
                    "outcome": outcome,
                    "terminal_artifact_id": terminal.reference.artifact_id,
                    "terminal_artifact_kind": terminal.reference.artifact_kind,
                }
            )
    artifact_id = "screening_population/all/current/v2"
    payload = {
        "selection_epoch": "v2",
        "repository_order": list(repository_order),
        "records": records,
        "deep_screen_count": len(records),
        "outcome_counts": dict(sorted(outcomes.items())),
        "complete": True,
    }
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="screening_population",
        semantic_version="2.0",
        created_at=_now(),
        inputs=tuple(inputs),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load_v2_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="screening_population",
        label="v2 screening population",
    )
    if artifact.inputs != tuple(inputs) or artifact.payload != payload:
        raise RuntimeError("v2 screening population dependencies are invalid")
    return ScreeningPopulationResult(
        artifact=artifact,
        deep_screen_count=len(records),
        outcome_counts=dict(sorted(outcomes.items())),
    )


def write_complete_selection_coverage_v2(
    artifact_root: str | Path,
    *,
    repository_order: tuple[str, ...],
    expected_merged_count: int,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Bind only v2 enumeration, audit, and terminal population evidence."""

    _validate_repository_order(repository_order, label="v2 selection")
    if type(expected_merged_count) is not int or expected_merged_count <= 0:
        raise ValueError("v2 selection expected merged count is invalid")
    root = Path(artifact_root)
    population = _load_v2_artifact(
        root,
        artifact_id="screening_population/all/current/v2",
        artifact_kind="screening_population",
        label="v2 screening population",
    )
    if (
        population.payload.get("selection_epoch") != "v2"
        or population.payload.get("complete") is not True
        or population.payload.get("repository_order") != list(repository_order)
    ):
        raise RuntimeError("v2 screening population is invalid")

    enumeration_inputs: list[ArtifactReference] = []
    audits: list[ArtifactReference] = []
    enumeration_terminals: list[ArtifactReference] = []
    expected_population_inputs: list[ArtifactReference] = []
    merged_count = 0
    deep_count = 0
    expected_candidates: list[dict[str, object]] = []
    for repository_key in repository_order:
        enumeration_artifact = _load_v2_repository_enumeration(root, repository_key)
        enumeration = enumeration_artifact.enumeration
        enumeration_inputs.append(enumeration_artifact.artifact.reference)
        expected_population_inputs.append(enumeration_artifact.artifact.reference)
        merged_count += len(enumeration.merged_change_numbers)
        deep_count += len(enumeration.candidates)
        expected_candidates.extend(
            candidate.to_value() for candidate in enumeration.candidates
        )
        missing = set(enumeration.authoritative_issue_link_missing)
        not_pinned = set(enumeration.repair_not_in_pinned)
        blocked = set(enumeration.infrastructure_blocked)
        for number in enumeration.merged_change_numbers:
            candidate = CandidateKey(repository_key, number)
            audit = load_v2_enumeration_audit(root, candidate)
            audits.append(audit.reference)
            if number in missing or number in not_pinned or number in blocked:
                reason = (
                    "authoritative_issue_link_missing"
                    if number in missing
                    else "repair_not_in_pinned"
                    if number in not_pinned
                    else "infrastructure_blocked"
                )
                terminal = _load_v2_enumeration_terminal(
                    root,
                    candidate,
                    enumeration_artifact.artifact.reference,
                    reason,
                )
                enumeration_terminals.append(terminal.reference)
            else:
                expected_population_inputs.append(
                    _load_v2_terminal_checkpoint(root, candidate).reference
                )
    if merged_count != expected_merged_count:
        raise RuntimeError("v2 complete selection merged coverage is invalid")
    records = population.payload.get("records")
    if type(records) is not list or [record.get("candidate") for record in records] != (
        expected_candidates
    ):
        raise RuntimeError("v2 screening population candidate coverage is invalid")
    if population.payload.get("deep_screen_count") != deep_count:
        raise RuntimeError("v2 screening population count is invalid")
    if population.inputs != tuple(expected_population_inputs):
        raise RuntimeError("v2 screening population input closure is invalid")
    artifact_id = "selection_coverage/all/current/v2"
    all_inputs = (
        *enumeration_inputs,
        population.reference,
        *audits,
        *enumeration_terminals,
    )
    payload = {
        "selection_epoch": "v2",
        "repository_order": list(repository_order),
        "merged_count": merged_count,
        "audit_count": len(audits),
        "enumeration_terminal_count": len(enumeration_terminals),
        "deep_screen_count": deep_count,
        "screening_population_artifact_id": population.reference.artifact_id,
        "complete": True,
    }
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="selection_coverage",
        semantic_version="2.0",
        created_at=_now(),
        inputs=all_inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load_v2_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="selection_coverage",
        label="v2 selection coverage",
    )
    if artifact.inputs != all_inputs or artifact.payload != payload:
        raise RuntimeError("v2 complete selection coverage dependencies are invalid")
    return artifact


def load_v2_enumeration_audit(
    artifact_root: str | Path,
    candidate: CandidateKey,
) -> StableArtifact:
    """Load one v2 enumeration audit rooted in the same v2 enumeration."""

    if type(candidate) is not CandidateKey:
        raise TypeError("v2 enumeration audit candidate must be typed")
    root = Path(artifact_root)
    enumeration = _load_v2_repository_enumeration(root, candidate.repository_key)
    if candidate not in enumeration.enumeration.candidates and (
        candidate.change_number not in enumeration.enumeration.merged_change_numbers
    ):
        raise RuntimeError("v2 enumeration audit candidate is outside enumeration")
    artifact = _load_v2_artifact(
        root,
        artifact_id=(
            f"enumeration_audit/{candidate.repository_key}/"
            f"{candidate.change_number}/current/v2"
        ),
        artifact_kind="enumeration_audit",
        label="v2 enumeration audit",
    )
    if (
        artifact.inputs != (enumeration.artifact.reference,)
        or artifact.payload.get("candidate") != candidate.to_value()
        or artifact.payload.get("complete") is not True
    ):
        raise RuntimeError("v2 enumeration audit binding is invalid")
    return artifact


def load_v2_candidate_eligibility(
    artifact_root: str | Path,
    candidate: CandidateKey,
) -> V2CandidateEligibility:
    """Require a complete v2 source, patch, and pre-annotation eligibility DAG."""

    if type(candidate) is not CandidateKey:
        raise TypeError("v2 eligibility candidate must be typed")
    root = Path(artifact_root)
    enumeration = _load_v2_repository_enumeration(root, candidate.repository_key)
    typed_enumeration = enumeration.enumeration
    if candidate not in typed_enumeration.candidates:
        raise RuntimeError(
            "v2 eligibility candidate is not in the deep-screen population"
        )
    source_packet = _load_v2_artifact(
        root,
        artifact_id=(
            f"candidate_source_packet/{candidate.repository_key}/"
            f"{candidate.change_number}/source/v2"
        ),
        artifact_kind="candidate_source_packet",
        label="v2 candidate source packet",
    )
    if (
        source_packet.inputs != (enumeration.artifact.reference,)
        or source_packet.payload.get("candidate") != candidate.to_value()
        or type(source_packet.payload.get("raw_evidence_path")) is not str
        or not source_packet.payload["raw_evidence_path"]
        or source_packet.payload.get("complete") is not True
    ):
        raise RuntimeError("v2 candidate source packet binding is invalid")
    test_patch = _load_v2_artifact(
        root,
        artifact_id=(
            f"candidate_test_patch/{candidate.repository_key}/"
            f"{candidate.change_number}/screening/v2"
        ),
        artifact_kind="candidate_test_patch",
        label="v2 candidate test patch",
    )
    if (
        test_patch.inputs != (enumeration.artifact.reference, source_packet.reference)
        or test_patch.payload.get("candidate") != candidate.to_value()
        or test_patch.payload.get("outcome") != "patch_eligible"
        or test_patch.payload.get("complete") is not True
        or not _nonempty_string_list(test_patch.payload.get("production_paths"))
        or not _nonempty_string_list(test_patch.payload.get("test_paths"))
    ):
        raise RuntimeError("v2 candidate test patch binding is invalid")
    raw_patch_evidence = resolve_raw_patch_evidence(
        root,
        candidate=candidate,
        source_manifest_path=source_packet.payload["raw_evidence_path"],
    )
    if (
        test_patch.payload.get("source_manifest_path")
        != raw_patch_evidence.source_manifest_path
        or test_patch.payload.get("raw_patch_path") != raw_patch_evidence.raw_patch_path
    ):
        raise RuntimeError("v2 candidate test patch raw evidence binding is invalid")
    screening = _load_v2_artifact(
        root,
        artifact_id=(
            f"screening_checkpoint/{candidate.repository_key}/"
            f"{candidate.change_number}/current/v2"
        ),
        artifact_kind="screening_checkpoint",
        label="v2 screening checkpoint",
    )
    if (
        screening.inputs != (enumeration.artifact.reference, test_patch.reference)
        or screening.payload.get("candidate") != candidate.to_value()
        or screening.payload.get("state") != "complete"
        or screening.payload.get("outcome") != "patch_eligible"
        or screening.payload.get("stage_artifact_id")
        != test_patch.reference.artifact_id
    ):
        raise RuntimeError("v2 screening checkpoint binding is invalid")

    previous = test_patch.reference
    stages: list[StableArtifact] = []
    for index, stage_name in enumerate(_V2_PREANNOTATION_STAGES):
        stage = _load_v2_artifact(
            root,
            artifact_id=(
                f"candidate_stage/{candidate.repository_key}/"
                f"{candidate.change_number}/{stage_name}/v2"
            ),
            artifact_kind="candidate_stage",
            label="v2 pre-annotation stage",
        )
        expected_state = (
            "eligible" if index == len(_V2_PREANNOTATION_STAGES) - 1 else "passed"
        )
        expected_outcome = "qualified" if expected_state == "eligible" else None
        evidence = _load_v2_stage_evidence(root, stage.inputs, previous)
        if (
            stage.inputs != (previous, *evidence)
            or stage.payload.get("candidate") != candidate.to_value()
            or stage.payload.get("stage") != stage_name
            or stage.payload.get("state") != expected_state
            or stage.payload.get("outcome") != expected_outcome
            or stage.payload.get("complete") is not True
            or type(stage.payload.get("details")) is not dict
        ):
            raise RuntimeError("v2 pre-annotation stage binding is invalid")
        previous = stage.reference
        stages.append(stage)
    checkpoint = _load_v2_artifact(
        root,
        artifact_id=(
            f"preannotation_checkpoint/{candidate.repository_key}/"
            f"{candidate.change_number}/current/v2"
        ),
        artifact_kind="preannotation_checkpoint",
        label="v2 pre-annotation checkpoint",
    )
    if (
        checkpoint.inputs != (previous,)
        or checkpoint.payload.get("candidate") != candidate.to_value()
        or checkpoint.payload.get("completed_stages") != list(_V2_PREANNOTATION_STAGES)
        or checkpoint.payload.get("state") != "eligible"
        or checkpoint.payload.get("outcome") != "qualified"
        or checkpoint.payload.get("current_artifact_id") != previous.artifact_id
    ):
        raise RuntimeError("v2 pre-annotation checkpoint binding is invalid")
    return V2CandidateEligibility(
        candidate=candidate,
        enumeration=enumeration.artifact,
        source_packet=source_packet,
        test_patch=test_patch,
        screening_checkpoint=screening,
        stages=tuple(stages),
        preannotation_checkpoint=checkpoint,
    )


def _load_v2_stage_evidence(
    root: Path,
    inputs: tuple[ArtifactReference, ...],
    previous: ArtifactReference,
) -> tuple[ArtifactReference, ...]:
    """Reload every composable stage receipt after its exact v2 predecessor."""

    if not inputs or inputs[0] != previous:
        raise RuntimeError("v2 pre-annotation stage predecessor is invalid")
    evidence = inputs[1:]
    if len(evidence) != len(set(evidence)):
        raise RuntimeError("v2 pre-annotation evidence is duplicated")
    for reference in evidence:
        if reference.semantic_version != "2.0":
            raise RuntimeError("v2 pre-annotation evidence is not v2")
        loaded = _load_v2_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            label="v2 pre-annotation evidence",
        )
        if loaded.reference != reference:
            raise RuntimeError("v2 pre-annotation evidence identity is invalid")
    return evidence


_V2_PREANNOTATION_STAGES = (
    "test_manifest",
    "test_repetitions",
    "version_materialization",
    "frozen_replay",
    "hidden_oracle",
    "construction_gate",
    "annotation_eligibility",
)


def _load_v2_terminal_checkpoint(
    root: Path,
    candidate: CandidateKey,
) -> StableArtifact:
    """Resolve a v2 deep-screen terminal, never a same-name v1 artifact."""

    enumeration = _load_v2_repository_enumeration(root, candidate.repository_key)
    if candidate not in enumeration.enumeration.candidates:
        raise RuntimeError(
            "v2 screening candidate is not in the deep-screen population"
        )
    checkpoint = _load_v2_artifact(
        root,
        artifact_id=(
            f"screening_checkpoint/{candidate.repository_key}/"
            f"{candidate.change_number}/current/v2"
        ),
        artifact_kind="screening_checkpoint",
        label="v2 screening checkpoint",
    )
    payload = checkpoint.payload
    outcome = payload.get("outcome")
    if (
        payload.get("candidate") != candidate.to_value()
        or payload.get("state") != "complete"
        or outcome
        not in {
            "source_rejected",
            "patch_rejected",
            "patch_eligible",
            "infrastructure_blocked",
        }
        or type(payload.get("stage_artifact_id")) is not str
    ):
        raise RuntimeError("v2 screening checkpoint binding is invalid")
    if outcome == "patch_eligible":
        eligibility = load_v2_candidate_eligibility(root, candidate)
        if eligibility.screening_checkpoint.reference != checkpoint.reference:
            raise RuntimeError("v2 screening checkpoint reload drift")
        return eligibility.preannotation_checkpoint
    if outcome in {"source_rejected", "infrastructure_blocked"}:
        stage = _load_v2_artifact(
            root,
            artifact_id=(
                f"candidate_source_terminal/{candidate.repository_key}/"
                f"{candidate.change_number}/source/v2"
            ),
            artifact_kind="candidate_source_terminal",
            label="v2 candidate source terminal",
        )
        if (
            stage.inputs != (enumeration.artifact.reference,)
            or stage.payload.get("candidate") != candidate.to_value()
            or stage.payload.get("outcome") != outcome
            or type(stage.payload.get("reason")) is not str
            or not stage.payload["reason"]
            or stage.payload.get("complete") is not True
            or checkpoint.inputs != (enumeration.artifact.reference, stage.reference)
            or payload["stage_artifact_id"] != stage.reference.artifact_id
        ):
            raise RuntimeError("v2 candidate source terminal binding is invalid")
        return checkpoint
    source = _load_v2_artifact(
        root,
        artifact_id=(
            f"candidate_source_packet/{candidate.repository_key}/"
            f"{candidate.change_number}/source/v2"
        ),
        artifact_kind="candidate_source_packet",
        label="v2 candidate source packet",
    )
    stage = _load_v2_artifact(
        root,
        artifact_id=(
            f"candidate_patch_terminal/{candidate.repository_key}/"
            f"{candidate.change_number}/patch/v2"
        ),
        artifact_kind="candidate_patch_terminal",
        label="v2 candidate patch terminal",
    )
    if (
        source.inputs != (enumeration.artifact.reference,)
        or source.payload.get("candidate") != candidate.to_value()
        or type(source.payload.get("raw_evidence_path")) is not str
        or not source.payload["raw_evidence_path"]
        or source.payload.get("complete") is not True
        or stage.inputs != (enumeration.artifact.reference, source.reference)
        or stage.payload.get("candidate") != candidate.to_value()
        or stage.payload.get("outcome") != "patch_rejected"
        or type(stage.payload.get("reason")) is not str
        or not stage.payload["reason"]
        or stage.payload.get("complete") is not True
        or checkpoint.inputs != (enumeration.artifact.reference, stage.reference)
        or payload["stage_artifact_id"] != stage.reference.artifact_id
    ):
        raise RuntimeError("v2 candidate patch terminal binding is invalid")
    return checkpoint


def _load_v2_enumeration_terminal(
    root: Path,
    candidate: CandidateKey,
    enumeration_reference: ArtifactReference,
    reason: str,
) -> StableArtifact:
    stage = (
        "issue-link"
        if reason == "authoritative_issue_link_missing"
        else "not-in-pinned"
        if reason == "repair_not_in_pinned"
        else "infrastructure-blocked"
    )
    artifact = _load_v2_artifact(
        root,
        artifact_id=(
            f"enumeration_terminal/{candidate.repository_key}/"
            f"{candidate.change_number}/{stage}/v2"
        ),
        artifact_kind="enumeration_terminal",
        label="v2 enumeration terminal",
    )
    if (
        artifact.inputs != (enumeration_reference,)
        or artifact.payload.get("candidate") != candidate.to_value()
        or artifact.payload.get("reason") != reason
        or artifact.payload.get("complete") is not True
    ):
        raise RuntimeError("v2 enumeration terminal binding is invalid")
    return artifact


def _load_v2_repository_enumeration(
    root: Path,
    repository_key: str,
) -> V2RepositoryEnumeration:
    reference = SELECTION_EPOCH_V2.enumeration_reference(repository_key)
    artifact = _load_v2_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        label="v2 repository enumeration",
    )
    payload = artifact.payload
    if (
        payload.get("repository_key") != repository_key
        or payload.get("complete") is not True
        or "infrastructure_blocked" not in payload
    ):
        raise RuntimeError("v2 repository enumeration identity is invalid")
    try:
        enumeration = RepositoryEnumeration(
            repository_key=repository_key,
            merged_change_numbers=_v2_number_tuple(
                payload.get("merged_change_numbers"), "merged change numbers"
            ),
            authoritative_issue_link_missing=_v2_number_tuple(
                payload.get("authoritative_issue_link_missing"), "issue-link terminals"
            ),
            repair_not_in_pinned=_v2_number_tuple(
                payload.get("repair_not_in_pinned"), "pinned terminals"
            ),
            infrastructure_blocked=_v2_number_tuple(
                payload.get("infrastructure_blocked"), "infrastructure terminals"
            ),
            candidates=tuple(
                CandidateKey.from_value(value)
                for value in _v2_plain_list(payload.get("candidates"), "candidates")
            ),
        )
    except ValueError as error:
        raise RuntimeError("v2 repository enumeration is invalid") from error
    return V2RepositoryEnumeration(artifact=artifact, enumeration=enumeration)


def _load_v2_artifact(
    root: Path,
    *,
    artifact_id: str,
    artifact_kind: str,
    label: str,
) -> StableArtifact:
    try:
        return load_stable_artifact(
            root,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            semantic_version="2.0",
        )
    except RuntimeError as error:
        raise RuntimeError(f"{label} is missing or invalid") from error


def _validate_repository_order(
    repository_order: tuple[str, ...], *, label: str
) -> None:
    if (
        type(repository_order) is not tuple
        or not repository_order
        or any(type(value) is not str or not value for value in repository_order)
        or len(set(repository_order)) != len(repository_order)
    ):
        raise ValueError(f"{label} repository order is invalid")


def _v2_plain_list(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"v2 repository enumeration {label} are invalid")
    return value


def _v2_number_tuple(value: object, label: str) -> tuple[int, ...]:
    values = _v2_plain_list(value, label)
    if any(type(number) is not int or number <= 0 for number in values):
        raise ValueError(f"v2 repository enumeration {label} are invalid")
    return tuple(values)


def _nonempty_string_list(value: object) -> bool:
    return (
        type(value) is list
        and bool(value)
        and all(type(item) is str and item for item in value)
    )


def _load_terminal_checkpoint(root: Path, candidate: CandidateKey) -> StableArtifact:
    screening = load_stable_artifact(
        root,
        artifact_id=(
            f"screening_checkpoint/{candidate.repository_key}/"
            f"{candidate.change_number}/current/v1"
        ),
        artifact_kind="screening_checkpoint",
        semantic_version="1.0",
    )
    if screening.payload.get("candidate") != candidate.to_value():
        raise RuntimeError("screening terminal candidate is invalid")
    outcome = screening.payload.get("outcome")
    if outcome in _SCREENING_TERMINALS:
        if screening.payload.get("state") != "complete":
            raise RuntimeError("screening terminal is incomplete")
        return screening
    if outcome != "patch_eligible" or screening.payload.get("state") != "complete":
        raise RuntimeError("screening candidate has no final outcome")
    preannotation = load_stable_artifact(
        root,
        artifact_id=(
            f"preannotation_checkpoint/{candidate.repository_key}/"
            f"{candidate.change_number}/current/v1"
        ),
        artifact_kind="preannotation_checkpoint",
        semantic_version="1.0",
    )
    if preannotation.payload.get("candidate") != candidate.to_value():
        raise RuntimeError("pre-annotation terminal is invalid")
    state = preannotation.payload.get("state")
    outcome = preannotation.payload.get("outcome")
    if state == "eligible" and outcome == "qualified":
        return preannotation
    if state != "terminal" or outcome not in _PREANNOTATION_TERMINALS:
        raise RuntimeError("pre-annotation terminal is invalid")
    return preannotation


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "ScreeningPopulationResult",
    "V2CandidateEligibility",
    "load_v2_candidate_eligibility",
    "load_v2_enumeration_audit",
    "write_complete_selection_coverage",
    "write_complete_selection_coverage_v2",
    "write_screening_population",
    "write_screening_population_v2",
]
