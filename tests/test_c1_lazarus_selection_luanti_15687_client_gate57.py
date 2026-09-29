from __future__ import annotations

from warptest.c1_lazarus_selection.luanti_15687_client_gate57 import (
    CONFIRMATION,
    _cells,
)


def test_15687_client_epoch_has_exact_fresh_truth_table() -> None:
    cells = _cells()
    assert len(cells) == 12
    assert len({cell.reference for cell in cells}) == 12
    assert {
        (cell.run.phase, cell.variant, cell.expected)
        for cell in cells
    } == {
        ("f2p", "fixed", "pass"),
        ("f2p", "buggy", "failure"),
        ("p2p", "fixed", "pass"),
        ("p2p", "buggy", "pass"),
    }
    assert {cell.run.selector for cell in cells} == {"matrix4", "TestSettings"}
    assert CONFIRMATION == "EXECUTE_LUANTI_15687_CLIENT_GATE57_V1"
