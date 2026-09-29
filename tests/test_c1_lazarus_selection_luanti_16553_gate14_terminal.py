from __future__ import annotations

from warptest.c1_lazarus_selection import luanti_16553_gate14_terminal as module


def test_16553_gate14_contract_exposes_fixed_only_api() -> None:
    assert module._FIXED_ONLY_API == "MakePathRelativeTo"
    assert "src/filesys.cpp" in module._PRODUCTION_PATHS
    assert "src/filesys.h" in module._PRODUCTION_PATHS
    assert module._TEST_PATHS == ("src/unittest/test_filesys.cpp",)


def test_16553_gate14_contract_accounts_for_all_raw_paths() -> None:
    assert len(module._RAW_PATHS) == 24
    assert "cmake/Modules/HashBuiltinFiles.cmake" in module._RAW_PATHS
    assert "src/builtin_files.cpp.in" in module._RAW_PATHS
    assert not hasattr(module, "run_candidate")
    assert not hasattr(module, "build_candidate")
