"""Typed static plan for a fresh non-null Luanti #15519 capture path.

The existing non-null audit proves that both persistent client binaries were
compiled with the OpenGL ``glReadPixels`` path and the built-in F12 screenshot
path.  It does *not* register a runner or create frames.  This receipt binds
that static capability to the current-v4 semantic lineage and records the
safe hand-off to a future disposable capture run.  No process is launched and
the persistent pair and protected PIDs remain untouched.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 15519}
_V1 = "1.0"
_V2 = "2.0"
_V4 = "4.0"
_V5 = "5.0"
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/15519/fixed-buggy/v1",
    "candidate_materialization",
    _V1,
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/15519/fixed/v2", "fixed_source_capsule", _V2
)
_CLIENT_CLOSURE = ArtifactReference(
    "client_dependency_closure/luanti/15519/darwin-arm64/v2",
    "client_dependency_closure",
    _V2,
)
_TASK_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/15519/issue-15518/v2",
    "lazarus_task_mapping",
    _V2,
)
_NONNULL_AUDIT = ArtifactReference(
    "candidate_nonnull_visual_static_audit/luanti/15519/v2",
    "candidate_nonnull_visual_static_audit",
    _V2,
)
_OBSERVATION = ArtifactReference(
    "candidate_headless_server_observation/luanti/15519/fixed-buggy/v4",
    "candidate_headless_server_observation",
    _V4,
)
_REPLAY = ArtifactReference(
    "candidate_headless_server_replay/luanti/15519/v4",
    "candidate_headless_server_replay",
    _V4,
)
_ORACLE = ArtifactReference(
    "candidate_headless_server_oracle/luanti/15519/v4",
    "candidate_headless_server_oracle",
    _V4,
)
_CONSTRUCTION = ArtifactReference(
    "candidate_headless_server_construction/luanti/15519/v4",
    "candidate_headless_server_construction",
    _V4,
)
_SEMANTIC = ArtifactReference(
    "candidate_headless_semantic_golden/luanti/15519/v4",
    "candidate_headless_semantic_golden",
    _V4,
)
_VISUAL_PLAN = ArtifactReference(
    "candidate_headless_visual_capture_plan/luanti/15519/v5",
    "candidate_headless_visual_capture_plan",
    _V5,
)
_LINEAGE = ArtifactReference(
    "candidate_headless_construction_lineage/luanti/15519/v4",
    "candidate_headless_construction_lineage",
    _V4,
)
_PATH_PLAN = ArtifactReference(
    "candidate_nonnull_capture_path_plan/luanti/15519/v1",
    "candidate_nonnull_capture_path_plan",
    _V1,
)
_PROTECTED_PIDS = (76485, 89366, 95665, 99762)


def write_luanti_15519_nonnull_capture_path_plan(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Bind the static OpenGL/F12 path without starting a capture run."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    artifacts = {
        "materialization": _load(root, _MATERIALIZATION),
        "capsule": _load(root, _CAPSULE),
        "client_closure": _load(root, _CLIENT_CLOSURE),
        "task_mapping": _load(root, _TASK_MAPPING),
        "nonnull_audit": _load(root, _NONNULL_AUDIT),
        "observation": _load(root, _OBSERVATION),
        "replay": _load(root, _REPLAY),
        "oracle": _load(root, _ORACLE),
        "construction": _load(root, _CONSTRUCTION),
        "semantic": _load(root, _SEMANTIC),
        "visual_plan": _load(root, _VISUAL_PLAN),
        "lineage": _load(root, _LINEAGE),
    }
    path_proof = _validate_inputs(root, artifacts)
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "downstream_pending",
        "outcome": "candidate_specific_non_null_path_identified_runner_unregistered",
        "terminal": False,
        "resumable": True,
        "complete": True,
        "candidate_specific_path": {
            "backend": "luanti_builtin_f12_opengl_glReadPixels",
            "fixed_tree_path": artifacts["materialization"].payload["fixed_tree_path"],
            "fixed_binary_path": artifacts["materialization"].payload["fixed_binary_path"],
            "buggy_tree_path": artifacts["materialization"].payload["buggy_tree_path"],
            "buggy_binary_path": artifacts["materialization"].payload["buggy_binary_path"],
            "static_proof": path_proof,
            "runner_registered": False,
            "frame_receipts_present": False,
        },
        "current_semantic_lineage": {
            "observation": _OBSERVATION.artifact_id,
            "replay": _REPLAY.artifact_id,
            "oracle": _ORACLE.artifact_id,
            "fixed_after_real": True,
            "buggy_after_real": True,
            "common_before_real": False,
            "construction_state": artifacts["lineage"].payload.get(
                "construction_lineage_state"
            ),
        },
        "legality": {
            "safe_only_in_fresh_disposable_copy": True,
            "persistent_pair_mutation_allowed": False,
            "old_pair_rerun": False,
            "old_attempts_loaded": False,
            "gate_1_7_mutation": False,
            "protected_pids": list(_PROTECTED_PIDS),
            "protected_pids_touched": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "bounded_run_authorization_present": False,
        },
        "capture_contract": {
            "common_before": "capture after world-start marker before defect observation",
            "fixed_after": "capture after identical ordered replay and translation load",
            "buggy_after": "capture after identical ordered replay and translation load",
            "before_visual_equivalence": "compare the two common-before frames",
            "after_2x2_cross_matrix": "judge fixed/buggy after frames against both goldens",
            "construction_lineage": "link all frame receipts to v4 oracle and current materialization",
        },
        "remaining_evidence": [
            "registered_non_null_client_capture_backend",
            "registered_client_start_and_ordered_replay_adapter",
            "common_before_semantic_observation",
            "common_before_visual_frame",
            "fixed_after_visual_frame",
            "buggy_after_visual_frame",
            "pairwise_before_visual_equivalence",
            "two_by_two_after_visual_cross_matrix",
            "same_lineage_construction_receipt",
        ],
        "dependency_caveat": {
            "closure_reference": _CLIENT_CLOSURE.artifact_id,
            "state": artifacts["client_closure"].payload.get("state"),
            "install_name_rewrites_deferred": artifacts["client_closure"].payload.get(
                "install_name_rewrites_deferred_to_client_packaging"
            ),
            "non_hermetic_absolute_dependencies_present": any(
                bool(item.get("non_hermetic_absolute_dependencies"))
                for item in path_proof.values()
                if isinstance(item, dict)
            ),
            "must_be_resolved_in_disposable_runner": True,
        },
        "next_action": (
            "obtain explicit bounded downstream authorization, copy both materialized "
            "trees into a new isolated run root, register the OpenGL/F12 Start and "
            "ordered replay adapter, then capture real common-before and fixed/buggy "
            "after frames; do not touch protected PIDs or the persistent pair"
        ),
    }
    inputs = tuple(
        artifacts[name].reference
        for name in (
            "materialization",
            "capsule",
            "client_closure",
            "task_mapping",
            "nonnull_audit",
            "observation",
            "replay",
            "oracle",
            "construction",
            "semantic",
            "visual_plan",
            "lineage",
        )
    )
    existing = _try_load(root, _PATH_PLAN)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("15519 non-null path plan conflicts with current state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_PATH_PLAN.artifact_id,
        artifact_kind=_PATH_PLAN.artifact_kind,
        semantic_version=_PATH_PLAN.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _PATH_PLAN)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("15519 non-null path plan typed reload failed")
    return artifact


def _validate_inputs(root: Path, artifacts: dict[str, StableArtifact]) -> dict[str, dict[str, Any]]:
    for name, artifact in artifacts.items():
        if artifact.payload.get("candidate") != _CANDIDATE:
            raise RuntimeError(f"15519 {name} is not candidate-bound")
    materialization = artifacts["materialization"].payload
    if materialization.get("state") != "built" or materialization.get("complete") is not True:
        raise RuntimeError("15519 persistent materialization is not built")
    for tree_key, binary_key in (("fixed_tree_path", "fixed_binary_path"), ("buggy_tree_path", "buggy_binary_path")):
        tree = root / PurePosixPath(str(materialization[tree_key]))
        binary = root / PurePosixPath(str(materialization[binary_key]))
        if not _ordinary_directory(tree) or not _ordinary_file(binary):
            raise RuntimeError(f"15519 materialized {tree_key} or {binary_key} is unsafe")
    audit = artifacts["nonnull_audit"].payload
    if not (
        audit.get("state") == "downstream_pending"
        and audit.get("persistent_tree_modified") is False
        and audit.get("production_test_bytes_changed") is False
        and audit.get("protected_pids") == list(_PROTECTED_PIDS)
        and audit.get("capture", {}).get("compiled_backend_available") is True
        and audit.get("capture", {}).get("builtin_f12_screenshot_path") is True
        and audit.get("capture", {}).get("registered_non_null_backend") is False
        and audit.get("capture", {}).get("frames_written") is False
    ):
        raise RuntimeError("15519 non-null static audit is not the expected pending state")
    variants = audit.get("variants")
    if type(variants) is not dict or set(variants) != {"fixed", "buggy"}:
        raise RuntimeError("15519 non-null variant proof is incomplete")
    for variant in variants.values():
        if not (
            variant.get("opengl_screenshot_readback") is True
            and variant.get("builtin_screenshot_path") is True
            and variant.get("null_driver_returns_null") is True
        ):
            raise RuntimeError("15519 OpenGL/F12 static proof changed")
    if artifacts["client_closure"].payload.get("state") != "materialized":
        raise RuntimeError("15519 client dependency closure is not materialized")
    replay = artifacts["replay"].payload
    observation = artifacts["observation"].payload
    oracle = artifacts["oracle"].payload
    if not (
        observation.get("state") == "passed"
        and replay.get("state") == "passed"
        and replay.get("registered_start") is True
        and replay.get("ordered_replay") is True
        and replay.get("hidden_oracle") is True
        and replay.get("real_semantic_observations") is True
        and oracle.get("state") == "passed"
    ):
        raise RuntimeError("15519 v4 semantic/replay/oracle closure is not passed")
    semantic = artifacts["semantic"].payload
    lineage = artifacts["lineage"].payload
    visual_plan = artifacts["visual_plan"].payload
    if not (
        semantic.get("state") == "downstream_pending"
        and semantic.get("common_before", {}).get("state") == "missing"
        and semantic.get("fixed_after", {}).get("synthetic") is False
        and semantic.get("buggy_after", {}).get("synthetic") is False
        and lineage.get("construction_lineage_state") == "pending_visual_and_common_before"
        and visual_plan.get("state") == "downstream_pending"
        and visual_plan.get("old_attempts_loaded") is False
        and visual_plan.get("do_not_touch_pids") == list(_PROTECTED_PIDS)
    ):
        raise RuntimeError("15519 downstream visual/lineage plan drifted")
    return {
        variant: {
            "opengl_screenshot_readback": variants[variant]["opengl_screenshot_readback"],
            "builtin_screenshot_path": variants[variant]["builtin_screenshot_path"],
            "binary_fingerprint": variants[variant]["binary_fingerprint"],
            "tree_path": variants[variant]["tree_path"],
            "binary_path": variants[variant]["binary_path"],
            "non_hermetic_absolute_dependencies": variants[variant].get(
                "non_hermetic_absolute_dependencies", []
            ),
        }
        for variant in ("fixed", "buggy")
    }


def _ordinary_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _ordinary_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("15519 non-null path-plan authorization is inactive")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_15519_nonnull_capture_path_plan"]
