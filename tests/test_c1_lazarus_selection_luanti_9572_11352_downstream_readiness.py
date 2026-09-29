from warptest.c1_lazarus_selection import (
    luanti_9572_11352_downstream_readiness as module,
)


def test_readiness_is_read_only() -> None:
    assert not hasattr(module, "build_candidate")
    assert not hasattr(module, "execute_candidate")
    assert not hasattr(module, "run_candidate")


def test_selection_uses_candidate_owned_11352_pair() -> None:
    assert module._PAIR.as_posix() == "private/fresh-gate57/luanti/11352/cpp17-v6"
    assert all("attempt" not in ref.artifact_id for ref in module._REFS.values())
