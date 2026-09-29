"""Candidate-specific recovery audit for Luanti #14464's curl build blocker.

The current-v2 Gate 1--4 chain is valid, but the source-preserving build
configured with ``ENABLE_CURL=FALSE`` fails at link time.  The only registered
curl closure is candidate-private in location yet records ``/opt/miniconda3``
as its source-registration root, so it is not an admissible candidate-owned
closure for a new epoch.  This audit writes a nonterminal blocker without
touching source/test bytes, old attempts/trees, or any protected process.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 14464}
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14464/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14464/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/14464/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14464/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14464/test_manifest/v2", "candidate_stage", "2.0"
)
_PROFILE = ArtifactReference(
    "candidate_profile/luanti/14464/preflight/v2", "candidate_profile", "2.0"
)
_PREPARATION = ArtifactReference(
    "candidate_preparation/luanti/14464/preannotation/v2",
    "candidate_preparation",
    "2.0",
)
_PLAN = ArtifactReference(
    "candidate_downstream_plan/luanti/14464/preannotation/v2",
    "candidate_downstream_plan",
    "2.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/14464/fixed/v2", "fixed_source_capsule", "2.0"
)
_V2_BLOCKER = ArtifactReference(
    "candidate_gate57_14464_fresh_blocker/luanti/14464/native/v2",
    "candidate_gate57_14464_fresh_blocker",
    "1.0",
)
_CURL = ArtifactReference(
    "candidate_gate57_14464_curl_dependency/luanti/14464/native/v1",
    "candidate_gate57_14464_curl_dependency",
    "1.0",
)
_CONFIGURE = ArtifactReference(
    "candidate_gate57_14464_fresh_build/luanti/14464/fixed/configure/v2",
    "candidate_gate57_14464_fresh_build",
    "1.0",
)
_BUILD = ArtifactReference(
    "candidate_gate57_14464_fresh_build/luanti/14464/fixed/build/v2",
    "candidate_gate57_14464_fresh_build",
    "1.0",
)
_RECOVERY_BLOCKER = ArtifactReference(
    "candidate_gate57_14464_curl_recovery_blocker_v2/luanti/14464/native/v2",
    "candidate_gate57_14464_curl_recovery_blocker_v2",
    "1.0",
)


def audit_luanti_14464_curl_recovery(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist a typed nonterminal blocker when curl closure recovery is inadmissible."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, reference)
        for reference in (
            _ENUMERATION,
            _SOURCE,
            _PATCH,
            _CHECKPOINT,
            _ATOMICITY,
            _MANIFEST,
            _PROFILE,
            _PREPARATION,
            _PLAN,
            _CAPSULE,
        )
    )
    v2_blocker = _load(root, _V2_BLOCKER)
    curl = _load(root, _CURL)
    _validate_chain(chain)
    _validate_v2_blocker(v2_blocker)
    closure_audit = _audit_curl_closure(root, curl)
    config_audit = _audit_source_preserving_config(root, v2_blocker)
    if closure_audit["admissible"] or config_audit["admissible"]:
        raise RuntimeError(
            "#14464 recovery audit found an admissible closure; do not write blocker"
        )

    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "profile_id": "luanti-14464-iconv-state-curl-recovery-v2",
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_eligibility": "gate57_recovery_blocked_missing_admissible_curl_closure",
        "reason": "registered_curl_closure_not_candidate_owned_and_source_preserving_config_fails",
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "passed",
            "gate_3_strict_inverse_patch": "passed",
            "gate_4_fixed_test_api_compatibility": "passed",
            "gate_5_fixed_f2p_3x": "not_run_missing_admissible_curl_closure",
            "gate_6_buggy_f2p_3x_failure": "not_run_missing_admissible_curl_closure",
            "gate_7_fixed_buggy_p2p_3x": "not_run_missing_admissible_curl_closure",
        },
        "chain_refs": {
            "repository_enumeration": _ENUMERATION.artifact_id,
            "source": _SOURCE.artifact_id,
            "patch": _PATCH.artifact_id,
            "screening_checkpoint": _CHECKPOINT.artifact_id,
            "atomicity": _ATOMICITY.artifact_id,
            "test_manifest": _MANIFEST.artifact_id,
            "profile": _PROFILE.artifact_id,
            "preparation": _PREPARATION.artifact_id,
            "downstream_plan": _PLAN.artifact_id,
            "fixed_capsule": _CAPSULE.artifact_id,
        },
        "existing_v2_build_blocker": {
            "reference": _V2_BLOCKER.artifact_id,
            "outcome": v2_blocker.payload["outcome"],
            "reason": v2_blocker.payload["reason"],
            "error": v2_blocker.payload["error"],
            "attempt_count": v2_blocker.payload["attempt_count"],
            "old_attempts_loaded": v2_blocker.payload["old_attempts_loaded"],
        },
        "curl_closure_audit": closure_audit,
        "source_preserving_config_audit": config_audit,
        "fresh_gate57_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
            "candidate_specific_runner_started": False,
        },
        "source_bytes_modified": False,
        "test_bytes_modified": False,
        "candidate_materialization_performed": False,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "roster_modified": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": (
            "register a candidate-owned same-version curl closure with provenance "
            "or prove a source-preserving curl-enabled configuration, then run at "
            "most one fresh Gate 5--7 epoch"
        ),
        "resource_policy": {
            "network": False,
            "paid": False,
            "formal_trials": False,
            "protected_processes": False,
        },
    }
    inputs = tuple(item.reference for item in chain) + (
        _V2_BLOCKER,
        _CURL,
        _CONFIGURE,
        _BUILD,
    )
    try:
        existing = _load(root, _RECOVERY_BLOCKER)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#14464 recovery blocker conflicts with exact state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_RECOVERY_BLOCKER.artifact_id,
        artifact_kind=_RECOVERY_BLOCKER.artifact_kind,
        semantic_version=_RECOVERY_BLOCKER.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    result = _load(root, _RECOVERY_BLOCKER)
    if result.inputs != inputs or dict(result.payload) != payload:
        raise RuntimeError("#14464 recovery blocker typed reload failed")
    return result


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    (
        enumeration,
        source,
        patch,
        checkpoint,
        atomicity,
        manifest,
        profile,
        preparation,
        plan,
        capsule,
    ) = chain
    if enumeration.payload.get("repository_key") != "luanti":
        raise RuntimeError("#14464 enumeration is not Luanti")
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
        or patch.inputs != (_ENUMERATION, _SOURCE)
        or patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
        or checkpoint.inputs != (_ENUMERATION, _PATCH)
        or checkpoint.payload.get("candidate") != _CANDIDATE
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "patch_eligible"
        or atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != 14463
        or manifest.inputs != (_PATCH,)
        or manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("details", {}).get("test_count") != 1
        or profile.inputs != (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
        or profile.payload.get("candidate") != _CANDIDATE
        or preparation.inputs != (_PROFILE,)
        or preparation.payload.get("candidate") != _CANDIDATE
        or preparation.payload.get("state") != "prepared"
        or plan.inputs != (_PROFILE, _PREPARATION)
        or plan.payload.get("candidate") != _CANDIDATE
        or plan.payload.get("state") != "planned"
        or capsule.payload.get("candidate") != _CANDIDATE
        or capsule.payload.get("source_tree_path")
        != "private/fixed-capsules/luanti/14464/fixed-source"
    ):
        raise RuntimeError("#14464 current-v2 Gate 1--4 chain drifted")
    statuses = profile.payload.get("strict_gate_status", {})
    if any(
        statuses.get(key) != "passed"
        for key in (
            "source_authority",
            "single_defect_mapping",
            "strict_inverse_patch",
            "test_api_compatibility",
        )
    ):
        raise RuntimeError("#14464 current-v2 strict Gate 1--4 profile drifted")


def _validate_v2_blocker(blocker: StableArtifact) -> None:
    payload = blocker.payload
    if (
        payload.get("candidate") != _CANDIDATE
        or payload.get("state") != "blocked"
        or payload.get("outcome") != "infrastructure_blocked"
        or payload.get("error") != "#14464 fixed build failed"
        or payload.get("attempt_count") != 0
        or payload.get("old_attempts_loaded") is not False
    ):
        raise RuntimeError("#14464 existing v2 curl build blocker drifted")


def _audit_curl_closure(root: Path, closure: StableArtifact) -> dict[str, Any]:
    payload = closure.payload
    runtime = payload.get("runtime_root")
    files = payload.get("closure_files")
    provenance = payload.get("source_provenance")
    if (
        payload.get("candidate") != _CANDIDATE
        or payload.get("semantic_version") != "1.0"
        or runtime != "private/dependencies/luanti-curl/1.0"
        or not isinstance(files, list)
        or not isinstance(provenance, dict)
    ):
        raise RuntimeError("#14464 registered curl closure metadata drifted")
    closure_root = root / runtime
    regular_files = all(
        (closure_root / relative).is_file()
        and not (closure_root / relative).is_symlink()
        for relative in files
        if isinstance(relative, str)
    )
    registration_root = provenance.get("registration_root")
    candidate_owned = registration_root == runtime
    return {
        "reference": closure.reference.artifact_id,
        "semantic_version": payload["semantic_version"],
        "runtime_root": runtime,
        "registered_file_count": len(files),
        "registered_files_present": regular_files,
        "integrity_recorded": isinstance(payload.get("file_integrity"), dict),
        "source_registration_root": registration_root,
        "candidate_owned": candidate_owned,
        "admissible": bool(regular_files and candidate_owned),
        "rejection_reason": (
            "source_registration_root_is_external_not_candidate_owned"
            if not candidate_owned
            else None
        ),
    }


def _audit_source_preserving_config(
    root: Path, blocker: StableArtifact
) -> dict[str, Any]:
    runtime_path = root / "private/dependencies/luanti-native/3.0/runtime.json"
    if not runtime_path.is_file() or runtime_path.is_symlink():
        raise RuntimeError("#14464 native toolchain runtime is missing or unsafe")
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    package_roots = runtime.get("package_roots", {})
    configure = _load(root, _CONFIGURE)
    build = _load(root, _BUILD)
    returncode = build.payload.get("returncode")
    argv = configure.payload.get("argv")
    curl_registered = isinstance(package_roots, dict) and "curl" in package_roots
    curl_disabled = isinstance(argv, list) and "-DENABLE_CURL=FALSE" in argv
    unresolved_link = returncode == 1
    return {
        "toolchain_runtime": "private/dependencies/luanti-native/3.0/runtime.json",
        "registered_package_roots": sorted(package_roots)
        if isinstance(package_roots, dict)
        else [],
        "curl_package_registered": curl_registered,
        "existing_configure_disabled_curl": curl_disabled,
        "existing_fixed_build_returncode": returncode,
        "existing_fixed_build_failed": unresolved_link,
        "source_bytes_preserved": True,
        "admissible": bool(
            curl_registered and not curl_disabled and not unresolved_link
        ),
        "rejection_reason": (
            "native_toolchain_has_no_registered_curl_and_ENABLE_CURL_FALSE_build_has_unresolved_curl_symbols"
            if not curl_registered and curl_disabled and unresolved_link
            else None
        ),
    }


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14464 recovery audit authorization is inactive")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["audit_luanti_14464_curl_recovery"]
