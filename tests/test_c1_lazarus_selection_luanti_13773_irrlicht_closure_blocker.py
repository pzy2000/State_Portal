from warptest.c1_lazarus_selection import (
    luanti_13773_irrlicht_closure_blocker as module,
)


def test_13773_exact_evidence_paths_are_candidate_specific() -> None:
    assert module._CAPSULE_IRRSTRING.as_posix().startswith(
        "private/fixed-capsules/luanti/13773/"
    )
    assert module._DEPENDENCY_IRRUSTRING.as_posix().startswith(
        "private/dependencies/luanti-client/13773/v3/"
    )


def test_13773_blocker_has_no_execution_entrypoint() -> None:
    assert not hasattr(module, "run_candidate")
    assert not hasattr(module, "build_candidate")
