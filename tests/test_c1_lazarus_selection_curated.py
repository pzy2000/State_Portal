from __future__ import annotations

import os
from pathlib import Path

import pytest

import warptest.c1_lazarus_selection.revalidation as revalidation_module
from warptest.c1_lazarus_selection.curated import (
    load_curated_catalog,
    write_curated_selection,
)
from warptest.c1_lazarus_selection.revalidation import (
    run_curated_construction_revalidation,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


WORKSPACE = Path(__file__).resolve().parents[1]
CATALOG = WORKSPACE / "benchmarks/p5/lazarus/curated.yaml"


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="authorization/curated/test/v1",
        scope="screening",
        granted_at="2026-08-13T01:00:00Z",
        local_user_id=os.getuid(),
    )


def _output_capture(*, stdout: str = "", stderr: str = "") -> dict[str, object]:
    return {
        "schema": "c1-lazarus-subprocess-output-v1",
        "schema_version": 1,
        "redaction": "environment_and_credential_markers_v1",
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def test_curated_catalog_selects_24_quality_floor_candidates() -> None:
    catalog = load_curated_catalog(CATALOG, workspace_root=WORKSPACE)

    assert len(catalog) == 24
    assert sum(task.defect_locus == "at_goal" for task in catalog) == 16
    assert sum(task.defect_locus == "on_path" for task in catalog) == 8
    assert {task.repository_key for task in catalog} == {
        "blender",
        "jupyterlab",
        "luanti",
        "openra",
    }
    repaired_mapping = next(
        task
        for task in catalog
        if task.task_id == "lz-jupyterlab-12863-dialog-getvalue-error"
    )
    assert repaired_mapping.change_number == 18938
    assert repaired_mapping.mapping_evidence_path is not None


def test_curated_selection_consumes_strict_audits_without_inheriting_them(
    tmp_path: Path,
) -> None:
    authorization = _authorization()
    catalog = load_curated_catalog(CATALOG, workspace_root=WORKSPACE)
    write_stable_artifact(
        tmp_path,
        artifact_id="population_selection/all/current/v1",
        artifact_kind="population_selection",
        semantic_version="1.0",
        created_at="2026-08-13T01:00:01Z",
        inputs=(),
        payload={
            "selection_status": "insufficient_population",
            "qualified_count": 0,
            "selected_candidates": [],
            "complete": True,
        },
        authorization=authorization,
    )
    for task in catalog:
        payload = {
            "task_label": task.task_id,
            "repository_key": task.repository_key,
            "mapping_state": "mapped",
            "change_number": task.change_number,
            "inherited_conclusions": False,
        }
        if task.mapping_evidence_path is not None:
            payload.update(
                {
                    "mapping_state": "unresolved",
                    "change_number": None,
                    "reason": "change_number_missing",
                }
            )
        write_stable_artifact(
            tmp_path,
            artifact_id=f"old24_audit/{task.task_id}/current/v1",
            artifact_kind="old24_audit",
            semantic_version="1.0",
            created_at="2026-08-13T01:00:02Z",
            inputs=(),
            payload=payload,
            authorization=authorization,
        )

    selected = write_curated_selection(
        tmp_path,
        catalog_path=CATALOG,
        workspace_root=WORKSPACE,
        strict_selection_artifact_id="population_selection/all/current/v1",
        authorization=authorization,
    )

    assert selected.payload["selection_mode"] == "curated_v1"
    assert selected.payload["selected_task_count"] == 24
    assert selected.payload["strict_selection_status"] == "insufficient_population"
    assert selected.payload["unbiased_population_selection"] is False
    assert selected.payload["construction_revalidation_required"] is True
    assert selected.payload["paid_actions_authorized"] is False
    assert selected.payload["formal_execution_authorized"] is False
    assert len(selected.inputs) == 25
    reloaded = load_stable_artifact(
        tmp_path,
        artifact_id="curated_selection/all/current/v1",
        artifact_kind="curated_selection",
        semantic_version="1.0",
    )
    assert reloaded.payload == selected.payload


def test_curated_construction_revalidation_executes_and_persists_fresh_matrix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    authorization = _authorization()
    catalog = load_curated_catalog(CATALOG, workspace_root=WORKSPACE)
    write_stable_artifact(
        tmp_path,
        artifact_id="population_selection/all/current/v1",
        artifact_kind="population_selection",
        semantic_version="1.0",
        created_at="2026-08-13T01:00:01Z",
        inputs=(),
        payload={
            "selection_status": "insufficient_population",
            "qualified_count": 0,
            "selected_candidates": [],
            "complete": True,
        },
        authorization=authorization,
    )
    for task in catalog:
        payload = {
            "task_label": task.task_id,
            "repository_key": task.repository_key,
            "mapping_state": "mapped",
            "change_number": task.change_number,
            "inherited_conclusions": False,
        }
        if task.mapping_evidence_path is not None:
            payload.update(
                {
                    "mapping_state": "unresolved",
                    "change_number": None,
                    "reason": "change_number_missing",
                }
            )
        write_stable_artifact(
            tmp_path,
            artifact_id=f"old24_audit/{task.task_id}/current/v1",
            artifact_kind="old24_audit",
            semantic_version="1.0",
            created_at="2026-08-13T01:00:02Z",
            inputs=(),
            payload=payload,
            authorization=authorization,
        )
    selection = write_curated_selection(
        tmp_path,
        catalog_path=CATALOG,
        workspace_root=WORKSPACE,
        strict_selection_artifact_id="population_selection/all/current/v1",
        authorization=authorization,
    )
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return {
            "complete": True,
            "qualified": True,
            "fixed_tests": {"f2p": True, "p2p": True},
            "buggy_tests": {"f2p": False, "p2p": True},
            "failures": [],
            "executions": [
                {
                    "version": "fixed",
                    "duration_ms": 12.5,
                    "results": (
                        {
                            "id": "f2p",
                            "passed": True,
                            "exit_code": 0,
                            "timed_out": False,
                            "duration_ms": 5.0,
                            "output_capture": _output_capture(stdout="fixed f2p"),
                        },
                        {
                            "id": "p2p",
                            "passed": True,
                            "exit_code": 0,
                            "timed_out": False,
                            "duration_ms": 5.0,
                            "output_capture": _output_capture(stdout="fixed p2p"),
                        },
                    ),
                },
                {
                    "version": "buggy",
                    "duration_ms": 13.5,
                    "results": (
                        {
                            "id": "f2p",
                            "passed": False,
                            "exit_code": 1,
                            "timed_out": False,
                            "duration_ms": 5.5,
                            "output_capture": _output_capture(stderr="buggy f2p"),
                        },
                        {
                            "id": "p2p",
                            "passed": True,
                            "exit_code": 0,
                            "timed_out": False,
                            "duration_ms": 5.5,
                            "output_capture": _output_capture(stdout="buggy p2p"),
                        },
                    ),
                },
            ],
        }

    monkeypatch.setattr(
        revalidation_module,
        "run_registered_construction_test_matrix",
        execute,
    )

    artifact = run_curated_construction_revalidation(
        tmp_path,
        task_id="lz-jupyterlab-12863-dialog-getvalue-error",
        catalog_path=CATALOG,
        workspace_root=WORKSPACE,
        curated_selection_artifact_id=selection.reference.artifact_id,
        authorization=authorization,
    )

    assert len(calls) == 3
    assert artifact.payload["outcome"] == "qualified"
    assert artifact.payload["fresh_revalidation"] is True
    assert artifact.payload["historical_result_inherited"] is False
    assert artifact.payload["required_repetitions"] == 3
    assert artifact.payload["completed_repetitions"] == 3
    assert artifact.payload["candidate"] == {
        "repository_key": "jupyterlab",
        "change_number": 18938,
    }
    assert artifact.payload["fixed_tests"] == {"f2p": True, "p2p": True}
    assert artifact.payload["buggy_tests"] == {"f2p": False, "p2p": True}
    assert len(artifact.inputs) == 4
    attempt = load_stable_artifact(
        tmp_path,
        artifact_id=(
            "curated_task_revalidation/jupyterlab/18938/construction/repetition-1"
        ),
        artifact_kind="curated_construction_repetition",
        semantic_version="1.0",
    )
    assert attempt.payload["executions"][0]["results"][0][
        "output_capture"
    ] == _output_capture(stdout="fixed f2p")

    resumed = run_curated_construction_revalidation(
        tmp_path,
        task_id="lz-jupyterlab-12863-dialog-getvalue-error",
        catalog_path=CATALOG,
        workspace_root=WORKSPACE,
        curated_selection_artifact_id=selection.reference.artifact_id,
        authorization=authorization,
    )

    assert resumed.payload == artifact.payload
    assert len(calls) == 3


def test_curated_revalidation_projects_registered_version_incompatibility() -> None:
    task = next(
        item
        for item in load_curated_catalog(CATALOG, workspace_root=WORKSPACE)
        if item.task_id == "lz-jupyterlab-13657-replace-selection-command"
    )
    failed_prepare = {
        "id": "jupyterlab-test-build-codemirror",
        "passed": False,
        "exit_code": 1,
        "timed_out": False,
        "duration_ms": 2.0,
        "output_capture": _output_capture(stderr="compile failure"),
    }
    report = {
        "complete": False,
        "qualified": False,
        "fixed_tests": {"f2p": False, "p2p": False},
        "buggy_tests": {"f2p": False, "p2p": False},
        "failures": ["registered construction terminal: version_incompatible"],
        "terminal_failure_class": "version_incompatible",
        "executions": [
            {
                "version": "fixed",
                "duration_ms": 12.5,
                "results": (),
                "prepare_results": (failed_prepare,),
                "terminal_failure_class": "version_incompatible",
            },
            {
                "version": "buggy",
                "duration_ms": 13.5,
                "results": (),
                "prepare_results": (failed_prepare,),
                "terminal_failure_class": "version_incompatible",
            },
        ],
    }

    payload = revalidation_module._attempt_payload(task, 1, report)

    assert payload["outcome"] == "version_incompatible"
    assert payload["complete"] is False
    assert payload["terminal_failure_class"] == "version_incompatible"
    assert payload["executions"][0]["results"] == []


def test_curated_revalidation_projects_prepare_failure_as_infrastructure() -> None:
    task = next(
        item
        for item in load_curated_catalog(CATALOG, workspace_root=WORKSPACE)
        if item.task_id == "lz-jupyterlab-13657-replace-selection-command"
    )
    failed_prepare = {
        "id": "registered-prepare",
        "passed": False,
        "exit_code": 17,
        "timed_out": False,
        "duration_ms": 2.0,
        "output_capture": _output_capture(stderr="prepare failure"),
    }
    report = {
        "complete": False,
        "qualified": False,
        # The preparation step prevented an upstream test from running, so a
        # false F2P/P2P table would be fabricated evidence rather than a
        # measured verdict.
        "fixed_tests": {},
        "buggy_tests": {},
        "failures": [
            "fixed: construction command registered-prepare exited 17",
            "buggy: construction command registered-prepare exited 17",
        ],
        "terminal_failure_class": None,
        "executions": [
            {
                "version": "fixed",
                "duration_ms": 12.5,
                "results": (),
                "prepare_results": (failed_prepare,),
                "failures": ("construction command registered-prepare exited 17",),
                "terminal_failure_class": None,
            },
            {
                "version": "buggy",
                "duration_ms": 13.5,
                "results": (),
                "prepare_results": (failed_prepare,),
                "failures": ("construction command registered-prepare exited 17",),
                "terminal_failure_class": None,
            },
        ],
    }

    payload = revalidation_module._attempt_payload(task, 1, report)

    assert payload["outcome"] == "infrastructure_blocked"
    assert payload["complete"] is False
    assert payload["runner_error"] is None
    assert payload["fixed_tests"] == {}
    assert payload["buggy_tests"] == {}
    assert payload["executions"][0]["results"] == []
    assert payload["executions"][0]["prepare_results"] == [
        {
            "test_id": "registered-prepare",
            "passed": False,
            "exit_code": 17,
            "timed_out": False,
            "duration_ms": 2.0,
            "output_capture": _output_capture(stderr="prepare failure"),
        }
    ]
    assert payload["executions"][0]["failures"] == [
        "construction command registered-prepare exited 17"
    ]


@pytest.mark.parametrize("capture_state", ("missing", "corrupt"))
def test_curated_revalidation_rejects_missing_or_corrupt_subprocess_capture(
    capture_state: str,
) -> None:
    task = next(
        item
        for item in load_curated_catalog(CATALOG, workspace_root=WORKSPACE)
        if item.task_id == "lz-jupyterlab-12863-dialog-getvalue-error"
    )

    def result_row(test_id: str, *, passed: bool) -> dict[str, object]:
        row: dict[str, object] = {
            "id": test_id,
            "passed": passed,
            "exit_code": 0 if passed else 1,
            "timed_out": False,
            "duration_ms": 5.0,
        }
        if capture_state == "corrupt" and test_id == "f2p":
            row["output_capture"] = {
                **_output_capture(stderr="corrupt schema"),
                "schema_version": 99,
            }
        elif capture_state != "missing" or test_id != "f2p":
            row["output_capture"] = _output_capture(stdout=f"{test_id} output")
        return row

    report = {
        "complete": True,
        "qualified": True,
        "fixed_tests": {"f2p": True, "p2p": True},
        "buggy_tests": {"f2p": False, "p2p": True},
        "failures": [],
        "executions": [
            {
                "version": "fixed",
                "duration_ms": 12.5,
                "results": (
                    result_row("f2p", passed=True),
                    result_row("p2p", passed=True),
                ),
            },
            {
                "version": "buggy",
                "duration_ms": 13.5,
                "results": (
                    result_row("f2p", passed=False),
                    result_row("p2p", passed=True),
                ),
            },
        ],
    }

    with pytest.raises(RuntimeError, match="output capture"):
        revalidation_module._attempt_payload(task, 1, report)


def test_curated_revalidation_preserves_a_registered_openra_gate_receipt() -> None:
    task = next(
        item
        for item in load_curated_catalog(CATALOG, workspace_root=WORKSPACE)
        if item.task_id == "lz-openra-21453-hotkey-ready-building"
    )
    f2p = "openra-pr21453-hidden-ready-building-f2p"
    p2p = "openra-action-queue-p2p"

    def execution(version: str, *, f2p_passed: bool) -> dict:
        return {
            "version": version,
            "duration_ms": 12.5,
            "results": (
                {
                    "id": f2p,
                    "passed": f2p_passed,
                    "exit_code": 0 if f2p_passed else 1,
                    "timed_out": False,
                    "duration_ms": 5.0,
                    "output_capture": _output_capture(stdout=f"{version} f2p output"),
                },
                {
                    "id": p2p,
                    "passed": True,
                    "exit_code": 0,
                    "timed_out": False,
                    "duration_ms": 5.0,
                    "output_capture": _output_capture(stdout=f"{version} p2p output"),
                },
            ),
            "prepare_results": (),
            "terminal_failure_class": None,
        }

    report = {
        "complete": True,
        "qualified": True,
        "fixed_tests": {f2p: True, p2p: True},
        "buggy_tests": {f2p: False, p2p: True},
        "failures": [],
        "executions": [
            execution("fixed", f2p_passed=True),
            execution("buggy", f2p_passed=False),
        ],
        "registered_gate": {
            "adapter_id": "openra-pr21453-gui-gate-v1",
            "task_id": task.task_id,
            "complete": True,
            "passed": True,
            "checks": {
                "fixed_f2p": True,
                "buggy_f2p": False,
                "fixed_p2p": True,
                "buggy_p2p": True,
            },
            "failures": [],
            "f2p_claim": "issue_derived_gui_differential_not_upstream",
            "f2p_origin": "issue_derived_gui_differential",
            "fail_to_pass": [f2p],
            "pass_to_pass": [p2p],
            "report_path": "/tmp/openra-gate.json",
        },
    }

    payload = revalidation_module._attempt_payload(task, 1, report)

    assert payload["outcome"] == "qualified"
    assert payload["registered_gate"] == report["registered_gate"]
