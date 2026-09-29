from __future__ import annotations

import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from warptest.providers import (
    DEFAULT_GATEWAY_BASE_URL,
    ProviderError,
    available_providers,
    resolve_model,
)
from warptest.synthesis import OpenAICompatibleChatClient, SynthesisError


DEFAULT_BASE_URL = "https://api-inference.modelscope.cn/v1"
DEFAULT_KEY_ENV = "MODELSCOPE_API_KEY"


class ResolveModelTests(unittest.TestCase):
    def test_untagged_uses_default_provider(self) -> None:
        model, provider = resolve_model(
            "deepseek-ai/DeepSeek-V4-Flash",
            default_base_url=DEFAULT_BASE_URL,
            default_api_key_env=DEFAULT_KEY_ENV,
        )
        self.assertEqual(model, "deepseek-ai/DeepSeek-V4-Flash")
        self.assertEqual(provider.name, "default")
        self.assertEqual(provider.base_url, DEFAULT_BASE_URL)
        self.assertEqual(provider.api_key_env, DEFAULT_KEY_ENV)
        self.assertEqual(provider.extra_headers, {})

    def test_modelscope_dashscope_suffix_preserved(self) -> None:
        # The ``:DashScope`` suffix is ModelScope-internal routing and must stay
        # inside the model id, using the default provider.
        model, provider = resolve_model(
            "deepseek-ai/DeepSeek-V4-Flash:DashScope",
            default_base_url=DEFAULT_BASE_URL,
            default_api_key_env=DEFAULT_KEY_ENV,
        )
        self.assertEqual(model, "deepseek-ai/DeepSeek-V4-Flash:DashScope")
        self.assertEqual(provider.name, "default")
        self.assertEqual(provider.base_url, DEFAULT_BASE_URL)

    def test_gateway_tag_routes_to_gateway(self) -> None:
        with patch.dict(
            "os.environ",
            {"GATEWAY_END_USER": "u-123", "GATEWAY_CUSTOM_SOURCE": "warptest"},
            clear=False,
        ):
            model, provider = resolve_model(
                "gpt-5.4@gateway",
                default_base_url=DEFAULT_BASE_URL,
                default_api_key_env=DEFAULT_KEY_ENV,
            )
        self.assertEqual(model, "gpt-5.4")
        self.assertEqual(provider.name, "gateway")
        self.assertEqual(provider.base_url, DEFAULT_GATEWAY_BASE_URL)
        self.assertEqual(provider.api_key_env, "GATEWAY_API_KEY")
        self.assertEqual(provider.extra_headers["X-End-User"], "u-123")
        self.assertEqual(provider.extra_headers["X-Custom-Source"], "warptest")

    def test_gateway_base_url_env_override(self) -> None:
        with patch.dict("os.environ", {"GATEWAY_BASE_URL": "https://gateway.example/v1"}, clear=False):
            _, provider = resolve_model(
                "claude-opus-4-7@gateway",
                default_base_url=DEFAULT_BASE_URL,
                default_api_key_env=DEFAULT_KEY_ENV,
            )
        self.assertEqual(provider.base_url, "https://gateway.example/v1")

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaises(ProviderError):
            resolve_model(
                "gpt-5.4@nope",
                default_base_url=DEFAULT_BASE_URL,
                default_api_key_env=DEFAULT_KEY_ENV,
            )

    def test_empty_model_before_tag_raises(self) -> None:
        with self.assertRaises(ProviderError):
            resolve_model(
                "@gateway",
                default_base_url=DEFAULT_BASE_URL,
                default_api_key_env=DEFAULT_KEY_ENV,
            )

    def test_available_providers_lists_known(self) -> None:
        providers = available_providers()
        self.assertIn("relay", providers)
        self.assertIn("modelscope", providers)
        self.assertIn("gateway", providers)

    def test_relay_tag_uses_gateway_and_end_user_header(self) -> None:
        with patch.dict(
            os.environ,
            {
                "RELAY_BASE_URL": "https://relay.example/v1",
                "RELAY_END_USER": "researcher-1",
            },
            clear=False,
        ):
            model, provider = resolve_model(
                "claude-sonnet-5@relay",
                default_base_url=DEFAULT_BASE_URL,
                default_api_key_env=DEFAULT_KEY_ENV,
            )

        self.assertEqual(model, "claude-sonnet-5")
        self.assertEqual(provider.name, "relay")
        self.assertEqual(provider.base_url, "https://relay.example/v1")
        self.assertEqual(provider.api_key_env, "RELAY_API_KEY")
        self.assertEqual(provider.extra_headers, {"X-End-User": "researcher-1"})


class ClientHeaderTests(unittest.TestCase):
    def test_extra_headers_reach_openai_client(self) -> None:
        fake_openai = MagicMock()
        with patch("openai.OpenAI", fake_openai):
            OpenAICompatibleChatClient(
                api_key="secret",
                base_url=DEFAULT_GATEWAY_BASE_URL,
                extra_headers={"X-End-User": "u-1"},
            )
        self.assertTrue(fake_openai.called)
        _, kwargs = fake_openai.call_args
        self.assertEqual(kwargs.get("default_headers"), {"X-End-User": "u-1"})

    def test_synthesis_client_honors_timeout_and_retry_env(self) -> None:
        fake_openai = MagicMock()
        with (
            patch.dict(
                os.environ,
                {
                    "WARPTEST_SYNTHESIS_TIMEOUT": "42.5",
                    "WARPTEST_SYNTHESIS_MAX_RETRIES": "0",
                },
                clear=False,
            ),
            patch("openai.OpenAI", fake_openai),
        ):
            OpenAICompatibleChatClient(api_key="secret", base_url=DEFAULT_BASE_URL)

        _, kwargs = fake_openai.call_args
        self.assertEqual(kwargs["timeout"], 42.5)
        self.assertEqual(kwargs["max_retries"], 0)

    def test_synthesis_stream_wall_clock_timeout_interrupts_hung_iterator(self) -> None:
        class HangingCompletions:
            def create(self, **_kwargs):
                def _stream():
                    time.sleep(1.0)
                    yield SimpleNamespace(choices=[])

                return _stream()

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=HangingCompletions()))
        with patch.dict(os.environ, {"WARPTEST_SYNTHESIS_TIMEOUT": "0.01"}, clear=False):
            client = OpenAICompatibleChatClient(api_key="secret", base_url=DEFAULT_BASE_URL, client=fake_client)

        with self.assertRaises(SynthesisError) as caught:
            client.converse("model/a", [{"role": "user", "content": "prompt"}], outage_budget_s=0.001)
        self.assertIn("timed out", str(caught.exception))

    def test_no_extra_headers_omits_default_headers(self) -> None:
        fake_openai = MagicMock()
        with patch("openai.OpenAI", fake_openai):
            OpenAICompatibleChatClient(api_key="secret", base_url=DEFAULT_BASE_URL)
        _, kwargs = fake_openai.call_args
        self.assertNotIn("default_headers", kwargs)


if __name__ == "__main__":
    unittest.main()
