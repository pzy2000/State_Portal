"""Typed, resumable Gate 5--7 blocker for JupyterLab change 3662.

The current-v2 source, inverse patch, test manifest, fixed capsule, and
fixed/buggy materialization are available for this candidate.  The historical
Node 5/Karma/Mocha/Webpack/TypeScript/Firefox closure is not present locally,
so this module records the exact pre-execution state without running either
tree and without inheriting any earlier attempts.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "jupyterlab", "change_number": 3662}
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
    ArtifactReference(
        "candidate_profile/jupyterlab/3662/preflight/v2",
        "candidate_profile",
        "2.0",
    ),
    ArtifactReference(
        "candidate_preparation/jupyterlab/3662/preannotation/v2",
        "candidate_preparation",
        "2.0",
    ),
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_fresh_execution_blocker/jupyterlab/3662/runtime/v1",
    "candidate_gate57_fresh_execution_blocker",
    "1.0",
)


def write_jupyterlab_3662_gate57_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist a no-inheritance blocker and strictly reload it."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#3662 Gate 5--7 blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(_load(root, reference) for reference in _CHAIN)
    _validate_chain(root, chain)
    _assert_absent(root)

    profile = chain[6].payload
    materialization = chain[5].payload
    runtime_root = str(profile["runtime_root"])
    dependency_id = str(profile["runtime_dependency_id"])
    payload = {
        "candidate": _CANDIDATE,
        "profile_id": str(profile["profile_id"]),
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "reason": "registered_historical_runtime_closure_unavailable",
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed_static",
            "fresh_gate57_epoch": "blocked_before_first_attempt",
        },
        "runtime": {
            "dependency_id": dependency_id,
            "runtime_root": runtime_root,
            "required_versions": {
                "node": "5.x",
                "karma": "1.x",
                "mocha": "3.x",
                "karma_mocha": "0.x",
                "webpack": "2.x",
                "typescript": "2.6.2",
                "firefox": "historical_registered_binary",
            },
            "observed_runtime_root_present": (
                root / PurePosixPath(runtime_root)
            ).is_dir(),
            "observed_host_substitute": "Node 22.22.0/npm 10.9.4 is not accepted",
            "missing_components": [
                "node5_compatible_binary",
                "matching_npm_or_yarn_lock_closure",
                "karma_mocha_webpack_typescript_historical_closure",
                "registered_firefox_binary_and_launcher",
            ],
        },
        "fresh_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
            "cells": [
                {
                    "phase": "f2p",
                    "variant": "fixed",
                    "repetitions": 3,
                    "expected": "pass",
                },
                {
                    "phase": "f2p",
                    "variant": "buggy",
                    "repetitions": 3,
                    "expected": "failure",
                },
                {
                    "phase": "p2p",
                    "variant": "fixed",
                    "repetitions": 3,
                    "expected": "pass",
                },
                {
                    "phase": "p2p",
                    "variant": "buggy",
                    "repetitions": 3,
                    "expected": "pass",
                },
            ],
        },
        "materialization": {
            "fixed_tree_path": str(materialization["fixed_tree_path"]),
            "buggy_tree_path": str(materialization["buggy_tree_path"]),
            "fixed_buggy_pair_present": True,
            "capsule_present": True,
        },
        "next_action": "register_complete_offline_historical_runtime_then_run_fresh_12_cell_epoch",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    write_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
        created_at=_now(),
        inputs=tuple(reference for reference in _CHAIN),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _BLOCKER)
    if artifact.inputs != _CHAIN or dict(artifact.payload) != payload:
        raise RuntimeError("#3662 Gate 5--7 blocker typed reload failed")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


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
    if patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#3662 test patch is not eligible")
    if atomicity.payload.get("outcome") != "accepted":
        raise RuntimeError("#3662 source atomicity is not accepted")
    if manifest.payload.get("state") != "passed":
        raise RuntimeError("#3662 test manifest is not passed")
    if capsule.payload.get("source_origin") != "captured_merged_revision":
        raise RuntimeError("#3662 fixed capsule is not captured current-v2 source")
    if materialization.payload.get("complete") is not True:
        raise RuntimeError("#3662 fixed/buggy materialization is incomplete")
    if profile.payload.get("runtime_status") != "pending":
        raise RuntimeError("#3662 runtime status unexpectedly changed")
    if preparation.payload.get("state") != "prepared":
        raise RuntimeError("#3662 preparation is not current-v2 prepared")
    for variant in ("fixed", "buggy"):
        path = materialization.payload.get(f"{variant}_tree_path")
        if not isinstance(path, str):
            raise RuntimeError("#3662 materialization tree path is invalid")
        tree = root / PurePosixPath(path)
        info = tree.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("#3662 materialized tree is not an ordinary directory")


def _assert_absent(root: Path) -> None:
    directory = (
        root
        / "artifacts"
        / PurePosixPath(_BLOCKER.artifact_id)
        / _BLOCKER.semantic_version
    )
    if directory.exists():
        raise RuntimeError("#3662 fresh Gate 5--7 blocker already exists")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_jupyterlab_3662_gate57_blocker"]
