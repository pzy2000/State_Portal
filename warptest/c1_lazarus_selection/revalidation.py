"""Fresh fixed/buggy construction revalidation for curated tasks."""

from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from warptest.lazarus_formal_gate import run_registered_construction_test_matrix
from warptest.lazarus_upstream import validate_subprocess_output_capture

from .curated import load_curated_catalog
from .resource_guard import disk_capacity
from .stable_protocol import (
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_REQUIRED_REPETITIONS = 3
_ATTEMPT_KIND = "curated_construction_repetition"
_ATTEMPT_VERSION = "1.0"
_AGGREGATE_KIND = "curated_task_revalidation"
_AGGREGATE_VERSION = "2.0"
_TERMINAL_OUTCOMES = frozenset(
    {
        "qualified",
        "quality_rejected",
        "flake_observed",
        "infrastructure_blocked",
        "version_incompatible",
    }
)
_TERMINAL_FAILURE_CLASSES = frozenset({"version_incompatible"})


def run_curated_construction_revalidation(
    artifact_root: str | Path,
    *,
    task_id: str,
    catalog_path: str | Path,
    workspace_root: str | Path,
    curated_selection_artifact_id: str,
    authorization: AuthorizationRecord,
    total_timeout_s: float = 600.0,
    resume_existing: bool = True,
) -> StableArtifact:
    """Run three fresh F2P/P2P matrices and persist each bounded attempt.

    A single green construction table is only a diagnostic.  Qualification
    requires three independently isolated fixed/buggy tables; each table is
    written before the next starts so an interrupted batch resumes from its
    durable repetition boundary rather than silently reusing a historical
    conclusion.
    """

    if type(task_id) is not str or not task_id:
        raise ValueError("curated revalidation task ID is invalid")
    if type(total_timeout_s) is not float or not 0 < total_timeout_s <= 1_200:
        raise ValueError("curated revalidation timeout is invalid")
    workspace = _safe_directory(Path(workspace_root))
    tasks = load_curated_catalog(
        catalog_path,
        workspace_root=workspace,
    )
    matches = tuple(task for task in tasks if task.task_id == task_id)
    if len(matches) != 1:
        raise RuntimeError("task is not in the curated relaxed catalog")
    task = matches[0]
    selection = load_stable_artifact(
        artifact_root,
        artifact_id=curated_selection_artifact_id,
        artifact_kind="curated_selection",
        semantic_version="1.0",
    )
    selected_rows = selection.payload.get("tasks")
    if type(selected_rows) is not list:
        raise RuntimeError("curated selection task list is invalid")
    selected = [row for row in selected_rows if row.get("task_id") == task_id]
    if len(selected) != 1 or selected[0].get("candidate") != {
        "repository_key": task.repository_key,
        "change_number": task.change_number,
    }:
        raise RuntimeError("curated selection does not bind the requested task")

    artifact_id = _aggregate_id(task)
    if resume_existing:
        existing = _load_if_present(
            artifact_root,
            artifact_id=artifact_id,
            artifact_kind=_AGGREGATE_KIND,
            semantic_version=_AGGREGATE_VERSION,
        )
        if existing is not None:
            _validate_existing_aggregate(
                artifact_root,
                existing,
                selection,
                task,
            )
            return existing

    attempts: list[StableArtifact] = []
    for repetition in range(1, _REQUIRED_REPETITIONS + 1):
        attempt_id = _attempt_id(task, repetition)
        attempt = (
            _load_if_present(
                artifact_root,
                artifact_id=attempt_id,
                artifact_kind=_ATTEMPT_KIND,
                semantic_version=_ATTEMPT_VERSION,
            )
            if resume_existing
            else None
        )
        if attempt is not None:
            _validate_existing_attempt(attempt, selection, task, repetition)
            attempts.append(attempt)
            if _stops_revalidation(attempt.payload["outcome"]):
                break
            continue

        capacity = disk_capacity(artifact_root)
        if not capacity.may_start_candidate:
            raise RuntimeError("disk guard prohibits a new curated revalidation")
        attempt_payload = _run_attempt(
            task,
            repetition=repetition,
            workspace=workspace,
            total_timeout_s=total_timeout_s,
        )
        write_stable_artifact(
            artifact_root,
            artifact_id=attempt_id,
            artifact_kind=_ATTEMPT_KIND,
            semantic_version=_ATTEMPT_VERSION,
            created_at=_now(),
            inputs=(selection.reference,),
            payload=attempt_payload,
            authorization=authorization,
        )
        attempt = load_stable_artifact(
            artifact_root,
            artifact_id=attempt_id,
            artifact_kind=_ATTEMPT_KIND,
            semantic_version=_ATTEMPT_VERSION,
        )
        if (
            attempt.inputs != (selection.reference,)
            or attempt.payload != attempt_payload
        ):
            raise RuntimeError("curated revalidation attempt reload drift")
        _validate_existing_attempt(attempt, selection, task, repetition)
        attempts.append(attempt)
        if _stops_revalidation(attempt.payload["outcome"]):
            break

    payload = _aggregate_payload(task, tuple(attempts))
    write_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind=_AGGREGATE_KIND,
        semantic_version=_AGGREGATE_VERSION,
        created_at=_now(),
        inputs=(selection.reference, *(attempt.reference for attempt in attempts)),
        payload=payload,
        authorization=authorization,
    )
    artifact = load_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind=_AGGREGATE_KIND,
        semantic_version=_AGGREGATE_VERSION,
    )
    if (
        artifact.inputs
        != (
            selection.reference,
            *(attempt.reference for attempt in attempts),
        )
        or artifact.payload != payload
    ):
        raise RuntimeError("curated revalidation reload drift")
    return artifact


def _run_attempt(
    task: Any,
    *,
    repetition: int,
    workspace: Path,
    total_timeout_s: float,
) -> dict[str, Any]:
    try:
        evidence = _safe_directory(
            workspace / "benchmarks/p5/lazarus/defects" / task.task_id
        )
        prebuilt = _safe_directory(
            workspace / ".warptest-cache/lazarus/prebuilt" / task.task_id
        )
        report = run_registered_construction_test_matrix(
            task_id=task.task_id,
            evidence_root=evidence,
            fixed_root=_safe_directory(prebuilt / "fixed"),
            buggy_root=_safe_directory(prebuilt / "buggy"),
            sandbox_root=_safe_directory(
                workspace / ".warptest-cache/lazarus/construction",
                create=True,
            ),
            harness_root=workspace,
            total_timeout_s=total_timeout_s,
        )
    except Exception as error:
        return _infrastructure_attempt_payload(task, repetition, error)
    return _attempt_payload(task, repetition, report)


def _attempt_payload(
    task: Any, repetition: int, report: dict[str, Any]
) -> dict[str, Any]:
    if type(repetition) is not int or not 1 <= repetition <= _REQUIRED_REPETITIONS:
        raise RuntimeError("curated revalidation repetition is invalid")
    if type(report) is not dict:
        raise RuntimeError("curated revalidation report is invalid")
    complete = report.get("complete") is True
    qualified = report.get("qualified") is True
    if qualified and not complete:
        raise RuntimeError("curated revalidation cannot qualify an incomplete matrix")
    allow_empty_test_table = not complete and not qualified
    fixed_tests = _test_outcomes(
        report.get("fixed_tests"),
        "fixed",
        allow_empty=allow_empty_test_table,
    )
    buggy_tests = _test_outcomes(
        report.get("buggy_tests"),
        "buggy",
        allow_empty=allow_empty_test_table,
    )
    if set(fixed_tests) != set(buggy_tests):
        raise RuntimeError("curated fixed and buggy test identities differ")
    failures = report.get("failures")
    if type(failures) is not list or any(type(value) is not str for value in failures):
        raise RuntimeError("curated revalidation failures are invalid")
    executions = report.get("executions")
    if type(executions) is not list or len(executions) != 2:
        raise RuntimeError("curated revalidation executions are invalid")
    projected_executions = [_execution_value(value) for value in executions]
    if {value["version"] for value in projected_executions} != {"fixed", "buggy"}:
        raise RuntimeError("curated revalidation versions are invalid")
    expected_test_ids = set(fixed_tests)
    for execution in projected_executions:
        observed_test_ids = {row["test_id"] for row in execution["results"]}
        if execution["terminal_failure_class"] is None:
            if observed_test_ids != expected_test_ids:
                raise RuntimeError("curated execution test identities are incomplete")
        elif observed_test_ids:
            raise RuntimeError("curated terminal execution has upstream results")
    if not expected_test_ids and not any(
        _execution_is_infrastructure(value) for value in projected_executions
    ):
        raise RuntimeError(
            "curated empty test table lacks a bounded infrastructure receipt"
        )
    terminal_failure_class = _terminal_failure_class(projected_executions)
    reported_terminal_failure_class = report.get("terminal_failure_class")
    if (
        reported_terminal_failure_class is not None
        and reported_terminal_failure_class not in _TERMINAL_FAILURE_CLASSES
    ):
        raise RuntimeError("curated revalidation terminal failure is invalid")
    if reported_terminal_failure_class != terminal_failure_class:
        raise RuntimeError("curated revalidation terminal failure drift")
    registered_gate = _registered_gate_value(
        report.get("registered_gate"),
        task_id=task.task_id,
        complete=complete,
        qualified=qualified,
        fixed_tests=fixed_tests,
        buggy_tests=buggy_tests,
    )
    infrastructure = not complete or any(
        _execution_is_infrastructure(value) for value in projected_executions
    )
    outcome = (
        "version_incompatible"
        if terminal_failure_class == "version_incompatible"
        else "infrastructure_blocked"
        if infrastructure
        else "qualified"
        if qualified
        else "quality_rejected"
    )
    payload = {
        "task_id": task.task_id,
        "candidate": {
            "repository_key": task.repository_key,
            "change_number": task.change_number,
        },
        "defect_locus": task.defect_locus,
        "repetition": repetition,
        "outcome": outcome,
        "complete": complete,
        "fresh_revalidation": True,
        "historical_result_inherited": False,
        "fixed_tests": fixed_tests,
        "buggy_tests": buggy_tests,
        "failures": list(failures),
        "executions": projected_executions,
        "runner_error": None,
    }
    if terminal_failure_class is not None:
        payload["terminal_failure_class"] = terminal_failure_class
    if registered_gate is not None:
        payload["registered_gate"] = registered_gate
    return payload


def _infrastructure_attempt_payload(
    task: Any,
    repetition: int,
    error: Exception,
) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "candidate": {
            "repository_key": task.repository_key,
            "change_number": task.change_number,
        },
        "defect_locus": task.defect_locus,
        "repetition": repetition,
        "outcome": "infrastructure_blocked",
        "complete": False,
        "fresh_revalidation": True,
        "historical_result_inherited": False,
        "fixed_tests": {},
        "buggy_tests": {},
        "failures": ["construction runner could not produce a bounded matrix"],
        "executions": [],
        "runner_error": type(error).__name__,
    }


def _test_outcomes(
    value: Any,
    version: str,
    *,
    allow_empty: bool = False,
) -> dict[str, bool]:
    if type(value) is not dict or (not value and not allow_empty):
        raise RuntimeError(f"curated {version} test outcomes are invalid")
    if any(
        type(key) is not str or type(result) is not bool
        for key, result in value.items()
    ):
        raise RuntimeError(f"curated {version} test outcomes are invalid")
    return dict(value)


def _execution_value(value: Any) -> dict[str, Any]:
    if type(value) is not dict or value.get("version") not in {"fixed", "buggy"}:
        raise RuntimeError("curated execution receipt is invalid")
    duration = value.get("duration_ms")
    if type(duration) not in {int, float} or duration < 0:
        raise RuntimeError("curated execution duration is invalid")
    terminal_failure_class = value.get("terminal_failure_class")
    if (
        terminal_failure_class is not None
        and terminal_failure_class not in _TERMINAL_FAILURE_CLASSES
    ):
        raise RuntimeError("curated execution terminal failure is invalid")
    results = value.get("results")
    if type(results) not in {list, tuple} or len(results) < 2:
        if not (
            type(results) in {list, tuple}
            and len(results) == 0
            and (terminal_failure_class is not None or value.get("failures"))
        ):
            raise RuntimeError("curated execution results are invalid")
    projected = [_result_value(row, label="upstream") for row in results]
    if len({row["test_id"] for row in projected}) != len(projected):
        raise RuntimeError("curated execution has duplicate upstream results")
    failures = value.get("failures", ())
    if type(failures) not in {list, tuple} or any(
        type(item) is not str or not item for item in failures
    ):
        raise RuntimeError("curated execution failures are invalid")
    prepare_results = value.get("prepare_results", ())
    if type(prepare_results) not in {list, tuple}:
        raise RuntimeError("curated construction preparation results are invalid")
    projected_prepare = [
        _result_value(row, label="preparation") for row in prepare_results
    ]
    if not projected and terminal_failure_class is None and not failures:
        raise RuntimeError("curated execution has no observed upstream result")
    return {
        "version": value["version"],
        "duration_ms": duration,
        "results": projected,
        "prepare_results": projected_prepare,
        "failures": list(failures),
        "terminal_failure_class": terminal_failure_class,
    }


def _result_value(value: Any, *, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise RuntimeError(f"curated {label} result is invalid")
    projected = {
        "test_id": value.get("id"),
        "passed": value.get("passed"),
        "exit_code": value.get("exit_code"),
        "timed_out": value.get("timed_out"),
        "duration_ms": value.get("duration_ms"),
    }
    if (
        type(projected["test_id"]) is not str
        or type(projected["passed"]) is not bool
        or projected["exit_code"] is not None
        and type(projected["exit_code"]) is not int
        or type(projected["timed_out"]) is not bool
        or type(projected["duration_ms"]) not in {int, float}
        or projected["duration_ms"] < 0
    ):
        raise RuntimeError(f"curated {label} result is invalid")
    try:
        projected["output_capture"] = validate_subprocess_output_capture(
            value.get("output_capture"),
            label=f"curated {label}",
        )
    except ValueError as error:
        raise RuntimeError(f"curated {label} output capture is invalid") from error
    return projected


def _registered_gate_value(
    value: Any,
    *,
    task_id: str,
    complete: bool,
    qualified: bool,
    fixed_tests: dict[str, bool],
    buggy_tests: dict[str, bool],
) -> dict[str, Any] | None:
    """Validate the compact receipt from a closed registered gate adapter."""

    if value is None:
        return None
    if type(value) is not dict:
        raise RuntimeError("curated registered gate receipt is invalid")
    expected = {
        "adapter_id",
        "task_id",
        "complete",
        "passed",
        "checks",
        "failures",
        "f2p_claim",
        "f2p_origin",
        "fail_to_pass",
        "pass_to_pass",
        "report_path",
    }
    if set(value) != expected:
        raise RuntimeError("curated registered gate receipt schema is invalid")
    adapter_id = value.get("adapter_id")
    if type(adapter_id) is not str or not adapter_id:
        raise RuntimeError("curated registered gate adapter is invalid")
    if (
        value.get("task_id") != task_id
        or value.get("complete") is not complete
        or value.get("passed") is not qualified
    ):
        raise RuntimeError("curated registered gate result drift")
    checks = value.get("checks")
    if (
        type(checks) is not dict
        or not checks
        or any(
            type(name) is not str or type(result) is not bool
            for name, result in checks.items()
        )
    ):
        raise RuntimeError("curated registered gate checks are invalid")
    failures = value.get("failures")
    if type(failures) is not list or any(type(item) is not str for item in failures):
        raise RuntimeError("curated registered gate failures are invalid")
    if value.get("f2p_claim") != "issue_derived_gui_differential_not_upstream":
        raise RuntimeError("curated registered gate F2P claim is invalid")
    if value.get("f2p_origin") != "issue_derived_gui_differential":
        raise RuntimeError("curated registered gate F2P origin is invalid")
    fail_to_pass = _registered_test_ids(value.get("fail_to_pass"), "F2P")
    pass_to_pass = _registered_test_ids(value.get("pass_to_pass"), "P2P")
    if set(fail_to_pass) | set(pass_to_pass) != set(fixed_tests):
        raise RuntimeError("curated registered gate test table is incomplete")
    if set(fixed_tests) != set(buggy_tests):
        raise RuntimeError("curated registered gate test identities drift")
    expected_checks = {
        "fixed_f2p": all(fixed_tests[test_id] for test_id in fail_to_pass),
        "buggy_f2p": any(buggy_tests[test_id] for test_id in fail_to_pass),
        "fixed_p2p": all(fixed_tests[test_id] for test_id in pass_to_pass),
        "buggy_p2p": all(buggy_tests[test_id] for test_id in pass_to_pass),
    }
    if any(checks.get(name) is not result for name, result in expected_checks.items()):
        raise RuntimeError("curated registered gate test truth table drift")
    report_path = value.get("report_path")
    if type(report_path) is not str or not report_path:
        raise RuntimeError("curated registered gate report path is invalid")
    return {
        "adapter_id": adapter_id,
        "task_id": task_id,
        "complete": complete,
        "passed": qualified,
        "checks": dict(checks),
        "failures": list(failures),
        "f2p_claim": value["f2p_claim"],
        "f2p_origin": value["f2p_origin"],
        "fail_to_pass": list(fail_to_pass),
        "pass_to_pass": list(pass_to_pass),
        "report_path": report_path,
    }


def _registered_test_ids(value: Any, label: str) -> tuple[str, ...]:
    if (
        type(value) is not list
        or not value
        or any(type(item) is not str or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise RuntimeError(f"curated registered gate {label} IDs are invalid")
    return tuple(value)


def _execution_is_infrastructure(value: dict[str, Any]) -> bool:
    if not value["results"]:
        return True
    if any(
        row["timed_out"] is True or row["exit_code"] is None for row in value["results"]
    ):
        return True
    return any(
        row["timed_out"] is True
        or row["exit_code"] is None
        or row["passed"] is not True
        for row in value["prepare_results"]
    )


def _terminal_failure_class(executions: list[dict[str, Any]]) -> str | None:
    values = {
        value["terminal_failure_class"]
        for value in executions
        if value["terminal_failure_class"] is not None
    }
    if len(values) > 1:
        raise RuntimeError("curated execution terminal failures conflict")
    return next(iter(values), None)


def _stops_revalidation(outcome: str) -> bool:
    return outcome in {"infrastructure_blocked", "version_incompatible"}


def _aggregate_payload(
    task: Any,
    attempts: tuple[StableArtifact, ...],
) -> dict[str, Any]:
    if not 1 <= len(attempts) <= _REQUIRED_REPETITIONS:
        raise RuntimeError("curated revalidation repetition count is invalid")
    payloads = tuple(attempt.payload for attempt in attempts)
    if any(type(value) is not dict for value in payloads):
        raise RuntimeError("curated revalidation attempt payload is invalid")
    outcomes = tuple(value["outcome"] for value in payloads)
    if any(value not in _TERMINAL_OUTCOMES for value in outcomes):
        raise RuntimeError("curated revalidation attempt outcome is invalid")
    complete = len(payloads) == _REQUIRED_REPETITIONS and all(
        value["complete"] is True for value in payloads
    )
    tables = tuple(
        (
            tuple(sorted(value["fixed_tests"].items())),
            tuple(sorted(value["buggy_tests"].items())),
        )
        for value in payloads
    )
    if "version_incompatible" in outcomes:
        outcome = "version_incompatible"
    elif "infrastructure_blocked" in outcomes:
        outcome = "infrastructure_blocked"
    elif all(value == "qualified" for value in outcomes):
        outcome = "qualified"
    elif len(set(tables)) != 1 or len(set(outcomes)) != 1:
        outcome = "flake_observed"
    else:
        outcome = "quality_rejected"
    if outcome == "qualified" and (not complete or len(set(tables)) != 1):
        raise RuntimeError("curated revalidation qualification is not stable")
    terminal_failure_classes = {
        value.get("terminal_failure_class")
        for value in payloads
        if value.get("terminal_failure_class") is not None
    }
    if len(terminal_failure_classes) > 1:
        raise RuntimeError("curated revalidation terminal failures conflict")
    terminal_failure_class = next(iter(terminal_failure_classes), None)
    if outcome == "version_incompatible" and terminal_failure_class is None:
        raise RuntimeError("curated version incompatibility lacks a terminal class")
    registered_adapters = {
        value["registered_gate"]["adapter_id"]
        for value in payloads
        if value.get("registered_gate") is not None
    }
    if registered_adapters and (
        len(registered_adapters) != 1
        or any(value.get("registered_gate") is None for value in payloads)
    ):
        raise RuntimeError("curated registered gate adapters are inconsistent")
    first = payloads[0]
    repetitions = []
    for value in payloads:
        row = {
            "repetition": value["repetition"],
            "outcome": value["outcome"],
            "complete": value["complete"],
            "fixed_tests": value["fixed_tests"],
            "buggy_tests": value["buggy_tests"],
            "runner_error": value["runner_error"],
        }
        if value.get("terminal_failure_class") is not None:
            row["terminal_failure_class"] = value["terminal_failure_class"]
        repetitions.append(row)
    payload = {
        "task_id": task.task_id,
        "candidate": {
            "repository_key": task.repository_key,
            "change_number": task.change_number,
        },
        "defect_locus": task.defect_locus,
        "outcome": outcome,
        "complete": complete,
        "qualified": outcome == "qualified",
        "fresh_revalidation": True,
        "historical_result_inherited": False,
        "required_repetitions": _REQUIRED_REPETITIONS,
        "completed_repetitions": len(payloads),
        "fixed_tests": dict(first["fixed_tests"]),
        "buggy_tests": dict(first["buggy_tests"]),
        "repetitions": repetitions,
    }
    if outcome == "version_incompatible":
        payload["terminal_failure_class"] = terminal_failure_class
    if registered_adapters:
        payload["registered_gate_adapter"] = next(iter(registered_adapters))
    return payload


def _attempt_id(task: Any, repetition: int) -> str:
    return (
        f"curated_task_revalidation/{task.repository_key}/{task.change_number}/"
        f"construction/repetition-{repetition}"
    )


def _aggregate_id(task: Any) -> str:
    return (
        f"curated_task_revalidation/{task.repository_key}/{task.change_number}/"
        "construction"
    )


def _load_if_present(
    artifact_root: str | Path,
    *,
    artifact_id: str,
    artifact_kind: str,
    semantic_version: str,
) -> StableArtifact | None:
    try:
        return load_stable_artifact(
            artifact_root,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            semantic_version=semantic_version,
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _validate_existing_attempt(
    artifact: StableArtifact,
    selection: StableArtifact,
    task: Any,
    repetition: int,
) -> None:
    if artifact.inputs != (selection.reference,):
        raise RuntimeError("existing curated revalidation dependency is invalid")
    payload = artifact.payload
    if (
        not _matches_task_payload(payload, task)
        or payload.get("repetition") != repetition
    ):
        raise RuntimeError("existing curated revalidation is incomplete")
    if (
        payload.get("fresh_revalidation") is not True
        or payload.get("historical_result_inherited") is not False
        or payload.get("outcome") not in _TERMINAL_OUTCOMES
        or type(payload.get("complete")) is not bool
    ):
        raise RuntimeError("existing curated revalidation is invalid")
    if payload.get("registered_gate") is not None:
        fixed_tests = _test_outcomes(payload.get("fixed_tests"), "fixed")
        buggy_tests = _test_outcomes(payload.get("buggy_tests"), "buggy")
        _registered_gate_value(
            payload["registered_gate"],
            task_id=task.task_id,
            complete=payload["complete"],
            qualified=payload["outcome"] == "qualified",
            fixed_tests=fixed_tests,
            buggy_tests=buggy_tests,
        )


def _validate_existing_aggregate(
    artifact_root: str | Path,
    artifact: StableArtifact,
    selection: StableArtifact,
    task: Any,
) -> None:
    attempts: list[StableArtifact] = []
    completed = artifact.payload.get("completed_repetitions")
    if type(completed) is not int or not 1 <= completed <= _REQUIRED_REPETITIONS:
        raise RuntimeError("existing curated revalidation count is invalid")
    for repetition in range(1, completed + 1):
        attempt = load_stable_artifact(
            artifact_root,
            artifact_id=_attempt_id(task, repetition),
            artifact_kind=_ATTEMPT_KIND,
            semantic_version=_ATTEMPT_VERSION,
        )
        _validate_existing_attempt(attempt, selection, task, repetition)
        attempts.append(attempt)
    expected_inputs = (
        selection.reference,
        *(attempt.reference for attempt in attempts),
    )
    if artifact.inputs != expected_inputs:
        raise RuntimeError("existing curated revalidation dependencies are invalid")
    expected_payload = _aggregate_payload(task, tuple(attempts))
    if artifact.payload != expected_payload:
        raise RuntimeError("existing curated revalidation aggregate is invalid")


def _matches_task_payload(payload: Any, task: Any) -> bool:
    return type(payload) is dict and (
        payload.get("task_id") == task.task_id
        and payload.get("candidate")
        == {
            "repository_key": task.repository_key,
            "change_number": task.change_number,
        }
        and payload.get("defect_locus") == task.defect_locus
    )


def _safe_directory(path: Path, *, create: bool = False) -> Path:
    absolute = path.absolute()
    if create:
        absolute.mkdir(parents=True, exist_ok=True)
    info = absolute.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("curated revalidation directory is unsafe")
    return absolute.resolve(strict=True)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["run_curated_construction_revalidation"]
