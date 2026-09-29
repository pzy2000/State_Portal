from __future__ import annotations

from warptest.c1_lazarus_selection import luanti_9284_gate14_terminal as module


def test_9284_gate14_contract_exposes_fixed_only_settings_api() -> None:
    assert module._FIXED_ONLY_API == "Settings::setDefault(name, FlagDesc*, u32)"
    assert len(module._PRODUCTION_PATHS) == 21
    assert module._TEST_PATHS == ("src/unittest/test_settings.cpp",)


def test_9284_gate14_contract_accounts_for_raw_paths() -> None:
    assert len(module._RAW_PATHS) == 23
    assert module._RAW_PATHS[0] == "doc/lua_api.txt"
    assert not hasattr(module, "run_candidate")
    assert not hasattr(module, "build_candidate")
