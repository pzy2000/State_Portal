"""Candidate-specific Gate 2 recomputation for Luanti #9187 and #11866.

The current-v2 mechanical receipts over-classified an incidental issue for
#9187 and selected a release-planning discussion for #11866.  Earlier typed
reviews reclassified each candidate to its unique defect issue.  This module
strictly reloads those reviews and the frozen current-v2 source chain, then
writes a nonterminal Gate 2 admission.  It does not evaluate Gate 3 or Gate 4,
execute candidate code, change the 24-row roster, or authorize paid/formal
work.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

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
class _Gate2Spec:
    number: int
    linked: tuple[int, ...]
    original_authoritative: tuple[int, ...]
    original_outcome: str
    original_primary: int | None
    original_reason: str | None
    primary: int
    incidental: tuple[int, ...]
    reclassification: ArtifactReference
    test_paths: tuple[str, ...]

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
    def manifest(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_stage/luanti/{self.number}/test_manifest/v2",
            "candidate_stage",
            "2.0",
        )

    @property
    def output(self) -> ArtifactReference:
        kind = "candidate_gate2_recomputed_admission"
        return ArtifactReference(
            f"{kind}/luanti/{self.number}/current-v2/v1", kind, _VERSION
        )


_SPECS = (
    _Gate2Spec(
        number=9187,
        linked=(9195, 9277),
        original_authoritative=(9195, 9277),
        original_outcome="rejected",
        original_primary=None,
        original_reason="multiple_authoritative_issues",
        primary=9277,
        incidental=(9195,),
        reclassification=ArtifactReference(
            "candidate_gate2_authority_reclassification/luanti/9187/current-v2/v1",
            "candidate_gate2_authority_reclassification",
            "1.0",
        ),
        test_paths=(
            "games/minimal/mods/test/formspec.lua",
            "src/unittest/test_utilities.cpp",
        ),
    ),
    _Gate2Spec(
        number=11866,
        linked=(11600, 11839),
        original_authoritative=(11600,),
        original_outcome="accepted",
        original_primary=11600,
        original_reason=None,
        primary=11839,
        incidental=(11600,),
        reclassification=ArtifactReference(
            "candidate_issue_classification/luanti/11866/preannotation/v1",
            "candidate_issue_classification",
            "1.0",
        ),
        test_paths=("src/unittest/test_map.cpp", "src/unittest/test_player.cpp"),
    ),
)


def write_luanti_gate2_recomputed_admissions(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, ...]:
    """Write and strictly reload both candidate-specific Gate 2 admissions."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    outputs: list[StableArtifact] = []
    for spec in _SPECS:
        chain = _load_chain(root, spec)
        source, patch, checkpoint, atomicity, manifest, reclassification = chain
        source_manifest = _source_manifest(root, source, spec)
        raw_patch = _raw_patch(root, patch, spec)
        _validate_reclassification(
            spec, reclassification, source_manifest, raw_patch
        )
        payload = _payload(spec, atomicity, manifest)
        inputs = tuple(artifact.reference for artifact in chain)
        directory = root / "artifacts" / Path(spec.output.artifact_id) / _VERSION
        if directory.exists():
            artifact = _load(root, spec.output)
            if artifact.inputs != inputs or artifact.payload != payload:
                raise RuntimeError(
                    f"Luanti #{spec.number} existing Gate 2 admission drifted"
                )
        else:
            write_stable_artifact(
                root,
                artifact_id=spec.output.artifact_id,
                artifact_kind=spec.output.artifact_kind,
                semantic_version=spec.output.semantic_version,
                created_at=_now(),
                inputs=inputs,
                payload=payload,
                authorization=authorization,
            )
            artifact = _load(root, spec.output)
            if artifact.inputs != inputs or artifact.payload != payload:
                raise RuntimeError(
                    f"Luanti #{spec.number} Gate 2 admission reload failed"
                )
        outputs.append(artifact)
    return tuple(outputs)


def _load_chain(root: Path, spec: _Gate2Spec) -> tuple[StableArtifact, ...]:
    source, patch, checkpoint, atomicity, manifest, reclassification = (
        _load(root, reference)
        for reference in (
            spec.source,
            spec.patch,
            spec.checkpoint,
            spec.atomicity,
            spec.manifest,
            spec.reclassification,
        )
    )
    atomicity_payload = atomicity.payload
    details = manifest.payload.get("details")
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != spec.candidate
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, spec.source)
        or patch.payload.get("candidate") != spec.candidate
        or patch.payload.get("outcome") != "patch_eligible"
        or checkpoint.inputs != (_ENUMERATION, spec.patch)
        or checkpoint.payload.get("candidate") != spec.candidate
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or atomicity.inputs != (_ENUMERATION, spec.source, spec.patch, spec.checkpoint)
        or atomicity_payload.get("candidate") != spec.candidate
        or atomicity_payload.get("linked_issue_numbers") != list(spec.linked)
        or atomicity_payload.get("authoritative_issue_numbers")
        != list(spec.original_authoritative)
        or atomicity_payload.get("outcome") != spec.original_outcome
        or atomicity_payload.get("primary_issue_number") != spec.original_primary
        or atomicity_payload.get("reason") != spec.original_reason
        or manifest.inputs != (spec.patch,)
        or manifest.payload.get("candidate") != spec.candidate
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("complete") is not True
        or type(details) is not dict
        or details.get("executable_test_paths") != list(spec.test_paths)
        or details.get("test_count") != len(spec.test_paths)
    ):
        raise RuntimeError(f"Luanti #{spec.number} current-v2 Gate 2 chain drifted")
    return source, patch, checkpoint, atomicity, manifest, reclassification


def _source_manifest(
    root: Path, source: StableArtifact, spec: _Gate2Spec
) -> dict[str, Any]:
    path = _safe_evidence_path(
        root, source.payload.get("raw_evidence_path"), "source manifest"
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Luanti #{spec.number} source manifest is invalid") from error
    if (
        type(value) is not dict
        or value.get("candidate") != spec.candidate
        or value.get("complete") is not True
        or value.get("issue_numbers") != list(spec.linked)
        or type(value.get("pull")) is not dict
        or value["pull"].get("number") != spec.number
        or value["pull"].get("merged") is not True
    ):
        raise RuntimeError(f"Luanti #{spec.number} source authority drifted")
    rows = value.get("issues")
    if type(rows) is not list or [row.get("number") for row in rows] != list(spec.linked):
        raise RuntimeError(f"Luanti #{spec.number} issue inventory drifted")
    return value


def _raw_patch(root: Path, patch: StableArtifact, spec: _Gate2Spec) -> bytes:
    path = _safe_evidence_path(root, patch.payload.get("raw_patch_path"), "raw patch")
    content = path.read_bytes()
    if not content.startswith(b"diff --git "):
        raise RuntimeError(f"Luanti #{spec.number} raw patch is invalid")
    return content


def _validate_reclassification(
    spec: _Gate2Spec,
    artifact: StableArtifact,
    source_manifest: dict[str, Any],
    raw_patch: bytes,
) -> None:
    issues = {
        row.get("number"): row.get("issue")
        for row in source_manifest["issues"]
        if type(row) is dict and type(row.get("issue")) is dict
    }
    if set(issues) != set(spec.linked):
        raise RuntimeError(f"Luanti #{spec.number} issue details drifted")
    if spec.number == 9187:
        payload = artifact.payload
        if (
            payload.get("candidate") != spec.candidate
            or payload.get("outcome") != "eligible_for_gate2_recompute"
            or payload.get("proposed_primary_issue_number") != spec.primary
            or payload.get("incidental_cross_reference_issue_numbers")
            != list(spec.incidental)
            or payload.get("terminal") is not False
            or payload.get("candidate_execution_performed") is not False
            or issues[9277].get("title")
            != "minetest.colorize doesn't work with formspec labels"
            or b"void EnrichedString::setDefaultColor" not in raw_patch
            or b"local color = minetest.colorize" not in raw_patch
        ):
            raise RuntimeError("Luanti #9187 Gate 2 reclassification drifted")
        return
    payload = artifact.payload
    selected = payload.get("selected_primary_issue")
    previous = payload.get("previous_primary_issue")
    pull_scope = payload.get("pull_scope")
    if (
        payload.get("candidate") != spec.candidate
        or payload.get("outcome") != "issue_classified"
        or type(selected) is not dict
        or selected.get("number") != spec.primary
        or selected.get("classification") != "actual_bug_report"
        or type(previous) is not dict
        or previous.get("number") != 11600
        or previous.get("classification") != "release_planning_discussion"
        or type(pull_scope) is not dict
        or pull_scope.get("single_defect_mapping") is not True
        or payload.get("terminal") is not False
        or payload.get("candidate_execution_performed") is not False
        or issues[11600].get("title") != "5.5 release plans"
        or issues[11839].get("title")
        != "Node burning handler that does not delete node lags server, produces 2.5MB logs / second"
        or b"#define MAX_MAP_GENERATION_LIMIT (31007)" not in raw_patch
        or b"void TestMap::testMaxMapgenLimit()" not in raw_patch
    ):
        raise RuntimeError("Luanti #11866 Gate 2 reclassification drifted")


def _payload(
    spec: _Gate2Spec, atomicity: StableArtifact, manifest: StableArtifact
) -> dict[str, object]:
    return {
        "receipt_schema": "c1_lazarus_candidate_gate2_recomputed_admission_v1",
        "candidate": spec.candidate,
        "state": "gate2_passed_candidate_specific",
        "outcome": "accepted",
        "terminal": False,
        "resumable": True,
        "current_v2_atomicity_head": spec.atomicity.artifact_id,
        "current_v2_atomicity_created_at": atomicity.created_at,
        "reclassification_receipt": spec.reclassification.artifact_id,
        "linked_issue_numbers": list(spec.linked),
        "original_authoritative_issue_numbers": list(spec.original_authoritative),
        "original_primary_issue_number": spec.original_primary,
        "authoritative_issue_numbers": [spec.primary],
        "authoritative_issue_count": 1,
        "primary_issue_number": spec.primary,
        "incidental_issue_numbers": list(spec.incidental),
        "single_defect_mapping": "passed_after_candidate_specific_reclassification",
        "gate_claims": {
            "gate_1": "source_packet_present_not_recomputed",
            "gate_2": "passed_candidate_specific_recompute",
            "gate_3": "pending_strict_production_only_inverse_audit",
            "gate_4": "pending_fixed_buggy_test_api_compatibility_audit",
            "gate_5_13": "not_claimed",
        },
        "gate_3_4_prerequisites": {
            "gate_3": (
                "derive_and_validate_a_strict_production_only_inverse_from_the_frozen_patch"
            ),
            "gate_4": (
                "prove_the_preserved_tests_use_APIs_available_in_both_fixed_and_buggy_sources"
            ),
            "test_manifest_reference": spec.manifest.artifact_id,
            "test_manifest_created_at": manifest.created_at,
            "preserved_test_paths": list(spec.test_paths),
            "candidate_profile_present": False,
            "candidate_preparation_present": False,
        },
        "candidate_execution_performed": False,
        "build_performed": False,
        "roster_modified": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }


def _safe_evidence_path(root: Path, value: object, label: str) -> Path:
    if type(value) is not str or not value:
        raise RuntimeError(f"{label} path is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or "." in relative.parts or ".." in relative.parts:
        raise RuntimeError(f"{label} path is unsafe")
    path = root.joinpath(*relative.parts)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"{label} file is unsafe")
    return path


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
        raise PermissionError(
            "Gate 2 recomputation requires candidate-screening authorization"
        )


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_gate2_recomputed_admissions"]
