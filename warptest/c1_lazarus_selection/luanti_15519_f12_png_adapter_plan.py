"""Typed, no-launch plan for the Luanti #15519 F12 PNG adapter.

This module proves only that the current fixed/buggy client pair exposes the
same candidate-owned F12-to-PNG path and that a future bounded runner can keep
all output below an isolated ``MINETEST_USER_PATH``.  It never launches a
client, injects input, or claims Gate 11--13 evidence.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 15519}
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/15519/fixed-buggy/v1",
    "candidate_materialization",
    "1.0",
)
_AUDIT = ArtifactReference(
    "candidate_nonnull_visual_static_audit/luanti/15519/v2",
    "candidate_nonnull_visual_static_audit",
    "2.0",
)
_PATH_PLAN = ArtifactReference(
    "candidate_nonnull_capture_path_plan/luanti/15519/v1",
    "candidate_nonnull_capture_path_plan",
    "1.0",
)
_PLAN = ArtifactReference(
    "candidate_f12_png_adapter_plan/luanti/15519/current-v2/v1",
    "candidate_f12_png_adapter_plan",
    "1.0",
)
_PROTECTED_PIDS = (76485, 89366, 95665, 99762)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def inspect_png_bytes(data: bytes) -> dict[str, int]:
    """Validate the minimal PNG envelope needed by a future frame receipt."""

    if len(data) < 24 or data[:8] != _PNG_SIGNATURE:
        raise ValueError("not a PNG stream")
    length = struct.unpack(">I", data[8:12])[0]
    if length != 13 or data[12:16] != b"IHDR":
        raise ValueError("PNG does not start with a canonical IHDR")
    width, height = struct.unpack(">II", data[16:24])
    if width <= 0 or height <= 0:
        raise ValueError("PNG dimensions must be positive")
    return {"width": width, "height": height}


def select_single_new_png(
    isolated_root: str | Path,
    *,
    before: Iterable[str | Path],
    after: Iterable[str | Path],
) -> Path:
    """Fail closed unless exactly one new ordinary PNG belongs to the run root."""

    root = Path(isolated_root).absolute().resolve(strict=True)
    before_set = {Path(item).absolute() for item in before}
    candidates = [Path(item).absolute() for item in after if Path(item).absolute() not in before_set]
    if len(candidates) != 1:
        raise RuntimeError("F12 adapter requires exactly one new output")
    candidate = candidates[0]
    info = candidate.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeError("F12 output is not an ordinary file")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root) or resolved.suffix.lower() != ".png":
        raise RuntimeError("F12 output escaped the isolated PNG root")
    inspect_png_bytes(resolved.read_bytes())
    return resolved


def write_luanti_15519_f12_png_adapter_plan(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write a current-v2 readiness/TDD receipt without starting a GUI."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    materialization = _load(root, _MATERIALIZATION)
    audit = _load(root, _AUDIT)
    path_plan = _load(root, _PATH_PLAN)
    evidence = _validate_inputs(root, materialization, audit, path_plan)
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "downstream_pending",
        "outcome": "candidate_specific_f12_png_adapter_statically_feasible",
        "terminal": False,
        "resumable": True,
        "complete": True,
        "gate_11_13_claimed": False,
        "gui_launch_performed": False,
        "input_injection_performed": False,
        "frames_written": False,
        "runner_registered": False,
        "static_source_evidence": evidence,
        "adapter_contract": {
            "backend": "builtin_F12_to_OpenGL_glReadPixels_to_PNG",
            "isolation": "fresh disposable pair with distinct MINETEST_USER_PATH roots",
            "settings": {
                "screenshot_path": "screenshots",
                "screenshot_format": "png",
                "keymap_screenshot": "KEY_F12",
            },
            "capture_slots": [
                "fixed_common_before",
                "buggy_common_before",
                "fixed_after",
                "buggy_after",
            ],
            "trigger": "one registered F12 press after the slot readiness marker",
            "collector": "snapshot output set before trigger; accept exactly one new ordinary PNG below the isolated root",
            "png_validation": "signature plus first IHDR with positive width and height",
            "success_marker": "Saved screenshot to '<candidate-owned path>'",
            "lineage": "bind each frame digest to variant binary digest, replay/oracle receipt, slot, and isolated run id",
        },
        "tdd_contract": {
            "unit_cases": [
                "accept canonical PNG signature and positive IHDR dimensions",
                "reject bad signature, non-IHDR first chunk, and zero dimensions",
                "reject zero or multiple new outputs",
                "reject symlink, non-PNG, and output outside isolated root",
            ],
            "future_integration_cases": [
                "fixed and buggy common-before semantic states are equal",
                "fixed and buggy common-before PNGs satisfy visual equivalence",
                "fixed/buggy after PNGs populate the full two-by-two golden matrix",
                "all four frames share current-v2 materialization and v4 semantic lineage",
            ],
        },
        "legality": {
            "bounded_run_authorization_present": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "old_attempts_loaded": False,
            "persistent_pair_mutation_allowed": False,
            "protected_pids": list(_PROTECTED_PIDS),
            "protected_pids_touched": False,
        },
        "remaining_evidence": list(path_plan.payload["remaining_evidence"]),
        "next_action": (
            "after explicit bounded authorization, register the disposable OpenGL/F12 "
            "runner and collect the four real PNG slots; do not reuse the persistent "
            "pair or touch protected PIDs"
        ),
    }
    inputs = (materialization.reference, audit.reference, path_plan.reference)
    try:
        existing = _load(root, _PLAN)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("15519 F12 PNG plan conflicts with current state")
        return existing
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
    artifact = _load(root, _PLAN)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("15519 F12 PNG plan typed reload failed")
    return artifact


def _validate_inputs(
    root: Path,
    materialization: StableArtifact,
    audit: StableArtifact,
    path_plan: StableArtifact,
) -> dict[str, Any]:
    for artifact in (materialization, audit, path_plan):
        if artifact.payload.get("candidate") != _CANDIDATE:
            raise RuntimeError("15519 F12 PNG input is not candidate-bound")
    if materialization.payload.get("state") != "built":
        raise RuntimeError("15519 client pair is not built")
    capture = audit.payload.get("capture", {})
    if not (
        audit.payload.get("state") == "downstream_pending"
        and capture.get("compiled_backend_available") is True
        and capture.get("builtin_f12_screenshot_path") is True
        and capture.get("registered_non_null_backend") is False
        and capture.get("frames_written") is False
    ):
        raise RuntimeError("15519 current visual blocker drifted")
    if not (
        path_plan.payload.get("state") == "downstream_pending"
        and path_plan.payload.get("candidate_specific_path", {}).get("runner_registered") is False
        and path_plan.payload.get("current_semantic_lineage", {}).get("fixed_after_real") is True
        and path_plan.payload.get("current_semantic_lineage", {}).get("buggy_after_real") is True
        and path_plan.payload.get("current_semantic_lineage", {}).get("common_before_real") is False
    ):
        raise RuntimeError("15519 Gate9/10 or visual plan state drifted")

    variants: dict[str, dict[str, str]] = {}
    for variant, key in (("fixed", "fixed_tree_path"), ("buggy", "buggy_tree_path")):
        tree = root / PurePosixPath(str(materialization.payload[key]))
        variants[variant] = _inspect_source(tree, root)
    if variants["fixed"]["capture_source_fingerprint"] != variants["buggy"]["capture_source_fingerprint"]:
        raise RuntimeError("15519 fixed/buggy F12 PNG source paths differ")
    return {
        "variants": variants,
        "fixed_buggy_capture_path_identical": True,
        "absolute_output_override_supported": True,
        "isolated_user_path_environment": "MINETEST_USER_PATH",
    }


def _inspect_source(tree: Path, root: Path) -> dict[str, str]:
    files = {
        "game": tree / "src/client/game.cpp",
        "client": tree / "src/client/client.cpp",
        "defaults": tree / "src/defaultsettings.cpp",
        "porting": tree / "src/porting.cpp",
    }
    texts: dict[str, str] = {}
    for name, path in files.items():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"15519 {name} source is unsafe")
        texts[name] = path.read_text(encoding="utf-8", errors="strict")
    required = (
        "wasKeyPressed(KeyType::SCREENSHOT)",
        "void Client::makeScreenshot()",
        "driver->createScreenShot()",
        'g_settings->get("screenshot_path")',
        "driver->writeImageToFile",
        "Saved screenshot to '",
        'setDefault("screenshot_path", "screenshots")',
        'setDefault("screenshot_format", "png")',
        'setDefault("keymap_screenshot", "KEY_F12")',
        'getenv("MINETEST_USER_PATH")',
    )
    joined = "\n".join(texts.values())
    missing = [marker for marker in required if marker not in joined]
    if missing:
        raise RuntimeError(f"15519 F12 PNG source markers missing: {missing}")
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return {
        "tree_path": str(tree.relative_to(root)),
        "capture_source_fingerprint": digest,
        "f12_entry": "src/client/game.cpp",
        "png_writer": "src/client/client.cpp",
        "isolated_user_path_source": "src/porting.cpp",
    }


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("15519 F12 PNG plan authorization is inactive")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "inspect_png_bytes",
    "select_single_new_png",
    "write_luanti_15519_f12_png_adapter_plan",
]
