"""Decide, per 2048 task, whether a from-start replay of the target can exist.

Four C1 2048 tasks carry ``exact-start-not-play-consistent`` because their start
board is injected through localStorage and no scripted replay reproduces it.
Whether that gap is closable is a question about the game's arithmetic, not
about search effort, because ``GameManager`` ties the score to the board:

* every merge of two ``v`` tiles adds ``2v`` to the score and to the board's
  potential ``g``, where ``g(2) = 0`` and ``g(2v) = 2g(v) + 2v``;
* a spawned 2 adds nothing to either; a spawned 4 adds ``g(4) = 4`` to the
  potential and nothing to the score.

So ``score = potential - 4 * (number of spawned 4s)``, and the spawned tiles
must also account for the board's total mass. A declared ``(board, score)``
pair that violates either equation cannot be produced by any move sequence
under any seed, and ``TwentyFortyEightProfile._validate_target`` compares the
score exactly, so a replay reaching only the board would still fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = ROOT / "specs/2048"
START_TILES = 2


def _potential(value: int) -> int:
    return 0 if value <= 2 else 2 * _potential(value // 2) + value


def _unreachability_reason(target: dict) -> str | None:
    """Why no move sequence produces this target, or None if one could exist."""
    tiles = [int(v) for row in target["board"] for v in row if v]
    potential = sum(_potential(v) for v in tiles)
    mass = sum(tiles)
    score = int(target.get("score", 0))

    if score > potential:
        return f"score {score} exceeds the board's maximum attainable {potential}"
    if (potential - score) % 4:
        return f"score {score} leaves {potential - score}, not a whole number of spawned 4s"
    spawned_fours = (potential - score) // 4
    remaining_mass = mass - 4 * spawned_fours
    if remaining_mass < 0:
        return (
            f"score {score} requires {spawned_fours} spawned 4s worth {4 * spawned_fours}, "
            f"more than the board's total mass {mass}"
        )
    if remaining_mass % 2:
        return f"mass {mass} cannot be split into spawned 2s and 4s"
    if remaining_mass // 2 + spawned_fours < START_TILES:
        return f"mass {mass} accounts for fewer than the {START_TILES} mandatory start tiles"
    if target.get("keepPlaying") and not target.get("won"):
        return "keepPlaying is only set after a win, so it cannot be true while won is false"
    best_score = target.get("bestScore")
    if best_score is not None and int(best_score) > score:
        return f"bestScore {best_score} above score {score} requires a separate earlier game"
    return None


def _target(spec_name: str) -> dict:
    spec = yaml.safe_load((SPEC_DIR / spec_name).read_text(encoding="utf-8"))
    return spec.get("target") or {}


def _specs_with_boards() -> list[str]:
    names = []
    for path in sorted(SPEC_DIR.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        if (spec.get("target") or {}).get("board"):
            names.append(path.name)
    return names


@pytest.mark.parametrize("spec_name", _specs_with_boards())
def test_declared_replay_targets_are_play_consistent(spec_name: str) -> None:
    """A spec may only claim replay reachability if its target can be played to."""
    spec = yaml.safe_load((SPEC_DIR / spec_name).read_text(encoding="utf-8"))
    if "replay_from_start" not in spec:
        pytest.skip(f"{spec_name} does not claim replay reachability")
    reason = _unreachability_reason(spec["target"])
    assert reason is None, f"{spec_name} declares replay_from_start but {reason}"


@pytest.mark.parametrize(
    "spec_name,expected_reason_fragment",
    [
        ("double-merge-left.yaml", "exceeds the board's maximum attainable 8"),
        ("c1-cascade-256.yaml", "exceeds the board's maximum attainable 2568"),
        ("c1-near-loss-rescue-128.yaml", "exceeds the board's maximum attainable 1884"),
        ("c1-multistage-win-2048.yaml", "more than the board's total mass 2288"),
    ],
)
def test_deep_task_starts_cannot_be_replayed(spec_name: str, expected_reason_fragment: str) -> None:
    """Pin down why the four deep starts stay injection-only.

    Closing ``exact-start-not-demonstrated`` for these tasks would mean changing
    the declared score, which is public in the prompt, so it is a task revision
    rather than a missing artifact.
    """
    reason = _unreachability_reason(_target(spec_name))
    assert reason is not None, f"{spec_name} is now play-consistent; a replay can be scripted"
    assert expected_reason_fragment in reason, f"{spec_name}: {reason}"
