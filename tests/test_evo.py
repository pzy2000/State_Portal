from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from warptest import run_context
from warptest.cli import main
from warptest.evo import (
    CandidateEvaluation,
    CORE_BENCHMARKS,
    MemoryEntry,
    ValidationDiagnosis,
    admit_candidate,
    build_repair_prompt,
    diagnose_report,
    evolution_operator_registry,
    filter_evo_tasks,
    filter_phase1_tasks,
    hypothesis_from_method_checkpoint,
    initial_hypotheses_from_checkpoint,
    load_evo_split,
    load_evo_memory,
    memory_entries_from_evaluation,
    memory_entry_from_evaluation,
    FormalC2IncompleteError,
    ProviderOutageIncompleteError,
    ProviderConfigurationIncompleteError,
    rank_candidate,
    retrieve_memory,
    run_evo_synthesis_benchmark,
    run_phase1_suite,
    run_phase3_suite,
    UsageAccountingIncompleteError,
)
from warptest.models import PhaseResult
from warptest.task_schema import MethodCheckpoint, load_public_issues
from warptest.synthesis import (
    ExploratoryLLMCheckpointSynthesisAgent,
    SynthesisError,
    normalize_llm_checkpoint_mapping,
    synthesize_spec_from_public_issue,
)


ROOT = Path(__file__).resolve().parents[1]


def valid_2048_spec() -> dict:
    return {
        "name": "Evo candidate",
        "benchmark": "2048",
        "target": {"kind": "board", "board": [[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]], "score": 0, "bestScore": 0},
        "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
        "actions": [{"type": "move", "direction": "left", "wait_ms": 450}],
        "assertions": [{"type": "tile_value_present", "value": 4}],
    }


def method_checkpoint(*, spec: dict | None = None, used_public_scaffold: bool = False) -> MethodCheckpoint:
    model_spec = spec or valid_2048_spec()
    return MethodCheckpoint(
        task_id="2048-merge-left",
        benchmark="2048",
        generated_spec=model_spec,
        source_evidence=[{"source_path": "games/2048/js/game_manager.js", "matched_symbol": "GameManager", "rationale": "public source"}],
        payload={"kind": "localStorage", "strategy": "localStorage_restore_reload"},
        validity_class="save-restorable",
        runtime_validation={"status": "pending"},
        inputs_read=[str(ROOT / "benchmarks/core/public/tasks.yaml"), str(ROOT / "games/2048/js/game_manager.js")],
        generated_by="test-model",
        model_generated_spec=model_spec,
        used_public_scaffold=used_public_scaffold,
    )


def method_checkpoint_for_task(task, spec: dict) -> MethodCheckpoint:
    return MethodCheckpoint(
        task_id=task.id,
        benchmark=task.benchmark,
        generated_spec=spec,
        source_evidence=[{"source_path": str(Path(task.repo) / "public"), "matched_symbol": task.benchmark, "rationale": "public contract"}],
        payload={"kind": "checkpoint", "strategy": spec.get("checkpoint", {}).get("allowed_strategies", ["unknown"])[0]},
        validity_class=spec.get("checkpoint", {}).get("expected_validity", "unknown"),
        runtime_validation={"status": "pending"},
        inputs_read=[str(ROOT / "benchmarks/core/public/tasks.yaml")],
        generated_by="test-model",
        model_generated_spec=spec,
        used_public_scaffold=False,
    )


def fake_report(
    *,
    target: bool = True,
    validation: str = "success",
    smoke: str = "success",
    public_assertions: str = "success",
    persistence: dict | None = None,
    render: dict | None = None,
    post: dict | None = None,
) -> SimpleNamespace:
    report_dict = {
        "target_validated_before_smoke": target,
        "checkpoint_restoration": {"status": "success", "detail": ""},
        "validation": {"status": validation, "detail": ""},
        "smoke_execution": {"status": smoke, "detail": ""},
        "oracle_result": {"status": public_assertions, "detail": ""},
        "visual_oracle_result": {"status": "skipped", "detail": ""},
        "overall_status": "success" if target and validation == "success" and smoke == "success" and public_assertions == "success" else "failure",
        "memory_persistence_diff": persistence or {},
        "render_readiness_signal": render or {},
        "soft_lock_signal": {},
        "post_checkpoint_interaction": post or {},
        "console_errors": [],
        "network_api_calls": {},
        "hook_availability": {"available": True},
        "screenshots": [],
        "metrics": {},
    }
    return SimpleNamespace(
        target_validated_before_smoke=report_dict["target_validated_before_smoke"],
        checkpoint_restoration=PhaseResult(**report_dict["checkpoint_restoration"]),
        validation=PhaseResult(**report_dict["validation"]),
        smoke_execution=PhaseResult(**report_dict["smoke_execution"]),
        oracle_result=PhaseResult(**report_dict["oracle_result"]),
        visual_oracle_result=PhaseResult(**report_dict["visual_oracle_result"]),
        overall_status=report_dict["overall_status"],
        memory_persistence_diff=report_dict["memory_persistence_diff"],
        render_readiness_signal=report_dict["render_readiness_signal"],
        soft_lock_signal=report_dict["soft_lock_signal"],
        post_checkpoint_interaction=report_dict["post_checkpoint_interaction"],
        console_errors=[],
        network_api_calls={},
        hook_availability={"available": True},
        screenshots=[],
        metrics={},
        to_dict=lambda: report_dict,
    )


class EvoPortalTests(unittest.TestCase):
    def test_phase1_filter_selects_browser_web_subset(self) -> None:
        tasks = load_public_issues(ROOT)
        selected = filter_phase1_tasks(tasks)
        self.assertEqual(len(selected), 15)
        self.assertEqual({task.benchmark for task in selected}, {"2048", "SpaceRPG", "DatingSim"})

    def test_all_core_scope_selects_current_76_tasks(self) -> None:
        tasks = load_public_issues(ROOT)
        selected = filter_evo_tasks(tasks, task_scope="all-core")
        self.assertEqual(len(selected), 76)
        self.assertEqual({task.benchmark for task in selected}, set(CORE_BENCHMARKS))

    def test_explicit_task_ids_preserve_order_and_fail_closed(self) -> None:
        tasks = load_public_issues(ROOT)
        requested = ["blender-scene-cube-material", "2048-merge-left"]
        selected = filter_evo_tasks(
            tasks,
            task_scope="all-core",
            task_ids=requested,
        )
        self.assertEqual([task.id for task in selected], requested)
        with self.assertRaisesRegex(ValueError, "missing from the selected benchmark scope"):
            filter_evo_tasks(
                tasks,
                task_scope="all-core",
                task_ids=["not-a-real-evo-task"],
            )
        with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
            filter_evo_tasks(
                tasks,
                task_scope="all-core",
                task_ids=["2048-merge-left", "2048-merge-left"],
            )

    def test_phase3_split_covers_all_core_without_overlap(self) -> None:
        tasks = load_public_issues(ROOT)
        split = load_evo_split(ROOT / "benchmarks/core/splits/evo_phase3.yaml")
        all_ids = {task.id for task in tasks}
        self.assertEqual(set(split.train) | set(split.heldout), all_ids)
        self.assertEqual(set(split.train) & set(split.heldout), set())
        self.assertEqual(len(split.train), 44)
        self.assertEqual(len(split.heldout), 32)

    def test_all_core_public_templates_pass_evo_admission_gate(self) -> None:
        tasks = load_public_issues(ROOT)
        by_benchmark = {}
        for task in tasks:
            by_benchmark.setdefault(task.benchmark, task)
        for benchmark in CORE_BENCHMARKS:
            task = by_benchmark[benchmark]
            spec = synthesize_spec_from_public_issue(task)
            checkpoint = method_checkpoint_for_task(task, spec)
            hypothesis = hypothesis_from_method_checkpoint(checkpoint)
            self.assertEqual(admit_candidate(hypothesis, ROOT), [], benchmark)

    def test_operator_registry_exposes_phase3_gene_operators(self) -> None:
        genes = {operator.gene for operator in evolution_operator_registry()}
        self.assertEqual(
            genes,
            {"target-field", "value", "strategy", "payload", "action", "assertion", "evidence"},
        )

    def test_scaffold_fallback_candidate_is_rejected(self) -> None:
        hypothesis = hypothesis_from_method_checkpoint(method_checkpoint(used_public_scaffold=True))
        errors = admit_candidate(hypothesis, ROOT)
        self.assertTrue(any("scaffold" in error for error in errors))

    def test_public_scaffold_seed_is_not_reintroduced_as_raw_model_candidate(self) -> None:
        hypotheses = initial_hypotheses_from_checkpoint(method_checkpoint(used_public_scaffold=True))
        self.assertEqual(len(hypotheses), 1)
        self.assertTrue(hypotheses[0].used_public_scaffold)

    def test_scaffolded_seed_prioritizes_raw_model_spec_when_available(self) -> None:
        checkpoint = method_checkpoint(used_public_scaffold=True).to_dict()
        raw_spec = valid_2048_spec()
        raw_spec["actions"] = [{"type": "move", "direction": "left", "wait_ms": 100}]
        raw_spec["assertions"] = [{"type": "score_increased_by_at_least", "min_increase": 4}]
        checkpoint["model_generated_spec"] = raw_spec
        hypotheses = initial_hypotheses_from_checkpoint(checkpoint)
        self.assertGreaterEqual(len(hypotheses), 2)
        self.assertFalse(hypotheses[0].used_public_scaffold)
        self.assertEqual(hypotheses[0].spec["assertions"][0]["amount"], 4)
        self.assertTrue(hypotheses[1].used_public_scaffold)

    def test_hidden_reference_candidate_is_rejected(self) -> None:
        spec = valid_2048_spec()
        spec["target"]["leak"] = "benchmarks/core/hidden/oracles.yaml"
        hypothesis = hypothesis_from_method_checkpoint(method_checkpoint(spec=spec))
        errors = admit_candidate(hypothesis, ROOT)
        self.assertTrue(any("private grader" in error for error in errors))

    def test_report_diagnosis_labels_public_failures(self) -> None:
        self.assertEqual(diagnose_report(fake_report()).label, "success")
        self.assertEqual(diagnose_report(fake_report(target=False, validation="failure")).label, "target_state_mismatch")
        self.assertEqual(diagnose_report(fake_report(persistence={"gameState": {"memory": 1, "storage": 0}})).label, "persistence_mismatch")
        self.assertEqual(diagnose_report(fake_report(persistence={"matches": True, "memory": {"score": 0}, "persistence": {"score": 0}})).label, "success")
        self.assertEqual(diagnose_report(fake_report(render={"ready": False})).label, "render_not_ready")
        self.assertEqual(diagnose_report(fake_report(smoke="failure")).label, "smoke_action_failed")

    def test_live_llm_schema_drift_is_normalized_before_evo_admission(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        raw = method_checkpoint().to_dict()
        raw["runtime_validation"] = True
        raw["generated_spec"]["assertions"].append({"type": "score_increased_by_at_least", "min_increase": 4})
        normalized, warnings = normalize_llm_checkpoint_mapping(raw, task=task)
        self.assertEqual(normalized["runtime_validation"]["status"], "pending")
        self.assertIn("runtime_validation_scalar_normalized_to_pending_mapping", warnings)
        self.assertTrue(
            any(
                assertion.get("type") == "score_increased_by_at_least" and assertion.get("amount") == 4
                for assertion in normalized["generated_spec"]["assertions"]
            )
        )

    def test_ranking_prefers_public_valid_candidate(self) -> None:
        hypothesis = hypothesis_from_method_checkpoint(method_checkpoint())
        good = rank_candidate(hypothesis, ValidationDiagnosis("success"), fake_report())
        bad = rank_candidate(hypothesis, ValidationDiagnosis("target_state_mismatch"), fake_report(target=False, validation="failure"))
        self.assertGreater(good, bad)

    def test_memory_entry_excludes_hidden_grading_fields(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        hypothesis = hypothesis_from_method_checkpoint(method_checkpoint())
        evaluation = CandidateEvaluation(
            hypothesis=hypothesis,
            admitted=True,
            executed=True,
            diagnosis=ValidationDiagnosis("success"),
            rank=rank_candidate(hypothesis, ValidationDiagnosis("success"), fake_report()),
            checkpoint=hypothesis.materialize_checkpoint(),
            runtime_report={
                "validation": {"status": "success"},
                "smoke_execution": {"status": "success"},
                "oracle_result": {"status": "success"},
                "target_validated_before_smoke": True,
                "strict": {"status": "success"},
                "hidden_assertions_covered": True,
            },
        )
        entry = memory_entry_from_evaluation(task, evaluation).to_dict()
        rendered = str(entry).lower()
        self.assertNotIn("hidden_assertions_covered", rendered)
        self.assertNotIn("'strict'", rendered)
        self.assertIn("public_validation_summary", entry)
        self.assertEqual(entry["entry_type"], "validated_recipe")

    def test_negative_memory_entry_is_sanitized_and_typed(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        hypothesis = hypothesis_from_method_checkpoint(method_checkpoint())
        evaluation = CandidateEvaluation(
            hypothesis=hypothesis,
            admitted=True,
            executed=True,
            diagnosis=ValidationDiagnosis("target_state_mismatch"),
            rank=rank_candidate(hypothesis, ValidationDiagnosis("target_state_mismatch"), fake_report(target=False, validation="failure")),
            checkpoint=hypothesis.materialize_checkpoint(),
            runtime_report={
                "validation": {"status": "failure"},
                "smoke_execution": {"status": "success"},
                "oracle_result": {"status": "success"},
                "target_validated_before_smoke": False,
                "strict": {"status": "success"},
                "hidden_assertions_covered": True,
            },
        )
        entry = memory_entry_from_evaluation(task, evaluation).to_dict()
        rendered = str(entry).lower()
        self.assertEqual(entry["entry_type"], "negative_contrast")
        self.assertNotIn("hidden_assertions_covered", rendered)
        self.assertNotIn("'strict'", rendered)

    def test_memory_v2_policies_write_expected_entry_kinds(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        hypothesis = hypothesis_from_method_checkpoint(method_checkpoint())
        negative = CandidateEvaluation(
            hypothesis=hypothesis,
            admitted=True,
            executed=True,
            diagnosis=ValidationDiagnosis("target_state_mismatch"),
            rank=rank_candidate(hypothesis, ValidationDiagnosis("target_state_mismatch"), fake_report(target=False, validation="failure")),
            checkpoint=hypothesis.materialize_checkpoint(),
            runtime_report={"validation": {"status": "failure"}, "smoke_execution": {"status": "success"}, "oracle_result": {"status": "success"}},
        )
        success = CandidateEvaluation(
            hypothesis=hypothesis,
            admitted=True,
            executed=True,
            diagnosis=ValidationDiagnosis("success"),
            rank=rank_candidate(hypothesis, ValidationDiagnosis("success"), fake_report()),
            checkpoint=hypothesis.materialize_checkpoint(),
            runtime_report={"validation": {"status": "success"}, "smoke_execution": {"status": "success"}, "oracle_result": {"status": "success"}},
        )
        self.assertEqual(memory_entries_from_evaluation(task, negative, policy="positive-only"), [])
        self.assertEqual(memory_entries_from_evaluation(task, success, policy="positive-only")[0].entry_type, "validated_recipe")
        textual = memory_entries_from_evaluation(task, negative, policy="textual-only")[0]
        self.assertEqual(textual.entry_type, "textual_summary")
        self.assertEqual(textual.checkpoint_recipe, {})
        self.assertIn("target_state_mismatch", textual.text_summary)

    def test_memory_loading_redacts_private_fields(self) -> None:
        payload = {
            "entries": [
                {
                    "id": "mem-private",
                    "scope": "repository",
                    "benchmark": "2048",
                    "semantic_surface": "board",
                    "issue_keywords": ["merge", "left"],
                    "strategy": "localStorage_restore_reload",
                    "failure_signature": "success",
                    "checkpoint_recipe": {
                        "target": {"kind": "board"},
                        "hidden_spec": "benchmarks/core/hidden/oracles.yaml",
                        "notes": "benchmarks/core/hidden/oracles.yaml",
                    },
                    "public_validation_summary": {"validation": "success", "strict": {"status": "success"}},
                    "source_symbols": ["GameManager"],
                    "entry_type": "validated_recipe",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            path.write_text(str(payload), encoding="utf-8")
            loaded = [entry.to_dict() for entry in load_evo_memory(path)]
        rendered = str(loaded).lower()
        self.assertNotIn("benchmarks/core/hidden", rendered)
        self.assertNotIn("hidden_spec", rendered)
        self.assertNotIn("'strict'", rendered)

    def test_bm25_memory_retrieval_prefers_matching_schema_keys(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        matching = MemoryEntry(
            id="mem-match",
            scope="repository",
            benchmark="2048",
            semantic_surface="board",
            issue_keywords=["merge", "left", "tile"],
            strategy="localStorage_restore_reload",
            failure_signature="success",
            checkpoint_recipe={"assertions": [{"type": "tile_value_present", "value": 4}]},
            public_validation_summary={"diagnosis": "success"},
            source_symbols=["GameManager"],
            entry_type="validated_recipe",
            confidence=0.9,
        )
        weak = MemoryEntry(
            id="mem-weak",
            scope="repository",
            benchmark="2048",
            semantic_surface="menu",
            issue_keywords=["settings"],
            strategy="unknown",
            failure_signature="runtime_error",
            checkpoint_recipe={},
            public_validation_summary={"diagnosis": "runtime_error"},
            source_symbols=[],
            entry_type="negative_contrast",
            confidence=0.1,
        )
        other_benchmark = MemoryEntry(
            id="mem-other",
            scope="repository",
            benchmark="DatingSim",
            semantic_surface="dialog",
            issue_keywords=["merge", "left"],
            strategy="zustand_checkpoint_restore",
            failure_signature="success",
            checkpoint_recipe={},
            public_validation_summary={"diagnosis": "success"},
            source_symbols=[],
            entry_type="validated_recipe",
        )
        retrieved = retrieve_memory([weak, other_benchmark, matching], task, limit=2)
        self.assertEqual([entry.id for entry in retrieved], ["mem-match", "mem-weak"])

    def test_repair_prompt_redacts_private_runtime_and_memory_fields(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        hypothesis = hypothesis_from_method_checkpoint(method_checkpoint())
        evaluation = CandidateEvaluation(
            hypothesis=hypothesis,
            admitted=True,
            executed=True,
            diagnosis=ValidationDiagnosis("target_state_mismatch", "public mismatch"),
            rank=rank_candidate(hypothesis, ValidationDiagnosis("target_state_mismatch"), fake_report(target=False, validation="failure")),
            checkpoint=hypothesis.materialize_checkpoint(),
            runtime_report={
                "validation": {"status": "failure", "detail": "public mismatch"},
                "strict": {"status": "success"},
                "hidden_assertions_covered": True,
                "console_errors": ["benchmarks/core/hidden/oracles.yaml should not leak"],
            },
        )
        memory = [
            MemoryEntry(
                id="mem-leaky",
                scope="repository",
                benchmark="2048",
                semantic_surface="board",
                issue_keywords=["merge"],
                strategy="localStorage_restore_reload",
                failure_signature="target_state_mismatch",
                checkpoint_recipe={"notes": "benchmarks/core/hidden/oracles.yaml"},
                public_validation_summary={"diagnosis": "target_state_mismatch", "strict": "success"},
                source_symbols=["GameManager"],
                entry_type="negative_contrast",
            )
        ]
        prompt = build_repair_prompt(task, evaluation, memory)
        self.assertIn("target_state_mismatch", prompt)
        self.assertIn("public mismatch", prompt)
        self.assertNotIn("benchmarks/core/hidden", prompt)
        self.assertNotIn("hidden_assertions_covered", prompt)
        self.assertNotIn("strict:\n", prompt)

    def test_mocked_evo_runner_writes_reports_without_browser_or_llm(self) -> None:
        class FakeAgent:
            name = "fake"

            def synthesize(self, task, root=ROOT, artifact_dir=None):
                return method_checkpoint()

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("warptest.evo.build_synthesis_agent", return_value=FakeAgent()),
                patch("warptest.evo.run_spec", return_value=fake_report()) as run_mock,
            ):
                result = run_evo_synthesis_benchmark(
                    ROOT,
                    agent_kind="deterministic",
                    benchmarks=["2048"],
                    task_limit=1,
                    iterations=1,
                    population=1,
                    report_dir=Path(tmp),
                )
            self.assertTrue((Path(tmp) / "evo-synthesis.json").exists())
            self.assertTrue((Path(tmp) / "evo-synthesis.md").exists())
            self.assertTrue((Path(tmp) / "evo-memory.json").exists())
            self.assertTrue((Path(tmp) / "phase1-browser-web-live.json").exists())
            self.assertTrue((Path(tmp) / "phase1-browser-web-live.md").exists())
            self.assertEqual(result["summary"]["total_tasks"], 1)
            self.assertEqual(result["summary"]["public_validation_success"], 1)
            self.assertEqual(len(result["memory"]["entries"]), 1)
            self.assertTrue(run_mock.called)

    def test_formal_provider_outage_keeps_checkpoint_and_aborts_without_score(self) -> None:
        """A formal C2 phase must remain resumable but never score a gateway outage."""

        class OutageAgent:
            name = "outage"

            def synthesize(self, *_args, **_kwargs):
                raise SynthesisError(
                    "LLM gateway unavailable",
                    artifacts={"failure_mode": "gateway_outage"},
                )

        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "phase"
            with patch("warptest.evo.build_synthesis_agent", return_value=OutageAgent()):
                with self.assertRaises(ProviderOutageIncompleteError) as raised:
                    run_evo_synthesis_benchmark(
                        ROOT,
                        agent_kind="deterministic",
                        benchmarks=["2048"],
                        task_limit=1,
                        iterations=1,
                        population=1,
                        provider_retries=0,
                        fail_on_provider_outage=True,
                        report_dir=report_dir,
                    )

            self.assertIn("temporarily unavailable", str(raised.exception))
            self.assertEqual(run_context.error_category(raised.exception), "infra")
            progress = json.loads((report_dir / "evo-progress.json").read_text(encoding="utf-8"))
            self.assertFalse(progress["complete"])
            self.assertEqual(progress["completed_task_ids"], [])
            self.assertFalse((report_dir / "evo-synthesis.json").exists())

    def test_formal_usage_accounting_incomplete_aborts_without_score_or_replay(self) -> None:
        """Paid but unauditable synthesis must not become a zero-score task."""

        class IncompleteUsageAgent:
            name = "incomplete-usage"

            def synthesize(self, *_args, **_kwargs):
                raise SynthesisError(
                    "checkpoint usage omitted reasoning tokens",
                    artifacts={"failure_mode": "usage_accounting_incomplete"},
                )

        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "phase"
            with patch(
                "warptest.evo.build_synthesis_agent",
                return_value=IncompleteUsageAgent(),
            ):
                with self.assertRaises(UsageAccountingIncompleteError) as raised:
                    run_evo_synthesis_benchmark(
                        ROOT,
                        agent_kind="deterministic",
                        benchmarks=["2048"],
                        task_limit=1,
                        iterations=1,
                        population=1,
                        provider_retries=0,
                        fail_on_provider_outage=True,
                        report_dir=report_dir,
                    )

            self.assertIsInstance(raised.exception, FormalC2IncompleteError)
            self.assertIn("usage accounting is incomplete", str(raised.exception))
            self.assertIsNone(run_context.error_category(raised.exception))
            progress = json.loads(
                (report_dir / "evo-progress.json").read_text(encoding="utf-8")
            )
            self.assertFalse(progress["complete"])
            self.assertEqual(progress["completed_task_ids"], [])
            self.assertFalse((report_dir / "evo-synthesis.json").exists())

    def test_real_exploration_agent_preserves_structured_provider_failures(self) -> None:
        """The real agent must not turn route failures into public scaffolds."""

        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")

        class FailingClient:
            def __init__(self, failure_mode: str) -> None:
                self.failure_mode = failure_mode

            def converse(self, *_args, **_kwargs):
                raise SynthesisError(
                    f"structured {self.failure_mode}",
                    artifacts={"failure_mode": self.failure_mode},
                )

        for failure_mode in ("gateway_outage", "client_error"):
            with self.subTest(failure_mode=failure_mode):
                agent = ExploratoryLLMCheckpointSynthesisAgent(
                    model="model",
                    client=FailingClient(failure_mode),
                    max_steps=1,
                )
                with self.assertRaises(SynthesisError) as raised:
                    agent.synthesize(task, ROOT)
                self.assertEqual(
                    raised.exception.artifacts.get("failure_mode"), failure_mode
                )

    def test_formal_client_error_is_nonretryable_incomplete(self) -> None:
        class RejectedAgent:
            name = "rejected"

            def synthesize(self, *_args, **_kwargs):
                raise SynthesisError(
                    "configured request rejected",
                    artifacts={"failure_mode": "client_error", "status_code": 400},
                )

        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "phase"
            with patch("warptest.evo.build_synthesis_agent", return_value=RejectedAgent()):
                with self.assertRaises(ProviderConfigurationIncompleteError) as raised:
                    run_evo_synthesis_benchmark(
                        ROOT,
                        agent_kind="deterministic",
                        benchmarks=["2048"],
                        task_limit=1,
                        iterations=1,
                        population=1,
                        provider_retries=0,
                        fail_on_provider_outage=True,
                        report_dir=report_dir,
                    )
            self.assertIsNone(run_context.error_category(raised.exception))
            progress = json.loads(
                (report_dir / "evo-progress.json").read_text(encoding="utf-8")
            )
            self.assertFalse(progress["complete"])
            self.assertEqual(progress["completed_task_ids"], [])

    def test_evo_phase_resumes_from_each_atomic_task_checkpoint(self) -> None:
        """An interruption must retain the completed task prefix exactly once."""

        def completed(task_id: str, benchmark: str) -> dict:
            return {
                "task_id": task_id,
                "benchmark": benchmark,
                "memory_events": [],
                "memory_context": [],
                "memory_context_by_generation": [],
                "candidates": [],
                "provider": {},
                "final_evaluation": None,
                "final_grading": {
                    "strict": {"status": "failure"},
                    "scaffold": {"status": "failure"},
                },
                "duration_ms": 1,
            }

        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "phase"
            interrupted_calls: list[str] = []

            def interrupted(*args, **_kwargs):
                task = args[1]
                interrupted_calls.append(task.id)
                if len(interrupted_calls) == 2:
                    raise KeyboardInterrupt("simulated process interruption")
                return completed(task.id, task.benchmark)

            with (
                patch("warptest.evo.build_synthesis_agent", return_value=object()),
                patch("warptest.evo._run_evo_task", side_effect=interrupted),
                self.assertRaisesRegex(KeyboardInterrupt, "simulated process interruption"),
            ):
                run_evo_synthesis_benchmark(
                    ROOT,
                    agent_kind="deterministic",
                    benchmarks=["2048"],
                    task_limit=2,
                    iterations=1,
                    population=1,
                    report_dir=report_dir,
                )

            progress = json.loads((report_dir / "evo-progress.json").read_text(encoding="utf-8"))
            self.assertFalse(progress["complete"])
            self.assertEqual(progress["completed_task_ids"], [interrupted_calls[0]])
            self.assertFalse((report_dir / "evo-synthesis.json").exists())

            resumed_calls: list[str] = []

            def resumed(*args, **_kwargs):
                task = args[1]
                resumed_calls.append(task.id)
                return completed(task.id, task.benchmark)

            with (
                patch("warptest.evo.build_synthesis_agent", return_value=object()),
                patch("warptest.evo._run_evo_task", side_effect=resumed),
            ):
                result = run_evo_synthesis_benchmark(
                    ROOT,
                    agent_kind="deterministic",
                    benchmarks=["2048"],
                    task_limit=2,
                    iterations=1,
                    population=1,
                    report_dir=report_dir,
                    resume=True,
                )

            self.assertEqual(resumed_calls, [interrupted_calls[1]])
            self.assertEqual(result["resumed_task_count"], 1)
            self.assertEqual([row["task_id"] for row in result["results"]], interrupted_calls)
            finished = json.loads((report_dir / "evo-progress.json").read_text(encoding="utf-8"))
            self.assertTrue(finished["complete"])
            self.assertTrue((report_dir / "evo-synthesis.json").exists())

    def test_partial_heldout_resume_rejects_changed_frozen_memory(self) -> None:
        """A task prefix cannot be mixed with a replaced memory treatment."""

        def completed(task_id: str, benchmark: str) -> dict:
            return {
                "task_id": task_id,
                "benchmark": benchmark,
                "memory_events": [],
                "memory_context": [],
                "memory_context_by_generation": [],
                "candidates": [],
                "provider": {},
                "final_evaluation": None,
                "final_grading": {
                    "strict": {"status": "failure"},
                    "scaffold": {"status": "failure"},
                },
                "duration_ms": 1,
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_dir = root / "heldout"
            memory_path = root / "frozen-train-memory.json"
            memory_path.write_text(
                json.dumps({"entries": [{"id": "memory-a", "benchmark": "2048"}]}),
                encoding="utf-8",
            )
            calls = 0

            def interrupted(*args, **_kwargs):
                nonlocal calls
                calls += 1
                task = args[1]
                if calls == 2:
                    raise KeyboardInterrupt("simulated heldout interruption")
                return completed(task.id, task.benchmark)

            with (
                patch("warptest.evo.build_synthesis_agent", return_value=object()),
                patch("warptest.evo._run_evo_task", side_effect=interrupted),
                self.assertRaisesRegex(
                    KeyboardInterrupt, "simulated heldout interruption"
                ),
            ):
                run_evo_synthesis_benchmark(
                    ROOT,
                    agent_kind="deterministic",
                    benchmarks=["2048"],
                    task_limit=2,
                    iterations=1,
                    population=1,
                    memory_mode="repository",
                    memory_in=memory_path,
                    report_dir=report_dir,
                )

            memory_path.write_text(
                json.dumps({"entries": [{"id": "memory-b", "benchmark": "2048"}]}),
                encoding="utf-8",
            )
            with (
                patch("warptest.evo.build_synthesis_agent", return_value=object()),
                patch("warptest.evo._run_evo_task") as run_task,
                self.assertRaisesRegex(ValueError, "changed settings"),
            ):
                run_evo_synthesis_benchmark(
                    ROOT,
                    agent_kind="deterministic",
                    benchmarks=["2048"],
                    task_limit=2,
                    iterations=1,
                    population=1,
                    memory_mode="repository",
                    memory_in=memory_path,
                    report_dir=report_dir,
                    resume=True,
                )
            self.assertFalse(run_task.called)

    def test_episode_memory_is_retrieved_for_next_generation(self) -> None:
        class FakeAgent:
            name = "fake"

            def synthesize(self, task, root=ROOT, artifact_dir=None):
                return method_checkpoint()

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("warptest.evo.build_synthesis_agent", return_value=FakeAgent()),
                patch("warptest.evo.run_spec", return_value=fake_report()),
            ):
                result = run_evo_synthesis_benchmark(
                    ROOT,
                    agent_kind="deterministic",
                    benchmarks=["2048"],
                    task_limit=1,
                    iterations=2,
                    population=1,
                    memory_mode="episode",
                    report_dir=Path(tmp),
                )
        generation_memory = result["results"][0]["memory_context_by_generation"]
        self.assertEqual(generation_memory[0]["generation"], 1)
        self.assertGreaterEqual(len(generation_memory[0]["entries"]), 1)
        self.assertEqual(result["summary"]["memory_context_tasks"], 1)

    def test_provider_empty_response_retries_are_accounted_separately(self) -> None:
        class FakeAgent:
            name = "fake"

            def __init__(self) -> None:
                self.calls = 0

            def synthesize(self, task, root=ROOT, artifact_dir=None):
                self.calls += 1
                if self.calls == 1:
                    return MethodCheckpoint(
                        task_id="2048-merge-left",
                        benchmark="2048",
                        generated_spec=valid_2048_spec(),
                        source_evidence=[{"source_path": "games/2048/js/game_manager.js", "matched_symbol": "GameManager"}],
                        payload={"kind": "localStorage", "strategy": "localStorage_restore_reload"},
                        validity_class="save-restorable",
                        runtime_validation={"status": "failure", "detail": "Exploration agent did not produce a FINAL checkpoint within max_steps."},
                        inputs_read=[str(ROOT / "benchmarks/core/public/tasks.yaml")],
                        generated_by="fake",
                        model_generated_spec=None,
                        used_public_scaffold=True,
                    )
                return method_checkpoint()

        fake_agent = FakeAgent()
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("warptest.evo.build_synthesis_agent", return_value=fake_agent),
                patch("warptest.evo.run_spec", return_value=fake_report()),
            ):
                result = run_evo_synthesis_benchmark(
                    ROOT,
                    agent_kind="deterministic",
                    benchmarks=["2048"],
                    task_limit=1,
                    iterations=1,
                    population=1,
                    provider_retries=1,
                    report_dir=Path(tmp),
                )
        provider = result["results"][0]["provider"]
        self.assertEqual(fake_agent.calls, 2)
        self.assertEqual(provider["synthesis_attempts"], 2)
        self.assertEqual(provider["provider_failures"], 1)
        self.assertEqual(result["summary"]["fallback_retries"], 1)

    def test_run_spec_exception_becomes_candidate_runtime_error(self) -> None:
        class FakeAgent:
            name = "fake"

            def synthesize(self, task, root=ROOT, artifact_dir=None):
                return method_checkpoint()

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("warptest.evo.build_synthesis_agent", return_value=FakeAgent()),
                patch("warptest.evo.run_spec", side_effect=RuntimeError("Page.goto: Timeout 45000ms exceeded.")),
            ):
                result = run_evo_synthesis_benchmark(
                    ROOT,
                    agent_kind="deterministic",
                    benchmarks=["2048"],
                    task_limit=1,
                    iterations=1,
                    population=1,
                    report_dir=Path(tmp),
                )
        candidate = result["results"][0]["candidates"][0]
        self.assertEqual(candidate["diagnosis"]["label"], "runtime_error")
        self.assertEqual(candidate["runtime_report"]["metrics"]["run_spec_exception"], "RuntimeError")
        self.assertEqual(result["summary"]["diagnoses"]["runtime_error"], 1)
        self.assertEqual(result["summary"]["candidates_executed"], 1)

    def test_llm_explore_seed_falls_back_to_no_explore_after_retryable_scaffold(self) -> None:
        class ExploreAgent:
            name = "explore"

            def synthesize(self, task, root=ROOT, artifact_dir=None):
                return MethodCheckpoint(
                    task_id="2048-merge-left",
                    benchmark="2048",
                    generated_spec=valid_2048_spec(),
                    source_evidence=[{"source_path": "games/2048/js/game_manager.js", "matched_symbol": "GameManager"}],
                    payload={"kind": "localStorage", "strategy": "localStorage_restore_reload"},
                    validity_class="save-restorable",
                    runtime_validation={"status": "failure", "detail": "Exploration agent did not produce a FINAL checkpoint within max_steps."},
                    inputs_read=[str(ROOT / "benchmarks/core/public/tasks.yaml")],
                    generated_by="explore",
                    model_generated_spec=None,
                    used_public_scaffold=True,
                )

        class NoExploreAgent:
            name = "no-explore"

            def synthesize(self, task, root=ROOT, artifact_dir=None):
                return method_checkpoint()

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict("os.environ", {"MODELSCOPE_API_KEY": "test-key"}),
                patch("warptest.evo.build_synthesis_agent", side_effect=[ExploreAgent(), NoExploreAgent()]),
                patch("warptest.evo.run_spec", return_value=fake_report()),
            ):
                result = run_evo_synthesis_benchmark(
                    ROOT,
                    agent_kind="llm",
                    benchmarks=["2048"],
                    task_limit=1,
                    iterations=1,
                    population=1,
                    provider_retries=0,
                    report_dir=Path(tmp),
                )
        provider = result["results"][0]["provider"]
        self.assertTrue(provider["fallback_seed_agent_used"])
        self.assertEqual(provider["synthesis_attempts"], 2)
        self.assertEqual(result["summary"]["public_validation_success"], 1)

    def test_scaffold_only_runner_has_no_final_hidden_grade(self) -> None:
        class FakeScaffoldAgent:
            name = "fake-scaffold"

            def synthesize(self, task, root=ROOT, artifact_dir=None):
                return method_checkpoint(used_public_scaffold=True)

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("warptest.evo.build_synthesis_agent", return_value=FakeScaffoldAgent()),
                patch("warptest.evo.run_spec") as run_mock,
            ):
                result = run_evo_synthesis_benchmark(
                    ROOT,
                    agent_kind="deterministic",
                    benchmarks=["2048"],
                    task_limit=1,
                    iterations=1,
                    population=1,
                    report_dir=Path(tmp),
                )
        self.assertFalse(run_mock.called)
        self.assertIsNone(result["results"][0]["final_evaluation"])
        self.assertEqual(result["summary"]["strict_success"], 0)
        self.assertEqual(result["summary"]["scaffold_rejected"], 1)

    def test_scaffold_only_runner_is_not_mutated_in_later_generations(self) -> None:
        class FakeScaffoldAgent:
            name = "fake-scaffold"

            def synthesize(self, task, root=ROOT, artifact_dir=None):
                return method_checkpoint(used_public_scaffold=True)

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("warptest.evo.build_synthesis_agent", return_value=FakeScaffoldAgent()),
                patch("warptest.evo.run_spec") as run_mock,
            ):
                result = run_evo_synthesis_benchmark(
                    ROOT,
                    agent_kind="deterministic",
                    benchmarks=["2048"],
                    task_limit=1,
                    iterations=2,
                    population=2,
                    report_dir=Path(tmp),
                )
        self.assertFalse(run_mock.called)
        self.assertIsNone(result["results"][0]["final_evaluation"])
        self.assertEqual(result["summary"]["strict_success"], 0)
        self.assertEqual(result["summary"]["candidates_executed"], 0)
        self.assertEqual(result["summary"]["scaffold_rejected"], len(result["results"][0]["candidates"]))

    def test_phase1_suite_aggregates_ablation_variants(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_run(root, *, report_dir=None, **kwargs):
            calls.append(dict(kwargs))
            return {
                "report_dir": str(report_dir),
                "duration_ms": 7,
                "summary": {
                    "total_tasks": 1,
                    "public_validation_success": 1,
                    "strict_success": 1,
                    "scaffold_success": 0,
                    "schema_rejected": 0,
                    "scaffold_rejected": 0,
                    "candidate_evaluations": 1,
                    "candidates_executed": 1,
                },
            }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("warptest.evo.run_evo_synthesis_benchmark", side_effect=fake_run):
                result = run_phase1_suite(
                    ROOT,
                    benchmarks=["2048"],
                    task_limit=1,
                    iterations=2,
                    population=2,
                    include_repository_memory=True,
                    report_dir=Path(tmp),
                )
            self.assertTrue((Path(tmp) / "phase1-browser-web-live.json").exists())
            self.assertTrue((Path(tmp) / "phase1-browser-web-live.md").exists())
        self.assertEqual([variant["name"] for variant in result["variants"]], ["source_exploration_seed_only", "evolution_no_memory", "evolution_episode_memory", "evolution_repository_memory"])
        self.assertEqual([call["memory_mode"] for call in calls], ["none", "none", "episode", "repository"])
        self.assertEqual(result["summary"]["variants"][0]["public_validation_success"], 1)

    def test_phase3_suite_aggregates_train_heldout_variants(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_run(root, *, report_dir=None, **kwargs):
            calls.append(dict(kwargs))
            memory_out = kwargs.get("memory_out")
            if memory_out:
                Path(memory_out).parent.mkdir(parents=True, exist_ok=True)
                Path(memory_out).write_text('{"schema_version": 2, "entries": []}\n', encoding="utf-8")
            return {
                "report_dir": str(report_dir),
                "summary": {
                    "total_tasks": 1,
                    "public_validation_success": 1,
                    "strict_success": 1,
                    "scaffold_success": 0,
                    "candidate_evaluations": 1,
                    "candidates_executed": 1,
                    "invalid_checkpoint_rate": 0.0,
                    "repair_success_rate": {},
                    "memory_hit_utility": {},
                    "duration_ms": 1,
                },
            }

        with tempfile.TemporaryDirectory() as tmp:
            with patch("warptest.evo.run_evo_synthesis_benchmark", side_effect=fake_run):
                result = run_phase3_suite(
                    ROOT,
                    benchmarks=["2048"],
                    task_limit=1,
                    iterations=2,
                    population=2,
                    agent_kind="deterministic",
                    report_dir=Path(tmp),
                )
            self.assertTrue((Path(tmp) / "phase3-core-suite.json").exists())
            self.assertTrue((Path(tmp) / "phase3-core-suite.md").exists())
        self.assertEqual([row["id"] for row in result["variants"]], [f"A{idx}" for idx in range(1, 10)])
        self.assertEqual(len(calls), 18)
        heldout_calls = calls[1::2]
        self.assertTrue(all(call["repository_memory_writes"] is False for call in heldout_calls))
        self.assertTrue(all(call["memory_export"] is False for call in heldout_calls))
        self.assertEqual(result["summary"]["variants"][0]["heldout_strict_success"], 1)

    def test_evo_bench_cli_forwards_options(self) -> None:
        result = {"report_dir": "reports/evo", "summary": {"total_tasks": 1}}
        with (
            patch("warptest.cli.run_evo_synthesis_benchmark", return_value=result) as evo_mock,
            redirect_stdout(StringIO()),
        ):
            main(
                [
                    "evo-bench",
                    "--benchmarks",
                    "2048,SpaceRPG",
                    "--task-limit",
                    "2",
                    "--iterations",
                    "5",
                    "--population",
                    "6",
                    "--memory-mode",
                    "repository",
                    "--agent",
                    "llm",
                    "--model",
                    "fake/model",
                    "--memory-in",
                    "in.json",
                    "--memory-out",
                    "out.json",
                    "--provider-retries",
                    "3",
                    "--no-repair",
                    "--repair-max-attempts",
                    "4",
                    "--headed",
                    "--no-explore",
                ]
            )
        self.assertEqual(evo_mock.call_args.kwargs["benchmarks"], ["2048", "SpaceRPG"])
        self.assertEqual(evo_mock.call_args.kwargs["task_limit"], 2)
        self.assertEqual(evo_mock.call_args.kwargs["iterations"], 5)
        self.assertEqual(evo_mock.call_args.kwargs["population"], 6)
        self.assertEqual(evo_mock.call_args.kwargs["memory_mode"], "repository")
        self.assertEqual(evo_mock.call_args.kwargs["memory_in"], "in.json")
        self.assertEqual(evo_mock.call_args.kwargs["memory_out"], "out.json")
        self.assertEqual(evo_mock.call_args.kwargs["provider_retries"], 3)
        self.assertEqual(evo_mock.call_args.kwargs["repair"], False)
        self.assertEqual(evo_mock.call_args.kwargs["repair_max_attempts"], 4)
        self.assertEqual(evo_mock.call_args.kwargs["model"], "fake/model")
        self.assertEqual(evo_mock.call_args.kwargs["headless"], False)
        self.assertEqual(evo_mock.call_args.kwargs["explore"], False)

    def test_phase1_suite_cli_forwards_suite_options(self) -> None:
        result = {"report_dir": "reports/evo", "summary": {"variants": []}}
        with (
            patch("warptest.cli.run_phase1_suite", return_value=result) as suite_mock,
            redirect_stdout(StringIO()),
        ):
            main(
                [
                    "evo-bench",
                    "--phase1-suite",
                    "--include-repository-memory",
                    "--benchmarks",
                    "2048",
                    "--task-limit",
                    "1",
                    "--iterations",
                    "2",
                    "--population",
                    "2",
                    "--temperature",
                    "0",
                    "--model",
                    "fake/model",
                    "--provider-retries",
                    "5",
                    "--no-repair",
                ]
            )
        self.assertTrue(suite_mock.called)
        self.assertTrue(suite_mock.call_args.kwargs["include_repository_memory"])
        self.assertEqual(suite_mock.call_args.kwargs["benchmarks"], ["2048"])
        self.assertEqual(suite_mock.call_args.kwargs["task_limit"], 1)
        self.assertEqual(suite_mock.call_args.kwargs["iterations"], 2)
        self.assertEqual(suite_mock.call_args.kwargs["population"], 2)
        self.assertEqual(suite_mock.call_args.kwargs["temperature"], 0)
        self.assertEqual(suite_mock.call_args.kwargs["model"], "fake/model")
        self.assertEqual(suite_mock.call_args.kwargs["provider_retries"], 5)
        self.assertEqual(suite_mock.call_args.kwargs["repair"], False)

    def test_phase3_suite_cli_forwards_all_core_options(self) -> None:
        result = {"report_dir": "reports/evo", "summary": {"variants": []}}
        with (
            patch("warptest.cli.run_phase3_suite", return_value=result) as suite_mock,
            redirect_stdout(StringIO()),
        ):
            main(
                [
                    "evo-bench",
                    "--phase3-suite",
                    "--task-scope",
                    "all-core",
                    "--task-limit",
                    "1",
                    "--iterations",
                    "2",
                    "--population",
                    "2",
                    "--memory-policy",
                    "positive-only",
                    "--mutation-policy",
                    "random",
                    "--no-diagnosis",
                    "--agent",
                    "deterministic",
                ]
            )
        self.assertTrue(suite_mock.called)
        self.assertEqual(suite_mock.call_args.kwargs["task_scope"], "all-core")
        self.assertEqual(suite_mock.call_args.kwargs["task_limit"], 1)
        self.assertEqual(set(suite_mock.call_args.kwargs["benchmarks"]), set(CORE_BENCHMARKS))
        self.assertEqual(suite_mock.call_args.kwargs["memory_policy"], "positive-only")
        self.assertEqual(suite_mock.call_args.kwargs["mutation_policy"], "random")
        self.assertEqual(suite_mock.call_args.kwargs["diagnosis_enabled"], False)


if __name__ == "__main__":
    unittest.main()
