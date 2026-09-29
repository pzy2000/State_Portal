from __future__ import annotations

import os
from pathlib import Path

from warptest.c1_lazarus_selection.luanti_15519_nonnull_static_audit import (
    _inspect_variant,
    write_luanti_15519_nonnull_static_audit,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/luanti-15519-nonnull-static-audit",
        scope="infrastructure-audit",
        granted_at="2026-08-14T18:00:00Z",
        local_user_id=os.getuid(),
    )


def _write(root: Path, reference: ArtifactReference, payload: dict[str, object]) -> None:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at="2026-08-14T18:00:01Z",
        inputs=(),
        payload=payload,
        authorization=_authorization(),
    )


def _tree(root: Path, variant: str) -> Path:
    tree = root / f"private/materialized/{variant}"
    (tree / "bin").mkdir(parents=True)
    binary = tree / "bin/luanti"
    binary.write_bytes(b"Mach-O fixture")
    binary.chmod(0o755)
    (tree / "build-luanti-15519").mkdir()
    (tree / "build-luanti-15519/CMakeCache.txt").write_text(
        "BUILD_CLIENT:BOOL=TRUE\nENABLE_OPENGL:BOOL=ON\nUSE_SDL2:BOOL=OFF\n",
        encoding="utf-8",
    )
    (tree / "src/client").mkdir(parents=True)
    (tree / "src/client/game.cpp").write_text(
        "void Client::makeScreenshot() {}\n"
        "wasKeyPressed(KeyType::SCREENSHOT);\n",
        encoding="utf-8",
    )
    (tree / "src/client/client.cpp").write_text(
        "void Client::makeScreenshot() { driver->createScreenShot(); }\n",
        encoding="utf-8",
    )
    (tree / "irr/src").mkdir(parents=True)
    (tree / "irr/src/CNullDriver.cpp").write_text(
        "IImage *CNullDriver::createScreenShot() { return 0; }\n",
        encoding="utf-8",
    )
    (tree / "irr/src/COpenGLDriver.cpp").write_text(
        "IImage *COpenGLDriver::createScreenShot() {\n"
        "  glReadPixels(0, 0, 1280, 720, GL_RGBA, GL_UNSIGNED_BYTE, pixels);\n"
        "}\n",
        encoding="utf-8",
    )
    (tree / "games/devtest").mkdir(parents=True)
    (tree / "games/devtest/game.conf").write_text("name = devtest\n", encoding="utf-8")
    return tree


def _seed(root: Path) -> None:
    fixed = _tree(root, "fixed")
    buggy = _tree(root, "buggy")
    materialization = ArtifactReference(
        "candidate_materialization/luanti/15519/fixed-buggy/v1",
        "candidate_materialization",
        "1.0",
    )
    _write(
        root,
        materialization,
        {
            "candidate": {"repository_key": "luanti", "change_number": 15519},
            "state": "built",
            "complete": True,
            "fixed_tree_path": str(fixed.relative_to(root)),
            "buggy_tree_path": str(buggy.relative_to(root)),
            "fixed_binary_path": str((fixed / "bin/luanti").relative_to(root)),
            "buggy_binary_path": str((buggy / "bin/luanti").relative_to(root)),
        },
    )
    _write(
        root,
        ArtifactReference(
            "candidate_gate57_15519_client_aggregate/luanti/15519/v1",
            "candidate_gate57_15519_client_aggregate",
            "1.0",
        ),
        {
            "candidate": {"repository_key": "luanti", "change_number": 15519},
            "qualified": True,
        },
    )
    for reference, payload in (
        (
            ArtifactReference(
                "lazarus_task_mapping/luanti/15519/issue-15518/v2",
                "lazarus_task_mapping",
                "2.0",
            ),
            {"candidate": {"repository_key": "luanti", "change_number": 15519}},
        ),
        (
            ArtifactReference(
                "fixed_source_capsule/luanti/15519/fixed/v2",
                "fixed_source_capsule",
                "2.0",
            ),
            {"candidate": {"repository_key": "luanti", "change_number": 15519}},
        ),
        (
            ArtifactReference(
                "client_dependency_closure/luanti/15519/darwin-arm64/v2",
                "client_dependency_closure",
                "2.0",
            ),
            {
                "candidate": {"repository_key": "luanti", "change_number": 15519},
                "state": "materialized",
            },
        ),
    ):
        _write(root, reference, payload)


def test_static_inspection_distinguishes_opengl_from_null_driver(tmp_path: Path) -> None:
    tree = _tree(tmp_path, "fixed")
    result = _inspect_variant(tree, tree / "bin/luanti", tmp_path)

    assert result["build_client_true"] is True
    assert result["opengl_enabled"] is True
    assert result["opengl_screenshot_readback"] is True
    assert result["null_driver_returns_null"] is True
    assert result["builtin_screenshot_path"] is True
    assert result["persistent_capture_hook_present"] is False
    assert result["binary_fingerprint"]
    assert result["source_scope_fingerprint"]


def test_typed_audit_is_pending_and_never_claims_frames(tmp_path: Path) -> None:
    _seed(tmp_path)

    artifact = write_luanti_15519_nonnull_static_audit(
        tmp_path,
        authorization=_authorization(),
    )

    assert artifact.payload["state"] == "downstream_pending"
    assert artifact.payload["outcome"] == "infrastructure_blocked"
    assert artifact.payload["candidate_execution_performed"] is False
    assert artifact.payload["persistent_tree_modified"] is False
    assert artifact.payload["production_test_bytes_changed"] is False
    capture = artifact.payload["capture"]
    assert capture["compiled_backend_available"] is True
    assert capture["registered_non_null_backend"] is False
    assert capture["frames_written"] is False
    assert capture["synthetic_frames_written"] is False
    assert artifact.payload["minimum_missing_evidence"]
