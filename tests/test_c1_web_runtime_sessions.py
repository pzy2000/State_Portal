from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import yaml

from warptest.c1_web_runtime import (
    C1WebRuntimeError,
    JupyterLabC1WebSession,
    create_c1_web_session,
)
from warptest.jupyterlab_runtime import JupyterLabRuntimeAdapter


ROOT = Path(__file__).resolve().parents[1]


class _LivePage:
    url = "http://127.0.0.1/live"


class _FakeSessionRuntime:
    def __init__(self) -> None:
        self.page = _LivePage()
        self.app_url = "http://127.0.0.1/live"
        self.calls: list[object] = []

    def start_c1_session(self, *, require_headed: bool):
        self.calls.append(("start", require_headed))
        return self

    def clean_c1_entry(self, *, require_headed: bool):
        self.calls.append(("clean", require_headed))
        return {
            "ok": True,
            "actions_executed": False,
            "detail": "clean",
            "observation": {"live": True},
            "artifacts": {},
        }

    def restore_c1_target(self, spec, spec_path, *, require_headed: bool):
        self.calls.append(("restore", spec, spec_path, require_headed))
        return {
            "ok": True,
            "actions_executed": False,
            "detail": "restored",
            "observation": {"target": spec["target"]},
            "artifacts": {},
        }

    def bind_c1_live_target(self, spec, spec_path, *, require_headed: bool):
        self.calls.append(("bind", spec, spec_path, require_headed))
        return {
            "ok": True,
            "actions_executed": False,
            "app_state_mutated": False,
            "detail": "bound",
            "observation": {"live": True},
            "artifacts": {},
        }

    def screenshot_c1_live(self, name: str, full_page: bool = False):
        self.calls.append(("screenshot", name, full_page))
        return f"/tmp/{name}"

    def grade_c1_start(self, _spec):
        return {"ok": True, "status": "success", "detail": "start", "observation": {"value": 1}}

    def apply_c1_state_transition(self, _spec):
        self.calls.append("transition")
        return {
            "ok": True,
            "status": "success",
            "detail": "transition",
            "observation": {"value": 2},
            "artifacts": {},
            "actions_executed": True,
            "action_count": 1,
        }

    def grade_c1_goal(self, _spec):
        return {"ok": True, "status": "success", "detail": "goal", "observation": {"value": 2}}

    def close(self):
        self.calls.append("close")


class C1WebSessionContractTests(unittest.TestCase):
    def test_jupyter_wrapper_exposes_target_only_contract(self) -> None:
        fake = _FakeSessionRuntime()
        session = JupyterLabC1WebSession(
            ROOT / "games/jupyterlab",
            ROOT / "reports/test",
            runtime=fake,  # type: ignore[arg-type]
        )
        session.start()
        restored = session.restore_target_only(
            {"target": {"kind": "text_file"}}, ROOT / "specs/jupyterlab/text-file-edit.yaml"
        )
        self.assertFalse(restored.actions_executed)
        self.assertEqual(restored.phase, "target_only")
        bound = session.bind_live_target_for_grading(
            {"target": {"kind": "text_file"}}, None
        )
        self.assertFalse(bound.actions_executed)
        self.assertFalse(bound.app_state_mutated)
        self.assertIs(session.page, fake.page)
        self.assertTrue(session.semantic_start({}).ok)
        self.assertTrue(session.apply_registered_transition({}).actions_executed)
        self.assertTrue(session.semantic_goal({}).ok)

    def test_restore_result_fails_closed_without_action_attestation(self) -> None:
        fake = _FakeSessionRuntime()
        fake.restore_c1_target = Mock(
            return_value={"ok": True, "observation": {"live": True}, "artifacts": {}}
        )
        session = JupyterLabC1WebSession(
            ROOT / "games/jupyterlab",
            ROOT / "reports/test",
            runtime=fake,  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(C1WebRuntimeError, "actions_executed=false"):
            session.restore_target_only({"target": {}}, None)

    def test_live_binding_fails_closed_on_state_mutation_attestation(self) -> None:
        fake = _FakeSessionRuntime()
        fake.bind_c1_live_target = Mock(
            return_value={
                "ok": True,
                "actions_executed": False,
                "app_state_mutated": True,
                "observation": {"live": True},
                "artifacts": {},
            }
        )
        session = JupyterLabC1WebSession(
            ROOT / "games/jupyterlab",
            ROOT / "reports/test",
            runtime=fake,  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(C1WebRuntimeError, "app_state_mutated=false"):
            session.bind_live_target_for_grading({"target": {}}, None)

    def test_factory_routes_only_the_supported_web_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jupyter = create_c1_web_session(
                "JupyterLab", ROOT / "games/jupyterlab", tmp, require_headed=False
            )
            self.assertIsInstance(jupyter, JupyterLabC1WebSession)
            with self.assertRaises(C1WebRuntimeError):
                create_c1_web_session("2048", ROOT / "games/2048", tmp)


class JupyterLabC1PrimitiveTests(unittest.TestCase):
    def test_live_binding_records_target_without_restoring_content(self) -> None:
        spec = yaml.safe_load(
            (ROOT / "specs/jupyterlab/text-file-edit.yaml").read_text()
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = JupyterLabRuntimeAdapter(
                ROOT / "games/jupyterlab", Path(tmp), headless=False
            )
            runtime.server = Mock()
            runtime.server.poll.return_value = None
            runtime.page = _LivePage()
            runtime._c1_ui_observation = Mock(return_value={"lab_shell": True})  # type: ignore[method-assign]
            runtime._restore_target = Mock(  # type: ignore[method-assign]
                side_effect=AssertionError("live binding must not restore")
            )

            result = runtime.bind_c1_live_target(spec)

            self.assertTrue(result["ok"])
            self.assertFalse(result["actions_executed"])
            self.assertFalse(result["app_state_mutated"])
            self.assertEqual(runtime._c1_spec, spec)
            runtime._restore_target.assert_not_called()  # type: ignore[attr-defined]

    def test_restore_target_only_never_executes_spec_actions(self) -> None:
        spec = yaml.safe_load((ROOT / "specs/jupyterlab/text-file-edit.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            runtime = JupyterLabRuntimeAdapter(
                ROOT / "games/jupyterlab", Path(tmp), headless=False
            )
            runtime.server = Mock()
            runtime.server.poll.return_value = None
            runtime.reset_c1_state = Mock(return_value={"removed_paths": []})  # type: ignore[method-assign]
            runtime._restore_target = Mock(return_value=("restored", {"path": "notes/warptest.txt"}))  # type: ignore[method-assign]
            runtime._validate_target = Mock(return_value=("valid", {"path": "notes/warptest.txt"}))  # type: ignore[method-assign]
            runtime._screenshot_url = Mock(return_value="http://127.0.0.1/lab/tree/notes/warptest.txt")  # type: ignore[method-assign]
            runtime._open_c1_page = Mock(return_value=_LivePage())  # type: ignore[method-assign]
            runtime._c1_target_observation = Mock(  # type: ignore[method-assign]
                return_value={
                    "kind": "text_file",
                    "path": "notes/warptest.txt",
                    "content": "WarpTest initial\n",
                    "ui": {"lab_shell": True, "file_editor": True},
                }
            )
            runtime._run_action = Mock(side_effect=AssertionError("Phase B must not run"))  # type: ignore[method-assign]

            result = runtime.restore_c1_target(spec, ROOT / "specs/jupyterlab/text-file-edit.yaml")

            self.assertTrue(result["ok"])
            self.assertFalse(result["actions_executed"])
            self.assertEqual(result["artifacts"]["ignored_scripted_action_count"], 2)
            runtime._run_action.assert_not_called()  # type: ignore[attr-defined]

    def test_registered_state_transition_is_narrow_and_resynchronizes_editor(self) -> None:
        spec = yaml.safe_load((ROOT / "specs/jupyterlab/text-file-edit.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            runtime = JupyterLabRuntimeAdapter(
                ROOT / "games/jupyterlab", Path(tmp), headless=False
            )
            runtime._c1_spec = spec
            live = {"kind": "text_file", "content": "WarpTest edited\n", "ui": {"file_editor": True}}
            runtime._run_action = Mock(return_value=("done", {"path": "notes/warptest.txt"}))  # type: ignore[method-assign]
            runtime._screenshot_url = Mock(return_value="http://127.0.0.1/lab/tree/notes/warptest.txt")  # type: ignore[method-assign]
            runtime._open_c1_page = Mock(return_value=object())  # type: ignore[method-assign]
            runtime._c1_target_observation = Mock(return_value=live)  # type: ignore[method-assign]

            transition = runtime.apply_c1_state_transition(spec)

            self.assertTrue(transition["actions_executed"])
            self.assertEqual(transition["action_count"], 2)
            self.assertEqual(runtime._run_action.call_count, 2)  # type: ignore[attr-defined]
            runtime._open_c1_page.assert_called_once()  # type: ignore[attr-defined]

            unsafe = yaml.safe_load(yaml.safe_dump(spec))
            unsafe["actions"].append({"type": "wait", "ms": 1})
            with self.assertRaisesRegex(ValueError, "Unregistered JupyterLab"):
                runtime.apply_c1_state_transition(unsafe)

    def test_registered_deep_notebook_transitions_are_exact_and_safe(self) -> None:
        spec_paths = [
            ROOT / "specs/jupyterlab/c1-sales-analysis-pipeline.yaml",
            ROOT / "specs/jupyterlab/c1-debug-notebook-rerun.yaml",
            ROOT / "specs/jupyterlab/c1-reproducible-experiment-workspace.yaml",
        ]
        for spec_path in spec_paths:
            with self.subTest(spec=spec_path.name), tempfile.TemporaryDirectory() as tmp:
                spec = yaml.safe_load(spec_path.read_text())
                runtime = JupyterLabRuntimeAdapter(
                    ROOT / "games/jupyterlab", Path(tmp), headless=False
                )
                runtime._c1_spec = spec
                runtime._run_action = Mock(return_value=("done", {}))  # type: ignore[method-assign]
                runtime._screenshot_url = Mock(return_value="http://127.0.0.1/lab")  # type: ignore[method-assign]
                runtime._open_c1_page = Mock(return_value=object())  # type: ignore[method-assign]
                runtime._c1_target_observation = Mock(  # type: ignore[method-assign]
                    return_value={"kind": "notebook", "ui": {"notebook": True}}
                )

                transition = runtime.apply_c1_state_transition(spec)

                self.assertEqual(transition["action_count"], len(spec["actions"]))
                self.assertEqual(runtime._run_action.call_count, len(spec["actions"]))  # type: ignore[attr-defined]

                unsafe = yaml.safe_load(yaml.safe_dump(spec))
                run_action = next(
                    action
                    for action in unsafe["actions"]
                    if action["type"] == "jupyterlab_run_notebook_cell"
                )
                run_action["path"] = "c1/other.ipynb"
                with self.assertRaisesRegex(ValueError, "must remain"):
                    runtime.apply_c1_state_transition(unsafe)

    def test_registered_deep_notebook_transition_rejects_incomplete_workspace(self) -> None:
        spec = yaml.safe_load(
            (ROOT / "specs/jupyterlab/c1-sales-analysis-pipeline.yaml").read_text()
        )
        workspace_action = spec["actions"][-1]
        workspace_action["paths"] = ["c1/sales-analysis.ipynb"]
        with tempfile.TemporaryDirectory() as tmp:
            runtime = JupyterLabRuntimeAdapter(
                ROOT / "games/jupyterlab", Path(tmp), headless=False
            )
            runtime._c1_spec = spec
            with self.assertRaisesRegex(ValueError, "every edited text file"):
                runtime.apply_c1_state_transition(spec)

    def test_live_start_and_goal_grade_contents_without_running_actions(self) -> None:
        spec = yaml.safe_load((ROOT / "specs/jupyterlab/text-file-edit.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            runtime = JupyterLabRuntimeAdapter(
                ROOT / "games/jupyterlab", Path(tmp), headless=False
            )
            runtime._c1_spec = spec
            start = {
                "kind": "text_file",
                "path": "notes/warptest.txt",
                "content_type": "file",
                "content": "WarpTest initial\n",
                "ui": {"lab_shell": True, "file_editor": True},
            }
            goal = {
                **start,
                "content": "WarpTest edited\n",
            }
            runtime._c1_target_observation = Mock(side_effect=[start, goal])  # type: ignore[method-assign]
            runtime._run_assertion = Mock(return_value=("passed", {}))  # type: ignore[method-assign]
            runtime._run_action = Mock(side_effect=AssertionError("Phase B must not run"))  # type: ignore[method-assign]

            self.assertTrue(runtime.grade_c1_start(spec)["ok"])
            graded = runtime.grade_c1_goal(spec)

            self.assertTrue(graded["ok"])
            self.assertTrue(graded["observation"]["changed_from_start"])
            runtime._run_action.assert_not_called()  # type: ignore[attr-defined]

    def test_notebook_start_treats_one_terminal_newline_as_serialization_equivalent(
        self,
    ) -> None:
        spec = yaml.safe_load(
            (ROOT / "specs/jupyterlab/notebook-cell-output.yaml").read_text()
        )
        observation = {
            "kind": "notebook",
            "path": "notebooks/cell-output.ipynb",
            "content_type": "notebook",
            "cells": [
                {
                    "cell_type": "code",
                    "source": "print('warptest:42')",
                    "execution_count": None,
                    "outputs": [],
                }
            ],
            "output_text": [],
            "ui": {"lab_shell": True, "notebook": True},
        }
        with tempfile.TemporaryDirectory() as tmp:
            runtime = JupyterLabRuntimeAdapter(
                ROOT / "games/jupyterlab", Path(tmp), headless=False
            )
            runtime._c1_spec = spec
            runtime._c1_target_observation = Mock(return_value=observation)  # type: ignore[method-assign]

            assert runtime.grade_c1_start(spec)["ok"] is True

            wrong = copy.deepcopy(spec)
            wrong["target"]["cells"][0]["source"] = "print('warptest:43')\n"
            assert runtime.grade_c1_start(wrong)["ok"] is False

    def test_missing_agent_built_start_is_a_live_counted_validation_failure(self) -> None:
        spec = yaml.safe_load(
            (ROOT / "specs/jupyterlab/text-file-edit.yaml").read_text()
        )
        with tempfile.TemporaryDirectory() as tmp:
            runtime = JupyterLabRuntimeAdapter(
                ROOT / "games/jupyterlab", Path(tmp), headless=False
            )
            runtime._c1_spec = spec
            runtime._c1_target_observation = Mock(  # type: ignore[method-assign]
                side_effect=RuntimeError("Contents API 404")
            )
            runtime._c1_ui_observation = Mock(  # type: ignore[method-assign]
                return_value={"lab_shell": True, "file_editor": False}
            )

            grade = runtime.grade_c1_start(spec)

            self.assertFalse(grade["ok"])
            self.assertEqual(grade["status"], "validate_failed")
            self.assertEqual(grade["observation"]["path"], "notes/warptest.txt")
            self.assertTrue(grade["observation"]["ui"]["lab_shell"])


if __name__ == "__main__":
    unittest.main()
