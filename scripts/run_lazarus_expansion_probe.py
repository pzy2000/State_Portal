#!/usr/bin/env python3
"""Replay one C1-Lazarus expansion row and grade it against its registered oracle.

This is the dispatcher a formal trial uses, exercised directly: it replays the
task's registered steps on a materialized version and resolves the row's own
``goal_checks``, ``defect_signature`` and ``invariant_checks`` against what it
observed.  Running it on both versions is how a reviewer confirms a row is
executable rather than merely declared executable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from warptest.lazarus_expansion_specs import EXPANSION_ROWS_BY_ID  # noqa: E402
from warptest.lazarus_jupyterlab_app_probe import (  # noqa: E402
    app_probe_bindings,
    evaluate_jupyterlab_app_lazarus_audit,
    evaluate_jupyterlab_app_lazarus_checks,
    run_jupyterlab_app_lazarus_probe,
)
from warptest.lazarus_luanti_expansion import (  # noqa: E402
    LUANTI_EXPANSION_CHANGES,
    build_luanti_expansion_binding,
)
from warptest.lazarus_luanti_probe import (  # noqa: E402
    evaluate_luanti_lazarus_audit,
    evaluate_luanti_lazarus_checks,
    run_luanti_lazarus_probe,
)


_VERSIONS = ("fixed", "buggy")
_PROBED = sorted(
    task_id
    for task_id, row in EXPANSION_ROWS_BY_ID.items()
    if row.benchmark in {"jupyterlab", "luanti"}
)


def _spec_checks(workspace: Path, task_id: str) -> dict[str, list]:
    row = EXPANSION_ROWS_BY_ID[task_id]
    spec = yaml.safe_load((workspace / row.spec_path).read_text(encoding="utf-8"))
    lazarus = spec["evaluation"]["lazarus"]
    return {
        "goal_checks": lazarus["goal_checks"],
        "defect_signature": lazarus["defect_signature"],
        "invariant_checks": lazarus["invariant_checks"],
    }


def _unmet_requirements(result: dict) -> list[str]:
    reasons = []
    if result["audit_complete"] is not True:
        reasons.append(
            "the audit did not complete: "
            + "; ".join(result["audit_failures"] or ["no recorded reason"])
        )
    reasons.extend(f"probe error: {error}" for error in result["probe_errors"])
    if result["resolved"]["invariant_checks"] is not True:
        reasons.append("invariant checks did not hold")
    return reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id", choices=_PROBED)
    parser.add_argument("--version", choices=_VERSIONS, action="append", default=[])
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument(
        "--prebuilt-root", type=Path, default=ROOT / ".warptest-cache/lazarus/prebuilt"
    )
    parser.add_argument(
        "--sandbox-root", type=Path, default=ROOT / ".warptest-cache/lazarus/probe-runs"
    )
    args = parser.parse_args(argv)
    versions = tuple(args.version) or _VERSIONS

    row = EXPANSION_ROWS_BY_ID[args.task_id]
    checks = _spec_checks(args.workspace, args.task_id)
    if row.benchmark == "jupyterlab":
        bindings = app_probe_bindings(
            prebuilt_root=args.prebuilt_root,
            sandbox_root=args.sandbox_root / "jupyterlab",
        )

        def run(version: str) -> dict:
            return run_jupyterlab_app_lazarus_probe(
                task_id=args.task_id, version=version, bindings=bindings
            )

        grade, resolve = (
            evaluate_jupyterlab_app_lazarus_audit,
            evaluate_jupyterlab_app_lazarus_checks,
        )
    else:
        change_number = next(
            change
            for change, task_id in LUANTI_EXPANSION_CHANGES.items()
            if task_id == args.task_id
        )
        binding = build_luanti_expansion_binding(
            args.workspace,
            change_number=change_number,
            prebuilt_root=args.prebuilt_root,
            sandbox_root=args.sandbox_root / "luanti",
        )
        registry = {binding.task_id: binding}

        def run(version: str) -> dict:
            return run_luanti_lazarus_probe(
                args.workspace,
                task_id=args.task_id,
                version=version,
                bindings=registry,
            )

        grade, resolve = (
            evaluate_luanti_lazarus_audit,
            evaluate_luanti_lazarus_checks,
        )

    results: dict[str, dict] = {}
    for version in versions:
        audit = run(version)
        verdict = grade(audit)
        resolved = {
            name: resolve(group, audit)[0] for name, group in checks.items()
        }
        results[version] = {
            "audit_complete": verdict.complete,
            "audit_failures": list(verdict.failures),
            "observed": audit.get("observed"),
            "probe_errors": list(audit.get("probe_errors") or []),
            "resolved": resolved,
        }
    # A row is executable only when the pair separates: the fixed product must
    # satisfy the goal and not the signature, and the buggy product the inverse.
    separates = None
    if set(results) == set(_VERSIONS):
        separates = (
            results["fixed"]["resolved"]["goal_checks"] is True
            and results["fixed"]["resolved"]["defect_signature"] is False
            and results["buggy"]["resolved"]["goal_checks"] is False
            and results["buggy"]["resolved"]["defect_signature"] is True
        )
    # An unexecuted or partially executed replay proves nothing, so every
    # requested version must have completed its audit, raised no probe error and
    # held its invariants; only the full pair can additionally prove separation.
    failures = [
        f"{version}: {reason}"
        for version, result in sorted(results.items())
        for reason in _unmet_requirements(result)
    ]
    if separates is False:
        failures.append("the version pair does not separate the defect")
    elif separates is None:
        failures.append(
            "separation is unproven: both versions must be probed in one run"
        )
    print(
        json.dumps(
            {
                "task_id": args.task_id,
                "ok": not failures,
                "failures": failures,
                "separates": separates,
                "versions": results,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
