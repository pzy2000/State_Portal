from __future__ import annotations

from warptest.c1_lazarus_selection import luanti_14543_gate14_semantic_terminal as module


def test_14543_gate14_contract_uses_buggy_visible_api() -> None:
    assert module._BUGGY_VISIBLE_API == "pkgmgr.install_dir"
    assert module._TEST_PATHS == (
        "builtin/mainmenu/content/tests/pkgmgr_spec.lua",
    )
    assert len(module._PRODUCTION_PATHS) == 6


def test_14543_gate14_contract_accounts_for_documentation_hunks() -> None:
    assert len(module._RAW_PATHS) == 9
    assert "doc/lua_api.md" in module._RAW_PATHS
    assert "doc/menu_lua_api.md" in module._RAW_PATHS
    assert not hasattr(module, "run_candidate")
    assert not hasattr(module, "build_candidate")
