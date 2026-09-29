"""Package an existing LuaRocks Busted tree for closed offline execution."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import shlex
import stat
import tempfile
import re
from pathlib import Path, PurePosixPath
from typing import Sequence

from .luanti_lua_regression import (
    OFFLINE_BUSTED_COMPONENTS,
    OFFLINE_BUSTED_DEPENDENCY_ID,
    OFFLINE_BUSTED_MODULE_VERSIONS,
    OFFLINE_BUSTED_RUNTIME_ROOT,
)


CONFIRMATION = "package-luanti-lua-busted-1.0"
_BUSTED_VERSION = "2.2.0-1"
_RUNNER = "bin/busted"
_INTERPRETER = "bin/luajit"
_LUA_PATHS = (
    "share/lua/5.1/?.lua",
    "share/lua/5.1/?/init.lua",
)
_LUA_CPATHS = ("lib/lua/5.1/?.so",)
_MODULE_CANDIDATES = {
    "busted": ("share/lua/5.1/busted/init.lua",),
    "cliargs": (
        "share/lua/5.1/cliargs.lua",
        "share/lua/5.1/cliargs/init.lua",
    ),
    "dkjson": ("share/lua/5.1/dkjson.lua",),
    "luafilesystem": ("lib/lua/5.1/lfs.so",),
    "lua_term": (
        "share/lua/5.1/term.lua",
        "share/lua/5.1/term/init.lua",
    ),
    "luassert": (
        "share/lua/5.1/luassert.lua",
        "share/lua/5.1/luassert/init.lua",
    ),
    "luasystem": (
        "share/lua/5.1/system.lua",
        "share/lua/5.1/system/init.lua",
    ),
    "mediator_lua": ("share/lua/5.1/mediator.lua",),
    "penlight": ("share/lua/5.1/pl/init.lua",),
    "say": (
        "share/lua/5.1/say.lua",
        "share/lua/5.1/say/init.lua",
    ),
}
MODULE_VERSIONS = OFFLINE_BUSTED_MODULE_VERSIONS
_SHELL_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)


def _ordinary_directory(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(f"{label} is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"{label} is unsafe")
    return path.resolve(strict=True)


def _regular_source_files(source: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    pending = [source]
    while pending:
        directory = pending.pop()
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError(
                    f"source tree contains an unsafe entry: "
                    f"{entry.relative_to(source).as_posix()}"
                )
            if stat.S_ISDIR(info.st_mode):
                pending.append(entry)
            elif stat.S_ISREG(info.st_mode):
                files.append(entry)
            else:
                raise RuntimeError(
                    f"source tree contains an unsafe entry: "
                    f"{entry.relative_to(source).as_posix()}"
                )
    return tuple(sorted(files, key=lambda item: item.relative_to(source).as_posix()))


def _module_entrypoints(source: Path) -> dict[str, str]:
    modules: dict[str, str] = {}
    for component in OFFLINE_BUSTED_COMPONENTS:
        candidates = _MODULE_CANDIDATES[component]
        matches = [relative for relative in candidates if (source / relative).is_file()]
        if len(matches) != 1:
            condition = "missing" if not matches else "ambiguous"
            raise RuntimeError(f"{component} module entrypoint is {condition}")
        modules[component] = matches[0]
    return modules


def _source_interpreter(source: Path) -> Path:
    runner = source / _RUNNER
    try:
        info = runner.lstat()
    except FileNotFoundError as error:
        raise RuntimeError("Busted runner is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("Busted runner is unsafe")
    if not os.access(runner, os.X_OK):
        raise RuntimeError("Busted runner is not executable")
    try:
        with runner.open("rb") as stream:
            lines = stream.read(65536).decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError("Busted runner is invalid") from error
    if not lines or lines[0] != "#!/bin/sh":
        raise RuntimeError("Busted runner must be a LuaRocks shell wrapper")
    body = [line for line in lines[1:] if line.strip()]
    if len(body) != 1:
        raise RuntimeError("Busted runner must be a simple LuaRocks shell wrapper")
    try:
        words = shlex.split(body[0])
    except ValueError as error:
        raise RuntimeError("Busted runner is invalid") from error
    exec_positions = [index for index, word in enumerate(words) if word == "exec"]
    if len(exec_positions) != 1:
        raise RuntimeError("Busted runner must be a simple LuaRocks shell wrapper")
    exec_index = exec_positions[0]
    if (
        any(not _SHELL_ASSIGNMENT.fullmatch(word) for word in words[:exec_index])
        or len(words) < exec_index + 3
    ):
        raise RuntimeError("Busted runner must be a simple LuaRocks shell wrapper")
    interpreter = Path(words[exec_index + 1])
    if not interpreter.is_absolute():
        raise RuntimeError("Busted runner must name one absolute interpreter")
    try:
        interpreter = interpreter.resolve(strict=True)
        interpreter_info = interpreter.lstat()
    except FileNotFoundError as error:
        raise RuntimeError("Busted interpreter is missing") from error
    if stat.S_ISLNK(interpreter_info.st_mode) or not stat.S_ISREG(
        interpreter_info.st_mode
    ):
        raise RuntimeError("Busted interpreter is unsafe")
    if not os.access(interpreter, os.X_OK):
        raise RuntimeError("Busted interpreter is not executable")
    return interpreter


def _relocatable_runner() -> bytes:
    lua_path = ";".join(f"${{root}}/{path}" for path in _LUA_PATHS) + ";;"
    lua_cpath = ";".join(f"${{root}}/{path}" for path in _LUA_CPATHS) + ";;"
    rock_runner = "lib/luarocks/rocks-5.1/busted/2.2.0-1/bin/busted"
    return (
        "#!/bin/sh\n"
        'case "$0" in\n'
        "  /*) self=$0 ;;\n"
        "  *) self=$PWD/$0 ;;\n"
        "esac\n"
        "root=${self%/*}/..\n"
        f'LUA_PATH="{lua_path}"\n'
        f'LUA_CPATH="{lua_cpath}"\n'
        "export LUA_PATH LUA_CPATH\n"
        f'exec "$root/{_INTERPRETER}" "$root/{rock_runner}" "$@"\n'
    ).encode("utf-8")


def _ensure_destination_parent(root: Path) -> Path:
    current = root
    for part in PurePosixPath(OFFLINE_BUSTED_RUNTIME_ROOT).parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir()
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("runtime destination parent is unsafe")
    return current


def package_runtime(
    *,
    artifact_root: str | Path,
    source_tree: str | Path,
    confirmation: str,
) -> Path:
    """Copy and validate one pre-installed tree without invoking a network tool."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    root = _ordinary_directory(Path(artifact_root).absolute(), "artifact root")
    source = _ordinary_directory(Path(source_tree).absolute(), "source tree")
    if root == source or root.is_relative_to(source) or source.is_relative_to(root):
        raise RuntimeError("source tree and artifact root must be separate")
    source_files = _regular_source_files(source)
    for rock, version in MODULE_VERSIONS.items():
        metadata = source / "lib/luarocks/rocks-5.1" / rock / version / "rock_manifest"
        if metadata not in source_files:
            raise RuntimeError(f"{rock} {version} rock metadata is missing")
    modules = _module_entrypoints(source)
    interpreter = _source_interpreter(source)
    parent = _ensure_destination_parent(root)
    destination = root / OFFLINE_BUSTED_RUNTIME_ROOT
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("registered runtime already exists")

    with tempfile.TemporaryDirectory(prefix=".luanti-busted-", dir=parent) as temporary:
        staging = Path(temporary)
        closure: list[str] = []
        for source_file in source_files:
            relative = source_file.relative_to(source)
            if relative.as_posix() == _RUNNER:
                continue
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, target, follow_symlinks=False)
            target.chmod(stat.S_IMODE(source_file.lstat().st_mode))
            closure.append(relative.as_posix())
        interpreter_target = staging / _INTERPRETER
        if interpreter_target.exists():
            raise RuntimeError("source tree collides with packaged interpreter path")
        interpreter_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(interpreter, interpreter_target, follow_symlinks=False)
        interpreter_target.chmod(stat.S_IMODE(interpreter.lstat().st_mode))
        closure.append(_INTERPRETER)
        runner_target = staging / _RUNNER
        runner_target.write_bytes(_relocatable_runner())
        runner_target.chmod(0o755)
        closure.append(_RUNNER)
        manifest = {
            "dependency_id": OFFLINE_BUSTED_DEPENDENCY_ID,
            "semantic_version": "1.0",
            "busted_version": _BUSTED_VERSION,
            "lua_abi": "5.1",
            "runner": _RUNNER,
            "interpreter": _INTERPRETER,
            "modules": modules,
            "module_versions": MODULE_VERSIONS,
            "lua_paths": list(_LUA_PATHS),
            "lua_cpaths": list(_LUA_CPATHS),
            "closure_files": sorted(closure),
        }
        (staging / "runtime.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(staging, destination)
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Package a pre-installed LuaRocks Busted tree for offline use."
    )
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--source-tree", required=True, type=Path)
    parser.add_argument("--confirm", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        destination = package_runtime(
            artifact_root=args.artifact_root,
            source_tree=args.source_tree,
            confirmation=args.confirm,
        )
    except RuntimeError as error:
        _parser().error(str(error))
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
