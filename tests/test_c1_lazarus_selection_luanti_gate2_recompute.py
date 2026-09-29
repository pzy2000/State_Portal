from __future__ import annotations

from warptest.c1_lazarus_selection.luanti_gate2_recompute import _SPECS


def test_gate2_recompute_specs_bind_unique_primary_issues() -> None:
    assert [
        (
            spec.number,
            spec.original_authoritative,
            spec.primary,
            spec.incidental,
        )
        for spec in _SPECS
    ] == [
        (9187, (9195, 9277), 9277, (9195,)),
        (11866, (11600,), 11839, (11600,)),
    ]
    assert len({spec.output for spec in _SPECS}) == 2


def test_gate2_recompute_has_no_execution_entrypoint() -> None:
    import warptest.c1_lazarus_selection.luanti_gate2_recompute as module

    assert not hasattr(module, "run_candidate")
    assert not hasattr(module, "build_candidate")
