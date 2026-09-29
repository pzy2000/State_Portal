from __future__ import annotations

from warptest.c1_lazarus_selection.luanti_14555_fresh_gate57 import (
    _cells,
)


def test_14555_fresh_epoch_has_exact_native_gate57_matrix() -> None:
    cells = _cells()
    assert len(cells) == 12
    assert len({cell.reference for cell in cells}) == 12
    assert {(cell.phase, cell.variant) for cell in cells} == {
        ("f2p", "fixed"),
        ("f2p", "buggy"),
        ("p2p", "fixed"),
        ("p2p", "buggy"),
    }
    for cell in cells:
        assert cell.repetition in {1, 2, 3}
        assert cell.test_path in {
            "src/unittest/test_voxelalgorithms.cpp",
            "src/unittest/test_settings.cpp",
        }
        assert cell.expected == (
            "failure" if cell.phase == "f2p" and cell.variant == "buggy" else "pass"
        )


def test_14555_fresh_p2p_is_same_test_on_both_variants() -> None:
    p2p = [cell for cell in _cells() if cell.phase == "p2p"]
    assert {cell.test_path for cell in p2p} == {"src/unittest/test_settings.cpp"}
    assert {cell.selector for cell in p2p} == {"TestSettings"}
    assert {cell.expected for cell in p2p} == {"pass"}
