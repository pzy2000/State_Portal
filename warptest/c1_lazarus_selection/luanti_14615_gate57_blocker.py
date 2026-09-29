"""Typed, resumable Gate 5--7 blocker for Luanti #14615.

The current-v2 source, patch, atomicity and test-manifest receipts for this
curl-compatibility regression are present.  There is no candidate-specific
profile, fixed-source capsule, old-curl dependency closure, or fresh native
runner yet.  This module records that exact preflight stop without building,
executing, inheriting another candidate's attempts, or scanning the artifact
tree.  A later worker may resume after registering the missing chain.
"""

from __future__ import annotations

import os
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
_BLOCKER = ArtifactReference(
    "candidate_gate57_14615_current_chain_blocker/luanti/14615/native/v1",
    "candidate_gate57_14615_current_chain_blocker",
    "1.0",
)

_PRODUCTION_PATHS = (
    "src/gui/guiOpenURL.cpp",
    "src/util/colorize.cpp",
    "src/util/colorize.h",
)
_TEST_PATHS = ("src/unittest/test_utilities.cpp",)


def write_luanti_14615_gate57_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist and reload a no-current-native-chain receipt."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14615 Gate 5--7 blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, reference) for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    )
    source, patch, atomicity, manifest = chain
    _validate_chain(source, patch, atomicity, manifest)

    # Probe only exact candidate namespaces.  If any of these appears, the
    # blocker is stale and must not mask a newly registered execution chain.
    required = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/14615",
        "candidate_preparation": root / "artifacts/candidate_preparation/luanti/14615",
        "candidate_downstream_plan": root
        / "artifacts/candidate_downstream_plan/luanti/14615",
        "fixed_source_capsule": root / "artifacts/fixed_source_capsule/luanti/14615",
        "old_curl_dependency": root / "private/dependencies/luanti-curl/14615",
        "gate57_runner": root / "artifacts/candidate_gate57_14615_fresh_execution",
    }
    present = {name: path.exists() for name, path in required.items()}
    if any(present.values()):
        raise RuntimeError(
            "#14615 current-v2 blocker inputs changed; re-audit exact chain"
        )

    payload = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-14615-curl-compatibility-v1",
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "current_v2_native_chain_not_registered",
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed_static_guard_audit",
            "fresh_gate57_epoch": "blocked_before_first_attempt",
        },
        "static_regression_binding": {
            "primary_issue_number": 14598,
            "production_paths": list(_PRODUCTION_PATHS),
            "preserved_test_paths": list(_TEST_PATHS),
            "compatibility_surface": "CURLUPART_ZONEID availability and HAVE_COLORIZE_URL guard",
            "required_dependency": "candidate-specific curl headers/libraries compatible with pre-7.65 curl",
            "existing_modern_curl_closure_not_reused": True,
        },
        "required_current_v2_chain": {
            name: {"registered": present[name], "probe": path.as_posix()}
            for name, path in required.items()
        },
        "missing_for_fresh_gate5_7": [
            "candidate_profile_and_selector_binding",
            "candidate_preparation_and_downstream_plan",
            "fixed_source_capsule_at_the_merged_fix_revision",
            "production_only_buggy_inverse_receipt",
            "candidate_specific_old_curl_dependency_closure",
            "fixed_buggy_client_build_receipts",
            "fresh_12_cell_gate57_runner_and_aggregate",
        ],
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
        "next_action": (
            "register the current-v2 fixed capsule and old-curl closure, then run an independent fresh 12-cell client epoch"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#14615 Gate 5--7 blocker conflicts with exact state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _BLOCKER)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#14615 Gate 5--7 blocker typed reload failed")
    return artifact


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    if any(
        artifact.payload.get("candidate") != _CANDIDATE
        for artifact in (source, patch, atomicity, manifest)
    ):
        raise RuntimeError("#14615 current-v2 chain candidate binding is invalid")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#14615 source packet is not complete")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("#14615 test patch is not eligible")
    if tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS:
        raise RuntimeError("#14615 production path inventory drifted")
    if tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS:
        raise RuntimeError("#14615 test path inventory drifted")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 14598
    ):
        raise RuntimeError("#14615 source atomicity is not accepted")
    details = manifest.payload.get("details")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != list(_TEST_PATHS)
        or details.get("test_count") != 1
    ):
        raise RuntimeError("#14615 test manifest is not passed")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path) -> StableArtifact | None:
    try:
        return _load(root, _BLOCKER)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_14615_gate57_blocker"]
