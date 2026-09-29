from warptest.c1_lazarus_selection import luanti_9722_runtime_closure as module


def test_fixture_exercises_reversible_numbers() -> None:
    assert b"0.2695949158945771" in module._INIT
    assert b"269594915894577" in module._INIT
    assert b"minetest.serialize" in module._INIT
    assert b"minetest.deserialize" in module._INIT


def test_runtime_is_candidate_specific() -> None:
    assert module._PAIR.as_posix().startswith("private/materialized/luanti/9722/")
    assert module._ROOT.as_posix().startswith("private/runtime-fixtures/luanti/9722/")
