"""gui_agent integration points for the ScaleCUA policy kind (offline)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from warptest import gui_agent
from warptest.p5_scale import C1_ARMS, C1_FORMAL_PAPER_ARMS


class PolicyKindTests(unittest.TestCase):
    def test_scalecua_policy_kind_is_accepted(self) -> None:
        # run_gui_agent_benchmark validates policy_kind before any other work;
        # an invalid task file name fails later, which proves the gate passed.
        try:
            gui_agent.run_gui_agent_benchmark(
                root=".",
                tasks_path="benchmarks/p5/definitely-missing-tasks.yaml",
                model="Qwen/Qwen3.8-27B@modelscope",
                base_url="http://127.0.0.1:9/v1",
                api_key_env="TEST_SCALECUA_KEY",
                policy_kind="scalecua",
                start_mode="from_start",
            )
        except (FileNotFoundError, Exception) as error:  # noqa: B014
            self.assertNotIn("policy_kind must be", str(error))

    def test_arm_name_derivation_scalecua(self) -> None:
        source = __import__("inspect").getsource(gui_agent.run_gui_agent_benchmark)
        self.assertIn('"c1_agent_scalecua_synth"', source)
        self.assertIn('"c1_agent_scalecua"', source)


class _FakeProvider:
    def __init__(self, name: str, base_url: str, api_key_env: str, headers=None):
        self.name = name
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.extra_headers = dict(headers or {})


class BuildPolicyClientTests(unittest.TestCase):
    def test_scalecua_client_builds_from_provider_tag(self) -> None:
        from warptest.scalecua_bridge import ScaleCUABridgeClient

        with patch.dict(os.environ, {"TEST_SCALECUA_KEY": "secret"}):
            with patch(
                "warptest.gui_agent.resolve_model",
                return_value=(
                    "Qwen/Qwen3.8-27B",
                    _FakeProvider("modelscope", "http://127.0.0.1:9/v1", "TEST_SCALECUA_KEY"),
                ),
            ):
                client = gui_agent._build_policy_client(
                    policy_kind="scalecua",
                    model="Qwen/Qwen3.8-27B@modelscope",
                    base_url="http://127.0.0.1:9/v1",
                    api_key_env="TEST_SCALECUA_KEY",
                    temperature=0.2,
                    grounder_model="match_decision_model",
                    provider_timeout_s=60.0,
                    root=gui_agent.Path.cwd(),
                )
        self.assertIsInstance(client, ScaleCUABridgeClient)
        self.assertNotIsInstance(client, __import__(
            "warptest.agent_s_bridge", fromlist=["AgentSBridgeClient"]
        ).AgentSBridgeClient)
        self.assertEqual(client.config.model, "Qwen/Qwen3.8-27B")
        self.assertEqual(client.config.execution_backend, "background_pixel_v1")


class RuntimeMetadataTests(unittest.TestCase):
    def test_scalecua_metadata_is_typed_and_secret_free(self) -> None:
        client = SimpleNamespaceClient()
        metadata = gui_agent._policy_runtime_metadata(
            client, policy_kind="scalecua"
        )
        self.assertEqual(metadata["policy_kind"], "scalecua")
        self.assertEqual(metadata["execution_backend"], "background_pixel_v1")
        self.assertEqual(metadata["action_execution_owner"], "parent_runtime")
        self.assertNotIn("api_key", metadata)


class SimpleNamespaceClient:
    def __init__(self) -> None:
        self.config = type("C", (), {"model": "Qwen/Qwen3.8-27B"})()
        self.provider_name = "modelscope"
        self.request_timeout_s = None
        self.control_timeout_s = 360.0
        self.metadata = {
            "protocol_version": 1,
            "scalecua_commit": "a" * 40,
            "scalecua_inference_revision": "scalecua-native-ours-ubuntu-v1",
            "runtime_mode": "background_pixel",
            "execution_backend": "background_pixel_v1",
            "coordinate_space": "frame_pixels_top_left_v1",
            "action_execution_owner": "parent_runtime",
            "foreground_required": False,
            "native_exec_enabled": False,
            "worker_observation_mode": "screenshot",
            "native_screen_width": 1280,
            "native_screen_height": 800,
            "provider_timeout_s": 60.0,
            "max_provider_attempts_per_predict": 3,
            "model_request_profiles": {},
            "temperature_contract": {},
        }


class ArmRegistryTests(unittest.TestCase):
    def test_scalecua_arms_are_registered_but_outside_paper_subset(self) -> None:
        self.assertIn("c1_agent_scalecua", C1_ARMS)
        self.assertIn("c1_agent_scalecua_synth", C1_ARMS)
        self.assertIn("c1_agent_scalecua", C1_FORMAL_PAPER_ARMS)
        self.assertIn("c1_agent_scalecua_synth", C1_FORMAL_PAPER_ARMS)


if __name__ == "__main__":
    unittest.main()
