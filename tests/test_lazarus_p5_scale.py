from __future__ import annotations

from pathlib import Path

import pytest

from warptest.lazarus_catalog import LAZARUS_ARMS
from warptest.p5_scale import ALL_ARMS, run_p5_scale


ROOT = Path(__file__).resolve().parents[1]


def test_lazarus_arms_are_manifest_specific_and_rejected_by_normal_p5() -> None:
    """The frozen C1/C2/C3 manifest must never plan a Lazarus cell."""

    assert not set(LAZARUS_ARMS) & set(ALL_ARMS)

    with pytest.raises(ValueError, match="normal P5 manifest"):
        run_p5_scale(
            ROOT,
            manifest_path=ROOT / "benchmarks/p5/manifest.yaml",
            arms=["lazarus_state"],
            dry_run=True,
        )
