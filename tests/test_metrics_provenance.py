"""Fast, offline tests for the metric-grounded provenance pipeline.

These exercise the deterministic, non-browser parts: static metrics, the frozen
config hash, composite ranking reproducibility (Gate A), the 2048 reachability
simulator monotonicity, and the token-equivalence guard used by naturalization.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _games_2048() -> Path:
    return ROOT / "games" / "2048"


def test_config_hash_is_stable():
    from warptest.metrics.config import default_config

    a = default_config().config_hash()
    b = default_config().config_hash()
    assert a == b and len(a) == 16


def test_static_metrics_move_is_complex():
    pytest.importorskip("lizard")
    from warptest.metrics.static_metrics import analyze_paths

    repo = _games_2048()
    if not (repo / "js" / "game_manager.js").exists():
        pytest.skip("2048 submodule not checked out")
    metrics = analyze_paths(repo, [repo / "js" / "game_manager.js"])
    by_short = {m.short_name: m for m in metrics}
    assert "move" in by_short
    # move() has real branching; not a trivial getter.
    assert by_short["move"].cyclomatic >= 3


def test_ranking_is_reproducible_gate_a():
    pytest.importorskip("lizard")
    repo = _games_2048()
    if not (repo / "js" / "game_manager.js").exists():
        pytest.skip("2048 submodule not checked out")
    from warptest.metrics.select import run_game

    a = run_game("2048", workspace=ROOT, top=50, reach_trials=20)
    b = run_game("2048", workspace=ROOT, top=50, reach_trials=20)
    ka = [c["key"] for c in a["candidates"]]
    kb = [c["key"] for c in b["candidates"]]
    assert ka == kb
    assert [c["final_score"] for c in a["candidates"]] == [c["final_score"] for c in b["candidates"]]
    # The top candidate on the board+score surface should be the move region.
    top = a["candidates"][0]
    assert "move" in top["name"]


def test_reachability_is_monotone_decreasing():
    from warptest.metrics.reachability import estimate_2048_reach

    tiers = [64, 128, 256, 512]
    rates = [estimate_2048_reach(t, trials=60, max_steps=1500) for t in tiers]
    # Higher tiles are never easier to reach.
    for earlier, later in zip(rates, rates[1:]):
        assert earlier + 1e-9 >= later
    assert rates[0] > rates[-1]  # 64 clearly easier than 512


def test_token_guard_detects_missing_and_leak():
    from warptest.issue_naturalize import TokenGuard

    spec = {
        "benchmark": "2048",
        "target": {"score": 42, "over": True},
        "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
        "actions": [{"type": "move", "direction": "left"}],
    }
    guard = TokenGuard(spec)

    good = (
        "Restore a 2048 board with score 42 and over set to true, using the "
        "localStorage_restore_reload strategy which is save-restorable, then apply a move left."
    )
    assert guard.check_shallow(good).ok

    missing = "Restore a board and move around."
    assert not guard.check_shallow(missing).ok

    # explore variant must NOT leak the strategy/validity/action-type tokens.
    leaky = good
    assert not guard.check_explore(leaky).ok
    clean = "Restore a 2048 board with score 42 and over set to true; read the source to pick persistence, then send one left input."
    assert guard.check_explore(clean).ok
