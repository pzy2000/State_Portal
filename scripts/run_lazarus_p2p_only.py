#!/usr/bin/env python3
"""Launch the authorized frozen paper C1-Lazarus P2P-only run."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.c1_experiment import relay_environment  # noqa: E402
from warptest.lazarus_p2p_only_experiment import (  # noqa: E402
    build_paid_authorization,
    preflight_p2p_only,
    run_p2p_only_groups,
    write_readiness,
    write_run_index,
    write_run_report,
)
from warptest.lazarus_p2p_only_paid_runtime import (  # noqa: E402
    build_p2p_only_paid_runtime_runner,
)
from warptest.lazarus_p2p_only_executor import (  # noqa: E402
    build_lazarus_p2p_only_executor,
)
from warptest.model_request import model_request_profiles_environment  # noqa: E402



def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/p5/lazarus_p2p_only_formal_seed0_manifest.yaml"))
    parser.add_argument("--credentials", type=Path, default=ROOT / "test_model_batch.py")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--resume-index", type=Path)
    parser.add_argument("--quarantine", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--runtime-canary", type=Path, default=ROOT / "reports/lazarus-p2p-only/runtime-canary-v3.json")
    parser.add_argument("--api-preflight", type=Path, default=ROOT / "reports/lazarus-p2p-only/api-preflight.json")
    parser.add_argument("--calibration", type=Path, default=ROOT / "reports/lazarus-p2p-only/calibration.json")
    parser.add_argument("--outer-workers", type=int, default=None,
                        help="Must match the selected manifest; defaults to its registered lane count.")
    parser.add_argument("--kimi-capacity-history-backoff", action="store_true")
    return parser.parse_args()


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be a mapping: {path}")
    return value


def main() -> int:
    args = _parse_args()
    workspace = args.workspace.expanduser().resolve()
    run_root = args.run_root.expanduser().resolve()
    if workspace != run_root and workspace not in run_root.parents:
        raise ValueError("P2P-only run root escapes workspace")
    if not args.manifest.is_absolute():
        args.manifest = workspace / args.manifest
    # Refuse before credentials, materialization or provider access.
    import yaml
    from warptest.lazarus_p2p_only_experiment import registered_schedule
    manifest = yaml.safe_load(args.manifest.read_text())
    if args.outer_workers is not None and args.outer_workers != manifest.get("outer_workers"):
        raise SystemExit("--outer-workers must match the registered manifest lane count")
    from warptest.image_history_backoff import validate_manifest_policy
    validate_manifest_policy(manifest, requested=args.kimi_capacity_history_backoff)
    _, scope, auth_id, _arms = registered_schedule(manifest["models"], manifest["seeds"])
    auth = manifest.get("authorization", {})
    if auth.get("scope") != scope or auth.get(auth_id) is not True:
        raise SystemExit("Selected v3 paid dispatch is not authorized; offline planning remains available")
    run_root.mkdir(parents=True, exist_ok=False)
    import os
    from warptest.image_history_backoff import AUDIT_ENV, POLICY_ENV, REVISION, protocol
    # Require the explicit CLI switch; inherited shell settings cannot silently
    # change a paid experiment's request protocol.
    os.environ.pop(POLICY_ENV, None)
    os.environ.pop(AUDIT_ENV, None)
    transport_protocol = None
    if args.kimi_capacity_history_backoff:
        audit_root = run_root / "image-history-backoff"
        audit_root.mkdir()
        os.environ[POLICY_ENV] = REVISION
        os.environ[AUDIT_ENV] = str(audit_root)
        transport_protocol = protocol()
        (run_root / "protocol-change.json").write_text(json.dumps(transport_protocol, indent=2) + "\n")
    readiness_path = run_root / "readiness.json"
    report_path = run_root / "run.json"
    index_path = run_root / "run-index.json"
    authorization_path = run_root / "authorization.json"
    runtime_root = args.runtime_root or (
        workspace
        / ".warptest-cache/lazarus-p2p-only/paid-runtime"
        / run_root.name
    )
    runtime_root = runtime_root.expanduser().resolve()
    if workspace not in runtime_root.parents:
        raise ValueError("runtime root escapes workspace")
    with relay_environment(args.credentials), model_request_profiles_environment(
        manifest["model_request_profiles"]
    ):
        readiness = preflight_p2p_only(
            workspace,
            manifest_path=args.manifest,
            runtime_canary_path=args.runtime_canary,
            api_preflight_path=args.api_preflight,
            calibration_path=args.calibration,
            materialize=True,
        )
        write_readiness(readiness, readiness_path)
        if not readiness.complete:
            print(
                json.dumps(
                    {
                        "complete": False,
                        "dispatch_started": False,
                        "readiness": str(readiness_path),
                        "failure_count": len(readiness.failures),
                        "failures": list(readiness.failures),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        granted_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
        authorization = build_paid_authorization(readiness, granted_at=granted_at)
        if transport_protocol:
            authorization["image_history_backoff"] = transport_protocol
        authorization_path.write_text(
            json.dumps(authorization, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        # New attempt logs must never overwrite earlier paid evidence. Only
        # immutable synthesis, attested start gates and compiled assets are shared.
        attempt_runtime = run_root / "runtime"
        attempt_runtime.mkdir()
        for name in ("shared-v3", "start-gates-v3", "candidate-assets-v3"):
            target = runtime_root / name
            target.mkdir(parents=True, exist_ok=True)
            (attempt_runtime / name).symlink_to(target, target_is_directory=True)
        runtime = build_p2p_only_paid_runtime_runner(
            workspace,
            runtime_root=attempt_runtime,
            provider_timeout_s=900.0,
            method_config=readiness.contract.method_config,
        )
        executor = build_lazarus_p2p_only_executor(
            workspace,
            runtime_runner=runtime,
            p2p_sandbox_root=(
                workspace
                / ".warptest-cache/lazarus-p2p-only/per-trial-p2p"
                / run_root.name
            ),
            judge_model="qwen3-vl-flash@relay",
            attempt_log_root=run_root / "cell-ledgers",
        )
        resume = _load_json(args.resume_index) if args.resume_index else None
        quarantine = _load_json(args.quarantine) if args.quarantine else {"groups": []}
        quarantine_keys = tuple((row["task_id"], row["model"], int(row["seed"])) for row in quarantine["groups"])
        run = run_p2p_only_groups(
            readiness,
            executor=executor,
            outer_workers=(
                args.outer_workers if args.outer_workers is not None
                else readiness.contract.outer_workers
            ),
            resume_index=resume,
            progress_index_path=index_path,
            quarantined_groups=quarantine_keys,
        )
        write_run_index(
            run.reports,
            plan=readiness.plan,
            readiness=readiness,
            path=index_path,
        )
        write_run_report(
            run.reports,
            plan=readiness.plan,
            readiness=readiness,
            authorization=authorization,
            path=report_path,
        )
    recovery = {
        "retryable_groups": [
            {"task_id": group.task_id, "model": group.model, "seed": group.seed, "failures": list(group.failures)}
            for group in run.groups if group.retryable
        ],
        "terminal_failures": [failure for group in run.groups if not group.complete and not group.retryable for failure in group.failures],
    }
    payload = _load_json(report_path)
    if args.resume_index:
        import hashlib
        payload["resume_provenance"] = {
            "source_index": str(args.resume_index.resolve()),
            "source_index_sha256": hashlib.sha256(args.resume_index.read_bytes()).hexdigest(),
            "source_identity": resume.get("identity"),
            "current_identity": readiness.to_report()["identity"],
            "imported_groups": [list(identity) for identity in run.imported_groups],
            "validation": "immutable cell SHA, tuple coverage, artifact provenance and registered identity compatibility",
        }
    payload["quarantine"] = quarantine
    if transport_protocol:
        payload["protocol_change"] = transport_protocol
        payload["image_history_backoff_receipts"] = [
            _load_json(path) for path in sorted((run_root / "image-history-backoff").glob("*.json"))
        ]
        if args.resume_index:
            source_report_path = args.resume_index.parent / "run.json"
            if source_report_path.is_file():
                payload["image_history_backoff_receipts"] = (
                    _load_json(source_report_path).get("image_history_backoff_receipts", [])
                    + payload["image_history_backoff_receipts"]
                )
    payload["recovery"] = recovery
    payload["failures"] = list(run.failures)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "complete": run.complete,
                "expected_cells": readiness.plan.trial_count,
                "observed_cells": len(run.reports),
                "imported_groups": len(run.imported_groups),
                "failure_count": len(run.failures),
                "failures": list(run.failures),
                "report": str(report_path),
                "index": str(index_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if run.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
