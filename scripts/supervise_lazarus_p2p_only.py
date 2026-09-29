#!/usr/bin/env python3
"""Persistently run a prepared Lazarus schedule and recover typed transient outages."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from warptest.lazarus_p2p_only_recovery import atomic_receipt  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--credentials", type=Path, default=ROOT / "test_model_batch.py"
    )
    parser.add_argument("--runtime-canary", type=Path, required=True)
    parser.add_argument("--api-preflight", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--kimi-capacity-history-backoff", action="store_true")
    parser.add_argument("--quarantine", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    # A deterministic local stall is retryable per cell, so bound the paid
    # replay loop instead of trusting every recovery to be a transient flake.
    parser.add_argument("--max-recoveries", type=int, default=8)
    args = parser.parse_args()
    import yaml
    from warptest.image_history_backoff import validate_manifest_policy

    manifest = yaml.safe_load(args.manifest.read_text())
    validate_manifest_policy(
        manifest,
        requested=args.kimi_capacity_history_backoff,
    )
    root = args.root.resolve()
    lock = root / "supervisor.lock"
    # Exclusive lock is released by the OS on process exit, permitting safe restart.
    import fcntl

    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        attempts = root / "attempts"
        attempts.mkdir(exist_ok=True)

        def status(state, **fields):
            atomic_receipt(
                root / "supervisor-status.json",
                {
                    "state": state,
                    "pid": os.getpid(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    **fields,
                },
            )

        prior = sorted(attempts.glob("attempt-*"))
        if prior and (prior[-1] / "authorization.json").is_file() and not (prior[-1] / "run.json").is_file():
            # A reboot/kill can interrupt a billable request before its ledger
            # is durable. An older index alone does not authorize replay.
            status("needs_recovery", attempt=len(prior), run_root=str(prior[-1]),
                   failures=["Interrupted paid attempt has no terminal report; reconcile provider usage before resuming"])
            return 1
        resume = None
        for entry in reversed(prior):
            if (entry / "run-index.json").is_file():
                resume = entry / "run-index.json"
                break
        if prior and (prior[-1] / "run.json").is_file():
            previous = json.loads((prior[-1] / "run.json").read_text())
            if previous.get("complete") is True:
                run = prior[-1]
                with (root / "audit.log").open("ab") as log:
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(ROOT / "scripts/audit_lazarus_p2p_only_run.py"),
                            "--run-root",
                            str(run),
                        ],
                        cwd=ROOT,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                status(
                    "complete" if result.returncode == 0 else "audit_failed",
                    run_root=str(run),
                )
                return result.returncode
        number = len(prior)
        recoveries = 0
        while True:
            number += 1
            run = attempts / f"attempt-{number:03d}"
            command = [
                sys.executable,
                str(ROOT / "scripts/run_lazarus_p2p_only.py"),
                "--manifest",
                str(args.manifest.resolve()),
                "--run-root",
                str(run),
                "--credentials",
                str(args.credentials.resolve()),
                "--runtime-canary",
                str(args.runtime_canary.resolve()),
                "--api-preflight",
                str(args.api_preflight.resolve()),
                "--calibration",
                str(args.calibration.resolve()),
                "--outer-workers",
                str(manifest["outer_workers"]),
                "--runtime-root",
                str(args.runtime_root.resolve() if args.runtime_root else ROOT / ".warptest-cache/lazarus-p2p-only/paid-runtime" / root.name),
            ]
            if args.kimi_capacity_history_backoff:
                command.append("--kimi-capacity-history-backoff")
            if args.quarantine:
                command.extend(["--quarantine", str(args.quarantine.resolve())])
            if resume:
                command.extend(["--resume-index", str(resume)])
            log_path = root / f"attempt-{number:03d}.log"
            with log_path.open("ab") as log:
                child = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                status(
                    "running", attempt=number, run_root=str(run), child_pid=child.pid
                )
                code = child.wait()
            atomic_receipt(
                root / f"attempt-{number:03d}-exit.json",
                {
                    "exit_code": code,
                    "run_root": str(run),
                    "command": command,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            report_path = run / "run.json"
            report = json.loads(report_path.read_text()) if report_path.exists() else {}
            if report.get("complete") is True:
                with (root / "audit.log").open("ab") as log:
                    audit = subprocess.run(
                        [
                            sys.executable,
                            str(ROOT / "scripts/audit_lazarus_p2p_only_run.py"),
                            "--run-root",
                            str(run),
                        ],
                        cwd=ROOT,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                status(
                    "complete" if audit.returncode == 0 else "audit_failed",
                    attempt=number,
                    run_root=str(run),
                    report=str(report_path),
                )
                return audit.returncode
            recovery = report.get("recovery", {})
            if recovery.get("retryable_groups") and not recovery.get(
                "terminal_failures"
            ) and recoveries < args.max_recoveries:
                recoveries += 1
                resume = run / "run-index.json"
                delay = min(300, 30 * 2 ** min(number - 1, 4))
                status(
                    "retry_wait",
                    attempt=number,
                    run_root=str(run),
                    delay_s=delay,
                    failures=report.get("failures", []),
                )
                time.sleep(delay)
                continue
            status(
                "needs_recovery",
                attempt=number,
                run_root=str(run),
                exit_code=code,
                failures=report.get("failures", []),
            )
            return code or 1


if __name__ == "__main__":
    raise SystemExit(main())
