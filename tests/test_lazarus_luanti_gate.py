from __future__ import annotations

from dataclasses import replace

from warptest.lazarus_goldens import LazarusGoldenVerification
from warptest.lazarus_luanti_gate import (
    LuantiSemanticObservation,
    evaluate_luanti_pr11919_gate,
)


def _goldens(**overrides) -> LazarusGoldenVerification:
    value = LazarusGoldenVerification(
        task_id="lz-luanti-11919-vector-from-string",
        metadata_sha256="a" * 64,
        complete=True,
        fixed_before_visual=True,
        buggy_before_visual=True,
        before_equivalent=True,
        fixed_after_visual=True,
        buggy_after_visual=True,
        failures=(),
    )
    return replace(value, **overrides)


def _semantic(**overrides) -> LuantiSemanticObservation:
    value = LuantiSemanticObservation(
        status="success",
        start_ok=True,
        view_ok=True,
        primary_ok=True,
        invariants_ok=True,
        report_sha256="b" * 64,
        checks=("ok",),
    )
    return replace(value, **overrides)


def test_luanti_gate_binds_upstream_truth_table_and_three_semantic_groups() -> None:
    result = evaluate_luanti_pr11919_gate(
        fixed_tests={
            "luanti-pr11919-vector-from-string-f2p": True,
            "luanti-vector-zero-p2p": True,
            "luanti-vector-to-string-p2p": True,
        },
        buggy_tests={
            "luanti-pr11919-vector-from-string-f2p": False,
            "luanti-vector-zero-p2p": True,
            "luanti-vector-to-string-p2p": True,
        },
        goldens=_goldens(),
        fixed_start=_semantic(),
        buggy_start=_semantic(),
        fixed_goal=_semantic(),
        buggy_signature=_semantic(),
        fixed_signature=_semantic(status="failure", primary_ok=False),
        provenance_verified=True,
    )

    assert result.passed is True
    assert result.complete is True
    assert result.f2p_claim == "upstream_fail_to_pass"
    assert result.checks["fixed_f2p"] is True
    assert result.checks["buggy_f2p"] is False
    assert result.checks["fixed_defect_signature"] is False


def test_luanti_gate_fails_closed_on_golden_or_invariant_drift() -> None:
    common = {
        "fixed_tests": {
            "luanti-pr11919-vector-from-string-f2p": True,
            "luanti-vector-zero-p2p": True,
            "luanti-vector-to-string-p2p": True,
        },
        "buggy_tests": {
            "luanti-pr11919-vector-from-string-f2p": False,
            "luanti-vector-zero-p2p": True,
            "luanti-vector-to-string-p2p": True,
        },
        "fixed_start": _semantic(),
        "buggy_start": _semantic(),
        "fixed_goal": _semantic(),
        "buggy_signature": _semantic(),
        "fixed_signature": _semantic(status="failure", primary_ok=False),
        "provenance_verified": True,
    }

    golden_failure = evaluate_luanti_pr11919_gate(
        **common,
        goldens=_goldens(complete=False, buggy_after_visual=False, failures=("tampered",)),
    )
    invariant_failure = evaluate_luanti_pr11919_gate(
        **{
            **common,
            "goldens": _goldens(),
            "buggy_signature": _semantic(invariants_ok=False),
        },
    )

    assert golden_failure.passed is False
    assert "buggy_after_visual" in " ".join(golden_failure.failures)
    assert invariant_failure.passed is False
    assert "buggy_invariants" in " ".join(invariant_failure.failures)


def test_luanti_gate_rejects_a_failed_start_report_even_if_target_rows_pass() -> None:
    result = evaluate_luanti_pr11919_gate(
        fixed_tests={
            "luanti-pr11919-vector-from-string-f2p": True,
            "luanti-vector-zero-p2p": True,
            "luanti-vector-to-string-p2p": True,
        },
        buggy_tests={
            "luanti-pr11919-vector-from-string-f2p": False,
            "luanti-vector-zero-p2p": True,
            "luanti-vector-to-string-p2p": True,
        },
        goldens=_goldens(),
        fixed_start=_semantic(status="failure", start_ok=True),
        buggy_start=_semantic(status="failure", start_ok=True),
        fixed_goal=_semantic(),
        buggy_signature=_semantic(),
        fixed_signature=_semantic(status="failure", primary_ok=False),
        provenance_verified=True,
    )

    assert result.passed is False
    assert result.checks["fixed_before_semantic"] is False
    assert result.checks["buggy_before_semantic"] is False


def test_luanti_gate_rejects_start_when_public_player_view_is_not_proven() -> None:
    tests = {
        "luanti-pr11919-vector-from-string-f2p": True,
        "luanti-vector-zero-p2p": True,
        "luanti-vector-to-string-p2p": True,
    }
    result = evaluate_luanti_pr11919_gate(
        fixed_tests=tests,
        buggy_tests={**tests, "luanti-pr11919-vector-from-string-f2p": False},
        goldens=_goldens(),
        fixed_start=_semantic(view_ok=False),
        buggy_start=_semantic(view_ok=False),
        fixed_goal=_semantic(),
        buggy_signature=_semantic(),
        fixed_signature=_semantic(status="failure", primary_ok=False),
        provenance_verified=True,
    )

    assert result.passed is False
    assert result.checks["fixed_before_semantic"] is False
    assert result.checks["buggy_before_semantic"] is False
