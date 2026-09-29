#!/usr/bin/env python3
"""Dispatch only the authorized, preflighted eight-cell C1-Lazarus pilot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_phase0 import launch_lazarus_phase0  # noqa: E402


_LOCKED_BOOTSTRAP_ITERATIONS = 10_000


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports/lazarus/phase0-run.json",
    )
    parser.add_argument(
        "--bootstrap-iterations",
        type=int,
        default=_LOCKED_BOOTSTRAP_ITERATIONS,
        help="Must remain the pre-registered value 10000.",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path("test_model_batch.py"),
        help="Literal Relay credential source, resolved under the workspace.",
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
    if args.bootstrap_iterations != _LOCKED_BOOTSTRAP_ITERATIONS:
        raise ValueError("Lazarus Phase 0 bootstrap_iterations must remain 10000")
    workspace = args.workspace.expanduser().resolve()
    output = _contained(workspace, args.output, "output")
    credentials = _contained(workspace, args.credentials, "credentials")
    launch = launch_lazarus_phase0(
        workspace,
        report_path=output,
        credential_source=credentials,
        bootstrap_iterations=_LOCKED_BOOTSTRAP_ITERATIONS,
    )
    print(
        json.dumps(
            {
                "artifact": str(launch.report_path.relative_to(workspace)),
                "complete": launch.run.complete,
                "stopped_early": launch.run.stopped_early,
                "stop_reason": launch.run.stop_reason,
                "expected_cells": launch.preflight.plan.trial_count,
                "observed_cells": len(launch.run.reports),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if launch.run.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
