"""Candidate-specific authority review for three Luanti atomicity rejections.

This module is evidence-only.  It replays the current-v2 source, patch,
checkpoint and atomicity heads, then distinguishes explicit multi-defect PRs
from an incidental cross-reference.  It never recomputes Gate 2, executes a
candidate, or claims Gate 1--4.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_VERSION = "1.0"
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)


@dataclass(frozen=True)
class _ReviewSpec:
    number: int
    linked: tuple[int, ...]
    authoritative: tuple[int, ...]
    disposition: Literal["confirmed_multi_defect", "reclassification_preflight"]
    primary: int | None

    @property
    def candidate(self) -> dict[str, object]:
        return {"repository_key": "luanti", "change_number": self.number}

    @property
    def source(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_source_packet/luanti/{self.number}/source/v2",
            "candidate_source_packet",
            "2.0",
        )

    @property
    def patch(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_test_patch/luanti/{self.number}/screening/v2",
            "candidate_test_patch",
            "2.0",
        )

    @property
    def checkpoint(self) -> ArtifactReference:
        return ArtifactReference(
            f"screening_checkpoint/luanti/{self.number}/current/v2",
            "screening_checkpoint",
            "2.0",
        )

    @property
    def atomicity(self) -> ArtifactReference:
        return ArtifactReference(
            f"source_atomicity/luanti/{self.number}/preannotation/v2",
            "source_atomicity",
            "2.0",
        )

    @property
    def output(self) -> ArtifactReference:
        kind = (
            "candidate_gate2_authority_reclassification"
            if self.disposition == "reclassification_preflight"
            else "candidate_gate2_authority_blocker"
        )
        return ArtifactReference(
            f"{kind}/luanti/{self.number}/current-v2/v1", kind, _VERSION
        )


_SPECS = (
    _ReviewSpec(11427, (7574, 8719, 11600, 11899), (8719, 11899), "confirmed_multi_defect", None),
    _ReviewSpec(12595, (13486, 14210), (13486, 14210), "confirmed_multi_defect", None),
    _ReviewSpec(9187, (9195, 9277), (9195, 9277), "reclassification_preflight", 9277),
)


def write_luanti_authority_miskill_review(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, ...]:
    """Write two blockers and one nonterminal reclassification preflight."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    outputs: list[StableArtifact] = []
    for spec in _SPECS:
        chain = _load_chain(root, spec)
        manifest = _manifest(root, chain[0], spec)
        raw_patch = _raw_patch(root, chain[1], spec)
        payload = _payload(spec, manifest, raw_patch)
        reference = spec.output
        directory = root / "artifacts" / Path(reference.artifact_id) / _VERSION
        if directory.exists():
            artifact = _load(root, reference)
            inputs = tuple(item.reference for item in chain)
            if artifact.inputs != inputs or artifact.payload != payload:
                raise RuntimeError(
                    f"Luanti #{spec.number} existing authority review drifted"
                )
            outputs.append(artifact)
            continue
        inputs = tuple(item.reference for item in chain)
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
        artifact = _load(root, reference)
        if artifact.inputs != inputs or artifact.payload != payload:
            raise RuntimeError(f"Luanti #{spec.number} authority review reload failed")
        outputs.append(artifact)
    return tuple(outputs)


def _load_chain(root: Path, spec: _ReviewSpec) -> tuple[StableArtifact, ...]:
    source, patch, checkpoint, atomicity = (
        _load(root, reference)
        for reference in (spec.source, spec.patch, spec.checkpoint, spec.atomicity)
    )
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != spec.candidate
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, spec.source)
        or patch.payload.get("candidate") != spec.candidate
        or patch.payload.get("outcome") != "patch_eligible"
        or checkpoint.inputs != (_ENUMERATION, spec.patch)
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or atomicity.inputs != (_ENUMERATION, spec.source, spec.patch, spec.checkpoint)
        or atomicity.payload.get("candidate") != spec.candidate
        or atomicity.payload.get("linked_issue_numbers") != list(spec.linked)
        or atomicity.payload.get("authoritative_issue_numbers")
        != list(spec.authoritative)
        or atomicity.payload.get("outcome") != "rejected"
        or atomicity.payload.get("reason") != "multiple_authoritative_issues"
    ):
        raise RuntimeError(f"Luanti #{spec.number} current-v2 authority chain drifted")
    return source, patch, checkpoint, atomicity


def _manifest(root: Path, source: StableArtifact, spec: _ReviewSpec) -> dict[str, object]:
    value = _json_path(root, source.payload.get("raw_evidence_path"), "source manifest")
    import json

    try:
        manifest = json.loads(value.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Luanti #{spec.number} source manifest is invalid") from error
    if (
        type(manifest) is not dict
        or manifest.get("candidate") != spec.candidate
        or manifest.get("complete") is not True
        or manifest.get("issue_numbers") != list(spec.linked)
        or type(manifest.get("pull")) is not dict
        or manifest["pull"].get("number") != spec.number
        or manifest["pull"].get("merged") is not True
    ):
        raise RuntimeError(f"Luanti #{spec.number} source manifest binding drifted")
    rows = manifest.get("issues")
    if type(rows) is not list or [row.get("number") for row in rows] != list(spec.linked):
        raise RuntimeError(f"Luanti #{spec.number} issue inventory drifted")
    return manifest


def _raw_patch(root: Path, patch: StableArtifact, spec: _ReviewSpec) -> bytes:
    path = _json_path(root, patch.payload.get("raw_patch_path"), "raw patch")
    content = path.read_bytes()
    if not content.startswith(b"diff --git "):
        raise RuntimeError(f"Luanti #{spec.number} raw patch is invalid")
    return content


def _json_path(root: Path, value: object, label: str) -> Path:
    if type(value) is not str or not value:
        raise RuntimeError(f"{label} path is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or "." in relative.parts or ".." in relative.parts:
        raise RuntimeError(f"{label} path is unsafe")
    path = root.joinpath(*relative.parts)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"{label} file is unsafe")
    return path


def _issue(manifest: dict[str, object], number: int) -> dict[str, object]:
    rows = manifest["issues"]
    assert type(rows) is list
    row = next((value for value in rows if value.get("number") == number), None)
    if type(row) is not dict or type(row.get("issue")) is not dict:
        raise RuntimeError(f"Issue #{number} evidence is missing")
    return row


def _labels(row: dict[str, object]) -> tuple[str, ...]:
    issue = row["issue"]
    assert type(issue) is dict
    labels = issue.get("labels")
    if type(labels) is not list:
        raise RuntimeError("Issue labels are invalid")
    return tuple(
        value["name"]
        for value in labels
        if type(value) is dict and type(value.get("name")) is str
    )


def _comments(row: dict[str, object]) -> tuple[str, ...]:
    values = row.get("comments")
    if type(values) is not list:
        raise RuntimeError("Issue comments are invalid")
    return tuple(
        str(value.get("body")) for value in values if type(value) is dict
    )


def _payload(
    spec: _ReviewSpec, manifest: dict[str, object], raw_patch: bytes
) -> dict[str, object]:
    pull = manifest["pull"]
    assert type(pull) is dict
    body = pull.get("body")
    if type(body) is not str:
        raise RuntimeError(f"Luanti #{spec.number} PR body is invalid")
    if spec.number == 11427:
        issue_8719 = _issue(manifest, 8719)
        issue_11899 = _issue(manifest, 11899)
        if (
            "Closes #8719" not in body
            or "fixes #11899" not in body
            or "Bug" not in _labels(issue_8719)
            or "Bug" not in _labels(issue_11899)
            or b"handles NaN & infinities" not in raw_patch
            or b"self-referencing" not in body.encode()
        ):
            raise RuntimeError("Luanti #11427 multi-defect evidence drifted")
        return _blocker_payload(
            spec,
            incidental=(7574, 11600),
            defects=(
                {
                    "issue": 8719,
                    "scope": "circular_reference_serialization_corrupts_output",
                    "authority": "explicit_closes_in_pr_body",
                },
                {
                    "issue": 11899,
                    "scope": "nan_and_infinity_deserialization_failure",
                    "authority": "explicit_fixes_in_pr_body",
                },
            ),
        )
    if spec.number == 12595:
        issue_13486 = _issue(manifest, 13486)
        issue_14210 = _issue(manifest, 14210)
        if (
            "Fixes #13486, fixes #14210" not in body
            or "Bug" not in _labels(issue_13486)
            or "Unconfirmed bug" not in _labels(issue_14210)
            or b"void IMoveAction::onTake" not in raw_patch
            or b"move_count = src_item.count" not in raw_patch
        ):
            raise RuntimeError("Luanti #12595 multi-defect evidence drifted")
        return _blocker_payload(
            spec,
            incidental=(),
            defects=(
                {
                    "issue": 13486,
                    "scope": "swap_callbacks_in_wrong_take_put_order",
                    "authority": "explicit_fixes_in_pr_body",
                },
                {
                    "issue": 14210,
                    "scope": "shift_click_callback_reports_wrong_moved_count",
                    "authority": "explicit_fixes_in_pr_body",
                },
            ),
        )
    issue_9195 = _issue(manifest, 9195)
    issue_9277 = _issue(manifest, 9277)
    comments_9195 = _comments(issue_9195)
    comments_9277 = _comments(issue_9277)
    gui_button = _patch_section(raw_patch, "src/gui/guiButton.cpp")
    semantic_gui_button_changes = tuple(
        line
        for line in gui_button.splitlines()
        if line[:1] in {b"+", b"-"}
        and not line.startswith((b"+++", b"---"))
        and line[1:].strip()
    )
    if (
        "Bugfix" not in " ".join(
            label.get("name", "") for label in pull.get("labels", [])
        )
        or "Unconfirmed bug" not in _labels(issue_9277)
        or not any(
            "broken button text color style (which isn't mentioned in this issue)"
            in comment
            for comment in comments_9195
        )
        or not any("Will be fixed by  #9187" in comment for comment in comments_9277)
        or semantic_gui_button_changes
        or b"void EnrichedString::setDefaultColor" not in raw_patch
        or b"local color = minetest.colorize" not in raw_patch
    ):
        raise RuntimeError("Luanti #9187 reclassification evidence drifted")
    return {
        "candidate": spec.candidate,
        "state": "reclassification_preflight",
        "outcome": "eligible_for_gate2_recompute",
        "terminal": False,
        "resumable": True,
        "mechanical_atomicity_head": spec.atomicity.artifact_id,
        "mechanical_authoritative_issue_numbers": list(spec.authoritative),
        "proposed_primary_issue_number": 9277,
        "incidental_cross_reference_issue_numbers": [9195],
        "reclassification": {
            "issue_9277_scope": "formspec_label_colorize_static_text_regression",
            "issue_9195_disposition": "incidental_cross_reference",
            "issue_9195_reason": (
                "the cross-reference explicitly identifies button text color as absent from the issue; "
                "the PR has no semantic guiButton image-state change"
            ),
            "gui_button_semantic_patch_line_count": 0,
            "common_root_cause": "StaticText_and_EnrichedString_color_propagation",
        },
        "gate_claims": {
            "gate_1": "not_recomputed",
            "gate_2": "not_recomputed_candidate_specific_review_only",
            "gate_3": "not_claimed",
            "gate_4": "not_claimed",
            "gate_5_13": "not_claimed",
        },
        "candidate_execution_performed": False,
        "roster_modified": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "mechanically_recompute_gate2_with_primary_issue_9277_then_continue_gate3_4_if_accepted",
        "complete": True,
    }


def _blocker_payload(
    spec: _ReviewSpec,
    *,
    incidental: tuple[int, ...],
    defects: tuple[dict[str, object], ...],
) -> dict[str, object]:
    return {
        "candidate": spec.candidate,
        "state": "blocked",
        "outcome": "multiple_distinct_authoritative_defects_confirmed",
        "terminal": True,
        "resumable": False,
        "mechanical_atomicity_head": spec.atomicity.artifact_id,
        "linked_issue_numbers": list(spec.linked),
        "authoritative_issue_numbers": list(spec.authoritative),
        "incidental_cross_reference_issue_numbers": list(incidental),
        "distinct_defects": list(defects),
        "gate_claims": {
            "gate_1": "source_packet_present",
            "gate_2": "failed_multiple_distinct_defects",
            "gate_3_13": "not_claimed",
        },
        "candidate_execution_performed": False,
        "roster_modified": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }


def _patch_section(content: bytes, path: str) -> bytes:
    marker = f"diff --git a/{path} b/{path}\n".encode()
    start = content.find(marker)
    if start < 0:
        raise RuntimeError(f"patch section is missing: {path}")
    end = content.find(b"diff --git ", start + len(marker))
    return content[start:] if end < 0 else content[start:end]


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
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("authority review requires candidate-screening authorization")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_authority_miskill_review"]
