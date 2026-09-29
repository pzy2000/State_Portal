#!/usr/bin/env python3
"""Dev-only guard for the explore-hard variant (benchmarks/core/public/tasks_explore.yaml).

For every de-leaked issue this asserts the three-part contract that keeps the variant
both hard and solvable:

  (i)  INTENT KEPT      - every grader-critical token that is NOT deliberately stripped
                          (target values, kept ids, kept action args) is still
                          recoverable from the issue prose. This catches accidental
                          information loss that would make a task unsolvable.
  (ii) MECHANISM HIDDEN - every deliberately stripped token is absent from the issue
                          (case/separator-insensitive). This is what forces exploration.
 (iii) DISCOVERABLE     - every stripped token has a discovery source:
                            * contract tokens (checkpoint strategy name + validity class)
                              come from the per-benchmark contract WarpTest already hands
                              the exploration agent (warptest/synthesis.py BENCHMARK_CONTRACTS);
                            * repo tokens (hook/method names, action ids, internal ids,
                              scenario/mission ids) must be grep-able in the task's game repo.

Not part of the benchmark pipeline. Usage:
    python scripts/_check_explore_issues.py [task_id ...]
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = ROOT / "benchmarks/core/public/tasks_explore.yaml"
ORACLES_PATH = ROOT / "benchmarks/core/hidden/oracles.yaml"

# Timing knobs the strict grader strips before comparing actions; not required tokens.
TIMING = {"wait_ms", "ms", "timeout_ms", "delay_ms"}

# Action types that read as plain English and are deliberately kept in the prose
# ("a single Left move", "presses the ArrowUp key", "a short wait", the game's own
# save routine invoked via a hook). They are NOT treated as stripped mechanism, so the
# auto-collector below skips them.
GENERIC_ACTION_TYPES = {"move", "key", "wait", "hook_call", "hook_action"}

# Repo-sourced mechanism tokens intentionally removed from each issue. The model must
# read the game repository to recover them. Strategy names, validity classes and
# framework action-type names (e.g. luanti_set_node, openra_validate_map) are NOT listed
# here: they are derived automatically from the hidden spec and treated as
# contract-sourced (the exploration prompt already provides them per benchmark).
#
# Only tokens that are literally grep-able in the task's game repository belong here.
# Values that a model cannot discover from the repo (designer-chosen paths/content, or
# ids synthesised at runtime such as luanti's dynamically registered stairs:slab_* nodes)
# are kept in the prose instead.
REPO_TOKENS: dict[str, list[str]] = {
    # --- browser subset (2048 / SpaceRPG / Love) ---
    "2048-merge-left": [],
    "2048-near-win": [],
    "2048-near-loss": [],
    "spacerpg-playable-flight": [],
    "spacerpg-station-save": ["saveGame"],
    "spacerpg-save-restore": ["saveGame", "setScreen", "loadGame"],
    "spacerpg-galaxy-jump": ["jumpToSystem"],
    "spacerpg-trading-cargo": [],
    "spacerpg-equipment-loadout": ["installEquipmentFromInventory"],
    "spacerpg-story-mission": ["glass-wake-hero", "story-probe-in-glass", "story-clean-carrier"],
    "love-resource-action": ["work_coding"],
    "love-cafe-date": ["date_coffee"],
    "love-dialogue-open": ["startDialogue", "char_1"],
    "love-relationship-update": ["applyRelationshipUpdates", "char_1"],
    "love-ending-precondition": ["char_1"],
    # --- OpenRA: internal actor/unit type codes -> human unit names ---
    "cnc-gdi01-beachhead": ["e1", "gun"],
    "cnc-nod03a-prison-break": ["jeep", "e1"],
    "cnc-hot-box-dropzone": ["apc", "mpspawn"],
    "ra-allies03a-dead-end": ["e1", "e2"],
    "ra-soviet05-distant-thunder": ["gun", "2tnk", "e1"],
    "ra-drop-zone": ["apc", "mpspawn"],
    "ra-soviet05-war-factory-heavy-tank": ["powr", "proc", "weap", "fix", "3tnk"],
    "ra-soviet05-barracks-rocket-infantry": ["powr", "barr", "e1"],
    "ra-allies07-soviet-war-factory-produces-heavy-tank": ["weap", "3tnk"],
    "ra-allies05a-soviet-war-factory-produces-vehicle": ["weap", "3tnk"],
    # --- jynew: designer-chosen numeric state values are kept; only mechanism stripped ---
    "jynew-team-join": [],
    "jynew-level-up": [],
    "jynew-skill-learn": [],
    "jynew-save-restore": [],
    "jynew-money-trade": [],
    # --- pal3: numeric story-var/scene values are kept; only mechanism stripped ---
    "pal3-story-progress": [],
    "pal3-scene-load": [],
    "pal3-scene-transition": [],
    "pal3-team-join": [],
    "pal3-team-leave": [],
    "pal3-item-add": [],
    "pal3-money-add": [],
    "pal3-world-region": [],
    "pal3-favor-add": [],
    "pal3-save-restore": [],
    # --- luanti: node/item ids -> human material names (slabs are runtime-registered
    #     and not grep-able, so those stay literal in the prose) ---
    "luanti-set-node": ["basenodes:dirt", "basenodes:stone", "chest:chest"],
    "luanti-dig-node": ["basenodes:cobble"],
    "luanti-fill-floor": ["basenodes:cobble", "basenodes:tree"],
    "luanti-replace-node": ["basenodes:stone", "basenodes:cobble", "chest:chest"],
    "luanti-build-column": ["basenodes:tree"],
    "luanti-chest-inventory": ["chest:chest", "basenodes:cobble", "basenodes:tree"],
    "luanti-player-teleport": ["basenodes:stone"],
    "luanti-player-hp": [],
    "luanti-player-inventory": ["basenodes:cobble", "basetools:pick_steel"],
    "luanti-mod-storage": [],
    "luanti-save-restore": ["basenodes:stone", "basetools:pick_steel"],
    "luanti-complex-house-workshop": [
        "basenodes:dirt_with_grass", "basenodes:cobble", "basenodes:tree",
        "chest:chest", "basetools:pick_steel", "stairs:stair_cobble",
    ],
    "luanti-irrigated-farmstead": [
        "basenodes:dirt_with_grass", "basenodes:dirt", "basenodes:water_source",
        "basenodes:cobble", "basenodes:junglegrass", "chest:chest", "basenodes:apple",
    ],
    "luanti-storage-workshop": [
        "basenodes:dirt_with_grass", "basenodes:stone", "basenodes:cobble",
        "basenodes:tree", "basenodes:leaves", "basetools:pick_steel",
        "basenodes:mossycobble", "chest:chest",
    ],
    "luanti-mine-bridge-outpost": [
        "basenodes:gravel", "basenodes:water_source", "basenodes:tree",
        "basenodes:stone", "basenodes:cobble", "stairs:stair_cobble",
    ],
    "luanti-homestead-save-restore": [
        "basenodes:dirt_with_grass", "basenodes:cobble", "basenodes:tree",
        "basenodes:dirt", "basenodes:junglegrass", "chest:chest",
        "basenodes:apple", "basetools:pick_steel", "stairs:stair_cobble",
    ],
    # --- JupyterLab & Blender: target paths/content/object names are designer-chosen
    #     (not repo-discoverable); only mechanism is stripped ---
    "jupyterlab-text-file-edit": [],
    "jupyterlab-notebook-cell-output": [],
    "jupyterlab-workspace-layout": [],
    "jupyterlab-session-smoke": [],
    "jupyterlab-markdown-file-save": [],
    "jupyterlab-notebook-markdown-note": [],
    "blender-scene-cube-material": [],
    "blender-save-reopen-scene": [],
    "blender-modifier-apply": [],
    "blender-animation-keyframes": [],
}


def norm(s: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def token_present(issue: str, tok: str) -> bool:
    """True when ``tok`` appears as a delimited token in the issue.

    Matching is case-insensitive and separator-insensitive *within* the token
    (so ``basenodes:cobble`` also catches ``basenodes cobble``), but requires
    alphanumeric boundaries on both ends. This avoids false positives where a
    short id (``e1``, ``ra``, ``gun``, ``barr``) is an incidental substring of an
    ordinary word (``level1``, ``restorable``, ``barracks``) while still catching
    a genuine leak of the literal id.
    """
    runs = [r for r in re.split(r"[^0-9a-zA-Z]+", tok) if r]
    if not runs:
        return False
    pattern = (
        r"(?<![0-9a-z])"
        + r"[^0-9a-z]*".join(re.escape(r.lower()) for r in runs)
        + r"(?![0-9a-z])"
    )
    return re.search(pattern, issue.lower()) is not None


def action_type_tokens(spec: dict) -> list[str]:
    """Framework action-type names from the hidden spec, minus generic English ones.

    These are provided to the exploration agent by the per-benchmark contract, so
    they are treated as contract-sourced (not required in the issue, not grep'd),
    but they must still be absent from the de-leaked prose.
    """
    out: list[str] = []
    for action in spec.get("actions") or []:
        if isinstance(action, dict):
            kind = action.get("type")
            if kind and kind not in GENERIC_ACTION_TYPES:
                out.append(str(kind))
    return out


def collect(v: object, out: list[tuple[str, str]]) -> None:
    if isinstance(v, dict):
        for k, val in v.items():
            if k in TIMING:
                continue
            collect(val, out)
    elif isinstance(v, list):
        for val in v:
            collect(val, out)
    elif v is not None:
        if isinstance(v, bool):
            out.append((("true" if v else "false"), "bool"))
        elif isinstance(v, (int, float)):
            f = float(v)
            out.append(((str(int(f)) if f.is_integer() else str(f)), "num"))
        elif "\n" in str(v):
            out.append((str(v), "multiline"))
        else:
            out.append((str(v), "str"))


def recoverable(issue: str, raw: str, kind: str) -> bool:
    if kind == "num":
        return re.search(r"(?<![\d.])" + re.escape(raw) + r"(?![\d])", issue) is not None
    if kind == "bool":
        return raw in issue.lower()
    if kind == "multiline":
        words = [w for w in re.split(r"[^a-z0-9]+", raw.lower()) if w]
        ni = norm(issue)
        return all(w in ni for w in words)
    return norm(raw) in norm(issue)


def repo_contains(repo: Path, token: str) -> bool:
    """True when the literal token appears anywhere under the repo (respecting .gitignore)."""
    if shutil.which("rg"):
        proc = subprocess.run(
            ["rg", "--fixed-strings", "--quiet", token, str(repo)],
            capture_output=True,
        )
        return proc.returncode == 0
    # Fallback: git grep from within the repo (also respects tracked files only).
    proc = subprocess.run(
        ["git", "-C", str(repo), "grep", "--quiet", "--fixed-strings", token],
        capture_output=True,
    )
    return proc.returncode == 0


def main() -> int:
    tasks = yaml.safe_load(TASKS_PATH.read_text())["tasks"]
    by_id = {t["id"]: t for t in tasks}
    oracles = {
        o["task_id"]: o["hidden_spec"]
        for o in yaml.safe_load(ORACLES_PATH.read_text())["oracles"]
    }

    requested = sys.argv[1:] or list(by_id)
    bad_any = False
    for tid in requested:
        if tid not in by_id:
            print(f"MISSING {tid}: not present in tasks_explore.yaml")
            bad_any = True
            continue
        task = by_id[tid]
        issue = task["issue"]
        repo = ROOT / task["repo"]
        spec = yaml.safe_load((ROOT / oracles[tid]).read_text())

        checkpoint = spec.get("checkpoint") or {}
        contract_tokens = list(checkpoint.get("allowed_strategies", []))
        validity = checkpoint.get("expected_validity")
        if validity:
            contract_tokens.append(validity)
        contract_tokens.extend(action_type_tokens(spec))
        repo_tokens = REPO_TOKENS.get(tid, [])
        stripped = contract_tokens + repo_tokens
        stripped_norm = {norm(tok) for tok in stripped}

        # (i) intent kept: the explore grade only structurally checks target intent
        # (actions/strategy are discovered, not transcribed), so intent recovery is
        # scoped to target tokens. Board empty cells encoded as 0 are the accepted
        # "empty cell" convention and are described in prose as empty, not as 0.
        req: list[tuple[str, str]] = []
        collect(spec.get("target"), req)
        missing = sorted(
            {
                (r, k)
                for (r, k) in req
                if norm(r) not in stripped_norm
                and not (k == "num" and r == "0")
                and not recoverable(issue, r, k)
            }
        )

        # (ii) mechanism hidden: stripped tokens must be absent from the issue.
        leaked = sorted({tok for tok in stripped if token_present(issue, tok)})

        # (iii) discoverable: repo tokens must be grep-able in the game repo.
        undiscoverable = sorted({tok for tok in repo_tokens if not repo_contains(repo, tok)})

        if missing or leaked or undiscoverable:
            bad_any = True
            print(f"REVIEW {tid}")
            if missing:
                print("   intent-not-recoverable:", missing)
            if leaked:
                print("   mechanism-leaked:", leaked)
            if undiscoverable:
                print("   not-in-repo:", undiscoverable)
        else:
            print(f"OK     {tid}  (stripped: {stripped})")
    return 1 if bad_any else 0


if __name__ == "__main__":
    sys.exit(main())
