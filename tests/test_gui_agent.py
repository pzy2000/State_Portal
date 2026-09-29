from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import yaml

from warptest import c1_synthesis_cache, gui_agent
from warptest.gui_agent import GuiAgentClient, _run_agent_task_synthesized
from warptest.task_schema import PublicIssue


class GuiAgentClientTests(unittest.TestCase):
    def test_openra_gameplay_tasks_use_native_openra_agent_runtime(self) -> None:
        task = SimpleNamespace(
            id="openra-deep-chain",
            benchmark="OpenRA",
            issue=SimpleNamespace(),
            oracle_spec="openra-deep-chain.yaml",
        )
        expected = {"status": "sentinel"}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with (
                patch.object(gui_agent, "resolve_task_repo", return_value=root),
                patch.object(gui_agent, "runtime_lease", return_value=nullcontext()),
                patch.object(
                    gui_agent,
                    "load_spec",
                    return_value={"target": {"kind": "openra_map"}},
                ),
                patch.object(
                    gui_agent,
                    "_run_openra_settings_agent_task",
                    return_value=expected,
                ) as native_run,
                patch.object(gui_agent, "_run_agent_task_synthesized") as generic_run,
            ):
                actual = gui_agent._run_agent_task(
                    root,
                    task,
                    model="model",
                    base_url="https://example.invalid/v1",
                    api_key="unused",
                    api_key_env="TEST_KEY",
                    synth_model="model",
                    max_explore_steps=3,
                    temperature=0.0,
                    out_dir=root / "report",
                    max_steps=4,
                    headless=False,
                    start_mode="synthesized",
                    policy_kind="agent_s",
                    seed=0,
                )

        self.assertIs(actual, expected)
        native_run.assert_called_once()
        generic_run.assert_not_called()

    def test_empty_synthesis_response_is_a_fair_failure_not_infrastructure(self) -> None:
        checkpoint = SimpleNamespace(
            used_public_scaffold=True,
            model_generated_spec=None,
            payload={
                "model_fallback_reason": (
                    "LLM API call failed during source exploration: "
                    "LLM returned empty content after retries"
                )
            },
            runtime_validation={},
        )
        agent = SimpleNamespace(synthesize=MagicMock(return_value=checkpoint))
        task = SimpleNamespace(
            id="task-a",
            benchmark="2048",
            issue=SimpleNamespace(),
        )
        provider = SimpleNamespace(
            base_url="https://example.invalid/v1",
            api_key_env="TEST_KEY",
            extra_headers=None,
        )
        with tempfile.TemporaryDirectory() as td, patch.object(
            gui_agent, "build_synthesis_agent", return_value=agent
        ):
            payload = gui_agent._produce_self_synthesis(
                task,
                base=Path(td),
                seed=0,
                clean_synth_model="kimi-k3",
                synth_provider=provider,
                temperature=1.0,
                max_explore_steps=12,
                artifact_dir=Path(td) / "artifacts",
                policy_kind="agent_s",
                cache_key={},
                cache_key_fingerprint="fingerprint",
            )

        self.assertEqual(payload["outcome"]["status"], "fair_failure")
        self.assertEqual(payload["outcome"]["synth_status"], "no_model_spec")

    def test_browser_calibration_retries_transient_screenshot_failure(self) -> None:
        page = MagicMock()
        page.screenshot.side_effect = [
            RuntimeError("Protocol error: Unable to capture screenshot"),
            None,
        ]
        page.evaluate.side_effect = [
            {
                "width": 1280,
                "height": 800,
                "dpr": 1.0,
                "box": {"x": 400, "y": 300, "width": 120, "height": 80},
            },
            1,
        ]
        runtime = SimpleNamespace(
            context=SimpleNamespace(new_page=MagicMock(return_value=page))
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = gui_agent.calibrate_browser_coordinates(runtime, Path(tmp))

        self.assertTrue(result["passed"])
        self.assertEqual(result["capture_attempts"], 2)
        self.assertEqual(len(result["capture_errors"]), 1)
        page.wait_for_timeout.assert_called_once_with(250)
        page.close.assert_called_once_with()

    def test_browser_calibration_exhaustion_is_retryable_infrastructure(self) -> None:
        page = MagicMock()
        page.screenshot.side_effect = RuntimeError(
            "Protocol error: Unable to capture screenshot"
        )
        runtime = SimpleNamespace(
            context=SimpleNamespace(new_page=MagicMock(return_value=page))
        )

        with tempfile.TemporaryDirectory() as tmp:
            calibration = gui_agent.calibrate_browser_coordinates(
                runtime, Path(tmp)
            )
        result: dict[str, object] = {"policy_phase_started": False}
        gui_agent._record_browser_calibration_failure(result, calibration)

        self.assertFalse(calibration["passed"])
        self.assertEqual(calibration["failure_kind"], "screenshot_capture")
        self.assertEqual(calibration["capture_attempts"], 3)
        self.assertTrue(result["infrastructure_error"])
        self.assertTrue(result["incomplete"])
        self.assertTrue(gui_agent._gui_trial_retryable_incomplete(result))

    def test_typed_desktop_runtime_failure_is_retryable_before_policy(self) -> None:
        result: dict[str, object] = {"policy_phase_started": False}

        gui_agent._record_desktop_runtime_failure(
            result,
            "LibreOffice",
            gui_agent.C1DesktopRuntimeError("VCL background bridge is unavailable"),
        )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["infrastructure_error"])
        self.assertTrue(result["incomplete"])
        self.assertTrue(gui_agent._gui_trial_retryable_incomplete(result))
        result["retryable_incomplete"] = True
        self.assertEqual(gui_agent._gui_retry_metadata(result)[0], "infra")

    def test_playwright_dispatch_normalizes_agent_s_arrow_keys(self) -> None:
        keyboard = MagicMock()
        runtime = SimpleNamespace(
            page=SimpleNamespace(
                keyboard=keyboard,
                mouse=MagicMock(),
                wait_for_timeout=MagicMock(),
            )
        )

        gui_agent._apply_action(
            runtime,
            gui_agent.AgentAction(kind="key", key="Left", modifiers=("cmd",)),
        )

        keyboard.press.assert_called_once_with("Meta+ArrowLeft")

    def test_provider_http_400_is_an_infrastructure_error(self) -> None:
        error = RuntimeError("BadRequestError: image content is unsupported")
        error.status_code = 400
        self.assertTrue(gui_agent._is_infrastructure_error(error))

    def test_background_input_mapping_records_blender_y_flip(self) -> None:
        mapping = gui_agent._background_input_mapping(
            [
                gui_agent.AgentAction(kind="click", x=12, y=34),
                gui_agent.AgentAction(kind="drag", x=1, y=2, x2=100, y2=200),
            ],
            width=1280,
            height=800,
            input_backend="blender_window_event_simulate_v1",
        )

        self.assertEqual(mapping["max_serialization_error_pixels"], 0)
        self.assertEqual(mapping["checks"][0]["serialized_frame_point"], [12, 34])
        self.assertEqual(mapping["checks"][0]["backend_event_point"], [12, 765])
        self.assertEqual(mapping["checks"][-1]["backend_event_point"], [100, 599])

    def test_client_honors_timeout_and_retry_env(self) -> None:
        fake_openai = MagicMock()
        with (
            patch.dict(
                os.environ,
                {
                    "WARPTEST_GUI_AGENT_TIMEOUT": "24.5",
                    "WARPTEST_GUI_AGENT_MAX_RETRIES": "0",
                },
                clear=False,
            ),
            patch("openai.OpenAI", fake_openai),
        ):
            GuiAgentClient(
                model="judge/model",
                base_url="https://judge.example/v1",
                api_key="not-secret",
            )

        _, kwargs = fake_openai.call_args
        self.assertEqual(kwargs["timeout"], 24.5)
        self.assertEqual(kwargs["max_retries"], 0)
        self.assertEqual(kwargs["base_url"], "https://judge.example/v1")

    def test_kimi_request_profile_is_applied_and_attested(self) -> None:
        fake_openai = MagicMock()
        fake_client = MagicMock()
        fake_openai.return_value = fake_client
        with (
            patch.dict(
                os.environ,
                {"WARPTEST_C1_MODEL_REQUEST_PROFILES": '{"kimi-k2.6":"thinking_disabled"}'},
                clear=False,
            ),
            patch("openai.OpenAI", fake_openai),
        ):
            client = GuiAgentClient(
                model="kimi-k2.6",
                base_url="https://judge.example/v1",
                api_key="not-secret",
            )
            client._create([{"role": "user", "content": "act"}], max_tokens=8192)
            metadata = gui_agent._policy_runtime_metadata(
                client, policy_kind="gui_agent"
            )

        _, request = fake_client.chat.completions.create.call_args
        self.assertEqual(request["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(metadata["model_request_profile"]["profile"], "thinking_disabled")
        self.assertEqual(metadata["reasoning_retry_max_tokens"], 16384)

    def test_synthesis_cache_key_binds_model_request_profile(self) -> None:
        task = _fake_task()
        common = {
            "task": task,
            "base": Path("/tmp/repo"),
            "seed": 0,
            "synth_model": "kimi-k2.6",
            "provider_name": "relay",
            "provider_base_url": "https://example.invalid/v1",
            "temperature": 1.0,
            "max_explore_steps": 12,
        }
        with patch.dict(
            os.environ,
            {"WARPTEST_C1_MODEL_REQUEST_PROFILES": '{"kimi-k2.6":"provider_default"}'},
            clear=False,
        ):
            default = gui_agent._self_synthesis_cache_key(**common)
        with patch.dict(
            os.environ,
            {"WARPTEST_C1_MODEL_REQUEST_PROFILES": '{"kimi-k2.6":"thinking_disabled"}'},
            clear=False,
        ):
            disabled = gui_agent._self_synthesis_cache_key(**common)
        self.assertNotEqual(
            default["model_request_profile"]["fingerprint"],
            disabled["model_request_profile"]["fingerprint"],
        )

    def test_synthesis_cache_key_binds_complete_public_issue(self) -> None:
        common = {
            "base": Path("/tmp/repo"),
            "seed": 0,
            "synth_model": "kimi-k2.6",
            "provider_name": "relay",
            "provider_base_url": "https://example.invalid/v1",
            "temperature": 1.0,
            "max_explore_steps": 12,
        }
        with patch.object(
            gui_agent,
            "_self_synthesis_cache_identity_root",
            return_value="/tmp/repo",
        ):
            first = gui_agent._self_synthesis_cache_key(
                task=_fake_task("first complete public issue"),
                **common,
            )
            second = gui_agent._self_synthesis_cache_key(
                task=_fake_task("second complete public issue"),
                **common,
            )
        self.assertEqual(
            first["schema_version"],
            c1_synthesis_cache.SELF_SYNTHESIS_CACHE_SCHEMA_VERSION,
        )
        # The consumer is independent of the consuming policy so the synthesized
        # GUI arm and the state arm restore the same self-synthesized start.
        self.assertEqual(
            first["consumer"], c1_synthesis_cache.C1_SYNTHESIS_CONSUMER
        )
        self.assertEqual(
            first["task"]["public_issue"]["issue"],
            "first complete public issue",
        )
        self.assertNotIn("goal_description", first["task"])
        self.assertNotEqual(first["public_task_hash"], second["public_task_hash"])

    def test_synthesis_cache_key_uses_git_common_worktree_root(self) -> None:
        common = {
            "task": _fake_task(),
            "seed": 0,
            "synth_model": "glm-5.2",
            "provider_name": "relay",
            "provider_base_url": "https://example.invalid/v1",
            "temperature": 1.0,
            "max_explore_steps": 12,
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            main_root = root / "main-repo"
            linked_root = root / "linked-repo"
            for checkout in (main_root, linked_root):
                (checkout / "games" / "2048").mkdir(parents=True)
            common_dir = main_root / ".git"
            task_common_dir = main_root / "games" / "2048" / ".git"

            def git_output(repo: Path, *arguments: str) -> str:
                if arguments[-1] == "--git-common-dir":
                    return str(
                        task_common_dir if "games/2048" in str(repo) else common_dir
                    )
                if arguments[-1] == "HEAD":
                    return "same-head"
                if arguments[0] == "status":
                    return ""
                self.fail(f"unexpected git probe: {arguments}")

            with (
                patch.dict(
                    os.environ,
                    {
                        gui_agent.SELF_SYNTHESIS_CACHE_IDENTITY_ROOT_ENV: str(
                            main_root
                        )
                    },
                    clear=False,
                ),
                patch.object(c1_synthesis_cache, "git_cache_output", side_effect=git_output),
            ):
                linked = gui_agent._self_synthesis_cache_key(
                    base=linked_root, **common
                )

        self.assertEqual(linked["root"], str(main_root.resolve()))

    def test_synthesis_cache_key_rejects_alias_with_different_parent_head(self) -> None:
        common = {
            "task": _fake_task(),
            "seed": 0,
            "synth_model": "glm-5.2",
            "provider_name": "relay",
            "provider_base_url": "https://example.invalid/v1",
            "temperature": 1.0,
            "max_explore_steps": 12,
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            main_root = root / "main-repo"
            linked_root = root / "linked-repo"
            for checkout in (main_root, linked_root):
                (checkout / "games" / "2048").mkdir(parents=True)

            def git_output(repo: Path, *arguments: str) -> str:
                if arguments[-1] == "--git-common-dir":
                    return str(main_root / ".git")
                if arguments[-1] == "HEAD":
                    return (
                        "main-head"
                        if repo.resolve() == main_root.resolve()
                        else "linked-head"
                    )
                return ""

            with (
                patch.dict(
                    os.environ,
                    {
                        gui_agent.SELF_SYNTHESIS_CACHE_IDENTITY_ROOT_ENV: str(
                            main_root
                        )
                    },
                    clear=False,
                ),
                patch.object(c1_synthesis_cache, "git_cache_output", side_effect=git_output),
                self.assertRaisesRegex(ValueError, "parent HEAD differs"),
            ):
                gui_agent._self_synthesis_cache_key(base=linked_root, **common)

    def test_synthesis_cache_key_rejects_alias_from_different_repository(self) -> None:
        common = {
            "task": _fake_task(),
            "seed": 0,
            "synth_model": "glm-5.2",
            "provider_name": "relay",
            "provider_base_url": "https://example.invalid/v1",
            "temperature": 1.0,
            "max_explore_steps": 12,
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            alias_root = root / "main-repo"
            checkout_root = root / "different-repo"
            for checkout in (alias_root, checkout_root):
                (checkout / "games" / "2048").mkdir(parents=True)

            def git_output(repo: Path, *arguments: str) -> str:
                if arguments[-1] == "--git-common-dir":
                    return str(repo.resolve() / ".git")
                return "same-head"

            with (
                patch.dict(
                    os.environ,
                    {
                        gui_agent.SELF_SYNTHESIS_CACHE_IDENTITY_ROOT_ENV: str(
                            alias_root
                        )
                    },
                    clear=False,
                ),
                patch.object(c1_synthesis_cache, "git_cache_output", side_effect=git_output),
                self.assertRaisesRegex(ValueError, "not in the same Git repository"),
            ):
                gui_agent._self_synthesis_cache_key(base=checkout_root, **common)

    def test_synthesis_cache_key_rejects_dirty_task_repository_alias(self) -> None:
        common = {
            "task": _fake_task(),
            "seed": 0,
            "synth_model": "glm-5.2",
            "provider_name": "relay",
            "provider_base_url": "https://example.invalid/v1",
            "temperature": 1.0,
            "max_explore_steps": 12,
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            main_root = root / "main-repo"
            linked_root = root / "linked-repo"
            for checkout in (main_root, linked_root):
                (checkout / "games" / "2048").mkdir(parents=True)

            def git_output(repo: Path, *arguments: str) -> str:
                if arguments[-1] == "--git-common-dir":
                    return str(
                        main_root / "games" / "2048" / ".git"
                        if "games/2048" in str(repo)
                        else main_root / ".git"
                    )
                if arguments[-1] == "HEAD":
                    return "same-head"
                if arguments[0] == "status":
                    return (
                        " M changed.lua"
                        if repo.resolve()
                        == (linked_root / "games" / "2048").resolve()
                        else ""
                    )
                return ""

            with (
                patch.dict(
                    os.environ,
                    {
                        gui_agent.SELF_SYNTHESIS_CACHE_IDENTITY_ROOT_ENV: str(
                            main_root
                        )
                    },
                    clear=False,
                ),
                patch.object(c1_synthesis_cache, "git_cache_output", side_effect=git_output),
                self.assertRaisesRegex(ValueError, "task repository is dirty"),
            ):
                gui_agent._self_synthesis_cache_key(base=linked_root, **common)

    def test_git_cache_probe_ignores_inherited_git_context(self) -> None:
        completed = MagicMock(stdout="ok\n")
        with (
            patch.dict(
                os.environ,
                {name: "contaminated" for name in c1_synthesis_cache._GIT_CONTEXT_ENV_VARS},
                clear=False,
            ),
            patch.object(c1_synthesis_cache.subprocess, "run", return_value=completed) as run,
        ):
            self.assertEqual(
                c1_synthesis_cache.git_cache_output(Path("/tmp"), "status"), "ok"
            )

        passed_env = run.call_args.kwargs["env"]
        self.assertTrue(
            all(name not in passed_env for name in c1_synthesis_cache._GIT_CONTEXT_ENV_VARS)
        )


class StartGradingRuntimeHoldTests(unittest.TestCase):
    def test_browser_runtime_is_frozen_and_released_around_start_grading(self) -> None:
        runtime = object.__new__(gui_agent.BrowserRuntimeAdapter)
        runtime.page = MagicMock()
        runtime.context = MagicMock()
        cdp = MagicMock()
        runtime.context.new_cdp_session.return_value = cdp

        with gui_agent._hold_runtime_state_for_start_grading(runtime) as metadata:
            self.assertTrue(metadata["held"])
            self.assertFalse(metadata["released"])

        self.assertTrue(metadata["released"])
        self.assertEqual(
            cdp.send.call_args_list,
            [
                call(
                    "Emulation.setScriptExecutionDisabled", {"value": True}
                ),
                call(
                    "Emulation.setScriptExecutionDisabled", {"value": False}
                ),
            ],
        )
        cdp.detach.assert_called_once_with()

    def test_luanti_runtime_is_suspended_and_resumed_around_start_grading(self) -> None:
        runtime = object.__new__(gui_agent.LuantiGuiRuntime)
        process = MagicMock(pid=4242)
        process.poll.return_value = None
        runtime.session = SimpleNamespace(_proc=process)

        with patch.object(gui_agent.os, "kill") as kill:
            with gui_agent._hold_runtime_state_for_start_grading(runtime) as metadata:
                self.assertTrue(metadata["held"])

        self.assertTrue(metadata["released"])
        self.assertEqual(
            kill.call_args_list,
            [
                call(4242, gui_agent.signal.SIGSTOP),
                call(4242, gui_agent.signal.SIGCONT),
            ],
        )

    def test_runtime_hold_failure_records_auditable_error_and_releases_cdp(self) -> None:
        runtime = object.__new__(gui_agent.BrowserRuntimeAdapter)
        runtime.page = MagicMock()
        runtime.context = MagicMock()
        cdp = MagicMock()
        runtime.context.new_cdp_session.return_value = cdp

        def send(method, params):
            if params["value"] is True:
                raise RuntimeError("freeze rejected")

        cdp.send.side_effect = send
        metadata: dict[str, object] = {}
        with self.assertRaisesRegex(RuntimeError, "failed to hold runtime"):
            with gui_agent._hold_runtime_state_for_start_grading(
                runtime, metadata=metadata
            ):
                self.fail("grading must not run without a stable start state")

        self.assertIn("freeze rejected", metadata["hold_error"])
        self.assertFalse(metadata["held"])
        self.assertTrue(metadata["released"])
        cdp.detach.assert_called_once_with()


class PhaseBoundaryCaptureTests(unittest.TestCase):
    def test_background_policy_failure_keeps_captured_frame_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            step = root / "step.png"
            final = root / "final.png"
            step.write_bytes(b"step")
            final.write_bytes(b"final")

            error = RuntimeError("RateLimitError: provider busy")
            error.status_code = 429

            class Client:
                metadata = {
                    "execution_backend": "background_pixel_v1",
                    "coordinate_space": "frame_pixels_top_left_v1",
                    "raw_screenshot_observation": True,
                    "action_execution_owner": "parent_runtime",
                    "foreground_required": False,
                }

                def next_action(self, **_kwargs):
                    raise error

            runtime = SimpleNamespace(
                session_id="background-session",
                last_policy_frame_id=7,
                capture_backend="playwright_page_screenshot_v1",
                input_backend="playwright_page_input_v1",
                pid=4321,
            )
            with (
                patch.object(
                    gui_agent,
                    "_capture_runtime",
                    MagicMock(side_effect=[str(step), str(final)]),
                ),
                patch.object(
                    gui_agent, "_runtime_dimensions", return_value=(1280, 800)
                ),
            ):
                phase = gui_agent._run_phase(
                    runtime=runtime,
                    client=Client(),
                    instruction="reach the goal",
                    phase_dir=root / "phase",
                    max_steps=1,
                )

        recorded = phase.actions[0]
        self.assertTrue(phase.incomplete)
        self.assertTrue(recorded["infrastructure_error"])
        self.assertEqual(recorded["execution_backend"], "background_pixel_v1")
        self.assertEqual(recorded["session_id"], "background-session")
        self.assertEqual(recorded["frame_id"], 7)
        self.assertEqual(
            recorded["capture_backend"], "playwright_page_screenshot_v1"
        )
        self.assertEqual(recorded["input_backend"], "playwright_page_input_v1")
        self.assertEqual(recorded["target_pid"], 4321)
        self.assertEqual(recorded["coordinate_space"], "frame_pixels_top_left_v1")
        self.assertNotIn("execution_receipt", recorded)
        self.assertNotIn("input_mapping", recorded)

    def test_provider_capacity_policy_failure_is_a_normal_treatment_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            step = root / "step.png"
            final = root / "final.png"
            step.write_bytes(b"step")
            final.write_bytes(b"final")

            error = RuntimeError(
                "TPM limit exceeded: 0+1121899 > 1000000 for model model-a"
            )
            error.status_code = 429

            class Client:
                metadata = {
                    "execution_backend": "background_pixel_v1",
                    "coordinate_space": "frame_pixels_top_left_v1",
                    "raw_screenshot_observation": True,
                    "action_execution_owner": "parent_runtime",
                    "foreground_required": False,
                }

                def next_action(self, **_kwargs):
                    raise error

            runtime = SimpleNamespace(
                session_id="background-session",
                last_policy_frame_id=1,
                capture_backend="playwright_page_screenshot_v1",
                input_backend="playwright_page_input_v1",
                pid=4321,
            )
            with (
                patch.object(
                    gui_agent,
                    "_capture_runtime",
                    MagicMock(side_effect=[str(step), str(final)]),
                ),
                patch.object(
                    gui_agent, "_runtime_dimensions", return_value=(1280, 800)
                ),
            ):
                phase = gui_agent._run_phase(
                    runtime=runtime,
                    client=Client(),
                    instruction="reach the goal",
                    phase_dir=root / "phase",
                    max_steps=1,
                )

        self.assertFalse(phase.incomplete)
        self.assertFalse(phase.actions[0]["infrastructure_error"])

    def test_known_usage_nonretryable_incomplete_has_legal_terminal_status(self) -> None:
        actual_cost = {
            "usage_complete": True,
            "incomplete_calls": 0,
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }
        attempts, ledger_hash = gui_agent._bind_attempt_ledger(
            [
                {
                    "attempt_status": "nonretryable_incomplete",
                    "retry_category": None,
                    "retry_reason": None,
                    "task_actual_cost": actual_cost,
                    "synthesis_accounting": None,
                }
            ],
            task_id="task-a",
        )
        result = {
            "incomplete": True,
            "retryable_incomplete": False,
            "actual_cost": actual_cost,
            "attempt_costs": attempts,
            "attempt_count": 1,
            "attempt_ledger_hash": ledger_hash,
        }

        validated = gui_agent._validated_attempt_ledger(
            result, task_id="task-a", incomplete=True
        )

        self.assertEqual(validated[0]["attempt_status"], "nonretryable_incomplete")

    def test_done_captures_a_fresh_post_policy_boundary(self) -> None:
        """Dynamic games may advance while the remote policy decides to stop."""

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            step = root / "step.png"
            final = root / "final.png"
            step.write_bytes(b"step")
            final.write_bytes(b"final")

            class Client:
                def next_action(self, **_kwargs):
                    return gui_agent.AgentAction(kind="done")

            capture = MagicMock(side_effect=[str(step), str(final)])
            with (
                patch.object(gui_agent, "_capture_runtime", capture),
                patch.object(
                    gui_agent, "_runtime_dimensions", return_value=(1280, 800)
                ),
            ):
                phase = gui_agent._run_phase(
                    runtime=object(),
                    client=Client(),
                    instruction="stop at the goal",
                    phase_dir=root / "phase",
                    max_steps=1,
                )

        self.assertEqual(capture.call_count, 2)
        self.assertEqual(phase.screenshot, str(final))

    def test_failed_fresh_terminal_capture_keeps_old_frame_diagnostic_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            step = root / "step.png"
            step.write_bytes(b"step")

            class Client:
                def next_action(self, **_kwargs):
                    return gui_agent.AgentAction(kind="done")

            capture = MagicMock(side_effect=[str(step), None, None, None])
            with (
                patch.object(gui_agent, "_capture_runtime", capture),
                patch.object(
                    gui_agent, "_runtime_dimensions", return_value=(1280, 800)
                ),
            ):
                phase = gui_agent._run_phase(
                    runtime=object(),
                    client=Client(),
                    instruction="stop at the goal",
                    phase_dir=root / "phase",
                    max_steps=1,
                )

        self.assertEqual(capture.call_count, 4)
        self.assertNotIn("last_shot", capture.call_args_list[1].kwargs)
        self.assertEqual(phase.screenshot, str(step))
        self.assertTrue(phase.incomplete)
        self.assertEqual(phase.actions[-1]["error"], "final_screenshot_capture_failed")
        self.assertEqual(phase.actions[-1]["diagnostic_screenshot"], str(step))
        self.assertEqual(phase.actions[-1]["capture_attempts"], 3)
        self.assertEqual(len(phase.actions[-1]["capture_errors"]), 3)

    def test_intermediate_capture_exhaustion_never_calls_policy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            final = root / "final.png"
            final.write_bytes(b"final")
            client = MagicMock()
            capture = MagicMock(side_effect=[None, None, None, str(final)])
            with (
                patch.object(gui_agent, "_capture_runtime", capture),
                patch.object(
                    gui_agent, "_runtime_dimensions", return_value=(1280, 800)
                ),
            ):
                phase = gui_agent._run_phase(
                    runtime=object(),
                    client=client,
                    instruction="do not use a stale frame",
                    phase_dir=root / "phase",
                    max_steps=1,
                )

        client.next_action.assert_not_called()
        self.assertEqual(capture.call_count, 4)
        self.assertTrue(phase.incomplete)
        self.assertEqual(phase.screenshot, str(final))
        self.assertEqual(phase.actions[0]["error"], "screenshot_capture_failed")
        self.assertTrue(phase.actions[0]["infrastructure_error"])
        self.assertIsNone(phase.actions[0]["diagnostic_screenshot"])
        self.assertEqual(phase.actions[0]["capture_attempts"], 3)
        self.assertEqual(len(phase.actions[0]["capture_errors"]), 3)

    def test_intermediate_retry_success_is_fresh_and_retry_time_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            step = root / "step.png"
            final = root / "final.png"
            step.write_bytes(b"step")
            final.write_bytes(b"final")

            class Client:
                calls: list[dict] = []

                def next_action(self, **kwargs):
                    self.calls.append(kwargs)
                    return gui_agent.AgentAction(kind="done")

            client = Client()
            capture = MagicMock(
                side_effect=[
                    RuntimeError("capture failed with Bearer very-secret-value"),
                    str(step),
                    str(final),
                ]
            )
            # Binary fractions avoid making the integer-millisecond assertion
            # depend on floating-point truncation at a decimal boundary.
            ticks = iter((0.0, 0.125, 0.250, 0.375, 0.500, 0.625))
            with (
                patch.object(gui_agent, "_capture_runtime", capture),
                patch.object(gui_agent.time, "perf_counter", side_effect=ticks),
                patch.object(
                    gui_agent, "_runtime_dimensions", return_value=(1280, 800)
                ),
            ):
                phase = gui_agent._run_phase(
                    runtime=object(),
                    client=client,
                    instruction="use the recovered fresh frame",
                    phase_dir=root / "phase",
                    max_steps=1,
                )

        self.assertEqual(capture.call_count, 3)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["screenshot"], step)
        self.assertFalse(phase.incomplete)
        self.assertEqual(phase.screenshot, str(final))
        self.assertEqual(phase.actions[0]["capture_attempts"], 2)
        errors = phase.actions[0]["capture_errors"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Bearer <redacted>", errors[0]["error"])
        self.assertNotIn("very-secret-value", errors[0]["error"])
        self.assertAlmostEqual(phase.grader_ms, 250.0)
        self.assertEqual(phase.elapsed_ms, 375)

    def test_terminal_capture_retry_success_remains_complete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            step = root / "step.png"
            final = root / "final.png"
            step.write_bytes(b"step")
            final.write_bytes(b"final")

            class Client:
                def next_action(self, **_kwargs):
                    return gui_agent.AgentAction(kind="done")

            capture = MagicMock(side_effect=[str(step), None, str(final)])
            with (
                patch.object(gui_agent, "_capture_runtime", capture),
                patch.object(
                    gui_agent, "_runtime_dimensions", return_value=(1280, 800)
                ),
            ):
                phase = gui_agent._run_phase(
                    runtime=object(),
                    client=Client(),
                    instruction="stop at the goal",
                    phase_dir=root / "phase",
                    max_steps=1,
                )

        self.assertEqual(capture.call_count, 3)
        self.assertFalse(phase.incomplete)
        self.assertEqual(phase.screenshot, str(final))
        terminal = phase.actions[-1]
        self.assertEqual(terminal["capture"], "terminal")
        self.assertEqual(terminal["capture_status"], "recovered")
        self.assertEqual(terminal["capture_attempts"], 2)
        self.assertEqual(len(terminal["capture_errors"]), 1)


class BuildPolicyClientNativeTests(unittest.TestCase):
    """Formal Agent-S clients start in fixed page pixels, not desktop pixels."""

    def _build(self, **overrides):
        kwargs = dict(
            policy_kind="agent_s",
            model="glm-5.2@relay",
            base_url="https://example.invalid/v1",
            api_key_env="RELAY_API_KEY",
            temperature=0.2,
            grounder_model="claude-sonnet-5@relay",
            provider_timeout_s=None,
            root=Path("/tmp/warptest-root"),
        )
        kwargs.update(overrides)
        with patch.dict(os.environ, {"RELAY_API_KEY": "not-secret"}, clear=False):
            return gui_agent._build_policy_client(**kwargs)

    def test_agent_s_uses_background_frame_size_without_probing_desktop(self) -> None:
        with patch.object(
            gui_agent.native_window, "screen_size", return_value=(2560, 1440)
        ) as screen_size:
            client = self._build()
        screen_size.assert_not_called()
        self.assertEqual(client.config.native_screen_width, 1280)
        self.assertEqual(client.config.native_screen_height, 800)
        self.assertEqual(client.config.execution_backend, "background_pixel_v1")


class NativeRuntimeModeCaptureTests(unittest.TestCase):
    """``_capture_runtime``/``_runtime_dimensions`` when ``native_exec_enabled``."""

    def test_browser_runtime_captures_via_native_window_when_native(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "shot.png"
            with patch.object(
                gui_agent.native_window, "capture_full_screen", return_value=target
            ) as capture:
                result = gui_agent._capture_runtime(
                    object(), target, native_exec_enabled=True
                )
        capture.assert_called_once_with(target)
        self.assertEqual(result, str(target))

    def test_luanti_runtime_uses_engine_policy_frame_capture(self) -> None:
        """Luanti captures through Irrlicht even if legacy metadata says native."""

        luanti = object.__new__(gui_agent.LuantiGuiRuntime)
        luanti.capture_policy_frame = MagicMock(return_value="/tmp/luanti-shot.png")
        result = gui_agent._capture_runtime(
            luanti, Path("/tmp/step.png"), native_exec_enabled=True
        )
        luanti.capture_policy_frame.assert_called_once_with(Path("/tmp/step.png"))
        self.assertEqual(result, "/tmp/luanti-shot.png")

    def test_runtime_dimensions_uses_real_screen_size_when_native(self) -> None:
        with patch.object(
            gui_agent.native_window, "screen_size", return_value=(3440, 1440)
        ):
            self.assertEqual(
                gui_agent._runtime_dimensions(object(), native_exec_enabled=True),
                (3440, 1440),
            )


class NativeRuntimeModePhaseTests(unittest.TestCase):
    """``policy_runtime_metadata.native_exec_enabled`` reroutes ``_run_phase``.

    The native worker child already executed pyautogui against the real
    desktop before ``next_action`` returns, so the parent must skip
    ``_apply_action``/the transition probe and must not spend an extra LLM
    call on the fixed visual observer.
    """

    def test_native_step_skips_apply_and_observer_but_still_focuses_and_captures(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            step = root / "step.png"
            final = root / "final.png"
            step.write_bytes(b"step")
            final.write_bytes(b"final")

            class Client:
                metadata = {"native_exec_enabled": True}
                calls: list[dict] = []

                def next_action(self, **kwargs):
                    self.calls.append(kwargs)
                    return gui_agent.AgentAction(kind="native_exec", raw="agent.click(1,2)")

            client = Client()
            observer = MagicMock()
            capture = MagicMock(side_effect=[str(step), str(final)])
            focus = MagicMock()
            apply_action = MagicMock()
            probe = MagicMock()
            with (
                patch.object(gui_agent, "_capture_runtime", capture),
                patch.object(gui_agent.native_window, "focus_runtime", focus),
                patch.object(gui_agent, "_apply_action", apply_action),
                patch.object(gui_agent, "record_public_transition_action", probe),
                patch.object(
                    gui_agent, "_runtime_dimensions", return_value=(1920, 1080)
                ),
            ):
                phase = gui_agent._run_phase(
                    runtime=object(),
                    client=client,
                    instruction="reach the goal",
                    phase_dir=root / "phase",
                    max_steps=1,
                    observer=observer,
                )

        focus.assert_called_once()
        observer.observe.assert_not_called()
        apply_action.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(client.calls[0]["observation"], "native")
        self.assertEqual(phase.actions[0]["kind"], "native_exec")
        self.assertFalse(phase.incomplete)

    def test_native_step_merges_prompt_plan_and_verb_from_prediction_info(self) -> None:
        """Native Agent-S enrichment (plan/plan_code/reflection/verb/prompt_text)
        travels from ``AgentSPrediction.info`` into the recorded phase action,
        via ``_native_info_fields``; a gui_agent-policy prediction (no ``.info``
        at all) leaves the recorded action untouched (see the sibling test
        above, whose bare ``AgentAction`` has no ``info`` and adds no keys)."""

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            step = root / "step.png"
            final = root / "final.png"
            step.write_bytes(b"step")
            final.write_bytes(b"final")

            class Prediction:
                action = gui_agent.AgentAction(
                    kind="click", x=801, y=442, raw="pyautogui.click(801, 442)"
                )
                usage_events = (
                    {
                        "kind": "agent_s_worker",
                        "model": "gpt-5.5",
                        "latency_ms": 123,
                        "temperature_configured": 1.0,
                        "temperature_requested": None,
                        "temperature_fallback_triggered": True,
                        "temperature_omitted_after_rejection": True,
                    },
                )
                info = {
                    "plan": "(Grounded Action)\n```python\nagent.click('button')\n```",
                    "plan_code": 'agent.click("the button", 1, "left")',
                    "reflection": "previous action verified successfully",
                    "verb": "click",
                    "prompt_text": "REFLECTION: ...\n",
                }

            class Client:
                metadata = {"native_exec_enabled": True}
                calls: list[dict] = []

                def next_action(self, **kwargs):
                    self.calls.append(kwargs)
                    return Prediction()

            client = Client()
            capture = MagicMock(side_effect=[str(step), str(final)])
            with (
                patch.object(gui_agent, "_capture_runtime", capture),
                patch.object(gui_agent.native_window, "focus_runtime", MagicMock()),
                patch.object(gui_agent, "_apply_action", MagicMock()),
                patch.object(gui_agent, "record_public_transition_action", MagicMock()),
                patch.object(
                    gui_agent, "_runtime_dimensions", return_value=(1920, 1080)
                ),
            ):
                phase = gui_agent._run_phase(
                    runtime=object(),
                    client=client,
                    instruction="reach the goal",
                    phase_dir=root / "phase",
                    max_steps=1,
                    observer=MagicMock(),
                )

        recorded = phase.actions[0]
        self.assertEqual(recorded["kind"], "click")
        self.assertIn("agent.click('button')", recorded["plan"])
        self.assertEqual(recorded["plan_code"], 'agent.click("the button", 1, "left")')
        self.assertEqual(recorded["reflection"], "previous action verified successfully")
        self.assertEqual(recorded["verb"], "click")
        self.assertEqual(recorded["prompt_text"], "REFLECTION: ...\n")
        self.assertEqual(
            recorded["temperature_audit"],
            [
                {
                    "kind": "agent_s_worker",
                    "model": "gpt-5.5",
                    "configured": 1.0,
                    "requested": None,
                    "fallback_triggered": True,
                    "omitted_after_rejection": True,
                }
            ],
        )

    def test_agent_s_runtime_metadata_preserves_temperature_contract(self) -> None:
        client = SimpleNamespace(
            metadata={
                "native_exec_enabled": True,
                "temperature_contract": {
                    "configured": 1.0,
                    "fallback": (
                        "omit_only_after_explicit_http_400_temperature_rejection"
                    ),
                    "scope": "persistent_worker_backend",
                },
            },
            config=SimpleNamespace(model="gpt-5.5", grounder_model="grounder"),
        )
        metadata = gui_agent._policy_runtime_metadata(
            client, policy_kind="agent_s"
        )
        self.assertEqual(metadata["temperature_contract"]["configured"], 1.0)
        self.assertIn(
            "explicit_http_400",
            metadata["temperature_contract"]["fallback"],
        )


def _fake_task(
    state_description: str = "a 4x4 board with two 2 tiles about to merge left",
) -> SimpleNamespace:
    return SimpleNamespace(
        id="2048-merge-left",
        benchmark="2048",
        state_description=state_description,
        issue=PublicIssue(
            id="2048-merge-left",
            benchmark="2048",
            repo="games/2048",
            issue=state_description,
        ),
        oracle_spec="benchmarks/oracle/2048-merge-left.yaml",
    )


class StartConstructionStatusTests(unittest.TestCase):
    def test_enum_and_initial_modes_are_strict(self) -> None:
        self.assertEqual(
            {status.value for status in gui_agent.StartConstructionStatus},
            {
                "not_applicable",
                "pending",
                "synthesis_failed",
                "restore_failed",
                "reached",
            },
        )
        from_start = gui_agent._new_gui_result(
            _fake_task(),
            start_mode="from_start",
            policy_kind="gui_agent",
            model="model",
            seed=0,
            max_steps=4,
        )
        synthesized = gui_agent._new_gui_result(
            _fake_task(),
            start_mode="synthesized",
            policy_kind="gui_agent",
            model="model",
            seed=0,
            max_steps=4,
        )
        self.assertEqual(from_start["start_construction_status"], "not_applicable")
        self.assertEqual(synthesized["start_construction_status"], "pending")
        with self.assertRaises(TypeError):
            gui_agent._set_start_construction_status(synthesized, "reached")  # type: ignore[arg-type]


class _FakeSession:
    """Context-manager stand-in for StateSession with a scriptable restore."""

    def __init__(self, restore_outcome: SimpleNamespace) -> None:
        self._restore_outcome = restore_outcome
        self.runtime = SimpleNamespace(
            page=MagicMock(),
            prng_metadata=lambda: {
                "source": "browser_page_runtime",
                "observed": True,
                "algorithm": "mulberry32",
                "seed": 0,
                "calls": 3,
                "viewport": {"width": 1280, "height": 800},
                "device_scale_factor": 1,
            },
        )
        self._profile = None
        self.restore_calls: list[dict] = []

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def restore(self, spec, spec_path, *, actions=None, screenshot_name="state.png"):
        self.restore_calls.append({"spec": spec, "spec_path": spec_path, "actions": actions})
        return self._restore_outcome


class GuiAgentSynthesizedTests(unittest.TestCase):
    """The fair self-synthesized start arm (build_synthesis_agent -> restore -> Phase B)."""

    GENERATED_SPEC = {
        "name": "s",
        "benchmark": "2048",
        "target": {"kind": "board"},
        "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
        "actions": [{"type": "move", "direction": "left"}],
        "assertions": [{"type": "tile_value_present"}],
    }

    def assert_fair_pre_start_zero(
        self, result: dict, *, construction_status: str
    ) -> None:
        self.assertEqual(result["start_construction_status"], construction_status)
        self.assertFalse(result["incomplete"])
        self.assertEqual(result["steps"], {"phase_a": 0, "phase_b": 0, "total": 0})
        self.assertFalse(result["policy_phase_started"])
        self.assertFalse(result["start_grading_started"])
        self.assertFalse(result["visual_grading_started"])
        self.assertEqual(
            result["start_gate_status"],
            "not_evaluated_start_construction_failed",
        )
        self.assertEqual(
            result["goal_evaluation_status"],
            "not_evaluated_start_construction_failed",
        )
        self.assertEqual(
            result["visual_only_evaluation_status"],
            "not_evaluated_start_construction_failed",
        )
        self.assertEqual(result["before_visual_status"], "not_evaluated")
        self.assertEqual(result["after_visual_status"], "not_evaluated")
        self.assertEqual(result["before_semantic_status"], "not_evaluated")
        self.assertEqual(result["after_semantic_status"], "not_evaluated")
        self.assertFalse(result["before_visual_ok"])
        self.assertFalse(result["after_visual_ok"])
        self.assertFalse(result["after_semantic_ok"])

    def _run(self, *, tmp: Path, synth_exc=None, checkpoint=None, restore_status="success",
             semantic=(True, "success", {}), before_semantic=None, verdict=None,
             cached_goldens: bool = False, cache_error: Exception | None = None):
        task = _fake_task()
        before_shot = tmp / "synth-start.png"
        after_shot = tmp / "after.png"
        before_shot.write_bytes(b"before")
        after_shot.write_bytes(b"after")

        agent = MagicMock()
        if synth_exc is not None:
            agent.synthesize.side_effect = synth_exc
        else:
            agent.synthesize.return_value = checkpoint or SimpleNamespace(
                generated_spec=self.GENERATED_SPEC,
                model_generated_spec=self.GENERATED_SPEC,
                used_public_scaffold=False,
            )
        restore_outcome = SimpleNamespace(
            status=restore_status, elapsed_ms=12,
            screenshot=str(before_shot) if restore_status == "success" else None,
            detail="",
        )
        session = _FakeSession(restore_outcome)
        if before_semantic is not None:
            session._profile = MagicMock()
        goldens = SimpleNamespace(before="g_before.png", after="g_after.png",
                                  after_description=None, after_rubric=None,
                                  fingerprint="cached-goldens",
                                  cache_hit=cached_goldens,
                                  cacheable=True,
                                  consistency={"source": "verified_cache"},
                                  quality={})
        lookup = MagicMock(
            return_value=goldens if cached_goldens else None,
            side_effect=cache_error,
        )
        ensure = MagicMock(return_value=goldens)
        if cached_goldens:
            ensure.side_effect = AssertionError("cache hit must skip golden generation")
        phase_b = SimpleNamespace(
            screenshot=str(after_shot),
            steps=3,
            actions=[{"step": 1}],
            elapsed_ms=17,
            grader_ms=0,
            incomplete=False,
        )

        with (
            patch.object(gui_agent, "build_synthesis_agent", return_value=agent) as build_mock,
            patch.object(gui_agent, "resolve_task_repo", return_value=tmp),
            patch.object(gui_agent, "load_spec", return_value={"assertions": []}),
            patch.object(
                gui_agent,
                "audit_scripted_reference_replay",
                return_value={
                    "status": "success",
                    "reference_reachable": True,
                    "reason": "test replay",
                },
            ),
            patch.object(gui_agent, "StateSession", return_value=session),
            patch.object(gui_agent, "lookup_cached_oracle_goldens", lookup),
            patch.object(gui_agent, "ensure_oracle_goldens", ensure),
            patch.object(gui_agent, "GuiAgentClient", return_value=MagicMock()),
            patch.object(gui_agent, "_run_phase", return_value=phase_b),
            patch.object(
                gui_agent,
                "_semantic_start_grade",
                return_value=before_semantic or (True, "success", {}),
            ),
            patch.object(gui_agent, "_semantic_grade", return_value=semantic),
            patch.object(gui_agent, "run_visual_pair_audit", return_value=verdict),
        ):
            result = _run_agent_task_synthesized(
                tmp, task, model="pixel/model", base_url="https://x/v1", api_key="k",
                api_key_env="X_KEY", synth_model="pixel/model", max_explore_steps=5,
                temperature=0.2, out_dir=tmp, max_steps=4, headless=True,
            )
        return result, agent, session, build_mock

    def test_synthesized_cache_hit_skips_hidden_golden_generation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result, *_ = self._run(
                tmp=Path(td),
                cached_goldens=True,
                verdict={
                    "result": True,
                    "before": {"label": "correct"},
                    "after": {"label": "correct"},
                },
            )

        self.assertTrue(result["oracle_goldens"]["cache_hit"])

    def test_synthesized_cache_read_error_is_structured_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result, *_ = self._run(
                tmp=Path(td),
                cache_error=OSError("cache unreadable"),
            )

        self.assertTrue(result["incomplete"])
        self.assertFalse(result["policy_phase_started"])
        self.assertIn("oracle golden generation failed", result["reason"])

    def test_synthesized_success_injects_self_built_start(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            verdict = {"result": True, "before": {"label": "correct"}, "after": {"label": "correct"}}
            result, agent, session, build_mock = self._run(tmp=tmp, verdict=verdict)

            # Synthesis stack built with the pixel model + api_key_env, in explore mode.
            _, kwargs = build_mock.call_args
            self.assertEqual(kwargs["model"], "pixel/model")
            self.assertEqual(kwargs["api_key_env"], "X_KEY")
            self.assertTrue(kwargs["explore"])
            self.assertEqual(kwargs["max_steps"], 5)

            # The SELF-synthesized spec (not the oracle) is what gets injected.
            expected_spec = {**self.GENERATED_SPEC, "experiment_seed": 0}
            self.assertEqual(session.restore_calls[0]["spec"], expected_spec)
            self.assertEqual(session.restore_calls[0]["actions"], [])

            self.assertEqual(result["start_mode"], "synthesized")
            self.assertTrue(result["before_ok"])
            self.assertTrue(result["semantic_ok"])
            self.assertTrue(result["stage2_pass"])
            self.assertTrue(result["e2e_pass"])
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["synth_status"], "restored")
            self.assertEqual(result["start_construction_status"], "reached")
            self.assertFalse(result["used_public_scaffold"])
            self.assertTrue(result["runtime_started"])
            self.assertTrue(result["policy_phase_started"])
            self.assertTrue(result["visual_grading_started"])
            self.assertTrue(result["seed_evidence"]["observed"])
            # The generated spec was persisted to disk.
            spec_path = Path(result["synthesized_spec"])
            self.assertTrue(spec_path.exists())
            self.assertEqual(yaml.safe_load(spec_path.read_text()), expected_spec)

    def test_semantic_start_failure_skips_visual_and_phase_b(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            verdict = {"result": True, "before": {"label": "correct"}, "after": {"label": "correct"}}
            result, *_ = self._run(
                tmp=tmp,
                before_semantic=(
                    False,
                    "assert_failed",
                    {"source": "love_production_save_roundtrip_v1"},
                ),
                verdict=verdict,
            )

        self.assertFalse(result["before_ok"])
        self.assertFalse(result["stage2_visual_pass"])
        self.assertFalse(result["semantic_ok"])
        self.assertFalse(result["stage2_pass"])
        self.assertFalse(result["e2e_pass"])
        self.assertFalse(result["policy_phase_started"])
        self.assertFalse(result["visual_grading_started"])
        self.assertEqual(result["steps"]["phase_b"], 0)
        self.assertEqual(
            result["goal_evaluation_status"],
            "not_evaluated_start_failed",
        )
        self.assertEqual(result["after_semantic_status"], "not_evaluated")
        self.assertEqual(
            result["visual_only_evaluation_status"],
            "not_evaluated_start_failed",
        )
        self.assertEqual(
            result["semantic_probe_source"],
            "love_production_save_roundtrip_v1",
        )
        self.assertEqual(result["start_construction_status"], "reached")

    def test_visual_start_failure_skips_policy_phase_and_after_grade(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            verdict = {
                "result": False,
                "before": {"label": "incorrect"},
                "after": {"label": "correct"},
            }
            result, *_ = self._run(tmp=tmp, verdict=verdict)

        self.assertFalse(result["policy_phase_started"])
        self.assertTrue(result["visual_grading_started"])
        self.assertEqual(result["steps"]["phase_b"], 0)
        self.assertEqual(result["start_gate_status"], "visual_failed")
        self.assertEqual(
            result["goal_evaluation_status"],
            "not_evaluated_start_failed",
        )
        self.assertEqual(result["pair_verdict"]["after"]["label"], "not_evaluated")
        self.assertEqual(result["start_construction_status"], "reached")

    def test_semantic_validation_error_is_incomplete_but_not_retryable_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result, *_ = self._run(
                tmp=Path(td),
                before_semantic=(False, "validate_error", {"error": "bad oracle"}),
            )

        self.assertTrue(result["incomplete"])
        self.assertFalse(result["infrastructure_error"])
        self.assertFalse(gui_agent._gui_trial_retryable_incomplete(result))

    def test_incomplete_phase_b_skips_semantic_and_visual_grading(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            task = _fake_task()
            before_shot = tmp / "synth-start.png"
            after_shot = tmp / "after.png"
            before_shot.write_bytes(b"before")
            after_shot.write_bytes(b"after")
            session = _FakeSession(
                SimpleNamespace(
                    status="success",
                    elapsed_ms=12,
                    screenshot=str(before_shot),
                    detail="",
                )
            )
            phase_b = gui_agent.PhaseResult(
                reached=False,
                steps=0,
                screenshot=str(after_shot),
                actions=[
                    {
                        "step": 1,
                        "error": "usage accounting is incomplete",
                        "infrastructure_error": True,
                    }
                ],
                elapsed_ms=23,
                incomplete=True,
            )
            semantic = MagicMock()
            visual = MagicMock(
                return_value={
                    "before": {"label": "correct"},
                    "after": {"label": "correct"},
                }
            )
            with (
                patch.object(gui_agent, "resolve_task_repo", return_value=SimpleNamespace()),
                patch.object(gui_agent, "load_spec", return_value={"assertions": []}),
                patch.object(
                    gui_agent,
                    "audit_scripted_reference_replay",
                    return_value={"status": "success", "reference_reachable": True},
                ),
                patch.object(
                    gui_agent,
                    "_obtain_self_synthesis",
                    return_value={
                        "status": "success",
                        "synth_status": "generated",
                        "generated_spec": self.GENERATED_SPEC,
                        "checkpoint_synthesis_ms": 7,
                        "self_synthesis_cost": gui_agent.cost.CostMeter().summary(),
                        "self_synthesis_cache": {},
                    },
                ),
                patch.object(gui_agent, "StateSession", return_value=session),
                patch.object(
                    gui_agent,
                    "ensure_oracle_goldens",
                    return_value=SimpleNamespace(
                        before="g-before.png",
                        after="g-after.png",
                        after_description=None,
                        after_rubric=None,
                    ),
                ),
                patch.object(gui_agent, "_run_phase", return_value=phase_b),
                patch.object(gui_agent, "_semantic_grade", semantic),
                patch.object(gui_agent, "run_visual_pair_audit", visual),
            ):
                result = _run_agent_task_synthesized(
                    tmp,
                    task,
                    model="pixel/model",
                    base_url="https://x/v1",
                    api_key="k",
                    api_key_env="X_KEY",
                    synth_model="pixel/model",
                    max_explore_steps=5,
                    temperature=0.2,
                    out_dir=tmp,
                    max_steps=4,
                    headless=True,
                    policy_client=MagicMock(),
                )

        self.assertTrue(result["incomplete"])
        self.assertEqual(result["aborted_after_incomplete_phase"], "phase_b")
        self.assertTrue(result["visual_grading_started"])
        self.assertEqual(result["goal_evaluation_status"], "not_evaluated_phase_b_incomplete")
        semantic.assert_not_called()
        visual.assert_called_once()
        self.assertFalse(visual.call_args.kwargs["evaluate_after"])

    def test_synthesis_failure_is_scored_zero_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            result, agent, session, _ = self._run(tmp=tmp, synth_exc=RuntimeError("boom"))

        # A counted failure row (not an infra error / drop): all pass flags are False.
        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["synth_status"], "synthesis_error")
        self.assert_fair_pre_start_zero(
            result, construction_status="synthesis_failed"
        )
        self.assertFalse(result["before_ok"])
        self.assertFalse(result["stage2_pass"])
        self.assertFalse(result["e2e_pass"])
        self.assertEqual(result["decision_budget"], 4)
        self.assertEqual(result["steps"], {"phase_a": 0, "phase_b": 0, "total": 0})
        self.assertFalse(result["budget_exhausted"])
        self.assertEqual(result["time_to_start_ms"], 0)
        self.assertEqual(result["time_e2e_excluding_synthesis_ms"], 0)
        self.assertTrue(result["initial_state_fingerprint"])
        self.assertFalse(result["runtime_started"])
        self.assertFalse(result["policy_phase_started"])
        self.assertFalse(result["visual_grading_started"])
        self.assertIsNone(result["prng"])
        self.assertFalse(result["seed_evidence"]["observed"])
        self.assertEqual(result["seed_evidence"]["source"], "runtime_not_started")
        self.assertFalse(result["result"])
        # Never opened a session / injected anything.
        self.assertEqual(session.restore_calls, [])

    def test_synthesis_infrastructure_failure_stays_pending_and_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result, *_ = self._run(
                tmp=Path(td), synth_exc=TimeoutError("provider timed out")
            )

        self.assertTrue(result["incomplete"])
        self.assertTrue(result["infrastructure_error"])
        self.assertEqual(result["start_construction_status"], "pending")
        self.assertNotIn(
            result["start_construction_status"],
            {"synthesis_failed", "restore_failed"},
        )
        self.assertEqual(result["steps"], {"phase_a": 0, "phase_b": 0, "total": 0})
        self.assertFalse(result["policy_phase_started"])
        self.assertFalse(result["start_grading_started"])
        self.assertFalse(result["visual_grading_started"])
        self.assertEqual(result["start_gate_status"], "not_evaluated")
        self.assertEqual(result["goal_evaluation_status"], "not_evaluated")
        self.assertEqual(result["visual_only_evaluation_status"], "not_evaluated")

    def test_self_start_restore_failure_is_scored_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            result, *_ = self._run(tmp=tmp, restore_status="mismatch")

        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["synth_status"], "mismatch")
        self.assert_fair_pre_start_zero(
            result, construction_status="restore_failed"
        )
        self.assertFalse(result["before_ok"])
        self.assertFalse(result["stage2_pass"])
        self.assertFalse(result["e2e_pass"])
        self.assertEqual(result["decision_budget"], 4)
        self.assertEqual(result["steps"], {"phase_a": 0, "phase_b": 0, "total": 0})
        self.assertFalse(result["budget_exhausted"])
        self.assertEqual(result["time_to_start_ms"], 12)
        self.assertEqual(result["time_e2e_excluding_synthesis_ms"], 12)
        self.assertTrue(result["initial_state_fingerprint"])
        self.assertTrue(result["runtime_started"])
        self.assertFalse(result["policy_phase_started"])
        self.assertFalse(result["visual_grading_started"])

    def test_no_usable_model_spec_is_scored_zero_without_injecting_scaffold(self) -> None:
        """Regression: a model that produced no structured spec (e.g. an untemplated
        public task fell back to ``public_scaffold_checkpoint``, or the model emitted
        prose) must not silently inject the hidden public scaffold. Before the fix this
        case injected ``checkpoint.generated_spec`` (the scaffold) unconditionally.
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            checkpoint = SimpleNamespace(
                generated_spec={"benchmark": "2048"},  # hidden scaffold fallback shape
                model_generated_spec=None,
                used_public_scaffold=False,
            )
            result, agent, session, _ = self._run(tmp=tmp, checkpoint=checkpoint)

        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["synth_status"], "no_model_spec")
        self.assert_fair_pre_start_zero(
            result, construction_status="synthesis_failed"
        )
        self.assertEqual(result["injected_spec_source"], "none")
        self.assertFalse(result["before_ok"])
        self.assertFalse(result["stage2_pass"])
        self.assertFalse(result["e2e_pass"])
        self.assertFalse(result["result"])
        self.assertEqual(result["decision_budget"], 4)
        self.assertEqual(result["steps"], {"phase_a": 0, "phase_b": 0, "total": 0})
        self.assertFalse(result["budget_exhausted"])
        self.assertTrue(result["initial_state_fingerprint"])
        self.assertFalse(result["runtime_started"])
        self.assertFalse(result["policy_phase_started"])
        self.assertFalse(result["visual_grading_started"])
        self.assertIsNone(result["prng"])
        self.assertFalse(result["seed_evidence"]["observed"])
        # Never opened a session / attempted to inject the scaffold as a start.
        self.assertEqual(session.restore_calls, [])

    def test_injects_model_own_spec_not_the_canonicalized_scaffold(self) -> None:
        """Regression: when the model's spec was canonicalized to the scaffold for
        ``generated_spec`` (``used_public_scaffold=True``) but the model's raw
        ``model_generated_spec`` differs, the arm must inject the model's own spec so
        it actually measures self-synthesis rather than template coverage.
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            model_spec = {
                "name": "model-own",
                "benchmark": "2048",
                "target": {"kind": "board", "board": []},
                "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
                "actions": [{"type": "move", "direction": "left"}],
                "assertions": [{"type": "tile_value_present"}],
            }
            scaffold_spec = {
                "name": "scaffold",
                "benchmark": "2048",
                "target": {"kind": "board", "board": [[2, 2, 0, 0]] * 4},
                "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
                "actions": [{"type": "move", "direction": "left"}],
                "assertions": [{"type": "tile_value_present"}],
            }
            checkpoint = SimpleNamespace(
                generated_spec=scaffold_spec,
                model_generated_spec=model_spec,
                used_public_scaffold=True,
            )
            verdict = {"result": True, "before": {"label": "correct"}, "after": {"label": "correct"}}
            result, agent, session, _ = self._run(tmp=tmp, checkpoint=checkpoint, verdict=verdict)

            self.assertEqual(
                session.restore_calls[0]["spec"],
                {**model_spec, "experiment_seed": 0},
            )
            self.assertEqual(
                yaml.safe_load(Path(result["synthesized_spec"]).read_text()),
                {**model_spec, "experiment_seed": 0},
            )
            self.assertTrue(result["used_public_scaffold"])
            self.assertEqual(result["injected_spec_source"], "model_generated_spec")


class LuantiStartConstructionStatusTests(unittest.TestCase):
    GENERATED_SPEC = {
        "name": "luanti-start",
        "benchmark": "luanti",
        "target": {"kind": "world_loaded"},
        "checkpoint": {
            "allowed_strategies": ["luanti_fresh_world"],
            "expected_validity": "replayable",
        },
        "actions": [{"type": "wait", "seconds": 0.1}],
        "assertions": [{"type": "world_loaded"}],
    }
    SEMANTIC_SNAPSHOT = {
        "source": "luanti_live_semantic_probe",
        "version": 1,
        "task_id": "luanti-start",
        "nodes": [
            {
                "pos": {"x": 2, "y": 1, "z": 2},
                "name": "basenodes:dirt",
                "param1": 0,
                "param2": 0,
            }
        ],
        "ranges": [],
        "fields": [{"path": "node.2,1,2", "value": "basenodes:dirt"}],
        "player": {"present": True, "name": "warptest"},
        "inventory": [],
        "mod_storage": [],
        "meta_inventory": [],
    }
    SEMANTIC_FINGERPRINT = hashlib.sha256(
        json.dumps(
            SEMANTIC_SNAPSHOT,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()

    @staticmethod
    def _seed_metadata() -> dict:
        return {
            "source": "luanti_warm_session",
            "observed": True,
            "algorithm": "luanti_fixed_map_seed",
            "seed": 0,
            "map_seed": 0,
            "process_alive_at_observation": True,
            "render": True,
            "frame": {"width": 1280, "height": 720},
            "map_backend": "sqlite3",
            "gameid": "devtest",
            "creative_mode": True,
            "git_commit": "test-commit",
            "world_path": "/tmp/luanti-world",
            "config_path": "/tmp/luanti.conf",
            "config_fingerprint": "test-config",
        }

    def _run(
        self,
        root: Path,
        *,
        synthesis_status: str = "success",
        restore_status: str = "success",
        semantic_evidence_ok: bool = True,
    ) -> dict:
        task = SimpleNamespace(
            id="luanti-start",
            benchmark="luanti",
            state_description="a constructed Luanti start",
            preconditions="a constructed Luanti start",
            reference_steps="perform the transition",
            goal_description="goal",
            horizon_class="long",
            issue=SimpleNamespace(id="luanti-start", benchmark="luanti"),
            oracle_spec="oracle.yaml",
        )
        initial = root / "initial.png"
        before = root / "before.png"
        initial.write_bytes(b"initial")
        before.write_bytes(b"before")
        synthesis = {
            "status": synthesis_status,
            "synth_status": (
                "generated" if synthesis_status == "success" else "no_model_spec"
            ),
            "generated_spec": (
                self.GENERATED_SPEC if synthesis_status == "success" else None
            ),
            "checkpoint_synthesis_ms": 7,
            "self_synthesis_cost": gui_agent.cost.CostMeter().summary(),
            "self_synthesis_cache": {},
            "reason": "deterministic unusable synthesis",
        }
        session = MagicMock()
        session.seed_metadata.return_value = self._seed_metadata()
        session.semantic_snapshot.return_value = {
            "source": "luanti_live_semantic_probe",
            "observed": semantic_evidence_ok,
            "task_id": "luanti-start",
            "fingerprint": (
                self.SEMANTIC_FINGERPRINT if semantic_evidence_ok else None
            ),
            "snapshot": (
                self.SEMANTIC_SNAPSHOT if semantic_evidence_ok else None
            ),
            "status": "success" if semantic_evidence_ok else "failure",
        }
        session.restore.return_value = SimpleNamespace(
            status=restore_status,
            elapsed_ms=12,
            detail="restore mismatch",
        )
        session_cm = MagicMock()
        session_cm.__enter__.return_value = session
        runtime = MagicMock()
        runtime.calibration_metadata.return_value = {"backend": "test"}
        runtime.screenshot.side_effect = [str(initial), str(before)]
        goldens = SimpleNamespace(
            before="gold-before.png",
            after="gold-after.png",
            after_description=None,
            after_rubric=None,
            fingerprint="goldens",
            cache_hit=True,
            cacheable=True,
            consistency={},
            quality={},
        )

        with (
            patch.object(gui_agent, "resolve_task_repo", return_value=root),
            patch.object(
                gui_agent,
                "load_spec",
                return_value={
                    "checkpoint": {"expected_validity": "replayable"},
                    "assertions": [],
                },
            ),
            patch.object(
                gui_agent,
                "audit_scripted_reference_replay",
                return_value={"status": "success", "reference_reachable": True},
            ),
            patch.object(gui_agent, "_obtain_self_synthesis", return_value=synthesis),
            patch.object(gui_agent, "lookup_cached_oracle_goldens", return_value=goldens),
            patch.object(gui_agent, "native_runtime_lock", return_value=nullcontext()),
            patch.object(gui_agent, "LuantiWarmSession", return_value=session_cm),
            patch.object(gui_agent, "LuantiGuiRuntime", return_value=runtime),
            patch.object(
                gui_agent,
                "_luanti_semantic_grade",
                return_value=(False, "validate_failed", {}),
            ),
        ):
            return gui_agent._run_luanti_agent_task(
                root,
                task,
                model="model",
                base_url="https://example.invalid/v1",
                api_key="unused",
                api_key_env="TEST_KEY",
                synth_model="model",
                max_explore_steps=3,
                temperature=0.0,
                out_dir=root / "report",
                max_steps=4,
                start_mode="synthesized",
                policy_kind="gui_agent",
                policy_client=MagicMock(),
                observer=MagicMock(),
                seed=0,
            )

    def assert_fair_pre_start_zero(
        self, result: dict, *, construction_status: str
    ) -> None:
        self.assertEqual(result["start_construction_status"], construction_status)
        self.assertFalse(result["incomplete"])
        self.assertEqual(result["steps"], {"phase_a": 0, "phase_b": 0, "total": 0})
        self.assertFalse(result["policy_phase_started"])
        self.assertFalse(result["start_grading_started"])
        self.assertFalse(result["visual_grading_started"])
        self.assertEqual(
            result["start_gate_status"],
            "not_evaluated_start_construction_failed",
        )
        self.assertEqual(
            result["goal_evaluation_status"],
            "not_evaluated_start_construction_failed",
        )
        self.assertEqual(
            result["visual_only_evaluation_status"],
            "not_evaluated_start_construction_failed",
        )
        self.assertEqual(result["before_visual_status"], "not_evaluated")
        self.assertEqual(result["after_visual_status"], "not_evaluated")
        self.assertEqual(result["before_semantic_status"], "not_evaluated")
        self.assertEqual(result["after_semantic_status"], "not_evaluated")
        self.assertFalse(result["before_visual_ok"])
        self.assertFalse(result["after_visual_ok"])
        self.assertFalse(result["after_semantic_ok"])

    def test_luanti_deterministic_synthesis_failure_is_fair_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = self._run(Path(td), synthesis_status="fair_failure")

        self.assert_fair_pre_start_zero(
            result, construction_status="synthesis_failed"
        )

    def test_luanti_restore_failure_is_fair_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = self._run(Path(td), restore_status="validate_failed")

        self.assert_fair_pre_start_zero(result, construction_status="restore_failed")

    def test_luanti_reached_survives_later_start_grade_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = self._run(Path(td))

        self.assertEqual(result["start_construction_status"], "reached")
        self.assertTrue(result["start_grading_started"])
        self.assertFalse(result["policy_phase_started"])
        self.assertFalse(result["visual_grading_started"])
        evidence = result["seed_evidence"]
        self.assertTrue(evidence["observed"])
        self.assertTrue(evidence["semantic_observed"])
        self.assertEqual(
            evidence["initial_semantic_source"],
            "luanti_live_semantic_probe",
        )
        self.assertEqual(evidence["initial_semantic_stage"], "restored")
        self.assertEqual(
            evidence["initial_semantic_fingerprint"],
            self.SEMANTIC_FINGERPRINT,
        )
        self.assertEqual(
            evidence["initial_semantic_observation"],
            self.SEMANTIC_SNAPSHOT,
        )

    def test_luanti_semantic_evidence_failure_stays_pending_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = self._run(Path(td), semantic_evidence_ok=False)

        self.assertEqual(result["start_construction_status"], "pending")
        self.assertTrue(result["incomplete"])
        self.assertTrue(result["infrastructure_error"])
        self.assertEqual(result["start_gate_status"], "not_evaluated")
        self.assertEqual(result["goal_evaluation_status"], "not_evaluated")
        self.assertEqual(result["visual_only_evaluation_status"], "not_evaluated")
        self.assertFalse(result["seed_evidence"]["observed"])
        self.assertFalse(result["seed_evidence"]["semantic_observed"])

    def test_luanti_seed_refresh_preserves_semantics_and_fails_closed(self) -> None:
        semantic_fields = {
            "semantic_observed": True,
            "initial_semantic_source": "luanti_live_semantic_probe",
            "initial_semantic_stage": "restored",
            "initial_semantic_fingerprint": self.SEMANTIC_FINGERPRINT,
            "initial_semantic_observation": self.SEMANTIC_SNAPSHOT,
        }
        result = {
            "seed_evidence": {
                "schema_version": 1,
                **self._seed_metadata(),
                **semantic_fields,
            }
        }
        session = MagicMock()
        session.seed_metadata.side_effect = [
            self._seed_metadata(),
            RuntimeError("live seed refresh failed"),
        ]

        gui_agent._record_luanti_seed_evidence(result, session)
        self.assertTrue(result["seed_evidence"]["observed"])
        for key, value in semantic_fields.items():
            self.assertEqual(result["seed_evidence"][key], value)

        gui_agent._record_luanti_seed_evidence(result, session)
        self.assertFalse(result["seed_evidence"]["observed"])
        self.assertIsNone(result["prng"])
        for key, value in semantic_fields.items():
            self.assertEqual(result["seed_evidence"][key], value)

    def test_luanti_seed_refresh_cannot_upgrade_failed_semantics(self) -> None:
        result = {
            "seed_evidence": {
                "schema_version": 1,
                **self._seed_metadata(),
                "semantic_observed": False,
                "initial_semantic_source": "luanti_live_semantic_probe",
                "initial_semantic_stage": "restored",
                "initial_semantic_fingerprint": None,
                "initial_semantic_observation": None,
                "observed": False,
            }
        }
        session = MagicMock()
        session.seed_metadata.return_value = self._seed_metadata()

        gui_agent._record_luanti_seed_evidence(result, session)

        self.assertFalse(result["seed_evidence"]["observed"])
        self.assertFalse(result["seed_evidence"]["semantic_observed"])


if __name__ == "__main__":
    unittest.main()
