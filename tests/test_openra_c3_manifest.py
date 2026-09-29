from __future__ import annotations

import importlib
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from warptest.cli import main
from warptest.runner import load_spec
from warptest.state_loop import STATE_FUZZ_TASKS, load_state_loop_tasks


ROOT = Path(__file__).resolve().parents[1]
GAMEPLAY_STRATEGY = "openra_gameplay_production_validate"


def _is_openra_gameplay_spec(spec: dict) -> bool:
    checkpoint = spec.get("checkpoint") if isinstance(spec.get("checkpoint"), dict) else {}
    strategies = checkpoint.get("allowed_strategies") if isinstance(checkpoint, dict) else []
    return (
        spec.get("benchmark") == "OpenRA"
        and GAMEPLAY_STRATEGY in (strategies or [])
        and "c1" not in (spec.get("tags") or [])
    )


class OpenRAC3ManifestTests(unittest.TestCase):
    def test_openra_tasks_are_exactly_live_gameplay_specs(self) -> None:
        expected = {
            f"openra-{spec_path.stem}"
            for spec_path in sorted((ROOT / "specs" / "openra").glob("*.yaml"))
            if _is_openra_gameplay_spec(load_spec(spec_path))
        }
        tasks = load_state_loop_tasks(ROOT, "benchmarks/state_loop/tasks_openra.yaml")
        self.assertEqual({task.id for task in tasks}, expected)
        self.assertEqual(len(tasks), 4)
        for task in tasks:
            self.assertEqual(task.benchmark, "OpenRA")
            spec = load_spec(ROOT / task.oracle_spec)
            self.assertTrue(_is_openra_gameplay_spec(spec), task.oracle_spec)

    def test_generated_openra_manifest_is_current(self) -> None:
        generator = importlib.import_module("scripts.build_openra_fuzz_tasks")
        payload = yaml.safe_load((ROOT / "benchmarks/state_loop" / "tasks_openra.yaml").read_text(encoding="utf-8"))
        self.assertEqual(payload["tasks"], generator.build())

    def test_generated_c3_manifest_is_current(self) -> None:
        generator = importlib.import_module("scripts.build_c3_fuzz_tasks")
        payload = yaml.safe_load((ROOT / STATE_FUZZ_TASKS).read_text(encoding="utf-8"))
        self.assertEqual(payload["tasks"], generator.build())

    def test_c3_default_adds_openra_without_changing_c1_manifest(self) -> None:
        c1_tasks = load_state_loop_tasks(ROOT, "benchmarks/state_loop/tasks.yaml")
        c3_tasks = load_state_loop_tasks(ROOT, STATE_FUZZ_TASKS)
        self.assertFalse(any(task.benchmark == "OpenRA" for task in c1_tasks))
        self.assertEqual(
            {task.id for task in c3_tasks if task.benchmark == "OpenRA"},
            {task.id for task in load_state_loop_tasks(ROOT, "benchmarks/state_loop/tasks_openra.yaml")},
        )
        self.assertTrue(all(task.benchmark not in {"JupyterLab", "LibreOffice", "Blender"} for task in c3_tasks))

    def test_cli_fuzz_default_uses_c3_manifest(self) -> None:
        with patch("warptest.cli.run_fuzz_benchmark", return_value={"report_dir": "x", "summary": {}}) as run:
            main(["fuzz-bench", "--model", "fake"])
        self.assertEqual(run.call_args.kwargs["tasks_path"], str(STATE_FUZZ_TASKS))

    def test_p5_c3_uses_c3_manifest(self) -> None:
        manifest = yaml.safe_load((ROOT / "benchmarks" / "p5" / "manifest.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["arms"]["c3_fuzz"]["tasks"], str(STATE_FUZZ_TASKS))
        self.assertEqual(manifest["arms"]["c3_fuzz"]["model"], "glm-5.2@gateway")


if __name__ == "__main__":
    unittest.main()
