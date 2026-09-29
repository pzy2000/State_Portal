from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from warptest.lazarus_gate import (
    ConstructionGateEvidence,
    evaluate_construction_test_matrix,
    evaluate_construction_gate,
    write_construction_gate_report,
)


@pytest.mark.parametrize(
    ("fixed_f2p", "buggy_f2p", "fixed_p2p", "buggy_p2p", "qualified"),
    [
        (True, False, True, True, True),
        (True, True, True, True, False),
        (False, False, True, True, False),
        (True, False, False, True, False),
        (True, False, True, False, False),
    ],
)
def test_construction_test_matrix_uses_real_f2p_p2p_truth_table(
    fixed_f2p: bool,
    buggy_f2p: bool,
    fixed_p2p: bool,
    buggy_p2p: bool,
    qualified: bool,
) -> None:
    result = evaluate_construction_test_matrix(
        task_id="lz-jupyterlab-example",
        fail_to_pass=("regression",),
        pass_to_pass=("smoke",),
        fixed_tests={"regression": fixed_f2p, "smoke": fixed_p2p},
        buggy_tests={"regression": buggy_f2p, "smoke": buggy_p2p},
    )

    assert result.complete is True
    assert result.qualified is qualified


def _evidence(**overrides) -> ConstructionGateEvidence:
    value = ConstructionGateEvidence(
        task_id="lz-luanti-example",
        benchmark="luanti",
        f2p_origin="upstream",
        fail_to_pass=("regression",),
        pass_to_pass=("smoke",),
        fixed_tests={"regression": True, "smoke": True},
        buggy_tests={"regression": False, "smoke": True},
        provenance_verified=True,
        fixed_before_semantic=True,
        buggy_before_semantic=True,
        fixed_before_visual=True,
        buggy_before_visual=True,
        before_equivalent=True,
        fixed_goal_checks=True,
        fixed_after_visual=True,
        buggy_defect_signature=True,
        buggy_after_visual=True,
        fixed_defect_signature=False,
        fixed_invariants=True,
        buggy_invariants=True,
    )
    return replace(value, **overrides)


def test_construction_gate_accepts_only_the_full_differential_contract(
    tmp_path: Path,
) -> None:
    result = evaluate_construction_gate(_evidence())

    assert result.passed is True
    assert result.complete is True
    assert result.failures == ()
    assert result.checks["fixed_f2p"] is True
    assert result.checks["buggy_f2p"] is False
    assert result.checks["fixed_p2p"] is True
    assert result.checks["buggy_p2p"] is True
    assert result.f2p_claim == "upstream_fail_to_pass"

    report_path = tmp_path / "gate.json"
    write_construction_gate_report(result, report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["status"] == "passed"
    assert report["complete"] is True
    assert report["task_id"] == "lz-luanti-example"


@pytest.mark.parametrize(
    ("fixed", "buggy", "passed"),
    [
        (True, False, True),
        (True, True, False),
        (False, False, False),
        (False, True, False),
    ],
)
def test_fail_to_pass_truth_table(fixed: bool, buggy: bool, passed: bool) -> None:
    result = evaluate_construction_gate(
        _evidence(
            fixed_tests={"regression": fixed, "smoke": True},
            buggy_tests={"regression": buggy, "smoke": True},
        )
    )

    assert result.passed is passed


@pytest.mark.parametrize(
    ("fixed", "buggy", "passed"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
        (False, False, False),
    ],
)
def test_pass_to_pass_truth_table(fixed: bool, buggy: bool, passed: bool) -> None:
    result = evaluate_construction_gate(
        _evidence(
            fixed_tests={"regression": True, "smoke": fixed},
            buggy_tests={"regression": False, "smoke": buggy},
        )
    )

    assert result.passed is passed


@pytest.mark.parametrize(
    "field",
    [
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
    ],
)
def test_construction_gate_fails_closed_when_required_evidence_is_false(
    field: str,
) -> None:
    result = evaluate_construction_gate(_evidence(**{field: False}))

    assert result.passed is False
    assert result.complete is True
    assert field in " ".join(result.failures)


def test_construction_gate_fails_closed_on_missing_outcome_and_fixed_signature() -> None:
    missing = evaluate_construction_gate(
        _evidence(fixed_tests={"regression": True})
    )
    false_alarm = evaluate_construction_gate(_evidence(fixed_defect_signature=True))

    assert missing.passed is False
    assert missing.complete is False
    assert "missing" in " ".join(missing.failures)
    assert false_alarm.passed is False
    assert false_alarm.complete is True
    assert "fixed_defect_signature" in " ".join(false_alarm.failures)


def test_openra_gui_differential_exception_is_explicit_and_keeps_p2p() -> None:
    allowed = evaluate_construction_gate(
        _evidence(
            task_id="lz-openra-example",
            benchmark="openra",
            f2p_origin="issue_derived_gui_differential",
        )
    )
    missing_p2p = evaluate_construction_gate(
        _evidence(
            task_id="lz-openra-example",
            benchmark="openra",
            f2p_origin="issue_derived_gui_differential",
            buggy_tests={"regression": False, "smoke": False},
        )
    )
    forbidden_elsewhere = evaluate_construction_gate(
        _evidence(f2p_origin="issue_derived_gui_differential")
    )

    assert allowed.passed is True
    assert allowed.f2p_claim == "issue_derived_gui_differential_not_upstream"
    assert allowed.upstream_f2p is False
    assert missing_p2p.passed is False
    assert forbidden_elsewhere.passed is False
