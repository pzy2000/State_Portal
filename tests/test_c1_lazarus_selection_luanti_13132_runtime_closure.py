from warptest.c1_lazarus_selection import luanti_13132_runtime_closure as module


def test_runtime_adapter_is_bounded_and_source_preserving() -> None:
    assert b"debug.sethook" in module._INIT
    assert b"C1_BOUND instruction limit" in module._INIT
    assert b"minetest.request_shutdown" in module._INIT


def test_runtime_namespaces_are_candidate_specific() -> None:
    assert module._ROOT.as_posix().startswith("private/runtime-fixtures/luanti/13132/")
    assert "9722" not in module._ROOT.as_posix()
