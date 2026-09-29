"""Package the registered host-pinned Luanti native toolchain without network."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .luanti_native_regression import (
    NATIVE_TOOLCHAIN_DEPENDENCY_ID,
    NATIVE_TOOLCHAIN_ROOT,
)


CONFIRMATION = "PACKAGE_LUANTI_NATIVE_TOOLCHAIN_3.0"
_MINIMUM_FREE_BYTES = 50 * 1024**3
_CLOSURE = (
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
)


@dataclass(frozen=True)
class NativeToolchainSources:
    git: Path
    cmake: Path
    ninja: Path
    c_compiler: Path
    cxx_compiler: Path
    archiver: Path
    ranlib: Path
    linker: Path
    symbol_tool: Path
    strip_tool: Path
    object_inspector: Path
    install_name_tool: Path
    iconv_header: Path
    iconv_library: Path
    sqlite_header: Path
    sqlite_library: Path
    zlib_header: Path
    zlib_config_header: Path
    zlib_library: Path
    zstd_header: Path
    zstd_errors_header: Path
    zstd_library: Path


def default_native_toolchain_sources() -> NativeToolchainSources:
    """Resolve the audited Darwin/arm64 host inputs to ordinary source files."""

    sdk = Path("/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/usr")
    sqlite = Path("/opt/homebrew/opt/sqlite").resolve(strict=True)
    zstd = Path("/opt/homebrew/opt/zstd").resolve(strict=True)
    return NativeToolchainSources(
        git=Path("/usr/bin/git"),
        cmake=Path("/opt/miniconda3/bin/cmake"),
        ninja=Path("/opt/miniconda3/bin/ninja"),
        c_compiler=Path("/usr/bin/clang"),
        cxx_compiler=Path("/usr/bin/clang++"),
        archiver=Path("/usr/bin/ar"),
        ranlib=Path("/usr/bin/ranlib"),
        linker=Path("/usr/bin/ld"),
        symbol_tool=Path("/usr/bin/nm"),
        strip_tool=Path("/usr/bin/strip"),
        object_inspector=Path("/usr/bin/otool"),
        install_name_tool=Path("/usr/bin/install_name_tool"),
        iconv_header=sdk / "include/iconv.h",
        iconv_library=sdk / "lib/libiconv.2.tbd",
        sqlite_header=sqlite / "include/sqlite3.h",
        sqlite_library=sqlite / "lib/libsqlite3.a",
        zlib_header=Path("/opt/miniconda3/include/zlib.h"),
        zlib_config_header=Path("/opt/miniconda3/include/zconf.h"),
        zlib_library=Path("/opt/miniconda3/lib/libz.a"),
        zstd_header=zstd / "include/zstd.h",
        zstd_errors_header=zstd / "include/zstd_errors.h",
        zstd_library=zstd / "lib/libzstd.a",
    )


def package_luanti_native_toolchain(
    *,
    artifact_root: str | Path,
    confirmation: str,
    sources: NativeToolchainSources | None = None,
) -> Path:
    """Create the exact 3.0 runtime once, using only audited local inputs."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    root = _ordinary_root(Path(artifact_root).absolute())
    if shutil.disk_usage(root).free < _MINIMUM_FREE_BYTES:
        raise RuntimeError("native toolchain packaging requires at least 50 GiB free")
    selected = sources or default_native_toolchain_sources()
    if type(selected) is not NativeToolchainSources:
        raise ValueError("native toolchain sources are invalid")
    _validate_sources(selected)

    relative = PurePosixPath(NATIVE_TOOLCHAIN_ROOT)
    parent = _ordinary_directory(
        root,
        PurePosixPath(*relative.parts[:-1]),
        create=True,
    )
    destination = parent / relative.name
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("native toolchain destination already exists")

    staging = Path(tempfile.mkdtemp(prefix=".luanti-native-", dir=parent))
    try:
        tools = {
            "bin/git": selected.git,
            "bin/cmake": selected.cmake,
            "bin/ninja": selected.ninja,
            "bin/cc": selected.c_compiler,
            "bin/c++": selected.cxx_compiler,
            "bin/ar": selected.archiver,
            "bin/ranlib": selected.ranlib,
            "bin/ld": selected.linker,
            "bin/nm": selected.symbol_tool,
            "bin/strip": selected.strip_tool,
            "bin/otool": selected.object_inspector,
            "bin/install_name_tool": selected.install_name_tool,
        }
        for target, source in tools.items():
            _write_new(
                staging / target,
                _wrapper(source.resolve(strict=True)),
                executable=True,
            )
        files = {
            "packages/sqlite3/include/sqlite3.h": selected.sqlite_header,
            "packages/sqlite3/lib/libsqlite3.a": selected.sqlite_library,
            "packages/zlib/include/zlib.h": selected.zlib_header,
            "packages/zlib/include/zconf.h": selected.zlib_config_header,
            "packages/zlib/lib/libz.a": selected.zlib_library,
            "packages/zstd/include/zstd.h": selected.zstd_header,
            "packages/zstd/include/zstd_errors.h": selected.zstd_errors_header,
            "packages/zstd/lib/libzstd.a": selected.zstd_library,
            "packages/iconv/include/iconv.h": selected.iconv_header,
            "packages/iconv/lib/libiconv.tbd": selected.iconv_library,
        }
        for target, source in files.items():
            _copy_new(source, staging / target)
        manifest = {
            "dependency_id": NATIVE_TOOLCHAIN_DEPENDENCY_ID,
            "semantic_version": "3.0",
            "git": "bin/git",
            "cmake": "bin/cmake",
            "ninja": "bin/ninja",
            "c_compiler": "bin/cc",
            "cxx_compiler": "bin/c++",
            "archiver": "bin/ar",
            "ranlib": "bin/ranlib",
            "linker": "bin/ld",
            "symbol_tool": "bin/nm",
            "strip_tool": "bin/strip",
            "object_inspector": "bin/otool",
            "install_name_tool": "bin/install_name_tool",
            "package_roots": {
                "sqlite3": "packages/sqlite3",
                "zlib": "packages/zlib",
                "zstd": "packages/zstd",
                "iconv": "packages/iconv",
            },
            "closure_files": list(_CLOSURE),
        }
        _write_new(
            staging / "runtime.json",
            (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode(),
            executable=False,
        )
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("native toolchain destination already exists")
        os.replace(staging, destination)
    finally:
        if staging.exists():
            _remove_tree(staging)
    return destination


def _validate_sources(sources: NativeToolchainSources) -> None:
    tools = {
        sources.git,
        sources.cmake,
        sources.ninja,
        sources.c_compiler,
        sources.cxx_compiler,
        sources.archiver,
        sources.ranlib,
        sources.linker,
        sources.symbol_tool,
        sources.strip_tool,
        sources.object_inspector,
        sources.install_name_tool,
    }
    for path in sources.__dict__.values():
        if not isinstance(path, Path) or not path.is_absolute():
            raise RuntimeError("native toolchain source is unsafe")
        try:
            info = path.lstat()
        except FileNotFoundError as error:
            raise RuntimeError("native toolchain source is missing") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError("native toolchain source is unsafe")
        if path in tools and not os.access(path, os.X_OK):
            raise RuntimeError("native toolchain source is not executable")


def _wrapper(source: Path) -> bytes:
    value = str(source)
    if "'" in value or "\n" in value or "\r" in value:
        raise RuntimeError("native toolchain source path is unsafe")
    return f"#!/bin/sh\nexec '{value}' \"$@\"\n".encode()


def _write_new(path: Path, content: bytes, *, executable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o700 if executable else 0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _copy_new(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output:
            shutil.copyfileobj(input_handle, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _ordinary_root(root: Path) -> Path:
    try:
        info = root.lstat()
        resolved = root.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return resolved


def _ordinary_directory(
    root: Path,
    relative: PurePosixPath,
    *,
    create: bool,
) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                raise RuntimeError("registered directory is unavailable")
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("registered directory is unsafe")
    return current


def _remove_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            path.rmdir()
    root.rmdir()


__all__ = [
    "CONFIRMATION",
    "NativeToolchainSources",
    "default_native_toolchain_sources",
    "package_luanti_native_toolchain",
]
