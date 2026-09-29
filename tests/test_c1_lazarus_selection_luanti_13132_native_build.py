from warptest.c1_lazarus_selection import luanti_13132_native_build as module


def test_build_namespace_and_parallelism_are_candidate_specific() -> None:
    assert module._PAIR.as_posix() == "private/materialized/luanti/13132/application-v1"
    assert module._BUILD_ROOT.as_posix() == "private/bounded-builds/luanti/13132/native-v1"
    assert b"-std=c++17" in module._LAUNCHER_CONTENT


def test_materialization_inputs_do_not_borrow_other_candidates() -> None:
    refs = (
        module._SOURCE,
        module._PATCH,
        module._ATOMICITY,
        module._MANIFEST,
        module._AGGREGATE,
        module._TRANSITION,
        module._DEPENDENCY,
    )
    assert all("/9722/" not in ref.artifact_id for ref in refs)
