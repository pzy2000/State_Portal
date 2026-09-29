from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from warptest.agent_s_bridge import AgentSAction, InputBatch
from warptest.c1_unity_runtime import (
    C1_UNITY_PROTOCOL_VERSION,
    JynewC1Session,
    Pal3C1Session,
    UnityC1ProtocolError,
    _save_load_transition_expectation,
)
from warptest.jynew_runtime import JynewRuntimeAdapter

ROOT = Path(__file__).resolve().parents[1]


def _declared_transition(slot: int, *, benchmark: str = "jynew") -> dict:
    events = (
        ["save", "load"]
        if benchmark == "jynew"
        else ["save", "reset", "scene_load", "load"]
    )
    return {
        "evaluation": {
            "schema_version": 1,
            "semantic": {
                "assertions": [],
                "surfaces": {
                    "gui": {
                        "adapter": f"{benchmark}_unity_live_v1",
                        "start": {"validate_target": True, "checks": []},
                        "goal": {
                            "use_assertions": True,
                            "checks": [],
                            "requirements": [
                                {
                                    "name": "c1.public_save_load_witness",
                                    "kind": "save_then_load",
                                    "slot": slot,
                                    "ordered_events": events,
                                }
                            ],
                        },
                    },
                    "state": {
                        "adapter": f"{benchmark}_unity_live_v1",
                        "start": {"validate_target": True, "checks": []},
                        "goal": {"use_assertions": True, "checks": []},
                    },
                },
            },
            "visual_oracle": {},
        }
    }


class _RecordingSessionMixin:
    calls: list[tuple[str, dict]]

    def install_recorder(self, session: object) -> None:
        self.calls = []

        def record(operation: str, **fields: object) -> dict:
            self.calls.append((operation, fields))
            return {
                "version": C1_UNITY_PROTOCOL_VERSION,
                "sequence": len(self.calls),
                "operation": operation,
                "status": "success",
                "checks": [],
            }

        session._request = record  # type: ignore[attr-defined]


class UnityC1PayloadTests(unittest.TestCase, _RecordingSessionMixin):
    def _jynew(self, root: Path) -> JynewC1Session:
        (root / "repo" / "jyx2" / "Assets").mkdir(parents=True)
        return JynewC1Session(root / "repo", root / "out", acquire_native_lock=False)

    def test_jynew_restores_only_known_material_serialization_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir) / "jyx2"
            material = (
                project
                / "Assets/3DScene/Model/BigMapNew/Materials/water-Jyx2.mat"
            )
            material.parent.mkdir(parents=True)
            original = b"water 0.050976038 0.05489698 0.20390114\n"
            material.write_bytes(original)
            session = object.__new__(JynewC1Session)
            session.benchmark = "jynew"
            session._project_path = project
            session.logs = []
            session._serialization_guard = session._capture_serialization_guard()
            material.write_bytes(b"water 0.050976016 0.05489696 0.20390111\n")

            session._restore_known_serialization_noise()
            self.assertEqual(material.read_bytes(), original)

            material.write_bytes(b"user change\n")
            session._restore_known_serialization_noise()
            self.assertEqual(material.read_bytes(), b"user change\n")
            self.assertTrue(any("unknown_drift" in row for row in session.logs))

    def test_cold_jynew_adapter_restores_known_noise_on_context_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            project = repo / "jyx2"
            material = (
                project
                / "Assets/3DScene/Model/BigMapNew/Materials/water-Jyx2.mat"
            )
            material.parent.mkdir(parents=True)
            original = b"water 0.050976038 0.05489698 0.20390114\n"
            material.write_bytes(original)
            adapter = JynewRuntimeAdapter(repo, Path(temp_dir) / "out")
            material.write_bytes(b"water 0.050976016 0.05489696 0.20390111\n")

            adapter.__exit__(None, None, None)

            self.assertEqual(material.read_bytes(), original)
            self.assertTrue(
                any("serialization_guard_restored" in row for row in adapter.logs)
            )

    def _pal3(self, root: Path) -> Pal3C1Session:
        (root / "repo" / "Assets").mkdir(parents=True)
        return Pal3C1Session(root / "repo", root / "out", acquire_native_lock=False)

    def test_pal3_parent_repo_resolves_child_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "repo" / "games" / "Pal3.Unity"
            (project / "Assets").mkdir(parents=True)

            session = Pal3C1Session(
                root / "repo",
                root / "out",
                acquire_native_lock=False,
            )

        self.assertEqual(session._project_path, project.resolve())

    def test_restore_target_strips_phase_b_twice_at_host_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._jynew(Path(tmp))
            self.install_recorder(session)
            original = {
                "target": {"kind": "synthesized_state", "player_level": 5},
                "actions": [{"type": "jynew_save", "save_index": 0}],
                "assertions": [{"type": "jynew_role_attr", "role_id": 0}],
            }
            session.restore_target(original)

        operation, fields = self.calls[0]
        self.assertEqual(operation, "restore_target")
        self.assertEqual(fields["spec"]["actions"], [])
        self.assertEqual(fields["spec"]["assertions"], [])
        self.assertEqual(original["actions"][0]["type"], "jynew_save")

    def test_live_goal_keeps_assertions_but_never_submits_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._pal3(Path(tmp))
            self.install_recorder(session)
            report = session.semantic_goal(
                {
                    "target": {"scene_city": "q01", "scene_name": "b"},
                    "actions": [
                        {"type": "pal3_load_scene", "city": "q02", "scene": "q02"}
                    ],
                    "assertions": [
                        {"type": "pal3_scene_is", "city": "q02", "scene": "q02"}
                    ],
                }
            )

        operation, fields = self.calls[0]
        self.assertEqual(operation, "semantic_goal")
        self.assertEqual(fields["spec"]["actions"], [])
        self.assertIsNone(fields["transition_expectation"])
        self.assertEqual(fields["spec"]["assertions"][0]["type"], "pal3_scene_is")
        self.assertEqual(report["semantic_source"], "pal3_live_semantic_probe_v1")

    def test_live_start_uses_target_only_read_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._jynew(Path(tmp))
            self.install_recorder(session)
            report = session.semantic_start(
                {
                    "target": {"player_level": 5, "map_id": 1},
                    "actions": [{"type": "jynew_save", "save_index": 2}],
                    "assertions": [{"type": "jynew_map_id", "int_value": 1}],
                }
            )

        operation, fields = self.calls[0]
        self.assertEqual(operation, "semantic_start")
        self.assertEqual(fields["spec"]["actions"], [])
        self.assertEqual(fields["spec"]["assertions"], [])
        self.assertEqual(report["semantic_source"], "jynew_live_semantic_probe_v1")

    def test_save_load_actions_become_bounded_transition_expectation(self) -> None:
        spec = yaml.safe_load((ROOT / "specs/jynew/submap-enter.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            session = self._jynew(Path(tmp))
            self.install_recorder(session)
            session.semantic_start(spec)

        operation, fields = self.calls[0]
        self.assertEqual(operation, "semantic_start")
        self.assertEqual(fields["spec"]["actions"], [])
        self.assertEqual(fields["spec"]["assertions"], [])
        self.assertEqual(
            fields["transition_expectation"],
            {"kind": "save_then_load", "slot": 2},
        )


class UnityC1ProtocolTests(unittest.TestCase):
    def _session(self, root: Path) -> JynewC1Session:
        (root / "repo" / "jyx2" / "Assets").mkdir(parents=True)
        return JynewC1Session(
            root / "repo",
            root / "out",
            startup_timeout_s=0.1,
            request_timeout_s=0.1,
            acquire_native_lock=False,
        )

    def test_launch_is_headed_and_uses_persistent_entry_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp))
            with patch.object(session, "_find_editor", return_value="/fake/Unity"):
                command = session.launch_command
        self.assertIn("Jyx2.WarptestCheckpoint.RunC1", command)
        self.assertIn("--warptest-c1-ready", command)
        self.assertNotIn("-hideWindow", command)
        self.assertNotIn("-batchmode", command)
        self.assertNotIn("-nographics", command)

    def test_caller_relative_capture_path_is_not_rebased_onto_out_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            session = self._session(root)
            relative = Path(
                "reports/tasks/pal3-scene-restore/phase-b/step-001.png"
            )
            self.assertEqual(
                session._screenshot_destination(relative),
                relative.resolve(),
            )
            self.assertNotEqual(
                session._screenshot_destination(relative),
                (session.out_dir / relative).resolve(),
            )
            self.assertEqual(
                session._screenshot_destination("unity-before.png"),
                session.out_dir / "unity-before.png",
            )

    def test_unity_screenshot_writes_relative_task_path_at_caller_root(self) -> None:
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            session = self._session(root)

            def capture(operation: str, **fields: object) -> dict:
                self.assertEqual(operation, "capture")
                target = Path(str(fields["screenshot_output_path"]))
                target.parent.mkdir(parents=True, exist_ok=True)
                image = Image.new("RGB", (1280, 720), "black")
                ImageDraw.Draw(image).rectangle((0, 0, 639, 719), fill="white")
                image.save(target)
                return {
                    "status": "success",
                    "screenshot_status": "success",
                    "screenshot_source": "unity_gameview_capture",
                    "frame_id": 1,
                    "frame_width": 1280,
                    "frame_height": 720,
                    "capture_source_width": 1280,
                    "capture_source_height": 720,
                }

            session._request = capture  # type: ignore[method-assign]
            relative = Path("reports/tasks/task-1/phase-b/step-001.png")
            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                captured = Path(session.unity_screenshot(relative))
            finally:
                os.chdir(original_cwd)

            self.assertEqual(captured, (root / relative).resolve())
            self.assertTrue(captured.is_file())
            self.assertFalse((session.out_dir / relative).exists())

    def test_restore_target_rejection_is_treatment_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp))
            session.out_dir.mkdir(parents=True, exist_ok=True)
            session._started = True
            session._process = MagicMock()
            session._process.poll.return_value = None
            for status in ("rejected", "engine_error"):
                with self.subTest(status=status), patch.object(
                    session,
                    "_wait_for_payload",
                    return_value={
                        "status": status,
                        "detail": "candidate target could not be constructed",
                    },
                ):
                    report = session._request("restore_target", spec={})
                    self.assertEqual(report["status"], status)

    def test_non_restore_engine_error_remains_protocol_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp))
            session.out_dir.mkdir(parents=True, exist_ok=True)
            session._started = True
            session._process = MagicMock()
            session._process.poll.return_value = None
            with patch.object(
                session,
                "_wait_for_payload",
                return_value={"status": "engine_error", "detail": "runtime broke"},
            ):
                with self.assertRaisesRegex(
                    UnityC1ProtocolError, "semantic_start failed closed"
                ):
                    session._request("semantic_start", spec={})

    def test_jynew_requires_exact_camera_render_source_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jynew = self._session(Path(tmp))
            jynew._require_full_frame_capture(
                {"capture_source_width": 1280, "capture_source_height": 720}
            )
            with self.assertRaisesRegex(
                UnityC1ProtocolError, "exact 1280x720 camera-rendered"
            ):
                jynew._require_full_frame_capture(
                    {
                        "capture_source_width": 2560,
                        "capture_source_height": 1440,
                    }
                )

        pal3 = object.__new__(Pal3C1Session)
        pal3._require_full_frame_capture(
            {"capture_source_width": 2560, "capture_source_height": 1440}
        )

    def test_unity_pre_main_guard_blocks_appkit_activation(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "warptest"
            / "c1_unity_background_guard.m"
        ).read_text(encoding="utf-8")
        self.assertIn("NSApplicationActivationPolicyAccessory", source)
        self.assertIn("activateIgnoringOtherApps:", source)
        self.assertIn("activateWithOptions:", source)
        self.assertIn("WARPTEST_C1_UNITY_LEGACY_ORDER_WINDOW_GUARD", source)
        self.assertIn("orderWindow:relativeTo:", source)
        self.assertIn("NSWindowOut", source)
        self.assertNotIn("NSApplicationActivationPolicyProhibited", source)

    def test_unity_templates_enforce_macos_background_activation_policy(self) -> None:
        for relative in (
            "warptest/instrumentation/jynew/WarptestCheckpointCommand.cs",
            "warptest/instrumentation/pal3/WarptestCheckpointCommand.cs",
        ):
            source = (Path(__file__).resolve().parents[1] / relative).read_text(
                encoding="utf-8"
            )
            self.assertIn("NSApplicationActivationPolicyAccessory", source)
            self.assertIn('sel_registerName("setActivationPolicy:")', source)
            self.assertIn('sel_registerName("deactivate")', source)
            self.assertGreaterEqual(
                source.count("EnforceC1BackgroundActivationPolicy();"), 3
            )

    def test_start_exposes_live_pid_without_global_native_input(self) -> None:
        class FakeProcess:
            pid = 4242
            returncode = None

            def poll(self) -> int | None:
                return self.returncode

            def wait(self, timeout: float | None = None) -> int:
                del timeout
                self.returncode = 0
                return 0

            def terminate(self) -> None:
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = -9

        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp))
            fake = FakeProcess()
            session._background_window_guard = MagicMock()
            session._background_window_guard.await_hidden.return_value = {
                "mode": "macos_process_group_hide_v1"
            }

            def launch(*_args: object, **kwargs: object) -> FakeProcess:
                launch_env = kwargs.get("env")
                self.assertIsInstance(launch_env, dict)
                assert isinstance(launch_env, dict)
                self.assertEqual(
                    launch_env.get(
                        "WARPTEST_C1_UNITY_LEGACY_ORDER_WINDOW_GUARD"
                    ),
                    "1",
                )
                session._ready_path.parent.mkdir(parents=True, exist_ok=True)
                session._ready_path.write_text(
                    json.dumps(
                        {
                            "version": C1_UNITY_PROTOCOL_VERSION,
                            "sequence": 0,
                            "status": "ready",
                            "pid": fake.pid,
                            "session_id": session.session_id,
                        }
                    ),
                    encoding="utf-8",
                )
                return fake

            with (
                patch.object(
                    session,
                    "_ensure_instrumentation",
                    return_value={"reason": "test", "path": "/tmp/hook"},
                ),
                patch.object(session, "_find_editor", return_value="/fake/Unity"),
                patch.object(
                    session,
                    "_background_guard",
                    return_value=Path("/tmp/unity-c1-background-guard.dylib"),
                ),
                patch("warptest.c1_unity_runtime.subprocess.Popen", side_effect=launch),
            ):
                session.start()
                session._background_window_guard.start.assert_called_once_with(4242)
                self.assertEqual(session.pid, 4242)
                self.assertEqual(session.hold_pid, 4242)
                self.assertIsNone(session.native)
                self.assertTrue(session.is_alive())
                evidence = session.seed_evidence("jynew-save-restore")
                self.assertEqual(evidence["source"], "unity_live_process")
                self.assertEqual(evidence["algorithm"], "not_applicable_unity_gui")
                self.assertTrue(evidence["observed"])
                self.assertEqual(evidence["process_id"], 4242)
                self.assertEqual(evidence["task_id"], "jynew-save-restore")
                fake.returncode = 0
                session.close()

    def test_input_batch_is_bound_to_session_frame_and_marks_optional_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp))
            calls: list[tuple[str, dict[str, object]]] = []

            def record(operation: str, **fields: object) -> dict[str, object]:
                calls.append((operation, fields))
                return {
                    "status": "success",
                    "accepted": True,
                    "batch_id": fields["batch_id"],
                    "event_count": 4,
                    "resulting_frame_id": 8,
                }

            session._request = record  # type: ignore[method-assign]
            batch = InputBatch(
                session_id=session.session_id,
                frame_id=7,
                batch_id="batch-7",
                actions=(
                    AgentSAction(kind="type", text="中文", x=None, y=None),
                    AgentSAction(kind="click", x=12, y=34),
                ),
            )
            receipt = session.dispatch_input_batch(batch)

        self.assertTrue(receipt["accepted"])
        self.assertEqual(calls[0][0], "input_batch")
        actions = calls[0][1]["actions"]
        self.assertFalse(actions[0]["has_point"])
        self.assertTrue(actions[1]["has_point"])

    def test_protocol_version_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp))
            path = Path(tmp) / "payload.json"
            path.write_text(
                json.dumps({"version": "wrong", "sequence": 0, "status": "ready"}),
                encoding="utf-8",
            )
            with self.assertRaises(UnityC1ProtocolError):
                session._wait_for_payload(
                    path, sequence=0, operation=None, timeout_s=0.1
                )

    def test_operation_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp))
            path = Path(tmp) / "payload.json"
            path.write_text(
                json.dumps(
                    {
                        "version": C1_UNITY_PROTOCOL_VERSION,
                        "sequence": 1,
                        "operation": "semantic_goal",
                        "status": "success",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(UnityC1ProtocolError):
                session._wait_for_payload(
                    path, sequence=1, operation="restore_target", timeout_s=0.1
                )

    def test_immediate_done_is_a_well_formed_semantic_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp))
            expectation = {
                "kind": "save_then_load",
                "slot": 0,
                "ordered_events": ["save", "load"],
            }
            report = {
                "sequence": 5,
                "status": "failure",
                "checks": [
                    {
                        "name": "c1.transition.expectation",
                        "status": "success",
                    },
                    {"name": "c1.transition.save", "status": "failure"},
                    {"name": "c1.transition.load", "status": "failure"},
                ],
                "transition_evidence": {
                    "required": True,
                    "kind": "save_then_load",
                    "slot": 0,
                    "source": "jynew_public_ui_save_load_witness_v1",
                    "armed_sequence": 4,
                    "observed_sequence": 5,
                    "save_observed": False,
                    "load_observed": False,
                    "save_frame": -1,
                    "load_frame": -1,
                    "ordered": False,
                },
            }
            result = session._semantic_result(
                report,
                transition_expectation=expectation,
                after=True,
            )

        self.assertTrue(result["transition_evidence_valid"])
        self.assertFalse(result["ok"])

    def test_tampered_success_checks_cannot_override_missing_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp))
            expectation = {
                "kind": "save_then_load",
                "slot": 0,
                "ordered_events": ["save", "load"],
            }
            report = {
                "sequence": 5,
                "status": "success",
                "checks": [
                    {
                        "name": "c1.transition.expectation",
                        "status": "success",
                    },
                    {"name": "c1.transition.save", "status": "success"},
                    {"name": "c1.transition.load", "status": "success"},
                ],
                "transition_evidence": {
                    "required": True,
                    "kind": "save_then_load",
                    "slot": 0,
                    "source": "jynew_public_ui_save_load_witness_v1",
                    "armed_sequence": 4,
                    "observed_sequence": 5,
                    "save_observed": False,
                    "load_observed": False,
                    "save_frame": -1,
                    "load_frame": -1,
                    "ordered": False,
                },
            }
            result = session._semantic_result(
                report,
                transition_expectation=expectation,
                after=True,
            )

        self.assertFalse(result["transition_evidence_valid"])
        self.assertFalse(result["ok"])

    def test_non_save_task_rejects_forged_transition_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(Path(tmp))
            evidence = {
                "required": False,
                "kind": "",
                "slot": -1,
                "source": "jynew_public_ui_save_load_witness_v1",
                "armed_sequence": -1,
                "observed_sequence": 3,
                "save_observed": False,
                "load_observed": False,
                "save_frame": -1,
                "load_frame": -1,
                "ordered": False,
            }
            report = {
                "sequence": 3,
                "status": "success",
                "checks": [
                    {
                        "name": "assertion[jynew_map_id]",
                        "status": "success",
                    }
                ],
                "transition_evidence": evidence,
            }
            valid = session._semantic_result(
                report,
                transition_expectation=None,
                after=True,
            )
            self.assertTrue(valid["transition_evidence_valid"])
            self.assertTrue(valid["ok"])

            evidence["save_observed"] = True
            evidence["save_frame"] = 90
            evidence["ordered"] = True
            forged = session._semantic_result(
                {**report, "transition_evidence": evidence},
                transition_expectation=None,
                after=True,
            )

        self.assertFalse(forged["transition_evidence_valid"])
        self.assertFalse(forged["ok"])


class UnityC1TransitionExpectationTests(unittest.TestCase):
    def test_declared_requirement_arms_without_action_inference(self) -> None:
        for benchmark, slot in (("jynew", 2), ("pal3", 5)):
            spec = _declared_transition(slot, benchmark=benchmark)
            spec["actions"] = [{"type": "deliberately_unrelated"}]
            self.assertEqual(
                _save_load_transition_expectation(spec),
                {
                    "kind": "save_then_load",
                    "slot": slot,
                    "ordered_events": (
                        ["save", "load"]
                        if benchmark == "jynew"
                        else ["save", "reset", "scene_load", "load"]
                    ),
                },
                benchmark,
            )

    def test_actions_alone_never_arm(self) -> None:
        spec = {
            "actions": [
                {"type": "jynew_save", "save_index": 0},
                {"type": "jynew_load_save", "save_index": 0},
            ]
        }
        self.assertIsNone(_save_load_transition_expectation(spec))

    def test_ambiguous_or_malformed_roundtrips_never_arm(self) -> None:
        cases = {
            "no actions": {},
            "single scene load": {
                "actions": [{"type": "pal3_load_scene", "city": "q02", "scene": "q02"}]
            },
            "roundtrip is not last": {
                "actions": [
                    {"type": "jynew_save", "save_index": 3},
                    {"type": "jynew_load_save", "save_index": 3},
                    {"type": "jynew_rest"},
                ]
            },
            "two roundtrips": {
                "actions": [
                    {"type": "jynew_save", "save_index": 1},
                    {"type": "jynew_load_save", "save_index": 1},
                    {"type": "jynew_save", "save_index": 2},
                    {"type": "jynew_load_save", "save_index": 2},
                ]
            },
            "earlier stray save": {
                "actions": [
                    {"type": "jynew_save", "save_index": 4},
                    {"type": "jynew_rest"},
                    {"type": "jynew_save", "save_index": 5},
                    {"type": "jynew_load_save", "save_index": 5},
                ]
            },
            "mixed benchmark families": {
                "actions": [
                    {"type": "jynew_save", "save_index": 1},
                    {"type": "pal3_load_save", "save_index": 1},
                ]
            },
            "reversed order": {
                "actions": [
                    {"type": "jynew_load_save", "save_index": 1},
                    {"type": "jynew_save", "save_index": 1},
                ]
            },
            "slot mismatch": {
                "actions": [
                    {"type": "jynew_save", "save_index": 1},
                    {"type": "jynew_load_save", "save_index": 2},
                ]
            },
            "negative slot": {
                "actions": [
                    {"type": "jynew_save", "save_index": -1},
                    {"type": "jynew_load_save", "save_index": -1},
                ]
            },
            "jynew slot above public UI range": {
                "actions": [
                    {"type": "jynew_save", "save_index": 3},
                    {"type": "jynew_load_save", "save_index": 3},
                ]
            },
            "pal3 autosave slot is not public manual UI": {
                "actions": [
                    {"type": "pal3_save", "save_index": 0},
                    {"type": "pal3_load_save", "save_index": 0},
                ]
            },
            "pal3 slot above public UI range": {
                "actions": [
                    {"type": "pal3_save", "save_index": 6},
                    {"type": "pal3_load_save", "save_index": 6},
                ]
            },
            "boolean slot": {
                "actions": [
                    {"type": "jynew_save", "save_index": True},
                    {"type": "jynew_load_save", "save_index": True},
                ]
            },
            "boolean load slot": {
                "actions": [
                    {"type": "jynew_save", "save_index": 1},
                    {"type": "jynew_load_save", "save_index": True},
                ]
            },
            "non-mapping action": {
                "actions": [
                    "jynew_save",
                    {"type": "jynew_save", "save_index": 1},
                    {"type": "jynew_load_save", "save_index": 1},
                ]
            },
        }
        for label, spec in cases.items():
            self.assertIsNone(_save_load_transition_expectation(spec), label)


class UnityC1InstrumentationTests(unittest.TestCase):
    def test_witnesses_match_the_save_log_markers_the_games_still_emit(self) -> None:
        """The witness greps the games' own logs, so the markers must not drift."""

        repo = Path(__file__).resolve().parents[1]
        jynew_save = (
            repo
            / "games/jynew/jyx2/Assets/Scripts/GameSave/GameRuntimeData.cs"
        ).read_text(encoding="utf-8")
        self.assertIn('Debug.Log("存档中.. index = " + index);', jynew_save)
        self.assertIn('Debug.Log("存档结束");', jynew_save)

        jynew_witness = (
            repo / "warptest/instrumentation/jynew/WarptestCheckpointCommand.cs"
        ).read_text(encoding="utf-8")
        self.assertIn('const string saveStart = "存档中.. index = ";', jynew_witness)
        self.assertIn('condition.IndexOf("存档结束"', jynew_witness)

        pal3_save = (
            repo / "games/Pal3.Unity/Assets/Scripts/Pal3.Game/State/SaveManager.cs"
        ).read_text(encoding="utf-8")
        self.assertIn('SAVE_FILE_FORMAT = "slot_{0}_v1.txt"', pal3_save)
        self.assertIn('EngineLogger.Log($"Game state saved to: {saveFilePath}")', pal3_save)

        pal3_witness = (
            repo / "warptest/instrumentation/pal3/WarptestCheckpointCommand.cs"
        ).read_text(encoding="utf-8")
        self.assertIn('"[SaveManager] Game state saved to:"', pal3_witness)
        self.assertIn('$"slot_{s_c1TransitionSlot}_v1.txt"', pal3_witness)

    def test_jynew_semantic_probe_does_not_create_missing_game_runtime(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        source = (
            repo
            / "warptest"
            / "instrumentation"
            / "jynew"
            / "WarptestCheckpointCommand.cs"
        ).read_text(encoding="utf-8")
        start = source.index("static List<WarptestCheck> CheckC1Target")
        end = source.index("internal static void WriteC1Json", start)
        probe = source[start:end]

        self.assertNotIn("var runtime = GameRuntimeData.Instance;", probe)
        self.assertIn('GetField(\n                "_instance"', probe)
        self.assertIn("BindingFlags.NonPublic", probe)
        self.assertIn("runtimeField?.GetValue(null) as GameRuntimeData", probe)

    def test_jynew_capture_never_accepts_editor_screen_readback(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "warptest"
            / "instrumentation"
            / "jynew"
            / "WarptestCheckpointCommand.cs"
        ).read_text(encoding="utf-8")
        start = source.index("static string CaptureScreenshotToFile")
        end = source.index("#if UNITY_EDITOR", start)
        capture = source[start:end]

        self.assertNotIn("CaptureScreenshotAsTexture", capture)
        self.assertIn("TryCaptureCameraToFile(outputPath, out cameraDetail)", capture)

    def test_restore_handlers_do_not_call_phase_b_action_executor(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for relative in (
            "warptest/instrumentation/jynew/WarptestCheckpointCommand.cs",
            "warptest/instrumentation/pal3/WarptestCheckpointCommand.cs",
        ):
            source = (repo / relative).read_text(encoding="utf-8")
            start = source.index('case "restore_target"')
            end = source.index('case "semantic_start"', start)
            handler = source[start:end]
            self.assertNotIn("ExecuteAction", handler, relative)
            self.assertNotIn("spec.actions", handler, relative)
            self.assertNotIn("spec.assertions", handler, relative)

    def test_both_templates_expose_versioned_persistent_protocol(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for benchmark in ("jynew", "pal3"):
            source = (
                repo
                / "warptest"
                / "instrumentation"
                / benchmark
                / "WarptestCheckpointCommand.cs"
            ).read_text(encoding="utf-8")
            self.assertIn("public static void RunC1()", source)
            self.assertIn(C1_UNITY_PROTOCOL_VERSION, source)
            self.assertIn("WarptestC1RunnerBehaviour", source)
            self.assertIn("transition_expectation", source)
            self.assertIn("transition_evidence", source)
            self.assertIn("AddC1TransitionGoalChecks", source)
            self.assertIn("warptest-unity-checkpoint-state-v1", source)
            self.assertIn("evidence_task_id", source)
            self.assertIn("evidence_benchmark", source)
            self.assertIn("process_alive_at_observation", source)
            self.assertIn('case "semantic_goal"', source)
            self.assertIn('case "capture"', source)
            self.assertIn('case "input_batch"', source)
            self.assertIn("QueueGameViewInputEvent", source)
            self.assertIn("Application.runInBackground = true", source)
            self.assertNotIn("FocusAndMaximizeGameView", source)
            self.assertIn('case "close"', source)
            if benchmark == "jynew":
                self.assertIn("C1SessionActive", source)
            else:
                capture = source.index('case "capture"')
                input_batch = source.index('case "input_batch"', capture)
                self.assertIn(
                    "yield return new WaitForEndOfFrame();",
                    source[capture:input_batch],
                )

    def test_jynew_startup_routes_to_public_mod_panel_during_c1(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        source = (
            repo
            / "games"
            / "jynew"
            / "jyx2"
            / "Assets"
            / "Scripts"
            / "GameCore"
            / "GameStart.cs"
        ).read_text(encoding="utf-8")
        self.assertIn("ModPanelNew.SwitchSceneTo();", source)
        self.assertNotIn("Suppressed normal startup scene routing", source)

        template = (
            repo
            / "warptest"
            / "instrumentation"
            / "jynew"
            / "WarptestCheckpointCommand.cs"
        ).read_text(encoding="utf-8")
        runner = template.index('new GameObject("WarptestC1Runner")')
        begin = template.index("runner.Begin", runner)
        self.assertIn("DontDestroyOnLoad(host)", template[runner:begin])

    def test_both_templates_ignore_empty_json_transition_placeholder(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for benchmark in ("jynew", "pal3"):
            source = (
                repo
                / "warptest"
                / "instrumentation"
                / benchmark
                / "WarptestCheckpointCommand.cs"
            ).read_text(encoding="utf-8")
            self.assertIn("HasC1TransitionExpectation", source)
            self.assertIn("!string.IsNullOrEmpty(expectation.kind)", source)
            self.assertIn("expectation.slot >= 0", source)
            self.assertIn("if (!HasC1TransitionExpectation(expectation))", source)


if __name__ == "__main__":
    unittest.main()
