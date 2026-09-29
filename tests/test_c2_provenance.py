from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from warptest import c2_provenance


def _base_manifest() -> dict:
    return {
        "models": ["m1"],
        "seeds": 1,
        "api": {"base_url": "http://example.invalid/v1"},
        "retry": {"max_attempts": 1},
        "model_request_profiles": {"m1": "provider_default"},
        "arms": {
            "c2_memory_baseline": {"iterations": 2, "baseline_backends": ["amem"]},
            "c1_gui": {"step_budgets": {"short": 20}},
            "c3_fuzz": {"seeds": 3},
        },
    }


class C2ManifestFingerprintTests(unittest.TestCase):
    """c2_manifest_fingerprint must bind only the fields C2 actually reads."""

    def test_unrelated_arm_and_pareto_edits_do_not_change_fingerprint(self) -> None:
        base = _base_manifest()
        baseline = c2_provenance.c2_manifest_fingerprint(base)

        edited = copy.deepcopy(base)
        edited["arms"]["c3_fuzz"]["seeds"] = 99
        self.assertEqual(c2_provenance.c2_manifest_fingerprint(edited), baseline)

        edited = copy.deepcopy(base)
        edited["arms"]["c1_gui"]["step_budgets"]["short"] = 999
        self.assertEqual(c2_provenance.c2_manifest_fingerprint(edited), baseline)

        edited = copy.deepcopy(base)
        edited["pareto"] = {"unrelated": True}
        self.assertEqual(c2_provenance.c2_manifest_fingerprint(edited), baseline)

    def test_c2_arm_or_shared_field_edits_change_fingerprint(self) -> None:
        base = _base_manifest()
        baseline = c2_provenance.c2_manifest_fingerprint(base)

        edited = copy.deepcopy(base)
        edited["arms"]["c2_memory_baseline"]["iterations"] = 5
        self.assertNotEqual(c2_provenance.c2_manifest_fingerprint(edited), baseline)

        for field, value in (
            ("models", ["m1", "m2"]),
            ("seeds", 3),
            ("api", {"base_url": "http://changed.invalid/v1"}),
            ("retry", {"max_attempts": 9}),
            ("model_request_profiles", {"m1": "thinking_disabled"}),
        ):
            edited = copy.deepcopy(base)
            edited[field] = value
            self.assertNotEqual(
                c2_provenance.c2_manifest_fingerprint(edited), baseline, field
            )

    def test_fingerprint_is_stable_and_key_order_insensitive(self) -> None:
        base = _base_manifest()
        reordered = {key: base[key] for key in reversed(list(base))}
        self.assertEqual(
            c2_provenance.c2_manifest_fingerprint(base),
            c2_provenance.c2_manifest_fingerprint(reordered),
        )


class C2HarnessHashTests(unittest.TestCase):
    """End-to-end c2_harness_hash sensitivity, mirroring test_c1_provenance.py."""

    def _write_fixture(self, root: Path, *, manifest: dict) -> None:
        (root / "warptest").mkdir(parents=True)
        (root / "benchmarks" / "p5").mkdir(parents=True)
        (root / "benchmarks" / "core" / "public").mkdir(parents=True)
        (root / "benchmarks" / "core" / "splits").mkdir(parents=True)
        (root / "benchmarks" / "core" / "hidden").mkdir(parents=True)
        (root / "games" / "fakegame").mkdir(parents=True)

        (root / ".gitmodules").write_text("[submodule]\n", encoding="utf-8")
        (root / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
        (root / "benchmarks/p5/manifest.yaml").write_text(
            yaml.safe_dump(manifest), encoding="utf-8"
        )
        (root / "benchmarks/core/public/tasks.yaml").write_text(
            yaml.safe_dump({"tasks": [{"id": "task-1", "repo": "games/fakegame"}]}),
            encoding="utf-8",
        )
        (root / "benchmarks/core/splits/evo_phase3.yaml").write_text(
            yaml.safe_dump({"train_task_ids": ["task-1"], "heldout_task_ids": []}),
            encoding="utf-8",
        )
        (root / "benchmarks/core/hidden/oracles.yaml").write_text(
            "oracles: []\n", encoding="utf-8"
        )
        # An unrelated core file outside the explicit allowlist: proves the
        # rglob-over-benchmarks/core scan was replaced by an allowlist.
        (root / "benchmarks/core/public/tasks_explore.yaml").write_text(
            "tasks: []\n", encoding="utf-8"
        )
        for name in c2_provenance._C2_RUNTIME_ROOTS:
            (root / "warptest" / name).write_text("VALUE = 1\n", encoding="utf-8")
        # A C3 fuzz-only module, reachable only through a runtime root import,
        # must stay excluded from the closure/hash despite being "imported".
        (root / "warptest/fuzzing.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "warptest/runner.py").write_text("from . import fuzzing\n", encoding="utf-8")

    def _patched_hash(self, root: Path) -> str:
        with patch.object(
            c2_provenance,
            "_pinned_repository_snapshot",
            side_effect=lambda _base, relpath: f"commit-{relpath}",
        ):
            return c2_provenance.c2_harness_hash(root)

    def test_hash_ignores_unrelated_manifest_arms_and_fuzz_module_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _base_manifest()
            self._write_fixture(root, manifest=manifest)
            baseline = self._patched_hash(root)

            manifest["arms"]["c3_fuzz"]["seeds"] = 99
            (root / "benchmarks/p5/manifest.yaml").write_text(
                yaml.safe_dump(manifest), encoding="utf-8"
            )
            after_c3_edit = self._patched_hash(root)

            (root / "warptest/fuzzing.py").write_text("VALUE = 2\n", encoding="utf-8")
            after_fuzz_edit = self._patched_hash(root)

            (root / "benchmarks/core/public/tasks_explore.yaml").write_text(
                "tasks: [changed]\n", encoding="utf-8"
            )
            after_unrelated_core_edit = self._patched_hash(root)

        self.assertEqual(baseline, after_c3_edit)
        self.assertEqual(baseline, after_fuzz_edit)
        self.assertEqual(baseline, after_unrelated_core_edit)

    def test_hash_changes_with_c2_config_or_runtime_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _base_manifest()
            self._write_fixture(root, manifest=manifest)
            baseline = self._patched_hash(root)

            manifest["arms"]["c2_memory_baseline"]["iterations"] = 5
            (root / "benchmarks/p5/manifest.yaml").write_text(
                yaml.safe_dump(manifest), encoding="utf-8"
            )
            after_c2_edit = self._patched_hash(root)

            (root / "warptest/evo.py").write_text("VALUE = 2\n", encoding="utf-8")
            after_runtime_edit = self._patched_hash(root)

        self.assertNotEqual(baseline, after_c2_edit)
        self.assertNotEqual(baseline, after_runtime_edit)


if __name__ == "__main__":
    unittest.main()
