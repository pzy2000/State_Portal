from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from warptest.lazarus_formal_build import (
    LUANTI_FORMAL_MULTIPLAYER_TASK_IDS,
    LUANTI_FORMAL_UNITTEST_TASK_IDS,
    create_formal_prebuild_callback,
    formal_construction_prepare_commands,
)
from warptest.lazarus_upstream import (
    FrozenUpstreamTestSource,
    UpstreamTestDefinition,
    run_lazarus_construction_test_subset,
)


def test_registered_luanti_candidates_use_the_clone_root_runtime_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    def fake_create_prebuild_callback(
        argv: tuple[str, ...],
        *,
        pre_commands: tuple[tuple[str, ...], ...],
        post_commands: tuple[tuple[str, ...], ...],
        artifact_paths: tuple[str, ...],
        timeout_s: float,
    ):
        captured.append(
            {
                "argv": argv,
                "pre_commands": pre_commands,
                "post_commands": post_commands,
                "artifact_paths": artifact_paths,
                "timeout_s": timeout_s,
            }
        )

        def callback(_version: str, _root: object) -> dict[str, str]:
            return {"status": "not-run"}

        callback.lazarus_build_fingerprint = "f" * 64
        return callback

    monkeypatch.setattr(
        "warptest.lazarus_formal_build.create_prebuild_callback",
        fake_create_prebuild_callback,
    )

    callbacks = [
        create_formal_prebuild_callback(task_id, timeout_s=321.0)
        for task_id in sorted(LUANTI_FORMAL_UNITTEST_TASK_IDS)
    ]

    expected = len(LUANTI_FORMAL_UNITTEST_TASK_IDS)
    assert [callback.lazarus_build_fingerprint for callback in callbacks] == [
        "f" * 64
    ] * expected
    assert len(captured) == expected
    for recipe in captured:
        assert recipe["pre_commands"] == (
            (
                "/opt/miniconda3/bin/cmake",
                "-S",
                ".",
                "-B",
                "build-lazarus-unittests",
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DBUILD_CLIENT=TRUE",
                "-DBUILD_SERVER=FALSE",
                "-DBUILD_UNITTESTS=TRUE",
                "-DBUILD_DOCUMENTATION=FALSE",
                "-DRUN_IN_PLACE=TRUE",
                "-DENABLE_LTO=FALSE",
                "-DBUILD_WITH_TRACY=FALSE",
                "-DENABLE_SOUND=FALSE",
                "-DENABLE_GETTEXT=FALSE",
                "-DENABLE_CURL=FALSE",
                "-DENABLE_CURSES=FALSE",
                "-DENABLE_POSTGRESQL=FALSE",
                "-DENABLE_LEVELDB=FALSE",
                "-DENABLE_REDIS=FALSE",
            ),
        )
        assert recipe["argv"] == (
            "/opt/miniconda3/bin/cmake",
            "--build",
            "build-lazarus-unittests",
            "--target",
            "luanti",
            "--parallel",
            "2",
        )
        # The in-place Luanti binary locates ``../builtin`` relative to
        # ``bin/luanti``.  Staging only the executable under the CMake build
        # directory would lose that clone-local runtime tree.
        assert recipe["post_commands"] == ()
        assert recipe["artifact_paths"] == ("bin/luanti",)
        assert recipe["timeout_s"] == 321.0


def test_registered_luanti_cpp_tests_rebuild_the_clone_root_binary_after_overlay() -> (
    None
):
    for task_id in LUANTI_FORMAL_UNITTEST_TASK_IDS:
        commands = formal_construction_prepare_commands(task_id, test_cwds=(".", "."))

        assert len(commands) == 4
        remove_cache, remove_cmake_files, reconfigure, rebuild = commands
        assert remove_cache.id == "luanti-unit-test-remove-stale-cache"
        assert remove_cache.argv == (
            "/opt/miniconda3/bin/cmake",
            "-E",
            "rm",
            "-f",
            "build-lazarus-unittests/CMakeCache.txt",
        )
        assert remove_cache.cwd == "."
        assert remove_cmake_files.id == "luanti-unit-test-remove-stale-cmake-files"
        assert remove_cmake_files.argv == (
            "/opt/miniconda3/bin/cmake",
            "-E",
            "remove_directory",
            "build-lazarus-unittests/CMakeFiles",
        )
        assert remove_cmake_files.cwd == "."
        assert reconfigure.id == "luanti-unit-test-reconfigure"
        assert reconfigure.argv == (
            "/opt/miniconda3/bin/cmake",
            "-S",
            ".",
            "-B",
            "build-lazarus-unittests",
            "-G",
            "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DBUILD_CLIENT=TRUE",
            "-DBUILD_SERVER=FALSE",
            "-DBUILD_UNITTESTS=TRUE",
            "-DBUILD_DOCUMENTATION=FALSE",
            "-DRUN_IN_PLACE=TRUE",
            "-DENABLE_LTO=FALSE",
            "-DBUILD_WITH_TRACY=FALSE",
            "-DENABLE_SOUND=FALSE",
            "-DENABLE_GETTEXT=FALSE",
            "-DENABLE_CURL=FALSE",
            "-DENABLE_CURSES=FALSE",
            "-DENABLE_POSTGRESQL=FALSE",
            "-DENABLE_LEVELDB=FALSE",
            "-DENABLE_REDIS=FALSE",
        )
        assert reconfigure.cwd == "."
        assert reconfigure.failure_class is None
        assert rebuild.id == "luanti-unit-test-rebuild"
        assert rebuild.argv == (
            "/opt/miniconda3/bin/cmake",
            "--build",
            "build-lazarus-unittests",
            "--target",
            "luanti",
            "--parallel",
            "2",
        )
        assert rebuild.cwd == "."
        assert rebuild.failure_class is None

    with pytest.raises(ValueError, match="clone root"):
        formal_construction_prepare_commands(
            "lz-luanti-14586-full-crafting-shift-move",
            test_cwds=("src/unittest",),
        )


@pytest.mark.skipif(
    not Path("/opt/miniconda3/bin/cmake").is_file(),
    reason="requires the registered local Luanti CMake toolchain",
)
def test_luanti_cpp_prepare_scrubs_a_stale_clone_cache_before_reconfigure(
    tmp_path: Path,
) -> None:
    """A clone with a removed materialization root must still build in place."""

    source = tmp_path / "source"
    source.mkdir()
    (source / "CMakeLists.txt").write_text(
        "\n".join(
            (
                "cmake_minimum_required(VERSION 3.20)",
                "project(lazarus_cache_scrub LANGUAGES NONE)",
                "add_custom_target(luanti",
                "  COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_SOURCE_DIR}/bin",
                "  COMMAND ${CMAKE_COMMAND} -E touch ${CMAKE_SOURCE_DIR}/bin/luanti",
                ")",
                "",
            )
        ),
        encoding="utf-8",
    )
    (source / "frozen-test.txt").write_text("obsolete\n", encoding="utf-8")
    stale_build = source / "build-lazarus-unittests"
    stale_build.mkdir()
    (stale_build / "CMakeCache.txt").write_text(
        "CMAKE_CACHEFILE_DIR:INTERNAL=/missing/staging/prebuilt/fixed/"
        "build-lazarus-unittests\n"
        "CMAKE_HOME_DIRECTORY:INTERNAL=/missing/staging/prebuilt/fixed\n",
        encoding="utf-8",
    )
    stale_files = stale_build / "CMakeFiles"
    stale_files.mkdir()
    (stale_files / "stale.txt").write_text("stale\n", encoding="utf-8")

    commands = formal_construction_prepare_commands(
        "lz-luanti-14586-full-crafting-shift-move", test_cwds=(".",)
    )
    direct_configure = subprocess.run(
        commands[2].argv,
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    assert direct_configure.returncode != 0
    assert "does not match" in direct_configure.stderr

    frozen = tmp_path / "frozen"
    frozen.mkdir()
    frozen_test = frozen / "frozen-test.txt"
    frozen_test.write_text("fixed\n", encoding="utf-8")
    definition = UpstreamTestDefinition(
        id="cache-scrub-proof",
        argv=(
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "raise SystemExit(0 if Path('frozen-test.txt').read_text() == 'fixed\\n' "
                "and Path('bin/luanti').is_file() "
                "and '/missing/staging/' not in "
                "Path('build-lazarus-unittests/CMakeCache.txt').read_text() else 1)"
            ),
        ),
        cwd=".",
        timeout_s=30,
        origin="upstream",
    )
    result = run_lazarus_construction_test_subset(
        source,
        {definition.id: definition},
        (definition.id,),
        sandbox_root=tmp_path / "sandboxes",
        frozen_source_root=frozen,
        frozen_sources=(
            FrozenUpstreamTestSource(
                source_path="frozen-test.txt",
                destination_path="frozen-test.txt",
                sha256=hashlib.sha256(frozen_test.read_bytes()).hexdigest(),
            ),
        ),
        prepare_commands=commands,
        total_timeout_s=120.0,
    )

    assert result.complete is True
    assert result.passed is True
    assert [entry.id for entry in result.prepare_results] == [
        "luanti-unit-test-remove-stale-cache",
        "luanti-unit-test-remove-stale-cmake-files",
        "luanti-unit-test-reconfigure",
        "luanti-unit-test-rebuild",
    ]
    assert all(entry.passed for entry in result.prepare_results)


def test_luanti_unittest_recipe_does_not_fall_back_to_an_unregistered_task() -> None:
    with pytest.raises(ValueError, match="no registered prebuild"):
        create_formal_prebuild_callback("lz-luanti-unregistered")


def test_metadata_multiplayer_recipe_uses_the_legacy_clone_local_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    def fake_create_prebuild_callback(
        argv: tuple[str, ...],
        *,
        pre_commands: tuple[tuple[str, ...], ...],
        post_commands: tuple[tuple[str, ...], ...] = (),
        artifact_paths: tuple[str, ...],
        timeout_s: float,
    ):
        captured.append(
            {
                "argv": argv,
                "pre_commands": pre_commands,
                "post_commands": post_commands,
                "artifact_paths": artifact_paths,
                "timeout_s": timeout_s,
            }
        )

        def callback(_version: str, _root: object) -> dict[str, str]:
            return {"status": "not-run"}

        callback.lazarus_build_fingerprint = "f" * 64
        return callback

    monkeypatch.setattr(
        "warptest.lazarus_formal_build.create_prebuild_callback",
        fake_create_prebuild_callback,
    )

    assert LUANTI_FORMAL_MULTIPLAYER_TASK_IDS == {
        "lz-luanti-14396-metadata-set-string-nil"
    }
    callback = create_formal_prebuild_callback(
        "lz-luanti-14396-metadata-set-string-nil", timeout_s=321.0
    )

    assert callback.lazarus_build_fingerprint == "f" * 64
    assert captured == [
        {
            "argv": (
                "/opt/miniconda3/bin/cmake",
                "--build",
                "build-lazarus-multiplayer",
                "--target",
                "luanti",
                "--parallel",
                "2",
            ),
            "pre_commands": (
                (
                    "/opt/miniconda3/bin/cmake",
                    "-S",
                    ".",
                    "-B",
                    "build-lazarus-multiplayer",
                    "-G",
                    "Ninja",
                    "-DCMAKE_BUILD_TYPE=Release",
                    "-DBUILD_CLIENT=TRUE",
                    "-DBUILD_SERVER=FALSE",
                    "-DBUILD_UNITTESTS=TRUE",
                    "-DBUILD_DOCUMENTATION=FALSE",
                    "-DRUN_IN_PLACE=TRUE",
                    "-DENABLE_LTO=FALSE",
                    "-DBUILD_WITH_TRACY=FALSE",
                    "-DENABLE_SOUND=FALSE",
                    "-DENABLE_GETTEXT=FALSE",
                    "-DENABLE_CURL=FALSE",
                    "-DENABLE_CURSES=FALSE",
                    "-DENABLE_POSTGRESQL=FALSE",
                    "-DENABLE_LEVELDB=FALSE",
                    "-DENABLE_REDIS=FALSE",
                ),
            ),
            "post_commands": (),
            "artifact_paths": ("bin/luanti",),
            "timeout_s": 321.0,
        }
    ]
