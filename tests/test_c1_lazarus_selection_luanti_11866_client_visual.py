from warptest.c1_lazarus_selection import luanti_11866_client_visual as module


def test_visual_fixture_is_grounded_in_gate10_boundary_oracle() -> None:
    assert 'minetest.settings:get("mapgen_limit")' in module._MOD_INIT
    assert "limit % 16" in module._MOD_INIT
    assert "goal_mapgen_limit_aligned" in module._MOD_INIT
    assert "defect_mapgen_limit_misaligned" in module._MOD_INIT
    assert "minetest.after(6.0" in module._MOD_INIT
    assert "player:set_sky" in module._MOD_INIT
    assert "player:set_clouds({density = 0})" in module._MOD_INIT


def test_client_build_is_candidate_owned_mt4() -> None:
    assert module._DEPENDENCY.as_posix().startswith(
        "private/dependencies/luanti-11866-irrlichtmt/1.9.0mt4"
    )
    assert "14464" not in module._DEPENDENCY.as_posix()


def test_admission_requires_separate_direct_visual_judgment() -> None:
    assert module.ADMISSION_CONFIRMATION != module.CONFIRMATION
