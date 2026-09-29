"""Audit-only mapping for the previously curated task labels."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .enumeration import RepositoryEnumeration
from .epoch import SELECTION_EPOCH_V1, SelectionEpoch, require_selection_epoch
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


@dataclass(frozen=True)
class Old24AuditResult:
    total: int
    mapped: int
    unresolved: int
    terminal: int
    eligible: int
    infrastructure_blocked: int


def write_old24_audit_labels(
    artifact_root: str | Path,
    *,
    source_path: str | Path,
    authorization: AuthorizationRecord,
    expected_count: int = 24,
    selection_epoch: SelectionEpoch = SELECTION_EPOCH_V1,
) -> Old24AuditResult:
    """Map old labels without importing any prior screening conclusion."""

    epoch = require_selection_epoch(selection_epoch)
    value = yaml.safe_load(Path(source_path).read_text(encoding="utf-8"))
    if type(value) is not dict or type(value.get("candidates")) is not list:
        raise RuntimeError("old24 label source is invalid")
    rows = value["candidates"]
    if len(rows) != expected_count:
        raise RuntimeError("old24 label source count is invalid")
    if any(type(row) is not dict for row in rows):
        raise RuntimeError("old24 label row is invalid")
    labels = [row.get("task_id") for row in rows]
    if any(type(label) is not str or not label for label in labels):
        raise RuntimeError("old24 task label is invalid")
    if len(set(labels)) != len(labels):
        raise RuntimeError("old24 task labels are duplicated")

    root = Path(artifact_root)
    enumerations: dict[str, RepositoryEnumeration | None] = {}
    mapped = 0
    unresolved = 0
    terminal = 0
    eligible = 0
    blocked = 0
    for row in rows:
        label = row["task_id"]
        repository_key = row.get("benchmark")
        number = row.get("pr_number")
        if type(repository_key) is not str or not repository_key:
            raise RuntimeError("old24 repository label is invalid")
        if type(number) is not int:
            payload = _unresolved_payload(
                label,
                repository_key,
                None,
                "change_number_missing",
            )
            inputs: tuple[ArtifactReference, ...] = ()
            unresolved += 1
        else:
            enumeration = enumerations.get(repository_key)
            if repository_key not in enumerations:
                enumeration = _load_enumeration_if_present(root, repository_key, epoch)
                enumerations[repository_key] = enumeration
            if enumeration is None or number not in enumeration.merged_change_numbers:
                payload = _unresolved_payload(
                    label,
                    repository_key,
                    number,
                    "change_not_in_complete_enumeration",
                )
                inputs = ()
                unresolved += 1
            else:
                enumeration_outcome = _enumeration_outcome(enumeration, number)
                screening_state, screening_outcome, terminal_reference = (
                    _screening_state(
                        root,
                        repository_key,
                        number,
                        enumeration_outcome,
                        epoch,
                    )
                )
                payload = {
                    "task_label": label,
                    "repository_key": repository_key,
                    "change_number": number,
                    "mapping_state": "mapped",
                    "enumeration_outcome": enumeration_outcome,
                    "screening_state": screening_state,
                    "screening_outcome": screening_outcome,
                    "inherited_conclusions": False,
                }
                enumeration_reference = epoch.enumeration_reference(repository_key)
                inputs = (
                    enumeration_reference,
                    *((terminal_reference,) if terminal_reference is not None else ()),
                )
                mapped += 1
                terminal += screening_state == "terminal"
                eligible += screening_outcome == "qualified"
                blocked += screening_outcome == "infrastructure_blocked"
        write_stable_artifact(
            root,
            artifact_id=f"old24_audit/{label}/current/{epoch.label}",
            artifact_kind="old24_audit",
            semantic_version=epoch.semantic_version,
            created_at=_now(),
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
        loaded = load_stable_artifact(
            root,
            artifact_id=f"old24_audit/{label}/current/{epoch.label}",
            artifact_kind="old24_audit",
            semantic_version=epoch.semantic_version,
        )
        if loaded.payload != payload:
            raise RuntimeError("old24 audit label reload is inconsistent")
    return Old24AuditResult(
        total=len(rows),
        mapped=mapped,
        unresolved=unresolved,
        terminal=terminal,
        eligible=eligible,
        infrastructure_blocked=blocked,
    )


def _load_enumeration_if_present(
    root: Path,
    repository_key: str,
    epoch: SelectionEpoch,
) -> RepositoryEnumeration | None:
    try:
        artifact = load_stable_artifact(
            root,
            artifact_id=(
                f"repository_enumeration/{repository_key}/current/{epoch.label}"
            ),
            artifact_kind="repository_enumeration",
            semantic_version=epoch.semantic_version,
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise
    payload = artifact.payload
    if (
        payload.get("repository_key") != repository_key
        or payload.get("complete") is not True
    ):
        raise RuntimeError("repository enumeration identity is invalid")
    candidates = payload.get("candidates")
    if type(candidates) is not list:
        raise RuntimeError("repository enumeration candidates are invalid")
    return RepositoryEnumeration(
        repository_key=repository_key,
        merged_change_numbers=_number_tuple(payload.get("merged_change_numbers")),
        authoritative_issue_link_missing=_number_tuple(
            payload.get("authoritative_issue_link_missing")
        ),
        repair_not_in_pinned=_number_tuple(payload.get("repair_not_in_pinned")),
        candidates=tuple(CandidateKey.from_value(value) for value in candidates),
    )


def _enumeration_outcome(enumeration: RepositoryEnumeration, number: int) -> str:
    if number in enumeration.authoritative_issue_link_missing:
        return "authoritative_issue_link_missing"
    if number in enumeration.repair_not_in_pinned:
        return "repair_not_in_pinned"
    return "deep_screen"


def _screening_state(
    root: Path,
    repository_key: str,
    number: int,
    enumeration_outcome: str,
    epoch: SelectionEpoch,
) -> tuple[str, str | None, ArtifactReference | None]:
    if enumeration_outcome != "deep_screen":
        stage = (
            "issue-link"
            if enumeration_outcome == "authoritative_issue_link_missing"
            else "not-in-pinned"
        )
        try:
            artifact = load_stable_artifact(
                root,
                artifact_id=(
                    f"enumeration_terminal/{repository_key}/{number}/"
                    f"{stage}/{epoch.label}"
                ),
                artifact_kind="enumeration_terminal",
                semantic_version=epoch.semantic_version,
            )
        except RuntimeError as error:
            if epoch.is_v2 and str(error) in {
                "artifact directory is missing",
                "artifact current head is missing",
            }:
                return "pending", None, None
            raise
        return "terminal", enumeration_outcome, artifact.reference
    try:
        checkpoint = load_stable_artifact(
            root,
            artifact_id=(
                f"screening_checkpoint/{repository_key}/{number}/current/{epoch.label}"
            ),
            artifact_kind="screening_checkpoint",
            semantic_version=epoch.semantic_version,
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return "pending", None, None
        raise
    state = checkpoint.payload.get("state")
    if state == "complete":
        outcome = checkpoint.payload.get("outcome")
        if outcome != "patch_eligible":
            return "terminal", outcome, checkpoint.reference
        try:
            preannotation = load_stable_artifact(
                root,
                artifact_id=(
                    f"preannotation_checkpoint/{repository_key}/{number}/"
                    f"current/{epoch.label}"
                ),
                artifact_kind="preannotation_checkpoint",
                semantic_version=epoch.semantic_version,
            )
        except RuntimeError as error:
            if str(error) in {
                "artifact directory is missing",
                "artifact current head is missing",
            }:
                return "preannotation_pending", None, checkpoint.reference
            raise
        if preannotation.payload.get("candidate") != {
            "repository_key": repository_key,
            "change_number": number,
        }:
            raise RuntimeError("old24 pre-annotation candidate is invalid")
        if preannotation.payload.get("state") == "terminal":
            return (
                "terminal",
                preannotation.payload.get("outcome"),
                preannotation.reference,
            )
        if preannotation.payload.get("state") == "eligible":
            return "eligible", "qualified", preannotation.reference
        return "preannotation_pending", None, preannotation.reference
    if state == "redo_required":
        return "redo_required", None, checkpoint.reference
    raise RuntimeError("old24 screening checkpoint state is invalid")


def _unresolved_payload(
    label: str,
    repository_key: str,
    number: int | None,
    reason: str,
) -> dict[str, object]:
    return {
        "task_label": label,
        "repository_key": repository_key,
        "change_number": number,
        "mapping_state": "unresolved",
        "reason": reason,
        "inherited_conclusions": False,
    }


def _number_tuple(value: object) -> tuple[int, ...]:
    if type(value) is not list or any(type(number) is not int for number in value):
        raise RuntimeError("repository enumeration number set is invalid")
    return tuple(value)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["Old24AuditResult", "write_old24_audit_labels"]
