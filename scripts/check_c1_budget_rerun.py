#!/usr/bin/env python3
"""Verify a post-unification C1 run against the uniform budget contract.

Usage:
    python3 scripts/check_c1_budget_rerun.py reports/warptest/<run-dir>

Checks every ``c1-trial-evidence/**/gui-agent.json`` under the given root for:
  * decision_budget == C1_STEP_BUDGET (100)
  * task_timeout_s == C1_TASK_BUDGET_MS / 1000 (2400)
  * budget_exhausted rate and which tasks still exhaust
  * wall-clock timeout count
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.c1_budgets import C1_STEP_BUDGET, C1_TASK_BUDGET_MS  # noqa: E402


def _load_rows(run_root: Path) -> list[dict]:
    rows: list[dict] = []
    for path in run_root.rglob("gui-agent.json"):
        if "c1-trial-evidence" not in path.parts:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for result in payload.get("results") or []:
            if isinstance(result, dict) and result.get("task_id"):
                rows.append(result)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path)
    args = parser.parse_args()
    run_root = args.run_root.expanduser().resolve()
    if not run_root.is_dir():
        print(f"not a directory: {run_root}", file=sys.stderr)
        return 2

    rows = _load_rows(run_root)
    if not rows:
        print(f"no c1-trial-evidence gui-agent.json under {run_root}", file=sys.stderr)
        return 2

    expected_timeout = C1_TASK_BUDGET_MS / 1000.0
    bad_budget = [
        r["task_id"]
        for r in rows
        if r.get("decision_budget") != C1_STEP_BUDGET
    ]
    bad_timeout = [
        r["task_id"]
        for r in rows
        if float(r.get("task_timeout_s") or 0) != expected_timeout
    ]
    exhausted = [r for r in rows if r.get("budget_exhausted") is True]
    timed_out = [
        r
        for r in rows
        if r.get("timed_out") is True
        and "wall-clock limit" in str(r.get("reason") or "")
    ]

    print(f"trials: {len(rows)}")
    print(f"decision_budget != {C1_STEP_BUDGET}: {len(bad_budget)}")
    if bad_budget:
        print(" ", Counter(bad_budget).most_common(10))
    print(f"task_timeout_s != {expected_timeout:g}: {len(bad_timeout)}")
    if bad_timeout:
        print(" ", Counter(bad_timeout).most_common(10))
    rate = 100.0 * len(exhausted) / len(rows)
    print(f"budget_exhausted: {len(exhausted)}/{len(rows)} ({rate:.1f}%)")
    if exhausted:
        print(" ", Counter(r["task_id"] for r in exhausted).most_common(10))
    print(f"wall-clock timeouts: {len(timed_out)}")
    if timed_out:
        print(" ", Counter(r["task_id"] for r in timed_out).most_common(10))

    ok = not bad_budget and not bad_timeout and rate < 1.0 and len(timed_out) <= 5
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
