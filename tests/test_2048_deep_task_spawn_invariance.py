"""Prove the three 2048 deep tasks are graded independently of tile spawning.

The registered oracles used to pin ``Math.random`` to sixteen zeros through
``target.action_random_values``.  That pin was exposed in the public
``preconditions``, where a pixel agent can neither observe nor set it, and it was
only installed on the full-restore path (never on the snapshot fast-restore path
or in the self-synthesized arms), so the same six moves were graded under
different spawn behaviour depending on the arm.

This module removes the need for any pin by proving the stronger property: the
registered assertions hold for *every* spawn the game can produce.  It replicates
``GameManager.move`` / ``addRandomTile`` / ``Grid.randomAvailableCell`` from
``games/2048/js`` and enumerates the whole spawn tree (value 2 or 4 in any empty
cell after every move that moved), which is a superset of what any seed can
produce.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import yaml

from warptest.c1_evaluation import semantic_assertions


ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "benchmarks/p5/c1_tasks.yaml"
SPEC_DIR = ROOT / "specs/2048"
DEEP_TASK_IDS = (
    "2048-cascade-256",
    "2048-near-loss-rescue-128",
    "2048-multistage-win-2048",
)
SIZE = 4
# 0: up, 1: right, 2: down, 3: left -- GameManager.getVector.
VECTORS = {"up": (0, -1), "right": (1, 0), "down": (0, 1), "left": (-1, 0)}
SPAWN_VALUES = (2, 4)

# A board is the immutable column-major ``Grid.cells`` layout: cells[x][y] with x
# the column and y the row, and 0 for an empty cell. The harness grades
# ``state.grid.cells[column][row]`` as ``board[row][column]``, so a spec's
# row/column coordinates index the transpose.
Cells = tuple[tuple[int, ...], ...]
# (cells, score, won, over)
State = tuple[Cells, int, bool, bool]


def _cells_from_rows(rows: list[list[int]]) -> Cells:
    return tuple(
        tuple(int(rows[y][x] or 0) for y in range(SIZE)) for x in range(SIZE)
    )


def _available(cells: Cells) -> list[tuple[int, int]]:
    return [
        (x, y) for x in range(SIZE) for y in range(SIZE) if not cells[x][y]
    ]


def _tile_matches_available(cells: Cells) -> bool:
    for x in range(SIZE):
        for y in range(SIZE):
            value = cells[x][y]
            if not value:
                continue
            for dx, dy in VECTORS.values():
                nx, ny = x + dx, y + dy
                if 0 <= nx < SIZE and 0 <= ny < SIZE and cells[nx][ny] == value:
                    return True
    return False


def _is_over(cells: Cells) -> bool:
    """GameManager.movesAvailable inverted, evaluated after the spawn."""

    return not (_available(cells) or _tile_matches_available(cells))


def _move(
    cells: Cells, score: int, won: bool, direction: str
) -> tuple[Cells, int, bool, bool]:
    dx, dy = VECTORS[direction]
    grid = [list(column) for column in cells]
    xs = list(range(SIZE))
    ys = list(range(SIZE))
    # buildTraversals always starts from the farthest cell in the direction.
    if dx == 1:
        xs.reverse()
    if dy == 1:
        ys.reverse()
    merged_here = [[False] * SIZE for _ in range(SIZE)]
    moved = False
    for x in xs:
        for y in ys:
            value = grid[x][y]
            if not value:
                continue
            # findFarthestPosition: advance until an obstacle or the wall.
            cx, cy = x, y
            while True:
                nx, ny = cx + dx, cy + dy
                if 0 <= nx < SIZE and 0 <= ny < SIZE and not grid[nx][ny]:
                    cx, cy = nx, ny
                else:
                    break
            nx, ny = cx + dx, cy + dy
            neighbour = (
                grid[nx][ny] if (0 <= nx < SIZE and 0 <= ny < SIZE) else 0
            )
            if neighbour == value and neighbour and not merged_here[nx][ny]:
                grid[nx][ny] = value * 2
                grid[x][y] = 0
                merged_here[nx][ny] = True
                score += value * 2
                if value * 2 == 2048:
                    won = True
                moved = True
            elif (cx, cy) != (x, y):
                grid[cx][cy] = value
                grid[x][y] = 0
                moved = True
    return tuple(tuple(column) for column in grid), score, won, moved


def _spawn_successors(cells: Cells) -> Iterator[Cells]:
    """Every ``addRandomTile`` outcome: 2 or 4 in any available cell."""

    for x, y in _available(cells):
        for value in SPAWN_VALUES:
            grid = [list(column) for column in cells]
            grid[x][y] = value
            yield tuple(tuple(column) for column in grid)


def _final_states(
    board_rows: list[list[int]],
    score: int,
    won: bool,
    over: bool,
    keep_playing: bool,
    directions: list[str],
) -> set[State]:
    frontier: set[State] = {(_cells_from_rows(board_rows), score, won, over)}
    for direction in directions:
        successors: set[State] = set()
        for cells, current_score, current_won, current_over in frontier:
            # GameManager.move returns early when the game is terminated.
            if current_over or (current_won and not keep_playing):
                successors.add((cells, current_score, current_won, current_over))
                continue
            moved_cells, moved_score, moved_won, moved = _move(
                cells, current_score, current_won, direction
            )
            if not moved:
                successors.add((cells, current_score, current_won, current_over))
                continue
            if not _available(moved_cells):
                successors.add(
                    (
                        moved_cells,
                        moved_score,
                        moved_won,
                        _is_over(moved_cells),
                    )
                )
                continue
            for spawned in _spawn_successors(moved_cells):
                successors.add(
                    (spawned, moved_score, moved_won, _is_over(spawned))
                )
        frontier = successors
    return frontier


def _board(cells: Cells) -> list[list[int]]:
    return [[cells[column][row] for column in range(SIZE)] for row in range(SIZE)]


def _assertion_failure(
    assertion: dict[str, Any],
    state: State,
    start_score: int,
    keep_playing: bool,
) -> str | None:
    """Mirror Game2048Profile._assert_oracles for the board/state assertions."""

    cells, score, won, over = state
    flat = [value for row in _board(cells) for value in row]
    kind = assertion["type"]
    if kind == "no_severe_console":
        return None
    if kind == "largest_tile_equals":
        actual = max(flat, default=0)
        expected = int(assertion["value"])
        return None if actual == expected else f"largest tile {actual} != {expected}"
    if kind == "empty_cell_count_at_least":
        actual = sum(value == 0 for value in flat)
        expected = int(assertion["count"])
        return None if actual >= expected else f"{actual} empty cells < {expected}"
    if kind == "tile_count_at_least":
        value = int(assertion["value"])
        actual = sum(cell == value for cell in flat)
        expected = int(assertion["count"])
        return (
            None
            if actual >= expected
            else f"{actual} tiles equal to {value} < {expected}"
        )
    if kind == "tile_value_present":
        value = int(assertion["value"])
        return None if value in flat else f"tile {value} is absent"
    if kind == "board_cell_equals":
        row = int(assertion["row"])
        column = int(assertion["column"])
        actual = _board(cells)[row][column]
        expected = int(assertion["value"])
        return (
            None
            if actual == expected
            else f"cell ({row},{column}) is {actual} != {expected}"
        )
    if kind == "score_increased_by_at_least":
        delta = score - start_score
        expected = int(assertion["amount"])
        return None if delta >= expected else f"score delta {delta} < {expected}"
    if kind == "state_equals":
        path = assertion["path"]
        # ``keepPlaying`` only changes through the game's own "Keep going" button,
        # which no registered move action presses.
        observable = {"over": over, "won": won, "keepPlaying": keep_playing}
        if path not in observable:
            raise AssertionError(f"unsupported state_equals path: {path}")
        actual = observable[path]
        expected = assertion["value"]
        return None if actual == expected else f"{path} is {actual} != {expected}"
    raise AssertionError(f"unsupported 2048 assertion type: {kind}")


def _tasks() -> dict[str, dict[str, Any]]:
    payload = yaml.safe_load(TASK_FILE.read_text(encoding="utf-8"))
    return {task["id"]: task for task in payload["tasks"]}


def _spec(task: dict[str, Any]) -> dict[str, Any]:
    return yaml.safe_load((ROOT / task["oracle_spec"]).read_text(encoding="utf-8"))


def test_deep_task_assertions_hold_for_every_possible_spawn() -> None:
    tasks = _tasks()
    for task_id in DEEP_TASK_IDS:
        spec = _spec(tasks[task_id])
        assertions = semantic_assertions(spec)
        assert isinstance(assertions, list), task_id
        target = spec["target"]
        directions = [action["direction"] for action in spec["actions"]]
        start_score = int(target.get("score", 0))
        keep_playing = bool(target.get("keepPlaying", False))
        finals = _final_states(
            target["board"],
            start_score,
            bool(target.get("won", False)),
            bool(target.get("over", False)),
            keep_playing,
            directions,
        )
        # A degenerate explorer that collapsed the spawn tree would still satisfy
        # the per-state checks below, so require real branching first.
        assert len(finals) > 100, (task_id, len(finals))
        violations = [
            (_board(state[0]), reason)
            for state in finals
            for reason in [
                _assertion_failure(assertion, state, start_score, keep_playing)
                for assertion in assertions
            ]
            if reason is not None
        ]
        assert not violations, (
            f"{task_id}: {len(violations)} of {len(finals)} reachable final "
            f"states violate the registered assertions, e.g. {violations[0]}"
        )


def test_no_2048_spec_pins_the_tile_spawn_rng() -> None:
    for spec_path in sorted(SPEC_DIR.glob("*.yaml")):
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        target = spec.get("target")
        assert isinstance(target, dict), spec_path
        assert "action_random_values" not in target, spec_path


def test_public_deep_task_text_declares_no_hidden_rng_state() -> None:
    tasks = _tasks()
    for task_id in DEEP_TASK_IDS:
        task = tasks[task_id]
        public = " ".join(
            str(task[field])
            for field in ("preconditions", "reference_steps", "state_description")
        ).lower()
        for forbidden in ("random sample", "random value", "math.random", "seed"):
            assert forbidden not in public, (task_id, forbidden)
