from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from warptest import cost
from warptest.agent_s_bridge import (
    AgentSBridgeConfig,
    _worker_environment,
)
from warptest.model_request import (
    MODEL_REQUEST_PROFILES_ENV,
    ModelRequestProfileError,
    canonical_profiles_json,
    model_request_profiles_environment,
    parse_model_request_profiles,
    resolve_model_request_profile,
)
from warptest.synthesis import (
    ALLOW_INCOMPLETE_USAGE_ENV,
    OpenAICompatibleChatClient,
)


KIMI = "kimi-k2.6"
DISABLED_ENV = json.dumps({KIMI: "thinking_disabled"})
DISABLED_BODY = {"thinking": {"type": "disabled"}}


def _usage(*, prompt: int = 3, completion: int = 2, reasoning: int = 0):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )


def _stream(
    content: str,
    *,
    reasoning_content: str | None = None,
    reasoning_tokens: int = 0,
):
    return iter(
        [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=content,
                            reasoning_content=reasoning_content,
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[],
                usage=_usage(reasoning=reasoning_tokens),
            ),
        ]
    )


def _response(
    content: str,
    *,
    reasoning_content: str | None = None,
    reasoning_tokens: int = 0,
):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    reasoning_content=reasoning_content,
                ),
                finish_reason="stop",
            )
        ],
        usage=_usage(reasoning=reasoning_tokens),
    )


class _RecordingCompletions:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _client(completions: _RecordingCompletions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


class ModelRequestProfileTests(unittest.TestCase):
    def test_strict_profile_and_canonical_provenance(self) -> None:
        with patch.dict(
            os.environ,
            {MODEL_REQUEST_PROFILES_ENV: '{"kimi-k2.6@relay":"thinking_disabled"}'},
            clear=False,
        ):
            profile = resolve_model_request_profile("kimi-k2.6@relay")
            same = resolve_model_request_profile("kimi-k2.6")

        self.assertEqual(profile.request_kwargs(), {"extra_body": DISABLED_BODY})
        self.assertEqual(profile.provenance(), same.provenance())
        self.assertEqual(len(profile.fingerprint), 64)
        self.assertEqual(
            profile.provenance()["request_overrides"],
            {"extra_body": DISABLED_BODY},
        )

    def test_thinking_disabled_uses_model_compatible_request_syntax(self) -> None:
        profiles = {
            "gpt-5.6-terra": "thinking_disabled",
            "gpt-5.6-luna": "thinking_disabled",
            "gemini-3.5-flash": "thinking_disabled",
            "kimi-k2.6": "thinking_disabled",
        }
        with model_request_profiles_environment(profiles):
            gpt = resolve_model_request_profile("gpt-5.6-terra@relay")
            luna = resolve_model_request_profile("gpt-5.6-luna@relay")
            gemini = resolve_model_request_profile("gemini-3.5-flash@relay")
            kimi = resolve_model_request_profile("kimi-k2.6@relay")

        for profile in (gpt, luna):
            self.assertEqual(profile.request_kwargs(), {"reasoning_effort": "none"})
            self.assertEqual(
                profile.provenance()["request_overrides"],
                {"reasoning_effort": "none"},
            )
        self.assertFalse(luna.reasoning_expected)
        for profile in (gemini, kimi):
            self.assertEqual(profile.request_kwargs(), {"extra_body": DISABLED_BODY})
            self.assertEqual(
                profile.provenance()["request_overrides"],
                {"extra_body": DISABLED_BODY},
            )
        self.assertFalse(gpt.reasoning_expected)
        self.assertFalse(gemini.reasoning_expected)
        self.assertFalse(kimi.reasoning_expected)

    def test_reasoning_medium_profiles_use_openai_reasoning_effort(self) -> None:
        profiles = {
            "gpt-5.6-terra": "reasoning_medium",
            "gemini-3.5-flash": "reasoning_medium",
            "kimi-k2.6": "reasoning_medium",
        }
        with model_request_profiles_environment(profiles):
            for model in profiles:
                profile = resolve_model_request_profile(f"{model}@relay")
                self.assertEqual(profile.profile, "reasoning_medium")
                self.assertEqual(
                    profile.request_kwargs(), {"reasoning_effort": "medium"}
                )
                self.assertEqual(
                    profile.provenance()["request_overrides"],
                    {"reasoning_effort": "medium"},
                )

    def test_reasoning_high_profile_is_strict_and_reasoning_expected(self) -> None:
        with model_request_profiles_environment({"kimi-k3": "reasoning_high"}):
            profile = resolve_model_request_profile("kimi-k3@relay")

        self.assertEqual(profile.profile, "reasoning_high")
        self.assertEqual(profile.request_kwargs(), {"reasoning_effort": "high"})
        self.assertTrue(profile.reasoning_expected)
        self.assertEqual(
            profile.provenance()["request_overrides"],
            {"reasoning_effort": "high"},
        )

    def test_other_models_remain_provider_default(self) -> None:
        with patch.dict(
            os.environ,
            {MODEL_REQUEST_PROFILES_ENV: DISABLED_ENV},
            clear=False,
        ):
            profile = resolve_model_request_profile("glm-5.2")
        self.assertEqual(profile.profile, "provider_default")
        self.assertEqual(profile.request_kwargs(), {})

    def test_invalid_or_ambiguous_profile_configuration_is_rejected(self) -> None:
        invalid = (
            "[]",
            '{"kimi-k2.6":"auto"}',
            '{"kimi-k2.6":{"extra_body":{}}}',
            '{"kimi-k2.6":"thinking_disabled","kimi-k2.6@relay":"provider_default"}',
            '{"kimi-k2.6":"thinking_disabled","kimi-k2.6":"provider_default"}',
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ModelRequestProfileError):
                parse_model_request_profiles(raw)

    def test_profile_is_canonicalized_before_crossing_agent_s_boundary(self) -> None:
        config = AgentSBridgeConfig(
            model=KIMI,
            base_url="https://example.invalid/v1",
            api_key_env="WORKER_KEY",
            grounder_model="claude-sonnet-5",
            grounder_base_url="https://example.invalid/v1",
            grounder_api_key_env="GROUNDER_KEY",
        )
        with patch.dict(
            os.environ,
            {
                "WORKER_KEY": "worker-secret",
                "GROUNDER_KEY": "grounder-secret",
                MODEL_REQUEST_PROFILES_ENV: '{ "kimi-k2.6" : "thinking_disabled" }',
            },
            clear=False,
        ):
            env = _worker_environment(config, Path("/venv/bin/python"))
        self.assertEqual(
            env[MODEL_REQUEST_PROFILES_ENV],
            canonical_profiles_json({KIMI: "thinking_disabled"}),
        )

    def test_profile_environment_restores_absent_state_after_exception(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                with model_request_profiles_environment(
                    {"kimi-k2.6@relay": "thinking_disabled"}
                ):
                    self.assertEqual(
                        os.environ[MODEL_REQUEST_PROFILES_ENV],
                        '{"kimi-k2.6":"thinking_disabled"}',
                    )
                    raise RuntimeError("stop")
            self.assertNotIn(MODEL_REQUEST_PROFILES_ENV, os.environ)

    def test_profile_environment_restores_present_value_byte_for_byte(self) -> None:
        original = '{ "glm-5.2" : "provider_default" }'
        with patch.dict(
            os.environ,
            {MODEL_REQUEST_PROFILES_ENV: original},
            clear=False,
        ):
            with model_request_profiles_environment(None):
                self.assertNotIn(MODEL_REQUEST_PROFILES_ENV, os.environ)
            self.assertEqual(os.environ[MODEL_REQUEST_PROFILES_ENV], original)


class SynthesisRequestProfileTests(unittest.TestCase):
    def _run(self, model: str, stream: object):
        completions = _RecordingCompletions([stream])
        client = OpenAICompatibleChatClient(
            api_key="unused",
            client=_client(completions),
            cost_kind="checkpoint_synthesis",
        )
        with cost.scoped_meter():
            result = client.converse(
                model,
                [{"role": "user", "content": "reply briefly"}],
            )
        return result, completions, client

    def test_kimi_synthesis_carries_disabled_thinking_other_models_do_not(self) -> None:
        with patch.dict(
            os.environ,
            {MODEL_REQUEST_PROFILES_ENV: DISABLED_ENV},
            clear=False,
        ):
            kimi_result, kimi, client = self._run(KIMI, _stream("ok"))
            glm_result, glm, _other = self._run("glm-5.2", _stream("ok"))

        self.assertEqual(kimi_result.content, "ok")
        self.assertEqual(glm_result.content, "ok")
        self.assertEqual(kimi.calls[0]["extra_body"], DISABLED_BODY)
        self.assertNotIn("extra_body", glm.calls[0])
        self.assertEqual(
            client._last_request_profile["profile"], "thinking_disabled"
        )

    def test_reasoning_high_rejection_never_downgrades_or_retries(self) -> None:
        class Rejected(RuntimeError):
            status_code = 400

        completions = _RecordingCompletions([])

        def reject(**kwargs):
            completions.calls.append(kwargs)
            raise Rejected("reasoning_effort is unsupported")

        completions.create = reject
        client = OpenAICompatibleChatClient(
            api_key="unused",
            client=_client(completions),
            cost_kind="checkpoint_synthesis",
        )
        with model_request_profiles_environment({"kimi-k3": "reasoning_high"}):
            with self.assertRaises(Rejected):
                client._open_stream(
                    "kimi-k3",
                    [{"role": "user", "content": "reply"}],
                )

        self.assertEqual(len(completions.calls), 1)
        self.assertEqual(completions.calls[0]["reasoning_effort"], "high")
        self.assertFalse(client._omit_reasoning_effort)

    def test_gemini_synthesis_uses_exact_non_streaming_usage(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=5,
            completion_tokens=3,
            total_tokens=8,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        )
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="ok",
                        reasoning_content=None,
                    ),
                    finish_reason="stop",
                )
            ],
            usage=usage,
        )
        completions = _RecordingCompletions([response])
        client = OpenAICompatibleChatClient(
            api_key="unused",
            client=_client(completions),
            cost_kind="checkpoint_synthesis",
        )

        with (
            model_request_profiles_environment(
                {"gemini-3.5-flash": "reasoning_medium"}
            ),
            cost.scoped_meter() as meter,
        ):
            result = client.converse(
                "gemini-3.5-flash",
                [{"role": "user", "content": "reply briefly"}],
            )

        self.assertEqual(result.content, "ok")
        self.assertEqual(result.total_tokens, 8)
        self.assertTrue(meter.summary()["usage_complete"])
        self.assertEqual(meter.summary()["total_tokens"], 8)
        self.assertNotIn("stream", completions.calls[0])
        self.assertNotIn("stream_options", completions.calls[0])
        self.assertEqual(completions.calls[0]["reasoning_effort"], "medium")

    def test_gpt56_terra_synthesis_uses_non_streaming_usage_under_concurrency(self) -> None:
        completions = _RecordingCompletions(
            [_response("ok", reasoning_content="private", reasoning_tokens=1)]
        )
        client = OpenAICompatibleChatClient(
            api_key="unused",
            client=_client(completions),
            cost_kind="checkpoint_exploration",
        )

        with (
            model_request_profiles_environment(
                {"gpt-5.6-terra": "reasoning_medium"}
            ),
            cost.scoped_meter() as meter,
        ):
            result = client.converse(
                "gpt-5.6-terra",
                [{"role": "user", "content": "reply briefly"}],
            )

        self.assertEqual(result.content, "ok")
        self.assertTrue(meter.summary()["usage_complete"])
        self.assertNotIn("stream", completions.calls[0])
        self.assertNotIn("stream_options", completions.calls[0])
        self.assertEqual(completions.calls[0]["reasoning_effort"], "medium")

    def test_reasoning_observed_with_zero_breakout_is_bounded(self) -> None:
        completions = _RecordingCompletions(
            [_stream("ok", reasoning_content="private", reasoning_tokens=0)]
        )
        client = OpenAICompatibleChatClient(
            api_key="unused",
            client=_client(completions),
            cost_kind="checkpoint_synthesis",
        )
        with (
            patch.dict(
                os.environ,
                {MODEL_REQUEST_PROFILES_ENV: DISABLED_ENV},
                clear=False,
            ),
            cost.scoped_meter() as meter,
        ):
            result = client.converse(KIMI, [{"role": "user", "content": "go"}])

        self.assertEqual(result.content, "ok")
        summary = meter.summary()
        self.assertTrue(summary["usage_complete"])
        self.assertFalse(summary["reasoning_detail_complete"])
        self.assertIsNone(summary["reasoning_tokens"])
        self.assertEqual(completions.calls[0]["extra_body"], DISABLED_BODY)

    def test_incomplete_usage_can_be_allowed_for_fuzzing(self) -> None:
        completions = _RecordingCompletions(
            [_stream("ok", reasoning_content="private", reasoning_tokens=0)]
        )
        client = OpenAICompatibleChatClient(
            api_key="unused",
            client=_client(completions),
            cost_kind="checkpoint_synthesis",
        )
        with (
            patch.dict(
                os.environ,
                {
                    MODEL_REQUEST_PROFILES_ENV: DISABLED_ENV,
                    ALLOW_INCOMPLETE_USAGE_ENV: "1",
                },
                clear=False,
            ),
            cost.scoped_meter() as meter,
        ):
            result = client.converse(KIMI, [{"role": "user", "content": "go"}])

        self.assertEqual(result.content, "ok")
        self.assertEqual(result.total_tokens, 5)
        summary = meter.summary()
        self.assertTrue(summary["usage_complete"])
        self.assertFalse(summary["reasoning_detail_complete"])


if __name__ == "__main__":
    unittest.main()
