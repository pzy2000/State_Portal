#!/usr/bin/env python3
"""Run and persist one formal Lazarus upstream F2P/P2P truth table."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_formal_gate import (  # noqa: E402
    run_formal_construction_test_matrix,
    write_formal_construction_test_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--timeout-s", type=float, default=600.0)
    args = parser.parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    task_id = str(args.task_id)
    evidence = workspace / "benchmarks/p5/lazarus/defects" / task_id
    prebuilt = workspace / ".warptest-cache/lazarus/prebuilt" / task_id
    report = run_formal_construction_test_matrix(
        task_id=task_id,
        evidence_root=evidence,
        fixed_root=prebuilt / "fixed",
        buggy_root=prebuilt / "buggy",
        sandbox_root=workspace / ".warptest-cache/lazarus/construction",
        harness_root=workspace,
        total_timeout_s=args.timeout_s,
    )
    output = evidence / "construction-tests.json"
    write_formal_construction_test_report(report, output)
    print(json.dumps({**report, "output": str(output)}, indent=2, sort_keys=True))
    return 0 if report["qualified"] is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
