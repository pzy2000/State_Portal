#!/usr/bin/env python3
"""Run one registered Luanti Phase-0 task's two cells as a paid runtime smoke.

This is not the authorized eight-cell pilot: it dispatches exactly the buggy and
fixed cells of one arm so a newly registered task can be validated first.  The
receipt records that it is a smoke run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_phase0_smoke import run_lazarus_phase0_smoke  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", default="lz-luanti-14757-math-round")
    parser.add_argument("--arm", default="lazarus_state")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--report", type=Path, default=ROOT / "reports/lazarus/phase0-runtime-smoke.json"
    )
    parser.add_argument("--credentials", type=Path, default=ROOT / "test_model_batch.py")
    parser.add_argument(
        "--confirm",
        required=True,
        help="must be RUN_PAID_LAZARUS_SMOKE to dispatch provider calls",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.confirm != "RUN_PAID_LAZARUS_SMOKE":
        raise SystemExit("explicit confirmation must be RUN_PAID_LAZARUS_SMOKE")
    smoke = run_lazarus_phase0_smoke(
        ROOT,
        task_id=args.task_id,
        arm=args.arm,
        seed=args.seed,
        report_path=args.report,
        credential_source=args.credentials,
    )
    print(
        json.dumps(
            {
                "task_id": smoke.task_id,
                "arm": smoke.arm,
                "complete": smoke.complete,
                "separates_goal_from_defect": smoke.separates_goal_from_defect,
                "report": str(smoke.report_path.relative_to(ROOT)),
                "cells": [
                    {
                        "version": cell["version"],
                        "goal_checks_ok": cell["goal_checks_ok"],
                        "defect_signature_ok": cell["defect_signature_ok"],
                        "invariants_ok": cell["invariants_ok"],
                        "actual_synthesis_calls": cell["actual_synthesis_calls"],
                        "treatment_tokens": cell["treatment_tokens"],
                    }
                    for cell in smoke.cells
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if smoke.complete and smoke.separates_goal_from_defect else 1


if __name__ == "__main__":
    raise SystemExit(main())
