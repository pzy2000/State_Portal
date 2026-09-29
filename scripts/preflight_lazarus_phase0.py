#!/usr/bin/env python3
"""Produce the non-paid, fail-closed pre-dispatch record for Lazarus Phase 0."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_phase0 import (  # noqa: E402
    preflight_lazarus_phase0,
    write_lazarus_phase0_preflight,
)
from warptest.c1_experiment import relay_environment  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports/lazarus/phase0-preflight.json",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path("test_model_batch.py"),
    )
    return parser.parse_args(argv)


def _contained(workspace: Path, value: Path, label: str) -> Path:
    candidate = value.expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    if workspace != path and workspace not in path.parents:
        raise ValueError(f"{label} escapes workspace")
    return path


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    output = _contained(workspace, args.output, "output")
    credentials = _contained(workspace, args.credentials, "credentials")
    with relay_environment(credentials):
        preflight = preflight_lazarus_phase0(workspace)
    destination = write_lazarus_phase0_preflight(preflight, output)
    print(
        json.dumps(
            {
                "artifact": str(destination.relative_to(workspace)),
                "complete": preflight.complete,
                "failures": list(preflight.failures),
                "expected_cells": preflight.plan.trial_count,
                "p2p_p95_s": preflight.p2p_p95_s,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if preflight.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
