"""Exact offline Gate 5--7 pool blocker for five JupyterLab candidates.

This audit binds only candidate profiles, preparations, fixed capsules, and
the two dependency receipts those profiles name.  It does not install or run
Node, build packages, start a browser, or inherit prior attempts.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .jupyterlab_top_profiles import profile_for_top_candidate
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_NUMBERS = (2437, 2469, 1846, 3867, 4796)
_STATIC = {
    2437: "gate4_pending_execution",
    2469: "gate1_4_static_admitted",
    1846: "source_closure_pending_attachments",
    3867: "gate4_pending_execution",
    4796: "source_closure_pending_attachments",
}
_DEPENDENCY_22 = ArtifactReference(
    "dependency_preparation/jupyterlab-node5-karma1.7-webpack2-typescript2.2.1/current/v1",
    "dependency_preparation",
    "1.0",
)
_DEPENDENCY_3867 = ArtifactReference(
    "dependency_preparation/jupyterlab-node5-karma1.7.1-webpack2-typescript2.6.2/current/v1",
    "dependency_preparation",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_pool_gate57_blocker/jupyterlab/2437-2469-1846-3867-4796/current-v2/v1",
    "candidate_pool_gate57_blocker",
    "1.0",
)


def write_jupyterlab_gate57_pool_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist the exact no-runtime result without rejecting candidates."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("JupyterLab pool blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    artifacts: list[StableArtifact] = []
    candidates: dict[str, Any] = {}
    for number in _NUMBERS:
        profile = profile_for_top_candidate(CandidateKey("jupyterlab", number))
        profile_artifact = _load(root, profile.profile_reference)
        preparation = _load(root, profile.preparation_reference)
        capsule = _load(root, profile.capsule_reference)
        _validate_candidate_artifacts(number, profile_artifact, preparation, capsule)
        artifacts.extend((profile_artifact, preparation, capsule))
        runtime_present = _ordinary_directory_exists(root / PurePosixPath(profile.runtime_root))
        if runtime_present:
            raise RuntimeError(f"JupyterLab #{number} runtime appeared; re-audit required")
        candidates[str(number)] = {
            "candidate": {"repository_key": "jupyterlab", "change_number": number},
            "profile_id": profile.profile_id,
            "tier": profile.tier,
            "static_admission": _STATIC[number],
            "profile_reference": profile.profile_reference.artifact_id,
            "preparation_reference": profile.preparation_reference.artifact_id,
            "capsule_reference": profile.capsule_reference.artifact_id,
            "runtime_dependency_id": profile.runtime_dependency_id,
            "runtime_root": profile.runtime_root,
            "candidate_owned_runtime_present": False,
            "executable_now": False,
        }

    dependency_22 = _load(root, _DEPENDENCY_22)
    dependency_3867 = _load(root, _DEPENDENCY_3867)
    artifacts.extend((dependency_22, dependency_3867))
    expected_2469_root = candidates["2469"]["runtime_root"]
    if (
        dependency_22.payload.get("outcome") != "infrastructure_blocked"
        or dependency_22.payload.get("reason")
        != "historical_runtime_components_unavailable"
        or dependency_22.payload.get("runtime_root") != expected_2469_root
    ):
        raise RuntimeError("JupyterLab 2.2.1 dependency blocker drifted")
    if (
        dependency_3867.payload.get("outcome") != "pending"
        or dependency_3867.payload.get("offline_runtime_buildable") is not False
        or dependency_3867.payload.get("runtime_root") != candidates["3867"]["runtime_root"]
    ):
        raise RuntimeError("JupyterLab #3867 dependency blocker drifted")

    absent_dependency_ids = {
        "1846": (
            "dependency_preparation/"
            + str(candidates["1846"]["runtime_dependency_id"])
            + "/current/v1"
        ),
        "4796": (
            "dependency_preparation/"
            + str(candidates["4796"]["runtime_dependency_id"])
            + "/current/v1"
        ),
    }
    if any((root / "artifacts" / PurePosixPath(value)).exists() for value in absent_dependency_ids.values()):
        raise RuntimeError("JupyterLab pending-source dependency receipt appeared")

    candidates["2437"]["dependency_status"] = "wrong_candidate_runtime_root_and_blocked"
    candidates["2469"]["dependency_status"] = "infrastructure_blocked"
    candidates["1846"]["dependency_status"] = "unregistered"
    candidates["3867"]["dependency_status"] = "pending_offline_runtime_not_buildable"
    candidates["4796"]["dependency_status"] = "unregistered"
    payload: dict[str, Any] = {
        "state": "blocked",
        "outcome": "candidate_owned_offline_runtime_unavailable",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "host_node_substitution_allowed": False,
        "network_actions_authorized": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "candidates": candidates,
        "dependency_receipts": {
            "node5_typescript2.2.1": {
                "reference": _DEPENDENCY_22.artifact_id,
                "outcome": dependency_22.payload.get("outcome"),
                "runtime_root": dependency_22.payload.get("runtime_root"),
                "applies_to_candidate": 2469,
                "missing_components": dependency_22.payload.get("missing_components"),
            },
            "candidate_3867": {
                "reference": _DEPENDENCY_3867.artifact_id,
                "outcome": dependency_3867.payload.get("outcome"),
                "offline_runtime_buildable": False,
                "missing_components": dependency_3867.payload.get("missing_components"),
            },
            "absent_exact_namespaces": absent_dependency_ids,
        },
        "admitted_but_runtime_blocked": [2469],
        "not_yet_gate1_4_admitted": [2437, 1846, 3867, 4796],
        "next_action": "register_a_candidate_owned_offline_historical_runtime_then_reaudit_without_host_node",
        "complete": True,
    }
    inputs = tuple(artifact.reference for artifact in artifacts)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("JupyterLab pool blocker conflicts with current heads")
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
    blocker = _load(root, _BLOCKER)
    if blocker.inputs != inputs or dict(blocker.payload) != payload:
        raise RuntimeError("JupyterLab pool blocker typed reload failed")
    return blocker


def _validate_candidate_artifacts(
    number: int,
    profile: StableArtifact,
    preparation: StableArtifact,
    capsule: StableArtifact,
) -> None:
    candidate = {"repository_key": "jupyterlab", "change_number": number}
    if any(a.payload.get("candidate") != candidate for a in (profile, preparation, capsule)):
        raise RuntimeError(f"JupyterLab #{number} artifact binding drifted")
    gates = profile.payload.get("strict_gate_status")
    if type(gates) is not dict:
        raise RuntimeError(f"JupyterLab #{number} gate status is missing")
    expected = _STATIC[number]
    if expected == "gate1_4_static_admitted":
        if any(
            gates.get(key) != value
            for key, value in {
                "source_closure": "passed",
                "single_defect_mapping": "passed",
                "production_inverse_static": "passed",
                "fixed_test_api_compatibility": "passed_static",
            }.items()
        ):
            raise RuntimeError(f"JupyterLab #{number} static admission drifted")
    elif expected == "gate4_pending_execution":
        if gates.get("source_closure") != "passed" or gates.get("fixed_test_api_compatibility") != "pending_execution":
            raise RuntimeError(f"JupyterLab #{number} Gate 4 pending state drifted")
    elif gates.get("source_closure") != "pending_attachment_closure":
        raise RuntimeError(f"JupyterLab #{number} source closure state drifted")


def _ordinary_directory_exists(path: Path) -> bool:
    if not path.exists():
        return False
    return path.is_dir() and not path.is_symlink()


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
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_jupyterlab_gate57_pool_blocker"]
