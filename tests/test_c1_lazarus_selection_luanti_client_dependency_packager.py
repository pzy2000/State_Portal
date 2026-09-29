from __future__ import annotations

import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_client_dependency_packager import (
    ClientDependencySources,
    plan_luanti_client_dependency_packaging,
    write_luanti_client_dependency_plan,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


@pytest.fixture(autouse=True)
def _registered_darwin_arm64_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the registered packaging plan independently of the CI host."""

    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.luanti_client_dependency_packager.platform.system",
        lambda: "Darwin",
    )
    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.luanti_client_dependency_packager.platform.machine",
        lambda: "arm64",
    )


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/luanti-client-dependency-packager",
        scope="preannotation-execution",
        granted_at="2026-08-14T02:00:00Z",
        local_user_id=os.getuid(),
    )


def _sources(root: Path) -> ClientDependencySources:
    roots = {name: root / name for name in ("freetype", "jpeg", "png")}
    for dependency, source in roots.items():
        include = source / (
            "include/freetype2" if dependency == "freetype" else "include"
        )
        include.mkdir(parents=True)
        (include / f"{dependency}.h").write_text("registered\n", encoding="utf-8")
        library = (
            source
            / "lib"
            / {
                "freetype": "libfreetype.6.dylib",
                "jpeg": "libjpeg.8.dylib",
                "png": "libpng16.16.dylib",
            }[dependency]
        )
        library.parent.mkdir()
        library.write_bytes(bytes.fromhex("cffaedfe0c000001") + bytes(24))
    opengl = root / "opengl"
    headers = opengl / "Headers"
    headers.mkdir(parents=True)
    for name in (
        "CGLContext.h",
        "CGLCurrent.h",
        "CGLDevice.h",
        "CGLIOSurface.h",
        "CGLMacro.h",
        "CGLRenderers.h",
        "CGLTypes.h",
        "OpenGL.h",
        "OpenGLAvailability.h",
        "gl.h",
        "gl3.h",
        "gl3ext.h",
        "glext.h",
        "gliContext.h",
        "gliDispatch.h",
        "gltypes.h",
        "glu.h",
        "gluContext.h",
        "gluMacro.h",
    ):
        (headers / name).write_text("registered OpenGL\n", encoding="utf-8")
    (opengl / "OpenGL.tbd").write_text("registered framework\n", encoding="utf-8")
    return ClientDependencySources(
        freetype_root=roots["freetype"],
        jpeg_root=roots["jpeg"],
        png_root=roots["png"],
        opengl_framework_root=opengl,
    )


def _seed_feasibility(root: Path) -> None:
    reference = ArtifactReference(
        "native_client_feasibility/luanti/15519/null-video/v1",
        "native_client_feasibility",
        "1.0",
    )
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at="2026-08-14T02:00:01Z",
        inputs=(),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 15519},
            "server_semantic_qualified": True,
            "missing_registered_client_dependencies": ["freetype", "jpeg", "png"],
            "null_client_status": "blocked_registered_client_dependencies",
            "candidate_execution_performed": False,
            "complete": True,
        },
        authorization=_authorization(),
    )


def test_plan_is_explicit_arm64_copy_and_configure_closure(tmp_path: Path) -> None:
    plan = plan_luanti_client_dependency_packaging(_sources(tmp_path))

    assert plan.feasible is True
    assert plan.host == "darwin-arm64"
    assert set(plan.dependencies) == {"freetype", "jpeg", "png", "opengl"}
    assert all(entry.source.is_file() for entry in plan.copy_entries)
    assert {
        entry.destination
        for entry in plan.copy_entries
        if entry.destination.suffix == ".dylib"
    } == {
        Path("packages/freetype/lib/libfreetype.6.dylib"),
        Path("packages/jpeg/lib/libjpeg.8.dylib"),
        Path("packages/png/lib/libpng16.16.dylib"),
    }
    assert plan.configure_fields["FREETYPE_INCLUDE_DIRS"].endswith(
        "packages/freetype/include/freetype2"
    )
    assert plan.configure_fields["JPEG_LIBRARY"].endswith(
        "packages/jpeg/lib/libjpeg.8.dylib"
    )
    assert plan.configure_fields["PNG_LIBRARY"].endswith(
        "packages/png/lib/libpng16.16.dylib"
    )
    assert plan.configure_fields["OPENGL_INCLUDE_DIR"].endswith(
        "packages/opengl/include"
    )
    assert plan.configure_fields["OPENGL_gl_LIBRARY"].endswith(
        "packages/opengl/lib/OpenGL.tbd"
    )
    assert Path("packages/opengl/include/OpenGL/gl.h") in {
        entry.destination for entry in plan.copy_entries
    }
    assert Path("packages/opengl/lib/OpenGL.tbd") in {
        entry.destination for entry in plan.copy_entries
    }
    assert (
        "install_name_tool",
        "-change",
        "/opt/homebrew/opt/libpng/lib/libpng16.16.dylib",
        "@rpath/libpng16.16.dylib",
        "private/dependencies/luanti-client/2.0/packages/freetype/lib/libfreetype.6.dylib",
    ) in plan.install_name_rewrites
    assert plan.install_name_rewrites[-1] == (
        "install_name_tool",
        "-add_rpath",
        "@loader_path/../Frameworks",
        "<client-executable>",
    )


def test_writer_persists_plan_without_copy_or_build(tmp_path: Path) -> None:
    _seed_feasibility(tmp_path)
    sources = _sources(tmp_path / "sources")

    artifact = write_luanti_client_dependency_plan(
        tmp_path,
        sources=sources,
        authorization=_authorization(),
    )

    assert artifact.reference == ArtifactReference(
        "client_dependency_packaging_plan/luanti/15519/darwin-arm64/v2",
        "client_dependency_packaging_plan",
        "2.0",
    )
    assert artifact.payload["feasible_offline"] is True
    assert artifact.payload["packaging_performed"] is False
    assert artifact.payload["client_build_started"] is False
    assert artifact.payload["network_required"] is False
    assert artifact.payload["downstream_state"] == "ready_for_offline_packaging"


def test_plan_rejects_incomplete_opengl_framework(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    (sources.opengl_framework_root / "Headers/OpenGLAvailability.h").unlink()

    with pytest.raises(RuntimeError, match="OpenGL headers are incomplete"):
        plan_luanti_client_dependency_packaging(sources)
