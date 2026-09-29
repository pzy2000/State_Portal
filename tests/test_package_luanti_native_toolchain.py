from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from warptest.c1_lazarus_selection.luanti_native_regression import (
    NATIVE_TOOLCHAIN_DEPENDENCY_ID,
    NATIVE_TOOLCHAIN_ROOT,
)
from warptest.c1_lazarus_selection.package_luanti_native_toolchain import (
    CONFIRMATION,
    NativeToolchainSources,
    package_luanti_native_toolchain,
)


@pytest.fixture(autouse=True)
def _ample_packaging_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tiny packaging fixtures independent of hosted-runner disk size."""

    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.package_luanti_native_toolchain.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=60 * 1024**3),
    )


def _write(path: Path, content: bytes, *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o700 if executable else 0o600)
    return path


def _sources(root: Path) -> NativeToolchainSources:
    return NativeToolchainSources(
        git=_write(root / "tools/git", b"git\n", executable=True),
        cmake=_write(root / "tools/cmake", b"cmake\n", executable=True),
        ninja=_write(root / "tools/ninja", b"ninja\n", executable=True),
        c_compiler=_write(root / "tools/clang", b"cc\n", executable=True),
        cxx_compiler=_write(root / "tools/clangxx", b"cxx\n", executable=True),
        archiver=_write(root / "tools/ar", b"ar\n", executable=True),
        ranlib=_write(root / "tools/ranlib", b"ranlib\n", executable=True),
        linker=_write(root / "tools/ld", b"ld\n", executable=True),
        symbol_tool=_write(root / "tools/nm", b"nm\n", executable=True),
        strip_tool=_write(root / "tools/strip", b"strip\n", executable=True),
        object_inspector=_write(root / "tools/otool", b"otool\n", executable=True),
        install_name_tool=_write(
            root / "tools/install_name_tool", b"install-name\n", executable=True
        ),
        iconv_header=_write(root / "packages/iconv/iconv.h", b"iconv h\n"),
        iconv_library=_write(root / "packages/iconv/libiconv.tbd", b"iconv tbd\n"),
        sqlite_header=_write(root / "packages/sqlite/sqlite3.h", b"sqlite h\n"),
        sqlite_library=_write(root / "packages/sqlite/libsqlite3.a", b"sqlite a\n"),
        zlib_header=_write(root / "packages/zlib/zlib.h", b"zlib h\n"),
        zlib_config_header=_write(root / "packages/zlib/zconf.h", b"zconf h\n"),
        zlib_library=_write(root / "packages/zlib/libz.a", b"zlib a\n"),
        zstd_header=_write(root / "packages/zstd/zstd.h", b"zstd h\n"),
        zstd_errors_header=_write(
            root / "packages/zstd/zstd_errors.h", b"zstd errors h\n"
        ),
        zstd_library=_write(root / "packages/zstd/libzstd.a", b"zstd a\n"),
    )


def test_packager_writes_closed_wrappers_and_static_package_inputs(
    tmp_path: Path,
) -> None:
    sources = _sources(tmp_path / "source")
    root = tmp_path / "artifact-root"
    root.mkdir()

    runtime = package_luanti_native_toolchain(
        artifact_root=root,
        confirmation=CONFIRMATION,
        sources=sources,
    )

    assert runtime == root / NATIVE_TOOLCHAIN_ROOT
    manifest = json.loads((runtime / "runtime.json").read_text())
    assert manifest["dependency_id"] == NATIVE_TOOLCHAIN_DEPENDENCY_ID
    assert manifest["semantic_version"] == "3.0"
    assert set(manifest["closure_files"]) == {
        "bin/git",
        "bin/cmake",
        "bin/ninja",
        "bin/cc",
        "bin/c++",
        "bin/ar",
        "bin/ranlib",
        "bin/ld",
        "bin/nm",
        "bin/strip",
        "bin/otool",
        "bin/install_name_tool",
        "packages/sqlite3/include/sqlite3.h",
        "packages/sqlite3/lib/libsqlite3.a",
        "packages/zlib/include/zlib.h",
        "packages/zlib/include/zconf.h",
        "packages/zlib/lib/libz.a",
        "packages/zstd/include/zstd.h",
        "packages/zstd/include/zstd_errors.h",
        "packages/zstd/lib/libzstd.a",
        "packages/iconv/include/iconv.h",
        "packages/iconv/lib/libiconv.tbd",
    }
    for name, source in {
        "git": sources.git,
        "cmake": sources.cmake,
        "ninja": sources.ninja,
        "cc": sources.c_compiler,
        "c++": sources.cxx_compiler,
        "ar": sources.archiver,
        "ranlib": sources.ranlib,
        "ld": sources.linker,
        "nm": sources.symbol_tool,
        "strip": sources.strip_tool,
        "otool": sources.object_inspector,
        "install_name_tool": sources.install_name_tool,
    }.items():
        wrapper = runtime / "bin" / name
        assert wrapper.stat().st_mode & 0o111
        assert wrapper.read_text() == f"#!/bin/sh\nexec '{source.resolve()}' \"$@\"\n"
    assert (runtime / "packages/sqlite3/lib/libsqlite3.a").read_bytes() == b"sqlite a\n"
    assert (runtime / "packages/zlib/lib/libz.a").read_bytes() == b"zlib a\n"
    assert (runtime / "packages/zstd/lib/libzstd.a").read_bytes() == b"zstd a\n"
    assert not any(path.is_symlink() for path in runtime.rglob("*"))


def test_packager_requires_confirmation_and_refuses_existing_destination(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifact-root"
    root.mkdir()
    sources = _sources(tmp_path / "source")
    with pytest.raises(RuntimeError, match="explicit confirmation"):
        package_luanti_native_toolchain(
            artifact_root=root,
            confirmation="wrong",
            sources=sources,
        )
    destination = root / NATIVE_TOOLCHAIN_ROOT
    destination.mkdir(parents=True)
    _write(destination / "foreign", b"do not overwrite\n")
    with pytest.raises(RuntimeError, match="destination already exists"):
        package_luanti_native_toolchain(
            artifact_root=root,
            confirmation=CONFIRMATION,
            sources=sources,
        )
    assert (destination / "foreign").read_bytes() == b"do not overwrite\n"


def test_packager_preserves_existing_versions_when_creating_immutable_3_0(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifact-root"
    root.mkdir()
    old = root / "private/dependencies/luanti-native/1.0/runtime.json"
    _write(old, b'{"semantic_version":"1.0"}\n')
    old_v2 = root / "private/dependencies/luanti-native/2.0/runtime.json"
    _write(old_v2, b'{"semantic_version":"2.0"}\n')

    runtime = package_luanti_native_toolchain(
        artifact_root=root,
        confirmation=CONFIRMATION,
        sources=_sources(tmp_path / "source"),
    )

    assert runtime == root / "private/dependencies/luanti-native/3.0"
    assert old.read_bytes() == b'{"semantic_version":"1.0"}\n'
    assert old_v2.read_bytes() == b'{"semantic_version":"2.0"}\n'


def test_packager_rejects_symlinked_sources_and_low_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifact-root"
    root.mkdir()
    sources = _sources(tmp_path / "source")
    linked = tmp_path / "source/tools/linked-cmake"
    linked.symlink_to(sources.cmake)
    bad = NativeToolchainSources(
        **{**sources.__dict__, "cmake": linked},
    )
    with pytest.raises(RuntimeError, match="source is unsafe"):
        package_luanti_native_toolchain(
            artifact_root=root,
            confirmation=CONFIRMATION,
            sources=bad,
        )

    monkeypatch.setattr(
        "warptest.c1_lazarus_selection.package_luanti_native_toolchain.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=49 * 1024**3),
    )
    with pytest.raises(RuntimeError, match="at least 50 GiB"):
        package_luanti_native_toolchain(
            artifact_root=root,
            confirmation=CONFIRMATION,
            sources=sources,
        )
