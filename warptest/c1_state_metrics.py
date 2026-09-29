"""Stage-metric contract and legacy reconstruction for C1 arm E.

New ``c1_state`` reports emit explicit Start/Goal/E2E fields.  Older reports
only retained the terminal pair result, even though the visual judge recorded
separate ``before`` and ``after`` labels.  This module is deliberately free of
runtime dependencies so the producer, aggregator, and log viewer share one
interpretation instead of silently drifting apart again.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


STATE_STAGE_METRICS_SCHEMA_VERSION = 1

_STAGE_BOOL_FIELDS = (
    "before_semantic_ok",
    "before_visual_ok",
    "after_semantic_ok",
    "after_visual_ok",
    "start_success",
    "goal_success",
    "e2e_success",
)
_STAGE_STATUS_FIELDS = (
    "start_gate_status",
    "goal_evaluation_status",
    "before_semantic_status",
    "before_visual_status",
    "after_semantic_status",
    "after_visual_status",
)


def initialize_state_stage_metrics(result: dict[str, Any]) -> None:
    """Install the explicit fail-closed stage contract on a new State result."""

    result.update(
        {
            "state_stage_metrics_schema_version": STATE_STAGE_METRICS_SCHEMA_VERSION,
            "start_grading_started": False,
            "visual_grading_started": False,
            "start_gate_status": "not_evaluated",
            "goal_evaluation_status": "not_evaluated",
            "before_semantic_status": "not_evaluated",
            "before_visual_status": "not_evaluated",
            "after_semantic_status": "not_evaluated",
            "after_visual_status": "not_evaluated",
            "before_semantic_ok": False,
            "before_visual_ok": False,
            "after_semantic_ok": False,
            "after_visual_ok": False,
            "start_success": False,
            "goal_success": False,
            "e2e_success": False,
        }
    )


def explicit_state_stage_metric_issues(result: Mapping[str, Any]) -> list[str]:
    """Validate the producer-side v1 State stage contract."""

    if (
        result.get("state_stage_metrics_schema_version")
        != STATE_STAGE_METRICS_SCHEMA_VERSION
    ):
        return ["State stage metrics schema_version must be 1"]

    issues: list[str] = []
    missing_bools = [field for field in _STAGE_BOOL_FIELDS if type(result.get(field)) is not bool]
    if missing_bools:
        issues.append("State stage metrics missing booleans: " + ",".join(missing_bools))
    missing_statuses = [
        field
        for field in _STAGE_STATUS_FIELDS
        if not isinstance(result.get(field), str) or not str(result.get(field)).strip()
    ]
    if missing_statuses:
        issues.append("State stage metrics missing statuses: " + ",".join(missing_statuses))

    if not missing_bools:
        expected_start = bool(
            result["before_semantic_ok"] and result["before_visual_ok"]
        )
        expected_goal = bool(
            result["after_semantic_ok"] and result["after_visual_ok"]
        )
        expected_e2e = bool(expected_start and expected_goal)
        if result["start_success"] is not expected_start:
            issues.append("State start_success is not semantic/visual conjunction")
        if result["goal_success"] is not expected_goal:
            issues.append("State goal_success is not semantic/visual conjunction")
        if result["e2e_success"] is not expected_e2e:
            issues.append("State e2e_success is not Start/Goal conjunction")
        if type(result.get("result")) is bool and result["result"] is not expected_e2e:
            issues.append("State terminal result disagrees with e2e_success")

    start_gate_status = result.get("start_gate_status")
    if start_gate_status == "passed" and result.get("start_success") is not True:
        issues.append("State passed Start gate lacks start_success")
    if result.get("start_success") is True and start_gate_status != "passed":
        issues.append("State start_success lacks passed Start gate")

    goal_status = result.get("goal_evaluation_status")
    goal_attempted = bool(
        goal_status in {"evaluated", "incomplete"}
        or result.get("after_semantic_status") != "not_evaluated"
        or result.get("after_visual_status") != "not_evaluated"
    )
    if goal_attempted and result.get("start_success") is not True:
        issues.append("State Goal evaluation requires passed Start gate")
    if goal_status == "not_evaluated_start_failed":
        if result.get("start_success") is not False:
            issues.append("State skipped Goal requires failed Start")
        if result.get("after_semantic_status") != "not_evaluated":
            issues.append("State skipped Goal evaluated after semantic")
        if result.get("after_visual_status") != "not_evaluated":
            issues.append("State skipped Goal evaluated after visual")
    return issues


def _last_iteration(result: Mapping[str, Any]) -> Mapping[str, Any]:
    iterations = result.get("iterations")
    if not isinstance(iterations, list):
        return {}
    for candidate in reversed(iterations):
        if isinstance(candidate, Mapping):
            return candidate
    return {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _explicit_bool(
    result: Mapping[str, Any], iteration: Mapping[str, Any], field: str
) -> bool | None:
    value = result.get(field)
    if type(value) is bool:
        return value
    value = iteration.get(field)
    return value if type(value) is bool else None


def _semantic_flag(
    result: Mapping[str, Any],
    iteration: Mapping[str, Any],
    *,
    explicit_field: str,
    status_field: str,
    iteration_status_field: str,
    detail_field: str,
) -> bool | None:
    explicit = _explicit_bool(result, iteration, explicit_field)
    if explicit is not None:
        return explicit

    status = result.get(status_field)
    if not isinstance(status, str):
        status = iteration.get(status_field)
    if isinstance(status, str):
        if status == "success":
            return True
        if status in {"failure", "failed", "validate_failed"}:
            return False

    runtime_status = iteration.get(iteration_status_field)
    if isinstance(runtime_status, str):
        if runtime_status == "success":
            return True
        if runtime_status in {"failure", "failed", "validate_failed"}:
            return False

    detail = result.get(detail_field)
    if not isinstance(detail, Mapping):
        detail = iteration.get(detail_field)
    if isinstance(detail, Mapping):
        for field in ("ok", "passed"):
            value = detail.get(field)
            if type(value) is bool:
                return value
        detail_status = detail.get("status")
        if detail_status == "success":
            return True
        if detail_status in {"failure", "failed", "validate_failed"}:
            return False
    return None


def _pair_verdict(
    result: Mapping[str, Any], iteration: Mapping[str, Any]
) -> Mapping[str, Any]:
    verdict = result.get("pair_verdict")
    if not isinstance(verdict, Mapping):
        verdict = iteration.get("pair_verdict")
    return _mapping(verdict)


def _visual_flag(verdict: Mapping[str, Any], stage: str) -> bool | None:
    side = verdict.get(stage)
    if not isinstance(side, Mapping):
        return None
    label = side.get("label")
    if label == "correct":
        return True
    if label == "incorrect":
        return False
    return None


def derive_state_stage_metrics(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return explicit or conservatively reconstructed State stage metrics.

    Legacy reports are never allowed to turn an unjudged screenshot into a
    pass or failure.  Their terminal ``result`` remains authoritative for E2E;
    Start/Goal stay ``None`` unless stage evidence exists.  A legacy pair
    verdict implies the runtime/semantic prechecks passed because every old
    State backend called the pair judge only after those checks.
    """

    iteration = _last_iteration(result)
    verdict = _pair_verdict(result, iteration)

    before_semantic_ok = _semantic_flag(
        result,
        iteration,
        explicit_field="before_semantic_ok",
        status_field="before_semantic_status",
        iteration_status_field="restore_status",
        detail_field="semantic_start",
    )
    after_semantic_ok = _semantic_flag(
        result,
        iteration,
        explicit_field="after_semantic_ok",
        status_field="after_semantic_status",
        iteration_status_field="after_status",
        detail_field="semantic_goal",
    )

    before_visual_ok = _explicit_bool(result, iteration, "before_visual_ok")
    if before_visual_ok is None:
        before_visual_ok = _visual_flag(verdict, "before")
    after_visual_ok = _explicit_bool(result, iteration, "after_visual_ok")
    if after_visual_ok is None:
        after_visual_ok = _visual_flag(verdict, "after")

    start_success = _explicit_bool(result, iteration, "start_success")
    if start_success is None:
        if before_visual_ok is not None:
            start_success = bool(
                before_visual_ok and before_semantic_ok is not False
            )
        elif before_semantic_ok is False:
            start_success = False

    goal_success = _explicit_bool(result, iteration, "goal_success")
    if goal_success is None:
        if after_visual_ok is not None:
            goal_success = bool(after_visual_ok and after_semantic_ok is not False)
        elif after_semantic_ok is False:
            goal_success = False

    e2e_success = _explicit_bool(result, iteration, "e2e_success")
    if e2e_success is None:
        terminal = result.get("result")
        e2e_success = terminal if type(terminal) is bool else None

    explicit_contract = (
        result.get("state_stage_metrics_schema_version")
        == STATE_STAGE_METRICS_SCHEMA_VERSION
    )
    if explicit_contract:
        start_gate_status = result.get("start_gate_status")
        goal_evaluation_status = result.get("goal_evaluation_status")
        start_evaluated = bool(
            isinstance(start_gate_status, str)
            and not start_gate_status.startswith("not_evaluated")
        )
        goal_evaluated = goal_evaluation_status in {"evaluated", "incomplete"}
    else:
        # Legacy reports did not carry evaluation-status fields.  A stage is
        # auditable only when its success value can actually be reconstructed.
        start_evaluated = start_success is not None
        goal_evaluated = goal_success is not None
    return {
        "state_stage_metrics_schema_version": result.get(
            "state_stage_metrics_schema_version"
        ),
        "stage_metrics_source": (
            "explicit_v1"
            if explicit_contract
            else "legacy_pair_verdict"
            if verdict
            else "legacy_terminal_only"
        ),
        "stage_metrics_complete": bool(
            explicit_contract and not explicit_state_stage_metric_issues(result)
        ),
        "before_semantic_ok": before_semantic_ok,
        "before_visual_ok": before_visual_ok,
        "after_semantic_ok": after_semantic_ok,
        "after_visual_ok": after_visual_ok,
        "start_success": start_success,
        "goal_success": goal_success,
        "e2e_success": e2e_success,
        "start_evaluated": start_evaluated,
        "goal_evaluated": goal_evaluated,
        "pair_verdict": dict(verdict) if verdict else None,
    }
