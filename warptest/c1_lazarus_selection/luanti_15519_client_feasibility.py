"""Preparation-only null-video client feasibility for Luanti PR 15519."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 15519}
_VERSION = "1.0"
_REQUIRED_CLIENT_DEPENDENCIES = ("freetype", "jpeg", "png")
_AGGREGATE = ArtifactReference(
    "native_server_closure/luanti/15519/fixed-buggy/v1",
    "native_server_closure",
    _VERSION,
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/15519/issue-15518/v2",
    "lazarus_task_mapping",
    "2.0",
)


@dataclass(frozen=True)
class Luanti15519ClientFeasibility:
    server_semantic_qualified: bool
    source_pair_complete: bool
    null_video_supported: bool
    null_video_screenshot_capable: bool
    missing_registered_dependencies: tuple[str, ...]
    null_client_status: str
    visual_status: str


def audit_luanti_15519_client_feasibility(
    artifact_root: str | Path,
) -> Luanti15519ClientFeasibility:
    """Audit only; this function never configures, builds or launches a client."""

    root = _ordinary_root(Path(artifact_root).absolute())
    aggregate = _load(root, _AGGREGATE)
    _validate_server_aggregate(aggregate)
    source_pair_complete = True
    null_supported = True
    screenshot_capable = True
    for variant in ("fixed", "buggy"):
        tree = _ordinary_directory(
            root / f"private/luanti-15519-server-closure/v1/{variant}", root
        )
        rendering = _ordinary_file(tree / "src/client/renderingengine.cpp", tree)
        null_driver = _ordinary_file(tree / "irr/src/CNullDriver.cpp", tree)
        client_cmake = _ordinary_file(tree / "src/CMakeLists.txt", tree)
        irr_cmake = _ordinary_file(tree / "irr/src/CMakeLists.txt", tree)
        rendering_text = rendering.read_text(encoding="utf-8")
        null_text = null_driver.read_text(encoding="utf-8")
        client_text = client_cmake.read_text(encoding="utf-8")
        irr_text = irr_cmake.read_text(encoding="utf-8")
        null_supported = null_supported and all(
            marker in rendering_text for marker in ("video::EDT_NULL", '"null"')
        )
        screenshot_capable = screenshot_capable and not (
            "createScreenShot" in null_text and "return 0;" in null_text
        )
        if "find_package(Freetype REQUIRED)" not in client_text:
            raise RuntimeError("registered client dependency declaration changed")
        if not all(
            marker in irr_text
            for marker in ("find_package(JPEG REQUIRED)", "find_package(PNG REQUIRED)")
        ):
            raise RuntimeError("registered Irrlicht dependency declaration changed")
    registered = _registered_packages(root)
    missing = tuple(
        dependency
        for dependency in _REQUIRED_CLIENT_DEPENDENCIES
        if dependency not in registered
    )
    null_status = (
        "blocked_registered_client_dependencies"
        if missing
        else "ready_for_bounded_build_not_executed"
    )
    visual_status = (
        "blocked_null_driver_has_no_framebuffer"
        if not screenshot_capable
        else "ready_for_bounded_frame_capture_not_executed"
    )
    return Luanti15519ClientFeasibility(
        server_semantic_qualified=True,
        source_pair_complete=source_pair_complete,
        null_video_supported=null_supported,
        null_video_screenshot_capable=screenshot_capable,
        missing_registered_dependencies=missing,
        null_client_status=null_status,
        visual_status=visual_status,
    )


def write_luanti_15519_client_feasibility(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    result = audit_luanti_15519_client_feasibility(root)
    mapping = _load(root, _MAPPING)
    reference = ArtifactReference(
        "native_client_feasibility/luanti/15519/null-video/v1",
        "native_client_feasibility",
        _VERSION,
    )
    payload: dict[str, object] = {
        "candidate": dict(_CANDIDATE),
        "server_semantic_qualified": result.server_semantic_qualified,
        "source_pair_complete": result.source_pair_complete,
        "registered_start": {
            "fixture_root": "private/downstream-fixtures/luanti/15519/russian-po-game-v1",
            "video_driver": "null",
            "isolated_world": True,
        },
        "ordered_replay": [
            "launch_fixed_or_buggy_client_with_video_driver_null",
            "start_registered_russian_plural_fixture",
            "wait_for_client_world_start_marker",
            "capture_invalid_plural_forms_and_translation_result",
            "request_bounded_shutdown",
        ],
        "raw_semantic_observation_contract": {
            "fixed": "world_start_and_translation_result_without_invalid_plural_forms",
            "buggy": "world_start_and_translation_result_with_invalid_plural_forms",
        },
        "null_video_supported": result.null_video_supported,
        "null_video_screenshot_capable": result.null_video_screenshot_capable,
        "required_registered_client_dependencies": list(_REQUIRED_CLIENT_DEPENDENCIES),
        "missing_registered_client_dependencies": list(
            result.missing_registered_dependencies
        ),
        "null_client_status": result.null_client_status,
        "visual_status": result.visual_status,
        "visual_golden_disposition": "still_required",
        "visual_cross_matrix_status": "blocked_until_non_null_frames_exist",
        "minimum_repairs": [
            "register_freetype_jpeg_png_client_dependency_closure",
            "run_one_bounded_null_client_build_per_existing_source_variant",
            "capture_fixed_buggy_null_client_start_and_semantic_replay_receipts",
            "build_or_reuse_non_null_rendering_client_pair_for_real_frames",
            "capture_common_before_fixed_after_buggy_after_visual_goldens",
            "evaluate_pairwise_before_and_two_by_two_after_visual_cross_matrix",
        ],
        "downstream_state": "pending",
        "candidate_execution_performed": False,
        "second_build_started": False,
        "permanent_terminal_written": False,
        "paid_actions_authorized": False,
        "complete": True,
    }
    inputs = (_AGGREGATE, mapping.reference)
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
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("native client feasibility typed reload failed")
    return artifact


def _validate_server_aggregate(artifact: StableArtifact) -> None:
    payload = artifact.payload
    expected_inputs = (
        ArtifactReference(
            "native_server_closure_step/luanti/15519/fixed/observer/v1",
            "native_server_closure_step",
            _VERSION,
        ),
        ArtifactReference(
            "native_server_closure_step/luanti/15519/buggy/observer/v1",
            "native_server_closure_step",
            _VERSION,
        ),
    )
    if (
        artifact.inputs != expected_inputs
        or payload.get("candidate") != _CANDIDATE
        or payload.get("server_only") is not True
        or payload.get("fixed_build") != "passed"
        or payload.get("buggy_build") != "passed"
        or payload.get("fixed_observer")
        != "fixed_accepts_registered_russian_plural_form"
        or payload.get("buggy_observer")
        != "buggy_rejects_registered_russian_plural_form"
        or payload.get("semantic_qualified") is not True
        or payload.get("complete") is not True
    ):
        raise RuntimeError("native server semantic aggregate is invalid")


def _registered_packages(root: Path) -> frozenset[str]:
    manifest = _ordinary_file(
        root / "private/dependencies/luanti-native/3.0/runtime.json", root
    )
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("registered native toolchain manifest is invalid") from error
    package_roots = value.get("package_roots") if type(value) is dict else None
    if type(package_roots) is not dict or any(
        type(key) is not str or type(path) is not str
        for key, path in package_roots.items()
    ):
        raise RuntimeError("registered native toolchain packages are invalid")
    return frozenset(package_roots)


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _ordinary_root(path: Path) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return path.resolve(strict=True)


def _ordinary_directory(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required source directory is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("required source directory is missing or unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required source file is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("required source file is missing or unsafe")
    return resolved


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("preannotation execution authorization is required")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "Luanti15519ClientFeasibility",
    "audit_luanti_15519_client_feasibility",
    "write_luanti_15519_client_feasibility",
]
