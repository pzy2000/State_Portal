from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from warptest import c1_provenance


class C1ProvenanceTests(unittest.TestCase):
    def test_hash_binds_runtime_manifest_task_and_pinned_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / "warptest").mkdir()
            (root / "benchmarks" / "p5").mkdir(parents=True)
            files = {
                ".gitmodules": "[submodule]\n",
                "pyproject.toml": "[project]\nname='test'\n",
                "requirements-agent-s.txt": "agent-s==0.3.2\n",
                "benchmarks/p5/manifest.yaml": "schema_version: 1\n",
                "benchmarks/p5/c1_tasks.yaml": "tasks: []\n",
                "warptest/runtime.py": "VALUE = 1\n",
                "warptest/LibreOfficeC1Bridge.java": "final class Bridge { int version = 1; }\n",
                "warptest/c1_background_window_guard.swift": "let version = 1\n",
                "warptest/c1_unity_background_guard.m": "static int version = 1;\n",
                "warptest/instrumentation/luanti/warptest/init.lua": "return {version = 1}\n",
                "warptest/node_preloads/no_fsevents.cjs": "module.exports = {version: 1};\n",
            }
            for relpath, content in files.items():
                path = root / relpath
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            task_file = root / "benchmarks/p5/c1_tasks.yaml"

            with (
                patch.object(c1_provenance, "_formal_task_inputs", return_value=([task_file], [])),
                patch.object(
                    c1_provenance,
                    "_pinned_repository_snapshot",
                    side_effect=lambda _base, relpath: f"commit-{relpath}",
                ),
            ):
                first = c1_provenance.c1_harness_hash(root)
                java_path = root / "warptest/LibreOfficeC1Bridge.java"
                java_path.write_text(
                    "final class Bridge { int version = 2; }\n", encoding="utf-8"
                )
                java_changed = c1_provenance.c1_harness_hash(root)
                java_path.write_text(
                    "final class Bridge { int version = 1; }\n", encoding="utf-8"
                )
                swift_path = root / "warptest/c1_background_window_guard.swift"
                swift_path.write_text("let version = 2\n", encoding="utf-8")
                swift_changed = c1_provenance.c1_harness_hash(root)
                swift_path.write_text("let version = 1\n", encoding="utf-8")
                objc_path = root / "warptest/c1_unity_background_guard.m"
                objc_path.write_text("static int version = 2;\n", encoding="utf-8")
                objc_changed = c1_provenance.c1_harness_hash(root)
                objc_path.write_text("static int version = 1;\n", encoding="utf-8")
                cjs_path = root / "warptest/node_preloads/no_fsevents.cjs"
                cjs_path.write_text(
                    "module.exports = {version: 2};\n", encoding="utf-8"
                )
                cjs_changed = c1_provenance.c1_harness_hash(root)
                lua_path = root / "warptest/instrumentation/luanti/warptest/init.lua"
                lua_path.write_text("return {version = 2}\n", encoding="utf-8")
                lua_changed = c1_provenance.c1_harness_hash(root)
                task_file.write_text("tasks: [changed]\n", encoding="utf-8")
                second = c1_provenance.c1_harness_hash(root)

        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, java_changed)
        self.assertNotEqual(first, swift_changed)
        self.assertNotEqual(first, objc_changed)
        self.assertNotEqual(first, cjs_changed)
        self.assertNotEqual(first, lua_changed)
        self.assertNotEqual(first, second)

    def test_hash_binds_every_task_registered_visual_golden(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            files = {
                ".gitmodules": "[submodule]\n",
                "pyproject.toml": "[project]\nname='test'\n",
                "requirements-agent-s.txt": "agent-s==0.3.2\n",
                "benchmarks/p5/manifest.yaml": (
                    "c1_task_count: 1\n"
                    "c1_benchmark_task_counts:\n"
                    "  Example: 1\n"
                ),
                "benchmarks/p5/c1_tasks.yaml": (
                    "tasks:\n"
                    "  - id: example-task\n"
                    "    benchmark: Example\n"
                    "    oracle_spec: specs/example/task.yaml\n"
                ),
                "specs/example/task.yaml": (
                    "visual_oracle:\n"
                    "  golden_image: visual/after.png\n"
                    "  golden_before: visual/before.png\n"
                    "  golden_after: visual/after.png\n"
                ),
                "specs/example/visual/before.png": "before-v1",
                "specs/example/visual/after.png": "after-v1",
                "warptest/runtime.py": "VALUE = 1\n",
                "warptest/LibreOfficeC1Bridge.java": "final class Bridge {}\n",
                "warptest/node_preloads/no_fsevents.cjs": "module.exports = {};\n",
            }
            for relpath, content in files.items():
                path = root / relpath
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            with patch.object(
                c1_provenance,
                "_pinned_repository_snapshot",
                side_effect=lambda _base, relpath: f"commit-{relpath}",
            ):
                inputs, tasks = c1_provenance._formal_task_inputs(root)
                first = c1_provenance.c1_harness_hash(root)
                before = root / "specs/example/visual/before.png"
                before.write_text("before-tampered", encoding="utf-8")
                tampered = c1_provenance.c1_harness_hash(root)

        self.assertEqual([task["id"] for task in tasks], ["example-task"])
        self.assertIn((root / "specs/example/visual/before.png").resolve(), inputs)
        self.assertIn((root / "specs/example/visual/after.png").resolve(), inputs)
        self.assertNotEqual(first, tampered)


if __name__ == "__main__":
    unittest.main()
