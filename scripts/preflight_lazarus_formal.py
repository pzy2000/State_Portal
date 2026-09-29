#!/usr/bin/env python3
"""Produce the durable pre-dispatch record for the formal C1-Lazarus matrix.

This verifies each row's construction gate, goldens and upstream provenance,
materializes both version trees, prepares the sanitized agent-visible sources,
checks calibration, APFS/submodule state and the run's own disk budget, and
measures the per-trial P2P P95.  It creates no model request.

Every missing input is reported in one pass, so an incomplete population yields
a complete gap list rather than the first failure encountered.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_formal import (  # noqa: E402
    DEFAULT_FORMAL_OUTER_WORKERS,
    preflight_lazarus_formal,
    write_lazarus_formal_preflight,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports/lazarus/formal-preflight.json",
    )
    parser.add_argument(
        "--outer-workers",
        type=int,
        default=DEFAULT_FORMAL_OUTER_WORKERS,
        help="peak concurrent groups, which sets the disk budget",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    preflight = preflight_lazarus_formal(
        workspace, outer_workers=args.outer_workers
    )
    destination = write_lazarus_formal_preflight(preflight, args.output)
    print(
        json.dumps(
            {
                "artifact": str(destination),
                "complete": preflight.complete,
                "expected_cells": preflight.plan.trial_count,
                "p2p_p95_s": preflight.p2p_p95_s,
                "required_free_bytes": (
                    preflight.capacity.required_free_bytes
                    if preflight.capacity
                    else None
                ),
                "failure_count": len(preflight.failures),
                "failures": list(preflight.failures),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if preflight.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
