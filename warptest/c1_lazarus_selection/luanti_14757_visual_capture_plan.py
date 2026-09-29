"""Read-only Gate 11--13 capture plan for Luanti #14757.

The current-v2 repackage-v3 pair has a real OpenGL client and the ordinary
Luanti F12 screenshot path, but it has no WarpTest capture hook.  The previous
GUI attempt also recorded ``NativeInputUnavailableError``.  This module binds
the current typed Gate 9/10 receipts, inspects the immutable pair/source, and
writes a non-terminal plan.  It never starts an app, posts input, copies a
bundle, signs a bundle, or touches a protected PID.

The resulting receipt is intentionally conservative: compiled OpenGL/F12
support is evidence of feasibility, not evidence of a framebuffer.  A later
run may be authorised only after the host native-input preflight succeeds and
the run is confined to a fresh disposable namespace.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 14757}
_V1 = "1.0"
_PROTECTED_PIDS = (20946, 21627, 30865, 70361, 60839)
_FORMAL_ROOT = PurePosixPath(
    "private/luanti-14757-formal-materialization"
)

_GATE_AGGREGATE = ArtifactReference(
    "lua_regression_aggregate/luanti/14757/fixed-buggy/v3",
    "lua_regression_aggregate",
    "3.0",
)
_GATE_TRANSITION = ArtifactReference(
    "luanti_lua_regression_transition/luanti/14757/test-repetitions/v2",
    "luanti_lua_regression_transition",
    "2.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/14757/lz-luanti-14757-math-round/v4",
    "lazarus_task_mapping",
    "4.0",
)
_REPACKAGE = ArtifactReference(
    "candidate_downstream_repackage_receipt/luanti/14757/repackage-v3/v1",
    "candidate_downstream_repackage_receipt",
    _V1,
)
_GATE9_REPLAY = ArtifactReference(
    "candidate_gate9_server_side_replay/luanti/14757/repackage-v3/v1",
    "candidate_gate9_server_side_replay",
    _V1,
)
_GATE10_OBSERVATION = ArtifactReference(
    "candidate_gate10_server_side_observation/luanti/14757/repackage-v3/v1",
    "candidate_gate10_server_side_observation",
    _V1,
)
_GATE10_ORACLE = ArtifactReference(
    "candidate_gate10_server_side_oracle/luanti/14757/repackage-v3/v1",
    "candidate_gate10_server_side_oracle",
    _V1,
)
_GUI_BLOCKER = ArtifactReference(
    "candidate_gate9_current_v2_blocker/luanti/14757/repackage-v3/v1",
    "candidate_gate9_current_v2_blocker",
    _V1,
)
_SOURCE_PACKET = ArtifactReference(
    "candidate_source_packet/luanti/14757/source/v2",
    "candidate_source_packet",
    "2.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/14757/fixed/v2",
    "fixed_source_capsule",
    "2.0",
)
_PLAN = ArtifactReference(
    "candidate_gate11_visual_capture_plan/luanti/14757/repackage-v3/v1",
    "candidate_gate11_visual_capture_plan",
    _V1,
)
_BLOCKER = ArtifactReference(
    "candidate_gate11_visual_capture_blocker/luanti/14757/repackage-v3/v1",
    "candidate_gate11_visual_capture_blocker",
    _V1,
)


def write_luanti_14757_visual_capture_plan(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write a typed, non-terminal, launch-free visual capture blocker."""

    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    refs = (
        _GATE_AGGREGATE,
        _GATE_TRANSITION,
        _MAPPING,
        _REPACKAGE,
        _GATE9_REPLAY,
        _GATE10_OBSERVATION,
        _GATE10_ORACLE,
        _GUI_BLOCKER,
        _SOURCE_PACKET,
        _CAPSULE,
    )
    loaded = {reference.artifact_id: _load(root, reference) for reference in refs}
    _validate_receipts(loaded)
    repackage = loaded[_REPACKAGE.artifact_id]
    variants = {
        variant: _inspect_variant(
            root,
            root / PurePosixPath(
                repackage.payload[f"binding_application_{variant}_path"]
            ),
            root / _FORMAL_ROOT / variant,
        )
        for variant in ("fixed", "buggy")
    }

    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked",
        "reason": "no_registered_non_null_client_capture_runner",
        "terminal": False,
        "resumable": True,
        "complete": True,
        "candidate_execution_performed": False,
        "client_launch_performed": False,
        "gui_launch_performed": False,
        "persistent_tree_modified": False,
        "production_test_bytes_changed": False,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "protected_pids": list(_PROTECTED_PIDS),
        "lineage": {
            "gate_1_7_aggregate": _GATE_AGGREGATE.artifact_id,
            "gate_1_7_transition": _GATE_TRANSITION.artifact_id,
            "mapping": _MAPPING.artifact_id,
            "repackage": _REPACKAGE.artifact_id,
            "gate9_replay": _GATE9_REPLAY.artifact_id,
            "gate10_observation": _GATE10_OBSERVATION.artifact_id,
            "gate10_oracle": _GATE10_ORACLE.artifact_id,
            "source_packet": _SOURCE_PACKET.artifact_id,
            "fixed_capsule": _CAPSULE.artifact_id,
        },
        "variants": variants,
        "capture": {
            "compiled_backend": "irrlicht_opengl_createScreenShot_glReadPixels",
            "compiled_backend_available": all(
                item["opengl_screenshot_readback"] for item in variants.values()
            ),
            "builtin_f12_screenshot_path": all(
                item["builtin_f12_screenshot_path"] for item in variants.values()
            ),
            "persistent_warptest_hook_present": any(
                item["persistent_warptest_hook_present"] for item in variants.values()
            ),
            "registered_non_null_backend": False,
            "registered_start_replay_adapter": False,
            "framebuffer_readback_registered": False,
            "native_input_preflight": "required_before_authorization",
            "candidate_execution_performed": False,
            "common_before_available": False,
            "fixed_after_available": False,
            "buggy_after_available": False,
            "frames_written": False,
            "synthetic_frames_written": False,
            "before_visual_equivalence": "pending",
            "after_2x2_cross_matrix": "pending",
        },
        "minimum_missing_evidence": [
            "registered_non_null_client_capture_backend",
            "unlocked_native_input_and_event_post_preflight",
            "fresh_disposable_fixed_and_buggy_client_start",
            "common_before_visual_frame",
            "fixed_after_visual_frame",
            "buggy_after_visual_frame",
            "pairwise_before_visual_equivalence",
            "two_by_two_after_visual_cross_matrix",
        ],
        "execution_authorization": {
            "status": "not_authorized",
            "scope_required": "preannotation-execution",
            "conditions": [
                "require_unlocked_native_session_passes",
                "use_a_new_run_root_below_private/bounded-runs/luanti/14757",
                "copy_bundles_before_launch_and_never_modify_repackage_v3",
                "set_an_isolated_HOME_and_absolute_screenshot_path",
                "launch_at_most_one_fixed_and_one_buggy_client_pair",
                "capture_only_engine_F12_PNGs_with_nonzero_dimensions",
                "terminate_only_owned_process_group_and_retain_raw_observations",
                "never_signal_protected_pids",
            ],
        },
        "next_action": (
            "run native-input preflight first; if it passes, request one explicitly "
            "bounded disposable F12/OpenGL pair and write separate visual receipts"
        ),
    }
    inputs = tuple(reference for reference in refs)
    existing = _try_load(root, _PLAN)
    if existing is None:
        write_stable_artifact(
            root,
            artifact_id=_PLAN.artifact_id,
            artifact_kind=_PLAN.artifact_kind,
            semantic_version=_PLAN.semantic_version,
            created_at=_now(),
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
        existing = _load(root, _PLAN)
    if existing.inputs != inputs or dict(existing.payload) != payload:
        raise RuntimeError("#14757 visual capture plan typed reload drifted")
    _write_blocker(root, existing, authorization=authorization)
    return existing


def _write_blocker(
    root: Path,
    plan: StableArtifact,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist an explicit non-terminal blocker bound to the static plan."""

    payload = {
        "candidate": dict(_CANDIDATE),
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked",
        "reason": "no_registered_non_null_client_capture_runner",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "protected_pids": list(_PROTECTED_PIDS),
        "plan_reference": plan.reference.artifact_id,
        "missing_required_evidence": list(
            plan.payload["minimum_missing_evidence"]
        ),
        "next_action": plan.payload["next_action"],
        "execution_authorization": plan.payload["execution_authorization"],
        "complete": True,
    }
    existing = _try_load(root, _BLOCKER)
    if existing is None:
        write_stable_artifact(
            root,
            artifact_id=_BLOCKER.artifact_id,
            artifact_kind=_BLOCKER.artifact_kind,
            semantic_version=_BLOCKER.semantic_version,
            created_at=_now(),
            inputs=(plan.reference,),
            payload=payload,
            authorization=authorization,
        )
        existing = _load(root, _BLOCKER)
    if existing.inputs != (plan.reference,) or dict(existing.payload) != payload:
        raise RuntimeError("#14757 visual capture blocker typed reload drifted")
    return existing


def _validate_receipts(loaded: dict[str, StableArtifact]) -> None:
    for artifact in loaded.values():
        if artifact.payload.get("candidate") != _CANDIDATE:
            raise RuntimeError("#14757 visual plan candidate mismatch")
    aggregate = loaded[_GATE_AGGREGATE.artifact_id]
    transition = loaded[_GATE_TRANSITION.artifact_id]
    replay = loaded[_GATE9_REPLAY.artifact_id]
    observation = loaded[_GATE10_OBSERVATION.artifact_id]
    oracle = loaded[_GATE10_ORACLE.artifact_id]
    blocker = loaded[_GUI_BLOCKER.artifact_id]
    if aggregate.payload.get("qualified") is not True or transition.payload.get(
        "qualified"
    ) is not True:
        raise RuntimeError("#14757 Gate 1-7 receipts are not qualified")
    if replay.payload.get("gate9_status") != "complete" or replay.payload.get(
        "state"
    ) != "passed":
        raise RuntimeError("#14757 Gate 9 replay receipt is not complete")
    if observation.payload.get("state") != "passed" or oracle.payload.get(
        "gate10_status"
    ) != "complete":
        raise RuntimeError("#14757 Gate 10 receipts are not complete")
    if any(
        artifact.payload.get("old_attempts_loaded") is not False
        or artifact.payload.get("old_construction_loaded") is not False
        for artifact in (replay, observation, oracle)
    ):
        raise RuntimeError("#14757 visual plan would inherit old evidence")
    if blocker.payload.get("native_input_unavailable") is not True:
        raise RuntimeError("#14757 GUI native-input blocker is not current")
    repackage = loaded[_REPACKAGE.artifact_id]
    if (
        repackage.payload.get("state") != "downstream_pending"
        or repackage.payload.get("outcome") != "static_packaging_qualified"
        or repackage.payload.get("candidate_execution_performed") is not False
        or repackage.payload.get("goldens_complete") is not False
        or repackage.payload.get("visual_cross_matrix_complete") is not False
        or repackage.payload.get("original_pair_modified") is not False
    ):
        raise RuntimeError("#14757 repackage-v3 is not an untouched static pair")


def _inspect_variant(root: Path, app: Path, source: Path) -> dict[str, Any]:
    _ordinary_directory(app, root)
    binary = _ordinary_file(app / "Contents/MacOS/minetest", app)
    if not os.access(binary, os.X_OK):
        raise RuntimeError("#14757 app binary is not executable")
    resources = _ordinary_directory(app / "Contents/Resources", app)
    devtest = _ordinary_directory(resources / "games/devtest", app)
    _ordinary_file(devtest / "game.conf", app)
    shader_link = resources / "client/shaders/Irrlicht"
    if not shader_link.is_symlink():
        raise RuntimeError("#14757 repaired Irrlicht shader target is missing")
    shader_target = shader_link.resolve(strict=True)
    try:
        shader_target.relative_to(app.resolve(strict=True))
    except ValueError as error:
        raise RuntimeError(
            "#14757 repaired Irrlicht shader target escapes app bundle"
        ) from error
    _ordinary_directory(shader_target, app)

    _ordinary_directory(source, root)
    cache = _ordinary_file(source / "build-app/CMakeCache.txt", root).read_text(
        encoding="utf-8", errors="replace"
    )
    game = _ordinary_file(source / "src/client/game.cpp", root).read_text(
        encoding="utf-8", errors="replace"
    )
    client = _ordinary_file(source / "src/client/client.cpp", root).read_text(
        encoding="utf-8", errors="replace"
    )
    opengl = _ordinary_file(source / "irr/src/COpenGLDriver.cpp", root).read_text(
        encoding="utf-8", errors="replace"
    )
    defaults = _ordinary_file(source / "src/defaultsettings.cpp", root).read_text(
        encoding="utf-8", errors="replace"
    )
    binary_bytes = binary.read_bytes()
    return {
        "app_path": str(app.relative_to(root)),
        "binary_path": str(binary.relative_to(root)),
        "binary_fingerprint": _sha256(binary),
        "devtest_file_count": sum(1 for item in devtest.rglob("*") if item.is_file()),
        "devtest_game_present": (devtest / "game.conf").is_file(),
        "opengl_cmake_enabled": "ENABLE_OPENGL:BOOL=ON" in cache,
        "build_client_true": "BUILD_CLIENT:BOOL=TRUE" in cache,
        "opengl_screenshot_readback": "createScreenShot" in opengl
        and "glReadPixels" in opengl,
        "builtin_f12_screenshot_path": all(
            needle in game or needle in defaults or needle in client
            for needle in (
                "wasKeyPressed(KeyType::SCREENSHOT)",
                "void Client::makeScreenshot",
                "createScreenShot",
            )
        )
        and "keymap_screenshot" in defaults
        and "KEY_F12" in defaults,
        "persistent_warptest_hook_present": "WARPTEST-CAPTURE-BEGIN" in game,
        "binary_contains_warptest_hook": b"WARPTEST-CAPTURE-BEGIN" in binary_bytes,
        "source_capture_scope_fingerprint": _scope_digest(source),
        "binary_has_opengl_framework_link": _has_opengl_link(binary),
    }


def _has_opengl_link(binary: Path) -> bool:
    try:
        result = subprocess.run(
            ["otool", "-L", str(binary)],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "OpenGL.framework" in result.stdout


def _scope_digest(source: Path) -> str:
    digest = hashlib.sha256()
    for relative in (
        "build-app/CMakeCache.txt",
        "src/client/game.cpp",
        "src/client/client.cpp",
        "src/defaultsettings.cpp",
        "irr/src/COpenGLDriver.cpp",
    ):
        path = source / relative
        data = path.read_bytes()
        digest.update(relative.encode())
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _ordinary_root(path: Path) -> Path:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("#14757 visual plan root is unsafe")
    return path.resolve(strict=True)


def _ordinary_directory(path: Path, root: Path) -> Path:
    path.relative_to(root)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"#14757 visual plan directory is unsafe: {path}")
    return path


def _ordinary_file(path: Path, root: Path) -> Path:
    path.relative_to(root)
    info = path.lstat()
    resolved = path.resolve(strict=True)
    resolved.relative_to(root.resolve(strict=True))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"#14757 visual plan file is unsafe: {path}")
    return resolved


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14757 visual plan authorization is inactive")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


__all__ = ["write_luanti_14757_visual_capture_plan", "_inspect_variant"]
