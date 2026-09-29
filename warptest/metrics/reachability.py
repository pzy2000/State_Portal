"""From-start reachability estimation for candidate deep states.

This is the signal that justifies *why a checkpoint is needed at all*: if a
target semantic state is essentially never reached by ordinary from-start play
within a bounded budget, then a from-start UI test cannot exercise it, and a
constructed checkpoint is the only tractable way to test the behaviour. We
estimate the from-start reach rate by Monte-Carlo playout of the artifact's own
transition rules and report ``unreachability = 1 - reach_rate``.

For 2048 we simulate the exact move/merge/spawn semantics of
``games/2048/js/game_manager.js``. For engines without a cheap faithful
simulator (e.g. Luanti voxel worlds) there is no from-start generator that
produces arbitrary authored deep structures, so we return a conservative
unreachability of 1.0 with that documented justification rather than a fake
number.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


# --------------------------------------------------------------------------
# 2048 faithful-enough simulator (mirrors game_manager.js semantics)
# --------------------------------------------------------------------------

def _compress_merge(row: list[int]) -> tuple[list[int], int]:
    """Slide non-zero tiles left and merge equal neighbours once. Returns
    (new_row, gained_score)."""
    tiles = [v for v in row if v != 0]
    out: list[int] = []
    gained = 0
    i = 0
    while i < len(tiles):
        if i + 1 < len(tiles) and tiles[i] == tiles[i + 1]:
            merged = tiles[i] * 2
            out.append(merged)
            gained += merged
            i += 2
        else:
            out.append(tiles[i])
            i += 1
    out += [0] * (len(row) - len(out))
    return out, gained


def _move(board: list[list[int]], direction: str) -> tuple[list[list[int]], bool, int]:
    """direction in {left,right,up,down}. Returns (board, moved, gained)."""
    size = len(board)
    b = [row[:] for row in board]
    gained = 0
    if direction in ("up", "down"):
        # transpose to operate on columns as rows
        b = [list(col) for col in zip(*b)]
    reverse = direction in ("right", "down")
    new_rows = []
    for row in b:
        r = row[::-1] if reverse else row
        merged, g = _compress_merge(r)
        gained += g
        new_rows.append(merged[::-1] if reverse else merged)
    b = new_rows
    if direction in ("up", "down"):
        b = [list(col) for col in zip(*b)]
    moved = b != board
    return b, moved, gained


def _spawn(board: list[list[int]], rng: random.Random) -> None:
    empties = [(x, y) for x in range(len(board)) for y in range(len(board)) if board[x][y] == 0]
    if not empties:
        return
    x, y = rng.choice(empties)
    board[x][y] = 2 if rng.random() < 0.9 else 4


def _max_tile(board: list[list[int]]) -> int:
    return max(max(row) for row in board)


def _moves_available(board: list[list[int]]) -> bool:
    for d in ("left", "right", "up", "down"):
        _, moved, _ = _move(board, d)
        if moved:
            return True
    return False


def _greedy_move(board: list[list[int]], rng: random.Random) -> str | None:
    """Pick the move maximising immediate gained score, tie-broken randomly.
    A mildly competent policy so we do not undersell reachability."""
    best: list[tuple[int, str]] = []
    for d in ("left", "up", "right", "down"):
        _, moved, gained = _move(board, d)
        if moved:
            best.append((gained, d))
    if not best:
        return None
    best.sort(key=lambda t: t[0], reverse=True)
    top = best[0][0]
    choices = [d for g, d in best if g == top]
    return rng.choice(choices)


def estimate_2048_reach(
    target_max_tile: int,
    *,
    trials: int = 200,
    max_steps: int = 2000,
    policy: str = "greedy",
    seed: int = 1234,
) -> float:
    """Fraction of from-start playouts that reach a tile >= target_max_tile."""
    rng = random.Random(seed)
    hits = 0
    for _ in range(trials):
        board = [[0] * 4 for _ in range(4)]
        _spawn(board, rng)
        _spawn(board, rng)
        reached = False
        for _ in range(max_steps):
            if _max_tile(board) >= target_max_tile:
                reached = True
                break
            if policy == "greedy":
                d = _greedy_move(board, rng)
            else:
                movable = [dd for dd in ("left", "right", "up", "down") if _move(board, dd)[1]]
                d = rng.choice(movable) if movable else None
            if d is None:
                break
            board, moved, _ = _move(board, d)
            if moved:
                _spawn(board, rng)
        if reached:
            hits += 1
    return hits / trials if trials else 0.0


@dataclass
class ReachResult:
    game: str
    descriptor: dict
    reach_rate: float
    unreachability: float
    method: str


def unreachability(game: str, descriptor: dict, *, trials: int = 200, max_steps: int = 2000) -> ReachResult:
    """Return the from-start unreachability of a target descriptor.

    descriptor examples:
      {"kind": "max_tile", "value": 1024}
    """
    game = game.lower()
    if game == "2048" and descriptor.get("kind") == "max_tile":
        rate = estimate_2048_reach(int(descriptor["value"]), trials=trials, max_steps=max_steps)
        return ReachResult("2048", descriptor, rate, 1.0 - rate, f"montecarlo greedy trials={trials}")
    # Conservative documented default for engines without a cheap simulator.
    return ReachResult(
        game,
        descriptor,
        0.0,
        1.0,
        "no from-start generator produces authored deep states (documented default=1.0)",
    )
