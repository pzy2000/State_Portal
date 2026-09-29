from warptest.c1_lazarus_selection import luanti_11352_runtime_closure as module


def test_fixture_observes_global_setting_leak_without_source_edits() -> None:
    assert b'minetest.set_mapgen_setting("water_level", "15", false)' in module._INIT
    assert b'minetest.settings:get("water_level")' in module._INIT
    assert b'minetest.request_shutdown' in module._INIT


def test_runtime_namespace_is_candidate_owned() -> None:
    assert module._PAIR.as_posix().startswith("private/fresh-gate57/luanti/11352/")
    assert module._ROOT.as_posix().startswith("private/runtime-fixtures/luanti/11352/")
