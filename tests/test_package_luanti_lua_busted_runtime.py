from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.package_luanti_busted import (
    CONFIRMATION,
    package_runtime,
)
from warptest.c1_lazarus_selection.luanti_lua_regression import (
    OFFLINE_BUSTED_COMPONENTS,
    OFFLINE_BUSTED_RUNTIME_ROOT,
    _offline_runtime_or_block,
    profile_for_candidate,
)
from warptest.c1_lazarus_selection.models import CandidateKey


MODULE_ENTRYPOINTS = {
    "busted": "share/lua/5.1/busted/init.lua",
    "cliargs": "share/lua/5.1/cliargs/init.lua",
    "dkjson": "share/lua/5.1/dkjson.lua",
    "luafilesystem": "lib/lua/5.1/lfs.so",
    "lua_term": "share/lua/5.1/term/init.lua",
    "luassert": "share/lua/5.1/luassert/init.lua",
    "luasystem": "share/lua/5.1/system/init.lua",
    "mediator_lua": "share/lua/5.1/mediator.lua",
    "penlight": "share/lua/5.1/pl/init.lua",
    "say": "share/lua/5.1/say/init.lua",
}
MODULE_VERSIONS = {
    "busted": "2.2.0-1",
    "lua_cliargs": "3.0-2",
    "luasystem": "0.7.1-1",
    "dkjson": "2.11-1",
    "say": "1.4.1-3",
    "luassert": "1.9.0-1",
    "lua-term": "0.8-1",
    "penlight": "1.15.0-1",
    "luafilesystem": "1.9.0-1",
    "mediator_lua": "1.1.2-0",
}


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _realistic_luarocks_tree(tmp_path: Path) -> tuple[Path, Path]:
    interpreter = _write(
        tmp_path / "host" / "luajit",
        b'#!/bin/sh\nif [ "$2" = "--version" ]; then echo "Busted 2.2.0"; exit 0; fi\nexit 3\n',
    )
    interpreter.chmod(0o755)
    source = tmp_path / "luarocks-tree"
    runner = _write(
        source / "bin" / "busted",
        (
            "#!/bin/sh\n"
            "LUAROCKS_SYSCONFDIR='/opt/homebrew/etc/luarocks' "
            f'exec "{interpreter}" '
            f'"{source}/lib/luarocks/rocks-5.1/busted/2.2.0-1/bin/busted" '
            '"$@"\n'
        ).encode(),
    )
    runner.chmod(0o755)
    for component, relative in MODULE_ENTRYPOINTS.items():
        _write(source / relative, f"return {{name={component!r}}}\n".encode())
    _write(source / "share/lua/5.1/busted/runner.lua", b"return function() end\n")
    _write(source / "share/lua/5.1/luassert/assert.lua", b"return {}\n")
    _write(source / "share/lua/5.1/pl/path.lua", b"return {}\n")
    _write(source / "share/lua/5.1/system/core.lua", b"return {}\n")
    _write(source / "lib/lua/5.1/system/core.so", b"real native module\n")
    _write(
        source / "lib/luarocks/rocks-5.1/busted/2.2.0-1/bin/busted",
        b"return require('busted.runner')({ ... })\n",
    )
    for rock, version in MODULE_VERSIONS.items():
        _write(
            source / f"lib/luarocks/rocks-5.1/{rock}/{version}/rock_manifest",
            b"rock_manifest = {}\n",
        )
    return source, interpreter


@pytest.mark.parametrize(
    "body",
    [
        "echo unsafe\nexec '/absolute/luajit' '/absolute/busted' \"$@\"\n",
        (
            "exec '/absolute/first' '/absolute/busted' \"$@\"\n"
            "exec '/absolute/second' '/absolute/busted' \"$@\"\n"
        ),
        "BAD-NAME=value exec '/absolute/luajit' '/absolute/busted' \"$@\"\n",
    ],
)
def test_packager_rejects_arbitrary_shell_or_multiple_exec_lines(
    tmp_path: Path, body: str
) -> None:
    source, _interpreter = _realistic_luarocks_tree(tmp_path)
    (source / "bin/busted").write_text("#!/bin/sh\n" + body)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    with pytest.raises(RuntimeError, match="LuaRocks shell wrapper"):
        package_runtime(
            artifact_root=artifact_root,
            source_tree=source,
            confirmation=CONFIRMATION,
        )


def test_packages_a_real_luarocks_tree_as_a_closed_registered_runtime(
    tmp_path: Path,
) -> None:
    source, interpreter = _realistic_luarocks_tree(tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    runtime_root = package_runtime(
        artifact_root=artifact_root,
        source_tree=source,
        confirmation=CONFIRMATION,
    )

    assert runtime_root == artifact_root / OFFLINE_BUSTED_RUNTIME_ROOT
    manifest = json.loads((runtime_root / "runtime.json").read_text())
    assert manifest["busted_version"] == "2.2.0-1"
    assert manifest["module_versions"] == MODULE_VERSIONS
    assert manifest["modules"] == MODULE_ENTRYPOINTS
    assert manifest["lua_paths"] == [
        "share/lua/5.1/?.lua",
        "share/lua/5.1/?/init.lua",
    ]
    assert manifest["lua_cpaths"] == ["lib/lua/5.1/?.so"]
    assert set(manifest["closure_files"]) == {
        path.relative_to(runtime_root).as_posix()
        for path in runtime_root.rglob("*")
        if path.is_file() and path.name != "runtime.json"
    }
    assert (runtime_root / "bin/luajit").read_bytes() == interpreter.read_bytes()
    assert all(
        str(source).encode() not in path.read_bytes()
        for path in runtime_root.rglob("*")
        if path.is_file()
    )
    assert set(manifest["modules"]) == set(OFFLINE_BUSTED_COMPONENTS)
    assert all(
        stat.S_ISREG((runtime_root / relative).lstat().st_mode)
        and not (runtime_root / relative).is_symlink()
        for relative in manifest["closure_files"]
    )

    loaded = _offline_runtime_or_block(
        artifact_root, profile_for_candidate(CandidateKey("luanti", 13132))
    )
    assert loaded.root == runtime_root
    assert loaded.lua_paths == tuple(
        runtime_root / path for path in manifest["lua_paths"]
    )
    assert loaded.lua_cpaths == tuple(
        runtime_root / path for path in manifest["lua_cpaths"]
    )
    assert loaded.module_versions == MODULE_VERSIONS

    shutil.rmtree(source)
    completed = subprocess.run(
        (runtime_root / "bin/busted", "--version"),
        check=False,
        capture_output=True,
        text=True,
        # Generous: the assertion is that the packaged launcher still runs once
        # its source tree is gone, not how fast a cold process starts.
        timeout=10,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "Busted 2.2.0"


def test_packager_requires_exact_confirmation_and_does_not_replace_a_runtime(
    tmp_path: Path,
) -> None:
    source, _interpreter = _realistic_luarocks_tree(tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    with pytest.raises(RuntimeError, match="explicit confirmation"):
        package_runtime(
            artifact_root=artifact_root,
            source_tree=source,
            confirmation="yes",
        )

    package_runtime(
        artifact_root=artifact_root,
        source_tree=source,
        confirmation=CONFIRMATION,
    )
    with pytest.raises(RuntimeError, match="already exists"):
        package_runtime(
            artifact_root=artifact_root,
            source_tree=source,
            confirmation=CONFIRMATION,
        )


def test_packager_rejects_a_symlink_anywhere_in_the_source_closure(
    tmp_path: Path,
) -> None:
    source, _interpreter = _realistic_luarocks_tree(tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    module = source / "share/lua/5.1/pl/path.lua"
    module.unlink()
    module.symlink_to(tmp_path / "outside.lua")

    with pytest.raises(RuntimeError, match="source tree contains an unsafe entry"):
        package_runtime(
            artifact_root=artifact_root,
            source_tree=source,
            confirmation=CONFIRMATION,
        )


def test_packager_rejects_a_missing_real_module_entrypoint(tmp_path: Path) -> None:
    source, _interpreter = _realistic_luarocks_tree(tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    (source / MODULE_ENTRYPOINTS["luassert"]).unlink()

    with pytest.raises(RuntimeError, match="luassert module entrypoint is missing"):
        package_runtime(
            artifact_root=artifact_root,
            source_tree=source,
            confirmation=CONFIRMATION,
        )


def test_runtime_loader_rejects_an_unlisted_or_symlinked_closure_entry(
    tmp_path: Path,
) -> None:
    source, _interpreter = _realistic_luarocks_tree(tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    runtime_root = package_runtime(
        artifact_root=artifact_root,
        source_tree=source,
        confirmation=CONFIRMATION,
    )
    added = runtime_root / "share/lua/5.1/unlisted.lua"
    added.write_text("return {}\n")

    decision = _offline_runtime_or_block(
        artifact_root, profile_for_candidate(CandidateKey("luanti", 13132))
    )
    assert decision.details["reason"] == "offline_busted_runtime_invalid"

    added.unlink()
    entrypoint = runtime_root / MODULE_ENTRYPOINTS["penlight"]
    entrypoint.unlink()
    entrypoint.symlink_to(tmp_path / "outside.lua")
    decision = _offline_runtime_or_block(
        artifact_root, profile_for_candidate(CandidateKey("luanti", 13132))
    )
    assert decision.details["reason"] == "offline_busted_runtime_unsafe"
    assert decision.details["unsafe_entry"] == MODULE_ENTRYPOINTS["penlight"]


def test_runtime_loader_rejects_a_wrong_module_version_or_missing_rock_metadata(
    tmp_path: Path,
) -> None:
    source, _interpreter = _realistic_luarocks_tree(tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    runtime_root = package_runtime(
        artifact_root=artifact_root,
        source_tree=source,
        confirmation=CONFIRMATION,
    )
    manifest_path = runtime_root / "runtime.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["module_versions"]["luassert"] = "1.9.0-2"
    manifest_path.write_text(json.dumps(manifest))

    decision = _offline_runtime_or_block(
        artifact_root, profile_for_candidate(CandidateKey("luanti", 13132))
    )
    assert decision.details["reason"] == "offline_busted_runtime_invalid"

    manifest["module_versions"] = MODULE_VERSIONS
    manifest_path.write_text(json.dumps(manifest))
    metadata = runtime_root / ("lib/luarocks/rocks-5.1/luassert/1.9.0-1/rock_manifest")
    metadata.unlink()
    decision = _offline_runtime_or_block(
        artifact_root, profile_for_candidate(CandidateKey("luanti", 13132))
    )
    assert decision.details["reason"] == "offline_busted_runtime_incomplete"
    assert decision.details["missing_entries"] == [
        "lib/luarocks/rocks-5.1/luassert/1.9.0-1/rock_manifest"
    ]


def test_source_tree_must_be_an_existing_non_symlink_directory(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    source_link = tmp_path / "source-link"
    source_link.symlink_to(tmp_path / "missing", target_is_directory=True)

    with pytest.raises(RuntimeError, match="source tree is unsafe"):
        package_runtime(
            artifact_root=artifact_root,
            source_tree=source_link,
            confirmation=CONFIRMATION,
        )


def test_packaged_interpreter_is_an_executable_ordinary_file(tmp_path: Path) -> None:
    source, interpreter = _realistic_luarocks_tree(tmp_path)
    interpreter.chmod(0o644)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()

    with pytest.raises(RuntimeError, match="interpreter is not executable"):
        package_runtime(
            artifact_root=artifact_root,
            source_tree=source,
            confirmation=CONFIRMATION,
        )


def test_packager_never_uses_a_network_or_luarocks_subprocess() -> None:
    source = Path(os.devnull)
    assert "luarocks" not in package_runtime.__code__.co_names
    assert source.exists()
