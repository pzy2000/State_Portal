#!/usr/bin/env python3
"""Consolidate a multi-iteration guided-fuzz run into deduped, classified issue drafts.

Follows the github-issuer skill's ENGINE-PATH conventions (luanti has no per-defect
injection_mode; it groups by a normalized ``detail`` signature / root cause):

  * Merge every iteration's unique defects (dedup by defect fingerprint).
  * Classify each into a root-cause bucket (reusing build_luanti_repro._classify),
    dropping pure validation mismatches as non-crash noise.
  * Emit one issue per root-cause bucket, using the SMALLEST target.json in the
    bucket as the minimal representative and listing the rest as an evidence table.
  * Write ``manifest.json`` (+ per-issue ``NN-<category>-<slug>.md``) that
    ``push_issues.py`` consumes unchanged. Each body carries a hidden fingerprint
    marker for idempotent dedup on (re-)push.

It does NOT upload anything -- pushing is a separate, user-confirmed step
(``push_issues.py`` / ``publish_issues.py``).

Usage:
    python scripts/consolidate_guided_issues.py \
        --run-dir reports/warptest/fuzz-luanti-guided-10h-<stamp>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import build_luanti_repro as blr  # noqa: E402

MARKER_TAG = "issue-fingerprint"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Root-cause buckets for the engine path. Validation mismatch is excluded as noise.
_BUCKET_LABELS = {
    "engine/restore: references an unregistered node name": ("crash", "unknown-node"),
    "un-loadable mutated state (engine timed out; malformed/oversized JSON or runaway synthesis)": ("performance", "unloadable-or-hang"),
    "Lua error inside synthesize/validate (caught by pcall)": ("crash", "lua-error"),
    "synthesize returned failure": ("crash", "synthesis-failure"),
    "Checkpoint processing crashed: ": ("crash", "checkpoint-crash"),
    "harness-mod bug (malformed item table -> Lua concat error in the synthesize path)": ("crash", "item-concat"),
    "other engine restore failure": ("crash", "other"),
}
_NOISE = "validation mismatch: state restored but declared validations do not hold"


def _slug(text: str) -> str:
    return _SAFE.sub("-", text).strip("-").lower()[:48] or "defect"


def _collect(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Merge unique defects (by fingerprint) across all iteration fuzz.json's."""

    game_dir = run_dir / "luanti"
    unique: dict[str, dict[str, Any]] = {}
    iters = sorted(p for p in game_dir.iterdir() if p.is_dir() and p.name.startswith("iter")) if game_dir.is_dir() else []
    for iter_dir in iters:
        if not (iter_dir / "fuzz.json").exists():
            continue
        try:
            defects = blr._load_defects(iter_dir)
        except Exception as err:
            print(f"  WARN {iter_dir.name}: {err}")
            continue
        for d in defects:
            fp = d.get("fingerprint")
            if not fp or fp in unique:
                continue
            d["iter"] = iter_dir.name
            d["root_cause"] = blr._classify(d.get("detail", ""))
            d["defect_id"] = blr._defect_id(d["task_id"], fp)
            d["target_size"] = len(json.dumps(d.get("target") or {}))
            unique[fp] = d
    return unique


def _bucket_of(root_cause: str) -> tuple[str, str] | None:
    if root_cause == _NOISE:
        return None
    return _BUCKET_LABELS.get(root_cause, ("crash", "other"))


def _render_body(root_cause: str, members: list[dict[str, Any]], run_dir: Path) -> str:
    rep = min(members, key=lambda m: m["target_size"])
    fp = rep["fingerprint"]
    target_json = json.dumps(rep.get("target") or {}, indent=2, ensure_ascii=False)
    others = [m for m in members if m["fingerprint"] != fp]
    table = ""
    if others:
        rows = "\n".join(
            f"| `{m['defect_id']}` | `{m.get('defect_type','')}` | {(m.get('detail') or '').strip()[:80]} | `{m['iter']}` |"
            for m in others[:50]
        )
        table = (
            "\n### Other detections (same root cause)\n\n"
            "| defect_id | type | detail | iteration |\n|---|---|---|---|\n" + rows + "\n"
        )
    return f"""## Summary

Found by WarpTest C3 **issue-guided** state fuzzing of the Luanti engine path.
Root cause (heuristic): **{root_cause}**. {len(members)} unique defect fingerprint(s)
in this bucket; the smallest triggering state is shown below.

## Steps to reproduce

1. Load a world/server state equivalent to the `target.json` below on a dedicated
   `luantiserver` (devtest game), as the WarpTest checkpoint-restore path does.
2. Observe the failure described in the root cause.

## Minimal triggering state

<details><summary><code>target.json</code> (representative, fingerprint <code>{fp}</code>)</summary>

```json
{target_json}
```

</details>

## What actually happens

```
{(rep.get('detail') or '').strip() or '(engine hard-crash / hang; see the standalone repro run.sh output)'}
```
{table}
## Provenance

WarpTest C3 issue-guided state fuzz. Representative defect: `{rep['defect_id']}`
(task `{rep['task_id']}`, iteration `{rep['iter']}`). A standalone, dependency-free
reproduction (`target.json` + `run.sh`, only needs `luantiserver` + `devtest`) is
generated under the run's `BUGS/luanti/.../repro/{rep['defect_id']}/`.

<!-- {MARKER_TAG}: {fp} -->
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, help="reports/warptest/fuzz-luanti-guided-10h-<stamp>")
    ap.add_argument("--out", default=None, help="Output dir (default: <run-dir>/github-issues).")
    args = ap.parse_args()

    run_dir = (ROOT / args.run_dir).resolve() if not Path(args.run_dir).is_absolute() else Path(args.run_dir)
    out_dir = Path(args.out).resolve() if args.out else run_dir / "github-issues"
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in [*out_dir.glob("*.md"), out_dir / "manifest.json"]:
        stale.unlink(missing_ok=True)

    unique = _collect(run_dir)
    # Group unique defects into root-cause buckets (excluding validation-mismatch noise).
    buckets: dict[str, list[dict[str, Any]]] = {}
    noise = 0
    for d in unique.values():
        bucket = _bucket_of(d["root_cause"])
        if bucket is None:
            noise += 1
            continue
        buckets.setdefault(d["root_cause"], []).append(d)

    issues: list[dict[str, Any]] = []
    for n, (root_cause, members) in enumerate(sorted(buckets.items(), key=lambda kv: -len(kv[1])), 1):
        category, slug = _bucket_of(root_cause)
        rep = min(members, key=lambda m: m["target_size"])
        title = f"[luanti] {root_cause[:80]}"
        body = _render_body(root_cause, members, run_dir)
        fname = f"{n:02d}-{category}-{slug}.md"
        (out_dir / fname).write_text(body, encoding="utf-8")
        issues.append({
            "n": n,
            "title": title,
            "labels": [category, "luanti"],
            "body_file": fname,
            "fingerprint": rep["fingerprint"],
            "category": category,
            "needs_review": False,
            "detections": len(members),
            "representative": rep["defect_id"],
        })

    src = None
    try:
        from warptest.upstream_sources import get_source
        src = get_source("luanti", ROOT)
    except Exception:
        pass
    manifest = {
        "run_dir": str(run_dir.relative_to(ROOT)) if str(run_dir).startswith(str(ROOT)) else str(run_dir),
        "marker_tag": MARKER_TAG,
        "source_repo": (src or {}).get("source_repo"),
        "default_publish_repo": (src or {}).get("default_publish_repo"),
        "unique_defects": len(unique),
        "validation_mismatch_excluded": noise,
        "issues": issues,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Consolidated {len(unique)} unique defect(s) into {len(issues)} issue draft(s) -> {out_dir}")
    print(f"  validation-mismatch excluded as noise: {noise}")
    for it in issues:
        print(f"  {it['n']:02d} [{it['category']}] {it['title']}  (detections={it['detections']}, rep={it['representative']})")
    if not issues:
        print("  (no crash/hang defects found; nothing to file)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
