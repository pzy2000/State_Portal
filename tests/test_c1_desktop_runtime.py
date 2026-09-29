from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml
from PIL import Image

from warptest import c1_desktop_runtime
from warptest.agent_s_bridge import AgentSAction, InputBatch
from warptest.c1_desktop_runtime import (
    BLENDER_SEMANTIC_SOURCE,
    BLENDER_SEED_EVIDENCE_SOURCE,
    LIBREOFFICE_SEMANTIC_SOURCE,
    LIBREOFFICE_SEED_EVIDENCE_SOURCE,
    NATIVE_GUI_SEED_ALGORITHM,
    BlenderC1DesktopSession,
    C1DesktopRuntimeError,
    C1DesktopRuntimeUnsupported,
    LibreOfficeC1DesktopSession,
    _libreoffice_protocol,
    _parse_libreoffice_result,
    semantic_probe_passed,
)
from warptest.runner import load_spec


ROOT = Path(__file__).resolve().parents[1]


def _spec(relative: str) -> dict:
    loaded = load_spec(ROOT / relative)
    assert isinstance(loaded, dict)
    return loaded


class C1DesktopRuntimeUnitTests(unittest.TestCase):
    def test_blender_letterboxed_policy_points_translate_to_live_window(self) -> None:
        session = object.__new__(BlenderC1DesktopSession)
        session.session_id = "blender-session"
        session._policy_window_mapping = {
            "width": 1060,
            "height": 600,
            "offset_x": 110,
            "offset_y": 100,
        }
        session._command = MagicMock(
            return_value={"status": "success", "accepted": True}
        )
        batch = InputBatch(
            session_id=session.session_id,
            frame_id=3,
            batch_id="batch-1",
            actions=(
                AgentSAction(kind="click", x=210, y=300, button="left", clicks=1),
            ),
        )

        session.dispatch_input_batch(batch)

        self.assertEqual(
            session.policy_to_backend_point(210, 300)["backend_event_point"],
            [100, 399],
        )
        self.assertEqual(
            session._command.call_args.kwargs["actions"][0]["x"],
            100,
        )
        self.assertEqual(
            session._command.call_args.kwargs["actions"][0]["y"],
            200,
        )
        with self.assertRaisesRegex(
            C1DesktopRuntimeError, "outside the live 1060x600 window content"
        ):
            session.policy_to_backend_point(10, 10)

    def test_blender_session_accepts_a_registered_external_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "build" / "Blender"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            session = BlenderC1DesktopSession(
                root / "sandbox" / "repo",
                root / "report",
                executable=executable,
            )
            self.assertEqual(session.executable, executable.resolve())

    def test_blender_session_rejects_a_missing_registered_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(
                C1DesktopRuntimeError, "registered Blender executable is unavailable"
            ):
                BlenderC1DesktopSession(
                    root / "sandbox" / "repo",
                    root / "report",
                    executable=root / "missing" / "Blender",
                )

    def test_libreoffice_duplicate_batch_rewrites_transport_sequence(self) -> None:
        source = (
            ROOT
            / "games/LibreOffice/vcl/headless/warptestbridge.cxx"
        ).read_text(encoding="utf-8")

        self.assertIn('duplicate.put("sequence", sequence);', source)
        self.assertIn('name == "command" || name == "cmd"', source)
        self.assertIn('response.put("sequence", *sequence);', source)

    def test_libreoffice_writer_toc_uses_native_index_semantics(self) -> None:
        source = (ROOT / "warptest/LibreOfficeC1Bridge.java").read_text(
            encoding="utf-8"
        )
        toc_assertion = source.split(
            'case "libreoffice_toc_contains": {', 1
        )[1].split("break;", 1)[0]

        self.assertIn("writerIndexContains(document, expected)", toc_assertion)
        self.assertNotIn("document.getText().getString()", toc_assertion)
        self.assertIn(
            'factory.createInstance(\n            "com.sun.star.text.ContentIndex"',
            source,
        )
        self.assertIn("supplier.getDocumentIndexes()", source)
        self.assertIn('properties.setPropertyValue("CreateFromOutline"', source)
        self.assertIn('paragraphProperties.setPropertyValue(\n                "ParaStyleName"', source)

    def test_libreoffice_vcl_capture_uses_active_repainted_window(self) -> None:
        source = (
            ROOT
            / "games/LibreOffice/vcl/headless/warptestbridge.cxx"
        ).read_text(encoding="utf-8")

        self.assertIn("Application::GetActiveTopWindow()", source)
        self.assertIn(
            "active->Invalidate(InvalidateFlags::Children | InvalidateFlags::Update);",
            source,
        )
        self.assertIn("active->PaintImmediately();", source)
        self.assertNotIn(
            "for (vcl::Window* window : visible)\n        window->PaintToDevice",
            source,
        )

    def test_libreoffice_vcl_rejection_consumes_valid_transport_sequence(self) -> None:
        class RejectedSocket:
            sent = b""
            responses = [
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": "nonce-1",
                        "sequence": 1,
                        "operation": "input_batch",
                        "accepted": False,
                        "error": "unsupported modifier",
                    }
                ).encode("utf-8"),
                b"",
            ]

            def settimeout(self, _seconds: float) -> None:
                pass

            def connect(self, _path: str) -> None:
                pass

            def sendall(self, value: bytes) -> None:
                self.sent = value

            def shutdown(self, _direction: int) -> None:
                pass

            def recv(self, _size: int) -> bytes:
                return self.responses.pop(0)

            def close(self) -> None:
                pass

        session = object.__new__(LibreOfficeC1DesktopSession)
        session.process = SimpleNamespace(poll=lambda: None)
        session.command_timeout_s = 5.0
        session.session_id = "nonce-1"
        session.vcl_socket_path = Path("/tmp/unused-test.sock")
        session._vcl_sequence = 0

        with patch(
            "warptest.c1_desktop_runtime.socket.socket",
            return_value=RejectedSocket(),
        ):
            with self.assertRaisesRegex(
                C1DesktopRuntimeError, "unsupported modifier"
            ):
                session._vcl_request("input_batch")

        self.assertEqual(session._vcl_sequence, 1)

    def test_libreoffice_vcl_connect_retry_reuses_one_logical_sequence(self) -> None:
        class FakeSocket:
            def __init__(self, *, connect_error: Exception | None = None) -> None:
                self.connect_error = connect_error
                self.sent = b""
                self.responses = [
                    json.dumps(
                        {
                            "schema_version": 1,
                            "session_id": "nonce-1",
                            "sequence": 1,
                            "operation": "ping",
                            "accepted": True,
                        }
                    ).encode("utf-8"),
                    b"",
                ]

            def settimeout(self, _seconds: float) -> None:
                pass

            def connect(self, _path: str) -> None:
                if self.connect_error is not None:
                    raise self.connect_error

            def sendall(self, value: bytes) -> None:
                self.sent = value

            def shutdown(self, _direction: int) -> None:
                pass

            def recv(self, _size: int) -> bytes:
                return self.responses.pop(0)

            def close(self) -> None:
                pass

        first = FakeSocket(connect_error=FileNotFoundError("not ready"))
        second = FakeSocket()
        session = object.__new__(LibreOfficeC1DesktopSession)
        session.process = SimpleNamespace(poll=lambda: None)
        session.command_timeout_s = 5.0
        session.session_id = "nonce-1"
        session.vcl_socket_path = Path("/tmp/unused-test.sock")
        session._vcl_sequence = 0

        with (
            patch(
                "warptest.c1_desktop_runtime.socket.socket",
                side_effect=[first, second],
            ),
            patch("warptest.c1_desktop_runtime.time.sleep"),
        ):
            response = session._vcl_request("ping")

        self.assertTrue(response["accepted"])
        self.assertEqual(session._vcl_sequence, 1)
        self.assertEqual(json.loads(second.sent)["sequence"], 1)
        self.assertEqual(first.sent, b"")

    def test_libreoffice_vcl_does_not_replay_after_dispatched_timeout(self) -> None:
        class TimeoutSocket:
            def settimeout(self, _seconds: float) -> None:
                pass

            def connect(self, _path: str) -> None:
                pass

            def sendall(self, _value: bytes) -> None:
                pass

            def shutdown(self, _direction: int) -> None:
                pass

            def recv(self, _size: int) -> bytes:
                raise socket.timeout("slow render loop")

            def close(self) -> None:
                pass

        session = object.__new__(LibreOfficeC1DesktopSession)
        session.process = SimpleNamespace(poll=lambda: None)
        session.command_timeout_s = 5.0
        session.session_id = "nonce-1"
        session.vcl_socket_path = Path("/tmp/unused-test.sock")
        session._vcl_sequence = 0
        created: list[TimeoutSocket] = []

        def make_socket(*_args: object) -> TimeoutSocket:
            value = TimeoutSocket()
            created.append(value)
            return value

        with patch(
            "warptest.c1_desktop_runtime.socket.socket", side_effect=make_socket
        ):
            with self.assertRaisesRegex(
                C1DesktopRuntimeError, "single in-flight request will not be replayed"
            ):
                session._vcl_request("ping")

        self.assertEqual(len(created), 1)
        self.assertEqual(session._vcl_sequence, 0)

    def test_blender_startup_suppresses_splash_without_fake_cli_flag(self) -> None:
        runtime_source = (ROOT / "warptest/c1_desktop_runtime.py").read_text(
            encoding="utf-8"
        )
        bridge_source = (ROOT / "warptest/blender_c1_bridge.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn('"--no-splash"', runtime_source)
        self.assertIn('"--no-window-focus"', runtime_source)
        self.assertIn("preferences.view.show_splash = False", bridge_source)
        self.assertIn('"startup_splash_suppressed"', bridge_source)

    def test_blender_bridge_keeps_file_browser_as_a_valid_policy_window(self) -> None:
        bridge_source = (ROOT / "warptest/blender_c1_bridge.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("requires exactly one Blender window", bridge_source)
        self.assertIn('area.type == "FILE_BROWSER"', bridge_source)
        self.assertIn(
            "bpy.context.temp_override(window=window)",
            bridge_source,
        )

    def test_libreoffice_live_semantic_timeout_is_a_complete_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = object.__new__(LibreOfficeC1DesktopSession)
            session.process = SimpleNamespace(poll=lambda: None)
            session._command_index = 0
            session.bridge_dir = root / "bridge"
            session.bridge_dir.mkdir()
            session.artifact_dir = root / "artifacts"
            session.artifact_dir.mkdir()
            session.classes_dir = root / "classes"
            session.java_jar = root / "libreoffice.jar"
            session.frameworks_dir = root / "Frameworks"
            session.repo = root
            session.contents_dir = root
            session.pipe_name = "office-pipe-timeout"
            session.command_timeout_s = 45.0

            with patch(
                "warptest.c1_desktop_runtime.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["java"], 45.0),
            ):
                evidence = session._run_bridge(
                    "probe_target",
                    target=_spec(
                        "specs/libreoffice/calc-quarterly-budget-rollup.yaml"
                    )["target"],
                )

        self.assertEqual(evidence["status"], "failure")
        self.assertIs(evidence["live"], True)
        self.assertIs(evidence["passed"], False)
        self.assertIs(evidence["probe_timeout"], True)
        self.assertEqual(evidence["checks"][0]["name"], "bridge.semantic_timeout")
        self.assertIs(evidence["checks"][0]["ok"], False)

    def test_blender_policy_frame_downsamples_retina_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "control" / "frames" / "frame-1.png"
            source.parent.mkdir(parents=True)
            Image.new("RGB", (2560, 1600), (10, 20, 30)).save(source)
            session = object.__new__(BlenderC1DesktopSession)
            session.width = 1280
            session.height = 800
            session.capture_backend = "blender_bpy_screen_screenshot_v1"
            session.control_dir = root / "control"
            session._command = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
                "status": "success",
                "path": str(source),
                "width": 1280,
                "height": 800,
                "frame_id": 4,
            }

            output = Path(session.capture_policy_frame(root / "policy.png"))

            with Image.open(output) as image:
                self.assertEqual(image.size, (1280, 800))
            self.assertEqual(
                session.last_policy_frame_capture,
                {
                    "source_frame": [2560, 1600],
                    "policy_frame": [1280, 800],
                    "source_to_policy_scale": 2,
                    "normalization": "integer_retina_downsample_lanczos_v1",
                },
            )

    def test_libreoffice_profile_disables_first_run_and_system_file_dialogs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            registry = root / "install" / "Resources" / "registry" / "main.xcd"
            registry.parent.mkdir(parents=True)
            registry.write_text(
                '<oor:data><prop oor:name="ooSetupVersion"><value>27.2</value>'
                '</prop></oor:data>',
                encoding="utf-8",
            )
            session = object.__new__(LibreOfficeC1DesktopSession)
            session.contents_dir = root / "install"
            session.user_dir = root / "profile"

            session._prepare_user_profile()

            modifications = (
                session.user_dir / "user" / "registrymodifications.xcu"
            ).read_text(encoding="utf-8")
            self.assertIn("<value>27.2</value>", modifications)
            self.assertIn('oor:name="WhatsNew"', modifications)
            self.assertIn('oor:name="ShowTipOfTheDay"', modifications)
            self.assertIn('oor:name="UseSystemFileDialog"', modifications)
            self.assertEqual(modifications.count("<value>false</value>"), 5)

    def test_semantic_probe_passes_only_nonempty_live_boolean_checks(self) -> None:
        valid = {
            "status": "success",
            "source": BLENDER_SEMANTIC_SOURCE,
            "live": True,
            "checks": [{"name": "object", "ok": True}],
        }
        self.assertTrue(
            semantic_probe_passed(valid, expected_source=BLENDER_SEMANTIC_SOURCE)
        )
        for mutation in (
            {**valid, "checks": []},
            {**valid, "live": False},
            {**valid, "source": "planned_default"},
            {**valid, "checks": [{"name": "object", "ok": False}]},
        ):
            self.assertFalse(
                semantic_probe_passed(mutation, expected_source=BLENDER_SEMANTIC_SOURCE)
            )

    def test_blender_restore_sends_target_only_never_task_transition(self) -> None:
        spec = _spec("specs/blender/scene-cube-material.yaml")
        session = object.__new__(BlenderC1DesktopSession)
        session.artifact_dir = Path("/tmp/c1-blender-artifacts")
        captured: dict[str, object] = {}

        def command(name: str, **payload: object) -> dict:
            captured.update({"command": name, **payload})
            return {"status": "success"}

        session._command = command  # type: ignore[method-assign]
        session.restore_target(spec)
        self.assertEqual(captured["command"], "restore_target")
        self.assertEqual(captured["target"], spec["target"])
        self.assertNotIn("actions", captured)
        self.assertNotIn("assertions", captured)

    def test_blender_restore_returns_structured_semantic_failure(self) -> None:
        spec = _spec("specs/blender/scene-cube-material.yaml")
        session = object.__new__(BlenderC1DesktopSession)
        session.artifact_dir = Path("/tmp/c1-blender-artifacts")
        report = {
            "status": "failure",
            "source": BLENDER_SEMANTIC_SOURCE,
            "live": True,
            "checks": [
                {"name": "restore.target_only", "ok": True},
                {"name": "target.object", "ok": False},
            ],
        }
        session._command = lambda *_args, **_kwargs: report  # type: ignore[method-assign]
        self.assertIs(session.restore_target(spec), report)

    def test_blender_restore_rejects_invalid_bridge_status(self) -> None:
        spec = _spec("specs/blender/scene-cube-material.yaml")
        session = object.__new__(BlenderC1DesktopSession)
        session.artifact_dir = Path("/tmp/c1-blender-artifacts")
        session._command = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "status": "error"
        }
        with self.assertRaisesRegex(C1DesktopRuntimeError, "invalid status"):
            session.restore_target(spec)

    def test_blender_semantic_probe_fails_closed_on_bad_live_check(self) -> None:
        spec = _spec("specs/blender/modifier-apply.yaml")
        session = object.__new__(BlenderC1DesktopSession)
        session._command = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "status": "failure",
            "source": BLENDER_SEMANTIC_SOURCE,
            "live": True,
            "checks": [{"name": "target.object", "ok": False}],
        }
        evidence = session.probe_start(spec)
        self.assertFalse(evidence["passed"])

    def test_blender_state_transition_uses_explicit_mutation_command_then_live_goal(
        self,
    ) -> None:
        spec = _spec("specs/blender/modifier-apply.yaml")
        session = object.__new__(BlenderC1DesktopSession)
        session.artifact_dir = Path("/tmp/c1-blender-artifacts")
        captured: dict[str, object] = {}

        def command(name: str, **payload: object) -> dict:
            captured.update({"command": name, **payload})
            return {
                "status": "success",
                "source": BLENDER_SEMANTIC_SOURCE,
                "live": True,
                "checks": [{"name": "transition.actions_applied", "ok": True}],
            }

        session._command = command  # type: ignore[method-assign]
        session.probe_goal = lambda _spec: {  # type: ignore[method-assign]
            "status": "success",
            "source": BLENDER_SEMANTIC_SOURCE,
            "live": True,
            "passed": True,
            "checks": [{"name": "assertion[modifier]", "ok": True}],
        }

        result = session.execute_state_transition(spec)

        self.assertEqual(captured["command"], "apply_state_actions")
        self.assertEqual(captured["actions"], spec["actions"])
        self.assertEqual(
            result["state_transition_backend"],
            "blender_live_bpy_state_transition_v1",
        )
        self.assertEqual(
            result["state_action_checks"][0]["name"],
            "transition.actions_applied",
        )

    def test_blender_state_transition_rejects_unregistered_action(self) -> None:
        session = object.__new__(BlenderC1DesktopSession)
        with self.assertRaisesRegex(
            C1DesktopRuntimeUnsupported, "unsupported Blender C1 state actions"
        ):
            session.execute_state_transition(
                {"actions": [{"type": "blender_run_arbitrary_python"}]}
            )

    def test_blender_state_transition_returns_semantic_action_failure(self) -> None:
        spec = _spec("specs/blender/modifier-apply.yaml")
        session = object.__new__(BlenderC1DesktopSession)
        session.artifact_dir = Path("/tmp/c1-blender-artifacts")
        report = {
            "status": "failure",
            "source": BLENDER_SEMANTIC_SOURCE,
            "live": True,
            "checks": [
                {"name": "action[blender_add_modifier]@0", "ok": False},
                {"name": "transition.actions_applied", "ok": False},
            ],
        }
        session._command = lambda *_args, **_kwargs: report  # type: ignore[method-assign]
        session.probe_goal = lambda *_args, **_kwargs: self.fail(  # type: ignore[method-assign]
            "goal probe must not run after a failed registered action"
        )

        result = session.execute_state_transition(spec)

        self.assertIs(result, report)
        self.assertIs(result["passed"], False)
        self.assertEqual(result["state_transition_stage"], "actions")
        self.assertEqual(
            result["state_transition_backend"],
            "blender_live_bpy_state_transition_v1",
        )
        self.assertEqual(result["state_action_checks"], result["checks"])

    def test_desktop_session_has_no_foreground_native_lock_surface(self) -> None:
        session = object.__new__(BlenderC1DesktopSession)
        self.assertFalse(hasattr(session, "_acquire_native_lock"))
        self.assertFalse(hasattr(session, "_release_native_lock"))

    def test_native_seed_evidence_uses_common_stable_live_schema(self) -> None:
        class FakeProcess:
            pid = 4312

            @staticmethod
            def poll() -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "desktop-app"
            executable.write_bytes(b"native-app")
            session = object.__new__(BlenderC1DesktopSession)
            session.process = FakeProcess()
            session.executable = executable
            session.repo = Path(tmp) / "not-a-repo"
            probe = {
                "status": "failure",
                "source": BLENDER_SEMANTIC_SOURCE,
                "live": True,
                "checks": [{"name": "target.location", "ok": False, "detail": "wrong"}],
            }
            evidence = session.seed_evidence(17, probe, "blender-cube", "entry")

        self.assertEqual(evidence["schema_version"], 1)
        self.assertEqual(evidence["source"], BLENDER_SEED_EVIDENCE_SOURCE)
        self.assertEqual(evidence["algorithm"], NATIVE_GUI_SEED_ALGORITHM)
        self.assertEqual(evidence["seed"], 17)
        self.assertEqual(evidence["benchmark"], "Blender")
        self.assertEqual(evidence["process_id"], 4312)
        self.assertIs(evidence["process_alive_at_observation"], True)
        # Observation means a real readback occurred; a failed task check remains
        # observed evidence rather than being rewritten as infrastructure absence.
        self.assertIs(evidence["observed"], True)
        self.assertEqual(evidence["semantic_source"], BLENDER_SEMANTIC_SOURCE)
        self.assertEqual(evidence["semantic_stage"], "entry")
        self.assertEqual(evidence["semantic_observation"], probe)
        self.assertEqual(len(evidence["semantic_fingerprint"]), 64)
        self.assertEqual(
            evidence["semantic_fingerprint"],
            evidence["initial_semantic_fingerprint"],
        )
        self.assertEqual(evidence["randomness"], "not_applicable")
        self.assertEqual(evidence["seed_role"], "trial_pairing_only")
        self.assertEqual(evidence["execution_backend"], "background_pixel_v1")
        self.assertIs(evidence["foreground_required"], False)
        self.assertEqual(
            evidence["coordinate_space"], "frame_pixels_top_left_v1"
        )
        self.assertEqual(
            evidence["capture_backend"], "blender_bpy_screen_screenshot_v1"
        )
        self.assertEqual(
            evidence["input_backend"], "blender_window_event_simulate_v1"
        )
        self.assertEqual(evidence["executable_size"], len(b"native-app"))

    def test_native_seed_fingerprint_is_canonical_and_app_source_is_distinct(
        self,
    ) -> None:
        class FakeProcess:
            pid = 991

            @staticmethod
            def poll() -> None:
                return None

        session = object.__new__(LibreOfficeC1DesktopSession)
        session.process = FakeProcess()
        session.soffice = Path("/missing/soffice")
        session.repo = Path("/missing/repo")
        probe_a = {
            "source": LIBREOFFICE_SEMANTIC_SOURCE,
            "live": True,
            "checks": [{"detail": "B2=40000", "ok": True, "name": "cell"}],
            "status": "success",
        }
        probe_b = {
            "status": "success",
            "checks": [{"name": "cell", "ok": True, "detail": "B2=40000"}],
            "live": True,
            "source": LIBREOFFICE_SEMANTIC_SOURCE,
        }
        first = session.seed_evidence(3, probe_a, "lo-calc", "restored")
        second = session.seed_evidence(3, probe_b, "lo-calc", "restored")
        self.assertEqual(first["source"], LIBREOFFICE_SEED_EVIDENCE_SOURCE)
        self.assertEqual(first["benchmark"], "LibreOffice")
        self.assertEqual(first["semantic_fingerprint"], second["semantic_fingerprint"])
        changed = dict(probe_b)
        changed["checks"] = [{"name": "cell", "ok": True, "detail": "B2=0"}]
        third = session.seed_evidence(3, changed, "lo-calc", "restored")
        self.assertNotEqual(
            first["semantic_fingerprint"], third["semantic_fingerprint"]
        )

    def test_native_seed_evidence_rejects_non_live_or_empty_semantics(self) -> None:
        session = object.__new__(BlenderC1DesktopSession)
        session.process = None
        session.executable = Path("/missing/blender")
        session.repo = Path("/missing/repo")
        with self.assertRaises(C1DesktopRuntimeError):
            session.seed_evidence(0, {}, "task", "entry")
        with self.assertRaises(C1DesktopRuntimeError):
            session.seed_evidence(
                0,
                {
                    "source": "planned_default",
                    "live": True,
                    "checks": [{"name": "x", "ok": True}],
                },
                "task",
                "entry",
            )

    def test_native_seed_evidence_records_dead_process_as_unobserved(self) -> None:
        class DeadProcess:
            pid = 77

            @staticmethod
            def poll() -> int:
                return 9

        session = object.__new__(LibreOfficeC1DesktopSession)
        session.process = DeadProcess()
        session.soffice = Path("/missing/soffice")
        session.repo = Path("/missing/repo")
        evidence = session.seed_evidence(
            5,
            {
                "source": LIBREOFFICE_SEMANTIC_SOURCE,
                "live": True,
                "checks": [{"name": "slide", "ok": True}],
            },
            "lo-impress",
            "entry",
        )
        self.assertIs(evidence["process_alive_at_observation"], False)
        self.assertIs(evidence["observed"], False)
        self.assertEqual(evidence["process_id"], 77)

    def test_libreoffice_selected_targets_have_allowlisted_target_only_protocol(
        self,
    ) -> None:
        for relative in (
            "specs/libreoffice/calc-quarterly-budget-rollup.yaml",
            "specs/libreoffice/impress-status-deck-build.yaml",
        ):
            spec = _spec(relative)
            protocol = _libreoffice_protocol(
                "restore_target",
                target=spec["target"],
                artifact_dir=Path("/tmp/c1-lo-artifacts"),
            )
            record_kinds = [row.split("\t", 1)[0] for row in protocol.splitlines()[1:]]
            self.assertIn("KIND", record_kinds)
            self.assertNotIn("ACTION", record_kinds)
            self.assertNotIn("ASSERT", record_kinds)
            decoded_command = base64.b64decode(
                protocol.splitlines()[1].split("\t", 1)[1]
            ).decode("utf-8")
            self.assertEqual(decoded_command, "restore_target")

    def test_libreoffice_selected_goal_assertions_are_allowlisted(self) -> None:
        for relative in (
            "specs/libreoffice/calc-quarterly-budget-rollup.yaml",
            "specs/libreoffice/impress-status-deck-build.yaml",
        ):
            spec = _spec(relative)
            protocol = _libreoffice_protocol(
                "probe_assertions", assertions=spec["assertions"]
            )
            self.assertEqual(
                sum(row.startswith("ASSERT\t") for row in protocol.splitlines()),
                len(spec["assertions"]),
            )

    def test_libreoffice_visual_capture_protocol_is_allowlisted_and_scoped(
        self,
    ) -> None:
        spec = _spec("specs/libreoffice/c1-calc-forecast-ranking-chart.yaml")
        protocol = _libreoffice_protocol(
            "prepare_visual_capture",
            target=spec["target"],
            visual={
                "label": "after",
                "active_sheet": "Regions",
            },
        )
        decoded: dict[str, list[str]] = {}
        for raw in protocol.splitlines()[1:]:
            fields = raw.split("\t")
            decoded.setdefault(fields[0], []).extend(
                base64.b64decode(value).decode("utf-8")
                for value in fields[1:]
            )
        self.assertEqual(decoded["VISUAL_LABEL"], ["after"])
        self.assertEqual(decoded["ACTIVE_SHEET"], ["Regions"])
        self.assertNotIn("ACTION", decoded)
        with self.assertRaises(C1DesktopRuntimeUnsupported):
            _libreoffice_protocol(
                "prepare_visual_capture",
                target=spec["target"],
                visual={"label": "after", "active_sheet": ""},
            )

    def test_libreoffice_state_protocol_allows_only_registered_mutations(self) -> None:
        spec = _spec("specs/libreoffice/calc-quarterly-budget-rollup.yaml")
        mutations = spec["actions"][:-2]
        protocol = _libreoffice_protocol(
            "apply_actions", target=spec["target"], actions=mutations
        )
        self.assertEqual(
            sum(row.startswith("ACTION\t") for row in protocol.splitlines()),
            len(mutations),
        )
        with self.assertRaises(C1DesktopRuntimeUnsupported):
            _libreoffice_protocol(
                "apply_actions",
                target=spec["target"],
                actions=[{"type": "libreoffice_save"}],
            )
        with self.assertRaises(C1DesktopRuntimeError):
            _libreoffice_protocol(
                "probe_assertions", actions=mutations
            )

    def test_libreoffice_writer_and_pdf_assertions_are_allowlisted(self) -> None:
        writer = _spec("specs/libreoffice/writer-board-report-toc.yaml")
        restore = _libreoffice_protocol(
            "restore_target", target=writer["target"]
        )
        probe = _libreoffice_protocol(
            "probe_assertions",
            assertions=[{"type": "libreoffice_pdf_exists", "path": "proof.pdf"}],
        )
        self.assertIn("KIND\t", restore)
        self.assertIn("ASSERT\t", probe)

    def test_libreoffice_writer_ui_default_style_maps_to_uno_standard(self) -> None:
        protocol = _libreoffice_protocol(
            "restore_target",
            target={
                "kind": "writer_document",
                "paragraphs": [
                    {
                        "text": "Draft packet for board publication.",
                        "style": "Default Paragraph Style",
                    }
                ],
            },
        )
        paragraph = next(
            row for row in protocol.splitlines() if row.startswith("PARAGRAPH\t")
        )
        fields = paragraph.split("\t")
        self.assertEqual(
            [base64.b64decode(value).decode("utf-8") for value in fields[1:]],
            ["Draft packet for board publication.", "Standard"],
        )

    def test_libreoffice_rejects_artifact_path_traversal(self) -> None:
        target = {
            "kind": "calc_spreadsheet",
            "path": "../escaped.ods",
            "sheets": {"Sheet1": {"cells": {"A1": "x"}}},
        }
        with self.assertRaises(C1DesktopRuntimeUnsupported):
            _libreoffice_protocol("restore_target", target=target)

    def test_libreoffice_result_parser_preserves_live_fail_closed_checks(self) -> None:
        def line(kind: str, *values: str) -> str:
            encoded = [
                base64.b64encode(value.encode("utf-8")).decode("ascii")
                for value in values
            ]
            return "\t".join([kind, *encoded])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "response"
            path.write_text(
                "\n".join(
                    [
                        "C1_DESKTOP_RESULT_V1",
                        line("STATUS", "failure"),
                        line("SOURCE", LIBREOFFICE_SEMANTIC_SOURCE),
                        line("DOCUMENT_IDENTITY", "uno-document-1"),
                        line("DOCUMENT_URL", ""),
                        line("DOCUMENT_MODIFIED", "true"),
                        line("SESSION_IDENTITY", "office-pipe-1"),
                        line("CHECK", "target.cell[Summary.B2]", "false", "actual=0"),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result = _parse_libreoffice_result(path)
        self.assertEqual(result["status"], "failure")
        self.assertIs(result["live"], True)
        self.assertEqual(result["document_identity"], "uno-document-1")
        self.assertEqual(result["document_url"], "")
        self.assertIs(result["document_modified"], True)
        self.assertEqual(result["session_identity"], "office-pipe-1")
        self.assertIs(result["checks"][0]["ok"], False)
        self.assertFalse(
            semantic_probe_passed(result, expected_source=LIBREOFFICE_SEMANTIC_SOURCE)
        )

    def test_libreoffice_probe_does_not_promote_empty_success(self) -> None:
        spec = _spec("specs/libreoffice/calc-quarterly-budget-rollup.yaml")
        session = object.__new__(LibreOfficeC1DesktopSession)
        session._transition_origin = {
            "document_identity": "uno-document-1",
            "session_identity": "office-pipe-1",
            "document_url": "",
            "document_modified": True,
        }
        session._run_bridge = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "status": "success",
            "source": LIBREOFFICE_SEMANTIC_SOURCE,
            "live": True,
            "checks": [],
        }
        evidence = session.probe_goal(spec)
        self.assertFalse(evidence["passed"])

    def test_libreoffice_goal_protocol_binds_start_document_and_session(self) -> None:
        spec = _spec("specs/libreoffice/calc-quarterly-budget-rollup.yaml")
        protocol = _libreoffice_protocol(
            "probe_assertions",
            assertions=spec["assertions"],
            transition_origin={
                "document_identity": "uno-document-1",
                "session_identity": "office-pipe-1",
            },
        )
        decoded: dict[str, list[str]] = {}
        for raw in protocol.splitlines()[1:]:
            fields = raw.split("\t")
            decoded.setdefault(fields[0], []).extend(
                base64.b64decode(value).decode("utf-8") for value in fields[1:]
            )
        self.assertEqual(
            decoded["ORIGIN_DOCUMENT_IDENTITY"], ["uno-document-1"]
        )
        self.assertEqual(decoded["ORIGIN_SESSION_IDENTITY"], ["office-pipe-1"])

    def test_libreoffice_goal_protocol_uses_declared_native_check_order(self) -> None:
        spec = _spec("specs/libreoffice/calc-quarterly-budget-rollup.yaml")
        declared = spec["evaluation"]["semantic"]["surfaces"]["gui"]["goal"][
            "native_checks"
        ]
        protocol = _libreoffice_protocol(
            "probe_assertions",
            assertions=spec["assertions"],
            transition_origin={
                "document_identity": "uno-document-1",
                "session_identity": "office-pipe-1",
            },
            transition_checks=declared,
        )
        decoded_checks = [
            base64.b64decode(raw.split("\t", 1)[1]).decode("utf-8")
            for raw in protocol.splitlines()
            if raw.startswith("TRANSITION_CHECK\t")
        ]

        self.assertEqual(decoded_checks, declared)
        with self.assertRaisesRegex(
            C1DesktopRuntimeUnsupported, "must be unique"
        ):
            _libreoffice_protocol(
                "probe_assertions",
                transition_checks=[declared[0], declared[0]],
            )
        with self.assertRaisesRegex(
            C1DesktopRuntimeUnsupported, "unsupported LibreOffice transition check"
        ):
            _libreoffice_protocol(
                "probe_assertions",
                transition_checks=["transition.injected_by_code"],
            )
        with self.assertRaisesRegex(
            C1DesktopRuntimeError, "exclusive to probe_assertions"
        ):
            _libreoffice_protocol(
                "restore_target",
                transition_checks=declared,
            )

    def test_libreoffice_java_executes_only_declared_transition_records(self) -> None:
        source = (ROOT / "warptest/LibreOfficeC1Bridge.java").read_text(
            encoding="utf-8"
        )
        method = source.split(
            "private void addDeclaredTransitionPersistenceChecks", 1
        )[1].split("private String assertionDocumentKind", 1)[0]

        self.assertIn('for (Record record : recordsOf("TRANSITION_CHECK"))', method)
        self.assertEqual(method.count('case "transition.'), 5)
        self.assertLess(
            method.index('for (Record record : recordsOf("TRANSITION_CHECK"))'),
            method.index("result.checks.add"),
        )

    def test_libreoffice_restore_records_unsaved_transition_origin(self) -> None:
        spec = _spec("specs/libreoffice/calc-quarterly-budget-rollup.yaml")
        session = object.__new__(LibreOfficeC1DesktopSession)
        session.pipe_name = "office-pipe-1"
        session._transition_origin = None
        session._transition_origin_from_restore = False
        session._run_bridge = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "status": "success",
            "source": LIBREOFFICE_SEMANTIC_SOURCE,
            "live": True,
            "checks": [{"name": "restore.target_only", "ok": True}],
            "document_identity": "uno-document-1",
            "document_url": "",
            "document_modified": True,
            "session_identity": "office-pipe-1",
        }
        session.restore_target(spec)
        self.assertEqual(
            session._transition_origin["document_identity"], "uno-document-1"
        )
        self.assertIs(session._transition_origin_from_restore, True)

    def test_libreoffice_restore_returns_structured_semantic_failure(self) -> None:
        spec = _spec("specs/libreoffice/calc-quarterly-budget-rollup.yaml")
        session = object.__new__(LibreOfficeC1DesktopSession)
        session.pipe_name = "office-pipe-1"
        session._transition_origin = None
        session._transition_origin_from_restore = False
        report = {
            "status": "failure",
            "source": LIBREOFFICE_SEMANTIC_SOURCE,
            "live": True,
            "checks": [
                {"name": "restore.target_only", "ok": True},
                {"name": "target.cell", "ok": False},
            ],
            "document_identity": "uno-document-1",
            "document_url": "",
            "document_modified": True,
            "session_identity": "office-pipe-1",
        }
        session._run_bridge = lambda *_args, **_kwargs: report  # type: ignore[method-assign]
        self.assertIs(session.restore_target(spec), report)
        self.assertEqual(
            session._transition_origin["document_identity"], "uno-document-1"
        )
        self.assertIs(session._transition_origin_from_restore, True)

    def test_libreoffice_restore_rejects_invalid_bridge_status(self) -> None:
        spec = _spec("specs/libreoffice/calc-quarterly-budget-rollup.yaml")
        session = object.__new__(LibreOfficeC1DesktopSession)
        session.pipe_name = "office-pipe-1"
        session._transition_origin = None
        session._transition_origin_from_restore = False
        session._run_bridge = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "status": "error"
        }
        with self.assertRaisesRegex(C1DesktopRuntimeError, "invalid status"):
            session.restore_target(spec)

    def test_libreoffice_argument_rejection_is_a_scored_treatment_failure(
        self,
    ) -> None:
        """An unexecutable model spec must not read as an infrastructure outage.

        The bridge validates caller arguments with ``IllegalArgumentException``
        and reports its own runtime problems with ``IllegalStateException``. When
        both were raised as the same error, one model spec asking for an
        unsupported shape marked the trial incomplete, which aborted the whole
        invocation and made the run ineligible for trial-atomic recovery.
        """

        rejection = (
            "IllegalArgumentException: only the registered Impress callout "
            "shape is supported"
        )
        assert c1_desktop_runtime._libreoffice_rejected_arguments(
            {"errors": [rejection]}, ""
        )
        assert c1_desktop_runtime._libreoffice_rejected_arguments(
            {"checks": [{"name": "bridge.exception", "detail": rejection}]}, ""
        )
        assert c1_desktop_runtime._libreoffice_rejected_arguments(None, rejection)

        # The runtime's own failures stay infrastructure errors.
        internal = "IllegalStateException: office process exited"
        assert not c1_desktop_runtime._libreoffice_rejected_arguments(
            {"errors": [internal]}, internal
        )
        assert not c1_desktop_runtime._libreoffice_rejected_arguments(None, "")
        assert issubclass(C1DesktopRuntimeUnsupported, C1DesktopRuntimeError)

    def test_libreoffice_state_transition_separates_mutation_from_save_reopen(
        self,
    ) -> None:
        spec = _spec("specs/libreoffice/calc-quarterly-budget-rollup.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            session = object.__new__(LibreOfficeC1DesktopSession)
            session.artifact_dir = Path(tmp) / "artifacts"
            session.artifact_dir.mkdir()
            session._transition_origin = {
                "document_identity": "uno-document-1",
                "session_identity": "office-pipe-1",
                "document_url": "",
                "document_modified": True,
            }
            captured: dict[str, object] = {}

            def bridge(command: str, **payload: object) -> dict:
                captured.update({"command": command, **payload})
                return {
                    "status": "success",
                    "source": LIBREOFFICE_SEMANTIC_SOURCE,
                    "live": True,
                    "checks": [
                        {"name": "transition.actions_applied", "ok": True}
                    ],
                    "document_identity": "uno-document-1",
                    "document_url": "",
                    "document_modified": True,
                    "session_identity": "office-pipe-1",
                }

            destination = session.artifact_dir / spec["target"]["path"]
            session._run_bridge = bridge  # type: ignore[method-assign]
            session._save_and_reopen = (  # type: ignore[method-assign]
                lambda _target, path: {
                    "document_identity": "uno-document-2",
                    "document_url": path.as_uri(),
                    "document_modified": False,
                    "session_identity": "office-pipe-1",
                }
            )
            session.probe_goal = lambda _spec: {  # type: ignore[method-assign]
                "status": "success",
                "source": LIBREOFFICE_SEMANTIC_SOURCE,
                "live": True,
                "passed": True,
                "checks": [{"name": "transition.document_reopened", "ok": True}],
                "document_identity": "uno-document-2",
                "document_url": destination.as_uri(),
                "document_path": str(destination),
                "document_modified": False,
                "session_identity": "office-pipe-1",
            }

            result = session.execute_state_transition(spec)

        self.assertEqual(captured["command"], "apply_actions")
        self.assertEqual(captured["actions"], spec["actions"][:-2])
        self.assertNotIn(
            "libreoffice_save",
            [action["type"] for action in captured["actions"]],
        )
        self.assertEqual(
            result["state_transition_backend"],
            "uno_mutation_native_ui_save_reopen_v1",
        )
        self.assertEqual(
            result["state_action_checks"][0]["name"],
            "transition.actions_applied",
        )

    def test_libreoffice_state_transition_rejects_missing_terminal_reopen(self) -> None:
        spec = _spec("specs/libreoffice/calc-quarterly-budget-rollup.yaml")
        spec["actions"] = spec["actions"][:-1]
        session = object.__new__(LibreOfficeC1DesktopSession)
        session._transition_origin = {
            "document_identity": "uno-document-1",
            "session_identity": "office-pipe-1",
        }
        with self.assertRaisesRegex(
            C1DesktopRuntimeUnsupported, "consecutive save/reopen"
        ):
            session.execute_state_transition(spec)

    def test_libreoffice_state_transition_returns_semantic_mutation_failure(self) -> None:
        spec = _spec("specs/libreoffice/calc-quarterly-budget-rollup.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            session = object.__new__(LibreOfficeC1DesktopSession)
            session.artifact_dir = Path(tmp)
            session._transition_origin = {
                "document_identity": "uno-document-1",
                "session_identity": "office-pipe-1",
            }
            report = {
                "status": "failure",
                "source": LIBREOFFICE_SEMANTIC_SOURCE,
                "live": True,
                "checks": [
                    {"name": "action[libreoffice_set_cell]@0", "ok": False},
                    {"name": "transition.actions_applied", "ok": False},
                ],
                "document_identity": "uno-document-1",
                "document_url": "",
                "document_modified": True,
                "session_identity": "office-pipe-1",
            }
            session._run_bridge = lambda *_args, **_kwargs: report  # type: ignore[method-assign]
            session._save_and_reopen = lambda *_args, **_kwargs: self.fail(  # type: ignore[method-assign]
                "save/reopen must not run after a failed registered mutation"
            )

            result = session.execute_state_transition(spec)

        self.assertIs(result, report)
        self.assertIs(result["passed"], False)
        self.assertEqual(result["state_transition_stage"], "actions")
        self.assertEqual(
            result["state_transition_backend"],
            "uno_mutation_native_ui_save_reopen_v1",
        )
        self.assertEqual(result["state_action_checks"], result["checks"])


@unittest.skipUnless(
    os.environ.get("WARPTEST_RUN_DESKTOP_INTEGRATION") == "1",
    "set WARPTEST_RUN_DESKTOP_INTEGRATION=1 for real foreground app tests",
)
class C1DesktopRuntimeLocalIntegrationTests(unittest.TestCase):
    def test_blender_target_only_restore_is_live_and_goal_is_not_faked(self) -> None:
        spec = _spec("specs/blender/scene-cube-material.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            with BlenderC1DesktopSession(
                ROOT / "games/blender", Path(tmp) / "blender"
            ) as session:
                session.restore_target(spec)
                self.assertTrue(session.probe_start(spec)["passed"])
                self.assertFalse(session.probe_goal(spec)["passed"])

    def test_blender_registered_state_pairs_remain_live_through_goal(self) -> None:
        for relative in (
            "specs/blender/scene-cube-material.yaml",
            "specs/blender/modifier-apply.yaml",
        ):
            with self.subTest(spec=relative), tempfile.TemporaryDirectory() as tmp:
                spec = _spec(relative)
                with BlenderC1DesktopSession(
                    ROOT / "games/blender", Path(tmp) / "blender-state"
                ) as session:
                    session.restore_target(spec)
                    start = session.probe_start(spec)
                    goal = session.execute_state_transition(spec)
                    self.assertTrue(start["passed"])
                    self.assertTrue(goal["passed"])
                    self.assertEqual(
                        goal["state_transition_backend"],
                        "blender_live_bpy_state_transition_v1",
                    )
                    self.assertIsNone(session.process.poll())

    def test_libreoffice_target_only_restore_is_live_and_goal_is_not_faked(
        self,
    ) -> None:
        spec = _spec("specs/libreoffice/calc-quarterly-budget-rollup.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            with LibreOfficeC1DesktopSession(
                ROOT / "games/LibreOffice", Path(tmp) / "libreoffice"
            ) as session:
                session.restore_target(spec)
                self.assertTrue(session.probe_start(spec)["passed"])
                self.assertFalse(session.probe_goal(spec)["passed"])

    def test_libreoffice_impress_target_only_restore_is_live_and_goal_is_not_faked(
        self,
    ) -> None:
        spec = _spec("specs/libreoffice/impress-status-deck-build.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            with LibreOfficeC1DesktopSession(
                ROOT / "games/LibreOffice", Path(tmp) / "libreoffice-impress"
            ) as session:
                session.restore_target(spec)
                self.assertTrue(session.probe_start(spec)["passed"])
                self.assertFalse(session.probe_goal(spec)["passed"])

    def test_libreoffice_calc_and_impress_state_pairs_persist_and_reopen(self) -> None:
        for relative in (
            "specs/libreoffice/calc-quarterly-budget-rollup.yaml",
            "specs/libreoffice/impress-status-deck-build.yaml",
        ):
            with self.subTest(spec=relative), tempfile.TemporaryDirectory() as tmp:
                spec = _spec(relative)
                with LibreOfficeC1DesktopSession(
                    ROOT / "games/LibreOffice", Path(tmp) / "libreoffice-state"
                ) as session:
                    session.restore_target(spec)
                    start = session.probe_start(spec)
                    goal = session.execute_state_transition(spec)
                    self.assertTrue(start["passed"])
                    self.assertTrue(goal["passed"])
                    self.assertEqual(
                        goal["state_transition_backend"],
                        "uno_mutation_native_ui_save_reopen_v1",
                    )
                    self.assertNotEqual(
                        goal["document_identity"], start["document_identity"]
                    )
                    self.assertFalse(goal["document_modified"])
                    self.assertTrue(Path(goal["document_path"]).is_file())

    def test_libreoffice_writer_native_toc_persists_and_reopens(self) -> None:
        spec = _spec("specs/libreoffice/c1-writer-board-publication.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            with LibreOfficeC1DesktopSession(
                ROOT / "games/LibreOffice", Path(tmp) / "libreoffice-writer"
            ) as session:
                session.restore_target(spec)
                start = session.probe_start(spec)
                goal = session.execute_state_transition(spec)

                self.assertTrue(start["passed"])
                self.assertTrue(goal["passed"])
                self.assertEqual(
                    goal["state_transition_backend"],
                    "uno_mutation_native_ui_save_reopen_v1",
                )
                self.assertTrue(
                    any(
                        check["name"] == "libreoffice_toc_contains"
                        and check["ok"]
                        for check in goal["checks"]
                    )
                )
                self.assertFalse(goal["document_modified"])
                self.assertTrue(Path(goal["document_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
