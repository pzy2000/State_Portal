from warptest.c1_lazarus_selection import (
    luanti_13132_9722_downstream_readiness as module,
)


def test_comparison_uses_no_attempt_references() -> None:
    assert all("attempt" not in ref.artifact_id for ref in module._REFS.values())


def test_comparison_has_no_build_entrypoint() -> None:
    assert not hasattr(module, "run_candidate")
    assert not hasattr(module, "build_candidate")
    assert not hasattr(module, "execute_candidate")
