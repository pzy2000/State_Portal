from warptest.c1_lazarus_selection import luanti_13132_irrlichtmt9_blocker as module


def test_blocker_uses_only_candidate_and_comparison_inputs() -> None:
    refs = (
        module._SOURCE,
        module._PATCH,
        module._ATOMICITY,
        module._MANIFEST,
        module._CAPSULE,
        module._AGGREGATE,
        module._TRANSITION,
        module._BUILD,
        module._COMPARISON,
    )
    assert all("/9722/" not in ref.artifact_id for ref in refs)
    assert all("attempt" not in ref.artifact_id for ref in refs)


def test_exact_dependency_destination_is_candidate_owned() -> None:
    assert module._DEPENDENCY_TREE.as_posix() == (
        "private/dependencies/luanti-13132-irrlicht/1.9.0mt9/source"
    )


def test_blocker_has_no_build_or_network_entrypoint() -> None:
    assert not hasattr(module, "build_candidate")
    assert not hasattr(module, "execute_candidate")
    assert not hasattr(module, "download_dependency")
