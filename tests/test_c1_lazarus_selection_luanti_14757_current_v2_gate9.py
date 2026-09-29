from __future__ import annotations

from pathlib import Path

from warptest.c1_lazarus_selection.luanti_14757_current_v2_gate9 import (
    StartObservation,
    StartSpec,
    _persist_raw_observation,
    _remove_variant_tree,
)


def test_current_v2_raw_observation_survives_variant_cleanup(tmp_path: Path) -> None:
    run = tmp_path / "private/bounded-runs/luanti/14757/run-1"
    variant = run / "fixed"
    variant.mkdir(parents=True)
    spec = StartSpec(
        variant="fixed",
        app_root=tmp_path,
        binary=tmp_path / "bin",
        fixture=tmp_path,
        run_root=variant,
        world=variant / "world",
        config=variant / "minetest.conf",
        log=variant / "luanti.log",
        watchdog_seconds=60,
    )
    raw = _persist_raw_observation(
        tmp_path,
        spec,
        StartObservation(
            process_outcome="completed",
            returncode=0,
            stdout=b"stdout",
            stderr=b"stderr",
            start_marker=None,
            start_result_empty=None,
            action_command=None,
            action_count=0,
            semantic_result=None,
            action_ok=None,
        ),
    )

    _remove_variant_tree(variant, run)

    retained = tmp_path / raw
    assert retained == run / "observations/fixed.json"
    assert retained.is_file()
