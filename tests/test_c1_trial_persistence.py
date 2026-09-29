from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

from warptest import cost, p5_aggregate, p5_scale


POLICY = {
    "schema_version": 3,
    "atomic_unit": "trial",
    "unknown_usage": "quarantine_primary_attempt",
    "same_attempt_resume": "forbidden",
    "source_import": "all_completed_tuples",
    "replacement_selection": "all_noncompleted_tuples",
    "legacy_import": "forbidden",
    "normal_execution": "shared_invocation_incremental_snapshot_v1",
    "recovery_execution": "one_task_fresh_invocation_v1",
    "recovery_unknown_resume": "fresh_attempt_same_clean_root",
    "replaceable_source_errors": [
        "exact_usage_task_timeout",
        "exact_usage_retryable_infrastructure",
    ],
    "max_recovery_attempts_per_tuple": 3,
    "attempt_order": "deterministic_registered_tuple_order",
    "global_unknown_stop": True,
}


def _tasks(root: Path) -> Path:
    path = root / "tasks.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "tasks": [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _manifest(root: Path) -> dict:
    return {
        "models": ["m"],
        "seeds": 1,
        "c1_task_timeout_s": 1200,
        "c1_trial_persistence": copy.deepcopy(POLICY),
        "arms": {
            "c1_state": {
                "tasks": str(_tasks(root)),
                "explore": True,
                "max_iters": 1,
                "restore_diagnostics": False,
            }
        },
        "retry": {"max_attempts": 1, "base_sleep_s": 0, "max_sleep_s": 0},
    }


def _exact_cost() -> dict:
    return {
        "usage_complete": True,
        "calls": 1,
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "reasoning_tokens": 0,
        "cached_prompt_tokens": 0,
        "total_tokens": 3,
        "incomplete_calls": 0,
        "unknown_prompt_token_calls": 0,
        "unknown_completion_token_calls": 0,
        "unknown_reasoning_token_calls": 0,
        "unknown_total_token_calls": 0,
    }


def _unknown_cost() -> dict:
    value = _exact_cost()
    value.update(
        usage_complete=False,
        incomplete_calls=1,
        unknown_completion_token_calls=1,
        unknown_total_token_calls=1,
    )
    value["by_kind"] = {
        "checkpoint_synthesis": {
            "calls": 1,
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "reasoning_tokens": 0,
            "cached_prompt_tokens": 0,
            "total_tokens": 3,
            "incomplete_calls": 1,
            "usage_complete": False,
        }
    }
    value["by_model"] = {"m": copy.deepcopy(value["by_kind"]["checkpoint_synthesis"])}
    value["by_kind_model"] = {
        "checkpoint_synthesis": copy.deepcopy(value["by_model"])
    }
    return value


@pytest.mark.parametrize("tampered_revision", [None, "different-revision"])
def test_eprime_recovery_validates_source_action_prompt_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tampered_revision: str | None
) -> None:
    """A reader must accept the producer's field and still reject drift."""
    root = tmp_path / "repo"
    root.mkdir()
    manifest = _manifest(root)
    arm = "c1_state_minimal_agent"
    cfg = copy.deepcopy(manifest["arms"]["c1_state"])
    cfg.update(transition_action_source="source_agent_v2",
               source_action_prompt_revision="c1-source-transition-v2")
    manifest["arms"][arm] = cfg
    run_dir = tmp_path / "recovery"
    monkeypatch.setattr(p5_scale, "c1_harness_hash", lambda _root: "harness")
    orchestrator = p5_scale.P5Orchestrator(
        root, manifest, run_dir=run_dir, models=["m"], seeds=1,
        arms=[arm], agent_kind="deterministic",
    )
    monkeypatch.setattr(orchestrator, "_require_preflight_gate", lambda: None)
    orchestrator._register_c1_trial_matrix([arm])
    task = orchestrator._c1_task_records(orchestrator._arm_cfg(arm))[0]
    task_id = task["id"]
    tasks_path = orchestrator._c1_trial_tasks_path(cfg, task)
    directory = orchestrator._c1_trial_dir(arm, "m", 0, task_id, attempt=0)
    replacement = {
        "source_index": str(tmp_path / "source/p5-run-index.json"),
        "source_index_sha256": "a" * 64, "source_run_id": "source-run",
        "source_status": "pending", "source_report": None,
        "source_report_sha256": None,
    }
    invocation = {
        "c1_outer_worker": orchestrator.c1_outer_worker,
        "c1_inner_worker": orchestrator.c1_inner_worker,
        "task_timeout_s": orchestrator.c1_task_timeout_s,
        "inner_task_plan": orchestrator._c1_inner_task_plan([task]),
        "unity_serialized": orchestrator.c1_unity_serialized,
        "unity_serialized_benchmarks": list(orchestrator.c1_unity_serialized_benchmarks),
        "background_browser_headless": True, "atomic_defer": True,
        "explore": True, "max_explore_steps": 12, "max_iters": 1, "seeds": 1,
        "restore_diagnostics": False, "restore_diagnostic_task_ids": [],
        "shared_synthesis_cache": str((run_dir / "c1-self-synthesis-cache").resolve()),
        "transition_action_source": "source_agent_v2",
        "source_action_cache": str((run_dir / "c1-source-action-cache").resolve()),
        "source_action_max_steps": 16, "source_action_max_tokens": 8192,
        "source_action_format_repairs": 1,
        "source_action_prompt_revision": "c1-source-transition-v2",
    }

    def producer(run_meta: dict) -> dict:
        payload = {
            "schema_version": 2, "complete": False, "retryable_incomplete": False,
            "model": "m", "seed": 0, "run_context": run_meta,
            "results": [{
                "task_id": task_id, "incomplete": True, "timed_out": True,
                "infrastructure_error": False, "retryable_incomplete": False,
                "reason": "C1 task t1 exceeded its 1200s wall-clock limit",
                "cost": _exact_cost(), "actual_cost": _exact_cost(),
            }],
        }
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "state-minimal-agent.json").write_text(json.dumps(payload))
        return {**payload, "report_dir": str(directory)}

    with pytest.raises(p5_scale.NonRetryableC1InvocationError):
        orchestrator._execute(
            arm, model="m", seed=0, task_id=task_id, attempt=0,
            replacement_for=replacement, invocation_dir=directory,
            tasks_path=tasks_path, invocation=invocation, fn=producer,
        )
    arguments = dict(arm=arm, model="m", seed=0, task_id=task_id,
                     expected_replacement=replacement)
    orchestrator._validate_existing_replacement_row(**arguments)
    sidecar_path = directory / "p5-invocation-context.json"
    sidecar = json.loads(sidecar_path.read_text())
    if tampered_revision is None:
        sidecar["invocation"].pop("source_action_prompt_revision")
    else:
        sidecar["invocation"]["source_action_prompt_revision"] = tampered_revision
    sidecar_path.write_text(json.dumps(sidecar))
    with pytest.raises(ValueError, match="sidecar provenance mismatch"):
        orchestrator._validate_existing_replacement_row(**arguments)


def _settled_terminal_result(task_id: str) -> dict:
    zero = cost.CostMeter().summary()
    synthesis = {
        "actual_cost": copy.deepcopy(zero),
        "transient_actual_cost": copy.deepcopy(zero),
        "attributed_cost": copy.deepcopy(zero),
    }
    attempt = {
        **copy.deepcopy(zero),
        "attempt_ordinal": 1,
        "attempt_status": "nonretryable_incomplete",
        "retry_category": None,
        "retry_reason": None,
        "previous_attempt_hash": None,
        "task_actual_cost": copy.deepcopy(zero),
        "synthesis_accounting": synthesis,
    }
    attempt["attempt_hash"] = hashlib.sha256(
        json.dumps(
            {
                "task_id": task_id,
                "attempt_ordinal": 1,
                "attempt_status": "nonretryable_incomplete",
                "retry_category": None,
                "retry_reason": None,
                "previous_attempt_hash": None,
                "task_actual_cost": attempt["task_actual_cost"],
                "synthesis_accounting": synthesis,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    ledger_hash = hashlib.sha256(
        json.dumps(
            {"task_id": task_id, "attempt_hashes": [attempt["attempt_hash"]]},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "task_id": task_id,
        "incomplete": True,
        "retryable_incomplete": False,
        "infrastructure_error": False,
        "timed_out": False,
        "reason": "settled terminal model failure",
        "attempt_count": 1,
        "attempt_costs": [attempt],
        "attempt_ledger_hash": ledger_hash,
        "cost": copy.deepcopy(zero),
        "actual_cost": copy.deepcopy(zero),
        "transient_infrastructure_cost": copy.deepcopy(zero),
        "synthesis_actual_cost": copy.deepcopy(zero),
        "synthesis_transient_actual_cost": copy.deepcopy(zero),
        "synthesis_attributed_cost": copy.deepcopy(zero),
    }


def test_sidecar_migration_keeps_consumed_attempt_in_total_budget() -> None:
    arm = "c1_state_minimal_agent"
    model = "gemini-3.5-flash@relay"
    task_id = "openra-allied-combined-arms-rally"
    orchestrator = object.__new__(p5_scale.P5Orchestrator)
    orchestrator.resume = True
    orchestrator.c1_source_index = Path("source.json")
    orchestrator.c1_trial_persistence = copy.deepcopy(POLICY)
    row = {"model": model, "seed": 1, "task_id": task_id,
           "status": "error", "trial_attempt": 0}
    orchestrator.index = {arm: [row]}
    marker = {"arm": arm, "model": model, "seed": 1, "task_id": task_id,
              "trial_attempt": 0, "source_provenance": None}
    orchestrator.c1_operational_overhead = [marker]
    orchestrator.c1_source_contract_migration_metadata = {
        "migration_id": "c1_source_action_sidecar_validation_v1",
        "consumed_recovery_attempts": [{"arm": arm, "model": model, "seed": 1,
                                        "task_id": task_id, "attempt_count": 1}],
    }
    kwargs = {"model": model, "seed": 1, "task_id": task_id}
    assert orchestrator._recovery_trial_attempt(arm, **kwargs) == 1
    row["trial_attempt"] = marker["trial_attempt"] = 1
    assert orchestrator._recovery_trial_attempt(arm, **kwargs) is None
    orchestrator.c1_source_contract_migration_metadata = None
    assert orchestrator._recovery_trial_attempt(arm, **kwargs) == 2


def test_imported_exhausted_operational_failure_is_terminal_not_success(
    tmp_path: Path,
) -> None:
    report = {
        "complete": False,
        "retryable_incomplete": True,
        "results": [{
            "task_id": "t1", "incomplete": True, "timed_out": False,
            "infrastructure_error": True, "retryable_incomplete": True,
            "reason": "temporarily unavailable", "actual_cost": _exact_cost(),
            "attempt_costs": [{"attempt_status": "retryable_infrastructure",
                               "retry_category": "infra",
                               "retry_reason": "temporarily unavailable"}],
        }],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    row = {
        "model": "m", "seed": 0, "task_id": "t1", "status": "error",
        "report": str(path), "report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "imported": True,
        "source_provenance": {"source_status": "error",
                              "completion_class": "exact_usage_exhausted_operational_failure"},
    }
    orchestrator = object.__new__(p5_scale.P5Orchestrator)
    orchestrator.resume = True
    orchestrator.base = tmp_path
    orchestrator.c1_task_timeout_s = 1200
    orchestrator.c1_operational_overhead = []
    orchestrator.index = {"c1_state": [row]}
    assert orchestrator._completed(
        "c1_state", model="m", seed=0, task_id="t1"
    ) is True
    report["results"][0]["actual_cost"]["usage_complete"] = False
    path.write_text(json.dumps(report))
    row["report_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert orchestrator._completed(
        "c1_state", model="m", seed=0, task_id="t1"
    ) is False


def test_retryable_infrastructure_can_be_classified_as_settled_exact_usage() -> None:
    result = {
        "incomplete": True,
        "timed_out": False,
        "infrastructure_error": True,
        "retryable_incomplete": True,
        "reason": "temporarily unavailable",
        "actual_cost": _exact_cost(),
        "attempt_costs": [
            {
                "attempt_status": "retryable_infrastructure",
                "retry_category": "infra",
                "retry_reason": "temporarily unavailable",
            }
        ],
    }
    report = {"complete": False, "retryable_incomplete": True}

    expected = "exact_usage_exhausted_retryable_infrastructure"
    assert (
        p5_scale.P5Orchestrator._registered_source_model_failure_kind(
            result=result, report=report
        )
        == expected
    )
    assert p5_aggregate._preserved_model_failure_kind(
        result=result, report=report
    ) == expected


def _gate_metadata(tag: str) -> tuple[dict, dict]:
    api = {"name": "test", "base_url": "https://example.invalid/v1"}
    preflight = {
        "complete": True,
        "generated_at_utc": f"2026-07-{tag}T00:00:00+00:00",
        "artifact_sha256": tag * 64,
        "git_commit": None,
        "manifest_hash": "manifest",
        "harness_hash": "harness",
        "c1_application_revisions": None,
        "c1_environment": None,
        "api_provider": api,
        "model_request_profiles": {},
        "model_request_profiles_sha256": "p" * 64,
        "probe_count": 7,
        "initial_max_tokens": 8192,
        "retry_max_tokens": 16384,
        "retry_rule": "reasoning-aware",
        "temperature_contract": {
            "configured": None,
            "applies_to": "decision_probes_only",
            "fallback": "omit_only_after_explicit_http_400_temperature_rejection",
        },
        "results": [],
    }
    calibration = {
        "schema_version": 3,
        "passed": True,
        "generated_at_utc": f"2026-07-{tag}T00:00:00+00:00",
        "artifact_sha256": tag * 64,
        "git_commit": None,
        "manifest_hash": "manifest",
        "harness_hash": "harness",
        "c1_application_revisions": None,
        "c1_environment": None,
        "api_provider": api,
        "observer_model": "observer",
        "grounder_model": "grounder",
        "native_session": {"keyboard_input_sources_sha256": "k" * 64},
        "browser": {
            "passed": True,
            "headless": True,
            "frame": {"width": 1280, "height": 800, "device_scale_factor": 1.0},
        },
        "luanti": {
            "passed": True,
            "frame": {"width": 1280, "height": 720},
            "worldmod_click_event": True,
            "text_input": {"protocol": "luanti_chat_command_exact_v1", "transport": "physical"},
        },
    }
    return preflight, calibration


def _build_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[p5_scale.P5Orchestrator, Path, dict]:
    root = tmp_path / "repo"
    root.mkdir()
    manifest = _manifest(root)
    run_dir = tmp_path / "source"
    monkeypatch.setattr(p5_scale, "c1_harness_hash", lambda _root: "harness")
    source = p5_scale.P5Orchestrator(
        root,
        manifest,
        run_dir=run_dir,
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
    )
    source.preflight_metadata, source.calibration_metadata = _gate_metadata("1")
    monkeypatch.setattr(source, "_verify_preflight_artifact_unchanged", lambda: None)
    monkeypatch.setattr(source, "_verify_calibration_artifact_unchanged", lambda: None)
    source._register_c1_trial_matrix(["c1_state"])
    invocation_dir = run_dir / "c1_state" / "shared" / "seed0"
    tasks_path = Path(manifest["arms"]["c1_state"]["tasks"])

    calls = 0

    def producer(run_meta: dict) -> dict:
        nonlocal calls
        calls += 1
        invocation_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 2,
            "complete": False,
            "model": "m",
            "seed": 0,
            "results": [
                {"task_id": "t1", "incomplete": False, "cost": _exact_cost()},
                {"task_id": "t2", "incomplete": True, "cost": _unknown_cost()},
            ],
            "run_context": run_meta,
        }
        (invocation_dir / "state-loop.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return {**payload, "report_dir": str(invocation_dir)}

    monkeypatch.setattr(source, "_require_preflight_gate", lambda: None)
    with pytest.raises(cost.UsageAccountingError):
        source._execute(
            "c1_state",
            model="m",
            seed=0,
            invocation_dir=invocation_dir,
            tasks_path=tasks_path,
            invocation={
                "explore": True,
                "max_iters": 1,
                "seeds": 1,
                "restore_diagnostics": False,
                "restore_diagnostic_task_ids": [],
            },
            fn=producer,
        )
    assert calls == 1
    source.write_index()
    index_path = run_dir / "p5-run-index.json"
    return source, index_path, manifest


def test_unknown_attempt_isolates_completed_trial_and_preserves_pending_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _index_path, _manifest_data = _build_source(tmp_path, monkeypatch)
    rows = {row["task_id"]: row for row in source.index["c1_state"]}
    assert rows["t1"]["status"] == "ok"
    assert rows["t2"]["status"] == "quarantined_unknown_usage"
    assert rows["t3"]["status"] == "pending"
    assert len(source.c1_operational_overhead) == 1
    assert source.c1_operational_overhead[0]["task_id"] == "t2"
    assert source.c1_operational_overhead[0]["known_and_unknown_usage"][
        "unknown_total_token_calls"
    ] == 1
    assert "checkpoint_synthesis" in source.c1_operational_overhead[0][
        "known_and_unknown_usage"
    ]["by_kind"]


def test_healthy_shared_invocation_yields_three_immutable_trial_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    manifest = _manifest(root)
    run_dir = tmp_path / "healthy"
    monkeypatch.setattr(p5_scale, "c1_harness_hash", lambda _root: "harness")
    orchestrator = p5_scale.P5Orchestrator(
        root,
        manifest,
        run_dir=run_dir,
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
    )
    monkeypatch.setattr(orchestrator, "_require_preflight_gate", lambda: None)
    orchestrator._register_c1_trial_matrix(["c1_state"])
    invocation_dir = run_dir / "c1_state" / "shared" / "seed0"
    calls = 0

    def producer(run_meta: dict) -> dict:
        nonlocal calls
        calls += 1
        invocation_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 2,
            "complete": True,
            "model": "m",
            "seed": 0,
            "results": [
                {"task_id": task_id, "incomplete": False, "cost": _exact_cost()}
                for task_id in ("t1", "t2", "t3")
            ],
            "run_context": run_meta,
        }
        (invocation_dir / "state-loop.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return {**payload, "report_dir": str(invocation_dir)}

    orchestrator._execute(
        "c1_state",
        model="m",
        seed=0,
        invocation_dir=invocation_dir,
        tasks_path=Path(manifest["arms"]["c1_state"]["tasks"]),
        invocation={
            "explore": True,
            "max_iters": 1,
            "seeds": 1,
            "restore_diagnostics": False,
            "restore_diagnostic_task_ids": [],
        },
        fn=producer,
    )
    assert calls == 1
    rows = orchestrator.index["c1_state"]
    assert len(rows) == 3
    assert {row["status"] for row in rows} == {"ok"}
    source_payload = json.loads(
        (invocation_dir / "state-loop.json").read_text(encoding="utf-8")
    )
    for row in rows:
        snapshot = json.loads(Path(row["report"]).read_text(encoding="utf-8"))
        marker = snapshot["atomic_trial_evidence"]
        assert marker["schema_version"] == 3
        assert marker["mode"] == "shared_invocation_incremental_snapshot_v1"
        expected_result = next(
            result
            for result in source_payload["results"]
            if result["task_id"] == row["task_id"]
        )
        assert snapshot["results"] == [expected_result]
        assert hashlib.sha256(Path(row["report"]).read_bytes()).hexdigest() == row[
            "report_sha256"
        ]


def test_recovery_task_timeout_persists_resumable_exact_usage_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    manifest = _manifest(root)
    run_dir = tmp_path / "recovery"
    monkeypatch.setattr(p5_scale, "c1_harness_hash", lambda _root: "harness")
    orchestrator = p5_scale.P5Orchestrator(
        root,
        manifest,
        run_dir=run_dir,
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
    )
    monkeypatch.setattr(orchestrator, "_require_preflight_gate", lambda: None)
    orchestrator._register_c1_trial_matrix(["c1_state"])
    task_id = "t1"
    attempt = 0
    invocation_dir = orchestrator._c1_trial_dir(
        "c1_state", "m", 0, task_id, attempt=attempt
    )
    task = next(
        item
        for item in orchestrator._c1_task_records(
            orchestrator._arm_cfg("c1_state")
        )
        if item["id"] == task_id
    )
    tasks_path = orchestrator._c1_trial_tasks_path(
        orchestrator._arm_cfg("c1_state"), task
    )
    replacement_for = {
        "source_index": str((tmp_path / "source" / "p5-run-index.json").resolve()),
        "source_index_sha256": "a" * 64,
        "source_run_id": "source-run",
        "source_status": "pending",
        "source_report": None,
        "source_report_sha256": None,
    }
    invocation = {
        "c1_outer_worker": orchestrator.c1_outer_worker,
        "c1_inner_worker": orchestrator.c1_inner_worker,
        "task_timeout_s": orchestrator.c1_task_timeout_s,
        "inner_task_plan": orchestrator._c1_inner_task_plan([task]),
        "unity_serialized": orchestrator.c1_unity_serialized,
        "unity_serialized_benchmarks": list(
            orchestrator.c1_unity_serialized_benchmarks
        ),
        "background_browser_headless": True,
        "explore": True,
        "max_iters": 1,
        "seeds": 1,
        "restore_diagnostics": False,
        "restore_diagnostic_task_ids": [],
        "atomic_defer": True,
        "max_explore_steps": 12,
        "shared_synthesis_cache": str(
            (run_dir / "c1-self-synthesis-cache").resolve()
        ),
    }

    def producer(run_meta: dict) -> dict:
        exact = _exact_cost()
        payload = {
            "schema_version": 2,
            "complete": False,
            "retryable_incomplete": False,
            "model": "m",
            "seed": 0,
            "results": [
                {
                    "task_id": task_id,
                    "incomplete": True,
                    "timed_out": True,
                    "infrastructure_error": False,
                    "retryable_incomplete": False,
                    "reason": "C1 task t1 exceeded its 1200s wall-clock limit",
                    "cost": copy.deepcopy(exact),
                    "actual_cost": copy.deepcopy(exact),
                }
            ],
            "run_context": run_meta,
        }
        invocation_dir.mkdir(parents=True, exist_ok=True)
        (invocation_dir / "state-loop.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return {**payload, "report_dir": str(invocation_dir)}

    with pytest.raises(p5_scale.NonRetryableC1InvocationError):
        orchestrator._execute(
            "c1_state",
            model="m",
            seed=0,
            task_id=task_id,
            attempt=attempt,
            replacement_for=replacement_for,
            invocation_dir=invocation_dir,
            tasks_path=tasks_path,
            invocation=invocation,
            fn=producer,
        )

    row = next(
        item
        for item in orchestrator.index["c1_state"]
        if item["task_id"] == task_id
    )
    assert row["status"] == "error"
    assert row["trial_attempt"] == attempt
    assert row["replacement_for"] == replacement_for
    assert row["report"] is not None
    report_path = Path(row["report"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "atomic_trial_evidence" not in report
    assert report["results"][0]["timed_out"] is True
    assert row["report_sha256"] == hashlib.sha256(report_path.read_bytes()).hexdigest()
    overhead = orchestrator.c1_operational_overhead
    assert len(overhead) == 1
    assert overhead[0]["status"] == "exact_usage_task_timeout"
    assert overhead[0]["report_sha256"] == row["report_sha256"]
    orchestrator._validate_existing_replacement_row(
        arm="c1_state",
        model="m",
        seed=0,
        task_id=task_id,
        expected_replacement=replacement_for,
    )
    orchestrator.c1_source_index = Path(replacement_for["source_index"])
    orchestrator.resume = True
    assert (
        orchestrator._recovery_trial_attempt(
            "c1_state", model="m", seed=0, task_id=task_id
        )
        == 1
    )

    success_dir = orchestrator._c1_trial_dir(
        "c1_state", "m", 0, task_id, attempt=1
    )

    def success(run_meta: dict) -> dict:
        payload = {
            "schema_version": 2,
            "complete": True,
            "retryable_incomplete": False,
            "model": "m",
            "seed": 0,
            "results": [
                {
                    "task_id": task_id,
                    "incomplete": False,
                    "cost": _exact_cost(),
                    "actual_cost": _exact_cost(),
                }
            ],
            "run_context": run_meta,
        }
        success_dir.mkdir(parents=True, exist_ok=True)
        (success_dir / "state-loop.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return {**payload, "report_dir": str(success_dir)}

    orchestrator._execute(
        "c1_state",
        model="m",
        seed=0,
        task_id=task_id,
        attempt=1,
        replacement_for=replacement_for,
        invocation_dir=success_dir,
        tasks_path=tasks_path,
        invocation=invocation,
        fn=success,
    )
    completed = next(
        item
        for item in orchestrator.index["c1_state"]
        if item["task_id"] == task_id
    )
    assert completed["status"] == "ok"
    assert completed["trial_attempt"] == 1
    assert [item["trial_attempt"] for item in orchestrator.c1_operational_overhead] == [
        0
    ]
    orchestrator._validate_existing_replacement_row(
        arm="c1_state",
        model="m",
        seed=0,
        task_id=task_id,
        expected_replacement=replacement_for,
    )


def test_gui_recovery_resume_validates_provider_timeout_in_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    manifest = _manifest(root)
    manifest["api"] = {"provider_timeout_s": 900.0}
    manifest["visual_bridge"] = {
        "observer_model": "observer",
        "grounder_model": "match_decision_model",
    }
    manifest["c1_background_pixel_protocol"] = copy.deepcopy(
        p5_scale.load_manifest(p5_scale.DEFAULT_MANIFEST)[
            "c1_background_pixel_protocol"
        ]
    )
    manifest["arms"] = {
        "c1_agent_s": {
            "tasks": manifest["arms"]["c1_state"]["tasks"],
            "start_mode": "from_start",
            "policy_kind": "agent_s",
            "step_budgets": {"long": 100},
            "max_explore_steps": 12,
        }
    }
    run_dir = tmp_path / "recovery"
    monkeypatch.setattr(p5_scale, "c1_harness_hash", lambda _root: "harness")
    orchestrator = p5_scale.P5Orchestrator(
        root,
        manifest,
        run_dir=run_dir,
        models=["m"],
        seeds=1,
        arms=["c1_agent_s"],
        agent_kind="deterministic",
    )
    monkeypatch.setattr(orchestrator, "_require_preflight_gate", lambda: None)
    orchestrator._register_c1_trial_matrix(["c1_agent_s"])
    task_id = "t1"
    task = next(
        item
        for item in orchestrator._c1_task_records(
            orchestrator._arm_cfg("c1_agent_s")
        )
        if item["id"] == task_id
    )
    tasks_path = orchestrator._c1_trial_tasks_path(
        orchestrator._arm_cfg("c1_agent_s"), task
    )
    invocation_dir = orchestrator._c1_trial_dir(
        "c1_agent_s", "m", 0, task_id, attempt=0
    )
    replacement_for = {
        "source_index": str((tmp_path / "source" / "p5-run-index.json").resolve()),
        "source_index_sha256": "a" * 64,
        "source_run_id": "source-run",
        "source_status": "pending",
        "source_report": None,
        "source_report_sha256": None,
    }
    invocation = {
        "c1_outer_worker": orchestrator.c1_outer_worker,
        "c1_inner_worker": orchestrator.c1_inner_worker,
        "inner_task_plan": orchestrator._c1_inner_task_plan([task]),
        "unity_serialized": orchestrator.c1_unity_serialized,
        "unity_serialized_benchmarks": list(
            orchestrator.c1_unity_serialized_benchmarks
        ),
        "background_browser_headless": True,
        "start_mode": "from_start",
        "policy_kind": "agent_s",
        "step_budgets": {"long": 100},
        "observer_model": "observer",
        "grounder_model": "m",
        "provider_timeout_s": orchestrator.provider_timeout_s,
        "task_timeout_s": orchestrator.c1_task_timeout_s,
        "synth_model": "m",
        "max_explore_steps": 12,
        "shared_synthesis_cache": str(
            (run_dir / "c1-self-synthesis-cache").resolve()
        ),
        "start_gate_protocol": orchestrator.c1_start_gate_protocol,
        "atomic_defer": True,
    }

    def producer(run_meta: dict) -> dict:
        payload = {
            "schema_version": 2,
            "complete": True,
            "model": "m",
            "seed": 0,
            "results": [
                {
                    "task_id": task_id,
                    "incomplete": False,
                    "cost": _exact_cost(),
                    "actual_cost": _exact_cost(),
                }
            ],
            "run_context": run_meta,
        }
        invocation_dir.mkdir(parents=True, exist_ok=True)
        (invocation_dir / "gui-agent.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return {**payload, "report_dir": str(invocation_dir)}

    orchestrator._execute(
        "c1_agent_s",
        model="m",
        seed=0,
        task_id=task_id,
        attempt=0,
        replacement_for=replacement_for,
        invocation_dir=invocation_dir,
        tasks_path=tasks_path,
        invocation=invocation,
        fn=producer,
    )
    sidecar = json.loads(
        (invocation_dir / "p5-invocation-context.json").read_text(encoding="utf-8")
    )
    assert sidecar["invocation"]["provider_timeout_s"] == 900.0
    orchestrator._validate_existing_replacement_row(
        arm="c1_agent_s",
        model="m",
        seed=0,
        task_id=task_id,
        expected_replacement=replacement_for,
    )


def test_clean_root_imports_all_completed_and_registers_every_noncompleted_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, index_path, manifest = _build_source(tmp_path, monkeypatch)
    current = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=tmp_path / "recovery",
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
    )
    # A fresh artifact may differ in timestamp/SHA while preserving the exact
    # provider, model, viewport, input protocol, and keyboard-source contract.
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    current._register_c1_trial_matrix(["c1_state"])
    current._import_c1_source_trials()

    rows = {row["task_id"]: row for row in current.index["c1_state"]}
    assert rows["t1"]["status"] == "ok"
    assert rows["t1"]["imported"] is True
    assert rows["t2"]["status"] == "pending"
    assert rows["t3"]["status"] == "pending"
    assert set(key[3] for key in current._c1_replacement_sources) == {"t2", "t3"}
    assert len(current.c1_operational_overhead) == 1


def _turn_t1_into_exact_timeout(index_path: Path, *, reason_suffix: str = "") -> None:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    row = next(
        item
        for item in index["arms"]["c1_state"]
        if item["task_id"] == "t1"
    )
    report_path = Path(row["report"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    invocation_path = Path(
        report["atomic_trial_evidence"]["source_invocation_report"]
    )
    invocation = json.loads(invocation_path.read_text(encoding="utf-8"))
    result = next(
        item for item in invocation["results"] if item["task_id"] == "t1"
    )
    result.update(
        timed_out=True,
        incomplete=True,
        infrastructure_error=False,
        retryable_incomplete=False,
        actual_cost=copy.deepcopy(result["cost"]),
        reason=(
            "C1 task t1 exceeded its 1200s wall-clock limit" + reason_suffix
        ),
    )
    invocation_path.write_text(json.dumps(invocation), encoding="utf-8")
    invocation_sha = hashlib.sha256(invocation_path.read_bytes()).hexdigest()
    report["complete"] = False
    report["retryable_incomplete"] = False
    report["results"] = [copy.deepcopy(result)]
    report["atomic_trial_evidence"]["source_invocation_report_sha256"] = invocation_sha
    report_path.write_text(json.dumps(report), encoding="utf-8")
    row.update(status="error", report_sha256=None)
    for other in index["arms"]["c1_state"]:
        if other["task_id"] == "t1" or not other.get("report"):
            continue
        other_path = Path(other["report"])
        other_report = json.loads(other_path.read_text(encoding="utf-8"))
        other_report["atomic_trial_evidence"][
            "source_invocation_report_sha256"
        ] = invocation_sha
        other_path.write_text(json.dumps(other_report), encoding="utf-8")
        other["report_sha256"] = hashlib.sha256(other_path.read_bytes()).hexdigest()
        for overhead in index.get("c1_operational_overhead") or []:
            if overhead.get("task_id") == other["task_id"]:
                overhead["report_sha256"] = other["report_sha256"]
    index_path.write_text(json.dumps(index), encoding="utf-8")


def _turn_t1_into_exact_retryable_infrastructure(
    index_path: Path, *, retry_category: str = "infra"
) -> None:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    row = next(
        item
        for item in index["arms"]["c1_state"]
        if item["task_id"] == "t1"
    )
    report_path = Path(row["report"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    invocation_path = Path(
        report["atomic_trial_evidence"]["source_invocation_report"]
    )
    invocation = json.loads(invocation_path.read_text(encoding="utf-8"))
    result = next(
        item for item in invocation["results"] if item["task_id"] == "t1"
    )
    result.update(
        timed_out=False,
        incomplete=True,
        infrastructure_error=True,
        retryable_incomplete=True,
        actual_cost=copy.deepcopy(result["cost"]),
        reason="phase_a stopped after final_screenshot_capture_failed",
        attempt_costs=[
            {
                **copy.deepcopy(result["cost"]),
                "attempt_status": "retryable_infrastructure",
                "retry_category": retry_category,
                "retry_reason": "service unavailable: final_screenshot_capture_failed",
            }
        ],
        attempt_count=1,
        attempt_ledger_hash="fixture-ledger",
    )
    invocation_path.write_text(json.dumps(invocation), encoding="utf-8")
    invocation_sha = hashlib.sha256(invocation_path.read_bytes()).hexdigest()
    report["complete"] = False
    report["retryable_incomplete"] = False
    report["results"] = [copy.deepcopy(result)]
    report["atomic_trial_evidence"]["source_invocation_report_sha256"] = invocation_sha
    report_path.write_text(json.dumps(report), encoding="utf-8")
    row.update(status="error", report_sha256=None)
    for other in index["arms"]["c1_state"]:
        if other["task_id"] == "t1" or not other.get("report"):
            continue
        other_path = Path(other["report"])
        other_report = json.loads(other_path.read_text(encoding="utf-8"))
        other_report["atomic_trial_evidence"][
            "source_invocation_report_sha256"
        ] = invocation_sha
        other_path.write_text(json.dumps(other_report), encoding="utf-8")
        other["report_sha256"] = hashlib.sha256(other_path.read_bytes()).hexdigest()
        for overhead in index.get("c1_operational_overhead") or []:
            if overhead.get("task_id") == other["task_id"]:
                overhead["report_sha256"] = other["report_sha256"]
    index_path.write_text(json.dumps(index), encoding="utf-8")


def test_source_import_replaces_only_exact_usage_registered_task_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, index_path, manifest = _build_source(tmp_path, monkeypatch)
    _turn_t1_into_exact_timeout(index_path)

    current = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=tmp_path / "recovery-timeout",
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
    )
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    current._register_c1_trial_matrix(["c1_state"])
    current._import_c1_source_trials()

    replacement = current._c1_replacement_sources[
        current._trial_key("c1_state", "m", 0, "t1")
    ]
    assert replacement["source_status"] == "error"
    assert replacement["source_report_sha256"] == hashlib.sha256(
        Path(replacement["source_report"]).read_bytes()
    ).hexdigest()
    timeout_overhead = [
        row
        for row in current.c1_operational_overhead
        if row["task_id"] == "t1"
    ]
    assert len(timeout_overhead) == 1
    assert timeout_overhead[0]["status"] == "replaced_exact_usage_task_timeout"
    assert timeout_overhead[0]["known_and_unknown_usage"]["usage_complete"] is True


def test_source_import_replaces_exact_usage_retryable_infrastructure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, index_path, manifest = _build_source(tmp_path, monkeypatch)
    _turn_t1_into_exact_retryable_infrastructure(index_path)

    current = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=tmp_path / "recovery-infrastructure",
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
    )
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    current._register_c1_trial_matrix(["c1_state"])
    current._import_c1_source_trials()

    replacement = current._c1_replacement_sources[
        current._trial_key("c1_state", "m", 0, "t1")
    ]
    assert replacement["source_status"] == "error"
    overhead = [
        row
        for row in current.c1_operational_overhead
        if row["task_id"] == "t1"
    ]
    assert len(overhead) == 1
    assert (
        overhead[0]["status"]
        == "replaced_exact_usage_retryable_infrastructure"
    )
    assert overhead[0]["known_and_unknown_usage"]["usage_complete"] is True
    assert current.c1_sources[0][
        "exact_usage_retryable_infrastructure_trials"
    ] == 1


def test_source_import_replaces_exact_usage_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, index_path, manifest = _build_source(tmp_path, monkeypatch)
    _turn_t1_into_exact_retryable_infrastructure(
        index_path, retry_category="rate_limit"
    )

    current = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=tmp_path / "recovery-rate-limit",
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
    )
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    current._register_c1_trial_matrix(["c1_state"])
    current._import_c1_source_trials()

    replacement = current._c1_replacement_sources[
        current._trial_key("c1_state", "m", 0, "t1")
    ]
    assert replacement["source_status"] == "error"
    overhead = [
        row
        for row in current.c1_operational_overhead
        if row["task_id"] == "t1"
    ]
    assert len(overhead) == 1
    assert (
        overhead[0]["status"]
        == "replaced_exact_usage_retryable_infrastructure"
    )


def test_source_import_rejects_error_that_is_not_registered_exact_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, index_path, manifest = _build_source(tmp_path, monkeypatch)
    _turn_t1_into_exact_timeout(index_path, reason_suffix=" (fabricated)")
    current = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=tmp_path / "recovery-bad-timeout",
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
    )
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    current._register_c1_trial_matrix(["c1_state"])
    with pytest.raises(
        ValueError,
        match="not a registered exact-usage timeout or retryable infrastructure",
    ):
        current._import_c1_source_trials()


def test_source_import_rejects_missing_tuple_tamper_and_copied_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, index_path, manifest = _build_source(tmp_path, monkeypatch)

    def importer(path: Path, name: str) -> p5_scale.P5Orchestrator:
        current = p5_scale.P5Orchestrator(
            tmp_path / "repo",
            manifest,
            run_dir=tmp_path / name,
            models=["m"],
            seeds=1,
            arms=["c1_state"],
            agent_kind="deterministic",
            c1_source_index=path,
        )
        current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
        current._register_c1_trial_matrix(["c1_state"])
        return current

    payload = json.loads(index_path.read_text(encoding="utf-8"))
    missing = copy.deepcopy(payload)
    missing["arms"]["c1_state"] = [
        row for row in missing["arms"]["c1_state"] if row["task_id"] != "t3"
    ]
    index_path.write_text(json.dumps(missing), encoding="utf-8")
    with pytest.raises(ValueError, match="complete tuple matrix"):
        importer(index_path, "missing")._import_c1_source_trials()

    index_path.write_text(json.dumps(payload), encoding="utf-8")
    completed = next(
        row for row in payload["arms"]["c1_state"] if row["task_id"] == "t1"
    )
    report_path = Path(completed["report"])
    original = report_path.read_bytes()
    report_path.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="report_sha256 mismatch"):
        importer(index_path, "tampered")._import_c1_source_trials()
    report_path.write_bytes(original)

    copied = tmp_path / "copied" / "p5-run-index.json"
    copied.parent.mkdir()
    copied.write_bytes(index_path.read_bytes())
    with pytest.raises(ValueError, match="distinct clean root"):
        importer(copied, "copied-recovery")._import_c1_source_trials()

    legacy = copy.deepcopy(payload)
    legacy.pop("c1_trial_persistence", None)
    index_path.write_text(json.dumps(legacy), encoding="utf-8")
    with pytest.raises(ValueError, match="legacy"):
        importer(index_path, "legacy")._import_c1_source_trials()

    chained = copy.deepcopy(payload)
    chained["c1_sources"] = [{"source_run_id": "older"}]
    index_path.write_text(json.dumps(chained), encoding="utf-8")
    with pytest.raises(ValueError, match="chaining"):
        importer(index_path, "chained")._import_c1_source_trials()

    incompatible = copy.deepcopy(payload)
    incompatible["models"] = ["different"]
    index_path.write_text(json.dumps(incompatible), encoding="utf-8")
    with pytest.raises(ValueError, match="incompatible"):
        importer(index_path, "incompatible")._import_c1_source_trials()
    index_path.write_text(json.dumps(payload), encoding="utf-8")


def test_source_import_recursively_validates_three_generation_flattened_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original, original_index, manifest = _build_source(tmp_path, monkeypatch)
    original_row = next(
        row for row in original.index["c1_state"] if row["task_id"] == "t1"
    )

    def import_source(source_index: Path, run_name: str) -> p5_scale.P5Orchestrator:
        current = p5_scale.P5Orchestrator(
            tmp_path / "repo",
            manifest,
            run_dir=tmp_path / run_name,
            models=["m"],
            seeds=1,
            arms=["c1_state"],
            agent_kind="deterministic",
            c1_source_index=source_index,
        )
        current.preflight_metadata, current.calibration_metadata = _gate_metadata("1")
        current._register_c1_trial_matrix(["c1_state"])
        current._import_c1_source_trials()
        current.write_index()
        return current

    def flatten(current: p5_scale.P5Orchestrator, filename: str) -> Path:
        index_path = current.run_dir / "p5-run-index.json"
        index_raw = index_path.read_bytes()
        payload = json.loads(index_raw)
        upstream = payload.pop("c1_sources")
        payload["flattened_source_lineage"] = {
            "schema_version": 1,
            "kind": "c1_flattened_recovery_source",
            "derived_from_index": str(index_path),
            "derived_from_index_sha256": hashlib.sha256(index_raw).hexdigest(),
            "upstream_sources": copy.deepcopy(upstream),
        }
        flattened = current.run_dir / filename
        flattened.write_text(json.dumps(payload), encoding="utf-8")
        return flattened

    generation1 = import_source(original_index, "generation1")
    replacement_calls: list[tuple[str, int]] = []
    _install_recovery_producer(
        monkeypatch,
        {("t2", 0): "ok", ("t3", 0): "ok"},
        replacement_calls,
    )
    generation1._run_c1_state_invocation("m", 0)
    generation1.write_index()
    assert sorted(replacement_calls) == [("t2", 0), ("t3", 0)]
    flattened1 = flatten(generation1, "flattened1.json")
    external_flattened1 = tmp_path / "external" / "flattened1.json"
    external_flattened1.parent.mkdir()
    external_flattened1.write_bytes(flattened1.read_bytes())
    generation2 = import_source(external_flattened1, "generation2")
    prior_report = Path(str(original_row["report"]))
    prior_report_sha = hashlib.sha256(prior_report.read_bytes()).hexdigest()
    for ordinal in (0, 1):
        generation2.c1_operational_overhead.append(
            {
                "attempt_id": f"prior-attempt-{ordinal}",
                "arm": "c1_state",
                "model": "m",
                "seed": 0,
                "task_id": "t1",
                "trial_attempt": 0,
                "status": "replaced_exact_usage_retryable_infrastructure",
                "report": str(prior_report),
                "report_sha256": prior_report_sha,
                "known_and_unknown_usage": _exact_cost(),
                "error": "prior retryable infrastructure attempt",
                "replacement_for": None,
                "source_provenance": {
                    "source_index": str(original_index),
                    "source_run_id": original.run_id,
                    "generation": ordinal,
                },
            }
        )
    generation2.write_index()
    flattened2 = flatten(generation2, "flattened2.json")
    generation3 = import_source(flattened2, "generation3")

    imported = next(
        row for row in generation3.index["c1_state"] if row["task_id"] == "t1"
    )
    assert imported["status"] == "ok"
    assert imported["attempt_id"] == original_row["attempt_id"]
    assert imported["report"] == original_row["report"]
    lineage = imported["source_provenance"]["flattened_source_lineage"]
    assert lineage["evidence_source_index"] == str(original_index)
    assert lineage["evidence_source_run_id"] == original.run_id
    imported_replacement = next(
        row for row in generation3.index["c1_state"] if row["task_id"] == "t2"
    )
    assert imported_replacement["imported"] is True
    assert imported_replacement["replacement_for"]["source_run_id"] == original.run_id
    replacement_lineage = imported_replacement["source_provenance"][
        "flattened_source_lineage"
    ]
    assert replacement_lineage["evidence_source_run_id"] == generation1.run_id
    recursive_overhead = [
        row
        for row in generation3.c1_operational_overhead
        if row.get("task_id") == "t1"
    ]
    assert [row["attempt_id"] for row in recursive_overhead] == [
        "prior-attempt-0",
        "prior-attempt-1",
    ]
    assert all(
        row["source_provenance"]["source_index"] == str(flattened2)
        for row in recursive_overhead
    )
    assert p5_aggregate._c1_trial_recovery_issues(generation3.write_index()) == []

    tampered = json.loads(flattened2.read_text(encoding="utf-8"))
    tampered_row = next(
        row for row in tampered["arms"]["c1_state"] if row["task_id"] == "t1"
    )
    tampered_row["source_provenance"]["flattened_source_lineage"][
        "evidence_source_index_sha256"
    ] = "0" * 64
    tampered_path = generation2.run_dir / "flattened2-tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="recursive lineage mismatch"):
        import_source(tampered_path, "generation3-tampered")


def test_persisted_recovery_source_is_normalized_without_changing_its_binding(
    tmp_path: Path,
) -> None:
    upstream = {
        "source_index": str(tmp_path / "original.json"),
        "source_index_sha256": "1" * 64,
        "source_run_id": "original-run",
    }
    source = {"run_id": "recovery-run", "c1_sources": [upstream], "arms": {}}
    source_path = tmp_path / "p5-run-index.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()

    normalized = p5_scale._flatten_persisted_c1_source_for_lineage(
        source_path=source_path,
        source=source,
        source_sha=source_sha,
    )

    assert source["c1_sources"] == [upstream]
    assert "c1_sources" not in normalized
    assert normalized["flattened_source_lineage"] == {
        "schema_version": 1,
        "kind": "c1_flattened_recovery_source",
        "derived_from_index": str(source_path),
        "derived_from_index_sha256": source_sha,
        "upstream_sources": [upstream],
    }


def test_aggregate_accepts_persisted_recovery_as_nested_lineage_hop(
    tmp_path: Path,
) -> None:
    original = {"run_id": "original-run", "arms": {}}
    original_path = tmp_path / "original.json"
    original_path.write_text(json.dumps(original), encoding="utf-8")
    original_meta = {
        "source_index": str(original_path),
        "source_index_sha256": hashlib.sha256(
            original_path.read_bytes()
        ).hexdigest(),
        "source_run_id": original["run_id"],
    }
    intermediate = {
        "run_id": "intermediate-run",
        "arms": {},
        "c1_sources": [original_meta],
    }
    intermediate_path = tmp_path / "intermediate.json"
    intermediate_path.write_text(json.dumps(intermediate), encoding="utf-8")
    intermediate_meta = {
        "source_index": str(intermediate_path),
        "source_index_sha256": hashlib.sha256(
            intermediate_path.read_bytes()
        ).hexdigest(),
        "source_run_id": intermediate["run_id"],
    }
    derived = {
        "run_id": "derived-run",
        "arms": {},
        "c1_sources": [intermediate_meta],
    }
    derived_path = tmp_path / "derived.json"
    derived_path.write_text(json.dumps(derived), encoding="utf-8")
    derived_sha = hashlib.sha256(derived_path.read_bytes()).hexdigest()
    flattened = copy.deepcopy(derived)
    flattened.pop("c1_sources")
    flattened["flattened_source_lineage"] = {
        "schema_version": 1,
        "kind": "c1_flattened_recovery_source",
        "derived_from_index": str(derived_path),
        "derived_from_index_sha256": derived_sha,
        "upstream_sources": [intermediate_meta],
    }

    issues = p5_aggregate._c1_flattened_trial_recovery_issues(
        run_index={},
        source=flattened,
        source_path=tmp_path / "flattened.json",
        source_sha="2" * 64,
        source_meta={},
        current_rows={},
        arms=[],
    )

    assert "C1 flattened upstream source provenance is invalid" not in issues
    assert "C1 nested flattened migration source is invalid" not in issues


def test_source_import_rejects_manifest_treatment_change_even_with_migration_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, index_path, manifest = _build_source(tmp_path, monkeypatch)
    changed_manifest = copy.deepcopy(manifest)
    changed_manifest["arms"]["c1_state"]["max_iters"] = 2
    migration_path = tmp_path / "migration.json"
    current = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        changed_manifest,
        run_dir=tmp_path / "changed-treatment",
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
        c1_source_contract_migration=migration_path,
    )
    source_raw = index_path.read_bytes()
    source = json.loads(source_raw)
    migration_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "c1_source_contract_migration",
                "source_index": str(index_path.resolve()),
                "source_index_sha256": hashlib.sha256(source_raw).hexdigest(),
                "source_run_id": source["run_id"],
                "source_manifest_hash": source["manifest_hash"],
                "source_harness_hash": source["harness_hash"],
                "current_manifest_hash": current.manifest_hash,
                "current_harness_hash": "harness",
                "allowed_migrations": ["manifest_hash", "task_manifest_hash"],
                "replaceable_source_statuses": [
                    "pending",
                    "quarantined_unknown_usage",
                    "exact_usage_task_timeout",
                    "exact_usage_retryable_infrastructure",
                ],
                "reason": "purported controller-only migration",
            }
        ),
        encoding="utf-8",
    )
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    current._register_c1_trial_matrix(["c1_state"])

    with pytest.raises(
        ValueError,
        match="C1 source index is incompatible: manifest_hash, task_manifest_hash",
    ):
        current._import_c1_source_trials()


def test_source_import_rejects_unregistered_git_and_harness_rebind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, index_path, manifest = _build_source(tmp_path, monkeypatch)
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["git_commit"] = "b2da5cdc438b5412cee2646358a0881e96ed42fc"
    payload["harness_hash"] = (
        "749668b812dac6bde95042a71a4573befb4aeb02e4e68f8d29bb96efe77570fe"
    )
    payload["run_id"] = "p5-3905122be605"
    for gate_name in ("preflight", "calibration"):
        payload[gate_name]["git_commit"] = payload["git_commit"]
        payload[gate_name]["harness_hash"] = payload["harness_hash"]
    index_path.write_text(json.dumps(payload), encoding="utf-8")

    migration_path = tmp_path / "migration.json"
    current = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=tmp_path / "writer-alias-recovery",
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
        c1_source_contract_migration=migration_path,
    )
    current.git_commit = "current"
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    for gate in (current.preflight_metadata, current.calibration_metadata):
        gate["git_commit"] = "current"
        gate["harness_hash"] = "current-harness"
    current._register_c1_trial_matrix(["c1_state"])
    monkeypatch.setattr(
        current,
        "_freeze_c1_harness_hash",
        lambda: "current-harness",
    )
    source_raw = index_path.read_bytes()
    migration_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "c1_source_contract_migration",
                "source_index": str(index_path.resolve()),
                "source_index_sha256": hashlib.sha256(source_raw).hexdigest(),
                "source_run_id": payload["run_id"],
                "source_manifest_hash": payload["manifest_hash"],
                "source_harness_hash": payload["harness_hash"],
                "current_manifest_hash": current.manifest_hash,
                "current_harness_hash": "current-harness",
                "allowed_migrations": [
                    "calibration_rebind",
                    "git_commit_rebind",
                    "harness_rebind",
                    "preflight_rebind",
                ],
                "replaceable_source_statuses": [
                    "pending",
                    "quarantined_unknown_usage",
                    "exact_usage_task_timeout",
                    "exact_usage_retryable_infrastructure",
                    "registered_exact_usage_harness_failure",
                ],
                "migration_id": "libreoffice_writer_default_style_alias_v1",
                "source_git_commit": payload["git_commit"],
                    "affected_trials": [],
                    "preserved_model_failure_trials": [],
                "reason": "test-only registered harness migration",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="C1 harness migration does not match",
    ):
        current._import_c1_source_trials()


def test_registered_openra_migration_dispatches_exact_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = {"run_id": "source-run"}
    migration = {"migration_id": "c1_openra_readonly_render_grade_v1"}
    observed: dict[str, object] = {}

    def validate(**kwargs: object) -> None:
        observed.update(kwargs)

    monkeypatch.setattr(
        p5_scale,
        "_validate_registered_c1_openra_render_grade_migration",
        validate,
    )

    p5_scale._validate_registered_c1_harness_migration(
        base=tmp_path,
        source=source,
        migration=migration,
    )

    assert observed == {
        "base": tmp_path,
        "source": source,
        "migration": migration,
    }


def test_source_import_rejects_gate_only_rebind_without_registered_harness_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, index_path, manifest = _build_source(tmp_path, monkeypatch)
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["preflight"]["git_commit"] = "different-gate-commit"
    index_path.write_text(json.dumps(payload), encoding="utf-8")

    migration_path = tmp_path / "gate-only-migration.json"
    current = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=tmp_path / "gate-only-recovery",
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
        c1_source_contract_migration=migration_path,
    )
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("1")
    current._register_c1_trial_matrix(["c1_state"])
    source_raw = index_path.read_bytes()
    migration_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "c1_source_contract_migration",
                "source_index": str(index_path.resolve()),
                "source_index_sha256": hashlib.sha256(source_raw).hexdigest(),
                "source_run_id": payload["run_id"],
                "source_manifest_hash": payload["manifest_hash"],
                "source_harness_hash": payload["harness_hash"],
                "current_manifest_hash": current.manifest_hash,
                "current_harness_hash": "harness",
                "allowed_migrations": ["preflight_rebind"],
                "replaceable_source_statuses": [
                    "pending",
                    "quarantined_unknown_usage",
                    "exact_usage_task_timeout",
                    "exact_usage_retryable_infrastructure",
                ],
                "reason": "purported gate-only migration",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="preflight scientific contract differs",
    ):
        current._import_c1_source_trials()


def test_source_model_failure_classifier_preserves_exact_nonretryable_usage() -> None:
    report = {
        "complete": False,
        "retryable_incomplete": False,
    }
    result = {
        "incomplete": True,
        "retryable_incomplete": False,
        "infrastructure_error": False,
        "timed_out": True,
        "reason": "registered task deadline",
        "cost": _exact_cost(),
    }
    assert (
        p5_scale.P5Orchestrator._registered_source_model_failure_kind(
            result=result,
            report=report,
        )
        == "exact_usage_nonretryable_task_timeout"
    )
    result["infrastructure_error"] = True
    assert (
        p5_scale.P5Orchestrator._registered_source_model_failure_kind(
            result=result,
            report=report,
        )
        is None
    )
    result.pop("infrastructure_error")
    assert (
        p5_scale.P5Orchestrator._registered_source_model_failure_kind(
            result=result,
            report=report,
        )
        is None
    )


def test_resume_accepts_imported_exact_usage_model_failure_as_settled(
    tmp_path: Path,
) -> None:
    orchestrator = p5_scale.P5Orchestrator(
        tmp_path,
        _manifest(tmp_path),
        run_dir=tmp_path / "run",
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        resume=False,
    )
    orchestrator.resume = True
    report_path = tmp_path / "preserved-failure.json"
    report = {
        "complete": False,
        "retryable_incomplete": False,
        "results": [
            {
                "task_id": "t1",
                "incomplete": True,
                "retryable_incomplete": False,
                "infrastructure_error": False,
                "timed_out": True,
                "reason": "registered task deadline",
                "cost": _exact_cost(),
            }
        ],
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    orchestrator.index["c1_state"] = [
        {
            "model": "m",
            "seed": 0,
            "task_id": "t1",
            "status": "ok",
            "report": str(report_path),
            "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            "imported": True,
            "source_provenance": {
                "source_status": "error",
                "completion_class": "exact_usage_nonretryable_task_timeout",
                "source_sidecar_sha256": "a" * 64,
            },
        }
    ]

    assert orchestrator._completed(
        "c1_state",
        model="m",
        seed=0,
        task_id="t1",
    )

    report["results"][0]["infrastructure_error"] = True
    report_path.write_text(json.dumps(report), encoding="utf-8")
    orchestrator.index["c1_state"][0]["report_sha256"] = hashlib.sha256(
        report_path.read_bytes()
    ).hexdigest()
    assert not orchestrator._completed(
        "c1_state",
        model="m",
        seed=0,
        task_id="t1",
    )


def test_recovery_gate_rejects_replacing_a_completed_source_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, index_path, manifest = _build_source(tmp_path, monkeypatch)
    current = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=tmp_path / "recovery",
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
    )
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    current._register_c1_trial_matrix(["c1_state"])
    current._import_c1_source_trials()
    index = current.write_index()
    bad_request = copy.deepcopy(index)
    bad_request["c1_source_index_request"] = str(tmp_path / "different-source.json")
    assert any(
        "source request does not bind" in issue
        for issue in p5_aggregate._c1_trial_recovery_issues(bad_request)
    )
    completed = next(
        row for row in index["arms"]["c1_state"] if row["task_id"] == "t1"
    )
    completed["imported"] = False
    completed["replacement_for"] = {"fabricated": True}
    issues = p5_aggregate._c1_trial_recovery_issues(index)
    assert any("not imported immutably" in issue for issue in issues)


def test_recovery_aggregate_rejects_task_timeout_contract_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, index_path, manifest = _build_source(tmp_path, monkeypatch)
    current = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=tmp_path / "recovery-timeout-contract",
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
    )
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    current._register_c1_trial_matrix(["c1_state"])
    current._import_c1_source_trials()
    index = current.write_index()
    index["c1_task_timeout_s"] = 1300

    assert any(
        "scientific contract differs: c1_task_timeout_s" in issue
        for issue in p5_aggregate._c1_trial_recovery_issues(index)
    )


def test_recovery_aggregate_rejects_invalid_source_path_without_raising() -> None:
    index = {
        "c1_trial_persistence": copy.deepcopy(POLICY),
        "arms_run": ["c1_state"],
        "arms": {"c1_state": []},
        "c1_sources": [
            {
                "source_index": "bad\x00path",
                "source_index_sha256": "0" * 64,
            }
        ],
        "c1_source_index_request": "bad\x00path",
    }
    assert p5_aggregate._c1_trial_recovery_issues(index) == [
        "C1 recovery source path is invalid"
    ]


def test_aggregate_treats_c1_report_directory_as_failure_not_exception(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "report-directory"
    report_dir.mkdir()
    run_index = {
        "run_id": "r",
        "c1_trial_persistence": copy.deepcopy(POLICY),
        "c1_source_index_request": None,
        "c1_sources": [],
        "c1_operational_overhead": [],
        "arms_run": ["c1_state"],
        "arms": {
            "c1_state": [
                {
                    "model": "m",
                    "seed": 0,
                    "task_id": "t",
                    "trial_attempt": 0,
                    "attempt_id": "r:c1_state:m:0:t:attempt0",
                    "status": "ok",
                    "report": str(report_dir),
                    "report_sha256": "0" * 64,
                    "replacement_for": None,
                    "source_provenance": None,
                }
            ]
        },
    }
    summary = p5_aggregate.aggregate(run_index, report_root=tmp_path, iters=2)
    assert summary["complete"] is False
    assert "paper_ready" not in summary
    assert any(
        "report file missing or not a regular file" in reason
        for reason in summary["c1_cost_accuracy"]["incomplete_reasons"]
    )


def test_aggregate_treats_non_path_c1_report_as_failure_not_exception(
    tmp_path: Path,
) -> None:
    run_index = {
        "run_id": "r",
        "c1_trial_persistence": copy.deepcopy(POLICY),
        "c1_source_index_request": None,
        "c1_sources": [],
        "c1_operational_overhead": [],
        "arms_run": ["c1_state"],
        "arms": {
            "c1_state": [
                {
                    "model": "m",
                    "seed": 0,
                    "task_id": "t",
                    "trial_attempt": 0,
                    "attempt_id": "r:c1_state:m:0:t:attempt0",
                    "status": "ok",
                    "report": {"bad": "path"},
                    "report_sha256": "0" * 64,
                    "replacement_for": None,
                    "source_provenance": None,
                }
            ]
        },
    }
    summary = p5_aggregate.aggregate(run_index, report_root=tmp_path, iters=2)
    assert summary["complete"] is False
    assert "paper_ready" not in summary
    assert any(
        "invalid report path" in reason
        for reason in summary["c1_cost_accuracy"]["incomplete_reasons"]
    )


def test_aggregate_includes_local_settled_terminal_model_failure(
    tmp_path: Path,
) -> None:
    task_id = "t"
    result = _settled_terminal_result(task_id)
    report = {
        "schema_version": 4,
        "complete": False,
        "retryable_incomplete": False,
        "incomplete_category": "nonretryable_incomplete_evidence",
        "model": "m",
        "seed": 0,
        "results": [result],
        "run_context": {"model": "m", "seed": 0, "arm": "c1_state"},
    }
    report_path = tmp_path / "terminal-state-loop.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    run_index = {
        "run_id": "recovery",
        "c1_trial_persistence": copy.deepcopy(POLICY),
        "c1_source_index_request": "source/p5-run-index.json",
        "c1_sources": [],
        "c1_operational_overhead": [],
        "arms_run": ["c1_state"],
        "arms": {
            "c1_state": [
                {
                    "model": "m",
                    "seed": 0,
                    "task_id": task_id,
                    "trial_attempt": 0,
                    "attempt_id": "recovery:c1_state:m:0:t:attempt0",
                    "status": "error",
                    "report": str(report_path),
                    "report_sha256": hashlib.sha256(
                        report_path.read_bytes()
                    ).hexdigest(),
                    "replacement_for": {"source_status": "pending"},
                    "source_provenance": None,
                    "imported": False,
                }
            ]
        },
    }

    summary = p5_aggregate.aggregate(run_index, report_root=tmp_path, iters=2)

    terminal_trials = [
        trial
        for trial in summary["c1_cost_accuracy"]["trials"]
        if trial.get("task_id") == task_id
    ]
    assert len(terminal_trials) == 1
    assert terminal_trials[0]["attempt_costs"][0]["attempt_status"] == (
        "nonretryable_incomplete"
    )
    assert not any(
        "run status error" in reason
        for reason in summary["c1_cost_accuracy"]["incomplete_reasons"]
    )


def test_exact_shakeout_is_luanti_first_but_formal_order_is_untouched() -> None:
    manifest = {
        "arms": {},
        "shakeout": {
            "task_ids": ["a", "b", "c", "luanti"],
            "execution_order": ["luanti", "a", "b", "c"],
        },
    }
    assert p5_scale._ordered_c1_task_ids(
        manifest, ["a", "b", "c", "luanti"]
    ) == ["luanti", "a", "b", "c"]
    assert p5_scale._ordered_c1_task_ids(manifest, None) is None
    assert p5_scale._ordered_c1_task_ids(manifest, ["a", "b"]) == ["a", "b"]


def test_formal_manifest_registers_separate_fixed_restore_diagnostic_sample() -> None:
    manifest = yaml.safe_load(
        (Path(__file__).parents[1] / "benchmarks/p5/manifest.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["arms"]["c1_state"]["restore_diagnostics"] is False
    assert manifest["c1_restore_diagnostic_sample"] == {
        "task_ids": [
            "2048-merge-left",
            "love-office-work-coding",
            "spacerpg-station-save",
            "luanti-set-node",
        ],
        "excluded_from_formal": True,
        "reporting": "separate_nonpaper",
    }


def test_recovery_resume_rebuilds_replacement_map_and_rechecks_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, index_path, manifest = _build_source(tmp_path, monkeypatch)
    recovery_dir = tmp_path / "recovery"
    current = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=recovery_dir,
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
    )
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    current._register_c1_trial_matrix(["c1_state"])
    current._import_c1_source_trials()
    current.write_index()

    resumed = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=recovery_dir,
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
        resume=True,
    )
    resumed._register_c1_trial_matrix(["c1_state"])
    resumed._import_c1_source_trials()
    assert set(key[3] for key in resumed._c1_replacement_sources) == {"t2", "t3"}
    assert len(resumed.c1_operational_overhead) == 1

    source_raw = index_path.read_bytes()
    index_path.write_bytes(source_raw + b"\n")
    second_resume = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=recovery_dir,
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
        resume=True,
    )
    second_resume._register_c1_trial_matrix(["c1_state"])
    with pytest.raises(ValueError, match="stored provenance"):
        second_resume._import_c1_source_trials()


def _fair_zero_trial(status: str) -> dict:
    restore_failed = status == "restore_failed"
    return {
        "arm": "c1_gui_synth",
        "model": "m",
        "task_id": "t",
        "seed": 0,
        "benchmark": "2048",
        "start_construction_status": status,
        "incomplete": False,
        "start_gate_status": "not_evaluated_start_construction_failed",
        "goal_evaluation_status": "not_evaluated_start_construction_failed",
        "visual_only_evaluation_status": "not_evaluated_start_construction_failed",
        "result_status": "failure",
        "raw_result": False,
        "policy_phase_started": False,
        "start_grading_started": False,
        "visual_grading_started": False,
        "before_visual_ok": False,
        "before_semantic_ok": False,
        "after_visual_ok": False,
        "after_semantic_ok": False,
        "start_success": False,
        "goal_success": False,
        "e2e_success": False,
        "steps": {"phase_a": 0, "phase_b": 0, "total": 0},
        "time_start_to_goal_ms": 0,
        "pair_verdict_present": False,
        "screenshot_after": None,
        "phase_b_actions": [],
        "after_semantic_detail": None,
        "before_visual_status": "not_evaluated",
        "after_visual_status": "not_evaluated",
        "before_semantic_status": "not_evaluated",
        "after_semantic_status": "not_evaluated",
        "start_grading_runtime_hold": None,
        "runtime_started": restore_failed,
        "prng": {"algorithm": "mulberry32", "seed": 0} if restore_failed else None,
        "seed_evidence": (
            {"observed": True, "source": "browser_page_runtime"}
            if restore_failed
            else {"observed": False, "source": "runtime_not_started"}
        ),
        "checkpoint_restore_ms": 4 if restore_failed else 0,
    }


def test_formal_fair_prestart_zero_is_tightly_scoped() -> None:
    for status in ("synthesis_failed", "restore_failed"):
        trial = _fair_zero_trial(status)
        assert p5_aggregate._c1_formal_start_gate_issues(trial) == []
        bad = copy.deepcopy(trial)
        bad["start_grading_started"] = True
        assert p5_aggregate._c1_formal_start_gate_issues(bad)
        bad_actions = copy.deepcopy(trial)
        bad_actions["phase_b_actions"] = [{"kind": "click"}]
        assert any(
            "phase-B actions" in issue
            for issue in p5_aggregate._c1_formal_start_gate_issues(bad_actions)
        )
    pending = _fair_zero_trial("synthesis_failed")
    pending["start_construction_status"] = "pending"
    assert any(
        "nonterminal" in issue
        for issue in p5_aggregate._c1_formal_start_gate_issues(pending)
    )


def test_restore_failed_fair_zero_keeps_restore_cost_outside_policy_time() -> None:
    result = {
        "task_id": "t",
        "benchmark": "PAL3",
        "status": "failure",
        "result": False,
        "start_construction_status": "restore_failed",
        **{
            field: False
            for field in p5_aggregate._GUI_V2_BOOL_FIELDS
        },
        "time_to_start_ms": 0,
        "time_start_to_goal_ms": 0,
        "time_e2e_excluding_synthesis_ms": 0,
        "time_e2e_including_synthesis_ms": 10,
        "checkpoint_synthesis_ms": 10,
        "checkpoint_restore_ms": 4,
        "runtime_bootstrap_ms": 1,
        "grader_ms": 0,
        "termination_time_ms": 10,
    }

    _trial, issues = p5_aggregate._normalize_c1_trial(
        "c1_agent_s_synth",
        {"run_context": {"model": "m", "seed": 0}},
        result,
    )

    assert "checkpoint_restore_ms exceeds time_to_start_ms" not in issues


def test_recovery_calibration_contract_ignores_fresh_backend_receipts() -> None:
    common = {
        "schema_version": 4,
        "passed": True,
        "git_commit": "git",
        "manifest_hash": "manifest",
        "harness_hash": "harness",
        "c1_application_revisions": {"openra": {"revision": "revision"}},
        "c1_environment": {"status": "matched"},
        "api_provider": {"name": "provider"},
        "provider_timeout_s": 900,
        "observer_model": "observer",
        "grounder_model": "grounder",
    }
    first = {**common, "backends": {"openra": {"session_id": "first"}}}
    second = {**common, "backends": {"openra": {"session_id": "second"}}}

    assert p5_aggregate._recovery_calibration_contract(
        first
    ) == p5_aggregate._recovery_calibration_contract(second)


@pytest.mark.parametrize(
    ("migration_id", "validator_name"),
    [
        ("c1_retry_task_plan_resume_v1", "_registered_retry_task_plan_migration"),
        (
            "c1_recovery_parallel_scheduler_v1",
            "_registered_recovery_parallel_migration",
        ),
    ],
)
def test_flattened_recovery_reaudits_intermediate_registered_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migration_id: str,
    validator_name: str,
) -> None:
    upstream = {"run_id": "upstream-run"}
    upstream_path = tmp_path / "upstream.json"
    upstream_path.write_text(json.dumps(upstream), encoding="utf-8")
    upstream_sha = hashlib.sha256(upstream_path.read_bytes()).hexdigest()
    source_meta = {
        "source_index": str(upstream_path),
        "source_index_sha256": upstream_sha,
        "source_run_id": upstream["run_id"],
        "contract_migration": {"migration_id": migration_id},
    }
    derived = {
        "run_id": "derived-run",
        "c1_sources": [source_meta],
        "c1_source_contract_migration": source_meta["contract_migration"],
    }
    derived_path = tmp_path / "derived.json"
    derived_path.write_text(json.dumps(derived), encoding="utf-8")
    derived_sha = hashlib.sha256(derived_path.read_bytes()).hexdigest()
    flattened = copy.deepcopy(derived)
    flattened.pop("c1_sources")
    flattened["flattened_source_lineage"] = {
        "schema_version": 1,
        "kind": "c1_flattened_recovery_source",
        "derived_from_index": str(derived_path),
        "derived_from_index_sha256": derived_sha,
        "upstream_sources": [source_meta],
    }
    flattened_path = tmp_path / "flattened.json"
    flattened_path.write_text(json.dumps(flattened), encoding="utf-8")
    observed: list[dict[str, object]] = []

    def validate(**kwargs: object) -> tuple[set[tuple[str, object, object, str]], list[str]]:
        observed.append(dict(kwargs))
        return set(), []

    monkeypatch.setattr(
        p5_aggregate,
        validator_name,
        validate,
    )

    p5_aggregate._c1_flattened_trial_recovery_issues(
        run_index={},
        source=flattened,
        source_path=flattened_path,
        source_sha=hashlib.sha256(flattened_path.read_bytes()).hexdigest(),
        source_meta={},
        current_rows={},
        arms=[],
    )

    assert {
        "run_index": derived,
        "source": upstream,
        "source_path": upstream_path,
        "source_sha": upstream_sha,
        "source_meta": source_meta,
        **(
            {"repository_root": None}
            if migration_id == "c1_recovery_parallel_scheduler_v1"
            else {}
        ),
    } in observed


def test_current_seed0_settled_failure_profile_is_registered(tmp_path: Path) -> None:
    source = {
        "run_id": "p5-f5a2dcca1df7",
        "git_commit": "3719208e1b2cd755114fc005752fc5225aeded4d",
        "harness_hash": (
            "a141e52b0d7b08499b477b016d26dfd89e8d896862df8f3279044aad57e8d732"
        ),
    }
    migration = {
        "migration_id": "c1_resume_settled_failure_v1",
        "source_run_id": source["run_id"],
        "source_git_commit": source["git_commit"],
        "source_harness_hash": source["harness_hash"],
        "affected_trials": [],
        "preserved_model_failure_trials": [
            {
                "arm": "c1_state",
                "model": "gemini-3.5-flash@relay",
                "seed": 0,
                "task_id": "luanti-replace-node",
                "source_status": "error",
            }
        ],
        "recovery_index": str(tmp_path / "missing-recovery-index.json"),
    }

    with pytest.raises(ValueError, match="source index is missing"):
        p5_scale._validate_registered_c1_resume_settled_failure_migration(
            base=tmp_path,
            source=source,
            migration=migration,
        )


def test_current_seed0_settled_failure_migration_is_aggregator_registered(
    tmp_path: Path,
) -> None:
    arms = [
        "c1_agent_s",
        "c1_agent_s_synth",
        "c1_state",
        "c1_state_minimal_agent",
    ]
    rows_by_arm: dict[str, list[dict[str, object]]] = {arm: [] for arm in arms}
    remaining_ok = 512
    for arm in arms:
        for ordinal in range(183):
            task_id = f"{arm}-{ordinal}"
            status = "pending"
            if arm == "c1_state" and ordinal == 0:
                task_id = "luanti-replace-node"
                status = "error"
            elif remaining_ok:
                status = "ok"
                remaining_ok -= 1
            rows_by_arm[arm].append(
                {
                    "model": "gemini-3.5-flash@relay",
                    "seed": 0,
                    "task_id": task_id,
                    "status": status,
                }
            )
    upstream_path = tmp_path / "initial-index.json"
    upstream_path.write_text(json.dumps({"run_id": "initial-run"}), encoding="utf-8")
    upstream = {
        "source_index": str(upstream_path),
        "source_index_sha256": hashlib.sha256(upstream_path.read_bytes()).hexdigest(),
        "source_run_id": "initial-run",
    }
    recovery = {
        "run_id": "p5-f5a2dcca1df7",
        "git_commit": "3719208e1b2cd755114fc005752fc5225aeded4d",
        "harness_hash": (
            "a141e52b0d7b08499b477b016d26dfd89e8d896862df8f3279044aad57e8d732"
        ),
        "manifest_hash": "manifest",
        "arms_run": arms,
        "arms": rows_by_arm,
        "c1_sources": [upstream],
    }
    recovery_path = tmp_path / "recovery-index.json"
    recovery_path.write_text(json.dumps(recovery), encoding="utf-8")
    recovery_sha = hashlib.sha256(recovery_path.read_bytes()).hexdigest()
    source = copy.deepcopy(recovery)
    source.pop("c1_sources")
    source["flattened_source_lineage"] = {
        "schema_version": 1,
        "kind": "c1_flattened_recovery_source",
        "derived_from_index": str(recovery_path),
        "derived_from_index_sha256": recovery_sha,
        "upstream_sources": [upstream],
    }
    source_path = tmp_path / "flattened-source.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    changed_paths = [
        "tests/test_c1_trial_persistence.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
    ]
    preserved = [
        {
            "arm": "c1_state",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": "luanti-replace-node",
            "source_status": "error",
        }
    ]
    migration_payload = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": "c1_resume_settled_failure_v1",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source["run_id"],
        "source_manifest_hash": source["manifest_hash"],
        "source_harness_hash": source["harness_hash"],
        "source_git_commit": source["git_commit"],
        "current_manifest_hash": "manifest",
        "current_harness_hash": "current-harness",
        "current_git_commit": "current-git",
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "recovery_scheduler_policy": "preserve_settled_exact_usage_failure_v1",
        "affected_trials": [],
        "preserved_model_failure_trials": preserved,
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "treatment_semantics_unchanged": True,
        "changed_paths": changed_paths,
        "changed_paths_sha256": hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "reason": "preserve one exact-usage settled failure and resume pending trials",
        "recovery_index": str(recovery_path),
        "recovery_index_sha256": recovery_sha,
        "recovery_run_id": recovery["run_id"],
        "recovery_git_commit": recovery["git_commit"],
        "recovery_harness_hash": recovery["harness_hash"],
        "upstream_source_index": upstream["source_index"],
        "upstream_source_index_sha256": upstream["source_index_sha256"],
        "upstream_source_run_id": upstream["source_run_id"],
    }
    artifact_path = tmp_path / "migration.json"
    artifact_path.write_text(json.dumps(migration_payload), encoding="utf-8")
    migration = {
        **migration_payload,
        "artifact": str(artifact_path.resolve()),
        "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
    }
    run_index = {
        "manifest_hash": "manifest",
        "harness_hash": "current-harness",
        "git_commit": "current-git",
        "arms_run": arms,
        "c1_source_contract_migration": migration,
        "c1_source_contract_migration_request": str(artifact_path.resolve()),
    }
    terminal_keys, issues = (
        p5_aggregate._registered_resume_settled_failure_migration(
            run_index=run_index,
            source=source,
            source_path=source_path,
            source_sha=source_sha,
            source_meta={"contract_migration": migration},
        )
    )

    assert issues == []
    assert terminal_keys == {
        ("c1_state", "gemini-3.5-flash@relay", 0, "luanti-replace-node")
    }


def test_registered_parallel_recovery_migration_validates_exact_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = {
        "run_id": "source-run",
        "git_commit": "source-git",
        "manifest_hash": "manifest",
        "harness_hash": "source-harness",
        "arms_run": ["c1_state"],
        "arms": {
            "c1_state": [
                {
                    "model": "m",
                    "seed": 0,
                    "task_id": "done",
                    "status": "ok",
                },
                {
                    "model": "m",
                    "seed": 0,
                    "task_id": "pending",
                    "status": "pending",
                },
            ]
        },
        "c1_trial_persistence": {
            "recovery_execution": "one_task_fresh_invocation_v1"
        },
    }
    source_path = tmp_path / "source.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    changed_paths = sorted(
        [
            "tests/test_c1_concurrency.py",
            "tests/test_c1_trial_persistence.py",
            "tests/test_p5.py",
            "warptest/c1_concurrency.py",
            "warptest/gui_agent.py",
            "warptest/p5_aggregate.py",
            "warptest/p5_scale.py",
            "warptest/state_loop.py",
        ]
    )
    artifact_payload = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": "c1_recovery_parallel_scheduler_v1",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": "source-run",
        "source_manifest_hash": "manifest",
        "source_harness_hash": "source-harness",
        "source_git_commit": "source-git",
        "current_manifest_hash": "manifest",
        "current_harness_hash": "current-harness",
        "current_git_commit": "current-git",
        "allowed_migrations": ["git_commit_rebind", "harness_rebind"],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "scheduler_policy": "global_resource_ready_round_robin_v2",
        "recovery_execution": "one_task_fresh_invocation_v1",
        "recovery_dispatch": "two_level_resource_ready_v1",
        "affected_trials": [
            {
                "arm": "c1_state",
                "model": "m",
                "seed": 0,
                "task_id": "pending",
                "source_status": "pending",
            }
        ],
        "preserved_model_failure_trials": [],
        "registered_harness_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "normal_execution_unchanged": True,
        "treatment_semantics_unchanged": True,
        "changed_paths": changed_paths,
        "changed_paths_sha256": hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "reason": "parallelize only recovery dispatch",
    }
    artifact_path = tmp_path / "migration.json"
    artifact_path.write_text(json.dumps(artifact_payload), encoding="utf-8")
    migration = {
        **artifact_payload,
        "artifact": str(artifact_path.resolve()),
        "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
    }
    run_index = {
        "manifest_hash": "manifest",
        "harness_hash": "current-harness",
        "git_commit": "current-git",
        "c1_source_contract_migration": migration,
        "c1_source_contract_migration_request": str(artifact_path),
    }
    source_meta = {
        "source_index": str(source_path),
        "source_index_sha256": source_sha,
        "source_run_id": "source-run",
        "contract_migration": migration,
    }
    monkeypatch.setattr(
        p5_aggregate,
        "_repository_changed_paths",
        lambda *_args: changed_paths,
    )

    affected, issues = p5_aggregate._registered_recovery_parallel_migration(
        run_index=run_index,
        source=source,
        source_path=source_path,
        source_sha=source_sha,
        source_meta=source_meta,
    )
    assert issues == []
    assert affected == {("c1_state", "m", 0, "pending")}

    tampered = copy.deepcopy(run_index)
    tampered["c1_source_contract_migration"]["source_index_sha256"] = "0" * 64
    _, issues = p5_aggregate._registered_recovery_parallel_migration(
        run_index=tampered,
        source=source,
        source_path=source_path,
        source_sha=source_sha,
        source_meta=source_meta,
    )
    assert "C1 recovery-parallel migration metadata is invalid" in issues

    extra_path = copy.deepcopy(run_index)
    extra_path["c1_source_contract_migration"]["changed_paths"].append(
        "warptest/treatment.py"
    )
    _, issues = p5_aggregate._registered_recovery_parallel_migration(
        run_index=extra_path,
        source=source,
        source_path=source_path,
        source_sha=source_sha,
        source_meta=source_meta,
    )
    assert "C1 recovery-parallel migration changed paths are invalid" in issues

    monkeypatch.setattr(
        p5_aggregate,
        "_repository_changed_paths",
        lambda *_args: [*changed_paths, "README.md"],
    )
    _, issues = p5_aggregate._registered_recovery_parallel_migration(
        run_index=run_index,
        source=source,
        source_path=source_path,
        source_sha=source_sha,
        source_meta=source_meta,
    )
    assert "C1 recovery-parallel migration changed paths are invalid" in issues
    monkeypatch.setattr(
        p5_aggregate,
        "_repository_changed_paths",
        lambda *_args: changed_paths,
    )

    invalid_error_source = copy.deepcopy(source)
    invalid_error_source["arms"]["c1_state"][1]["status"] = "error"
    invalid_error_migration = copy.deepcopy(run_index)
    invalid_payload = invalid_error_migration["c1_source_contract_migration"]
    invalid_payload["affected_trials"][0]["source_status"] = "error"
    _, issues = p5_aggregate._registered_recovery_parallel_migration(
        run_index=invalid_error_migration,
        source=invalid_error_source,
        source_path=source_path,
        source_sha=source_sha,
        source_meta=source_meta,
    )
    assert "C1 recovery-parallel migration contains an unregistered source error" in issues


def test_repository_changed_paths_uses_exact_commit_range(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Warptest"],
        cwd=repository,
        check=True,
    )
    tracked = repository / "tracked.txt"
    tracked.write_text("first\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=repository, check=True)
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tracked.write_text("second\n", encoding="utf-8")
    untracked = repository / "untracked.txt"
    untracked.write_text("not committed\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=repository, check=True)
    current_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert p5_aggregate._repository_changed_paths(
        repository,
        source_commit,
        current_commit,
    ) == ["tracked.txt"]


def test_parallel_recovery_import_validator_rejects_unregistered_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = {
        "git_commit": "source-git",
        "arms_run": ["c1_state"],
        "arms": {
            "c1_state": [
                {
                    "model": "m",
                    "seed": 0,
                    "task_id": "pending",
                    "status": "pending",
                }
            ]
        },
        "c1_trial_persistence": {
            "recovery_execution": "one_task_fresh_invocation_v1"
        },
    }
    required_paths = sorted(
        [
            "warptest/c1_concurrency.py",
            "warptest/gui_agent.py",
            "warptest/p5_scale.py",
            "warptest/state_loop.py",
        ]
    )

    def migration(paths: list[str]) -> dict[str, object]:
        return {
            "migration_id": "c1_recovery_parallel_scheduler_v1",
            "source_git_commit": "source-git",
            "current_git_commit": "current-git",
            "scheduler_policy": "global_resource_ready_round_robin_v2",
            "recovery_execution": "one_task_fresh_invocation_v1",
            "recovery_dispatch": "two_level_resource_ready_v1",
            "affected_trials": [
                {
                    "arm": "c1_state",
                    "model": "m",
                    "seed": 0,
                    "task_id": "pending",
                    "source_status": "pending",
                }
            ],
            "preserved_model_failure_trials": [],
            "registered_harness_failure_trials": [],
            "completed_trial_import_only": True,
            "unfinished_tuples_requeued": True,
            "normal_execution_unchanged": True,
            "treatment_semantics_unchanged": True,
            "changed_paths": paths,
            "changed_paths_sha256": hashlib.sha256(
                json.dumps(paths, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }

    monkeypatch.setattr(p5_scale.run_context, "git_commit", lambda _base: "current-git")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="\n".join(required_paths) + "\n"
        ),
    )
    p5_scale._validate_registered_c1_harness_migration(
        base=tmp_path,
        source=source,
        migration=migration(required_paths),
    )

    unregistered = sorted([*required_paths, "warptest/treatment.py"])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="\n".join(unregistered) + "\n"
        ),
    )
    with pytest.raises(ValueError, match="unregistered changed paths"):
        p5_scale._validate_registered_c1_harness_migration(
            base=tmp_path,
            source=source,
            migration=migration(unregistered),
        )


def test_registered_retry_migration_validates_artifact_and_requeued_tuples(
    tmp_path: Path,
) -> None:
    source_run_id = "p5-d65ab3c4aa7c"
    current_run_id = "retry-run"
    model = "gpt-5.6-terra@relay"
    affected = [
        ("c1_agent_s", "jynew-save-restore"),
        ("c1_agent_s_synth", "luanti-dig-node"),
    ]
    source_arms: dict[str, list[dict[str, object]]] = {
        "c1_agent_s": [],
        "c1_agent_s_synth": [],
        "c1_state": [],
    }
    current_arms: dict[str, list[dict[str, object]]] = {
        arm: [] for arm in source_arms
    }
    error_reports: dict[tuple[str, str], tuple[Path, str]] = {}
    for arm, task_id in affected:
        report_path = tmp_path / f"{task_id}.json"
        report_path.write_text(
            json.dumps(
                {
                    "complete": False,
                    "results": [
                        {
                            "task_id": task_id,
                            "incomplete": True,
                            "cost": _exact_cost(),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        error_reports[(arm, task_id)] = (
            report_path,
            hashlib.sha256(report_path.read_bytes()).hexdigest(),
        )

    for arm_index, arm in enumerate(source_arms):
        ok_count = 19
        pending_count = 42 if arm == "c1_state" else 41
        affected_task = next(
            (task_id for affected_arm, task_id in affected if affected_arm == arm),
            None,
        )
        if affected_task is not None:
            report_path, report_sha = error_reports[(arm, affected_task)]
            source_row = {
                "model": model,
                "seed": 0,
                "task_id": affected_task,
                "status": "error",
                "trial_attempt": 0,
                "attempt_id": (
                    f"{source_run_id}:{arm}:{model}:0:{affected_task}:attempt0"
                ),
                "report": str(report_path),
                "report_sha256": report_sha,
            }
            source_arms[arm].append(source_row)
        for index in range(ok_count):
            task_id = f"{arm_index}-ok-{index}"
            source_row = {
                "model": model,
                "seed": 0,
                "task_id": task_id,
                "status": "ok",
                "trial_attempt": 0,
                "attempt_id": f"{source_run_id}:{arm}:{model}:0:{task_id}:attempt0",
                "report": f"/evidence/{task_id}.json",
                "report_sha256": f"{arm_index}{index:063d}"[-64:],
            }
            source_arms[arm].append(source_row)
        for index in range(pending_count):
            source_arms[arm].append(
                {
                    "model": model,
                    "seed": 0,
                    "task_id": f"{arm_index}-pending-{index}",
                    "status": "pending",
                }
            )

    source = {
        "run_id": source_run_id,
        "git_commit": "7bdf4d095d6809c03e50f11f77954c58a734d958",
        "manifest_hash": "manifest",
        "harness_hash": (
            "cf3fba87e98cc0a4513f7148162590f7e6df461a5454978ef808ef0a36c5e3ab"
        ),
        "arms": source_arms,
    }
    source_path = tmp_path / "retry-source.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    overhead: list[dict[str, object]] = []
    for arm, rows in source_arms.items():
        for source_row in rows:
            task_id = str(source_row["task_id"])
            if source_row["status"] == "ok":
                current_arms[arm].append(
                    {
                        **copy.deepcopy(source_row),
                        "imported": True,
                        "replacement_for": None,
                        "source_provenance": {
                            "source_index_sha256": source_sha,
                            "source_run_id": source_run_id,
                        },
                    }
                )
            elif source_row["status"] == "pending":
                current_arms[arm].append(copy.deepcopy(source_row))
            else:
                replacement = {
                    "source_index": str(source_path),
                    "source_index_sha256": source_sha,
                    "source_run_id": source_run_id,
                    "source_status": "error",
                    "source_report": source_row["report"],
                    "source_report_sha256": source_row["report_sha256"],
                }
                current_row = {
                    "model": model,
                    "seed": 0,
                    "task_id": task_id,
                    "status": "ok",
                    "trial_attempt": 0,
                    "attempt_id": (
                        f"{current_run_id}:{arm}:{model}:0:{task_id}:attempt0"
                    ),
                    "replacement_for": replacement,
                    "source_provenance": None,
                }
                current_arms[arm].append(current_row)
                overhead.append(
                    {
                        "arm": arm,
                        "model": model,
                        "seed": 0,
                        "task_id": task_id,
                        "status": "replaced_registered_exact_usage_harness_failure",
                        "replacement_for": replacement,
                        "source_provenance": {
                            "source_index_sha256": source_sha,
                        },
                    }
                )

    affected_metadata = [
        {
            "arm": arm,
            "model": model,
            "seed": 0,
            "task_id": task_id,
            "source_status": "error",
        }
        for arm, task_id in affected
    ]
    artifact_payload = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": "c1_retry_task_plan_resume_v1",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source_run_id,
        "source_manifest_hash": source["manifest_hash"],
        "source_harness_hash": source["harness_hash"],
        "source_git_commit": source["git_commit"],
        "current_manifest_hash": "manifest",
        "current_harness_hash": "current-harness",
        "current_git_commit": "current-git",
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "affected_trials": affected_metadata,
        "preserved_model_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "treatment_semantics_unchanged": True,
        "changed_paths": [
            "tests/test_c1_attempt_costs.py",
            "tests/test_c1_trial_persistence.py",
            "warptest/gui_agent.py",
            "warptest/p5_scale.py",
            "warptest/state_loop.py",
        ],
        "reason": "test exact retry-plan migration",
    }
    artifact_path = tmp_path / "retry-migration.json"
    artifact_path.write_text(json.dumps(artifact_payload), encoding="utf-8")
    migration = {
        **artifact_payload,
        "artifact": str(artifact_path.resolve()),
        "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
    }
    run_index = {
        "run_id": current_run_id,
        "git_commit": "current-git",
        "manifest_hash": "manifest",
        "harness_hash": "current-harness",
        "arms": current_arms,
        "c1_operational_overhead": overhead,
        "c1_source_contract_migration": migration,
        "c1_source_contract_migration_request": str(artifact_path),
    }
    source_meta = {"contract_migration": migration}

    affected_keys, issues = p5_aggregate._registered_retry_task_plan_migration(
        run_index=run_index,
        source=source,
        source_path=source_path,
        source_sha=source_sha,
        source_meta=source_meta,
    )

    assert issues == []
    assert affected_keys == {
        (item["arm"], item["model"], item["seed"], item["task_id"])
        for item in affected_metadata
    }
    tampered = copy.deepcopy(run_index)
    tampered["c1_source_contract_migration"]["changed_paths"] = []
    _, tampered_issues = p5_aggregate._registered_retry_task_plan_migration(
        run_index=tampered,
        source=source,
        source_path=source_path,
        source_sha=source_sha,
        source_meta=source_meta,
    )
    assert "C1 retry-plan migration metadata is invalid" in tampered_issues


def test_aggregate_accepts_exact_openra_render_grade_migration(
    tmp_path: Path,
) -> None:
    model = "gpt-5.6-terra@relay"
    affected = [
        {
            "arm": "c1_agent_s",
            "model": model,
            "seed": 0,
            "task_id": "openra-allied-combined-arms-rally",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s_synth",
            "model": model,
            "seed": 0,
            "task_id": "openra-soviet-rocket-infantry-chain",
            "source_status": "error",
        },
    ]

    def row(task_id: str, status: str) -> dict[str, object]:
        return {
            "model": model,
            "seed": 0,
            "task_id": task_id,
            "status": status,
        }

    source = {
        "run_id": "p5-e92822c0dfa1",
        "git_commit": "9b981e327ba001b422bc2381f68df3532ad841ce",
        "manifest_hash": "source-manifest",
        "harness_hash": (
            "7dd8db55c5f367367d62d6cd636e87d512d0cf7401d8854692302c20480fa0a6"
        ),
        "arms": {
            "c1_agent_s": [
                row("openra-allied-combined-arms-rally", "error"),
                *(row(f"c-ok-{index}", "ok") for index in range(60)),
            ],
            "c1_agent_s_synth": [
                row("openra-soviet-rocket-infantry-chain", "error"),
                *(row(f"d-ok-{index}", "ok") for index in range(60)),
            ],
            "c1_state": [
                *(row(f"e-ok-{index}", "ok") for index in range(41)),
                *(row(f"e-pending-{index}", "pending") for index in range(20)),
            ],
        },
    }
    source_path = tmp_path / "source.json"
    source_path.write_text(json.dumps(source, sort_keys=True), encoding="utf-8")
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    changed_paths = [
        "benchmarks/p5/c1_tasks.yaml",
        "games/OpenRA",
        "tests/test_c1_openra_runtime.py",
        "tests/test_c1_v5_catalog.py",
        "warptest/p5_scale.py",
    ]
    artifact_payload = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source["run_id"],
        "source_manifest_hash": source["manifest_hash"],
        "source_harness_hash": source["harness_hash"],
        "current_manifest_hash": "current-manifest",
        "current_harness_hash": "current-harness",
        "allowed_migrations": [
            "c1_application_revisions_rebind",
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
            "task_manifest_hash_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "migration_id": "c1_openra_readonly_render_grade_v1",
        "source_git_commit": source["git_commit"],
        "current_git_commit": "current-git",
        "changed_paths": changed_paths,
        "changed_paths_sha256": hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "affected_trials": affected,
        "preserved_model_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "treatment_semantics_unchanged": True,
        "reason": "test exact OpenRA render-grade migration",
    }
    artifact_path = tmp_path / "migration.json"
    artifact_path.write_text(
        json.dumps(artifact_payload, sort_keys=True),
        encoding="utf-8",
    )
    migration = {
        **artifact_payload,
        "artifact": str(artifact_path.resolve()),
        "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
    }
    run_index = {
        "git_commit": "current-git",
        "manifest_hash": "current-manifest",
        "harness_hash": "current-harness",
        "c1_source_contract_migration": migration,
        "c1_source_contract_migration_request": str(artifact_path),
    }

    affected_keys, issues = (
        p5_aggregate._registered_openra_render_grade_migration(
            run_index=run_index,
            source=source,
            source_path=source_path,
            source_sha=source_sha,
            source_meta={"contract_migration": migration},
        )
    )

    assert issues == []
    assert affected_keys == {
        (item["arm"], item["model"], item["seed"], item["task_id"])
        for item in affected
    }


def _love_before_detail(ok: bool) -> dict:
    return {
        "source": "love_production_save_roundtrip_v1",
        "checks": [{"ok": ok}],
        "network": {},
        "errors": {},
        "snapshot": {"prior_restored": True},
    }


def test_semantic_and_visual_start_failures_require_only_before_evidence() -> None:
    base = {
        "arm": "c1_gui_synth",
        "model": "m",
        "task_id": "love",
        "seed": 0,
        "benchmark": "DatingSim",
        "start_construction_status": "reached",
        "incomplete": False,
        "goal_evaluation_status": "not_evaluated_start_failed",
        "policy_phase_started": False,
        "start_grading_started": True,
        "start_success": False,
        "goal_success": False,
        "e2e_success": False,
        "steps": {"phase_a": 0, "phase_b": 0, "total": 0},
        "after_visual_status": "not_evaluated",
        "after_semantic_status": "not_evaluated",
        "after_semantic_detail": None,
        "policy_action_interface": "pixel_mouse_keyboard_only",
        "policy_received_semantic_probe": False,
        "semantic_probe_source": "love_production_save_roundtrip_v1",
    }
    semantic = {
        **base,
        "start_gate_status": "semantic_failed",
        "visual_grading_started": False,
        "before_visual_status": "not_evaluated_semantic_failed",
        "before_semantic_status": "validate_failed",
        "before_semantic_ok": False,
        "before_semantic_detail": _love_before_detail(False),
        "start_grading_runtime_hold": None,
    }
    assert p5_aggregate._c1_formal_start_gate_issues(semantic) == []
    assert p5_aggregate._c1_public_semantic_trial_issues(semantic) == []

    visual = {
        **base,
        "start_gate_status": "visual_failed",
        "visual_grading_started": True,
        "before_visual_status": "evaluated",
        "before_semantic_status": "success",
        "before_semantic_ok": True,
        "before_semantic_detail": _love_before_detail(True),
        "start_grading_runtime_hold": {
            "required": True,
            "held": True,
            "released": True,
            "backend": "chromium_cdp_script_pause",
        },
    }
    assert p5_aggregate._c1_formal_start_gate_issues(visual) == []
    assert p5_aggregate._c1_public_semantic_trial_issues(visual) == []
    visual["start_grading_runtime_hold"]["released"] = False
    assert any(
        "released" in issue
        for issue in p5_aggregate._c1_formal_start_gate_issues(visual)
    )


def test_formal_cost_gate_rejects_ghost_or_zero_call_stage_buckets() -> None:
    trial = {
        "arm": "c1_gui_synth",
        "model": "m",
        "task_id": "t",
        "seed": 0,
        "runtime_started": False,
        "policy_phase_started": False,
        "visual_grading_started": False,
        "start_gate_status": "semantic_failed",
        "goal_evaluation_status": "not_evaluated_start_failed",
        "llm_calls": 1,
        "total_tokens": 3,
        "by_kind": {
            "visual_observer": {"calls": 1, "total_tokens": 3},
            "visual_judge": {"calls": 0, "total_tokens": 0},
        },
        "by_kind_model": {},
        "synthesis_attributed_cost": _exact_cost(),
    }
    issues = p5_aggregate._c1_cost_module_issues(
        trial,
        expected_observer_model="observer",
        expected_grounder_model="grounder",
        expected_judge_model="judge",
    )
    assert any("policy-not-started" in issue for issue in issues)
    assert not any("visual grading has no" in issue for issue in issues)


def test_recovery_resume_rejects_deleted_completed_replacement_before_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, index_path, manifest = _build_source(tmp_path, monkeypatch)
    recovery_dir = tmp_path / "recovery"
    current = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=recovery_dir,
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
    )
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    monkeypatch.setattr(current, "_verify_preflight_artifact_unchanged", lambda: None)
    monkeypatch.setattr(current, "_verify_calibration_artifact_unchanged", lambda: None)
    current._register_c1_trial_matrix(["c1_state"])
    current._import_c1_source_trials()
    task = next(
        row for row in current._c1_task_records(current._arm_cfg("c1_state"))
        if row["id"] == "t3"
    )
    trial_tasks = current._c1_trial_tasks_path(current._arm_cfg("c1_state"), task)
    trial_dir = current._c1_trial_dir("c1_state", "m", 0, "t3")
    current._prepare_c1_invocation(
        "c1_state",
        model="m",
        seed=0,
        invocation_dir=trial_dir,
        tasks_path=trial_tasks,
        invocation={
            "explore": True,
            "max_iters": 1,
            "seeds": 1,
            "restore_diagnostics": False,
            "restore_diagnostic_task_ids": [],
        },
        task_id="t3",
    )
    report_path = trial_dir / "state-loop.json"
    report_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "complete": True,
                "model": "m",
                "seed": 0,
                "results": [{"task_id": "t3", "incomplete": False}],
                "run_context": current._run_meta(
                    "c1_state", model="m", seed=0, task_id="t3"
                ),
            }
        ),
        encoding="utf-8",
    )
    current._record(
        "c1_state",
        model="m",
        seed=0,
        task_id="t3",
        status="ok",
        report=str(report_path),
        history=[],
        replacement_for=current._c1_replacement_sources[
            current._trial_key("c1_state", "m", 0, "t3")
        ],
    )
    current.write_index()
    report_path.unlink()

    resumed = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=recovery_dir,
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=index_path,
        resume=True,
    )
    resumed._register_c1_trial_matrix(["c1_state"])
    with pytest.raises(ValueError, match="replacement report disappeared"):
        resumed._import_c1_source_trials()


def _recovery_orchestrator(
    tmp_path: Path,
    manifest: dict,
    source_index: Path,
    *,
    resume: bool,
) -> p5_scale.P5Orchestrator:
    return p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=tmp_path / "recovery-attempts",
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=source_index,
        resume=resume,
        c1_inner_worker=1,
    )


def _install_recovery_producer(
    monkeypatch: pytest.MonkeyPatch, outcomes: dict[tuple[str, int], str], calls: list
) -> None:
    def producer(*_args: object, report_dir: Path, run_meta: dict, **_kwargs: object) -> dict:
        task_id = str(run_meta["task_id"])
        attempt = int(run_meta["trial_attempt"])
        calls.append((task_id, attempt))
        outcome = outcomes[(task_id, attempt)]
        unknown = outcome == "unknown"
        timed_out = outcome == "timeout"
        retryable_infrastructure = outcome in {
            "retryable_infrastructure",
            "retryable_infrastructure_implicit",
        }
        exact = _exact_cost()
        result = {
            "task_id": task_id,
            "incomplete": unknown or timed_out or retryable_infrastructure,
            "cost": _unknown_cost() if unknown else copy.deepcopy(exact),
        }
        if timed_out:
            result.update(
                timed_out=True,
                infrastructure_error=False,
                retryable_incomplete=False,
                reason=f"C1 task {task_id} exceeded its 1200s wall-clock limit",
                actual_cost=copy.deepcopy(exact),
            )
        if retryable_infrastructure:
            result.update(
                timed_out=False,
                retryable_incomplete=True,
                reason="phase_a stopped after final_screenshot_capture_failed",
                actual_cost=copy.deepcopy(exact),
                attempt_costs=[
                    {
                        **copy.deepcopy(exact),
                        "attempt_status": "retryable_infrastructure",
                        "retry_category": "infra",
                        "retry_reason": (
                            "service unavailable: final_screenshot_capture_failed"
                        ),
                    }
                ],
                attempt_count=1,
                attempt_ledger_hash="fixture-ledger",
            )
            if outcome == "retryable_infrastructure":
                result["infrastructure_error"] = True
        payload = {
            "schema_version": 2,
            "complete": not (unknown or timed_out or retryable_infrastructure),
            "retryable_incomplete": retryable_infrastructure,
            "model": "m",
            "seed": 0,
            "results": [result],
            "run_context": run_meta,
        }
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "state-loop.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return {**payload, "report_dir": str(report_dir)}

    monkeypatch.setattr(p5_scale, "run_state_loop_benchmark", producer)
    monkeypatch.setattr(
        p5_scale.P5Orchestrator, "_require_preflight_gate", lambda _self: None
    )
    monkeypatch.setattr(
        p5_scale.P5Orchestrator,
        "_verify_preflight_artifact_unchanged",
        lambda _self: None,
    )
    monkeypatch.setattr(
        p5_scale.P5Orchestrator,
        "_verify_calibration_artifact_unchanged",
        lambda _self: None,
    )


def test_run_p5_scale_resume_continues_after_terminal_recovery_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    manifest = _manifest(root)
    manifest_path = root / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    source_dir = tmp_path / "source-public-seam"
    recovery_dir = tmp_path / "recovery-public-seam"
    phase = {"name": "source"}
    recovery_calls: list[str] = []

    def producer(
        *_args: object,
        report_dir: Path,
        run_meta: dict,
        **_kwargs: object,
    ) -> dict:
        exact = _exact_cost()
        if phase["name"] == "source":
            payload = {
                "schema_version": 2,
                "complete": False,
                "retryable_incomplete": False,
                "model": "deterministic",
                "seed": 0,
                "results": [
                    {
                        "task_id": "t1",
                        "incomplete": False,
                        "cost": copy.deepcopy(exact),
                    },
                    {
                        "task_id": "t2",
                        "incomplete": True,
                        "cost": _unknown_cost(),
                    },
                ],
                "run_context": run_meta,
            }
        else:
            task_id = str(run_meta["task_id"])
            recovery_calls.append(task_id)
            terminal = phase["name"] == "terminal"
            payload = {
                "schema_version": 2,
                "complete": not terminal,
                "retryable_incomplete": False,
                "incomplete_category": (
                    "nonretryable_incomplete_evidence" if terminal else None
                ),
                "model": "deterministic",
                "seed": 0,
                "results": (
                    [_settled_terminal_result(task_id)]
                    if terminal
                    else [
                        {
                            "task_id": task_id,
                            "incomplete": False,
                            "cost": copy.deepcopy(exact),
                        }
                    ]
                ),
                "run_context": run_meta,
            }
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "state-loop.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return {**payload, "report_dir": str(report_dir)}

    monkeypatch.setattr(p5_scale, "c1_harness_hash", lambda _root: "harness")
    monkeypatch.setattr(p5_scale, "run_state_loop_benchmark", producer)

    with pytest.raises(cost.UsageAccountingError):
        p5_scale.run_p5_scale(
            root,
            manifest_path=manifest_path,
            arms=["c1_state"],
            smoke=True,
            report_dir=source_dir,
            c1_outer_worker=1,
            c1_inner_worker=1,
        )

    phase["name"] = "terminal"
    source_index = source_dir / "p5-run-index.json"
    with pytest.raises(p5_scale.NonRetryableC1InvocationError):
        p5_scale.run_p5_scale(
            root,
            manifest_path=manifest_path,
            arms=["c1_state"],
            smoke=True,
            report_dir=recovery_dir,
            c1_outer_worker=1,
            c1_inner_worker=1,
            c1_source_index=source_index,
        )

    phase["name"] = "success"
    result = p5_scale.run_p5_scale(
        root,
        manifest_path=manifest_path,
        arms=["c1_state"],
        smoke=True,
        report_dir=recovery_dir,
        c1_outer_worker=1,
        c1_inner_worker=1,
        c1_source_index=source_index,
        resume=True,
    )

    rows = {row["task_id"]: row for row in result["index"]["arms"]["c1_state"]}
    assert recovery_calls == ["t2", "t3"]
    assert rows["t1"]["status"] == "ok"
    assert rows["t2"]["status"] == "error"
    assert rows["t3"]["status"] == "ok"
    assert p5_aggregate._c1_trial_recovery_issues(
        result["index"], repository_root=root
    ) == []

    repeated = p5_scale.run_p5_scale(
        root,
        manifest_path=manifest_path,
        arms=["c1_state"],
        smoke=True,
        report_dir=recovery_dir,
        c1_outer_worker=1,
        c1_inner_worker=1,
        c1_source_index=source_index,
        resume=True,
    )

    assert recovery_calls == ["t2", "t3"]
    assert {
        row["task_id"]: row["status"]
        for row in repeated["index"]["arms"]["c1_state"]
    } == {"t1": "ok", "t2": "error", "t3": "ok"}


def test_recovery_exact_timeout_then_success_has_valid_aggregate_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, source_index, manifest = _build_source(tmp_path, monkeypatch)
    _turn_t1_into_exact_timeout(source_index)
    calls: list[tuple[str, int]] = []
    _install_recovery_producer(
        monkeypatch,
        {
            ("t1", 0): "timeout",
            ("t1", 1): "ok",
            ("t2", 0): "ok",
            ("t3", 0): "ok",
        },
        calls,
    )

    current = _recovery_orchestrator(
        tmp_path, manifest, source_index, resume=False
    )
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    current._register_c1_trial_matrix(["c1_state"])
    current._import_c1_source_trials()
    with pytest.raises(p5_scale.NonRetryableC1InvocationError):
        current._run_c1_state_invocation("m", 0)

    resumed = _recovery_orchestrator(
        tmp_path, manifest, source_index, resume=True
    )
    resumed._register_c1_trial_matrix(["c1_state"])
    resumed._import_c1_source_trials()
    resumed._run_c1_state_invocation("m", 0)
    index = resumed.write_index()

    assert calls == [("t1", 0), ("t1", 1), ("t2", 0), ("t3", 0)]
    row = next(
        item for item in index["arms"]["c1_state"] if item["task_id"] == "t1"
    )
    assert row["status"] == "ok"
    assert row["trial_attempt"] == 1
    local = [
        item
        for item in index["c1_operational_overhead"]
        if item["task_id"] == "t1" and item.get("source_provenance") is None
    ]
    assert [item["status"] for item in local] == ["exact_usage_task_timeout"]
    assert local[0]["known_and_unknown_usage"] == (
        p5_scale.P5Orchestrator._unknown_usage_snapshot(local[0]["report"])
    )
    assert p5_aggregate._c1_trial_recovery_issues(index) == []


def test_parallel_recovery_success_preserves_sidecars_and_attempt_ledgers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, source_index, manifest = _build_source(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)
    active = 0
    peak = 0
    lock = threading.Lock()

    def producer(
        *_args: object,
        report_dir: Path,
        run_meta: dict,
        ready_handle: object,
        ready_ticket: object,
        **_kwargs: object,
    ) -> dict:
        nonlocal active, peak
        lane_id = ready_ticket.claimed_lane_id
        ready_handle.admit(ready_ticket, lane_id=lane_id)
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=2)
        exact = _exact_cost()
        task_id = str(run_meta["task_id"])
        payload = {
            "schema_version": 2,
            "complete": True,
            "retryable_incomplete": False,
            "model": "m",
            "seed": 0,
            "results": [
                {
                    "task_id": task_id,
                    "incomplete": False,
                    "cost": copy.deepcopy(exact),
                }
            ],
            "run_context": run_meta,
        }
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "state-loop.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        with lock:
            active -= 1
        ready_handle.complete(ready_ticket)
        return {**payload, "report_dir": str(report_dir)}

    monkeypatch.setattr(p5_scale, "run_state_loop_benchmark", producer)
    monkeypatch.setattr(
        p5_scale.P5Orchestrator, "_require_preflight_gate", lambda _self: None
    )
    monkeypatch.setattr(
        p5_scale.P5Orchestrator,
        "_verify_preflight_artifact_unchanged",
        lambda _self: None,
    )
    monkeypatch.setattr(
        p5_scale.P5Orchestrator,
        "_verify_calibration_artifact_unchanged",
        lambda _self: None,
    )
    current = p5_scale.P5Orchestrator(
        tmp_path / "repo",
        manifest,
        run_dir=tmp_path / "parallel-recovery",
        models=["m"],
        seeds=1,
        arms=["c1_state"],
        agent_kind="deterministic",
        c1_source_index=source_index,
        c1_inner_worker=2,
    )
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    current._register_c1_trial_matrix(["c1_state"])
    current._import_c1_source_trials()
    current._run_c1_state_invocation("m", 0)
    index = current.write_index()

    assert peak == 2
    replacement_rows = [
        row
        for row in index["arms"]["c1_state"]
        if row.get("imported") is not True
    ]
    assert {row["task_id"] for row in replacement_rows} == {"t2", "t3"}
    assert all(row["trial_attempt"] == 0 for row in replacement_rows)
    for row in replacement_rows:
        sidecar = json.loads(
            (Path(row["report"]).parent / "p5-invocation-context.json").read_text(
                encoding="utf-8"
            )
        )
        assert len(sidecar["invocation"]["inner_task_plan"]) == 1
        assert sidecar["invocation"]["inner_task_plan"][0]["task_id"] == row[
            "task_id"
        ]
    recovery_snapshot = next(
        snapshot
        for invocation_id, snapshot in index["c1_resource_scheduler"][
            "invocations"
        ].items()
        if invocation_id.startswith("recovery-c1-state-")
    )
    assert recovery_snapshot["worker_limit"] == 2
    assert recovery_snapshot["max_active_tasks"] == 2
    assert p5_aggregate._c1_trial_recovery_issues(index) == []


def test_recovery_retryable_infrastructure_then_success_has_contiguous_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, source_index, manifest = _build_source(tmp_path, monkeypatch)
    calls: list[tuple[str, int]] = []
    _install_recovery_producer(
        monkeypatch,
        {
            ("t2", 0): "retryable_infrastructure_implicit",
            ("t2", 1): "ok",
            ("t3", 0): "ok",
        },
        calls,
    )

    current = _recovery_orchestrator(
        tmp_path, manifest, source_index, resume=False
    )
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    current._register_c1_trial_matrix(["c1_state"])
    current._import_c1_source_trials()
    with pytest.raises(p5_scale.IncompleteC1InvocationError):
        current._run_c1_state_invocation("m", 0)

    resumed = _recovery_orchestrator(
        tmp_path, manifest, source_index, resume=True
    )
    resumed._register_c1_trial_matrix(["c1_state"])
    resumed._import_c1_source_trials()
    resumed._run_c1_state_invocation("m", 0)
    index = resumed.write_index()

    assert calls == [("t2", 0), ("t2", 1), ("t3", 0)]
    row = next(
        item for item in index["arms"]["c1_state"] if item["task_id"] == "t2"
    )
    assert row["status"] == "ok"
    assert row["trial_attempt"] == 1
    local = [
        item
        for item in index["c1_operational_overhead"]
        if item["task_id"] == "t2" and item.get("source_provenance") is None
    ]
    assert [item["status"] for item in local] == [
        "exact_usage_retryable_infrastructure"
    ]
    assert p5_aggregate._c1_trial_recovery_issues(index) == []


def test_recovery_two_unknown_attempts_then_attempt2_success_is_auditable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, source_index, manifest = _build_source(tmp_path, monkeypatch)
    calls: list[tuple[str, int]] = []
    _install_recovery_producer(
        monkeypatch,
        {
            ("t2", 0): "unknown",
            ("t2", 1): "unknown",
            ("t2", 2): "ok",
            ("t3", 0): "ok",
        },
        calls,
    )

    current = _recovery_orchestrator(
        tmp_path, manifest, source_index, resume=False
    )
    current.preflight_metadata, current.calibration_metadata = _gate_metadata("2")
    current._register_c1_trial_matrix(["c1_state"])
    current._import_c1_source_trials()
    with pytest.raises(cost.UsageAccountingError):
        current._run_c1_state_invocation("m", 0)

    resumed1 = _recovery_orchestrator(
        tmp_path, manifest, source_index, resume=True
    )
    resumed1._register_c1_trial_matrix(["c1_state"])
    resumed1._import_c1_source_trials()
    with pytest.raises(cost.UsageAccountingError):
        resumed1._run_c1_state_invocation("m", 0)

    resumed2 = _recovery_orchestrator(
        tmp_path, manifest, source_index, resume=True
    )
    resumed2._register_c1_trial_matrix(["c1_state"])
    resumed2._import_c1_source_trials()
    resumed2._run_c1_state_invocation("m", 0)
    index = resumed2.write_index()

    assert calls == [("t2", 0), ("t2", 1), ("t2", 2), ("t3", 0)]
    rows = {row["task_id"]: row for row in index["arms"]["c1_state"]}
    assert rows["t1"]["imported"] is True
    assert rows["t2"]["status"] == "ok"
    assert rows["t2"]["trial_attempt"] == 2
    assert rows["t2"]["attempt_id"].endswith(":t2:attempt2")
    local_t2 = [
        row
        for row in index["c1_operational_overhead"]
        if row["task_id"] == "t2" and row.get("source_provenance") is None
    ]
    assert [row["trial_attempt"] for row in local_t2] == [0, 1]
    assert all(row["replacement_for"] == rows["t2"]["replacement_for"] for row in local_t2)
    assert p5_aggregate._c1_trial_recovery_issues(index) == []


def test_recovery_attempt3_is_terminal_and_does_not_block_pending_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, source_index, manifest = _build_source(tmp_path, monkeypatch)
    calls: list[tuple[str, int]] = []
    _install_recovery_producer(
        monkeypatch,
        {
            ("t2", 0): "unknown",
            ("t2", 1): "unknown",
            ("t2", 2): "unknown",
            ("t3", 0): "ok",
        },
        calls,
    )
    for ordinal in range(3):
        current = _recovery_orchestrator(
            tmp_path, manifest, source_index, resume=ordinal > 0
        )
        if ordinal == 0:
            current.preflight_metadata, current.calibration_metadata = _gate_metadata(
                "2"
            )
        current._register_c1_trial_matrix(["c1_state"])
        current._import_c1_source_trials()
        with pytest.raises(cost.UsageAccountingError):
            current._run_c1_state_invocation("m", 0)

    exhausted = _recovery_orchestrator(
        tmp_path, manifest, source_index, resume=True
    )
    exhausted._register_c1_trial_matrix(["c1_state"])
    exhausted._import_c1_source_trials()
    exhausted._run_c1_state_invocation("m", 0)
    rows = {row["task_id"]: row for row in exhausted.index["c1_state"]}
    assert rows["t2"]["status"] == "quarantined_unknown_usage"
    assert rows["t2"]["trial_attempt"] == 2
    assert rows["t3"]["status"] == "ok"
    assert calls == [("t2", 0), ("t2", 1), ("t2", 2), ("t3", 0)]


def _jupyter_runtime_recovery_fixture(tmp_path: Path) -> dict[str, object]:
    base = tmp_path / "repo"
    source_root = base / "reports/warptest/source-recovery"
    source_path = source_root / "p5/p5-run-index.json"
    gates = source_root / "gates"
    gates.mkdir(parents=True)
    for relative in (
        "specs/jupyterlab/text-file-edit.yaml",
        "specs/jupyterlab/c1-debug-notebook-rerun.yaml",
    ):
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("schema_version: 1\n", encoding="utf-8")

    exact_cost = {
        "usage_complete": True,
        "unknown_prompt_token_calls": 0,
        "unknown_completion_token_calls": 0,
        "unknown_total_token_calls": 0,
    }
    error_report = source_root / "jupyter-error/state-loop.json"
    error_report.parent.mkdir(parents=True)
    error_payload = {
        "complete": False,
        "retryable_incomplete": False,
        "results": [
            {
                "task_id": "jupyterlab-debug-notebook-rerun",
                "incomplete": True,
                "retryable_incomplete": False,
                "reason": (
                    "JupyterLab runtime dependencies are not installed; "
                    "ModuleNotFoundError: No module named 'jupyter_server'"
                ),
                "actual_cost": exact_cost,
            }
        ],
    }
    error_report.write_text(json.dumps(error_payload), encoding="utf-8")
    error_sha = hashlib.sha256(error_report.read_bytes()).hexdigest()

    interrupted_keys = [
        (
            "c1_agent_s_synth",
            "gemini-3.5-flash@relay",
            "jynew-sidequest-trade-submap",
        ),
        ("c1_agent_s", "kimi-k2.6@relay", "2048-merge-up"),
        ("c1_agent_s", "kimi-k2.6@relay", "love-shopping-mall-spa"),
        ("c1_agent_s", "kimi-k2.6@relay", "spacerpg-galaxy-jump"),
    ]
    arms: dict[str, list[dict[str, object]]] = {
        "c1_agent_s": [],
        "c1_agent_s_synth": [],
        "c1_state": [],
    }
    interrupted: list[dict[str, object]] = []
    for ordinal, (arm, model, task_id) in enumerate(interrupted_keys):
        arms[arm].append(
            {
                "model": model,
                "seed": 0,
                "task_id": task_id,
                "status": "pending",
            }
        )
        attempt_dir = source_root / f"attempts/{ordinal}"
        attempt_dir.mkdir(parents=True)
        artifact = attempt_dir / "partial.txt"
        artifact.write_text(f"partial-{ordinal}\n", encoding="utf-8")
        interrupted.append(
            {
                "arm": arm,
                "model": model,
                "seed": 0,
                "task_id": task_id,
                "trial_attempt": 0,
                "attempt_dir": str(attempt_dir),
                "file_count": 1,
                "total_bytes": artifact.stat().st_size,
                "recursive_artifact_sha256": (
                    p5_scale._recursive_artifact_sha256(attempt_dir)
                ),
                "observed_action_frames": 0,
                "usage_status": "unknown",
                "disposition": (
                    "quarantine_primary_attempt_and_register_replacement"
                ),
            }
        )
    arms["c1_state"].extend(
        {
            "model": "pending-model",
            "seed": 0,
            "task_id": f"pending-{ordinal}",
            "status": "pending",
        }
        for ordinal in range(142)
    )
    arms["c1_state"].extend(
        {
            "model": "complete-model",
            "seed": 0,
            "task_id": f"complete-{ordinal}",
            "status": "ok",
        }
        for ordinal in range(402)
    )
    arms["c1_state"].append(
        {
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "jupyterlab-debug-notebook-rerun",
            "status": "error",
            "report": str(error_report),
            "report_sha256": error_sha,
        }
    )
    source = {
        "run_id": "p5-b9c6a7b91801",
        "git_commit": "21f0fb9ab273f7dfaf51efe8354371bb434ade8d",
        "manifest_hash": "manifest",
        "harness_hash": (
            "b35cb4ef39d93c02d56cbb81f34b3f7279f6b0e70b1ae786a757c55809700201"
        ),
        "arms": arms,
    }
    source_path.parent.mkdir(parents=True)
    source_path.write_text(json.dumps(source), encoding="utf-8")
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    harness_failure = {
        "arm": "c1_state",
        "model": "kimi-k2.6@relay",
        "seed": 0,
        "task_id": "jupyterlab-debug-notebook-rerun",
        "source_status": "error",
    }
    pause_payload = {
        "run_id": source["run_id"],
        "paused_index_sha256": source_sha,
        "registered_exact_usage_harness_failures": [
            {
                **harness_failure,
                "report": str(error_report),
                "report_sha256": error_sha,
                "usage_complete": True,
            }
        ],
        "interrupted_unknown_usage_attempts": interrupted,
    }
    pause_path = gates / "c1-pause-audit-jupyter-runtime.json"
    pause_path.write_text(json.dumps(pause_payload), encoding="utf-8")

    canary_checks = []
    for ordinal, relative in enumerate(
        (
            "specs/jupyterlab/text-file-edit.yaml",
            "specs/jupyterlab/c1-debug-notebook-rerun.yaml",
        )
    ):
        spec = (base / relative).resolve()
        report = source_root / f"canary-{ordinal}/report.json"
        report.parent.mkdir()
        report.write_text(
            json.dumps(
                {
                    "overall_status": "success",
                    "semantic_status": "success",
                    "spec_path": str(spec),
                    "launch_command": (
                        "/opt/miniconda3/bin/python3.13 -m jupyterlab --no-browser"
                    ),
                }
            ),
            encoding="utf-8",
        )
        canary_checks.append(
            {
                "spec_relative_path": relative,
                "spec": str(spec),
                "spec_sha256": hashlib.sha256(spec.read_bytes()).hexdigest(),
                "report": str(report),
                "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                "overall_status": "success",
            }
        )
    canary_payload = {
        "passed": True,
        "python_executable": "/opt/miniconda3/bin/python3.13",
        "python_version": "3.13.11",
        "jupyterlab_version": "4.7.0a0",
        "jupyter_server_version": "2.20.0",
        "jupyter_client_version": "8.9.1",
        "checks": canary_checks,
    }
    canary_path = gates / "c1-jupyterlab-runtime-canary.json"
    canary_path.write_text(json.dumps(canary_payload), encoding="utf-8")

    changed_paths = sorted(
        [
            "tests/test_c1_trial_persistence.py",
            "warptest/p5_aggregate.py",
            "warptest/p5_scale.py",
        ]
    )
    migration_payload = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": "c1_background_resource_partition_v1",
        "migration_stage": "c1_jupyterlab_runtime_environment_v1",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source["run_id"],
        "source_manifest_hash": "manifest",
        "source_harness_hash": source["harness_hash"],
        "source_git_commit": source["git_commit"],
        "current_manifest_hash": "manifest",
        "current_harness_hash": "current-harness",
        "current_git_commit": "current-git",
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "runtime_environment_variable": "WARPTEST_JUPYTERLAB_PYTHON",
        "runtime_python": "/opt/miniconda3/bin/python3.13",
        "runtime_python_version": "3.13.11",
        "runtime_packages": {
            "jupyterlab": "4.7.0a0",
            "jupyter_server": "2.20.0",
            "jupyter_client": "8.9.1",
        },
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "recursive_lineage_preserved": True,
        "treatment_semantics_unchanged": True,
        "preserved_model_failure_trials": [],
        "registered_harness_failure_trials": [harness_failure],
        "pause_audit": str(pause_path.resolve()),
        "pause_audit_sha256": hashlib.sha256(pause_path.read_bytes()).hexdigest(),
        "interrupted_unknown_usage_attempts": interrupted,
        "runtime_canary": str(canary_path.resolve()),
        "runtime_canary_sha256": hashlib.sha256(canary_path.read_bytes()).hexdigest(),
        "canary_policy": "two_real_no_model_jupyterlab_specs_v1",
        "changed_paths": changed_paths,
        "changed_paths_sha256": hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "reason": "bind the missing Jupyter runtime and preserve all prior evidence",
    }
    artifact_path = gates / "c1-source-contract-migration.json"
    artifact_path.write_text(json.dumps(migration_payload), encoding="utf-8")
    migration = {
        **migration_payload,
        "artifact": str(artifact_path.resolve()),
        "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
    }
    overhead = [
        {
            "arm": item["arm"],
            "model": item["model"],
            "seed": item["seed"],
            "task_id": item["task_id"],
            "status": "quarantined_unknown_usage",
            "report": str(pause_path.resolve()),
            "report_sha256": migration_payload["pause_audit_sha256"],
            "known_and_unknown_usage": p5_aggregate._interrupted_unknown_usage_snapshot(),
            "source_provenance": {
                "source_index_sha256": source_sha,
                "interrupted_attempt": item,
            },
        }
        for item in interrupted
    ]
    overhead.append(
        {
            **harness_failure,
            "status": "replaced_registered_exact_usage_harness_failure",
            "source_provenance": {"source_index_sha256": source_sha},
        }
    )
    run_index = {
        "manifest_hash": "manifest",
        "harness_hash": "current-harness",
        "git_commit": "current-git",
        "c1_source_contract_migration": migration,
        "c1_source_contract_migration_request": str(artifact_path.resolve()),
        "c1_operational_overhead": overhead,
    }
    source_meta = {
        "source_index": str(source_path),
        "source_index_sha256": source_sha,
        "source_run_id": source["run_id"],
        "completed_trials": 402,
        "replacement_trials": 147,
        "quarantined_trials": 4,
        "exact_usage_timeout_trials": 0,
        "exact_usage_retryable_infrastructure_trials": 1,
        "imported_nonretryable_model_failure_trials": 3,
        "contract_migration": migration,
    }
    return {
        "base": base,
        "source": source,
        "source_path": source_path,
        "source_sha": source_sha,
        "migration": migration,
        "run_index": run_index,
        "source_meta": source_meta,
    }


def test_jupyter_runtime_recovery_stage_validates_pause_and_canary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _jupyter_runtime_recovery_fixture(tmp_path)
    changed_paths = fixture["migration"]["changed_paths"]
    monkeypatch.setenv(
        "WARPTEST_JUPYTERLAB_PYTHON", "/opt/miniconda3/bin/python3.13"
    )
    monkeypatch.setattr(
        p5_scale.run_context, "git_commit", lambda _base: "current-git"
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="\n".join(changed_paths) + "\n"
        ),
    )

    p5_scale._validate_registered_c1_harness_migration(
        base=fixture["base"],
        source=fixture["source"],
        migration=fixture["migration"],
    )
    monkeypatch.setattr(
        p5_aggregate,
        "_repository_changed_paths",
        lambda *_args: changed_paths,
    )
    affected, issues = (
        p5_aggregate._registered_background_resource_partition_migration(
            run_index=fixture["run_index"],
            source=fixture["source"],
            source_path=fixture["source_path"],
            source_sha=fixture["source_sha"],
            source_meta=fixture["source_meta"],
            repository_root=fixture["base"],
        )
    )
    assert issues == []
    assert len(affected) == 147

    tampered = copy.deepcopy(fixture["migration"])
    tampered["runtime_python"] = "/usr/bin/python3"
    with pytest.raises(ValueError, match="contract is incomplete"):
        p5_scale._validate_registered_c1_harness_migration(
            base=fixture["base"],
            source=fixture["source"],
            migration=tampered,
        )


def test_jynew_exit_timeout_stage_routes_strict_validators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration = {
        "migration_id": "c1_background_resource_partition_v1",
        "migration_stage": "c1_jynew_success_report_exit_timeout_v1",
    }
    scale_calls: list[tuple[Path, dict[str, object], dict[str, object]]] = []

    def validate_scale(
        *, base: Path, source: dict[str, object], migration: dict[str, object]
    ) -> None:
        scale_calls.append((base, source, migration))

    monkeypatch.setattr(
        p5_scale,
        "_validate_jynew_exit_timeout_recovery_stage",
        validate_scale,
    )
    source = {"run_id": "source"}
    p5_scale._validate_registered_c1_harness_migration(
        base=tmp_path,
        source=source,
        migration=migration,
    )
    assert scale_calls == [(tmp_path, source, migration)]

    affected = {("c1_state", "model", 0, "task")}
    aggregate_calls: list[dict[str, object]] = []

    def validate_aggregate(**kwargs: object) -> tuple[set[tuple[str, str, int, str]], list[str]]:
        aggregate_calls.append(kwargs)
        return affected, []

    monkeypatch.setattr(
        p5_aggregate,
        "_registered_jynew_exit_timeout_recovery_stage",
        validate_aggregate,
    )
    source_path = tmp_path / "p5/p5-run-index.json"
    result = p5_aggregate._registered_background_resource_partition_migration(
        run_index={"c1_source_contract_migration": migration},
        source=source,
        source_path=source_path,
        source_sha="source-sha",
        source_meta={},
        repository_root=tmp_path,
    )
    assert result == (affected, [])
    assert aggregate_calls[0]["source_path"] == source_path


def test_screen_size_probe_cache_stage_routes_strict_validators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration = {
        "migration_id": "c1_background_resource_partition_v1",
        "migration_stage": "c1_screen_size_probe_cache_v1",
    }
    scale_calls: list[tuple[Path, dict[str, object], dict[str, object]]] = []

    def validate_scale(
        *, base: Path, source: dict[str, object], migration: dict[str, object]
    ) -> None:
        scale_calls.append((base, source, migration))

    monkeypatch.setattr(
        p5_scale,
        "_validate_screen_size_probe_cache_recovery_stage",
        validate_scale,
    )
    source = {"run_id": "source"}
    p5_scale._validate_registered_c1_harness_migration(
        base=tmp_path,
        source=source,
        migration=migration,
    )
    assert scale_calls == [(tmp_path, source, migration)]

    affected = {("c1_agent_s", "model", 0, "task")}
    aggregate_calls: list[dict[str, object]] = []

    def validate_aggregate(
        **kwargs: object,
    ) -> tuple[set[tuple[str, str, int, str]], list[str]]:
        aggregate_calls.append(kwargs)
        return affected, []

    monkeypatch.setattr(
        p5_aggregate,
        "_registered_screen_size_probe_cache_recovery_stage",
        validate_aggregate,
    )
    source_path = tmp_path / "p5/p5-run-index.json"
    result = p5_aggregate._registered_background_resource_partition_migration(
        run_index={"c1_source_contract_migration": migration},
        source=source,
        source_path=source_path,
        source_sha="source-sha",
        source_meta={},
        repository_root=tmp_path,
    )
    assert result == (affected, [])
    assert aggregate_calls[0]["source_path"] == source_path


def test_terminal_nonretryable_ledger_stage_routes_strict_validators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration = {
        "migration_id": "c1_background_resource_partition_v1",
        "migration_stage": "c1_terminal_nonretryable_ledger_v1",
    }
    with pytest.raises(
        ValueError, match="terminal-ledger migration contract is incomplete"
    ):
        p5_scale._validate_registered_c1_harness_migration(
            base=tmp_path,
            source={},
            migration=migration,
        )

    affected = {("c1_agent_s", "model", 0, "task")}
    aggregate_calls: list[dict[str, object]] = []

    def validate_aggregate(
        **kwargs: object,
    ) -> tuple[set[tuple[str, str, int, str]], list[str]]:
        aggregate_calls.append(kwargs)
        return affected, []

    monkeypatch.setattr(
        p5_aggregate,
        "_registered_terminal_nonretryable_ledger_stage",
        validate_aggregate,
    )
    source_path = tmp_path / "p5/p5-run-index.json"
    result = p5_aggregate._registered_background_resource_partition_migration(
        run_index={"c1_source_contract_migration": migration},
        source={},
        source_path=source_path,
        source_sha="source-sha",
        source_meta={},
        repository_root=tmp_path,
    )
    assert result == (affected, [])
    assert aggregate_calls[0]["source_path"] == source_path
