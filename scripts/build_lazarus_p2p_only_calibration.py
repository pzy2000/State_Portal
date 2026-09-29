#!/usr/bin/env python3
"""Bind P2P-only runtime canaries and routes to the current frozen harness."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.c1_experiment import relay_environment  # noqa: E402
from warptest.lazarus_calibration import (  # noqa: E402
    lazarus_harness_hash,
    lazarus_provider_identity_sha256,
)
from warptest.lazarus_p2p_only_experiment import (  # noqa: E402
    CATALOG_PATH,
    LAZARUS_ARMS,
    EXPECTED_TASK_COUNT,
    START_PROTOCOL,
)
from warptest.lazarus_p2p_only_identity import scoped_identities  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kimi-capacity-history-backoff", action="store_true")
    parser.add_argument("--manifest", type=Path, default=ROOT / "benchmarks/p5/lazarus_p2p_only_formal_seed0_manifest.yaml")
    parser.add_argument("--credentials", type=Path, default=ROOT / "test_model_batch.py")
    parser.add_argument(
        "--runtime-canary",
        type=Path,
        default=ROOT / "reports/lazarus-p2p-only/runtime-canary.json",
    )
    parser.add_argument(
        "--api-preflight",
        type=Path,
        default=ROOT / "reports/lazarus-p2p-only/api-preflight.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports/lazarus-p2p-only/calibration.json",
    )
    return parser.parse_args()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = _parse_args()
    from warptest.image_history_backoff import POLICY_ENV, REVISION
    os.environ.pop(POLICY_ENV, None)
    if args.kimi_capacity_history_backoff:
        os.environ[POLICY_ENV] = REVISION
    manifest_path = args.manifest.resolve()
    catalog_path = ROOT / CATALOG_PATH
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    from warptest.image_history_backoff import validate_manifest_policy
    validate_manifest_policy(manifest, requested=args.kimi_capacity_history_backoff)
    scoped = scoped_identities(ROOT, manifest)
    canary = json.loads(args.runtime_canary.read_text(encoding="utf-8"))
    api = json.loads(args.api_preflight.read_text(encoding="utf-8"))
    failures: list[str] = []
    if canary.get("complete") is not True:
        failures.append("runtime canary is incomplete")
    if api.get("complete") is not True:
        failures.append("API preflight is incomplete")
    rows = canary.get("tasks")
    if not isinstance(rows, list) or len(rows) != EXPECTED_TASK_COUNT:
        failures.append("runtime canary does not cover 11 tasks")
        rows = []
    bindings: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            failures.append("runtime canary contains an invalid task row")
            continue
        versions = row.get("versions")
        if not isinstance(versions, dict) or set(versions) != {"fixed", "buggy"}:
            failures.append(f"{row.get('task_id')}: runtime versions are incomplete")
            continue
        version_binding: dict[str, object] = {}
        for version in ("fixed", "buggy"):
            arms = versions[version]
            if not isinstance(arms, dict):
                failures.append(f"{row.get('task_id')}/{version}: arms are missing")
                continue
            if set(arms) != set(LAZARUS_ARMS) or any(
                not isinstance(arms.get(arm), dict) or arms[arm].get("complete") is not True
                for arm in LAZARUS_ARMS
            ):
                failures.append(f"{row.get('task_id')}/{version}: complete C/D/E evidence is required")
                continue
            version_binding[version] = {
                arm: {
                    "initial_sha256": arms[arm].get("initial_sha256"),
                    "before_sha256": arms[arm].get("before_sha256"),
                    "after_sha256": arms[arm].get("after_sha256"),
                    "start_constructed": arms[arm].get("start_constructed"),
                    "start_action_interface": (
                        arms[arm].get("start_action", {}).get("interface")
                        if isinstance(arms[arm], dict)
                        else None
                    ),
                    "same_live_lineage": arms[arm].get("same_live_lineage"),
                    "action_interface": (
                        arms[arm].get("action", {}).get("interface")
                        if isinstance(arms[arm], dict)
                        else None
                    ),
                }
                for arm in LAZARUS_ARMS
            }
        bindings.append(
            {
                "task_id": row.get("task_id"),
                "benchmark": row.get("benchmark"),
                "versions": version_binding,
            }
        )
    with relay_environment(args.credentials):
        provider_identity = lazarus_provider_identity_sha256(manifest)
    payload = {
        "schema": "c1-lazarus-p2p-only-calibration-v2",
        "schema_version": 2,
        "start_protocol": START_PROTOCOL,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        ),
        "complete": not failures,
        "failures": failures,
        "formal": False,
        "confirmatory": False,
        "expected_cells": manifest["expected_cells"],
        "models": list(manifest["models"]),
        "identity": {
            "identity_version": "scoped-v1",
            "schedule_sha256": scoped["schedule_sha256"],
            "catalog_sha256": _sha(catalog_path),
            "provider_identity_sha256": provider_identity,
            "provider_profile_sha256": scoped["provider_profile_sha256"],
            "runtime_sha256": scoped["runtime_sha256"],
            "grading_sha256": scoped["grading_sha256"],
            "orchestrator_sha256": scoped["orchestrator_sha256"],
            "treatment_runtime_sha256": scoped["treatment_runtime_sha256"],
            "materialization_sha256": scoped["materialization_sha256"],
            "runtime_canary_sha256": _sha(args.runtime_canary),
            "api_preflight_sha256": _sha(args.api_preflight),
            "start_protocol": START_PROTOCOL,
        },
        "presentation_identity": {},
        "legacy_diagnostic_harness_sha256": lazarus_harness_hash(ROOT),
        "runtime_bindings": bindings,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, args.output)
    print(
        json.dumps(
            {
                "artifact": str(args.output),
                "complete": payload["complete"],
                "bindings": len(bindings),
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if payload["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
