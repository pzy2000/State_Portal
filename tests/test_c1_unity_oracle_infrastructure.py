from __future__ import annotations

from pathlib import Path

import pytest

from warptest import gui_agent, state_loop


def _runtime_failure_pair() -> dict[str, object]:
    return {
        "status": "failure",
        "before": {
            "status": "failure",
            "exit_code": 199,
            "utility_report": {
                "status": "failure",
                "detail": "Pal3 WarpTest command did not write a report.",
            },
            "live_source": "capture_failure",
        },
        "after": {"status": "success", "exit_code": 0},
    }


def test_unity_exit_without_report_is_typed_infrastructure() -> None:
    pair = _runtime_failure_pair()

    assert state_loop._unity_oracle_pair_infrastructure_failure(pair) is True
    error = state_loop.UnityOracleInfrastructureError(
        f"Unity oracle spec failed: {pair!r}"
    )
    result = {
        "incomplete": True,
        "policy_phase_started": False,
        "infrastructure_error": gui_agent._is_infrastructure_error(error),
        "reason": f"Unity oracle golden generation failed: {error}",
    }

    assert result["infrastructure_error"] is True
    assert gui_agent._gui_trial_retryable_incomplete(result) is True
    result["retryable_incomplete"] = True
    assert gui_agent._gui_retry_metadata(result)[0] == "infra"


@pytest.mark.parametrize(
    "failure",
    [
        {
            "status": "failure",
            "exit_code": 199,
            "utility_report": {"detail": "Registered assertion did not match."},
            "live_source": "unity_capture",
        },
        {
            "status": "failure",
            "exit_code": 0,
            "utility_report": {"detail": "Unity did not write a report."},
            "live_source": "unity_capture",
        },
        {
            "status": "failure",
            "exit_code": 0,
            "utility_report": {"detail": "Capture did not complete."},
            "live_source": "capture_failure",
        },
    ],
    ids=("nonzero-exit", "missing-report", "capture-failure"),
)
def test_unity_runtime_failure_signals_are_independently_retryable(
    failure: dict[str, object],
) -> None:
    pair = {
        "status": "failure",
        "before": failure,
        "after": {"status": "success", "exit_code": 0},
    }

    assert state_loop._unity_oracle_pair_infrastructure_failure(pair) is True


def test_unity_golden_builder_raises_typed_runtime_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair = _runtime_failure_pair()
    monkeypatch.setattr(state_loop, "_repo_git_commit", lambda _repo: "")
    monkeypatch.setattr(
        state_loop,
        "_cached_oracle_goldens",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        state_loop,
        "_run_unity_checkpoint_pair",
        lambda *_args, **_kwargs: (pair, None, None, 1, 1),
    )

    with pytest.raises(state_loop.UnityOracleInfrastructureError):
        state_loop._ensure_unity_checkpoint_goldens(
            tmp_path,
            {"checkpoint": {}, "actions": [], "assertions": []},
            tmp_path / "oracle.yaml",
            tmp_path / "report",
            benchmark="pal3",
            headless=False,
        )


def test_unity_golden_builder_types_missing_screenshot_as_infrastructure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair = {
        "status": "success",
        "before": {"status": "success", "exit_code": 0},
        "after": {"status": "success", "exit_code": 0},
    }
    monkeypatch.setattr(state_loop, "_repo_git_commit", lambda _repo: "")
    monkeypatch.setattr(
        state_loop,
        "_cached_oracle_goldens",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        state_loop,
        "_run_unity_checkpoint_pair",
        lambda *_args, **_kwargs: (pair, None, None, 1, 1),
    )

    with pytest.raises(
        state_loop.UnityOracleInfrastructureError,
        match="before-state screenshot is missing or non-informative",
    ):
        state_loop._ensure_unity_checkpoint_goldens(
            tmp_path,
            {"checkpoint": {}, "actions": [], "assertions": []},
            tmp_path / "oracle.yaml",
            tmp_path / "report",
            benchmark="pal3",
            headless=False,
        )


def test_state_loop_retries_typed_prepolicy_unity_failure() -> None:
    result = {
        "incomplete": True,
        "visual_grading_started": False,
        "infrastructure_error": True,
        "reason": "oracle golden generation failed: Unity runtime unavailable",
    }

    assert state_loop._state_trial_retryable_incomplete(result) is True
    result["retryable_incomplete"] = True
    assert state_loop._state_retry_metadata(result)[0] == "infra"


def test_semantic_unity_oracle_failure_remains_nonretryable() -> None:
    pair = {
        "status": "failure",
        "before": {
            "status": "failure",
            "exit_code": 0,
            "utility_report": {
                "status": "failure",
                "detail": "Registered assertion did not match.",
            },
            "live_source": "unity_capture",
        },
        "after": {"status": "success", "exit_code": 0},
    }

    assert state_loop._unity_oracle_pair_infrastructure_failure(pair) is False
    error = RuntimeError(f"Unity oracle spec failed: {pair!r}")
    gui_result = {
        "incomplete": True,
        "policy_phase_started": False,
        "infrastructure_error": gui_agent._is_infrastructure_error(error),
        "reason": f"Unity oracle golden generation failed: {error}",
    }
    state_result = {
        "incomplete": True,
        "visual_grading_started": False,
        "infrastructure_error": False,
        "reason": f"oracle golden generation failed: {error}",
    }

    assert gui_result["infrastructure_error"] is False
    assert gui_agent._gui_trial_retryable_incomplete(gui_result) is False
    assert state_loop._state_trial_retryable_incomplete(state_result) is False
