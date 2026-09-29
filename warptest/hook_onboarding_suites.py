"""Visible acceptance and agent-generated self-test suites for hook onboarding."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

import yaml

from .hook_onboarding import (
    ALLOWED_ROUNDTRIPS,
    CaseSpec,
    OnboardingConfigError,
    find_forbidden_result_keys,
    run_driver_case,
    write_json,
)


SELF_TEST_PATH = Path("warptest_onboarding/self_tests/cases.yaml")


def _git_status(repo: Path) -> str:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.stdout


def _case_identity(case: CaseSpec) -> str:
    return json.dumps(
        {"target": case.target, "roundtrip": case.roundtrip},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def load_public_case_suite(path: str | Path, *, expected_app_id: str) -> list[CaseSpec]:
    source = Path(path).expanduser().resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OnboardingConfigError(f"cannot load visible development suite: {error}") from error
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        raise OnboardingConfigError("visible development suite schema_version must be 1")
    if value.get("app_id") != expected_app_id:
        raise OnboardingConfigError("visible development suite app_id mismatch")
    cases_raw = value.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise OnboardingConfigError("visible development suite must contain cases")
    return [
        CaseSpec.from_mapping(item, label=f"development_suite[{index}]")
        for index, item in enumerate(cases_raw)
    ]


def load_generated_self_tests(
    repo: str | Path,
    *,
    expected_app_id: str,
    development_cases: Sequence[CaseSpec],
    min_cases: int,
    max_cases: int,
) -> tuple[list[CaseSpec], dict[str, Any]]:
    root = Path(repo).expanduser().resolve()
    source = root / SELF_TEST_PATH
    try:
        value = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise OnboardingConfigError(f"cannot load generated self-test suite: {error}") from error
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        raise OnboardingConfigError("generated self-test suite schema_version must be 1")
    if value.get("app_id") != expected_app_id:
        raise OnboardingConfigError("generated self-test suite app_id mismatch")
    forbidden = find_forbidden_result_keys(value)
    if forbidden:
        raise OnboardingConfigError(
            "generated self-test suite contains forbidden hash/fingerprint keys: " + ", ".join(forbidden)
        )
    cases_raw = value.get("cases")
    if not isinstance(cases_raw, list) or not min_cases <= len(cases_raw) <= max_cases:
        raise OnboardingConfigError(
            f"generated self-test suite must contain between {min_cases} and {max_cases} cases"
        )
    cases: list[CaseSpec] = []
    coverage: set[str] = set()
    rationales: list[str] = []
    for index, raw in enumerate(cases_raw):
        label = f"self_tests.cases[{index}]"
        if not isinstance(raw, Mapping):
            raise OnboardingConfigError(f"{label} must be a mapping")
        if raw.get("verifier") is not None:
            raise OnboardingConfigError(f"{label}.verifier is not allowed in agent-generated tests")
        rationale = str(raw.get("rationale") or "").strip()
        declared = raw.get("coverage")
        if not rationale:
            raise OnboardingConfigError(f"{label}.rationale is required")
        if not isinstance(declared, list) or not declared or any(
            not isinstance(item, str) or not item.strip() for item in declared
        ):
            raise OnboardingConfigError(f"{label}.coverage must contain non-empty strings")
        case = CaseSpec.from_mapping(raw, label=label)
        if not case.id.startswith("self-"):
            raise OnboardingConfigError(f"{label}.id must start with 'self-'")
        cases.append(case)
        rationales.append(rationale)
        coverage.update(item.strip() for item in declared)
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise OnboardingConfigError("generated self-test case ids must be unique")
    identities = [_case_identity(case) for case in cases]
    if len(identities) != len(set(identities)):
        raise OnboardingConfigError("generated self-test target/roundtrip pairs must be unique")
    development_identities = {_case_identity(case) for case in development_cases}
    if any(identity in development_identities for identity in identities):
        raise OnboardingConfigError("generated self-tests must not duplicate a development case")
    roundtrips = {case.roundtrip for case in cases}
    if roundtrips != set(ALLOWED_ROUNDTRIPS):
        raise OnboardingConfigError("generated self-tests must cover both live and save_reload")
    if sum(case.roundtrip == "save_reload" for case in cases) < 2:
        raise OnboardingConfigError("generated self-tests must include at least two save_reload cases")
    return cases, {
        "schema_version": 1,
        "app_id": expected_app_id,
        "case_count": len(cases),
        "case_ids": ids,
        "roundtrips": sorted(roundtrips),
        "declared_coverage": sorted(coverage),
        "rationales": rationales,
        "source_path": str(source),
    }


def run_case_suite(
    repo: str | Path,
    cases: Sequence[CaseSpec],
    report_dir: str | Path,
    *,
    driver_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    root = Path(repo).expanduser().resolve()
    out = Path(report_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    before = _git_status(root)
    results: list[dict[str, Any]] = []
    source_mutation = False
    for case in cases:
        results.append(run_driver_case(root, case, out, driver_env=driver_env))
        if _git_status(root) != before:
            source_mutation = True
            break
    passed = sum(result.get("status") == "success" for result in results)
    status = "pass" if passed == len(cases) and not source_mutation else "failure"
    return {
        "schema_version": 1,
        "status": status,
        "case_count": len(cases),
        "cases_run": len(results),
        "passes": passed,
        "source_mutation": source_mutation,
        "results": results,
    }


def run_generated_self_tests(
    repo: str | Path,
    *,
    app_id: str,
    development_cases: Sequence[CaseSpec],
    report_dir: str | Path,
    min_cases: int,
    max_cases: int,
    driver_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    cases, metadata = load_generated_self_tests(
        repo,
        expected_app_id=app_id,
        development_cases=development_cases,
        min_cases=min_cases,
        max_cases=max_cases,
    )
    result = run_case_suite(repo, cases, report_dir, driver_env=driver_env)
    result["metadata"] = metadata
    return result


def write_suite_result(path: str | Path, value: Mapping[str, Any]) -> None:
    write_json(Path(path), dict(value))


__all__ = [
    "SELF_TEST_PATH",
    "load_generated_self_tests",
    "load_public_case_suite",
    "run_case_suite",
    "run_generated_self_tests",
    "write_suite_result",
]
