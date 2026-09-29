"""Read-only non-null visual preflight for Luanti #15519.

The current-v2 client pair is named ``null-client`` because the first
downstream probe selected Irrlicht's null driver.  The pair was nevertheless
compiled with the macOS OpenGL backend.  This module records that distinction
without launching either binary: it proves which source paths can read a
framebuffer, captures immutable baseline digests for a future disposable copy,
and leaves Gate 11--12 pending until a candidate-specific capture runner is
registered.

No files below the persistent materialization are changed.  In particular,
the audit does not install the WarpTest C++ hook; doing so would alter the
production source bytes and would require a fresh build.  A later bounded
runner may use the ordinary built-in F12 screenshot path or a fresh additive
instrumentation copy, but neither is claimed here.
"""

from __future__ import annotations

import hashlib
import os
import re
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 15519}
_V1 = "1.0"
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/15519/fixed-buggy/v1",
    "candidate_materialization",
    "1.0",
)
_CLIENT_AGGREGATE = ArtifactReference(
    "candidate_gate57_15519_client_aggregate/luanti/15519/v1",
    "candidate_gate57_15519_client_aggregate",
    "1.0",
)
_TASK_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/15519/issue-15518/v2",
    "lazarus_task_mapping",
    "2.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/15519/fixed/v2",
    "fixed_source_capsule",
    "2.0",
)
_CLIENT_CLOSURE = ArtifactReference(
    "client_dependency_closure/luanti/15519/darwin-arm64/v2",
    "client_dependency_closure",
    "2.0",
)
_AUDIT = ArtifactReference(
    "candidate_nonnull_visual_static_audit/luanti/15519/v2",
    "candidate_nonnull_visual_static_audit",
    "2.0",
)
_PROTECTED_PIDS = (76485, 89366, 95665, 99762)
_HOOK_MARKERS = ("WARPTEST-CAPTURE-BEGIN", "WARPTEST-CALL-BEGIN")
_PATH_LINE = re.compile(r"^\s+([^\s(]+)\s+\(compatibility", re.MULTILINE)


def write_luanti_15519_nonnull_static_audit(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write a typed, non-terminal OpenGL/static preflight receipt.

    The authorization scope is intentionally ``infrastructure-audit``.  This
    is not a runtime launch and cannot consume paid or formal-trial authority.
    """

    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    materialization = _load(root, _MATERIALIZATION)
    client = _load(root, _CLIENT_AGGREGATE)
    mapping = _load(root, _TASK_MAPPING)
    capsule = _load(root, _CAPSULE)
    closure = _load(root, _CLIENT_CLOSURE)
    _validate_lineage(materialization, client, mapping, capsule, closure)

    variants: dict[str, dict[str, Any]] = {}
    for variant, tree_key, binary_key in (
        ("fixed", "fixed_tree_path", "fixed_binary_path"),
        ("buggy", "buggy_tree_path", "buggy_binary_path"),
    ):
        tree = root / PurePosixPath(str(materialization.payload[tree_key]))
        binary = root / PurePosixPath(str(materialization.payload[binary_key]))
        variants[variant] = _inspect_variant(tree, binary, root)

    # The current pair has not been copied or modified by this audit.  Keeping
    # the baseline digest in the receipt lets a later disposable runner prove
    # that only its copy (fixture/config/capture output) changed.
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked",
        "reason": "no_registered_non_null_capture_runner",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": False,
        "client_launch_performed": False,
        "gui_launch_performed": False,
        "persistent_tree_modified": False,
        "production_test_bytes_changed": False,
        "old_runtime_inheritance": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "protected_pids": list(_PROTECTED_PIDS),
        "lineage": {
            "materialization": materialization.reference.artifact_id,
            "client_gate57": client.reference.artifact_id,
            "task_mapping": mapping.reference.artifact_id,
            "fixed_capsule": capsule.reference.artifact_id,
            "client_dependency_closure": closure.reference.artifact_id,
        },
        "variants": variants,
        "capture": {
            "compiled_backend": "irrlicht_opengl_createScreenShot_glReadPixels",
            "compiled_backend_available": all(
                item["opengl_screenshot_readback"] is True
                for item in variants.values()
            ),
            "builtin_f12_screenshot_path": all(
                item["builtin_screenshot_path"] is True
                for item in variants.values()
            ),
            "registered_non_null_backend": False,
            "registered_start_replay_adapter": False,
            "framebuffer_readback_registered": False,
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
            "registered_client_start_and_ordered_replay_adapter",
            "common_before_semantic_observation",
            "common_before_visual_frame",
            "fixed_after_visual_frame",
            "buggy_after_visual_frame",
            "pairwise_before_visual_equivalence",
            "two_by_two_after_visual_cross_matrix",
            "same_lineage_construction_receipt",
        ],
        "next_action": (
            "register a fresh disposable F12/OpenGL capture runner, bind the "
            "same current-v2 source/materialization/semantic lineage, then "
            "perform one explicitly bounded fixed/buggy launch"
        ),
        "complete": True,
    }
    inputs = (
        materialization.reference,
        client.reference,
        mapping.reference,
        capsule.reference,
        closure.reference,
    )
    existing = _try_load(root, _AUDIT)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("15519 non-null static audit conflicts with current state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_AUDIT.artifact_id,
        artifact_kind=_AUDIT.artifact_kind,
        semantic_version=_AUDIT.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _AUDIT)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("15519 non-null static audit typed reload failed")
    return artifact


def _inspect_variant(tree: Path, binary: Path, root: Path) -> dict[str, Any]:
    _ordinary_directory(tree, root)
    _ordinary_file(binary, tree)
    if not os.access(binary, os.X_OK):
        raise RuntimeError("15519 client binary is not executable")
    cmake = _ordinary_file(tree / "build-luanti-15519/CMakeCache.txt", tree)
    game_cpp = _ordinary_file(tree / "src/client/game.cpp", tree)
    client_cpp = _ordinary_file(tree / "src/client/client.cpp", tree)
    null_driver = _ordinary_file(tree / "irr/src/CNullDriver.cpp", tree)
    opengl_candidates = (
        tree / "irr/src/COpenGLDriver.cpp",
        tree / "irr/src/OpenGL/Driver.cpp",
    )
    opengl = next((path for path in opengl_candidates if path.is_file()), None)
    if opengl is None:
        raise RuntimeError("15519 OpenGL driver source is missing")
    opengl = _ordinary_file(opengl, tree)
    devtest = _ordinary_file(tree / "games/devtest/game.conf", tree)
    cmake_text = cmake.read_text(encoding="utf-8", errors="replace")
    game_text = game_cpp.read_text(encoding="utf-8", errors="replace")
    client_text = client_cpp.read_text(encoding="utf-8", errors="replace")
    null_text = null_driver.read_text(encoding="utf-8", errors="replace")
    opengl_text = opengl.read_text(encoding="utf-8", errors="replace")
    null_start = null_text.find("IImage *CNullDriver::createScreenShot")
    null_body = null_text[null_start : null_start + 320] if null_start >= 0 else ""
    gl_start = opengl_text.find("createScreenShot")
    gl_body = opengl_text[gl_start : gl_start + 1800] if gl_start >= 0 else ""
    linked = _otool_links(binary)
    # System framework/libc links are expected.  Homebrew absolute links are
    # recorded as a packaging caveat rather than silently treated as closed.
    external = tuple(
        sorted(
            path
            for path in linked
            if path.startswith("/")
            and not path.startswith(("/System/Library/", "/usr/lib/"))
        )
    )
    return {
        "tree_path": str(tree.relative_to(root)),
        "binary_path": str(binary.relative_to(root)),
        "binary_fingerprint": _sha256(binary),
        "build_client_true": "BUILD_CLIENT:BOOL=TRUE" in cmake_text,
        "opengl_enabled": "ENABLE_OPENGL:BOOL=ON" in cmake_text,
        "sdl2_disabled": "USE_SDL2:BOOL=OFF" in cmake_text,
        "devtest_game_present": devtest.is_file(),
        "opengl_driver_source": str(opengl.relative_to(root)),
        "opengl_screenshot_symbol": gl_start >= 0,
        "opengl_screenshot_readback": gl_start >= 0 and "glReadPixels" in gl_body,
        "null_driver_screenshot_symbol": null_start >= 0,
        "null_driver_returns_null": null_start >= 0 and "return 0;" in null_body,
        "builtin_screenshot_path": "wasKeyPressed(KeyType::SCREENSHOT)" in game_text
        and "void Client::makeScreenshot" in client_text
        and "createScreenShot" in client_text,
        "persistent_capture_hook_present": any(
            marker in game_text for marker in _HOOK_MARKERS
        ),
        "linked_dependencies": list(linked),
        "non_hermetic_absolute_dependencies": list(external),
        "source_scope_fingerprint": _scope_digest(tree),
    }


def _validate_lineage(
    materialization: StableArtifact,
    client: StableArtifact,
    mapping: StableArtifact,
    capsule: StableArtifact,
    closure: StableArtifact,
) -> None:
    if materialization.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("15519 materialization candidate mismatch")
    if materialization.payload.get("state") != "built" or materialization.payload.get(
        "complete"
    ) is not True:
        raise RuntimeError("15519 materialization is not built")
    for artifact in (client, mapping, capsule, closure):
        if artifact.payload.get("candidate") != _CANDIDATE:
            raise RuntimeError("15519 static audit lineage candidate mismatch")
    if client.payload.get("qualified") is not True:
        raise RuntimeError("15519 Gate 5-7 aggregate is not qualified")
    if closure.payload.get("state") != "materialized":
        raise RuntimeError("15519 client dependency closure is not materialized")


def _scope_digest(tree: Path) -> str:
    digest = hashlib.sha256()
    roots = ("src", "irr", "builtin", "games/devtest", "CMakeLists.txt", "cmake")
    paths: list[Path] = []
    for relative in roots:
        path = tree / relative
        if path.is_file():
            paths.append(path)
        elif path.is_dir():
            paths.extend(item for item in path.rglob("*") if item.is_file())
    for path in sorted(paths):
        rel = path.relative_to(tree).as_posix().encode()
        digest.update(len(rel).to_bytes(8, "big"))
        digest.update(rel)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _otool_links(binary: Path) -> tuple[str, ...]:
    if not shutil_which("otool"):
        return ()
    try:
        completed = subprocess.run(
            ["otool", "-L", str(binary)],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if completed.returncode != 0:
        return ()
    return tuple(_PATH_LINE.findall(completed.stdout))


def shutil_which(command: str) -> str | None:
    """Tiny local ``which`` helper to keep the audit dependency-free."""

    for entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(entry) / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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
        raise RuntimeError("15519 static audit root is unsafe")
    return path.resolve(strict=True)


def _ordinary_directory(path: Path, root: Path) -> None:
    path.relative_to(root)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("15519 static audit directory is unsafe")


def _ordinary_file(path: Path, root: Path) -> Path:
    path.relative_to(root)
    info = path.lstat()
    resolved = path.resolve(strict=True)
    resolved.relative_to(root.resolve(strict=True))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("15519 static audit file is unsafe")
    return resolved


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("15519 static audit authorization is inactive")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_15519_nonnull_static_audit"]
