#!/usr/bin/env python3
"""Run a visible development suite or agent-generated self-test suite."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys


HARNESS_ROOT = Path(__file__).resolve().parents[1]
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

from warptest.hook_onboarding_suites import (  # noqa: E402
    load_public_case_suite,
    run_case_suite,
    run_generated_self_tests,
    write_suite_result,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("development", "self-tests"), required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--suite", type=Path)
    parser.add_argument("--min-cases", type=int, default=5)
    parser.add_argument("--max-cases", type=int, default=10)
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()
    report_dir = args.report_dir.expanduser().resolve()
    if report_dir.exists():
        shutil.rmtree(report_dir)
    report_dir.mkdir(parents=True)
    if args.suite is None:
        raise SystemExit("--suite is required")
    development_cases = load_public_case_suite(args.suite, expected_app_id=args.app)
    if args.mode == "development":
        result = run_case_suite(repo, development_cases, report_dir)
    else:
        result = run_generated_self_tests(
            repo,
            app_id=args.app,
            development_cases=development_cases,
            report_dir=report_dir,
            min_cases=args.min_cases,
            max_cases=args.max_cases,
        )
    write_suite_result(report_dir / "summary.json", result)
    print(
        json.dumps(
            {
                "status": result["status"],
                "case_count": result["case_count"],
                "cases_run": result["cases_run"],
                "passes": result["passes"],
                "source_mutation": result["source_mutation"],
                "failures": [
                    {
                        "id": item.get("id"),
                        "stage": item.get("stage"),
                        "errors": item.get("errors", []),
                        "driver_detail": (
                            item.get("payload", {}).get("detail")
                            if isinstance(item.get("payload"), dict)
                            else None
                        ),
                    }
                    for item in result.get("results", [])
                    if item.get("status") != "success"
                ],
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if result["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
