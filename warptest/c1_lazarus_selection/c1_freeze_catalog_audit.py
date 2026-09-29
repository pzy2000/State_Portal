"""Recursively audit the C1-Lazarus freeze roster requirement by requirement.

For every roster row the audit loads the candidate's formal freeze admission head,
walks its typed input closure, and re-checks the hard requirements: strict Gate
1--7, the Gate 8--13 construction results, no inherited or synthetic evidence, and
no paid or formal-trial authorization.  It also re-checks the roster shape (exactly
24 rows, 16 ``at_goal`` and 8 ``on_path``, no duplicates).

The audit only reads typed artifacts, so it can be re-run before flipping
``selection_status`` to frozen.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import yaml

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_ADMISSION_KIND = "candidate_formal_freeze_admission"
# Admissions were written by several candidate-specific adapters whose payload
# vocabularies differ, so every requirement is recomputed structurally from the
# typed input closure instead of from one adapter's key names.
_REQUIRED_GATES = (8, 9, 10, 11, 12, 13)
# Adapters from different sprint phases named the same construction role
# differently, so each gate accepts the artifact-kind fragments that carry its
# role.  A candidate still needs one artifact per role whose payload reports a
# finished state; ``downstream_pending`` and blockers never count.
_GATE_ROLE_FRAGMENTS = {
    8: (
        "gate8",
        "materialization",
        "client_binding",
        "client_closure",
        "repackage_receipt",
        "build_receipt",
    ),
    9: ("gate9", "replay", "registered_start", "rendered_observation"),
    10: ("gate10", "oracle"),
    11: (
        "gate11",
        "visual_observation",
        "ui_observation",
        "edge_observation",
        "opaque_attempt",
    ),
    12: ("gate12", "matrix"),
    13: ("gate13", "lineage"),
}
_PASSING_STATES = frozenset({"passed", "admitted", "built", "ready", "captured"})
_BLOCKED_FRAGMENTS = ("blocker", "terminal")
_STRICT_GATE57_KINDS = (
    "candidate_gate57",
    "candidate_pool_admission",
    "candidate_gate57_pool_admission",
)
_STRICT_MARKERS = ("strict", "qualified")
_ADMITTED_KEYS = ("state", "freeze_state")
_FORBIDDEN_TRUE = ("paid_actions_authorized", "formal_trials_authorized")
_AUDIT = ArtifactReference(
    "c1_freeze_catalog_audit/lazarus/v2-roster/current-v1",
    "c1_freeze_catalog_audit",
    "1.0",
)


@dataclass(frozen=True)
class RowAudit:
    repository_key: str
    change_number: int
    defect_locus: str
    admission_id: str | None
    admitted: bool
    strict_gate_1_7: bool
    gates: Mapping[str, str]
    closure_size: int
    inherited_or_synthetic: bool
    paid_or_formal: bool
    problems: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.problems

    def to_value(self) -> dict[str, Any]:
        return {
            "repository_key": self.repository_key,
            "change_number": self.change_number,
            "defect_locus": self.defect_locus,
            "admission_id": self.admission_id,
            "admitted": self.admitted,
            "strict_gate_1_7": self.strict_gate_1_7,
            "gates": dict(self.gates),
            "typed_closure_size": self.closure_size,
            "inherited_or_synthetic_evidence": self.inherited_or_synthetic,
            "paid_or_formal_authorized": self.paid_or_formal,
            "problems": list(self.problems),
            "passed": self.passed,
        }


def audit_freeze_catalog(
    artifact_root: str | Path, roster_path: str | Path
) -> tuple[RowAudit, ...]:
    """Re-derive every roster row's admission evidence from typed artifacts."""

    root = Path(artifact_root).absolute().resolve(strict=True)
    document = yaml.safe_load(Path(roster_path).read_text(encoding="utf-8"))
    rows = document["candidates"]
    audits: list[RowAudit] = []
    for row in rows:
        audits.append(_audit_row(root, row))
    return tuple(audits)


def roster_shape_problems(roster_path: str | Path) -> tuple[str, ...]:
    """Re-check row count, locus split and identity uniqueness."""

    document = yaml.safe_load(Path(roster_path).read_text(encoding="utf-8"))
    rows = document["candidates"]
    problems: list[str] = []
    if len(rows) != 24:
        problems.append(f"roster has {len(rows)} rows instead of 24")
    locus = Counter(row["defect_locus"] for row in rows)
    if locus.get("at_goal") != 16 or locus.get("on_path") != 8:
        problems.append(f"defect locus split is {dict(locus)} instead of 16/8")
    identities = [(row["repository_key"], row["change_number"]) for row in rows]
    if len(set(identities)) != len(identities):
        problems.append("roster contains duplicate candidate identities")
    target = document.get("freeze_target", {})
    if target.get("task_count") != 24 or target.get("defect_locus") != {
        "at_goal": 16,
        "on_path": 8,
    }:
        problems.append("freeze target does not declare 24 tasks split 16/8")
    return tuple(problems)


def _audit_row(root: Path, row: Mapping[str, Any]) -> RowAudit:
    repository = str(row["repository_key"])
    change = int(row["change_number"])
    locus = str(row["defect_locus"])
    problems: list[str] = []
    admission = _load_admission(root, repository, change)
    if admission is None:
        return RowAudit(
            repository_key=repository,
            change_number=change,
            defect_locus=locus,
            admission_id=None,
            admitted=False,
            strict_gate_1_7=False,
            gates={},
            closure_size=0,
            inherited_or_synthetic=False,
            paid_or_formal=False,
            problems=("no formal freeze admission head",),
        )
    payload = admission.payload
    if not any(payload.get(key) == "admitted" for key in _ADMITTED_KEYS):
        problems.append(
            "admission state is "
            + repr({key: payload.get(key) for key in _ADMITTED_KEYS})
        )
    if payload.get("complete") is not True:
        problems.append("admission is not complete")
    closure = _closure(root, admission.reference)
    gates = _gate_evidence(payload, closure)
    for number in _REQUIRED_GATES:
        if not gates.get(f"gate{number}"):
            problems.append(f"gate{number} evidence is absent from the typed closure")
    strict = _strict_gate57_evidence(payload, closure)
    if not strict:
        problems.append("no strict Gate 1-7 evidence in the typed closure")
    inherited = False
    paid = False
    for artifact in closure.values():
        for key in _FORBIDDEN_TRUE:
            if artifact.payload.get(key) is True:
                paid = True
                problems.append(f"{artifact.reference.artifact_id} sets {key}")
        if artifact.payload.get("old_attempts_loaded") is True:
            inherited = True
            problems.append(f"{artifact.reference.artifact_id} loaded old attempts")
        if artifact.payload.get("synthetic_observation_frames") is True:
            inherited = True
            problems.append(f"{artifact.reference.artifact_id} used synthetic frames")
        if artifact.payload.get("lineage_inherited") is True:
            inherited = True
            problems.append(f"{artifact.reference.artifact_id} inherited lineage")
    return RowAudit(
        repository_key=repository,
        change_number=change,
        defect_locus=locus,
        admission_id=admission.reference.artifact_id,
        admitted=any(payload.get(key) == "admitted" for key in _ADMITTED_KEYS),
        strict_gate_1_7=bool(strict),
        gates=gates,
        closure_size=len(closure),
        inherited_or_synthetic=inherited,
        paid_or_formal=paid,
        problems=tuple(problems),
    )


def _gate_evidence(
    payload: Mapping[str, Any],
    closure: Mapping[ArtifactReference, StableArtifact],
) -> dict[str, str]:
    """Locate one passing evidence source per gate, whatever the vocabulary is."""

    found: dict[str, str] = {}
    for number in _REQUIRED_GATES:
        prefix = f"gate{number}"
        for key, value in payload.items():
            if not key.startswith(prefix):
                continue
            if value is True or (isinstance(value, str) and "passed" in value):
                found[prefix] = f"admission:{key}={value}"
                break
        if prefix in found:
            continue
        for reference, artifact in closure.items():
            kind = reference.artifact_kind
            if any(fragment in kind for fragment in _BLOCKED_FRAGMENTS):
                continue
            if not any(
                fragment in kind for fragment in _GATE_ROLE_FRAGMENTS[number]
            ):
                continue
            state = artifact.payload.get("state")
            complete = artifact.payload.get("complete")
            if state in _PASSING_STATES or (state is None and complete is True):
                found[prefix] = f"closure:{reference.artifact_id}"
                break
    return found


def _strict_gate57_evidence(
    payload: Mapping[str, Any],
    closure: Mapping[ArtifactReference, StableArtifact],
) -> str | None:
    """Confirm strict Gate 1--7 from the admission or its Gate 5--7 closure."""

    if payload.get("strict_gate_1_7") is True:
        return "admission:strict_gate_1_7"
    for reference, artifact in closure.items():
        if not any(kind in reference.artifact_kind for kind in _STRICT_GATE57_KINDS):
            continue
        values = artifact.payload
        if values.get("strict_gate_1_7") is True or values.get("qualified") is True:
            return f"closure:{reference.artifact_id}"
        outcome = str(values.get("outcome", ""))
        if any(marker in outcome for marker in _STRICT_MARKERS):
            return f"closure:{reference.artifact_id}"
    outcome = str(payload.get("outcome", ""))
    if any(marker in outcome for marker in _STRICT_MARKERS):
        return f"admission:outcome={outcome}"
    gate = str(payload.get("source_patch_test_gate", ""))
    if "qualified" in gate:
        return f"admission:source_patch_test_gate={gate}"
    return None


def _load_admission(
    root: Path, repository: str, change: int
) -> StableArtifact | None:
    base = root / "artifacts" / _ADMISSION_KIND / repository / str(change)
    if not base.is_dir():
        return None
    newest: StableArtifact | None = None
    for head in sorted(base.rglob("head.json")):
        relative = head.parent.relative_to(root / "artifacts" / _ADMISSION_KIND)
        parts = relative.parts
        artifact_id = f"{_ADMISSION_KIND}/" + "/".join(parts[:-1])
        artifact = load_stable_artifact(
            root,
            artifact_id=artifact_id,
            artifact_kind=_ADMISSION_KIND,
            semantic_version=parts[-1],
        )
        if newest is None or artifact.created_at > newest.created_at:
            newest = artifact
    return newest


def _closure(
    root: Path, reference: ArtifactReference
) -> dict[ArtifactReference, StableArtifact]:
    found: dict[ArtifactReference, StableArtifact] = {}
    pending = [reference]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        artifact = load_stable_artifact(
            root,
            artifact_id=current.artifact_id,
            artifact_kind=current.artifact_kind,
            semantic_version=current.semantic_version,
        )
        found[current] = artifact
        pending.extend(artifact.inputs)
    return found


def write_catalog_audit(
    artifact_root: str | Path,
    roster_path: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist the recomputed audit so the freeze decision is auditable."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("an active infrastructure-audit authorization is required")
    root = Path(artifact_root).absolute().resolve(strict=True)
    audits = audit_freeze_catalog(root, roster_path)
    shape = roster_shape_problems(roster_path)
    frozen_ready = not shape and all(audit.passed for audit in audits)
    locus = Counter(audit.defect_locus for audit in audits if audit.passed)
    repositories = Counter(audit.repository_key for audit in audits)
    payload = {
        "roster_path": str(Path(roster_path)),
        "state": "passed" if frozen_ready else "blocked",
        "complete": True,
        "freeze_ready": frozen_ready,
        "row_count": len(audits),
        "admitted_row_count": sum(1 for audit in audits if audit.passed),
        "admitted_locus_split": dict(locus),
        "repository_split": dict(repositories),
        "roster_structure_problems": list(shape),
        "rows": [audit.to_value() for audit in audits],
        "checked_requirements": [
            "formal_freeze_admission_head_present",
            "admission_state_admitted_and_complete",
            "strict_gate_1_7_asserted",
            *_REQUIRED_GATES,
            "no_old_attempts_or_synthetic_frames_or_inherited_lineage",
            "no_paid_or_formal_trial_authorization",
            "roster_structure_24_rows_16_at_goal_8_on_path",
        ],
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=_AUDIT.artifact_id,
        artifact_kind=_AUDIT.artifact_kind,
        semantic_version=_AUDIT.semantic_version,
        created_at=_now(),
        inputs=tuple(
            ArtifactReference(audit.admission_id, _ADMISSION_KIND, "1.0")
            for audit in audits
            if audit.admission_id
        ),
        payload=payload,
        authorization=authorization,
    )
    return load_stable_artifact(
        root,
        artifact_id=_AUDIT.artifact_id,
        artifact_kind=_AUDIT.artifact_kind,
        semantic_version=_AUDIT.semantic_version,
    )


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "RowAudit",
    "audit_freeze_catalog",
    "roster_shape_problems",
    "write_catalog_audit",
]
