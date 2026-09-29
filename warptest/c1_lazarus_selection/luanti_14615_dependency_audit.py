"""Candidate-specific, resumable native dependency audit for Luanti #14615.

The #14615 regression is only observable when compiling against libcurl before
7.65: the buggy source references ``CURLUPART_ZONEID`` unconditionally while
the fixed source guards it.  This audit narrows the old broad Gate 5--7
blocker to that exact missing dependency.  It never substitutes the registered
curl 8.x closure, downloads a dependency, materializes a tree, builds, or runs
a test process.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 14615}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14615/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14615/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14615/preannotation/v2",
    "source_atomicity",
    "2.0",
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14615/test_manifest/v2",
    "candidate_stage",
    "2.0",
)
_PRIOR_BLOCKER = ArtifactReference(
    "candidate_gate57_14615_current_chain_blocker/luanti/14615/native/v1",
    "candidate_gate57_14615_current_chain_blocker",
    "1.0",
)
_AUDIT = ArtifactReference(
    "candidate_gate57_14615_dependency_audit/luanti/14615/native/v1",
    "candidate_gate57_14615_dependency_audit",
    "1.0",
)

_MIRROR_RELATIVE = Path("private/repository-enumeration-git/luanti/mirror.git")
_OLD_CURL_RELATIVE = Path("private/dependencies/luanti-curl/14615")
_MODERN_CURL_RELATIVE = Path("private/dependencies/luanti-curl/1.0")
_FIXED_REVISION = "59bf1d8cd97a1643b88b44374885c319c79269f5"
_BUGGY_PARENT_REVISION = "780543f0a2d2905a5b4e6a8f63e1ac436646dfd6"


def audit_luanti_14615_dependency(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Write and strict-reload the current, no-network dependency blocker."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, reference)
        for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _PRIOR_BLOCKER)
    )
    source, patch, atomicity, manifest, prior_blocker = chain
    _validate_current_chain(source, patch, atomicity, manifest, prior_blocker)

    mirror = root / _MIRROR_RELATIVE
    fixed_revision_present = _has_commit(mirror, _FIXED_REVISION)
    buggy_parent_present = _has_commit(mirror, _BUGGY_PARENT_REVISION)
    old_curl = root / _OLD_CURL_RELATIVE
    modern_curl = root / _MODERN_CURL_RELATIVE
    old_curl_registered = old_curl.is_dir()
    modern_curl_registered = modern_curl.is_dir()

    payload = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "candidate_specific_pre_7_65_curl_closure_not_locally_registered",
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed_static_guard_audit",
            "version_materialization": (
                "ready_from_local_mirror"
                if fixed_revision_present and buggy_parent_present
                else "blocked_missing_local_mirror_revision"
            ),
            "fresh_gate57_epoch": "blocked_before_first_attempt",
        },
        "source_mirror_probe": {
            "mirror_relative_path": _MIRROR_RELATIVE.as_posix(),
            "fixed_revision_readable": fixed_revision_present,
            "buggy_parent_revision_readable": buggy_parent_present,
            "network_used": False,
        },
        "curl_dependency_probe": {
            "required_relative_path": _OLD_CURL_RELATIVE.as_posix(),
            "required_api_epoch": "pre_7_65",
            "old_curl_registered": old_curl_registered,
            "modern_curl_relative_path": _MODERN_CURL_RELATIVE.as_posix(),
            "modern_curl_registered": modern_curl_registered,
            "modern_curl_rejected_as_substitute": True,
        },
        "next_action": (
            "register a content-addressed pre-7.65 curl headers-and-libraries closure at "
            "the candidate-specific path, then materialize the mirror revisions and run "
            "an independent fresh 12-cell epoch"
        ),
        "fresh_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
        },
        "candidate_materialization_performed": False,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = tuple(item.reference for item in chain)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#14615 dependency audit conflicts with current state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_AUDIT.artifact_id,
        artifact_kind=_AUDIT.artifact_kind,
        semantic_version=_AUDIT.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    audit = _load(root, _AUDIT)
    if audit.inputs != inputs or dict(audit.payload) != payload:
        raise RuntimeError("#14615 dependency audit typed reload failed")
    return audit


def _validate_current_chain(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
    prior_blocker: StableArtifact,
) -> None:
    if any(
        item.payload.get("candidate") != _CANDIDATE
        for item in (source, patch, atomicity, manifest, prior_blocker)
    ):
        raise RuntimeError("#14615 current-v2 candidate binding drifted")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#14615 source authority is incomplete")
    if patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#14615 test patch is not eligible")
    if tuple(patch.payload.get("production_paths", ())) != (
        "src/gui/guiOpenURL.cpp",
        "src/util/colorize.cpp",
        "src/util/colorize.h",
    ):
        raise RuntimeError("#14615 production patch inventory drifted")
    if tuple(patch.payload.get("test_paths", ())) != (
        "src/unittest/test_utilities.cpp",
    ):
        raise RuntimeError("#14615 test patch inventory drifted")
    if (
        atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 14598
    ):
        raise RuntimeError("#14615 atomicity mapping is not accepted")
    details = manifest.payload.get("details")
    if (
        manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != ["src/unittest/test_utilities.cpp"]
        or details.get("test_count") != 1
    ):
        raise RuntimeError("#14615 manifest is not executable")
    if (
        prior_blocker.payload.get("outcome") != "infrastructure_blocked"
        or prior_blocker.payload.get("terminal") is not False
        or prior_blocker.payload.get("old_attempts_loaded") is not False
    ):
        raise RuntimeError("#14615 prior blocker is not a resumable no-attempt receipt")


def _has_commit(mirror: Path, revision: str) -> bool:
    if not mirror.is_dir():
        return False
    result = subprocess.run(
        ["git", f"--git-dir={mirror}", "cat-file", "-e", f"{revision}^{{commit}}"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
    )
    return result.returncode == 0


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path) -> StableArtifact | None:
    try:
        return _load(root, _AUDIT)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14615 dependency audit authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["audit_luanti_14615_dependency"]
