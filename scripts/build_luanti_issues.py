#!/usr/bin/env python3
"""Classify WarpTest luanti (engine-path) fuzz defects and build a GitHub issue draft.

Unlike the SpaceRPG browser path (which persists per-defect ``state.json`` dumps and
classifies by ``injection_mode``), the luanti engine path only records, per defect
mutation in ``fuzz.json``: ``defect_type=engine_synthesis_error``, a ``detail``
string, and a truncated ``mutated_state_signature``. This script therefore reads
defects straight from ``fuzz.json`` and groups them by a normalized ``detail``
signature.

Triage (validated against the actual crash traceback):
  * "Unknown node: <name>" / builtin ``item_s.lua`` __index / warptest ``init.lua``
    concatenate  -> ONE root cause: the WarpTest luanti checkpoint-restore path
    (mods/warptest/init.lua ``synthesize`` -> ``set_node`` / ``ItemStack``) feeds
    UNVALIDATED node names / item entries straight into engine calls, so a single
    unregistered node or malformed item in restored world data raises a Lua error
    inside an async callback and takes the whole server down (ServerError AsyncErr).
    This is a harness robustness gap (NOT a luanti-upstream bug: ``core.set_node``
    erroring on an unknown node is documented behaviour, and luanti tolerates
    unknown nodes when LOADING map data via CONTENT_UNKNOWN). It belongs on the
    StatePortal repo that owns the ``warptest`` mod.
  * "timed out waiting for report" -> cascade after a prior candidate crashed the
    server; a symptom of the same gap, folded into the same issue as evidence.
  * "One or more checks failed." -> assertion mismatch, not a crash; excluded.

Emits ``<report_dir>/github-issues/{manifest.json, 01-*.md}`` that ``push_issues.py``
consumes unchanged.

Usage:
  python scripts/build_luanti_issues.py --report reports/warptest/<ts>/luanti/fuzz.json \
    --repro specs/luanti/repro/unknown-node.yaml
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BASE_LABELS = ["warptest", "fuzz", "robustness", "luanti"]

CLASSIFICATION_TEXT = (
    "Player-reachable: the state can be reached by a player through normal play over some period of time, "
    "or from a specific save state.\n\n"
    "Buggy-not-reachable but worth fixing: the triggering state itself is not player-reachable, but the defect "
    "exposes a cross-cutting resilience gap that is independent of how it is triggered (a missing fault-tolerance "
    "mechanism that spans the whole application rather than any specific bug site), and that gap is also triggered "
    "by some defect already classified as player-reachable."
)
QUOTED_STANDARD = "\n".join(("> " + ln) if ln.strip() else ">" for ln in CLASSIFICATION_TEXT.splitlines())

# The engine restore defect fingerprint: one cross-cutting resilience gap, so it
# collapses to a single stable marker (idempotent re-runs never duplicate).
GAP_MARKER = "luanti-restore-unvalidated-node-item-input"


def normalize_detail(detail: str) -> str:
    s = re.sub(r"0x[0-9a-fA-F]+|\d+", "<n>", str(detail or ""))
    s = re.sub(r"/[^\s:;,'\"]+", "<path>", s)
    return re.sub(r"\s+", " ", s).strip().lower()[:90]


def bucket(detail: str) -> str:
    d = str(detail or "")
    if "item_s.lua" in d or "Unknown node" in d or "init.lua" in d or "Checkpoint processing crashed" in d:
        return "engine_crash_unvalidated_input"
    if "timed out" in d:
        return "cascade_timeout"
    if "checks failed" in d:
        return "assertion_mismatch"
    return "other"


def collect(report: dict) -> tuple[Counter, dict[str, str], int]:
    sig_counts: Counter = Counter()
    sig_example: dict[str, str] = {}
    total_defects = 0
    for r in report.get("results", []):
        for m in r.get("mutations", []) or []:
            if m.get("verdict") != "defect":
                continue
            total_defects += 1
            key = (bucket(m.get("detail")), normalize_detail(m.get("detail")))
            sig_counts[key] += 1
            sig_example.setdefault(key, str(m.get("detail") or "")[:160])
    return sig_counts, sig_example, total_defects


TRACEBACK = (
    'ServerError: AsyncErr: Lua: Runtime error from mod \'warptest\' in callback\n'
    'environment_Step(): "Unknown node: warptest:__no_such_node__"\n'
    "  [C]: in function '__index'\n"
    "  .../builtin/common/item_s.lua:229\n"
    "  [C]: in function 'set_node'\n"
    "  .../mods/warptest/init.lua:149: in function 'set_node'\n"
    "  .../mods/warptest/init.lua:261: in function 'synthesize'\n"
    "  .../mods/warptest/init.lua:586: in function 'process_request'\n"
    "  .../mods/warptest/init.lua:627: in function 'run_checkpoint'"
)


def render_issue(report: dict, sig_counts: Counter, sig_example: dict, total: int, repro_path: Path, commit: str) -> tuple[str, str]:
    llm = report.get("llm_config", {})
    engine_n = sum(c for (b, _), c in sig_counts.items() if b == "engine_crash_unvalidated_input")
    timeout_n = sum(c for (b, _), c in sig_counts.items() if b == "cascade_timeout")
    assert_n = sum(c for (b, _), c in sig_counts.items() if b == "assertion_mismatch")

    rows = "\n".join(
        f"| `{b}` | {c} | {ex.replace('|', '/')[:90]} |"
        for (b, _), c in sig_counts.most_common()
        for ex in [sig_example[(b, _)]]
    )
    repro_rel = repro_path.relative_to(ROOT) if repro_path.is_absolute() else repro_path
    repro_text = repro_path.read_text(encoding="utf-8") if repro_path.exists() else "(repro spec missing)"

    title = "[luanti][warptest] Checkpoint restore does not validate node/item inputs -> server-fatal crash"

    body = f"""## Problem statement

WarpTest C3 state-fuzzing of the luanti engine path found that the checkpoint
**restore/synthesize** path in the WarpTest probe mod feeds UNVALIDATED node names
and item entries straight into engine calls. A single unregistered node name (or a
malformed item) in the restored world data raises a Lua runtime error inside an
async server callback, which is not caught and takes the **entire server down**
(`ServerError: AsyncErr`) instead of being skipped/clamped or reported gracefully.

Representative crash (from the minimal repro below):

```
{TRACEBACK}
```

The crash flows through `mods/warptest/init.lua` `synthesize` (line 261) ->
`set_node` (line 149) -> `core.set_node`, and the engine's content-id lookup
(`builtin/common/item_s.lua:229`, `name2content[node.name]`) throws on the unknown
name. The same missing-guard shape also produces `init.lua:218` (`ItemStack(entry.item .. " " ..)`
"attempt to concatenate field 'item' (a table value)") for malformed inventory items.

## Reproduction (exact, self-contained)

A minimal validated WarpTest spec (`{repro_rel}`) that places ONE unregistered node
and crashes the restore:

```bash
python3 -m warptest run {repro_rel} --repo games/luanti
# -> status: failure; failure_phases.checkpoint_restoration set;
#    luanti stderr: ServerError: AsyncErr ... "Unknown node: warptest:__no_such_node__"
```

<details><summary>{repro_rel} (the exact repro spec)</summary>

```yaml
{repro_text}
```

</details>

## Expected vs actual

- **Expected:** an unregistered node name / malformed item in restored data is
  skipped or clamped (e.g. mapped to `CONTENT_UNKNOWN`, matching how luanti tolerates
  unknown nodes when loading map data), or at worst raises a *contained* error that is
  reported as a failed checkpoint — the server stays up.
- **Actual:** the un-guarded `core.set_node` / `ItemStack` call raises inside an async
  callback and the whole `luantiserver` shuts down (`ServerError: AsyncErr`).

## Signature inventory (this fuzz run)

{total} defect detections over the engine path, grouped by signature:

| bucket | count | example detail |
|---|---|---|
{rows}

- `engine_crash_unvalidated_input` ({engine_n}): the root cause above.
- `cascade_timeout` ({timeout_n}): `timed out waiting for report` — subsequent candidates time out after a prior candidate crashed the server; a symptom of the same gap.
- `assertion_mismatch` ({assert_n}): `One or more checks failed` — not a crash; excluded from the fix scope.

## Classification & rationale

Per the standard:

{QUOTED_STANDARD}

This is a **cross-cutting resilience gap** in the WarpTest luanti checkpoint-restore
path (not a luanti-upstream bug: `core.set_node` erroring on an unknown node is
documented behaviour). Restored/hand-edited world data referencing a node from an
uninstalled mod is player-reachable in spirit, and it should degrade gracefully
rather than crash the server.

## FAIL_TO_PASS regression test

`{repro_rel}` is the regression artifact: `python3 -m warptest run {repro_rel} --repo games/luanti`
**fails on the current build** (checkpoint restore crashes) and will pass once the
restore path validates inputs.

## Suggested fix

In `games/luanti/mods/warptest/init.lua`, harden the `synthesize`/`set_node`/
`set_meta_inventory` path:
1. Before `core.set_node`, check `core.registered_nodes[name]` (and that `name` is a
   string); skip/clamp unknown nodes to `CONTENT_UNKNOWN` (or record a per-node
   restore warning) instead of calling `set_node` blindly.
2. Validate item entries before `ItemStack(entry.item .. " " .. count)` (require
   `type(entry.item) == "string"`); reject/skip malformed items.
3. Wrap the per-node/per-item work in `pcall` so a single bad entry cannot escalate to
   a server-fatal `AsyncErr`, and surface it as a contained checkpoint failure.

- Found by: WarpTest C3 state-fuzzing (`fuzz-bench`), LLM engine `{llm.get('model')}`
- Report: `{report.get('generated_at_utc')}` (StatePortal commit `{commit}`)

<!-- warptest-fingerprint: {GAP_MARKER} -->
"""
    return title, body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", required=True, help="Path to a luanti fuzz.json report.")
    ap.add_argument("--repro", default="specs/luanti/repro/unknown-node.yaml", help="Validated minimal repro spec.")
    ap.add_argument("--out", default=None, help="Output dir (default: <report_dir>/github-issues).")
    ap.add_argument("--commit", default=None, help="StatePortal commit for provenance.")
    args = ap.parse_args()

    report_path = Path(args.report).expanduser().resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_dir = report_path.parent
    out_dir = Path(args.out).expanduser().resolve() if args.out else (report_dir / "github-issues")
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in [*out_dir.glob("*.md"), out_dir / "manifest.json"]:
        if stale.exists():
            stale.unlink()

    repro_path = Path(args.repro)
    if not repro_path.is_absolute():
        repro_path = (ROOT / repro_path).resolve()

    sig_counts, sig_example, total = collect(report)
    if total == 0:
        print("No defects in report; nothing to file.")
        return 1

    title, body = render_issue(report, sig_counts, sig_example, total, repro_path, args.commit or "see report")
    fname = "01-luanti-restore-unvalidated-input.md"
    (out_dir / fname).write_text(body, encoding="utf-8")

    manifest = {
        "report": str(report_path),
        "generated_at_utc": report.get("generated_at_utc"),
        "commit": args.commit or "see report",
        "issues": [{
            "n": 1, "title": title,
            "labels": BASE_LABELS + ["harness"],
            "body_file": fname, "fingerprint": GAP_MARKER,
            "category": "buggy-not-reachable", "needs_review": False,
            "detections": total,
        }],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote 1 luanti issue draft to {out_dir}")
    print(f"  01 {title}")
    print(f"  buckets: {dict(Counter({b: c for (b, _), c in sig_counts.items()}))}")
    print(f"Manifest: {out_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
