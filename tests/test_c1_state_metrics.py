from __future__ import annotations

from warptest.c1_state_metrics import (
    explicit_state_stage_metric_issues,
    initialize_state_stage_metrics,
)


def _metrics() -> dict[str, object]:
    result: dict[str, object] = {"result": False}
    initialize_state_stage_metrics(result)
    return result


def test_explicit_state_metrics_reject_goal_after_failed_start() -> None:
    result = _metrics()
    result.update(
        goal_evaluation_status="evaluated",
        after_semantic_status="success",
        after_visual_status="evaluated",
        after_semantic_ok=True,
        after_visual_ok=True,
        goal_success=True,
    )

    assert "State Goal evaluation requires passed Start gate" in (
        explicit_state_stage_metric_issues(result)
    )


def test_explicit_state_metrics_allow_goal_after_passed_start() -> None:
    result = _metrics()
    result.update(
        start_grading_started=True,
        visual_grading_started=True,
        start_gate_status="passed",
        goal_evaluation_status="evaluated",
        before_semantic_status="success",
        before_visual_status="evaluated",
        after_semantic_status="success",
        after_visual_status="evaluated",
        before_semantic_ok=True,
        before_visual_ok=True,
        after_semantic_ok=True,
        after_visual_ok=True,
        start_success=True,
        goal_success=True,
        e2e_success=True,
        result=True,
    )

    assert explicit_state_stage_metric_issues(result) == []
