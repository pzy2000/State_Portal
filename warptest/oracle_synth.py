"""Execution-grounded oracle synthesis (Stage 2 of metric-grounded provenance).

The golden oracle for a task is not authored by hand: it is *recorded* by driving
the real game through the metric-selected complex region and reading back what the
artifact itself computes. For 2048 the selected region is
``GameManager.prototype.move`` (top of reports/metrics/2048-candidates.json); its
control-flow branches induce a small set of branch-covering seed states:

    merge      -> the merge branch (next.value === tile.value && !mergedFrom)
    near-win   -> the win branch  (merged.value === 2048 -> won = true)
    near-loss  -> the terminated / no-moves branch (isGameTerminated / over)

For each scenario we:
  1. pin Math.random so the post-move tile spawn is deterministic,
  2. restore the seed board through the game's own localStorage path,
  3. call the game's real ``move`` via the __WARPTEST_2048__ hook,
  4. read back ``getState()`` -- the observed after-state is the ground truth,
  5. emit a spec whose assertions are the *deterministic projection* of that
     observed state (score delta, resulting tile, terminal flags), so the oracle
     re-passes on a normal (unpinned) run.

The only authored part is the seed board + action, and those are mechanically
derived from the selected region's branches. The answer (post-state) is whatever
the real game code produces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Hook direction codes: 0=up, 1=right, 2=down, 3=left (games/2048/js/game_manager.js).
_DIR = {"up": 0, "right": 1, "down": 2, "left": 3}


@dataclass
class BranchScenario:
    id: str
    branch: str  # which move() branch this seed exercises
    description: str
    board: list[list[int]]  # 4x4 row-major, 0 = empty
    score: int = 0
    best_score: int = 0
    won: bool = False
    over: bool = False
    keep_playing: bool = False
    actions: list[dict[str, Any]] = field(default_factory=list)


# Seeds mirror the branches of GameManager.prototype.move. They intentionally
# match the three existing hand-authored 2048 tasks so we can show the
# metric-grounded pipeline recovers them.
SCENARIOS_2048: list[BranchScenario] = [
    BranchScenario(
        id="2048-merge-left",
        branch="merge (next.value === tile.value)",
        description="Two 2 tiles in the top row merge into a 4 on a left move.",
        board=[[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
        score=0,
        actions=[{"type": "move", "direction": "left"}],
    ),
    BranchScenario(
        id="2048-near-win",
        branch="win (merged.value === 2048 -> won)",
        description="Two 1024 tiles merge into a 2048 tile on a left move.",
        board=[[1024, 1024, 0, 0], [4, 8, 16, 32], [0, 0, 0, 0], [0, 0, 0, 0]],
        score=4096,
        best_score=4096,
        actions=[{"type": "move", "direction": "left"}],
    ),
    BranchScenario(
        id="2048-near-loss",
        branch="terminated / no-moves (isGameTerminated, over)",
        description="A full 2/4 checkerboard already over stays over on a left move.",
        board=[[2, 4, 2, 4], [4, 2, 4, 2], [2, 4, 2, 4], [4, 2, 4, 2]],
        score=512,
        best_score=512,
        over=True,
        actions=[{"type": "move", "direction": "left"}],
    ),
]


# --------------------------------------------------------------------------
# Execution recorder: drive the real game, read back the observed after-state
# --------------------------------------------------------------------------

# Math.random pinned to 0.0: spawn value = 2 (0 < 0.9), first available cell.
# This makes the post-move state fully deterministic so the recorded ground
# truth is itself reproducible (Gate A for the oracle).
_PIN_RANDOM = "(() => { Math.random = () => 0.0; })();"


def _tile_values(state: dict[str, Any]) -> list[int]:
    cells = (state.get("grid") or {}).get("cells") or []
    values: list[int] = []
    for column in cells:
        for cell in column:
            if cell and cell.get("value"):
                values.append(int(cell["value"]))
    return values


def record_2048_afterstate(
    scenario: BranchScenario,
    *,
    workspace: Path,
    headless: bool = True,
) -> dict[str, Any]:
    """Return {before, after, delta_score} recorded from the real 2048 game."""
    from .discovery import RepositoryInspector
    from .profiles import Game2048Profile
    from .reports import new_report_dir
    from .runtime import BrowserRuntimeAdapter

    repo = (workspace / "games" / "2048").resolve()
    inspect_result = RepositoryInspector().inspect(repo)
    launch_command = inspect_result.metadata.launch_command
    serialized = Game2048Profile._serialized_state(
        scenario.board, scenario.score, scenario.won, scenario.over, scenario.keep_playing
    )
    out_dir = new_report_dir()
    with BrowserRuntimeAdapter(repo, launch_command, out_dir, headless=headless) as runtime:
        runtime.context.add_init_script(script=_PIN_RANDOM)
        values = {"gameState": json.dumps(serialized)}
        if scenario.best_score:
            values["bestScore"] = str(scenario.best_score)
        runtime.install_storage_init(values)
        runtime.open("/?warptest=1")
        runtime.page.wait_for_function("() => !!window.__WARPTEST_2048__", timeout=10_000)
        runtime.page.wait_for_selector(".tile-container .tile", timeout=10_000)
        before = runtime.page.evaluate("() => window.__WARPTEST_2048__.getState()")
        for action in scenario.actions:
            if action.get("type") == "move":
                runtime.page.evaluate(
                    "(d) => window.__WARPTEST_2048__.move(d)", _DIR[action["direction"]]
                )
            elif action.get("type") == "restart":
                runtime.page.evaluate("() => window.__WARPTEST_2048__.restart()")
        after = runtime.page.evaluate("() => window.__WARPTEST_2048__.getState()")
    return {
        "before": before,
        "after": after,
        "delta_score": int(after.get("score", 0)) - int(before.get("score", 0)),
    }


# --------------------------------------------------------------------------
# Oracle spec construction from the recorded (execution-grounded) after-state
# --------------------------------------------------------------------------

def build_2048_spec(scenario: BranchScenario, recorded: dict[str, Any]) -> dict[str, Any]:
    after = recorded["after"]
    delta = recorded["delta_score"]
    before_tiles = _tile_values(recorded["before"])
    after_tiles = _tile_values(after)
    max_before = max(before_tiles) if before_tiles else 0
    max_after = max(after_tiles) if after_tiles else 0

    assertions: list[dict[str, Any]] = []
    # Score delta is deterministic (independent of the random spawn).
    if delta > 0:
        assertions.append({"type": "score_increased_by_at_least", "amount": delta})
    # A resulting merged tile is deterministic when the move produced a higher tile.
    if max_after > max_before:
        assertions.append({"type": "tile_value_present", "value": max_after})
    # Terminal flags recorded from the real run.
    if after.get("won"):
        assertions.append({"type": "state_equals", "path": "won", "value": True})
    if after.get("over"):
        assertions.append({"type": "state_equals", "path": "over", "value": True})
    assertions.append({"type": "no_severe_console"})

    return {
        "name": f"2048 {scenario.branch} (execution-grounded)",
        "benchmark": "2048",
        "target": {
            "board": scenario.board,
            "score": scenario.score,
            "bestScore": scenario.best_score,
            "won": scenario.won,
            "over": scenario.over,
            "keepPlaying": scenario.keep_playing,
        },
        "checkpoint": {
            "allowed_strategies": ["localStorage_restore_reload"],
            "expected_validity": "save-restorable",
        },
        "actions": scenario.actions,
        "assertions": assertions,
    }


# --------------------------------------------------------------------------
# Orchestration: synthesize, persist, verify, and link provenance
# --------------------------------------------------------------------------

def _select_move_candidate(workspace: Path) -> dict | None:
    """Return the top metric candidate that represents the move/merge region."""
    path = workspace / "reports" / "metrics" / "2048-candidates.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    for cand in data.get("candidates", []):
        fields = set((cand.get("semantic") or {}).get("matched_fields") or [])
        if "board" in fields and "score" in fields:
            return {"config": data.get("config"), "git_commit": data.get("git_commit"), "candidate": cand}
    cands = data.get("candidates") or []
    if cands:
        return {"config": data.get("config"), "git_commit": data.get("git_commit"), "candidate": cands[0]}
    return None


def synthesize_2048(
    *,
    workspace: Path,
    headless: bool = True,
    verify: bool = True,
    out_specs_dir: Path | None = None,
    out_report: Path | None = None,
) -> dict[str, Any]:
    import yaml

    out_specs_dir = out_specs_dir or (workspace / "specs" / "2048" / "generated")
    out_specs_dir.mkdir(parents=True, exist_ok=True)
    provenance_src = _select_move_candidate(workspace)

    results: list[dict[str, Any]] = []
    for scenario in SCENARIOS_2048:
        recorded = record_2048_afterstate(scenario, workspace=workspace, headless=headless)
        spec = build_2048_spec(scenario, recorded)
        spec_path = out_specs_dir / f"{scenario.id}.yaml"
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True), encoding="utf-8")

        entry: dict[str, Any] = {
            "task_id": scenario.id,
            "spec": str(spec_path.relative_to(workspace)),
            "branch": scenario.branch,
            "recorded_after": {
                "score": recorded["after"].get("score"),
                "won": recorded["after"].get("won"),
                "over": recorded["after"].get("over"),
                "delta_score": recorded["delta_score"],
                "tiles": sorted(_tile_values(recorded["after"]), reverse=True),
            },
            "assertions": spec["assertions"],
        }
        if verify:
            entry["verification"] = _verify_spec(spec_path, workspace, headless)
        results.append(entry)

    payload = {
        "game": "2048",
        "method": "execution-grounded (pinned Math.random, real game move, read-back)",
        "selected_region": provenance_src,
        "branch_coverage": [s.branch for s in SCENARIOS_2048],
        "tasks": results,
    }
    out_report = out_report or (workspace / "reports" / "oracle_synth" / "2048-provenance.json")
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    payload["_out_report"] = str(out_report)
    return payload


def _verify_spec(spec_path: Path, workspace: Path, headless: bool) -> dict[str, Any]:
    """Re-run the generated spec through the normal (unpinned) harness so the
    execution-grounded oracle is proven self-consistent and save-restorable."""
    try:
        from .runner import run_spec

        repo_arg = str((workspace / "games" / "2048").resolve())
        report = run_spec(str(spec_path), repo_arg=repo_arg, headless=headless)
        rd = report.to_dict()
        oracle = rd.get("oracle_result") or {}
        restoration = rd.get("checkpoint_restoration") or {}
        return {
            "status": oracle.get("status"),
            "oracle_detail": oracle.get("detail"),
            "restoration_status": restoration.get("status"),
            "validity": rd.get("validity") or rd.get("checkpoint_validity"),
        }
    except Exception as exc:  # environment-dependent; record rather than crash
        return {"status": "blocked", "reason": str(exc)[:300]}

