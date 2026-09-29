from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from warptest import cost
from warptest.agent_s_bridge import (
    PINNED_AGENT_S_COMMIT,
    PINNED_AGENT_S_VERSION,
    AgentSAction,
    AgentSBridgeClient,
    AgentSBridgeConfig,
    AgentSUsageAccountingError,
    AgentSWorkerError,
    _worker_environment,
)
from warptest.agent_s_worker import (
    AgentSBindings,
    AgentSJsonlServer,
    WorkerConfigurationError,
    _quiet_call_llm_safe,
    load_agent_s_bindings,
)
from warptest.c1_task_deadline import C1TaskTimeout, c1_task_deadline


ROOT = Path(__file__).resolve().parents[1]


def test_pinned_agent_s_commit_matches_parent_gitlink() -> None:
    gitlink = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-s", "--", "baselines/agent-s"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert len(gitlink) == 4 and gitlink[0] == "160000"
    assert gitlink[1] == PINNED_AGENT_S_COMMIT


def _screenshot(path: Path, width: int = 16, height: int = 12) -> Path:
    Image.new("RGB", (width, height), color=(20, 40, 60)).save(path)
    return path


def _config(**overrides):
    values = {
        "model": "decision/model",
        "base_url": "https://decision.invalid/v1",
        "api_key_env": "TEST_AGENT_S_KEY",
        "grounder_model": "vision/model",
        "grounder_base_url": "https://vision.invalid/v1",
        "grounder_api_key_env": "TEST_AGENT_S_GROUND_KEY",
        "default_headers": {"X-End-User": "unit-test"},
        "grounder_default_headers": {"X-End-User": "unit-test"},
        "native_screen_width": 16,
        "native_screen_height": 12,
    }
    values.update(overrides)
    return AgentSBridgeConfig(**values)


class QuietCallLlmSafeTests(unittest.TestCase):
    def test_quiet_wrapper_never_retries_unknown_usage(self) -> None:
        class Agent:
            calls = 0

            def get_response(self, **_kwargs):
                self.calls += 1
                raise cost.UsageAccountingError("unknown provider usage")

        agent = Agent()
        with self.assertRaises(cost.UsageAccountingError):
            _quiet_call_llm_safe(agent)
        self.assertEqual(agent.calls, 1)

    def test_quiet_wrapper_reraises_last_provider_error_after_three_attempts(self) -> None:
        errors = [RuntimeError(f"rate limit {index}") for index in range(3)]

        class Agent:
            calls = 0

            def get_response(self, **_kwargs):
                error = errors[self.calls]
                self.calls += 1
                raise error

        agent = Agent()
        with self.assertRaisesRegex(RuntimeError, "rate limit 2") as raised:
            _quiet_call_llm_safe(agent)
        self.assertIs(raised.exception, errors[-1])
        self.assertEqual(agent.calls, 3)

    def test_quiet_wrapper_returns_a_later_success(self) -> None:
        class Agent:
            calls = 0

            def get_response(self, **_kwargs):
                self.calls += 1
                if self.calls < 3:
                    raise ConnectionError("temporary provider failure")
                return "agent.done()"

        agent = Agent()
        self.assertEqual(_quiet_call_llm_safe(agent), "agent.done()")
        self.assertEqual(agent.calls, 3)


class JsonlProtocolTests(unittest.TestCase):
    def test_init_reset_predict_close_is_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            shot = _screenshot(tmp / "screen.png")
            payload = _config().to_worker_payload(agent_s_root=tmp)
            requests = [
                {"id": 1, "op": "init", "config": payload},
                {"id": 2, "op": "reset", "task": "task-1"},
                {
                    "id": 3,
                    "op": "predict",
                    "task": "task-1",
                    "instruction": "reach the goal",
                    "screenshot": str(shot),
                    "width": 16,
                    "height": 12,
                    "history": ["key(Left)"],
                },
                {"id": 4, "op": "close"},
            ]
            input_stream = io.StringIO(
                "".join(json.dumps(item) + "\n" for item in requests)
            )
            output_stream = io.StringIO()

            class BaseAgent:
                pass

            bindings = AgentSBindings(
                agent_s3_class=BaseAgent,
                reflection_prompt="reflect",
                version=PINNED_AGENT_S_VERSION,
                commit=PINNED_AGENT_S_COMMIT,
            )

            class FakeExecutor:
                turn_count = 1

                def begin_step(self):
                    pass

            class FakeAgent(BaseAgent):
                def __init__(self):
                    self.executor = FakeExecutor()
                    self.reset_count = 0
                    self.warptest_usage_events = []
                    self.warptest_agent_s_version = PINNED_AGENT_S_VERSION
                    self.warptest_agent_s_commit = PINNED_AGENT_S_COMMIT

                def reset(self):
                    self.reset_count += 1

                def predict(self, instruction, observation):
                    self.last = (instruction, observation)
                    self.warptest_usage_events.append(
                        {
                            "model": "decision/model",
                            "kind": "agent_s_worker",
                            "prompt_tokens": 5,
                            "completion_tokens": 2,
                            "reasoning_tokens": 1,
                            "cached_prompt_tokens": 3,
                            "total_tokens": 7,
                            "latency_ms": 4,
                            "usage_complete": True,
                        }
                    )
                    return {"plan": "press enter"}, ["DONE"]

            with patch.dict(
                os.environ,
                {"TEST_AGENT_S_KEY": "dummy", "TEST_AGENT_S_GROUND_KEY": "dummy"},
            ), patch("warptest.agent_s_worker._native_exec") as exec_mock:
                server = AgentSJsonlServer(
                    input_stream,
                    output_stream,
                    agent_factory=lambda _config, _bindings: FakeAgent(),
                    bindings_loader=lambda _root: bindings,
                )
                server.serve()
            responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertEqual([item["id"] for item in responses], [1, 2, 3, 4])
        self.assertTrue(all(item["ok"] for item in responses))
        self.assertEqual(responses[0]["result"]["runtime_mode"], "native")
        self.assertEqual(
            responses[0]["result"]["worker_observation_mode"], "screenshot"
        )
        self.assertTrue(responses[0]["result"]["native_exec_enabled"])
        self.assertEqual(responses[2]["result"]["action"]["kind"], "done")
        self.assertEqual(
            responses[2]["result"]["usage_events"][0]["reasoning_tokens"], 1
        )
        self.assertTrue(responses[3]["result"]["closed"])
        exec_mock.assert_not_called()

    def test_failed_prediction_still_returns_current_step_usage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            shot = _screenshot(tmp / "screen.png")
            payload = _config().to_worker_payload(agent_s_root=tmp)
            requests = [
                {"id": 1, "op": "init", "config": payload},
                {
                    "id": 2,
                    "op": "predict",
                    "task": "task",
                    "instruction": "goal",
                    "screenshot": str(shot),
                    "width": 16,
                    "height": 12,
                    "history": [],
                },
                {"id": 3, "op": "close"},
            ]
            input_stream = io.StringIO(
                "".join(json.dumps(item) + "\n" for item in requests)
            )
            output_stream = io.StringIO()

            class BaseAgent:
                pass

            bindings = AgentSBindings(
                BaseAgent,
                "reflect",
                PINNED_AGENT_S_VERSION,
                PINNED_AGENT_S_COMMIT,
            )

            class FailingAgent(BaseAgent):
                def __init__(self):
                    self.warptest_usage_events = []
                    self.warptest_agent_s_version = PINNED_AGENT_S_VERSION
                    self.warptest_agent_s_commit = PINNED_AGENT_S_COMMIT
                    self.executor = self

                def begin_step(self):
                    self.warptest_usage_events.clear()

                def reset(self):
                    pass

                def predict(self, _instruction, _observation):
                    self.warptest_usage_events.append(
                        {
                            "model": "decision/model",
                            "kind": "agent_s_worker",
                            "prompt_tokens": 5,
                            "completion_tokens": 9,
                            "total_tokens": 14,
                        }
                    )
                    raise RuntimeError("native predict failed after paid call")

            with patch.dict(
                os.environ,
                {"TEST_AGENT_S_KEY": "dummy", "TEST_AGENT_S_GROUND_KEY": "dummy"},
            ):
                AgentSJsonlServer(
                    input_stream,
                    output_stream,
                    agent_factory=lambda _config, _bindings: FailingAgent(),
                    bindings_loader=lambda _root: bindings,
                ).serve()
            responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
        self.assertFalse(responses[1]["ok"])
        self.assertEqual(
            responses[1]["error"]["usage_events"][0]["total_tokens"], 14
        )

    def test_real_pinned_worker_static_init_without_api_request(self) -> None:
        python = ROOT / "baselines" / "agent-s" / ".venv" / "bin" / "python"
        agent_root = ROOT / "baselines" / "agent-s"
        if not python.is_file() or not agent_root.is_dir():
            self.skipTest("local Agent-S Python 3.12 environment is not installed")
        payload = _config().to_worker_payload(agent_s_root=agent_root)
        requests = "\n".join(
            [
                json.dumps({"id": 1, "op": "init", "config": payload}),
                json.dumps({"id": 2, "op": "close"}),
                "",
            ]
        )
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(ROOT),
                "TEST_AGENT_S_KEY": "dummy-not-used",
                "TEST_AGENT_S_GROUND_KEY": "dummy-not-used",
            }
        )
        completed = subprocess.run(
            [str(python), "-u", "-m", "warptest.agent_s_worker"],
            cwd=ROOT,
            env=env,
            input=requests,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["agent_s_version"], "0.3.2")
        self.assertEqual(
            responses[0]["result"]["agent_s_commit"], PINNED_AGENT_S_COMMIT
        )
        self.assertEqual(responses[0]["result"]["runtime_mode"], "native")
        self.assertEqual(
            responses[0]["result"]["worker_observation_mode"], "screenshot"
        )
        self.assertTrue(responses[0]["result"]["native_exec_enabled"])
        self.assertFalse(responses[0]["result"]["local_code_enabled"])
        self.assertTrue(responses[0]["result"]["reflection_enabled"])
        self.assertTrue(responses[0]["result"]["upstream_worker"])

    def test_parent_client_can_restart_static_pinned_worker(self) -> None:
        python = ROOT / "baselines" / "agent-s" / ".venv" / "bin" / "python"
        if not python.is_file():
            self.skipTest("local Agent-S Python 3.12 environment is not installed")
        with patch.dict(
            os.environ,
            {
                "TEST_AGENT_S_KEY": "dummy-not-used",
                "TEST_AGENT_S_GROUND_KEY": "dummy-not-used",
            },
        ):
            client = AgentSBridgeClient(
                _config(),
                root=ROOT,
                python_executable=python,
                request_timeout_s=30,
            )
            first = dict(client.start().metadata)
            client.close()
            second = dict(client.start().metadata)
            client.close()
        self.assertEqual(first["agent_s_version"], PINNED_AGENT_S_VERSION)
        self.assertEqual(second["agent_s_commit"], PINNED_AGENT_S_COMMIT)
        self.assertTrue(first["native_exec_enabled"])
        self.assertFalse(client.running)

    def test_dirty_agent_s_is_rejected_before_upstream_import(self) -> None:
        completed = [
            SimpleNamespace(stdout=PINNED_AGENT_S_COMMIT + "\n"),
            SimpleNamespace(stdout=" M gui_agents/s3/agents/agent_s.py\n"),
        ]
        with (
            tempfile.TemporaryDirectory() as td,
            patch("warptest.agent_s_worker.subprocess.run", side_effect=completed),
            patch("warptest.agent_s_worker.importlib.import_module") as importer,
        ):
            with self.assertRaises(WorkerConfigurationError):
                load_agent_s_bindings(Path(td))
        importer.assert_not_called()


class ParentCostImportTests(unittest.TestCase):
    def test_default_predict_has_no_compound_parent_deadline(self) -> None:
        with (
            tempfile.TemporaryDirectory() as td,
            patch.dict(
                os.environ,
                {"WARPTEST_AGENT_S_REQUEST_TIMEOUT": "1"},
                clear=False,
            ),
        ):
            client = AgentSBridgeClient(_config(timeout_s=300), root=td)

        # The legacy environment knob is intentionally ignored so a formal run
        # cannot silently restore the defective whole-step ceiling. The child
        # keeps its bounded provider transports and the parent waits for their
        # auditable JSONL result (or reader-detected child exit).
        self.assertIsNone(client.request_timeout_s)
        self.assertEqual(client.control_timeout_s, 360.0)
        self.assertEqual(
            client.predict_timeout_mode,
            "child_provider_bounded_no_parent_deadline",
        )
        self.assertEqual(client.max_provider_attempts_per_predict, 23)

    def test_explicit_predict_deadline_must_be_positive_and_finite(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "positive finite"):
                AgentSBridgeClient(_config(), root=td, request_timeout_s=0)

    def test_task_deadline_quiesces_predict_and_discards_action_after_usage(self) -> None:
        class FakeProcess:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO()
                self.stderr = io.StringIO()
                self.returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            shot = _screenshot(tmp / "screen.png")
            client = AgentSBridgeClient(_config(), root=tmp)
            process = FakeProcess()
            client._process = process  # type: ignore[assignment]

            def finish_after_deadline() -> None:
                time.sleep(0.03)
                client._responses.put(
                    {
                        "id": 1,
                        "ok": True,
                        "result": {
                            "action": {"kind": "done"},
                            "usage_events": [
                                {
                                    "model": "decision/model",
                                    "kind": "agent_s_worker",
                                    "prompt_tokens": 10,
                                    "completion_tokens": 2,
                                    "reasoning_tokens": 1,
                                    "cached_prompt_tokens": 0,
                                    "total_tokens": 12,
                                    "latency_ms": 25,
                                    "usage_complete": True,
                                }
                            ],
                            "info": {"usage_accounting_complete": True},
                        },
                    }
                )

            responder = threading.Thread(target=finish_after_deadline)
            responder.start()
            with cost.scoped_meter() as meter, self.assertRaises(C1TaskTimeout):
                with c1_task_deadline("quiesce-paid-predict", 0.01):
                    client.predict(
                        task="quiesce-paid-predict",
                        instruction="Finish the task.",
                        observation="A stable screen is visible.",
                        screenshot=shot,
                        width=16,
                        height=12,
                    )
            responder.join(timeout=1.0)

        self.assertFalse(responder.is_alive())
        self.assertIsNone(client.last_prediction)
        self.assertIsNone(process.returncode)
        summary = meter.summary()
        self.assertTrue(summary["usage_complete"])
        self.assertEqual(summary["llm_calls"], 1)
        self.assertEqual(summary["total_tokens"], 12)
        self.assertEqual(summary["unknown_total_token_calls"], 0)

    def test_prediction_imports_child_usage_once_into_parent_meter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            shot = _screenshot(tmp / "screen.png")
            client = AgentSBridgeClient(_config(), root=tmp)
            client._ensure_started = lambda: None  # type: ignore[method-assign]
            client._request = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
                "action": {"kind": "native_exec", "raw": "import pyautogui"},
                "usage_events": [
                    {
                        "model": "decision/model",
                        "kind": "agent_s_worker",
                        "prompt_tokens": 10,
                        "completion_tokens": 6,
                        "reasoning_tokens": 4,
                        "cached_prompt_tokens": 3,
                        "total_tokens": 16,
                        "latency_ms": 8,
                        "messages": [
                            {"role": "user", "content": "press enter"},
                        ],
                        "response": "import pyautogui; pyautogui.press('enter')",
                    },
                    {
                        "model": "vision/model",
                        "kind": "agent_s_grounding",
                        "prompt_tokens": 12,
                        "completion_tokens": 2,
                        "reasoning_tokens": 0,
                        "cached_prompt_tokens": 1,
                        "total_tokens": 14,
                        "latency_ms": 9,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "target"},
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": "data:image/png;base64,AAAA"
                                        },
                                    },
                                ],
                            }
                        ],
                        "response": '{"x":1,"y":2}',
                    },
                ],
                "info": {"turn": 1, "usage_accounting_complete": True},
            }
            with cost.scoped_meter() as meter:
                prediction = client.next_action(
                    task="task",
                    instruction="press enter",
                    observation="native",
                    screenshot=shot,
                    width=16,
                    height=12,
                    history=[],
                )
            summary = meter.summary()
        self.assertEqual(prediction.action.kind, "native_exec")
        self.assertEqual(summary["llm_calls"], 2)
        self.assertEqual(summary["total_tokens"], 30)
        self.assertEqual(summary["reasoning_tokens"], 4)
        self.assertEqual(summary["cached_prompt_tokens"], 4)
        self.assertEqual(summary["by_kind"]["agent_s_worker"]["calls"], 1)
        self.assertEqual(summary["by_kind"]["agent_s_grounding"]["calls"], 1)
        self.assertEqual(len(summary["calls"]), 2)
        self.assertEqual(
            summary["calls"][0]["response"],
            "import pyautogui; pyautogui.press('enter')",
        )
        self.assertEqual(
            summary["calls"][1]["messages"][0]["content"][1]["image_url"]["url"],
            "[omitted image]",
        )

    def test_failed_child_response_imports_usage_before_raising(self) -> None:
        class FakeProcess:
            stdin = io.StringIO()

            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory() as td:
            client = AgentSBridgeClient(_config(), root=td)
            client._process = FakeProcess()  # type: ignore[assignment]
            client._responses.put(
                {
                    "id": 1,
                    "ok": False,
                    "error": {
                        "type": "RuntimeError",
                        "message": "native predict failed",
                        "usage_events": [
                            {
                                "model": "decision/model",
                                "kind": "agent_s_worker",
                                "prompt_tokens": 5,
                                "completion_tokens": 9,
                                "reasoning_tokens": 8,
                                "cached_prompt_tokens": 2,
                                "total_tokens": 14,
                                "latency_ms": 3,
                            }
                        ],
                    },
                }
            )
            with cost.scoped_meter() as meter:
                with self.assertRaises(AgentSWorkerError):
                    client._request("predict")
            summary = meter.summary()
        self.assertEqual(summary["llm_calls"], 1)
        self.assertEqual(summary["total_tokens"], 14)
        self.assertEqual(summary["reasoning_tokens"], 8)

    def test_reasoning_detail_gap_does_not_block_action(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            shot = _screenshot(tmp / "screen.png")
            client = AgentSBridgeClient(_config(), root=tmp)
            client._ensure_started = lambda: None  # type: ignore[method-assign]
            client._request = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
                "action": {"kind": "done"},
                "usage_events": [
                    {
                        "model": "decision/model",
                        "kind": "agent_s_worker",
                        "prompt_tokens": 5,
                        "completion_tokens": 5,
                        "reasoning_tokens": None,
                        "total_tokens": 10,
                        "reasoning_expected": True,
                        "reasoning_detail_complete": False,
                        "reasoning_detail_status": "missing",
                        "usage_complete": True,
                    }
                ],
                "info": {"usage_accounting_complete": True},
            }
            with cost.scoped_meter() as meter:
                prediction = client.predict(
                    task="task",
                    instruction="goal",
                    observation="native",
                    screenshot=shot,
                    width=16,
                    height=12,
                    history=[],
                )
            summary = meter.summary()
            self.assertEqual(prediction.action.kind, "done")
            self.assertEqual(summary["total_tokens"], 10)
            self.assertTrue(summary["usage_complete"])
            self.assertFalse(summary["reasoning_detail_complete"])
            self.assertEqual(summary["reasoning_tokens_upper_bound"], 5)
            self.assertEqual(summary["incomplete_calls"], 0)
            self.assertEqual(summary["unknown_reasoning_token_calls"], 1)

    def test_worker_429_without_usage_does_not_add_parent_cost(self) -> None:
        class FakeProcess:
            stdin = io.StringIO()

            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory() as td:
            client = AgentSBridgeClient(_config(), root=td)
            client._process = FakeProcess()  # type: ignore[assignment]
            client._responses.put(
                {
                    "id": 1,
                    "ok": False,
                    "error": {
                        "type": "TimeoutError",
                        "message": "quota exceeded",
                        "status_code": 429,
                        "usage_events": [],
                    },
                }
            )
            with cost.scoped_meter() as meter, self.assertRaisesRegex(
                AgentSWorkerError, "quota exceeded"
            ):
                client._request("predict")

        self.assertEqual(meter.records, [])
        self.assertTrue(meter.summary()["usage_complete"])

    def test_worker_timeout_without_usage_records_unknown_parent_attempt(self) -> None:
        class FakeProcess:
            stdin = io.StringIO()

            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory() as td:
            client = AgentSBridgeClient(_config(), root=td)
            client._process = FakeProcess()  # type: ignore[assignment]
            client._responses.put(
                {
                    "id": 1,
                    "ok": False,
                    "error": {
                        "type": "APITimeoutError",
                        "message": "provider read timeout",
                        "usage_events": [],
                    },
                }
            )
            with cost.scoped_meter() as meter, self.assertRaises(
                AgentSUsageAccountingError
            ):
                client._request("predict")

        summary = meter.summary()
        self.assertEqual(summary["llm_calls"], 1)
        self.assertFalse(summary["usage_complete"])
        self.assertEqual(summary["unknown_total_token_calls"], 1)

    def test_predict_timeout_breaks_child_and_records_unknown_usage(self) -> None:
        class FakeProcess:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO()
                self.stderr = io.StringIO()
                self.returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                self.returncode = -9

        with tempfile.TemporaryDirectory() as td:
            client = AgentSBridgeClient(_config(), root=td)
            client._process = FakeProcess()  # type: ignore[assignment]
            with cost.scoped_meter() as meter, self.assertRaises(
                AgentSUsageAccountingError
            ) as raised:
                client._request("predict", timeout_s=0.01)
        self.assertFalse(raised.exception.usage_accounting_complete)
        self.assertIsNone(client._process)
        self.assertFalse(meter.summary()["usage_complete"])
        self.assertEqual(meter.summary()["unknown_total_token_calls"], 1)


class NativeConfigTests(unittest.TestCase):
    def test_config_requires_positive_screen_dimensions(self) -> None:
        with self.assertRaises(ValueError):
            _config(native_screen_width=0, native_screen_height=12).validated()
        with self.assertRaises(ValueError):
            AgentSBridgeConfig(
                model="decision/model",
                base_url="https://decision.invalid/v1",
                api_key_env="TEST_AGENT_S_KEY",
                grounder_model="vision/model",
                grounder_base_url="https://vision.invalid/v1",
                grounder_api_key_env="TEST_AGENT_S_GROUND_KEY",
            ).validated()

    def test_config_accepts_positive_screen_dimensions(self) -> None:
        config = _config(
            native_screen_width=1440,
            native_screen_height=900,
        ).validated()
        self.assertEqual(config.native_screen_width, 1440)
        self.assertEqual(config.native_screen_height, 900)

    def test_worker_payload_declares_screenshot_observation_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            payload = _config(
                native_screen_width=1280,
                native_screen_height=800,
            ).to_worker_payload(agent_s_root=Path(td))
        self.assertEqual(payload["worker_observation_mode"], "screenshot")
        self.assertEqual(payload["native_screen_width"], 1280)
        self.assertEqual(payload["native_screen_height"], 800)
        self.assertNotIn("runtime_mode", payload)
        self.assertNotIn("max_tokens", payload)

    def test_action_from_mapping_accepts_native_exec_kind(self) -> None:
        action = AgentSAction.from_mapping(
            {"kind": "native_exec", "raw": "agent.click(1,2)"}
        )
        self.assertEqual(action.kind, "native_exec")


class SecretBoundaryTests(unittest.TestCase):
    def test_config_payload_contains_env_names_but_no_key_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            payload = _config().to_worker_payload(agent_s_root=Path(td))
        encoded = json.dumps(payload)
        self.assertEqual(payload["api_key_env"], "TEST_AGENT_S_KEY")
        self.assertNotIn("secret-value", encoded)
        self.assertNotIn("api_key\"", encoded)
        self.assertFalse(payload["enable_local_env"])
        self.assertTrue(payload["enable_reflection"])
        self.assertGreaterEqual(payload["timeout_s"], 300)

    def test_authorization_headers_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _config(default_headers={"Authorization": "secret"}).validated()

    def test_child_environment_excludes_unrelated_parent_secrets(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TEST_AGENT_S_KEY": "selected-one",
                "TEST_AGENT_S_GROUND_KEY": "selected-two",
                "UNRELATED_CLOUD_SECRET": "must-not-cross",
            },
        ):
            env = _worker_environment(_config(), Path("/venv/bin/python"))
        self.assertEqual(env["TEST_AGENT_S_KEY"], "selected-one")
        self.assertEqual(env["TEST_AGENT_S_GROUND_KEY"], "selected-two")
        self.assertNotIn("UNRELATED_CLOUD_SECRET", env)


if __name__ == "__main__":
    unittest.main()
