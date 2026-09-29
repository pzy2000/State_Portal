"""Offline Node 5/Karma readiness audit for JupyterLab #2437.

This lane reloads only the current-v2 #2437 source/preparation/capsule and
static Gate 4 admission.  It does not install packages, contact a registry,
run Node, build, or substitute the host Node 22 runtime for the required
historical Node 5 closure.
"""

from __future__ import annotations

import os
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


_CANDIDATE = {"repository_key": "jupyterlab", "change_number": 2437}
_SOURCE = ArtifactReference(
    "candidate_source_packet/jupyterlab/2437/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/jupyterlab/2437/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/jupyterlab/2437/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/jupyterlab/2437/test_manifest/v2", "candidate_stage", "2.0"
)
_PROFILE = ArtifactReference(
    "candidate_profile/jupyterlab/2437/preflight/v2", "candidate_profile", "2.0"
)
_PREPARATION = ArtifactReference(
    "candidate_preparation/jupyterlab/2437/preannotation/v2", "candidate_preparation", "2.0"
)
_ADMISSION = ArtifactReference(
    "candidate_gate4_static_admission/jupyterlab/2437/current-v2/v1",
    "candidate_gate4_static_admission",
    "1.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/jupyterlab/2437/fixed/v2", "fixed_source_capsule", "2.0"
)
_AUDIT = ArtifactReference(
    "candidate_gate57_runtime_audit/jupyterlab/2437/node5-karma/v1",
    "candidate_gate57_runtime_audit",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_runtime_readiness_blocker/jupyterlab/2437/node5-karma/v1",
    "candidate_gate57_runtime_readiness_blocker",
    "1.0",
)


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _chain(root: Path) -> tuple[StableArtifact, ...]:
    artifacts = tuple(
        _load(root, reference)
        for reference in (
            _SOURCE,
            _PATCH,
            _ATOMICITY,
            _MANIFEST,
            _PROFILE,
            _PREPARATION,
            _ADMISSION,
            _CAPSULE,
        )
    )
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in artifacts):
        raise RuntimeError("#2437 readiness chain is not candidate-bound")
    source, patch, atomicity, manifest, profile, preparation, admission, capsule = artifacts
    if (
        source.payload.get("complete") is not True
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("state") != "passed"
        or profile.payload.get("candidate_execution_performed") is not False
        or profile.payload.get("runtime_status") != "pending"
        or preparation.payload.get("state") != "prepared"
        or preparation.payload.get("candidate_execution_performed") is not False
        or admission.payload.get("state") != "admitted"
        or admission.payload.get("candidate_execution_performed") is not False
        or admission.payload.get("current_gate_status", {}).get("gate_5_7")
        != "downstream_pending"
        or capsule.payload.get("candidate_execution_performed") is not False
        or capsule.payload.get("profile_id") != profile.payload.get("profile_id")
    ):
        raise RuntimeError("#2437 current-v2 readiness chain is not closed")
    return artifacts


def _fresh_epoch() -> dict[str, Any]:
    cells = (
        ("f2p", "fixed", "pass"),
        ("f2p", "buggy", "failure"),
        ("p2p", "fixed", "pass"),
        ("p2p", "buggy", "pass"),
    )
    return {
        "required_attempt_count": 12,
        "attempt_count": 0,
        "inherit_prior_attempts": False,
        "old_attempts_referenced": False,
        "cells": [
            {"phase": phase, "variant": variant, "repetitions": 3, "expected": expected}
            for phase, variant, expected in cells
        ],
    }


def audit_and_write_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact]:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#2437 runtime readiness authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _chain(root)
    profile = chain[4].payload
    runtime_root = profile.get("runtime_root")
    dependency_id = profile.get("runtime_dependency_id")
    if type(runtime_root) is not str or type(dependency_id) is not str:
        raise RuntimeError("#2437 candidate runtime binding is invalid")
    runtime_path = root / PurePosixPath(runtime_root)
    runtime_present = runtime_path.is_dir() and not runtime_path.is_symlink()
    required_versions = {
        "node": "5.12.0",
        "npm": "3.8.6",
        "karma": "1.7.0",
        "mocha": "3.4.2",
        "karma_mocha": "0.2.2",
        "webpack": "2.6.1",
        "typescript": "2.2.1",
        "firefox": "53.0",
    }
    missing_components = [
        "node5_compatible_binary",
        "npm_3.8.6_closure",
        "karma_1.7.0_closure",
        "mocha_3.4.2_closure",
        "karma_mocha_0.2.2_closure",
        "webpack_2.6.1_closure",
        "typescript_2.2.1_closure",
        "firefox_53_binary_and_launcher",
    ]
    if runtime_present:
        # A present root still cannot be admitted without its complete typed
        # closure; this read-only audit deliberately does not execute probes.
        missing_components = ["candidate_runtime_closure_not_typed_verified"]
    runtime = {
        "dependency_id": dependency_id,
        "runtime_root": runtime_root,
        "exact_runtime_root_present": runtime_present,
        "required_versions": required_versions,
        "observed_versions": {},
        "missing_components": missing_components,
        "mismatched_components": [],
        "candidate_bound": True,
        "host_substitute_rejected": {
            "node": "host Node 22.x is not Node 5.12.0",
            "npm": "host npm 10.x is not npm 3.8.6",
        },
        "network_accessed": False,
    }
    audit_payload = {
        "candidate": _CANDIDATE,
        "profile_id": profile["profile_id"],
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "reason": "candidate_bound_historical_node5_karma_runtime_unavailable",
        "runtime": runtime,
        "fresh_epoch": _fresh_epoch(),
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate_8_13": "not_claimed",
        "next_action": "register_complete_candidate_bound_offline_node5_karma_closure_then_run_fresh_12_cell_epoch",
    }
    blocker_payload = {
        "candidate": _CANDIDATE,
        "profile_id": profile["profile_id"],
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "old_attempts_referenced": False,
        "reason": "candidate_bound_historical_node5_karma_runtime_unavailable",
        "runtime_audit_reference": _AUDIT.artifact_id,
        "runtime_dependency_id": dependency_id,
        "runtime_root": runtime_root,
        "missing_components": missing_components,
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed_static",
            "fresh_gate57_epoch": "blocked_before_first_attempt",
        },
        "fresh_epoch": _fresh_epoch(),
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate_8_13": "not_claimed",
        "next_action": audit_payload["next_action"],
    }
    inputs = tuple(artifact.reference for artifact in chain)
    write_stable_artifact(
        root,
        artifact_id=_AUDIT.artifact_id,
        artifact_kind=_AUDIT.artifact_kind,
        semantic_version=_AUDIT.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=audit_payload,
        authorization=authorization,
    )
    audit = _load(root, _AUDIT)
    if audit.inputs != inputs or dict(audit.payload) != audit_payload:
        raise RuntimeError("#2437 runtime audit typed reload failed")
    write_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
        created_at=_now(),
        inputs=(*inputs, audit.reference),
        payload=blocker_payload,
        authorization=authorization,
    )
    blocker = _load(root, _BLOCKER)
    if blocker.inputs != (*inputs, audit.reference) or dict(blocker.payload) != blocker_payload:
        raise RuntimeError("#2437 runtime blocker typed reload failed")
    return audit, blocker


def _auth() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="authorization/jupyterlab-2437-runtime-readiness-v1",
        scope="preannotation-execution",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )


if __name__ == "__main__":
    audit, blocker = audit_and_write_blocker(
        ".warptest-cache/c1_lazarus_selection_v1", authorization=_auth()
    )
    print(audit.reference.artifact_id)
    print(blocker.reference.artifact_id)


__all__ = ["audit_and_write_blocker"]
