"""Differential construction gate for C1-Lazarus task state correctness."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


LAZARUS_GATE_SCHEMA_VERSION = 1
_F2P_ORIGINS = frozenset({"upstream", "issue_derived_gui_differential"})


@dataclass(frozen=True)
class ConstructionGateEvidence:
    task_id: str
    benchmark: str
    f2p_origin: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    fixed_tests: Mapping[str, bool]
    buggy_tests: Mapping[str, bool]
    provenance_verified: bool
    fixed_before_semantic: bool
    buggy_before_semantic: bool
    fixed_before_visual: bool
    buggy_before_visual: bool
    before_equivalent: bool
    fixed_goal_checks: bool
    fixed_after_visual: bool
    buggy_defect_signature: bool
    buggy_after_visual: bool
    fixed_defect_signature: bool
    fixed_invariants: bool
    buggy_invariants: bool


@dataclass(frozen=True)
class ConstructionGateResult:
    task_id: str
    benchmark: str
    passed: bool
    complete: bool
    checks: dict[str, bool]
    failures: tuple[str, ...]
    f2p_claim: str
    upstream_f2p: bool
    f2p_origin: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]

    def to_report(self) -> dict[str, Any]:
        value = asdict(self)
        value.update(
            {
                "schema_version": LAZARUS_GATE_SCHEMA_VERSION,
                "status": "passed" if self.passed else "failed",
            }
        )
        return value


@dataclass(frozen=True)
class ConstructionTestMatrixResult:
    """The construction-only F2P/P2P truth table before GUI/oracle gates."""

    task_id: str
    qualified: bool
    complete: bool
    fixed_tests: dict[str, bool]
    buggy_tests: dict[str, bool]
    failures: tuple[str, ...]


def evaluate_construction_test_matrix(
    *,
    task_id: str,
    fail_to_pass: tuple[str, ...],
    pass_to_pass: tuple[str, ...],
    fixed_tests: Mapping[str, bool],
    buggy_tests: Mapping[str, bool],
) -> ConstructionTestMatrixResult:
    """Qualify real upstream tests without pretending this is the full gate."""

    failures: list[str] = []
    missing: list[str] = []
    f2p_ids = _normalized_ids(fail_to_pass, "fail_to_pass", failures)
    p2p_ids = _normalized_ids(pass_to_pass, "pass_to_pass", failures)
    if set(f2p_ids) & set(p2p_ids):
        failures.append("fail_to_pass and pass_to_pass ids overlap")
    fixed_f2p = _outcomes(fixed_tests, f2p_ids, "fixed fail_to_pass", missing)
    buggy_f2p = _outcomes(buggy_tests, f2p_ids, "buggy fail_to_pass", missing)
    fixed_p2p = _outcomes(fixed_tests, p2p_ids, "fixed pass_to_pass", missing)
    buggy_p2p = _outcomes(buggy_tests, p2p_ids, "buggy pass_to_pass", missing)
    if fixed_f2p and not all(fixed_f2p):
        failures.append("fixed fail_to_pass did not pass")
    if buggy_f2p and any(buggy_f2p):
        failures.append("buggy fail_to_pass did not fail")
    if fixed_p2p and not all(fixed_p2p):
        failures.append("fixed pass_to_pass did not pass")
    if buggy_p2p and not all(buggy_p2p):
        failures.append("buggy pass_to_pass did not pass")
    failures.extend(missing)
    complete = not missing and bool(f2p_ids) and bool(p2p_ids)
    return ConstructionTestMatrixResult(
        task_id=str(task_id),
        qualified=complete and not failures,
        complete=complete,
        fixed_tests={key: bool(value) for key, value in fixed_tests.items()},
        buggy_tests={key: bool(value) for key, value in buggy_tests.items()},
        failures=tuple(dict.fromkeys(failures)),
    )


def evaluate_construction_gate(
    evidence: ConstructionGateEvidence,
) -> ConstructionGateResult:
    """Apply the registered F2P/P2P and three-group oracle truth table."""

    failures: list[str] = []
    missing: list[str] = []
    f2p_ids = _normalized_ids(evidence.fail_to_pass, "fail_to_pass", failures)
    p2p_ids = _normalized_ids(evidence.pass_to_pass, "pass_to_pass", failures)
    if set(f2p_ids) & set(p2p_ids):
        failures.append("fail_to_pass and pass_to_pass ids overlap")

    fixed_f2p_values = _outcomes(
        evidence.fixed_tests, f2p_ids, "fixed fail_to_pass", missing
    )
    buggy_f2p_values = _outcomes(
        evidence.buggy_tests, f2p_ids, "buggy fail_to_pass", missing
    )
    fixed_p2p_values = _outcomes(
        evidence.fixed_tests, p2p_ids, "fixed pass_to_pass", missing
    )
    buggy_p2p_values = _outcomes(
        evidence.buggy_tests, p2p_ids, "buggy pass_to_pass", missing
    )

    checks = {
        "fixed_f2p": bool(fixed_f2p_values) and all(fixed_f2p_values),
        # This is the observed aggregate pass bit, deliberately false for the
        # qualified buggy cell.
        "buggy_f2p": any(buggy_f2p_values),
        "fixed_p2p": bool(fixed_p2p_values) and all(fixed_p2p_values),
        "buggy_p2p": bool(buggy_p2p_values) and all(buggy_p2p_values),
        "provenance_verified": evidence.provenance_verified is True,
        "fixed_before_semantic": evidence.fixed_before_semantic is True,
        "buggy_before_semantic": evidence.buggy_before_semantic is True,
        "fixed_before_visual": evidence.fixed_before_visual is True,
        "buggy_before_visual": evidence.buggy_before_visual is True,
        "before_equivalent": evidence.before_equivalent is True,
        "fixed_goal_checks": evidence.fixed_goal_checks is True,
        "fixed_after_visual": evidence.fixed_after_visual is True,
        "buggy_defect_signature": evidence.buggy_defect_signature is True,
        "buggy_after_visual": evidence.buggy_after_visual is True,
        "fixed_defect_signature": evidence.fixed_defect_signature is True,
        "fixed_invariants": evidence.fixed_invariants is True,
        "buggy_invariants": evidence.buggy_invariants is True,
    }

    if evidence.f2p_origin not in _F2P_ORIGINS:
        failures.append(f"unknown f2p_origin {evidence.f2p_origin!r}")
        f2p_claim = "invalid"
        upstream_f2p = False
    elif evidence.f2p_origin == "issue_derived_gui_differential":
        upstream_f2p = False
        f2p_claim = "issue_derived_gui_differential_not_upstream"
        if evidence.benchmark != "openra":
            failures.append(
                "issue_derived_gui_differential is permitted only for openra"
            )
    else:
        upstream_f2p = True
        f2p_claim = "upstream_fail_to_pass"

    if not checks["fixed_f2p"]:
        failures.append("fixed fail_to_pass did not pass")
    if checks["buggy_f2p"]:
        failures.append("buggy fail_to_pass did not fail")
    if not checks["fixed_p2p"]:
        failures.append("fixed pass_to_pass did not pass")
    if not checks["buggy_p2p"]:
        failures.append("buggy pass_to_pass did not pass")

    required_true = (
        "provenance_verified",
        "fixed_before_semantic",
        "buggy_before_semantic",
        "fixed_before_visual",
        "buggy_before_visual",
        "before_equivalent",
        "fixed_goal_checks",
        "fixed_after_visual",
        "buggy_defect_signature",
        "buggy_after_visual",
        "fixed_invariants",
        "buggy_invariants",
    )
    for name in required_true:
        if not checks[name]:
            failures.append(f"{name} is false")
    if checks["fixed_defect_signature"]:
        failures.append("fixed_defect_signature must be false")

    if missing:
        failures.extend(missing)
    complete = not missing
    passed = complete and not failures
    return ConstructionGateResult(
        task_id=evidence.task_id,
        benchmark=evidence.benchmark,
        passed=passed,
        complete=complete,
        checks=checks,
        failures=tuple(dict.fromkeys(failures)),
        f2p_claim=f2p_claim,
        upstream_f2p=upstream_f2p,
        f2p_origin=evidence.f2p_origin,
        fail_to_pass=f2p_ids,
        pass_to_pass=p2p_ids,
    )


def write_construction_gate_report(
    result: ConstructionGateResult, path: str | Path
) -> Path:
    """Atomically persist a coordinator-side gate report."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(result.to_report(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _normalized_ids(
    values: tuple[str, ...], label: str, failures: list[str]
) -> tuple[str, ...]:
    normalized = tuple(str(value).strip() for value in values)
    if not normalized or any(not value for value in normalized):
        failures.append(f"{label} ids must be non-empty")
    if len(normalized) != len(set(normalized)):
        failures.append(f"{label} ids contain duplicates")
    return normalized


def _outcomes(
    values: Mapping[str, bool],
    ids: tuple[str, ...],
    label: str,
    missing: list[str],
) -> list[bool]:
    outcomes: list[bool] = []
    for test_id in ids:
        value = values.get(test_id)
        if type(value) is not bool:
            missing.append(f"missing {label} outcome for {test_id}")
            continue
        outcomes.append(value)
    return outcomes
