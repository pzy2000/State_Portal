#!/usr/bin/env python3
"""Generate persistent before/after golden screenshots for SpaceRPG core samples.

For each SpaceRPG core task declared in ``benchmarks/core/hidden/oracles.yaml`` this
script runs the hidden spec twice on the live SpaceRPG runtime:

- before: the spec with ``actions`` cleared, so the screenshot is the restored
  target state prior to any smoke actions.
- after: the original spec, so the screenshot is the post-smoke state.

Both screenshots are copied into ``specs/spacerpg/visual/`` as
``<task-id>.before.golden.png`` and ``<task-id>.after.golden.png``. The single
``golden_image`` already referenced by each spec is left untouched, so existing
single-image visual oracles keep working; updating the spec YAML to reference the
new pair is done separately (see ``update`` step in the plan).

Usage:
    python3 scripts/generate_spacerpg_visual_pairs.py \
        [--task spacerpg-playable-flight ...] [--headed] [--wait-ms 400]
"""
from __future__ import annotations

import argparse
import copy
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from warptest.runner import run_spec  # noqa: E402
from warptest.c1_evaluation import materialize_legacy_spec  # noqa: E402
from warptest.task_schema import load_hidden_oracles, resolve_hidden_spec  # noqa: E402
from warptest.visual import screenshot_quality  # noqa: E402

# The SpaceRPG core samples graded by benchmarks/core/hidden/oracles.yaml.
SPACERPG_CORE_TASK_IDS = [
    "spacerpg-playable-flight",
    "spacerpg-station-save",
    "spacerpg-save-restore",
    "spacerpg-galaxy-jump",
    "spacerpg-trading-cargo",
    "spacerpg-equipment-loadout",
    "spacerpg-story-mission",
]

# Per-task capture overrides. Some SpaceRPG station samples pop the "Clean Carrier
# Briefing" story dialogue on dock, which obscures the market/hangar; closeDialogue
# (a getState() store action, callable via hook_call) dismisses it before capture.
# save-restore's BEFORE frame should instead show the in-space pause menu with the
# Manual Slot save list, which is reached by saving then opening screen "pause".
CLOSE_DIALOGUE = {"type": "hook_call", "method": "closeDialogue"}
TASK_OVERRIDES: dict[str, dict[str, Any]] = {
    "spacerpg-trading-cargo": {
        "before_actions": [CLOSE_DIALOGUE],
        "after_extra_actions": [CLOSE_DIALOGUE],
    },
    "spacerpg-equipment-loadout": {
        "before_actions": [CLOSE_DIALOGUE],
        "after_extra_actions": [CLOSE_DIALOGUE],
    },
    "spacerpg-save-restore": {
        # BEFORE = the Manual Slot save menu (pause menu in space).
        "before_actions": [
            {"type": "hook_call", "method": "saveGame", "args": ["manual-1"]},
            CLOSE_DIALOGUE,
            {"type": "hook_call", "method": "setScreen", "args": ["pause"]},
        ],
        # AFTER frame (clean station after load) was already accepted; keep it.
        "regen_after": False,
    },
}

VISUAL_DIR = REPO_ROOT / "specs" / "spacerpg" / "visual"
SPACERPG_REPO = REPO_ROOT / "games" / "SpaceRPG"


def _run_and_capture(
    spec: dict[str, Any],
    *,
    stem: str,
    headless: bool,
    wait_ms: int,
    work_dir: Path,
) -> Path:
    """Run a SpaceRPG spec once and return the captured screenshot path."""
    spec_path = work_dir / f"{stem}.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=False), encoding="utf-8")
    report_dir = work_dir / f"{stem}-run"
    report = run_spec(
        spec_path,
        repo_arg=str(SPACERPG_REPO),
        report_dir=report_dir,
        headless=headless,
        oracle_policy="semantic",
    )
    # The SpaceRPG profile always captures spacerpg-final.png via _complete_report.
    if not report.screenshots:
        raise RuntimeError(f"{stem}: no screenshot captured (restore status={report.checkpoint_restoration.status}).")
    shot = Path(report.screenshots[0])
    if not shot.exists():
        raise RuntimeError(f"{stem}: screenshot path missing on disk: {shot}")
    if wait_ms:
        # A short settle is already implied by restore+validate+smoke timing; this
        # is only a guard so very fast runs do not capture a mid-transition frame.
        time.sleep(wait_ms / 1000.0)
    return shot


def generate_for_task(
    task_id: str,
    hidden_spec_path: Path,
    *,
    headless: bool,
    wait_ms: int,
) -> dict[str, Any]:
    raw_spec = yaml.safe_load(hidden_spec_path.read_text(encoding="utf-8"))
    if not isinstance(raw_spec, dict):
        raise ValueError(f"{task_id}: hidden spec is not a mapping: {hidden_spec_path}")
    spec = materialize_legacy_spec(raw_spec)
    spec.pop("evaluation", None)

    override = TASK_OVERRIDES.get(task_id, {})
    regen_before = override.get("regen_before", True)
    regen_after = override.get("regen_after", True)

    before_spec = copy.deepcopy(spec)
    # By default the before frame is the restored target with no smoke; a task may
    # override the before actions (e.g. to dismiss a dialogue or open a menu).
    before_spec["actions"] = copy.deepcopy(override.get("before_actions", []))
    # Drop assertions for the before pass: they describe the post-smoke state and
    # would only add noise; the screenshot is captured regardless of oracle status.
    before_spec["assertions"] = []

    after_spec = copy.deepcopy(spec)
    after_extra = override.get("after_extra_actions")
    if after_extra:
        after_spec["actions"] = [*spec.get("actions", []), *copy.deepcopy(after_extra)]

    # The temp specs live outside specs/spacerpg/, so the relative golden_image path
    # would fail schema validation. We run under the semantic policy (no visual
    # judge) and the SpaceRPG profile captures spacerpg-final.png regardless, so the
    # visual_oracle block is unnecessary here and is dropped from both passes.
    before_spec.pop("visual_oracle", None)
    after_spec.pop("visual_oracle", None)

    VISUAL_DIR.mkdir(parents=True, exist_ok=True)
    before_dest = VISUAL_DIR / f"{task_id}.before.golden.png"
    after_dest = VISUAL_DIR / f"{task_id}.after.golden.png"

    with tempfile.TemporaryDirectory(prefix=f"spacerpg-pair-{task_id}-") as tmp:
        work_dir = Path(tmp)
        if regen_before:
            before_shot = _run_and_capture(
                before_spec, stem=f"{task_id}-before", headless=headless, wait_ms=wait_ms, work_dir=work_dir
            )
            shutil.copyfile(before_shot, before_dest)
        if regen_after:
            after_shot = _run_and_capture(
                after_spec, stem=f"{task_id}-after", headless=headless, wait_ms=wait_ms, work_dir=work_dir
            )
            shutil.copyfile(after_shot, after_dest)

    before_quality = screenshot_quality(before_dest) if before_dest.exists() else {"informative": False}
    after_quality = screenshot_quality(after_dest) if after_dest.exists() else {"informative": False}
    return {
        "task_id": task_id,
        "before": str(before_dest.relative_to(REPO_ROOT)),
        "after": str(after_dest.relative_to(REPO_ROOT)),
        "before_regenerated": regen_before,
        "after_regenerated": regen_after,
        "before_informative": before_quality["informative"],
        "after_informative": after_quality["informative"],
        "before_quality": before_quality,
        "after_quality": after_quality,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--task",
        action="append",
        dest="tasks",
        help="Limit to specific SpaceRPG core task ids. Repeatable. Defaults to all 7.",
    )
    parser.add_argument("--headed", action="store_true", help="Show the browser window while capturing.")
    parser.add_argument("--wait-ms", type=int, default=400, help="Settle wait after each run before reusing the frame.")
    args = parser.parse_args()

    oracles = load_hidden_oracles(REPO_ROOT)
    selected = args.tasks or SPACERPG_CORE_TASK_IDS
    unknown = [task_id for task_id in selected if task_id not in SPACERPG_CORE_TASK_IDS]
    if unknown:
        parser.error(f"Unknown SpaceRPG core task id(s): {', '.join(unknown)}")

    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for task_id in selected:
        oracle = oracles.get(task_id)
        if oracle is None:
            failures.append(f"{task_id}: not found in hidden oracles")
            continue
        hidden_spec_path = resolve_hidden_spec(REPO_ROOT, oracle)
        print(f"[spacerpg-pairs] generating {task_id} from {hidden_spec_path.relative_to(REPO_ROOT)} ...", flush=True)
        try:
            result = generate_for_task(
                task_id, hidden_spec_path, headless=not args.headed, wait_ms=args.wait_ms
            )
        except Exception as error:  # noqa: BLE001 - report and continue per task
            failures.append(f"{task_id}: {error}")
            print(f"[spacerpg-pairs] FAILED {task_id}: {error}", flush=True)
            continue
        results.append(result)
        flag_before = "ok" if result["before_informative"] else "NON-INFORMATIVE"
        flag_after = "ok" if result["after_informative"] else "NON-INFORMATIVE"
        print(f"[spacerpg-pairs] done {task_id}: before={flag_before} after={flag_after}", flush=True)

    print("\n[spacerpg-pairs] summary:")
    for result in results:
        print(
            f"  {result['task_id']}: before={result['before']} ({'ok' if result['before_informative'] else 'NON-INFORMATIVE'}), "
            f"after={result['after']} ({'ok' if result['after_informative'] else 'NON-INFORMATIVE'})"
        )
    if failures:
        print("\n[spacerpg-pairs] failures:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
