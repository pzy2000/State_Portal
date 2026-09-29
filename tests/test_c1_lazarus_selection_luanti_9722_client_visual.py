from warptest.c1_lazarus_selection import luanti_9722_client_visual as module


def test_visual_fixture_is_semantically_grounded_and_delayed() -> None:
    assert "minetest.serialize(values)" in module._MOD_INIT
    assert "minetest.deserialize(encoded)" in module._MOD_INIT
    assert "goal_reversible_numbers" in module._MOD_INIT
    assert "defect_number_changed" in module._MOD_INIT
    assert "minetest.after(6.0" in module._MOD_INIT


def test_client_closure_is_candidate_specific() -> None:
    assert module._PAIR.as_posix().startswith("private/materialized/luanti/9722/")
    assert module._DEP.as_posix() == "private/dependencies/luanti-9722-irrlicht/1.8.4"
    assert "14464" not in module._DEP.as_posix()


def test_admission_requires_direct_visual_judgment() -> None:
    assert module.ADMISSION_CONFIRMATION != module.CONFIRMATION
