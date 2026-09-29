"""The per-cell acceptance contract shared by every C1-Lazarus orchestrator.

A trial report is accepted only if the coordinator can rebuild its public verdict
from raw evidence.  Both the Phase-0 pilot and the formal matrix enforce exactly
this contract, so it lives here rather than beside either of them: a formal run
must never accept a cell the pilot would have rejected.

The functions take the dispatched cell's fields rather than an orchestrator's
context object, which keeps this module free of any import back into a caller.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .lazarus_grading import (
    LazarusTrialEvidence,
    VisualJudgement,
    grade_lazarus_trial,
)
from .lazarus_orchestrator import LazarusTrialKey
from .lazarus_trial import lazarus_p2p_checkpoint_restore_is_valid


def validate_lazarus_cell_report(
    report: Mapping[str, Any],
    *,
    key: LazarusTrialKey,
    defect_locus: str,
    version_switch_ms: float,
    provenance_sha256: str,
    per_trial_p2p_ids: Sequence[str],
    observer_model: str,
    p2p_timeout_s: float,
) -> str | None:
    """Return the first reason this cell cannot be accepted, or ``None``.

    The executor is permitted to run a provider-backed runtime, but never to
    self-report the outcome: :func:`validate_independent_grading_evidence`
    reruns the pure grader over the evidence the report carries and refuses any
    disagreement.
    """

    identity = {
        "task_id": key.task_id,
        "arm": key.arm,
        "model": key.model,
        "seed": key.seed,
        "version": key.version,
    }
    for name, expected in identity.items():
        if report.get(name) != expected:
            return f"report {name} does not match the dispatched cell"
    if report.get("defect_locus") != defect_locus:
        return "report defect_locus drift"
    switch_ms = report.get("version_switch_ms")
    if (
        isinstance(switch_ms, bool)
        or not isinstance(switch_ms, (int, float))
        or not math.isfinite(float(switch_ms))
        or float(switch_ms) != float(version_switch_ms)
    ):
        return "report version_switch_ms drift"
    if report.get("complete") is not True:
        return "trial evidence is incomplete"
    if key.arm == "lazarus_agent_s":
        runtime = report.get("runtime")
        trace = runtime.get("trace") if isinstance(runtime, Mapping) else None
        observer = trace.get("observer") if isinstance(trace, Mapping) else None
        expected_observer = {
            "model": observer_model,
            "usage": "registered_not_consumed_native_screenshot",
            "llm_calls": 0,
        }
        if observer != expected_observer:
            return "Agent-S observer evidence drift"
    grading_issue = validate_independent_grading_evidence(report)
    if grading_issue is not None:
        return grading_issue
    if report.get("provenance_valid") is not True:
        return "provenance is invalid"
    if report.get("provenance_sha256") != provenance_sha256:
        return "provenance SHA drift"
    for field in ("start_success", "goal_success", "defect_exposed", "invariants_ok"):
        if type(report.get(field)) is not bool:
            return f"{field} is missing"
    if report.get("start_success") is False:
        issue = validate_start_failed_p2p_evidence(
            report, task_id=key.task_id, version=key.version, p2p_timeout_s=p2p_timeout_s
        )
        if issue is not None:
            return issue
    else:
        if report.get("per_trial_p2p_ok") is not True:
            return "per-trial P2P failed"
        issue = validate_per_trial_p2p_evidence(
            report,
            task_id=key.task_id,
            version=key.version,
            arm=key.arm,
            per_trial_p2p_ids=per_trial_p2p_ids,
            p2p_timeout_s=p2p_timeout_s,
        )
        if issue is not None:
            return issue
        if (
            key.task_id == "lz-luanti-11919-vector-from-string"
            and key.arm == "lazarus_agent_s"
        ):
            snapshot_issue = validate_luanti_agent_s_snapshot_binding(report)
            if snapshot_issue is not None:
                return snapshot_issue
    if key.version == "fixed" and (
        report.get("false_alarm") is True or report.get("defect_exposed") is True
    ):
        return "fixed-version false alarm"
    return None


def validate_luanti_agent_s_snapshot_binding(
    report: Mapping[str, Any],
) -> str | None:
    """Bind the P2P restore to the exact Start snapshot Agent-S reached."""

    runtime = report.get("runtime")
    trace = runtime.get("trace") if isinstance(runtime, Mapping) else None
    execution = trace.get("execution") if isinstance(trace, Mapping) else None
    expected = (
        execution.get("start_snapshot_sha256")
        if isinstance(execution, Mapping)
        else None
    )
    p2p = report.get("per_trial_p2p_evidence")
    restore = p2p.get("checkpoint_restore") if isinstance(p2p, Mapping) else None
    state = restore.get("state_restore") if isinstance(restore, Mapping) else None
    observed = (
        state.get("start_snapshot_sha256") if isinstance(state, Mapping) else None
    )
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or observed != expected
    ):
        return "Agent-S Start snapshot provenance drift"
    return None


def validate_independent_grading_evidence(
    report: Mapping[str, Any],
) -> str | None:
    """Recompute the public verdict fields from trusted raw grader evidence."""

    grading = report.get("grading")
    if not isinstance(grading, Mapping) or grading.get("schema_version") != 1:
        return "independent grading evidence is missing"
    evidence = grading.get("evidence")
    result = grading.get("result")
    if not isinstance(evidence, Mapping) or not isinstance(result, Mapping):
        return "independent grading evidence is incomplete"
    try:
        expected = grade_lazarus_trial(
            LazarusTrialEvidence(
                version=str(evidence.get("version") or ""),
                before_semantic_ok=evidence.get("before_semantic_ok"),
                before_visual_ok=evidence.get("before_visual_ok"),
                goal_checks_ok=evidence.get("goal_checks_ok"),
                defect_signature_ok=evidence.get("defect_signature_ok"),
                invariants_ok=evidence.get("invariants_ok"),
                fixed_after_visual=_visual_judgement_from_evidence(
                    evidence.get("fixed_after_visual")
                ),
                buggy_after_visual=_visual_judgement_from_evidence(
                    evidence.get("buggy_after_visual")
                ),
                per_trial_p2p_ok=evidence.get("per_trial_p2p_ok"),
            )
        )
    except (TypeError, ValueError) as error:
        return f"independent grading evidence is invalid: {error}"
    expected_fields = {
        "version": expected.version,
        "start_success": expected.start_success,
        "goal_success": expected.goal_success,
        "defect_exposed": expected.defect_exposed,
        "invariants_ok": expected.invariants_ok,
        "per_trial_p2p_ok": expected.per_trial_p2p_ok,
        "false_alarm": expected.false_alarm,
        "complete": expected.complete,
        "after_statuses": expected.after_statuses,
        "failures": list(expected.failures),
    }
    for name, expected_value in expected_fields.items():
        if result.get(name) != expected_value:
            return f"independent grading result {name} drift"
        if name in report and report.get(name) != expected_value:
            return f"report {name} does not match independent grading"
    return None


def validate_per_trial_p2p_evidence(
    report: Mapping[str, Any],
    *,
    task_id: str,
    version: str,
    arm: str,
    per_trial_p2p_ids: Sequence[str],
    p2p_timeout_s: float,
) -> str | None:
    """Require one fresh, bounded P2P result for every registered test id."""

    expected_ids = tuple(str(value) for value in per_trial_p2p_ids)
    if not expected_ids:
        return "per-trial P2P registration is missing"
    evidence = report.get("per_trial_p2p_evidence")
    if not isinstance(evidence, Mapping) or evidence.get("schema_version") != 1:
        return "per-trial P2P evidence is missing"
    if (
        evidence.get("task_id") != task_id
        or evidence.get("version") != version
        or evidence.get("completed") is not True
    ):
        return "per-trial P2P evidence identity drift"
    timeout_s = evidence.get("timeout_s")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or float(timeout_s) != float(p2p_timeout_s)
    ):
        return "per-trial P2P timeout drift"
    duration_ms = evidence.get("duration_ms")
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, (int, float))
        or not math.isfinite(float(duration_ms))
        or float(duration_ms) < 0
    ):
        return "per-trial P2P duration is invalid"
    checkpoint_sha256 = report.get("checkpoint_sha256")
    if arm in {"lazarus_state", "lazarus_agent_s_synth", "lazarus_scalecua_synth"}:
        checkpoint_kind = "semantic_state"
    elif arm == "lazarus_scalecua":
        checkpoint_kind = "scalecua_from_start"
    else:
        checkpoint_kind = "agent_s_from_start"
    if not lazarus_p2p_checkpoint_restore_is_valid(
        evidence.get("checkpoint_restore"),
        checkpoint_sha256=(
            checkpoint_sha256 if isinstance(checkpoint_sha256, str) else None
        ),
        checkpoint_kind=checkpoint_kind,
    ):
        return "per-trial P2P checkpoint restore evidence is invalid"
    rows = evidence.get("results")
    if not isinstance(rows, list):
        return "per-trial P2P results are missing"
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            return "per-trial P2P result is malformed"
        test_id = str(row["id"])
        if test_id in by_id:
            return "per-trial P2P result is duplicated"
        by_id[test_id] = row
    if set(by_id) != set(expected_ids):
        return "per-trial P2P result ids drift"
    for test_id in expected_ids:
        row = by_id[test_id]
        duration_ms = row.get("duration_ms")
        if (
            row.get("passed") is not True
            or row.get("timed_out") is not False
            or isinstance(duration_ms, bool)
            or not isinstance(duration_ms, (int, float))
            or not math.isfinite(float(duration_ms))
            or float(duration_ms) < 0
            or float(duration_ms) > float(p2p_timeout_s) * 1000
        ):
            return f"per-trial P2P did not pass for {test_id}"
    return None


def validate_start_failed_p2p_evidence(
    report: Mapping[str, Any],
    *,
    task_id: str,
    version: str,
    p2p_timeout_s: float,
) -> str | None:
    """Validate the single registered no-P2P outcome for a measured Start failure."""

    del p2p_timeout_s  # the not-executed receipt is timeout independent
    if report.get("per_trial_p2p_ok") is not False:
        return "Start-failed trial has an invalid P2P status"
    evidence = report.get("per_trial_p2p_evidence")
    if not isinstance(evidence, Mapping):
        return "Start-failed trial P2P evidence is missing"
    if (
        evidence.get("schema_version") != 1
        or evidence.get("task_id") != task_id
        or evidence.get("version") != version
        or evidence.get("completed") is not False
        or evidence.get("passed") is not False
        or evidence.get("isolation") != "not_executed_start_failed"
        or evidence.get("results") != []
        or evidence.get("failures") != ["not_executed_start_failed"]
    ):
        return "Start-failed trial P2P evidence drift"
    grading = report.get("grading")
    result = grading.get("result") if isinstance(grading, Mapping) else None
    if not isinstance(result, Mapping) or result.get("after_statuses") != {
        "goal": "not_executed_start_failed",
        "defect": "not_executed_start_failed",
    }:
        return "Start-failed trial after statuses drift"
    return None


def observed_p2p_p95_s(
    reports: Sequence[Mapping[str, Any]],
) -> tuple[float | None, str | None]:
    """Measure the completed clone/restore/subset wall-clock budget.

    A Start-failed cell has already been independently validated as a deliberate
    ``not_executed_start_failed`` outcome, so it is excluded rather than being
    misrepresented as a zero-duration P2P execution.
    """

    durations_s: list[float] = []
    for report in reports:
        if report.get("start_success") is False:
            continue
        if report.get("start_success") is not True:
            return None, "observed a trial with an invalid Start status"
        evidence = report.get("per_trial_p2p_evidence")
        if not isinstance(evidence, Mapping):
            return None, "observed per-trial P2P duration evidence is missing"
        duration_ms = evidence.get("duration_ms")
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, (int, float))
            or not math.isfinite(float(duration_ms))
            or float(duration_ms) < 0
        ):
            return None, "observed per-trial P2P duration evidence is invalid"
        durations_s.append(float(duration_ms) / 1000.0)
    return nearest_rank_p95(durations_s), None


def nearest_rank_p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def _visual_judgement_from_evidence(value: Any) -> VisualJudgement:
    if not isinstance(value, Mapping):
        raise ValueError("visual judgement is missing")
    return VisualJudgement(
        ok=value.get("ok"),
        screenshot_sha256=value.get("screenshot_sha256"),
        oracle_sha256=value.get("oracle_sha256"),
    )


__all__ = [
    "nearest_rank_p95",
    "observed_p2p_p95_s",
    "validate_independent_grading_evidence",
    "validate_lazarus_cell_report",
    "validate_luanti_agent_s_snapshot_binding",
    "validate_per_trial_p2p_evidence",
    "validate_start_failed_p2p_evidence",
]
