#!/usr/bin/env python3
"""Generate self-contained, reproducible defect artifacts from a SpaceRPG fuzz report.

Reads a ``fuzz.json`` produced by ``warptest fuzz-bench`` plus the per-defect dumps
the harness persists under ``tasks/<task>/defects/<screenshot-stem>/`` and emits, per
detection, an LLM-independent reproduction that replays the exact captured deep-state
through the game's OWN paths (real save loader for the save/legacy/index kinds, live
``__SPACERPG_E2E__.setState`` for the overlay/replace kinds):

    reports/<ts>/spacerpg-defect-artifacts/
      ISSUE.md                 # consolidated GitHub issue draft (root causes A/B)
      README.md                # overview table of every detection
      reproduce-all.sh         # one-click: run every detection's Playwright repro
      detections/<NN>-<mode>-<fp>/
        state.json             # exact injected state + a self-contained repro recipe
        console.log            # full captured console errors
        screenshot.png         # captured (blank/broken) frame
        repro.spec.ts          # game-native Playwright regression test
        reproduce.sh           # one-click repro for this single detection

Usage:
    python scripts/build_spacerpg_defect_artifacts.py --report reports/warptest/<ts>/fuzz.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GAME_DIR = ROOT / "games" / "SpaceRPG"

HOOK_LS = {"spacerpg-e2e-hook": "enabled", "spacerpg-e2e-disable-economy-backend": "true"}
SLOT_PREFIX = "spacerpg-save-slot:"
SAVE_KEY = "spacerpg-save"
INDEX_KEY = "spacerpg-save-index"

# injection_mode -> (family, root-cause id, human label)
FAMILY = {
    "save_restore": ("load", "A", "NaN/invalid numeric survives normalizeSave() and breaks the three.js renderer (real save-load path)"),
    "legacy_save": ("load", "A", "Same NaN/invalid-numeric renderer crash via the legacy-save migration path"),
    "save_index": ("load", "A", "Same NaN/invalid-numeric renderer crash via the save-index resolution path"),
    "state_overlay": ("memory", "B", "No top-level ErrorBoundary: a shallow-merged inconsistent store state unmounts the whole app"),
    "replace_state": ("memory", "B", "No top-level ErrorBoundary: a full-store replace drops fields/methods and unmounts the whole app"),
    "action_replay": ("action", "C", "Adversarial store-action sequence crashes a reducer/component"),
}

ROOT_CAUSES = {
    "A": {
        "title": "Crafted/corrupted save crashes the flight renderer (NaN reaches three.js geometry)",
        "detail": (
            "`loadGame()` -> `normalizeSave()` ([src/systems/save.ts](../../../../games/SpaceRPG/src/systems/save.ts)) "
            "fills defaults for missing fields but does NOT validate that numeric fields are finite or correctly typed. "
            "A save whose numeric field (e.g. `player.position`/`velocity`/`rotation` or a stat) is `NaN`/`Infinity`/a "
            "type-flipped value passes normalization and is hydrated into the store. The flight scene then feeds it into "
            "three.js, producing `THREE.LineSegmentsGeometry.computeBoundingSphere(): Computed radius is NaN`, and the "
            "flight render breaks. This is reachable in production via a corrupted or hand-edited save in localStorage "
            "(`spacerpg-save-slot:*` / the legacy `spacerpg-save` / a malformed `spacerpg-save-index`)."
        ),
        "fix": (
            "Harden `normalizeSave()` to reject or clamp non-finite / wrong-typed numeric fields (player position/velocity/"
            "rotation, stats, credits, gameClock, etc.) before hydration, and/or sanitize vectors at the render boundary."
        ),
    },
    "B": {
        "title": "No top-level React ErrorBoundary: one component render error white-screens the entire app",
        "detail": (
            "`App.tsx` renders `AudioRuntime`, the screen components and `Hud` directly with no wrapping error boundary "
            "([src/App.tsx](../../../../games/SpaceRPG/src/App.tsx): `AudioRuntime` and the per-screen returns). "
            "`FlightScene.tsx` has only a LOCAL boundary ([src/components/FlightScene.tsx](../../../../games/SpaceRPG/src/components/FlightScene.tsx)) "
            "that does not protect its siblings. When any component throws during render (e.g. from an inconsistent store "
            "state), React unmounts the whole tree and `#root` goes empty -- a full white screen with no recovery UI."
        ),
        "fix": (
            "Wrap the app component tree in a top-level React ErrorBoundary in `App.tsx` with a fallback UI (and a "
            "'reset to menu / reload' affordance), so a single render error degrades gracefully instead of blanking the app."
        ),
    },
    "C": {
        "title": "Adversarial store-action sequence crashes a reducer or component",
        "detail": "An adversarial sequence of store actions drove the app into a crashing/blank state.",
        "fix": "Guard the affected action(s)/reducer(s) against invalid arguments and add the top-level ErrorBoundary (see B).",
    },
}


def _valid_index(save: dict, slot: str) -> dict:
    player = save.get("player") if isinstance(save.get("player"), dict) else {}
    summary = {
        "id": slot, "label": slot, "exists": True,
        "savedAt": save.get("savedAt") or "2020-01-01T00:00:00.000Z",
        "screen": save.get("screen"), "currentSystemId": save.get("currentSystemId"),
        "currentStationId": save.get("currentStationId"), "credits": player.get("credits"),
        "gameClock": save.get("gameClock"), "version": 3,
    }
    return {"version": 3, "slots": {slot: summary}, "lastPlayedSlotId": slot}


def _corrupt_index(save: dict, slot: str) -> dict:
    index = _valid_index(save, slot)
    index["slots"]["__garbage_slot__"] = {"unexpected": True, "exists": "yes"}
    index["slots"][slot]["credits"] = "not-a-number"
    index["slots"][slot]["gameClock"] = None
    index["extraTopLevelGarbage"] = [1, 2, {"nested": None}]
    return index


def _repro_recipe(target: dict) -> dict:
    """Turn a persisted injection target into a self-contained localStorage/hook recipe."""

    kind = target.get("kind", "")
    save = target.get("save")
    slot = "auto"
    if kind == "fuzz_save_restore":
        values = {**HOOK_LS, f"{SLOT_PREFIX}{slot}": json.dumps(save), INDEX_KEY: json.dumps(_valid_index(save, slot))}
        return {"family": "load", "local_storage": values, "clear_keys": [], "load_slot": slot}
    if kind == "fuzz_legacy_save":
        values = {**HOOK_LS, SAVE_KEY: json.dumps(save)}
        return {"family": "load", "local_storage": values, "clear_keys": [INDEX_KEY], "load_slot": None}
    if kind == "fuzz_save_index":
        values = {**HOOK_LS, f"{SLOT_PREFIX}{slot}": json.dumps(save), INDEX_KEY: json.dumps(_corrupt_index(save, slot))}
        return {"family": "load", "local_storage": values, "clear_keys": [], "load_slot": None}
    if kind == "fuzz_state_overlay":
        return {"family": "memory", "local_storage": dict(HOOK_LS), "clear_keys": [], "setState_save": save, "replace": False}
    if kind == "fuzz_replace_state":
        return {"family": "memory", "local_storage": dict(HOOK_LS), "clear_keys": [], "setState_save": save, "replace": True}
    if kind == "fuzz_action_replay":
        return {"family": "action", "local_storage": dict(HOOK_LS), "clear_keys": [], "actions": target.get("actions")}
    return {"family": "unknown", "local_storage": dict(HOOK_LS), "clear_keys": []}


SPEC_TEMPLATE = r'''import { test, expect } from "@playwright/test";
import { readFileSync } from "fs";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";

// Self-contained, LLM-independent reproduction of a SpaceRPG deep-state defect found
// by C3 state-fuzzing. It replays the EXACT captured state through the game's own
// paths and asserts the app does NOT crash/blank. It is a REGRESSION test: it FAILS
// on the current build (bug reproduced) and PASSES once the app sanitizes the state
// / adds a top-level ErrorBoundary. Run via ../reproduce.sh.

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(readFileSync(resolve(here, "state.json"), "utf-8"));
const recipe = fixture.repro;
// Concrete crash signatures only. "error occurred in the <" / "error boundary" are
// React's own diagnostic whenever ANY boundary catches an error -- they fire just as
// reliably on a caught-and-recovered render error as on a genuine crash, so matching
// them here would flag a working ErrorBoundary recovery as a regression. Whether the
// app actually stayed usable is decided by rootChildren below (the real post-recovery
// screen), not by this log text.
const CRASH = /Computed radius is NaN|LineSegmentsGeometry|computeBoundingSphere|\bNaN\b/i;
// A render error IS a graceful recovery (not a crash) when a boundary caught it AND
// the page still shows something (rootChildren > 0) -- see GRACEFUL_RECOVERY below.
const GRACEFUL_RECOVERY = /recovered from an unexpected render error|error boundary you provided/i;

test(`SpaceRPG defect [${fixture.injection_mode}] ${fixture.fingerprint ?? ""}`.trim(), async ({ page }) => {
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });

  await page.addInitScript((ls) => {
    for (const k of (ls.clear ?? [])) localStorage.removeItem(k as string);
    for (const [k, v] of Object.entries(ls.values ?? {})) localStorage.setItem(k as string, v as string);
  }, { values: recipe.local_storage, clear: recipe.clear_keys });

  await page.goto("/?warptest=1");
  await page.waitForFunction(() => typeof (window as any).__SPACERPG_E2E__?.getState === "function", null, { timeout: 30000 });

  if (recipe.family === "load") {
    // Drive the game's OWN save loader -- the production save-restore path.
    const loaded = await page.evaluate((slot) => {
      const store = (window as any).__SPACERPG_E2E__.getState();
      store.stopEconomyStream?.();
      const ok = store.loadGame ? store.loadGame(slot ?? undefined) : false;
      (window as any).__SPACERPG_E2E__.getState().closeDialogue?.();
      return ok === undefined ? true : ok;
    }, recipe.load_slot ?? null);
    expect(loaded, "loadGame() accepted the save (normalizeSave passed) - so any crash below is a render-time defect").toBeTruthy();
  } else if (recipe.family === "memory") {
    await page.evaluate(() => {
      const s = (window as any).__SPACERPG_E2E__.getState();
      s.newGame?.(); s.closeDialogue?.(); s.stopEconomyStream?.();
    });
    await page.waitForTimeout(300);
    await page.evaluate(({ save, replace }) => {
      const hook = (window as any).__SPACERPG_E2E__;
      hook.getState().stopEconomyStream?.();
      hook.getState().closeDialogue?.();
      hook.setState(save, replace === true);
    }, { save: recipe.setState_save, replace: recipe.replace });
  } else if (recipe.family === "action") {
    await page.evaluate(() => {
      const s = (window as any).__SPACERPG_E2E__.getState();
      s.newGame?.(); s.closeDialogue?.(); s.stopEconomyStream?.();
    });
    await page.waitForTimeout(300);
    await page.evaluate((actions) => {
      const hook = (window as any).__SPACERPG_E2E__;
      for (const a of (actions ?? [])) {
        if (!a || typeof a !== "object") continue;
        const fn = hook.getState()[a.method];
        if (typeof fn !== "function") continue;
        try { fn(...(Array.isArray(a.args) ? a.args : [])); } catch (e) { /* keep going */ }
      }
      hook.getState().closeDialogue?.();
    }, recipe.actions);
  }

  await page.waitForTimeout(800);
  await page.evaluate(() => (document as any).fonts?.ready).catch(() => {});

  const rootChildren = await page.evaluate(() => document.getElementById("root")?.childElementCount ?? 0);
  const recovered = consoleErrors.some((e) => GRACEFUL_RECOVERY.test(e)) && rootChildren > 0;
  const crashed =
    pageErrors.some((e) => CRASH.test(e)) ||
    consoleErrors.some((e) => CRASH.test(e)) ||
    rootChildren === 0;

  console.log("== SpaceRPG defect repro ==");
  console.log("injection_mode:", fixture.injection_mode, "operator:", fixture.operator_id, "defect_type:", fixture.defect_type);
  console.log("#root childElementCount:", rootChildren);
  console.log("gracefully recovered by an error boundary:", recovered);
  console.log("pageErrors:", JSON.stringify(pageErrors.slice(0, 3)));
  console.log("consoleErrors:", JSON.stringify(consoleErrors.slice(0, 5)));

  expect(crashed, "app crashed/blanked on the injected deep-state (see logs above for the captured errors)").toBe(false);
});
'''


def _spec_ts() -> str:
    return SPEC_TEMPLATE


def _reproduce_sh(rel_game: str, dest_sub: str) -> str:
    return f'''#!/usr/bin/env bash
# One-click reproduction for this single SpaceRPG defect detection.
# Boots the game's own dev server (Playwright webServer) and replays the captured
# deep-state. The regression test FAILS on the current build == bug reproduced.
set -euo pipefail
HERE="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
GAME="$(cd "$HERE/{rel_game}" && pwd)"
DEST="$GAME/tests/e2e/_spacerpg_defect_repro/{dest_sub}"
mkdir -p "$DEST"
cp "$HERE/repro.spec.ts" "$DEST/repro.spec.ts"
cp "$HERE/state.json" "$DEST/state.json"
cd "$GAME"
[ -d node_modules ] || npm ci
npx playwright install chromium
npx playwright test "tests/e2e/_spacerpg_defect_repro/{dest_sub}/repro.spec.ts"
'''


def _reproduce_all_sh(rel_game_from_root: str, detections: list[dict]) -> str:
    lines = []
    for d in detections:
        lines.append(f'mkdir -p "$DEST/{d["dir"]}"')
        lines.append(f'cp "$HERE/detections/{d["dir"]}/repro.spec.ts" "$DEST/{d["dir"]}/repro.spec.ts"')
        lines.append(f'cp "$HERE/detections/{d["dir"]}/state.json" "$DEST/{d["dir"]}/state.json"')
    body = "\n".join(lines)
    return f'''#!/usr/bin/env bash
# One-click reproduction for ALL captured SpaceRPG defect detections.
set -euo pipefail
HERE="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
GAME="$(cd "$HERE/{rel_game_from_root}" && pwd)"
DEST="$GAME/tests/e2e/_spacerpg_defect_repro"
mkdir -p "$DEST"
{body}
cd "$GAME"
[ -d node_modules ] || npm ci
npx playwright install chromium
npx playwright test "tests/e2e/_spacerpg_defect_repro"
'''


def _fingerprint_by_screenshot(report: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for result in report.get("results", []):
        for mut in result.get("mutations", []) or []:
            shot = mut.get("screenshot")
            if shot:
                out[os.path.basename(shot)] = mut
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, help="Path to a fuzz.json report.")
    parser.add_argument("--out", default=None, help="Output dir (default: <report_dir>/spacerpg-defect-artifacts).")
    args = parser.parse_args()

    report_path = Path(args.report).expanduser().resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_dir = report_path.parent
    out_dir = Path(args.out).expanduser().resolve() if args.out else (report_dir / "spacerpg-defect-artifacts")
    det_root = out_dir / "detections"
    det_root.mkdir(parents=True, exist_ok=True)

    rec_by_shot = _fingerprint_by_screenshot(report)

    # Collect every persisted defect dump across all tasks in the report.
    # Scan tasks/*/defects directly (not only tasks listed in fuzz.json) so
    # wall-clock-capped runs still get repros for in-flight task artifacts.
    dumps: list[dict] = []
    tasks_root = report_dir / "tasks"
    defect_dirs: list[Path] = []
    if tasks_root.is_dir():
        for task_dir in sorted(tasks_root.iterdir()):
            ddir = task_dir / "defects"
            if ddir.is_dir():
                defect_dirs.extend(sorted(ddir.iterdir()))
    for sub in defect_dirs:
        state_file = sub / "state.json"
        if not state_file.exists():
            continue
        state = json.loads(state_file.read_text(encoding="utf-8"))
        shot_name = state.get("screenshot_name", "")
        rec = rec_by_shot.get(shot_name, {})
        state["fingerprint"] = rec.get("fingerprint")
        state["repro"] = _repro_recipe(state.get("target", {}))
        dumps.append({"state": state, "src_dir": sub})

    if not dumps:
        print(f"No defect dumps found under {report_dir}/tasks/*/defects. Re-run fuzz-bench with the persistence build.")
        return 1

    # Stable order: by root cause (A,B,C), then injection_mode, then screenshot name.
    def sort_key(d):
        s = d["state"]
        fam = FAMILY.get(s.get("injection_mode"), ("z", "Z", ""))
        return (fam[1], s.get("injection_mode", ""), s.get("screenshot_name", ""))

    dumps.sort(key=sort_key)

    detections: list[dict] = []
    for i, d in enumerate(dumps, start=1):
        s = d["state"]
        mode = s.get("injection_mode", "unknown")
        fp = (s.get("fingerprint") or "nofp")[:12]
        nn = f"{i:02d}"
        dname = f"{nn}-{mode}-{fp}"
        det_dir = det_root / dname
        det_dir.mkdir(parents=True, exist_ok=True)

        (det_dir / "state.json").write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        console = s.get("console_errors") or []
        (det_dir / "console.log").write_text("\n\n".join(str(c) for c in console) + "\n", encoding="utf-8")
        src_shot = d["src_dir"] / "screenshot.png"
        if src_shot.exists():
            shutil.copyfile(src_shot, det_dir / "screenshot.png")
        (det_dir / "repro.spec.ts").write_text(_spec_ts(), encoding="utf-8")
        rel_game = os.path.relpath(GAME_DIR, det_dir)
        sh = _reproduce_sh(rel_game, dname)
        sh_path = det_dir / "reproduce.sh"
        sh_path.write_text(sh, encoding="utf-8")
        os.chmod(sh_path, 0o755)

        fam = FAMILY.get(mode, ("?", "?", "unclassified"))
        detections.append({
            "n": nn, "dir": dname, "mode": mode, "operator": s.get("operator_id"),
            "defect_type": s.get("defect_type"), "fingerprint": s.get("fingerprint"),
            "root_cause": fam[1], "family": fam[0],
            "console_head": (console[0] if console else (s.get("detail") or "")).splitlines()[0][:140] if (console or s.get("detail")) else "",
        })

    # reproduce-all.sh
    rel_game_root = os.path.relpath(GAME_DIR, out_dir)
    all_sh = out_dir / "reproduce-all.sh"
    all_sh.write_text(_reproduce_all_sh(rel_game_root, detections), encoding="utf-8")
    os.chmod(all_sh, 0o755)

    commit = next((d["state"].get("git_commit") for d in dumps if d["state"].get("git_commit")), "see state.json")
    _write_readme(out_dir, report, detections, commit)
    _write_issue(out_dir, report, detections, commit)

    print(f"Wrote {len(detections)} detection artifacts to {out_dir}")
    by_rc: dict[str, int] = {}
    for d in detections:
        by_rc[d["root_cause"]] = by_rc.get(d["root_cause"], 0) + 1
    print("by root cause:", by_rc)
    print("detections:", ", ".join(d["dir"] for d in detections))
    return 0


def _env_block(report: dict, commit: str) -> str:
    llm = report.get("llm_config", {})
    return (
        f"- Game: `games/SpaceRPG` (commit `{commit}`)\n"
        f"- Runtime: Chromium via Playwright, `npm run dev` (Vite DEV, port 4173)\n"
        f"- Found by: WarpTest C3 state-fuzzing (`fuzz-bench`), LLM generation engine `{llm.get('model')}`\n"
        f"- Report: `{report.get('generated_at_utc')}`\n"
    )


def _write_readme(out_dir: Path, report: dict, detections: list[dict], commit: str) -> None:
    n = len(detections)
    uniq = len({d["fingerprint"] for d in detections if d["fingerprint"]})
    rows = "\n".join(
        f"| {d['n']} | RC-{d['root_cause']} | `{d['mode']}` | `{d['operator']}` | `{d['defect_type']}` | `{d['fingerprint']}` | "
        f"[{d['dir']}](detections/{d['dir']}/) | {d['console_head'].replace('|', '/')} |"
        for d in detections
    )
    text = f"""# SpaceRPG fuzz defect artifacts

Generated from `{report.get('generated_at_utc')}` fuzz report.

**{n} detections** ({uniq} unique fingerprints) across the C3 injection kinds, mapping to
**two root causes**:

- **RC-A** - {ROOT_CAUSES['A']['title']} (reachable via the game's real save-load path).
- **RC-B** - {ROOT_CAUSES['B']['title']}.

See [ISSUE.md](ISSUE.md) for the consolidated bug report.

## Environment

{_env_block(report, commit)}

## Detections

| # | Root cause | injection_mode | operator | defect_type | fingerprint | artifacts | console (head) |
|---|---|---|---|---|---|---|---|
{rows}

## Reproduce

Each detection is self-contained and LLM-independent (it replays the exact captured
deep-state, not the fuzzer):

```bash
# a single detection
bash detections/{detections[0]['dir']}/reproduce.sh

# all detections at once
bash reproduce-all.sh
```

Each `repro.spec.ts` is a game-native Playwright regression test: it boots the game's
own dev server, replays the captured state through the game's real paths (`loadGame()`
for save/legacy/index kinds; live `__SPACERPG_E2E__.setState` for overlay/replace), and
asserts the app must NOT crash/blank. It therefore **FAILS on the current build (bug
reproduced)** and will pass once the fixes in ISSUE.md land.
"""
    (out_dir / "README.md").write_text(text, encoding="utf-8")


def _write_issue(out_dir: Path, report: dict, detections: list[dict], commit: str) -> None:
    present_rcs = []
    for rc in ("A", "B", "C"):
        members = [d for d in detections if d["root_cause"] == rc]
        if members:
            present_rcs.append((rc, members))

    def rc_section(rc: str, members: list[dict]) -> str:
        info = ROOT_CAUSES[rc]
        modes = sorted({m["mode"] for m in members})
        example = members[0]
        return f"""### Root cause {rc}: {info['title']}

Observed via injection kinds: {', '.join(f'`{m}`' for m in modes)} ({len(members)} detection(s)).

{info['detail']}

**Representative evidence** (`detections/{example['dir']}/console.log`):

```
{example['console_head']}
```

**Suggested fix.** {info['fix']}
"""

    # Prefer a production-reachable RC-A (real save-load path) detection as the minimal
    # starting point; fall back to the unmutated seed, then anything.
    minimal = next(
        (d for d in detections if d["mode"] == "save_restore"),
        next((d for d in detections if d["operator"] == "oracle_seed"), detections[0]),
    )
    n = len(detections)
    uniq = len({d["fingerprint"] for d in detections if d["fingerprint"]})
    rc_titles = "; ".join(f"RC-{rc}: {ROOT_CAUSES[rc]['title']}" for rc, _ in present_rcs)
    sections = "\n".join(rc_section(rc, m) for rc, m in present_rcs)

    text = f"""# [SpaceRPG] Deep-state robustness defects: {rc_titles}

## Summary

WarpTest C3 state-fuzzing of SpaceRPG surfaced **{n} defect detections** ({uniq} unique
fingerprints) that fall into distinct root causes. The most impactful is a
**production-reachable crash**: a crafted/corrupted save survives `normalizeSave()` and
breaks the three.js flight renderer. A second systemic gap is the **absence of a top-level
React ErrorBoundary**, so any render error white-screens the whole app with no recovery.

## Environment

{_env_block(report, commit)}

## Root causes

{sections}

## How to reproduce

Reproductions are self-contained and do NOT require WarpTest or any LLM - each replays the
exact captured deep-state through the game's own code paths, as a Playwright regression test
under this repo. Minimal starting point: `detections/{minimal['dir']}/`.

```bash
# single detection (boots `npm run dev`, replays the state, asserts no crash -> FAILS today)
bash detections/{minimal['dir']}/reproduce.sh
# or every detection
bash reproduce-all.sh
```

See [README.md](README.md) for the full detection table and per-detection `state.json`
fixtures (the exact injected save/actions), `console.log` (full captured errors), and
`screenshot.png` (the blank/broken frame).

## Severity notes (honest scope)

- RC-A is reachable in production: the save lives in `localStorage` (`spacerpg-save-slot:*`,
  the legacy `spacerpg-save`, and `spacerpg-save-index`) and is parsed by the game's own
  loader; a corrupted/hand-edited save crashes the flight renderer.
- The `replace_state`/`state_overlay` triggers use the `__SPACERPG_E2E__` dev hook, which is gated
  by `import.meta.env.DEV` and is not exposed in production builds. However, the underlying gap
  they reveal - **no top-level ErrorBoundary** - still affects production resilience: any
  unexpected render error (including RC-A) blanks the entire app instead of degrading gracefully.

## Suggested fixes

1. Add a top-level React ErrorBoundary in `App.tsx` wrapping the component tree, with a
   fallback UI and a reset/reload affordance.
2. Harden `normalizeSave()` (`src/systems/save.ts`) to reject/clamp non-finite or wrong-typed
   numeric fields before hydration (and/or sanitize vectors at the three.js boundary).
"""
    (out_dir / "ISSUE.md").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
