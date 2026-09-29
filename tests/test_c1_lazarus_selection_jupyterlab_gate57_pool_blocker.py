from warptest.c1_lazarus_selection import jupyterlab_gate57_pool_blocker as module


def test_pool_classification_keeps_static_and_pending_states_distinct() -> None:
    assert module._STATIC[2469] == "gate1_4_static_admitted"
    assert module._STATIC[2437] == "gate4_pending_execution"
    assert module._STATIC[3867] == "gate4_pending_execution"
    assert module._STATIC[1846] == "source_closure_pending_attachments"
    assert module._STATIC[4796] == "source_closure_pending_attachments"


def test_pool_blocker_has_no_execution_entrypoint() -> None:
    assert not hasattr(module, "run_candidate")
    assert not hasattr(module, "build_candidate")
    assert module._NUMBERS == (2437, 2469, 1846, 3867, 4796)
