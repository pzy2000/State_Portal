from __future__ import annotations

from pathlib import Path

from warptest.c1_lazarus_selection import luanti_14072_fresh_gate57_v2 as runner
from warptest.c1_lazarus_selection.luanti_native_executor import (
    NativeProcessReceipt,
    completed_native_test_count,
)
from warptest.c1_lazarus_selection.luanti_native_regression import (
    NativeRegressionRun,
    profile_for_candidate,
)
from warptest.c1_lazarus_selection.models import CandidateKey


def _run(selector: str) -> NativeRegressionRun:
    return NativeRegressionRun(
        phase="p2p" if selector.endswith("testMovePartial") else "f2p",
        selector=selector,
        executable_path="bin/minetestserver",
        argv=("bin/minetestserver", "--run-unittests", "--test-module", selector),
        repetitions=3,
        fixed_expected_outcome="pass",
        buggy_expected_outcome=(
            "pass" if selector.endswith("testMovePartial") else "failure"
        ),
    )


def test_14072_profile_binds_both_methods() -> None:
    profile = profile_for_candidate(CandidateKey("luanti", 14072))
    assert profile.f2p_selector == "TestMoveAction.testMoveFillStack"
    assert profile.p2p_selector == "TestMoveAction.testMovePartial"
    assert profile.test_paths == ("src/unittest/test_moveaction.cpp",)
    assert profile.p2p_test_paths == profile.test_paths


def test_selector_adapter_filters_the_selected_method(monkeypatch) -> None:
    module_output = b"\n".join(
        [
            b"======== Testing module TestMoveAction",
            b"[PASS] testMove - 0ms",
            b"[FAIL] testMoveFillStack - 0ms",
            b"[PASS] testMovePartial - 0ms",
            b"======== Module TestMoveAction failed (1 failures / 8 tests) - 0ms",
            b"Unit Test Results: FAILED",
            b"    1 / 8 failed tests.",
        ]
    )
    monkeypatch.setattr(
        runner,
        "run_bounded_process",
        lambda argv, *, cwd, env, timeout_seconds: NativeProcessReceipt(
            "completed", 1, b"", module_output
        ),
    )
    p2p = runner._selector_process_runner(
        (
            "/tmp/minetestserver",
            "--run-unittests",
            "--test-module",
            "TestMoveAction.testMovePartial",
        ),
        cwd=Path("/tmp"),
        env={},
        timeout_seconds=30,
    )
    assert p2p.returncode == 0
    assert p2p.stderr.endswith(b"0 / 1 failed tests.\n")
    assert (
        completed_native_test_count(
            _run("TestMoveAction.testMovePartial"), p2p.outcome, p2p.stdout, p2p.stderr
        )
        == 1
    )

    f2p = runner._selector_process_runner(
        (
            "/tmp/minetestserver",
            "--run-unittests",
            "--test-module",
            "TestMoveAction.testMoveFillStack",
        ),
        cwd=Path("/tmp"),
        env={},
        timeout_seconds=30,
    )
    assert f2p.returncode == 1
    assert f2p.stderr.endswith(b"1 / 1 failed tests.\n")
    assert (
        completed_native_test_count(
            _run("TestMoveAction.testMoveFillStack"),
            f2p.outcome,
            f2p.stdout,
            f2p.stderr,
        )
        == 1
    )
