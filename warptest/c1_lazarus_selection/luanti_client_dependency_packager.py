"""Preparation-only offline dependency plan for the Luanti 15519 client."""

from __future__ import annotations

import os
import platform
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Mapping

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_VERSION = "2.0"
_CLIENT_FEASIBILITY = ArtifactReference(
    "native_client_feasibility/luanti/15519/null-video/v1",
    "native_client_feasibility",
    "1.0",
)
_LIBRARY_NAMES = {
    "freetype": "libfreetype.6.dylib",
    "jpeg": "libjpeg.8.dylib",
    "png": "libpng16.16.dylib",
}
_OPENGL_HEADER_NAMES = {
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
}


@dataclass(frozen=True)
class ClientDependencySources:
    freetype_root: Path
    jpeg_root: Path
    png_root: Path
    opengl_framework_root: Path

    def roots(self) -> Mapping[str, Path]:
        return {
            "freetype": self.freetype_root,
            "jpeg": self.jpeg_root,
            "png": self.png_root,
            "opengl": self.opengl_framework_root,
        }


@dataclass(frozen=True)
class DependencyCopyEntry:
    source: Path
    destination: Path


@dataclass(frozen=True)
class ClientDependencyPackagingPlan:
    feasible: bool
    host: str
    dependencies: Mapping[str, str]
    copy_entries: tuple[DependencyCopyEntry, ...]
    configure_fields: Mapping[str, str]
    install_name_rewrites: tuple[tuple[str, ...], ...]


def default_client_dependency_sources() -> ClientDependencySources:
    return ClientDependencySources(
        freetype_root=Path("/opt/homebrew/opt/freetype"),
        jpeg_root=Path("/opt/homebrew/opt/jpeg-turbo"),
        png_root=Path("/opt/homebrew/opt/libpng"),
        opengl_framework_root=Path(
            "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/"
            "System/Library/Frameworks/OpenGL.framework/Versions/A"
        ),
    )


def plan_luanti_client_dependency_packaging(
    sources: ClientDependencySources | None = None,
) -> ClientDependencyPackagingPlan:
    """Validate local inputs and return a copy plan; never mutate or build."""

    if platform.system() != "Darwin" or platform.machine().casefold() != "arm64":
        raise RuntimeError("Luanti client dependency plan requires Darwin arm64")
    selected = sources or default_client_dependency_sources()
    if type(selected) is not ClientDependencySources:
        raise ValueError("client dependency sources are invalid")
    entries: list[DependencyCopyEntry] = []
    versions: dict[str, str] = {}
    for dependency, raw_root in selected.roots().items():
        root = _ordinary_directory(raw_root)
        versions[dependency] = root.name
        if dependency == "opengl":
            headers_root = _ordinary_directory(root / "Headers")
            headers = sorted(
                (_ordinary_file(path) for path in headers_root.iterdir()),
                key=lambda path: path.name,
            )
            if {path.name for path in headers} != _OPENGL_HEADER_NAMES:
                raise RuntimeError("registered OpenGL headers are incomplete")
            entries.extend(
                DependencyCopyEntry(
                    source=header,
                    destination=Path("packages/opengl/include/OpenGL") / header.name,
                )
                for header in headers
            )
            entries.append(
                DependencyCopyEntry(
                    source=_ordinary_file(root / "OpenGL.tbd"),
                    destination=Path("packages/opengl/lib/OpenGL.tbd"),
                )
            )
            continue
        include_relative = (
            PurePosixPath("include/freetype2")
            if dependency == "freetype"
            else PurePosixPath("include")
        )
        include = _ordinary_directory(root / include_relative)
        headers = sorted(
            (
                _ordinary_file(path)
                for path in include.rglob("*.h")
                if not path.is_symlink()
            ),
            key=lambda path: path.relative_to(include).as_posix(),
        )
        if not headers:
            raise RuntimeError(f"registered {dependency} headers are unavailable")
        for header in headers:
            entries.append(
                DependencyCopyEntry(
                    source=header,
                    destination=Path("packages")
                    / dependency
                    / include_relative
                    / header.relative_to(include),
                )
            )
        library = _ordinary_file(root / "lib" / _LIBRARY_NAMES[dependency])
        if not _is_arm64_macho(library):
            raise RuntimeError(f"registered {dependency} library is not arm64 Mach-O")
        entries.append(
            DependencyCopyEntry(
                source=library,
                destination=Path("packages")
                / dependency
                / "lib"
                / _LIBRARY_NAMES[dependency],
            )
        )
    destination = "private/dependencies/luanti-client/2.0"
    configure = {
        "FREETYPE_INCLUDE_DIRS": f"{destination}/packages/freetype/include/freetype2",
        "FREETYPE_LIBRARY": f"{destination}/packages/freetype/lib/libfreetype.6.dylib",
        "JPEG_INCLUDE_DIR": f"{destination}/packages/jpeg/include",
        "JPEG_LIBRARY": f"{destination}/packages/jpeg/lib/libjpeg.8.dylib",
        "PNG_PNG_INCLUDE_DIR": f"{destination}/packages/png/include",
        "PNG_LIBRARY": f"{destination}/packages/png/lib/libpng16.16.dylib",
        "OPENGL_INCLUDE_DIR": f"{destination}/packages/opengl/include",
        "OPENGL_gl_LIBRARY": f"{destination}/packages/opengl/lib/OpenGL.tbd",
    }
    rewrites = (
        (
            "install_name_tool",
            "-id",
            "@rpath/libfreetype.6.dylib",
            f"{destination}/packages/freetype/lib/libfreetype.6.dylib",
        ),
        (
            "install_name_tool",
            "-change",
            "/opt/homebrew/opt/libpng/lib/libpng16.16.dylib",
            "@rpath/libpng16.16.dylib",
            f"{destination}/packages/freetype/lib/libfreetype.6.dylib",
        ),
        (
            "install_name_tool",
            "-id",
            "@rpath/libjpeg.8.dylib",
            f"{destination}/packages/jpeg/lib/libjpeg.8.dylib",
        ),
        (
            "install_name_tool",
            "-id",
            "@rpath/libpng16.16.dylib",
            f"{destination}/packages/png/lib/libpng16.16.dylib",
        ),
        (
            "install_name_tool",
            "-add_rpath",
            "@loader_path/../Frameworks",
            "<client-executable>",
        ),
    )
    return ClientDependencyPackagingPlan(
        feasible=True,
        host="darwin-arm64",
        dependencies=versions,
        copy_entries=tuple(entries),
        configure_fields=configure,
        install_name_rewrites=rewrites,
    )


def write_luanti_client_dependency_plan(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    sources: ClientDependencySources | None = None,
) -> StableArtifact:
    _require_authorization(authorization)
    root = _ordinary_directory(Path(artifact_root))
    feasibility = _load(root, _CLIENT_FEASIBILITY)
    if (
        feasibility.payload.get("candidate")
        != {"repository_key": "luanti", "change_number": 15519}
        or feasibility.payload.get("server_semantic_qualified") is not True
        or feasibility.payload.get("candidate_execution_performed") is not False
        or feasibility.payload.get("complete") is not True
    ):
        raise RuntimeError("native client feasibility boundary is invalid")
    plan = plan_luanti_client_dependency_packaging(sources)
    reference = ArtifactReference(
        "client_dependency_packaging_plan/luanti/15519/darwin-arm64/v2",
        "client_dependency_packaging_plan",
        _VERSION,
    )
    payload: dict[str, object] = {
        "candidate": {"repository_key": "luanti", "change_number": 15519},
        "host": plan.host,
        "feasible_offline": plan.feasible,
        "dependency_versions": dict(plan.dependencies),
        "copy_entries": [
            {"source": str(entry.source), "destination": entry.destination.as_posix()}
            for entry in plan.copy_entries
        ],
        "configure_fields": dict(plan.configure_fields),
        "install_name_rewrites": [list(argv) for argv in plan.install_name_rewrites],
        "system_runtime_dependencies": [
            "/usr/lib/libz.1.dylib",
            "/usr/lib/libbz2.1.0.dylib",
            "/usr/lib/libSystem.B.dylib",
        ],
        "destination_root": "private/dependencies/luanti-client/2.0",
        "packaging_performed": False,
        "client_build_started": False,
        "candidate_execution_performed": False,
        "network_required": False,
        "downstream_state": "ready_for_offline_packaging",
        "complete": True,
    }
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=(feasibility.reference,),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, reference)
    if artifact.inputs != (feasibility.reference,) or artifact.payload != payload:
        raise RuntimeError("client dependency packaging plan typed reload failed")
    return artifact


def _is_arm64_macho(path: Path) -> bool:
    header = path.read_bytes()[:8]
    if len(header) != 8 or header[:4] != bytes.fromhex("cffaedfe"):
        return False
    return int.from_bytes(header[4:8], "little") == 0x0100000C


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _ordinary_directory(path: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("client dependency directory is unavailable") from error
    if stat.S_ISLNK(info.st_mode):
        info = resolved.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("client dependency directory is unsafe")
    return resolved


def _ordinary_file(path: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("client dependency file is unavailable") from error
    if stat.S_ISLNK(info.st_mode):
        info = resolved.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("client dependency file is unsafe")
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
    "ClientDependencyPackagingPlan",
    "ClientDependencySources",
    "DependencyCopyEntry",
    "default_client_dependency_sources",
    "plan_luanti_client_dependency_packaging",
    "write_luanti_client_dependency_plan",
]
