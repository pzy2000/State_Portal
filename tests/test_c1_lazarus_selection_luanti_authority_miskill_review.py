from __future__ import annotations

from warptest.c1_lazarus_selection.luanti_authority_miskill_review import _SPECS


def test_authority_miskill_review_has_two_blockers_and_one_preflight() -> None:
    assert [(spec.number, spec.disposition, spec.primary) for spec in _SPECS] == [
        (11427, "confirmed_multi_defect", None),
        (12595, "confirmed_multi_defect", None),
        (9187, "reclassification_preflight", 9277),
    ]
    assert len({spec.output for spec in _SPECS}) == 3
