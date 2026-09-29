from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock, patch

from warptest import p5_scale
from warptest.agent_s_bridge import AgentSBridgeConfig
from warptest.synthesis import OpenAICompatibleChatClient
from warptest.visual import VisualJudgeClient
from warptest.visual_bridge import FixedVisualBridge


def test_c1_provider_clients_disable_opaque_sdk_retries_by_default() -> None:
    factory = Mock(return_value=Mock())
    env = {
        "RELAY_API_KEY": "test-secret-value",
        "RELAY_BASE_URL": "https://provider.invalid/v1",
    }
    with patch.dict(os.environ, env, clear=True), patch("openai.OpenAI", factory):
        FixedVisualBridge(
            model="observer@relay",
            base_url=env["RELAY_BASE_URL"],
            api_key_env="RELAY_API_KEY",
        )
        OpenAICompatibleChatClient(
            api_key=env["RELAY_API_KEY"],
            base_url=env["RELAY_BASE_URL"],
        )
        VisualJudgeClient(
            api_key=env["RELAY_API_KEY"],
            base_url=env["RELAY_BASE_URL"],
            model="judge",
        )

    assert [call.kwargs["max_retries"] for call in factory.call_args_list] == [0, 0, 0]
    assert AgentSBridgeConfig(
        model="worker",
        base_url=env["RELAY_BASE_URL"],
        api_key_env="RELAY_API_KEY",
        grounder_model="grounder",
        grounder_base_url=env["RELAY_BASE_URL"],
        grounder_api_key_env="RELAY_API_KEY",
    ).max_retries == 0


def test_formal_c1_forces_single_shot_transports_and_restores_environment() -> None:
    names = (
        "WARPTEST_VISUAL_BRIDGE_MAX_RETRIES",
        "WARPTEST_GUI_AGENT_MAX_RETRIES",
        "WARPTEST_SYNTHESIS_MAX_RETRIES",
        "WARPTEST_VISUAL_JUDGE_MAX_RETRIES",
    )
    ambient = {name: "7" for name in names}
    with patch.dict(os.environ, ambient, clear=False):
        with p5_scale._formal_c1_cache_env(Path("formal-cache")):
            assert {os.environ[name] for name in names} == {"0"}
        assert {os.environ[name] for name in names} == {"7"}
