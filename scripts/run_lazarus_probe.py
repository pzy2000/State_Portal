#!/usr/bin/env python3
"""Run one registered issue/PR-derived Lazarus probe against a version root."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_probes import luanti_pr15638_probe  # noqa: E402
from warptest.lazarus_openra_probe import (  # noqa: E402
    OPENRA_TABS_AUDIT_ENV,
    OPENRA_TABS_AUDIT_FILENAME,
    evaluate_openra_pr21906_audit,
    openra_pr21906_probe,
)
from warptest.lazarus_openra_hotkey_probe import (  # noqa: E402
    run_openra_pr21453_hotkey_probe,
)
from warptest.luanti_runtime import LuantiRuntimeAdapter  # noqa: E402
from warptest.openra_runtime import OpenRARuntimeAdapter  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", choices=("luanti", "openra"), required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--out-dir", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo = args.repo.expanduser().resolve()
    if args.out_dir:
        out_dir = args.out_dir.expanduser().resolve()
    else:
        parent = repo / ".warptest-lazarus-probes"
        parent.mkdir(parents=True, exist_ok=True)
        out_dir = Path(tempfile.mkdtemp(prefix=f"lazarus-{args.benchmark}-{args.case}-", dir=parent))
    if args.benchmark == "luanti":
        summary = _run_luanti(args.case, repo, out_dir)
    else:
        summary = _run_openra(args.case, repo, out_dir)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["passed"] else 1


def _run_luanti(case: str, repo: Path, out_dir: Path) -> dict[str, object]:
    if case not in {"normal-node", "torch", "liquid"}:
        raise ValueError("Luanti case must be normal-node, torch, or liquid")
    spec = luanti_pr15638_probe(case)
    spec_path = out_dir / f"luanti-pr15638-{case}.yaml"
    with LuantiRuntimeAdapter(
        repo,
        out_dir,
        headless=True,
        visual_capture=False,
        sync_builtin_from_repo=True,
    ) as runtime:
        result = runtime.run_checkpoint(spec, spec_path)
    return {
        "schema": "c1-lazarus-probe-result-v1",
        "schema_version": 1,
        "benchmark": "luanti",
        "case": case,
        "kind": spec["probe_case"]["kind"],
        "passed": result.get("status") == "success",
        "status": result.get("status"),
        "exit_code": result.get("exit_code"),
        "report_path": result.get("report_path"),
        "checks": result.get("checks"),
    }


def _run_openra(case: str, repo: Path, out_dir: Path) -> dict[str, object]:
    if case == "hidden-ready-building":
        return run_openra_pr21453_hotkey_probe(repo, out_dir)
    if case == "action-queue-p2p":
        return _run_openra_action_queue_p2p(repo)
    if case != "production-tabs":
        raise ValueError(
            "OpenRA case must be production-tabs, hidden-ready-building, or action-queue-p2p"
        )
    spec = openra_pr21906_probe("after")
    spec_path = out_dir / "openra-pr21906-production-tabs.yaml"
    audit_path = out_dir / OPENRA_TABS_AUDIT_FILENAME
    with _temporary_environment(OPENRA_TABS_AUDIT_ENV, str(audit_path)):
        with OpenRARuntimeAdapter(repo, out_dir) as runtime:
            result = runtime.run_checkpoint(spec, spec_path)
    try:
        audit_raw = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        audit_raw = {"error": f"audit unavailable: {error}"}
    audit = evaluate_openra_pr21906_audit(audit_raw)
    passed = (
        result.get("status") == "success"
        and audit.complete
        and audit.goal_checks_ok
        and audit.invariants_ok
    )
    return {
        "schema": "c1-lazarus-probe-result-v1",
        "schema_version": 1,
        "benchmark": "openra",
        "case": case,
        "kind": "issue_derived_gui_differential",
        "passed": passed,
        "status": result.get("status"),
        "exit_code": result.get("exit_code"),
        "report_path": result.get("report_path"),
        "checks": result.get("checks"),
        "audit": {
            "complete": audit.complete,
            "goal_checks_ok": audit.goal_checks_ok,
            "defect_signature_ok": audit.defect_signature_ok,
            "invariants_ok": audit.invariants_ok,
            "failures": list(audit.failures),
        },
    }


def _run_openra_action_queue_p2p(repo: Path) -> dict[str, object]:
    """Run the registered upstream P2P test under the host's .NET runtime."""

    command = [
        "dotnet",
        "test",
        "OpenRA.Test/OpenRA.Test.csproj",
        "-c",
        "Release",
        "--no-build",
        "--no-restore",
        "--filter",
        "FullyQualifiedName=OpenRA.Test.ActionQueueTest.ActionsArePerformedOrderedByTimeThenByInsertionOrder",
    ]
    started = time.monotonic()
    environment = dict(os.environ)
    environment["DOTNET_ROLL_FORWARD"] = "Major"
    try:
        completed = subprocess.run(
            command,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
            env=environment,
        )
        passed = completed.returncode == 0
        return {
            "schema": "c1-lazarus-probe-result-v1",
            "schema_version": 1,
            "benchmark": "openra",
            "case": "action-queue-p2p",
            "kind": "pass_to_pass",
            "passed": passed,
            "status": "success" if passed else "failure",
            "exit_code": int(completed.returncode),
            "duration_ms": (time.monotonic() - started) * 1000.0,
            "stdout_tail": completed.stdout[-4_000:],
            "stderr_tail": completed.stderr[-4_000:],
        }
    except subprocess.TimeoutExpired as error:
        return {
            "schema": "c1-lazarus-probe-result-v1",
            "schema_version": 1,
            "benchmark": "openra",
            "case": "action-queue-p2p",
            "kind": "pass_to_pass",
            "passed": False,
            "status": "timeout",
            "exit_code": None,
            "duration_ms": (time.monotonic() - started) * 1000.0,
            "stdout_tail": _process_text(error.stdout)[-4_000:],
            "stderr_tail": _process_text(error.stderr)[-4_000:],
        }


def _process_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


@contextmanager
def _temporary_environment(name: str, value: str):
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


if __name__ == "__main__":
    raise SystemExit(main())
