"""Typed, resumable Gate 5--7 blocker for Luanti #11866.

The current-v2 Gate 3/4 audit and the candidate-specific fixed capsule are
complete.  A fresh native attempt was then started with the registered
server-only toolchain.  CMake stopped before the first test because the
Luanti tree requires Irrlicht/IrrlichtMt headers even for this server build.
This receipt binds that exact configure failure and leaves the candidate
resumable only after a current dependency closure is registered.  It does not
inherit attempts from another candidate and does not authorize paid/formal
trials.
"""

from __future__ import annotations

import base64
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 11866}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/11866/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/11866/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/11866/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/11866/test_manifest/v2", "candidate_stage", "2.0"
)
_STATIC_AUDIT = ArtifactReference(
    "candidate_gate34_static_audit/luanti/11866/current-v2/v1",
    "candidate_gate34_static_audit",
    "1.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/11866/fixed/v2", "fixed_source_capsule", "2.0"
)
_BUILD = ArtifactReference(
    "native_regression_build/luanti/11866/fixed/configure/v3",
    "native_regression_build",
    "3.0",
)
_CHAIN = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _STATIC_AUDIT, _CAPSULE, _BUILD)
_BLOCKER = ArtifactReference(
    "candidate_gate57_11866_current_execution_blocker/luanti/11866/native/v1",
    "candidate_gate57_11866_current_execution_blocker",
    "1.0",
)


def write_luanti_11866_gate57_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist and reload the exact first native infrastructure failure."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#11866 Gate 5--7 blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(_load(root, reference) for reference in _CHAIN)
    _validate_chain(chain)
    _assert_no_attempts(root)

    static_audit = chain[4]
    build = chain[6]
    stderr = _decode_output(build.payload.get("stderr_base64"))
    stdout = _decode_output(build.payload.get("stdout_base64"))
    payload = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-11866-mapgen-limit-v1-native-gate57",
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "fixed_configure_missing_irrlicht_headers",
        "candidate_execution_performed": False,
        "candidate_materialization_performed": True,
        "new_build_started": True,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "current_gate_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "passed",
            "gate_3_strict_inverse_patch": "passed_static_production_only",
            "gate_4_fixed_test_api_compatibility": "passed_static_buggy_parent",
            "gate_5_fixed_f2p_3x": "blocked_infrastructure_before_first_attempt",
            "gate_6_buggy_f2p_3x_failure": "not_run",
            "gate_7_p2p_3x_both_pass": "not_run",
            "gate_8_13": "not_claimed",
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
        "configure_failure": {
            "variant": "fixed",
            "step": "configure",
            "process_outcome": build.payload["process_outcome"],
            "returncode": build.payload["returncode"],
            "build_receipt": _BUILD.artifact_id,
            "stderr_contains": "Irrlicht or IrrlichtMt headers are required to build the server, but none found.",
            "stderr_excerpt": stderr[-1200:],
            "stdout_excerpt": stdout[-1200:],
            "output_encoding": "utf-8",
        },
        "dependency_closure": {
            "registered_toolchain": "luanti-native/3.0",
            "registered_toolchain_used": True,
            "irrlichtmt_registered": False,
            "required_for_server_configure": True,
            "required_next": "register candidate-compatible IrrlichtMt headers, then rerun a fresh epoch",
        },
        "static_gate34_reference": {
            "artifact": _STATIC_AUDIT.artifact_id,
            "authority_primary_issue": static_audit.payload["authority"][
                "primary_issue_number"
            ],
            "authority_authoritative_issues": static_audit.payload["authority"][
                "authoritative_issue_numbers"
            ],
        },
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
        "next_action": "register the exact current-v2 IrrlichtMt dependency closure and rerun one fresh 12-cell Gate 5--7 epoch without old-attempt inheritance",
    }
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != _CHAIN or dict(existing.payload) != payload:
            raise RuntimeError("#11866 Gate 5--7 blocker conflicts with exact state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
        created_at=_now(),
        inputs=_CHAIN,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _BLOCKER)
    if artifact.inputs != _CHAIN or dict(artifact.payload) != payload:
        raise RuntimeError("#11866 Gate 5--7 blocker typed reload failed")
    return artifact


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


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain):
        raise RuntimeError("#11866 current-v2 chain candidate binding is invalid")
    source, patch, atomicity, manifest, static_audit, capsule, build = chain
    if source.payload.get("complete") is not True:
        raise RuntimeError("#11866 source packet is not complete")
    if patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#11866 test patch is not eligible")
    if atomicity.payload.get("outcome") != "accepted":
        raise RuntimeError("#11866 source atomicity receipt is not accepted")
    if manifest.payload.get("state") != "passed":
        raise RuntimeError("#11866 test manifest is not passed")
    if static_audit.payload.get("outcome") != "gate3_4_static_passed_pending_gate57":
        raise RuntimeError("#11866 Gate 3/4 audit is not the expected current-v2 pass")
    authority = static_audit.payload.get("authority")
    if (
        not isinstance(authority, dict)
        or authority.get("primary_issue_number") != 11839
    ):
        raise RuntimeError("#11866 current authority mapping is not bound to #11839")
    if (
        capsule.payload.get("profile_id")
        != "luanti-11866-mapgen-limit-v1-native-gate57"
    ):
        raise RuntimeError("#11866 fixed capsule profile is invalid")
    if (
        build.payload.get("step") != "configure"
        or build.payload.get("variant") != "fixed"
    ):
        raise RuntimeError("#11866 configure failure receipt is invalid")
    if (
        build.payload.get("process_outcome") != "completed"
        or build.payload.get("returncode") != 1
    ):
        raise RuntimeError(
            "#11866 configure receipt is not the observed infrastructure failure"
        )
    stderr = _decode_output(build.payload.get("stderr_base64"))
    if "Irrlicht or IrrlichtMt headers are required" not in stderr:
        raise RuntimeError(
            "#11866 configure receipt does not prove missing Irrlicht headers"
        )


def _assert_no_attempts(root: Path) -> None:
    for relative in (
        "artifacts/native_regression_attempt/luanti/11866",
        "artifacts/native_regression_aggregate/luanti/11866",
    ):
        if (root / relative).exists():
            raise RuntimeError(
                "#11866 fresh blocker cannot mask existing native attempts"
            )


def _decode_output(value: object) -> str:
    if not isinstance(value, str):
        raise RuntimeError("#11866 configure output is not encoded")
    try:
        return base64.b64decode(value, validate=True).decode("utf-8", errors="replace")
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("#11866 configure output is invalid") from error


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_11866_gate57_blocker"]
