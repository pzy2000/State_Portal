from warptest.c1_lazarus_selection import luanti_9722_irrlicht184_closure as module


def test_dependency_is_candidate_specific_upstream_irrlicht() -> None:
    assert module._DEP.as_posix() == "private/dependencies/luanti-9722-irrlicht/1.8.4"
    assert "14464" not in module._DEP.as_posix()
    assert "irrlicht-1.8.4.zip" in module._REMOTE


def test_closure_has_no_build_entrypoint() -> None:
    assert not hasattr(module, "build_candidate")
    assert not hasattr(module, "execute_candidate")
