"""Fresh current-v2 runtime audit and resumable blocker for JupyterLab #3662.

This is intentionally candidate-specific.  It checks only the already
registered #3662 chain and its exact historical Node/Karma/Firefox runtime
namespace.  It never installs packages, contacts a registry, runs candidate
code, or substitutes the host Node 22 runtime for Node 5.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .jupyterlab_historical_runtime import audit_local_host, audit_runtime_source
from .jupyterlab_regression import profile_for_candidate
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "jupyterlab", "change_number": 3662}
_PROFILE = ArtifactReference(
    "candidate_profile/jupyterlab/3662/preflight/v2", "candidate_profile", "2.0"
)
_PREPARATION = ArtifactReference(
    "candidate_preparation/jupyterlab/3662/preannotation/v2",
    "candidate_preparation",
    "2.0",
)
_CHAIN = (
    ArtifactReference(
        "candidate_source_packet/jupyterlab/3662/source/v2",
        "candidate_source_packet",
        "2.0",
    ),
    ArtifactReference(
        "candidate_test_patch/jupyterlab/3662/screening/v2",
        "candidate_test_patch",
        "2.0",
    ),
    ArtifactReference(
        "source_atomicity/jupyterlab/3662/preannotation/v2",
        "source_atomicity",
        "2.0",
    ),
    ArtifactReference(
        "candidate_stage/jupyterlab/3662/test_manifest/v2",
        "candidate_stage",
        "2.0",
    ),
    ArtifactReference(
        "fixed_source_capsule/jupyterlab/3662/fixed/v2",
        "fixed_source_capsule",
        "2.0",
    ),
    ArtifactReference(
        "candidate_materialization/jupyterlab/3662/fixed-buggy/v1",
        "candidate_materialization",
        "1.0",
    ),
    _PROFILE,
    _PREPARATION,
)
_AUDIT = ArtifactReference(
    "candidate_gate57_runtime_audit/jupyterlab/3662/runtime/v2",
    "candidate_gate57_runtime_audit",
    "2.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_fresh_execution_blocker/jupyterlab/3662/runtime/v2",
    "candidate_gate57_fresh_execution_blocker",
    "2.0",
)


def write_jupyterlab_3662_gate57_runtime_audit(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact]:
    """Record a fresh exact-runtime audit and a resumable Gate 5--7 blocker."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#3662 runtime audit authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(_load(root, reference) for reference in _CHAIN)
    _validate_chain(root, chain)
    profile = chain[6].payload
    runtime_root = str(profile["runtime_root"])
    runtime_path = root / PurePosixPath(runtime_root)
    runtime_profile = profile_for_candidate(CandidateKey("jupyterlab", 3662))
    runtime_audit = audit_runtime_source(
        runtime_profile, runtime_path if runtime_path.exists() else None
    )
    host_audit = audit_local_host(runtime_profile)
    runtime_present = _ordinary_directory(runtime_path)
    audit_payload = {
        "candidate": _CANDIDATE,
        "profile_id": str(profile["profile_id"]),
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "network_accessed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "reason": "registered_historical_runtime_closure_unavailable",
        "chain_reloaded": True,
        "runtime": {
            "dependency_id": str(profile["runtime_dependency_id"]),
            "runtime_root": runtime_root,
            "exact_runtime_root_present": runtime_present,
            "required_versions": dict(runtime_audit.required_versions),
            "observed_versions": dict(runtime_audit.observed_versions),
            "missing_components": list(runtime_audit.missing_components),
            "mismatched_components": [
                dict(value) for value in runtime_audit.mismatched_components
            ],
            "host_observed_versions": dict(host_audit.observed_versions),
            "host_missing_components": list(host_audit.missing_components),
            "host_mismatched_components": [
                dict(value) for value in host_audit.mismatched_components
            ],
            "host_substitute_rejected": {
                "node": "22.22.0 is not Node 5.x",
                "npm": "10.9.4 is not npm 3.8.6",
            },
            "required_components": [
                "node5_compatible_binary",
                "npm_3.8.6_closure",
                "karma_1.7.1_closure",
                "mocha_3.5.3_closure",
                "karma_mocha_0.2.2_closure",
                "webpack_2.7.0_closure",
                "typescript_2.6.2_closure",
                "firefox_57_binary_and_launcher",
            ],
        },
        "fresh_epoch": _fresh_epoch(),
        "next_action": "register_complete_offline_historical_runtime_then_run_fresh_12_cell_epoch",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    audit = _write_or_reload(
        root,
        _AUDIT,
        inputs=_CHAIN,
        payload=audit_payload,
        authorization=authorization,
    )
    blocker_payload = {
        "candidate": _CANDIDATE,
        "profile_id": str(profile["profile_id"]),
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "old_attempts_referenced": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "reason": "registered_historical_runtime_closure_unavailable",
        "runtime_audit_reference": _AUDIT.artifact_id,
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed_static",
            "fresh_gate57_epoch": "blocked_before_first_attempt",
        },
        "fresh_epoch": _fresh_epoch(),
        "next_action": audit_payload["next_action"],
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    blocker = _write_or_reload(
        root,
        _BLOCKER,
        inputs=(*_CHAIN, _AUDIT),
        payload=blocker_payload,
        authorization=authorization,
    )
    return audit, blocker


def _validate_chain(root: Path, chain: tuple[StableArtifact, ...]) -> None:
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain):
        raise RuntimeError("#3662 current-v2 chain candidate binding is invalid")
    (
        source,
        patch,
        atomicity,
        manifest,
        capsule,
        materialization,
        profile,
        preparation,
    ) = chain
    if source.payload.get("complete") is not True:
        raise RuntimeError("#3662 source packet is not complete")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("#3662 test patch is not eligible")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
    ):
        raise RuntimeError("#3662 source atomicity is not accepted")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("state") != "passed"
    ):
        raise RuntimeError("#3662 test manifest is not passed")
    if capsule.payload.get("source_origin") != "captured_merged_revision":
        raise RuntimeError("#3662 fixed capsule is not captured current-v2 source")
    if materialization.payload.get("complete") is not True:
        raise RuntimeError("#3662 fixed/buggy materialization is incomplete")
    if profile.payload.get("runtime_status") != "pending":
        raise RuntimeError("#3662 runtime status unexpectedly changed")
    if preparation.payload.get("state") != "prepared":
        raise RuntimeError("#3662 preparation is not current-v2 prepared")
    for key in ("fixed_tree_path", "buggy_tree_path"):
        path = materialization.payload.get(key)
        if type(path) is not str or not _ordinary_directory(root / PurePosixPath(path)):
            raise RuntimeError(f"#3662 {key} is not an ordinary directory")


def _ordinary_directory(path: Path) -> bool:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and resolved == path.resolve()
    )


def _fresh_epoch() -> dict[str, object]:
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


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _write_or_reload(
    root: Path,
    reference: ArtifactReference,
    *,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    try:
        existing = _load(root, reference)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
        existing = None
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"{reference.artifact_id} conflicts with fresh audit")
        return existing
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
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError(f"{reference.artifact_id} typed reload failed")
    return artifact


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_jupyterlab_3662_gate57_runtime_audit"]
