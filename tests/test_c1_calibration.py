from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from warptest.c1_calibration import (
    run_c1_calibration,
)
from warptest.luanti_state import LuantiWarmSession


class C1CalibrationTests(unittest.TestCase):
    @staticmethod
    def _manifest() -> dict:
        web = {
            "capture_backend": "playwright_page_screenshot_v1",
            "input_backend": "playwright_page_input_v1",
            "frame": [1280, 800],
        }
        native = {
            "luanti": ("luanti_capture", "luanti_input", [1280, 720]),
            "OpenRA": ("openra_capture", "openra_input", [1280, 720]),
            "LibreOffice": ("lo_capture", "lo_input", [1440, 1000]),
            "Blender": ("blender_capture", "blender_input", [1280, 800]),
            "jynew": ("unity_capture", "unity_input", [1280, 720]),
            "pal3": ("unity_capture", "unity_input", [1280, 720]),
        }
        backends = {name: dict(web) for name in ("2048", "DatingSim", "SpaceRPG", "JupyterLab")}
        backends.update(
            {
                name: {"capture_backend": values[0], "input_backend": values[1], "frame": values[2]}
                for name, values in native.items()
            }
        )
        return {
            "name": "test",
            "api": {"provider_timeout_s": 900.0},
            "calibration": {"schema": "c1-calibration-v4"},
            "c1_background_pixel_protocol": {
                "schema_version": 1,
                "protocol": "warptest-agent-s-v2",
                "benchmarks": backends,
            },
            "arms": {},
        }

    def test_artifact_fails_closed_without_native_proofs_and_contains_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "benchmarks" / "p5").mkdir(parents=True)
            manifest = self._manifest()
            manifest_path = root / "benchmarks" / "p5" / "manifest.yaml"
            manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
            output = root / "report" / "calibration.json"
            fake_meter = MagicMock()
            fake_meter.summary.return_value = {"usage_complete": True}
            with (
                patch("warptest.c1_calibration.FixedVisualBridge"),
                patch("warptest.c1_calibration.FixedVisualGrounder"),
                patch("warptest.c1_calibration.cost.scoped_meter") as scoped,
                patch("warptest.c1_calibration._calibrate_browser", return_value={"passed": True, "headless": True}),
                patch("warptest.c1_calibration.run_context.git_commit", return_value="abc"),
                patch("warptest.c1_calibration.c1_harness_hash", return_value="harness"),
                patch("warptest.c1_calibration.c1_application_revisions", return_value={}),
                patch("warptest.c1_calibration.validate_c1_environment", return_value={"status": "matched"}),
            ):
                scoped.return_value.__enter__.return_value = fake_meter
                scoped.return_value.__exit__.return_value = False
                result = run_c1_calibration(
                    root,
                    output=output,
                    base_url="https://example.invalid/v1",
                    manifest_path=manifest_path,
                )
            self.assertFalse(result["passed"])
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], 4)
            self.assertEqual(saved["schema"], "c1-calibration-v4")
            self.assertEqual(saved["harness_hash"], "harness")
            self.assertEqual(len(saved["backends"]), 10)
            self.assertFalse(saved["backends"]["luanti"]["passed"])
            self.assertTrue(saved["manifest_hash"])
            self.assertNotIn("api_key", output.read_text(encoding="utf-8").lower())
            self.assertNotIn("reasoning_content", output.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["background_pixel_protocol_sha256"]), 64)

    def test_strict_native_proofs_can_complete_all_backends(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "benchmarks" / "p5").mkdir(parents=True)
            manifest_path = root / "benchmarks" / "p5" / "manifest.yaml"
            manifest = self._manifest()
            manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
            output = root / "report" / "calibration.json"
            proof_dir = root / "proofs"
            proof_dir.mkdir()
            isolation = {
                "target_never_frontmost": True,
                "system_pointer_unchanged": True,
                "system_clipboard_unchanged": True,
                "foreground_text_unchanged": True,
                "background_window_guard_passed": True,
                "window_hidden_at_create_passed": True,
                "transient_window_policy_passed": True,
            }
            for benchmark, identity in manifest["c1_background_pixel_protocol"]["benchmarks"].items():
                if identity["capture_backend"] == "playwright_page_screenshot_v1":
                    continue
                (proof_dir / f"{benchmark}.json").write_text(
                    json.dumps({
                        "schema_version": 1,
                        "benchmark": benchmark,
                        **identity,
                        "foreground_isolation": isolation,
                        "passed": True,
                    }),
                    encoding="utf-8",
                )
            fake_meter = MagicMock()
            fake_meter.summary.return_value = {"usage_complete": True}
            with (
                patch("warptest.c1_calibration.cost.scoped_meter") as scoped,
                patch("warptest.c1_calibration.c1_harness_hash", return_value="harness"),
                patch("warptest.c1_calibration.FixedVisualBridge"),
                patch("warptest.c1_calibration.FixedVisualGrounder"),
                patch("warptest.c1_calibration._calibrate_browser", return_value={"passed": True, "headless": True}),
                patch("warptest.c1_calibration.c1_application_revisions", return_value={}),
                patch("warptest.c1_calibration.validate_c1_environment", return_value={"status": "matched"}),
            ):
                scoped.return_value.__enter__.return_value = fake_meter
                scoped.return_value.__exit__.return_value = False
                result = run_c1_calibration(
                    root,
                    output=output,
                    base_url="https://example.invalid/v1",
                    manifest_path=manifest_path,
                    backend_proof_dir=proof_dir,
                )
            self.assertTrue(result["passed"])

    def test_luanti_calibration_mod_is_ephemeral_and_seeded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = root / "luanti"
            repo.mkdir()
            event = root / "clicked.txt"
            session = LuantiWarmSession(
                repo,
                root / "out",
                render=True,
                experiment_seed=17,
                creative_mode=True,
                calibration_event_path=event,
            )
            world = session._prepare_world("calibration")
            world_mt = (world / "world.mt").read_text(encoding="utf-8")
            init_lua = (world / "worldmods" / "warptest_c1_calibration" / "init.lua").read_text(encoding="utf-8")
            self.assertIn("creative_mode = true", world_mt)
            self.assertIn("load_mod_warptest_c1_calibration = true", world_mt)
            self.assertIn("WarpTest C1 calibration", init_lua)
            self.assertIn('core.register_chatcommand("warptestcalibrate"', init_lua)
            self.assertIn(str(event), init_lua)

if __name__ == "__main__":
    unittest.main()
