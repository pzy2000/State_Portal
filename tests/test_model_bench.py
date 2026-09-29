from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from warptest.model_bench import (
    ModelBenchTask,
    ModelScopeChatClient,
    attach_canonical_visual_oracle,
    build_prompt,
    default_tasks,
    dump_spec,
    extract_yaml_mapping,
    repo_for_task,
    summarize_results,
    validate_task_conformance,
    write_model_benchmark_report,
    _run_one_model_task,
    load_task_file,
)
from warptest.runner import load_spec


ROOT = Path(__file__).resolve().parents[1]


class _FakeMessage:
    content = "name: fake\nbenchmark: '2048'\ntarget: {}\ncheckpoint: {}\nactions: []\nassertions: []\n"


class _FakeChoice:
    message = _FakeMessage()


class _FakeResponse:
    choices = [_FakeChoice()]


class _FakeCompletions:
    def __init__(self) -> None:
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _FakeResponse()


class _FakeClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": _FakeCompletions()})()


class _FailingCompletions:
    def create(self, **kwargs):
        raise RuntimeError("provider unavailable")


class _FailingClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": _FailingCompletions()})()


class ModelBenchTests(unittest.TestCase):
    def test_extracts_bare_and_fenced_yaml(self) -> None:
        bare = extract_yaml_mapping("name: test\nbenchmark: '2048'\n")
        fenced = extract_yaml_mapping("```yaml\nname: test\nbenchmark: '2048'\n```\n")
        self.assertEqual(bare["benchmark"], "2048")
        self.assertEqual(fenced["name"], "test")

    def test_rejects_non_mapping_yaml(self) -> None:
        with self.assertRaises(Exception):
            extract_yaml_mapping("- just\n- a\n- list\n")

    def test_default_tasks_accept_golden_specs(self) -> None:
        tasks = {task.slug: task for task in default_tasks()}
        pairs = {
            "2048-merge-left": ROOT / "specs/2048/merge-left.yaml",
            "2048-near-win": ROOT / "specs/2048/near-win.yaml",
            "spacerpg-playable-flight": ROOT / "specs/spacerpg/playable-flight.yaml",
            "spacerpg-station-save": ROOT / "specs/spacerpg/station-save.yaml",
            "love-resource-action": ROOT / "specs/love/resource-action.yaml",
            "openra-cnc-gdi01-beachhead": ROOT / "specs/openra/cnc-gdi01-beachhead.yaml",
            "openra-ra-drop-zone": ROOT / "specs/openra/ra-drop-zone.yaml",
            "jupyterlab-text-file-edit": ROOT / "specs/jupyterlab/text-file-edit.yaml",
            "jupyterlab-notebook-cell-output": ROOT / "specs/jupyterlab/notebook-cell-output.yaml",
            "libreoffice-writer-board-report-toc": ROOT / "specs/libreoffice/writer-board-report-toc.yaml",
            "libreoffice-calc-quarterly-budget-rollup": ROOT / "specs/libreoffice/calc-quarterly-budget-rollup.yaml",
            "blender-scene-cube-material": ROOT / "specs/blender/scene-cube-material.yaml",
            "blender-save-reopen-scene": ROOT / "specs/blender/save-reopen-scene.yaml",
        }
        for slug, path in pairs.items():
            with self.subTest(slug=slug):
                ok, errors = validate_task_conformance(load_spec(path), tasks[slug])
                self.assertTrue(ok, errors)

    def test_conformance_reports_wrong_task_fields(self) -> None:
        task = {task.slug: task for task in default_tasks()}["2048-merge-left"]
        spec = load_spec(ROOT / "specs/2048/merge-left.yaml")
        spec["actions"][0]["direction"] = "right"
        ok, errors = validate_task_conformance(spec, task)
        self.assertFalse(ok)
        self.assertTrue(any("left move" in error for error in errors))

    def test_conformance_handles_non_numeric_model_values(self) -> None:
        task = {task.slug: task for task in default_tasks()}["2048-merge-left"]
        spec = load_spec(ROOT / "specs/2048/merge-left.yaml")
        spec["target"]["score"] = "III"
        ok, errors = validate_task_conformance(spec, task)
        self.assertFalse(ok)
        self.assertTrue(any("score/bestScore" in error for error in errors))

    def test_modelscope_client_uses_chat_completions(self) -> None:
        fake = _FakeClient()
        client = ModelScopeChatClient(api_key="not-secret", client=fake)
        result = client.generate("model/a", "prompt text")
        self.assertIn("benchmark", result.content)
        self.assertEqual(fake.chat.completions.kwargs["model"], "model/a")
        self.assertEqual(fake.chat.completions.kwargs["temperature"], 0.2)

    def test_modelscope_client_honors_timeout_and_retry_env(self) -> None:
        fake_openai = MagicMock()
        with (
            patch.dict(
                os.environ,
                {
                    "WARPTEST_MODEL_BENCH_TIMEOUT": "31.5",
                    "WARPTEST_MODEL_BENCH_MAX_RETRIES": "0",
                },
                clear=False,
            ),
            patch("openai.OpenAI", fake_openai),
        ):
            ModelScopeChatClient(api_key="not-secret", base_url="https://bench.example/v1")

        _, kwargs = fake_openai.call_args
        self.assertEqual(kwargs["timeout"], 31.5)
        self.assertEqual(kwargs["max_retries"], 0)
        self.assertEqual(kwargs["base_url"], "https://bench.example/v1")

    def test_summary_and_report_do_not_include_api_key(self) -> None:
        rows = [
            {
                "model": "model/a",
                "status": "success",
                "parse_success": True,
                "task_conformance": True,
                "run_success": True,
                "model_latency_ms": 100,
                "time_to_target_ms": 50,
            },
            {
                "model": "model/a",
                "status": "parse_failed",
                "parse_success": False,
                "task_conformance": False,
                "run_success": False,
                "model_latency_ms": 80,
                "time_to_target_ms": None,
            },
        ]
        summary = summarize_results(rows, ["model/a"])
        self.assertEqual(summary["models"]["model/a"]["successes"], 1)
        provider_summary = summarize_results([{**rows[0], "model_spec": "model/a@gateway"}], ["model/a@gateway"])
        self.assertEqual(provider_summary["models"]["model/a@gateway"]["attempts"], 1)
        self.assertEqual(provider_summary["models"]["model/a@gateway"]["successes"], 1)
        report = {
            "positioning": {
                "paper_role": "auxiliary_appendix",
                "primary_claim": False,
                "note": "Auxiliary test.",
            },
            "provider": "modelscope",
            "base_url": "https://api-inference.modelscope.cn/v1",
            "models": ["model/a"],
            "repeats": 1,
            "tasks": [],
            "results": rows,
            "summary": summary,
            "report_dir": "unused",
        }
        with tempfile.TemporaryDirectory() as tmp:
            write_model_benchmark_report(report, Path(tmp))
            text = json.dumps(report) + (Path(tmp) / "model-bench.md").read_text(encoding="utf-8")
            self.assertNotIn("not-secret", text)
            self.assertIn("auxiliary_appendix", text)

    def test_dump_spec_round_trips(self) -> None:
        spec = {"name": "x", "benchmark": "2048", "target": {}, "checkpoint": {}, "actions": [], "assertions": []}
        self.assertEqual(extract_yaml_mapping(dump_spec(spec))["name"], "x")

    def test_prompt_includes_precise_profile_schema(self) -> None:
        tasks = {task.slug: task for task in default_tasks()}
        prompt_2048 = build_prompt(tasks["2048-merge-left"], {})
        self.assertIn("target.board must be [[2, 2, 0, 0]", prompt_2048)
        self.assertIn('allowed_strategies: ["localStorage_restore_reload"]', prompt_2048)
        self.assertIn("Do not put localStorage or gameState under setup for 2048", prompt_2048)

        prompt_spacerpg = build_prompt(tasks["spacerpg-station-save"], {})
        self.assertIn("target.station_id must be helion-prime", prompt_spacerpg)
        self.assertIn('allowed_strategies: ["test_hook_jump_and_validate"]', prompt_spacerpg)
        self.assertIn('method: saveGame, args: ["auto"]', prompt_spacerpg)

        prompt_love = build_prompt(tasks["love-resource-action"], {})
        self.assertIn("target.patch.player.resources must set money 1000", prompt_love)
        self.assertIn('allowed_strategies: ["test_hook_seeded_state"]', prompt_love)
        self.assertIn("path: player.resources.money", prompt_love)

        prompt_openra = build_prompt(tasks["openra-cnc-gdi01-beachhead"], {})
        self.assertIn("benchmark: \"OpenRA\"", prompt_openra)
        self.assertIn('allowed_strategies: ["openra_utility_map_validate"]', prompt_openra)
        self.assertIn("openra_gameplay_production_validate", prompt_openra)
        self.assertIn("openra_validate_map", prompt_openra)
        self.assertIn("Do not use OpenRA SDL menu", prompt_openra)

        prompt_jupyterlab = build_prompt(tasks["jupyterlab-text-file-edit"], {})
        self.assertIn("benchmark: \"JupyterLab\"", prompt_jupyterlab)
        self.assertIn('allowed_strategies: ["jupyterlab_workspace_api_validate"]', prompt_jupyterlab)
        self.assertIn("jupyterlab_edit_text_file", prompt_jupyterlab)
        self.assertIn("Do not modify games/jupyterlab", prompt_jupyterlab)

        prompt_libreoffice = build_prompt(tasks["libreoffice-writer-board-report-toc"], {})
        self.assertIn("benchmark: \"LibreOffice\"", prompt_libreoffice)
        self.assertIn('allowed_strategies: ["libreoffice_uno_document_validate"]', prompt_libreoffice)
        self.assertIn("libreoffice_insert_toc", prompt_libreoffice)
        self.assertIn("libreoffice_insert_table_row", prompt_libreoffice)
        self.assertIn("libreoffice_append_table_row", prompt_libreoffice)
        self.assertIn("impress_presentation", prompt_libreoffice)
        self.assertIn("UNO/ODF", prompt_libreoffice)
        self.assertIn("Do not modify games/LibreOffice", prompt_libreoffice)

        prompt_blender = build_prompt(tasks["blender-scene-cube-material"], {})
        self.assertIn("benchmark: \"Blender\"", prompt_blender)
        self.assertIn('allowed_strategies: ["blender_python_scene_validate"]', prompt_blender)
        self.assertIn("blender_set_object_location", prompt_blender)
        self.assertIn("visible Blender GUI", prompt_blender)
        self.assertIn("Do not run Blender with --background/headless", prompt_blender)

    def test_openra_task_file_accepts_all_golden_specs(self) -> None:
        tasks = load_task_file(ROOT / "benchmarks/model/openra-tasks.yaml")
        self.assertEqual(len(tasks), 10)
        for task in tasks:
            canonical = ROOT / {
                "openra-cnc-gdi01-beachhead": "specs/openra/cnc-gdi01-beachhead.yaml",
                "openra-cnc-nod03a-prison-break": "specs/openra/cnc-nod03a-prison-break.yaml",
                "openra-cnc-hot-box-dropzone": "specs/openra/cnc-hot-box-dropzone.yaml",
                "openra-ra-allies03a-dead-end": "specs/openra/ra-allies03a-dead-end.yaml",
                "openra-ra-soviet05-distant-thunder": "specs/openra/ra-soviet05-distant-thunder.yaml",
                "openra-ra-drop-zone": "specs/openra/ra-drop-zone.yaml",
                "openra-ra-soviet05-war-factory-heavy-tank": "specs/openra/ra-soviet05-war-factory-heavy-tank.yaml",
                "openra-ra-soviet05-barracks-rocket-infantry": "specs/openra/ra-soviet05-barracks-rocket-infantry.yaml",
                "openra-ra-allies07-soviet-war-factory-produces-heavy-tank": "specs/openra/ra-allies07-soviet-war-factory-produces-heavy-tank.yaml",
                "openra-ra-allies05a-soviet-war-factory-produces-vehicle": "specs/openra/ra-allies05a-soviet-war-factory-produces-vehicle.yaml",
            }[task.slug]
            ok, errors = validate_task_conformance(load_spec(canonical), task)
            self.assertTrue(ok, errors)

    def test_jupyterlab_task_file_accepts_all_golden_specs(self) -> None:
        tasks = load_task_file(ROOT / "benchmarks/model/jupyterlab-tasks.yaml")
        self.assertEqual(len(tasks), 6)
        canonical_by_slug = {
            "jupyterlab-text-file-edit": "specs/jupyterlab/text-file-edit.yaml",
            "jupyterlab-markdown-file-save": "specs/jupyterlab/markdown-file-save.yaml",
            "jupyterlab-notebook-cell-output": "specs/jupyterlab/notebook-cell-output.yaml",
            "jupyterlab-notebook-markdown-note": "specs/jupyterlab/notebook-markdown-note.yaml",
            "jupyterlab-workspace-layout": "specs/jupyterlab/workspace-layout.yaml",
            "jupyterlab-session-smoke": "specs/jupyterlab/session-smoke.yaml",
        }
        for task in tasks:
            with self.subTest(slug=task.slug):
                ok, errors = validate_task_conformance(load_spec(ROOT / canonical_by_slug[task.slug]), task)
                self.assertTrue(ok, errors)
                self.assertEqual(repo_for_task(task), ROOT / "games/jupyterlab")

    def test_libreoffice_task_file_accepts_all_golden_specs(self) -> None:
        tasks = load_task_file(ROOT / "benchmarks/model/libreoffice-tasks.yaml")
        self.assertEqual(len(tasks), 10)
        canonical_by_slug = {
            "libreoffice-writer-board-report-toc": "specs/libreoffice/writer-board-report-toc.yaml",
            "libreoffice-writer-invoice-table-rollup": "specs/libreoffice/writer-invoice-table-rollup.yaml",
            "libreoffice-writer-policy-cross-reference": "specs/libreoffice/writer-policy-cross-reference.yaml",
            "libreoffice-calc-quarterly-budget-rollup": "specs/libreoffice/calc-quarterly-budget-rollup.yaml",
            "libreoffice-calc-sales-sort-ranking": "specs/libreoffice/calc-sales-sort-ranking.yaml",
            "libreoffice-calc-named-range-forecast": "specs/libreoffice/calc-named-range-forecast.yaml",
            "libreoffice-calc-chart-refresh-export": "specs/libreoffice/calc-chart-refresh-export.yaml",
            "libreoffice-impress-status-deck-build": "specs/libreoffice/impress-status-deck-build.yaml",
            "libreoffice-impress-slide-reorder-summary": "specs/libreoffice/impress-slide-reorder-summary.yaml",
            "libreoffice-impress-kpi-chart-pdf": "specs/libreoffice/impress-kpi-chart-pdf.yaml",
        }
        for task in tasks:
            with self.subTest(slug=task.slug):
                spec = load_spec(ROOT / canonical_by_slug[task.slug])
                ok, errors = validate_task_conformance(spec, task)
                self.assertTrue(ok, errors)
                self.assertEqual(repo_for_task(task), ROOT / "games/LibreOffice")
                if task.slug == "libreoffice-writer-invoice-table-rollup":
                    self.assertIn(
                        {"type": "libreoffice_insert_table_row", "table": "Invoice", "row": 4, "values": ["Incident review", 3, 120, 360]},
                        spec["actions"],
                    )
                    self.assertIn(
                        {"type": "libreoffice_table_cell_equals", "table": "Invoice", "cell": "D7", "expected": 1232},
                        spec["assertions"],
                    )

    def test_blender_task_file_accepts_all_golden_specs(self) -> None:
        tasks = load_task_file(ROOT / "benchmarks/model/blender-tasks.yaml")
        self.assertEqual(len(tasks), 4)
        canonical_by_slug = {
            "blender-scene-cube-material": "specs/blender/scene-cube-material.yaml",
            "blender-save-reopen-scene": "specs/blender/save-reopen-scene.yaml",
            "blender-modifier-apply": "specs/blender/modifier-apply.yaml",
            "blender-animation-keyframes": "specs/blender/animation-keyframes.yaml",
        }
        for task in tasks:
            with self.subTest(slug=task.slug):
                ok, errors = validate_task_conformance(load_spec(ROOT / canonical_by_slug[task.slug]), task)
                self.assertTrue(ok, errors)
                self.assertEqual(repo_for_task(task), ROOT / "games/blender")

    def test_blender_canonical_visual_oracles_attach(self) -> None:
        tasks = load_task_file(ROOT / "benchmarks/model/blender-tasks.yaml")
        canonical_by_slug = {
            "blender-scene-cube-material": "specs/blender/scene-cube-material.yaml",
            "blender-save-reopen-scene": "specs/blender/save-reopen-scene.yaml",
            "blender-modifier-apply": "specs/blender/modifier-apply.yaml",
            "blender-animation-keyframes": "specs/blender/animation-keyframes.yaml",
        }
        for task in tasks:
            with self.subTest(slug=task.slug):
                spec = load_spec(ROOT / canonical_by_slug[task.slug])
                updated = attach_canonical_visual_oracle(spec, task)
                oracle = updated["visual_oracle"]
                self.assertTrue(Path(oracle["golden_image"]).is_absolute())
                self.assertTrue(Path(oracle["golden_before"]).is_absolute())
                self.assertTrue(Path(oracle["golden_after"]).is_absolute())
                self.assertTrue(Path(oracle["golden_before"]).exists())
                self.assertTrue(Path(oracle["golden_after"]).exists())

    def test_blender_task_conformance_rejects_missing_exact_fields(self) -> None:
        tasks = {task.slug: task for task in load_task_file(ROOT / "benchmarks/model/blender-tasks.yaml")}
        cases = [
            (
                "blender-scene-cube-material",
                "specs/blender/scene-cube-material.yaml",
                lambda spec: spec["target"]["objects"][0]["material"].update({"color": [0.0, 0.0, 0.0, 1.0]}),
            ),
            (
                "blender-save-reopen-scene",
                "specs/blender/save-reopen-scene.yaml",
                lambda spec: spec["target"]["objects"][0]["material"].update({"color": [0.0, 0.0, 0.0, 1.0]}),
            ),
            (
                "blender-modifier-apply",
                "specs/blender/modifier-apply.yaml",
                lambda spec: (spec["actions"][0].pop("width"), spec["actions"][0].pop("segments")),
            ),
            (
                "blender-animation-keyframes",
                "specs/blender/animation-keyframes.yaml",
                lambda spec: spec["actions"][1].pop("value"),
            ),
        ]
        for slug, spec_path, mutate in cases:
            with self.subTest(slug=slug):
                spec = load_spec(ROOT / spec_path)
                mutate(spec)
                ok, errors = validate_task_conformance(spec, tasks[slug])
                self.assertFalse(ok)
                self.assertTrue(errors)

    def test_libreoffice_task_conformance_rejects_missing_exact_fields(self) -> None:
        tasks = {task.slug: task for task in load_task_file(ROOT / "benchmarks/model/libreoffice-tasks.yaml")}
        cases = [
            (
                "libreoffice-writer-board-report-toc",
                "specs/libreoffice/writer-board-report-toc.yaml",
                lambda spec: spec["actions"][1].update({"title": "Wrong"}),
            ),
            (
                "libreoffice-calc-quarterly-budget-rollup",
                "specs/libreoffice/calc-quarterly-budget-rollup.yaml",
                lambda spec: spec["assertions"][0].update({"expected": 1}),
            ),
        ]
        for slug, spec_path, mutate in cases:
            with self.subTest(slug=slug):
                spec = load_spec(ROOT / spec_path)
                mutate(spec)
                ok, errors = validate_task_conformance(spec, tasks[slug])
                self.assertFalse(ok)
                self.assertTrue(errors)

    def test_progress_logging_can_be_suppressed(self) -> None:
        task = ModelBenchTask(slug="bad-task", title="bad", benchmark="2048", description="bad")
        client = ModelScopeChatClient(api_key="not-secret", client=_FakeClient())
        with tempfile.TemporaryDirectory() as tmp:
            loud = StringIO()
            with redirect_stdout(loud):
                _run_one_model_task(
                    client=client,
                    model="model/a",
                    task=task,
                    repo=repo_for_task(task),
                    prompt="prompt",
                    repeat_index=0,
                    out_dir=Path(tmp),
                    headless=True,
                    quiet=False,
                    run_number=1,
                    total_runs=1,
                )
            self.assertIn("[1/1] model/a bad-task r1: start", loud.getvalue())

            quiet = StringIO()
            with redirect_stdout(quiet):
                _run_one_model_task(
                    client=client,
                    model="model/a",
                    task=task,
                    repo=repo_for_task(task),
                    prompt="prompt",
                    repeat_index=0,
                    out_dir=Path(tmp),
                    headless=True,
                    quiet=True,
                    run_number=1,
                    total_runs=1,
                )
            self.assertEqual(quiet.getvalue(), "")

    def test_model_call_failure_writes_raw_output_artifact(self) -> None:
        task = ModelBenchTask(slug="bad-task", title="bad", benchmark="2048", description="bad")
        client = ModelScopeChatClient(api_key="not-secret", client=_FailingClient())
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_one_model_task(
                client=client,
                model="model/a",
                task=task,
                repo=repo_for_task(task),
                prompt="prompt",
                repeat_index=0,
                out_dir=Path(tmp),
                headless=True,
                quiet=True,
                run_number=1,
                total_runs=1,
            )
            self.assertEqual(result["status"], "model_call_failed")
            raw_output = Path(result["raw_output_path"])
            self.assertTrue(raw_output.exists())
            self.assertIn("MODEL_CALL_FAILED", raw_output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
