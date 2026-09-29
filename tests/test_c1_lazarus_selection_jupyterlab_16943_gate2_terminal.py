from warptest.c1_lazarus_selection import jupyterlab_16943_gate2_terminal as module


def test_candidate_has_two_authoritative_issue_fixes() -> None:
    assert module._FIX_REVISION == "141904e62406c407434f5fd56c95d657e366eb3f"
    assert module._BASE_REVISION == "55213d8bca9d133f89e4b6fa989f372b7c3da9a9"


def test_terminal_is_gate2_and_not_runtime() -> None:
    assert module._TERMINAL.artifact_kind == "candidate_sprint_terminal"
    assert module._ATOMICITY.artifact_kind == "source_atomicity"


def test_no_gate57_executor_is_exported() -> None:
    assert not hasattr(module, "execute_gate57")
