from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from warptest import cost, gui_agent, p5_aggregate, state_loop
from warptest.c1_budgets import C1_STEP_BUDGETS
from warptest.c1_concurrency import build_ready_task_plan
from warptest.state_loop import StateLoopTask
from warptest.task_schema import PublicIssue


def _loop_task(
    root: Path,
    task_id: str,
    *,
    benchmark: str = "2048",
    horizon: str = "short",
) -> StateLoopTask:
    description = f"do {task_id}"
    return StateLoopTask(
        issue=PublicIssue(
            id=task_id,
            benchmark=benchmark,
            repo=".",
            issue=description,
            source_path=str(root / "tasks.yaml"),
        ),
        oracle_spec=f"specs/{task_id}.yaml",
        state_description=description,
        horizon_class=horizon,
    )


def _summary(*, prompt: int | None, complete: bool = True) -> dict[str, object]:
    meter = cost.CostMeter()
    meter.record(
        cost.CallRecord(
            model="prior-model",
            prompt_tokens=prompt,
            completion_tokens=7,
            reasoning_tokens=3,
            cached_prompt_tokens=0,
            total_tokens=None if prompt is None else prompt + 7,
            latency_ms=11,
            kind="gui_policy",
            usage_complete=complete,
        )
    )
    return meter.summary()


def _synthesis_summary() -> dict[str, object]:
    meter = cost.CostMeter()
    meter.record(
        cost.CallRecord(
            model="synth-model",
            prompt_tokens=7,
            completion_tokens=4,
            reasoning_tokens=2,
            total_tokens=11,
            latency_ms=13,
            kind="checkpoint_synthesis",
        )
    )
    return meter.summary()


def _state_synthesis_accounting() -> dict[str, object]:
    empty = cost.CostMeter().summary()
    return {
        "attributed_cost": empty,
        "actual_cost": empty,
        "transient_actual_cost": empty,
        "cache_hit": False,
        "cache_fingerprint": None,
    }


class C1AttemptCostTests(unittest.TestCase):
    def test_gui_resume_validates_full_plan_then_runs_only_remaining_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_dir = root / "report"
            tasks = [
                _loop_task(root, "task-1"),
                _loop_task(root, "task-2"),
            ]
            expected_task_plan = [
                item.metadata()
                for item in build_ready_task_plan(
                    tasks,
                    repo_for_task=lambda task: (root / task.issue.repo).resolve(),
                )
            ]
            phase = "initial"
            calls: list[tuple[str, str]] = []

            def run_task(_base, task, **_kwargs):
                calls.append((phase, task.id))
                if phase == "initial" and task.id == "task-2":
                    return {
                        "task_id": task.id,
                        "status": "incomplete",
                        "incomplete": True,
                        "result": False,
                        "reason": "HTTP 503 service unavailable",
                        "infrastructure_error": True,
                        "policy_phase_started": False,
                    }
                return {
                    "task_id": task.id,
                    "status": "success",
                    "incomplete": False,
                    "result": True,
                }

            with (
                patch.dict(os.environ, {"MODELSCOPE_API_KEY": "test-key"}),
                patch.object(gui_agent, "load_state_loop_tasks", return_value=tasks),
                patch.object(gui_agent, "FixedVisualBridge", return_value=MagicMock()),
                patch.object(gui_agent, "_build_policy_client", return_value=MagicMock()),
                patch.object(gui_agent, "_run_agent_task", side_effect=run_task),
                patch.object(gui_agent.run_context, "stamp_payload", return_value=None),
            ):
                first = gui_agent.run_gui_agent_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    model="current-model",
                    base_url="https://example.invalid/v1",
                    report_dir=report_dir,
                    expected_task_plan=expected_task_plan,
                    inner_workers=1,
                )
                self.assertFalse(first["complete"])
                phase = "resume"
                resumed = gui_agent.run_gui_agent_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    model="current-model",
                    base_url="https://example.invalid/v1",
                    report_dir=report_dir,
                    expected_task_plan=expected_task_plan,
                    inner_workers=1,
                    resume=True,
                )

        self.assertTrue(resumed["complete"])
        self.assertEqual(
            calls,
            [("initial", "task-1"), ("initial", "task-2"), ("resume", "task-2")],
        )
        self.assertEqual(
            resumed["scheduler"]["task_registration"][0]["manifest_ordinal"],
            1,
        )
        self.assertEqual(
            resumed["scheduler"]["task_registration"][0]["dispatch_ordinal"],
            1,
        )

    def test_state_resume_validates_full_plan_then_runs_only_remaining_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_dir = root / "state-report"
            tasks = [
                _loop_task(root, "task-1"),
                _loop_task(root, "task-2"),
            ]
            expected_task_plan = [
                item.metadata()
                for item in build_ready_task_plan(
                    tasks,
                    repo_for_task=lambda task: (root / task.issue.repo).resolve(),
                )
            ]
            empty = cost.CostMeter().summary()
            phase = "initial"
            calls: list[tuple[str, str]] = []

            def run_task(_base, task, _agent, **_kwargs):
                calls.append((phase, task.id))
                if phase == "initial" and task.id == "task-2":
                    return {
                        "task_id": task.id,
                        "benchmark": task.benchmark,
                        "status": "failed",
                        "result": False,
                        "incomplete": True,
                        "reason": "HTTP 503 service unavailable",
                        "visual_grading_started": False,
                        "cost": empty,
                    }
                return {
                    "task_id": task.id,
                    "benchmark": task.benchmark,
                    "status": "success",
                    "result": True,
                    "cost": empty,
                }

            with (
                patch.object(state_loop, "load_state_loop_tasks", return_value=tasks),
                patch.object(state_loop, "build_synthesis_agent", return_value=MagicMock()),
                patch.object(state_loop, "_run_single_task", side_effect=run_task),
                patch.object(state_loop.run_context, "stamp_payload", return_value=None),
            ):
                first = state_loop.run_state_loop_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    agent_kind="deterministic",
                    report_dir=report_dir,
                    expected_task_plan=expected_task_plan,
                    inner_workers=1,
                )
                self.assertFalse(first["complete"])
                phase = "resume"
                resumed = state_loop.run_state_loop_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    agent_kind="deterministic",
                    report_dir=report_dir,
                    expected_task_plan=expected_task_plan,
                    inner_workers=1,
                    resume=True,
                )

        self.assertTrue(resumed["complete"])
        self.assertEqual(
            calls,
            [("initial", "task-1"), ("initial", "task-2"), ("resume", "task-2")],
        )
        self.assertEqual(
            resumed["scheduler"]["task_registration"][0]["manifest_ordinal"],
            1,
        )
        self.assertEqual(
            resumed["scheduler"]["task_registration"][0]["dispatch_ordinal"],
            1,
        )

    def test_policy_runtime_metadata_rejects_opaque_non_json_values(self) -> None:
        client = MagicMock()
        client.model = MagicMock(name="opaque-model")
        client.provider_name = "relay"
        client.max_tokens = 8192
        client.max_retries = MagicMock(name="opaque-retries")

        metadata = gui_agent._policy_runtime_metadata(
            client, policy_kind="gui_agent"
        )

        self.assertIsNone(metadata["model"])
        self.assertEqual(metadata["provider"], "relay")
        self.assertEqual(metadata["max_tokens"], 8192)
        self.assertIsNone(metadata["sdk_max_retries"])
        json.dumps(metadata)

    def test_state_provider_fallback_remains_retryable_incomplete(self) -> None:
        checkpoint = SimpleNamespace(
            payload={
                "model_fallback_reason": (
                    "LLM API call failed before producing a checkpoint: HTTP 503"
                )
            },
            runtime_validation={},
        )
        reason = state_loop._checkpoint_provider_failure(checkpoint)

        self.assertIsNotNone(reason)
        self.assertTrue(
            state_loop._state_trial_incomplete(
                {"iterations": [{"incomplete": True, "reason": reason}]}
            )
        )

    def test_gui_http_500_before_policy_is_retryable_incomplete(self) -> None:
        result = {
            "incomplete": True,
            "policy_phase_started": False,
            "reason": (
                "JupyterLab C1 web oracle golden generation failed: "
                "HTTP Error 500: Internal Server Error"
            ),
            "actual_cost": cost.CostMeter().summary(),
        }

        self.assertEqual(gui_agent._gui_retry_category(result), "infra")
        self.assertTrue(gui_agent._gui_trial_retryable_incomplete(result))

    def test_gui_invocation_stops_after_first_incomplete_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_dir = root / "report"
            tasks = [
                _loop_task(root, "task-1"),
                _loop_task(root, "task-2"),
            ]
            run_task = MagicMock(
                return_value={
                    "task_id": "task-1",
                    "status": "incomplete",
                    "incomplete": True,
                    "result": False,
                    "reason": "unknown provider usage",
                }
            )
            run_meta = {"experiment_id": "test", "run_id": "run-1"}
            with (
                patch.dict(os.environ, {"MODELSCOPE_API_KEY": "test-key"}),
                patch.object(gui_agent, "load_state_loop_tasks", return_value=tasks),
                patch.object(
                    gui_agent, "FixedVisualBridge", return_value=MagicMock()
                ),
                patch.object(
                    gui_agent, "_build_policy_client", return_value=MagicMock()
                ),
                patch.object(gui_agent, "_run_agent_task", run_task),
            ):
                report = gui_agent.run_gui_agent_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    model="current-model",
                    base_url="https://example.invalid/v1",
                    report_dir=report_dir,
                    run_meta=run_meta,
                )

            persisted = json.loads(
                (report_dir / "gui-agent.json").read_text(encoding="utf-8")
            )

        self.assertEqual(run_task.call_count, 1)
        self.assertEqual(len(report["results"]), 1)
        self.assertFalse(report["complete"])
        self.assertEqual(persisted["arm"], "c1_gui")
        self.assertEqual(persisted["run_context"], run_meta)

    def test_single_gui_treatment_has_zero_transient_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = _loop_task(root, "task-1")

            def run_task(*_args, **_kwargs):
                cost.record_call(
                    "current-model",
                    prompt_tokens=5,
                    completion_tokens=9,
                    reasoning_tokens=4,
                    total_tokens=14,
                    latency_ms=13,
                    kind="gui_policy",
                )
                return {
                    "task_id": "task-1",
                    "status": "success",
                    "incomplete": False,
                    "result": True,
                }

            with (
                patch.dict(os.environ, {"MODELSCOPE_API_KEY": "test-key"}),
                patch.object(gui_agent, "load_state_loop_tasks", return_value=[task]),
                patch.object(gui_agent, "FixedVisualBridge", return_value=MagicMock()),
                patch.object(gui_agent, "_build_policy_client", return_value=MagicMock()),
                patch.object(gui_agent, "_run_agent_task", side_effect=run_task),
                patch.object(gui_agent.run_context, "stamp_payload", return_value=None),
            ):
                report = gui_agent.run_gui_agent_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    model="current-model",
                    base_url="https://example.invalid/v1",
                    report_dir=root / "report",
                )

        result = report["results"][0]
        self.assertEqual(result["cost"]["llm_calls"], 1)
        self.assertEqual(result["actual_cost"]["llm_calls"], 1)
        self.assertEqual(result["transient_infrastructure_cost"]["llm_calls"], 0)
        self.assertEqual(result["attempt_costs"][0]["attempt_status"], "treatment")
        self.assertIsNone(result["attempt_costs"][0]["retry_category"])
        self.assertIsNone(result["attempt_costs"][0]["retry_reason"])

    def test_incremental_gui_reports_match_all_arm_sidecar_contracts(self) -> None:
        observer_model = "observer@relay"
        grounder_model = "grounder@relay"
        model_profile = p5_aggregate.ModelRequestProfile(
            "M1", "provider_default"
        ).provenance()
        grounder_profile = p5_aggregate.ModelRequestProfile(
            "grounder", "provider_default"
        ).provenance()
        runtime_lock = {
            "scope": "machine-shared",
            "lock_dir": "/tmp/warptest-runtime-locks",
        }
        run_index = {
            "run_id": "run",
            "git_commit": "git",
            "manifest_hash": "manifest",
            "task_manifest_hash": "tasks",
            "harness_hash": "harness",
            "api_provider": {"name": "relay"},
            "model_request_profiles": {
                "M1": "provider_default",
                "grounder": "provider_default",
            },
            "runtime_lock": runtime_lock,
            "c1_cache_policy": {"self_synthesis": "/tmp/synth-cache"},
        }
        direct_metadata = {
            "policy_kind": "gui_agent",
            "model": "M1",
            "provider": "relay",
            "model_request_profile": model_profile,
            "max_tokens": 8192,
            "reasoning_retry_max_tokens": 16384,
            "sdk_max_retries": 0,
        }
        agent_s_metadata = {
            "policy_kind": "agent_s",
            "model": "M1",
            "provider": "relay",
            "grounder_model": "grounder",
            "grounder_provider": "relay",
            "protocol_version": p5_aggregate.AGENT_S_PROTOCOL_VERSION,
            "agent_s_version": p5_aggregate.PINNED_AGENT_S_VERSION,
            "agent_s_commit": p5_aggregate.PINNED_AGENT_S_COMMIT,
            "runtime_mode": "background_pixel",
            "execution_backend": "background_pixel_v1",
            "coordinate_space": "frame_pixels_top_left_v1",
            "raw_screenshot_observation": True,
            "action_execution_owner": "parent_runtime",
            "foreground_required": False,
            "upstream_worker": True,
            "reflection_enabled": True,
            "local_code_enabled": False,
            "worker_observation_mode": "screenshot",
            "native_exec_enabled": False,
            "provider_timeout_s": p5_aggregate.AGENT_S_PROVIDER_TIMEOUT_S,
            "predict_timeout_mode": p5_aggregate.AGENT_S_PREDICT_TIMEOUT_MODE,
            "max_provider_attempts_per_predict": (
                p5_aggregate.AGENT_S_MAX_PROVIDER_ATTEMPTS_PER_PREDICT
            ),
            "model_request_profiles": {
                "worker": model_profile,
                "reflection": model_profile,
                "grounding": grounder_profile,
            },
        }
        cases = (
            ("c1_gui", "gui_agent", "from_start"),
            ("c1_gui_synth", "gui_agent", "synthesized"),
            ("c1_agent_s", "agent_s", "from_start"),
            ("c1_agent_s_synth", "agent_s", "synthesized"),
        )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for arm, policy_kind, start_mode in cases:
                with self.subTest(arm=arm):
                    directory = root / arm
                    directory.mkdir()
                    report_path = directory / "gui-agent.json"
                    run_meta = {
                        "arm": arm,
                        "model": "M1",
                        "seed": 0,
                        "git_commit": "git",
                        "task_manifest_hash": "tasks",
                        "runtime_lock": runtime_lock,
                    }
                    gui_agent._write_gui_report_incremental(
                        report_path,
                        results=[],
                        model="M1",
                        seed=0,
                        policy_kind=policy_kind,
                        start_mode=start_mode,
                        budgets=dict(C1_STEP_BUDGETS),
                        complete=False,
                        arm=arm,
                        run_meta=run_meta,
                        git_root=root,
                        policy_runtime_metadata=(
                            agent_s_metadata
                            if policy_kind == "agent_s"
                            else direct_metadata
                        ),
                        observer_model=observer_model,
                        grounder_model=grounder_model,
                    )
                    sidecar = {
                        "schema_version": 4,
                        "kind": "p5_c1_invocation_context",
                        "run_id": "run",
                        "arm": arm,
                        "model": "M1",
                        "seed": 0,
                        "git_commit": "git",
                        "manifest_hash": "manifest",
                        "task_manifest_hash": "tasks",
                        "harness_hash": "harness",
                        "api_provider": {"name": "relay"},
                        "model_request_profiles": {
                            "M1": "provider_default",
                            "grounder": "provider_default",
                        },
                        "runtime_lock": runtime_lock,
                        "cache_policy": {
                            "self_synthesis": "/tmp/synth-cache"
                        },
                        "model_request_profile": model_profile,
                        "invocation": {
                            "start_mode": start_mode,
                            "policy_kind": policy_kind,
                            "step_budgets": dict(C1_STEP_BUDGETS),
                            "observer_model": observer_model,
                            "grounder_model": grounder_model,
                            "synth_model": "M1",
                            "shared_synthesis_cache": "/tmp/synth-cache",
                        },
                    }
                    (directory / "p5-invocation-context.json").write_text(
                        json.dumps(sidecar), encoding="utf-8"
                    )
                    report = json.loads(report_path.read_text(encoding="utf-8"))

                    self.assertEqual(report["observer_model"], observer_model)
                    self.assertEqual(
                        report["grounder_model"],
                        grounder_model if policy_kind == "agent_s" else None,
                    )
                    self.assertEqual(
                        p5_aggregate._c1_sidecar_issues(
                            report_path,
                            arm=arm,
                            entry={"model": "M1", "seed": 0},
                            run_index=run_index,
                            report=report,
                            visual_bridge={
                                "observer_model": observer_model,
                                "grounder_model": grounder_model,
                            },
                        ),
                        [],
                    )

    def _resume(
        self,
        prior_cost: dict[str, object],
        *,
        tamper_attempt: bool = False,
    ) -> dict[str, object]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        report_dir = root / "report"
        report_dir.mkdir()
        prior_result: dict[str, object] = {
            "task_id": "task-1",
            "status": "incomplete",
            "incomplete": True,
            "retryable_incomplete": True,
        }
        prior_attempt = gui_agent._gui_attempt_cost(
            prior_result,
            prior_cost,
            start_mode="from_start",
        )
        prior_attempt.update(
            {
                "attempt_status": "retryable_infrastructure",
                "retry_category": "infra",
                "retry_reason": "service unavailable",
            }
        )
        prior_attempts, prior_ledger_hash = gui_agent._bind_attempt_ledger(
            [prior_attempt], task_id="task-1"
        )
        prior_result.update(
            {
                "attempt_costs": prior_attempts,
                "attempt_count": 1,
                "attempt_ledger_hash": prior_ledger_hash,
                "cost": prior_cost,
            }
        )
        report_path = report_dir / "gui-agent.json"
        gui_agent._write_gui_report_incremental(
            report_path,
            results=[prior_result],
            model="current-model",
            seed=0,
            policy_kind="gui_agent",
            start_mode="from_start",
            budgets=dict(C1_STEP_BUDGETS),
            complete=False,
            arm="c1_gui",
            run_meta={},
            git_root=root,
            policy_runtime_metadata={},
            observer_model=gui_agent.DEFAULT_OBSERVER_MODEL,
            grounder_model=gui_agent.DEFAULT_OBSERVER_MODEL,
        )
        if tamper_attempt:
            persisted = json.loads(report_path.read_text(encoding="utf-8"))
            persisted["results"][0]["attempt_costs"][0]["task_actual_cost"][
                "prompt_tokens"
            ] += 1
            report_path.write_text(json.dumps(persisted), encoding="utf-8")
        task = _loop_task(root, "task-1")
        policy = MagicMock()
        self._resume_run_calls = 0

        def run_task(*_args, **_kwargs):
            self._resume_run_calls += 1
            cost.record_call(
                "current-model",
                prompt_tokens=5,
                completion_tokens=9,
                reasoning_tokens=4,
                total_tokens=14,
                latency_ms=13,
                kind="gui_policy",
            )
            return {"task_id": "task-1", "status": "success", "incomplete": False, "result": True}

        with (
            patch.dict(os.environ, {"MODELSCOPE_API_KEY": "test-key"}),
            patch.object(gui_agent, "load_state_loop_tasks", return_value=[task]),
            patch.object(gui_agent, "FixedVisualBridge", return_value=MagicMock()),
            patch.object(gui_agent, "_build_policy_client", return_value=policy),
            patch.object(gui_agent, "_run_agent_task", side_effect=run_task),
            patch.object(gui_agent.run_context, "stamp_payload", return_value=None),
        ):
            return gui_agent.run_gui_agent_benchmark(
                root,
                tasks_path="tasks.yaml",
                model="current-model",
                base_url="https://example.invalid/v1",
                resume=True,
                report_dir=report_dir,
            )

    def test_resume_preserves_prior_incomplete_attempt_cost(self) -> None:
        report = self._resume(_summary(prompt=3))
        result = report["results"][0]
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["cost"]["llm_calls"], 1)
        self.assertEqual(result["cost"]["prompt_tokens"], 5)
        self.assertEqual(result["actual_cost"]["llm_calls"], 2)
        self.assertEqual(result["actual_cost"]["prompt_tokens"], 8)
        self.assertEqual(result["transient_infrastructure_cost"]["llm_calls"], 1)
        self.assertEqual(
            [attempt["attempt_status"] for attempt in result["attempt_costs"]],
            ["retryable_infrastructure", "treatment"],
        )
        self.assertTrue(all(len(attempt["attempt_hash"]) == 64 for attempt in result["attempt_costs"]))
        self.assertEqual(len(result["attempt_ledger_hash"]), 64)
        self.assertTrue(report["complete"])

    def test_unknown_prior_usage_refuses_resume_before_new_paid_attempt(self) -> None:
        with self.assertRaisesRegex(
            cost.UsageAccountingError, "refusing to resume GUI trials"
        ):
            self._resume(_summary(prompt=None, complete=False))
        self.assertEqual(self._resume_run_calls, 0)

    def test_gui_resume_rejects_tampered_attempt_before_paid_call(self) -> None:
        with self.assertRaisesRegex(
            cost.UsageAccountingError, "attempt hash mismatch"
        ):
            self._resume(_summary(prompt=3), tamper_attempt=True)
        self.assertEqual(self._resume_run_calls, 0)

    def test_nested_unknown_synthesis_usage_is_permanently_incomplete(self) -> None:
        attempt = _summary(prompt=3)
        attempt["synthesis_accounting"] = {
            "actual_cost": _summary(prompt=None, complete=False)
        }

        self.assertTrue(cost.contains_unknown_usage([attempt]))

    def test_state_arm_resume_also_preserves_prior_attempt_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_dir = root / "state-report"
            report_dir.mkdir()
            prior_cost = _summary(prompt=3)
            prior_attempts, prior_ledger_hash = state_loop._bind_attempt_ledger(
                [
                    {
                        **prior_cost,
                        "attempt_status": "retryable_infrastructure",
                        "retry_category": "infra",
                        "retry_reason": "service unavailable",
                        "task_actual_cost": prior_cost,
                        "synthesis_accounting": _state_synthesis_accounting(),
                    }
                ],
                task_id="task-1",
            )
            (report_dir / "state-loop.json").write_text(
                json.dumps(
                    {
                        "synthesis_model": None,
                        "seed": 0,
                        "results": [
                            {
                                "task_id": "task-1",
                                "status": "failed",
                                "incomplete": True,
                                "retryable_incomplete": True,
                                "cost": prior_cost,
                                "attempt_costs": prior_attempts,
                                "attempt_count": 1,
                                "attempt_ledger_hash": prior_ledger_hash,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            task = _loop_task(root, "task-1")
            current = _summary(prompt=5)
            current["by_model"] = {
                "current-model": current["by_model"].pop("prior-model")
            }

            with (
                patch.object(state_loop, "load_state_loop_tasks", return_value=[task]),
                patch.object(state_loop, "build_synthesis_agent", return_value=MagicMock()),
                patch.object(
                    state_loop,
                    "_run_single_task",
                    return_value={
                        "task_id": "task-1",
                        "benchmark": "2048",
                        "status": "failure",
                        "result": False,
                        "cost": current,
                    },
                ),
                patch.object(state_loop.run_context, "stamp_payload", return_value=None),
            ):
                report = state_loop.run_state_loop_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    agent_kind="deterministic",
                    resume=True,
                    report_dir=report_dir,
                )

        result = report["results"][0]
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["cost"]["llm_calls"], 1)
        self.assertEqual(result["cost"]["prompt_tokens"], 5)
        self.assertEqual(result["actual_cost"]["llm_calls"], 2)
        self.assertEqual(result["actual_cost"]["prompt_tokens"], 8)
        self.assertEqual(result["transient_infrastructure_cost"]["llm_calls"], 1)
        self.assertEqual(
            [attempt["attempt_status"] for attempt in result["attempt_costs"]],
            ["retryable_infrastructure", "treatment"],
        )
        self.assertTrue(
            all(
                attempt["synthesis_accounting"]["actual_cost"]["llm_calls"] == 0
                for attempt in result["attempt_costs"]
            )
        )

    def test_state_resume_rejects_tampered_attempt_before_paid_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_dir = root / "state-report"
            report_dir.mkdir()
            prior_cost = _summary(prompt=3)
            attempts, ledger_hash = state_loop._bind_attempt_ledger(
                [
                    {
                        **prior_cost,
                        "attempt_status": "retryable_infrastructure",
                        "retry_category": "infra",
                        "retry_reason": "service unavailable",
                        "task_actual_cost": prior_cost,
                        "synthesis_accounting": _state_synthesis_accounting(),
                    }
                ],
                task_id="task-1",
            )
            attempts[0]["task_actual_cost"]["prompt_tokens"] += 1
            (report_dir / "state-loop.json").write_text(
                json.dumps(
                    {
                        "synthesis_model": None,
                        "seed": 0,
                        "results": [
                            {
                                "task_id": "task-1",
                                "incomplete": True,
                                "retryable_incomplete": True,
                                "attempt_costs": attempts,
                                "attempt_count": 1,
                                "attempt_ledger_hash": ledger_hash,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            runner = MagicMock()
            task = _loop_task(root, "task-1")
            with (
                patch.object(state_loop, "load_state_loop_tasks", return_value=[task]),
                patch.object(state_loop, "build_synthesis_agent", return_value=MagicMock()),
                patch.object(state_loop, "_run_single_task", runner),
            ):
                with self.assertRaisesRegex(
                    cost.UsageAccountingError, "attempt hash mismatch"
                ):
                    state_loop.run_state_loop_benchmark(
                        root,
                        tasks_path="tasks.yaml",
                        agent_kind="deterministic",
                        resume=True,
                        report_dir=report_dir,
                    )

        runner.assert_not_called()

    def test_state_invocation_stops_and_stamps_partial_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_dir = root / "state-report"
            tasks = [
                _loop_task(root, "task-1"),
                _loop_task(root, "task-2"),
            ]
            task_result = {
                "task_id": "task-1",
                "benchmark": "2048",
                "status": "failed",
                "result": False,
                "incomplete": True,
                "reason": "HTTP 503 service unavailable",
                "visual_grading_started": False,
                "cost": _summary(prompt=3),
            }
            runner = MagicMock(return_value=task_result)
            run_meta = {"experiment_id": "test-state", "run_id": "run-1"}
            with (
                patch.object(state_loop, "load_state_loop_tasks", return_value=tasks),
                patch.object(
                    state_loop, "build_synthesis_agent", return_value=MagicMock()
                ),
                patch.object(state_loop, "_run_single_task", runner),
            ):
                report = state_loop.run_state_loop_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    agent_kind="deterministic",
                    report_dir=report_dir,
                    run_meta=run_meta,
                )
            persisted = json.loads(
                (report_dir / "state-loop.json").read_text(encoding="utf-8")
            )

        self.assertEqual(runner.call_count, 1)
        self.assertFalse(report["complete"])
        self.assertTrue(report["retryable_incomplete"])
        self.assertEqual(persisted["arm"], "c1_state")
        self.assertEqual(persisted["run_context"], run_meta)

    def test_single_state_treatment_has_zero_transient_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_dir = root / "state-report"
            task = _loop_task(root, "task-1")
            current = _summary(prompt=5)
            with (
                patch.object(state_loop, "load_state_loop_tasks", return_value=[task]),
                patch.object(state_loop, "build_synthesis_agent", return_value=MagicMock()),
                patch.object(
                    state_loop,
                    "_run_single_task",
                    return_value={
                        "task_id": "task-1",
                        "benchmark": "2048",
                        "status": "failure",
                        "result": False,
                        "cost": current,
                    },
                ),
                patch.object(state_loop.run_context, "stamp_payload", return_value=None),
            ):
                report = state_loop.run_state_loop_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    agent_kind="deterministic",
                    report_dir=report_dir,
                )

        result = report["results"][0]
        self.assertEqual(result["cost"]["llm_calls"], 1)
        self.assertEqual(result["actual_cost"]["llm_calls"], 1)
        self.assertEqual(result["transient_infrastructure_cost"]["llm_calls"], 0)
        self.assertEqual(result["attempt_costs"][0]["attempt_status"], "treatment")
        # The state arm now carries the same synthesis ledger as the
        # synthesized GUI arm; a run without synthesis records zeros, not None.
        accounting = result["attempt_costs"][0]["synthesis_accounting"]
        self.assertEqual(accounting["actual_cost"]["llm_calls"], 0)
        self.assertEqual(accounting["attributed_cost"]["llm_calls"], 0)
        self.assertFalse(accounting["cache_hit"])

    def _synthesized_retry(
        self,
        *,
        first_is_zero_call_transport: bool,
        first_paid_synthesis_infrastructure: bool = False,
        second_fingerprint: str = "synth-fp-a",
        second_cache_hit: bool | None = None,
    ) -> dict[str, object]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        report_dir = root / "report"
        task = _loop_task(root, "task-1")
        policy = MagicMock()
        synth_cost = _synthesis_summary()
        empty_cost = cost.CostMeter().summary()
        calls = 0

        def run_task(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls > 1 or (
                not first_is_zero_call_transport
                and not first_paid_synthesis_infrastructure
            ):
                cost.record_call(
                    "policy-model",
                    prompt_tokens=5,
                    completion_tokens=9,
                    reasoning_tokens=4,
                    total_tokens=14,
                    latency_ms=13,
                    kind="gui_policy",
                )
            first = calls == 1
            synthesis_infrastructure = bool(
                first
                and (
                    first_is_zero_call_transport
                    or first_paid_synthesis_infrastructure
                )
            )
            cache_hit = False if first else (
                bool(second_cache_hit)
                if second_cache_hit is not None
                else not (
                    first_is_zero_call_transport
                    or first_paid_synthesis_infrastructure
                )
            )
            return {
                "task_id": "task-1",
                "status": "incomplete" if first else "success",
                "incomplete": first,
                "result": not first,
                "reason": "service unavailable" if first else None,
                "infrastructure_error": first,
                "policy_phase_started": False if first else True,
                "self_synthesis_cost": (
                    empty_cost
                    if first and first_is_zero_call_transport
                    else synth_cost
                ),
                "self_synthesis_outcome_status": (
                    "infrastructure_error"
                    if synthesis_infrastructure
                    else "success"
                ),
                "self_synthesis_cache": {
                    "cache_hit": cache_hit,
                    "cache_key_fingerprint": (
                        "synth-fp-a" if first else second_fingerprint
                    ),
                },
            }

        patches = (
            patch.dict(os.environ, {"MODELSCOPE_API_KEY": "test-key"}),
            patch.object(gui_agent, "load_state_loop_tasks", return_value=[task]),
            patch.object(gui_agent, "FixedVisualBridge", return_value=MagicMock()),
            patch.object(gui_agent, "_build_policy_client", return_value=policy),
            patch.object(gui_agent, "_run_agent_task", side_effect=run_task),
            patch.object(gui_agent.run_context, "stamp_payload", return_value=None),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            gui_agent.run_gui_agent_benchmark(
                root,
                tasks_path="tasks.yaml",
                model="current-model",
                base_url="https://example.invalid/v1",
                start_mode="synthesized",
                resume=True,
                report_dir=report_dir,
            )
            return gui_agent.run_gui_agent_benchmark(
                root,
                tasks_path="tasks.yaml",
                model="current-model",
                base_url="https://example.invalid/v1",
                start_mode="synthesized",
                resume=True,
                report_dir=report_dir,
            )

    def test_runtime_retry_does_not_charge_cached_synthesis_twice(self) -> None:
        report = self._synthesized_retry(first_is_zero_call_transport=False)
        result = report["results"][0]

        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["cost"]["llm_calls"], 2)
        self.assertEqual(result["actual_cost"]["llm_calls"], 3)
        self.assertEqual(result["transient_infrastructure_cost"]["llm_calls"], 1)
        self.assertEqual(
            result["cost"]["by_kind"]["checkpoint_synthesis"]["calls"],
            1,
        )
        self.assertTrue(result["synthesis_cost_charged"])
        self.assertFalse(result["synthesis_cost_charged_this_attempt"])
        attempts = result["attempt_costs"]
        first = attempts[0]["synthesis_accounting"]
        second = attempts[1]["synthesis_accounting"]
        self.assertEqual(first["cache_key_fingerprint"], "synth-fp-a")
        self.assertEqual(
            first["counterfactual_burden_fingerprint"],
            second["counterfactual_burden_fingerprint"],
        )
        self.assertTrue(first["actual_incurred"])
        self.assertTrue(first["attributed"])
        self.assertTrue(second["cache_hit"])
        self.assertFalse(second["actual_incurred"])
        self.assertFalse(second["attributed"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(result["synthesis_actual_cost"]["llm_calls"], 1)
        self.assertEqual(result["synthesis_attributed_cost"]["llm_calls"], 1)
        self.assertEqual(
            [attempt["retry_category"] for attempt in attempts],
            ["infra", None],
        )

    def test_zero_call_transport_retry_can_charge_synthesis_once_later(self) -> None:
        report = self._synthesized_retry(first_is_zero_call_transport=True)
        result = report["results"][0]

        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["cost"]["llm_calls"], 2)
        self.assertEqual(
            result["cost"]["by_kind"]["checkpoint_synthesis"]["calls"],
            1,
        )
        self.assertTrue(result["synthesis_cost_charged"])
        self.assertTrue(result["synthesis_cost_charged_this_attempt"])
        first, second = [
            attempt["synthesis_accounting"]
            for attempt in result["attempt_costs"]
        ]
        self.assertFalse(first["actual_incurred"])
        self.assertFalse(first["attributed"])
        self.assertFalse(second["cache_hit"])
        self.assertTrue(second["actual_incurred"])
        self.assertTrue(second["attributed"])

    def test_paid_uncached_synthesis_failure_is_actual_only_on_retry(self) -> None:
        report = self._synthesized_retry(
            first_is_zero_call_transport=False,
            first_paid_synthesis_infrastructure=True,
        )
        result = report["results"][0]

        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(
            result["cost"]["by_kind"]["checkpoint_synthesis"]["calls"],
            1,
        )
        self.assertEqual(result["cost"]["llm_calls"], 2)
        attempts = result["attempt_costs"]
        self.assertEqual(
            [
                attempt["synthesis_accounting"]["cache_key_fingerprint"]
                for attempt in attempts
            ],
            ["synth-fp-a", "synth-fp-a"],
        )
        self.assertTrue(
            all(
                attempt["synthesis_accounting"]["actual_incurred"]
                for attempt in attempts
            )
        )
        self.assertFalse(attempts[0]["synthesis_accounting"]["attributed"])
        self.assertTrue(attempts[1]["synthesis_accounting"]["attributed"])
        self.assertEqual(result["synthesis_actual_cost"]["llm_calls"], 2)
        self.assertEqual(result["synthesis_transient_actual_cost"]["llm_calls"], 1)
        self.assertEqual(result["synthesis_attributed_cost"]["llm_calls"], 1)

    def test_same_cache_key_different_cached_burden_is_not_deduplicated(self) -> None:
        report = self._synthesized_retry(
            first_is_zero_call_transport=False,
            first_paid_synthesis_infrastructure=True,
            second_cache_hit=True,
        )
        result = report["results"][0]

        first, second = [
            attempt["synthesis_accounting"]
            for attempt in result["attempt_costs"]
        ]
        self.assertEqual(
            first["cache_key_fingerprint"], second["cache_key_fingerprint"]
        )
        self.assertNotEqual(
            first["counterfactual_burden_fingerprint"],
            second["counterfactual_burden_fingerprint"],
        )
        self.assertTrue(second["cache_hit"])
        self.assertFalse(second["actual_incurred"])
        self.assertTrue(second["attributed"])
        self.assertFalse(second["deduplicated"])
        self.assertEqual(
            result["cost"]["by_kind"]["checkpoint_synthesis"]["calls"], 1
        )
        self.assertEqual(result["synthesis_actual_cost"]["llm_calls"], 1)
        self.assertEqual(result["synthesis_transient_actual_cost"]["llm_calls"], 1)
        self.assertEqual(result["synthesis_attributed_cost"]["llm_calls"], 1)

    def test_new_fingerprint_cache_hit_gets_its_own_counterfactual_charge(self) -> None:
        report = self._synthesized_retry(
            first_is_zero_call_transport=False,
            second_fingerprint="synth-fp-b",
            second_cache_hit=True,
        )
        result = report["results"][0]

        self.assertEqual(
            result["cost"]["by_kind"]["checkpoint_synthesis"]["calls"],
            2,
        )
        first, second = [
            attempt["synthesis_accounting"]
            for attempt in result["attempt_costs"]
        ]
        self.assertEqual(first["cache_key_fingerprint"], "synth-fp-a")
        self.assertEqual(second["cache_key_fingerprint"], "synth-fp-b")
        self.assertTrue(second["cache_hit"])
        self.assertFalse(second["actual_incurred"])
        self.assertTrue(second["attributed"])
        self.assertFalse(second["deduplicated"])
        self.assertEqual(result["synthesis_actual_cost"]["llm_calls"], 1)
        self.assertEqual(result["synthesis_attributed_cost"]["llm_calls"], 2)


if __name__ == "__main__":
    unittest.main()
