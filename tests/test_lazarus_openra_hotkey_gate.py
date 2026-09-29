from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import warptest.lazarus_openra_hotkey_gate as openra_gate
from warptest.lazarus_gate import ConstructionGateResult
from warptest.lazarus_goldens import LazarusGoldenVerification
from warptest.lazarus_openra_hotkey_gate import (
    OpenRAHotkeySemanticObservation,
    evaluate_openra_pr21453_gate,
    run_openra_pr21453_revalidation_matrix,
)
from warptest.lazarus_upstream import (
    SubprocessOutputCapture,
    UpstreamSubsetResult,
    UpstreamTestResult,
)


def _goldens(**overrides) -> LazarusGoldenVerification:
    value = LazarusGoldenVerification(
        task_id="lz-openra-21453-hotkey-ready-building",
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


def _semantic(**overrides) -> OpenRAHotkeySemanticObservation:
    value = OpenRAHotkeySemanticObservation(
        status="success",
        start_ok=True,
        primary_ok=True,
        invariants_ok=True,
        report_sha256="b" * 64,
        checks=("ok",),
    )
    return replace(value, **overrides)


def test_openra_gate_allows_only_the_registered_issue_derived_f2p_exception() -> None:
    result = evaluate_openra_pr21453_gate(
        fixed_tests={
            "openra-pr21453-hidden-ready-building-f2p": True,
            "openra-action-queue-p2p": True,
        },
        buggy_tests={
            "openra-pr21453-hidden-ready-building-f2p": False,
            "openra-action-queue-p2p": True,
        },
        goldens=_goldens(),
        fixed_start=_semantic(),
        buggy_start=_semantic(),
        fixed_goal=_semantic(),
        buggy_signature=_semantic(),
        fixed_signature=_semantic(status="goal_not_reached", primary_ok=False),
        provenance_verified=True,
    )

    assert result.passed is True
    assert result.complete is True
    assert result.f2p_claim == "issue_derived_gui_differential_not_upstream"
    assert result.upstream_f2p is False
    assert result.checks["fixed_f2p"] is True
    assert result.checks["buggy_f2p"] is False


def test_openra_gate_fails_closed_when_the_fixed_signature_or_before_state_drifts() -> (
    None
):
    common = {
        "fixed_tests": {
            "openra-pr21453-hidden-ready-building-f2p": True,
            "openra-action-queue-p2p": True,
        },
        "buggy_tests": {
            "openra-pr21453-hidden-ready-building-f2p": False,
            "openra-action-queue-p2p": True,
        },
        "goldens": _goldens(),
        "fixed_start": _semantic(),
        "buggy_start": _semantic(),
        "fixed_goal": _semantic(),
        "buggy_signature": _semantic(),
        "provenance_verified": True,
    }

    signature_failure = evaluate_openra_pr21453_gate(
        **common,
        fixed_signature=_semantic(primary_ok=True),
    )
    start_failure = evaluate_openra_pr21453_gate(
        **{
            **common,
            "fixed_start": _semantic(start_ok=False),
            "fixed_signature": _semantic(status="goal_not_reached", primary_ok=False),
        },
    )

    assert signature_failure.passed is False
    assert "fixed_defect_signature" in " ".join(signature_failure.failures)
    assert start_failure.passed is False
    assert "fixed_before_semantic" in " ".join(start_failure.failures)


def _subset(version: str) -> UpstreamSubsetResult:
    f2p_passed = version == "fixed"
    return UpstreamSubsetResult(
        test_ids=(
            "openra-pr21453-hidden-ready-building-f2p",
            "openra-action-queue-p2p",
        ),
        complete=True,
        passed=f2p_passed,
        results=(
            UpstreamTestResult(
                id="openra-pr21453-hidden-ready-building-f2p",
                passed=f2p_passed,
                exit_code=0 if f2p_passed else 1,
                timed_out=False,
                duration_ms=12.0,
                stdout_sha256="a" * 64,
                stderr_sha256="b" * 64,
                output_capture=SubprocessOutputCapture(
                    schema="c1-lazarus-subprocess-output-v1",
                    schema_version=1,
                    redaction="environment_and_credential_markers_v1",
                    stdout="f2p output",
                    stderr="",
                    stdout_truncated=False,
                    stderr_truncated=False,
                ),
            ),
            UpstreamTestResult(
                id="openra-action-queue-p2p",
                passed=True,
                exit_code=0,
                timed_out=False,
                duration_ms=9.0,
                stdout_sha256="c" * 64,
                stderr_sha256="d" * 64,
                output_capture=SubprocessOutputCapture(
                    schema="c1-lazarus-subprocess-output-v1",
                    schema_version=1,
                    redaction="environment_and_credential_markers_v1",
                    stdout="p2p output",
                    stderr="",
                    stdout_truncated=False,
                    stderr_truncated=False,
                ),
            ),
        ),
        failures=()
        if f2p_passed
        else ("upstream test openra-pr21453-hidden-ready-building-f2p exited 1",),
        isolation="apfs_cow_clone",
        duration_ms=25.0,
    )


def test_openra_revalidation_adapter_projects_the_real_registered_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = tmp_path / "harness"
    evidence = tmp_path / "evidence"
    fixed = tmp_path / "fixed"
    buggy = tmp_path / "buggy"
    for directory in (harness, evidence, fixed, buggy):
        directory.mkdir()
    pair = object()
    pair_calls: list[dict[str, object]] = []
    gate_calls: list[dict[str, object]] = []
    fixed_upstream = _subset("fixed")
    buggy_upstream = _subset("buggy")
    gate_result: ConstructionGateResult = evaluate_openra_pr21453_gate(
        fixed_tests={row.id: row.passed for row in fixed_upstream.results},
        buggy_tests={row.id: row.passed for row in buggy_upstream.results},
        goldens=_goldens(),
        fixed_start=_semantic(),
        buggy_start=_semantic(),
        fixed_goal=_semantic(),
        buggy_signature=_semantic(),
        fixed_signature=_semantic(status="goal_not_reached", primary_ok=False),
        provenance_verified=True,
    )

    def load_pair(**kwargs):
        pair_calls.append(kwargs)
        return pair

    def run_gate(received_pair, **kwargs):
        gate_calls.append({"pair": received_pair, **kwargs})
        return SimpleNamespace(
            result=gate_result,
            fixed_upstream=fixed_upstream,
            buggy_upstream=buggy_upstream,
            report_path=kwargs["report_path"],
        )

    monkeypatch.setattr(
        openra_gate,
        "_load_openra_pr21453_revalidation_pair",
        load_pair,
    )
    monkeypatch.setattr(
        openra_gate,
        "run_openra_pr21453_construction_gate",
        run_gate,
    )

    report = run_openra_pr21453_revalidation_matrix(
        task_id="lz-openra-21453-hotkey-ready-building",
        evidence_root=evidence,
        fixed_root=fixed,
        buggy_root=buggy,
        sandbox_root=tmp_path / "sandbox",
        harness_root=harness,
        total_timeout_s=123.0,
    )

    assert pair_calls == [
        {
            "fixed_root": fixed.resolve(),
            "buggy_root": buggy.resolve(),
            "harness_root": harness.resolve(),
        }
    ]
    assert gate_calls[0]["pair"] is pair
    assert gate_calls[0]["repository_root"] == harness.resolve()
    assert gate_calls[0]["upstream_record_path"] == evidence.resolve() / "upstream.json"
    assert gate_calls[0]["sandbox_root"] == (tmp_path / "sandbox").resolve()
    assert report["complete"] is True
    assert report["qualified"] is True
    assert report["fixed_tests"] == {
        "openra-pr21453-hidden-ready-building-f2p": True,
        "openra-action-queue-p2p": True,
    }
    assert report["buggy_tests"] == {
        "openra-pr21453-hidden-ready-building-f2p": False,
        "openra-action-queue-p2p": True,
    }
    assert report["registered_gate"]["adapter_id"] == "openra-pr21453-gui-gate-v1"
    assert report["registered_gate"]["passed"] is True
    assert len(report["executions"]) == 2


def test_openra_revalidation_adapter_keeps_semantic_gate_failure_nonqualifying(
    tmp_path: Path,
) -> None:
    fixed_upstream = _subset("fixed")
    buggy_upstream = _subset("buggy")
    semantic_failure = evaluate_openra_pr21453_gate(
        fixed_tests={row.id: row.passed for row in fixed_upstream.results},
        buggy_tests={row.id: row.passed for row in buggy_upstream.results},
        goldens=_goldens(),
        fixed_start=_semantic(),
        buggy_start=_semantic(),
        fixed_goal=_semantic(),
        buggy_signature=_semantic(),
        fixed_signature=_semantic(primary_ok=True),
        provenance_verified=True,
    )

    report = openra_gate._openra_pr21453_revalidation_report(
        SimpleNamespace(
            result=semantic_failure,
            fixed_upstream=fixed_upstream,
            buggy_upstream=buggy_upstream,
            report_path=tmp_path / "gate.json",
        )
    )

    assert report["complete"] is True
    assert report["qualified"] is False
    assert "fixed_defect_signature" in " ".join(report["failures"])
