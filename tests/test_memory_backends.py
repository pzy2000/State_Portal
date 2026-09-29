from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from warptest import run_context
from warptest.evo import (
    EvoSplit,
    FormalC2IncompleteError,
    MemoryEntry,
    MemoryBaselineEvidenceInvalidError,
    _generation_memory_context,
    run_memory_baseline_suite,
    summarize_memory_baseline_suite,
)
from warptest.memory_backends import (
    MEMORY_BACKEND_CHOICES,
    AMemBackend,
    Mem0Backend,
    StatePortalAnchorBackend,
    StatePortalBackend,
    get_memory_backend,
)
from warptest.task_schema import PublicIssue


def entry(
    eid: str,
    benchmark: str,
    surface: str,
    strategy: str,
    keywords: list[str],
    *,
    entry_type: str = "validated_recipe",
    confidence: float = 0.9,
) -> MemoryEntry:
    return MemoryEntry(
        id=eid,
        scope="repository",
        benchmark=benchmark,
        semantic_surface=surface,
        issue_keywords=keywords,
        strategy=strategy,
        failure_signature="success" if entry_type == "validated_recipe" else "target_state_mismatch",
        checkpoint_recipe={
            "target": {"kind": surface},
            "checkpoint": {"allowed_strategies": [strategy], "expected_validity": "save-restorable"},
            "actions": [{"type": "noop"}],
            "assertions": [],
        },
        public_validation_summary={"diagnosis": "success"},
        source_symbols=[f"sym_{surface}"],
        confidence=confidence,
        entry_type=entry_type,
    )


def task(benchmark: str, issue: str, tags: list[str]) -> PublicIssue:
    return PublicIssue(id=f"{benchmark}-t", benchmark=benchmark, repo=f"games/{benchmark}", issue=issue, tags=tags)


def sample_entries() -> list[MemoryEntry]:
    return [
        entry("m1", "luanti", "node", "luanti_worldmod_state_validate", ["voxel", "node", "build"]),
        entry("m2", "luanti", "node", "luanti_worldmod_state_validate", ["voxel", "node", "place"]),
        entry("m3", "spacerpg", "station", "save_restore", ["station", "dock", "save"]),
        entry("m4", "luanti", "inventory", "luanti_worldmod_state_validate", ["inventory", "item"]),
        entry("m5", "spacerpg", "node", "luanti_worldmod_state_validate", ["voxel", "node", "build"]),
    ]


class FactoryTests(unittest.TestCase):
    def test_choices_and_types(self):
        self.assertEqual(
            MEMORY_BACKEND_CHOICES,
            ("stateportal", "amem", "mem0", "stateportal-anchor"),
        )
        self.assertIsInstance(get_memory_backend("stateportal"), StatePortalBackend)
        self.assertIsInstance(get_memory_backend("amem"), AMemBackend)
        self.assertIsInstance(get_memory_backend("mem0"), Mem0Backend)
        self.assertIsInstance(get_memory_backend("stateportal-anchor"), StatePortalAnchorBackend)

    def test_unknown_backend_rejected(self):
        with self.assertRaises(ValueError):
            get_memory_backend("nope")


class RetrievalTests(unittest.TestCase):
    def test_all_backends_return_relevant_entries(self):
        entries = sample_entries()
        query = task("luanti", "restore a voxel node build and validate", ["voxel", "node"])
        for name in MEMORY_BACKEND_CHOICES:
            backend = get_memory_backend(name, memory_scope="both")
            result = backend.retrieve(entries, query, limit=3, policy="positive-negative")
            self.assertTrue(result, f"{name} returned nothing")
            # The top hit should be a luanti node recipe for a luanti node query.
            self.assertEqual(result[0].benchmark, "luanti", name)

    def test_policy_positive_only_filters_negatives(self):
        entries = sample_entries() + [
            entry("neg1", "luanti", "node", "luanti_worldmod_state_validate", ["voxel", "node"], entry_type="negative_contrast", confidence=0.4)
        ]
        backend = get_memory_backend("amem")
        result = backend.retrieve(entries, task("luanti", "voxel node", ["node"]), policy="positive-only")
        self.assertTrue(all(e.entry_type == "validated_recipe" for e in result))


class EvolutionTests(unittest.TestCase):
    def test_amem_evolution_merges_duplicate_recipes(self):
        # m1 and m2 share benchmark+surface-family+strategy+type -> merged with support 2.
        backend = AMemBackend(memory_scope="both")
        result = backend.retrieve(sample_entries(), task("luanti", "voxel node build", ["node"]), limit=5)
        top = next(e for e in result if e.benchmark == "luanti" and e.semantic_surface == "node")
        self.assertGreaterEqual(top.support_count, 2)

    def test_mem0_consolidates_to_one_fact_per_key(self):
        backend = Mem0Backend()
        dupes = [
            entry("a", "luanti", "node", "luanti_worldmod_state_validate", ["node"], confidence=0.7),
            entry("b", "luanti", "node", "luanti_worldmod_state_validate", ["node"], confidence=0.95),
        ]
        result = backend.retrieve(dupes, task("luanti", "voxel node", ["node"]), limit=5)
        # Consolidation keeps a single highest-confidence fact for the salient key.
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "b")


class AnchorScopeTests(unittest.TestCase):
    def test_intra_scope_excludes_other_games(self):
        backend = StatePortalAnchorBackend(memory_scope="intra")
        result = backend.retrieve(sample_entries(), task("luanti", "voxel node build", ["node"]), limit=5)
        self.assertTrue(all(e.benchmark == "luanti" for e in result))

    def test_both_scope_allows_schema_compatible_cross_game(self):
        backend = StatePortalAnchorBackend(memory_scope="both")
        result = backend.retrieve(sample_entries(), task("luanti", "voxel node build", ["voxel", "node", "build"]), limit=5)
        benches = {e.benchmark for e in result}
        # m5 is a spacerpg entry sharing the voxel-node worldmod insight, so it is eligible cross-game.
        self.assertIn("spacerpg", benches)

    def test_both_scope_gates_incompatible_cross_game(self):
        # spacerpg station/save_restore shares no insight with a luanti voxel-node query.
        backend = StatePortalAnchorBackend(memory_scope="both")
        only_incompatible_cross = [
            entry("m1", "luanti", "node", "luanti_worldmod_state_validate", ["voxel", "node"]),
            entry("m3", "spacerpg", "station", "save_restore", ["station", "dock", "save"]),
        ]
        result = backend.retrieve(only_incompatible_cross, task("luanti", "voxel node build", ["node"]), limit=5)
        self.assertTrue(all(e.benchmark == "luanti" for e in result))


class ScopeFidelityTests(unittest.TestCase):
    def test_game_agnostic_backends_do_not_claim_intra(self):
        for name in ("amem", "mem0"):
            b = get_memory_backend(name, memory_scope="intra")
            self.assertEqual(b.requested_scope, "intra")
            self.assertEqual(b.effective_scope, "both", name)
            self.assertFalse(b.scope_honored, name)
            self.assertTrue(b.scope_warning, name)

    def test_game_agnostic_backends_honor_both(self):
        for name in ("amem", "mem0"):
            b = get_memory_backend(name, memory_scope="both")
            self.assertEqual(b.effective_scope, "both", name)
            self.assertTrue(b.scope_honored, name)
            self.assertEqual(b.scope_warning, "", name)

    def test_anchor_honors_all_scopes(self):
        for scope in ("intra", "cross", "both"):
            b = get_memory_backend("stateportal-anchor", memory_scope=scope)
            self.assertEqual(b.effective_scope, scope)
            self.assertTrue(b.scope_honored)

    def test_stateportal_supports_only_intra(self):
        honored = get_memory_backend("stateportal", memory_scope="intra")
        self.assertEqual(honored.effective_scope, "intra")
        self.assertTrue(honored.scope_honored)
        coerced = get_memory_backend("stateportal", memory_scope="both")
        self.assertEqual(coerced.effective_scope, "intra")
        self.assertFalse(coerced.scope_honored)

    def test_inventory_reports_scope_fidelity(self):
        inv = get_memory_backend("mem0", memory_scope="intra").inventory(sample_entries())
        self.assertEqual(inv["requested_scope"], "intra")
        self.assertEqual(inv["effective_scope"], "both")
        self.assertFalse(inv["scope_honored"])


class SuiteAnchorGuardTests(unittest.TestCase):
    def _variant(self, backend: str, strict: int) -> dict:
        return {
            "backend": backend,
            "is_anchor": backend == "stateportal-anchor",
            "heldout_summary": {"total_tasks": 9, "strict_success": strict, "by_benchmark": {}},
        }

    def test_summary_with_anchor_computes_deltas(self):
        variants = [self._variant("amem", 2), self._variant("stateportal-anchor", 5)]
        summary = summarize_memory_baseline_suite(variants, anchor_backend="stateportal-anchor")
        self.assertTrue(summary["anchor_present"])
        self.assertEqual(summary["anchor_heldout_strict_success"], 5)
        self.assertEqual(summary["anchor_delta_vs_baseline"], {"amem": 3})
        self.assertTrue(summary["anchor_beats_all_baselines"])

    def test_summary_without_anchor_does_not_fabricate_deltas(self):
        variants = [self._variant("amem", 2), self._variant("mem0", 3)]
        summary = summarize_memory_baseline_suite(variants, anchor_backend="stateportal-anchor")
        self.assertFalse(summary["anchor_present"])
        self.assertIsNone(summary["anchor_heldout_strict_success"])
        self.assertEqual(summary["anchor_delta_vs_baseline"], {})
        self.assertIsNone(summary["anchor_beats_all_baselines"])
        for row in summary["rows"]:
            self.assertNotIn("anchor_minus_this", row)

    def test_suite_rejects_missing_anchor_before_running(self):
        with self.assertRaises(ValueError):
            run_memory_baseline_suite(backends=["amem", "mem0"])


class SuiteResumeTests(unittest.TestCase):
    @staticmethod
    def _write_phase(
        phase_dir: Path,
        *,
        task_id: str,
        backend: str,
        memory_in: Path | None,
        memory_out: Path | None,
        memory_export: bool,
        repository_memory_writes: bool,
        strict_success: int,
        candidate_evaluations: int = 1,
    ) -> None:
        phase_dir.mkdir(parents=True, exist_ok=True)
        frozen_entries = [
            entry(
                f"{backend}-fixture",
                "luanti",
                "node",
                "luanti_worldmod_state_validate",
                ["node"],
            ).to_dict()
        ]
        if memory_out is not None:
            memory_out.parent.mkdir(parents=True, exist_ok=True)
            memory_out.write_text(
                json.dumps({"schema_version": 2, "entries": frozen_entries}),
                encoding="utf-8",
            )
        payload = {
            "experiment": "evo_synthesis",
            "memory_mode": "repository",
            "memory_backend": backend,
            "memory_in": str(memory_in) if memory_in is not None else None,
            "memory_out": str(memory_out) if memory_out is not None else None,
            "memory_export": memory_export,
            "repository_memory_writes": repository_memory_writes,
            "results": [{"task_id": task_id}],
            "summary": {
                "total_tasks": 1,
                "candidate_evaluations": candidate_evaluations,
                "candidates_executed": candidate_evaluations,
                "strict_success": strict_success,
            },
        }
        (phase_dir / "evo-synthesis.json").write_text(json.dumps(payload), encoding="utf-8")
        (phase_dir / "evo-memory.json").write_text(
            json.dumps(
                {
                    "input_memory_inventory": {
                        "total_entries": 1 if memory_in is not None else 0
                    },
                    "input_entries": frozen_entries if memory_in is not None else [],
                    "entries": frozen_entries if memory_out is not None else [],
                }
            ),
            encoding="utf-8",
        )

    def test_resume_reuses_only_complete_phase_reports(self):
        split = EvoSplit(name="tiny", train=["train-task"], heldout=["heldout-task"])
        calls: list[dict] = []
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite_dir = root / "suite"
            amem_memory = suite_dir / "amem" / "frozen-train-memory.json"
            anchor_memory = suite_dir / "stateportal-anchor" / "frozen-train-memory.json"
            self._write_phase(
                suite_dir / "amem" / "train", task_id="train-task", backend="amem",
                memory_in=None, memory_out=amem_memory, memory_export=True,
                repository_memory_writes=True, strict_success=1,
            )
            self._write_phase(
                suite_dir / "stateportal-anchor" / "train", task_id="train-task", backend="stateportal-anchor",
                memory_in=None, memory_out=anchor_memory, memory_export=True,
                repository_memory_writes=True, strict_success=1,
            )
            self._write_phase(
                suite_dir / "stateportal-anchor" / "heldout", task_id="heldout-task", backend="stateportal-anchor",
                memory_in=anchor_memory, memory_out=None, memory_export=False,
                repository_memory_writes=False, strict_success=1,
            )

            def fake_evo(*_args, **kwargs):
                calls.append(kwargs)
                frozen = json.loads(Path(kwargs["memory_in"]).read_text(encoding="utf-8"))[
                    "entries"
                ]
                return {
                    "report_dir": str(kwargs["report_dir"]),
                    "memory_scope_effective": "both",
                    "memory_scope_honored": True,
                    "results": [{"task_id": "heldout-task"}],
                    "summary": {
                        "total_tasks": 1,
                        "candidate_evaluations": 1,
                        "candidates_executed": 1,
                    "strict_success": 1,
                },
                    "memory": {
                        "input_memory_inventory": {"total_entries": 1},
                        "input_entries": frozen,
                    },
                }

            with (
                patch("warptest.evo.load_evo_split", return_value=split),
                patch("warptest.evo.run_evo_synthesis_benchmark", side_effect=fake_evo),
            ):
                result = run_memory_baseline_suite(
                    root,
                    split_path=root / "split.yaml",
                    backends=("amem", "stateportal-anchor"),
                    report_dir=suite_dir,
                    resume=True,
                )

        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0]["report_dir"]).name, "heldout")
        self.assertEqual(Path(calls[0]["memory_in"]), amem_memory)
        self.assertFalse(calls[0]["memory_export"])
        self.assertTrue(calls[0]["fail_on_provider_outage"])
        self.assertTrue(all(call["fail_on_provider_outage"] for call in calls))
        variants = {row["backend"]: row for row in result["variants"]}
        self.assertTrue(variants["amem"]["train_resumed"])
        self.assertFalse(variants["amem"]["heldout_resumed"])
        self.assertTrue(variants["stateportal-anchor"]["train_resumed"])
        self.assertTrue(variants["stateportal-anchor"]["heldout_resumed"])
        self.assertTrue(result["resume"])
        self.assertEqual(result["formal_validity"]["status"], "valid")

    def test_resume_revalidates_and_rejects_completed_zero_candidate_train(self):
        split = EvoSplit(name="tiny", train=["train-task"], heldout=["heldout-task"])
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite_dir = root / "suite"
            amem_memory = suite_dir / "amem" / "frozen-train-memory.json"
            self._write_phase(
                suite_dir / "amem" / "train",
                task_id="train-task",
                backend="amem",
                memory_in=None,
                memory_out=amem_memory,
                memory_export=True,
                repository_memory_writes=True,
                strict_success=0,
                candidate_evaluations=0,
            )
            with (
                patch("warptest.evo.load_evo_split", return_value=split),
                patch("warptest.evo.run_evo_synthesis_benchmark") as run_mock,
                self.assertRaises(MemoryBaselineEvidenceInvalidError) as raised,
            ):
                run_memory_baseline_suite(
                    root,
                    split_path=root / "split.yaml",
                    backends=("amem", "stateportal-anchor"),
                    report_dir=suite_dir,
                    resume=True,
                )
            suite_exists = (suite_dir / "memory-baseline-suite.json").exists()

        self.assertIsInstance(raised.exception, FormalC2IncompleteError)
        self.assertIsNone(run_context.error_category(raised.exception))
        self.assertFalse(run_mock.called)
        self.assertFalse(suite_exists)


class SuiteValidityGateTests(unittest.TestCase):
    def _run_with_fake_phases(
        self,
        *,
        train_candidates: int = 1,
        heldout_candidates: int = 1,
        frozen_entries: int = 1,
        heldout_input_entries: int | None = None,
        heldout_wrong_content: bool = False,
        train_export_wrong_content: bool = False,
        heldout_strict_success: int | None = 0,
        train_total_tasks: int = 1,
    ) -> tuple[dict | None, list[dict], BaseException | None]:
        split = EvoSplit(name="tiny", train=["train-task"], heldout=["heldout-task"])
        calls: list[dict] = []
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite_dir = root / "suite"

            def fake_evo(*_args, **kwargs):
                calls.append(kwargs)
                is_train = kwargs.get("memory_out") is not None
                count = train_candidates if is_train else heldout_candidates
                task_id = "train-task" if is_train else "heldout-task"
                if is_train:
                    payload = {
                        "schema_version": 2,
                        "entries": [
                            entry(
                                f"fixture-{index}",
                                "luanti",
                                "node",
                                "luanti_worldmod_state_validate",
                                ["node"],
                            ).to_dict()
                            for index in range(frozen_entries)
                        ],
                    }
                    memory_out = Path(kwargs["memory_out"])
                    memory_out.parent.mkdir(parents=True, exist_ok=True)
                    memory_out.write_text(json.dumps(payload), encoding="utf-8")
                input_count = (
                    frozen_entries
                    if is_train or heldout_input_entries is None
                    else heldout_input_entries
                )
                input_payload = [
                    entry(
                        f"fixture-{index}",
                        "luanti",
                        "node",
                        "luanti_worldmod_state_validate",
                        ["node"],
                    ).to_dict()
                    for index in range(input_count)
                ]
                # Retrieval is allowed to update ranking scores in memory, so
                # content identity deliberately ignores only that field.
                for item in input_payload:
                    item["score"] = 999.0
                if heldout_wrong_content and not is_train and input_payload:
                    input_payload[0]["id"] = "wrong-model-memory"
                train_export_payload = [dict(item) for item in input_payload]
                for item in train_export_payload:
                    item["score"] = 0.0
                if train_export_wrong_content and train_export_payload:
                    train_export_payload[0]["id"] = "wrong-train-export"
                summary = {
                    "total_tasks": train_total_tasks if is_train else 1,
                    "candidate_evaluations": count,
                    "candidates_executed": 0,
                    # Zero strict success is a valid model result and must not
                    # be confused with missing experiment evidence.
                    "strict_success": 0,
                }
                if not is_train:
                    if heldout_strict_success is None:
                        summary.pop("strict_success")
                    else:
                        summary["strict_success"] = heldout_strict_success
                return {
                    "report_dir": str(kwargs["report_dir"]),
                    "memory_scope_effective": "both",
                    "memory_scope_honored": True,
                    "results": [{"task_id": task_id}],
                    "summary": summary,
                    "memory": {
                        "input_memory_inventory": {
                            "total_entries": 0 if is_train else input_count
                        },
                        "input_entries": [] if is_train else input_payload,
                        "entries": train_export_payload if is_train else [],
                    },
                }

            result: dict | None = None
            caught: BaseException | None = None
            try:
                with (
                    patch("warptest.evo.load_evo_split", return_value=split),
                    patch(
                        "warptest.evo.run_evo_synthesis_benchmark",
                        side_effect=fake_evo,
                    ),
                ):
                    result = run_memory_baseline_suite(
                        root,
                        split_path=root / "split.yaml",
                        backends=("amem", "stateportal-anchor"),
                        report_dir=suite_dir,
                    )
            except Exception as error:  # test helper returns the exact gate error
                caught = error
            suite_exists = (suite_dir / "memory-baseline-suite.json").exists()
        if caught is not None:
            self.assertFalse(suite_exists)
        return result, calls, caught

    def test_rejects_zero_candidate_train_before_heldout(self):
        _result, calls, error = self._run_with_fake_phases(train_candidates=0)
        self.assertIsInstance(error, MemoryBaselineEvidenceInvalidError)
        self.assertIsInstance(error, FormalC2IncompleteError)
        self.assertIsNone(run_context.error_category(error))
        self.assertEqual(len(calls), 1)

    def test_rejects_empty_frozen_train_memory(self):
        _result, calls, error = self._run_with_fake_phases(frozen_entries=0)
        self.assertIsInstance(error, MemoryBaselineEvidenceInvalidError)
        self.assertEqual(len(calls), 1)

    def test_rejects_frozen_memory_not_emitted_by_train_phase(self):
        _result, calls, error = self._run_with_fake_phases(
            train_export_wrong_content=True,
        )
        self.assertIsInstance(error, MemoryBaselineEvidenceInvalidError)
        self.assertIn("does not match the train phase export", str(error))
        self.assertEqual(len(calls), 1)

    def test_rejects_incomplete_train_task_count(self):
        _result, calls, error = self._run_with_fake_phases(train_total_tasks=0)
        self.assertIsInstance(error, MemoryBaselineEvidenceInvalidError)
        self.assertEqual(len(calls), 1)

    def test_rejects_zero_candidate_heldout(self):
        _result, calls, error = self._run_with_fake_phases(heldout_candidates=0)
        self.assertIsInstance(error, MemoryBaselineEvidenceInvalidError)
        self.assertEqual(len(calls), 2)

    def test_rejects_heldout_inventory_mismatch(self):
        _result, calls, error = self._run_with_fake_phases(
            frozen_entries=2,
            heldout_input_entries=1,
        )
        self.assertIsInstance(error, MemoryBaselineEvidenceInvalidError)
        self.assertEqual(len(calls), 2)

    def test_rejects_same_count_heldout_memory_from_wrong_source(self):
        _result, calls, error = self._run_with_fake_phases(
            heldout_wrong_content=True,
        )
        self.assertIsInstance(error, MemoryBaselineEvidenceInvalidError)
        self.assertIn("content does not match", str(error))
        self.assertEqual(len(calls), 2)

    def test_rejects_missing_or_out_of_range_heldout_strict_metric(self):
        for strict_value in (None, 2):
            with self.subTest(strict_value=strict_value):
                _result, calls, error = self._run_with_fake_phases(
                    heldout_strict_success=strict_value,
                )
                self.assertIsInstance(error, MemoryBaselineEvidenceInvalidError)
                self.assertIn("strict_success", str(error))
                self.assertEqual(len(calls), 2)

    def test_accepts_strict_zero_with_real_candidate_and_memory_evidence(self):
        result, calls, error = self._run_with_fake_phases()
        self.assertIsNone(error)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(calls), 4)
        self.assertEqual(result["formal_validity"]["status"], "valid")
        self.assertEqual(len(result["formal_validity"]["backend_checks"]), 2)
        for check in result["formal_validity"]["backend_checks"]:
            self.assertEqual(
                check["train"]["frozen_memory_transfer_sha256"],
                check["heldout"]["input_memory_transfer_sha256"],
            )
        self.assertEqual(result["summary"]["anchor_heldout_strict_success"], 0)
        self.assertEqual(result["summary"]["anchor_delta_vs_baseline"], {"amem": 0})


class IntegrationTests(unittest.TestCase):
    def test_generation_memory_context_uses_backend(self):
        entries = sample_entries()
        query = task("luanti", "restore voxel node build", ["node"])
        for name in MEMORY_BACKEND_CHOICES:
            backend = get_memory_backend(name, memory_scope="both")
            ctx = _generation_memory_context(
                "repository",
                query,
                entries,
                [],
                memory_backend=backend,
                memory_policy="positive-negative",
            )
            self.assertTrue(ctx, f"{name} produced empty generation context")
            self.assertTrue(all(isinstance(e, MemoryEntry) for e in ctx))

    def test_inventory_reports_per_benchmark(self):
        inv = get_memory_backend("stateportal-anchor").inventory(sample_entries())
        self.assertEqual(inv["total_entries"], 5)
        self.assertIn("luanti", inv["by_benchmark"])
        self.assertIn("spacerpg", inv["by_benchmark"])


if __name__ == "__main__":
    unittest.main()
