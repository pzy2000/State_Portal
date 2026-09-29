from __future__ import annotations

import importlib
from contextlib import redirect_stdout
from io import StringIO
import copy
import json
import os
import signal
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from warptest.adapters import CapabilityError, LocalStorageSaveAdapter, TestHookAdapter, ThreeCanvasAdapter
from warptest.cli import main
from warptest.core_experiment import _attach_hidden_visual_oracle, grade_method_checkpoint, grade_method_checkpoint_explore, grade_method_checkpoint_strict, run_core_synthesis_benchmark
from warptest.discovery import MAX_DISCOVERY_TEXT_FILE_BYTES, RepositoryInspector, _first_path_containing
from warptest.effort import build_effort_report
from warptest.spacerpg_gui_baseline import forbidden_gui_action_calls
from warptest.gui import GenericGuiAppAdapter
from warptest.blender_runtime import BlenderRuntimeAdapter, find_blender_executable
from warptest.jupyterlab_runtime import JupyterLabRuntimeAdapter
from warptest.libreoffice_runtime import HELPER_SOURCE, LibreOfficeRuntimeAdapter
from warptest.jynew_runtime import (
    JYNEW_HOOK_RELATIVE_PATH,
    JynewRuntimeAdapter,
    _jynew_unity_candidates_for_version,
    ensure_jynew_instrumentation,
)
from warptest.luanti_state import LuantiWarmSession
from warptest.luanti_runtime import LUANTI_WORLDMOD_RELATIVE_DIR, LuantiRuntimeAdapter, _spec_needs_client, ensure_luanti_instrumentation
from warptest.pal3_runtime import (
    PAL3_HOOK_RELATIVE_PATH,
    PAL3_HOOK_TEMPLATE_PATH,
    Pal3RuntimeAdapter,
    _pal3_unity_candidates_for_version,
    ensure_pal3_instrumentation,
    find_pal3_project_path,
)
from warptest.unity_process import cleanup_detached_unity_hub_processes
from warptest.models import PhaseResult, StoragePolicy
from warptest.oracle_policy import OraclePolicyConfig, visual_audit_decision
from warptest.paper_tables import build_paper_tables
from warptest.profiles import BlenderProfile, SpaceRPGProfile, Game2048Profile, JupyterLabProfile, LibreOfficeProfile, LuantiProfile, Pal3Profile
from warptest.runner import compact_mode_result, compare_summary, load_spec, resolve_repo, summarize_benchmark_comparisons
from warptest.runtime import BrowserRuntimeAdapter, RuntimeErrorWithLog
from warptest.segments import load_hidden_bugs, load_public_segments, run_segment_bug_discovery, validate_public_segment_bank
from warptest.repo_tools import RepoSandbox
from warptest.synthesis import (
    DeterministicCheckpointSynthesisAgent,
    ExploratoryLLMCheckpointSynthesisAgent,
    LLMCheckpointSynthesisAgent,
    ModelCallResult,
    SynthesisError,
    build_explore_initial_prompt,
    build_explore_system_prompt,
    build_llm_checkpoint_prompt,
    detect_private_grader_references,
    parse_react_reply,
)
from warptest.spec_schema import SpecSchemaError
from warptest.strategies import BLENDER_REPLAY_UNSUPPORTED_REASON, SPACERPG_REPLAY_UNSUPPORTED_REASON, JUPYTERLAB_REPLAY_UNSUPPORTED_REASON, LIBREOFFICE_REPLAY_UNSUPPORTED_REASON, JYNEW_REPLAY_UNSUPPORTED_REASON, LUANTI_REPLAY_UNSUPPORTED_REASON, PAL3_REPLAY_UNSUPPORTED_REASON, StrategySelector
from warptest.task_schema import PublicIssue, load_hidden_oracles, load_public_issues, resolve_hidden_spec, validate_method_checkpoint, validate_public_tasks_do_not_reference_hidden
from warptest.state_loop import load_state_loop_tasks
from warptest import fuzzing
from warptest.visual import validate_visual_oracle


ROOT = Path(__file__).resolve().parents[1]


def _load_libreoffice_checkpoint_class():
    modules: dict[str, ModuleType] = {}
    for name in [
        "com",
        "com.sun",
        "com.sun.star",
        "com.sun.star.beans",
        "com.sun.star.connection",
        "com.sun.star.text",
        "com.sun.star.text.ReferenceFieldSource",
        "com.sun.star.text.ReferenceFieldPart",
    ]:
        modules[name] = ModuleType(name)

    class PropertyValue:
        pass

    class NoConnectException(Exception):
        pass

    uno = ModuleType("uno")
    uno.systemPathToFileUrl = lambda path: "file://" + str(path)
    uno.createUnoStruct = lambda _name: SimpleNamespace()
    modules["uno"] = uno
    modules["com.sun.star.beans"].PropertyValue = PropertyValue
    modules["com.sun.star.connection"].NoConnectException = NoConnectException
    modules["com.sun.star.text.ReferenceFieldSource"].BOOKMARK = 2
    modules["com.sun.star.text.ReferenceFieldPart"].TEXT = 2

    namespace: dict[str, object] = {}
    with patch.dict(sys.modules, modules):
        exec(HELPER_SOURCE, namespace)
    return namespace["LibreOfficeCheckpoint"]


class WarpTestCoreTests(unittest.TestCase):
    def test_run_requires_explicit_or_declared_repo(self) -> None:
        spec = {"name": "no repo"}
        with self.assertRaises(ValueError):
            resolve_repo(spec, ROOT / "specs/2048/merge-left.yaml", None)

    def test_repo_arg_overrides_spec_repo(self) -> None:
        spec = load_spec(ROOT / "specs/2048/merge-left.yaml")
        resolved = resolve_repo(spec, ROOT / "specs/2048/merge-left.yaml", str(ROOT / "games/2048"))
        self.assertEqual(resolved, ROOT / "games/2048")

    def test_2048_inspect_storage_and_hook(self) -> None:
        result = RepositoryInspector().inspect(ROOT / "games/2048")
        self.assertEqual(result.profile, "Game2048Profile")
        self.assertEqual(result.capabilities.hook_name, "__WARPTEST_2048__")
        self.assertTrue(result.storage_policy.allows_write("gameState"))
        self.assertFalse(result.storage_policy.allows_write("unknownKey"))

    def test_zustand_runtime_access_is_bridge_declared(self) -> None:
        result = RepositoryInspector().inspect(ROOT / "games/DatingSim")
        self.assertTrue(result.capabilities.zustand_detected)
        self.assertTrue(result.capabilities.zustand_runtime_access_available)
        self.assertEqual(result.capabilities.zustand_runtime_access_method, "test_hook")

    def test_spacerpg_storage_policy_allows_hook_gate_key(self) -> None:
        result = RepositoryInspector().inspect(ROOT / "games/SpaceRPG")
        self.assertTrue(result.storage_policy.allows_write("spacerpg-e2e-hook"))
        self.assertIn("spacerpg-e2e-hook", result.storage_policy.preserve_keys)

    def test_spacerpg_profile_detection_survives_sandbox_repo_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            marker_files = (
                repo / "src" / "state" / "gameStore.ts",
                repo / "src" / "data" / "systems.ts",
                repo / "src" / "components" / "FlightScene.tsx",
            )
            for path in marker_files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
            (repo / "package.json").write_text(
                json.dumps({"scripts": {"dev": "vite"}}),
                encoding="utf-8",
            )

            result = RepositoryInspector().inspect(repo)

        self.assertEqual(result.profile, "SpaceRPGProfile")
        self.assertEqual(result.capabilities.hook_name, "__SPACERPG_E2E__")

    def test_web_profile_detection_survives_sandbox_repo_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jupyterlab = root / "jupyterlab-sandbox" / "repo"
            (jupyterlab / "jupyterlab").mkdir(parents=True)
            (jupyterlab / "jupyterlab" / "labapp.py").write_text(
                "", encoding="utf-8"
            )
            (jupyterlab / "pyproject.toml").write_text(
                "[project]\nname = 'jupyterlab'\n", encoding="utf-8"
            )

            jupyterlab_result = RepositoryInspector().inspect(jupyterlab)

        self.assertEqual(jupyterlab_result.profile, "JupyterLabProfile")
        self.assertIn("python -m jupyterlab", jupyterlab_result.metadata.launch_command)

    def test_native_desktop_profile_detection_survives_sandbox_repo_name(self) -> None:
        cases = (
            (
                "LibreOfficeProfile",
                (
                    "configure.ac",
                    "desktop/source/app/app.cxx",
                    "include/LibreOfficeKit/LibreOfficeKit.h",
                ),
            ),
            (
                "BlenderProfile",
                (
                    "source/creator/creator_args.cc",
                    "scripts/startup/.keep",
                ),
            ),
        )
        for expected_profile, marker_names in cases:
            with self.subTest(profile=expected_profile), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                for marker_name in marker_names:
                    marker = repo / marker_name
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text("", encoding="utf-8")

                result = RepositoryInspector().inspect(repo)

            self.assertEqual(result.profile, expected_profile)
            self.assertFalse(result.capabilities.browser_runtime)
            self.assertNotIn("http.server", result.metadata.launch_command)

    def test_semantic_schema_contains_expected_benchmark_surfaces(self) -> None:
        cases = [
            (ROOT / "games/2048", {"board", "score"}, {"vanilla_dom_manager", "localStorage_key", "test_hook"}),
            (ROOT / "games/SpaceRPG", {"currentSystemId", "player.cargo"}, {"zustand_store", "test_hook", "canvas_webgl_readiness"}),
            (ROOT / "games/DatingSim", {"player.relationships", "world.location"}, {"zustand_store", "test_hook", "mocked_ai_gate"}),
            (ROOT / "games/OpenRA", {"target.mod", "target.map", "target.actors"}, {"native_utility_command", "openra_mod_manifest", "openra_map_yaml", "openra_gameplay_capture"}),
            (ROOT / "games/jupyterlab", {"target.kind", "target.path", "workspace.paths", "session.kernel"}, {"jupyter_lab_server", "jupyter_contents_api", "jupyter_sessions_api", "jupyter_workspace_metadata"}),
            (ROOT / "games/blender", {"scene.objects", "object.location", "animation.keyframes"}, {"blender_gui_app", "bpy_python_api", "blend_file_persistence"}),
            (ROOT / "games/jynew", {"player.Level", "team.count", "money", "skills"}, {"unity_batch_command", "es3_save_system", "role_instance"}),
        ]
        for repo, expected_fields, expected_surfaces in cases:
            with self.subTest(repo=str(repo)):
                schema = RepositoryInspector().inspect(repo).semantic_schema
                field_names = {field.name for field in schema.fields}
                surface_kinds = {surface.kind for surface in schema.surfaces}
                self.assertTrue(expected_fields.issubset(field_names))
                self.assertTrue(expected_surfaces.issubset(surface_kinds))
                self.assertTrue(all(surface.evidence for surface in schema.surfaces))
                self.assertTrue(all(field.evidence for field in schema.fields))

    def test_local_storage_adapter_enforces_storage_policy(self) -> None:
        calls: list[dict[str, object]] = []

        def install_storage_init(values: dict[str, str], clear_keys: list[str]) -> list[str]:
            calls.append({"values": values, "clear_keys": clear_keys})
            return clear_keys + list(values)

        runtime = SimpleNamespace(page=None, install_storage_init=install_storage_init)
        policy = StoragePolicy(safe_to_clear=["clear"], safe_to_overwrite=["allowed"])
        touched = LocalStorageSaveAdapter(runtime).write_many({"allowed": "1"}, ["clear"], policy)
        self.assertEqual(touched, ["clear", "allowed"])
        self.assertEqual(calls[0]["values"], {"allowed": "1"})
        with self.assertRaises(CapabilityError):
            LocalStorageSaveAdapter(runtime).write_many({"blocked": "1"}, [], policy)
        with self.assertRaises(CapabilityError):
            LocalStorageSaveAdapter(runtime).write_many({}, ["blocked"], policy)

    def test_test_hook_adapter_reports_gate_metadata(self) -> None:
        class FakePage:
            def evaluate(self, _script: str, _arg: str) -> dict[str, str]:
                return {"gate": "dev && warptest", "mode": "test", "enabledBy": "query"}

        runtime = SimpleNamespace(
            page=FakePage(),
            open=lambda path: setattr(runtime, "opened", path),
            wait_for_hook=lambda hook_name, timeout_ms: setattr(runtime, "waited", (hook_name, timeout_ms)),
        )
        availability = TestHookAdapter(runtime, "__HOOK__").open_with_gate("/?warptest=1", timeout_ms=123)
        self.assertEqual(runtime.opened, "/?warptest=1")
        self.assertEqual(runtime.waited, ("__HOOK__", 123))
        self.assertEqual(availability["enabled_by"], "query")

    def test_three_canvas_adapter_returns_stable_readiness_shape(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.heartbeat = 1

            def wait_for_function(self, _script: str, timeout: int) -> None:
                self.timeout = timeout

            def wait_for_timeout(self, _ms: int) -> None:
                self.heartbeat += 1

            def evaluate(self, _script: str) -> dict[str, object]:
                return {
                    "present": True,
                    "width": 640,
                    "height": 360,
                    "visible": True,
                    "scene_container_present": True,
                    "render_heartbeat": self.heartbeat,
                    "nonblank_sample": {"available": False, "nonblank": None, "reason": None},
                }

        page = FakePage()
        signal = ThreeCanvasAdapter(SimpleNamespace(page=page)).wait_ready(timeout_ms=321)
        self.assertEqual(page.timeout, 321)
        self.assertTrue(signal["present"])
        self.assertTrue(signal["render_heartbeat_changed"])
        self.assertIn("nonblank_sample", signal)

    def test_three_canvas_readiness_does_not_claim_webgl_canvas_as_2d(self) -> None:
        class FakePage:
            def wait_for_function(self, _script: str, timeout: int) -> None:
                pass

            def evaluate(self, script: str) -> dict[str, object]:
                self.script = script
                return {
                    "present": True,
                    "width": 640,
                    "height": 360,
                    "visible": True,
                    "scene_container_present": True,
                    "render_heartbeat": 1,
                    "nonblank_sample": {"available": False, "nonblank": None, "reason": None},
                }

        page = FakePage()
        ThreeCanvasAdapter(SimpleNamespace(page=page)).readiness_signal()
        self.assertNotIn("getContext('2d'", page.script)
        self.assertNotIn('getContext("2d"', page.script)

    def test_2048_serialized_state_is_grid_tile_shape(self) -> None:
        state = Game2048Profile._serialized_state(
            [
                [2, 2, 0, 0],
                [0, 0, 0, 0],
                [0, 0, 0, 0],
                [0, 0, 0, 0],
            ],
            score=0,
            won=False,
            over=False,
            keep_playing=False,
        )
        self.assertEqual(state["grid"]["cells"][0][0]["position"], {"x": 0, "y": 0})
        self.assertEqual(state["grid"]["cells"][1][0]["value"], 2)
        self.assertIsNone(state["grid"]["cells"][2][0])

    def test_2048_merge_left_declares_replay_baseline(self) -> None:
        spec = load_spec(ROOT / "specs/2048/merge-left.yaml")
        self.assertEqual(spec["replay_from_start"]["random_values"], [0.0, 0.0, 0.0, 0.2])

    def test_load_spec_rejects_invalid_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text("name: incomplete\nbenchmark: '2048'\n", encoding="utf-8")
            with self.assertRaises(SpecSchemaError) as raised:
                load_spec(path)
        self.assertIn("checkpoint must be a mapping", str(raised.exception))

    def test_libreoffice_insert_table_row_schema_requires_row_and_values(self) -> None:
        valid_spec = {
            "name": "insert row",
            "benchmark": "LibreOffice",
            "target": {"kind": "writer_document", "path": "x.odt", "text": "body"},
            "checkpoint": {
                "allowed_strategies": ["libreoffice_uno_document_validate"],
                "expected_validity": "save-restorable",
            },
            "actions": [{"type": "libreoffice_insert_table_row", "table": "Invoice", "row": 4, "values": ["x"]}],
            "assertions": [{"type": "no_libreoffice_errors"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "valid.yaml"
            path.write_text(yaml.safe_dump(valid_spec), encoding="utf-8")
            self.assertEqual(load_spec(path)["actions"][0]["type"], "libreoffice_insert_table_row")

            invalid = copy.deepcopy(valid_spec)
            del invalid["actions"][0]["row"]
            invalid_path = Path(tmp) / "invalid.yaml"
            invalid_path.write_text(yaml.safe_dump(invalid), encoding="utf-8")
            with self.assertRaises(SpecSchemaError) as raised:
                load_spec(invalid_path)
        self.assertIn("actions[0].row must be a positive integer", str(raised.exception))

    def test_bundled_specs_pass_schema_validation(self) -> None:
        for spec_path in sorted((ROOT / "specs").glob("*/*.yaml")):
            with self.subTest(spec=str(spec_path)):
                self.assertIsInstance(load_spec(spec_path), dict)

    def test_bundled_benchmark_has_expected_specs(self) -> None:
        spec_paths = sorted((ROOT / "specs").glob("*/*.yaml"))
        self.assertEqual(len(spec_paths), 177)

    def test_love_cafe_date_declares_replay_baseline(self) -> None:
        spec = load_spec(ROOT / "specs/love/cafe-date.yaml")
        self.assertEqual(spec["target"]["expect"]["location"], "cafe")
        self.assertEqual(spec["replay_from_start"]["actions"][0]["type"], "click")

    def test_compare_summary_reports_speedup(self) -> None:
        summary = compare_summary(
            {
                "jump_and_validate": {"overall_status": "success", "metrics": {"time_to_target_ms": 100}},
                "replay_from_start": {"overall_status": "success", "metrics": {"time_to_target_ms": 250}},
            }
        )
        self.assertEqual(summary["jump_and_validate_time_to_target_ms"], 100)
        self.assertEqual(summary["replay_time_to_target_ms"], 250)
        self.assertEqual(summary["speedup_vs_replay"], 2.5)

    def test_compare_summary_excludes_errored_replay_from_speedup(self) -> None:
        summary = compare_summary(
            {
                "jump_and_validate": {"overall_status": "success", "metrics": {"time_to_target_ms": 100}},
                "replay_from_start": {"overall_status": "failed", "reason": "boom", "metrics": {"time_to_target_ms": 250}},
            }
        )
        self.assertNotIn("speedup_vs_replay", summary)
        self.assertIn("replay_excluded_from_speedup", summary)

    def test_oracle_policy_visual_skips_semantic_failures_by_default(self) -> None:
        report = SimpleNamespace(
            semantic_status="failure",
            benchmark="2048",
            profile="Game2048Profile",
            console_errors=[],
            render_readiness_signal={},
            soft_lock_signal={},
            screenshots=[],
        )
        selected, reason = visual_audit_decision(
            config=OraclePolicyConfig(policy="visual"),
            spec={"visual_oracle": {}},
            spec_path=ROOT / "specs/2048/merge-left.yaml",
            report=report,
        )
        self.assertFalse(selected)
        self.assertEqual(reason, "skipped_semantic_failed")

    def test_oracle_policy_adaptive_selects_high_risk_runtime(self) -> None:
        report = SimpleNamespace(
            semantic_status="success",
            benchmark="SpaceRPG",
            profile="SpaceRPGProfile",
            console_errors=[],
            render_readiness_signal={},
            soft_lock_signal={},
            screenshots=[],
        )
        selected, reason = visual_audit_decision(
            config=OraclePolicyConfig(policy="adaptive"),
            spec={"visual_oracle": {}},
            spec_path=ROOT / "specs/spacerpg/playable-flight.yaml",
            report=report,
        )
        self.assertTrue(selected)
        self.assertEqual(reason, "risk_trigger")

    def test_oracle_policy_adaptive_sampling_can_select_low_risk_runtime(self) -> None:
        report = SimpleNamespace(
            semantic_status="success",
            benchmark="2048",
            profile="Game2048Profile",
            console_errors=[],
            render_readiness_signal={},
            soft_lock_signal={},
            screenshots=[],
        )
        selected, reason = visual_audit_decision(
            config=OraclePolicyConfig(policy="adaptive", visual_sample_rate=1.0),
            spec={"visual_oracle": {}},
            spec_path=ROOT / "specs/2048/merge-left.yaml",
            report=report,
        )
        self.assertTrue(selected)
        self.assertEqual(reason, "sampled")

    def test_oracle_policy_missing_visual_oracle_semantics(self) -> None:
        report = SimpleNamespace(
            semantic_status="success",
            benchmark="2048",
            profile="Game2048Profile",
            console_errors=[],
            render_readiness_signal={},
            soft_lock_signal={},
            screenshots=[],
        )
        visual_selected, visual_reason = visual_audit_decision(
            config=OraclePolicyConfig(policy="visual"),
            spec={},
            spec_path=ROOT / "specs/2048/merge-left.yaml",
            report=report,
        )
        fake_selected, fake_reason = visual_audit_decision(
            config=OraclePolicyConfig(policy="fake-visual"),
            spec={},
            spec_path=ROOT / "specs/2048/merge-left.yaml",
            report=report,
        )
        adaptive_selected, adaptive_reason = visual_audit_decision(
            config=OraclePolicyConfig(policy="adaptive"),
            spec={},
            spec_path=ROOT / "specs/2048/merge-left.yaml",
            report=report,
        )
        semantic_selected, semantic_reason = visual_audit_decision(
            config=OraclePolicyConfig(policy="semantic"),
            spec={},
            spec_path=ROOT / "specs/2048/merge-left.yaml",
            report=report,
        )
        self.assertTrue(visual_selected)
        self.assertEqual(visual_reason, "missing_visual_oracle")
        self.assertTrue(fake_selected)
        self.assertEqual(fake_reason, "missing_visual_oracle")
        self.assertFalse(adaptive_selected)
        self.assertEqual(adaptive_reason, "missing_visual_oracle")
        self.assertFalse(semantic_selected)
        self.assertEqual(semantic_reason, "semantic_policy")

    def test_strategy_selector_uses_spec_validity_and_default_strategy(self) -> None:
        spec = load_spec(ROOT / "specs/2048/near-win.yaml")
        plan = StrategySelector.select(
            spec,
            profile_name="Game2048Profile",
            default_strategy="localStorage_restore_reload",
            default_validity="unknown",
            mode="jump_and_validate",
        )
        self.assertEqual(plan.strategy_id, "localStorage_restore_reload")
        self.assertEqual(plan.validity, "save-restorable")
        self.assertTrue(plan.validation_required)

    def test_strategy_selector_reports_replay_unsupported(self) -> None:
        spacerpg_spec = load_spec(ROOT / "specs/spacerpg/playable-flight.yaml")
        spacerpg_plan = StrategySelector.select(
            spacerpg_spec,
            profile_name="SpaceRPGProfile",
            default_strategy="test_hook_jump_and_validate",
            default_validity="injection-only",
            mode="replay_from_start",
        )
        self.assertEqual(spacerpg_plan.unsupported_reason, SPACERPG_REPLAY_UNSUPPORTED_REASON)
        compact = compact_mode_result({"status": "unsupported", "reason": SPACERPG_REPLAY_UNSUPPORTED_REASON})
        self.assertEqual(compact["status"], "unsupported")

        openra_plan = StrategySelector.select(
            load_spec(ROOT / "specs/openra/cnc-gdi01-beachhead.yaml"),
            profile_name="OpenRAProfile",
            default_strategy="openra_utility_map_validate",
            default_validity="replay-reachable",
            mode="replay_from_start",
        )
        self.assertIsNone(openra_plan.unsupported_reason)
        self.assertEqual(openra_plan.strategy_id, "openra_clean_launch_gameplay")

        missing_replay = StrategySelector.select(
            load_spec(ROOT / "specs/love/dialogue-open.yaml"),
            profile_name="DatingSimProfile",
            default_strategy="test_hook_seeded_state",
            default_validity="injection-only",
            mode="replay_from_start",
        )
        self.assertIn("does not declare replay_from_start", missing_replay.unsupported_reason or "")

    def test_benchmark_compare_summary_counts_ablation_modes(self) -> None:
        summary = summarize_benchmark_comparisons(
            [
                {
                    "benchmark": "2048",
                    "modes": {
                        "inject_or_restore_no_validation": {"status": "success"},
                        "jump_and_validate": {"status": "success"},
                        "replay_from_start": {"status": "success"},
                    },
                    "summary": {"speedup_vs_replay": 2.5, "validation_overhead_ms": 15},
                },
                {
                    "benchmark": "SpaceRPG",
                    "modes": {
                        "inject_or_restore_no_validation": {"status": "success"},
                        "jump_and_validate": {"status": "success"},
                        "replay_from_start": {"status": "unsupported"},
                    },
                    "summary": {"validation_overhead_ms": 30},
                },
            ]
        )
        self.assertEqual(summary["total_specs"], 2)
        self.assertEqual(summary["benchmarks"]["SpaceRPG"]["success"], 1)
        self.assertEqual(summary["modes"]["replay_from_start"]["unsupported"], 1)
        self.assertEqual(summary["speedup_vs_replay"]["mean"], 2.5)
        self.assertEqual(summary["validation_overhead_ms"]["median"], 22.5)

    def test_effort_report_counts_profiles_specs_and_instrumentation(self) -> None:
        report = build_effort_report(ROOT)
        self.assertGreater(report["loc"]["reusable_core_and_adapters"], 0)
        self.assertGreater(report["loc"]["game_profiles"]["SpaceRPG"]["loc"], 0)
        self.assertGreater(report["loc"]["game_profiles"]["OpenRA"]["loc"], 0)
        self.assertGreater(report["loc"]["game_profiles"]["JupyterLab"]["loc"], 0)
        self.assertGreater(report["loc"]["game_profiles"]["LibreOffice"]["loc"], 0)
        self.assertGreater(report["loc"]["game_profiles"]["Blender"]["loc"], 0)
        self.assertGreaterEqual(report["loc"]["specs"]["2048"]["count"], 3)
        self.assertGreaterEqual(report["loc"]["specs"]["OpenRA"]["count"], 10)
        self.assertEqual(report["loc"]["specs"]["JupyterLab"]["count"], 9)
        self.assertEqual(report["loc"]["specs"]["LibreOffice"]["count"], 13)
        self.assertEqual(report["loc"]["specs"]["Blender"]["count"], 7)
        self.assertIn("DatingSim", report["instrumentation"])
        self.assertIn("OpenRA", report["instrumentation"])
        self.assertIn("LibreOffice", report["instrumentation"])
        self.assertIn("Blender", report["instrumentation"])
        self.assertGreaterEqual(report["instrumentation"]["DatingSim"]["marker_lines"], 1)
        self.assertGreaterEqual(report["instrumentation"]["OpenRA"]["marker_lines"], 1)
        self.assertIn("warptest/openra_runtime.py", report["loc"]["reusable_files"])
        self.assertIn("warptest/jupyterlab_runtime.py", report["loc"]["reusable_files"])
        self.assertIn("warptest/libreoffice_runtime.py", report["loc"]["reusable_files"])
        self.assertIn("warptest/blender_runtime.py", report["loc"]["reusable_files"])

    def test_paper_tables_include_required_tables_and_openra_coverage(self) -> None:
        payload = build_paper_tables(ROOT)
        tables = payload["tables"]
        self.assertIn("benchmark_spec_coverage", tables)
        self.assertIn("core_public_task_coverage", tables)
        self.assertIn("leakage_control", tables)
        self.assertIn("llm_synthesis_results", tables)
        self.assertIn("method_checkpoint_validity", tables)
        self.assertIn("baseline_fairness", tables)
        self.assertIn("spacerpg_gui_baseline_results", tables)
        self.assertIn("electron_gui_boundary", tables)
        self.assertIn("segment_bank", tables)
        self.assertIn("segment_bug_discovery", tables)
        self.assertIn("ablation_mode_summary", tables)
        self.assertIn("replay_speedup", tables)
        self.assertIn("unsupported_baselines", tables)
        self.assertIn("adapter_profile_effort_loc", tables)
        self.assertIn("capability_discovery_evidence", tables)
        self.assertGreaterEqual(len(tables["benchmark_spec_coverage"]), 15)
        self.assertTrue(any(row["benchmark"] == "OpenRA" for row in tables["benchmark_spec_coverage"]))
        self.assertEqual(sum(1 for row in tables["benchmark_spec_coverage"] if row["benchmark"] == "JupyterLab"), 9)
        self.assertEqual(sum(1 for row in tables["benchmark_spec_coverage"] if row["benchmark"] == "LibreOffice"), 13)
        self.assertEqual(sum(1 for row in tables["benchmark_spec_coverage"] if row["benchmark"] == "Blender"), 7)
        openra_tasks = [row for row in tables["core_public_task_coverage"] if row["benchmark"] == "OpenRA"]
        self.assertGreaterEqual(len(openra_tasks), 10)
        self.assertTrue(all(row["has_hidden_oracle"] for row in openra_tasks))
        jupyterlab_tasks = [row for row in tables["core_public_task_coverage"] if row["benchmark"] == "JupyterLab"]
        self.assertEqual(len(jupyterlab_tasks), 6)
        self.assertTrue(all(row["has_hidden_oracle"] for row in jupyterlab_tasks))
        libreoffice_tasks = [row for row in tables["core_public_task_coverage"] if row["benchmark"] == "LibreOffice"]
        self.assertEqual(len(libreoffice_tasks), 10)
        self.assertTrue(all(row["has_hidden_oracle"] for row in libreoffice_tasks))
        blender_tasks = [row for row in tables["core_public_task_coverage"] if row["benchmark"] == "Blender"]
        self.assertEqual(len(blender_tasks), 4)
        self.assertTrue(all(row["has_hidden_oracle"] for row in blender_tasks))
        self.assertTrue(any(row["benchmark"] == "OpenRA" for row in tables["segment_bank"]))
        self.assertTrue(
            any(
                row["component"] == "game_profile" and row["benchmark"] == "OpenRA" and row["loc"] > 0
                for row in tables["adapter_profile_effort_loc"]
            )
        )
        self.assertTrue(
            any(
                row["component"] == "specs" and row["benchmark"] == "OpenRA" and row["count"] >= 10
                for row in tables["adapter_profile_effort_loc"]
            )
        )
        openra_evidence = [row for row in tables["capability_discovery_evidence"] if row["benchmark"] == "OpenRA"]
        self.assertTrue(openra_evidence)
        openra_names = {row["name"] for row in openra_evidence}
        self.assertIn("--warptest-checkpoint", openra_names)
        self.assertIn("Launch.WarptestScreenshotPath", openra_names)
        for expected_field in ["target.mod", "target.map", "target.actors"]:
            self.assertIn(expected_field, openra_names)
        jupyterlab_evidence = [row for row in tables["capability_discovery_evidence"] if row["benchmark"] == "JupyterLab"]
        self.assertTrue(jupyterlab_evidence)
        jupyterlab_names = {row["name"] for row in jupyterlab_evidence}
        self.assertIn("/api/contents", jupyterlab_names)
        self.assertIn("target.path", jupyterlab_names)
        self.assertIn("workspace.paths", jupyterlab_names)
        libreoffice_evidence = [row for row in tables["capability_discovery_evidence"] if row["benchmark"] == "LibreOffice"]
        self.assertTrue(libreoffice_evidence)
        libreoffice_names = {row["name"] for row in libreoffice_evidence}
        self.assertIn("uno.py / pyuno", libreoffice_names)
        self.assertIn("Writer / sw", libreoffice_names)
        self.assertIn("Calc / sc", libreoffice_names)
        self.assertIn("Impress / sd", libreoffice_names)
        self.assertIn("target.kind", libreoffice_names)
        blender_evidence = [row for row in tables["capability_discovery_evidence"] if row["benchmark"] == "Blender"]
        self.assertTrue(blender_evidence)
        blender_names = {row["name"] for row in blender_evidence}
        self.assertIn("Blender.app / creator arguments", blender_names)
        self.assertIn("bpy scene data API", blender_names)
        self.assertIn("scene.objects", blender_names)
        reasons = [row["reason"] for row in tables["unsupported_baselines"] if row["benchmark"] == "SpaceRPG"]
        self.assertTrue(reasons)
        self.assertTrue(all(reason == SPACERPG_REPLAY_UNSUPPORTED_REASON for reason in reasons))
        libreoffice_reasons = [row["reason"] for row in tables["unsupported_baselines"] if row["benchmark"] == "LibreOffice"]
        self.assertTrue(libreoffice_reasons)
        self.assertTrue(all(reason == LIBREOFFICE_REPLAY_UNSUPPORTED_REASON for reason in libreoffice_reasons))
        openra_static_reasons = [
            row
            for row in tables["unsupported_baselines"]
            if row["benchmark"] == "OpenRA" and row["source"] == "static_spec_scan"
        ]
        self.assertEqual(openra_static_reasons, [])

    def test_public_tasks_and_hidden_oracles_are_separate(self) -> None:
        public_tasks = load_public_issues(ROOT)
        hidden = load_hidden_oracles(ROOT)
        self.assertGreaterEqual(len(public_tasks), 15)
        self.assertEqual(validate_public_tasks_do_not_reference_hidden(ROOT), [])
        self.assertTrue(all(task.id in hidden for task in public_tasks))
        for task in public_tasks:
            self.assertNotIn("hidden_spec", task.issue)
            self.assertNotIn("hidden target", task.issue.lower())

    def test_checkpoint_synthesis_outputs_required_artifacts_without_hidden_reads(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "spacerpg-station-save")
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT)
        self.assertEqual(checkpoint.hidden_files_read, [])
        self.assertEqual(checkpoint.payload["kind"], "test_hook")
        self.assertEqual(checkpoint.runtime_validation["status"], "pending")
        self.assertGreaterEqual(len(checkpoint.source_evidence), 1)
        self.assertEqual(validate_method_checkpoint(checkpoint, ROOT), [])

    def test_core_graders_accept_canonical_hidden_benchmark_ids(self) -> None:
        task = next(
            task
            for task in load_public_issues(ROOT)
            if task.id == "spacerpg-station-save"
        )
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT)
        hidden = load_hidden_oracles(ROOT)[task.id]
        hidden_spec = load_spec(resolve_hidden_spec(ROOT, hidden))

        self.assertEqual(task.benchmark, "SpaceRPG")
        self.assertEqual(hidden_spec["benchmark"], "spacerpg")
        self.assertTrue(
            grade_method_checkpoint(checkpoint, hidden_spec, ROOT)["benchmark_match"]
        )
        strict = grade_method_checkpoint_strict(
            checkpoint.to_dict(),
            hidden_spec,
            ROOT,
        )
        self.assertTrue(strict["benchmark_match"])
        self.assertEqual(strict["status"], "success")

    def test_hidden_grader_scores_method_checkpoint_without_exposing_oracle_to_agent(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT)
        hidden = load_hidden_oracles(ROOT)[task.id]
        hidden_spec = load_spec(resolve_hidden_spec(ROOT, hidden))
        grading = grade_method_checkpoint(checkpoint, hidden_spec, ROOT)
        self.assertEqual(grading["status"], "success")
        self.assertTrue(grading["hidden_oracle_used_only_by_grader"])

    def test_strict_grader_requires_model_own_spec_to_cover_hidden_target(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "spacerpg-trading-cargo")
        hidden = load_hidden_oracles(ROOT)[task.id]
        hidden_spec = load_spec(resolve_hidden_spec(ROOT, hidden))
        # A checkpoint whose own proposed spec omits the hidden deep-state values fails strict grading.
        shallow = {
            "task_id": task.id,
            "benchmark": task.benchmark,
            "generated_spec": {"benchmark": task.benchmark},
            "model_generated_spec": {
                "benchmark": task.benchmark,
                "target": {"kind": "station", "screen": "station", "system_id": "helion-reach"},
                "checkpoint": {"allowed_strategies": ["test_hook_jump_and_validate"], "expected_validity": "replay-reachable"},
                "actions": [{"type": "hook_call", "method": "sell", "args": ["basic-food", 1]}],
                "assertions": [{"type": "no_severe_console"}],
            },
            "source_evidence": [{"source_path": "games/SpaceRPG/package.json"}],
            "payload": {"kind": "test_hook"},
            "validity_class": "replay-reachable",
            "runtime_validation": {"status": "pending"},
            "inputs_read": [],
        }
        strict = grade_method_checkpoint_strict(shallow, hidden_spec, ROOT)
        self.assertEqual(strict["status"], "failure")
        self.assertFalse(strict["target_match"])
        self.assertEqual(strict["graded_on"], "model_generated_spec")

    def test_strict_grader_fails_when_model_returns_no_spec_mapping(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        hidden = load_hidden_oracles(ROOT)[task.id]
        hidden_spec = load_spec(resolve_hidden_spec(ROOT, hidden))
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT).to_dict()
        checkpoint["model_generated_spec"] = None
        strict = grade_method_checkpoint_strict(checkpoint, hidden_spec, ROOT)
        self.assertEqual(strict["status"], "failure")
        self.assertIn("executable spec mapping", strict["reason"])

    def test_strict_grader_accepts_model_spec_that_covers_hidden(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        hidden = load_hidden_oracles(ROOT)[task.id]
        hidden_spec = load_spec(resolve_hidden_spec(ROOT, hidden))
        # Deterministic agent copies the answer-key template into model_generated_spec.
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT).to_dict()
        strict = grade_method_checkpoint_strict(checkpoint, hidden_spec, ROOT)
        self.assertEqual(strict["status"], "success")
        self.assertTrue(strict["target_match"])

    EXPLORE_TASKS_PATH = "benchmarks/core/public/tasks_explore.yaml"

    def _deterministic_explore_target_matches(self) -> set[str]:
        """Task ids the deterministic agent covers (benchmark + target match).

        Used to derive expected explore-execute success counts without hard-coding a
        number that shifts whenever the deterministic template's coverage changes.
        """
        agent = DeterministicCheckpointSynthesisAgent()
        hidden = load_hidden_oracles(ROOT)
        matches: set[str] = set()
        for task in load_public_issues(ROOT, self.EXPLORE_TASKS_PATH):
            hidden_spec = load_spec(resolve_hidden_spec(ROOT, hidden[task.id]))
            checkpoint = agent.synthesize(task, ROOT).to_dict()
            strict = grade_method_checkpoint_strict(checkpoint, hidden_spec, ROOT)
            if strict.get("benchmark_match") and strict.get("target_match"):
                matches.add(task.id)
        return matches

    def test_explore_tasks_are_deleaked_subset_without_leakage(self) -> None:
        explore_tasks = load_public_issues(ROOT, self.EXPLORE_TASKS_PATH)
        hidden = load_hidden_oracles(ROOT)
        # The explore-hard variant now covers every core task (all 10 benchmarks).
        public_tasks = load_public_issues(ROOT)
        self.assertEqual(len(explore_tasks), len(public_tasks))
        self.assertEqual(len(explore_tasks), 76)
        self.assertEqual(
            validate_public_tasks_do_not_reference_hidden(ROOT, self.EXPLORE_TASKS_PATH), []
        )
        self.assertTrue(all(task.id in hidden for task in explore_tasks))
        self.assertEqual(
            {task.id for task in explore_tasks}, {task.id for task in public_tasks}
        )
        benchmarks = {task.benchmark for task in explore_tasks}
        self.assertEqual(
            benchmarks,
            {
                "2048",
                "SpaceRPG",
                "DatingSim",
                "OpenRA",
                "JupyterLab",
                "LibreOffice",
                "Blender",
                "jynew",
                "pal3",
                "luanti",
            },
        )
        # Mechanism tokens must be absent from the de-leaked prose so the model has to
        # discover them from the repository / benchmark contract rather than transcribe.
        blob = " ".join(task.issue for task in explore_tasks)
        for token in [
            # browser subset mechanism / repo tokens
            "test_hook_jump_and_validate",
            "test_hook_seeded_state",
            "localStorage_restore_reload",
            "save-restorable",
            "replay-reachable",
            "injection-only",
            "saveGame",
            "jumpToSystem",
            "installEquipmentFromInventory",
            "startDialogue",
            "applyRelationshipUpdates",
            "char_1",
            "work_coding",
            "date_coffee",
            "glass-wake-hero",
            # framework action-type / strategy names across the new runtimes
            "luanti_set_node",
            "luanti_fill",
            "luanti_worldmod_state_validate",
            "openra_validate_map",
            "libreoffice_uno_document_validate",
            "libreoffice_replace_text",
            "libreoffice_set_cell",
            "jynew_join_team",
            "pal3_set_story_var",
            # repo-discoverable game ids (must be recovered from the repo, not transcribed)
            "basenodes:stone",
            "basenodes:cobble",
            "chest:chest",
        ]:
            self.assertNotIn(token, blob)

    def test_explore_solvability_checker_passes(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "_check_explore_issues.py")],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_explore_love_tasks_distinguish_start_and_post_action_state(self) -> None:
        tasks = {task.id: task for task in load_public_issues(ROOT, self.EXPLORE_TASKS_PATH)}
        self.assertIn("restored checkpoint should validate those starting resources", tasks["love-resource-action"].issue)
        self.assertIn("money should become 1100 and energy should become 80", tasks["love-resource-action"].issue)
        self.assertIn("restored checkpoint should validate that starting state", tasks["love-cafe-date"].issue)
        self.assertIn("money 920, energy 90, and communication 11", tasks["love-cafe-date"].issue)
        self.assertIn("restored checkpoint should validate that starting state", tasks["love-relationship-update"].issue)
        self.assertIn("trust 30, attraction 30, comfort 20, and respect 13", tasks["love-relationship-update"].issue)

    def test_explore_grader_requires_target_execution_and_visual(self) -> None:
        task = next(
            t for t in load_public_issues(ROOT, self.EXPLORE_TASKS_PATH) if t.id == "2048-merge-left"
        )
        hidden = load_hidden_oracles(ROOT)[task.id]
        hidden_spec = load_spec(resolve_hidden_spec(ROOT, hidden))
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT).to_dict()
        checkpoint["runtime_semantic_status"] = "success"
        checkpoint["runtime_overall_status"] = "success"
        checkpoint["runtime_visual_validation"] = {"status": "success"}

        passing = grade_method_checkpoint_explore(checkpoint, hidden_spec, ROOT)
        self.assertEqual(passing["status"], "success")
        self.assertEqual(passing["graded_on"], "execution+visual")
        self.assertTrue(passing["target_match"])
        self.assertTrue(passing["execution_passed"])
        self.assertTrue(passing["visual_passed"])

        no_visual = copy.deepcopy(checkpoint)
        no_visual["runtime_visual_validation"] = {"status": "failure"}
        self.assertEqual(grade_method_checkpoint_explore(no_visual, hidden_spec, ROOT)["status"], "failure")

        no_exec = copy.deepcopy(checkpoint)
        no_exec["runtime_semantic_status"] = "failure"
        no_exec["runtime_overall_status"] = "failure"
        self.assertEqual(grade_method_checkpoint_explore(no_exec, hidden_spec, ROOT)["status"], "failure")

        # Even with execution + visual passing, a wrong target intent must fail.
        bad_target = copy.deepcopy(checkpoint)
        bad_target["model_generated_spec"]["target"] = {
            "kind": "board",
            "board": [[0, 0, 0, 0]] * 4,
            "score": 999,
            "bestScore": 999,
        }
        bad = grade_method_checkpoint_explore(bad_target, hidden_spec, ROOT)
        self.assertEqual(bad["status"], "failure")
        self.assertFalse(bad["target_match"])

    def test_explore_grader_semantic_only_when_visual_skipped(self) -> None:
        # When the visual audit is skipped by the oracle policy (e.g. the luanti voxel
        # subset run with --oracle-policy semantic, whose desktop-GL goldens are not
        # reproducible on this host), the grade collapses to target + execution.
        task = next(
            t for t in load_public_issues(ROOT, self.EXPLORE_TASKS_PATH) if t.id == "2048-merge-left"
        )
        hidden = load_hidden_oracles(ROOT)[task.id]
        hidden_spec = load_spec(resolve_hidden_spec(ROOT, hidden))
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT).to_dict()
        checkpoint["runtime_semantic_status"] = "success"
        checkpoint["runtime_overall_status"] = "success"
        checkpoint["runtime_visual_validation"] = {"status": "skipped"}

        graded = grade_method_checkpoint_explore(checkpoint, hidden_spec, ROOT)
        self.assertEqual(graded["status"], "success")
        self.assertEqual(graded["graded_on"], "execution")
        self.assertFalse(graded["visual_graded"])
        self.assertTrue(graded["target_match"])
        self.assertTrue(graded["execution_passed"])

        # Execution still gates the semantic-only path.
        no_exec = copy.deepcopy(checkpoint)
        no_exec["runtime_semantic_status"] = "failure"
        no_exec["runtime_overall_status"] = "failure"
        self.assertEqual(
            grade_method_checkpoint_explore(no_exec, hidden_spec, ROOT)["status"], "failure"
        )

    def test_explore_grader_reports_blocked_runtime(self) -> None:
        # A runtime/environment block (engine unbuildable, missing desktop display,
        # uninstalled binary) is reported as a neutral "blocked" status, never as a
        # model failure, so it does not pollute the explore-execute failure count.
        task = next(
            t for t in load_public_issues(ROOT, self.EXPLORE_TASKS_PATH) if t.id == "2048-merge-left"
        )
        hidden = load_hidden_oracles(ROOT)[task.id]
        hidden_spec = load_spec(resolve_hidden_spec(ROOT, hidden))
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT).to_dict()
        checkpoint["runtime_blocked"] = {
            "reason": "OpenRA build failed; see log",
            "error_type": "RuntimeError",
        }

        graded = grade_method_checkpoint_explore(checkpoint, hidden_spec, ROOT)
        self.assertEqual(graded["status"], "blocked")
        self.assertEqual(graded["graded_on"], "execution+visual")
        self.assertIn("build failed", graded["blocked_reason"])
        self.assertFalse(graded["execution_passed"])

        # The unsupported runtime-validation status is treated the same way.
        via_validation = copy.deepcopy(checkpoint)
        via_validation.pop("runtime_blocked")
        via_validation["runtime_validation"] = {"status": "unsupported", "detail": "no display"}
        self.assertEqual(
            grade_method_checkpoint_explore(via_validation, hidden_spec, ROOT)["status"], "blocked"
        )

    def test_runtime_blocked_reason_classifier(self) -> None:
        from warptest.core_experiment import _runtime_blocked_reason

        for blocked in [
            "OpenRA build failed; see /tmp/log",
            "Could not launch the engine",
            "cannot open display :0",
            "blender executable not found",
            "unsupported strategy",
            "LibreOffice UNO runtime dependencies are not available: import uno failed",
            "Set URE_BOOTSTRAP/PYTHONPATH for pyuno",
        ]:
            self.assertIsNotNone(_runtime_blocked_reason(blocked))
        for real_failure in [
            "OpenRA spec target.mod is required",
            "target validation failed",
            "assertion luanti_node_count did not reach min_count",
            "",
        ]:
            self.assertIsNone(_runtime_blocked_reason(real_failure))

    def test_explore_summary_counts_blocked_separately(self) -> None:
        # Blocked results are neutral: not counted as method-checkpoint failures.
        with tempfile.TemporaryDirectory() as tmp:
            with patch("warptest.core_experiment._maybe_execute_checkpoint") as mocked:
                def _fake_execute(base, task, checkpoint, out_dir, **kwargs):
                    data = checkpoint.to_dict()
                    if task.benchmark == "luanti":
                        data["runtime_blocked"] = {
                            "reason": "requires a desktop GL session",
                            "error_type": "RuntimeError",
                        }
                        return data, None
                    data["runtime_semantic_status"] = "success"
                    data["runtime_overall_status"] = "success"
                    data["runtime_visual_validation"] = {"status": "success"}
                    data["runtime_validation"] = {"status": "success"}
                    return data, {"overall_status": "success"}

                mocked.side_effect = _fake_execute
                result = run_core_synthesis_benchmark(
                    ROOT,
                    tasks_path=self.EXPLORE_TASKS_PATH,
                    agent_kind="deterministic",
                    grading_mode="explore_execute",
                    gui_baseline=False,
                    report_dir=Path(tmp),
                )
        summary = result["summary"]
        explore_tasks = load_public_issues(ROOT, self.EXPLORE_TASKS_PATH)
        luanti_ids = {t.id for t in explore_tasks if t.benchmark == "luanti"}
        target_matches = self._deterministic_explore_target_matches()
        # luanti is blocked (neutral); non-luanti split into success (target covered)
        # and genuine failure (deterministic did not cover the target intent).
        self.assertEqual(summary["explore_execute_blocked"], len(luanti_ids))
        self.assertEqual(
            summary["explore_execute_success"], len(target_matches - luanti_ids)
        )
        expected_failures = sum(
            1 for t in explore_tasks if t.id not in luanti_ids and t.id not in target_matches
        )
        self.assertEqual(summary["method_checkpoint_failure"], expected_failures)

    def test_explore_execute_grading_mode_forces_visual_policy(self) -> None:
        policies: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("warptest.core_experiment._maybe_execute_checkpoint") as mocked:
                def _fake_execute(base, task, checkpoint, out_dir, **kwargs):
                    policies.append(kwargs["oracle_policy"].policy)
                    data = checkpoint.to_dict()
                    data["runtime_semantic_status"] = "success"
                    data["runtime_overall_status"] = "success"
                    data["runtime_visual_validation"] = {"status": "success"}
                    data["runtime_validation"] = {"status": "success"}
                    return data, {"overall_status": "success"}

                mocked.side_effect = _fake_execute
                result = run_core_synthesis_benchmark(
                    ROOT,
                    tasks_path=self.EXPLORE_TASKS_PATH,
                    agent_kind="deterministic",
                    grading_mode="explore_execute",
                    gui_baseline=False,
                    report_dir=Path(tmp),
                )
        expected_success = len(self._deterministic_explore_target_matches())
        self.assertEqual(result["grading_mode"], "explore_execute")
        self.assertEqual(result["summary"]["total_tasks"], 76)
        self.assertTrue(result["visual_required"])
        self.assertEqual(set(policies), {"visual"})
        # With execution + visual forced to success by the mock, explore success is
        # exactly the set the deterministic agent covers on target intent.
        self.assertEqual(result["summary"]["explore_execute_success"], expected_success)
        self.assertEqual(result["summary"]["explore_execute_blocked"], 0)
        for item in result["results"]:
            self.assertEqual(item["grading"]["graded_on"], "execution+visual")
            self.assertIn("grading_structural", item)

    def test_explore_execute_grading_mode_upgrades_adaptive_policy_to_visual(self) -> None:
        policies: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("warptest.core_experiment._maybe_execute_checkpoint") as mocked:
                def _fake_execute(base, task, checkpoint, out_dir, **kwargs):
                    policies.append(kwargs["oracle_policy"].policy)
                    data = checkpoint.to_dict()
                    data["runtime_semantic_status"] = "success"
                    data["runtime_overall_status"] = "success"
                    data["runtime_visual_validation"] = {"status": "success"}
                    data["runtime_validation"] = {"status": "success"}
                    return data, {"overall_status": "success"}

                mocked.side_effect = _fake_execute
                result = run_core_synthesis_benchmark(
                    ROOT,
                    tasks_path=self.EXPLORE_TASKS_PATH,
                    agent_kind="deterministic",
                    grading_mode="explore_execute",
                    oracle_policy="adaptive",
                    gui_baseline=False,
                    report_dir=Path(tmp),
                )
        expected_success = len(self._deterministic_explore_target_matches())
        self.assertEqual(result["oracle_policy"], "visual")
        self.assertEqual(set(policies), {"visual"})
        self.assertEqual(result["summary"]["explore_execute_success"], expected_success)

    def test_explore_execute_grading_mode_honours_explicit_semantic_only(self) -> None:
        # An explicit `--oracle-policy semantic` is a deliberate semantic-only run: the
        # visual audit is skipped and the grade collapses to target + execution.
        policies: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("warptest.core_experiment._maybe_execute_checkpoint") as mocked:
                def _fake_execute(base, task, checkpoint, out_dir, **kwargs):
                    policies.append(kwargs["oracle_policy"].policy)
                    data = checkpoint.to_dict()
                    data["runtime_semantic_status"] = "success"
                    data["runtime_overall_status"] = "success"
                    # semantic policy skips the visual audit
                    data["runtime_visual_validation"] = {"status": "skipped"}
                    data["runtime_validation"] = {"status": "success"}
                    return data, {"overall_status": "success"}

                mocked.side_effect = _fake_execute
                result = run_core_synthesis_benchmark(
                    ROOT,
                    tasks_path=self.EXPLORE_TASKS_PATH,
                    agent_kind="deterministic",
                    grading_mode="explore_execute",
                    oracle_policy="semantic",
                    gui_baseline=False,
                    report_dir=Path(tmp),
                )
        expected_success = len(self._deterministic_explore_target_matches())
        self.assertEqual(result["oracle_policy"], "semantic")
        self.assertFalse(result["visual_required"])
        self.assertEqual(set(policies), {"semantic"})
        self.assertEqual(result["summary"]["explore_execute_success"], expected_success)
        for item in result["results"]:
            self.assertEqual(item["grading"]["graded_on"], "execution")
            self.assertFalse(item["grading"]["visual_graded"])

    def test_core_synthesis_benchmark_static_report_includes_baseline_fairness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_core_synthesis_benchmark(ROOT, execute=False, agent_kind="deterministic", gui_baseline=False, report_dir=Path(tmp))
            self.assertTrue((Path(tmp) / "core-synthesis.json").exists())
        self.assertEqual(result["summary"]["total_tasks"], 76)
        self.assertEqual(result["summary"]["public_leakage_failures"], 0)
        self.assertEqual(result["summary"]["schema_error_tasks"], 0)
        self.assertEqual(result["summary"]["gui_from_start"]["public_inputs_only"], 76)

    def test_llm_prompt_uses_public_inputs_without_private_grader_refs(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "spacerpg-station-save")
        located = DeterministicCheckpointSynthesisAgent().locator.locate(task, ROOT)
        prompt = build_llm_checkpoint_prompt(task, located)
        self.assertEqual(detect_private_grader_references(prompt), [])
        self.assertNotIn("benchmarks/core/hidden", prompt)
        self.assertNotIn("hidden_spec", prompt)

    def test_openra_production_llm_prompt_exposes_gameplay_contract(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "ra-soviet05-war-factory-heavy-tank")
        located = DeterministicCheckpointSynthesisAgent().locator.locate(task, ROOT)
        prompt = build_llm_checkpoint_prompt(task, located)

        self.assertEqual(detect_private_grader_references(prompt), [])
        for expected in [
            "openra_gameplay_production_validate",
            "openra_build_actor",
            "openra_actor_built",
            "openra_production_prerequisite_respected",
            "openra_gameplay_probe",
            "Building",
            "Vehicle",
            "3tnk",
            "startmcv",
        ]:
            self.assertIn(expected, prompt)

    def test_llm_prompt_includes_state_loop_feedback_without_private_refs(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        located = DeterministicCheckpointSynthesisAgent().locator.locate(task, ROOT)
        feedback = {
            "before": {"label": "incorrect", "rationale": "wrong start board", "rubric_failures": ["missing 2 tile"]},
            "after": {"label": "incorrect", "rationale": "score did not increase", "rubric_failures": []},
        }
        prompt = build_llm_checkpoint_prompt(task, located, feedback=feedback)

        self.assertIn("PRIOR ATTEMPT FAILED", prompt)
        self.assertIn("wrong start board", prompt)
        self.assertIn("missing 2 tile", prompt)
        self.assertIn("score did not increase", prompt)
        self.assertEqual(detect_private_grader_references(prompt), [])

    def test_deterministic_agent_accepts_feedback(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(
            task,
            ROOT,
            feedback={"before": {"label": "incorrect", "rationale": "ignored"}},
        )
        self.assertEqual(checkpoint.task_id, "2048-merge-left")
        self.assertEqual(validate_method_checkpoint(checkpoint, ROOT), [])

    def test_llm_agent_parses_method_checkpoint_with_fake_client(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        spec = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT).generated_spec

        class FakeClient:
            def generate(self, model: str, prompt: str) -> ModelCallResult:
                return ModelCallResult(
                    content=(
                        "task_id: 2048-merge-left\n"
                        "benchmark: '2048'\n"
                        f"generated_spec: {spec!r}\n"
                        "source_evidence:\n"
                        "  - source_path: games/2048/js/game_manager.js\n"
                        "    matched_symbol: localStorage\n"
                        "    confidence: 0.9\n"
                        "payload:\n"
                        "  kind: localStorage\n"
                        "runtime_validation:\n"
                        "  status: pending\n"
                        "validity_class: save-restorable\n"
                    ),
                    latency_ms=12,
                )

        checkpoint = LLMCheckpointSynthesisAgent(model="fake/model", client=FakeClient()).synthesize(task, ROOT)
        self.assertEqual(checkpoint.generated_by, "fake/model")
        self.assertEqual(checkpoint.payload["kind"], "localStorage")
        self.assertEqual(validate_method_checkpoint(checkpoint, ROOT), [])

    def test_llm_agent_repairs_prose_generated_spec_with_public_scaffold(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")

        class ProseSpecClient:
            def generate(self, model: str, prompt: str) -> ModelCallResult:
                return ModelCallResult(
                    content=(
                        "task_id: 2048-merge-left\n"
                        "benchmark: '2048'\n"
                        "generated_spec: Verify that two top-row 2 tiles merge after a left move.\n"
                        "source_evidence:\n"
                        "  - source_path: games/2048/js/game_manager.js\n"
                        "    matched_symbol: grid\n"
                        "    confidence: 0.9\n"
                        "payload:\n"
                        "  kind: checkpoint\n"
                        "  hook: __WARPTEST_2048__\n"
                        "runtime_validation:\n"
                        "  status: pending\n"
                        "validity_class: unknown\n"
                    ),
                    latency_ms=8,
                )

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = LLMCheckpointSynthesisAgent(model="fake/model", client=ProseSpecClient()).synthesize(task, ROOT, artifact_dir=Path(tmp))
            meta = json.loads((Path(tmp) / "synthesis-meta.json").read_text(encoding="utf-8"))
        self.assertIsInstance(checkpoint.generated_spec, dict)
        self.assertEqual(checkpoint.payload["kind"], "localStorage")
        self.assertEqual(checkpoint.validity_class, "save-restorable")
        self.assertIn("llm_generated_spec_text", checkpoint.generated_spec)
        self.assertIn("generated_spec_string_wrapped_with_public_task_scaffold", meta["normalization_warnings"])
        self.assertEqual(validate_method_checkpoint(checkpoint, ROOT), [])

    def test_llm_agent_reports_invalid_output_and_private_leaks(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")

        class MissingClient:
            def generate(self, model: str, prompt: str) -> ModelCallResult:
                return ModelCallResult(content="generated_spec: {}", latency_ms=1)

        checkpoint = LLMCheckpointSynthesisAgent(model="fake/model", client=MissingClient()).synthesize(task, ROOT)
        self.assertTrue(checkpoint.used_public_scaffold)
        self.assertIsNone(checkpoint.model_generated_spec)
        self.assertEqual(checkpoint.payload["kind"], "localStorage")
        self.assertEqual(validate_method_checkpoint(checkpoint, ROOT), [])

        class LeakyClient:
            def generate(self, model: str, prompt: str) -> ModelCallResult:
                return ModelCallResult(
                    content=(
                        "task_id: 2048-merge-left\n"
                        "benchmark: '2048'\n"
                        "generated_spec: {}\n"
                        "source_evidence:\n"
                        "  - source_path: benchmarks/core/hidden/oracles.yaml\n"
                        "payload:\n"
                        "  kind: localStorage\n"
                        "runtime_validation:\n"
                        "  status: pending\n"
                        "validity_class: save-restorable\n"
                    ),
                    latency_ms=1,
                )

        with self.assertRaises(SynthesisError):
            LLMCheckpointSynthesisAgent(model="fake/model", client=LeakyClient()).synthesize(task, ROOT)

    def test_core_bench_llm_requires_api_key(self) -> None:
        with patch.dict(os.environ, {"MODELSCOPE_API_KEY": ""}, clear=False):
            with self.assertRaises(SynthesisError):
                run_core_synthesis_benchmark(ROOT, agent_kind="llm", execute=False)


class WarpTestExplorationTests(unittest.TestCase):
    def test_repo_sandbox_reads_record_and_confine(self) -> None:
        sandbox = RepoSandbox(ROOT / "games/2048")

        ok = sandbox.read_file("js/game_manager.js", 1, 10)
        self.assertTrue(ok.startswith("FILE js/game_manager.js"))
        self.assertIn("js/game_manager.js", sandbox.reads)

        escape = sandbox.read_file("../../benchmarks/core/hidden/oracles.yaml")
        self.assertTrue(escape.startswith("ERROR:"))
        absolute = sandbox.read_file("/etc/passwd")
        self.assertTrue(absolute.startswith("ERROR:"))
        ignored = sandbox.read_file("node_modules/anything.js")
        self.assertTrue(ignored.startswith("ERROR:"))
        # No escape attempt should ever be recorded as a successful read.
        self.assertNotIn("../../benchmarks/core/hidden/oracles.yaml", sandbox.reads)
        self.assertTrue(all("benchmarks" not in entry for entry in sandbox.reads))

    def test_repo_sandbox_grep_and_list_dir(self) -> None:
        sandbox = RepoSandbox(ROOT / "games/SpaceRPG")
        hits = sandbox.grep("useGameStore", glob="*.ts")
        self.assertNotEqual(hits, "")
        self.assertNotIn("ERROR:", hits)
        listing = sandbox.list_dir("src")
        self.assertTrue(listing.startswith("DIR src"))
        game_scripts = sandbox.list_dir("scripts")
        self.assertTrue(game_scripts.startswith("DIR scripts"))
        self.assertIn("dev-full.mjs", game_scripts)
        bad = sandbox.list_dir("../..")
        self.assertTrue(bad.startswith("ERROR:"))

    def test_repo_sandbox_rejects_private_surfaces_when_root_is_misconfigured(self) -> None:
        sandbox = RepoSandbox(ROOT)
        hidden_spec = ROOT / "specs/spacerpg/equipment-loadout.yaml"

        relative = sandbox.read_file("specs/spacerpg/equipment-loadout.yaml")
        absolute = sandbox.read_file(str(hidden_spec))
        specs_dir = sandbox.list_dir("specs")
        scripts_file = sandbox.read_file("scripts/play_luanti.py")
        scripts_dir = sandbox.list_dir("scripts")
        state_loop = sandbox.read_file("benchmarks/state_loop/tasks.yaml")
        root_listing = sandbox.list_dir(".")
        core_listing = sandbox.list_dir("benchmarks/core")
        grep_hits = sandbox.grep("golden_image")
        script_hits = sandbox.grep("play_luanti")
        repo_map = sandbox.build_repo_map()

        self.assertTrue(relative.startswith("ERROR: access to private grader data is not allowed"))
        self.assertTrue(absolute.startswith("ERROR: access to private grader data is not allowed"))
        self.assertTrue(specs_dir.startswith("ERROR: access to private grader data is not allowed"))
        self.assertTrue(scripts_file.startswith("ERROR: access to private grader data is not allowed"))
        self.assertTrue(scripts_dir.startswith("ERROR: access to private grader data is not allowed"))
        self.assertTrue(state_loop.startswith("ERROR: access to private grader data is not allowed"))
        self.assertTrue(all(not entry.startswith("specs/") for entry in sandbox.reads))
        self.assertTrue(all(not entry.startswith("scripts/") for entry in sandbox.reads))
        self.assertTrue(all(not entry.startswith("benchmarks/core/hidden") for entry in sandbox.reads))
        self.assertTrue(all(not entry.startswith("benchmarks/state_loop") for entry in sandbox.reads))
        self.assertNotIn("specs/", root_listing)
        self.assertNotIn("scripts/", root_listing)
        self.assertNotIn(".warptest-cache", root_listing)
        self.assertNotIn("hidden", core_listing)
        self.assertFalse(any(line.startswith("specs/") for line in grep_hits.splitlines()))
        self.assertFalse(any(line.startswith("benchmarks/core/hidden") for line in grep_hits.splitlines()))
        self.assertFalse(any(line.startswith("scripts/") for line in script_hits.splitlines()))
        self.assertNotIn("specs/", repo_map)
        self.assertNotIn("  scripts/", repo_map.splitlines())
        self.assertNotIn("oracles.yaml", repo_map)

    def test_repo_sandbox_allows_a_sanitized_repo_root_under_warptest_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            benchmark = Path(tmp)
            tasks = benchmark / "benchmarks/core/public/tasks.yaml"
            tasks.parent.mkdir(parents=True)
            tasks.write_text("tasks: []\n", encoding="utf-8")
            (benchmark / "games").mkdir()
            repo = benchmark / ".warptest-cache/lazarus/agent-view"
            source = repo / "builtin/common/vector.lua"
            source.parent.mkdir(parents=True)
            source.write_text("local function read_vector() end\n", encoding="utf-8")

            sandbox = RepoSandbox(repo)
            repo_map = sandbox.build_repo_map()
            contents = sandbox.read_file("builtin/common/vector.lua")

        self.assertIn("builtin/", repo_map)
        self.assertIn("vector.lua", repo_map)
        self.assertTrue(contents.startswith("FILE builtin/common/vector.lua"))
        self.assertEqual(sandbox.reads, ["builtin/common/vector.lua"])

    def test_repo_sandbox_does_not_exempt_a_direct_private_root(self) -> None:
        sandbox = RepoSandbox(ROOT / "specs")

        contents = sandbox.read_file("spacerpg/equipment-loadout.yaml")
        repo_map = sandbox.build_repo_map()

        self.assertTrue(
            contents.startswith("ERROR: access to private grader data is not allowed")
        )
        self.assertNotIn("equipment-loadout.yaml", repo_map)

    def test_repo_sandbox_traversal_skips_symlinks(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink is not available on this platform")

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            external = base / "external"
            repo.mkdir()
            external.mkdir()
            (repo / "normal.ts").write_text("export const SAFE_VALUE = true;\n", encoding="utf-8")
            (external / "secret.ts").write_text("export const SECRET_VALUE = true;\n", encoding="utf-8")
            os.symlink(external / "secret.ts", repo / "linked-secret.ts")
            os.symlink(external, repo / "linked-external")

            sandbox = RepoSandbox(repo)
            hits = sandbox.grep("SECRET_VALUE")
            safe_hits = sandbox.grep("SAFE_VALUE")
            repo_map = sandbox.build_repo_map()

        self.assertIn("SAFE_VALUE", safe_hits)
        self.assertTrue(hits.startswith("No matches for pattern"))
        self.assertNotIn("linked-secret.ts:", hits)
        self.assertNotIn("secret.ts:", hits)
        self.assertNotIn("linked-secret.ts", repo_map)
        self.assertNotIn("linked-external", repo_map)
        self.assertNotIn("secret.ts", repo_map)
        self.assertIn("normal.ts", repo_map)

    def test_parse_react_reply_variants(self) -> None:
        self.assertEqual(parse_react_reply("FINAL\n```yaml\na: 1\n```"), ("final", "a: 1"))
        self.assertEqual(
            parse_react_reply('ACTION: grep\n{"pattern": "x", "glob": "*.ts"}'),
            ("action", "grep", {"pattern": "x", "glob": "*.ts"}),
        )
        self.assertEqual(parse_react_reply("just chatting")[0], "error")

    def test_parse_react_reply_rejects_mixed_action_and_final_blocks(self) -> None:
        reply = (
            'ACTION: list_dir\n{"path": "."}\n'
            'ACTION: read_file\n{"path": "public/source.cs"}\n'
            "FINAL\n```yaml\ntask_id: candidate\n```"
        )

        legacy = parse_react_reply(reply)
        kind, detail = parse_react_reply(reply, strict_protocol=True)

        self.assertEqual(legacy[0], "final")
        self.assertEqual(kind, "error")
        self.assertIn("exactly one", detail)

    def test_explore_prompt_has_no_vocabulary_or_graph(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "spacerpg-station-save")
        system = build_explore_system_prompt(task)
        initial = build_explore_initial_prompt(task, ROOT / "games/SpaceRPG", "repo-map-placeholder")
        combined = system + "\n" + initial
        self.assertEqual(detect_private_grader_references(combined), [])
        self.assertNotIn("source_vocabulary", combined)
        self.assertNotIn("store_actions", combined)
        # Public issues may name target fields; private grader files and oracle keys must stay out.
        self.assertNotIn("benchmarks/core/hidden", combined)
        self.assertNotIn("hidden_spec", combined)
        self.assertNotIn("\ngraph:", combined)
        self.assertIn("ACTION: read_file", system)
        self.assertIn("test_hook", system)

    def test_explore_prompt_includes_state_loop_feedback(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        initial = build_explore_initial_prompt(
            task,
            ROOT / "games/2048",
            "repo-map-placeholder",
            feedback={"after": {"label": "incorrect", "rationale": "wrong merge result"}},
        )
        self.assertIn("PRIOR ATTEMPT FAILED", initial)
        self.assertIn("wrong merge result", initial)
        self.assertEqual(detect_private_grader_references(initial), [])

    def test_exploration_agent_runs_react_loop_with_fake_client(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        spec = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT).generated_spec
        final_yaml = (
            "FINAL\n"
            "```yaml\n"
            "task_id: 2048-merge-left\n"
            "benchmark: '2048'\n"
            f"generated_spec: {spec!r}\n"
            "source_evidence:\n"
            "  - source_path: js/game_manager.js\n"
            "    matched_symbol: gameStateKey\n"
            "    rationale: Found localStorage key via grep.\n"
            "payload:\n"
            "  kind: localStorage\n"
            "runtime_validation:\n"
            "  status: pending\n"
            "validity_class: save-restorable\n"
            "```\n"
        )

        class FakeReactClient:
            def __init__(self) -> None:
                self.replies = [
                    'ACTION: grep\n{"pattern": "gameStateKey", "glob": "*.js"}',
                    'ACTION: read_file\n{"path": "js/game_manager.js", "start": 1, "end": 20}',
                    final_yaml,
                ]
                self.calls = 0

            def converse(self, model: str, messages: list) -> ModelCallResult:
                content = self.replies[min(self.calls, len(self.replies) - 1)]
                self.calls += 1
                return ModelCallResult(content=content, latency_ms=5)

        with tempfile.TemporaryDirectory() as tmp:
            agent = ExploratoryLLMCheckpointSynthesisAgent(model="fake/model", client=FakeReactClient())
            checkpoint = agent.synthesize(task, ROOT, artifact_dir=Path(tmp))
            transcript = json.loads((Path(tmp) / "transcript.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint.generated_by, "fake/model")
        self.assertEqual(checkpoint.payload["kind"], "localStorage")
        self.assertEqual(validate_method_checkpoint(checkpoint, ROOT), [])
        self.assertTrue(any(entry.endswith("js/game_manager.js") for entry in checkpoint.inputs_read))
        self.assertGreaterEqual(len(transcript["messages"]), 4)

    def test_lazarus_exploration_rejects_source_evidence_for_an_unread_file(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        task = copy.deepcopy(task)
        task.tags.extend(["c1", "lazarus"])
        spec = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT).generated_spec
        final_yaml = (
            "FINAL\n"
            "```yaml\n"
            "task_id: 2048-merge-left\n"
            "benchmark: '2048'\n"
            f"generated_spec: {spec!r}\n"
            "source_evidence:\n"
            "  - source_path: js/game_manager.js\n"
            "    matched_symbol: gameStateKey\n"
            "    rationale: Claims evidence without reading the file.\n"
            "payload:\n"
            "  kind: localStorage\n"
            "runtime_validation:\n"
            "  status: pending\n"
            "  detail: not run\n"
            "validity_class: save-restorable\n"
            "```\n"
        )

        class UnreadEvidenceClient:
            def __init__(self) -> None:
                self.replies = ['ACTION: list_dir\n{"path": "js"}', final_yaml]
                self.calls = 0

            def converse(self, model: str, messages: list) -> ModelCallResult:
                content = self.replies[min(self.calls, len(self.replies) - 1)]
                self.calls += 1
                return ModelCallResult(content=content, latency_ms=1)

        agent = ExploratoryLLMCheckpointSynthesisAgent(
            model="fake/model",
            client=UnreadEvidenceClient(),
        )

        with self.assertRaisesRegex(SynthesisError, "source evidence.*not read"):
            agent.synthesize(task, ROOT)

    def test_lazarus_exploration_can_grep_lua_source_evidence(self) -> None:
        base = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        generated_spec = DeterministicCheckpointSynthesisAgent().synthesize(
            base, ROOT
        ).generated_spec
        final_yaml = (
            "FINAL\n```yaml\n"
            "task_id: 2048-merge-left\n"
            "benchmark: '2048'\n"
            f"generated_spec: {generated_spec!r}\n"
            "source_evidence:\n"
            "  - source_path: vector.lua\n"
            "    matched_symbol: from_string\n"
            "    rationale: Located the implementation through the Lazarus grep tool.\n"
            "payload:\n  kind: localStorage\n"
            "runtime_validation:\n  status: pending\n  detail: not run\n"
            "validity_class: save-restorable\n```"
        )

        class LuaEvidenceClient:
            def __init__(self) -> None:
                self.replies = [
                    'ACTION: grep\n{"pattern": "from_string", "glob": "*.lua"}',
                    final_yaml,
                ]
                self.calls = 0

            def converse(self, model: str, messages: list) -> ModelCallResult:
                del model, messages
                content = self.replies[min(self.calls, len(self.replies) - 1)]
                self.calls += 1
                return ModelCallResult(content=content, latency_ms=1)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "vector.lua").write_text(
                "function vector.from_string(value) return value end\n",
                encoding="utf-8",
            )
            task = PublicIssue(
                id=base.id,
                benchmark=base.benchmark,
                repo="repo",
                issue=base.issue,
                tags=[*base.tags, "c1", "lazarus"],
                budget_ms=base.budget_ms,
                prompt_revision=base.prompt_revision,
                random_control=base.random_control,
            )
            checkpoint = ExploratoryLLMCheckpointSynthesisAgent(
                model="fake/model",
                client=LuaEvidenceClient(),
                max_steps=3,
            ).synthesize(task, root)

        self.assertEqual(checkpoint.source_evidence[0]["source_path"], "vector.lua")
        self.assertTrue(any(path.endswith("vector.lua") for path in checkpoint.inputs_read))

    def test_lazarus_exploration_rejects_a_zero_tool_final_before_source_read(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        task = copy.deepcopy(task)
        task.tags.extend(["c1", "lazarus"])
        spec = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT).generated_spec
        final_yaml = (
            "FINAL\n```yaml\n"
            "task_id: 2048-merge-left\n"
            "benchmark: '2048'\n"
            f"generated_spec: {spec!r}\n"
            "source_evidence:\n"
            "  - source_path: js/game_manager.js\n"
            "    matched_symbol: gameStateKey\n"
            "    rationale: Read the public storage implementation.\n"
            "payload:\n  kind: localStorage\n"
            "runtime_validation:\n  status: pending\n  detail: not run\n"
            "validity_class: save-restorable\n```"
        )

        class ZeroToolThenReadClient:
            def __init__(self) -> None:
                self.replies = [
                    final_yaml,
                    'ACTION: read_file\n{"path": "js/game_manager.js", "start": 1, "end": 20}',
                    final_yaml,
                ]
                self.calls = 0
                self.saw_rejection = False

            def converse(self, model: str, messages: list) -> ModelCallResult:
                del model
                if self.calls == 1:
                    self.saw_rejection = any(
                        "at least one repository tool" in str(message.get("content") or "")
                        for message in messages
                    )
                content = self.replies[min(self.calls, len(self.replies) - 1)]
                self.calls += 1
                return ModelCallResult(content=content, latency_ms=1)

        client = ZeroToolThenReadClient()
        agent = ExploratoryLLMCheckpointSynthesisAgent(
            model="fake/model", client=client, max_steps=4
        )
        checkpoint = agent.synthesize(task, ROOT)

        self.assertTrue(client.saw_rejection)
        self.assertEqual(checkpoint.model_generated_spec, spec)

    def test_non_c1_exploration_keeps_accepting_a_zero_tool_final(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")
        spec = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT).generated_spec
        final_yaml = (
            "FINAL\n```yaml\n"
            "task_id: 2048-merge-left\n"
            "benchmark: '2048'\n"
            f"generated_spec: {spec!r}\n"
            "source_evidence:\n"
            "  - source_path: public issue\n"
            "    matched_symbol: merge-left\n"
            "    rationale: The public issue fully states the transition.\n"
            "payload:\n  kind: localStorage\n"
            "runtime_validation:\n  status: pending\n  detail: not run\n"
            "validity_class: save-restorable\n```"
        )

        class ZeroToolClient:
            calls = 0

            def converse(self, model: str, messages: list) -> ModelCallResult:
                del model, messages
                self.calls += 1
                return ModelCallResult(content=final_yaml, latency_ms=1)

        client = ZeroToolClient()
        checkpoint = ExploratoryLLMCheckpointSynthesisAgent(
            model="fake/model", client=client, max_steps=2
        ).synthesize(task, ROOT)

        self.assertEqual(client.calls, 1)
        self.assertEqual(checkpoint.model_generated_spec, spec)

    def test_exploration_agent_fails_without_final_within_steps(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "2048-merge-left")

        class NeverFinalClient:
            def converse(self, model: str, messages: list) -> ModelCallResult:
                return ModelCallResult(content='ACTION: list_dir\n{"path": "."}', latency_ms=1)

        agent = ExploratoryLLMCheckpointSynthesisAgent(model="fake/model", client=NeverFinalClient(), max_steps=3)
        checkpoint = agent.synthesize(task, ROOT)
        self.assertTrue(checkpoint.used_public_scaffold)
        self.assertIsNone(checkpoint.model_generated_spec)
        self.assertEqual(checkpoint.payload["kind"], "localStorage")
        self.assertEqual(validate_method_checkpoint(checkpoint, ROOT), [])

    def test_public_segment_bank_is_separate_from_core_synthesis(self) -> None:
        segments = load_public_segments(ROOT)
        hidden_bugs = load_hidden_bugs(ROOT)
        self.assertGreaterEqual(len(segments), 5)
        self.assertGreaterEqual(len(hidden_bugs), 5)
        self.assertEqual(validate_public_segment_bank(ROOT), [])
        for segment in segments:
            self.assertNotIn("core/hidden", segment.start_spec)
            self.assertIn("segment-bank", segment.tags)

    def test_segment_bug_discovery_uses_same_public_segment_bank(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_segment_bug_discovery(ROOT, agents=["segment_probe", "smoke_only"], parallel=True, report_dir=Path(tmp))
            self.assertTrue((Path(tmp) / "segment-bugs.json").exists())
        self.assertEqual(result["summary"]["bank_error_count"], 0)
        self.assertIn("segment_probe", result["summary"]["agents"])
        self.assertTrue(all(row["same_segment_bank_for_all_methods"] for row in result["results"]))
        self.assertTrue(all(row["hidden_oracle_used_only_by_grader"] for row in result["results"]))

    def test_generic_gui_adapter_declares_browser_scope_and_native_boundary(self) -> None:
        browser = GenericGuiAppAdapter().inspect(ROOT / "games/2048")
        self.assertEqual(browser.runtime_family, "browser-web")
        self.assertEqual(browser.checkpointing_status, "supported_by_browser_runtime_adapter")
        native = GenericGuiAppAdapter().inspect(ROOT)
        self.assertIn(native.checkpointing_status, {"explicitly_unsupported_for_v1", "unsupported", "supported_by_browser_runtime_adapter"})

    def test_spacerpg_gui_baseline_trace_forbids_checkpoint_hooks(self) -> None:
        safe_trace = [{"type": "click", "target": "button:New Game"}, {"type": "keyboard", "key": "KeyF"}]
        unsafe_trace = [{"type": "evaluate", "call": "window.__SPACERPG_E2E__.setState"}]
        self.assertEqual(forbidden_gui_action_calls(safe_trace), [])
        self.assertEqual(forbidden_gui_action_calls(unsafe_trace), ["setState"])

    def test_generic_gui_adapter_reports_electron_fixture(self) -> None:
        electron = GenericGuiAppAdapter().inspect(ROOT / "tests/fixtures/electron_app")
        self.assertEqual(electron.runtime_family, "electron")
        self.assertEqual(electron.checkpointing_status, "supported_by_electron_runtime_probe")
        self.assertIn("browser_window_dom", electron.supported_evidence_channels)

    def test_profile_completion_guard_fails_on_console_errors(self) -> None:
        inspect_result = RepositoryInspector().inspect(ROOT / "games/2048")
        profile = Game2048Profile(inspect_result)
        runtime = SimpleNamespace(
            launch_command="test-server",
            app_url="http://127.0.0.1:1",
            console_errors=["boom"],
            page_errors=[],
        )
        report = profile.base_report(ROOT / "specs/2048/merge-left.yaml", runtime, "test_strategy", "unknown")
        report.target_validated_before_smoke = True
        report.smoke_execution = PhaseResult("success")
        report.oracle_result = PhaseResult("success")
        profile._complete_report(report, runtime, start=0.0, metrics={"actions_replayed": 0})
        self.assertIn("cannot yet classify", report.validity_explanation)
        self.assertEqual(report.post_checkpoint_interaction["status"], "success")
        self.assertEqual(report.oracle_result.status, "failure")
        self.assertEqual(report.overall_status, "failure")
        self.assertEqual(report.metrics["actions_replayed"], 0)

    def test_validation_signals_include_render_readiness(self) -> None:
        inspect_result = RepositoryInspector().inspect(ROOT / "games/SpaceRPG")
        profile = SpaceRPGProfile(inspect_result)
        runtime = SimpleNamespace(launch_command="test-server", app_url="http://127.0.0.1:1")
        report = profile.base_report(ROOT / "specs/spacerpg/playable-flight.yaml", runtime, "test_strategy", "unknown")
        report.validation = PhaseResult(
            "success",
            data={
                "memory_persistence_diff": {"matches": True},
                "soft_lock_signal": {"game_over_screen": False},
                "render_readiness_signal": {"canvas": {"present": True}, "hook": {"storeReady": True}},
            },
        )
        profile._record_validation_signals(report)
        self.assertEqual(report.render_readiness_signal["hook"]["storeReady"], True)
        self.assertIn("getReadiness", (ROOT / "games/SpaceRPG/src/state/gameStore.ts").read_text(encoding="utf-8"))

    def test_spacerpg_flight_transition_waits_for_new_render_frames(self) -> None:
        inspect_result = RepositoryInspector().inspect(ROOT / "games/SpaceRPG")
        profile = SpaceRPGProfile(inspect_result)

        class FakePage:
            def __init__(self) -> None:
                self.wait_arg: dict[str, int] | None = None
                self.extra_wait_ms: int | None = None

            def wait_for_function(
                self,
                script: str,
                *,
                arg: dict[str, int],
                timeout: int,
            ) -> None:
                self.wait_arg = arg
                self.assert_in_readiness_contract(script)
                self.assert_timeout(timeout)

            @staticmethod
            def assert_in_readiness_contract(script: str) -> None:
                if "assetManifestReady" not in script or "settleFrames" not in script:
                    raise AssertionError("expected SpaceRPG loaded-frame readiness contract")

            @staticmethod
            def assert_timeout(timeout: int) -> None:
                if timeout != 120_000:
                    raise AssertionError(f"unexpected timeout: {timeout}")

            def evaluate(self, script: str) -> dict[str, object]:
                if "getReadiness" not in script:
                    raise AssertionError("expected SpaceRPG readiness evidence")
                return {
                    "currentScreen": "flight",
                    "renderFrame": 112,
                    "assetManifestReady": True,
                }

            def wait_for_timeout(self, timeout: int) -> None:
                self.extra_wait_ms = timeout

        page = FakePage()
        readiness = profile._wait_for_flight_scene_rendered(
            SimpleNamespace(page=page),
            previous_render_frame=100,
        )
        self.assertEqual(
            page.wait_arg,
            {"previousRenderFrame": 100, "settleFrames": 12},
        )
        self.assertEqual(page.extra_wait_ms, 3_000)
        self.assertEqual(readiness["renderFrame"], 112)

        from warptest.c1_task_deadline import c1_task_deadline

        bounded_page = FakePage()
        bounded_page.assert_timeout = lambda value: self.assertTrue(0 < value <= 1000)
        with c1_task_deadline("spacerpg-render-wait", timeout_s=1):
            profile._wait_for_flight_scene_rendered(
                SimpleNamespace(page=bounded_page), previous_render_frame=100
            )
        self.assertTrue(0 < bounded_page.extra_wait_ms <= 1000)

    def test_spacerpg_recovery_screen_fails_no_severe_console_oracle(self) -> None:
        inspect_result = RepositoryInspector().inspect(ROOT / "games/SpaceRPG")
        profile = SpaceRPGProfile(inspect_result)

        class RecoveryPage:
            def evaluate(self, script: str, *_args: object) -> bool:
                self.assert_recovery_probe(script)
                return True

            @staticmethod
            def assert_recovery_probe(script: str) -> None:
                if "Unable to continue" not in script:
                    raise AssertionError("expected SpaceRPG recovery-screen probe")

        result = profile._assert_oracles(
            SimpleNamespace(page=RecoveryPage()),
            [{"type": "no_severe_console"}],
        )
        self.assertEqual(result.status, "failure")
        self.assertIn("recovery screen", result.detail)

    def test_jupyterlab_inspect_profile_and_schema(self) -> None:
        result = RepositoryInspector().inspect(ROOT / "games/jupyterlab")
        self.assertEqual(result.profile, "JupyterLabProfile")
        self.assertEqual(result.capabilities.hook_name, "jupyter server contents/session api")
        self.assertTrue(result.capabilities.browser_runtime)
        self.assertTrue(result.capabilities.backend_or_external_api)
        self.assertIn("JupyterLabRuntimeAdapter", result.adapter_layers)
        self.assertEqual(result.instrumentation_mode, "none")
        schema = result.semantic_schema
        field_names = {field.name for field in schema.fields}
        surface_kinds = {surface.kind for surface in schema.surfaces}
        self.assertIn("jupyter_lab_server", surface_kinds)
        self.assertIn("jupyter_contents_api", surface_kinds)
        self.assertIn("jupyter_sessions_api", surface_kinds)
        self.assertIn("jupyter_workspace_metadata", surface_kinds)
        self.assertIn("target.path", field_names)
        self.assertIn("workspace.paths", field_names)
        self.assertIn("session.kernel", field_names)
        self.assertTrue(all(surface.evidence for surface in schema.surfaces))
        self.assertTrue(all(field.evidence for field in schema.fields))

    def test_jupyterlab_strategy_selector_reports_replay_unsupported(self) -> None:
        spec = load_spec(ROOT / "specs/jupyterlab/text-file-edit.yaml")
        plan = StrategySelector.select(
            spec,
            profile_name="JupyterLabProfile",
            default_strategy="jupyterlab_workspace_api_validate",
            default_validity="save-restorable",
            mode="replay_from_start",
        )
        self.assertEqual(plan.unsupported_reason, JUPYTERLAB_REPLAY_UNSUPPORTED_REASON)

    def test_jupyterlab_spec_schema_validates_all_bundled_specs(self) -> None:
        spec_paths = sorted((ROOT / "specs/jupyterlab").glob("*.yaml"))
        self.assertEqual(len(spec_paths), 9)
        for spec_path in spec_paths:
            with self.subTest(spec=str(spec_path)):
                self.assertIsInstance(load_spec(spec_path), dict)

    def test_jupyterlab_benchmark_resolve_repo(self) -> None:
        spec = {"benchmark": "JupyterLab"}
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                resolved = resolve_repo(spec, ROOT / "specs/jupyterlab/text-file-edit.yaml", None)
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(resolved, ROOT / "games/jupyterlab")
        override = resolve_repo(spec, ROOT / "specs/jupyterlab/text-file-edit.yaml", str(ROOT / "games/jupyterlab"))
        self.assertEqual(override, ROOT / "games/jupyterlab")

    def test_jupyterlab_deterministic_synthesis(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "jupyterlab-text-file-edit")
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT)
        self.assertEqual(checkpoint.hidden_files_read, [])
        self.assertEqual(checkpoint.payload["kind"], "jupyterlab_workspace_checkpoint")
        self.assertEqual(validate_method_checkpoint(checkpoint, ROOT), [])

    def test_jupyterlab_profile_run_maps_api_checks_to_phases(self) -> None:
        inspect_result = RepositoryInspector().inspect(ROOT / "games/jupyterlab")
        profile = JupyterLabProfile(inspect_result)
        spec = load_spec(ROOT / "specs/jupyterlab/text-file-edit.yaml")

        class FakeJupyterLabRuntime:
            launch_command = "python -m jupyterlab ..."
            app_url = "http://127.0.0.1:8888/lab"
            logs: list[str] = []

            def run_checkpoint(self, _spec: dict, _spec_path: Path) -> dict:
                return {
                    "status": "success",
                    "exit_code": None,
                    "request_path": "req.json",
                    "report_path": "rep.json",
                    "checks": [
                        {"name": "target.state_synthesized", "status": "success", "detail": ""},
                        {"name": "target.validate.kind", "status": "success", "detail": ""},
                        {"name": "action[jupyterlab_open_file]@0", "status": "success", "detail": ""},
                        {"name": "action[jupyterlab_edit_text_file]@1", "status": "success", "detail": ""},
                        {"name": "assertion[jupyterlab_file_text_equals]@1", "status": "success", "detail": ""},
                        {"name": "assertion[no_jupyterlab_server_errors]@2", "status": "success", "detail": ""},
                    ],
                }

        report = profile.run(spec, ROOT / "specs/jupyterlab/text-file-edit.yaml", FakeJupyterLabRuntime(), mode="jump_and_validate")
        self.assertEqual(report.checkpoint_restoration.status, "success")
        self.assertEqual(report.validation.status, "success")
        self.assertEqual(report.smoke_execution.status, "success")
        self.assertEqual(report.oracle_result.status, "success")
        self.assertEqual(report.overall_status, "success")

    def test_libreoffice_inspect_profile_and_schema(self) -> None:
        result = RepositoryInspector().inspect(ROOT / "games/LibreOffice")
        self.assertEqual(result.profile, "LibreOfficeProfile")
        self.assertEqual(result.capabilities.hook_name, "uno/odf document api")
        self.assertFalse(result.capabilities.browser_runtime)
        self.assertTrue(result.capabilities.backend_or_external_api)
        self.assertIn("LibreOfficeRuntimeAdapter", result.adapter_layers)
        self.assertIn("LibreOfficeUNOAdapter", result.adapter_layers)
        self.assertEqual(result.instrumentation_mode, "none")
        schema = result.semantic_schema
        self.assertIn("uno_component_context", {surface.kind for surface in schema.surfaces})
        self.assertIn("writer_document_model", {surface.kind for surface in schema.surfaces})
        self.assertIn("calc_document_model", {surface.kind for surface in schema.surfaces})
        self.assertIn("impress_presentation_model", {surface.kind for surface in schema.surfaces})
        self.assertIn("writer.text", {field.name for field in schema.fields})
        self.assertIn("calc.formula", {field.name for field in schema.fields})
        self.assertIn("impress.slide", {field.name for field in schema.fields})
        self.assertIn("soffice --headless", result.metadata.launch_command)

    def test_libreoffice_strategy_selector_reports_replay_unsupported(self) -> None:
        spec = load_spec(ROOT / "specs/libreoffice/writer-board-report-toc.yaml")
        plan = StrategySelector.select(
            spec,
            profile_name="LibreOfficeProfile",
            default_strategy="libreoffice_uno_document_validate",
            default_validity="save-restorable",
            mode="replay_from_start",
        )
        self.assertEqual(plan.unsupported_reason, LIBREOFFICE_REPLAY_UNSUPPORTED_REASON)

    def test_libreoffice_spec_schema_validates_all_bundled_specs(self) -> None:
        spec_paths = sorted((ROOT / "specs/libreoffice").glob("*.yaml"))
        self.assertEqual(len(spec_paths), 13)
        for spec_path in spec_paths:
            with self.subTest(spec=str(spec_path)):
                self.assertIsInstance(load_spec(spec_path), dict)

    def test_libreoffice_benchmark_resolve_repo(self) -> None:
        spec = {"benchmark": "LibreOffice"}
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                resolved = resolve_repo(spec, ROOT / "specs/libreoffice/writer-board-report-toc.yaml", None)
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(resolved, ROOT / "games/LibreOffice")
        override = resolve_repo(spec, ROOT / "specs/libreoffice/writer-board-report-toc.yaml", str(ROOT / "games/LibreOffice"))
        self.assertEqual(override, ROOT / "games/LibreOffice")

    def test_libreoffice_deterministic_synthesis(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "libreoffice-writer-board-report-toc")
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT)
        self.assertEqual(checkpoint.hidden_files_read, [])
        self.assertEqual(checkpoint.payload["kind"], "libreoffice_uno_document_checkpoint")
        self.assertEqual(validate_method_checkpoint(checkpoint, ROOT), [])

    def test_libreoffice_profile_run_maps_uno_checks_to_phases(self) -> None:
        inspect_result = RepositoryInspector().inspect(ROOT / "games/LibreOffice")
        profile = LibreOfficeProfile(inspect_result)
        spec = load_spec(ROOT / "specs/libreoffice/writer-board-report-toc.yaml")

        class FakeLibreOfficeRuntime:
            launch_command = "soffice --headless --accept=pipe,name=test;urp;"
            app_url = None
            logs: list[str] = []

            def run_checkpoint(self, _spec: dict, _spec_path: Path) -> dict:
                return {
                    "status": "success",
                    "exit_code": 0,
                    "request_path": "req.json",
                    "report_path": "rep.json",
                    "soffice": "/tmp/soffice",
                    "soffice_version": "LibreOffice Test",
                    "python": sys.executable,
                    "document_path": "writer-board-report-toc.odt",
                    "checks": [
                        {"name": "target.state_synthesized", "status": "success", "detail": ""},
                        {"name": "target.validate.kind", "status": "success", "detail": ""},
                        {"name": "action[libreoffice_open_document]@0", "status": "success", "detail": ""},
                        {"name": "action[libreoffice_insert_toc]@1", "status": "success", "detail": ""},
                        {"name": "action[libreoffice_set_table_cell]@3", "status": "success", "detail": ""},
                        {"name": "assertion[libreoffice_toc_contains]@0", "status": "success", "detail": ""},
                        {"name": "assertion[libreoffice_table_cell_equals]@2", "status": "success", "detail": ""},
                        {"name": "assertion[no_libreoffice_errors]@4", "status": "success", "detail": ""},
                    ],
                }

        report = profile.run(spec, ROOT / "specs/libreoffice/writer-board-report-toc.yaml", FakeLibreOfficeRuntime(), mode="jump_and_validate")
        self.assertEqual(report.checkpoint_restoration.status, "success")
        self.assertEqual(report.validation.status, "success")
        self.assertEqual(report.smoke_execution.status, "success")
        self.assertEqual(report.oracle_result.status, "success")
        self.assertEqual(report.metrics["soffice_version"], "LibreOffice Test")
        self.assertEqual(report.overall_status, "success")

    def test_libreoffice_runtime_missing_pyuno_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            soffice = root / "soffice"
            soffice.write_text("#!/bin/sh\n", encoding="utf-8")
            soffice.chmod(0o755)
            with (
                patch.dict(os.environ, {"WARPTEST_LIBREOFFICE_SOFFICE": str(soffice)}, clear=False),
                patch(
                    "warptest.libreoffice_runtime.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout="LibreOffice Test", stderr=""),
                ),
            ):
                adapter = LibreOfficeRuntimeAdapter(ROOT / "games/LibreOffice", root / "report")
            with patch(
                "warptest.libreoffice_runtime.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr="ModuleNotFoundError: No module named 'uno'"),
            ):
                with self.assertRaises(RuntimeErrorWithLog) as ctx:
                    adapter._ensure_python_dependency()
        self.assertIn("WARPTEST_LIBREOFFICE_PYTHON", str(ctx.exception))
        self.assertIn("WARPTEST_LIBREOFFICE_SOFFICE", str(ctx.exception))
        self.assertIn("uno/pyuno", str(ctx.exception))

    def test_libreoffice_helper_inserts_real_bookmark_reference_text(self) -> None:
        Checkpoint = _load_libreoffice_checkpoint_class()

        class FakeRange:
            def __init__(self, text: "FakeText", value: str) -> None:
                self.Text = text
                self.String = value

        class FakeBookmarks:
            def __init__(self) -> None:
                self.items: dict[str, SimpleNamespace] = {}

            def hasByName(self, name: str) -> bool:
                return name in self.items

            def getByName(self, name: str) -> SimpleNamespace:
                return self.items[name]

            def add(self, name: str, anchor_text: str) -> None:
                self.items[name] = SimpleNamespace(Anchor=SimpleNamespace(String=anchor_text))

        class FakeText:
            def __init__(self, body: str, doc: "FakeWriterDoc") -> None:
                self.String = body
                self.doc = doc

            def createTextCursor(self) -> SimpleNamespace:
                return SimpleNamespace(gotoEnd=lambda _expand: None)

            def insertString(self, _cursor: object, value: str, _absorb: bool) -> None:
                self.String += value

            def insertTextContent(self, cursor: object, content: object, _absorb: bool) -> None:
                if hasattr(content, "SourceName"):
                    self.String += self.doc.Bookmarks.getByName(content.SourceName).Anchor.String
                    return
                self.doc.Bookmarks.add(content.Name, getattr(cursor, "String", ""))

        class FakeWriterDoc:
            def __init__(self) -> None:
                self.Bookmarks = FakeBookmarks()
                self.Text = FakeText("Incident Response Policy\nEscalation Matrix\n", self)
                self.TextFields = SimpleNamespace(refresh=lambda: None)

            def createInstance(self, service: str) -> SimpleNamespace:
                if service == "com.sun.star.text.Bookmark":
                    return SimpleNamespace(Name="")
                if service == "com.sun.star.text.TextField.GetReference":
                    return SimpleNamespace(SourceName="", ReferenceFieldSource=None, ReferenceFieldPart=None)
                raise AssertionError(service)

            def createSearchDescriptor(self) -> SimpleNamespace:
                return SimpleNamespace(SearchString="")

            def findFirst(self, descriptor: SimpleNamespace) -> FakeRange | None:
                if descriptor.SearchString in self.Text.String:
                    return FakeRange(self.Text, descriptor.SearchString)
                return None

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Checkpoint({"spec": {}, "spec_path": str(ROOT / "specs/libreoffice/writer-policy-cross-reference.yaml"), "runtime_root": tmp})
            checkpoint.doc_kind = "writer"
            checkpoint.doc = FakeWriterDoc()
            checkpoint._insert_bookmark("escalation_matrix", "Escalation Matrix")
            checkpoint._insert_reference_field("escalation_matrix", prefix="See section: ")

        self.assertIn("See section: Escalation Matrix", checkpoint.doc.Text.String)
        self.assertNotIn("[bookmark:", checkpoint.doc.Text.String)
        self.assertNotIn("See section: escalation_matrix", checkpoint.doc.Text.String)

    def test_libreoffice_helper_named_range_and_chart_fail_closed(self) -> None:
        Checkpoint = _load_libreoffice_checkpoint_class()

        class FailingNamedRanges:
            def addNewByName(self, *_args: object) -> None:
                raise RuntimeError("named range failed")

            def hasByName(self, _name: str) -> bool:
                return False

        class FailingCharts:
            def addNewByName(self, *_args: object) -> None:
                raise RuntimeError("chart failed")

            def hasByName(self, _name: str) -> bool:
                return False

        class FakeSheet:
            Charts = FailingCharts()

            def getCellRangeByName(self, _name: str) -> SimpleNamespace:
                return SimpleNamespace(CellAddress=SimpleNamespace(), RangeAddress=SimpleNamespace())

        class FakeSheets:
            def __init__(self) -> None:
                self.sheet = FakeSheet()

            def hasByName(self, _name: str) -> bool:
                return True

            def getByName(self, _name: str) -> FakeSheet:
                return self.sheet

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Checkpoint({"spec": {}, "spec_path": str(ROOT / "specs/libreoffice/calc-named-range-forecast.yaml"), "runtime_root": tmp})
            checkpoint.doc_kind = "calc"
            checkpoint.doc = SimpleNamespace(Sheets=FakeSheets(), NamedRanges=FailingNamedRanges())
            with self.assertRaises(RuntimeError):
                checkpoint._define_named_range("GrowthRate", "B3", "Inputs")
            with self.assertRaises(RuntimeError):
                checkpoint._create_chart("RevenueTrend", "A1:B5", "Inputs")

        self.assertNotIn("__named_range__", HELPER_SOURCE)
        self.assertNotIn("__chart__", HELPER_SOURCE)

    def test_libreoffice_helper_move_slide_preserves_full_text_shape_payload(self) -> None:
        Checkpoint = _load_libreoffice_checkpoint_class()

        class FakeShape:
            def __init__(self, name: str = "", text: str = "", x: int = 1, y: int = 2, width: int = 300, height: int = 120) -> None:
                self.Name = name
                self.String = text
                self._position = SimpleNamespace(X=x, Y=y)
                self._size = SimpleNamespace(Width=width, Height=height)

            def getPosition(self) -> SimpleNamespace:
                return self._position

            def getSize(self) -> SimpleNamespace:
                return self._size

            def setPosition(self, position: SimpleNamespace) -> None:
                self._position = position

            def setSize(self, size: SimpleNamespace) -> None:
                self._size = size

        class FakePage:
            def __init__(self, shapes: list[FakeShape]) -> None:
                self.shapes = list(shapes)

            def getCount(self) -> int:
                return len(self.shapes)

            def getByIndex(self, index: int) -> FakeShape:
                return self.shapes[index]

            def remove(self, shape: FakeShape) -> None:
                self.shapes.remove(shape)

            def add(self, shape: FakeShape) -> None:
                self.shapes.append(shape)

        class FakePages:
            def __init__(self) -> None:
                self.pages = [
                    FakePage([FakeShape("WarpTestTitle", "Appendix Metrics"), FakeShape("WarpTestCallout", "appendix callout")]),
                    FakePage([FakeShape("WarpTestTitle", "Customer Impact")]),
                    FakePage([
                        FakeShape("WarpTestTitle", "Executive Summary"),
                        FakeShape("WarpTestBullets", "- Draft summary\n- Needs final order"),
                        FakeShape("WarpTestChart", "executive chart payload"),
                    ]),
                    FakePage([FakeShape("WarpTestTitle", "Mitigation Plan")]),
                ]

            def getCount(self) -> int:
                return len(self.pages)

            def getByIndex(self, index: int) -> FakePage:
                return self.pages[index]

        class FakeImpressDoc:
            def __init__(self) -> None:
                self.pages = FakePages()

            def getDrawPages(self) -> FakePages:
                return self.pages

            def createInstance(self, service: str) -> FakeShape:
                if service != "com.sun.star.drawing.TextShape":
                    raise AssertionError(service)
                return FakeShape()

        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Checkpoint({"spec": {}, "spec_path": str(ROOT / "specs/libreoffice/impress-slide-reorder-summary.yaml"), "runtime_root": tmp})
            checkpoint.doc_kind = "impress"
            doc = FakeImpressDoc()
            checkpoint.doc = doc
            checkpoint._move_slide(3, 1)

        first_slide_text = "\n".join(shape.String for shape in doc.pages.pages[0].shapes)
        self.assertIn("Executive Summary", first_slide_text)
        self.assertIn("executive chart payload", first_slide_text)
        self.assertIn("Appendix Metrics", "\n".join(shape.String for shape in doc.pages.pages[1].shapes))

    def test_blender_inspect_profile_and_schema(self) -> None:
        result = RepositoryInspector().inspect(ROOT / "games/blender")
        self.assertEqual(result.profile, "BlenderProfile")
        self.assertEqual(result.capabilities.hook_name, "bpy/operator api")
        self.assertFalse(result.capabilities.browser_runtime)
        self.assertFalse(result.capabilities.backend_or_external_api)
        self.assertIn("BlenderRuntimeAdapter", result.adapter_layers)
        self.assertIn("BlenderGUIAppBundle", result.adapter_layers)
        self.assertIn("blender_gui_app", {surface.kind for surface in result.semantic_schema.surfaces})
        self.assertIn("scene.objects", {field.name for field in result.semantic_schema.fields})
        self.assertIn("Blender --factory-startup --python", result.metadata.launch_command)

    def test_blender_strategy_selector_reports_replay_unsupported(self) -> None:
        spec = load_spec(ROOT / "specs/blender/scene-cube-material.yaml")
        plan = StrategySelector.select(
            spec,
            profile_name="BlenderProfile",
            default_strategy="blender_python_scene_validate",
            default_validity="save-restorable",
            mode="replay_from_start",
        )
        self.assertEqual(plan.unsupported_reason, BLENDER_REPLAY_UNSUPPORTED_REASON)

    def test_blender_spec_schema_validates_all_bundled_specs(self) -> None:
        spec_paths = sorted((ROOT / "specs/blender").glob("*.yaml"))
        self.assertEqual(len(spec_paths), 7)
        for spec_path in spec_paths:
            with self.subTest(spec=str(spec_path)):
                self.assertIsInstance(load_spec(spec_path), dict)

    def test_blender_benchmark_resolve_repo(self) -> None:
        spec = {"benchmark": "Blender"}
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                resolved = resolve_repo(spec, ROOT / "specs/blender/scene-cube-material.yaml", None)
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(resolved, ROOT / "games/blender")
        override = resolve_repo(spec, ROOT / "specs/blender/scene-cube-material.yaml", str(ROOT / "games/blender"))
        self.assertEqual(override, ROOT / "games/blender")

    def test_blender_deterministic_synthesis(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "blender-scene-cube-material")
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT)
        self.assertEqual(checkpoint.hidden_files_read, [])
        self.assertEqual(checkpoint.payload["kind"], "blender_python_scene_checkpoint")
        self.assertEqual(validate_method_checkpoint(checkpoint, ROOT), [])

    def test_blender_profile_run_maps_checks_to_phases(self) -> None:
        inspect_result = RepositoryInspector().inspect(ROOT / "games/blender")
        profile = BlenderProfile(inspect_result)
        spec = load_spec(ROOT / "specs/blender/scene-cube-material.yaml")

        class FakeBlenderRuntime:
            launch_command = "Blender --factory-startup --python warptest/blender_driver.py -- {request} {report}"
            app_url = None
            logs: list[str] = []

            def run_checkpoint(self, _spec: dict, _spec_path: Path) -> dict:
                return {
                    "status": "success",
                    "exit_code": 0,
                    "request_path": "req.json",
                    "report_path": "rep.json",
                    "blend_artifacts": ["scene-cube-material.blend"],
                    "checks": [
                        {"name": "target.state_synthesized", "status": "success", "detail": ""},
                        {"name": "target.validate.scene", "status": "success", "detail": ""},
                        {"name": "action[blender_set_object_location]@0", "status": "success", "detail": ""},
                        {"name": "assertion[blender_object_exists]@0", "status": "success", "detail": ""},
                        {"name": "assertion[blender_material_color]@1", "status": "success", "detail": ""},
                        {"name": "assertion[no_blender_python_errors]@3", "status": "success", "detail": ""},
                    ],
                }

        report = profile.run(spec, ROOT / "specs/blender/scene-cube-material.yaml", FakeBlenderRuntime(), mode="jump_and_validate")
        self.assertEqual(report.checkpoint_restoration.status, "success")
        self.assertEqual(report.validation.status, "success")
        self.assertEqual(report.smoke_execution.status, "success")
        self.assertEqual(report.oracle_result.status, "success")
        self.assertEqual(report.metrics["blend_artifacts"], ["scene-cube-material.blend"])
        self.assertEqual(report.overall_status, "success")

    def test_blender_profile_reports_after_screenshot_first(self) -> None:
        inspect_result = RepositoryInspector().inspect(ROOT / "games/blender")
        profile = BlenderProfile(inspect_result)
        spec = load_spec(ROOT / "specs/blender/scene-cube-material.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "before.png"
            after = root / "after.png"
            before.write_bytes(b"before")
            after.write_bytes(b"after")

            class FakeBlenderRuntime:
                launch_command = "Blender --factory-startup --python warptest/blender_driver.py -- {request} {report}"
                app_url = None
                logs: list[str] = []

                def run_checkpoint(self, _spec: dict, _spec_path: Path) -> dict:
                    return {
                        "status": "success",
                        "exit_code": 0,
                        "request_path": "req.json",
                        "report_path": "rep.json",
                        "screenshot_before": str(before),
                        "screenshot_after": str(after),
                        "screenshots": {"before": str(before), "after": str(after)},
                        "checks": [
                            {"name": "target.state_synthesized", "status": "success", "detail": ""},
                            {"name": "target.validate.scene", "status": "success", "detail": ""},
                            {"name": "visual.screenshot_before", "status": "success", "detail": ""},
                            {"name": "action[blender_set_object_location]@0", "status": "success", "detail": ""},
                            {"name": "assertion[blender_object_exists]@0", "status": "success", "detail": ""},
                            {"name": "visual.screenshot_after", "status": "success", "detail": ""},
                        ],
                    }

            report = profile.run(spec, ROOT / "specs/blender/scene-cube-material.yaml", FakeBlenderRuntime(), mode="jump_and_validate")
            self.assertEqual(report.screenshots, [str(after), str(before)])

    def test_blender_runtime_executable_discovery_and_missing_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "Blender.app" / "Contents" / "MacOS" / "Blender"
            exe.parent.mkdir(parents=True)
            exe.write_text("#!/bin/sh\n", encoding="utf-8")
            exe.chmod(0o755)
            with patch.dict(os.environ, {"WARPTEST_BLENDER_EXECUTABLE": str(exe)}, clear=False):
                adapter = BlenderRuntimeAdapter(ROOT / "games/blender", root / "report", headless=False)
            self.assertEqual(adapter.executable, exe.resolve())
            self.assertNotIn("--background", adapter.launch_command)
            self.assertIn("--factory-startup", adapter.launch_command)

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                key: value
                for key, value in os.environ.items()
                if key not in {"WARPTEST_BLENDER_EXECUTABLE", "WARPTEST_BLENDER_BUILD_DIR"}
            }
            with patch.dict(os.environ, env, clear=True), patch("warptest.blender_runtime.shutil.which", return_value=None), patch("warptest.blender_runtime.Path.glob", return_value=[]):
                with self.assertRaises(Exception) as raised:
                    find_blender_executable(Path(tmp) / "blender")
        self.assertIn("No Blender executable found", str(raised.exception))

    def test_blender_runtime_converts_timeout_ms_to_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "Blender.app" / "Contents" / "MacOS" / "Blender"
            exe.parent.mkdir(parents=True)
            exe.write_text("#!/bin/sh\n", encoding="utf-8")
            exe.chmod(0o755)
            captured: dict[str, object] = {}

            def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
                captured["timeout"] = kwargs["timeout"]
                report_path = Path(command[-1])
                report_path.write_text(json.dumps({"status": "success", "checks": []}) + "\n", encoding="utf-8")
                return SimpleNamespace(returncode=0)

            with (
                patch.dict(os.environ, {"WARPTEST_BLENDER_EXECUTABLE": str(exe)}, clear=False),
                patch("warptest.blender_runtime.subprocess.run", side_effect=fake_run),
            ):
                adapter = BlenderRuntimeAdapter(ROOT / "games/blender", root / "report", headless=True)
                adapter.run_checkpoint({"timeout_ms": 2500}, ROOT / "specs/blender/scene-cube-material.yaml")

        self.assertEqual(captured["timeout"], 2.5)

    def test_blender_runtime_records_before_after_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / "Blender.app" / "Contents" / "MacOS" / "Blender"
            exe.parent.mkdir(parents=True)
            exe.write_text("#!/bin/sh\n", encoding="utf-8")
            exe.chmod(0o755)

            def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
                request_path = Path(command[-2])
                report_path = Path(command[-1])
                request = json.loads(request_path.read_text(encoding="utf-8"))
                artifact_dir = Path(request["artifact_dir"])
                before = artifact_dir / request["screenshot_before"]
                after = artifact_dir / request["screenshot_after"]
                before.parent.mkdir(parents=True)
                before.write_bytes(b"before")
                after.write_bytes(b"after")
                report_path.write_text(
                    json.dumps({"status": "success", "checks": [], "screenshot_before": str(before), "screenshot_after": str(after)}) + "\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0)

            with (
                patch.dict(os.environ, {"WARPTEST_BLENDER_EXECUTABLE": str(exe)}, clear=False),
                patch("warptest.blender_runtime.subprocess.run", side_effect=fake_run),
            ):
                adapter = BlenderRuntimeAdapter(ROOT / "games/blender", root / "report", headless=True)
                result = adapter.run_checkpoint({}, ROOT / "specs/blender/scene-cube-material.yaml")
                copied = Path(adapter.screenshot("visual-post.png"))

            self.assertEqual(Path(result["screenshot_before"]).read_bytes(), b"before")
            self.assertEqual(copied.read_bytes(), b"after")
            self.assertEqual(adapter.last_screenshot_metadata()["live_source"], "blender_render_capture")
            self.assertEqual(adapter.last_screenshot_metadata()["screenshot_after"], result["screenshot_after"])

    def test_blender_driver_artifact_paths_stay_under_artifact_dir(self) -> None:
        module_name = "warptest.blender_driver"
        previous_module = sys.modules.pop(module_name, None)
        missing = object()
        previous_bpy = sys.modules.get("bpy", missing)
        try:
            sys.modules["bpy"] = SimpleNamespace()
            driver = importlib.import_module(module_name)
            with tempfile.TemporaryDirectory() as tmp:
                artifact_dir = (Path(tmp) / "artifacts").resolve()
                artifact_dir.mkdir()
                driver.ARTIFACT_DIR = artifact_dir

                self.assertEqual(driver._artifact_path("scene.blend"), artifact_dir / "scene.blend")
                self.assertEqual(driver._artifact_path("nested/scene.blend"), artifact_dir / "nested" / "scene.blend")

                for value in ["", "   ", ".", "../escape.blend", "nested/../../escape.blend", "/tmp/escape.blend"]:
                    with self.subTest(value=value):
                        with self.assertRaises(ValueError):
                            driver._artifact_path(value)
        finally:
            sys.modules.pop(module_name, None)
            if previous_module is not None:
                sys.modules[module_name] = previous_module
            if previous_bpy is missing:
                sys.modules.pop("bpy", None)
            else:
                sys.modules["bpy"] = previous_bpy

    def test_jupyterlab_runtime_keeps_file_writes_under_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = JupyterLabRuntimeAdapter(ROOT / "games/jupyterlab", Path(tmp) / "report")
            adapter.root_dir.mkdir(parents=True)
            adapter._ensure_parent_dir("notes/warptest.txt")
            self.assertTrue((adapter.root_dir / "notes").is_dir())
            with self.assertRaises(ValueError):
                adapter._ensure_parent_dir("../escape.txt")

    def test_jupyterlab_notebook_cell_uses_kernel_websocket_and_reuses_session(self) -> None:
        class FakeKernelSocket:
            def __init__(self) -> None:
                self.sent: dict | None = None
                self.closed = False
                self.index = 0

            def send(self, payload: str) -> None:
                self.sent = json.loads(payload)

            def recv(self) -> str:
                assert self.sent is not None
                msg_id = self.sent["header"]["msg_id"]
                messages = [
                    {"channel": "iopub", "msg_type": "execute_input", "parent_header": {"msg_id": msg_id}, "content": {"execution_count": 1}},
                    {"channel": "iopub", "msg_type": "stream", "parent_header": {"msg_id": msg_id}, "content": {"name": "stdout", "text": "warptest:42\n"}},
                    {"channel": "shell", "msg_type": "execute_reply", "parent_header": {"msg_id": msg_id}, "content": {"status": "ok", "execution_count": 1}},
                    {"channel": "iopub", "msg_type": "status", "parent_header": {"msg_id": msg_id}, "content": {"execution_state": "idle"}},
                ]
                message = messages[self.index]
                self.index += 1
                return json.dumps(message)

            def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as tmp:
            adapter = JupyterLabRuntimeAdapter(ROOT / "games/jupyterlab", Path(tmp) / "report")
            notebook = adapter._notebook_model({"cells": [{"cell_type": "code", "source": "print('warptest:42')\n"}]})
            saved_notebooks: list[dict] = []
            api_calls: list[tuple[str, str, dict | None]] = []
            sockets: list[FakeKernelSocket] = []

            def fake_request(method: str, api_path: str, payload: dict | None = None) -> dict:
                api_calls.append((method, api_path, payload))
                if method == "GET" and api_path == "/contents/notebooks/cell-output.ipynb?content=1":
                    return {"path": "notebooks/cell-output.ipynb", "type": "notebook", "content": copy.deepcopy(notebook)}
                if method == "PUT" and api_path == "/contents/notebooks/cell-output.ipynb":
                    saved_notebooks.append(copy.deepcopy(payload["content"]))  # type: ignore[index]
                    return {}
                if method == "POST" and api_path == "/sessions":
                    return {"id": "session-1", "kernel": {"id": "kernel-1", "name": "python3"}}
                raise AssertionError(f"Unexpected Jupyter API call: {method} {api_path}")

            def fake_open_socket(ws_url: str, timeout: float) -> FakeKernelSocket:
                self.assertIn("/api/kernels/kernel-1/channels?session_id=session-1", ws_url)
                socket = FakeKernelSocket()
                sockets.append(socket)
                return socket

            adapter._request_json = fake_request  # type: ignore[method-assign]
            adapter._request_workspace_json = fake_request  # type: ignore[method-assign]
            adapter._open_kernel_websocket = fake_open_socket  # type: ignore[method-assign]

            output = adapter.run_notebook_cell("notebooks/cell-output.ipynb", 0)
            output_again = adapter.run_notebook_cell("notebooks/cell-output.ipynb", 0)

        session_posts = [call for call in api_calls if call[0] == "POST" and call[1] == "/sessions"]
        self.assertEqual(len(session_posts), 1)
        self.assertEqual(output, "warptest:42\n")
        self.assertEqual(output_again, "warptest:42\n")
        self.assertEqual(len(sockets), 2)
        self.assertTrue(all(socket.closed for socket in sockets))
        self.assertIn("execute_request", [socket.sent["header"]["msg_type"] for socket in sockets if socket.sent])
        self.assertEqual(saved_notebooks[-1]["cells"][0]["outputs"][0]["text"], "warptest:42\n")
        self.assertEqual(adapter.sessions, ["session-1"])

    def test_jupyterlab_workspace_actions_persist_and_assert_via_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = JupyterLabRuntimeAdapter(ROOT / "games/jupyterlab", Path(tmp) / "report")
            workspaces: dict[str, dict] = {}
            api_calls: list[tuple[str, str, dict | None]] = []

            def fake_request(method: str, api_path: str, payload: dict | None = None) -> dict:
                api_calls.append((method, api_path, payload))
                if method == "GET" and api_path.startswith("/contents/"):
                    return {"path": api_path.removeprefix("/contents/").split("?")[0], "type": "file"}
                if method == "PUT" and api_path.startswith("/contents/"):
                    return {}
                if method == "PUT" and api_path == "/workspaces/warptest":
                    workspaces["warptest"] = copy.deepcopy(payload)  # type: ignore[arg-type]
                    return {}
                if method == "GET" and api_path == "/workspaces/warptest":
                    if "warptest" not in workspaces:
                        raise RuntimeError("Jupyter API GET /workspaces/warptest failed with 404: missing")
                    return copy.deepcopy(workspaces["warptest"])
                raise AssertionError(f"Unexpected Jupyter API call: {method} {api_path}")

            adapter._request_json = fake_request  # type: ignore[method-assign]
            adapter._request_workspace_json = fake_request  # type: ignore[method-assign]
            adapter._run_action({"type": "jupyterlab_open_file", "path": "notes/workspace.md", "workspace": "warptest"})
            adapter._run_action(
                {
                    "type": "jupyterlab_set_workspace_layout",
                    "workspace": "warptest",
                    "paths": ["notes/workspace.md", "notebooks/workspace.ipynb"],
                }
            )
            detail, data = adapter._run_assertion({"type": "jupyterlab_workspace_contains", "workspace": "warptest", "path": "notebooks/workspace.ipynb"})
            saved_layout = copy.deepcopy(workspaces["warptest"])
            workspaces["warptest"] = adapter._workspace_model("warptest", ["notes/workspace.md"])

            with self.assertRaises(AssertionError):
                adapter._run_assertion({"type": "jupyterlab_workspace_contains", "workspace": "warptest", "path": "notebooks/workspace.ipynb"})

        self.assertEqual(detail, "Workspace contains expected path.")
        self.assertEqual(data["path"], "notebooks/workspace.ipynb")
        self.assertTrue(any(call[0] == "PUT" and call[1] == "/workspaces/warptest" for call in api_calls))
        self.assertTrue(any(call[0] == "GET" and call[1] == "/workspaces/warptest" for call in api_calls))
        self.assertIn("notebooks/workspace.ipynb", saved_layout["data"]["warptest:opened"])

    def test_jupyterlab_session_cache_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = JupyterLabRuntimeAdapter(ROOT / "games/jupyterlab", Path(tmp) / "report")
            api_calls: list[tuple[str, str, dict | None]] = []

            def fake_request(method: str, api_path: str, payload: dict | None = None) -> dict:
                api_calls.append((method, api_path, payload))
                if method == "POST" and api_path == "/sessions":
                    return {"id": "session-1", "kernel": {"id": "kernel-1", "name": "python3"}}
                if method == "DELETE" and api_path == "/sessions/session-1":
                    return {}
                raise AssertionError(f"Unexpected Jupyter API call: {method} {api_path}")

            adapter._request_json = fake_request  # type: ignore[method-assign]
            first = adapter.start_session("notebooks/session-smoke.ipynb", "python3")
            second = adapter.start_session("notebooks/session-smoke.ipynb", "python3")
            adapter.close()

        self.assertIs(first, second)
        self.assertEqual([call for call in api_calls if call[0] == "POST"], [("POST", "/sessions", {"path": "notebooks/session-smoke.ipynb", "type": "notebook", "name": "session-smoke.ipynb", "kernel": {"name": "python3"}})])
        self.assertEqual([call for call in api_calls if call[0] == "DELETE"], [("DELETE", "/sessions/session-1", None)])
        self.assertEqual(adapter.sessions, [])
        self.assertEqual(adapter._sessions_by_key, {})

    def test_jupyterlab_screenshot_url_tracks_workspace_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = JupyterLabRuntimeAdapter(ROOT / "games/jupyterlab", Path(tmp) / "report")
            adapter._remember_opened_path("notebooks/session-smoke.ipynb")
            self.assertTrue(adapter._screenshot_url().endswith("/lab/tree/notebooks/session-smoke.ipynb"))
            adapter._remember_opened_path("notes/workspace.md", workspace="warptest")
            self.assertTrue(adapter._screenshot_url().endswith("/lab/workspaces/warptest"))

    def test_jynew_inspect_profile_and_schema(self) -> None:
        result = RepositoryInspector().inspect(ROOT / "games/jynew")
        self.assertEqual(result.profile, "JynewProfile")
        self.assertEqual(result.capabilities.hook_name, "--warptest-checkpoint")
        self.assertFalse(result.capabilities.browser_runtime)
        self.assertIn("JynewRuntimeAdapter", result.adapter_layers)
        schema = result.semantic_schema
        field_names = {field.name for field in schema.fields}
        surface_kinds = {surface.kind for surface in schema.surfaces}
        self.assertIn("unity_batch_command", surface_kinds)
        self.assertIn("es3_save_system", surface_kinds)
        self.assertIn("player.Level", field_names)
        self.assertIn("money", field_names)

    def test_jynew_strategy_selector_reports_replay_unsupported(self) -> None:
        spec = load_spec(ROOT / "specs/jynew/team-join.yaml")
        plan = StrategySelector.select(
            spec,
            profile_name="JynewProfile",
            default_strategy="jynew_utility_save_validate",
            default_validity="save-restorable",
            mode="replay_from_start",
        )
        self.assertEqual(plan.unsupported_reason, JYNEW_REPLAY_UNSUPPORTED_REASON)

    def test_jynew_spec_schema_validates_all_bundled_specs(self) -> None:
        for spec_path in sorted((ROOT / "specs/jynew").glob("*.yaml")):
            with self.subTest(spec=str(spec_path)):
                self.assertIsInstance(load_spec(spec_path), dict)

    def test_jynew_benchmark_resolve_repo(self) -> None:
        spec = {"benchmark": "jynew"}
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                resolved = resolve_repo(spec, ROOT / "specs/jynew/team-join.yaml", None)
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(resolved, ROOT / "games/jynew")
        override = resolve_repo(spec, ROOT / "specs/jynew/team-join.yaml", str(ROOT / "games/jynew"))
        self.assertEqual(override, ROOT / "games/jynew")

    def test_jynew_instrumentation_installs_bundled_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "jyx2" / "Assets").mkdir(parents=True)
            result = ensure_jynew_instrumentation(repo)
            target = repo / JYNEW_HOOK_RELATIVE_PATH
            self.assertTrue(result["installed"])
            self.assertTrue(target.exists())
            text = target.read_text(encoding="utf-8")
            self.assertIn("WarptestCheckpoint", text)
            self.assertIn("Application.isBatchMode", text)
            second = ensure_jynew_instrumentation(repo)
            self.assertFalse(second["installed"])

    def test_jynew_deterministic_synthesis(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "jynew-team-join")
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT)
        self.assertEqual(checkpoint.hidden_files_read, [])
        self.assertEqual(checkpoint.payload["kind"], "jynew_utility_checkpoint")
        self.assertEqual(validate_method_checkpoint(checkpoint, ROOT), [])

    def test_pal3_inspect_profile_and_schema(self) -> None:
        result = RepositoryInspector().inspect(ROOT / "games/Pal3.Unity")
        self.assertEqual(result.profile, "Pal3Profile")
        self.assertEqual(result.capabilities.hook_name, "--warptest-checkpoint")
        self.assertFalse(result.capabilities.browser_runtime)
        self.assertIn("Pal3RuntimeAdapter", result.adapter_layers)
        self.assertEqual(result.instrumentation_mode, "in_place_test_gated")
        schema = result.semantic_schema
        field_names = {field.name for field in schema.fields}
        surface_kinds = {surface.kind for surface in schema.surfaces}
        self.assertIn("unity_batch_command", surface_kinds)
        self.assertIn("text_save_system", surface_kinds)
        self.assertIn("money", field_names)
        self.assertIn("team.count", field_names)
        self.assertIn("story_var.32768", field_names)

    def test_pal3_strategy_selector_reports_replay_unsupported(self) -> None:
        spec = load_spec(ROOT / "specs/pal3/team-join-single.yaml")
        plan = StrategySelector.select(
            spec,
            profile_name="Pal3Profile",
            default_strategy="pal3_console_save_validate",
            default_validity="save-restorable",
            mode="replay_from_start",
        )
        self.assertEqual(plan.unsupported_reason, PAL3_REPLAY_UNSUPPORTED_REASON)

    def test_pal3_spec_schema_validates_all_bundled_specs(self) -> None:
        spec_paths = sorted((ROOT / "specs/pal3").glob("*.yaml"))
        self.assertEqual(len(spec_paths), 28)
        for spec_path in spec_paths:
            with self.subTest(spec=str(spec_path)):
                self.assertIsInstance(load_spec(spec_path), dict)

    def test_pal3_benchmark_resolve_repo(self) -> None:
        spec = {"benchmark": "pal3"}
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                resolved = resolve_repo(spec, ROOT / "specs/pal3/team-join-single.yaml", None)
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(resolved, ROOT / "games/Pal3.Unity")
        override = resolve_repo(spec, ROOT / "specs/pal3/team-join-single.yaml", str(ROOT / "games/Pal3.Unity"))
        self.assertEqual(override, ROOT / "games/Pal3.Unity")

    def test_pal3_instrumentation_installs_bundled_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "Assets" / "Scripts" / "Pal3.Game").mkdir(parents=True)
            result = ensure_pal3_instrumentation(repo)
            target = repo / PAL3_HOOK_RELATIVE_PATH
            self.assertTrue(result["installed"])
            self.assertTrue(target.exists())
            self.assertIn("WarptestCheckpoint", target.read_text(encoding="utf-8"))
            second = ensure_pal3_instrumentation(repo)
            self.assertFalse(second["installed"])

    def test_pal3_instrumentation_refreshes_legacy_region_threshold(self) -> None:
        current = "bool ok = flag >= assertion.int_value;"
        legacy = (
            "bool ok = assertion.int_value > 0 ? "
            "flag >= assertion.int_value : flag > 0;"
        )
        source = PAL3_HOOK_TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertIn(current, source)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            target = repo / PAL3_HOOK_RELATIVE_PATH
            target.parent.mkdir(parents=True)
            target.write_text(source.replace(current, legacy), encoding="utf-8")

            result = ensure_pal3_instrumentation(repo)

            self.assertTrue(result["installed"])
            self.assertIn(current, target.read_text(encoding="utf-8"))

    def test_pal3_parent_repo_resolves_child_project_and_instrumentation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            project = repo / "games" / "Pal3.Unity"
            (project / "Assets").mkdir(parents=True)

            self.assertEqual(find_pal3_project_path(repo), project.resolve())
            runtime = Pal3RuntimeAdapter(repo, Path(tmp) / "out")
            self.assertEqual(runtime._project_path, project.resolve())

            result = ensure_pal3_instrumentation(repo)
            target = project / PAL3_HOOK_RELATIVE_PATH
            self.assertEqual(Path(result["path"]), target.resolve())
            self.assertTrue(target.is_file())
            self.assertFalse((repo / PAL3_HOOK_RELATIVE_PATH).exists())

    def test_pal3_cold_probe_reaps_only_exact_detached_unity_hub(self) -> None:
        request = Path("/tmp/pal3-exact-request.json")
        report = Path("/tmp/pal3-exact-report.json")
        exact = (
            "4321 1 /Applications/Unity Hub.app/Contents/MacOS/Unity Hub "
            f"-- --warptest-request {request} --warptest-report {report}\n"
        )
        unrelated = (
            "9876 1 /Applications/Unity Hub.app/Contents/MacOS/Unity Hub "
            "-- --warptest-request /tmp/other.json "
            "--warptest-report /tmp/other-result.json\n"
        )
        with (
            patch("warptest.unity_process.sys.platform", "darwin"),
            patch(
                "warptest.unity_process.subprocess.Popen",
                side_effect=[
                    SimpleNamespace(
                        communicate=lambda timeout: (exact + unrelated, ""),
                    ),
                    SimpleNamespace(
                        communicate=lambda timeout: (unrelated, ""),
                    ),
                ],
            ),
            patch("warptest.unity_process.os.kill") as kill_mock,
        ):
            cleaned = cleanup_detached_unity_hub_processes(
                request,
                report,
                spawn_grace_s=0,
                terminate_grace_s=0.1,
            )

        self.assertEqual(cleaned, [4321])
        kill_mock.assert_called_once_with(4321, signal.SIGTERM)

    def test_jynew_cold_probe_uses_shared_detached_hub_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "jynew"
            out_dir = Path(tmp) / "out"
            (repo / "jyx2" / "Assets").mkdir(parents=True)
            out_dir.mkdir()

            def fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
                report_path = Path(cmd[cmd.index("--warptest-report") + 1])
                report_path.write_text(
                    json.dumps({"status": "success", "checks": []}),
                    encoding="utf-8",
                )
                return SimpleNamespace(stdout="", stderr="", returncode=0)

            with (
                patch(
                    "warptest.jynew_runtime._find_jynew_unity_executable",
                    return_value="/fake/Unity",
                ),
                patch(
                    "warptest.jynew_runtime.subprocess.run",
                    side_effect=fake_run,
                ),
                patch(
                    "warptest.jynew_runtime.cleanup_detached_unity_hub_processes",
                    return_value=[777],
                ) as cleanup_mock,
            ):
                runtime = JynewRuntimeAdapter(repo, out_dir)
                result = runtime.run_checkpoint(
                    {"name": "jynew cleanup", "timeout_ms": 1000},
                    Path("/tmp/jynew-cleanup.yaml"),
                )

        self.assertEqual(result["status"], "success")
        cleanup_mock.assert_called_once_with(
            out_dir.resolve() / "jynew-cleanup-jynew-request.json",
            out_dir.resolve() / "jynew-cleanup-jynew-result.json",
        )
        self.assertIn("detached_unity_hub_cleanup=777", runtime.logs)

    def test_pal3_unity_candidates_include_project_version_install(self) -> None:
        candidates = _pal3_unity_candidates_for_version("6000.1.10f1")
        self.assertIn("/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity", candidates)
        self.assertIn("/Applications/Unity/Hub/Editor/6000.1.10f1/Unity.app/Contents/MacOS/Unity", candidates)

    def test_jynew_unity_candidates_keep_project_version_and_generic_install(self) -> None:
        candidates = _jynew_unity_candidates_for_version("2020.3.32f1c1")
        self.assertIn(
            "/Applications/Unity/Unity-2020.3.32f1c1/Unity.app/Contents/MacOS/Unity",
            candidates,
        )
        self.assertIn(
            "/Applications/Unity/Unity.app/Contents/MacOS/Unity",
            candidates,
        )

    def test_pal3_runtime_reports_unity_version_mismatch_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "Pal3.Unity"
            out_dir = Path(tmp) / "out"
            (repo / "Assets").mkdir(parents=True)
            (repo / "ProjectSettings").mkdir()
            (repo / "ProjectSettings" / "ProjectVersion.txt").write_text(
                "m_EditorVersion: 6000.1.10f1\n",
                encoding="utf-8",
            )
            spec = {"name": "pal3 version mismatch", "timeout_ms": 1000}
            version_result = SimpleNamespace(stdout="2020.3.32f1c1\n", stderr="", returncode=0)

            with patch("warptest.pal3_runtime._find_pal3_unity_executable", return_value="/fake/Unity"):
                with patch("warptest.pal3_runtime.subprocess.run", return_value=version_result) as run_mock:
                    result = Pal3RuntimeAdapter(repo, out_dir).run_checkpoint(
                        spec,
                        ROOT / "specs/pal3/team-join-single.yaml",
                    )

            self.assertEqual(result["status"], "failure")
            self.assertEqual(result["checks"][0]["name"], "probe.unity_version_mismatch")
            self.assertIn("project requires 6000.1.10f1", result["stderr_tail"])
            self.assertEqual(run_mock.call_count, 1)

    def test_discovery_first_path_containing_skips_large_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            large = repo / "large.cs"
            small = repo / "small.cs"
            large.write_text("x" * (MAX_DISCOVERY_TEXT_FILE_BYTES + 1) + "WarptestCheckpoint", encoding="utf-8")
            small.write_text("class WarptestCheckpoint {}\n", encoding="utf-8")

            result = _first_path_containing(repo, [large, small], "WarptestCheckpoint")

        self.assertEqual(result, small)

    def test_browser_static_bundle_rewrites_vite_entry(self) -> None:
        html = """
<!doctype html>
<html>
  <head><title>app</title></head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
"""
        rewritten = BrowserRuntimeAdapter._rewrite_index_html(html, "assets/main-abc.js", ["assets/main-def.css"])
        self.assertNotIn("/src/main.tsx", rewritten)
        self.assertIn('<link rel="stylesheet" href="/assets/main-def.css" />', rewritten)
        self.assertIn('<script type="module" src="/assets/main-abc.js"></script>', rewritten)

    def test_browser_static_bundle_is_manual_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "SpaceRPG"
            (repo / "src").mkdir(parents=True)
            (repo / "src" / "main.tsx").write_text("", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("WARPTEST_BROWSER_STATIC_BUNDLE", None)
                runtime = BrowserRuntimeAdapter(repo, "npm run dev -- --port {port}", Path(tmp) / "report")
                self.assertFalse(runtime._should_use_static_bundle())
            with patch.dict(os.environ, {"WARPTEST_BROWSER_STATIC_BUNDLE": "1"}):
                runtime = BrowserRuntimeAdapter(repo, "npm run dev -- --port {port}", Path(tmp) / "report")
                self.assertTrue(runtime._should_use_static_bundle())

    def test_browser_vite_entry_probe_auto_scoped_to_known_vite_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "DatingSim"
            (repo / "src").mkdir(parents=True)
            (repo / "src" / "main.tsx").write_text("", encoding="utf-8")
            runtime = BrowserRuntimeAdapter(repo, "npm run dev -- --port {port}", Path(tmp) / "report")
            self.assertTrue(runtime._should_probe_vite_entry())

    def test_pal3_visual_capture_uses_batchmode_by_default(self) -> None:
        spec_path = ROOT / "specs/pal3/team-join-single.yaml"
        spec = load_spec(spec_path)
        cmd_seen: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "Pal3.Unity"
            out_dir = Path(tmp) / "out"
            (repo / "Assets").mkdir(parents=True)
            (repo / "ProjectSettings").mkdir()
            (repo / "ProjectSettings" / "ProjectVersion.txt").write_text(
                "m_EditorVersion: 6000.1.10f1\n",
                encoding="utf-8",
            )

            def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
                if "-version" in cmd:
                    return SimpleNamespace(stdout="6000.1.10f1\n", stderr="", returncode=0)
                cmd_seen.extend(cmd)
                report_path = Path(cmd[cmd.index("--warptest-report") + 1])
                screenshot_path = report_path.parent / "pal3-visual-post-smoke.png"
                screenshot_path.write_bytes((ROOT / "specs/pal3/visual/team-join-single.golden.png").read_bytes())
                report_path.write_text(
                    json.dumps(
                        {
                            "status": "success",
                            "screenshot_path": str(screenshot_path),
                            "screenshot_status": "success",
                            "screenshot_source": "unity_capture",
                            "screenshot_detail": "camera render captured",
                            "checks": [{"name": "target.state_synthesized", "status": "success", "detail": "ok"}],
                        }
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(stdout="", stderr="", returncode=0)

            with patch("warptest.pal3_runtime._find_pal3_unity_executable", return_value="/fake/Unity"):
                with patch("warptest.pal3_runtime.subprocess.run", side_effect=fake_run):
                    runtime = Pal3RuntimeAdapter(repo, out_dir, visual_capture=True)
                    result = runtime.run_checkpoint(spec, spec_path)

        self.assertEqual(result["live_source"], "unity_capture")
        self.assertTrue(runtime.headless)
        self.assertIn("-batchmode", cmd_seen)
        self.assertIn("-nographics", cmd_seen)

    def test_pal3_semantic_run_does_not_capture_declared_visual_oracle(self) -> None:
        spec_path = ROOT / "specs/pal3/team-join-single.yaml"
        spec = load_spec(spec_path)
        spec["timeout_ms"] = 900_000
        requests_seen: list[dict[str, object]] = []
        timeout_seen: list[object] = []

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "Pal3.Unity"
            out_dir = Path(tmp) / "out"
            (repo / "Assets").mkdir(parents=True)
            (repo / "ProjectSettings").mkdir()
            (repo / "ProjectSettings" / "ProjectVersion.txt").write_text(
                "m_EditorVersion: 6000.1.10f1\n",
                encoding="utf-8",
            )

            def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
                if "-version" in cmd:
                    return SimpleNamespace(stdout="6000.1.10f1\n", stderr="", returncode=0)
                timeout_seen.append(kwargs.get("timeout"))
                request_path = Path(cmd[cmd.index("--warptest-request") + 1])
                requests_seen.append(json.loads(request_path.read_text(encoding="utf-8")))
                report_path = Path(cmd[cmd.index("--warptest-report") + 1])
                report_path.write_text(
                    json.dumps(
                        {
                            "status": "success",
                            "checks": [{"name": "target.state_synthesized", "status": "success", "detail": "ok"}],
                        }
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(stdout="", stderr="", returncode=0)

            with patch("warptest.pal3_runtime._find_pal3_unity_executable", return_value="/fake/Unity"):
                with patch("warptest.pal3_runtime.subprocess.run", side_effect=fake_run):
                    runtime = Pal3RuntimeAdapter(repo, out_dir, visual_capture=False)
                    result = runtime.run_checkpoint(spec, spec_path)

        self.assertEqual(result["status"], "success")
        self.assertEqual(requests_seen[0]["screenshot_output_path"], "")
        self.assertEqual(timeout_seen, [900.0])

    def test_pal3_deterministic_synthesis(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "pal3-team-join")
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT)
        self.assertEqual(checkpoint.hidden_files_read, [])
        self.assertEqual(checkpoint.payload["kind"], "pal3_console_checkpoint")
        self.assertEqual(validate_method_checkpoint(checkpoint, ROOT), [])

    def test_pal3_profile_run_maps_unity_checks_to_phases(self) -> None:
        inspect_result = RepositoryInspector().inspect(ROOT / "games/Pal3.Unity")
        profile = Pal3Profile(inspect_result)
        spec = load_spec(ROOT / "specs/pal3/team-join-single.yaml")

        class FakePal3Runtime:
            launch_command = "unity ..."
            app_url = None
            logs: list[str] = []

            def run_checkpoint(self, _spec: dict, _spec_path: Path) -> dict:
                return {
                    "status": "success",
                    "exit_code": 0,
                    "request_path": "req.json",
                    "report_path": "rep.json",
                    "stdout_path": "out.log",
                    "stderr_path": "err.log",
                    "checks": [
                        {"name": "target.state_synthesized", "status": "success", "detail": ""},
                        {"name": "action[pal3_add_team_member].actor_1", "status": "success", "detail": ""},
                        {"name": "assertion[pal3_team_contains].actor_1", "status": "success", "detail": ""},
                        {"name": "assertion[pal3_team_count]", "status": "success", "detail": ""},
                        {"name": "assertion[no_pal3_utility_errors]", "status": "success", "detail": ""},
                    ],
                }

        report = profile.run(spec, ROOT / "specs/pal3/team-join-single.yaml", FakePal3Runtime(), mode="jump_and_validate")
        self.assertEqual(report.checkpoint_restoration.status, "success")
        self.assertEqual(report.smoke_execution.status, "success")
        self.assertEqual(report.oracle_result.status, "success")
        self.assertEqual(report.overall_status, "success")

    def test_luanti_inspect_profile_and_schema(self) -> None:
        result = RepositoryInspector().inspect(ROOT / "games/luanti")
        self.assertEqual(result.profile, "LuantiProfile")
        self.assertEqual(result.capabilities.hook_name, "warptest")
        self.assertFalse(result.capabilities.browser_runtime)
        self.assertTrue(result.capabilities.deterministic_seed)
        self.assertIn("LuantiRuntimeAdapter", result.adapter_layers)
        self.assertEqual(result.instrumentation_mode, "in_place_test_gated")
        schema = result.semantic_schema
        field_names = {field.name for field in schema.fields}
        surface_kinds = {surface.kind for surface in schema.surfaces}
        self.assertIn("lua_worldmod_command", surface_kinds)
        self.assertIn("voxel_map", surface_kinds)
        self.assertIn("player_object", surface_kinds)
        self.assertIn("engine_screenshot_capture", surface_kinds)
        self.assertIn("node@pos", field_names)
        self.assertIn("player.hp", field_names)
        self.assertIn("mod_storage", field_names)
        self.assertTrue(all(surface.evidence for surface in schema.surfaces))
        self.assertTrue(all(field.evidence for field in schema.fields))

    def test_luanti_strategy_selector_reports_replay_unsupported(self) -> None:
        spec = load_spec(ROOT / "specs/luanti/set-node.yaml")
        plan = StrategySelector.select(
            spec,
            profile_name="LuantiProfile",
            default_strategy="luanti_worldmod_state_validate",
            default_validity="save-restorable",
            mode="replay_from_start",
        )
        self.assertEqual(plan.unsupported_reason, LUANTI_REPLAY_UNSUPPORTED_REASON)

    def test_luanti_spec_schema_validates_all_bundled_specs(self) -> None:
        spec_paths = sorted((ROOT / "specs/luanti").glob("*.yaml"))
        self.assertEqual(len(spec_paths), 28)
        for spec_path in spec_paths:
            with self.subTest(spec=str(spec_path)):
                self.assertIsInstance(load_spec(spec_path), dict)

    def test_luanti_param2_specs_load_for_oriented_nodes(self) -> None:
        spec = load_spec(ROOT / "specs/luanti/complex-house-workshop.yaml")
        roof = next(fill for fill in spec["target"]["fill"] if fill["node"] == "stairs:slab_cobble")
        stair_action = next(action for action in spec["actions"] if action.get("node") == "stairs:stair_cobble")
        self.assertEqual(roof["param2"], 0)
        self.assertEqual(stair_action["param2"], 2)

    def test_luanti_benchmark_resolve_repo(self) -> None:
        spec = {"benchmark": "luanti"}
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                resolved = resolve_repo(spec, ROOT / "specs/luanti/set-node.yaml", None)
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(resolved, ROOT / "games/luanti")
        override = resolve_repo(spec, ROOT / "specs/luanti/set-node.yaml", str(ROOT / "games/luanti"))
        self.assertEqual(override, ROOT / "games/luanti")

    def test_luanti_instrumentation_installs_mod_and_screenshot_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "mods").mkdir(parents=True)
            (repo / "builtin").mkdir(parents=True)
            (repo / "builtin" / "init.lua").write_text("-- builtin\n", encoding="utf-8")
            (repo / "games" / "devtest").mkdir(parents=True)
            game_cpp = repo / "src" / "client" / "game.cpp"
            game_cpp.parent.mkdir(parents=True)
            game_cpp.write_text(
                "void Game::run()\n{\n\twhile (true) {\n\t\tupdateProfilerGraphs(&graph);\n\t}\n}\n",
                encoding="utf-8",
            )
            result = ensure_luanti_instrumentation(repo)
            self.assertTrue(result["installed"])
            mod_init = repo / LUANTI_WORLDMOD_RELATIVE_DIR / "init.lua"
            self.assertTrue(mod_init.exists())
            self.assertIn("warptest_request_path", mod_init.read_text(encoding="utf-8"))
            cpp_text = game_cpp.read_text(encoding="utf-8")
            self.assertIn("warptest_maybe_capture", cpp_text)
            self.assertIn("WARPTEST-CAPTURE-BEGIN", cpp_text)
            second = ensure_luanti_instrumentation(repo)
            self.assertFalse(second["installed"])

    def test_luanti_deterministic_synthesis(self) -> None:
        task = next(task for task in load_public_issues(ROOT) if task.id == "luanti-set-node")
        checkpoint = DeterministicCheckpointSynthesisAgent().synthesize(task, ROOT)
        self.assertEqual(checkpoint.hidden_files_read, [])
        self.assertEqual(checkpoint.payload["kind"], "luanti_worldmod_checkpoint")
        self.assertEqual(validate_method_checkpoint(checkpoint, ROOT), [])

    def test_luanti_world_only_specs_use_headless_server(self) -> None:
        # World-only specs (no online player) run on the headless dedicated server;
        # player specs require the rendered single-player client.
        self.assertFalse(_spec_needs_client(load_spec(ROOT / "specs/luanti/set-node.yaml")))
        self.assertFalse(_spec_needs_client(load_spec(ROOT / "specs/luanti/chest-inventory.yaml")))
        self.assertFalse(_spec_needs_client(load_spec(ROOT / "specs/luanti/mod-storage.yaml")))
        self.assertFalse(_spec_needs_client(load_spec(ROOT / "specs/luanti/complex-house-workshop.yaml")))
        self.assertFalse(_spec_needs_client(load_spec(ROOT / "specs/luanti/irrigated-farmstead.yaml")))
        self.assertFalse(_spec_needs_client(load_spec(ROOT / "specs/luanti/storage-workshop.yaml")))
        self.assertTrue(_spec_needs_client(load_spec(ROOT / "specs/luanti/player-teleport.yaml")))
        self.assertTrue(_spec_needs_client(load_spec(ROOT / "specs/luanti/player-hp.yaml")))
        self.assertTrue(_spec_needs_client(load_spec(ROOT / "specs/luanti/mine-bridge-outpost.yaml")))
        self.assertTrue(_spec_needs_client(load_spec(ROOT / "specs/luanti/homestead-save-restore.yaml")))

    def test_luanti_engine_command_server_vs_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = LuantiRuntimeAdapter(ROOT / "games/luanti", Path(tmp))
            server_cmd = adapter._engine_command(Path("bin/luantiserver"), Path(tmp) / "w", Path(tmp) / "c.conf", use_client=False)
            self.assertIn("bin/luantiserver", server_cmd[0])
            self.assertNotIn("--go", server_cmd)
            client_cmd = adapter._engine_command(Path("bin/luanti"), Path(tmp) / "w", Path(tmp) / "c.conf", use_client=True)
            self.assertIn("--go", client_cmd)

    def test_luanti_profile_run_maps_worldmod_checks_to_phases(self) -> None:
        inspect_result = RepositoryInspector().inspect(ROOT / "games/luanti")
        profile = LuantiProfile(inspect_result)
        spec = load_spec(ROOT / "specs/luanti/set-node.yaml")

        class FakeLuantiRuntime:
            launch_command = "xvfb-run ..."
            app_url = None
            logs: list[str] = []

            def run_checkpoint(self, _spec: dict, _spec_path: Path) -> dict:
                return {
                    "status": "success",
                    "exit_code": 0,
                    "request_path": "req.json",
                    "report_path": "rep.json",
                    "stdout_path": "out.log",
                    "stderr_path": "err.log",
                    "checks": [
                        {"name": "target.state_synthesized", "status": "success", "detail": ""},
                        {"name": "target.validate.node.2,1,2", "status": "success", "detail": ""},
                        {"name": "action[luanti_set_node]@1", "status": "success", "detail": ""},
                        {"name": "assertion[luanti_node_is]@1", "status": "success", "detail": ""},
                        {"name": "assertion[no_luanti_mod_errors]@2", "status": "success", "detail": ""},
                    ],
                }

        report = profile.run(spec, ROOT / "specs/luanti/set-node.yaml", FakeLuantiRuntime(), mode="jump_and_validate")
        self.assertEqual(report.checkpoint_restoration.status, "success")
        self.assertEqual(report.validation.status, "success")
        self.assertEqual(report.smoke_execution.status, "success")
        self.assertEqual(report.oracle_result.status, "success")
        self.assertEqual(report.overall_status, "success")

    def test_luanti_fuzz_tasks_are_world_class_and_loadable(self) -> None:
        tasks = load_state_loop_tasks(ROOT, "benchmarks/state_loop/tasks_luanti.yaml")
        self.assertTrue(tasks)
        for task in tasks:
            self.assertEqual(task.benchmark, "luanti")
            spec_path = ROOT / task.oracle_spec
            self.assertTrue(spec_path.exists(), task.oracle_spec)
            # The C3 fuzz warm session is a headless luantiserver with no online
            # player; only world-class specs can be synthesized there.
            self.assertFalse(_spec_needs_client(load_spec(spec_path)), f"{task.id} needs an online-player client")

    def test_jupyterlab_state_loop_tasks_are_loadable(self) -> None:
        tasks = load_state_loop_tasks(ROOT, "benchmarks/state_loop/tasks_jupyterlab.yaml")
        self.assertEqual(len(tasks), 6)
        expected = {
            "jupyterlab-text-file-edit",
            "jupyterlab-markdown-file-save",
            "jupyterlab-notebook-cell-output",
            "jupyterlab-notebook-markdown-note",
            "jupyterlab-workspace-layout",
            "jupyterlab-session-smoke",
        }
        self.assertEqual({task.id for task in tasks}, expected)
        for task in tasks:
            self.assertEqual(task.benchmark, "JupyterLab")
            self.assertTrue((ROOT / task.oracle_spec).exists(), task.oracle_spec)

    def test_luanti_fuzz_task_set_in_sync_with_world_class_specs(self) -> None:
        expected = set()
        for spec_path in sorted((ROOT / "specs/luanti").glob("*.yaml")):
            spec = load_spec(spec_path)
            if (
                spec.get("benchmark") == "luanti"
                and "c1" not in (spec.get("tags") or [])
                and not _spec_needs_client(spec)
            ):
                expected.add(f"luanti-{spec_path.stem}")
        actual = {t.id for t in load_state_loop_tasks(ROOT, "benchmarks/state_loop/tasks_luanti.yaml")}
        self.assertEqual(
            actual, expected, "tasks_luanti.yaml is out of sync; re-run scripts/build_luanti_fuzz_tasks.py"
        )

    def test_luanti_fuzz_task_dispatches_to_engine_path(self) -> None:
        tasks = load_state_loop_tasks(ROOT, "benchmarks/state_loop/tasks_luanti.yaml")
        task = next(t for t in tasks if t.id == "luanti-set-node")
        sentinel = {"status": "success", "dispatched": "luanti"}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(fuzzing, "_run_fuzz_luanti", return_value=sentinel) as stub:
                out = fuzzing._run_fuzz_task(
                    ROOT, task, out_dir=Path(tmp), headless=True, config=fuzzing.FuzzConfig()
                )
        self.assertEqual(out, sentinel)
        self.assertTrue(stub.called)

    def test_luanti_fuzz_player_skip_conf_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_dir = root / "report"
            report_dir.mkdir()
            default_session = LuantiWarmSession(
                ROOT / "games/luanti", report_dir, cache_root=root / "cache", render=False
            )
            default_conf = default_session._write_persistent_conf(root / "world").read_text(encoding="utf-8")
            fuzz_session = LuantiWarmSession(
                ROOT / "games/luanti",
                report_dir,
                cache_root=root / "cache",
                render=False,
                allow_headless_player_skip=True,
            )
            fuzz_conf = fuzz_session._write_persistent_conf(root / "world").read_text(encoding="utf-8")

        self.assertNotIn("warptest_allow_headless_player_skip", default_conf)
        self.assertIn("warptest_allow_headless_player_skip = true", fuzz_conf)
        self.assertIn("bind_address = 127.0.0.1", default_conf)
        default_port = int(
            next(line.split("=", 1)[1] for line in default_conf.splitlines() if line.startswith("port ="))
        )
        fuzz_port = int(
            next(line.split("=", 1)[1] for line in fuzz_conf.splitlines() if line.startswith("port ="))
        )
        self.assertGreaterEqual(default_port, 1024)
        self.assertLessEqual(default_port, 65535)
        self.assertNotEqual(default_port, fuzz_port)
        self.assertIn(f"remote_port = {default_port}", default_conf)
        self.assertIn(f"remote_port = {fuzz_port}", fuzz_conf)

    def test_luanti_warm_session_accepts_an_explicit_isolated_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_dir = root / "report"
            report_dir.mkdir()
            session = LuantiWarmSession(
                ROOT / "games/luanti",
                report_dir,
                cache_root=root / "cache",
                render=True,
                listen_port=43123,
            )
            conf = session._write_persistent_conf(root / "world").read_text(encoding="utf-8")

        self.assertIn("bind_address = 127.0.0.1", conf)
        self.assertIn("port = 43123", conf)
        self.assertIn("remote_port = 43123", conf)

    def test_luanti_fuzz_runner_enables_headless_player_skip(self) -> None:
        tasks = load_state_loop_tasks(ROOT, "benchmarks/state_loop/tasks_luanti.yaml")
        task = next(t for t in tasks if t.id == "luanti-set-node")
        captured: dict[str, object] = {}

        class FakeSession:
            def __init__(self, repo: Path, report_dir: Path, **kwargs: object) -> None:
                captured["repo"] = repo
                captured["report_dir"] = report_dir
                captured["kwargs"] = kwargs

            def start(self) -> "FakeSession":
                return self

            def close(self) -> None:
                captured["closed"] = True

        def fake_run_evo(
            task: object,
            seed_target: dict[str, object],
            inject_fn: object,
            make_record: object,
            result: dict[str, object],
            config: object,
        ) -> None:
            captured["evo_called"] = True
            result["status"] = "success"

        seed_spec = load_spec(ROOT / task.oracle_spec)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("warptest.luanti_state.LuantiWarmSession", FakeSession):
                with patch.object(fuzzing, "_run_evo_into_result", fake_run_evo):
                    result = fuzzing._run_fuzz_luanti(
                        ROOT,
                        task,
                        ROOT / "games/luanti",
                        seed_spec,
                        seed_spec["target"],
                        {"task_id": task.id, "benchmark": task.benchmark, "mutations": []},
                        Path(tmp),
                        fuzzing.FuzzConfig(),
                    )

        self.assertEqual(result["status"], "success")
        self.assertTrue(captured["evo_called"])
        self.assertTrue(captured["closed"])
        self.assertEqual(
            captured["kwargs"],
            {"render": False, "allow_headless_player_skip": True, "map_backend": "dummy"},
        )

    def test_luanti_fuzz_rejects_malformed_request_protocol_failures(self) -> None:
        tasks = load_state_loop_tasks(ROOT, "benchmarks/state_loop/tasks_luanti.yaml")
        task = next(t for t in tasks if t.id == "luanti-set-node")

        class FakeSession:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def start(self) -> "FakeSession":
                return self

            def is_alive(self) -> bool:
                return True

            def restore(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
                return SimpleNamespace(
                    status="failure",
                    detail="malformed request: request JSON missing spec",
                    checks=[],
                )

            def close(self) -> None:
                pass

        def fake_run_evo(
            task: object,
            seed_target: dict[str, object],
            inject_fn: object,
            make_record: object,
            result: dict[str, object],
            config: object,
        ) -> None:
            injected = inject_fn(seed_target, "field_value", "candidate.png")
            record = make_record(
                target=seed_target,
                operator_id="field_value",
                provenance="regression",
                generation=1,
                fitness=0.0,
                result=injected,
            )
            result["mutations"].append(record)
            result["status"] = "success"

        seed_spec = load_spec(ROOT / task.oracle_spec)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("warptest.luanti_state.LuantiWarmSession", FakeSession):
                with patch.object(fuzzing, "_run_evo_into_result", fake_run_evo):
                    result = fuzzing._run_fuzz_luanti(
                        ROOT,
                        task,
                        ROOT / "games/luanti",
                        seed_spec,
                        seed_spec["target"],
                        {"task_id": task.id, "benchmark": task.benchmark, "mutations": []},
                        Path(tmp),
                        fuzzing.FuzzConfig(),
                    )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["mutations"][0]["verdict"], "rejected")
        self.assertFalse(list(Path(tmp).glob("defects/*/state.json")))

    def test_luanti_worldmod_guards_fuzzed_meta_inventory_types(self) -> None:
        source = (
            ROOT / "warptest" / "instrumentation" / "luanti" / "warptest" / "init.lua"
        ).read_text(encoding="utf-8")
        self.assertIn('type(list) ~= "string"', source)
        self.assertIn('type(items) ~= "table"', source)
        self.assertIn('type(node_name) == "string" and node_name ~= "" and is_known_node(node_name)', source)

    def test_spacerpg_fuzz_skips_shadow_injections_when_primary_rejects_save(self) -> None:
        tasks = load_state_loop_tasks(ROOT, "benchmarks/state_loop/tasks_spacerpg.yaml")
        task = next(t for t in tasks if t.id == "spacerpg-equipment-install-cargo-expansion-repeat")
        restore_kinds: list[str] = []

        class FakeSession:
            git_commit = "test-commit"

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def start(self) -> "FakeSession":
                return self

            def restore(self, cand_spec: dict[str, object], *_args: object, **_kwargs: object) -> SimpleNamespace:
                target = cand_spec.get("target")
                kind = target.get("kind") if isinstance(target, dict) else None
                restore_kinds.append(str(kind))
                if kind == "fuzz_save_restore":
                    return SimpleNamespace(report=None, detail="SpaceRPG loadGame rejected the mutated save.", screenshot=None)
                return SimpleNamespace(
                    report={"console_errors": ["equipment.reduce is not a function"]},
                    detail="shadow crash",
                    screenshot=None,
                )

            def close(self) -> None:
                pass

        def fake_run_evo_fuzz(**kwargs: object) -> tuple[list[dict[str, object]], dict[str, object]]:
            inject_fn = kwargs["inject_fn"]
            make_record = kwargs["make_record"]
            seed_target = kwargs["seed_target"]
            candidate = copy.deepcopy(seed_target)
            candidate["player"] = {"equipment": {"primary": {"id": "bad-shape"}}}
            injected = inject_fn(candidate, "subtree", "candidate.png")
            record = make_record(
                target=candidate,
                operator_id="subtree",
                provenance="regression",
                generation=1,
                fitness=0.0,
                result=injected,
            )
            return [record], {"generations": 1}

        seed = {
            "seed_save": {
                "version": 3,
                "savedAt": "2020-01-01T00:00:00.000Z",
                "screen": "station",
                "currentSystemId": "helion-reach",
                "currentStationId": "helion-prime",
                "gameClock": 0,
                "player": {"shipId": "valkyrie", "equipment": ["pulse-laser"]},
            },
            "seed_actions": [],
        }
        seed_spec = load_spec(ROOT / task.oracle_spec)
        config = fuzzing.FuzzConfig(spacerpg_inject=("save_restore", "state_overlay", "replace_state"))
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(fuzzing, "StateSession", FakeSession):
                with patch.object(fuzzing, "_load_or_capture_spacerpg_seed", return_value=seed):
                    with patch.object(fuzzing, "_make_fuzzer", return_value=object()):
                        with patch.object(fuzzing, "run_evo_fuzz", fake_run_evo_fuzz):
                            result = fuzzing._run_fuzz_spacerpg(
                                ROOT,
                                task,
                                ROOT / "games/SpaceRPG",
                                seed_spec,
                                {"task_id": task.id, "benchmark": task.benchmark, "mutations": []},
                                Path(tmp),
                                True,
                                config,
                            )

            self.assertEqual(result["status"], "success", result.get("reason"))
            self.assertEqual(restore_kinds, ["fuzz_save_restore"])
            self.assertEqual(result["mutations"][0]["verdict"], "rejected")
            self.assertEqual(result["mutations"][0]["injection_mode"], "save_restore")
            self.assertFalse(list(Path(tmp).glob("defects/*/state.json")))

    def test_open_with_hook_reports_gate_metadata(self) -> None:
        inspect_result = RepositoryInspector().inspect(ROOT / "games/SpaceRPG")
        profile = SpaceRPGProfile(inspect_result)

        class FakePage:
            def evaluate(self, _script: str, _arg: str) -> dict[str, str]:
                return {
                    "gate": "import.meta.env.DEV && (?warptest=1 || localStorage.spacerpg-e2e-hook=enabled)",
                    "mode": "development",
                    "enabledBy": "query",
                }

        runtime = SimpleNamespace(
            page=FakePage(),
            open=lambda _path: None,
            wait_for_hook=lambda _hook, timeout_ms: None,
        )
        availability = profile._open_with_hook(runtime, "/?warptest=1")
        self.assertEqual(availability["gate"], "import.meta.env.DEV && (?warptest=1 || localStorage.spacerpg-e2e-hook=enabled)")
        self.assertEqual(availability["mode"], "development")
        self.assertEqual(availability["enabled_by"], "query")

    def test_bench_headed_flag_passes_headless_false(self) -> None:
        report = SimpleNamespace(
            repo_path=str(ROOT / "games/2048"),
            overall_status="success",
            visual_oracle_result=SimpleNamespace(status="success"),
        )
        with (
            patch("warptest.cli.discover_bench_specs", return_value=[ROOT / "specs/2048/merge-left.yaml"]),
            patch("warptest.cli.new_report_dir", return_value=ROOT / "reports/test-cli-bench"),
            patch("warptest.cli.write_json"),
            patch("warptest.cli.run_spec", return_value=report) as run_spec_mock,
            redirect_stdout(StringIO()),
        ):
            main(["bench", "--headed"])
        self.assertEqual(run_spec_mock.call_args.kwargs["headless"], False)
        self.assertEqual(run_spec_mock.call_args.kwargs["oracle_policy"], "visual")

    def test_bench_defaults_to_headless(self) -> None:
        report = SimpleNamespace(
            repo_path=str(ROOT / "games/2048"),
            overall_status="success",
            visual_oracle_result=SimpleNamespace(status="success"),
        )
        with (
            patch("warptest.cli.discover_bench_specs", return_value=[ROOT / "specs/2048/merge-left.yaml"]),
            patch("warptest.cli.new_report_dir", return_value=ROOT / "reports/test-cli-bench"),
            patch("warptest.cli.write_json"),
            patch("warptest.cli.run_spec", return_value=report) as run_spec_mock,
            redirect_stdout(StringIO()),
        ):
            main(["bench"])
        self.assertEqual(run_spec_mock.call_args.kwargs["headless"], True)

    def test_bench_compare_flag_runs_benchmark_comparison(self) -> None:
        result = {"report_dir": "reports/test", "summary": {"total_specs": 1}}
        with (
            patch("warptest.cli.discover_bench_specs", return_value=[ROOT / "specs/2048/merge-left.yaml"]),
            patch("warptest.cli.new_report_dir", return_value=ROOT / "reports/test-cli-bench"),
            patch("warptest.cli.compare_benchmark_specs", return_value=result) as compare_mock,
            redirect_stdout(StringIO()),
        ):
            main(["bench", "--compare", "--headed"])
        self.assertEqual(compare_mock.call_args.kwargs["headless"], False)
        self.assertEqual(compare_mock.call_args.kwargs["oracle_policy"], "visual")

    def test_compare_cli_visual_flag_runs_visual_oracle(self) -> None:
        result = {"report_dir": "reports/test", "summary": {"total_specs": 1}}
        with (
            patch("warptest.cli.compare_spec", return_value=result) as compare_mock,
            redirect_stdout(StringIO()),
        ):
            main(["compare", str(ROOT / "specs/openra/cnc-gdi01-beachhead.yaml"), "--visual"])
        self.assertEqual(compare_mock.call_args.kwargs["oracle_policy"], "visual")

    def test_compare_cli_accepts_adaptive_oracle_policy(self) -> None:
        result = {"report_dir": "reports/test", "summary": {"total_specs": 1}}
        with (
            patch("warptest.cli.compare_spec", return_value=result) as compare_mock,
            redirect_stdout(StringIO()),
        ):
            main(["compare", str(ROOT / "specs/openra/cnc-gdi01-beachhead.yaml"), "--oracle-policy", "adaptive", "--visual-sample-rate", "0.25"])
        self.assertEqual(compare_mock.call_args.kwargs["oracle_policy"], "adaptive")
        self.assertEqual(compare_mock.call_args.kwargs["visual_sample_rate"], 0.25)

    def test_compare_cli_passes_visual_cascade_config(self) -> None:
        result = {"report_dir": "reports/test", "summary": {"total_specs": 1}}
        with (
            patch("warptest.cli.compare_spec", return_value=result) as compare_mock,
            redirect_stdout(StringIO()),
        ):
            main(
                [
                    "compare",
                    str(ROOT / "specs/2048/merge-left.yaml"),
                    "--visual",
                    "--cascade-policy",
                    "conservative",
                    "--visual-frame-sample-count",
                    "3",
                    "--visual-frame-sample-interval-ms",
                    "125",
                    "--visual-cache",
                    "report+global",
                ]
            )
        cascade = compare_mock.call_args.kwargs["cascade_config"]
        self.assertEqual(cascade.cascade_policy, "conservative")
        self.assertEqual(cascade.sample_count, 3)
        self.assertEqual(cascade.sample_interval_ms, 125)
        self.assertEqual(cascade.cache_mode, "report+global")

    def test_core_bench_cli_runs_static_public_issue_protocol(self) -> None:
        result = {"report_dir": "reports/core", "summary": {"total_tasks": 21}}
        with (
            patch("warptest.cli.run_core_synthesis_benchmark", return_value=result) as core_mock,
            redirect_stdout(StringIO()),
        ):
            main(["core-bench", "--execute", "--visual", "--headed", "--model", "fake/model"])
        self.assertTrue(core_mock.call_args.kwargs["execute"])
        self.assertEqual(core_mock.call_args.kwargs["oracle_policy"], "visual")
        self.assertEqual(core_mock.call_args.kwargs["headless"], False)
        self.assertEqual(core_mock.call_args.kwargs["agent_kind"], "llm")
        self.assertEqual(core_mock.call_args.kwargs["model"], "fake/model")

    def test_core_bench_cli_accepts_deterministic_agent(self) -> None:
        result = {"report_dir": "reports/core", "summary": {"total_tasks": 21}}
        with (
            patch("warptest.cli.run_core_synthesis_benchmark", return_value=result) as core_mock,
            redirect_stdout(StringIO()),
        ):
            main(["core-bench", "--agent", "deterministic", "--model", "fake/model", "--base-url", "http://127.0.0.1", "--api-key-env", "FAKE_KEY"])
        self.assertEqual(core_mock.call_args.kwargs["agent_kind"], "deterministic")
        self.assertEqual(core_mock.call_args.kwargs["model"], "fake/model")

    def test_core_bench_hidden_visual_oracle_absolutizes_pair_goldens(self) -> None:
        hidden_spec_path = ROOT / "specs/blender/scene-cube-material.yaml"
        executable_spec: dict[str, object] = {}
        _attach_hidden_visual_oracle(executable_spec, load_spec(hidden_spec_path), hidden_spec_path)
        oracle = executable_spec["visual_oracle"]
        self.assertIsInstance(oracle, dict)
        for key in ("golden_image", "golden_before", "golden_after"):
            path = Path(oracle[key])
            self.assertTrue(path.is_absolute(), key)
            self.assertTrue(path.exists(), key)

        with tempfile.TemporaryDirectory() as tmp:
            method_spec_path = Path(tmp) / "method-specs" / "blender-scene-cube-material.yaml"
            method_spec_path.parent.mkdir(parents=True)
            normalized = validate_visual_oracle(executable_spec, method_spec_path, required=True)

        self.assertEqual(normalized["golden_before"], oracle["golden_before"])
        self.assertEqual(normalized["golden_after"], oracle["golden_after"])

    def test_segment_bench_cli_runs_parallel_agents(self) -> None:
        result = {"report_dir": "reports/segments", "summary": {"runs": 10}}
        with (
            patch("warptest.cli.run_segment_bug_discovery", return_value=result) as segment_mock,
            redirect_stdout(StringIO()),
        ):
            main(["segment-bench", "--agents", "segment_probe,smoke_only", "--parallel"])
        self.assertEqual(segment_mock.call_args.kwargs["agents"], ["segment_probe", "smoke_only"])
        self.assertTrue(segment_mock.call_args.kwargs["parallel"])

    def test_gui_inspect_cli_reports_boundary(self) -> None:
        with redirect_stdout(StringIO()) as output:
            main(["gui-inspect", str(ROOT / "games/2048")])
        self.assertIn("browser-web", output.getvalue())

    def test_effort_cli_writes_report(self) -> None:
        with (
            patch("warptest.cli.new_report_dir", return_value=ROOT / "reports/test-cli-effort"),
            patch("warptest.cli.write_json") as write_mock,
            redirect_stdout(StringIO()),
        ):
            main(["effort"])
        self.assertEqual(write_mock.call_args.args[0], ROOT / "reports/test-cli-effort" / "effort.json")

    def test_paper_tables_cli_writes_json_and_markdown(self) -> None:
        out_dir = ROOT / "reports/test-cli-paper"
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("warptest.cli.new_report_dir", return_value=Path(tmp)),
            redirect_stdout(StringIO()),
        ):
            main(["paper-tables"])
            self.assertTrue((Path(tmp) / "paper-tables.json").exists())
            self.assertTrue((Path(tmp) / "paper-tables.md").exists())
        self.assertFalse(out_dir.exists())

    def test_model_bench_headed_flag_passes_headless_false(self) -> None:
        with (
            patch("warptest.cli.run_model_benchmark", return_value={"report_dir": "reports/test", "summary": {}}) as bench_mock,
            redirect_stdout(StringIO()),
        ):
            main(["model-bench", "--headed", "--models", "model/a"])
        self.assertEqual(bench_mock.call_args.kwargs["headless"], False)
        self.assertEqual(bench_mock.call_args.kwargs["oracle_policy"], "visual")

    def test_model_bench_defaults_to_headless(self) -> None:
        with (
            patch("warptest.cli.run_model_benchmark", return_value={"report_dir": "reports/test", "summary": {}}) as bench_mock,
            redirect_stdout(StringIO()),
        ):
            main(["model-bench", "--models", "model/a"])
        self.assertEqual(bench_mock.call_args.kwargs["headless"], True)
        self.assertEqual(bench_mock.call_args.kwargs["oracle_policy"], "visual")

    def test_model_bench_accepts_fake_visual_oracle_policy(self) -> None:
        with (
            patch("warptest.cli.run_model_benchmark", return_value={"report_dir": "reports/test", "summary": {}}) as bench_mock,
            redirect_stdout(StringIO()),
        ):
            main(["model-bench", "--models", "model/a", "--oracle-policy", "fake-visual"])
        self.assertEqual(bench_mock.call_args.kwargs["oracle_policy"], "fake-visual")


if __name__ == "__main__":
    unittest.main()
