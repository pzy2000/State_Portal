from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml
from PIL import Image

from scripts import check_issue_completeness as issue_checker
from warptest import (
    c1_golden_artifacts,
    cost,
    fuzzing,
    gui_agent,
    p5_aggregate,
    p5_scale,
    p5_stats,
    run_context,
    state_loop,
)
from warptest.c1_budgets import C1_STEP_BUDGET, C1_STEP_BUDGETS, C1_TASK_BUDGET_MS
from warptest.c1_evaluation import materialize_legacy_spec
from warptest.c2_provenance import c2_harness_hash, c2_runtime_input_paths, validate_c2_repositories_clean
from warptest.model_request import MODEL_REQUEST_PROFILES_ENV
from warptest.synthesis import (
    build_explore_initial_prompt,
    generated_spec_contract_errors,
)


ROOT = Path(__file__).resolve().parents[1]
C1_BACKGROUND_PROTOCOL = yaml.safe_load(
    (ROOT / "benchmarks/p5/manifest.yaml").read_text(encoding="utf-8")
)["c1_background_pixel_protocol"]
_SELF_SYNTHESIZING_C1_ARMS = frozenset(p5_scale.C1_SELF_SYNTHESIZING_ARMS)
_DECLARED_C1_HOSTS = yaml.safe_load(
    (ROOT / "benchmarks/p5/c1_tasks.yaml").read_text(encoding="utf-8")
)["benchmark_hosts"]
_AGGREGATE_FIXTURE_APPLICATION_REVISIONS = {
    benchmark: {
        "display_name": host["display_name"],
        "repo": host["repo"],
        "gitmodules_url": host["gitmodules_url"],
        "revision": host["revision"],
    }
    for benchmark, host in _DECLARED_C1_HOSTS.items()
}
_AGGREGATE_FIXTURE_ENVIRONMENT = {
    "schema_version": 1,
    "declared": {
        "harness_revision": "aggregate-fixture",
        "os": "test-os",
        "architecture": "test-architecture",
        "physical_screen": [1, 1],
    },
    "observed": {
        "os": "test-os",
        "architecture": "test-architecture",
        "physical_screen": [1, 1],
    },
    "environment_fingerprint": "e" * 64,
    "status": "matched",
    "mismatches": [],
}


def _aggregate_fixture_application_revisions(_root: str | Path) -> dict:
    return copy.deepcopy(_AGGREGATE_FIXTURE_APPLICATION_REVISIONS)


def _aggregate_fixture_environment(
    _root: str | Path, *, manifest: dict | None = None
) -> dict:
    del manifest
    return copy.deepcopy(_AGGREGATE_FIXTURE_ENVIRONMENT)


def _stamp_single_attempt_ledger(
    result: dict,
    *,
    task_actual_cost: dict,
    is_state: bool,
) -> None:
    """Build the same hash-bound one-attempt ledger as the live producers."""

    empty = cost.CostMeter().summary()
    synthesis_attributed = cost.merge_summaries(
        [result.get("synthesis_attributed_cost") or empty]
    )
    synthesis_actual = cost.merge_summaries(
        [result.get("synthesis_actual_cost") or empty]
    )
    synthesis_transient = cost.merge_summaries(
        [result.get("synthesis_transient_actual_cost") or empty]
    )
    result["synthesis_attributed_cost"] = synthesis_attributed
    result["synthesis_actual_cost"] = synthesis_actual
    result["synthesis_transient_actual_cost"] = synthesis_transient
    if is_state:
        result.setdefault("synthesis_cache_hit", False)
        result.setdefault("synthesis_cache_fingerprint", None)
    synthesis_accounting = {
        "actual_cost": synthesis_actual,
        "transient_actual_cost": synthesis_transient,
        "attributed_cost": synthesis_attributed,
    }
    attempt = cost.merge_summaries([task_actual_cost, synthesis_attributed])
    attempt.update(
        {
            "attempt_status": "treatment",
            "retry_category": None,
            "retry_reason": None,
            "task_actual_cost": copy.deepcopy(task_actual_cost),
            "synthesis_accounting": copy.deepcopy(synthesis_accounting),
        }
    )
    attempts, ledger_hash = state_loop._bind_attempt_ledger(
        [attempt], task_id=str(result["task_id"])
    )
    result["attempt_costs"] = attempts
    result["attempt_count"] = 1
    result["attempt_ledger_hash"] = ledger_hash
    result["cost"] = cost.merge_summaries(
        [task_actual_cost, synthesis_attributed]
    )
    result["actual_cost"] = cost.merge_summaries(
        [task_actual_cost, synthesis_actual]
    )
    result["transient_infrastructure_cost"] = cost.merge_summaries(
        [synthesis_transient]
    )


def _recompute_attempt_ledger(result: dict, attempts: list[dict], *, is_state: bool) -> None:
    """Rebind a synthetic multi-attempt fixture and recompute all three ledgers."""

    rebound, ledger_hash = state_loop._bind_attempt_ledger(
        attempts, task_id=str(result["task_id"])
    )
    result["attempt_costs"] = rebound
    result["attempt_count"] = len(rebound)
    result["attempt_ledger_hash"] = ledger_hash
    task_costs = [attempt["task_actual_cost"] for attempt in rebound]
    synthesis_actual = cost.merge_summaries(
        [attempt["synthesis_accounting"]["actual_cost"] for attempt in rebound]
    )
    synthesis_transient = cost.merge_summaries(
        [
            attempt["synthesis_accounting"]["transient_actual_cost"]
            for attempt in rebound
        ]
    )
    synthesis_attributed = cost.merge_summaries(
        [
            attempt["synthesis_accounting"]["attributed_cost"]
            for attempt in rebound
        ]
    )
    result["synthesis_actual_cost"] = synthesis_actual
    result["synthesis_transient_actual_cost"] = synthesis_transient
    result["synthesis_attributed_cost"] = synthesis_attributed
    result["cost"] = cost.merge_summaries([task_costs[-1], synthesis_attributed])
    result["actual_cost"] = cost.merge_summaries([*task_costs, synthesis_actual])
    result["transient_infrastructure_cost"] = cost.merge_summaries(
        [*task_costs[:-1], synthesis_transient]
    )


def _write_p5_informative_png(path: Path, *, seed: int) -> None:
    image = Image.new("RGB", (192, 192))
    pixels = image.load()
    for y in range(192):
        for x in range(192):
            pixels[x, y] = (
                (x * 37 + y * 17 + seed) % 256,
                (x * 11 + y * 43 + seed * 3) % 256,
                (x * y + x * 7 + y * 13 + seed * 5) % 256,
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _write_test_c1_tasks(root: Path, *, task_id: str = "t1") -> Path:
    path = root / "tasks.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "tasks": [
                    {
                        "id": task_id,
                        "random_control": {
                            "mode": "experiment_seed",
                            "seeds": [0, 1, 2],
                            "algorithm": "mulberry32",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_valid_c2_suite(
    report_dir: Path, run_meta: dict | None = None
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / "memory-baseline-suite.json"
    path.write_text(
        json.dumps(
            {
                "formal_validity": {"status": "valid"},
                "run_context": run_meta or {},
            }
        ),
        encoding="utf-8",
    )
    return path


class RunContextTests(unittest.TestCase):
    def test_manifest_hash_is_stable_and_order_insensitive(self) -> None:
        a = {"models": ["x", "y"], "seeds": 3}
        b = {"seeds": 3, "models": ["x", "y"]}
        self.assertEqual(run_context.manifest_hash(a), run_context.manifest_hash(b))
        self.assertNotEqual(run_context.manifest_hash(a), run_context.manifest_hash({"seeds": 4}))

    def test_error_category_classifies_transient_vs_fatal(self) -> None:
        self.assertEqual(run_context.error_category(RuntimeError("HTTP 429 Too Many Requests")), "rate_limit")
        self.assertEqual(run_context.error_category(RuntimeError("Connection timed out")), "infra")
        self.assertEqual(
            run_context.error_category(
                RuntimeError("HTTP Error 500: Internal Server Error")
            ),
            "infra",
        )
        self.assertEqual(run_context.error_category(RuntimeError("Visual judge returned no choices.")), "infra")
        self.assertIsNone(run_context.error_category(ValueError("model returned an unusable spec")))

    def test_single_request_tpm_ceiling_is_terminal_provider_capacity(self) -> None:
        class RateLimit(RuntimeError):
            status_code = 429
            body = {
                "error": {
                    "message": (
                        "TPM limit exceeded: 414614+1019203 > 1000000 "
                        "for model doubao-seed-2-1-pro"
                    )
                }
            }

        error = RateLimit("rate limited")
        proof = run_context.provider_capacity_failure(error)
        self.assertEqual(proof["request_tokens"], 1019203)
        self.assertEqual(proof["limit_tokens"], 1000000)
        self.assertEqual(proof["proof"], "single_request_tokens_exceed_tpm_limit")
        self.assertIsNone(run_context.error_category(error))

        calls = 0

        def impossible():
            nonlocal calls
            calls += 1
            raise error

        with self.assertRaises(RateLimit):
            run_context.call_with_retries(
                impossible, max_attempts=6, sleep=lambda _seconds: None
            )
        self.assertEqual(calls, 1)

    def test_rolling_window_tpm_429_remains_retryable(self) -> None:
        class RateLimit(RuntimeError):
            status_code = 429
            body = {
                "error": {
                    "message": "TPM limit exceeded: 900000+200000 > 1000000"
                }
            }

        error = RateLimit("rate limited")
        self.assertIsNone(run_context.provider_capacity_failure(error))
        self.assertEqual(run_context.error_category(error), "rate_limit")

    def test_call_with_retries_retries_transient_then_succeeds(self) -> None:
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("rate limit hit")
            return "ok"

        value, history = run_context.call_with_retries(flaky, max_attempts=5, sleep=lambda _s: None)
        self.assertEqual(value, "ok")
        self.assertEqual(calls["n"], 3)
        self.assertEqual([h["status"] for h in history], ["retryable_error", "retryable_error", "ok"])

    def test_call_with_retries_does_not_retry_fatal(self) -> None:
        calls = {"n": 0}

        def fatal() -> str:
            calls["n"] += 1
            raise ValueError("bad model output")

        with self.assertRaises(ValueError):
            run_context.call_with_retries(fatal, max_attempts=5, sleep=lambda _s: None)
        self.assertEqual(calls["n"], 1)

    def test_stamp_payload_self_generates_when_no_run_meta(self) -> None:
        payload: dict = {"experiment": "state_fuzzing"}
        run_context.stamp_payload(payload)
        ctx = payload["run_context"]
        self.assertEqual(ctx["experiment_id"], "state_fuzzing")
        self.assertIn("run_id", ctx)
        self.assertIn("retry_history", ctx)


class VisualJudgeEnvTests(unittest.TestCase):
    def test_visual_judge_env_values_follow_manifest_without_storing_key(self) -> None:
        manifest = {
            "api": {"key_env": "GATEWAY_API_KEY", "base_url": "http://gateway.example/v1"},
            "visual_judge": {"key_env": "GATEWAY_API_KEY", "base_url": "http://judge/v1", "model": "glm-5.2"},
        }
        with patch.dict(os.environ, {"GATEWAY_API_KEY": "secret-key"}, clear=True):
            values = p5_scale.visual_judge_env_values(manifest)
        self.assertEqual(
            values,
            {
                "WARPTEST_VISUAL_JUDGE_API_KEY": "secret-key",
                "WARPTEST_VISUAL_JUDGE_BASE_URL": "http://judge/v1",
                "WARPTEST_VISUAL_JUDGE_MODEL": "glm-5.2",
            },
        )
        self.assertNotIn("GATEWAY_API_KEY", values)

    def test_manifest_visual_judge_env_forces_route_then_restores_ambient_value(self) -> None:
        manifest = {
            "api": {"key_env": "GATEWAY_API_KEY", "base_url": "http://gateway.example/v1"},
            "visual_judge": {"model": "glm-5.2"},
        }
        env = {
            "GATEWAY_API_KEY": "secret-key",
            "WARPTEST_VISUAL_JUDGE_MODEL": "override-model",
        }
        with patch.dict(os.environ, env, clear=True):
            with p5_scale._manifest_visual_judge_env(manifest):
                self.assertEqual(os.environ["WARPTEST_VISUAL_JUDGE_API_KEY"], "secret-key")
                self.assertEqual(os.environ["WARPTEST_VISUAL_JUDGE_BASE_URL"], "http://gateway.example/v1")
                self.assertEqual(os.environ["WARPTEST_VISUAL_JUDGE_MODEL"], "glm-5.2")
            self.assertNotIn("WARPTEST_VISUAL_JUDGE_API_KEY", os.environ)
            self.assertNotIn("WARPTEST_VISUAL_JUDGE_BASE_URL", os.environ)
            self.assertEqual(os.environ["WARPTEST_VISUAL_JUDGE_MODEL"], "override-model")

    def test_formal_c1_cache_environment_is_run_scoped_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "cache"
            with patch.dict(
                os.environ,
                {
                    "WARPTEST_C1_CACHE_ROOT": "ambient-cache",
                    "WARPTEST_C1_DISABLE_VISUAL_CACHE": "0",
                },
                clear=False,
            ):
                with p5_scale._formal_c1_cache_env(root):
                    self.assertEqual(
                        os.environ["WARPTEST_C1_CACHE_ROOT"], str(root.resolve())
                    )
                    self.assertEqual(os.environ["WARPTEST_C1_DISABLE_VISUAL_CACHE"], "1")
                self.assertEqual(os.environ["WARPTEST_C1_CACHE_ROOT"], "ambient-cache")
                self.assertEqual(os.environ["WARPTEST_C1_DISABLE_VISUAL_CACHE"], "0")

    def test_p5_c1_context_forces_registered_provider_timeouts_and_restores(self) -> None:
        timeout_envs = (
            "WARPTEST_GUI_AGENT_TIMEOUT",
            "WARPTEST_SYNTHESIS_TIMEOUT",
            "WARPTEST_VISUAL_BRIDGE_TIMEOUT",
            "WARPTEST_VISUAL_JUDGE_TIMEOUT",
        )
        manifest = p5_scale.load_manifest(ROOT / "benchmarks/p5/manifest.yaml")
        expected = format(float(manifest["api"]["provider_timeout_s"]), ".15g")
        observed: dict[str, str | None] = {}
        with (
            tempfile.TemporaryDirectory() as temp,
            patch(
                "warptest.p5_scale.validate_c1_environment",
                return_value={},
            ),
            patch(
                "warptest.p5_scale.c1_application_revisions",
                return_value={},
            ),
        ):
            orchestrator = p5_scale.P5Orchestrator(
                ROOT,
                manifest,
                run_dir=Path(temp) / "p5",
                models=["deterministic"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
            )

            def inspect_timeout_context(_arms: list[str]) -> None:
                observed.update({name: os.environ.get(name) for name in timeout_envs})

            ambient = {
                "WARPTEST_GUI_AGENT_TIMEOUT": "11",
                "WARPTEST_VISUAL_BRIDGE_TIMEOUT": "22",
                "UNRELATED_ENV": "preserved",
            }
            with (
                patch.dict(os.environ, ambient, clear=True),
                patch(
                    "warptest.p5_scale.preflight_c1_sandboxes",
                    return_value={},
                ),
                patch.object(
                    orchestrator,
                    "_run_c1_latin_square",
                    side_effect=inspect_timeout_context,
                ),
            ):
                orchestrator.run()
                self.assertEqual(observed, {name: expected for name in timeout_envs})
                self.assertEqual(os.environ["WARPTEST_GUI_AGENT_TIMEOUT"], "11")
                self.assertEqual(os.environ["WARPTEST_VISUAL_BRIDGE_TIMEOUT"], "22")
                self.assertNotIn("WARPTEST_SYNTHESIS_TIMEOUT", os.environ)
                self.assertNotIn("WARPTEST_VISUAL_JUDGE_TIMEOUT", os.environ)
                self.assertEqual(os.environ["UNRELATED_ENV"], "preserved")


class MemoryBaselineSchedulingTests(unittest.TestCase):
    def test_c2_memory_runs_each_model_and_seed_in_parallel_isolated_dirs(self) -> None:
        manifest = {
            "arms": {
                "c2_memory_baseline": {
                    "split": "benchmarks/core/splits/evo_phase3.yaml",
                    "baseline_backends": ["amem", "mem0"],
                    "anchor_backend": "stateportal-anchor",
                    "iterations": 2,
                }
            },
            "retry": {"max_attempts": 1, "base_sleep_s": 0, "max_sleep_s": 0},
        }
        models = ["model-a", "model-b", "model-c", "model-d"]
        barrier = threading.Barrier(len(models))
        calls: list[dict] = []
        calls_lock = threading.Lock()

        def fake_suite(*_args, **kwargs):
            with calls_lock:
                calls.append(kwargs)
            barrier.wait(timeout=3)
            report_dir = Path(kwargs["report_dir"])
            _write_valid_c2_suite(report_dir, kwargs.get("run_meta"))
            return {"report_dir": str(report_dir)}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "p5"
            with (
                patch(
                    "warptest.p5_scale.c2_harness_hash",
                    return_value="test-c2-harness",
                ),
                patch(
                    "warptest.p5_scale.run_memory_baseline_suite",
                    side_effect=fake_suite,
                ),
            ):
                orchestrator = p5_scale.P5Orchestrator(
                    root,
                    manifest,
                    run_dir=run_dir,
                    models=models,
                    seeds=1,
                    arms=["c2_memory_baseline"],
                    agent_kind="deterministic",
                    memory_workers=4,
                )
                index = orchestrator.run()
                context_paths = list(
                    run_dir.glob("c2_memory_baseline/*/seed0/c2-invocation-context.json")
                )

        self.assertEqual(len(calls), 4)
        self.assertEqual({call["model"] for call in calls}, set(models))
        self.assertEqual({call["run_meta"]["seed"] for call in calls}, {0})
        report_dirs = {Path(call["report_dir"]) for call in calls}
        self.assertEqual(len(report_dirs), 4)
        self.assertTrue(all(path.name == "seed0" for path in report_dirs))
        rows = index["arms"]["c2_memory_baseline"]
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["seed"] for row in rows}, {0})
        self.assertTrue(all(row["status"] == "ok" for row in rows))
        self.assertEqual(index["c2_harness_hash"], "test-c2-harness")
        self.assertEqual(len(context_paths), len(models))

    def test_c2_activates_and_records_effective_model_request_profile(self) -> None:
        model = "kimi-k2.6@relay"
        profiles = {"kimi-k2.6": "thinking_disabled"}
        manifest = {
            "model_request_profiles": profiles,
            "arms": {
                "c2_memory_baseline": {
                    "split": "benchmarks/core/splits/evo_phase3.yaml",
                    "baseline_backends": ["amem", "mem0"],
                    "anchor_backend": "stateportal-anchor",
                    "iterations": 2,
                }
            },
        }
        observed: list[dict[str, object]] = []

        def fake_suite(*_args, **kwargs):
            active_profiles = p5_scale.configured_model_request_profiles()
            canonical_model = p5_scale.canonical_model_name(kwargs["model"])
            effective_profile = p5_scale.ModelRequestProfile(
                canonical_model,
                active_profiles.get(canonical_model, p5_scale.PROVIDER_DEFAULT),
            ).provenance()
            observed.append(
                {
                    "profiles": active_profiles,
                    "effective_profile": effective_profile,
                }
            )
            report_dir = Path(kwargs["report_dir"])
            _write_valid_c2_suite(report_dir, kwargs.get("run_meta"))
            return {"report_dir": str(report_dir)}

        ambient_profiles = '{"ambient-model":"provider_default"}'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "p5"
            with (
                patch.dict(
                    os.environ,
                    {MODEL_REQUEST_PROFILES_ENV: ambient_profiles},
                    clear=False,
                ),
                patch(
                    "warptest.p5_scale.c2_harness_hash",
                    return_value="test-c2-harness",
                ),
                patch(
                    "warptest.p5_scale.run_memory_baseline_suite",
                    side_effect=fake_suite,
                ),
            ):
                index = p5_scale.P5Orchestrator(
                    root,
                    manifest,
                    run_dir=run_dir,
                    models=[model],
                    seeds=1,
                    arms=["c2_memory_baseline"],
                    agent_kind="llm",
                ).run()
                restored_profiles = os.environ[MODEL_REQUEST_PROFILES_ENV]
            context = json.loads(
                next(
                    run_dir.glob(
                        "c2_memory_baseline/*/seed0/c2-invocation-context.json"
                    )
                ).read_text(encoding="utf-8")
            )
            effect_report = json.loads(
                next(
                    run_dir.glob(
                        "c2_memory_baseline/*/seed0/memory-baseline-suite.json"
                    )
                ).read_text(encoding="utf-8")
            )

        expected_profile = p5_scale.ModelRequestProfile(
            "kimi-k2.6", "thinking_disabled"
        ).provenance()
        self.assertEqual(
            observed,
            [{"profiles": profiles, "effective_profile": expected_profile}],
        )
        self.assertEqual(restored_profiles, ambient_profiles)
        self.assertEqual(context["schema_version"], 3)
        self.assertEqual(context["model_request_profiles"], profiles)
        self.assertEqual(
            context["model_request_profiles_sha256"],
            index["model_request_profiles_sha256"],
        )
        self.assertEqual(context["model_request_profile"], expected_profile)
        self.assertEqual(
            effect_report["run_context"]["model_request_profiles"], profiles
        )
        self.assertEqual(
            effect_report["run_context"]["model_request_profiles_sha256"],
            index["model_request_profiles_sha256"],
        )
        self.assertEqual(
            effect_report["run_context"]["model_request_profile"],
            expected_profile,
        )

    def test_c2_completed_requires_report_hash_and_valid_formal_evidence(self) -> None:
        manifest = {"arms": {"c2_memory_baseline": {}}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = _write_valid_c2_suite(root / "suite")
            with patch(
                "warptest.p5_scale.c2_harness_hash",
                return_value="test-c2-harness",
            ):
                orchestrator = p5_scale.P5Orchestrator(
                    root,
                    manifest,
                    run_dir=root / "p5",
                    models=["model"],
                    seeds=1,
                    arms=["c2_memory_baseline"],
                    resume=False,
                )
            orchestrator.resume = True
            row = {
                "model": "model",
                "seed": 0,
                "status": "ok",
                "report": str(report_path),
                "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            }
            orchestrator.index["c2_memory_baseline"] = [row]

            self.assertTrue(
                orchestrator._completed(
                    "c2_memory_baseline", model="model", seed=0
                )
            )
            row.pop("report_sha256")
            self.assertFalse(
                orchestrator._completed(
                    "c2_memory_baseline", model="model", seed=0
                )
            )
            row["report_sha256"] = "0" * 64
            self.assertFalse(
                orchestrator._completed(
                    "c2_memory_baseline", model="model", seed=0
                )
            )

            report_path.write_text(
                json.dumps({"formal_validity": {"status": "invalid"}}),
                encoding="utf-8",
            )
            row["report_sha256"] = hashlib.sha256(
                report_path.read_bytes()
            ).hexdigest()
            self.assertFalse(
                orchestrator._completed(
                    "c2_memory_baseline", model="model", seed=0
                )
            )

    def test_c2_invalid_suite_is_recorded_as_nonretryable_error(self) -> None:
        manifest = {
            "arms": {"c2_memory_baseline": {}},
            "retry": {"max_attempts": 3, "base_sleep_s": 0, "max_sleep_s": 0},
        }
        calls = 0

        def invalid_suite(*_args, **kwargs):
            nonlocal calls
            calls += 1
            report_dir = Path(kwargs["report_dir"])
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "memory-baseline-suite.json").write_text(
                json.dumps({"formal_validity": {"status": "invalid"}}),
                encoding="utf-8",
            )
            return {"report_dir": str(report_dir)}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch(
                    "warptest.p5_scale.c2_harness_hash",
                    return_value="test-c2-harness",
                ),
                patch(
                    "warptest.p5_scale.run_memory_baseline_suite",
                    side_effect=invalid_suite,
                ),
                self.assertRaisesRegex(
                    RuntimeError, "C2 memory baseline is incomplete"
                ),
            ):
                p5_scale.P5Orchestrator(
                    root,
                    manifest,
                    run_dir=root / "p5",
                    models=["model"],
                    seeds=1,
                    arms=["c2_memory_baseline"],
                ).run()
            index = json.loads(
                (root / "p5" / "p5-run-index.json").read_text(encoding="utf-8")
            )

        self.assertEqual(calls, 1)
        row = index["arms"]["c2_memory_baseline"][0]
        self.assertEqual(row["status"], "error")
        self.assertIsNone(row["report"])
        self.assertIsNone(row["report_sha256"])
        self.assertIn("formal_validity.status is not valid", row["error"])

    def test_c2_harness_binds_current_runtime_tasks_and_clean_game_pins(self) -> None:
        validate_c2_repositories_clean(ROOT)
        self.assertEqual(c2_harness_hash(ROOT), c2_harness_hash(ROOT))

    def test_c2_runtime_provenance_excludes_unrelated_c1_and_aggregation_modules(self) -> None:
        paths = {path.relative_to(ROOT).as_posix() for path in c2_runtime_input_paths(ROOT)}
        self.assertIn("warptest/evo.py", paths)
        self.assertIn("warptest/runner.py", paths)
        self.assertIn("warptest/runtime_lock.py", paths)
        self.assertNotIn("warptest/gui_agent.py", paths)
        self.assertNotIn("warptest/state_loop.py", paths)
        self.assertNotIn("warptest/p5_scale.py", paths)
        # C3 fuzz-injection code is edited independently of the C2 memory
        # baseline; it must never join the C2 static-import closure.
        self.assertNotIn("warptest/spacerpg_fuzz_profile.py", paths)
        self.assertNotIn("warptest/openra_fuzz.py", paths)
        self.assertNotIn("warptest/jynew_fuzz.py", paths)

    def test_c2_only_resume_ignores_unrelated_arm_manifest_edits(self) -> None:
        """A concurrent c3_fuzz/c1 manifest edit must not block a C2-only resume."""

        def make_manifest(c3_seeds: int) -> dict:
            return {
                "arms": {
                    "c2_memory_baseline": {"iterations": 2},
                    "c3_fuzz": {"seeds": c3_seeds},
                },
                "retry": {"max_attempts": 1, "base_sleep_s": 0, "max_sleep_s": 0},
            }

        def fake_suite(*_args, **kwargs):
            report_dir = Path(kwargs["report_dir"])
            _write_valid_c2_suite(report_dir, kwargs.get("run_meta"))
            return {"report_dir": str(report_dir)}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "p5"
            with (
                patch("warptest.p5_scale.c2_harness_hash", return_value="test-c2-harness"),
                patch("warptest.p5_scale.run_memory_baseline_suite", side_effect=fake_suite),
            ):
                p5_scale.P5Orchestrator(
                    root, make_manifest(3), run_dir=run_dir, models=["model"], seeds=1,
                    arms=["c2_memory_baseline"],
                ).run()
                # Editing an unrelated c3_fuzz section (e.g. mutation tuning
                # while C2 is still running) changes manifest_hash but must
                # not raise on a C2-only resume.
                index = p5_scale.P5Orchestrator(
                    root, make_manifest(99), run_dir=run_dir, models=["model"], seeds=1,
                    arms=["c2_memory_baseline"], resume=True,
                ).run()
        rows = index["arms"]["c2_memory_baseline"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "ok")

    def test_c2_only_resume_rejects_after_c2_arm_config_changed(self) -> None:
        def make_manifest(iterations: int) -> dict:
            return {
                "arms": {"c2_memory_baseline": {"iterations": iterations}},
                "retry": {"max_attempts": 1, "base_sleep_s": 0, "max_sleep_s": 0},
            }

        def fake_suite(*_args, **kwargs):
            report_dir = Path(kwargs["report_dir"])
            _write_valid_c2_suite(report_dir, kwargs.get("run_meta"))
            return {"report_dir": str(report_dir)}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "p5"
            with (
                patch("warptest.p5_scale.c2_harness_hash", return_value="test-c2-harness"),
                patch("warptest.p5_scale.run_memory_baseline_suite", side_effect=fake_suite),
            ):
                p5_scale.P5Orchestrator(
                    root, make_manifest(2), run_dir=run_dir, models=["model"], seeds=1,
                    arms=["c2_memory_baseline"],
                ).run()
                with self.assertRaisesRegex(ValueError, "C2 manifest config changed"):
                    p5_scale.P5Orchestrator(
                        root, make_manifest(5), run_dir=run_dir, models=["model"], seeds=1,
                        arms=["c2_memory_baseline"], resume=True,
                    )

    def test_c2_only_resume_rejects_legacy_index_without_manifest_fingerprint(self) -> None:
        manifest = {
            "arms": {"c2_memory_baseline": {"iterations": 2}},
            "retry": {"max_attempts": 1, "base_sleep_s": 0, "max_sleep_s": 0},
        }

        def fake_suite(*_args, **kwargs):
            report_dir = Path(kwargs["report_dir"])
            _write_valid_c2_suite(report_dir, kwargs.get("run_meta"))
            return {"report_dir": str(report_dir)}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "p5"
            with (
                patch("warptest.p5_scale.c2_harness_hash", return_value="test-c2-harness"),
                patch("warptest.p5_scale.run_memory_baseline_suite", side_effect=fake_suite),
            ):
                p5_scale.P5Orchestrator(
                    root, manifest, run_dir=run_dir, models=["model"], seeds=1,
                    arms=["c2_memory_baseline"],
                ).run()
            index_path = run_dir / "p5-run-index.json"
            legacy_index = json.loads(index_path.read_text(encoding="utf-8"))
            del legacy_index["c2_manifest_fingerprint"]
            index_path.write_text(json.dumps(legacy_index), encoding="utf-8")
            with (
                patch("warptest.p5_scale.c2_harness_hash", return_value="test-c2-harness"),
                self.assertRaisesRegex(ValueError, "recorded before c2_manifest_fingerprint"),
            ):
                p5_scale.P5Orchestrator(
                    root, manifest, run_dir=run_dir, models=["model"], seeds=1,
                    arms=["c2_memory_baseline"], resume=True,
                )

    def test_c2_memory_dry_run_counts_models_times_seeds(self) -> None:
        result = p5_scale.plan(
            {"arms": {}}, models=["a", "b", "c", "d"], seeds=3, arms=["c2_memory_baseline"]
        )
        self.assertEqual(result["invocations_per_arm"]["c2_memory_baseline"], 12)
        self.assertEqual(result["total_invocations"], 12)

    def test_c2_retry_reuses_task_progress_without_replaying_completed_prefix(self) -> None:
        manifest = {
            "arms": {
                "c2_memory_baseline": {
                    "split": "benchmarks/core/splits/evo_phase3.yaml",
                    "baseline_backends": ["amem", "mem0"],
                    "anchor_backend": "stateportal-anchor",
                    "iterations": 2,
                }
            },
            "retry": {"max_attempts": 2, "base_sleep_s": 0, "max_sleep_s": 0},
        }
        calls: list[bool] = []

        def flaky_suite(*_args, **kwargs):
            calls.append(bool(kwargs["resume"]))
            report_dir = Path(kwargs["report_dir"])
            if len(calls) == 1:
                progress = report_dir / "amem" / "train" / "evo-progress.json"
                progress.parent.mkdir(parents=True, exist_ok=True)
                progress.write_text("{}", encoding="utf-8")
                raise RuntimeError("gateway timed out")
            _write_valid_c2_suite(report_dir, kwargs.get("run_meta"))
            return {"report_dir": str(report_dir)}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("warptest.p5_scale.c2_harness_hash", return_value="test-c2-harness"),
                patch("warptest.p5_scale.run_memory_baseline_suite", side_effect=flaky_suite),
            ):
                orchestrator = p5_scale.P5Orchestrator(
                    root,
                    manifest,
                    run_dir=root / "p5",
                    models=["model"],
                    seeds=1,
                    arms=["c2_memory_baseline"],
                    agent_kind="deterministic",
                )
                index = orchestrator.run()

        self.assertEqual(calls, [False, True])
        self.assertEqual(index["arms"]["c2_memory_baseline"][0]["status"], "ok")

    def test_c2_provider_outage_stays_incomplete_after_retry_budget(self) -> None:
        manifest = {
            "arms": {
                "c2_memory_baseline": {
                    "split": "benchmarks/core/splits/evo_phase3.yaml",
                    "baseline_backends": ["amem", "mem0"],
                    "anchor_backend": "stateportal-anchor",
                    "iterations": 2,
                }
            },
            "retry": {"max_attempts": 2, "base_sleep_s": 0, "max_sleep_s": 0},
        }
        calls: list[bool] = []

        def outage_suite(*_args, **kwargs):
            calls.append(bool(kwargs["resume"]))
            report_dir = Path(kwargs["report_dir"])
            progress = report_dir / "amem" / "train" / "evo-progress.json"
            progress.parent.mkdir(parents=True, exist_ok=True)
            progress.write_text("{}", encoding="utf-8")
            raise RuntimeError("temporarily unavailable: C2 model gateway outage")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("warptest.p5_scale.c2_harness_hash", return_value="test-c2-harness"),
                patch("warptest.p5_scale.run_memory_baseline_suite", side_effect=outage_suite),
                self.assertRaisesRegex(RuntimeError, "C2 memory baseline is incomplete"),
            ):
                p5_scale.P5Orchestrator(
                    root,
                    manifest,
                    run_dir=root / "p5",
                    models=["model"],
                    seeds=1,
                    arms=["c2_memory_baseline"],
                    agent_kind="deterministic",
                ).run()

            index = json.loads((root / "p5" / "p5-run-index.json").read_text(encoding="utf-8"))

        self.assertEqual(calls, [False, True])
        row = index["arms"]["c2_memory_baseline"][0]
        self.assertEqual(row["status"], "error")
        self.assertIn("temporarily unavailable", row["error"])


class C1ManifestAndSchedulingTests(unittest.TestCase):
    def test_official_c1_manifest_has_one_exact_registered_task_source(self) -> None:
        manifest = p5_scale.load_manifest(ROOT / "benchmarks/p5/manifest.yaml")
        self.assertEqual(
            manifest["models"],
            [
                "gpt-5.6-terra@relay",
                "gemini-3.5-flash@relay",
                "kimi-k2.6@relay",
                "gpt-6-astra@relay",
            ],
        )
        self.assertEqual(
            manifest["model_request_profiles"],
            {
                "gpt-5.6-terra": "thinking_disabled",
                "gemini-3.5-flash": "thinking_disabled",
                "kimi-k2.6": "thinking_disabled",
                "gpt-6-astra": "thinking_disabled",
            },
        )
        self.assertEqual(
            manifest["runtime_lock"],
            {
                "scope": "machine-shared",
                "lock_dir": "/tmp/warptest-runtime-locks",
                "wait_timeout_s": 10800.0,
            },
        )
        task_paths = {
            manifest["arms"][arm]["tasks"]
            for arm in p5_scale.C1_ARMS
        }
        self.assertEqual(task_paths, {"benchmarks/p5/c1_tasks.yaml"})
        data = yaml.safe_load((ROOT / task_paths.pop()).read_text(encoding="utf-8"))
        tasks = data["tasks"]
        deep_ids = set(data["expansion_sets"]["deep30"]["task_ids"])
        legacy_tasks = [task for task in tasks if task["id"] not in deep_ids]
        task_semantics = hashlib.sha256(
            json.dumps(
                [
                    {
                        key: value
                        for key, value in task.items()
                        if key not in {"state_description", "goal_description"}
                    }
                    for task in legacy_tasks
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            task_semantics,
            "77912e80cc130a61465d34f13922e9845b86408a23a4e6a35091b899a3a86de6",
        )
        self.assertEqual(data["analysis_schema_version"], 1)
        self.assertEqual(
            data["tag_vocabulary"],
            [
                "c1",
                "deep-chain",
                "2048",
                "love",
                "spacerpg",
                "luanti",
                "openra",
                "jupyterlab",
                "libreoffice",
                "blender",
                "jynew",
                "pal3",
            ],
        )
        self.assertEqual(
            list(data["capability_vocabulary"]),
            [
                "multi-step-planning",
                "gui-localization",
                "state-persistence",
                "numeric-reasoning",
                "spatial-reasoning",
                "resource-management",
                "structured-artifact-editing",
            ],
        )
        self.assertEqual(len(tasks), manifest["c1_task_count"])
        self.assertEqual(
            len({task["id"] for task in tasks}), manifest["c1_task_count"]
        )
        self.assertEqual(
            Counter(task["benchmark"] for task in tasks),
            Counter(manifest["c1_benchmark_task_counts"]),
        )
        self.assertEqual(manifest["c1_task_count"], 61)
        self.assertEqual(
            manifest["c1_benchmark_task_counts"],
            {
                "2048": 8,
                "datingsim": 7,
                "spacerpg": 8,
                "luanti": 8,
                "openra": 5,
                "jupyterlab": 5,
                "libreoffice": 5,
                "blender": 5,
                "jynew": 5,
                "pal3": 5,
            },
        )
        self.assertEqual(len(deep_ids), 30)
        self.assertNotIn("love-resource-action", {task["id"] for task in tasks})
        capability_counts = Counter(
            capability
            for task in tasks
            for capability in task["capabilities"]
        )
        self.assertEqual(
            capability_counts,
            Counter(
                {
                    "multi-step-planning": 35,
                    "gui-localization": 52,
                    "state-persistence": 24,
                    "numeric-reasoning": 41,
                    "spatial-reasoning": 39,
                    "resource-management": 23,
                    "structured-artifact-editing": 23,
                }
            ),
        )
        capability_benchmarks = {
            capability: {
                task["benchmark"]
                for task in tasks
                if capability in task["capabilities"]
            }
            for capability in data["capability_vocabulary"]
        }
        self.assertTrue(
            all(len(benchmarks) >= 2 for benchmarks in capability_benchmarks.values())
        )
        for task in tasks:
            expected_tags = [
                "c1",
                next(
                    tag
                    for tag in data["tag_vocabulary"]
                    if tag == task["benchmark"]
                    or (
                        task["benchmark"] == "datingsim" and tag == "love"
                    )
                    or (
                        task["benchmark"] == "spacerpg" and tag == "spacerpg"
                    )
                ),
            ]
            if task["id"] in deep_ids:
                expected_tags.append("deep-chain")
            self.assertEqual(task["tags"], expected_tags)
            self.assertTrue(task["capabilities"])
            self.assertEqual(
                len(task["capabilities"]), len(set(task["capabilities"]))
            )
            self.assertLessEqual(
                set(task["capabilities"]),
                set(data["capability_vocabulary"]),
            )
            self.assertNotIn("goal_description", task)
            for field in ("preconditions", "reference_steps", "horizon_class"):
                self.assertTrue(task[field], (task["id"], field))
            self.assertIn(task["horizon_class"], {"short", "mid", "long"})
            self.assertTrue((ROOT / task["oracle_spec"]).exists())
            self.assertEqual(
                task["state_description"],
                state_loop.canonical_state_description(
                    task["preconditions"], task["reference_steps"]
                ),
            )
            spec = yaml.safe_load(
                (ROOT / task["oracle_spec"]).read_text(encoding="utf-8")
            )
            self.assertTrue(
                spec["evaluation"]["semantic"]["assertions"], task["id"]
            )
            visual = spec["evaluation"]["visual_oracle"]
            self.assertTrue(
                str(
                    visual.get("after_description")
                    or visual.get("description")
                    or ""
                ).strip(),
                task["id"],
            )
            self.assertTrue(
                visual.get("after_rubric") or visual.get("rubric"),
                task["id"],
            )
        loaded_tasks = {
            task.id: task
            for task in state_loop.load_state_loop_tasks(
                ROOT, "benchmarks/p5/c1_tasks.yaml"
            )
        }
        for task_id, task in loaded_tasks.items():
            self.assertEqual(
                list(task.capabilities),
                next(
                    row["capabilities"]
                    for row in tasks
                    if row["id"] == task_id
                ),
            )
            self.assertNotIn("capabilities", task.issue.public_payload())

        for task in tasks:
            if task["id"] not in deep_ids:
                continue
            # Deep-task prompt revisions after the original c1-deep30-v3 cut:
            # - c1-deep30-rng-free-v1: three 2048 tasks after removing the
            #   hidden Math.random precondition
            # - c1-deep30-v5-ui-save-slots: Unity roundtrips remapped to slots
            #   exposed by each game's public save/load UI
            # - c1-deep30-v6-player-reachable: the jynew battle-ready task after
            #   removing its private raw-key sentinel
            # - c1-deep30-v7-realistic-credits: the SpaceRPG contraband task after
            #   binding its start and goal to player-reachable credits
            # - c1-deep30-v8-console-syntax: PAL3 and jynew roundtrips after
            #   spelling out the console command names and arguments
            # - c1-deep30-v9-trimmed-wording: the JupyterLab workspace task after
            #   trimming its restated payload
            # - c1-deep30-v10-locatable-coordinates: the two Luanti roundtrips
            #   after anchoring absolute coordinates to visible landmarks
            # Results under a newer revision must not be pooled with older ones.
            self.assertIn(
                task["prompt_revision"],
                {
                    "c1-deep30-v3",
                    "c1-deep30-rng-free-v1",
                    "c1-deep30-random-control-v1",
                    "c1-deep30-v5-ui-save-slots",
                    "c1-deep30-v6-player-reachable",
                    "c1-deep30-v7-realistic-credits",
                    "c1-deep30-v8-console-syntax",
                    "c1-deep30-v9-trimmed-wording",
                    "c1-deep30-v10-locatable-coordinates",
                },
            )
            spec = yaml.safe_load(
                (ROOT / task["oracle_spec"]).read_text(encoding="utf-8")
            )
            self.assertGreaterEqual(len(spec["actions"]), 5)
            self.assertLessEqual(len(spec["actions"]), 8)
            generated_candidate = {
                key: value
                for key, value in materialize_legacy_spec(spec).items()
                if key not in {"evaluation", "visual_oracle"}
            }
            loaded_task = state_loop.load_state_loop_tasks(
                ROOT, "benchmarks/p5/c1_tasks.yaml"
            )[tasks.index(task)]
            self.assertEqual(
                generated_spec_contract_errors(
                    generated_candidate, task=loaded_task.issue
                ),
                [],
                task["id"],
            )
        for arm in ("c1_agent_s", "c1_agent_s_synth"):
            self.assertEqual(manifest["arms"][arm]["step_budgets"], C1_STEP_BUDGETS)
        self.assertEqual(
            manifest["arms"]["c1_agent_s"]["prompt_revision"],
            "c1-public-checklist-v4",
        )
        self.assertEqual(
            manifest["arms"]["c1_agent_s_synth"]["phase_b_prompt_revision"],
            "c1-public-checklist-v4",
        )
        self.assertEqual(
            manifest["arms"]["c1_agent_s_synth"]["max_explore_steps"], 16
        )
        self.assertTrue(all("task_order_offset" not in manifest["arms"][arm] for arm in p5_scale.C1_ARMS))

    def test_jynew_battle_ready_contract_has_no_private_raw_key(self) -> None:
        payload = yaml.safe_load(
            (ROOT / "benchmarks/p5/c1_tasks.yaml").read_text(encoding="utf-8")
        )
        task = next(
            row
            for row in payload["tasks"]
            if row["id"] == "jynew-battle-ready-loadout"
        )
        spec = yaml.safe_load(
            (ROOT / task["oracle_spec"]).read_text(encoding="utf-8")
        )
        public_and_hidden = json.dumps(
            {"task": task, "spec": spec},
            sort_keys=True,
        )
        semantic_contract = json.dumps(
            p5_aggregate._c1_formal_gui_surface(task["id"]),
            sort_keys=True,
        )
        private_key = "c1_" + "battle_ready"

        self.assertEqual(
            task["prompt_revision"],
            "c1-deep30-v6-player-reachable",
        )
        self.assertNotIn(private_key, public_and_hidden)
        self.assertNotIn(private_key, semantic_contract)
        self.assertNotIn(
            "jynew_set_key_value",
            {action["type"] for action in spec["actions"]},
        )
        self.assertNotIn(
            "jynew_key_value_equals",
            {
                assertion["type"]
                for assertion in spec["evaluation"]["semantic"]["assertions"]
            },
        )
        self.assertEqual(len(spec["actions"]), 7)
        self.assertEqual(len(spec["evaluation"]["semantic"]["assertions"]), 8)

    def test_registered_c1_budgets_are_uniform_across_every_task(self) -> None:
        """Budgets must stay one constant, never a function of task or horizon.

        A per-benchmark or per-horizon budget silently turns a horizon-grouped
        result into a benchmark-grouped result, so both budgets are pinned here.
        """

        manifest = p5_scale.load_manifest(ROOT / "benchmarks/p5/manifest.yaml")
        records = yaml.safe_load(
            (ROOT / "benchmarks/p5/c1_tasks.yaml").read_text(encoding="utf-8")
        )["tasks"]
        self.assertEqual(len(records), manifest["c1_task_count"])
        self.assertEqual(
            {int(record["budget_ms"]) for record in records}, {C1_TASK_BUDGET_MS}
        )
        # The manifest scalar is only the fallback for a task row without the
        # field, so it must agree with the value every task actually declares.
        self.assertEqual(
            float(manifest["c1_task_timeout_s"]), C1_TASK_BUDGET_MS / 1000.0
        )
        self.assertEqual(set(C1_STEP_BUDGETS.values()), {C1_STEP_BUDGET})

        tasks = state_loop.load_state_loop_tasks(ROOT, "benchmarks/p5/c1_tasks.yaml")
        self.assertEqual(
            {
                gui_agent._resolve_step_budget(
                    task,
                    dict(C1_STEP_BUDGETS),
                    default_steps=15,
                )
                for task in tasks
            },
            {C1_STEP_BUDGET},
        )
        # budget_ms must stay wired to the executed wall clock rather than decay
        # back into a field nothing reads.
        for resolve in (gui_agent._resolve_task_timeout_s, state_loop._resolve_task_timeout_s):
            self.assertEqual(
                {resolve(task, 1.0) for task in tasks},
                {C1_TASK_BUDGET_MS / 1000.0},
            )

    def test_standalone_gui_budget_uses_max_steps_until_explicitly_overridden(
        self,
    ) -> None:
        standalone = gui_agent._build_step_budgets(7, None)
        self.assertEqual(standalone, {"short": 7, "mid": 7, "long": 7})
        self.assertEqual(
            gui_agent._resolve_step_budget(
                SimpleNamespace(horizon_class="short"),
                standalone,
                default_steps=7,
            ),
            7,
        )
        self.assertEqual(
            gui_agent._resolve_step_budget(
                SimpleNamespace(horizon_class="custom"),
                standalone,
                default_steps=7,
            ),
            7,
        )

        partial = gui_agent._build_step_budgets(7, {"mid": 23})
        self.assertEqual(partial, {"short": 7, "mid": 23, "long": 7})
        self.assertEqual(
            gui_agent._resolve_step_budget(
                SimpleNamespace(horizon_class="mid"),
                partial,
                default_steps=7,
            ),
            23,
        )

    def test_administrative_rmst_tau_exceeds_the_task_wall_clock(self) -> None:
        manifest = p5_scale.load_manifest(ROOT / "benchmarks/p5/manifest.yaml")
        tau = manifest["stats"]["c1_administrative_rmst_tau_ms"]
        self.assertEqual(
            {horizon: {k: int(v) for k, v in endpoints.items()} for horizon, endpoints in tau.items()},
            p5_aggregate._DEFAULT_C1_ADMINISTRATIVE_RMST_TAU_MS,
        )
        for endpoints in tau.values():
            for value in endpoints.values():
                self.assertGreater(int(value), C1_TASK_BUDGET_MS)

    def test_default_dry_run_uses_registered_c1_task_count(self) -> None:
        result = p5_scale.run_p5_scale(ROOT, dry_run=True)
        manifest = p5_scale.load_manifest(ROOT / "benchmarks/p5/manifest.yaml")
        self.assertEqual(result["plan"]["arms"], list(p5_scale.C1_ARMS))
        self.assertEqual(result["plan"]["seeds"], 1)
        self.assertEqual(manifest["seed_base"], 0)
        self.assertEqual(result["plan"]["invocations_per_arm"], {arm: 4 for arm in p5_scale.C1_ARMS})
        self.assertEqual(
            result["plan"]["total_invocations"], 4 * len(p5_scale.C1_ARMS)
        )
        self.assertEqual(
            result["plan"]["total_c1_trials"],
            4 * len(p5_scale.C1_ARMS) * manifest["c1_task_count"],
        )
        # The paper's frozen formal set is the complete five-arm matrix.
        self.assertIn("c1_agent_scalecua", p5_scale.C1_ARMS)
        self.assertIn("c1_agent_scalecua_synth", p5_scale.C1_ARMS)
        self.assertEqual(
            p5_scale.C1_FORMAL_PAPER_ARMS,
            tuple(p5_scale.C1_ARMS),
        )
        self.assertIsNone(p5_scale.cost_env_value({"cost": {"verified": False, "usd_per_mtok_by_model": {"default": 1}}}))

    def test_minimal_state_agent_is_explicit_nonformal_diagnostic_arm(self) -> None:
        result = p5_scale.run_p5_scale(
            ROOT,
            arms=["c1_state_minimal_agent"],
            seeds=1,
            dry_run=True,
        )
        manifest = p5_scale.load_manifest(ROOT / "benchmarks/p5/manifest.yaml")
        self.assertEqual(result["plan"]["arms"], ["c1_state_minimal_agent"])
        self.assertEqual(
            result["plan"]["invocations_per_arm"],
            {"c1_state_minimal_agent": 4},
        )
        self.assertEqual(
            result["plan"]["total_c1_trials"],
            4 * manifest["c1_task_count"],
        )
        cfg = manifest["arms"]["c1_state_minimal_agent"]
        self.assertIs(cfg["formal"], False)
        self.assertIs(cfg["excluded_from_formal"], True)
        self.assertEqual(cfg["transition_action_source"], "source_agent_v2")
        self.assertEqual(cfg["source_action_max_steps"], 16)
        self.assertEqual(cfg["source_action_max_tokens"], 8192)
        self.assertNotIn("minimal_action_max_actions", cfg)
        self.assertEqual(
            result["plan"]["paired_start_preparation_tasks"],
            manifest["c1_task_count"],
        )
        self.assertEqual(
            result["plan"]["paired_start_preparation"],
            {
                "protocol": "c1-paired-start-gate-v1",
                "execution": "lazy_first_consumer_before_transition",
                "scheduler": "inherits_c1_inner_worker_and_runtime_locks",
                "trial_denominator_increment": 0,
            },
        )
        self.assertEqual(
            result["plan"]["source_transition_protocol"],
            {
                "transition_action_source": "source_agent_v2",
                "max_steps": 16,
                "max_tokens": 8192,
                "format_repairs": 1,
                "prompt_revision": "c1-source-transition-v2",
                "pair_start_protocol": "c1-paired-start-gate-v1",
            },
        )
        self.assertEqual(result["plan"]["preflight_route_count"], 18)
        self.assertEqual(
            [route["name"] for route in result["plan"]["preflight_routes"]],
            [
                "decision:gpt-5.6-terra",
                "decision:gemini-3.5-flash",
                "decision:kimi-k2.6",
                "decision:gpt-6-astra",
                "source_transition:gpt-5.6-terra",
                "source_transition:gemini-3.5-flash",
                "source_transition:kimi-k2.6",
                "source_transition:gpt-6-astra",
                "synthesis:gpt-5.6-terra",
                "synthesis:gemini-3.5-flash",
                "synthesis:kimi-k2.6",
                "synthesis:gpt-6-astra",
                "visual_observer",
                "agent_s_grounder:gpt-5.6-terra",
                "agent_s_grounder:gemini-3.5-flash",
                "agent_s_grounder:kimi-k2.6",
                "agent_s_grounder:gpt-6-astra",
                "visual_judge",
            ],
        )
        self.assertEqual(tuple(p5_scale.C1_ARMS), (
            "c1_agent_s",
            "c1_agent_s_synth",
            "c1_state",
            "c1_agent_scalecua",
            "c1_agent_scalecua_synth",
        ))
        self.assertEqual(p5_scale.C1_START_GATE_PROTOCOL["schema_version"], 4)
        self.assertIn(
            "c1_state_minimal_agent",
            p5_scale.C1_START_GATE_PROTOCOL["arms_under_runtime_hold"],
        )

    def test_source_agent_budget_gate_compares_unselected_e_arm(self) -> None:
        manifest = copy.deepcopy(
            p5_scale.load_manifest(ROOT / "benchmarks/p5/manifest.yaml")
        )
        manifest["arms"]["c1_state"]["max_explore_steps"] = 15
        with (
            tempfile.TemporaryDirectory() as td,
            patch(
                "warptest.p5_scale.validate_c1_environment",
                return_value=copy.deepcopy(_AGGREGATE_FIXTURE_ENVIRONMENT),
            ),
            patch(
                "warptest.p5_scale.c1_application_revisions",
                return_value=copy.deepcopy(
                    _AGGREGATE_FIXTURE_APPLICATION_REVISIONS
                ),
            ),
        ):
            orchestrator = p5_scale.P5Orchestrator(
                ROOT,
                manifest,
                run_dir=Path(td),
                models=["gpt-5.6-terra@relay"],
                seeds=1,
                arms=["c1_state_minimal_agent"],
            )
            with self.assertRaisesRegex(
                RuntimeError, "source_agent_v2 must match"
            ):
                orchestrator._require_shared_synthesis_parity(
                    ["c1_state_minimal_agent"]
                )
        self.assertIn(
            "c1_state_minimal_agent",
            p5_scale.C1_START_GATE_PROTOCOL["shared_start_spec"]["arms"],
        )

    def test_selected_diagnostic_trial_universe_is_registered_before_formal_arm_runs(
        self,
    ) -> None:
        formal = p5_scale.load_manifest(ROOT / "benchmarks/p5/manifest.yaml")
        manifest = {
            "arms": {
                arm: {
                    "tasks": "tasks.yaml",
                    "explore": True,
                    "max_explore_steps": 16,
                }
                for arm in ("c1_state", "c1_state_minimal_agent")
            },
            "c1_trial_persistence": copy.deepcopy(
                formal["c1_trial_persistence"]
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_test_c1_tasks(root)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                manifest,
                run_dir=root / "run",
                models=["m"],
                seeds=1,
                arms=["c1_state", "c1_state_minimal_agent"],
                agent_kind="deterministic",
            )
            with (
                patch(
                    "warptest.p5_scale.c1_harness_hash",
                    return_value="test-harness",
                ),
                patch.object(
                    orchestrator,
                    "_run_c1_latin_square",
                    side_effect=RuntimeError("stop before first formal trial"),
                ),
                self.assertRaisesRegex(
                    RuntimeError, "stop before first formal trial"
                ),
            ):
                orchestrator.run()
            index = json.loads(
                (root / "run" / "p5-run-index.json").read_text(
                    encoding="utf-8"
                )
            )

        for arm in ("c1_state", "c1_state_minimal_agent"):
            self.assertEqual(len(index["arms"][arm]), 1)
            self.assertEqual(index["arms"][arm][0]["task_id"], "t1")
            self.assertEqual(index["arms"][arm][0]["status"], "pending")

    def test_minimal_state_agent_diagnostic_pairs_without_entering_formal_c1(self) -> None:
        state = [{
            "synthesis_model": "M",
            "seed": 0,
            "results": [{"task_id": "t", "e2e_success": False}],
        }]
        minimal = [{
            "synthesis_model": "M",
            "seed": 0,
            "results": [{
                "task_id": "t",
                "start_success": True,
                "e2e_success": True,
                "smoke_action_verdict": "pass",
                "minimal_smoke_agent": {"status": "repaired"},
            }],
        }]
        summary = p5_aggregate.c1_minimal_agent_diagnostic_from_reports(
            state, minimal
        )
        self.assertIs(summary["formal"], False)
        self.assertEqual(summary["smoke_passes"], 1)
        self.assertEqual(summary["paired_with_state"]["minimal_only_successes"], 1)

    def test_source_agent_v2_diagnostic_is_versioned_and_cannot_mix_v1(self) -> None:
        state = [
            {
                "synthesis_model": "M",
                "seed": 0,
                "results": [{"task_id": "t", "e2e_success": False}],
            }
        ]
        source = [
            {
                "schema": "c1-state-source-agent-v2",
                "schema_version": 2,
                "synthesis_model": "M",
                "seed": 0,
                "results": [
                    {
                        "task_id": "t",
                        "start_success": True,
                        "e2e_success": True,
                        "smoke_action_verdict": "pass",
                        "source_transition_agent": {
                            "status": "generated",
                            "assertions": [{"type": "state_equals"}],
                        },
                    }
                ],
            }
        ]
        summary = p5_aggregate.c1_minimal_agent_diagnostic_from_reports(
            state, source
        )
        self.assertEqual(summary["schema"], "c1-state-source-agent-diagnostic-v2")
        self.assertEqual(summary["schema_version"], 2)
        self.assertEqual(summary["action_plan_generated"], 1)
        self.assertEqual(summary["auxiliary_assertions_generated"], 1)

        with self.assertRaisesRegex(ValueError, "cannot mix"):
            p5_aggregate.c1_minimal_agent_diagnostic_from_reports(
                state,
                [
                    *source,
                    {
                        "schema": "c1-state-minimal-agent-v1",
                        "schema_version": 1,
                        "synthesis_model": "M",
                        "seed": 0,
                        "results": [],
                    },
                ],
            )

    def test_source_agent_core_validity_downgrades_missing_sha_to_warning(
        self,
    ) -> None:
        state = [
            {
                "synthesis_model": "M",
                "seed": 0,
                "results": [
                    {
                        "task_id": "t",
                        "accepted_start_id": "run:M:0:t",
                        "before_semantic_ok": False,
                        "before_visual_ok": False,
                        "start_success": False,
                        "goal_success": False,
                        "e2e_success": False,
                        "incomplete": False,
                        "cost": {"usage_complete": True},
                    }
                ],
            }
        ]
        source = [
            {
                "schema": "c1-state-source-agent-v2",
                "schema_version": 2,
                "synthesis_model": "M",
                "seed": 0,
                "results": [
                    {
                        "task_id": "t",
                        "accepted_start_id": "run:M:0:t",
                        "before_semantic_ok": False,
                        "before_visual_ok": False,
                        "start_success": False,
                        "goal_success": False,
                        "e2e_success": False,
                        "incomplete": False,
                        "cost": {"usage_complete": True},
                        "planner_called": False,
                        "planner_called_after_start": None,
                        "action_execution_started": False,
                        "source_transition_agent": {"status": "not_called"},
                    }
                ],
            }
        ]
        summary = p5_aggregate.c1_minimal_agent_diagnostic_from_reports(
            state, source
        )
        self.assertIs(summary["comparison_valid"], True)
        self.assertIs(summary["paired_goal_inference_allowed"], True)
        self.assertEqual(summary["core_audit"]["invalid_pair_count"], 0)
        self.assertGreater(summary["core_audit"]["audit_warning_count"], 0)
        self.assertEqual(
            summary["core_audit"]["sha_policy"],
            "optional_diagnostic_never_comparison_gate",
        )

    def test_source_agent_core_validity_fails_closed_on_missing_core_evidence(
        self,
    ) -> None:
        def core_row(task_id: str) -> dict[str, object]:
            return {
                "task_id": task_id,
                "accepted_start_id": f"run:M:0:{task_id}",
                "before_semantic_ok": False,
                "before_visual_ok": False,
                "start_success": False,
                "goal_success": False,
                "e2e_success": False,
                "incomplete": False,
                "cost": {"usage_complete": True},
            }

        def report(
            result_rows: list[dict[str, object]], *, source: bool = False
        ) -> dict[str, object]:
            payload: dict[str, object] = {
                "synthesis_model": "M",
                "seed": 0,
                "results": result_rows,
            }
            if source:
                payload.update(
                    schema="c1-state-source-agent-v2",
                    schema_version=2,
                )
                for row in result_rows:
                    row.update(
                        planner_called=False,
                        planner_called_after_start=None,
                        action_execution_started=False,
                        source_transition_agent={"status": "not_called"},
                    )
            return payload

        state_only = core_row("state-only")
        missing_outcomes = core_row("missing-outcomes")
        missing_outcomes.pop("goal_success")
        missing_outcomes.pop("e2e_success")
        missing_usage = core_row("missing-usage")
        missing_usage.pop("cost")
        cases = (
            (
                [report([core_row("paired"), state_only])],
                [report([core_row("paired")], source=True)],
                "state-only",
                "eprime_pair_missing",
            ),
            (
                [report([core_row("missing-outcomes")])],
                [report([missing_outcomes], source=True)],
                "missing-outcomes",
                "eprime_goal_success_missing",
            ),
            (
                [report([missing_usage])],
                [report([core_row("missing-usage")], source=True)],
                "missing-usage",
                "state_usage_evidence_missing",
            ),
        )

        for state, source, task_id, expected_failure in cases:
            with self.subTest(expected_failure=expected_failure):
                summary = p5_aggregate.c1_minimal_agent_diagnostic_from_reports(
                    state, source
                )
                self.assertIs(summary["comparison_valid"], False)
                self.assertIs(summary["paired_goal_inference_allowed"], False)
                invalid = {
                    row["task_id"]: row["failed_fields"]
                    for row in summary["core_audit"]["invalid_pairs"]
                }
                self.assertIn(expected_failure, invalid[task_id])

    def test_source_agent_core_validity_rejects_real_start_or_order_drift(
        self,
    ) -> None:
        state = [
            {
                "synthesis_model": "M",
                "seed": 0,
                "results": [
                    {
                        "task_id": "t",
                        "accepted_start_id": "run:M:0:t",
                        "before_semantic_ok": True,
                        "before_visual_ok": True,
                        "start_success": True,
                        "goal_success": False,
                        "e2e_success": False,
                        "incomplete": False,
                        "cost": {"usage_complete": True},
                    }
                ],
            }
        ]
        source = [
            {
                "schema": "c1-state-source-agent-v2",
                "schema_version": 2,
                "synthesis_model": "M",
                "seed": 0,
                "results": [
                    {
                        "task_id": "t",
                        "accepted_start_id": "different-run:M:0:t",
                        "before_semantic_ok": True,
                        "before_visual_ok": True,
                        "start_success": False,
                        "goal_success": False,
                        "e2e_success": False,
                        "incomplete": False,
                        "cost": {"usage_complete": True},
                        "planner_called": True,
                        "planner_called_after_start": False,
                        "action_execution_started": True,
                        "source_transition_agent": {"status": "generated"},
                    }
                ],
            }
        ]
        summary = p5_aggregate.c1_minimal_agent_diagnostic_from_reports(
            state, source
        )
        self.assertIs(summary["comparison_valid"], False)
        failed = summary["core_audit"]["invalid_pairs"][0]["failed_fields"]
        self.assertIn("accepted_start_id_mismatch", failed)
        self.assertIn("start_success_mismatch", failed)
        self.assertIn("planner_called_before_start_pass", failed)
        self.assertIn("action_execution_before_start_pass", failed)

    def test_shakeout_task_selection_plans_registered_subset_without_second_source(
        self,
    ) -> None:
        result = p5_scale.run_p5_scale(
            ROOT,
            models=["gpt-5.6-terra"],
            seeds=1,
            task_ids=["2048-merge-left", "love-office-work-coding", "spacerpg-station-save", "luanti-set-node"],
            dry_run=True,
        )
        self.assertEqual(result["plan"]["models"], ["gpt-5.6-terra@relay"])
        self.assertEqual(result["plan"]["tasks_per_c1_invocation"], 4)
        self.assertEqual(
            result["plan"]["total_invocations"], len(p5_scale.C1_ARMS)
        )
        self.assertEqual(
            result["plan"]["total_c1_trials"], len(p5_scale.C1_ARMS) * 4
        )

    def test_lightweight_manifest_registers_two_models_and_tasks_per_game(self) -> None:
        manifest_path = (
            ROOT / "benchmarks/p5/c1_lightweight_gpt56_terra_kimi_k26.yaml"
        )
        manifest = p5_scale.load_manifest(manifest_path)
        result = p5_scale.run_p5_scale(
            ROOT,
            manifest_path=manifest_path,
            dry_run=True,
        )
        task_rows = yaml.safe_load(
            (ROOT / "benchmarks/p5/c1_tasks.yaml").read_text(encoding="utf-8")
        )["tasks"]
        by_id = {str(row["id"]): str(row["benchmark"]) for row in task_rows}
        selected = result["plan"]["task_ids"]
        self.assertEqual(
            result["plan"]["models"],
            ["gpt-5.6-terra@relay", "kimi-k2.6@relay"],
        )
        self.assertEqual(result["plan"]["seeds"], 1)
        self.assertEqual(result["plan"]["tasks_per_c1_invocation"], 20)
        self.assertEqual(result["plan"]["total_invocations"], 10)
        self.assertEqual(result["plan"]["total_c1_trials"], 200)
        self.assertEqual(manifest["c1_task_timeout_s"], C1_TASK_BUDGET_MS / 1000.0)
        self.assertEqual(
            manifest["arms"]["c1_agent_s"]["step_budgets"],
            C1_STEP_BUDGETS,
        )
        self.assertEqual(
            manifest["model_request_profiles"],
            {
                "gpt-5.6-terra": "thinking_disabled",
                "kimi-k2.6": "thinking_disabled",
            },
        )
        self.assertEqual(
            manifest["calibration"]["schema"],
            "c1-calibration-v4",
        )
        self.assertEqual(
            set(manifest["c1_background_pixel_protocol"]["benchmarks"]),
            set(manifest["c1_benchmark_task_counts"]),
        )
        self.assertEqual(
            Counter(by_id[task_id] for task_id in selected),
            Counter({benchmark: 2 for benchmark in set(by_id.values())}),
        )
        self.assertEqual(
            manifest["arms"]["c1_agent_s"]["prompt_revision"],
            "c1-public-checklist-v4",
        )
        self.assertEqual(
            manifest["arms"]["c1_agent_s_synth"]["phase_b_prompt_revision"],
            "c1-public-checklist-v4",
        )

    def test_model_seed_blocks_use_cyclic_three_arm_latin_square(self) -> None:
        manifest = {
            "arms": {arm: {} for arm in p5_scale.C1_ARMS},
            "c1_background_pixel_protocol": copy.deepcopy(C1_BACKGROUND_PROTOCOL),
        }
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = p5_scale.P5Orchestrator(
                tmp, manifest, run_dir=Path(tmp) / "run", models=["m1", "m2"], seeds=3,
                arms=list(p5_scale.C1_ARMS), agent_kind="deterministic",
            )
            calls: list[tuple[str, str, int]] = []
            with (
                patch.object(orchestrator, "_run_c1_state_invocation", side_effect=lambda model, seed: calls.append(("c1_state", model, seed))),
                patch.object(orchestrator, "_run_gui_invocation", side_effect=lambda arm, *, start_mode, model, seed: calls.append((arm, model, seed))),
            ):
                orchestrator._run_c1_latin_square(list(p5_scale.C1_ARMS))
        blocks = orchestrator.latin_square["blocks"]
        arm_count = len(p5_scale.C1_ARMS)
        expected_offsets = [
            (model_index * 3 + seed) % arm_count
            for model_index in range(2)
            for seed in range(3)
        ]
        self.assertEqual([block["offset"] for block in blocks], expected_offsets)
        base = list(p5_scale.C1_ARMS)
        for block in blocks:
            offset = block["offset"]
            self.assertEqual(block["arms"], base[offset:] + base[:offset])
        self.assertEqual(len(calls), arm_count * 6)
        self.assertEqual(len(orchestrator.execution_order), arm_count * 6)

    def test_formal_scheduler_has_no_foreground_console_precondition(self) -> None:
        self.assertFalse(hasattr(p5_scale, "require_unlocked_native_session"))

    def test_gui_arms_schedule_every_model_seed_with_policy_and_budgets(self) -> None:
        arms = {
            "c1_agent_s": {"tasks": "tasks.yaml", "policy_kind": "agent_s", "start_mode": "from_start", "step_budgets": dict(C1_STEP_BUDGETS)},
            "c1_agent_s_synth": {"tasks": "tasks.yaml", "policy_kind": "agent_s", "start_mode": "synthesized", "step_budgets": dict(C1_STEP_BUDGETS)},
        }
        manifest = {
            "api": {"key_env": "C1_TEST_KEY", "base_url": "http://example.invalid/v1"},
            "visual_bridge": {"observer_model": "observer@relay", "grounder_model": "grounder@relay"},
            "arms": arms,
            "c1_background_pixel_protocol": copy.deepcopy(C1_BACKGROUND_PROTOCOL),
            "retry": {"max_attempts": 1, "base_sleep_s": 0, "max_sleep_s": 0},
        }
        calls: list[dict] = []

        def fake_gui(*_args, **kwargs):
            calls.append(kwargs)
            report_dir = Path(kwargs["report_dir"])
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "gui-agent.json").write_text('{"complete": true}', encoding="utf-8")
            return {"report_dir": str(report_dir), "complete": True}

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"C1_TEST_KEY": "secret"}, clear=False):
            _write_test_c1_tasks(Path(tmp))
            expected_synthesis_cache = Path(tmp) / "run" / "c1-self-synthesis-cache"
            orchestrator = p5_scale.P5Orchestrator(
                tmp, manifest, run_dir=Path(tmp) / "run", models=["m1", "m2"], seeds=3,
                arms=list(arms), agent_kind="llm",
            )
            with (
                patch("warptest.p5_scale.c1_harness_hash", return_value="test-harness"),
                patch("warptest.p5_scale.run_gui_agent_benchmark", side_effect=fake_gui),
            ):
                index = orchestrator.run()
        self.assertEqual(len(calls), 12)
        self.assertEqual({call["seed"] for call in calls}, {0, 1, 2})
        self.assertEqual({call["policy_kind"] for call in calls}, {"agent_s"})
        self.assertEqual({call["start_mode"] for call in calls}, {"from_start", "synthesized"})
        self.assertTrue(all(call["step_budgets"] == dict(C1_STEP_BUDGETS) for call in calls))
        self.assertTrue(all(call["observer_model"] == "observer@relay" for call in calls))
        self.assertTrue(all(call["background_browser_headless"] is True for call in calls))
        self.assertEqual(len({id(call["global_stop_event"]) for call in calls}), 1)
        self.assertEqual(
            {Path(call["shared_synthesis_cache"]).resolve() for call in calls},
            {expected_synthesis_cache.resolve()},
        )
        self.assertEqual(sum(len(index["arms"][arm]) for arm in arms), 12)
        self.assertEqual(len(index["latin_square"]["blocks"]), 6)
        self.assertEqual(len(index["execution_order"]), 12)

    def test_state_arm_schedules_one_exact_seed_per_invocation(self) -> None:
        manifest = {
            "arms": {"c1_state": {"tasks": "tasks.yaml", "explore": True, "max_iters": 1}},
            "retry": {"max_attempts": 1, "base_sleep_s": 0, "max_sleep_s": 0},
        }
        calls: list[dict] = []

        def fake_state(*_args, **kwargs):
            calls.append(kwargs)
            report_dir = Path(kwargs["report_dir"])
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "state-loop.json").write_text('{"complete": true}', encoding="utf-8")
            return {"report_dir": str(report_dir), "complete": True}

        with tempfile.TemporaryDirectory() as tmp:
            _write_test_c1_tasks(Path(tmp))
            orchestrator = p5_scale.P5Orchestrator(
                tmp, manifest, run_dir=Path(tmp) / "run", models=["m1", "m2"], seeds=3,
                arms=["c1_state"], agent_kind="deterministic",
            )
            with (
                patch("warptest.p5_scale.c1_harness_hash", return_value="test-harness"),
                patch("warptest.p5_scale.run_state_loop_benchmark", side_effect=fake_state),
            ):
                index = orchestrator.run()
        self.assertEqual(len(calls), 6)
        self.assertEqual({call["seed"] for call in calls}, {0, 1, 2})
        self.assertTrue(all(call["seeds"] == 1 for call in calls))
        self.assertTrue(all(call["background_browser_headless"] is True for call in calls))
        self.assertEqual(len({id(call["global_stop_event"]) for call in calls}), 1)
        self.assertEqual({row["seed"] for row in index["arms"]["c1_state"]}, {0, 1, 2})

    def test_formal_calibration_gate_blocks_before_gui_calls(self) -> None:
        manifest = {
            "arms": {"c1_agent_s": {}},
            "c1_background_pixel_protocol": copy.deepcopy(C1_BACKGROUND_PROTOCOL),
            "calibration": {
                "required": True,
                "artifact_env": "NO_SUCH_CALIBRATION_ENV",
                "luanti_input_protocol": "luanti_chat_command_exact_v1",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = p5_scale.P5Orchestrator(
                tmp, manifest, run_dir=Path(tmp) / "run", models=["m"], seeds=1,
                arms=["c1_agent_s"], agent_kind="llm",
            )
            with self.assertRaisesRegex(RuntimeError, "c1-calibration-v4 artifact"):
                orchestrator._require_calibration_gate()
            artifact = Path(tmp) / "calibration.json"
            artifact.write_text(json.dumps({
                "schema_version": 2,
                "passed": True,
                "git_commit": orchestrator.git_commit,
                "manifest_hash": orchestrator.manifest_hash,
                "harness_hash": "test-harness",
                "api_provider": orchestrator._api_provider_provenance(),
                "observer_model": "claude-sonnet-5@relay",
                "grounder_model": "claude-sonnet-5@relay",
                "native_session": {
                    "platform": "macos",
                    "console_unlocked": True,
                    "accessibility_trusted": True,
                    "event_post_access": True,
                    "keyboard_input_sources_sha256": "c" * 64,
                },
                "browser": {
                    "passed": True,
                    "headless": True,
                    "frame": {"width": 1280, "height": 800, "device_scale_factor": 1.0},
                },
                "luanti": {
                    "passed": True,
                    "frame": {"width": 1280, "height": 720},
                    "worldmod_click_event": True,
                },
            }), encoding="utf-8")
            orchestrator.calibration_artifact = artifact
            with patch("warptest.p5_scale.c1_harness_hash", return_value="test-harness"):
                with self.assertRaisesRegex(RuntimeError, "c1-calibration-v4"):
                    orchestrator._require_calibration_gate()
            isolation = {
                "target_never_frontmost": True,
                "system_pointer_unchanged": True,
                "system_clipboard_unchanged": True,
                "foreground_text_unchanged": True,
            }
            calibration = {
                "schema_version": 4,
                "schema": "c1-calibration-v4",
                "passed": True,
                "git_commit": orchestrator.git_commit,
                "manifest_hash": orchestrator.manifest_hash,
                "harness_hash": "test-harness",
                "api_provider": orchestrator._api_provider_provenance(),
                "observer_model": "claude-sonnet-5@relay",
                "grounder_model": "claude-sonnet-5@relay",
                "background_pixel_protocol_sha256": (
                    orchestrator.c1_background_pixel_protocol_sha256
                ),
                "backends": {
                    benchmark: {
                        **identity,
                        "passed": True,
                        "foreground_isolation": isolation,
                    }
                    for benchmark, identity in C1_BACKGROUND_PROTOCOL[
                        "benchmarks"
                    ].items()
                },
            }
            artifact.write_text(json.dumps(calibration), encoding="utf-8")
            with patch("warptest.p5_scale.c1_harness_hash", return_value="test-harness"):
                orchestrator._require_calibration_gate()
            changed = json.loads(artifact.read_text(encoding="utf-8"))
            changed["backends"]["2048"]["passed"] = False
            artifact.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed during the run"):
                orchestrator._require_calibration_gate()

    def test_resume_uses_atomic_index_and_skips_completed_invocation(self) -> None:
        manifest = {
            "api": {"key_env": "C1_TEST_KEY", "base_url": "http://example.invalid/v1"},
            "arms": {"c1_agent_s": {"tasks": "tasks.yaml", "policy_kind": "agent_s", "start_mode": "from_start", "prompt_revision": "c1-public-checklist-v2", "step_budgets": dict(C1_STEP_BUDGETS)}},
            "c1_background_pixel_protocol": copy.deepcopy(C1_BACKGROUND_PROTOCOL),
            "retry": {"max_attempts": 1, "base_sleep_s": 0, "max_sleep_s": 0},
        }
        calls = {"count": 0}

        def fake_gui(*_args, **kwargs):
            calls["count"] += 1
            report_dir = Path(kwargs["report_dir"])
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "gui-agent.json").write_text('{"complete": true}', encoding="utf-8")
            return {"report_dir": str(report_dir), "complete": True}

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"C1_TEST_KEY": "secret"}, clear=False):
            _write_test_c1_tasks(Path(tmp))
            run_dir = Path(tmp) / "run"
            with (
                patch("warptest.p5_scale.c1_harness_hash", return_value="test-harness"),
                patch("warptest.p5_scale.run_gui_agent_benchmark", side_effect=fake_gui),
            ):
                p5_scale.P5Orchestrator(
                    tmp, manifest, run_dir=run_dir, models=["m"], seeds=1,
                    arms=["c1_agent_s"], agent_kind="llm",
                ).run()
                p5_scale.P5Orchestrator(
                    tmp, manifest, run_dir=run_dir, models=["m"], seeds=1,
                    arms=["c1_agent_s"], agent_kind="llm", resume=True,
                ).run()
            self.assertEqual(calls["count"], 1)
            index_path = run_dir / "p5-run-index.json"
            self.assertTrue(index_path.exists())
            self.assertFalse((run_dir / "p5-run-index.json.tmp").exists())
            prompt_revisions = json.loads(
                index_path.read_text(encoding="utf-8")
            )["c1_prompt_revisions"]
            self.assertEqual(
                prompt_revisions["arms"]["c1_agent_s"]["prompt_revision"],
                "c1-public-checklist-v2",
            )
            self.assertEqual(prompt_revisions["tasks"], {"t1": ""})

    def test_resume_rejects_runtime_lock_namespace_drift_before_producer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_test_c1_tasks(root)
            run_dir = root / "run"
            lock_a = (root / "machine-lock-a").resolve()
            lock_b = (root / "machine-lock-b").resolve()
            manifest = {
                "api": {
                    "key_env": "C1_TEST_KEY",
                    "base_url": "http://example.invalid/v1",
                },
                "runtime_lock": {
                    "scope": "machine-shared",
                    "lock_dir": str(lock_a),
                },
                "arms": {"c1_agent_s": {"tasks": "tasks.yaml", "policy_kind": "agent_s", "start_mode": "from_start"}},
                "c1_background_pixel_protocol": copy.deepcopy(C1_BACKGROUND_PROTOCOL),
                "retry": {"max_attempts": 1, "base_sleep_s": 0, "max_sleep_s": 0},
            }
            calls = Mock()

            def fake_gui(*_args, **kwargs):
                calls()
                report_dir = Path(kwargs["report_dir"])
                report_dir.mkdir(parents=True, exist_ok=True)
                (report_dir / "gui-agent.json").write_text(
                    '{"complete": true}', encoding="utf-8"
                )
                return {"report_dir": str(report_dir), "complete": True}

            with (
                patch.dict(
                    os.environ,
                    {
                        "C1_TEST_KEY": "secret",
                        "WARPTEST_RUNTIME_LOCK_DIR": str(lock_a),
                    },
                    clear=False,
                ),
                patch("warptest.p5_scale.c1_harness_hash", return_value="harness-a"),
                patch(
                    "warptest.p5_scale.run_gui_agent_benchmark",
                    side_effect=fake_gui,
                ),
            ):
                p5_scale.P5Orchestrator(
                    root,
                    manifest,
                    run_dir=run_dir,
                    models=["m"],
                    seeds=1,
                    arms=["c1_agent_s"],
                    agent_kind="llm",
                ).run()

            producer = Mock()
            with (
                patch.dict(
                    os.environ,
                    {
                        "C1_TEST_KEY": "secret",
                        "WARPTEST_RUNTIME_LOCK_DIR": str(lock_b),
                    },
                    clear=False,
                ),
                patch("warptest.p5_scale.run_gui_agent_benchmark", producer),
                self.assertRaisesRegex(RuntimeError, "manifest-registered"),
            ):
                p5_scale.P5Orchestrator(
                    root,
                    manifest,
                    run_dir=run_dir,
                    models=["m"],
                    seeds=1,
                    arms=["c1_agent_s"],
                    agent_kind="llm",
                    resume=True,
                )

            index_path = run_dir / "p5-run-index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["runtime_lock"]["lock_dir"] = str(lock_b)
            index_path.write_text(json.dumps(index), encoding="utf-8")
            with (
                patch.dict(
                    os.environ,
                    {
                        "C1_TEST_KEY": "secret",
                        "WARPTEST_RUNTIME_LOCK_DIR": str(lock_a),
                    },
                    clear=False,
                ),
                patch("warptest.p5_scale.c1_harness_hash", return_value="harness-a"),
                self.assertRaisesRegex(ValueError, "runtime_lock"),
            ):
                p5_scale.P5Orchestrator(
                    root,
                    manifest,
                    run_dir=run_dir,
                    models=["m"],
                    seeds=1,
                    arms=["c1_agent_s"],
                    agent_kind="llm",
                    resume=True,
                )
            calls.assert_called_once_with()
            producer.assert_not_called()

    def test_task_level_partial_resume_is_bound_to_atomic_invocation_provenance(self) -> None:
        manifest = {
            "api": {"key_env": "C1_TEST_KEY", "base_url": "http://example.invalid/v1"},
            "arms": {"c1_agent_s": {
                "tasks": "tasks.yaml",
                "policy_kind": "agent_s",
                "start_mode": "from_start",
                "step_budgets": dict(C1_STEP_BUDGETS),
            }},
            "c1_background_pixel_protocol": copy.deepcopy(C1_BACKGROUND_PROTOCOL),
            "retry": {"max_attempts": 1, "base_sleep_s": 0, "max_sleep_s": 0},
        }
        calls = {"partial": 0, "resume": 0}

        def write_partial(*_args, **kwargs):
            calls["partial"] += 1
            report_dir = Path(kwargs["report_dir"])
            context_path = report_dir / "p5-invocation-context.json"
            self.assertTrue(context_path.exists())
            self.assertFalse(context_path.with_name(context_path.name + ".tmp").exists())
            (report_dir / "gui-agent.json").write_text(
                json.dumps({
                    "schema_version": 2,
                    "complete": False,
                    "model": "m",
                    "seed": 0,
                    "results": [{"task_id": "t1", "incomplete": False}],
                }),
                encoding="utf-8",
            )
            raise RuntimeError("HTTP 429 temporary provider outage")

        def finish_from_partial(*_args, **kwargs):
            calls["resume"] += 1
            report_dir = Path(kwargs["report_dir"])
            prior = json.loads((report_dir / "gui-agent.json").read_text(encoding="utf-8"))
            self.assertEqual(prior["results"][0]["task_id"], "t1")
            (report_dir / "gui-agent.json").write_text('{"complete": true}', encoding="utf-8")
            return {"report_dir": str(report_dir), "complete": True}

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"C1_TEST_KEY": "secret"}, clear=False):
            root = Path(tmp)
            _write_test_c1_tasks(root)
            run_dir = root / "run"
            with (
                patch("warptest.p5_scale.c1_harness_hash", return_value="harness-a"),
                patch("warptest.p5_scale.run_gui_agent_benchmark", side_effect=write_partial),
            ):
                with self.assertRaisesRegex(RuntimeError, "429"):
                    p5_scale.P5Orchestrator(
                        root, manifest, run_dir=run_dir, models=["m"], seeds=1,
                        arms=["c1_agent_s"], agent_kind="llm",
                    ).run()

            arm_dir = run_dir / "c1_agent_s" / run_context.manifest_hash("m") / "seed0"
            context_path = arm_dir / "p5-invocation-context.json"
            context = json.loads(context_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {key: context[key] for key in ("arm", "model", "seed", "harness_hash")},
                {"arm": "c1_agent_s", "model": "m", "seed": 0, "harness_hash": "harness-a"},
            )
            self.assertEqual(context["git_commit"], json.loads((run_dir / "p5-run-index.json").read_text())["git_commit"])
            self.assertEqual(context["manifest_hash"], run_context.manifest_hash(manifest))
            self.assertIsNone(context["calibration"])
            self.assertEqual(context["task_set"]["task_ids"], ["t1"])
            self.assertIn("task_file_sha256", context["task_set"])
            self.assertIn("source_task_file_sha256", context["task_set"])

            with (
                patch("warptest.p5_scale.c1_harness_hash", return_value="harness-a"),
                patch("warptest.p5_scale.run_gui_agent_benchmark", side_effect=finish_from_partial),
            ):
                result = p5_scale.P5Orchestrator(
                    root, manifest, run_dir=run_dir, models=["m"], seeds=1,
                    arms=["c1_agent_s"], agent_kind="llm", resume=True,
                ).run()
            self.assertEqual(calls, {"partial": 1, "resume": 1})
            self.assertEqual(result["arms"]["c1_agent_s"][0]["status"], "ok")

    def test_resume_rejects_legacy_partial_before_calling_producer(self) -> None:
        manifest = {
            "api": {"key_env": "C1_TEST_KEY", "base_url": "http://example.invalid/v1"},
            "arms": {"c1_agent_s": {"tasks": "tasks.yaml", "policy_kind": "agent_s", "start_mode": "from_start"}},
            "c1_background_pixel_protocol": copy.deepcopy(C1_BACKGROUND_PROTOCOL),
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"C1_TEST_KEY": "secret"}, clear=False):
            root = Path(tmp)
            _write_test_c1_tasks(root)
            run_dir = root / "run"
            arm_dir = run_dir / "c1_agent_s" / run_context.manifest_hash("m") / "seed0"
            arm_dir.mkdir(parents=True)
            (arm_dir / "gui-agent.json").write_text(
                '{"schema_version": 2, "complete": false, "results": [{"task_id": "t1"}]}',
                encoding="utf-8",
            )
            producer = Mock()
            with (
                patch("warptest.p5_scale.c1_harness_hash", return_value="harness-a"),
                patch("warptest.p5_scale.run_gui_agent_benchmark", producer),
            ):
                with self.assertRaisesRegex(ValueError, "without invocation provenance"):
                    p5_scale.P5Orchestrator(
                        root, manifest, run_dir=run_dir, models=["m"], seeds=1,
                        arms=["c1_agent_s"], agent_kind="llm", resume=True,
                    ).run()
            producer.assert_not_called()

    def test_resume_rejects_changed_task_set_before_calling_producer(self) -> None:
        manifest = {
            "api": {"key_env": "C1_TEST_KEY", "base_url": "http://example.invalid/v1"},
            "arms": {"c1_agent_s": {"tasks": "tasks.yaml", "policy_kind": "agent_s", "start_mode": "from_start"}},
            "c1_background_pixel_protocol": copy.deepcopy(C1_BACKGROUND_PROTOCOL),
            "retry": {"max_attempts": 1, "base_sleep_s": 0, "max_sleep_s": 0},
        }

        def partial(*_args, **kwargs):
            report_dir = Path(kwargs["report_dir"])
            (report_dir / "gui-agent.json").write_text(
                '{"schema_version": 2, "complete": false, "results": [{"task_id": "t1", "incomplete": false}]}',
                encoding="utf-8",
            )
            raise RuntimeError("HTTP 429 temporary provider outage")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"C1_TEST_KEY": "secret"}, clear=False):
            root = Path(tmp)
            tasks_path = _write_test_c1_tasks(root)
            run_dir = root / "run"
            with (
                patch("warptest.p5_scale.c1_harness_hash", return_value="harness-a"),
                patch("warptest.p5_scale.run_gui_agent_benchmark", side_effect=partial),
            ):
                with self.assertRaises(RuntimeError):
                    p5_scale.P5Orchestrator(
                        root, manifest, run_dir=run_dir, models=["m"], seeds=1,
                        arms=["c1_agent_s"], agent_kind="llm",
                    ).run()
            tasks_path.write_text(
                yaml.safe_dump({"schema_version": 2, "tasks": [{"id": "t1", "changed": True}]}),
                encoding="utf-8",
            )
            producer = Mock()
            with (
                patch("warptest.p5_scale.c1_harness_hash", return_value="harness-a"),
                patch("warptest.p5_scale.run_gui_agent_benchmark", producer),
            ):
                with self.assertRaisesRegex(ValueError, "changed provenance"):
                    p5_scale.P5Orchestrator(
                        root, manifest, run_dir=run_dir, models=["m"], seeds=1,
                        arms=["c1_agent_s"], agent_kind="llm", resume=True,
                    ).run()
            producer.assert_not_called()

    def test_invocation_provenance_validator_rejects_every_bound_identity(self) -> None:
        manifest = {
            "arms": {"c1_agent_s": {"tasks": "tasks.yaml", "policy_kind": "agent_s", "start_mode": "from_start"}},
            "c1_background_pixel_protocol": copy.deepcopy(C1_BACKGROUND_PROTOCOL),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks_path = _write_test_c1_tasks(root)
            orchestrator = p5_scale.P5Orchestrator(
                root, manifest, run_dir=root / "run", models=["m"], seeds=1,
                arms=["c1_agent_s"], agent_kind="llm", resume=True,
            )
            orchestrator.calibration_metadata = {
                "passed": True,
                "artifact_sha256": "calibration-a",
                "git_commit": orchestrator.git_commit,
                "manifest_hash": orchestrator.manifest_hash,
                "harness_hash": "harness-a",
                "observer_model": "observer",
                "grounder_model": "grounder",
                "browser": {"passed": True},
                "luanti": {"passed": True},
            }
            arm_dir = root / "run/c1_agent_s/model/seed0"
            context_path = arm_dir / "p5-invocation-context.json"
            arm_dir.mkdir(parents=True)
            with patch("warptest.p5_scale.c1_harness_hash", return_value="harness-a"):
                expected = orchestrator._c1_invocation_context(
                    "c1_agent_s", model="m", seed=0, tasks_path=tasks_path,
                    invocation={"policy_kind": "gui_agent"},
                )
                mutations = {
                    "arm": "c1_state",
                    "model": "other-model",
                    "seed": 1,
                    "git_commit": "other-git",
                    "manifest_hash": "other-manifest",
                    "harness_hash": "other-harness",
                    "runtime_lock": {
                        "scope": "machine-shared",
                        "lock_dir": "/tmp/other-runtime-locks",
                    },
                    "calibration": {**expected["calibration"], "artifact_sha256": "calibration-b"},
                    "task_set": {**expected["task_set"], "task_set_hash": "other-task-set"},
                }
                for field, value in mutations.items():
                    with self.subTest(field=field):
                        actual = json.loads(json.dumps(expected))
                        actual[field] = value
                        context_path.write_text(json.dumps(actual), encoding="utf-8")
                        with self.assertRaisesRegex(ValueError, "changed provenance"):
                            orchestrator._prepare_c1_invocation(
                                "c1_agent_s", model="m", seed=0,
                                invocation_dir=arm_dir, tasks_path=tasks_path,
                                invocation={"policy_kind": "agent_s"},
                            )

    def test_incomplete_c1_producer_is_never_recorded_ok(self) -> None:
        manifest = {
            "arms": {"c1_state": {"tasks": "tasks.yaml"}},
            "retry": {"max_attempts": 6, "base_sleep_s": 0, "max_sleep_s": 0},
        }
        calls = 0

        def incomplete(*_args, **kwargs):
            nonlocal calls
            calls += 1
            report_dir = Path(kwargs["report_dir"])
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "state-loop.json").write_text('{"complete": false}', encoding="utf-8")
            return {
                "report_dir": str(report_dir),
                "complete": False,
                "retryable_incomplete": False,
            }

        with tempfile.TemporaryDirectory() as tmp:
            _write_test_c1_tasks(Path(tmp))
            orchestrator = p5_scale.P5Orchestrator(
                tmp, manifest, run_dir=Path(tmp) / "run", models=["m"], seeds=1,
                arms=["c1_state"], agent_kind="deterministic",
            )
            with (
                patch("warptest.p5_scale.c1_harness_hash", return_value="test-harness"),
                patch("warptest.p5_scale.run_state_loop_benchmark", side_effect=incomplete),
            ):
                with self.assertRaises(p5_scale.NonRetryableC1InvocationError):
                    orchestrator.run()
            index = json.loads((Path(tmp) / "run/p5-run-index.json").read_text(encoding="utf-8"))
        row = index["arms"]["c1_state"][0]
        self.assertEqual(row["status"], "error")
        self.assertTrue(str(row["report"]).endswith("/state-loop.json"))
        self.assertEqual(
            row["report_sha256"],
            hashlib.sha256(b'{"complete": false}').hexdigest(),
        )
        self.assertIn("incomplete", row["error"])
        self.assertEqual(calls, 1)
        self.assertEqual(row["retry_history"][0]["status"], "fatal_error")

    def test_unknown_usage_is_fatal_and_not_retried_by_p5(self) -> None:
        manifest = {
            "arms": {"c1_state": {"tasks": "tasks.yaml"}},
            "retry": {"max_attempts": 6, "base_sleep_s": 0, "max_sleep_s": 0},
        }
        calls = 0

        def permanently_incomplete(*_args, **kwargs):
            nonlocal calls
            calls += 1
            report_dir = Path(kwargs["report_dir"])
            report_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "complete": False,
                "results": [
                    {
                        "task_id": "t1",
                        "incomplete": True,
                        "cost": {
                            "usage_complete": False,
                            "incomplete_calls": 1,
                            "unknown_total_token_calls": 1,
                        },
                    }
                ],
            }
            (report_dir / "state-loop.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            return {**payload, "report_dir": str(report_dir)}

        with tempfile.TemporaryDirectory() as tmp:
            _write_test_c1_tasks(Path(tmp))
            orchestrator = p5_scale.P5Orchestrator(
                tmp,
                manifest,
                run_dir=Path(tmp) / "run",
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
            )
            with (
                patch("warptest.p5_scale.c1_harness_hash", return_value="test-harness"),
                patch(
                    "warptest.p5_scale.run_state_loop_benchmark",
                    side_effect=permanently_incomplete,
                ),
            ):
                with self.assertRaisesRegex(
                    cost.UsageAccountingError, "permanently unknown provider usage"
                ):
                    orchestrator.run()
            index = json.loads(
                (Path(tmp) / "run/p5-run-index.json").read_text(encoding="utf-8")
            )
            resume_producer = Mock()
            with (
                patch("warptest.p5_scale.c1_harness_hash", return_value="test-harness"),
                patch(
                    "warptest.p5_scale.run_state_loop_benchmark",
                    resume_producer,
                ),
            ):
                with self.assertRaisesRegex(
                    cost.UsageAccountingError, "refusing same-directory C1 resume"
                ):
                    p5_scale.P5Orchestrator(
                        tmp,
                        manifest,
                        run_dir=Path(tmp) / "run",
                        models=["m"],
                        seeds=1,
                        arms=["c1_state"],
                        agent_kind="deterministic",
                        resume=True,
                    ).run()
            resume_producer.assert_not_called()

        self.assertEqual(calls, 1)
        row = index["arms"]["c1_state"][0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(len(row["retry_history"]), 1)
        self.assertEqual(row["retry_history"][0]["status"], "fatal_error")

    def test_unknown_usage_stops_queued_outer_invocations(self) -> None:
        manifest = {
            "arms": {
                "c1_agent_s": {"tasks": "tasks.yaml"},
                "c1_agent_s_synth": {"tasks": "tasks.yaml"},
                "c1_state": {"tasks": "tasks.yaml"},
                "c1_agent_scalecua": {"tasks": "tasks.yaml"},
                "c1_agent_scalecua_synth": {"tasks": "tasks.yaml"},
            },
            "c1_background_pixel_protocol": copy.deepcopy(C1_BACKGROUND_PROTOCOL),
        }
        started: list[str] = []
        started_lock = threading.Lock()
        active_pair = threading.Barrier(2)

        def gui_invocation(
            arm: str, *, start_mode: str, model: str, seed: int
        ) -> None:
            del start_mode, model, seed
            with started_lock:
                started.append(arm)
            active_pair.wait(timeout=2)
            if arm == "c1_agent_s":
                raise cost.UsageAccountingError(
                    "C1 producer returned permanently unknown provider usage"
                )
            threading.Event().wait(0.1)

        def state_invocation(model: str, seed: int) -> None:
            del model, seed
            with started_lock:
                started.append("c1_state")

        with tempfile.TemporaryDirectory() as tmp:
            _write_test_c1_tasks(Path(tmp))
            orchestrator = p5_scale.P5Orchestrator(
                tmp,
                manifest,
                run_dir=Path(tmp) / "run",
                models=["m"],
                seeds=1,
                arms=list(p5_scale.C1_ARMS),
                agent_kind="deterministic",
                c1_outer_worker=2,
            )
            with (
                patch.object(
                    orchestrator,
                    "_run_gui_invocation",
                    side_effect=gui_invocation,
                ),
                patch.object(
                    orchestrator,
                    "_run_c1_state_invocation",
                    side_effect=state_invocation,
                ),
            ):
                with self.assertRaisesRegex(
                    cost.UsageAccountingError,
                    "permanently unknown provider usage",
                ):
                    orchestrator.run()
            index = json.loads(
                (Path(tmp) / "run/p5-run-index.json").read_text(encoding="utf-8")
            )

        self.assertCountEqual(started, ["c1_agent_s", "c1_agent_s_synth"])
        state_row = next(
            row for row in index["execution_order"] if row["arm"] == "c1_state"
        )
        self.assertEqual(state_row["status"], "cancelled_global_unknown_usage")
        self.assertEqual(
            state_row["cancellation_reason"],
            "permanently unknown provider usage",
        )
        self.assertFalse(state_row["integrity_failure"])

    def test_recovery_state_loop_stops_before_next_tuple_after_global_stop(
        self,
    ) -> None:
        formal = yaml.safe_load(
            (ROOT / "benchmarks/p5/manifest.yaml").read_text(encoding="utf-8")
        )
        manifest = {
            "arms": {
                "c1_state": {
                    "tasks": "tasks.yaml",
                    "max_explore_steps": 16,
                }
            },
            "c1_trial_persistence": copy.deepcopy(
                formal["c1_trial_persistence"]
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                manifest,
                run_dir=root / "run",
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_source_index=root / "source-index.json",
                c1_inner_worker=1,
            )
            stop = threading.Event()
            orchestrator._c1_global_stop_event = stop
            started: list[str] = []
            invocations: list[dict[str, object]] = []
            producer_kwargs: list[dict[str, object]] = []

            def run_state(*_args, **kwargs):
                producer_kwargs.append(dict(kwargs))
                return {}

            def execute(*_args, **kwargs) -> None:
                started.append(str(kwargs["task_id"]))
                invocations.append(dict(kwargs["invocation"]))
                kwargs["fn"]({})
                stop.set()

            with (
                patch.object(
                    orchestrator,
                    "_c1_task_records",
                    return_value=[{"id": "t1"}, {"id": "t2"}],
                ),
                patch.object(
                    orchestrator,
                    "_recovery_trial_attempt",
                    return_value=0,
                ),
                patch.object(
                    orchestrator,
                    "_c1_trial_tasks_path",
                    return_value=root / "tasks.yaml",
                ),
                patch.object(
                    orchestrator,
                    "_c1_inner_lane_plan",
                    return_value=[],
                ),
                patch.object(
                    p5_scale,
                    "run_state_loop_benchmark",
                    side_effect=run_state,
                ),
                patch.object(orchestrator, "_execute", side_effect=execute),
            ):
                orchestrator._run_c1_state_invocation("m", 0)

        self.assertEqual(started, ["t1"])
        expected_cache = str((root / "run/c1-self-synthesis-cache").resolve())
        self.assertEqual(invocations[0]["max_explore_steps"], 16)
        self.assertEqual(invocations[0]["shared_synthesis_cache"], expected_cache)
        self.assertEqual(producer_kwargs[0]["max_explore_steps"], 16)
        self.assertEqual(
            str(producer_kwargs[0]["shared_synthesis_cache"]), expected_cache
        )

    def test_recovery_gui_loop_stops_before_next_tuple_after_global_stop(
        self,
    ) -> None:
        formal = yaml.safe_load(
            (ROOT / "benchmarks/p5/manifest.yaml").read_text(encoding="utf-8")
        )
        manifest = {
            "arms": {"c1_agent_s": {"tasks": "tasks.yaml"}},
            "c1_trial_persistence": copy.deepcopy(
                formal["c1_trial_persistence"]
            ),
            "c1_background_pixel_protocol": copy.deepcopy(
                formal["c1_background_pixel_protocol"]
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                manifest,
                run_dir=root / "run",
                models=["m"],
                seeds=1,
                arms=["c1_agent_s"],
                agent_kind="deterministic",
                c1_source_index=root / "source-index.json",
                c1_inner_worker=1,
            )
            stop = threading.Event()
            orchestrator._c1_global_stop_event = stop
            started: list[str] = []

            def execute(*_args, **kwargs) -> None:
                started.append(str(kwargs["task_id"]))
                stop.set()

            with (
                patch.dict(os.environ, {"MODELSCOPE_API_KEY": "test-key"}),
                patch.object(orchestrator, "_require_calibration_gate"),
                patch.object(
                    orchestrator,
                    "_c1_task_records",
                    return_value=[{"id": "t1"}, {"id": "t2"}],
                ),
                patch.object(
                    orchestrator,
                    "_recovery_trial_attempt",
                    return_value=0,
                ),
                patch.object(
                    orchestrator,
                    "_c1_trial_tasks_path",
                    return_value=root / "tasks.yaml",
                ),
                patch.object(
                    orchestrator,
                    "_c1_inner_lane_plan",
                    return_value=[],
                ),
                patch.object(orchestrator, "_execute", side_effect=execute),
            ):
                orchestrator._run_gui_invocation(
                    "c1_agent_s",
                    start_mode="from_start",
                    model="m",
                    seed=0,
                )

        self.assertEqual(started, ["t1"])

    def test_recovery_state_producer_receives_parallel_ticket_lanes(self) -> None:
        formal = yaml.safe_load(
            (ROOT / "benchmarks/p5/manifest.yaml").read_text(encoding="utf-8")
        )
        manifest = {
            "arms": {"c1_state": {"tasks": "tasks.yaml"}},
            "c1_trial_persistence": copy.deepcopy(
                formal["c1_trial_persistence"]
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                manifest,
                run_dir=root / "run",
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_source_index=root / "source-index.json",
                c1_inner_worker=2,
            )
            barrier = threading.Barrier(2)
            lanes: set[int] = set()
            snapshots: list[dict[str, object]] = []
            lock = threading.Lock()

            def run_state(*_args, **kwargs):
                ticket = kwargs["ready_ticket"]
                handle = kwargs["ready_handle"]
                lane_id = ticket.claimed_lane_id
                handle.admit(ticket, lane_id=lane_id)
                with lock:
                    lanes.add(lane_id)
                barrier.wait(timeout=2)
                snapshots.append(handle.snapshot())
                handle.complete(ticket)
                return {}

            def execute(*_args, **kwargs):
                return kwargs["fn"]({})

            with (
                patch.object(
                    orchestrator,
                    "_c1_task_records",
                    return_value=[
                        {"id": "t1", "benchmark": "spacerpg"},
                        {"id": "t2", "benchmark": "spacerpg"},
                    ],
                ),
                patch.object(orchestrator, "_recovery_trial_attempt", return_value=0),
                patch.object(
                    orchestrator,
                    "_c1_trial_tasks_path",
                    return_value=root / "tasks.yaml",
                ),
                patch.object(
                    p5_scale,
                    "run_state_loop_benchmark",
                    side_effect=run_state,
                ),
                patch.object(orchestrator, "_execute", side_effect=execute),
            ):
                orchestrator._run_c1_state_invocation("m", 0)

        self.assertEqual(lanes, {0, 1})
        self.assertTrue(all(row["worker_limit"] == 2 for row in snapshots))
        self.assertTrue(any(row["max_active_tasks"] == 2 for row in snapshots))

    def test_recovery_gui_producer_receives_parallel_ticket_lanes(self) -> None:
        formal = yaml.safe_load(
            (ROOT / "benchmarks/p5/manifest.yaml").read_text(encoding="utf-8")
        )
        manifest = {
            "arms": {"c1_agent_s": {"tasks": "tasks.yaml"}},
            "c1_trial_persistence": copy.deepcopy(
                formal["c1_trial_persistence"]
            ),
            "c1_background_pixel_protocol": copy.deepcopy(
                formal["c1_background_pixel_protocol"]
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                manifest,
                run_dir=root / "run",
                models=["m"],
                seeds=1,
                arms=["c1_agent_s"],
                agent_kind="deterministic",
                c1_source_index=root / "source-index.json",
                c1_inner_worker=2,
            )
            barrier = threading.Barrier(2)
            lanes: set[int] = set()
            snapshots: list[dict[str, object]] = []
            lock = threading.Lock()

            def run_gui(*_args, **kwargs):
                ticket = kwargs["ready_ticket"]
                handle = kwargs["ready_handle"]
                lane_id = ticket.claimed_lane_id
                handle.admit(ticket, lane_id=lane_id)
                with lock:
                    lanes.add(lane_id)
                barrier.wait(timeout=2)
                snapshots.append(handle.snapshot())
                handle.complete(ticket)
                return {}

            def execute(*_args, **kwargs):
                return kwargs["fn"]({})

            with (
                patch.dict(os.environ, {"MODELSCOPE_API_KEY": "test-key"}),
                patch.object(orchestrator, "_require_calibration_gate"),
                patch.object(
                    orchestrator,
                    "_c1_task_records",
                    return_value=[
                        {"id": "t1", "benchmark": "spacerpg"},
                        {"id": "t2", "benchmark": "spacerpg"},
                    ],
                ),
                patch.object(orchestrator, "_recovery_trial_attempt", return_value=0),
                patch.object(
                    orchestrator,
                    "_c1_trial_tasks_path",
                    return_value=root / "tasks.yaml",
                ),
                patch.object(
                    p5_scale,
                    "run_gui_agent_benchmark",
                    side_effect=run_gui,
                ),
                patch.object(orchestrator, "_execute", side_effect=execute),
            ):
                orchestrator._run_gui_invocation(
                    "c1_agent_s",
                    start_mode="from_start",
                    model="m",
                    seed=0,
                )

        self.assertEqual(lanes, {0, 1})
        self.assertTrue(all(row["worker_limit"] == 2 for row in snapshots))
        self.assertTrue(any(row["max_active_tasks"] == 2 for row in snapshots))

    def test_trial_atomic_task_file_publish_is_thread_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_test_c1_tasks(root)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                {"arms": {"c1_state": {"tasks": "tasks.yaml"}}},
                run_dir=root / "run",
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
            )
            task = {"id": "t1"}
            cfg = {"tasks": "tasks.yaml"}
            ready = [threading.Event(), threading.Event()]
            results: list[Path] = []
            errors: list[BaseException] = []

            def materialize(index: int) -> None:
                try:
                    ready[index].set()
                    results.append(orchestrator._c1_trial_tasks_path(cfg, task))
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=materialize, args=(index,))
                for index in range(2)
            ]
            orchestrator._trial_task_paths_lock.acquire()
            for thread in threads:
                thread.start()
            try:
                self.assertTrue(all(event.wait(timeout=1) for event in ready))
                for thread in threads:
                    thread.join(timeout=0.05)
                blocked_while_locked = all(thread.is_alive() for thread in threads)
                published_while_locked = list(
                    (root / "run" / "c1-trial-tasks").glob("*.yaml")
                )
            finally:
                orchestrator._trial_task_paths_lock.release()
            for thread in threads:
                thread.join(timeout=5)

            self.assertTrue(blocked_while_locked)
            self.assertEqual(published_while_locked, [])
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            self.assertTrue(results[0].is_file())
            self.assertEqual(
                yaml.safe_load(results[0].read_text(encoding="utf-8"))[
                    "selected_task_ids"
                ],
                ["t1"],
            )
            self.assertEqual(
                list(results[0].parent.glob(f"{results[0].name}.tmp*")),
                [],
            )

    def test_explicit_transient_incomplete_is_retryable(self) -> None:
        manifest = {
            "arms": {"c1_state": {"tasks": "tasks.yaml"}},
            "retry": {"max_attempts": 2, "base_sleep_s": 0, "max_sleep_s": 0},
        }
        calls = 0

        def producer(*_args, **kwargs):
            nonlocal calls
            calls += 1
            report_dir = Path(kwargs["report_dir"])
            report_dir.mkdir(parents=True, exist_ok=True)
            complete = calls == 2
            payload = {
                "complete": complete,
                "retryable_incomplete": not complete,
                "results": [],
            }
            (report_dir / "state-loop.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            return {**payload, "report_dir": str(report_dir)}

        with tempfile.TemporaryDirectory() as tmp:
            _write_test_c1_tasks(Path(tmp))
            with (
                patch("warptest.p5_scale.c1_harness_hash", return_value="test-harness"),
                patch(
                    "warptest.p5_scale.run_state_loop_benchmark",
                    side_effect=producer,
                ),
            ):
                index = p5_scale.P5Orchestrator(
                    tmp,
                    manifest,
                    run_dir=Path(tmp) / "run",
                    models=["m"],
                    seeds=1,
                    arms=["c1_state"],
                    agent_kind="deterministic",
                ).run()

        self.assertEqual(calls, 2)
        row = index["arms"]["c1_state"][0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(
            [item["status"] for item in row["retry_history"]],
            ["retryable_error", "ok"],
        )


class C1TaskSchemaTests(unittest.TestCase):
    def test_c1_expansion_public_issues_are_complete_and_prompt_safe(self) -> None:
        manifest = yaml.safe_load(
            (ROOT / "benchmarks/p5/c1_tasks.yaml").read_text(encoding="utf-8")
        )
        expansion_ids = manifest["expansion_sets"]["deep30"]["task_ids"]
        tasks = {
            task.id: task
            for task in state_loop.load_state_loop_tasks(
                ROOT, "benchmarks/p5/c1_tasks.yaml"
            )
        }
        self.assertEqual(len(expansion_ids), 30)
        forbidden_operators = (
            "hook_call",
            "hook_action",
            "spacerpg_mine_sortie",
            "luanti_set_",
            "luanti_fill",
            "luanti_save_world",
            "openra_",
            "jupyterlab_",
            "libreoffice_",
            "blender_",
            "jynew_",
            "pal3_",
        )
        for task_id in expansion_ids:
            task = tasks[task_id]
            issue = task.state_description
            self.assertEqual(task.issue.issue, issue)
            self.assertGreater(len(issue.split()), 20)
            self.assertNotIn("registered", issue.lower())
            self.assertFalse(
                [
                    operator
                    for operator in forbidden_operators
                    if operator in issue.lower()
                ]
            )
            prompt = build_explore_initial_prompt(
                task.issue,
                ROOT / task.issue.repo,
                "repo-map-placeholder",
            )
            serialized_payload = prompt.split(
                "Public issue to test:\n", 1
            )[1].split("\nRepository map", 1)[0]
            self.assertEqual(
                yaml.safe_load(serialized_payload)["task"]["issue"],
                issue,
            )
            self.assertNotIn(task.oracle_spec, prompt)
            self.assertNotIn("oracle_spec", prompt)
            self.assertNotIn("specs/", prompt)
            self.assertNotIn(".golden", prompt)

    def test_c1_expansion_issue_token_checker(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/check_issue_completeness.py"),
                "--c1-expansion",
                "deep30",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.count("OK     "), 30)

    def test_c1_issue_checker_requires_semantics_not_adapter_operators(self) -> None:
        spec = yaml.safe_load(
            (ROOT / "specs/love/c1-work-gym-date-roundtrip.yaml").read_text(
                encoding="utf-8"
            )
        )
        required = issue_checker.requirements(
            spec,
            include_assertions=True,
            semantic=True,
        )
        self.assertIn(("960", "num"), required)
        self.assertIn(("work_coding", "identifier_words"), required)
        self.assertNotIn(("hook_call", "str"), required)
        self.assertNotIn(("patchState", "str"), required)
        self.assertTrue(
            issue_checker.recoverable(
                "Build one power plant.",
                "powr",
                "str",
                semantic=True,
            )
        )
        self.assertFalse(
            issue_checker.recoverable(
                "Reach the specified seeded state.",
                "960",
                "num",
                semantic=True,
            )
        )

    def test_legacy_state_description_still_loads(self) -> None:
        task = state_loop.load_state_loop_tasks(ROOT, "benchmarks/state_loop/tasks.yaml")[0]
        self.assertEqual(task.preconditions, task.state_description)
        self.assertEqual(task.reference_steps, task.state_description)
        self.assertEqual(task.goal_description, task.state_description)

    def test_repository_description_terminology_guard(self) -> None:
        production_roots = (
            ROOT / "warptest",
            ROOT / "scripts",
            ROOT / "benchmarks",
            ROOT / "docs",
        )
        text_suffixes = {".py", ".yaml", ".yml", ".json", ".html", ".md"}
        allowed_legacy_fields = {ROOT / "warptest/state_loop.py"}
        violations: list[str] = []
        for production_root in production_roots:
            for path in production_root.rglob("*"):
                if not path.is_file() or path.suffix not in text_suffixes:
                    continue
                relative = path.relative_to(ROOT)
                if relative.parts[0] == "benchmarks" and relative.parts[1:2] == ("upstream",):
                    continue
                text = path.read_text(encoding="utf-8")
                for legacy in ("start_description", "transition_instruction"):
                    if legacy in text and path not in allowed_legacy_fields:
                        violations.append(f"{relative}: contains {legacy}")
                for obsolete_revision in (
                    "c1-public-checklist-v3",
                    "c1-deep30-v2",
                    "historical_bug_v1",
                ):
                    if (
                        obsolete_revision in text
                        and not (
                            obsolete_revision == "c1-deep30-v2"
                            and path
                            == ROOT / "docs/c1_REPRODUCIBILITY.md"
                        )
                    ):
                        violations.append(
                            f"{relative}: contains {obsolete_revision}"
                        )
        readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
        for forbidden in (
            "start_description",
            "transition_instruction",
            "c1-public-checklist-v3",
            "c1-deep30-v2",
        ):
            if forbidden in readme_text:
                violations.append(f"README.md: contains {forbidden}")
        self.assertEqual(violations, [])

    def test_legacy_formal_c1_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.yaml"
            for schema_version, task_schema in ((2, "c1_v2"), (3, "c1_v3")):
                with self.subTest(task_schema=task_schema):
                    path.write_text(yaml.safe_dump({
                        "schema_version": schema_version,
                        "task_schema": task_schema,
                        "tasks": [{
                            "id": "t", "benchmark": "2048", "repo": "games/2048",
                            "state_description": "legacy combined description",
                            "preconditions": "start", "reference_steps": "move",
                            "horizon_class": "short",
                            "oracle_spec": "specs/2048/merge-left.yaml",
                        }],
                    }), encoding="utf-8")
                    with self.assertRaisesRegex(
                        ValueError, "unsupported formal C1 task_schema"
                    ):
                        state_loop.load_state_loop_tasks(ROOT, path)

    def test_formal_v4_rejects_public_goal_and_noncanonical_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            spec_path = tmp_path / "spec.yaml"
            spec_path.write_text(
                yaml.safe_dump(
                    {
                        "benchmark": "2048",
                        "evaluation": {
                            "schema_version": 1,
                            "semantic": {
                                "assertions": [
                                    {"type": "equals", "path": "score", "value": 4}
                                ],
                                "surfaces": {
                                    surface: {
                                        "adapter": "2048_state_profile_v1",
                                        "start": {"validate_target": True, "checks": []},
                                        "goal": {"use_assertions": True, "checks": []},
                                    }
                                    for surface in ("gui", "state")
                                },
                            },
                            "visual_oracle": {
                                "after_description": "A visible merged tile.",
                                "after_rubric": {"required": ["merged tile"]},
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            task = {
                "id": "t",
                "benchmark": "2048",
                "repo": "games/2048",
                "state_description": "Preconditions: start\nReference steps: move",
                "preconditions": "start",
                "reference_steps": "move",
                "horizon_class": "short",
                "oracle_spec": str(spec_path),
            }
            path = tmp_path / "tasks.yaml"

            def write(candidate: dict[str, object]) -> None:
                path.write_text(
                    yaml.safe_dump(
                        {
                            "schema_version": 4,
                            "task_schema": "c1_v4",
                            "tasks": [candidate],
                        },
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                )

            write({**task, "goal_description": "sentinel goal"})
            with self.assertRaisesRegex(ValueError, "must not expose goal_description"):
                state_loop.load_state_loop_tasks(ROOT, path)
            for invalid_state in (
                "start then move",
                "Preconditions: start\n\nReference steps: move",
                "Preconditions: start\nReference steps: move\n",
                "Preconditions: start\nReference steps: move\nGoal: done",
                "Precondition: start\nReference steps: move",
            ):
                with self.subTest(invalid_state=invalid_state):
                    write({**task, "state_description": invalid_state})
                    with self.assertRaisesRegex(
                        ValueError, "canonical Preconditions/Reference steps"
                    ):
                        state_loop.load_state_loop_tasks(ROOT, path)
            write({**task, "start_description": "legacy start"})
            with self.assertRaisesRegex(ValueError, "unsupported legacy description"):
                state_loop.load_state_loop_tasks(ROOT, path)
            write({**task, "transition_instruction": "legacy transition"})
            with self.assertRaisesRegex(ValueError, "unsupported legacy description"):
                state_loop.load_state_loop_tasks(ROOT, path)
            write(task)
            loaded = state_loop.load_state_loop_tasks(ROOT, path)[0]
            self.assertEqual(loaded.goal_description, "")

    def test_formal_v4_fails_closed_without_hidden_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            spec_path = tmp_path / "spec.yaml"
            task_path = tmp_path / "tasks.yaml"
            task = {
                "id": "t",
                "benchmark": "2048",
                "repo": "games/2048",
                "state_description": "Preconditions: start\nReference steps: move",
                "preconditions": "start",
                "reference_steps": "move",
                "horizon_class": "short",
                "oracle_spec": str(spec_path),
            }
            task_path.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": 4,
                        "task_schema": "c1_v4",
                        "tasks": [task],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            spec_path.write_text(
                yaml.safe_dump(
                    {
                        "benchmark": "2048",
                        "evaluation": {
                            "schema_version": 1,
                            "semantic": {
                                "assertions": [],
                                "surfaces": {
                                    surface: {
                                        "adapter": "2048_state_profile_v1",
                                        "start": {"validate_target": True, "checks": []},
                                        "goal": {"use_assertions": True, "checks": []},
                                    }
                                    for surface in ("gui", "state")
                                },
                            },
                            "visual_oracle": {
                                "after_description": "after",
                                "after_rubric": {"required": ["after"]},
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "hidden assertions"):
                state_loop.load_state_loop_tasks(ROOT, task_path)
            spec_path.write_text(
                yaml.safe_dump(
                    {
                        "benchmark": "2048",
                        "evaluation": {
                            "schema_version": 1,
                            "semantic": {
                                "assertions": [{"type": "equals"}],
                                "surfaces": {
                                    surface: {
                                        "adapter": "2048_state_profile_v1",
                                        "start": {"validate_target": True, "checks": []},
                                        "goal": {"use_assertions": True, "checks": []},
                                    }
                                    for surface in ("gui", "state")
                                },
                            },
                            "visual_oracle": {"after_description": "after"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "after-state rubric"):
                state_loop.load_state_loop_tasks(ROOT, task_path)


class StatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = random.Random(7)

    def test_mean_ci_brackets_mean(self) -> None:
        stat = p5_stats.mean_ci([1, 1, 1, 2, 2, 2], iterations=400, rng=self.rng)
        self.assertAlmostEqual(stat["mean"], 1.5, places=6)
        self.assertLessEqual(stat["lo"], stat["mean"])
        self.assertGreaterEqual(stat["hi"], stat["mean"])
        self.assertEqual(stat["n"], 6)

    def test_mean_ci_handles_empty_and_singleton(self) -> None:
        empty = p5_stats.mean_ci([], iterations=10, rng=self.rng)
        self.assertEqual(empty["n"], 0)
        single = p5_stats.mean_ci([3.0], iterations=10, rng=self.rng)
        self.assertEqual((single["lo"], single["hi"]), (3.0, 3.0))

    def test_paired_bootstrap_diff_detects_positive_shift(self) -> None:
        a = [3, 4, 5, 6, 7]
        b = [1, 1, 1, 1, 1]
        diff = p5_stats.paired_bootstrap_diff(a, b, iterations=600, rng=self.rng)
        self.assertGreater(diff["mean_diff"], 0)
        self.assertTrue(diff["excludes_zero"])

    def test_permutation_test_small_p_for_consistent_difference(self) -> None:
        a = [5, 6, 7, 8, 9, 10]
        b = [1, 1, 1, 1, 1, 1]
        res = p5_stats.paired_permutation_test(a, b, iterations=2000, rng=self.rng)
        self.assertLess(res["p_value"], 0.1)
        self.assertEqual(res["n"], 6)

    def test_mcnemar_exact_and_from_maps(self) -> None:
        # 5 tasks: state passes all, gui fails all -> b=5, c=0 -> tiny p.
        a_pass = {f"t{i}": True for i in range(5)}
        b_pass = {f"t{i}": False for i in range(5)}
        res = p5_stats.paired_binary_from_maps(a_pass, b_pass)
        self.assertEqual((res["b"], res["c"]), (5, 0))
        self.assertLess(res["p_value"], 0.1)
        self.assertEqual(p5_stats.mcnemar_exact(0, 0)["p_value"], 1.0)

    def test_cluster_bootstrap_keeps_all_seeds_in_each_task_cluster(self) -> None:
        a = {(task, seed): 1 for task in ("t1", "t2", "t3") for seed in range(3)}
        b = {(task, seed): 0 for task in ("t1", "t2", "t3") for seed in range(3)}
        result = p5_stats.clustered_paired_bootstrap_diff(a, b, iterations=100, rng=self.rng)
        self.assertEqual((result["clusters"], result["n"]), (3, 9))
        self.assertEqual(result["mean_diff"], 1.0)

    def test_clustered_per_arm_and_conditional_ci_keep_seed_clusters(self) -> None:
        values = {
            (task, seed): float(task == "t2")
            for task in ("t1", "t2")
            for seed in range(3)
        }
        mean = p5_stats.clustered_mean_ci(
            values, iterations=100, rng=random.Random(1)
        )
        self.assertEqual((mean["n"], mean["clusters"], mean["mean"]), (6, 2, 0.5))
        conditions = {key: key[0] == "t2" for key in values}
        conditional = p5_stats.clustered_conditional_mean_ci(
            values, conditions, iterations=100, rng=random.Random(1)
        )
        self.assertEqual(conditional["n"], 3)
        self.assertEqual(conditional["clusters"], 2)
        self.assertEqual(conditional["mean"], 1.0)

    def test_clustered_conditional_rate_diff_keeps_arm_specific_denominators(self) -> None:
        keys = {(task, seed) for task in ("t1", "t2", "t3") for seed in range(2)}
        b_conditions = {key: key[0] in {"t1", "t2"} for key in keys}
        a_conditions = {key: key[0] in {"t2", "t3"} for key in keys}
        b_values = {key: float(key[0] in {"t1", "t2"}) for key in keys}
        a_values = {key: float(key[0] == "t3") for key in keys}

        result = p5_stats.clustered_conditional_rate_diff(
            b_values,
            b_conditions,
            a_values,
            a_conditions,
            iterations=200,
            rng=random.Random(3),
        )

        self.assertEqual((result["clusters"], result["n"]), (3, 6))
        self.assertEqual(
            (result["b_condition_positive"], result["a_condition_positive"]),
            (4, 4),
        )
        self.assertEqual((result["b_mean"], result["a_mean"]), (1.0, 0.5))
        self.assertEqual(result["mean_diff"], 0.5)
        # Conditioning on the intersection instead would use only t2 and
        # produce 1.0, so this guards the intended arm-specific estimand.
        self.assertNotEqual(result["mean_diff"], 1.0)
        self.assertGreater(result["valid_bootstrap_samples"], 0)

    def test_clustered_conditional_rate_diff_omits_zero_denominator_replicates(self) -> None:
        keys = {("b-only", 0), ("a-only", 0)}
        result = p5_stats.clustered_conditional_rate_diff(
            {key: 1.0 for key in keys},
            {key: key[0] == "b-only" for key in keys},
            {key: 0.0 for key in keys},
            {key: key[0] == "a-only" for key in keys},
            iterations=200,
            rng=random.Random(5),
        )

        self.assertEqual(result["mean_diff"], 1.0)
        self.assertGreater(result["valid_bootstrap_samples"], 0)
        self.assertLess(result["valid_bootstrap_samples"], 200)
        self.assertEqual((result["lo"], result["hi"]), (1.0, 1.0))

    def test_clustered_rmst_treats_every_failure_as_censored(self) -> None:
        durations = {
            ("t1", 0): 1,
            ("t1", 1): 10,
            ("t2", 0): 10,
            ("t2", 1): 10,
        }
        events = {
            ("t1", 0): False,  # early model failure is censoring, never success
            ("t1", 1): True,
            ("t2", 0): False,
            ("t2", 1): False,
        }
        result = p5_stats.clustered_rmst_ci(
            durations, events, tau=10, iterations=100, rng=random.Random(1)
        )
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["n"], 4)
        self.assertEqual(result["clusters"], 2)
        self.assertEqual(result["tau"], 10)
        self.assertGreater(result["rmst"], 9)

    def test_clustered_paired_rmst_does_not_reward_early_failure(self) -> None:
        keys = {("t1", 0), ("t2", 0)}
        result = p5_stats.clustered_paired_rmst_diff(
            {key: 1 for key in keys},
            {key: False for key in keys},
            {key: 8 for key in keys},
            {key: True for key in keys},
            tau=10,
            iterations=100,
            rng=random.Random(1),
        )
        self.assertEqual((result["clusters"], result["n"]), (2, 2))
        self.assertEqual((result["b_events"], result["a_events"]), (0, 2))
        # Raw termination would report 1 - 8 = -7 ms and reward failure.  At
        # common tau, the censored B arm is correctly slower: 10 - 8 = +2 ms.
        self.assertEqual(result["mean_diff"], 2.0)
        self.assertEqual(result["tau"], 10)

    def test_holm_adjust_and_rmst(self) -> None:
        adjusted = p5_stats.holm_adjust({"m1": 0.01, "m2": 0.02, "m3": 0.5, "m4": 1.0})
        self.assertEqual(adjusted["m1"], 0.04)
        self.assertGreaterEqual(adjusted["m2"], adjusted["m1"])
        rmst = p5_stats.restricted_mean_survival_time([2, 5, 5], [True, False, True], tau=5)
        self.assertEqual(rmst["n"], 3)
        self.assertEqual(rmst["events"], 2)
        self.assertGreater(rmst["rmst"], 0)


class FuzzFingerprintTests(unittest.TestCase):
    def test_normalize_detail_collapses_volatile_tokens(self) -> None:
        d1 = fuzzing.normalize_detail("Error at 0x1f3 line 42 in /tmp/abc/run-1/x.png")
        d2 = fuzzing.normalize_detail("Error at 0x9ab line 99 in /tmp/zzz/run-7/y.png")
        self.assertEqual(d1, d2)

    def test_fingerprint_is_deterministic_and_degrades_without_screenshot(self) -> None:
        kwargs = dict(
            benchmark="2048", task_id="2048-merge-left", mutation="huge_tile",
            defect_type="console_error", detail="boom at 12", mutated_state={"target": {"board": [[1]]}},
            defect_path="browser",
        )
        fp1 = fuzzing.defect_fingerprint(**kwargs)
        fp2 = fuzzing.defect_fingerprint(**kwargs)
        self.assertEqual(fp1, fp2)
        # Engine path: no screenshot must not raise and must still dedup.
        engine = fuzzing.defect_fingerprint(
            benchmark="luanti", task_id="luanti-set-node", mutation="invalid_node",
            defect_type="engine_synthesis_error", detail="restore failed", mutated_state=None,
            defect_path="engine", screenshot=None,
        )
        self.assertEqual(len(engine), 16)

    def test_summarize_fuzz_dedupes_unique_and_splits_paths(self) -> None:
        results = [
            {
                "benchmark": "2048",
                "task_id": "2048-merge-left",
                "mutations": [
                    {"verdict": "defect", "defect_type": "console_error", "defect_path": "browser", "fingerprint": "fpA"},
                    {"verdict": "defect", "defect_type": "console_error", "defect_path": "browser", "fingerprint": "fpA"},
                    {"verdict": "defect", "defect_type": "blank_render", "defect_path": "browser", "fingerprint": "fpB"},
                    {"verdict": "robust"},
                ],
            },
            {
                "benchmark": "luanti",
                "task_id": "luanti-set-node",
                "mutations": [
                    {"verdict": "defect", "defect_type": "engine_synthesis_error", "defect_path": "engine", "fingerprint": "fpC"},
                ],
            },
        ]
        summary = fuzzing._summarize_fuzz(results)
        self.assertEqual(summary["defects"], 4)
        self.assertEqual(summary["unique_defects"], 3)  # fpA deduped
        self.assertEqual(summary["robust"], 1)
        self.assertEqual(summary["defect_paths"], {"browser": 3, "engine": 1})
        self.assertEqual(summary["by_benchmark"]["2048"]["unique_defects"], 2)
        self.assertEqual(summary["by_benchmark"]["luanti"]["unique_defects"], 1)


class P5C3IntegrityTests(unittest.TestCase):
    def test_p5_rejects_partial_c3_fuzz_result(self) -> None:
        with self.assertRaisesRegex(ValueError, "C3 fuzz invocation incomplete"):
            p5_scale.require_complete_c3_fuzz_result(
                {
                    "results": [
                        {"task_id": "openra-task", "status": "failed", "reason": "warm session bootstrap failed"},
                    ]
                }
            )

    def test_c3_aggregate_marks_failed_tasks_and_runs_incomplete(self) -> None:
        task_failed = p5_aggregate.c3_from_reports(
            [
                {
                    "results": [
                        {"task_id": "openra-task", "benchmark": "OpenRA", "status": "failed", "reason": "bootstrap"},
                    ],
                    "summary": {"defects": 0, "unique_defects": 0},
                }
            ],
            rng=random.Random(1),
            iters=20,
            ci=0.95,
            run_entries=[{"status": "ok", "report": "fuzz.json"}],
        )
        self.assertFalse(task_failed["complete"])
        self.assertEqual(task_failed["task_failures"][0]["task_id"], "openra-task")

        arm_failed = p5_aggregate.c3_from_reports(
            [],
            rng=random.Random(1),
            iters=20,
            ci=0.95,
            run_entries=[{"status": "error", "error": "OpenRA warm session bootstrap failed"}],
        )
        self.assertFalse(arm_failed["complete"])
        self.assertIn("bootstrap", arm_failed["run_failures"][0]["reason"])


def _write(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


class AggregateTests(unittest.TestCase):
    def test_preserved_model_failure_requires_explicit_non_infrastructure(self) -> None:
        result = {
            "actual_cost": {
                "usage_complete": True,
                "unknown_prompt_token_calls": 0,
                "unknown_completion_token_calls": 0,
                "unknown_total_token_calls": 0,
            },
            "incomplete": True,
            "retryable_incomplete": False,
            "reason": "model failure",
        }
        report = {
            "complete": False,
            "retryable_incomplete": False,
        }
        self.assertIsNone(
            p5_aggregate._preserved_model_failure_kind(
                result=result,
                report=report,
            )
        )
        result["infrastructure_error"] = False
        self.assertEqual(
            p5_aggregate._preserved_model_failure_kind(
                result=result,
                report=report,
            ),
            "exact_usage_nonretryable_model_failure",
        )

    def test_native_seed_validation_accepts_large_mtime_and_openra_gameplay_wrapper(
        self,
    ) -> None:
        self.assertEqual(
            p5_aggregate._integer(1_784_383_372_524_296_009),
            1_784_383_372_524_296_009,
        )
        snapshot = {
            "schema_version": 1,
            "source": "openra_live_gameplay_process",
            "observed": True,
            "algorithm": "not_applicable_live_gameplay",
            "seed": 0,
            "process_id": 123,
            "process_alive_at_observation": True,
            "benchmark": "openra",
            "task_id": "openra-task",
            "semantic_fingerprint": {
                "source": "openra_live_gameplay_probe_v1",
                "checks": [],
            },
        }
        evidence = {
            "schema_version": 1,
            "source": "openra_live_settings_process",
            "observed": True,
            "algorithm": "not_applicable_public_settings",
            "seed": 0,
            "task_id": "openra-task",
            "random_control": {
                "mode": "not_applicable",
                "seed_role": "trial_pairing_only",
            },
            "randomness": "not_applicable",
            "seed_role": "trial_pairing_only",
            "initial": snapshot,
            "final": copy.deepcopy(snapshot),
        }
        self.assertEqual(
            p5_aggregate._runtime_seed_evidence_issues(
                evidence,
                benchmark="openra",
                seed=0,
                task_id="openra-task",
                expected_semantic_stage="entry",
                require_final=False,
            ),
            [],
        )

    CALIBRATION_GATE = {
        "schema_version": 4,
        "schema": "c1-calibration-v4",
        "passed": True,
        "generated_at_utc": "2026-07-11T00:00:00+00:00",
        "artifact": "/tmp/calibration.json",
        "artifact_sha256": "a" * 64,
        "git_commit": "git",
        "manifest_hash": "manifest",
        "harness_hash": "harness",
        "api_provider": None,
        "observer_model": "observer",
        "grounder_model": "grounder",
        "background_pixel_protocol_sha256": "b" * 64,
        "backends": {
            benchmark: {
                **identity,
                "passed": True,
                "foreground_isolation": {
                    "target_never_frontmost": True,
                    "system_pointer_unchanged": True,
                    "system_clipboard_unchanged": True,
                    "foreground_text_unchanged": True,
                },
            }
            for benchmark, identity in C1_BACKGROUND_PROTOCOL["benchmarks"].items()
        },
    }

    @staticmethod
    def _formal_c1_reports() -> dict[str, list[dict]]:
        task_ids = ["t1", "t2"]

        def browser_seed_evidence(seed: int) -> tuple[dict, dict]:
            initial = {
                "source": "browser_page_runtime",
                "observed": True,
                "algorithm": "mulberry32",
                "seed": seed,
                "calls": 3,
                "viewport": {"width": 1280, "height": 800},
                "device_scale_factor": 1,
            }
            final = {**initial, "calls": 5}
            return final, {
                "schema_version": 1,
                "source": "browser_page_runtime",
                "observed": True,
                "algorithm": "mulberry32",
                "seed": seed,
                "initial": initial,
                "final": final,
            }

        def cost_payload(tokens: int, *, calls: int = 1) -> dict:
            latency = 10 if calls else 0
            bucket = {
                "calls": calls,
                "prompt_tokens": tokens,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cached_prompt_tokens": 0,
                "total_tokens": tokens,
                "latency_ms": latency,
                "incomplete_calls": 0,
                "unknown_prompt_token_calls": 0,
                "unknown_completion_token_calls": 0,
                "unknown_reasoning_token_calls": 0,
                "unknown_total_token_calls": 0,
                "usage_complete": True,
            }
            return {
                "llm_calls": calls,
                "total_tokens": tokens,
                "prompt_tokens": tokens,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cached_prompt_tokens": 0,
                "llm_latency_ms": latency,
                "est_usd": None,
                "usage_complete": True,
                "incomplete_calls": 0,
                "unknown_prompt_token_calls": 0,
                "unknown_completion_token_calls": 0,
                "unknown_reasoning_token_calls": 0,
                "unknown_total_token_calls": 0,
                "by_model": {"M1": dict(bucket)},
                "by_kind": {"gui_policy": dict(bucket)},
                "by_kind_model": {"gui_policy": {"M1": dict(bucket)}},
            }

        def gui_report(
            seed: int, *, better: bool, actual_synthesis: bool = False
        ) -> dict:
            results = []
            for index, task_id in enumerate(task_ids):
                goal = better or index == 0
                prng, seed_evidence = browser_seed_evidence(seed)
                row = {
                    "task_id": task_id,
                    "benchmark": "2048",
                    "seed": seed,
                    "horizon_class": "short",
                    "result": goal,
                    "before_visual_ok": True,
                    "before_semantic_ok": True,
                    "after_visual_ok": goal,
                    "after_semantic_ok": goal,
                    "start_success": True,
                    "goal_success": goal,
                    "e2e_success": goal,
                    "time_to_start_ms": 10,
                    "time_start_to_goal_ms": 20,
                    "time_e2e_excluding_synthesis_ms": 30,
                    "time_e2e_including_synthesis_ms": 35 if better else 30,
                    "checkpoint_synthesis_ms": 5 if better else 0,
                    "checkpoint_restore_ms": 0,
                    "runtime_bootstrap_ms": 2,
                    "grader_ms": 3,
                    "termination_time_ms": 35 if better else 30,
                    "decision_budget": C1_STEP_BUDGET,
                    "steps": (
                        {"phase_a": 0, "phase_b": 10, "total": 10}
                        if better
                        else {
                            "phase_a": C1_STEP_BUDGET // 2,
                            "phase_b": C1_STEP_BUDGET // 2,
                            "total": C1_STEP_BUDGET,
                        }
                    ),
                    "budget_exhausted": not better,
                    "runtime_started": True,
                    "policy_phase_started": True,
                    "visual_grading_started": True,
                    "prng": prng,
                    "seed_evidence": seed_evidence,
                    "initial_state_fingerprint": (
                        f"{'synth' if better else 'from'}-M1-{seed}-{task_id}"
                    ),
                    "generated_spec_fingerprint": (
                        f"spec-M1-{seed}-{task_id}" if better else None
                    ),
                    "synthesis_cache_fingerprint": (
                        f"cache-M1-{seed}-{task_id}" if better else None
                    ),
                    "synthesis_cache_hit": bool(better and not actual_synthesis),
                    "synthesis_actual_incurred_this_attempt": bool(
                        better and actual_synthesis
                    ),
                    "synthesis_attributed_fingerprints": (
                        [f"cache-M1-{seed}-{task_id}"] if better else []
                    ),
                    "synthesis_attributed_burdens": (
                        [{
                            "cache_key_fingerprint": f"cache-M1-{seed}-{task_id}",
                            "counterfactual_burden_fingerprint": f"burden-M1-{seed}-{task_id}",
                        }]
                        if better
                        else []
                    ),
                    "calibration": {"passed": True},
                    "reference_reachable": True,
                    "cost": cost_payload(100 + seed),
                    "synthesis_actual_cost": cost_payload(25) if actual_synthesis else cost_payload(0, calls=0),
                    "synthesis_transient_actual_cost": cost_payload(0, calls=0),
                    "synthesis_attributed_cost": cost_payload(25) if better else cost_payload(0, calls=0),
                }
                _stamp_single_attempt_ledger(
                    row,
                    task_actual_cost=cost_payload(100 + seed),
                    is_state=False,
                )
                results.append(row)
            return {
                "schema_version": 4,
                "complete": True,
                "model": "M1",
                "seed": seed,
                "policy_runtime_metadata": {
                    "protocol_version": 2,
                    "execution_backend": "background_pixel_v1",
                    "coordinate_space": "frame_pixels_top_left_v1",
                    "raw_screenshot_observation": True,
                    "action_execution_owner": "parent_runtime",
                    "foreground_required": False,
                    "local_code_enabled": False,
                    "native_exec_enabled": False,
                },
                "step_budgets": dict(C1_STEP_BUDGETS),
                "results": results,
            }

        def state_report(seed: int) -> dict:
            report = {
                "schema_version": 3,
                "complete": True,
                "synthesis_model": "M1",
                "seed": seed,
                "results": [{
                    "task_id": task_id,
                    "benchmark": "2048",
                    "seed": seed,
                    "result": True,
                    "visual_grading_started": True,
                    "time_to_start_ms": 5,
                    "time_start_to_goal_ms": 7,
                    "time_e2e_excluding_synthesis_ms": 12,
                    "time_e2e_including_synthesis_ms": 14,
                    "checkpoint_synthesis_ms": 2,
                    "checkpoint_restore_ms": 1,
                    "runtime_bootstrap_ms": 1,
                    "grader_ms": 1,
                    "termination_time_ms": 14,
                    "cost": cost_payload(50 + seed),
                    # Shared with the synthesized GUI arm: one accepted start per
                    # (model, seed, task).
                    "generated_spec_hash": f"spec-M1-{seed}-{task_id}",
                } for task_id in task_ids],
            }
            for row in report["results"]:
                _stamp_single_attempt_ledger(
                    row,
                    task_actual_cost=cost_payload(50 + seed),
                    is_state=True,
                )
            return report

        return {
            "c1_gui": [],
            "c1_gui_synth": [],
            "c1_agent_s": [gui_report(seed, better=False) for seed in range(3)],
            "c1_agent_s_synth": [
                gui_report(seed, better=True, actual_synthesis=True)
                for seed in range(3)
            ],
            "c1_state": [state_report(seed) for seed in range(3)],
        }

    @staticmethod
    def _full_formal_c1_reports() -> tuple[dict[str, list[dict]], list[str], list[str]]:
        task_rows = yaml.safe_load((ROOT / "benchmarks/p5/c1_tasks.yaml").read_text())["tasks"]
        models = ["M1", "M2", "M3"]

        def runtime_seed_evidence(
            benchmark: str,
            seed: int,
            task_id: str,
            semantic_stage: str,
        ) -> tuple[dict, dict]:
            if benchmark == "luanti":
                semantic_snapshot = {
                    "source": "luanti_live_semantic_probe",
                    "version": 1,
                    "task_id": task_id,
                    "nodes": [
                        {
                            "pos": {"x": 1, "y": 2, "z": 3},
                            "name": "basenodes:dirt",
                            "param1": 0,
                            "param2": 0,
                        }
                    ],
                    "ranges": [],
                    "fields": [],
                    "player": {"present": True, "name": "warptest"},
                    "inventory": [],
                    "mod_storage": [],
                    "meta_inventory": [],
                }
                fingerprint = hashlib.sha256(
                    json.dumps(
                        semantic_snapshot,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    ).encode("utf-8")
                ).hexdigest()
                evidence = {
                    "schema_version": 1,
                    "source": "luanti_warm_session",
                    "observed": True,
                    "algorithm": "luanti_fixed_map_seed",
                    "seed": seed,
                    "map_seed": seed,
                    "process_alive_at_observation": True,
                    "render": True,
                    "frame": {"width": 1280, "height": 720},
                    "map_backend": "sqlite3",
                    "gameid": "devtest",
                    "creative_mode": True,
                    "git_commit": "abc",
                    "world_path": "/tmp/world",
                    "config_path": "/tmp/minetest.conf",
                    "config_fingerprint": f"luanti-config-seed-{seed}",
                    "semantic_observed": True,
                    "initial_semantic_source": "luanti_live_semantic_probe",
                    "initial_semantic_stage": semantic_stage,
                    "initial_semantic_fingerprint": fingerprint,
                    "initial_semantic_observation": semantic_snapshot,
                }
                return dict(evidence), evidence
            if benchmark == "openra":
                process_id = 4100 + seed
                initial = {
                    "schema_version": 1,
                    "source": "openra_live_settings_process",
                    "observed": True,
                    "algorithm": "not_applicable_public_settings",
                    "seed": seed,
                    "process_id": process_id,
                    "process_alive_at_observation": True,
                    "benchmark": benchmark,
                    "task_id": task_id,
                    "semantic_fingerprint": {
                        "Game": {
                            "TargetLines": "Manual",
                            "ViewportEdgeScroll": True,
                        }
                    },
                }
                final = json.loads(json.dumps(initial))
                return final, {
                    "schema_version": 1,
                    "source": "openra_live_settings_process",
                    "observed": True,
                    "algorithm": "not_applicable_public_settings",
                    "seed": seed,
                    "task_id": task_id,
                    "initial": initial,
                    "final": final,
                }
            if benchmark.lower() in {"jynew", "pal3"}:
                unity_benchmark = benchmark.lower()
                process_id = 5100 + seed
                semantic_fingerprint = {
                    "source": f"{unity_benchmark}_live_semantic_probe_v1",
                    "status": "success",
                    "ok": True,
                    "checks": [
                        {"name": "fixture.live", "status": "success"}
                    ],
                }
                initial = {
                    "schema_version": 1,
                    "source": "unity_live_process",
                    "observed": True,
                    "algorithm": "not_applicable_unity_gui",
                    "seed": seed,
                    "process_id": process_id,
                    "process_alive_at_observation": True,
                    "benchmark": unity_benchmark,
                    "task_id": task_id,
                    "protocol_version": "warptest-c1-unity-v3",
                    "sequence": 3,
                    "semantic_fingerprint": semantic_fingerprint,
                }
                final = json.loads(json.dumps(initial))
                final["sequence"] = 7
                return final, {
                    "schema_version": 1,
                    "source": "unity_live_process",
                    "observed": True,
                    "algorithm": "not_applicable_unity_gui",
                    "seed": seed,
                    "task_id": task_id,
                    "initial": initial,
                    "final": final,
                }
            if benchmark.lower() in {"blender", "libreoffice"}:
                normalized = benchmark.lower()
                semantic_source = (
                    "blender_live_bpy_probe_v1"
                    if normalized == "blender"
                    else "libreoffice_live_uno_probe_v1"
                )
                seed_source = (
                    "blender_native_desktop_session_v1"
                    if normalized == "blender"
                    else "libreoffice_native_desktop_session_v1"
                )
                process_id = 6100 + seed

                def desktop_snapshot(stage: str, check_name: str) -> dict:
                    checks = [{"name": check_name, "ok": True}]
                    observation_extra: dict = {}
                    if normalized == "libreoffice":
                        goal = stage == "goal"
                        if goal:
                            checks.extend(
                                {"name": name, "ok": True}
                                for name in (
                                    "transition.session_same",
                                    "transition.saved_url",
                                    "transition.persisted_file",
                                    "transition.not_modified",
                                    "transition.document_reopened",
                                )
                            )
                        observation_extra = {
                            "document_identity": (
                                f"fixture-document-goal-{seed}"
                                if goal
                                else f"fixture-document-start-{seed}"
                            ),
                            "session_identity": f"fixture-office-session-{seed}",
                            "document_url": (
                                f"file:///tmp/fixture-policy-saved-{seed}.ods"
                                if goal
                                else ""
                            ),
                            "document_modified": not goal,
                            "document_path": (
                                f"/tmp/fixture-policy-saved-{seed}.ods"
                                if goal
                                else None
                            ),
                        }
                    observation = {
                        "status": "success",
                        "source": semantic_source,
                        "live": True,
                        "passed": True,
                        "checks": checks,
                        "errors": [],
                        **observation_extra,
                    }
                    fingerprint = hashlib.sha256(
                        json.dumps(
                            observation,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=True,
                            default=str,
                        ).encode("utf-8")
                    ).hexdigest()
                    return {
                        "schema_version": 1,
                        "source": seed_source,
                        "observed": True,
                        "algorithm": "not_applicable_native_gui",
                        "seed": seed,
                        "benchmark": benchmark,
                        "process_id": process_id,
                        "process_alive_at_observation": True,
                        "task_id": task_id,
                        "semantic_stage": stage,
                        "semantic_source": semantic_source,
                        "semantic_observed": True,
                        "semantic_fingerprint": fingerprint,
                        "semantic_observation": observation,
                        "initial_semantic_stage": stage,
                        "initial_semantic_source": semantic_source,
                        "initial_semantic_fingerprint": fingerprint,
                        "initial_semantic_observation": observation,
                        "randomness": "not_applicable",
                        "seed_role": "trial_pairing_only",
                        "execution_backend": "background_pixel_v1",
                        "foreground_required": False,
                        "coordinate_space": "frame_pixels_top_left_v1",
                        "capture_backend": (
                            "blender_bpy_screen_screenshot_v1"
                            if normalized == "blender"
                            else "libreoffice_svp_composited_v1"
                        ),
                        "input_backend": (
                            "blender_window_event_simulate_v1"
                            if normalized == "blender"
                            else "libreoffice_vcl_post_event_v1"
                        ),
                        "repo_commit": "a" * 40,
                        "executable_path": f"/Applications/{benchmark}.app/bin/{normalized}",
                        "executable_size": 1024,
                        "executable_mtime_ns": 123456789,
                    }

                initial = desktop_snapshot(semantic_stage, "fixture.start")
                final = desktop_snapshot("goal", "fixture.goal")
                return final, {
                    "schema_version": 1,
                    "source": seed_source,
                    "observed": True,
                    "algorithm": "not_applicable_native_gui",
                    "seed": seed,
                    "initial": initial,
                    "final": final,
                }
            if benchmark == "jupyterlab":
                semantic_source = "jupyterlab_contents_kernel_live_v1"
                screen = {"width": 1280, "height": 800}

                def web_snapshot(value: str) -> dict:
                    observation = {
                        "value": value,
                        "task_id": task_id,
                        "ui": {
                            "url": f"http://127.0.0.1:{7000 + seed}/live"
                        },
                    }
                    fingerprint = hashlib.sha256(
                        json.dumps(
                            observation,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ).encode("utf-8")
                    ).hexdigest()
                    stable = {key: item for key, item in observation.items() if key != "ui"}
                    stable_fingerprint = hashlib.sha256(
                        json.dumps(
                            stable,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ).encode("utf-8")
                    ).hexdigest()
                    return {
                        "schema_version": 1,
                        "source": "c1_web_background_pixel_snapshot_v1",
                        "observed": True,
                        "algorithm": "not_applicable_web_gui",
                        "seed": seed,
                        "benchmark": benchmark,
                        "task_id": task_id,
                        "headless": True,
                        "foreground_required": False,
                        "execution_backend": "background_pixel_v1",
                        "coordinate_space": "frame_pixels_top_left_v1",
                        "capture_backend": "playwright_headless_page_v1",
                        "process_id": 7100 + seed,
                        "process_alive_at_observation": True,
                        "app_url": f"http://127.0.0.1:{7000 + seed}/live",
                        "page_url": f"http://127.0.0.1:{7000 + seed}/live",
                        "viewport": {"width": 1280, "height": 800},
                        "device_scale_factor": 1,
                        "screen": screen,
                        "calibration": {
                            "passed": True,
                            "backend": "playwright_headless_page_v1",
                            "page_visible": True,
                            "screenshot": screen,
                            "click_space": screen,
                        },
                        "semantic_fingerprint": {
                            "source": semantic_source,
                            "status": "success",
                            "ok": True,
                            "observation": observation,
                            "fingerprint": fingerprint,
                            "stable_fingerprint": stable_fingerprint,
                        },
                    }

                initial = web_snapshot("start")
                final = web_snapshot("goal")
                return final, {
                    "schema_version": 1,
                    "source": "c1_web_background_pixel",
                    "observed": True,
                    "algorithm": "not_applicable_web_gui",
                    "seed": seed,
                    "benchmark": benchmark,
                    "task_id": task_id,
                    "initial": initial,
                    "final": final,
                }
            initial = {
                "source": "browser_page_runtime",
                "observed": True,
                "algorithm": "mulberry32",
                "seed": seed,
                "calls": 3,
                "viewport": {"width": 1280, "height": 800},
                "device_scale_factor": 1,
            }
            final = {**initial, "calls": 5}
            return final, {
                "schema_version": 1,
                "source": "browser_page_runtime",
                "observed": True,
                "algorithm": "mulberry32",
                "seed": seed,
                "initial": initial,
                "final": final,
            }

        def state_pair_seed_evidence(
            benchmark: str,
            seed: int,
            task_id: str,
        ) -> tuple[dict, dict]:
            normalized = benchmark.lower()
            if normalized == "openra":
                pair_source = "openra_state_process_pair"
                snapshot_source = "openra_state_live_process"
                algorithm = "not_applicable_public_settings_pair"
                canonical_benchmark = "openra"

                def observation() -> dict:
                    check = {
                        "section": "Game",
                        "key": "TargetLines",
                        "expected": "Automatic",
                        "actual": "Automatic",
                        "ok": True,
                    }
                    return {
                        "source": "openra_public_settings_yaml",
                        "live": True,
                        "status": "success",
                        "ok": True,
                        "checks": [check],
                        "mismatches": [],
                        "settings": {"Game": {"TargetLines": "Automatic"}},
                    }

                protocol = None
            else:
                pair_source = "unity_state_checkpoint_process_pair"
                snapshot_source = "unity_state_checkpoint_process"
                algorithm = "not_applicable_unity_checkpoint_pair"
                canonical_benchmark = normalized

                def observation() -> dict:
                    return {
                        "source": f"{normalized}_checkpoint_utility_report_v1",
                        "live": True,
                        "status": "success",
                        "detail": "All checks passed.",
                        "checks": [
                            {
                                "name": "fixture.utility",
                                "status": "success",
                                "detail": "fixture utility check passed",
                            }
                        ],
                        "screenshot_status": "success",
                        "screenshot_source": "unity_capture",
                    }

                protocol = "warptest-unity-checkpoint-state-v1"

            def snapshot(stage: str, process_id: int) -> dict:
                semantic = observation()
                fingerprint = hashlib.sha256(
                    json.dumps(
                        semantic,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()
                value = {
                    "schema_version": 1,
                    "source": snapshot_source,
                    "observed": True,
                    "algorithm": algorithm,
                    "seed": seed,
                    "task_id": task_id,
                    "benchmark": canonical_benchmark,
                    "stage": stage,
                    "process_id": process_id,
                    "process_alive_at_observation": True,
                    "semantic_observation": semantic,
                    "semantic_fingerprint": fingerprint,
                    "expected_task_id": task_id,
                    "expected_seed": seed,
                }
                if protocol is not None:
                    value.update(
                        protocol_version=protocol,
                        expected_stage=stage,
                        expected_benchmark=canonical_benchmark,
                    )
                return value

            initial = snapshot("before", 8100 + seed)
            final = snapshot("after", 9100 + seed)
            return final, {
                "schema_version": 1,
                "source": pair_source,
                "observed": True,
                "algorithm": algorithm,
                "seed": seed,
                "task_id": task_id,
                "benchmark": canonical_benchmark,
                "pair_semantics": "separate_registered_before_after_processes",
                "initial": initial,
                "final": final,
            }

        def cost_payload(components: dict[str, tuple[str, int]]) -> dict:
            counter_fields = (
                "calls", "prompt_tokens", "completion_tokens", "reasoning_tokens",
                "cached_prompt_tokens", "total_tokens", "latency_ms",
                "incomplete_calls", "unknown_prompt_token_calls",
                "unknown_completion_token_calls", "unknown_reasoning_token_calls",
                "unknown_total_token_calls",
            )

            def bucket(tokens: int) -> dict:
                calls = 1 if tokens else 0
                return {
                    "calls": calls,
                    "prompt_tokens": tokens,
                    "completion_tokens": 0,
                    "reasoning_tokens": 0,
                    "cached_prompt_tokens": 0,
                    "total_tokens": tokens,
                    "latency_ms": 10 if calls else 0,
                    "incomplete_calls": 0,
                    "unknown_prompt_token_calls": 0,
                    "unknown_completion_token_calls": 0,
                    "unknown_reasoning_token_calls": 0,
                    "unknown_total_token_calls": 0,
                    "usage_complete": True,
                }

            def merge(rows: list[dict]) -> dict:
                return {
                    **{field: sum(row[field] for row in rows) for field in counter_fields},
                    "usage_complete": True,
                }

            by_kind = {kind: bucket(tokens) for kind, (_model, tokens) in components.items()}
            by_model_rows: dict[str, list[dict]] = {}
            by_kind_model: dict[str, dict[str, dict]] = {}
            for kind, (component_model, _tokens) in components.items():
                by_model_rows.setdefault(component_model, []).append(by_kind[kind])
                by_kind_model[kind] = {component_model: dict(by_kind[kind])}
            top = merge(list(by_kind.values()))
            return {
                **top,
                "llm_calls": top.pop("calls"),
                "llm_latency_ms": top.pop("latency_ms"),
                "est_usd": None,
                "by_model": {
                    component_model: merge(rows)
                    for component_model, rows in by_model_rows.items()
                },
                "by_kind": by_kind,
                "by_kind_model": by_kind_model,
            }

        def apply_registered_task_contract(
            row: dict, task: dict, seed: int
        ) -> None:
            random_control = copy.deepcopy(task["random_control"])
            row.update(
                prompt_revision=task["prompt_revision"],
                random_control=random_control,
                random_control_evidence_valid=True,
            )
            evidence = row.get("seed_evidence")
            if not isinstance(evidence, dict):
                evidence = {
                    "schema_version": 1,
                    "source": "synthetic_formal_fixture",
                }
                row["seed_evidence"] = evidence
            evidence.update(
                observed=True,
                seed=seed,
                random_control=copy.deepcopy(random_control),
            )
            if random_control["mode"] == "experiment_seed":
                evidence["algorithm"] = random_control["algorithm"]
            else:
                evidence.update(
                    randomness="not_applicable",
                    seed_role="trial_pairing_only",
                )

        def public_semantic_fields(
            benchmark: str,
            *,
            task_id: str,
            synthesized: bool,
            seed_evidence: dict | None = None,
        ) -> dict:
            if benchmark == "datingsim":
                source = "love_production_save_roundtrip_v1"
                before_detail = {
                    "source": source,
                    "checks": [{"name": "fixture.semantic", "ok": True}],
                    "network": {},
                    "errors": {},
                    "snapshot": {"prior_restored": True},
                }
            elif benchmark == "spacerpg":
                source = "spacerpg_production_dom_save_v1"
                before_detail = {
                    "source": source,
                    "checks": [{"name": "fixture.semantic", "ok": True}],
                    "network": {},
                    "errors": {},
                    "snapshot": {"dom": {}, "storage": {}},
                }
            elif benchmark == "openra":
                adapter, before_contract = p5_aggregate._c1_expected_semantic_checks(
                    task_id, "before"
                )
                _, after_contract = p5_aggregate._c1_expected_semantic_checks(
                    task_id, "after"
                )
                if adapter == "openra_live_gameplay_probe_v1":
                    source = "openra_live_gameplay_probe_v1"

                    def gameplay_check(name: str) -> dict:
                        return {
                            "name": name,
                            "status": "success",
                            "detail": f"fixture {name}",
                            "expected": True,
                            "actual": True,
                        }

                    before_checks = [
                        gameplay_check(item["name"])
                        for item in before_contract
                    ]
                    after_checks = [
                        gameplay_check(item["name"])
                        for item in after_contract
                    ]
                    before_detail = {
                        "source": source,
                        "live": True,
                        "process_alive": True,
                        "report_path": "/tmp/openra-live-gameplay-report.json",
                        "sequence": 1,
                        "checks": before_checks,
                        "errors": [],
                        "world_tick": 100,
                    }
                    after_detail = {
                        "source": source,
                        "live": True,
                        "process_alive": True,
                        "report_path": "/tmp/openra-live-gameplay-report.json",
                        "sequence": 2,
                        "checks": after_checks,
                        "errors": [],
                        "world_tick": 200,
                    }
                    if isinstance(seed_evidence, dict):
                        for snapshot_name, checks in (
                            ("initial", before_checks),
                            ("final", after_checks),
                        ):
                            snapshot = seed_evidence.get(snapshot_name)
                            if isinstance(snapshot, dict):
                                snapshot["semantic_fingerprint"] = {
                                    "source": source,
                                    "checks": copy.deepcopy(checks),
                                }
                else:
                    source = "openra_public_settings_yaml"
                    def openra_check(item: dict) -> dict:
                        return {
                            "section": item["section"],
                            "key": item["key"],
                            "expected": item["expected"],
                            "actual": item["expected"],
                            "ok": True,
                        }

                    before_detail = {
                        "source": source,
                        "live": True,
                        "process_alive": True,
                        "settings_path": "/tmp/openra-support/settings.yaml",
                        "checks": [openra_check(item) for item in before_contract],
                        "mismatches": [],
                    }
                    after_detail = {
                        "source": source,
                        "live": True,
                        "process_alive": True,
                        "settings_path": "/tmp/openra-support/settings.yaml",
                        "checks": [openra_check(item) for item in after_contract],
                        "mismatches": [],
                    }
                    if isinstance(seed_evidence, dict):
                        initial_snapshot = seed_evidence.get("initial")
                        final_snapshot = seed_evidence.get("final")
                        if isinstance(initial_snapshot, dict):
                            initial_snapshot["semantic_fingerprint"] = {}
                            for item in before_contract:
                                initial_snapshot["semantic_fingerprint"].setdefault(
                                    item["section"], {}
                                )[item["key"]] = item["expected"]
                        if isinstance(final_snapshot, dict):
                            final_snapshot["semantic_fingerprint"] = {}
                            for item in after_contract:
                                final_snapshot["semantic_fingerprint"].setdefault(
                                    item["section"], {}
                                )[item["key"]] = item["expected"]
            elif benchmark.lower() in {"jynew", "pal3"}:
                normalized = benchmark.lower()
                source = f"{normalized}_live_semantic_probe_v1"
                transition_requirement = p5_aggregate._c1_save_load_requirement(
                    task_id
                )
                slot = (
                    transition_requirement["slot"]
                    if transition_requirement is not None
                    else None
                )
                transition_source = (
                    f"{normalized}_public_ui_save_load_witness_v1"
                )

                def transition(stage: str) -> dict:
                    if slot is None:
                        return {
                            "required": False,
                            "kind": "",
                            "slot": -1,
                            "source": transition_source,
                            "armed_sequence": -1,
                            "observed_sequence": 4 if stage == "before" else 5,
                            "save_observed": False,
                            "load_observed": False,
                            "save_frame": -1,
                            "load_frame": -1,
                            "ordered": False,
                            **(
                                {
                                    "reset_observed": False,
                                    "scene_load_observed": False,
                                    "reset_frame": -1,
                                    "scene_load_frame": -1,
                                }
                                if normalized == "pal3"
                                else {}
                            ),
                        }
                    value = {
                        "required": True,
                        "kind": "save_then_load",
                        "slot": slot,
                        "source": transition_source,
                        "armed_sequence": 4,
                        "observed_sequence": 4 if stage == "before" else 5,
                        "save_observed": stage == "after",
                        "load_observed": stage == "after",
                        "save_frame": -1 if stage == "before" else 100,
                        "load_frame": -1 if stage == "before" else 120,
                        "ordered": stage == "after",
                    }
                    if normalized == "pal3":
                        value.update(
                            reset_observed=stage == "after",
                            scene_load_observed=stage == "after",
                            reset_frame=-1 if stage == "before" else 105,
                            scene_load_frame=-1 if stage == "before" else 110,
                        )
                    return value

                def unity_check(name: str) -> dict:
                    return {
                        "name": name,
                        "status": "success",
                        "detail": f"fixture {name}",
                    }

                task_checks = {}
                for stage in ("before", "after"):
                    _, declared = p5_aggregate._c1_expected_semantic_checks(
                        task_id, stage
                    )
                    task_checks[stage] = [item["any_of"][0] for item in declared]
                before_checks = [unity_check(name) for name in task_checks["before"]]
                after_checks = [unity_check(name) for name in task_checks["after"]]
                before_detail = {
                    "source": source,
                    "live": True,
                    "detail": "C1 live operation succeeded.",
                    "checks": before_checks,
                    "transition_evidence": transition("before"),
                    "transition_evidence_valid": True,
                }
                after_detail = {
                    "source": source,
                    "live": True,
                    "detail": "C1 live operation succeeded.",
                    "checks": after_checks,
                    "transition_evidence": transition("after"),
                    "transition_evidence_valid": True,
                }
                if isinstance(seed_evidence, dict):
                    for snapshot_name, detail in (
                        ("initial", before_detail),
                        ("final", after_detail),
                    ):
                        snapshot = seed_evidence.get(snapshot_name)
                        evidence = detail["transition_evidence"]
                        if isinstance(snapshot, dict):
                            snapshot["sequence"] = evidence["observed_sequence"]
                            snapshot["semantic_fingerprint"] = {
                                "source": source,
                                "status": "success",
                                "ok": True,
                                "checks": copy.deepcopy(detail["checks"]),
                                "transition_evidence": copy.deepcopy(evidence),
                                "transition_evidence_valid": True,
                            }
            elif benchmark in {"blender", "libreoffice"}:
                source = (
                    "blender_live_bpy_probe_v1"
                    if benchmark == "blender"
                    else "libreoffice_live_uno_probe_v1"
                )
                if benchmark == "libreoffice" and isinstance(
                    seed_evidence, dict
                ):
                    initial = seed_evidence.get("initial")
                    final = seed_evidence.get("final")
                    before_detail = json.loads(
                        json.dumps(
                            initial.get("semantic_observation")
                            if isinstance(initial, dict)
                            else {}
                        )
                    )
                    after_detail = json.loads(
                        json.dumps(
                            final.get("semantic_observation")
                            if isinstance(final, dict)
                            else {}
                        )
                    )
                else:
                    before_detail = {
                        "source": source,
                        "live": True,
                        "checks": [{"name": "fixture.semantic", "ok": True}],
                        "errors": [],
                    }
                    if benchmark == "libreoffice":
                        after_detail = json.loads(json.dumps(before_detail))
            elif benchmark == "jupyterlab":
                initial = (
                    seed_evidence.get("initial")
                    if isinstance(seed_evidence, dict)
                    else None
                )
                final = (
                    seed_evidence.get("final")
                    if isinstance(seed_evidence, dict)
                    else None
                )
                initial_semantic = (
                    initial.get("semantic_fingerprint")
                    if isinstance(initial, dict)
                    else {}
                )
                final_semantic = (
                    final.get("semantic_fingerprint")
                    if isinstance(final, dict)
                    else {}
                )
                source = str(initial_semantic.get("source") or "")
                before_detail = {
                    **initial_semantic,
                    "detail": "fixture live start",
                    "provenance_valid": True,
                }
                after_detail = {
                    **final_semantic,
                    "detail": "fixture live goal",
                    "provenance_valid": True,
                }
            else:
                source = "profile_semantic_v1"
                before_detail = {"source": source, "snapshot": {}}
            if benchmark not in {
                "jupyterlab",
                "libreoffice",
                "openra",
                "jynew",
                "pal3",
            }:
                after_detail = json.loads(json.dumps(before_detail))
            if benchmark in {"datingsim", "spacerpg"}:
                after_detail["transition_trace"] = {
                    "screens": [],
                    "events": [],
                    "errors": [],
                }
            fields = {
                "semantic_probe_source": source,
                "policy_action_interface": "pixel_mouse_keyboard_only",
                "policy_received_semantic_probe": False,
                "before_semantic_detail": before_detail,
                "after_semantic_detail": after_detail,
            }
            if benchmark in {"blender", "libreoffice"}:
                fields["policy_observation_source"] = (
                    "application_framebuffer_pixels"
                )
            if benchmark == "jupyterlab":
                fields["policy_observation_source"] = (
                    "playwright_page_pixels"
                )
                fields["scripted_transition_executed"] = False
            if benchmark == "openra" or benchmark.lower() in {"jynew", "pal3"}:
                fields.update(
                    policy_observation_source="application_framebuffer_pixels",
                    scripted_transition_executed=False,
                    before_semantic_status="success",
                    after_semantic_status="success",
                    semantic_status="success",
                    semantic_ok=True,
                    semantic_detail=after_detail,
                )
            if benchmark in {"datingsim", "spacerpg"} and not synthesized:
                fields["policy_runtime_attestation"] = (
                    {
                        "required": True,
                        "passed": True,
                        "entry_surface": "public_ai_query_mock",
                        "private_hook_present": False,
                        "ai_query_mode": "mocked",
                        "ai_storage_mode": None,
                    }
                    if benchmark == "datingsim"
                    else {
                        "required": True,
                        "passed": True,
                        "entry_surface": "public_offline_economy",
                        "private_hook_present": False,
                        "economy_backend_disabled": True,
                        "hook_storage_enabled": False,
                    }
                )
            return fields

        reports: dict[str, list[dict]] = {arm: [] for arm in p5_scale.C1_ARMS}
        for arm in p5_scale.C1_ARMS:
            for model in models:
                for seed in range(3):
                    results = []
                    for task in task_rows:
                        task_id = task["id"]
                        benchmark = task["benchmark"]
                        horizon = task["horizon_class"]
                        budget = C1_STEP_BUDGETS[horizon]
                        if arm == "c1_state":
                            state_cost = cost_payload({
                                "checkpoint_synthesis": (model, 20),
                                "visual_judge": ("judge", 30),
                            })
                            row = {
                                "task_id": task_id,
                                "benchmark": benchmark,
                                "horizon_class": horizon,
                                "seed": seed,
                                "result": True,
                                "visual_grading_started": True,
                                "reference_reachable": True,
                                "validity_class": "save-restorable",
                                "time_to_start_ms": 5,
                                "time_start_to_goal_ms": 7,
                                "time_e2e_excluding_synthesis_ms": 12,
                                "time_e2e_including_synthesis_ms": 14,
                                "checkpoint_synthesis_ms": 2,
                                "checkpoint_restore_ms": 1,
                                "runtime_bootstrap_ms": 1,
                                "grader_ms": 1,
                                "termination_time_ms": 14,
                                "cost": state_cost,
                                # The state arm restores the same accepted start
                                # as the synthesized GUI arm, so it records the
                                # same arm-independent spec hash.
                                "generated_spec_hash": (
                                    f"spec-{model}-{seed}-{task_id}"
                                ),
                            }
                            state_prng, state_seed_evidence = runtime_seed_evidence(
                                benchmark,
                                seed,
                                task_id,
                                "restored",
                            )
                            row.update(
                                runtime_started=True,
                                prng=state_prng,
                                seed_evidence=state_seed_evidence,
                            )
                            if benchmark == "openra" or benchmark.lower() in {
                                "jynew",
                                "pal3",
                            }:
                                state_prng, state_seed_evidence = (
                                    state_pair_seed_evidence(
                                        benchmark,
                                        seed,
                                        task_id,
                                    )
                                )
                                row.update(
                                    runtime_started=True,
                                    prng=state_prng,
                                    seed_evidence=state_seed_evidence,
                                )
                            elif benchmark in {"blender", "libreoffice"}:
                                state_prng, state_seed_evidence = (
                                    runtime_seed_evidence(
                                        benchmark,
                                        seed,
                                        task_id,
                                        "restored",
                                    )
                                )
                                row.update(
                                    runtime_started=True,
                                    prng=state_prng,
                                    seed_evidence=state_seed_evidence,
                                    scripted_transition_executed=True,
                                )
                            apply_registered_task_contract(row, task, seed)
                            _stamp_single_attempt_ledger(
                                row,
                                task_actual_cost=state_cost,
                                is_state=True,
                            )
                            results.append(row)
                            continue
                        synthesized = arm in {"c1_gui_synth", "c1_agent_s_synth"}
                        prng, seed_evidence = runtime_seed_evidence(
                            benchmark,
                            seed,
                            task_id,
                            "restored" if synthesized else "entry",
                        )
                        task_modules = {
                            "visual_observer": (
                                "observer",
                                15 if not synthesized else 10,
                            ),
                            **(
                                {
                                    "gui_policy": (
                                        model,
                                        55 if not synthesized else 35,
                                    )
                                }
                                if arm in {"c1_gui", "c1_gui_synth"}
                                else {
                                    "agent_s_worker": (
                                        model,
                                        35 if not synthesized else 20,
                                    ),
                                    "agent_s_reflection": (
                                        model,
                                        20 if not synthesized else 15,
                                    ),
                                }
                            ),
                            "visual_judge": ("judge", 30),
                        }
                        task_actual_cost = cost_payload(task_modules)
                        row = {
                            "task_id": task_id,
                            "benchmark": benchmark,
                            "horizon_class": horizon,
                            "seed": seed,
                            "arm": arm,
                            "result": True,
                            "incomplete": False,
                            "before_visual_ok": True,
                            "before_semantic_ok": True,
                            "after_visual_ok": True,
                            "after_semantic_ok": True,
                            "start_success": True,
                            "goal_success": True,
                            "e2e_success": True,
                            **public_semantic_fields(
                                benchmark,
                                task_id=task_id,
                                synthesized=synthesized,
                                seed_evidence=seed_evidence,
                            ),
                            "time_to_start_ms": 10,
                            "time_start_to_goal_ms": 20,
                            "time_e2e_excluding_synthesis_ms": 30,
                            "time_e2e_including_synthesis_ms": 35 if synthesized else 30,
                            "checkpoint_synthesis_ms": 5 if synthesized else 0,
                            "checkpoint_restore_ms": 0,
                            "runtime_bootstrap_ms": 2,
                            "grader_ms": 3,
                            "termination_time_ms": 35 if synthesized else 30,
                            "decision_budget": budget,
                            "steps": {
                                "phase_a": 0 if synthesized else 1,
                                "phase_b": 1,
                                "total": 1 if synthesized else 2,
                            },
                            "budget_exhausted": False,
                            "runtime_started": True,
                            "policy_phase_started": True,
                            "visual_grading_started": True,
                            "prng": prng,
                            "seed_evidence": seed_evidence,
                            "initial_state_fingerprint": (
                                f"{'synth' if synthesized else 'from'}-{model}-{seed}-{task_id}"
                            ),
                            "generated_spec_fingerprint": (
                                f"spec-{model}-{seed}-{task_id}" if synthesized else None
                            ),
                            # Every self-synthesizing arm, including the state arm,
                            # restores the one start accepted for this
                            # (model, seed, task), so the hash is arm-independent.
                            "generated_spec_hash": (
                                f"spec-{model}-{seed}-{task_id}"
                                if arm in _SELF_SYNTHESIZING_C1_ARMS
                                else None
                            ),
                            "synthesis_cache_fingerprint": (
                                f"cache-{model}-{seed}-{task_id}" if synthesized else None
                            ),
                            "synthesis_cache_hit": False,
                            "synthesis_actual_incurred_this_attempt": arm == "c1_agent_s_synth",
                            "synthesis_attributed_fingerprints": (
                                [f"cache-{model}-{seed}-{task_id}"] if synthesized else []
                            ),
                            "synthesis_attributed_burdens": (
                                [{
                                    "cache_key_fingerprint": f"cache-{model}-{seed}-{task_id}",
                                    "counterfactual_burden_fingerprint": f"burden-{model}-{seed}-{task_id}",
                                }]
                                if synthesized else []
                            ),
                            "reference_reachable": True,
                            "validity_class": "save-restorable",
                            "cost": cost.merge_summaries(
                                [
                                    task_actual_cost,
                                    cost_payload(
                                        {"checkpoint_synthesis": (model, 25)}
                                    )
                                    if synthesized
                                    else cost_payload({}),
                                ]
                            ),
                            "synthesis_actual_cost": (
                                cost_payload({"checkpoint_synthesis": (model, 25)})
                                if arm == "c1_agent_s_synth"
                                else cost_payload({})
                            ),
                            "synthesis_transient_actual_cost": cost_payload({}),
                            "synthesis_attributed_cost": (
                                cost_payload({"checkpoint_synthesis": (model, 25)})
                                if synthesized
                                else cost_payload({})
                            ),
                        }
                        apply_registered_task_contract(row, task, seed)
                        _stamp_single_attempt_ledger(
                            row,
                            task_actual_cost=task_actual_cost,
                            is_state=False,
                        )
                        results.append(row)
                    report = {
                        "schema_version": 4 if arm == "c1_state" else 5,
                        "complete": True,
                        "seed": seed,
                        "results": results,
                    }
                    if arm == "c1_state":
                        report["synthesis_model"] = model
                    else:
                        report["model"] = model
                        report["step_budgets"] = dict(C1_STEP_BUDGETS)
                        report["policy_runtime_metadata"] = {
                            "protocol_version": 2,
                            "execution_backend": "background_pixel_v1",
                            "coordinate_space": "frame_pixels_top_left_v1",
                            "raw_screenshot_observation": True,
                            "action_execution_owner": "parent_runtime",
                            "foreground_required": False,
                            "local_code_enabled": False,
                            "native_exec_enabled": False,
                        }
                    reports[arm].append(report)
        return reports, models, [task["id"] for task in task_rows]

    def _run_full_formal_fixture(
        self,
        reports: dict[str, list[dict]],
        models: list[str],
        task_ids: list[str],
    ) -> dict:
        task_rows = yaml.safe_load(
            (ROOT / "benchmarks/p5/c1_tasks.yaml").read_text()
        )["tasks"]
        manifest = p5_scale.load_manifest(ROOT / "benchmarks/p5/manifest.yaml")
        return p5_aggregate.c1_from_reports(
            reports["c1_state"],
            reports.get("c1_gui", []),
            gui_synth_reports=reports.get("c1_gui_synth", []),
            agent_s_reports=reports["c1_agent_s"],
            agent_s_synth_reports=reports["c1_agent_s_synth"],
            subset_ids=task_ids,
            rng=random.Random(1),
            iters=2,
            ci=0.95,
            expected_models=models,
            expected_seeds=3,
            expected_arms=list(p5_scale.C1_FORMAL_PAPER_ARMS),
            calibration_gate=self.CALIBRATION_GATE,
            formal=True,
            expected_headless=False,
            expected_task_metadata={
                task["id"]: {
                    "benchmark": task["benchmark"],
                    "horizon_class": task["horizon_class"],
                    "capabilities": task["capabilities"],
                    "prompt_revision": task["prompt_revision"],
                    "random_control": copy.deepcopy(task["random_control"]),
                }
                for task in task_rows
            },
            expected_game_counts=manifest["c1_benchmark_task_counts"],
            administrative_rmst_tau_ms=manifest["stats"][
                "c1_administrative_rmst_tau_ms"
            ],
            expected_observer_model="observer",
            expected_grounder_model="grounder",
            expected_judge_model="judge",
            expected_retry_max_attempts=manifest["retry"]["max_attempts"],
        )

    def _run_index(self, root: Path) -> dict:
        state = _write(root / "c1_state.json", {
            "experiment": "state_loop", "synthesis_model": "M1",
            "results": [
                {"task_id": "2048-merge-left", "benchmark": "2048", "result": True,
                 "cost": {"est_usd": 0.001, "total_tokens": 100, "llm_latency_ms": 50}},
                {"task_id": "spacerpg-playable-flight", "benchmark": "SpaceRPG", "result": True,
                 "cost": {"est_usd": 0.002, "total_tokens": 200, "llm_latency_ms": 80}},
            ],
            "summary": {"pass_at_1": 1.0},
        })
        gui = _write(root / "c1_agent_s.json", {
            "experiment": "gui_agent_baseline", "model": "M1",
            "results": [
                {"task_id": "2048-merge-left", "benchmark": "2048", "result": False,
                 "wall_ms": 5000, "cost": {"est_usd": 0.02, "total_tokens": 5000, "llm_latency_ms": 4000}},
                {"task_id": "spacerpg-playable-flight", "benchmark": "SpaceRPG", "result": False,
                 "wall_ms": 9000, "cost": {"est_usd": 0.05, "total_tokens": 9000}},
            ],
            "summary": {"passed": 0, "total_tasks": 2},
        })
        phase3 = _write(root / "c2_phase3.json", {
            "experiment": "evo_phase3_core_suite", "run_context": {"model": "M1"},
            "summary": {"variants": [
                {"id": "A5", "name": "evolution_no_memory", "heldout_strict_success": 1, "transfer_gain_vs_A5": 0, "heldout_tasks": 24},
                {"id": "A9", "name": "full_evoportal", "heldout_strict_success": 3, "transfer_gain_vs_A5": 2, "heldout_tasks": 24},
            ]},
        })
        c2_harness = "c" * 64
        c2_fingerprint = "f" * 64
        c2_profiles = {"M1": "provider_default"}
        c2_profiles_sha256 = hashlib.sha256(
            p5_scale.canonical_profiles_json(c2_profiles).encode("utf-8")
        ).hexdigest()
        mem = _write(root / "c2_mem.json", {
            "experiment": "memory_baseline_transfer_suite", "anchor_backend": "stateportal-anchor",
            "run_context": {
                "model": "M1",
                "c2_harness_hash": c2_harness,
                "c2_manifest_fingerprint": c2_fingerprint,
                "model_request_profiles": c2_profiles,
                "model_request_profiles_sha256": c2_profiles_sha256,
                "model_request_profile": p5_scale.ModelRequestProfile(
                    "M1", "provider_default"
                ).provenance(),
            },
            "formal_validity": {"status": "valid"},
            "summary": {
                "anchor_present": True,
                "anchor_heldout_strict_success": 5,
                "anchor_delta_vs_baseline": {"amem": 2, "mem0": 3},
            },
        })
        fuzz0 = _write(root / "fuzz0.json", {
            "experiment": "state_fuzzing",
            "results": [{"benchmark": "2048", "task_id": "2048-merge-left", "mutations": [
                {"verdict": "defect", "defect_type": "console_error", "defect_path": "browser", "fingerprint": "fpA", "reachability": "injection-only"},
                {"verdict": "defect", "defect_type": "blank_render", "defect_path": "browser", "fingerprint": "fpB", "reachability": "injection-only"},
                {"verdict": "robust"},
            ]}],
            "summary": {"defects": 2, "unique_defects": 2},
        })
        fuzz1 = _write(root / "fuzz1.json", {
            "experiment": "state_fuzzing",
            "results": [{"benchmark": "2048", "task_id": "2048-merge-left", "mutations": [
                {"verdict": "defect", "defect_type": "console_error", "defect_path": "browser", "fingerprint": "fpA", "reachability": "player-reachable"},
            ]}],
            "summary": {"defects": 1, "unique_defects": 1},
        })
        return {
            "experiment_id": "p5_scale", "run_id": "test", "git_commit": "abc",
            "task_manifest_hash": "h", "c2_harness_hash": c2_harness,
            "c2_manifest_fingerprint": c2_fingerprint,
            "model_request_profiles": c2_profiles,
            "model_request_profiles_sha256": c2_profiles_sha256,
            "models": ["M1"], "seeds": 2,
            "visual_bridge": {
                "observer_model": "observer@relay",
                "grounder_model": "grounder@relay",
            },
            "visual_judge": {
                "model": "judge@relay",
                "base_url": "https://judge.invalid/v1",
            },
            "arms": {
                "c1_state": [{"model": "M1", "report": state}],
                "c1_agent_s": [{"model": "M1", "report": gui}],
                "c2_phase3": [{"model": "M1", "report": phase3}],
                "c2_memory_baseline": [{
                    "model": "M1", "seed": 0, "status": "ok", "report": mem,
                    "report_sha256": hashlib.sha256(Path(mem).read_bytes()).hexdigest(),
                }],
                "c3_fuzz": [{"seed": 0, "report": fuzz0}, {"seed": 1, "report": fuzz1}],
            },
        }

    def test_aggregate_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {"arms": {"c1_agent_s": {"subset": [
                {"id": "2048-merge-left"}, {"id": "spacerpg-playable-flight"}, {"id": "love-resource-action"},
            ]}}}
            summary = p5_aggregate.aggregate(self._run_index(root), manifest=manifest,
                                             report_root=root, rng_seed=1, iters=200)
            self.assertEqual(summary["provenance"]["visual_bridge"], {
                "observer_model": "observer@relay",
                "grounder_model": "grounder@relay",
            })
            self.assertEqual(summary["provenance"]["visual_judge"], {
                "model": "judge@relay",
                "base_url": "https://judge.invalid/v1",
            })
            # C1: aligns on the GUI subset; state passes the 2 present subset ids, gui fails them.
            c1 = summary["c1_cost_accuracy"]
            self.assertEqual(c1["align_on"], "c1_task_file")
            methods = {p["method"]: p for p in c1["points"]}
            self.assertAlmostEqual(methods["state_managed"]["deep_state_pass"]["mean"], 1.0)
            self.assertAlmostEqual(methods["agent_s_from_start"]["deep_state_pass"]["mean"], 0.0)
            # Legacy partial reports remain inspectable but cannot become a
            # complete result under the v2 three-arm completeness gate.
            self.assertFalse(c1["complete"])
            self.assertGreater(c1["coverage"]["missing_trials"], 0)
            # C2: transfer gain vs A5 == 2; anchor deltas preserved.
            c2 = summary["c2_transfer"]
            self.assertAlmostEqual(c2["phase3_aggregate"]["transfer_gain_vs_A5"]["mean"], 2.0)
            self.assertAlmostEqual(c2["memory_baseline_aggregate"]["anchor_minus_baseline"]["amem"]["mean"], 2.0)
            self.assertAlmostEqual(c2["memory_baseline_aggregate"]["anchor_minus_baseline"]["mem0"]["mean"], 3.0)
            # C3: unique deduped across seeds (fpA once) -> 2 unique of 3 raw.
            c3 = summary["c3_robustness"]
            self.assertTrue(c3["complete"])
            self.assertEqual(c3["unique_defects_total"], 2)
            self.assertEqual(c3["raw_defects_total"], 3)
            self.assertEqual(c3["cumulative_curve"][-1]["unique_defects"], 2)
            # A verified player-reachable reproduction promotes the fingerprint,
            # while the independent fpB observation remains injection-only.
            self.assertEqual(c3["unique_by_reachability"], {
                "player-reachable": 1,
                "injection-only": 1,
                "unclassified": 0,
            })

    def test_c2_aggregate_excludes_unvalidated_or_hash_mismatched_ok_reports(self) -> None:
        """Legacy zero-score evidence must not produce an anchor effect."""

        c2_harness = "c" * 64
        c2_fingerprint = "f" * 64
        profiles = {"kimi-k2.6": "thinking_disabled"}
        profiles_sha256 = hashlib.sha256(
            p5_scale.canonical_profiles_json(profiles).encode("utf-8")
        ).hexdigest()

        def aggregate_case(root: Path, case: str) -> dict:
            payload = {
                "experiment": "memory_baseline_transfer_suite",
                "anchor_backend": "stateportal-anchor",
                "run_context": {
                    "model": "kimi-k2.6@relay",
                    "c2_harness_hash": c2_harness,
                    "c2_manifest_fingerprint": c2_fingerprint,
                    "model_request_profiles": profiles,
                    "model_request_profiles_sha256": profiles_sha256,
                    "model_request_profile": p5_scale.ModelRequestProfile(
                        "kimi-k2.6", "thinking_disabled"
                    ).provenance(),
                },
                "formal_validity": {"status": "valid"},
                "summary": {
                    "anchor_present": True,
                    "anchor_heldout_strict_success": 0,
                    "anchor_delta_vs_baseline": {"amem": 0, "mem0": 0},
                },
            }
            if case == "legacy_missing_validity":
                payload.pop("formal_validity")
            elif case == "invalid_validity":
                payload["formal_validity"] = {"status": "invalid"}
            elif case == "harness_mismatch":
                payload["run_context"]["c2_harness_hash"] = "d" * 64
            elif case == "profile_mismatch":
                payload["run_context"]["model_request_profile"] = (
                    p5_scale.ModelRequestProfile(
                        "kimi-k2.6", "provider_default"
                    ).provenance()
                )
            elif case == "malformed_summary":
                payload.pop("summary")
            report = Path(_write(root / f"{case}.json", payload))
            report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
            if case == "report_sha_mismatch":
                report_sha = "0" * 64
            run_index = {
                "experiment_id": "p5_scale",
                "run_id": f"test-{case}",
                "models": ["kimi-k2.6@relay"],
                "seeds": 1,
                "arms_run": ["c2_memory_baseline"],
                "c2_harness_hash": c2_harness,
                "c2_manifest_fingerprint": c2_fingerprint,
                "model_request_profiles": profiles,
                "model_request_profiles_sha256": profiles_sha256,
                "arms": {
                    "c2_memory_baseline": [
                        {
                            "model": "kimi-k2.6@relay",
                            "seed": 0,
                            "status": "ok",
                            "report": str(report),
                            "report_sha256": report_sha,
                        }
                    ]
                },
            }
            with patch(
                "warptest.p5_aggregate.c1_from_reports",
                return_value={"complete": True, "coverage": {}, "points": []},
            ):
                return p5_aggregate.aggregate(
                    run_index,
                    report_root=root,
                    rng_seed=1,
                    iters=20,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = aggregate_case(root, "valid")
            self.assertTrue(valid["complete"])
            self.assertTrue(valid["c2_transfer"]["complete"])
            self.assertEqual(len(valid["c2_transfer"]["memory_baseline_rows"]), 1)
            self.assertEqual(
                valid["c2_transfer"]["memory_baseline_aggregate"]
                ["anchor_minus_baseline"]["amem"]["mean"],
                0.0,
            )

            cases = {
                "legacy_missing_validity": "formal_validity.status is not valid",
                "invalid_validity": "formal_validity.status is not valid",
                "report_sha_mismatch": "report_sha256 mismatch",
                "harness_mismatch": "c2_harness_hash does not match",
                "profile_mismatch": "model_request_profile does not match",
                "malformed_summary": "report summary is missing",
            }
            for case, expected_reason in cases.items():
                with self.subTest(case=case):
                    summary = aggregate_case(root, case)
                    c2 = summary["c2_transfer"]
                    self.assertFalse(summary["complete"])
                    self.assertFalse(c2["complete"])
                    self.assertEqual(c2["memory_baseline_rows"], [])
                    self.assertEqual(
                        c2["memory_baseline_aggregate"]["anchor_minus_baseline"],
                        {},
                    )
                    self.assertIn(
                        expected_reason,
                        "\n".join(c2["incomplete_reasons"]),
                    )

            direct_legacy = p5_aggregate.c2_from_reports(
                [],
                [
                    {
                        "experiment": "memory_baseline_transfer_suite",
                        "summary": {
                            "anchor_present": True,
                            "anchor_heldout_strict_success": 0,
                            "anchor_delta_vs_baseline": {"amem": 0, "mem0": 0},
                        },
                    }
                ],
                rng=random.Random(1),
                iters=20,
                ci=0.95,
                expected_memory_reports=1,
            )
            self.assertFalse(direct_legacy["complete"])
            self.assertEqual(direct_legacy["memory_baseline_rows"], [])
            self.assertEqual(
                direct_legacy["memory_baseline_aggregate"]["anchor_minus_baseline"],
                {},
            )

    def test_sidecar_validates_non_secret_visual_judge_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            report_path = directory / "report.json"
            entry = {"model": "M1", "seed": 0}
            runtime_lock = {
                "scope": "machine-shared",
                "lock_dir": str(Path("/tmp/warptest-runtime-locks").resolve()),
            }
            run_index = {
                "run_id": "run",
                "git_commit": "git",
                "manifest_hash": "manifest",
                "task_manifest_hash": "tasks",
                "harness_hash": "harness",
                "runtime_lock": runtime_lock,
                "c1_cache_policy": {"self_synthesis": "/tmp/synth-cache"},
            }
            sidecar = {
                "schema_version": 4,
                "kind": "p5_c1_invocation_context",
                "run_id": "run",
                "arm": "c1_gui",
                "model": "M1",
                "seed": 0,
                "git_commit": "git",
                "manifest_hash": "manifest",
                "task_manifest_hash": "tasks",
                "harness_hash": "harness",
                "runtime_lock": runtime_lock,
                "cache_policy": {"self_synthesis": "/tmp/synth-cache"},
                "model_request_profile": {
                    "schema_version": 1,
                    "model": "M1",
                    "profile": "provider_default",
                    "request_overrides": {},
                    "fingerprint": "939b8dd5d5fb73fc6aff37a28f2a8f097127b5eb80d530298cf6601c3d0be1bc",
                },
                "invocation": {
                    "start_mode": "from_start",
                    "policy_kind": "gui_agent",
                    "step_budgets": dict(C1_STEP_BUDGETS),
                    "observer_model": "observer@relay",
                    "grounder_model": "grounder@relay",
                    "synth_model": "M1",
                    "shared_synthesis_cache": "/tmp/synth-cache",
                },
                "visual_judge": {
                    "model": "judge@relay",
                    "base_url": "https://judge.invalid/v1",
                },
                "task_set": {
                    "task_ids": ["t1", "t2"],
                    "source_task_file": "/repo/benchmarks/p5/c1_tasks.yaml",
                    "source_task_file_sha256": "source-sha",
                    "task_set_hash": "task-set-hash",
                },
            }
            context_path = directory / "p5-invocation-context.json"
            context_path.write_text(json.dumps(sidecar), encoding="utf-8")
            report = {
                "policy_kind": "gui_agent",
                "start_mode": "from_start",
                "step_budgets": dict(C1_STEP_BUDGETS),
                "observer_model": "observer@relay",
                "grounder_model": None,
                "policy_runtime_metadata": {
                    "policy_kind": "gui_agent",
                    "model": "M1",
                    "provider": None,
                    "model_request_profile": {
                        "schema_version": 1,
                        "model": "M1",
                        "profile": "provider_default",
                        "request_overrides": {},
                        "fingerprint": "939b8dd5d5fb73fc6aff37a28f2a8f097127b5eb80d530298cf6601c3d0be1bc",
                    },
                    "max_tokens": 8192,
                    "reasoning_retry_max_tokens": 16384,
                    "sdk_max_retries": 0,
                },
                "run_context": {
                    "arm": "c1_gui",
                    "model": "M1",
                    "seed": 0,
                    "git_commit": "git",
                    "task_manifest_hash": "tasks",
                    "runtime_lock": runtime_lock,
                }
            }
            expected_judge = {
                "model": "judge@relay",
                "base_url": "https://judge.invalid/v1",
            }
            expected_bridge = {
                "observer_model": "observer@relay",
                "grounder_model": "grounder@relay",
            }
            expected_task_set = dict(sidecar["task_set"])
            self.assertEqual(
                p5_aggregate._c1_sidecar_issues(
                    report_path,
                    arm="c1_gui",
                    entry=entry,
                    run_index=run_index,
                    report=report,
                    visual_bridge=expected_bridge,
                    visual_judge=expected_judge,
                    expected_task_set=expected_task_set,
                ),
                [],
            )
            sidecar["runtime_lock"] = {
                **runtime_lock,
                "lock_dir": "/tmp/wrong-runtime-locks",
            }
            context_path.write_text(json.dumps(sidecar), encoding="utf-8")
            issues = p5_aggregate._c1_sidecar_issues(
                report_path,
                arm="c1_gui",
                entry=entry,
                run_index=run_index,
                report=report,
                visual_bridge=expected_bridge,
                visual_judge=expected_judge,
                expected_task_set=expected_task_set,
            )
            self.assertTrue(
                any("sidecar runtime_lock mismatch" in issue for issue in issues)
            )
            sidecar["runtime_lock"] = runtime_lock
            context_path.write_text(json.dumps(sidecar), encoding="utf-8")
            report["run_context"]["runtime_lock"] = {
                **runtime_lock,
                "scope": "report-scoped",
            }
            issues = p5_aggregate._c1_sidecar_issues(
                report_path,
                arm="c1_gui",
                entry=entry,
                run_index=run_index,
                report=report,
                visual_bridge=expected_bridge,
                visual_judge=expected_judge,
                expected_task_set=expected_task_set,
            )
            self.assertTrue(
                any("report runtime_lock provenance mismatch" in issue for issue in issues)
            )
            report["run_context"]["runtime_lock"] = runtime_lock
            sidecar["visual_judge"]["base_url"] = "https://wrong.invalid/v1"
            context_path.write_text(json.dumps(sidecar), encoding="utf-8")
            issues = p5_aggregate._c1_sidecar_issues(
                report_path,
                arm="c1_gui",
                entry=entry,
                run_index=run_index,
                report=report,
                visual_bridge=expected_bridge,
                visual_judge=expected_judge,
                expected_task_set=expected_task_set,
            )
            self.assertTrue(any("visual_judge.base_url mismatch" in issue for issue in issues))
            sidecar["visual_judge"]["base_url"] = "https://judge.invalid/v1"
            sidecar["task_set"]["source_task_file_sha256"] = "wrong-source-sha"
            context_path.write_text(json.dumps(sidecar), encoding="utf-8")
            issues = p5_aggregate._c1_sidecar_issues(
                report_path,
                arm="c1_gui",
                entry=entry,
                run_index=run_index,
                report=report,
                visual_bridge=expected_bridge,
                visual_judge=expected_judge,
                expected_task_set=expected_task_set,
            )
            self.assertTrue(
                any("task_set.source_task_file_sha256 mismatch" in issue for issue in issues)
            )

    def test_formal_preflight_gate_rejects_identity_usage_profile_and_retry_tampering(self) -> None:
        profiles = {model: "provider_default" for model in ("M1", "M2", "M3", "M4")}
        expected = [
            {"name": f"decision:{model}", "model": model, "modality": "vision"}
            for model in profiles
        ] + [
            {"name": "visual_observer", "model": "observer", "modality": "vision"},
            {"name": "agent_s_grounder", "model": "grounder", "modality": "vision"},
            {"name": "visual_judge", "model": "judge", "modality": "vision"},
        ]
        provider = {"name": "relay", "base_url": "https://example.invalid/v1"}
        usage = {
            "prompt_tokens": 4,
            "completion_tokens": 2,
            "reasoning_tokens": 1,
            "cached_prompt_tokens": 1,
            "total_tokens": 6,
        }
        results = []
        for row in expected:
            profile = p5_aggregate.ModelRequestProfile(
                row["model"], profiles.get(row["model"], "provider_default")
            ).provenance()
            results.append({
                **row,
                "provider": "relay",
                "success": True,
                "attempts": 1,
                "finish_reason": "stop",
                "request_profile": profile,
                "attempt_records": [{
                    "ordinal": 1,
                    "max_tokens": 8192,
                    "request_succeeded": True,
                    "content_nonempty": True,
                    "finish_reason": "stop",
                    "reasoning_observed": True,
                    "reasoning_content_observed": False,
                    "reasoning_token_anomaly": False,
                    "request_contract_passed": True,
                    "reasoning_exhausted": False,
                    "usage": dict(usage),
                }],
                "request_contract_passed": True,
                "reasoning_content_observed": False,
                "reasoning_token_anomaly": False,
                "usage": dict(usage),
            })
        gate = {
            "schema_version": 2,
            "complete": True,
            "generated_at_utc": "2026-07-11T00:00:00+00:00",
            "artifact_sha256": "a" * 64,
            "git_commit": "git",
            "manifest_hash": "manifest",
            "harness_hash": "harness",
            "api_provider": provider,
            "model_request_profiles": profiles,
            "model_request_profiles_sha256": "b" * 64,
            "probe_count": len(results),
            "initial_max_tokens": 8192,
            "retry_max_tokens": 16384,
            "retry_rule": "one_retry_only_if_empty_content_and_explicit_output_limit",
            "results": results,
        }

        def valid(value: dict) -> bool:
            return p5_aggregate._valid_preflight_gate(
                value,
                expected_git_commit="git",
                expected_manifest_hash="manifest",
                expected_harness_hash="harness",
                expected_api_provider=provider,
                expected_profiles=profiles,
                expected_profiles_sha256="b" * 64,
                expected_probes=expected,
            )

        self.assertTrue(valid(gate))
        attacks = []
        wrong_identity = json.loads(json.dumps(gate))
        wrong_identity["results"][0]["name"] = "decision:wrong"
        attacks.append(wrong_identity)
        wrong_usage = json.loads(json.dumps(gate))
        wrong_usage["results"][0]["usage"]["reasoning_tokens"] = 3
        attacks.append(wrong_usage)
        wrong_profile = json.loads(json.dumps(gate))
        wrong_profile["results"][0]["request_profile"]["profile"] = "thinking_disabled"
        attacks.append(wrong_profile)
        hidden_retry = json.loads(json.dumps(gate))
        hidden_retry["results"][0]["attempts"] = 2
        attacks.append(hidden_retry)
        wrong_protocol = json.loads(json.dumps(gate))
        wrong_protocol["retry_max_tokens"] = 64
        attacks.append(wrong_protocol)
        for attack in attacks:
            with self.subTest(attack=attacks.index(attack)):
                self.assertFalse(valid(attack))

    def test_formal_cache_policy_requires_exact_run_scoped_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp).resolve()
            report = run_dir / "c1_agent_s" / "m" / "seed0" / "gui-agent.json"
            index = {
                "c1_cache_policy": {
                    "root": str(run_dir / "c1-artifact-cache"),
                    "golden_and_reference": "run_scoped",
                    "visual_judge": "disabled",
                    "self_synthesis": str(run_dir / "c1-self-synthesis-cache"),
                },
                "arms": {"c1_agent_s": [{"model": "M", "seed": 0, "report": str(report)}]},
            }
            self.assertEqual(
                p5_aggregate._formal_cache_policy_issues(index, ROOT), []
            )
            index["c1_cache_policy"]["visual_judge"] = "shared"
            index["c1_cache_policy"]["self_synthesis"] = "/tmp/global-cache"
            issues = "\n".join(
                p5_aggregate._formal_cache_policy_issues(index, ROOT)
            )
            self.assertIn("visual-judge cache policy must be disabled", issues)
            self.assertIn("absolute run-scoped c1-self-synthesis-cache", issues)

    def test_calibration_gate_binds_provider_models_and_exact_frames(self) -> None:
        provider = {"name": "relay", "base_url": "https://example.invalid/v1"}
        gate = json.loads(json.dumps(self.CALIBRATION_GATE))
        gate["api_provider"] = provider
        self.assertTrue(
            p5_aggregate._valid_calibration_gate(
                gate,
                expected_headless=False,
                expected_api_provider=provider,
                expected_observer_model="observer",
                expected_grounder_model="grounder",
            )
        )
        bad_text_proof = json.loads(json.dumps(gate))
        bad_text_proof["backends"]["luanti"]["foreground_isolation"][
            "system_pointer_unchanged"
        ] = False
        self.assertFalse(
            p5_aggregate._valid_calibration_gate(
                bad_text_proof,
                expected_headless=False,
                expected_api_provider=provider,
                expected_observer_model="observer",
                expected_grounder_model="grounder",
            )
        )
        gate["backends"]["2048"]["frame"][0] = 0
        self.assertFalse(
            p5_aggregate._valid_calibration_gate(
                gate,
                expected_headless=False,
                expected_api_provider=provider,
                expected_observer_model="observer",
                expected_grounder_model="grounder",
            )
        )

    def test_expected_task_set_is_recomputed_from_registered_source(self) -> None:
        manifest = p5_scale.load_manifest(ROOT / "benchmarks/p5/manifest.yaml")
        full, error = p5_aggregate._expected_c1_task_set_provenance(
            {}, manifest, ROOT
        )
        self.assertIsNone(error)
        self.assertIsNotNone(full)
        assert full is not None
        self.assertEqual(len(full["task_ids"]), manifest["c1_task_count"])
        self.assertEqual(
            Path(full["source_task_file"]),
            (ROOT / "benchmarks/p5/c1_tasks.yaml").resolve(),
        )
        self.assertEqual(len(full["source_task_file_sha256"]), 64)
        selected, error = p5_aggregate._expected_c1_task_set_provenance(
            {"task_ids": full["task_ids"][:2]}, manifest, ROOT
        )
        self.assertIsNone(error)
        assert selected is not None
        self.assertEqual(selected["task_ids"], full["task_ids"][:2])
        self.assertNotEqual(selected["task_set_hash"], full["task_set_hash"])

    def test_from_start_stage2_requires_reached_start(self) -> None:
        state = [{"synthesis_model": "M1", "results": [{"task_id": "t1", "result": True}]}]
        gui = [{
            "experiment": "gui_agent_baseline",
            "model": "M1",
            "start_mode": "from_start",
            "results": [{
                "task_id": "t1",
                "result": False,
                "start_mode": "from_start",
                "before_ok": False,
                "stage2_pass": True,
                "e2e_pass": False,
            }],
        }]
        c1 = p5_aggregate.c1_from_reports(
            state,
            gui,
            subset_ids=["t1"],
            rng=random.Random(1),
            iters=20,
            ci=0.95,
        )

        same_start = c1["same_start"]
        self.assertEqual(same_start["per_arm"]["gui_client_from_start"]["e2e_success"], 0)
        self.assertTrue(same_start["state_managed_excluded"])
        self.assertEqual(same_start["comparisons"], [])

    def test_formal_c1_preserves_task_seed_trials_and_required_comparisons(self) -> None:
        reports = self._formal_c1_reports()
        c1 = p5_aggregate.c1_from_reports(
            reports["c1_state"],
            reports.get("c1_gui", []),
            gui_synth_reports=reports.get("c1_gui_synth", []),
            agent_s_reports=reports["c1_agent_s"],
            agent_s_synth_reports=reports["c1_agent_s_synth"],
            subset_ids=["t1", "t2"],
            rng=random.Random(1),
            iters=100,
            ci=0.95,
            expected_models=["M1"],
            expected_seeds=3,
            expected_arms=list(p5_scale.C1_FORMAL_PAPER_ARMS),
            calibration_gate=self.CALIBRATION_GATE,
        )
        self.assertTrue(c1["complete"], c1["incomplete_reasons"])
        self.assertEqual(c1["coverage"], {
            "expected_trials": 18,
            "observed_trials": 18,
            "missing_trials": 0,
            "unexpected_trials": 0,
        })
        self.assertEqual(len(c1["trials"]), 18)
        comparisons = c1["same_start"]["comparisons"]
        self.assertEqual({row["comparison"] for row in comparisons}, {"C_vs_D"})
        self.assertTrue(all(row["shared_trials"] == 6 for row in comparisons))
        self.assertTrue(all(row["shared_tasks"] == 2 for row in comparisons))
        for row in comparisons:
            for metric in ("start", "e2e"):
                self.assertIn("holm_p_value", row[f"{metric}_mcnemar"])
                self.assertEqual(row[f"{metric}_effect_b_minus_a"]["clusters"], 2)
            self.assertEqual(
                row["goal_given_start_effect_b_minus_a"]["clusters"], 2
            )
            self.assertIn(
                "holm_p_value", row["goal_given_both_start_mcnemar"]
            )
            self.assertEqual(
                row["time_effect_method"],
                "task_horizon_administrative_restricted_mean",
            )
            self.assertEqual(
                row["time_effect_b_minus_a_ms"]["tau_by_horizon"]["short"],
                p5_aggregate._DEFAULT_C1_ADMINISTRATIVE_RMST_TAU_MS["short"][
                    "e2e_excluding_synthesis"
                ],
            )
            self.assertEqual(
                set(row["token_effects_b_minus_a"]),
                {
                    "total",
                    "agent",
                    "evaluation",
                    "reasoning_observed",
                    "reasoning_lower_bound",
                    "reasoning_upper_bound",
                    "cached_prompt",
                },
            )
        checkpoint = next(row for row in comparisons if row["comparison"] == "C_vs_D")
        self.assertEqual(checkpoint["both_started_trials"], 6)
        self.assertEqual(checkpoint["goal_given_start_effect_b_minus_a"]["mean_diff"], 0.5)
        self.assertTrue(c1["same_start"]["state_managed_excluded"])
        accounting = c1["cost_accounting"]
        self.assertEqual(
            accounting["actual_provider_incurred"]["total_tokens"], 150
        )
        self.assertEqual(
            accounting["counterfactual_attributed_total"]["total_tokens"], 150
        )
        self.assertIn("counterfactual", accounting["primary_arm_cost_basis"])

        markdown = p5_aggregate.render_markdown({
            "provenance": {"stats": {}},
            "c1_cost_accuracy": c1,
            "c2_transfer": {},
            "c3_robustness": {},
        })
        for label in (
            "Time to start ms",
            "Start to goal ms",
            "E2E excl synth ms",
            "E2E incl synth ms",
            "Mean agent tokens",
            "Mean evaluation tokens",
            "Mean reasoning tokens",
            "Mean cached prompt tokens",
            "Primary checkpoint comparisons",
            "Provider-attempt and synthesis cost accounting diagnostic",
            "Actual provider-incurred synthesis",
            "comparison burden, not spend",
            "Holm=",
        ):
            self.assertIn(label, markdown)

    def test_capability_sensitivity_is_multilabel_and_task_clustered(self) -> None:
        index = {
            ("t1", 0): {
                "capabilities": ["numeric-reasoning", "spatial-reasoning"],
                "benchmark": "2048",
                "horizon_class": "short",
                "start_success": True,
                "goal_success": True,
                "e2e_success": True,
            },
            ("t1", 1): {
                "capabilities": ["numeric-reasoning", "spatial-reasoning"],
                "benchmark": "2048",
                "horizon_class": "short",
                "start_success": True,
                "goal_success": False,
                "e2e_success": False,
            },
            ("t2", 0): {
                "capabilities": ["numeric-reasoning"],
                "benchmark": "datingsim",
                "horizon_class": "mid",
                "start_success": False,
                "goal_success": False,
                "e2e_success": False,
            },
            ("t2", 1): {
                "capabilities": ["numeric-reasoning"],
                "benchmark": "datingsim",
                "horizon_class": "mid",
                "start_success": True,
                "goal_success": True,
                "e2e_success": True,
            },
        }
        sensitivity = p5_aggregate._c1_sensitivity(
            {
                "c1_state": {"M1": index},
                "c1_agent_s": {},
                "c1_agent_s_synth": {},
            },
            ["t1", "t2"],
            random.Random(7),
            iters=20,
            ci=0.95,
        )
        rows = {
            row["capability"]: row
            for row in sensitivity["by_capability"]
        }
        self.assertEqual(rows["numeric-reasoning"]["tasks"], 2)
        self.assertEqual(rows["spatial-reasoning"]["tasks"], 1)
        self.assertEqual(rows["numeric-reasoning"]["trials"], 4)
        self.assertEqual(rows["spatial-reasoning"]["trials"], 2)
        for row in rows.values():
            for metric in (
                "start_success",
                "goal_success_given_start",
                "e2e_success",
            ):
                self.assertEqual(row[metric]["clusters"], row["tasks"])
        self.assertEqual(sensitivity["capability_membership"], "non_exclusive")
        self.assertEqual(sensitivity["capability_cluster_unit"], "task_id")

    def test_full_formal_gate_requires_registered_trial_universe(self) -> None:
        reports, models, task_ids = self._full_formal_c1_reports()
        task_rows = yaml.safe_load(
            (ROOT / "benchmarks/p5/c1_tasks.yaml").read_text()
        )["tasks"]
        manifest = p5_scale.load_manifest(ROOT / "benchmarks/p5/manifest.yaml")
        c1 = p5_aggregate.c1_from_reports(
            reports["c1_state"], reports.get("c1_gui", []),
            gui_synth_reports=reports.get("c1_gui_synth", []),
            agent_s_reports=reports["c1_agent_s"],
            agent_s_synth_reports=reports["c1_agent_s_synth"],
            subset_ids=task_ids,
            rng=random.Random(1),
            iters=5,
            ci=0.95,
            expected_models=models,
            expected_seeds=3,
            expected_arms=list(p5_scale.C1_FORMAL_PAPER_ARMS),
            calibration_gate=self.CALIBRATION_GATE,
            formal=True,
            expected_headless=False,
            expected_task_metadata={
                task["id"]: {
                    "benchmark": task["benchmark"],
                    "horizon_class": task["horizon_class"],
                    "capabilities": task["capabilities"],
                    "prompt_revision": task["prompt_revision"],
                    "random_control": copy.deepcopy(task["random_control"]),
                }
                for task in task_rows
            },
            expected_game_counts=manifest["c1_benchmark_task_counts"],
            administrative_rmst_tau_ms=manifest["stats"][
                "c1_administrative_rmst_tau_ms"
            ],
            expected_observer_model="observer",
            expected_grounder_model="grounder",
            expected_judge_model="judge",
        )
        self.assertTrue(c1["complete"], c1["incomplete_reasons"])
        formal_trials = (
            manifest["c1_task_count"]
            * len(p5_scale.C1_FORMAL_PAPER_ARMS)
            * len(models)
            * manifest["seeds"]
        )
        per_model_trials = manifest["c1_task_count"] * manifest["seeds"]
        self.assertEqual(c1["coverage"]["expected_trials"], formal_trials)
        self.assertEqual(c1["coverage"]["observed_trials"], formal_trials)
        self.assertEqual(
            c1["coverage"]["formal_game_counts"],
            manifest["c1_benchmark_task_counts"],
        )
        gui_points = [point for point in c1["points"] if point["arm"] != "c1_state"]
        self.assertTrue(
            all(
                point["start_success"]["clusters"] == manifest["c1_task_count"]
                for point in gui_points
            )
        )
        self.assertTrue(
            all(
                point["goal_success_given_start"]["n"] == per_model_trials
                for point in gui_points
            )
        )
        self.assertTrue(
            all(
                point["rmst_e2e_ms"]["events"] == per_model_trials
                for point in gui_points
            )
        )
        detail = gui_points[0]
        self.assertEqual(detail["mean_agent_tokens"]["mean"], 70)
        self.assertEqual(detail["mean_evaluation_tokens"]["mean"], 30)
        self.assertEqual(
            detail["cost_by_kind"]["visual_observer"]["calls"], per_model_trials
        )
        self.assertEqual(
            detail["cost_by_model"]["observer"]["total_tokens"],
            15 * per_model_trials,
        )
        self.assertEqual(
            detail["cost_by_kind_model"]["visual_judge/judge"]["usage_complete"],
            True,
        )
        self.assertEqual(
            len(c1["sensitivity"]["checkpoint_effects"]), len(models)
        )
        for effect in c1["sensitivity"]["checkpoint_effects"]:
            for strata in effect["dimensions"].values():
                for stratum in strata.values():
                    self.assertEqual(
                        stratum["time_effect_method"],
                        "task_horizon_administrative_restricted_mean",
                    )
                    self.assertEqual(
                        set(stratum[
                            "rmst_time_e2e_excluding_synthesis_effect_b_minus_a_ms"
                        ]["tau_by_horizon"]),
                        {"short", "mid", "long"},
                    )
        self.assertEqual(
            len(c1["sensitivity"]["state_managed_by_model"]), len(models)
        )
        by_capability = c1["sensitivity"]["by_capability"]
        self.assertEqual(
            len(by_capability),
            len(p5_scale.C1_FORMAL_PAPER_ARMS) * len(models) * 7,
        )
        expected_capability_counts = Counter(
            capability
            for task in yaml.safe_load(
                (ROOT / "benchmarks/p5/c1_tasks.yaml").read_text()
            )["tasks"]
            for capability in task["capabilities"]
        )
        for row in by_capability:
            self.assertEqual(
                row["tasks"], expected_capability_counts[row["capability"]]
            )
            for metric in (
                "start_success",
                "goal_success_given_start",
                "e2e_success",
            ):
                self.assertEqual(row[metric]["clusters"], row["tasks"])
        self.assertEqual(
            c1["sensitivity"]["capability_membership"], "non_exclusive"
        )
        self.assertEqual(
            c1["sensitivity"]["capability_cluster_unit"], "task_id"
        )
        self.assertFalse(c1["sensitivity"]["state_managed_pooled_with_gui"])
        self.assertFalse(c1["pareto"]["cross_semantic_dominance_computed"])

    def test_formal_gate_accepts_a_consistent_public_semantic_failure(self) -> None:
        reports, models, task_ids = self._full_formal_c1_reports()
        row = next(
            item
            for report in reports["c1_agent_s"]
            for item in report["results"]
            if item["benchmark"] == "datingsim"
        )
        row.update({
            "result": False,
            "after_semantic_ok": False,
            "goal_success": False,
            "e2e_success": False,
        })
        row["after_semantic_detail"] = json.loads(
            json.dumps(row["after_semantic_detail"])
        )
        row["after_semantic_detail"]["checks"][0]["ok"] = False

        c1 = self._run_full_formal_fixture(reports, models, task_ids)
        self.assertTrue(c1["complete"], c1["incomplete_reasons"])
        self.assertEqual(
            c1["coverage"]["observed_trials"],
            len(task_ids) * len(models) * 3 * len(p5_scale.C1_FORMAL_PAPER_ARMS),
        )

    def test_formal_gate_accepts_real_synthesized_semantic_detail_shape(self) -> None:
        reports, models, task_ids = self._full_formal_c1_reports()
        for arm in ("c1_agent_s_synth",):
            for benchmark in ("datingsim", "spacerpg"):
                row = next(
                    item
                    for report in reports[arm]
                    for item in report["results"]
                    if item["benchmark"] == benchmark
                )
                row["semantic_detail"] = row.pop("after_semantic_detail")

        c1 = self._run_full_formal_fixture(reports, models, task_ids)
        self.assertTrue(c1["complete"], c1["incomplete_reasons"])

    def test_formal_gate_rejects_inconsistent_public_semantic_evidence(self) -> None:
        def reasons_after(mutate) -> str:
            reports, models, task_ids = self._full_formal_c1_reports()
            mutate(reports)
            result = self._run_full_formal_fixture(reports, models, task_ids)
            self.assertFalse(result["complete"])
            return "\n".join(result["incomplete_reasons"])

        def contradict_check(reports: dict[str, list[dict]]) -> None:
            row = next(
                item
                for report in reports["c1_agent_s"]
                for item in report["results"]
                if item["benchmark"] == "datingsim"
            )
            row["after_semantic_detail"]["checks"][0]["ok"] = False

        self.assertIn("does not match all", reasons_after(contradict_check))

        def drop_attestation_evidence(reports: dict[str, list[dict]]) -> None:
            row = next(
                item
                for report in reports["c1_agent_s"]
                for item in report["results"]
                if item["benchmark"] == "spacerpg"
            )
            row["policy_runtime_attestation"].pop("private_hook_present")

        self.assertIn(
            "policy_runtime_attestation.private_hook_present must be False",
            reasons_after(drop_attestation_evidence),
        )

    def test_desktop_native_evidence_and_sigstop_pid_fail_closed(self) -> None:
        reports, _, _ = self._full_formal_c1_reports()
        report = next(
            report
            for report in reports["c1_agent_s"]
            if any(row["benchmark"] == "blender" for row in report["results"])
        )
        row = next(
            row for row in report["results"] if row["benchmark"] == "blender"
        )
        trial, issues = p5_aggregate._normalize_c1_trial(
            "c1_agent_s", report, row
        )
        self.assertEqual(issues, [])
        self.assertEqual(
            p5_aggregate._c1_native_desktop_semantic_trial_issues(trial), []
        )

        bad_semantic = copy.deepcopy(trial)
        bad_semantic["before_semantic_detail"]["live"] = False
        semantic_issues = p5_aggregate._c1_native_desktop_semantic_trial_issues(
            bad_semantic
        )
        self.assertTrue(any("not a live semantic probe" in issue for issue in semantic_issues))

        bad_seed = copy.deepcopy(row)
        bad_seed["seed_evidence"]["final"]["semantic_fingerprint"] = "0" * 64
        _, seed_issues = p5_aggregate._normalize_c1_trial(
            "c1_agent_s", report, bad_seed
        )
        self.assertTrue(
            any("fingerprint does not match observation" in issue for issue in seed_issues)
        )

        pid = trial["seed_evidence"]["initial"]["process_id"]
        trial.update(
            start_construction_status="not_applicable",
            start_grading_started=True,
            start_gate_status="passed",
            goal_evaluation_status="evaluated",
            before_visual_status="evaluated",
            after_visual_status="evaluated",
            before_semantic_status="success",
            after_semantic_status="success",
            visual_grading_started=True,
            start_grading_runtime_hold={
                "required": True,
                "held": True,
                "released": True,
                "backend": "sigstop",
                "held_pid": pid,
            },
        )
        self.assertEqual(p5_aggregate._c1_formal_start_gate_issues(trial), [])
        trial["start_grading_runtime_hold"]["held_pid"] = pid + 1
        hold_issues = p5_aggregate._c1_formal_start_gate_issues(trial)
        self.assertTrue(any("held_pid does not match" in issue for issue in hold_issues))

        libreoffice_report = next(
            report
            for report in reports["c1_agent_s"]
            if any(
                candidate["benchmark"] == "libreoffice"
                for candidate in report["results"]
            )
        )
        libreoffice_row = next(
            candidate
            for candidate in libreoffice_report["results"]
            if candidate["benchmark"] == "libreoffice"
        )
        libreoffice_trial, normalize_issues = p5_aggregate._normalize_c1_trial(
            "c1_agent_s", libreoffice_report, libreoffice_row
        )
        self.assertEqual(normalize_issues, [])
        libreoffice_trial["goal_evaluation_status"] = "evaluated"
        self.assertEqual(
            p5_aggregate._c1_native_desktop_semantic_trial_issues(
                libreoffice_trial
            ),
            [],
        )

        missing_reopen = copy.deepcopy(libreoffice_trial)
        missing_reopen["after_semantic_detail"]["checks"] = [
            check
            for check in missing_reopen["after_semantic_detail"]["checks"]
            if check["name"] != "transition.document_reopened"
        ]
        reopen_issues = p5_aggregate._c1_native_desktop_semantic_trial_issues(
            missing_reopen
        )
        self.assertTrue(
            any("lacks save/reopen checks" in issue for issue in reopen_issues)
        )

        unchanged_identity = copy.deepcopy(libreoffice_trial)
        unchanged_identity["after_semantic_detail"]["document_identity"] = (
            unchanged_identity["before_semantic_detail"]["document_identity"]
        )
        identity_issues = p5_aggregate._c1_native_desktop_semantic_trial_issues(
            unchanged_identity
        )
        self.assertTrue(
            any(
                "document_reopened is inconsistent" in issue
                or "lacks persisted save/reopen proof" in issue
                for issue in identity_issues
            )
        )

        for benchmark in ("blender", "libreoffice"):
            with self.subTest(state_native_seed=benchmark):
                state_report = next(
                    candidate
                    for candidate in reports["c1_state"]
                    if any(
                        row["benchmark"] == benchmark
                        for row in candidate["results"]
                    )
                )
                state_row = next(
                    candidate
                    for candidate in state_report["results"]
                    if candidate["benchmark"] == benchmark
                )
                state_trial, state_issues = p5_aggregate._normalize_c1_trial(
                    "c1_state", state_report, state_row
                )
                self.assertEqual(state_issues, [])
                self.assertIs(state_trial["runtime_started"], True)
                self.assertEqual(
                    state_trial["prng"],
                    state_trial["seed_evidence"]["final"],
                )

                wrong_pid = copy.deepcopy(state_row)
                wrong_pid["seed_evidence"]["final"]["process_id"] += 1
                _, wrong_pid_issues = p5_aggregate._normalize_c1_trial(
                    "c1_state", state_report, wrong_pid
                )
                self.assertTrue(
                    any(
                        "process_id differs" in issue
                        for issue in wrong_pid_issues
                    )
                )

                missing = copy.deepcopy(state_row)
                missing.pop("seed_evidence")
                _, missing_issues = p5_aggregate._normalize_c1_trial(
                    "c1_state", state_report, missing
                )
                self.assertTrue(
                    any("missing seed_evidence" in issue for issue in missing_issues)
                )

    def test_state_normalization_keeps_start_goal_and_e2e_independent(self) -> None:
        reports, _, _ = self._full_formal_c1_reports()
        report = copy.deepcopy(reports["c1_state"][0])
        row = report["results"][0]
        report["state_stage_metrics"] = {
            "schema_version": 1,
            "start_gate_boundary": "before_registered_transition",
            "start_success": "before_semantic_and_before_visual",
            "goal_success": "after_semantic_and_after_visual",
            "e2e_success": "start_success_and_goal_success",
        }
        row.update(
            state_stage_metrics_schema_version=1,
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
            after_visual_ok=False,
            start_success=True,
            goal_success=False,
            e2e_success=False,
            result=False,
            pair_verdict={
                "result": False,
                "before": {"label": "correct"},
                "after": {"label": "incorrect"},
            },
        )

        trial, issues = p5_aggregate._normalize_c1_trial(
            "c1_state", report, row
        )

        self.assertFalse(
            any("State stage" in issue for issue in issues), issues
        )
        self.assertIs(trial["start_success"], True)
        self.assertIs(trial["goal_success"], False)
        self.assertIs(trial["e2e_success"], False)

    def test_cost_gate_is_stage_aware_for_fair_pre_module_failures(self) -> None:
        reports, _, _ = self._full_formal_c1_reports()

        # A synthesized GUI arm may start the runtime and then fail its own
        # restore. No policy/observer/judge call was attempted, so the only legal
        # nonzero ledger is the checkpoint burden.
        gui_report = reports["c1_agent_s_synth"][0]
        gui_row = gui_report["results"][0]
        gui_row.update({
            "result": False,
            "before_visual_ok": False,
            "before_semantic_ok": False,
            "after_visual_ok": False,
            "after_semantic_ok": False,
            "start_success": False,
            "goal_success": False,
            "e2e_success": False,
            "time_to_start_ms": 10,
            "time_start_to_goal_ms": 0,
            "time_e2e_excluding_synthesis_ms": 10,
            "time_e2e_including_synthesis_ms": 15,
            "termination_time_ms": 15,
            "steps": {"phase_a": 0, "phase_b": 0, "total": 0},
            "budget_exhausted": False,
            "policy_phase_started": False,
            "visual_grading_started": False,
            "cost": json.loads(json.dumps(gui_row["synthesis_attributed_cost"])),
        })
        _stamp_single_attempt_ledger(
            gui_row,
            task_actual_cost=cost.CostMeter().summary(),
            is_state=False,
        )
        gui_trial, gui_issues = p5_aggregate._normalize_c1_trial(
            "c1_agent_s_synth", gui_report, gui_row
        )
        self.assertEqual(gui_issues, [])
        self.assertEqual(
            p5_aggregate._c1_cost_module_issues(
                gui_trial,
                expected_observer_model="observer",
                expected_grounder_model="grounder",
                expected_judge_model="judge",
            ),
            [],
        )

        # E can likewise fail after synthesis but before visual grading. It must
        # retain checkpoint spend without inventing a visual-judge bucket.
        state_report = reports["c1_state"][0]
        state_row = state_report["results"][0]
        state_row.update({
            "result": False,
            "visual_grading_started": False,
            "cost": json.loads(json.dumps(gui_row["synthesis_attributed_cost"])),
        })
        _stamp_single_attempt_ledger(
            state_row,
            task_actual_cost=state_row["cost"],
            is_state=True,
        )
        state_trial, state_issues = p5_aggregate._normalize_c1_trial(
            "c1_state", state_report, state_row
        )
        self.assertEqual(state_issues, [])
        self.assertEqual(
            p5_aggregate._c1_cost_module_issues(
                state_trial,
                expected_observer_model="observer",
                expected_grounder_model="grounder",
                expected_judge_model="judge",
            ),
            [],
        )

        # Once the grader boundary is attested, absence of its cost bucket is a
        # hard error; the explicit flag cannot be used to waive a real module.
        state_trial["visual_grading_started"] = True
        self.assertTrue(
            any(
                "lacks visual_judge module" in issue
                for issue in p5_aggregate._c1_cost_module_issues(
                    state_trial,
                    expected_observer_model="observer",
                    expected_grounder_model="grounder",
                    expected_judge_model="judge",
                )
            )
        )

    def test_state_cost_gate_accepts_deterministic_or_shared_pair_visual_evidence(
        self,
    ) -> None:
        base = {
            "arm": "c1_state",
            "model": "model",
            "task_id": "task",
            "seed": 0,
            "runtime_started": True,
            "visual_grading_started": True,
            "llm_calls": 1,
            "by_kind": {
                "checkpoint_synthesis": {"calls": 1},
            },
            "by_kind_model": {
                "checkpoint_synthesis": {"model": {"calls": 1}}
            },
        }
        deterministic = {
            **base,
            "pair_verdict": {
                "before": {
                    "label": "correct",
                    "verdict_source": "normalized_image_equality_v1",
                }
            },
        }
        issues = p5_aggregate._c1_cost_module_issues(
            deterministic,
            expected_observer_model="observer",
            expected_grounder_model="grounder",
            expected_judge_model="judge",
        )
        self.assertFalse(any("lacks visual_judge" in issue for issue in issues))

        shared = {
            **base,
            "pair_start_gate_cache_hit": True,
            "pair_verdict": {"before": {"label": "correct"}},
        }
        issues = p5_aggregate._c1_cost_module_issues(
            shared,
            expected_observer_model="observer",
            expected_grounder_model="grounder",
            expected_judge_model="judge",
        )
        self.assertFalse(any("lacks visual_judge" in issue for issue in issues))

    def test_paid_infrastructure_retry_is_actual_overhead_not_arm_burden(self) -> None:
        reports, _, _ = self._full_formal_c1_reports()
        # D (c1_agent_s_synth) is the arm that actually incurs the shared
        # synthesis burden, so leave its clean trial untouched.
        d_report = reports["c1_agent_s_synth"][0]
        d_row = d_report["results"][0]
        # B (c1_gui_synth) shares the same counterfactual burden via cache, so
        # build a fully independent trial from D's data. It hit the shared cache
        # (zero core synthesis spend) but paid one transient infrastructure
        # retry before succeeding.
        b_report = copy.deepcopy(d_report)
        b_row = b_report["results"][0]
        b_row["synthesis_cache_hit"] = True
        b_row["synthesis_actual_incurred_this_attempt"] = False
        transient = json.loads(json.dumps(b_row["synthesis_attributed_cost"]))

        # B encountered one paid provider failure before its successful shared
        # synthesis. The main arm cost remains one attributed burden; actual spend
        # transparently includes the extra infrastructure attempt.
        prior = cost.CostMeter().summary()
        prior.update(
            {
                "attempt_status": "retryable_infrastructure",
                "retry_category": "infra",
                "retry_reason": "provider synthesis outage",
                "task_actual_cost": cost.CostMeter().summary(),
                "synthesis_accounting": {
                    "actual_cost": transient,
                    "transient_actual_cost": transient,
                    "attributed_cost": cost.CostMeter().summary(),
                },
            }
        )
        # The successful attempt is a pure cache hit: it keeps the attributed
        # counterfactual burden but incurs no core synthesis spend of its own.
        treatment = copy.deepcopy(b_row["attempt_costs"][-1])
        treatment["synthesis_accounting"]["actual_cost"] = (
            cost.CostMeter().summary()
        )
        treatment["synthesis_accounting"]["transient_actual_cost"] = (
            cost.CostMeter().summary()
        )
        _recompute_attempt_ledger(
            b_row,
            [prior, treatment],
            is_state=False,
        )
        b_trial, b_issues = p5_aggregate._normalize_c1_trial(
            "c1_gui_synth", b_report, b_row
        )
        d_trial, d_issues = p5_aggregate._normalize_c1_trial(
            "c1_agent_s_synth", d_report, d_row
        )
        self.assertEqual(b_issues, [])
        self.assertEqual(d_issues, [])
        indices = {
            "c1_gui_synth": {"M1": {(b_trial["task_id"], 0): b_trial}},
            "c1_agent_s_synth": {"M1": {(d_trial["task_id"], 0): d_trial}},
        }
        self.assertEqual(
            p5_aggregate._synthesis_accounting_issues(
                indices, [b_trial["task_id"]]
            ),
            [],
        )

    def test_attempt_cost_gate_recomputes_hash_chain_and_three_cost_views(self) -> None:
        reports, _, _ = self._full_formal_c1_reports()
        report = reports["c1_agent_s"][0]
        row = report["results"][0]
        treatment = copy.deepcopy(row["attempt_costs"][-1])
        prior_task = copy.deepcopy(treatment["task_actual_cost"])
        prior = cost.merge_summaries(
            [prior_task, treatment["synthesis_accounting"]["attributed_cost"]]
        )
        prior.update(
            {
                "attempt_status": "retryable_infrastructure",
                "retry_category": "infra",
                "retry_reason": "HTTP 503 service unavailable",
                "task_actual_cost": prior_task,
                "synthesis_accounting": copy.deepcopy(
                    treatment["synthesis_accounting"]
                ),
            }
        )
        # A failed runtime attempt must not claim another counterfactual burden.
        prior["synthesis_accounting"]["attributed_cost"] = (
            cost.CostMeter().summary()
        )
        prior_cost = cost.merge_summaries(
            [prior_task, prior["synthesis_accounting"]["attributed_cost"]]
        )
        prior.update(p5_aggregate._cost_ledger_view(prior_cost) or {})
        _recompute_attempt_ledger(
            row,
            [prior, treatment],
            is_state=False,
        )
        trial, issues = p5_aggregate._normalize_c1_trial("c1_agent_s", report, row)
        self.assertEqual(issues, [])
        self.assertEqual(trial["attempt_count"], 2)
        self.assertEqual(
            trial["total_tokens"], treatment["task_actual_cost"]["total_tokens"]
        )
        self.assertEqual(
            trial["actual_cost"]["total_tokens"],
            2 * treatment["task_actual_cost"]["total_tokens"],
        )
        self.assertEqual(
            trial["transient_infrastructure_cost"]["total_tokens"],
            treatment["task_actual_cost"]["total_tokens"],
        )

        def normalized_issues(mutator) -> str:
            attacked = copy.deepcopy(row)
            mutator(attacked)
            _, attacked_issues = p5_aggregate._normalize_c1_trial(
                "c1_agent_s", report, attacked
            )
            self.assertTrue(attacked_issues)
            return "\n".join(attacked_issues)

        attacks = (
            ("attempt_count", lambda value: value.__setitem__("attempt_count", 1)),
            (
                "attempt_ordinal",
                lambda value: value["attempt_costs"][0].__setitem__(
                    "attempt_ordinal", 0
                ),
            ),
            (
                "status must be retryable_infrastructure",
                lambda value: value["attempt_costs"][0].__setitem__(
                    "attempt_status", "treatment"
                ),
            ),
            (
                "retry_category",
                lambda value: value["attempt_costs"][0].__setitem__(
                    "retry_category", "model_failure"
                ),
            ),
            (
                "retry_reason",
                lambda value: value["attempt_costs"][0].__setitem__(
                    "retry_reason", ""
                ),
            ),
            (
                "previous_attempt_hash",
                lambda value: value["attempt_costs"][1].__setitem__(
                    "previous_attempt_hash", "0" * 64
                ),
            ),
            (
                "attempt_hash",
                lambda value: value["attempt_costs"][0].__setitem__(
                    "attempt_hash", "0" * 64
                ),
            ),
            (
                "attempt_ledger_hash",
                lambda value: value.__setitem__("attempt_ledger_hash", "0" * 64),
            ),
            (
                "task_actual_cost",
                lambda value: value["attempt_costs"][0]["task_actual_cost"].__setitem__(
                    "total_tokens", 1
                ),
            ),
            (
                "synthesis_attributed_cost does not match attempt synthesis ledger",
                lambda value: value["synthesis_attributed_cost"].__setitem__(
                    "total_tokens", 1
                ),
            ),
            (
                "cost does not match",
                lambda value: value["cost"].__setitem__("total_tokens", 1),
            ),
            (
                "actual_cost does not match",
                lambda value: value["actual_cost"].__setitem__("total_tokens", 1),
            ),
            (
                "transient_infrastructure_cost does not match",
                lambda value: value["transient_infrastructure_cost"].__setitem__(
                    "total_tokens", 1
                ),
            ),
        )
        for expected, mutate in attacks:
            with self.subTest(expected=expected):
                self.assertIn(expected, normalized_issues(mutate))

        state_report = reports["c1_state"][0]
        state_row = state_report["results"][0]
        state_treatment = copy.deepcopy(state_row["attempt_costs"][-1])
        state_prior = copy.deepcopy(state_treatment)
        state_prior.update(
            {
                "attempt_status": "retryable_infrastructure",
                "retry_category": "rate_limit",
                "retry_reason": "HTTP 429",
            }
        )
        _recompute_attempt_ledger(
            state_row,
            [state_prior, state_treatment],
            is_state=True,
        )
        _, state_issues = p5_aggregate._normalize_c1_trial(
            "c1_state", state_report, state_row
        )
        self.assertEqual(state_issues, [])

    def test_cost_breakdown_requires_kind_model_marginals(self) -> None:
        reports, _, _ = self._full_formal_c1_reports()
        malformed = copy.deepcopy(reports["c1_agent_s"][0]["results"][0]["cost"])
        observer = malformed["by_kind_model"]["visual_observer"].pop("observer")
        malformed["by_kind_model"]["visual_observer"]["wrong-model"] = observer
        issues = p5_aggregate._cost_summary_issues(malformed)
        self.assertTrue(any("model keys do not match" in issue for issue in issues))

    def test_formal_gate_rejects_task_phase_cost_cache_and_finite_time_attacks(self) -> None:
        def reasons_after(mutate) -> str:
            reports, models, task_ids = self._full_formal_c1_reports()
            mutate(reports)
            result = self._run_full_formal_fixture(reports, models, task_ids)
            self.assertFalse(result["complete"])
            return "\n".join(result["incomplete_reasons"])

        def relabel_horizon(reports: dict[str, list[dict]]) -> None:
            row = next(
                item
                for report in reports["c1_agent_s"]
                for item in report["results"]
                if item["horizon_class"] == "mid"
            )
            row["horizon_class"] = "short"

        self.assertIn("does not match registered value", reasons_after(relabel_horizon))

        def drop_state_metadata(reports: dict[str, list[dict]]) -> None:
            reports["c1_state"][0]["results"][0].pop("horizon_class")

        self.assertIn("horizon_class None", reasons_after(drop_state_metadata))

        def add_synth_phase_a(reports: dict[str, list[dict]]) -> None:
            steps = reports["c1_agent_s_synth"][0]["results"][0]["steps"]
            steps.update({"phase_a": 1, "phase_b": 1, "total": 2})

        self.assertIn("steps.phase_a=0", reasons_after(add_synth_phase_a))

        def exceed_trial_cost(reports: dict[str, list[dict]]) -> None:
            summary = reports["c1_agent_s_synth"][0]["results"][0][
                "synthesis_attributed_cost"
            ]
            summary["prompt_tokens"] = summary["total_tokens"] = 101
            for partition in ("by_model", "by_kind", "by_kind_model"):
                for bucket in p5_aggregate._cost_leaf_buckets(summary[partition]):
                    bucket["prompt_tokens"] = bucket["total_tokens"] = 101

        self.assertIn("exceeds trial cost", reasons_after(exceed_trial_cost))

        def zero_attributed_but_positive_actual(reports: dict[str, list[dict]]) -> None:
            row = reports["c1_agent_s_synth"][0]["results"][0]
            row["synthesis_attributed_cost"] = json.loads(
                json.dumps(row["synthesis_actual_cost"])
            )
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                row["synthesis_attributed_cost"][field] = 0
            for partition in ("by_model", "by_kind", "by_kind_model"):
                for bucket in p5_aggregate._cost_leaf_buckets(
                    row["synthesis_attributed_cost"][partition]
                ):
                    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        bucket[field] = 0
            row["synthesis_actual_incurred_this_attempt"] = True

        self.assertIn(
            "checkpoint module costs do not equal synthesis attribution",
            reasons_after(zero_attributed_but_positive_actual),
        )

        def inject_unknown_module(reports: dict[str, list[dict]]) -> None:
            row = reports["c1_agent_s"][0]["results"][0]
            prior_attempt = row["attempt_costs"][-1]
            task_cost = copy.deepcopy(prior_attempt["task_actual_cost"])
            task_cost["by_kind"]["unknown_module"] = task_cost["by_kind"].pop(
                "visual_observer"
            )
            task_cost["by_kind_model"]["unknown_module"] = task_cost[
                "by_kind_model"
            ].pop("visual_observer")
            attempt = cost.merge_summaries(
                [
                    task_cost,
                    prior_attempt["synthesis_accounting"]["attributed_cost"],
                ]
            )
            attempt.update(
                {
                    "attempt_status": "treatment",
                    "retry_category": None,
                    "retry_reason": None,
                    "task_actual_cost": task_cost,
                    "synthesis_accounting": copy.deepcopy(
                        prior_attempt["synthesis_accounting"]
                    ),
                }
            )
            _recompute_attempt_ledger(row, [attempt], is_state=False)

        self.assertIn("unregistered C1 cost kind", reasons_after(inject_unknown_module))

        def inject_infinite_time(reports: dict[str, list[dict]]) -> None:
            reports["c1_agent_s"][0]["results"][0]["termination_time_ms"] = float("inf")

        self.assertIn("missing v2 time fields", reasons_after(inject_infinite_time))

    def test_goal_given_start_main_estimand_uses_each_arms_own_starts(self) -> None:
        reports = self._formal_c1_reports()
        # A fails to start t2, while B starts and reaches it. The own-start main
        # estimand includes B/t2; the both-start sensitivity does not.
        for report in reports["c1_agent_s"]:
            report["results"][1].update({
                "before_visual_ok": False,
                "before_semantic_ok": False,
                "after_visual_ok": False,
                "after_semantic_ok": False,
                "start_success": False,
                "goal_success": False,
                "e2e_success": False,
            })
        for report in reports["c1_agent_s_synth"]:
            report["results"][1].update({
                "after_visual_ok": False,
                "after_semantic_ok": False,
                "goal_success": False,
                "e2e_success": False,
            })
        c1 = p5_aggregate.c1_from_reports(
            reports["c1_state"], reports.get("c1_gui", []),
            gui_synth_reports=reports.get("c1_gui_synth", []),
            agent_s_reports=reports["c1_agent_s"],
            agent_s_synth_reports=reports["c1_agent_s_synth"],
            subset_ids=["t1", "t2"], rng=random.Random(3), iters=50, ci=0.95,
            expected_models=["M1"], expected_seeds=3,
            expected_arms=list(p5_scale.C1_FORMAL_PAPER_ARMS),
            calibration_gate=self.CALIBRATION_GATE,
        )
        row = next(
            item for item in c1["same_start"]["comparisons"]
            if item["comparison"] == "C_vs_D"
        )
        self.assertNotEqual(
            row["goal_given_start_effect_b_minus_a"]["mean_diff"],
            row["goal_given_both_start_effect_b_minus_a"]["mean_diff"],
        )

    def test_formal_gate_rejects_wrong_shape_budget_and_seed_evidence(self) -> None:
        reports = self._formal_c1_reports()
        manifest = p5_scale.load_manifest(ROOT / "benchmarks/p5/manifest.yaml")
        reports["c1_agent_s"][0]["results"][0]["decision_budget"] = 19
        reports["c1_agent_s"][0]["results"][0]["seed_evidence"]["initial"]["calls"] = 99
        c1 = p5_aggregate.c1_from_reports(
            reports["c1_state"], reports.get("c1_gui", []),
            gui_synth_reports=reports.get("c1_gui_synth", []),
            agent_s_reports=reports["c1_agent_s"],
            agent_s_synth_reports=reports["c1_agent_s_synth"],
            subset_ids=["t1", "t2"], rng=random.Random(1), iters=5, ci=0.95,
            expected_models=["M1"], expected_seeds=3,
            expected_arms=list(p5_scale.C1_FORMAL_PAPER_ARMS),
            calibration_gate=self.CALIBRATION_GATE,
            formal=True,
            expected_headless=False,
            expected_game_counts=manifest["c1_benchmark_task_counts"],
        )
        self.assertFalse(c1["complete"])
        reasons = "\n".join(c1["incomplete_reasons"])
        self.assertIn(
            f"exactly {manifest['c1_task_count']} unique tasks", reasons
        )
        self.assertIn("decision_budget 19", reasons)
        self.assertIn("browser final PRNG call counter precedes initial counter", reasons)

    def test_pareto_computes_dominance_and_keeps_state_semantics_separate(self) -> None:
        def point(arm: str, model: str, success: float, tokens: float, rmst: float) -> dict:
            return {
                "arm": arm,
                "method": arm,
                "model": model,
                "e2e_success": {"mean": success},
                "mean_tokens": {"mean": tokens},
                "rmst_e2e_ms": {"rmst": rmst},
            }

        summary = p5_aggregate._pareto_summary([
            point("c1_agent_s", "best", 1.0, 10, 10),
            point("c1_agent_s_synth", "dominated", 0.5, 20, 20),
            point("c1_agent_s", "tradeoff", 1.0, 5, 30),
            point("c1_state", "semantic", 1.0, 1, 1),
        ])
        gui = {row["model"]: row for row in summary["gui"]["points"]}
        self.assertTrue(gui["dominated"]["dominated_by"])
        self.assertFalse(gui["dominated"]["is_frontier"])
        self.assertTrue(gui["best"]["is_frontier"])
        self.assertTrue(gui["tradeoff"]["is_frontier"])
        self.assertEqual(summary["state_managed"]["frontier_ids"], ["c1_state:semantic"])
        self.assertFalse(summary["cross_semantic_dominance_computed"])

    def test_method_rmst_censors_early_failure_at_common_tau(self) -> None:
        def row(success: bool, termination: int) -> dict:
            return {
                "start_success": success,
                "goal_success": success,
                "e2e_success": success,
                "termination_time_ms": termination,
                "time_e2e_excluding_synthesis_ms": termination,
                "time_e2e_including_synthesis_ms": termination,
                "time_to_start_ms": termination,
                "time_start_to_goal_ms": termination,
                "total_tokens": 1,
                "reasoning_tokens": 0,
                "cached_prompt_tokens": 0,
                "by_kind": {},
                "by_model": {},
                "by_kind_model": {},
            }

        point = p5_aggregate._method_point(
            {("success", 0): row(True, 5), ("early-failure", 0): row(False, 1)},
            ["success", "early-failure"],
            random.Random(1),
            iters=10,
            ci=0.95,
            rmst_tau=10,
        )
        self.assertEqual(point["rmst_e2e_ms"]["events"], 1)
        self.assertEqual(point["rmst_e2e_ms"]["tau"], 10)
        self.assertEqual(point["rmst_e2e_ms"]["rmst"], 7.5)

    def test_early_all_failure_group_does_not_collapse_common_tau(self) -> None:
        reports = self._formal_c1_reports()
        for report in reports["c1_agent_s"]:
            for row in report["results"]:
                row.update(
                    {
                        "after_visual_ok": False,
                        "after_semantic_ok": False,
                        "goal_success": False,
                        "e2e_success": False,
                        "time_e2e_excluding_synthesis_ms": 1,
                        "time_e2e_including_synthesis_ms": 1,
                        "termination_time_ms": 1,
                    }
                )
        c1 = p5_aggregate.c1_from_reports(
            reports["c1_state"],
            reports.get("c1_gui", []),
            gui_synth_reports=reports.get("c1_gui_synth", []),
            agent_s_reports=reports["c1_agent_s"],
            agent_s_synth_reports=reports["c1_agent_s_synth"],
            subset_ids=["t1", "t2"],
            rng=random.Random(1),
            iters=20,
            ci=0.95,
            expected_models=["M1"],
            expected_seeds=3,
            expected_arms=list(p5_scale.C1_FORMAL_PAPER_ARMS),
            calibration_gate=self.CALIBRATION_GATE,
        )

        self.assertEqual(
            c1["time_analysis"]["tau_by_horizon_ms"]["short"],
            p5_aggregate._DEFAULT_C1_ADMINISTRATIVE_RMST_TAU_MS["short"][
                "e2e_excluding_synthesis"
            ],
        )

    def test_time_start_to_goal_mean_conditions_on_start_success(self) -> None:
        reports = self._formal_c1_reports()
        failed_start = reports["c1_agent_s"][0]["results"][1]
        failed_start.update({
            "before_visual_ok": False,
            "before_semantic_ok": False,
            "after_visual_ok": False,
            "after_semantic_ok": False,
            "start_success": False,
            "goal_success": False,
            "e2e_success": False,
            "time_start_to_goal_ms": 1,
        })
        c1 = p5_aggregate.c1_from_reports(
            reports["c1_state"], reports.get("c1_gui", []),
            gui_synth_reports=reports.get("c1_gui_synth", []),
            agent_s_reports=reports["c1_agent_s"],
            agent_s_synth_reports=reports["c1_agent_s_synth"],
            subset_ids=["t1", "t2"], rng=random.Random(1), iters=20, ci=0.95,
            expected_models=["M1"], expected_seeds=3, expected_arms=list(p5_scale.C1_FORMAL_PAPER_ARMS),
            calibration_gate=self.CALIBRATION_GATE,
        )
        point = next(
            row for row in c1["points"]
            if row["arm"] == "c1_agent_s" and row["model"] == "M1"
        )
        self.assertEqual(point["mean_time_start_to_goal_ms"]["n"], 5)
        self.assertEqual(point["mean_time_start_to_goal_ms"]["mean"], 20.0)

    def test_formal_c1_fails_closed_on_calibration_or_v2_field(self) -> None:
        reports = self._formal_c1_reports()
        reports["c1_agent_s"][0]["results"][0].pop("after_semantic_ok")
        reports["c1_agent_s"][0]["results"][0]["cost"]["usage_complete"] = False
        reports["c1_agent_s"][0]["results"][0]["cost"]["incomplete_calls"] = 1
        c1 = p5_aggregate.c1_from_reports(
            reports["c1_state"], reports.get("c1_gui", []),
            gui_synth_reports=reports.get("c1_gui_synth", []),
            agent_s_reports=reports["c1_agent_s"],
            agent_s_synth_reports=reports["c1_agent_s_synth"],
            subset_ids=["t1", "t2"], rng=random.Random(1), iters=20, ci=0.95,
            expected_models=["M1"], expected_seeds=3, expected_arms=list(p5_scale.C1_FORMAL_PAPER_ARMS),
            calibration_gate={"passed": False},
        )
        self.assertFalse(c1["complete"])
        reasons = "\n".join(c1["incomplete_reasons"])
        self.assertIn("c1-calibration-v4 gate missing or failed", reasons)
        self.assertIn("after_semantic_ok", reasons)
        self.assertIn("cost usage accounting is incomplete", reasons)

    def test_cost_gate_rejects_internally_inconsistent_token_buckets(self) -> None:
        reports = self._formal_c1_reports()
        malformed = reports["c1_agent_s"][0]["results"][0]["cost"]
        malformed["total_tokens"] += 1
        malformed["by_model"]["M1"]["reasoning_tokens"] = 999

        issues = p5_aggregate._cost_summary_issues(malformed)

        self.assertIn(
            "cost summary total_tokens does not equal prompt + completion", issues
        )
        self.assertTrue(
            any("reasoning_tokens exceeds completion_tokens" in issue for issue in issues)
        )

    def _write_complete_aggregate_fixture(
        self, run_dir: Path
    ) -> tuple[dict, dict, Path]:
        """Materialize the complete producer/sidecar/index publication chain."""

        reports, fixture_models, task_ids = self._full_formal_c1_reports()
        manifest = p5_scale.load_manifest(ROOT / "benchmarks/p5/manifest.yaml")
        runtime_lock = p5_scale._runtime_lock_policy(manifest)
        models = [str(model) for model in manifest["models"]]
        observer_model = str(manifest["visual_bridge"]["observer_model"])
        grounder_model = str(manifest["visual_bridge"]["grounder_model"])
        judge_model = str(manifest["visual_judge"]["model"])
        clean_observer = observer_model.split("@", 1)[0]
        clean_judge = judge_model.split("@", 1)[0]

        def trial_grounder(model: str) -> str:
            return p5_aggregate.resolve_grounder_model(
                grounder_model,
                observer_model=observer_model,
                decision_model=model,
            )

        calibration_grounder = p5_aggregate.calibration_grounder_model(
            grounder_model, observer_model=observer_model
        )

        manifest_hash = run_context.manifest_hash(manifest)
        task_manifest_hash = p5_aggregate.c1_task_manifest_hash(
            ROOT,
            manifest=manifest,
            arms=list(p5_scale.C1_ARMS),
            task_ids=task_ids,
            manifest_hash=manifest_hash,
        )
        git_commit = run_context.git_commit(ROOT)
        harness_hash = p5_aggregate.c1_harness_hash(ROOT)
        self.assertIsNotNone(git_commit)

        api = manifest["api"]
        provider_timeout_s = float(api["provider_timeout_s"])
        temperature = float(api["temperature"])
        api_provider = {
            "name": api["provider"],
            "base_url": str(api["base_url"]).rstrip("/"),
            "end_user_sha256": None,
        }
        profiles = dict(sorted(manifest["model_request_profiles"].items()))
        profiles_blob = json.dumps(profiles, sort_keys=True, separators=(",", ":"))
        profiles_sha256 = hashlib.sha256(profiles_blob.encode("utf-8")).hexdigest()

        def request_profile(model: str) -> dict:
            clean = model.split("@", 1)[0]
            return p5_aggregate.ModelRequestProfile(
                clean,
                profiles.get(clean, p5_aggregate.PROVIDER_DEFAULT),
            ).provenance()

        probes = [
            {
                "name": f"decision:{model.split('@', 1)[0]}",
                "model": model.split("@", 1)[0],
                "modality": "vision",
            }
            for model in models
        ] + [
            {
                "name": f"synthesis:{model.split('@', 1)[0]}",
                "model": model.split("@", 1)[0],
                "modality": "text",
            }
            for model in models
        ] + [
            {"name": "visual_observer", "model": clean_observer, "modality": "vision"},
        ] + [
            {
                "name": probe.name,
                "model": probe.model.split("@", 1)[0],
                "modality": "vision",
            }
            for probe in p5_aggregate.grounder_preflight_probes(
                models, observer=observer_model, configured=grounder_model
            )
        ] + [
            {"name": "visual_judge", "model": clean_judge, "modality": "vision"},
        ]
        usage = {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "reasoning_tokens": 0,
            "reasoning_tokens_observed": 0,
            "reasoning_tokens_lower_bound": 0,
            "reasoning_tokens_upper_bound": 0,
            "reasoning_detail_complete": True,
            "reasoning_detail_missing_calls": 0,
            "reasoning_detail_unknown_completion_tokens": 0,
            "reasoning_detail_expected_completion_tokens": 0,
            "reasoning_detail_unknown_fraction": 0.0,
            "reasoning_detail_tolerance_fraction": 0.10,
            "reasoning_detail_within_tolerance": True,
            "cached_prompt_tokens": 0,
            "total_tokens": 2,
        }
        preflight_results = []
        for probe in probes:
            probe_temperature = (
                temperature if str(probe["name"]).startswith("decision:") else None
            )
            attempt = {
                "ordinal": 1,
                "max_tokens": 8192,
                "request_succeeded": True,
                "content_nonempty": True,
                "reasoning_observed": False,
                "reasoning_content_observed": False,
                "reasoning_token_anomaly": False,
                "request_contract_passed": True,
                "reasoning_exhausted": False,
                "finish_reason": "stop",
                "temperature_configured": probe_temperature,
                "temperature_requested": probe_temperature,
                "temperature_fallback_triggered": False,
                "temperature_omitted_after_rejection": False,
                "usage": dict(usage),
            }
            preflight_results.append({
                **probe,
                "provider": api_provider["name"],
                "success": True,
                "request_contract_passed": True,
                "reasoning_content_observed": False,
                "reasoning_token_anomaly": False,
                "attempts": 1,
                "finish_reason": "stop",
                "temperature_configured": probe_temperature,
                "temperature_fallback_triggered": False,
                "temperature_omitted_after_rejection": False,
                "usage": dict(usage),
                "request_profile": request_profile(probe["model"]),
                "attempt_records": [attempt],
            })
        preflight_artifact = run_dir / "c1-preflight.json"
        preflight_payload = {
            "schema_version": 4,
            "complete": True,
            "generated_at_utc": "2026-07-11T00:00:00+00:00",
            "git_commit": git_commit,
            "manifest_hash": manifest_hash,
            "harness_hash": harness_hash,
            "c1_application_revisions": (
                p5_aggregate.c1_application_revisions(ROOT)
            ),
            "c1_environment": p5_aggregate.validate_c1_environment(
                ROOT,
                manifest=manifest,
            ),
            "provider_timeout_s": provider_timeout_s,
            "api_provider": api_provider,
            "model_request_profiles": profiles,
            "model_request_profiles_sha256": profiles_sha256,
            "usage_accounting": copy.deepcopy(manifest["usage_accounting"]),
            "probe_count": len(preflight_results),
            "initial_max_tokens": 8192,
            "retry_max_tokens": 16384,
            "retry_rule": (
                "one_retry_only_if_empty_content_and_explicit_output_limit"
            ),
            "temperature_contract": {
                "configured": temperature,
                "applies_to": "decision_probes_only",
                "fallback": (
                    "omit_only_after_explicit_http_400_temperature_rejection"
                ),
            },
            "results": preflight_results,
        }
        preflight_artifact.write_text(
            json.dumps(preflight_payload, sort_keys=True), encoding="utf-8"
        )
        preflight = {
            **preflight_payload,
            "artifact": str(preflight_artifact.resolve()),
            "artifact_sha256": hashlib.sha256(
                preflight_artifact.read_bytes()
            ).hexdigest(),
        }
        preflight_public = {
            key: value for key, value in preflight.items() if key != "artifact"
        }

        calibration_artifact = run_dir / "c1-calibration.json"
        background_protocol = copy.deepcopy(manifest["c1_background_pixel_protocol"])
        background_blob = json.dumps(
            background_protocol,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        background_sha256 = hashlib.sha256(
            background_blob.encode("utf-8")
        ).hexdigest()
        isolation = {
            "target_never_frontmost": True,
            "system_pointer_unchanged": True,
            "system_clipboard_unchanged": True,
            "foreground_text_unchanged": True,
        }
        calibration_payload = {
            "schema_version": 4,
            "schema": "c1-calibration-v4",
            "passed": True,
            "generated_at_utc": "2026-07-11T00:00:00+00:00",
            "git_commit": git_commit,
            "manifest_hash": manifest_hash,
            "harness_hash": harness_hash,
            "c1_application_revisions": (
                p5_aggregate.c1_application_revisions(ROOT)
            ),
            "c1_environment": p5_aggregate.validate_c1_environment(
                ROOT,
                manifest=manifest,
            ),
            "provider_timeout_s": provider_timeout_s,
            "api_provider": api_provider,
            "observer_model": observer_model,
            "grounder_model": calibration_grounder,
            "background_pixel_protocol_sha256": background_sha256,
            "backends": {
                benchmark: {
                    **identity,
                    "passed": True,
                    "foreground_isolation": isolation,
                }
                for benchmark, identity in background_protocol["benchmarks"].items()
            },
        }
        calibration_artifact.write_text(
            json.dumps(calibration_payload, sort_keys=True), encoding="utf-8"
        )
        calibration = {
            **calibration_payload,
            "artifact": str(calibration_artifact.resolve()),
            "artifact_sha256": hashlib.sha256(
                calibration_artifact.read_bytes()
            ).hexdigest(),
        }

        cache_policy = {
            "root": str((run_dir / "c1-artifact-cache").resolve()),
            "golden_and_reference": "run_scoped",
            "visual_judge": "disabled",
            "self_synthesis": str(
                (run_dir / "c1-self-synthesis-cache").resolve()
            ),
        }
        visual_judge = {
            "model": judge_model,
            "base_url": str(manifest["visual_judge"]["base_url"]).rstrip("/"),
        }
        expected_task_set, task_set_error = (
            p5_aggregate._expected_c1_task_set_provenance(
                {"task_ids": task_ids}, manifest, ROOT
            )
        )
        self.assertIsNone(task_set_error)
        self.assertIsNotNone(expected_task_set)
        assert expected_task_set is not None
        expected_task_metadata = expected_task_set["task_metadata"]
        c1_prompt_revisions = {
            "arms": {
                arm: {
                    key: str(manifest["arms"][arm].get(key) or "")
                    for key in ("prompt_revision", "phase_b_prompt_revision")
                }
                for arm in p5_scale.C1_ARMS
            },
            "tasks": {
                task_id: str(metadata.get("prompt_revision") or "")
                for task_id, metadata in expected_task_metadata.items()
            },
        }
        c1_random_controls = {
            task_id: copy.deepcopy(metadata.get("random_control"))
            for task_id, metadata in expected_task_metadata.items()
        }
        def remap_cost(cost: dict, old_model: str, model: str) -> None:
            replacements = {
                old_model: model.split("@", 1)[0],
                "observer": clean_observer,
                "judge": clean_judge,
            }
            cost["by_model"] = {
                replacements.get(key, key): value
                for key, value in cost.get("by_model", {}).items()
            }
            cost["by_kind_model"] = {
                kind: {
                    replacements.get(key, key): value
                    for key, value in model_rows.items()
                }
                for kind, model_rows in cost.get("by_kind_model", {}).items()
            }

        arms: dict[str, list[dict]] = {arm: [] for arm in p5_scale.C1_ARMS}
        first_report: Path | None = None
        for arm in p5_scale.C1_ARMS:
            for report_index, report in enumerate(reports[arm]):
                model_index = report_index // 3
                model = models[model_index]
                old_model = fixture_models[model_index]
                seed = int(report["seed"])
                if arm == "c1_state":
                    report["synthesis_model"] = model
                else:
                    report["schema_version"] = 5
                    report["model"] = model
                    policy_kind = (
                        "agent_s"
                        if arm in {"c1_agent_s", "c1_agent_s_synth"}
                        else "gui_agent"
                    )
                    start_mode = (
                        "synthesized"
                        if arm in {"c1_gui_synth", "c1_agent_s_synth"}
                        else "from_start"
                    )
                    report.update({
                        "policy_kind": policy_kind,
                        "start_mode": start_mode,
                        "provider_timeout_s": provider_timeout_s,
                        "observer_model": observer_model,
                        "grounder_model": (
                            trial_grounder(model) if policy_kind == "agent_s" else None
                        ),
                    })
                    if policy_kind == "gui_agent":
                        report["policy_runtime_metadata"] = {
                            "policy_kind": "gui_agent",
                            "model": model.split("@", 1)[0],
                            "provider": api_provider["name"],
                            "model_request_profile": request_profile(model),
                            "max_tokens": 8192,
                            "reasoning_retry_max_tokens": 16384,
                            "provider_timeout_s": provider_timeout_s,
                            "sdk_max_retries": 0,
                        }
                    else:
                        report["policy_runtime_metadata"] = {
                            "policy_kind": "agent_s",
                            "model": model.split("@", 1)[0],
                            "provider": api_provider["name"],
                            "grounder_model": trial_grounder(model).split("@", 1)[0],
                            "grounder_provider": api_provider["name"],
                            "protocol_version": p5_aggregate.AGENT_S_PROTOCOL_VERSION,
                            "agent_s_version": p5_aggregate.PINNED_AGENT_S_VERSION,
                            "agent_s_commit": p5_aggregate.PINNED_AGENT_S_COMMIT,
                            "runtime_mode": "background_pixel",
                            "execution_backend": "background_pixel_v1",
                            "coordinate_space": "frame_pixels_top_left_v1",
                            "raw_screenshot_observation": True,
                            "capture_backend": "application_registered_v1",
                            "action_execution_owner": "parent_runtime",
                            "foreground_required": False,
                            "upstream_worker": True,
                            "reflection_enabled": True,
                            "local_code_enabled": False,
                            "worker_observation_mode": "screenshot",
                            "native_exec_enabled": False,
                            "provider_timeout_s": provider_timeout_s,
                            "predict_timeout_mode": (
                                p5_aggregate.AGENT_S_PREDICT_TIMEOUT_MODE
                            ),
                            "predict_timeout_s": None,
                            "control_timeout_s": (
                                p5_aggregate.AGENT_S_CONTROL_REQUEST_TIMEOUT_S
                            ),
                            "max_provider_attempts_per_predict": (
                                p5_aggregate.AGENT_S_MAX_PROVIDER_ATTEMPTS_PER_PREDICT
                            ),
                            "model_request_profiles": {
                                "worker": request_profile(model),
                                "reflection": request_profile(model),
                                "grounding": request_profile(trial_grounder(model)),
                            },
                        }
                for result in report["results"]:
                    for field in (
                        "cost",
                        "actual_cost",
                        "transient_infrastructure_cost",
                        "synthesis_actual_cost",
                        "synthesis_transient_actual_cost",
                        "synthesis_attributed_cost",
                    ):
                        value = result.get(field)
                        if isinstance(value, dict):
                            remap_cost(value, old_model, model)
                    attempts = result.get("attempt_costs")
                    if isinstance(attempts, list):
                        for attempt in attempts:
                            if not isinstance(attempt, dict):
                                continue
                            remap_cost(attempt, old_model, model)
                            task_actual = attempt.get("task_actual_cost")
                            if isinstance(task_actual, dict):
                                remap_cost(task_actual, old_model, model)
                            accounting = attempt.get("synthesis_accounting")
                            if isinstance(accounting, dict):
                                for field in (
                                    "actual_cost",
                                    "transient_actual_cost",
                                    "attributed_cost",
                                ):
                                    value = accounting.get(field)
                                    if isinstance(value, dict):
                                        remap_cost(value, old_model, model)
                        rebound, ledger_hash = state_loop._bind_attempt_ledger(
                            attempts,
                            task_id=str(result["task_id"]),
                        )
                        result["attempt_costs"] = rebound
                        result["attempt_ledger_hash"] = ledger_hash
                report["run_context"] = {
                    "arm": arm,
                    "model": model,
                    "seed": seed,
                    "git_commit": git_commit,
                    "task_manifest_hash": task_manifest_hash,
                    "runtime_lock": runtime_lock,
                    "c1_background_pixel_protocol": background_protocol,
                    "c1_background_pixel_protocol_sha256": background_sha256,
                    "provider_timeout_s": provider_timeout_s,
                    "usage_accounting": copy.deepcopy(manifest["usage_accounting"]),
                    "preflight": preflight_public,
                    "calibration": calibration,
                }

                directory = run_dir / arm / f"model-{model_index}" / f"seed-{seed}"
                directory.mkdir(parents=True, exist_ok=True)
                report_path = directory / "report.json"
                report_path.write_text(
                    json.dumps(report, sort_keys=True), encoding="utf-8"
                )
                if first_report is None:
                    first_report = report_path
                sidecar = {
                    "schema_version": 2,
                    "kind": "p5_c1_invocation_context",
                    "run_id": "complete-fixture",
                    "arm": arm,
                    "model": model,
                    "seed": seed,
                    "git_commit": git_commit,
                    "manifest_hash": manifest_hash,
                    "task_manifest_hash": task_manifest_hash,
                    "harness_hash": harness_hash,
                    "preflight": preflight_public,
                    "api_provider": api_provider,
                    "model_request_profiles": profiles,
                    "model_request_profiles_sha256": profiles_sha256,
                    "model_request_profile": request_profile(model),
                    "runtime_lock": runtime_lock,
                    "c1_background_pixel_protocol": background_protocol,
                    "c1_background_pixel_protocol_sha256": background_sha256,
                    "provider_timeout_s": provider_timeout_s,
                    "usage_accounting": copy.deepcopy(manifest["usage_accounting"]),
                    "cache_policy": cache_policy,
                    "calibration": {
                        key: value
                        for key, value in calibration.items()
                        if key != "artifact"
                    },
                    "visual_judge": visual_judge,
                    "task_set": expected_task_set,
                }
                if arm != "c1_state":
                    sidecar["invocation"] = {
                        "start_mode": report["start_mode"],
                        "policy_kind": report["policy_kind"],
                        "step_budgets": dict(C1_STEP_BUDGETS),
                        "observer_model": observer_model,
                        "grounder_model": trial_grounder(model),
                        "provider_timeout_s": provider_timeout_s,
                        "synth_model": model,
                        "shared_synthesis_cache": cache_policy["self_synthesis"],
                    }
                (directory / "p5-invocation-context.json").write_text(
                    json.dumps(sidecar, sort_keys=True), encoding="utf-8"
                )
                arms[arm].append({
                    "status": "ok",
                    "model": model,
                    "seed": seed,
                    "report": str(report_path.resolve()),
                    "report_sha256": hashlib.sha256(
                        report_path.read_bytes()
                    ).hexdigest(),
                })

        base_order = list(p5_scale.C1_ARMS)
        execution_order: list[dict] = []
        blocks: list[dict] = []
        for model_index, model in enumerate(models):
            for seed in range(3):
                offset = (model_index * 3 + seed) % len(base_order)
                ordered = base_order[offset:] + base_order[:offset]
                blocks.append({
                    "model": model,
                    "model_index": model_index,
                    "seed": seed,
                    "offset": offset,
                    "arms": ordered,
                })
                for position, arm in enumerate(ordered):
                    execution_order.append({
                        "ordinal": len(execution_order),
                        "arm": arm,
                        "model": model,
                        "seed": seed,
                        "model_index": model_index,
                        "offset": offset,
                        "position": position,
                        "ordered_arms": ordered,
                    })
        run_index = {
            "experiment_id": "p5_scale",
            "run_id": "complete-fixture",
            "git_commit": git_commit,
            "manifest_hash": manifest_hash,
            "task_manifest_hash": task_manifest_hash,
            "harness_hash": harness_hash,
            "c1_application_revisions": (
                p5_aggregate.c1_application_revisions(ROOT)
            ),
            "c1_environment": p5_aggregate.validate_c1_environment(
                ROOT,
                manifest=manifest,
            ),
            "c1_prompt_revisions": c1_prompt_revisions,
            "c1_random_controls": c1_random_controls,
            "task_ids": task_ids,
            "headless": False,
            "models": models,
            "seeds": 3,
            "provider_timeout_s": provider_timeout_s,
            "api_provider": api_provider,
            "visual_bridge": dict(manifest["visual_bridge"]),
            "visual_judge": visual_judge,
            "visual_judge_model": judge_model,
            "model_request_profiles": profiles,
            "model_request_profiles_sha256": profiles_sha256,
            "runtime_lock": runtime_lock,
            "c1_background_pixel_protocol": background_protocol,
            "c1_background_pixel_protocol_sha256": background_sha256,
            "c1_cache_policy": cache_policy,
            "usage_accounting": copy.deepcopy(manifest["usage_accounting"]),
            "preflight": preflight,
            "calibration": calibration,
            "execution_order": execution_order,
            "latin_square": {"base_order": base_order, "blocks": blocks},
            "arms_run": base_order,
            "arms": arms,
        }
        assert first_report is not None
        return run_index, manifest, first_report

    def _upgrade_paper_fixture_to_atomic(
        self, run_index: dict, manifest: dict, run_dir: Path
    ) -> Path:
        """Upgrade the complete synthetic fixture to the registered schema-v3 chain."""

        policy = copy.deepcopy(manifest["c1_trial_persistence"])
        protocol = copy.deepcopy(manifest["c1_start_gate_protocol"])
        task_rows = yaml.safe_load(
            (ROOT / "benchmarks/p5/c1_tasks.yaml").read_text(encoding="utf-8")
        )["tasks"]
        task_metadata = {str(task["id"]): task for task in task_rows}
        run_index.update({
            "c1_trial_persistence": policy,
            "c1_start_gate_protocol": protocol,
            "c1_source_index_request": None,
            "c1_sources": [],
            "c1_operational_overhead": [],
        })
        cache_root = Path(run_index["c1_cache_policy"]["root"])
        cache_root.mkdir(parents=True, exist_ok=True)
        expected_goldens, expected_golden_issues = (
            p5_aggregate._expected_c1_golden_provenance(
                run_index,
                manifest,
                ROOT,
            )
        )
        self.assertEqual(expected_golden_issues, [])
        self.assertIsNotNone(expected_goldens)
        assert expected_goldens is not None
        golden_payloads: dict[tuple[str, int], dict] = {}

        def golden_payload(task_id: str, seed: int) -> dict:
            key = (task_id, seed)
            if key in golden_payloads:
                return copy.deepcopy(golden_payloads[key])
            expected = expected_goldens[key]
            golden_dir = cache_root / task_id / f"seed{seed}"
            before = golden_dir / "golden_before.png"
            after = golden_dir / "golden_after.png"
            deterministic_seed = int(
                hashlib.sha256(f"{task_id}:{seed}".encode("utf-8")).hexdigest()[:8],
                16,
            )
            _write_p5_informative_png(before, seed=deterministic_seed)
            _write_p5_informative_png(after, seed=deterministic_seed + 1)
            quality = {
                "before": state_loop.screenshot_quality(before),
                "after": state_loop.screenshot_quality(after),
            }
            metadata = {
                "schema_version": 2,
                "fingerprint": expected["fingerprint"],
                "git_commit": expected["git_commit"],
                "experiment_seed": seed,
                "oracle_spec_sha256": expected["oracle_spec_sha256"],
                "cacheable": True,
                "consistency": {"source": "verified_cache"},
                "quality": quality,
                "images": {
                    "before": {"sha256": hashlib.sha256(before.read_bytes()).hexdigest()},
                    "after": {"sha256": hashlib.sha256(after.read_bytes()).hexdigest()},
                },
            }
            (golden_dir / "meta.json").write_text(
                yaml.safe_dump(metadata, sort_keys=False),
                encoding="utf-8",
            )
            payload = {
                "before": str(before.resolve()),
                "after": str(after.resolve()),
                "fingerprint": expected["fingerprint"],
                "cache_hit": True,
                "cacheable": True,
                "consistency": {"source": "verified_cache"},
                "quality": quality,
                "artifacts": c1_golden_artifacts.capture_oracle_golden_artifact_digests(
                    before,
                    after,
                ),
            }
            golden_payloads[key] = payload
            return copy.deepcopy(payload)

        first_snapshot: Path | None = None
        atomic_arms: dict[str, list[dict]] = {
            arm: [] for arm in p5_scale.C1_ARMS
        }
        for arm in p5_scale.C1_ARMS:
            for invocation_index, entry in enumerate(run_index["arms"][arm]):
                source_report = Path(entry["report"])
                report = json.loads(source_report.read_text(encoding="utf-8"))
                report["schema_version"] = 4 if arm == "c1_state" else 5
                sidecar_path = source_report.parent / "p5-invocation-context.json"
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
                sidecar.update({
                    "schema_version": 6,
                    "task_id": None,
                    "trial_attempt": 0,
                    "usage_accounting": copy.deepcopy(manifest["usage_accounting"]),
                    "c1_trial_persistence": policy,
                    "c1_start_gate_protocol": protocol,
                    "c1_prompt_revisions": run_index["c1_prompt_revisions"],
                    "c1_random_controls": run_index["c1_random_controls"],
                    "c1_application_revisions": run_index[
                        "c1_application_revisions"
                    ],
                    "c1_environment": run_index["c1_environment"],
                })
                if arm == "c1_state":
                    sidecar["invocation"] = {
                        "explore": True,
                        "max_iters": 1,
                        "seeds": 1,
                        "restore_diagnostics": False,
                        "restore_diagnostic_task_ids": [],
                    }
                    report["restore_diagnostics"] = {
                        "enabled": False,
                        "task_ids": [],
                    }
                else:
                    sidecar["invocation"]["start_gate_protocol"] = protocol
                sidecar_path.write_text(
                    json.dumps(sidecar, sort_keys=True), encoding="utf-8"
                )
                for result in report["results"]:
                    task_id = str(result["task_id"])
                    seed = int(result["seed"])
                    registered = task_metadata[task_id]
                    random_control = copy.deepcopy(registered["random_control"])
                    result["benchmark"] = registered["benchmark"]
                    result["horizon_class"] = registered["horizon_class"]
                    result["prompt_revision"] = registered["prompt_revision"]
                    result["random_control"] = random_control
                    result["random_control_evidence_valid"] = True
                    seed_evidence = result["seed_evidence"]
                    seed_evidence["random_control"] = copy.deepcopy(
                        random_control
                    )
                    if random_control["mode"] == "experiment_seed":
                        seed_evidence["algorithm"] = random_control["algorithm"]
                        seed_evidence["seed"] = seed
                    else:
                        seed_evidence["randomness"] = "not_applicable"
                        seed_evidence["seed_role"] = "trial_pairing_only"
                        seed_evidence["seed"] = seed
                    result["incomplete"] = False
                    result["oracle_goldens"] = golden_payload(task_id, seed)
                    benchmark = str(result.get("benchmark") or "")
                    normalized_benchmark = benchmark.strip().lower()
                    native_process = normalized_benchmark in {
                        "openra",
                        "jynew",
                        "pal3",
                        "blender",
                        "libreoffice",
                    }
                    if arm == "c1_state" and normalized_benchmark in {
                        "jynew",
                        "pal3",
                    }:
                        # The state arm closes its Unity adapter before grading,
                        # so there is no live process left to suspend.
                        hold = {
                            "required": False,
                            "held": False,
                            "released": True,
                            "backend": "runtime_terminated_before_grading",
                        }
                    else:
                        hold = {
                            "required": True,
                            "held": True,
                            "released": True,
                            "backend": (
                                "sigstop"
                                if normalized_benchmark
                                in {
                                    "luanti",
                                    "minetest",
                                    "openra",
                                    "jynew",
                                    "pal3",
                                    "blender",
                                    "libreoffice",
                                }
                                else "chromium_cdp_script_pause"
                            ),
                        }
                        if native_process:
                            hold["held_pid"] = result["seed_evidence"]["initial"][
                                "process_id"
                            ]
                    if arm == "c1_state":
                        result["restore_diagnostics_enabled"] = False
                        # Start is graded under a hold for every arm whose Start
                        # rate is compared, the state arm included.
                        result["start_grading_runtime_hold"] = hold
                    else:
                        result.update({
                            "start_construction_status": (
                                "not_applicable"
                                if arm in {"c1_gui", "c1_agent_s"}
                                else "reached"
                            ),
                            "start_grading_started": True,
                            "start_gate_status": "passed",
                            "goal_evaluation_status": "evaluated",
                            "before_visual_status": "evaluated",
                            "after_visual_status": "evaluated",
                            "before_semantic_status": "success",
                            "after_semantic_status": "success",
                            "start_grading_runtime_hold": hold,
                        })
                report["run_context"]["c1_trial_persistence"] = policy
                report["run_context"]["c1_start_gate_protocol"] = protocol
                report["run_context"]["c1_application_revisions"] = run_index[
                    "c1_application_revisions"
                ]
                report["run_context"]["c1_environment"] = run_index[
                    "c1_environment"
                ]
                report["run_context"]["c1_prompt_revisions"] = run_index[
                    "c1_prompt_revisions"
                ]
                report["run_context"]["c1_random_controls"] = run_index[
                    "c1_random_controls"
                ]
                report["run_context"]["trial_attempt"] = 0
                report["run_context"]["run_id"] = (
                    f"{run_index['run_id']}:{arm}:{entry['model']}:"
                    f"{entry['seed']}:all:attempt0"
                )
                source_report.write_text(
                    json.dumps(report, sort_keys=True), encoding="utf-8"
                )
                source_sha = hashlib.sha256(source_report.read_bytes()).hexdigest()
                for result in report["results"]:
                    task_id = str(result["task_id"])
                    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]
                    snapshot_dir = (
                        run_dir
                        / "c1-trial-evidence"
                        / arm
                        / f"invocation-{invocation_index}"
                        / digest
                    )
                    snapshot_dir.mkdir(parents=True, exist_ok=True)
                    snapshot_path = snapshot_dir / "report.json"
                    snapshot = copy.deepcopy(report)
                    snapshot["results"] = [copy.deepcopy(result)]
                    snapshot["complete"] = True
                    snapshot["atomic_trial_evidence"] = {
                        "schema_version": 3,
                        "mode": "shared_invocation_incremental_snapshot_v1",
                        "task_id": task_id,
                        "source_invocation_report": str(source_report.resolve()),
                        "source_invocation_report_sha256": source_sha,
                    }
                    snapshot_path.write_text(
                        json.dumps(snapshot, sort_keys=True), encoding="utf-8"
                    )
                    (snapshot_dir / "p5-invocation-context.json").write_bytes(
                        sidecar_path.read_bytes()
                    )
                    atomic_arms[arm].append({
                        "status": "ok",
                        "model": entry["model"],
                        "seed": entry["seed"],
                        "task_id": task_id,
                        "trial_attempt": 0,
                        "attempt_id": (
                            f"{run_index['run_id']}:{arm}:{entry['model']}:"
                            f"{entry['seed']}:{task_id}:attempt0"
                        ),
                        "imported": False,
                        "report": str(snapshot_path.resolve()),
                        "report_sha256": hashlib.sha256(
                            snapshot_path.read_bytes()
                        ).hexdigest(),
                        "retry_history": [],
                        "error": None,
                        "execution": entry.get("execution"),
                        "replacement_for": None,
                        "source_provenance": None,
                    })
                    if first_snapshot is None:
                        first_snapshot = snapshot_path
        run_index["arms"] = atomic_arms
        assert first_snapshot is not None
        return first_snapshot

    @patch.object(
        p5_aggregate,
        "c1_harness_hash",
        new=lambda _root: "f" * 64,
    )
    @patch.object(
        p5_aggregate,
        "c1_application_revisions",
        new=_aggregate_fixture_application_revisions,
    )
    @patch.object(
        p5_aggregate,
        "validate_c1_environment",
        new=_aggregate_fixture_environment,
    )
    def test_aggregate_rejects_legacy_invocation_level_formal_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_index, manifest, report_path = (
                self._write_complete_aggregate_fixture(Path(tmp))
            )

            summary = p5_aggregate.aggregate(
                run_index, manifest=manifest, report_root=ROOT, rng_seed=7, iters=2
            )
            self.assertFalse(summary["complete"])
            self.assertFalse(summary["c1_cost_accuracy"]["complete"])
            self.assertNotIn("paper_ready", summary)
            self.assertNotIn("paper_ready", summary["c1_cost_accuracy"])
            self.assertTrue(summary["c1_cost_accuracy"]["incomplete_reasons"])
            self.assertEqual(
                summary["c1_cost_accuracy"]["coverage"]["observed_trials"],
                (
                    manifest["c1_task_count"]
                    * len(p5_scale.C1_ARMS)
                    * len(manifest["models"])
                    * manifest["seeds"]
                ),
            )
            return

    @patch.object(
        p5_aggregate,
        "c1_harness_hash",
        new=lambda _root: "f" * 64,
    )
    @patch.object(
        p5_aggregate,
        "c1_application_revisions",
        new=_aggregate_fixture_application_revisions,
    )
    @patch.object(
        p5_aggregate,
        "validate_c1_environment",
        new=_aggregate_fixture_environment,
    )
    def test_atomic_complete_fixture_is_true_then_tamper_is_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            run_index, manifest, _legacy_report = (
                self._write_complete_aggregate_fixture(run_dir)
            )
            report_path = self._upgrade_paper_fixture_to_atomic(
                run_index, manifest, run_dir
            )
            expected_timeout = float(manifest["api"]["provider_timeout_s"])
            timeout_report = json.loads(report_path.read_text(encoding="utf-8"))
            timeout_sidecar = json.loads(
                (report_path.parent / "p5-invocation-context.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(run_index["provider_timeout_s"], expected_timeout)
            self.assertEqual(
                run_index["preflight"]["provider_timeout_s"], expected_timeout
            )
            self.assertEqual(
                run_index["calibration"]["provider_timeout_s"], expected_timeout
            )
            self.assertEqual(timeout_sidecar["provider_timeout_s"], expected_timeout)
            self.assertEqual(
                timeout_sidecar["invocation"]["provider_timeout_s"],
                expected_timeout,
            )
            self.assertEqual(timeout_report["provider_timeout_s"], expected_timeout)
            self.assertEqual(
                timeout_report["policy_runtime_metadata"]["provider_timeout_s"],
                expected_timeout,
            )
            self.assertEqual(
                timeout_report["run_context"]["provider_timeout_s"],
                expected_timeout,
            )
            summary = p5_aggregate.aggregate(
                run_index, manifest=manifest, report_root=ROOT, rng_seed=7, iters=2
            )
            self.assertTrue(
                summary["complete"],
                summary["c1_cost_accuracy"]["incomplete_reasons"],
            )
            original = report_path.read_bytes()
            report_path.write_bytes(original + b"\n")
            tampered = p5_aggregate.aggregate(
                run_index, manifest=manifest, report_root=ROOT, rng_seed=7, iters=2
            )
            self.assertFalse(tampered["complete"])
            self.assertTrue(any(
                "report_sha256 mismatch" in reason
                for reason in tampered["c1_cost_accuracy"]["incomplete_reasons"]
            ))
            report_path.write_bytes(original)

            malformed_path = json.loads(original)
            malformed_path["results"][0]["oracle_goldens"]["before"] = "\x00"
            report_path.write_text(
                json.dumps(malformed_path, sort_keys=True),
                encoding="utf-8",
            )
            bad_path_index = copy.deepcopy(run_index)
            bad_path_index["arms"]["c1_state"][0]["report_sha256"] = hashlib.sha256(
                report_path.read_bytes()
            ).hexdigest()
            bad_path_summary = p5_aggregate.aggregate(
                bad_path_index,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )
            self.assertFalse(bad_path_summary["complete"])
            self.assertTrue(any(
                "oracle golden before path is invalid" in reason
                for reason in bad_path_summary["c1_cost_accuracy"]["incomplete_reasons"]
            ))
            report_path.write_bytes(original)

            # The report SHA protects the producer payload, while the payload's
            # three artifact digests protect bytes that remain outside JSON.
            report_payload = json.loads(original)
            golden_before = Path(
                report_payload["results"][0]["oracle_goldens"]["before"]
            )
            golden_meta = golden_before.parent / "meta.json"
            original_before = golden_before.read_bytes()
            original_meta = golden_meta.read_bytes()

            _write_p5_informative_png(golden_before, seed=987_654)
            png_tampered = p5_aggregate.aggregate(
                run_index,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )
            self.assertFalse(png_tampered["complete"])
            self.assertTrue(any(
                "sha256 does not match report artifact digest" in reason
                for reason in png_tampered["c1_cost_accuracy"]["incomplete_reasons"]
            ))
            golden_before.write_bytes(original_before)

            _write_p5_informative_png(golden_before, seed=987_655)
            rewritten_meta = yaml.safe_load(original_meta.decode("utf-8"))
            rewritten_meta["images"]["before"]["sha256"] = hashlib.sha256(
                golden_before.read_bytes()
            ).hexdigest()
            golden_meta.write_text(
                yaml.safe_dump(rewritten_meta, sort_keys=False),
                encoding="utf-8",
            )
            coordinated_tamper = p5_aggregate.aggregate(
                run_index,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )
            self.assertFalse(coordinated_tamper["complete"])
            coordinated_reasons = coordinated_tamper["c1_cost_accuracy"][
                "incomplete_reasons"
            ]
            self.assertTrue(any(
                "artifacts.metadata: sha256 does not match report artifact digest"
                in reason
                for reason in coordinated_reasons
            ))
            self.assertTrue(any(
                "before: sha256 does not match report artifact digest" in reason
                for reason in coordinated_reasons
            ))
            golden_before.write_bytes(original_before)
            golden_meta.write_bytes(original_meta)

            malformed_fingerprint = json.loads(original)
            malformed_fingerprint["results"][0]["oracle_goldens"][
                "fingerprint"
            ] = ["not", "a", "string"]
            report_path.write_text(
                json.dumps(malformed_fingerprint, sort_keys=True),
                encoding="utf-8",
            )
            bad_fingerprint_index = copy.deepcopy(run_index)
            bad_fingerprint_index["arms"]["c1_state"][0][
                "report_sha256"
            ] = hashlib.sha256(report_path.read_bytes()).hexdigest()
            malformed_summary = p5_aggregate.aggregate(
                bad_fingerprint_index,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )
            self.assertFalse(malformed_summary["complete"])
            self.assertTrue(any(
                "fingerprint" in reason
                for reason in malformed_summary["c1_cost_accuracy"][
                    "incomplete_reasons"
                ]
            ))
            report_path.write_bytes(original)

            original_report = report_path.read_bytes()
            report_path.write_bytes(original_report + b"\n")
            tampered_bytes = p5_aggregate.aggregate(
                run_index, manifest=manifest, report_root=ROOT, rng_seed=7, iters=2
            )
            self.assertFalse(tampered_bytes["complete"])
            self.assertTrue(any(
                "report_sha256 mismatch" in reason
                for reason in tampered_bytes["c1_cost_accuracy"]["incomplete_reasons"]
            ))
            report_path.write_bytes(original_report)

            bad_report_hash = json.loads(json.dumps(run_index))
            bad_report_hash["arms"]["c1_agent_s"][0]["report_sha256"] = "0" * 64
            self.assertFalse(p5_aggregate.aggregate(
                bad_report_hash,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )["complete"])

            bad_cache = json.loads(json.dumps(run_index))
            bad_cache["c1_cache_policy"]["visual_judge"] = "run_scoped"
            self.assertFalse(p5_aggregate.aggregate(
                bad_cache,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )["complete"])

            bad_runtime_lock = json.loads(json.dumps(run_index))
            bad_runtime_lock["runtime_lock"]["lock_dir"] = str(
                Path("/tmp/warptest-wrong-runtime-locks").resolve()
            )
            bad_runtime_summary = p5_aggregate.aggregate(
                bad_runtime_lock,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )
            self.assertFalse(bad_runtime_summary["complete"])
            self.assertTrue(any(
                "runtime_lock.lock_dir differs from manifest" in reason
                for reason in bad_runtime_summary["c1_cost_accuracy"][
                    "incomplete_reasons"
                ]
            ))

            bad_manifest = json.loads(json.dumps(manifest))
            bad_manifest["runtime_lock"]["lock_dir"] = (
                "/tmp/warptest-wrong-runtime-locks"
            )
            bad_manifest_summary = p5_aggregate.aggregate(
                run_index,
                manifest=bad_manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )
            self.assertFalse(bad_manifest_summary["complete"])
            self.assertTrue(any(
                "runtime_lock.lock_dir differs from manifest" in reason
                for reason in bad_manifest_summary["c1_cost_accuracy"][
                    "incomplete_reasons"
                ]
            ))

            sidecar_path = report_path.parent / "p5-invocation-context.json"
            original_sidecar = sidecar_path.read_bytes()
            tampered_sidecar = json.loads(original_sidecar)
            tampered_sidecar["runtime_lock"]["scope"] = "report-scoped"
            sidecar_path.write_text(
                json.dumps(tampered_sidecar, sort_keys=True), encoding="utf-8"
            )
            sidecar_summary = p5_aggregate.aggregate(
                run_index,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )
            self.assertFalse(sidecar_summary["complete"])
            self.assertTrue(any(
                "sidecar runtime_lock mismatch" in reason
                for reason in sidecar_summary["c1_cost_accuracy"][
                    "incomplete_reasons"
                ]
            ))
            sidecar_path.write_bytes(original_sidecar)

            protocol_sidecar = json.loads(original_sidecar)
            protocol_sidecar["c1_start_gate_protocol"]["sequence"] = ["tampered"]
            sidecar_path.write_text(
                json.dumps(protocol_sidecar, sort_keys=True), encoding="utf-8"
            )
            protocol_summary = p5_aggregate.aggregate(
                run_index,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )
            self.assertFalse(protocol_summary["complete"])
            self.assertTrue(any(
                "c1_start_gate_protocol" in reason
                for reason in protocol_summary["c1_cost_accuracy"][
                    "incomplete_reasons"
                ]
            ))
            sidecar_path.write_bytes(original_sidecar)

            provider_timeout_report = json.loads(original)
            provider_timeout_report["provider_timeout_s"] = expected_timeout + 1
            provider_timeout_report["policy_runtime_metadata"][
                "provider_timeout_s"
            ] = expected_timeout + 1
            provider_timeout_report["run_context"]["provider_timeout_s"] = (
                expected_timeout + 1
            )
            report_path.write_text(
                json.dumps(provider_timeout_report, sort_keys=True),
                encoding="utf-8",
            )
            provider_timeout_sidecar = json.loads(original_sidecar)
            provider_timeout_sidecar["provider_timeout_s"] = expected_timeout + 1
            provider_timeout_sidecar["invocation"]["provider_timeout_s"] = (
                expected_timeout + 1
            )
            sidecar_path.write_text(
                json.dumps(provider_timeout_sidecar, sort_keys=True),
                encoding="utf-8",
            )
            bad_provider_timeout = copy.deepcopy(run_index)
            bad_provider_timeout["preflight"]["provider_timeout_s"] = (
                expected_timeout + 1
            )
            bad_provider_timeout["calibration"]["provider_timeout_s"] = (
                expected_timeout + 1
            )
            bad_provider_timeout["arms"]["c1_agent_s"][0]["report_sha256"] = (
                hashlib.sha256(report_path.read_bytes()).hexdigest()
            )
            provider_timeout_summary = p5_aggregate.aggregate(
                bad_provider_timeout,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )
            self.assertFalse(provider_timeout_summary["complete"])
            provider_timeout_reasons = provider_timeout_summary[
                "c1_cost_accuracy"
            ]["incomplete_reasons"]
            for expected_reason in (
                "formal C1 paid-model preflight gate missing or failed",
                "run-level 10-backend c1-calibration-v4 gate missing or failed",
                "sidecar provider_timeout_s mismatch",
                "sidecar invocation.provider_timeout_s mismatch",
                "report provider_timeout_s runtime contract mismatch",
                "Agent-S runtime metadata provider_timeout_s mismatch",
                "report provider_timeout_s provenance mismatch",
            ):
                self.assertTrue(
                    any(expected_reason in reason for reason in provider_timeout_reasons),
                    (expected_reason, provider_timeout_reasons),
                )
            report_path.write_bytes(original)
            sidecar_path.write_bytes(original_sidecar)

            timeout_entry = run_index["arms"]["c1_agent_s"][0]
            timeout_report_path = Path(timeout_entry["report"])
            original_timeout_report = timeout_report_path.read_bytes()
            timeout_report = json.loads(original_timeout_report)
            del timeout_report["policy_runtime_metadata"]["execution_backend"]
            timeout_report_path.write_text(
                json.dumps(timeout_report, sort_keys=True), encoding="utf-8"
            )
            bad_timeout = json.loads(json.dumps(run_index))
            bad_timeout["arms"]["c1_agent_s"][0]["report_sha256"] = (
                hashlib.sha256(timeout_report_path.read_bytes()).hexdigest()
            )
            timeout_summary = p5_aggregate.aggregate(
                bad_timeout,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )
            self.assertFalse(timeout_summary["complete"])
            self.assertTrue(
                any(
                    "background policy execution_backend does not match protocol"
                    in reason
                    for reason in timeout_summary["c1_cost_accuracy"][
                        "incomplete_reasons"
                    ]
                )
            )
            timeout_report_path.write_bytes(original_timeout_report)

            missing_status = copy.deepcopy(run_index)
            missing_status["arms"]["c1_state"][0].pop("status")
            missing_status_summary = p5_aggregate.aggregate(
                missing_status,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )
            self.assertFalse(missing_status_summary["complete"])
            self.assertTrue(any(
                "original trial tuple is not complete" in reason
                or "run status None" in reason
                for reason in missing_status_summary["c1_cost_accuracy"][
                    "incomplete_reasons"
                ]
            ))

            fabricated_original = copy.deepcopy(run_index)
            fabricated_row = fabricated_original["arms"]["c1_state"][0]
            fabricated_row["imported"] = True
            fabricated_row["replacement_for"] = {"fabricated": True}
            fabricated_row["source_provenance"] = {"fabricated": True}
            fabricated_summary = p5_aggregate.aggregate(
                fabricated_original,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )
            self.assertFalse(fabricated_summary["complete"])
            self.assertTrue(any(
                "original trial tuple has recovery provenance" in reason
                for reason in fabricated_summary["c1_cost_accuracy"][
                    "incomplete_reasons"
                ]
            ))

            malformed_bytes = b"{not-json"
            report_path.write_bytes(malformed_bytes)
            malformed_index = copy.deepcopy(run_index)
            malformed_index["arms"]["c1_state"][0]["report_sha256"] = (
                hashlib.sha256(malformed_bytes).hexdigest()
            )
            unreadable_summary = p5_aggregate.aggregate(
                malformed_index,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )
            self.assertFalse(unreadable_summary["complete"])
            self.assertTrue(any(
                "unreadable report JSON" in reason
                for reason in unreadable_summary["c1_cost_accuracy"][
                    "incomplete_reasons"
                ]
            ))

            wrong_shape = b"[]"
            report_path.write_bytes(wrong_shape)
            wrong_shape_index = copy.deepcopy(run_index)
            wrong_shape_index["arms"]["c1_state"][0]["report_sha256"] = (
                hashlib.sha256(wrong_shape).hexdigest()
            )
            wrong_shape_summary = p5_aggregate.aggregate(
                wrong_shape_index,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )
            self.assertFalse(wrong_shape_summary["complete"])
            self.assertTrue(any(
                "malformed report JSON: expected an object" in reason
                for reason in wrong_shape_summary["c1_cost_accuracy"][
                    "incomplete_reasons"
                ]
            ))
            report_path.write_bytes(original)

            bad_provenance = json.loads(json.dumps(run_index))
            bad_provenance["harness_hash"] = "0" * 64
            self.assertFalse(p5_aggregate.aggregate(
                bad_provenance,
                manifest=manifest,
                report_root=ROOT,
                rng_seed=7,
                iters=2,
            )["complete"])


if __name__ == "__main__":
    unittest.main()
