"""Typed blocker for the bounded #11866 C++17 build failure before tests."""

from __future__ import annotations

import base64
import os
import re
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
_STATIC = ArtifactReference(
    "candidate_gate34_static_audit/luanti/11866/current-v2/v1",
    "candidate_gate34_static_audit",
    "1.0",
)
_COMPATIBILITY = ArtifactReference(
    "candidate_gate57_11866_irrlicht_compatibility/luanti/11866/native/v1",
    "candidate_gate57_11866_irrlicht_compatibility",
    "1.0",
)
_DEPENDENCY = ArtifactReference(
    "candidate_gate57_11866_irrlicht_dependency/luanti/11866/native/v1",
    "candidate_gate57_11866_irrlicht_dependency",
    "1.0",
)
_OVERRIDE = ArtifactReference(
    "candidate_gate57_11866_cxx17_override/luanti/11866/native/v1",
    "candidate_gate57_11866_cxx17_override",
    "1.0",
)
_CONFIGURE = ArtifactReference(
    "native_regression_build/luanti/11866/fixed/configure/irrlicht-v3",
    "native_regression_build",
    "3.0",
)
_BUILD = ArtifactReference(
    "native_regression_build/luanti/11866/fixed/build/irrlicht-v3",
    "native_regression_build",
    "3.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_11866_cxx17_build_blocker/luanti/11866/native/v3",
    "candidate_gate57_11866_cxx17_build_blocker",
    "1.0",
)
_CHAIN = (_STATIC, _COMPATIBILITY, _DEPENDENCY, _OVERRIDE, _CONFIGURE, _BUILD)


def write_luanti_11866_cxx17_build_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist and reload the exact bounded C++17 build stop."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#11866 C++17 build blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(_load(root, reference) for reference in _CHAIN)
    _validate_chain(chain)
    _assert_no_attempts(root)
    configure, build = chain[4], chain[5]
    stdout = _decode(build.payload["stdout_base64"])
    stderr = _decode(build.payload["stderr_base64"])
    progress = re.findall(r"\[(\d+)/(\d+)\]", stdout)
    payload = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-11866-mapgen-limit-v1-native-gate57",
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "cxx17_native_build_failed_before_tests",
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
            "gate_5_fixed_f2p_3x": "blocked_build_before_first_attempt",
            "gate_6_buggy_f2p_3x_failure": "not_run",
            "gate_7_p2p_3x_both_pass": "not_run",
            "gate_8_13": "not_claimed",
        },
        "fresh_epoch": {
            "namespace": "irrlicht-v3",
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
        },
        "build_failure": {
            "configure_receipt": _CONFIGURE.artifact_id,
            "build_receipt": _BUILD.artifact_id,
            "configure_returncode": configure.payload["returncode"],
            "build_returncode": build.payload["returncode"],
            "build_target": "minetestserver",
            "cxx17_override": _OVERRIDE.artifact_id,
            "process_outcome": build.payload["process_outcome"],
            "stdout_truncated": build.payload["stdout_truncated"],
            "stderr_truncated": build.payload["stderr_truncated"],
            "observed_progress": list(progress[-1]) if progress else None,
            "diagnostic_status": "bounded_output_truncated_before_failure_diagnostic",
            "stdout_excerpt": stdout[-2500:],
            "stderr_excerpt": stderr[-2500:],
            "output_encoding": "utf-8",
        },
        "source_preservation": {
            "production_source_bytes_modified": False,
            "test_source_bytes_modified": False,
            "dependency_source_bytes_modified": False,
        },
        "next_action": "retain blocker; do not reuse irrlicht-v1/irrlicht-v2/irrlicht-v3 build refs; only a separately authorized diagnostic epoch may inspect the truncated compiler failure",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    try:
        existing = _load(root, _BLOCKER)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
        existing = None
    if existing is not None:
        if existing.inputs != _CHAIN or dict(existing.payload) != payload:
            raise RuntimeError("#11866 C++17 build blocker conflicts with exact state")
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
    return _load(root, _BLOCKER)


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _validate_chain(chain: tuple[StableArtifact, ...]) -> None:
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain):
        raise RuntimeError("#11866 C++17 build blocker chain candidate binding drifted")
    static, compatibility, dependency, override, configure, build = chain
    if static.payload.get("outcome") != "gate3_4_static_passed_pending_gate57":
        raise RuntimeError("#11866 Gate 3/4 receipt is not current")
    if compatibility.payload.get("outcome") != "irrlichtmt_headers_compatible":
        raise RuntimeError("#11866 Irrlicht compatibility receipt is not current")
    if dependency.payload.get("complete") is not True:
        raise RuntimeError("#11866 Irrlicht dependency receipt is incomplete")
    if override.payload.get("outcome") != "source_preserving_cxx17_override":
        raise RuntimeError("#11866 C++17 override receipt is not current")
    if (
        configure.payload.get("step") != "configure"
        or configure.payload.get("variant") != "fixed"
        or configure.payload.get("returncode") != 0
    ):
        raise RuntimeError("#11866 v3 configure receipt is not successful")
    if (
        build.payload.get("step") != "build"
        or build.payload.get("variant") != "fixed"
        or build.payload.get("returncode") != 1
    ):
        raise RuntimeError("#11866 v3 build receipt is not the observed failure")
    if build.payload.get("stdout_truncated") is not True:
        raise RuntimeError("#11866 v3 build receipt is not the bounded-output stop")


def _assert_no_attempts(root: Path) -> None:
    for relative in (
        "artifacts/native_regression_attempt/luanti/11866",
        "artifacts/native_regression_aggregate/luanti/11866/fixed-buggy/irrlicht-v3",
    ):
        if (root / relative).exists():
            raise RuntimeError("#11866 C++17 blocker cannot mask native attempts")


def _decode(value: object) -> str:
    if not isinstance(value, str):
        raise RuntimeError("#11866 C++17 build output encoding is invalid")
    try:
        return base64.b64decode(value, validate=True).decode("utf-8", errors="replace")
    except (ValueError, UnicodeError) as error:
        raise RuntimeError("#11866 C++17 build output is invalid") from error


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_11866_cxx17_build_blocker"]
