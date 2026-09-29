#!/usr/bin/env python3
"""Generate readiness for the paper's frozen 11-task/264-cell P2P-only run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_p2p_only_experiment import (  # noqa: E402
    preflight_p2p_only,
    write_readiness,
)
from warptest.c1_experiment import relay_environment  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/p5/lazarus_p2p_only_formal_seed0_manifest.yaml"))
    parser.add_argument("--credentials", type=Path, default=ROOT / "test_model_batch.py")
    parser.add_argument("--kimi-capacity-history-backoff", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports/lazarus-p2p-only/readiness.json",
    )
    parser.add_argument(
        "--runtime-canary",
        type=Path,
        default=ROOT / "reports/lazarus-p2p-only/runtime-canary-v3.json",
    )
    parser.add_argument(
        "--api-preflight",
        type=Path,
        default=ROOT / "reports/lazarus-p2p-only/api-preflight.json",
    )
    parser.add_argument(
        "--calibration",
        type=Path,
        default=ROOT / "reports/lazarus-p2p-only/calibration.json",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    import os
    import yaml
    from warptest.image_history_backoff import POLICY_ENV, REVISION, validate_manifest_policy

    manifest_path = args.manifest if args.manifest.is_absolute() else args.workspace / args.manifest
    validate_manifest_policy(
        yaml.safe_load(manifest_path.read_text()), requested=args.kimi_capacity_history_backoff
    )
    os.environ.pop(POLICY_ENV, None)
    if args.kimi_capacity_history_backoff:
        os.environ[POLICY_ENV] = REVISION
    with relay_environment(args.credentials):
        readiness = preflight_p2p_only(
            args.workspace,
            manifest_path=args.manifest,
            runtime_canary_path=args.runtime_canary,
            api_preflight_path=args.api_preflight,
            calibration_path=args.calibration,
        )
    destination = write_readiness(readiness, args.output)
    print(
        json.dumps(
            {
                "artifact": str(destination),
                "complete": readiness.complete,
                "expected_cells": readiness.plan.trial_count,
                "expected_groups": len(readiness.plan.groups),
                "runtime_bindings": sum(
                    value.get("canary_passed") is True
                    for value in readiness.runtime_bindings.values()
                ),
                "failure_count": len(readiness.failures),
                "failures": list(readiness.failures),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if readiness.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
