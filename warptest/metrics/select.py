"""Orchestration: run the full metric pipeline for a game and emit a ranked,
re-derivable candidate list to reports/metrics/<game>-candidates.json.

The emitted file records the config hash, the game git commit, and every raw +
normalised signal, so the ranking is fully auditable and reproducible.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import MetricConfig, default_config
from .static_metrics import analyze_paths
from .coupling import annotate_coupling
from .churn import annotate_churn
from .score import rank_candidates, build_semantic_anchors


@dataclass
class GameSource:
    game: str
    repo: str  # repo-relative to workspace root
    globs: list[str]
    reach_descriptor: dict | None = None
    # Whether to keep only candidates that sit on a per-file discovery anchor.
    # True for games where discovery pins specific source symbols (2048). False
    # for engines whose controllable surface is a whole registry driven by a
    # test worldmod (Luanti: the worldmod can set any registered node/meta/
    # player state, so every node/behaviour-defining function is controllable).
    require_surface: bool = True


# Per-game source scope + the deep-state landmark used for reachability.
# Kept small and explicit; extend when generalising past the PoC.
GAME_SOURCES: dict[str, GameSource] = {
    "2048": GameSource(
        game="2048",
        repo="games/2048",
        globs=["js/*.js"],
        reach_descriptor={"kind": "max_tile", "value": 1024},
    ),
    "luanti": GameSource(
        game="luanti",
        repo="games/luanti",
        # The game's own Lua mods define the node/item ids and behaviours that
        # tasks reference; the C++ engine is out of PoC scope.
        globs=[
            "games/devtest/mods/**/*.lua",
            "builtin/**/*.lua",
        ],
        reach_descriptor=None,
        require_surface=False,
    ),
}


def _git_commit(repo: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except Exception:
        return None


def _collect_files(repo: Path, globs: list[str]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for pattern in globs:
        for p in sorted(repo.glob(pattern)):
            if p.is_file() and p not in seen:
                seen.add(p)
                files.append(p)
    return files


def run_game(
    game: str,
    *,
    workspace: Path,
    cfg: MetricConfig | None = None,
    top: int = 25,
    per_line_churn: bool = True,
    reach_trials: int = 200,
    require_surface: bool | None = None,
    out_dir: Path | None = None,
) -> dict:
    cfg = cfg or default_config()
    if game not in GAME_SOURCES:
        raise KeyError(f"unknown game '{game}'; known: {sorted(GAME_SOURCES)}")
    src = GAME_SOURCES[game]
    if require_surface is None:
        require_surface = src.require_surface
    repo = (workspace / src.repo).resolve()
    if not repo.exists():
        raise FileNotFoundError(f"game repo not found: {repo}")

    files = _collect_files(repo, src.globs)
    metrics = analyze_paths(repo, files)
    annotate_coupling(repo, metrics)
    annotate_churn(repo, metrics, per_line=per_line_churn)

    anchors = build_semantic_anchors(repo)
    candidates = rank_candidates(
        repo,
        metrics,
        game=game,
        cfg=cfg,
        anchors=anchors,
        reach_descriptor=src.reach_descriptor,
        require_surface=require_surface,
        reach_trials=reach_trials,
    )

    payload = {
        "game": game,
        "repo": src.repo,
        "git_commit": _git_commit(repo),
        "config": cfg.to_dict(),
        "files_analyzed": [str(f.relative_to(repo)) for f in files],
        "n_functions": len(metrics),
        "n_candidates": len(candidates),
        "semantic_anchor_count": len(anchors),
        "candidates": [c.to_dict() for c in candidates[:top]],
    }

    out_dir = out_dir or (workspace / "reports" / "metrics")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{game}-candidates.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    payload["_out_path"] = str(out_path)
    return payload
