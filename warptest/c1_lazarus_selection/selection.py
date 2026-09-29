"""Population stop decision and registered sampling-power report."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .power import evaluate_sampling_power
from .stable_protocol import (
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


def write_population_selection(
    artifact_root: str | Path,
    *,
    screening_population_artifact_id: str,
    coverage_artifact_id: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Record whether the fully screened population can advance to annotation."""

    population = load_stable_artifact(
        artifact_root,
        artifact_id=screening_population_artifact_id,
        artifact_kind="screening_population",
        semantic_version="1.0",
    )
    coverage = load_stable_artifact(
        artifact_root,
        artifact_id=coverage_artifact_id,
        artifact_kind="selection_coverage",
        semantic_version="1.0",
    )
    if (
        coverage.payload.get("screening_population_artifact_id")
        != population.reference.artifact_id
        or coverage.payload.get("complete") is not True
        or population.payload.get("complete") is not True
    ):
        raise RuntimeError("population selection coverage is invalid")
    records = population.payload.get("records")
    if type(records) is not list:
        raise RuntimeError("population selection records are invalid")
    qualified = []
    blocked = 0
    seen: set[tuple[str, int]] = set()
    for record in records:
        if type(record) is not dict or type(record.get("candidate")) is not dict:
            raise RuntimeError("population selection record is invalid")
        candidate = record["candidate"]
        repository = candidate.get("repository_key")
        number = candidate.get("change_number")
        if type(repository) is not str or type(number) is not int:
            raise RuntimeError("population selection candidate is invalid")
        key = (repository, number)
        if key in seen:
            raise RuntimeError("population selection candidate is duplicated")
        seen.add(key)
        outcome = record.get("outcome")
        if outcome == "qualified":
            qualified.append(candidate)
        elif outcome == "infrastructure_blocked":
            blocked += 1
    if blocked:
        status = "infrastructure_blocked"
    elif not qualified:
        status = "insufficient_population"
    else:
        status = "annotation_required"
    payload = {
        "screened_count": len(records),
        "qualified_count": len(qualified),
        "infrastructure_blocked_count": blocked,
        "annotation_candidate_count": len(qualified),
        "selected_candidates": [],
        "selection_status": status,
        "paid_actions_authorized": False,
        "complete": status == "insufficient_population",
    }
    artifact_id = "population_selection/all/current/v1"
    write_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind="population_selection",
        semantic_version="1.0",
        created_at=_now(),
        inputs=(population.reference, coverage.reference),
        payload=payload,
        authorization=authorization,
    )
    artifact = load_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind="population_selection",
        semantic_version="1.0",
    )
    if artifact.inputs != (population.reference, coverage.reference):
        raise RuntimeError("population selection dependencies are invalid")
    return artifact


def write_sampling_power_report(
    artifact_root: str | Path,
    *,
    population_selection_artifact_id: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist the power stop implied by the current selected population."""

    selection = load_stable_artifact(
        artifact_root,
        artifact_id=population_selection_artifact_id,
        artifact_kind="population_selection",
        semantic_version="1.0",
    )
    selected = selection.payload.get("selected_candidates")
    if type(selected) is not list:
        raise RuntimeError("sampling power selected population is invalid")
    result = evaluate_sampling_power(
        task_count=len(selected),
        simulations=2_000,
        random_seed=14_031,
        standardized_time_effect=0.8,
        standardized_token_effect=0.8,
        expected_time_ratio=0.5,
        expected_token_ratio=0.5,
    )
    payload = {
        "task_count": result.task_count,
        "simulations": result.simulations,
        "h3_p_only_power": result.h3_p_only_power,
        "h3_complete_event_power": result.h3_complete_event_power,
        "stop_status": result.stop_status,
        "confirmatory_execution_allowed": False,
        "paid_actions_authorized": False,
        "complete": True,
    }
    artifact_id = "sampling_power/all/current/v1"
    write_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind="sampling_power",
        semantic_version="1.0",
        created_at=_now(),
        inputs=(selection.reference,),
        payload=payload,
        authorization=authorization,
    )
    artifact = load_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind="sampling_power",
        semantic_version="1.0",
    )
    if artifact.inputs != (selection.reference,):
        raise RuntimeError("sampling power dependencies are invalid")
    return artifact


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_population_selection", "write_sampling_power_report"]
