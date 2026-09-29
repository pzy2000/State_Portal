from __future__ import annotations

import importlib
import unittest
from pathlib import Path

import yaml

from warptest.runner import load_spec
from warptest.state_loop import STATE_FUZZ_TASKS, load_state_loop_tasks


ROOT = Path(__file__).resolve().parents[1]
STRATEGY = "jynew_utility_save_validate"
TARGET_KIND = "synthesized_state"


def _is_jynew_fuzzable_spec(spec: dict) -> bool:
    checkpoint = spec.get("checkpoint") if isinstance(spec.get("checkpoint"), dict) else {}
    strategies = checkpoint.get("allowed_strategies") if isinstance(checkpoint, dict) else []
    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    return (
        spec.get("benchmark") == "jynew"
        and TARGET_KIND == target.get("kind")
        and STRATEGY in (strategies or [])
        and "c1" not in (spec.get("tags") or [])
    )


class JynewC3ManifestTests(unittest.TestCase):
    def test_jynew_tasks_are_exactly_synthesized_state_specs(self) -> None:
        expected = {
            f"jynew-{spec_path.stem}"
            for spec_path in sorted((ROOT / "specs" / "jynew").glob("*.yaml"))
            if _is_jynew_fuzzable_spec(load_spec(spec_path))
        }
        tasks = load_state_loop_tasks(ROOT, "benchmarks/state_loop/tasks_jynew.yaml")
        self.assertEqual({task.id for task in tasks}, expected)
        self.assertEqual(len(tasks), 10)
        for task in tasks:
            self.assertEqual(task.benchmark, "jynew")
            self.assertEqual(task.issue.repo, "games/jynew")
            spec = load_spec(ROOT / task.oracle_spec)
            self.assertTrue(_is_jynew_fuzzable_spec(spec), task.oracle_spec)

    def test_generated_jynew_manifest_is_current(self) -> None:
        generator = importlib.import_module("scripts.build_jynew_fuzz_tasks")
        payload = yaml.safe_load((ROOT / "benchmarks/state_loop" / "tasks_jynew.yaml").read_text(encoding="utf-8"))
        self.assertEqual(payload["tasks"], generator.build())

    def test_generated_c3_manifest_is_current(self) -> None:
        generator = importlib.import_module("scripts.build_c3_fuzz_tasks")
        payload = yaml.safe_load((ROOT / STATE_FUZZ_TASKS).read_text(encoding="utf-8"))
        self.assertEqual(payload["tasks"], generator.build())

    def test_c3_default_adds_jynew_without_changing_c1_manifest(self) -> None:
        c1_tasks = load_state_loop_tasks(ROOT, "benchmarks/state_loop/tasks.yaml")
        c3_tasks = load_state_loop_tasks(ROOT, STATE_FUZZ_TASKS)
        self.assertFalse(any(task.benchmark == "jynew" for task in c1_tasks))
        self.assertEqual(
            {task.id for task in c3_tasks if task.benchmark == "jynew"},
            {task.id for task in load_state_loop_tasks(ROOT, "benchmarks/state_loop/tasks_jynew.yaml")},
        )
        self.assertTrue(all(task.benchmark not in {"JupyterLab", "LibreOffice", "Blender"} for task in c3_tasks))

    def test_c3_manifest_keeps_openra_and_jynew_both_present(self) -> None:
        c3_tasks = load_state_loop_tasks(ROOT, STATE_FUZZ_TASKS)
        benchmarks = {task.benchmark for task in c3_tasks}
        self.assertIn("OpenRA", benchmarks)
        self.assertIn("jynew", benchmarks)


if __name__ == "__main__":
    unittest.main()
