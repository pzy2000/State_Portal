from __future__ import annotations

import copy

import pytest

from warptest.c1_lazarus_selection.luanti_14555_admission import (
    CANDIDATE,
    F2P_ID,
    P2P_ID,
    TASK_ID,
    _validate_attempt_payload,
)


_PREPARE_IDS = (
    "luanti-unit-test-remove-stale-cache",
    "luanti-unit-test-remove-stale-cmake-files",
    "luanti-unit-test-reconfigure",
    "luanti-unit-test-rebuild",
)


def _result(test_id: str, *, passed: bool) -> dict[str, object]:
    return {
        "test_id": test_id,
        "passed": passed,
        "exit_code": 0 if passed else 1,
        "timed_out": False,
        "duration_ms": 1.0,
    }


def _execution(version: str) -> dict[str, object]:
    buggy = version == "buggy"
    return {
        "version": version,
        "prepare_results": [
            {
                "test_id": test_id,
                "passed": True,
                "exit_code": 0,
                "timed_out": False,
                "duration_ms": 1.0,
            }
            for test_id in _PREPARE_IDS
        ],
        "results": [
            _result(F2P_ID, passed=not buggy),
            _result(P2P_ID, passed=True),
        ],
        "failures": [] if not buggy else [f"upstream test {F2P_ID} exited 1"],
        "terminal_failure_class": None,
        "duration_ms": 10.0,
    }


def _attempt() -> dict[str, object]:
    return {
        "task_id": TASK_ID,
        "candidate": CANDIDATE.to_value(),
        "defect_locus": "at_goal",
        "repetition": 1,
        "outcome": "qualified",
        "complete": True,
        "fresh_revalidation": True,
        "historical_result_inherited": False,
        "fixed_tests": {F2P_ID: True, P2P_ID: True},
        "buggy_tests": {F2P_ID: False, P2P_ID: True},
        "failures": [],
        "executions": [_execution("fixed"), _execution("buggy")],
        "runner_error": None,
    }


def test_14555_transition_accepts_only_the_real_three_by_two_truth_table() -> None:
    _validate_attempt_payload(_attempt(), 1)

    tampered = copy.deepcopy(_attempt())
    tampered["buggy_tests"][F2P_ID] = True
    with pytest.raises(RuntimeError, match="verdict is invalid"):
        _validate_attempt_payload(tampered, 1)


def test_14555_transition_rejects_skipped_frozen_test_rebuild() -> None:
    tampered = copy.deepcopy(_attempt())
    tampered["executions"][1]["prepare_results"].pop()

    with pytest.raises(RuntimeError, match="rebuild was incomplete"):
        _validate_attempt_payload(tampered, 1)


def test_14555_transition_rejects_timeout_disguised_as_expected_failure() -> None:
    tampered = copy.deepcopy(_attempt())
    buggy_f2p = tampered["executions"][1]["results"][0]
    buggy_f2p["timed_out"] = True
    buggy_f2p["exit_code"] = None

    with pytest.raises(RuntimeError, match="truth table is invalid"):
        _validate_attempt_payload(tampered, 1)
