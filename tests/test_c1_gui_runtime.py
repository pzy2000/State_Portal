from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call as mock_call, patch

from warptest import cost, gui_agent
from warptest.c1_task_deadline import c1_task_deadline
from warptest.gui_agent import GuiAgentClient, PhaseResult
from warptest.luanti_state import LuantiWarmSession
from warptest.native_input import (
    NativeActionValidationError,
    NativeInputUnavailableError,
    QuartzInputError,
    QuartzNativeInput,
    WindowBounds,
    _console_lock_state_from_ioreg,
    require_unlocked_native_session,
)
from warptest.runtime import BrowserRuntimeAdapter
from warptest.visual_bridge import FixedVisualBridge, FixedVisualGrounder


class _Message:
    def __init__(self, content: str | None) -> None:
        self.content = content

    @property
    def reasoning_content(self):  # pragma: no cover - touching it fails the test
        raise AssertionError("reasoning_content must never be read or persisted")


def _response(
    content: str | None,
    *,
    finish_reason: str = "stop",
    prompt_tokens: int = 3,
    completion_tokens: int = 5,
    reasoning_tokens: int = 0,
):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=_Message(content),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            completion_tokens_details=SimpleNamespace(
                reasoning_tokens=reasoning_tokens,
            ),
        ),
    )


class _Completions:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _client_for(*responses: object) -> tuple[object, _Completions]:
    completions = _Completions(list(responses))
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


class _OptionsClient:
    def __init__(self, completions: _Completions) -> None:
        self.chat = SimpleNamespace(completions=completions)
        self.timeouts: list[float] = []

    def with_options(self, *, timeout: float) -> "_OptionsClient":
        self.timeouts.append(float(timeout))
        return self


class GuiPolicyContractTests(unittest.TestCase):
    def test_observer_task_deadline_never_relaxes_provider_timeout(self) -> None:
        completions = _Completions([_response('{"screen":"menu"}')])
        fake_client = _OptionsClient(completions)
        with (
            patch.dict(os.environ, {"OBSERVER_KEY": "local-test-key"}, clear=False),
            patch("openai.OpenAI", return_value=fake_client),
            patch(
                "warptest.visual_bridge.image_data_url",
                return_value="data:image/png;base64,AA==",
            ),
            c1_task_deadline("observer", 1200.0),
        ):
            observer = FixedVisualBridge(
                model="observer",
                base_url="https://gateway.example/v1",
                api_key_env="OBSERVER_KEY",
                provider_timeout_s=900.0,
            )
            observer.observe(
                Path("mock-frame.png"),
                instruction="find menu",
                width=1280,
                height=800,
            )

        self.assertEqual(len(fake_client.timeouts), 1)
        self.assertLessEqual(fake_client.timeouts[0], 900.0)
        self.assertGreater(fake_client.timeouts[0], 899.0)

    def test_model_action_error_is_not_retried_as_infrastructure(self) -> None:
        self.assertFalse(
            gui_agent._is_infrastructure_error(ValueError("Unknown key NotARealKey"))
        )
        self.assertTrue(
            gui_agent._is_infrastructure_error(
                RuntimeError("Target page, context or browser has been closed")
            )
        )

    def test_reasoning_only_response_retries_once_from_8192_to_16384(self) -> None:
        fake_client, completions = _client_for(
            _response(
                None,
                finish_reason="length",
                prompt_tokens=11,
                completion_tokens=8192,
                reasoning_tokens=8192,
            ),
            _response(
                '{"action":"done","reason":"goal visible"}',
                prompt_tokens=12,
                completion_tokens=8,
                reasoning_tokens=2,
            ),
        )
        with patch("openai.OpenAI", return_value=fake_client):
            policy = GuiAgentClient(
                model="reasoner",
                base_url="https://gateway.example/v1",
                api_key="local-test-key",
                max_tokens=8192,
            )

        with cost.scoped_meter() as meter:
            action = policy.next_action(
                instruction="finish the transition",
                screenshot=Path("unused-in-text-only-mode.png"),
                history=[],
                observation='{"screen":"goal"}',
            )

        self.assertEqual(action.kind, "done")
        self.assertEqual(
            [call["max_tokens"] for call in completions.calls],
            [8192, 16384],
        )
        self.assertEqual(len(meter.records), 2)
        self.assertEqual([record.kind for record in meter.records], ["gui_policy", "gui_policy"])
        self.assertEqual(sum(record.reasoning_tokens for record in meter.records), 8194)

    def test_reasoning_only_stop_is_not_retried_at_16384(self) -> None:
        fake_client, completions = _client_for(
            _response(
                None,
                finish_reason="stop",
                prompt_tokens=11,
                completion_tokens=40,
                reasoning_tokens=40,
            ),
        )
        with patch("openai.OpenAI", return_value=fake_client):
            policy = GuiAgentClient(
                model="reasoner",
                base_url="https://gateway.example/v1",
                api_key="local-test-key",
                max_tokens=8192,
            )

        with cost.scoped_meter() as meter, self.assertRaisesRegex(
            RuntimeError, "empty content"
        ):
            policy.next_action(
                instruction="finish the transition",
                screenshot=Path("unused-in-text-only-mode.png"),
                history=[],
                observation='{"screen":"goal"}',
            )

        self.assertEqual(
            [call["max_tokens"] for call in completions.calls],
            [8192],
        )
        self.assertEqual(len(meter.records), 1)

    def test_relay_headers_reach_observer_and_text_only_policy(self) -> None:
        observer_api, observer_calls = _client_for(_response('{"screen":"menu"}'))
        policy_api, policy_calls = _client_for(
            _response('{"action":"click","x":20,"y":30,"reason":"start"}')
        )
        openai_factory = MagicMock(side_effect=[observer_api, policy_api])
        with (
            patch.dict(
                os.environ,
                {
                    "RELAY_API_KEY": "local-test-key",
                    "RELAY_BASE_URL": "https://relay.example/v1",
                    "RELAY_END_USER": "c1-tester",
                },
                clear=False,
            ),
            patch("openai.OpenAI", openai_factory),
        ):
            observer = FixedVisualBridge(
                model="claude-sonnet-5@relay",
                base_url="https://unused.example/v1",
                api_key_env="UNUSED_API_KEY",
            )
            policy = GuiAgentClient(
                model="glm-5.2@relay",
                base_url="https://unused.example/v1",
                api_key_env="UNUSED_API_KEY",
            )
            with patch("warptest.visual_bridge.image_data_url", return_value="data:image/png;base64,AA=="):
                observation = observer.observe(
                    Path("mock-frame.png"),
                    instruction="reach the start",
                    width=1280,
                    height=800,
                )
            with patch(
                "warptest.gui_agent.image_data_url",
                side_effect=AssertionError("formal decision policy must not receive image_url"),
            ):
                action = policy.next_action(
                    instruction="reach the start",
                    screenshot=Path("mock-frame.png"),
                    history=[],
                    observation=observation,
                )

        self.assertEqual(action.kind, "click")
        self.assertEqual(observer.model, "claude-sonnet-5")
        self.assertEqual(policy.model, "glm-5.2")
        self.assertEqual(openai_factory.call_count, 2)
        for call in openai_factory.call_args_list:
            kwargs = call.kwargs
            self.assertEqual(kwargs["base_url"], "https://relay.example/v1")
            self.assertEqual(kwargs["default_headers"], {"X-End-User": "c1-tester"})
            self.assertGreaterEqual(kwargs["timeout"], 300)

        observer_user_content = observer_calls.calls[0]["messages"][1]["content"]
        self.assertIsInstance(observer_user_content, list)
        self.assertEqual(observer_user_content[-1]["type"], "image_url")

        policy_user_content = policy_calls.calls[0]["messages"][1]["content"]
        self.assertIsInstance(policy_user_content, str)
        policy_payload = json.loads(policy_user_content)
        self.assertEqual(policy_payload["visual_observation"], '{"screen":"menu"}')
        self.assertNotIn("image_url", policy_user_content)

    def test_gui_policy_missing_usage_fails_closed_and_marks_meter(self) -> None:
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=_Message('{"action":"done"}'), finish_reason="stop")],
            usage=None,
        )
        fake_client, completions = _client_for(response)
        with patch("openai.OpenAI", return_value=fake_client):
            policy = GuiAgentClient(
                model="no-usage",
                base_url="https://gateway.example/v1",
                api_key="local-test-key",
            )
        with cost.scoped_meter() as meter, self.assertRaises(cost.UsageAccountingError):
            policy.next_action(
                instruction="finish",
                screenshot=Path("unused.png"),
                history=[],
                observation='{"screen":"goal"}',
            )
        self.assertEqual(len(completions.calls), 1)
        self.assertFalse(meter.summary()["usage_complete"])

    def test_failed_gui_call_with_provider_usage_is_still_counted(self) -> None:
        request_error = RuntimeError("provider rejected generated response")
        request_error.usage = SimpleNamespace(
            prompt_tokens=9,
            completion_tokens=7,
            total_tokens=16,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=7),
        )
        fake_client, completions = _client_for(request_error)
        with patch("openai.OpenAI", return_value=fake_client):
            policy = GuiAgentClient(
                model="failed-reasoner",
                base_url="https://gateway.example/v1",
                api_key="local-test-key",
            )

        with cost.scoped_meter() as meter, self.assertRaisesRegex(
            RuntimeError, "provider rejected"
        ):
            policy.next_action(
                instruction="finish",
                screenshot=Path("unused.png"),
                history=[],
                observation='{"screen":"goal"}',
            )

        self.assertEqual(len(completions.calls), 1)
        summary = meter.summary()
        self.assertTrue(summary["usage_complete"])
        self.assertEqual(summary["llm_calls"], 1)
        self.assertEqual(summary["prompt_tokens"], 9)
        self.assertEqual(summary["completion_tokens"], 7)
        self.assertEqual(summary["reasoning_tokens"], 7)

    def test_gui_timeout_without_usage_records_unknown_attempt(self) -> None:
        request_error = TimeoutError("gateway transport timed out")
        fake_client, _completions = _client_for(request_error)
        with patch("openai.OpenAI", return_value=fake_client):
            policy = GuiAgentClient(
                model="transport-model",
                base_url="https://gateway.example/v1",
                api_key="local-test-key",
            )

        with cost.scoped_meter() as meter, self.assertRaises(
            cost.UsageAccountingError
        ):
            policy.next_action(
                instruction="finish",
                screenshot=Path("unused.png"),
                history=[],
                observation='{"screen":"goal"}',
            )

        self.assertEqual(len(meter.records), 1)
        self.assertFalse(meter.summary()["usage_complete"])
        self.assertEqual(meter.summary()["unknown_total_token_calls"], 1)

    def test_gui_429_without_usage_is_not_a_paid_attempt(self) -> None:
        request_error = RuntimeError("quota exceeded")
        request_error.status_code = 429
        fake_client, _completions = _client_for(request_error)
        with patch("openai.OpenAI", return_value=fake_client):
            policy = GuiAgentClient(
                model="rate-limited-model",
                base_url="https://gateway.example/v1",
                api_key="local-test-key",
            )
        with cost.scoped_meter() as meter, self.assertRaisesRegex(
            RuntimeError, "quota exceeded"
        ):
            policy.next_action(
                instruction="finish",
                screenshot=Path("unused.png"),
                history=[],
                observation='{"screen":"goal"}',
            )
        self.assertEqual(meter.records, [])

    def test_fixed_observer_retries_once_at_16384(self) -> None:
        fake_client, completions = _client_for(
            _response(
                None,
                finish_reason="max_tokens",
                completion_tokens=8192,
                reasoning_tokens=8192,
            ),
            _response('{"screen":"menu"}', completion_tokens=6),
        )
        with (
            patch.dict(os.environ, {"OBSERVER_KEY": "local-test-key"}, clear=False),
            patch("openai.OpenAI", return_value=fake_client),
            patch("warptest.visual_bridge.image_data_url", return_value="data:image/png;base64,AA=="),
            cost.scoped_meter() as meter,
        ):
            observer = FixedVisualBridge(
                model="observer",
                base_url="https://gateway.example/v1",
                api_key_env="OBSERVER_KEY",
                max_tokens=64,
            )
            result = observer.observe(
                Path("mock-frame.png"),
                instruction="find menu",
                width=1280,
                height=800,
            )
        self.assertEqual(result, '{"screen":"menu"}')
        self.assertEqual([call["max_tokens"] for call in completions.calls], [8192, 16384])
        self.assertEqual(meter.summary()["by_kind"]["visual_observer"]["calls"], 2)

    def test_fixed_observer_reasoning_only_stop_is_not_retried(self) -> None:
        fake_client, completions = _client_for(
            _response(
                None,
                finish_reason="stop",
                completion_tokens=40,
                reasoning_tokens=40,
            ),
        )
        with (
            patch.dict(os.environ, {"OBSERVER_KEY": "local-test-key"}, clear=False),
            patch("openai.OpenAI", return_value=fake_client),
            patch(
                "warptest.visual_bridge.image_data_url",
                return_value="data:image/png;base64,AA==",
            ),
            cost.scoped_meter() as meter,
            self.assertRaisesRegex(RuntimeError, "empty content"),
        ):
            observer = FixedVisualBridge(
                model="observer",
                base_url="https://gateway.example/v1",
                api_key_env="OBSERVER_KEY",
            )
            observer.observe(
                Path("mock-frame.png"),
                instruction="find menu",
                width=1280,
                height=800,
            )

        self.assertEqual([call["max_tokens"] for call in completions.calls], [8192])
        self.assertEqual(meter.summary()["by_kind"]["visual_observer"]["calls"], 1)

    def test_observer_unquantified_reasoning_is_bounded_and_retried(self) -> None:
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=_Message(None), finish_reason="length")],
            usage=SimpleNamespace(
                prompt_tokens=3,
                completion_tokens=8192,
                total_tokens=8195,
                completion_tokens_details=None,
            ),
        )
        fake_client, completions = _client_for(
            response,
            _response('{"screen":"menu"}', completion_tokens=6),
        )
        with (
            patch.dict(os.environ, {"OBSERVER_KEY": "local-test-key"}, clear=False),
            patch("openai.OpenAI", return_value=fake_client),
            patch("warptest.visual_bridge.image_data_url", return_value="data:image/png;base64,AA=="),
            cost.scoped_meter() as meter,
        ):
            observer = FixedVisualBridge(
                model="observer",
                base_url="https://gateway.example/v1",
                api_key_env="OBSERVER_KEY",
            )
            result = observer.observe(
                Path("mock-frame.png"),
                instruction="find menu",
                width=1280,
                height=800,
            )
        self.assertEqual(result, '{"screen":"menu"}')
        self.assertEqual(len(completions.calls), 2)
        summary = meter.summary()
        self.assertTrue(summary["usage_complete"])
        self.assertEqual(summary["unknown_reasoning_token_calls"], 1)
        self.assertIsNone(summary["reasoning_tokens"])
        self.assertEqual(summary["reasoning_tokens_upper_bound"], 8192)

    def test_failed_observer_call_with_usage_is_counted(self) -> None:
        request_error = RuntimeError("observer provider failure")
        request_error.usage = SimpleNamespace(
            prompt_tokens=12,
            completion_tokens=4,
            total_tokens=16,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=4),
        )
        fake_client, completions = _client_for(request_error)
        with (
            patch.dict(os.environ, {"OBSERVER_KEY": "local-test-key"}, clear=False),
            patch("openai.OpenAI", return_value=fake_client),
            patch("warptest.visual_bridge.image_data_url", return_value="data:image/png;base64,AA=="),
            cost.scoped_meter() as meter,
        ):
            observer = FixedVisualBridge(
                model="observer",
                base_url="https://gateway.example/v1",
                api_key_env="OBSERVER_KEY",
            )
            with self.assertRaisesRegex(RuntimeError, "observer provider failure"):
                observer.observe(
                    Path("mock-frame.png"),
                    instruction="find menu",
                    width=1280,
                    height=800,
                )

        self.assertEqual(len(completions.calls), 1)
        summary = meter.summary()
        self.assertTrue(summary["usage_complete"])
        self.assertEqual(summary["by_kind"]["visual_observer"]["calls"], 1)
        self.assertEqual(summary["reasoning_tokens"], 4)

    def test_observer_timeout_without_usage_records_unknown_attempt(self) -> None:
        fake_client, _completions = _client_for(TimeoutError("observer transport timeout"))
        with (
            patch.dict(os.environ, {"OBSERVER_KEY": "local-test-key"}, clear=False),
            patch("openai.OpenAI", return_value=fake_client),
            patch("warptest.visual_bridge.image_data_url", return_value="data:image/png;base64,AA=="),
            cost.scoped_meter() as meter,
        ):
            observer = FixedVisualBridge(
                model="observer",
                base_url="https://gateway.example/v1",
                api_key_env="OBSERVER_KEY",
            )
            with self.assertRaises(cost.UsageAccountingError):
                observer.observe(
                    Path("mock-frame.png"),
                    instruction="find menu",
                    width=1280,
                    height=800,
                )

        self.assertEqual(len(meter.records), 1)
        self.assertFalse(meter.summary()["usage_complete"])

    def test_grounder_missing_usage_fails_closed(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=_Message('{"x":10,"y":20,"bbox_px":[1,2,30,40]}'),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )
        fake_client, _completions = _client_for(response)
        with (
            patch.dict(os.environ, {"OBSERVER_KEY": "local-test-key"}, clear=False),
            patch("openai.OpenAI", return_value=fake_client),
            patch("warptest.visual_bridge.image_data_url", return_value="data:image/png;base64,AA=="),
            cost.scoped_meter() as meter,
        ):
            grounder = FixedVisualGrounder(
                model="grounder",
                base_url="https://gateway.example/v1",
                api_key_env="OBSERVER_KEY",
            )
            with self.assertRaises(cost.UsageAccountingError):
                grounder.ground(
                    Path("mock-frame.png"),
                    instruction="the play button",
                    width=1280,
                    height=800,
                )
        self.assertFalse(meter.summary()["usage_complete"])


class BrowserSeedAndViewportTests(unittest.TestCase):
    def test_context_pins_dpr_and_installs_seeded_prng_before_navigation(self) -> None:
        runtime = object.__new__(BrowserRuntimeAdapter)
        runtime.browser = MagicMock()
        runtime.context = None
        runtime.page = None
        runtime.experiment_seed = -1
        context = MagicMock()
        runtime.browser.new_context.return_value = context

        runtime._new_context()

        runtime.browser.new_context.assert_called_once_with(
            viewport={"width": 1280, "height": 800},
            device_scale_factor=1,
        )
        context.add_init_script.assert_called_once()
        script = context.add_init_script.call_args.kwargs["script"]
        self.assertIn("Math.random = random", script)
        self.assertIn("seed: 4294967295", script)
        self.assertIn("algorithm: 'mulberry32'", script)

        runtime.page = MagicMock()
        runtime.page.evaluate.return_value = {
            "algorithm": "mulberry32",
            "seed": 4294967295,
            "calls": 7,
            "viewport": {"width": 1280, "height": 800},
            "device_scale_factor": 1,
        }
        self.assertEqual(
            runtime.prng_metadata(),
            {
                "source": "browser_page_runtime",
                "observed": True,
                "algorithm": "mulberry32",
                "seed": 4294967295,
                "viewport": {"width": 1280, "height": 800},
                "device_scale_factor": 1,
                "calls": 7,
            },
        )

    def test_prng_metadata_does_not_fall_back_to_requested_values(self) -> None:
        runtime = object.__new__(BrowserRuntimeAdapter)
        runtime.experiment_seed = 17
        runtime.page = None

        self.assertEqual(
            runtime.prng_metadata(),
            {
                "source": "browser_page_runtime",
                "observed": False,
                "algorithm": None,
                "seed": None,
                "calls": None,
                "viewport": None,
                "device_scale_factor": None,
            },
        )


class LuantiSeedEvidenceTests(unittest.TestCase):
    def test_rendered_macos_session_uses_sdl_background_application_hint(self) -> None:
        session = object.__new__(LuantiWarmSession)
        session.render = True

        with (
            patch.dict(os.environ, {"EXISTING": "kept"}, clear=True),
            patch("warptest.luanti_state.platform.system", return_value="Darwin"),
        ):
            environment = session._launch_environment()

        self.assertEqual(environment["EXISTING"], "kept")
        self.assertEqual(environment["SDL_MAC_BACKGROUND_APP"], "1")

    def test_non_rendered_session_does_not_set_sdl_background_application_hint(self) -> None:
        session = object.__new__(LuantiWarmSession)
        session.render = False

        with (
            patch.dict(os.environ, {"EXISTING": "kept"}, clear=True),
            patch("warptest.luanti_state.platform.system", return_value="Darwin"),
        ):
            environment = session._launch_environment()

        self.assertNotIn("SDL_MAC_BACKGROUND_APP", environment)

    def test_seed_metadata_reads_live_config_instead_of_requested_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            world = root / "world"
            world.mkdir()
            (world / "world.mt").write_text(
                "gameid = devtest\nbackend = sqlite3\nplayer_backend = sqlite3\n",
                encoding="utf-8",
            )
            conf = root / "warm-minetest.conf"
            conf.write_text(
                "fixed_map_seed = 17\ncreative_mode = true\nmg_name = flat\n",
                encoding="utf-8",
            )
            session = object.__new__(LuantiWarmSession)
            session._world = world
            session._conf = conf
            session._proc = MagicMock()
            session._proc.poll.return_value = None
            session.experiment_seed = 999
            session.render = True
            session.git_commit = "deadbeef"

            evidence = session.seed_metadata()

        self.assertTrue(evidence["observed"])
        self.assertEqual(evidence["source"], "luanti_warm_session")
        self.assertEqual(evidence["seed"], 17)
        self.assertEqual(evidence["map_seed"], 17)
        self.assertEqual(evidence["map_backend"], "sqlite3")
        self.assertEqual(evidence["gameid"], "devtest")
        self.assertEqual(evidence["frame"], {"width": 1280, "height": 720})

    def test_semantic_snapshot_fingerprints_live_values_not_spec_expectations(self) -> None:
        session = object.__new__(LuantiWarmSession)
        session._seq = 0
        live_snapshot = {
            "source": "luanti_live_semantic_probe",
            "version": 1,
            "task_id": "luanti-task",
            "nodes": [
                {
                    "pos": {"x": 2, "y": 1, "z": 2},
                    "name": "air",
                    "param1": 0,
                    "param2": 0,
                }
            ],
            "ranges": [],
            "fields": [{"path": "node.2,1,2", "value": "air"}],
            "player": {"present": True, "name": "warptest"},
            "inventory": [],
            "mod_storage": [],
            "meta_inventory": [],
        }
        session._roundtrip = MagicMock(
            return_value={"status": "success", "semantic_snapshot": live_snapshot}
        )
        requested_spec = {
            "target": {
                "kind": "synthesized_world",
                "nodes": [
                    {
                        "pos": {"x": 2, "y": 1, "z": 2},
                        "node": "basenodes:dirt",
                    }
                ],
                "validations": [
                    {"path": "node.2,1,2", "expected": "basenodes:dirt"}
                ],
            },
            "actions": [
                {
                    "type": "luanti_set_node",
                    "pos": {"x": 2, "y": 2, "z": 2},
                    "node": "basenodes:stone",
                }
            ],
        }

        evidence = session.semantic_snapshot("luanti-task", requested_spec)

        self.assertTrue(evidence["observed"])
        self.assertEqual(evidence["snapshot"]["nodes"][0]["name"], "air")
        request = session._roundtrip.call_args.args[0]
        probe_json = json.dumps(request["spec"]["semantic_probe"], sort_keys=True)
        self.assertNotIn("basenodes:dirt", probe_json)
        self.assertNotIn("basenodes:stone", probe_json)
        self.assertIn('"task_id": "luanti-task"', probe_json)
        expected = hashlib.sha256(
            json.dumps(
                live_snapshot,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(evidence["fingerprint"], expected)


class BrowserTeardownTests(unittest.TestCase):
    def test_closed_playwright_transport_does_not_invalidate_completed_trial(self) -> None:
        runtime = object.__new__(BrowserRuntimeAdapter)
        runtime.context = MagicMock()
        runtime.browser = MagicMock()
        runtime._playwright = MagicMock()
        runtime.context.close.side_effect = RuntimeError("connection already closed")
        runtime.browser.close.side_effect = RuntimeError("connection already closed")
        runtime._playwright.stop.side_effect = RuntimeError("connection already closed")
        runtime._stop_server = MagicMock(side_effect=RuntimeError("already stopped"))

        runtime.close()
        runtime.close()

        self.assertIsNone(runtime.context)
        self.assertIsNone(runtime.browser)
        self.assertIsNone(runtime._playwright)
        runtime._stop_server.assert_called()


class HybridGradingAndBudgetTests(unittest.TestCase):
    def _run_from_start(
        self,
        root: Path,
        *,
        before_semantic: bool,
        after_semantic: bool,
        cached_goldens: bool = False,
        phase_a_steps: int = 7,
        phase_b_steps: int = 3,
        budget: int = 10,
        reference_reachable: bool = True,
        phase_a_incomplete: bool = False,
        phase_b_incomplete: bool = False,
    ) -> tuple[dict[str, object], MagicMock, MagicMock, MagicMock]:
        initial = root / "initial.png"
        before = root / "before.png"
        after = root / "after.png"
        for path, payload in ((initial, b"initial"), (before, b"before"), (after, b"after")):
            path.write_bytes(payload)

        task = SimpleNamespace(
            id="task-1",
            benchmark="2048",
            state_description="legacy state",
            preconditions="two tiles ready to merge",
            reference_steps="press left",
            goal_description="a merged tile is visible",
            horizon_class="short",
            issue=SimpleNamespace(id="task-1", benchmark="2048"),
            oracle_spec="oracle.yaml",
        )
        goldens = SimpleNamespace(
            before="gold-before.png",
            after="gold-after.png",
            after_description=None,
            after_rubric=None,
            fingerprint="golden-fingerprint",
            cache_hit=cached_goldens,
            cacheable=True,
            consistency={"source": "verified_cache"},
            quality={
                "before": {"informative": True},
                "after": {"informative": True},
            },
            artifacts={
                "before": {"sha256": "a" * 64},
                "after": {"sha256": "b" * 64},
                "metadata": {"sha256": "c" * 64},
            },
        )
        phase_a = PhaseResult(
            reached=False,
            steps=phase_a_steps,
            screenshot=str(before),
            actions=(
                [{"step": 1, "error": "usage accounting is incomplete"}]
                if phase_a_incomplete
                else []
            ),
            elapsed_ms=70,
            grader_ms=11,
            incomplete=phase_a_incomplete,
        )
        phase_b = PhaseResult(
            reached=False,
            steps=phase_b_steps,
            screenshot=str(after),
            actions=(
                [{"step": 1, "error": "usage accounting is incomplete"}]
                if phase_b_incomplete
                else []
            ),
            elapsed_ms=30,
            grader_ms=13,
            incomplete=phase_b_incomplete,
        )

        gold_session_cm = MagicMock()
        gold_session_cm.__enter__.return_value = MagicMock()
        runtime = MagicMock()
        runtime.page = MagicMock()
        runtime.prng_metadata.return_value = {
            "source": "browser_page_runtime",
            "observed": True,
            "algorithm": "mulberry32",
            "seed": 17,
            "calls": 4,
            "viewport": {"width": 1280, "height": 800},
            "device_scale_factor": 1,
        }
        runtime_cm = MagicMock()
        runtime_cm.__enter__.return_value = runtime
        inspector = MagicMock()
        inspector.inspect.return_value = SimpleNamespace(
            metadata=SimpleNamespace(launch_command="serve --port {port}")
        )
        run_phase = MagicMock(side_effect=[phase_a, phase_b])
        state_session = MagicMock(return_value=gold_session_cm)
        browser_runtime = MagicMock(return_value=runtime_cm)
        verdict = {
            "before": {"label": "correct"},
            "after": {"label": "correct"},
        }

        with (
            patch.object(gui_agent, "resolve_task_repo", return_value=root),
            patch.object(gui_agent, "load_spec", return_value={"assertions": [{"type": "score_increased"}]}),
            patch.object(
                gui_agent,
                "audit_scripted_reference_replay",
                return_value={
                    "status": "success" if reference_reachable else "unsupported",
                    "reference_reachable": reference_reachable,
                    "reason": "test replay",
                },
            ),
            patch.object(gui_agent, "StateSession", state_session),
            patch.object(
                gui_agent,
                "lookup_cached_oracle_goldens",
                return_value=goldens if cached_goldens else None,
            ),
            patch.object(gui_agent, "ensure_oracle_goldens", return_value=goldens),
            patch.object(gui_agent, "RepositoryInspector", return_value=inspector),
            patch.object(gui_agent, "BrowserRuntimeAdapter", browser_runtime),
            patch.object(
                gui_agent,
                "calibrate_browser_coordinates",
                return_value={
                    "passed": True,
                    "viewport": {"width": 1280, "height": 800},
                    "device_scale_factor": 1,
                },
            ),
            patch.object(gui_agent, "_capture_runtime", return_value=str(initial)),
            patch.object(gui_agent, "_run_phase", run_phase),
            patch.object(gui_agent, "profile_for", return_value=MagicMock()),
            patch.object(
                gui_agent,
                "_semantic_start_grade",
                return_value=(
                    before_semantic,
                    "success" if before_semantic else "validate_failed",
                    {},
                ),
            ),
            patch.object(
                gui_agent,
                "_semantic_grade_live",
                return_value=(
                    after_semantic,
                    "success" if after_semantic else "assert_failed",
                    {},
                ),
            ),
            patch.object(gui_agent, "run_visual_pair_audit", return_value=verdict),
        ):
            result = gui_agent._run_agent_task_from_start(
                root,
                task,
                model="glm-5.2@relay",
                base_url="https://relay.example/v1",
                api_key="local-test-key",
                temperature=0.2,
                out_dir=root / "report",
                max_steps=budget,
                headless=True,
                policy_client=MagicMock(),
                observer=MagicMock(),
                seed=17,
            )
        return result, run_phase, state_session, browser_runtime

    def test_verified_golden_cache_hit_skips_hidden_browser_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result, _, state_session, _ = self._run_from_start(
                Path(temp),
                before_semantic=True,
                after_semantic=True,
                cached_goldens=True,
            )

        state_session.assert_not_called()
        self.assertTrue(result["oracle_goldens"]["cache_hit"])
        self.assertEqual(
            result["oracle_goldens"]["fingerprint"], "golden-fingerprint"
        )
        self.assertEqual(
            set(result["oracle_goldens"]["artifacts"]),
            {"before", "after", "metadata"},
        )

    def test_phase_a_and_phase_b_share_one_total_decision_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result, run_phase, state_session, browser_runtime = self._run_from_start(
                Path(temp),
                before_semantic=True,
                after_semantic=True,
            )

        self.assertEqual(
            [call.kwargs["max_steps"] for call in run_phase.call_args_list],
            [10, 3],
        )
        self.assertEqual(result["decision_budget"], 10)
        self.assertEqual(result["steps"], {"phase_a": 7, "phase_b": 3, "total": 10})
        self.assertGreaterEqual(result["grader_ms"], 24)
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(state_session.call_args.kwargs["experiment_seed"], 17)
        self.assertEqual(browser_runtime.call_args.kwargs["experiment_seed"], 17)
        self.assertTrue(result["runtime_started"])
        self.assertTrue(result["policy_phase_started"])
        self.assertTrue(result["visual_grading_started"])
        self.assertTrue(result["seed_evidence"]["observed"])
        self.assertEqual(result["seed_evidence"]["source"], "browser_page_runtime")
        self.assertEqual(result["seed_evidence"]["initial"]["calls"], 4)
        self.assertEqual(result["seed_evidence"]["final"]["calls"], 4)


    def test_exhausted_phase_a_does_not_measure_a_zero_step_phase_b(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result, run_phase, *_ = self._run_from_start(
                Path(temp),
                before_semantic=False,
                after_semantic=False,
                phase_a_steps=10,
                phase_b_steps=0,
                budget=10,
            )

        self.assertEqual(run_phase.call_count, 1)
        self.assertEqual(result["steps"], {"phase_a": 10, "phase_b": 0, "total": 10})
        self.assertEqual(result["time_start_to_goal_ms"], 0)
        self.assertIsNone(result["screenshot_after"])
        self.assertEqual(result["goal_evaluation_status"], "not_evaluated_start_failed")
        self.assertTrue(result["budget_exhausted"])

    def test_incomplete_phase_a_skips_phase_b_and_visual_grading(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result, run_phase, *_ = self._run_from_start(
                Path(temp),
                before_semantic=True,
                after_semantic=True,
                phase_a_incomplete=True,
            )

        self.assertEqual(run_phase.call_count, 1)
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["aborted_after_incomplete_phase"], "phase_a")
        self.assertEqual(result["steps"]["phase_b"], 0)
        self.assertFalse(result["visual_grading_started"])

    def test_incomplete_phase_b_skips_final_visual_grading(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result, run_phase, *_ = self._run_from_start(
                Path(temp),
                before_semantic=True,
                after_semantic=True,
                phase_b_incomplete=True,
            )

        self.assertEqual(run_phase.call_count, 2)
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["aborted_after_incomplete_phase"], "phase_b")
        self.assertTrue(result["visual_grading_started"])
        self.assertTrue(result["start_success"])
        self.assertEqual(result["goal_evaluation_status"], "not_evaluated_phase_b_incomplete")
        self.assertEqual(result["after_visual_status"], "not_evaluated")

    def test_hybrid_flags_require_visual_and_semantic_success_per_half(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            start_semantic_fail, *_ = self._run_from_start(
                Path(temp),
                before_semantic=False,
                after_semantic=True,
            )
        self.assertFalse(start_semantic_fail["before_visual_ok"])
        self.assertFalse(start_semantic_fail["before_semantic_ok"])
        self.assertFalse(start_semantic_fail["start_success"])
        self.assertFalse(start_semantic_fail["goal_success"])
        self.assertFalse(start_semantic_fail["e2e_success"])
        self.assertFalse(start_semantic_fail["visual_only_upper_bound"])
        self.assertEqual(
            start_semantic_fail["visual_only_evaluation_status"],
            "not_evaluated_start_failed",
        )
        self.assertEqual(
            start_semantic_fail["goal_evaluation_status"],
            "not_evaluated_start_failed",
        )
        self.assertIsNotNone(start_semantic_fail["seed_evidence"]["final"])
        self.assertFalse(start_semantic_fail["incomplete"])

        with tempfile.TemporaryDirectory() as temp:
            goal_semantic_fail, *_ = self._run_from_start(
                Path(temp),
                before_semantic=True,
                after_semantic=False,
            )
        self.assertTrue(goal_semantic_fail["start_success"])
        self.assertTrue(goal_semantic_fail["after_visual_ok"])
        self.assertFalse(goal_semantic_fail["after_semantic_ok"])
        self.assertFalse(goal_semantic_fail["goal_success"])
        self.assertFalse(goal_semantic_fail["e2e_success"])
        self.assertTrue(goal_semantic_fail["visual_only_upper_bound"])
        self.assertFalse(goal_semantic_fail["incomplete"])

    def test_successful_oracle_goldens_do_not_claim_reference_reachability(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result, *_ = self._run_from_start(
                Path(temp),
                before_semantic=True,
                after_semantic=True,
                reference_reachable=False,
            )

        self.assertTrue(result["e2e_success"])
        self.assertFalse(result["reference_reachable"])
        self.assertEqual(result["reference_replay"]["status"], "unsupported")


class LuantiSemanticGradingTests(unittest.TestCase):
    def test_after_grade_checks_goal_assertions_without_requiring_start_target(self) -> None:
        session = MagicMock()
        session.fast_restore.return_value = SimpleNamespace(
            status="success", detail="", checks=[]
        )
        spec = {
            "target": {
                "kind": "node",
                "position": [3, 2, 3],
                "name": "default:cobble",
            },
            "assertions": [
                {"type": "node_equals", "position": [3, 2, 3], "name": "air"}
            ],
        }

        ok, status, _detail = gui_agent._luanti_semantic_grade(
            session, spec, after=True
        )

        self.assertTrue(ok)
        self.assertEqual(status, "success")
        called_spec = session.fast_restore.call_args.args[0]
        self.assertEqual(called_spec["target"], {"kind": "world_loaded"})
        self.assertEqual(
            session.fast_restore.call_args.kwargs["assertions"], spec["assertions"]
        )
        self.assertEqual(spec["target"]["name"], "default:cobble")


class QuartzMappingTests(unittest.TestCase):
    def test_console_lock_parser_fails_closed(self) -> None:
        self.assertTrue(
            _console_lock_state_from_ioreg(
                '"IOConsoleLocked" = No\n"CGSSessionScreenIsLocked"=Yes'
            )
        )
        self.assertFalse(
            _console_lock_state_from_ioreg(
                '"IOConsoleLocked" = No\n"CGSSessionScreenIsLocked"=No'
            )
        )
        self.assertIsNone(_console_lock_state_from_ioreg("unrelated"))

    def test_native_session_gate_checks_lock_before_permissions(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout='"IOConsoleLocked" = Yes',
        )
        with (
            patch(
                "warptest.native_input.os.uname",
                return_value=SimpleNamespace(sysname="Darwin"),
            ),
            patch("warptest.native_input.subprocess.run", return_value=completed),
            patch("warptest.native_input.ctypes.cdll.LoadLibrary") as load,
            self.assertRaisesRegex(QuartzInputError, "console session is locked"),
        ):
            require_unlocked_native_session()
        load.assert_not_called()

    def test_frame_pixels_map_into_content_bounds_without_native_calls(self) -> None:
        native = object.__new__(QuartzNativeInput)
        native.pid = 123
        native.frame_width = 1280
        native.frame_height = 720
        bounds = WindowBounds(
            x=100,
            y=20,
            width=640,
            height=390,
            content_x=100,
            content_y=50,
            content_width=640,
            content_height=360,
        )

        with patch.object(native, "window_bounds", return_value=bounds) as window_bounds:
            self.assertEqual(native.map_point(640, 360), (420.0, 230.0))
        window_bounds.assert_called_once_with()

        # Source coordinates are clamped to the captured frame before scaling.
        self.assertEqual(native.map_point(-100, -5, bounds=bounds), (100.0, 50.0))
        self.assertAlmostEqual(native.map_point(9999, 9999, bounds=bounds)[0], 739.5)
        self.assertAlmostEqual(native.map_point(9999, 9999, bounds=bounds)[1], 409.5)

    def test_click_moves_and_preserves_engine_visible_press_interval(self) -> None:
        native = object.__new__(QuartzNativeInput)
        native.focus = MagicMock()
        native.map_point = MagicMock(return_value=(756.0, 506.0))
        native._mouse_event = MagicMock()

        with patch("warptest.native_input.time.sleep") as sleep:
            native.click(640, 390)

        native.focus.assert_called_once_with()
        native.map_point.assert_called_once_with(640, 390)
        self.assertEqual(
            native._mouse_event.call_args_list,
            [
                mock_call(5, 756.0, 506.0),
                mock_call(1, 756.0, 506.0, button=0),
                mock_call(2, 756.0, 506.0, button=0),
            ],
        )
        self.assertEqual(
            sleep.call_args_list,
            [
                mock_call(QuartzNativeInput._MOUSE_SETTLE_S),
                mock_call(QuartzNativeInput._MOUSE_PRESS_S),
            ],
        )

    def test_type_text_posts_physical_ascii_key_down_and_up_events(self) -> None:
        native = object.__new__(QuartzNativeInput)
        native.focus = MagicMock()
        native._press_key = MagicMock()

        native.type_text("Ab_:/ 09?\n")

        native.focus.assert_called_once_with()
        self.assertEqual(
            native._press_key.call_args_list,
            [
                mock_call(0, flags=QuartzNativeInput._SHIFT_FLAG, text="A"),
                mock_call(11, flags=0, text="b"),
                mock_call(27, flags=QuartzNativeInput._SHIFT_FLAG, text="_"),
                mock_call(41, flags=QuartzNativeInput._SHIFT_FLAG, text=":"),
                mock_call(44, flags=0, text="/"),
                mock_call(49, flags=0, text=" "),
                mock_call(29, flags=0, text="0"),
                mock_call(25, flags=0, text="9"),
                mock_call(44, flags=QuartzNativeInput._SHIFT_FLAG, text="?"),
                mock_call(36, flags=0, text="\n"),
            ],
        )

    def test_shifted_text_posts_explicit_modifier_down_and_up(self) -> None:
        native = object.__new__(QuartzNativeInput)
        native._key_event = MagicMock()

        with patch("warptest.native_input.time.sleep") as sleep:
            native._press_key(0, flags=QuartzNativeInput._SHIFT_FLAG)

        self.assertEqual(
            native._key_event.call_args_list,
            [
                mock_call(56, True, flags=QuartzNativeInput._SHIFT_FLAG),
                mock_call(0, True, flags=QuartzNativeInput._SHIFT_FLAG),
                mock_call(0, False, flags=QuartzNativeInput._SHIFT_FLAG),
                mock_call(56, False, flags=0),
            ],
        )
        self.assertEqual(
            sleep.call_args_list,
            [
                mock_call(QuartzNativeInput._KEY_INTERVAL_S),
                mock_call(QuartzNativeInput._KEY_PRESS_S),
                mock_call(QuartzNativeInput._KEY_INTERVAL_S),
                mock_call(QuartzNativeInput._KEY_INTERVAL_S),
            ],
        )

    def test_text_key_down_carries_one_character_unicode_payload(self) -> None:
        native = object.__new__(QuartzNativeInput)
        native._cg = MagicMock()
        native._cg.CGEventCreateKeyboardEvent.return_value = 123

        native._key_event(
            0,
            True,
            flags=QuartzNativeInput._SHIFT_FLAG,
            text="A",
        )

        native._cg.CGEventSetFlags.assert_called_once_with(
            123,
            QuartzNativeInput._SHIFT_FLAG,
        )
        unicode_call = native._cg.CGEventKeyboardSetUnicodeString.call_args
        self.assertEqual(unicode_call.args[0:2], (123, 1))
        self.assertEqual(list(unicode_call.args[2]), [ord("A")])
        native._cg.CGEventPost.assert_called_once_with(0, 123)
        native._cg.CFRelease.assert_called_once_with(123)

    def test_type_text_rejects_unmapped_unicode_instead_of_typing_a(self) -> None:
        native = object.__new__(QuartzNativeInput)
        native.focus = MagicMock()
        native._press_key = MagicMock()

        with self.assertRaisesRegex(
            QuartzInputError,
            "unsupported native text character at index 3",
        ):
            native.type_text("abcé")

        native.focus.assert_not_called()
        native._press_key.assert_not_called()

    def test_key_validates_modifiers_before_focus_and_supports_safe_navigation(self) -> None:
        native = object.__new__(QuartzNativeInput)
        native.focus = MagicMock()
        native._press_key = MagicMock()

        with self.assertRaisesRegex(QuartzInputError, "unsupported native key modifier"):
            native.key("a", modifiers=("hyper",))
        native.focus.assert_not_called()

        native.key("Delete")
        native.focus.assert_called_once_with()
        native._press_key.assert_called_once_with(117, flags=0)

    def test_native_validation_and_unavailability_have_distinct_retry_classes(self) -> None:
        self.assertFalse(
            gui_agent._is_infrastructure_error(
                NativeActionValidationError("unsupported native text character")
            )
        )
        self.assertTrue(
            gui_agent._is_infrastructure_error(
                NativeInputUnavailableError("native input target disappeared")
            )
        )

    def test_luanti_overwrite_and_global_modifiers_fail_closed(self) -> None:
        runtime = object.__new__(gui_agent.LuantiGuiRuntime)
        runtime.native = MagicMock()

        with self.assertRaisesRegex(
            NativeActionValidationError,
            "do not support overwrite",
        ):
            runtime.apply(
                gui_agent.AgentAction(
                    kind="type",
                    text="replacement",
                    overwrite=True,
                    enter=True,
                )
            )
        self.assertEqual(runtime.native.method_calls, [])

        runtime.native.reset_mock()
        with self.assertRaisesRegex(
            NativeActionValidationError,
            "only the Shift modifier",
        ):
            runtime.apply(
                gui_agent.AgentAction(
                    kind="key",
                    key="a",
                    modifiers=("Meta",),
                )
            )
        runtime.native.key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
