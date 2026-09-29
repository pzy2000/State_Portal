from warptest.c1_lazarus_selection import luanti_11866_runtime_closure as module


def test_fixture_replays_mapblock_boundary_semantics() -> None:
    assert b'minetest.get_mod_storage()' in module._INIT
    assert b'"c1-11866-ready"' in module._INIT
    assert b'minetest.settings:get("mapgen_limit")' in module._INIT
    assert b"limit % 16" in module._INIT
    assert b"goal_mapgen_limit_aligned" in module._INIT
    assert b"defect_mapgen_limit_misaligned" in module._INIT


def test_runtime_is_candidate_specific() -> None:
    assert module._PAIR.as_posix().startswith("private/materialized/luanti/11866/")
    assert module._ROOT.as_posix().startswith("private/runtime-fixtures/luanti/11866/")
    assert "14464" not in module._PAIR.as_posix()


def test_visual_blocker_is_not_terminal() -> None:
    assert module._VISUAL_BLOCKER.artifact_kind == "candidate_gate11_13_11866_visual_blocker"
