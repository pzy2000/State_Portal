"""Candidate-specific arm64 PAGEZERO build adapter for Luanti #12703."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import subprocess
from typing import Any

from .luanti_12703_materialization import load_luanti_12703_materialization
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 12703}
_PAIR_ROOT = "private/luanti-12703-formal-materialization"
_DEPENDENCY = "private/dependencies/luanti-12703-irrlicht/1.9.0mt7/build/cmake"
_BUILD_NAME = "build-app-pagezero-v1"
_PAGEZERO = 0x100000000
_LINKER_FLAGS = "-liconv -Wl,-pagezero_size,0x100000000"
_BUILDS = {
    variant: ArtifactReference(
        f"candidate_gate8_12703_pagezero_build/luanti/12703/{variant}/v1",
        "candidate_gate8_12703_pagezero_build",
        "1.0",
    )
    for variant in ("fixed", "buggy")
}
MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/12703/fixed-buggy-pagezero/v2",
    "candidate_materialization",
    "2.0",
)


def build_luanti_12703_pagezero_pair(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Build and register a fresh fixed/buggy app pair with the arm64 adapter."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    existing = _try_load(root, MATERIALIZATION)
    if existing is not None:
        return load_luanti_12703_pagezero_materialization(root)
    base = load_luanti_12703_materialization(root)
    builds = tuple(
        _build_variant(root, variant, base, authorization)
        for variant in ("fixed", "buggy")
    )
    payload = {
        "candidate": dict(_CANDIDATE),
        "state": "built",
        "gate8_status": "passed_complete_fixed_buggy_pagezero_materialization",
        "fixed_tree_path": f"{_PAIR_ROOT}/fixed",
        "buggy_tree_path": f"{_PAIR_ROOT}/buggy",
        "fixed_app_path": f"{_PAIR_ROOT}/fixed/{_BUILD_NAME}/out/minetest.app",
        "buggy_app_path": f"{_PAIR_ROOT}/buggy/{_BUILD_NAME}/out/minetest.app",
        "build_receipts": [item.reference.artifact_id for item in builds],
        "source_materialization": base.reference.artifact_id,
        "production_only_difference": "builtin/mainmenu/pkgmgr.lua",
        "linker_adapter": _LINKER_FLAGS,
        "pagezero_vmsize": _PAGEZERO,
        "source_or_test_files_modified": False,
        "adapter_scope": "link_only_outside_production_patch",
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }
    return _write_once(
        root,
        MATERIALIZATION,
        (base.reference, *(item.reference for item in builds)),
        payload,
        authorization,
    )


def load_luanti_12703_pagezero_materialization(
    artifact_root: str | Path,
) -> StableArtifact:
    root = Path(artifact_root).absolute().resolve(strict=True)
    artifact = _load(root, MATERIALIZATION)
    if not (
        artifact.payload.get("candidate") == _CANDIDATE
        and artifact.payload.get("state") == "built"
        and artifact.payload.get("gate8_status")
        == "passed_complete_fixed_buggy_pagezero_materialization"
        and artifact.payload.get("pagezero_vmsize") == _PAGEZERO
        and artifact.payload.get("source_or_test_files_modified") is False
        and artifact.payload.get("old_attempts_loaded") is False
        and artifact.payload.get("paid_actions_authorized") is False
        and artifact.payload.get("formal_trials_authorized") is False
        and artifact.payload.get("complete") is True
    ):
        raise RuntimeError("#12703 PAGEZERO materialization is invalid")
    for reference in artifact.inputs[1:]:
        build = _load(root, reference)
        if not (
            build.payload.get("state") == "passed"
            and build.payload.get("pagezero_vmsize") == _PAGEZERO
            and build.payload.get("version_exit_code") == 0
            and build.payload.get("build_parallelism") == 1
            and build.payload.get("source_or_test_files_modified") is False
        ):
            raise RuntimeError("#12703 PAGEZERO build receipt is invalid")
        _validate_app(root, str(build.payload["application_path"]))
    return artifact


def _build_variant(
    root: Path,
    variant: str,
    base: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = _BUILDS[variant]
    existing = _try_load(root, reference)
    if existing is not None:
        return existing
    tree = _ordinary_directory(root / _PAIR_ROOT / variant, root)
    build = tree / _BUILD_NAME
    output = build / "out"
    dependency = _ordinary_directory(root / _DEPENDENCY, root)
    configure = (
        "cmake",
        "-S",
        str(tree),
        "-B",
        str(build),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_CLIENT=TRUE",
        "-DBUILD_SERVER=FALSE",
        "-DBUILD_UNITTESTS=FALSE",
        "-DENABLE_GETTEXT=FALSE",
        "-DENABLE_SOUND=FALSE",
        "-DENABLE_CURSES=FALSE",
        "-DRUN_IN_PLACE=FALSE",
        f"-DIrrlichtMt_DIR={dependency}",
        f"-DCMAKE_INSTALL_PREFIX={output}",
        f"-DCMAKE_EXE_LINKER_FLAGS={_LINKER_FLAGS}",
    )
    _run(configure, tree, timeout=300)
    _run(("cmake", "--build", str(build), "--parallel", "1"), tree, timeout=3600)
    _run(("cmake", "--install", str(build)), tree, timeout=300)
    app = _ordinary_directory(output / "minetest.app", root)
    binary = _ordinary_file(app / "Contents/MacOS/minetest", app)
    source_product = _ordinary_file(tree / "builtin/mainmenu/pkgmgr.lua", tree)
    app_product = _ordinary_file(
        app / "Contents/Resources/builtin/mainmenu/pkgmgr.lua", app
    )
    if source_product.read_bytes() != app_product.read_bytes():
        raise RuntimeError(f"#12703 {variant} app production resource drifted")
    pagezero = _pagezero(binary)
    if pagezero != _PAGEZERO:
        raise RuntimeError(f"#12703 {variant} PAGEZERO adapter was not linked")
    unsigned_binary_fingerprint = _sha256(binary)
    unsigned_text_fingerprint = _text_sha256(binary)
    _run(
        (
            "codesign",
            "--force",
            "--deep",
            "--sign",
            "-",
            "--timestamp=none",
            str(app),
        ),
        tree,
        timeout=300,
    )
    _run(
        ("codesign", "--verify", "--deep", "--strict", str(app)),
        tree,
        timeout=300,
    )
    signed_text_fingerprint = _text_sha256(binary)
    if signed_text_fingerprint != unsigned_text_fingerprint:
        raise RuntimeError(f"#12703 {variant} app signing changed __TEXT,__text")
    code_resources = _ordinary_file(
        app / "Contents/_CodeSignature/CodeResources", app
    )
    version = subprocess.run(
        (str(binary), "--version"),
        cwd=tree,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
    )
    if version.returncode != 0:
        raise RuntimeError(
            f"#12703 {variant} PAGEZERO binary --version failed rc={version.returncode}"
        )
    cache = _ordinary_file(build / "CMakeCache.txt", root).read_text(
        encoding="utf-8", errors="replace"
    )
    if f"CMAKE_EXE_LINKER_FLAGS:STRING={_LINKER_FLAGS}" not in cache:
        raise RuntimeError(f"#12703 {variant} linker adapter missing from CMake cache")
    payload: dict[str, Any] = {
        "candidate": dict(_CANDIDATE),
        "variant": variant,
        "state": "passed",
        "tree_path": tree.relative_to(root).as_posix(),
        "build_path": build.relative_to(root).as_posix(),
        "application_path": app.relative_to(root).as_posix(),
        "binary_path": binary.relative_to(root).as_posix(),
        "binary_fingerprint": _sha256(binary),
        "unsigned_binary_fingerprint": unsigned_binary_fingerprint,
        "unsigned_text_fingerprint": unsigned_text_fingerprint,
        "text_fingerprint": signed_text_fingerprint,
        "text_hash_unchanged_by_bundle_signing": True,
        "code_resources_fingerprint": _sha256(code_resources),
        "bundle_adhoc_signed": True,
        "pagezero_vmsize": pagezero,
        "pagezero_hex": hex(pagezero),
        "linker_adapter": _LINKER_FLAGS,
        "linker_adapter_in_cmake_cache": True,
        "version_exit_code": version.returncode,
        "version_stdout": version.stdout.decode("utf-8", errors="replace").strip(),
        "version_stderr": version.stderr.decode("utf-8", errors="replace").strip(),
        "build_parallelism": 1,
        "source_or_test_files_modified": False,
        "adapter_scope": "link_only_outside_production_patch",
        "source_materialization": base.reference.artifact_id,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
    }
    return _write_once(root, reference, (base.reference,), payload, authorization)


def _validate_app(root: Path, relative: str) -> None:
    app = _ordinary_directory(root / PurePosixPath(relative), root)
    binary = _ordinary_file(app / "Contents/MacOS/minetest", app)
    if _pagezero(binary) != _PAGEZERO:
        raise RuntimeError("#12703 PAGEZERO app changed")


def _pagezero(path: Path) -> int:
    raw = path.read_bytes()
    if raw[:4] != b"\xcf\xfa\xed\xfe":
        raise RuntimeError("#12703 executable is not arm64 Mach-O64")
    count = struct.unpack_from("<I", raw, 16)[0]
    offset = 32
    for _ in range(count):
        command, size = struct.unpack_from("<II", raw, offset)
        if size < 8 or offset + size > len(raw):
            raise RuntimeError("#12703 Mach-O load commands are invalid")
        if command == 0x19:
            name = raw[offset + 8 : offset + 24].split(b"\0", 1)[0]
            if name == b"__PAGEZERO":
                return struct.unpack_from("<Q", raw, offset + 32)[0]
        offset += size
    raise RuntimeError("#12703 executable lacks __PAGEZERO")


def _text_sha256(path: Path) -> str:
    raw = path.read_bytes()
    count = struct.unpack_from("<I", raw, 16)[0]
    offset = 32
    for _ in range(count):
        command, size = struct.unpack_from("<II", raw, offset)
        if command == 0x19 and size >= 72:
            sections = struct.unpack_from("<I", raw, offset + 64)[0]
            for index in range(sections):
                current = offset + 72 + index * 80
                name = raw[current : current + 16].split(b"\0", 1)[0]
                segment = raw[current + 16 : current + 32].split(b"\0", 1)[0]
                if name == b"__text" and segment == b"__TEXT":
                    length = struct.unpack_from("<Q", raw, current + 40)[0]
                    start = struct.unpack_from("<I", raw, current + 48)[0]
                    return hashlib.sha256(raw[start : start + length]).hexdigest()
        offset += size
    raise RuntimeError("#12703 executable lacks __TEXT,__text")


def _run(argv: tuple[str, ...], cwd: Path, *, timeout: int) -> None:
    result = subprocess.run(
        argv,
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).decode("utf-8", errors="replace")[-4000:]
        raise RuntimeError(f"#12703 command failed rc={result.returncode}: {tail}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ordinary_directory(path: Path, root: Path) -> Path:
    info = path.lstat()
    resolved = path.resolve(strict=True)
    resolved.relative_to(root.resolve(strict=True))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("#12703 directory is unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    info = path.lstat()
    resolved = path.resolve(strict=True)
    resolved.relative_to(root.resolve(strict=True))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("#12703 file is unsafe")
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


def _write_once(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, Any],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    existing = _try_load(root, reference)
    if existing is not None:
        if existing.inputs != inputs or existing.payload != payload:
            raise RuntimeError(f"#12703 artifact drifted: {reference.artifact_id}")
        return existing
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=datetime.now(UTC).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    return _load(root, reference)


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if not (
        type(authorization) is AuthorizationRecord
        and authorization.scope == "preannotation-execution"
        and not authorization.revoked
        and authorization.local_user_id == os.getuid()
    ):
        raise PermissionError("preannotation execution authorization is required")


__all__ = [
    "MATERIALIZATION",
    "build_luanti_12703_pagezero_pair",
    "load_luanti_12703_pagezero_materialization",
]
