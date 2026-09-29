from warptest.c1_lazarus_selection import luanti_8704_execution as module


def test_observation_parser_covers_goal_and_defect() -> None:
    fixed = module._OBSERVATION_RE.search(
        b"C1_LAZARUS_8704_OBSERVATION item=default:torch count=4 "
        b"empty=false fuel=true cooking=true"
    )
    buggy = module._OBSERVATION_RE.search(
        b"C1_LAZARUS_8704_OBSERVATION item= count=0 "
        b"empty=true fuel=true cooking=true"
    )
    assert fixed is not None and fixed.groups() == (
        b"default:torch",
        b"4",
        b"false",
        b"true",
        b"true",
    )
    assert buggy is not None and buggy.groups() == (
        b"",
        b"0",
        b"true",
        b"true",
        b"true",
    )


def test_dry_run_step_parser_is_bounded() -> None:
    assert module._step(b"[304/304] Linking CXX executable minetest") is True
    assert module._step(b"ninja: Entering directory build") is False
