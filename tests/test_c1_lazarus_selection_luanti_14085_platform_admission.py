from __future__ import annotations

from warptest.c1_lazarus_selection import luanti_14085_platform_admission as module


def test_14085_platform_contract_is_windows_only() -> None:
    assert module._PRODUCTION_PATH == "src/filesys.cpp"
    assert module._TEST_PATH == "src/unittest/test_filesys.cpp"
    assert module._RAW_PATHS == (
        "src/filesys.cpp",
        "src/unittest/CMakeLists.txt",
        "src/unittest/test_filesys.cpp",
    )


def test_14085_platform_contract_requires_candidate_windows_closure() -> None:
    assert set(module._RUNNER_REFERENCES) == {
        "windows_runner",
        "windows_toolchain",
        "windows_dependency_closure",
        "windows_profile",
        "fixed_capsule",
        "version_materialization",
    }
    assert not hasattr(module, "run_candidate")
    assert not hasattr(module, "build_candidate")
