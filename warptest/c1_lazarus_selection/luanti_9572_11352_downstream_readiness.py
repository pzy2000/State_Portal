"""Read-only Gate 8--13 readiness comparison for Luanti #9572/#11352."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_REFS = {
    "9572_aggregate": ArtifactReference(
        "candidate_gate57_9572_strict_aggregate/luanti/9572/lua/v4",
        "candidate_gate57_9572_strict_aggregate",
        "1.0",
    ),
    "9572_transition": ArtifactReference(
        "candidate_gate57_9572_strict_transition/luanti/9572/lua/v4",
        "candidate_gate57_9572_strict_transition",
        "1.0",
    ),
    "9572_blocker": ArtifactReference(
        "candidate_gate8_9572_downstream_blocker/luanti/9572/current-v2",
        "candidate_gate8_9572_downstream_blocker",
        "1.0",
    ),
    "11352_materialization": ArtifactReference(
        "candidate_gate57_fresh_materialization/luanti/11352/cpp17/v6",
        "candidate_gate57_fresh_materialization",
        "1.0",
    ),
    "11352_fixed_build": ArtifactReference(
        "candidate_gate57_fresh_build/luanti/11352/cpp17/fixed/build/v6",
        "candidate_gate57_fresh_build",
        "1.0",
    ),
    "11352_buggy_build": ArtifactReference(
        "candidate_gate57_fresh_build/luanti/11352/cpp17/buggy/build/v6",
        "candidate_gate57_fresh_build",
        "1.0",
    ),
    "11352_aggregate": ArtifactReference(
        "candidate_gate57_fresh_aggregate/luanti/11352/cpp17/v6",
        "candidate_gate57_fresh_aggregate",
        "1.0",
    ),
}
_OUTPUT = ArtifactReference(
    "candidate_downstream_readiness_comparison/luanti/9572-11352/current-v2/v1",
    "candidate_downstream_readiness_comparison",
    "1.0",
)
_PAIR = PurePosixPath("private/fresh-gate57/luanti/11352/cpp17-v6")
_MAPPING_ROOT = PurePosixPath("artifacts/lazarus_task_mapping/luanti/11352")


def write_luanti_9572_11352_downstream_readiness(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Select #11352 and persist its exact resumable downstream blocker."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#9572/#11352 readiness authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    loaded = {name: _load(root, ref) for name, ref in _REFS.items()}
    _validate_receipts(loaded)

    fixed_server = _ordinary_file(root / _PAIR / "fixed/bin/minetestserver", root)
    buggy_server = _ordinary_file(root / _PAIR / "buggy/bin/minetestserver", root)
    if (root / _MAPPING_ROOT).exists():
        raise RuntimeError("#11352 task mapping appeared; downstream readiness must be re-audited")
    client_candidates = (
        root / _PAIR / "fixed/bin/minetest",
        root / _PAIR / "fixed/bin/luanti",
        root / _PAIR / "buggy/bin/minetest",
        root / _PAIR / "buggy/bin/luanti",
    )
    if any(path.exists() or path.is_symlink() for path in client_candidates):
        raise RuntimeError("#11352 client appeared; downstream readiness must be re-audited")

    payload = {
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "selected_candidate": {"repository_key": "luanti", "change_number": 11352},
        "selection_reason": "candidate_owned_fixed_buggy_pair_and_two_successful_native_server_builds",
        "comparison": {
            "9572": {
                "gate_1_7": "qualified_strict_12_of_12",
                "candidate_owned_application_pair": False,
                "native_build": False,
                "registered_task_mapping": False,
                "readiness": "lua_capsule_only",
            },
            "11352": {
                "gate_1_7": "qualified_strict_12_of_12",
                "candidate_owned_application_pair": True,
                "fixed_server": fixed_server.relative_to(root).as_posix(),
                "buggy_server": buggy_server.relative_to(root).as_posix(),
                "fixed_server_build": "passed",
                "buggy_server_build": "passed",
                "registered_task_mapping": False,
                "candidate_owned_clients": False,
                "readiness": "selected_needs_mapping_replay_and_real_client_visuals",
            },
        },
        "completed": {
            "gate8_fixed_buggy_materialization": True,
            "gate8_fixed_buggy_native_server_build": True,
        },
        "pending": {
            "registered_task_mapping": True,
            "gate9_registered_start": True,
            "gate10_ordered_replay_hidden_oracle_semantic_observations": True,
            "gate11_raw_common_before_fixed_after_buggy_after": True,
            "gate12_before_equivalence_and_after_2x2_visual_cross_judgment": True,
            "gate13_same_lineage_construction_receipt": True,
            "formal_freeze_admission": True,
        },
        "does_not_invalidate_gate_1_7": True,
        "no_new_build_started": True,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "other_candidate_assets_reused": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "register a candidate-specific #11352 task mapping, then run bounded server Start/replay/oracle; build clients only when the native lane is free for required real frames",
        "complete": True,
    }
    inputs = tuple(ref for ref in _REFS.values())
    existing = _try_load(root, _OUTPUT)
    if existing is None:
        write_stable_artifact(
            root,
            artifact_id=_OUTPUT.artifact_id,
            artifact_kind=_OUTPUT.artifact_kind,
            semantic_version=_OUTPUT.semantic_version,
            created_at=_now(),
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
    elif existing.inputs != inputs or dict(existing.payload) != payload:
        raise RuntimeError("#9572/#11352 readiness comparison conflicts")
    result = _load(root, _OUTPUT)
    if result.inputs != inputs or dict(result.payload) != payload:
        raise RuntimeError("#9572/#11352 readiness typed reload failed")
    return result


def _validate_receipts(loaded: dict[str, StableArtifact]) -> None:
    if (
        loaded["9572_aggregate"].payload.get("qualified") is not True
        or loaded["9572_aggregate"].payload.get("attempt_count") != 12
        or loaded["9572_transition"].payload.get("state") != "passed"
        or loaded["9572_blocker"].payload.get("terminal") is not False
        or loaded["11352_materialization"].payload.get("state") != "materialized"
        or loaded["11352_fixed_build"].payload.get("state") != "built"
        or loaded["11352_fixed_build"].payload.get("returncode") != 0
        or loaded["11352_buggy_build"].payload.get("state") != "built"
        or loaded["11352_buggy_build"].payload.get("returncode") != 0
        or loaded["11352_aggregate"].payload.get("qualified") is not True
        or loaded["11352_aggregate"].payload.get("attempt_count") != 12
    ):
        raise RuntimeError("#9572/#11352 typed readiness chain drifted")


def _ordinary_file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError("readiness file is unsafe")
    return resolved


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
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_9572_11352_downstream_readiness"]
