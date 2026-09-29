#!/usr/bin/env python3
"""Build a bounded, bucketed set of SpaceRPG GitHub issue drafts from a fuzz run.

Unlike ``.cursor/skills/github-issuer/scripts/build_issues.py`` (one issue per
unique injected save-state -> hundreds of issues), this groups defects by
``(category, injection_mode, defect_type)`` and emits only the top-K
representative *report fingerprints* per bucket (default 2), so a 10h run's ~676
detections attribute to ~20 focused, reproducible issues instead of ~600.

Each issue body is the same SWE-bench-Verified "good sample" shape as build_issues.py
(problem statement + exact localStorage recipe + state.json fixture + game-native
FAIL_TO_PASS repro.spec.ts + verbatim classification + suggested fix), carries a
hidden ``<!-- issue-fingerprint: <fp> -->`` dedup marker, and reuses
``scripts/build_spacerpg_defect_artifacts.py`` (FAMILY / ROOT_CAUSES / _repro_recipe /
_spec_ts) so the reproduction logic stays DRY.

Reads across ALL iteration report dirs under a run (``<run>/spacerpg/*/fuzz.json``).

Usage:
  python scripts/build_spacerpg_issues_bucketed.py --run reports/warptest/<run> [--per-bucket 2]
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PLAYER_REACHABLE_MODES = {"save_restore", "legacy_save", "save_index"}
BUGGY_MODES = {"state_overlay", "replace_state"}
ERRORBOUNDARY_RE = re.compile(r"error occurred in the <|error boundary|ErrorBoundary", re.I)
BASE_LABELS = ["warptest", "fuzz", "robustness", "spacerpg"]

CLASSIFICATION_TEXT = (
    "Player-reachable: the state can be reached by a player through normal play over some period of time, "
    "or from a specific save state.\n\n"
    "Buggy-not-reachable but worth fixing: the triggering state itself is not player-reachable, but the defect "
    "exposes a cross-cutting resilience gap that is independent of how it is triggered (a missing fault-tolerance "
    "mechanism that spans the whole application rather than any specific bug site), and that gap is also triggered "
    "by some defect already classified as player-reachable."
)
QUOTED_STANDARD = "\n".join(("> " + ln) if ln.strip() else ">" for ln in CLASSIFICATION_TEXT.splitlines())


def load_builder():
    path = ROOT / "scripts" / "build_spacerpg_defect_artifacts.py"
    spec = importlib.util.spec_from_file_location("_spacerpg_artifact_builder", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Failed to load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def classify(mode: str) -> tuple[str, bool]:
    if mode in PLAYER_REACHABLE_MODES:
        return "player-reachable", False
    if mode == "action_replay":
        return "player-reachable", True
    if mode in BUGGY_MODES:
        return "buggy-not-reachable", False
    return "buggy-not-reachable", True


def console_head(state: dict) -> str:
    console = state.get("console_errors") or []
    src = console[0] if console else (state.get("detail") or "")
    lines = [ln for ln in str(src).splitlines() if ln.strip()]
    return lines[0][:200] if lines else ""


def screenshot_fps(report: dict) -> dict[str, set]:
    """basename(screenshot) -> set of DEFECT report-fingerprints."""
    out: dict[str, set] = defaultdict(set)
    for result in report.get("results", []):
        for mut in result.get("mutations", []) or []:
            if mut.get("verdict") == "defect" and mut.get("fingerprint") and mut.get("screenshot"):
                out[Path(mut["screenshot"]).name].add(mut["fingerprint"])
    return out


def collect(run_dir: Path, builder) -> list[dict]:
    """Every persisted SpaceRPG defect dump across all iters, enriched with fingerprint(s) + recipe."""
    dumps: list[dict] = []
    for fuzz_json in sorted(glob.glob(str(run_dir / "spacerpg" / "*" / "fuzz.json"))):
        report = json.loads(Path(fuzz_json).read_text(encoding="utf-8"))
        report_dir = Path(fuzz_json).parent
        fps_by_shot = screenshot_fps(report)
        for result in report.get("results", []):
            task_id = result.get("task_id")
            ddir = report_dir / "tasks" / str(task_id) / "defects"
            if not ddir.exists():
                continue
            for sub in sorted(ddir.iterdir()):
                sf = sub / "state.json"
                if not sf.exists():
                    continue
                state = json.loads(sf.read_text(encoding="utf-8"))
                fps = sorted(fps_by_shot.get(state.get("screenshot_name", ""), []))
                state["report_fingerprints"] = fps
                # Primary fingerprint for grouping/dedup: first matching report fp,
                # else fall back to a stable hash of the exact injected target.
                if fps:
                    state["fingerprint"] = fps[0]
                else:
                    import hashlib
                    payload = json.dumps(state.get("target", {}), sort_keys=True, ensure_ascii=False)
                    state["fingerprint"] = "t" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:11]
                state["repro"] = builder._repro_recipe(state.get("target", {}))
                state["_report_dir"] = str(report_dir)
                dumps.append(state)
    return dumps


def _details(summary: str, lang: str, body: str) -> str:
    return f"<details><summary>{summary}</summary>\n\n```{lang}\n{body}\n```\n\n</details>"


def env_block(report: dict, commit: str) -> str:
    llm = report.get("llm_config", {})
    return (
        f"- Game: `games/SpaceRPG` (commit `{commit}`)\n"
        f"- Runtime: Chromium via Playwright, `npm run dev` (Vite DEV)\n"
        f"- Found by: WarpTest C3 state-fuzzing (`fuzz-bench`), LLM engine `{llm.get('model')}`\n"
        f"- Report: `{report.get('generated_at_utc')}`\n"
    )


def render_issue(
    rep: dict, category: str, mode: str, defect_type: str, fp: str,
    fp_count: int, bucket_count: int, report: dict, commit: str, builder, spec_ts: str,
) -> tuple[str, str]:
    fam = builder.FAMILY.get(mode, ("load", "A", ""))
    rc = builder.ROOT_CAUSES.get(fam[1], builder.ROOT_CAUSES["A"])
    recipe = rep.get("repro", {})
    fixture = json.dumps(rep, indent=2, ensure_ascii=False)
    head = console_head(rep)
    fp8 = fp[:8]

    symptom = {
        "blank_render": "blank/non-informative frame after loading a crafted save",
        "soft_lock": "whole app unmounts to a white screen (#root emptied)",
        "console_error": "unhandled error thrown while loading a crafted save",
    }.get(defect_type, defect_type)
    title = f"[SpaceRPG][{category}] {symptom} ({mode}, {fp8})"

    if category == "player-reachable":
        steps = (
            "1. Seed `localStorage` with the keys below (the game reads them itself).\n"
            "2. Open `/?warptest=1` and wait for `window.__SPACERPG_E2E__`.\n"
            "3. Call the game's own loader `__SPACERPG_E2E__.getState().loadGame(<slot>)` "
            "-- the production save-restore path.\n"
        )
        cls_rationale = (
            "The trigger is the game's real `localStorage` save "
            "(`spacerpg-save-slot:*` / the legacy `spacerpg-save` / "
            "`spacerpg-save-index`), parsed by the game's own loader. A corrupted or "
            "hand-edited save -- or a specific saved state -- reaches this path in "
            "production, so the defect is player-reachable."
        )
    else:
        steps = (
            "1. Seed `localStorage` with the dev-hook keys below.\n"
            "2. Open `/?warptest=1` and wait for `window.__SPACERPG_E2E__`.\n"
            "3. Apply the captured store state via `__SPACERPG_E2E__.setState(save, replace)`.\n"
        )
        cls_rationale = (
            "The trigger (`state_overlay` / `replace_state`) uses the `__SPACERPG_E2E__` dev "
            "hook, gated by `import.meta.env.DEV` and absent from production builds -- so "
            "the trigger state itself is not player-reachable. But the gap it reveals -- "
            "no top-level React ErrorBoundary, a cross-cutting fault-tolerance hole not "
            "bound to any single bug point -- is itself triggered by the player-reachable "
            "save crash (RC-A), which white-screens the whole app instead of degrading "
            "gracefully. Hence it is worth fixing."
        )

    body = f"""## Problem statement

Injection kind `{mode}` with an LLM-fuzzed deep state produces defect type
`{defect_type}`. Representative captured signature:

```
{head}
```

## Reproduction (exact input)

- Family: `{recipe.get('family')}`
- Load slot: `{recipe.get('load_slot')}`
- Cleared keys: `{recipe.get('clear_keys')}`

localStorage recipe (write these values, then load):

```json
{json.dumps(recipe.get('local_storage', {}), indent=2, ensure_ascii=False)}
```

Steps:

{steps}

{_details('state.json fixture (exact injected deep-state + repro recipe)', 'json', fixture)}

## Expected vs actual

- **Expected:** the injected state either loads without crashing or is rejected
  cleanly; the app stays interactive and the flight renderer stays alive.
- **Actual:** `{defect_type}` -- {'the injected numeric survives `normalizeSave()` and reaches three.js, crashing the renderer' if defect_type == 'blank_render' else ('React unmounts the tree and `#root` goes empty (white screen)' if defect_type == 'soft_lock' else 'a component throws during render (see the captured error above)')}.

## Environment

{env_block(report, commit)}
Observed on {fp_count} detection(s) for this fingerprint (`{fp}`); this
`{mode}`/`{defect_type}` class had {bucket_count} detection(s) in the run.

## Classification & rationale

**{category}.** Per the standard:

{QUOTED_STANDARD}

{cls_rationale}

## FAIL_TO_PASS regression test

Save the `state.json` fixture above next to this spec, then run it. It replays the
exact captured state through the game's own paths and asserts the app must NOT
crash/blank: it **FAILS on the current build (bug reproduced)** and passes once the
fix lands.

{_details('repro.spec.ts (game-native, LLM-independent)', 'ts', spec_ts)}

## Suggested fix

{rc['fix']}

<!-- issue-fingerprint: {fp} -->
"""
    return title, body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="Run dir containing spacerpg/*/fuzz.json.")
    ap.add_argument("--per-bucket", type=int, default=2, help="Top-K fingerprints per (category,mode,defect_type) bucket.")
    ap.add_argument("--out", default=None, help="Output dir (default: <run>/spacerpg/github-issues).")
    args = ap.parse_args()

    run_dir = Path(args.run).expanduser().resolve()
    builder = load_builder()
    spec_ts = builder._spec_ts()

    out_dir = Path(args.out).expanduser().resolve() if args.out else (run_dir / "spacerpg" / "github-issues")
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in [*out_dir.glob("*.md"), out_dir / "manifest.json"]:
        if stale.exists():
            stale.unlink()

    dumps = collect(run_dir, builder)
    if not dumps:
        print("No SpaceRPG defect dumps found.")
        return 1

    # Use the newest report for env/commit provenance.
    report_paths = sorted(glob.glob(str(run_dir / "spacerpg" / "*" / "fuzz.json")))
    last_report = (
        json.loads(Path(report_paths[-1]).read_text(encoding="utf-8"))
        if report_paths
        else {}
    )
    commit = (
        next((d.get("git_commit") for d in dumps if d.get("git_commit")), None)
        or (last_report.get("run_context", {}) or {}).get("git_commit")
        or "see state.json"
    )

    # bucket -> fingerprint -> list of dumps
    buckets: dict[tuple, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for d in dumps:
        mode = d.get("injection_mode", "unknown")
        dt = d.get("defect_type", "unknown")
        category, _ = classify(mode)
        buckets[(category, mode, dt)][d["fingerprint"]].append(d)

    issues: list[dict] = []
    n = 0
    for (category, mode, dt) in sorted(buckets):
        fp_map = buckets[(category, mode, dt)]
        bucket_count = sum(len(v) for v in fp_map.values())
        # top-K fingerprints by detection count
        ranked = sorted(fp_map.items(), key=lambda kv: (-len(kv[1]), kv[0]))[: args.per_bucket]
        for fp, members in ranked:
            rep = members[0]
            n += 1
            _, needs_review = classify(mode)
            title, body = render_issue(
                rep, category, mode, dt, fp, len(members), bucket_count,
                last_report, commit, builder, spec_ts,
            )
            labels = BASE_LABELS + [category] + (["needs-review"] if needs_review else [])
            safe_fp = re.sub(r"[^A-Za-z0-9]", "", fp)[:12]
            fname = f"{n:02d}-{category}-{mode}-{dt}-{safe_fp}.md"
            (out_dir / fname).write_text(body, encoding="utf-8")
            issues.append({
                "n": n, "title": title, "labels": labels, "body_file": fname,
                "fingerprint": fp, "category": category, "injection_mode": mode,
                "defect_type": dt, "needs_review": needs_review,
                "detections": len(members), "bucket_detections": bucket_count,
            })

    manifest = {
        "run": str(run_dir),
        "commit": commit,
        "per_bucket": args.per_bucket,
        "issues": issues,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    pr = sum(1 for i in issues if i["category"] == "player-reachable")
    bg = sum(1 for i in issues if i["category"] == "buggy-not-reachable")
    print(f"Wrote {len(issues)} issue drafts to {out_dir}")
    print(f"  player-reachable: {pr}   buggy-not-reachable: {bg}")
    for i in issues:
        flag = " [needs-review]" if i["needs_review"] else ""
        print(f"  {i['n']:02d} [{i['category']}] {i['injection_mode']}/{i['defect_type']} "
              f"x{i['detections']}(bucket {i['bucket_detections']}) {i['fingerprint'][:8]}{flag}")
    print(f"\nManifest: {out_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
