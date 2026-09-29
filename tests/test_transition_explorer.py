from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from warptest.cli import main
from warptest.runner import load_spec
from warptest.synthesis import ModelCallResult
from warptest.transition_explorer import (
    build_transition_action_prompt,
    llm_candidate_actions_for_spec,
    parse_llm_action_output,
    state_fingerprint,
)


ROOT = Path(__file__).resolve().parents[1]


class TransitionExplorerTests(unittest.TestCase):
    def test_llm_candidate_actions_use_model_output(self) -> None:
        spec = load_spec(ROOT / "specs/2048/merge-left.yaml")

        class FakeClient:
            def converse(self, model: str, messages: list[dict[str, str]], max_attempts: int = 3) -> ModelCallResult:
                self.model = model
                self.messages = messages
                self.max_attempts = max_attempts
                return ModelCallResult(content='{"actions":[{"type":"move","direction":"up","wait_ms":123}]}', latency_ms=7)

        fake = FakeClient()
        actions, plan = llm_candidate_actions_for_spec(
            spec,
            "2048",
            {"fields": []},
            max_actions=4,
            model="fake/model",
            base_url="http://model.test/v1",
            api_key_env="UNUSED",
            client=fake,
        )
        self.assertEqual(actions, [{"type": "move", "direction": "up", "wait_ms": 123}])
        self.assertEqual(plan["source"], "llm")
        self.assertEqual(plan["model"], "fake/model")
        self.assertIn("allowed_action_schema", fake.messages[1]["content"])

    def test_llm_action_parser_filters_invalid_actions(self) -> None:
        actions, invalid = parse_llm_action_output(
            '{"actions":[{"type":"move","direction":"left"},{"type":"move","direction":"sideways"}]}',
            benchmark="2048",
            max_actions=8,
        )
        self.assertEqual(actions, [{"type": "move", "direction": "left"}])
        self.assertEqual(len(invalid), 1)

    def test_transition_prompt_includes_spec_actions_as_context(self) -> None:
        spec = load_spec(ROOT / "specs/2048/merge-left.yaml")
        prompt = build_transition_action_prompt(spec, "2048", {"fields": []}, max_actions=8)
        self.assertIn("Use the spec's existing actions only as examples", prompt)
        self.assertIn("direction: left", prompt)

    def test_state_fingerprint_is_stable_for_key_order(self) -> None:
        self.assertEqual(state_fingerprint({"b": 2, "a": 1}), state_fingerprint({"a": 1, "b": 2}))

    def test_cli_dispatches_explore_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "report"
            fake_result = {
                "report_dir": str(out_dir),
                "benchmark": "2048",
                "summary": {"actions_considered": 3, "nodes": 1, "edges": 3},
            }
            with (
                patch("warptest.cli.new_report_dir", return_value=out_dir),
                patch("warptest.cli.explore_transitions", return_value=fake_result) as explore,
                redirect_stdout(StringIO()) as stdout,
            ):
                main(
                    [
                        "explore-transitions",
                        "specs/2048/merge-left.yaml",
                        "--max-actions",
                        "3",
                        "--horizon",
                        "1",
                        "--headed",
                        "--model",
                        "fake/model",
                        "--base-url",
                        "http://model.test/v1",
                        "--api-key-env",
                        "FAKE_KEY",
                        "--temperature",
                        "0.1",
                        "--max-tokens",
                        "99",
                    ]
                )
        explore.assert_called_once()
        _, kwargs = explore.call_args
        self.assertEqual(kwargs["max_actions"], 3)
        self.assertEqual(kwargs["horizon"], 1)
        self.assertFalse(kwargs["headless"])
        self.assertEqual(kwargs["model"], "fake/model")
        self.assertEqual(kwargs["base_url"], "http://model.test/v1")
        self.assertEqual(kwargs["api_key_env"], "FAKE_KEY")
        self.assertEqual(kwargs["temperature"], 0.1)
        self.assertEqual(kwargs["max_tokens"], 99)
        printed = json.loads(stdout.getvalue())
        self.assertEqual(printed["summary"]["edges"], 3)


if __name__ == "__main__":
    unittest.main()
