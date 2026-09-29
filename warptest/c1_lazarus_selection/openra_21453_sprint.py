"""Fail-closed sprint admission audit for the OpenRA PR 21453 candidate.

This module deliberately stops before legacy build, construction, replay, or
golden evidence.  A candidate that cannot pass the current v2 source and patch
prefix must not consume downstream sprint resources, even when an older task
with the same repository/change label has successful execution receipts.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .enumeration import load_repository_enumeration
from .epoch import SELECTION_EPOCH_V2
from .models import CandidateKey
from .patches import classify_unified_patch
from .population import load_v2_enumeration_audit
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


_CANDIDATE = CandidateKey("openra", 21453)
_VERSION = "2.0"
_TERMINAL = ArtifactReference(
    "candidate_sprint_terminal/openra/21453/admission/v2",
    "candidate_sprint_terminal",
    _VERSION,
)
_PAYLOAD_FIELDS = frozenset(
    {
        "candidate",
        "current_enumeration_candidate",
        "current_source_state",
        "current_patch_state",
        "current_patch_reason",
        "production_paths",
        "test_paths",
        "legacy_conclusions_reused",
        "admissible",
        "blockers",
        "complete",
    }
)


@dataclass(frozen=True)
class OpenRA21453SprintAudit:
    candidate: CandidateKey
    current_enumeration_candidate: bool
    current_source_state: Literal["missing", "unverified", "accepted", "rejected"]
    current_patch_state: Literal["eligible", "rejected"]
    current_patch_reason: str | None
    production_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    legacy_conclusions_reused: bool
    admissible: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class OpenRA21453SprintTerminal:
    artifact: StableArtifact
    audit: OpenRA21453SprintAudit


def audit_openra_21453_sprint_admission(
    artifact_root: str | Path,
    *,
    patch_path: str | Path,
) -> OpenRA21453SprintAudit:
    """Recompute the current prefix and reject before any legacy conclusion."""

    root = Path(artifact_root).resolve(strict=True)
    enumeration = load_repository_enumeration(
        root,
        repository_key="openra",
        selection_epoch=SELECTION_EPOCH_V2,
    )
    if _CANDIDATE not in enumeration.candidates:
        raise RuntimeError("OpenRA 21453 is not a current v2 candidate")
    load_v2_enumeration_audit(root, _CANDIDATE)

    source_state = _current_source_state(root)
    patch_file = _regular_patch_file(patch_path)
    decision = classify_unified_patch(patch_file.read_bytes())
    blockers: list[str] = []
    if source_state != "accepted":
        blockers.append("current_authoritative_source_closure_missing")
    if decision.status == "rejected":
        blockers.append(
            "current_patch_has_no_regression_test"
            if decision.reason == "no_regression_test"
            else f"current_patch_rejected_{decision.reason}"
        )
    admissible = source_state == "accepted" and decision.status == "eligible"
    if admissible:
        blockers.append("downstream_gates_not_evaluated")
        admissible = False
    return OpenRA21453SprintAudit(
        candidate=_CANDIDATE,
        current_enumeration_candidate=True,
        current_source_state=source_state,
        current_patch_state=decision.status,
        current_patch_reason=decision.reason,
        production_paths=decision.production_paths,
        test_paths=decision.test_paths,
        legacy_conclusions_reused=False,
        admissible=admissible,
        blockers=tuple(blockers),
    )


def write_openra_21453_sprint_terminal(
    artifact_root: str | Path,
    *,
    patch_path: str | Path,
    authorization: AuthorizationRecord,
) -> OpenRA21453SprintTerminal:
    """Persist and immediately reload the current fail-closed sprint terminal."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
    ):
        raise PermissionError(
            "OpenRA 21453 sprint terminal requires candidate-screening authorization"
        )
    root = Path(artifact_root).resolve(strict=True)
    audit = audit_openra_21453_sprint_admission(root, patch_path=patch_path)
    if audit.admissible:
        raise RuntimeError("an admissible candidate cannot be written as a terminal")
    inputs = _current_inputs(root)
    write_stable_artifact(
        root,
        artifact_id=_TERMINAL.artifact_id,
        artifact_kind=_TERMINAL.artifact_kind,
        semantic_version=_TERMINAL.semantic_version,
        created_at=authorization.granted_at,
        inputs=inputs,
        payload=_payload(audit),
        authorization=authorization,
    )
    return load_openra_21453_sprint_terminal(root, patch_path=patch_path)


def load_openra_21453_sprint_terminal(
    artifact_root: str | Path,
    *,
    patch_path: str | Path,
) -> OpenRA21453SprintTerminal:
    """Typed-load the terminal and reproduce its decision from current inputs."""

    root = Path(artifact_root).resolve(strict=True)
    audit = audit_openra_21453_sprint_admission(root, patch_path=patch_path)
    artifact = load_stable_artifact(
        root,
        artifact_id=_TERMINAL.artifact_id,
        artifact_kind=_TERMINAL.artifact_kind,
        semantic_version=_TERMINAL.semantic_version,
    )
    if artifact.inputs != _current_inputs(root):
        raise RuntimeError("OpenRA 21453 sprint terminal input binding is invalid")
    if type(artifact.payload) is not dict or set(artifact.payload) != _PAYLOAD_FIELDS:
        raise RuntimeError("OpenRA 21453 sprint terminal schema is invalid")
    if artifact.payload != _payload(audit):
        raise RuntimeError("OpenRA 21453 sprint terminal mechanical replay drift")
    return OpenRA21453SprintTerminal(artifact=artifact, audit=audit)


def _current_source_state(
    root: Path,
) -> Literal["missing", "unverified", "accepted", "rejected"]:
    source_id = "candidate_source_packet/openra/21453/source/v2"
    try:
        load_stable_artifact(
            root,
            artifact_id=source_id,
            artifact_kind="candidate_source_packet",
            semantic_version=_VERSION,
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return "missing"
        raise
    try:
        context = load_source_atomicity_batch_context(root, repository_key="openra")
        receipt = load_source_atomicity_artifact(
            batch_context=context,
            candidate=_CANDIDATE,
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return "unverified"
        raise
    return receipt.outcome


def _current_inputs(root: Path) -> tuple[ArtifactReference, ArtifactReference]:
    enumeration = SELECTION_EPOCH_V2.enumeration_reference("openra")
    audit = load_v2_enumeration_audit(root, _CANDIDATE)
    return enumeration, audit.reference


def _regular_patch_file(value: str | Path) -> Path:
    path = Path(value).resolve(strict=True)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("OpenRA 21453 patch evidence is not a regular file")
    return path


def _payload(audit: OpenRA21453SprintAudit) -> dict[str, object]:
    return {
        "candidate": audit.candidate.to_value(),
        "current_enumeration_candidate": audit.current_enumeration_candidate,
        "current_source_state": audit.current_source_state,
        "current_patch_state": audit.current_patch_state,
        "current_patch_reason": audit.current_patch_reason,
        "production_paths": list(audit.production_paths),
        "test_paths": list(audit.test_paths),
        "legacy_conclusions_reused": audit.legacy_conclusions_reused,
        "admissible": audit.admissible,
        "blockers": list(audit.blockers),
        "complete": True,
    }


__all__ = [
    "OpenRA21453SprintAudit",
    "OpenRA21453SprintTerminal",
    "audit_openra_21453_sprint_admission",
    "load_openra_21453_sprint_terminal",
    "write_openra_21453_sprint_terminal",
]
