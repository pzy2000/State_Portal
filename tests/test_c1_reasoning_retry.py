from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from warptest import cost
from warptest.synthesis import (
    DeterministicCheckpointSynthesisAgent,
    ExploratoryLLMCheckpointSynthesisAgent,
    ModelCallResult,
    OpenAICompatibleChatClient,
    SynthesisError,
)
from warptest.task_schema import load_public_issues


ROOT = Path(__file__).resolve().parents[1]


class C1SynthesisReasoningRetryTests(unittest.TestCase):
    def test_empty_reasoning_response_expands_once_to_16384(self) -> None:
        client = OpenAICompatibleChatClient(
            api_key="not-secret",
            base_url="https://example.invalid/v1",
            max_tokens=8192,
            client=MagicMock(),
        )
        budgets: list[int] = []

        def stream(_model, _messages, *, max_tokens=None):
            budgets.append(max_tokens)
            if len(budgets) == 1:
                client._last_usage = SimpleNamespace(
                    prompt_tokens=10,
                    completion_tokens=8192,
                    total_tokens=8202,
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=8192),
                )
                client._last_finish_reason = "length"
                return ""
            client._last_usage = SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=20,
                total_tokens=30,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=5),
            )
            client._last_finish_reason = "stop"
            return "name: usable\nbenchmark: '2048'\ntarget: {}\ncheckpoint: {}"

        with cost.scoped_meter() as meter, patch.object(client, "_stream_content", side_effect=stream):
            result = client.converse("reasoner", [{"role": "user", "content": "go"}])

        self.assertTrue(result.content.startswith("name:"))
        self.assertEqual(budgets, [8192, 16384])
        summary = meter.summary()
        self.assertEqual(summary["llm_calls"], 2)
        self.assertEqual(summary["reasoning_tokens"], 8197)
        self.assertEqual(summary["total_tokens"], 8232)

    def test_empty_16384_response_is_not_retried_again(self) -> None:
        client = OpenAICompatibleChatClient(
            api_key="not-secret",
            max_tokens=8192,
            client=MagicMock(),
        )
        budgets: list[int] = []

        def stream(_model, _messages, *, max_tokens=None):
            budgets.append(max_tokens)
            client._last_usage = SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=int(max_tokens),
                total_tokens=10 + int(max_tokens),
                completion_tokens_details=SimpleNamespace(
                    reasoning_tokens=int(max_tokens)
                ),
            )
            client._last_finish_reason = "max_tokens"
            return ""

        with (
            cost.scoped_meter() as meter,
            patch.object(client, "_stream_content", side_effect=stream),
            patch("warptest.synthesis.time.sleep"),
            self.assertRaises(SynthesisError),
        ):
            client.converse(
                "reasoner",
                [{"role": "user", "content": "go"}],
                empty_retries=9,
            )

        self.assertEqual(budgets, [8192, 16384])
        self.assertEqual(meter.summary()["llm_calls"], 2)

    def test_reasoning_only_stop_is_not_retried_at_16384(self) -> None:
        client = OpenAICompatibleChatClient(
            api_key="not-secret",
            max_tokens=8192,
            client=MagicMock(),
        )
        budgets: list[int] = []

        def stream(_model, _messages, *, max_tokens=None):
            budgets.append(max_tokens)
            client._last_usage = SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=40,
                total_tokens=50,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=40),
            )
            client._last_finish_reason = "stop"
            client._last_reasoning_observed = True
            client._last_reasoning_content = "private-stop-reasoning"
            return ""

        with (
            cost.scoped_meter() as meter,
            patch.object(client, "_stream_content", side_effect=stream),
            self.assertRaises(SynthesisError),
        ):
            client.converse(
                "reasoner",
                [{"role": "user", "content": "go"}],
            )

        self.assertEqual(budgets, [8192])
        self.assertEqual(meter.summary()["llm_calls"], 1)

    def test_stream_preserves_reasoning_in_memory_and_redacts_ledger(self) -> None:
        private = "PRIVATE-PRESERVED-THINKING"
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=8,
            total_tokens=18,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=4),
        )
        chunks = [
            SimpleNamespace(
                id="response-id",
                model="kimi-k3",
                system_fingerprint=None,
                usage=None,
                choices=[
                    SimpleNamespace(
                        finish_reason=None,
                        delta=SimpleNamespace(
                            reasoning_content=private[:10], content=None
                        ),
                    )
                ],
            ),
            SimpleNamespace(
                id="response-id",
                model="kimi-k3",
                system_fingerprint=None,
                usage=None,
                choices=[
                    SimpleNamespace(
                        finish_reason=None,
                        delta=SimpleNamespace(
                            reasoning_content=private[10:],
                            content='ACTION: list_dir\n{"path":"js"}',
                        ),
                    )
                ],
            ),
            SimpleNamespace(
                id="response-id",
                model="kimi-k3",
                system_fingerprint=None,
                usage=usage,
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        delta=SimpleNamespace(reasoning_content=None, content=None),
                    )
                ],
            ),
        ]
        completions = MagicMock()
        completions.create.return_value = iter(chunks)
        provider = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        client = OpenAICompatibleChatClient(
            api_key="not-secret",
            max_tokens=8192,
            client=provider,
        )

        with cost.scoped_meter() as meter:
            result = client.converse(
                "kimi-k3@relay",
                [{"role": "user", "content": "inspect"}],
            )

        self.assertEqual(result.reasoning_content, private)
        self.assertTrue(result.content.startswith("ACTION:"))
        self.assertNotIn(private, json.dumps(meter.summary(), sort_keys=True))

    def test_exploration_echoes_reasoning_but_never_persists_or_parses_it(self) -> None:
        task = next(
            task for task in load_public_issues(ROOT) if task.id == "2048-merge-left"
        )
        spec = DeterministicCheckpointSynthesisAgent().synthesize(
            task, ROOT
        ).generated_spec
        private = "PRIVATE FINAL ```yaml\nsecret: must-not-be-parsed\n```"
        final = (
            "FINAL\n```yaml\n"
            "task_id: 2048-merge-left\n"
            "benchmark: '2048'\n"
            f"generated_spec: {spec!r}\n"
            "source_evidence:\n"
            "  - source_path: js/game_manager.js\n"
            "    matched_symbol: gameStateKey\n"
            "    rationale: located public save state\n"
            "payload:\n  kind: localStorage\n"
            "runtime_validation:\n  status: pending\n"
            "validity_class: save-restorable\n```"
        )

        class FakeExploreClient:
            def __init__(self) -> None:
                self.turn = 0

            def converse(self, model, messages):
                del model
                self.turn += 1
                if self.turn == 1:
                    return ModelCallResult(
                        content='ACTION: list_dir\n{"path":"js"}',
                        latency_ms=1,
                        reasoning_content=private,
                    )
                assistant = next(
                    item for item in messages if item.get("role") == "assistant"
                )
                if assistant.get("reasoning_content") != private:
                    raise AssertionError("reasoning_content was not echoed exactly")
                return ModelCallResult(content=final, latency_ms=1)

        with tempfile.TemporaryDirectory() as temp:
            artifact_dir = Path(temp)
            agent = ExploratoryLLMCheckpointSynthesisAgent(
                model="kimi-k3@relay",
                client=FakeExploreClient(),
                max_steps=3,
            )
            checkpoint = agent.synthesize(task, ROOT, artifact_dir=artifact_dir)
            persisted = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in artifact_dir.rglob("*")
                if path.is_file()
            )

        self.assertEqual(checkpoint.generated_by, "kimi-k3@relay")
        self.assertNotIn(private, persisted)

    def test_synthesis_response_without_usage_fails_closed(self) -> None:
        client = OpenAICompatibleChatClient(
            api_key="not-secret",
            max_tokens=8192,
            client=MagicMock(),
        )

        def stream(_model, _messages, *, max_tokens=None):
            del max_tokens
            client._last_usage = None
            client._last_finish_reason = "stop"
            return "name: unusable-without-usage"

        with (
            cost.scoped_meter() as meter,
            patch.object(client, "_stream_content", side_effect=stream),
            self.assertRaises(SynthesisError) as raised,
        ):
            client.converse("reasoner", [{"role": "user", "content": "go"}])
        self.assertFalse(raised.exception.usage_accounting_complete)
        self.assertFalse(meter.summary()["usage_complete"])

    def test_synthesis_timeout_without_usage_records_unknown_attempt(self) -> None:
        client = OpenAICompatibleChatClient(
            api_key="not-secret",
            max_tokens=8192,
            client=MagicMock(),
        )
        with (
            cost.scoped_meter() as meter,
            patch.object(
                client,
                "_stream_content",
                side_effect=TimeoutError("synthesis transport timeout"),
            ),
            self.assertRaises(SynthesisError),
        ):
            client.converse(
                "reasoner",
                [{"role": "user", "content": "go"}],
                outage_budget_s=0,
            )

        self.assertEqual(len(meter.records), 1)
        self.assertFalse(meter.summary()["usage_complete"])
        self.assertEqual(meter.summary()["unknown_total_token_calls"], 1)

    def test_synthesis_failed_call_with_usage_is_counted_before_client_error(self) -> None:
        client = OpenAICompatibleChatClient(
            api_key="not-secret",
            max_tokens=8192,
            client=MagicMock(),
        )
        request_error = RuntimeError("bad request")
        request_error.status_code = 400
        request_error.usage = SimpleNamespace(
            prompt_tokens=8,
            completion_tokens=4,
            total_tokens=12,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=4),
        )
        with (
            cost.scoped_meter() as meter,
            patch.object(client, "_stream_content", side_effect=request_error),
            self.assertRaises(SynthesisError),
        ):
            client.converse("reasoner", [{"role": "user", "content": "go"}])

        summary = meter.summary()
        self.assertEqual(summary["llm_calls"], 1)
        self.assertEqual(summary["total_tokens"], 12)
        self.assertEqual(summary["reasoning_tokens"], 4)

    def test_explore_final_call_is_separate_synthesis_cost_kind(self) -> None:
        task = next(
            task for task in load_public_issues(ROOT) if task.id == "2048-merge-left"
        )
        spec = DeterministicCheckpointSynthesisAgent().synthesize(
            task, ROOT
        ).generated_spec
        final = (
            "FINAL\n```yaml\n"
            "task_id: 2048-merge-left\n"
            "benchmark: '2048'\n"
            f"generated_spec: {spec!r}\n"
            "source_evidence:\n"
            "  - source_path: js/game_manager.js\n"
            "    matched_symbol: gameStateKey\n"
            "    rationale: located public save state\n"
            "payload:\n  kind: localStorage\n"
            "runtime_validation:\n  status: pending\n"
            "validity_class: save-restorable\n```"
        )

        class FakeExploreClient:
            def __init__(self) -> None:
                self.replies = [
                    'ACTION: list_dir\n{"path":"js"}',
                    final,
                ]

            def converse(self, model, messages):
                del messages
                cost.record_call(
                    model,
                    prompt_tokens=4,
                    completion_tokens=2,
                    total_tokens=6,
                    kind="checkpoint_exploration",
                )
                return ModelCallResult(content=self.replies.pop(0), latency_ms=1)

        agent = ExploratoryLLMCheckpointSynthesisAgent(
            model="fake/model",
            client=FakeExploreClient(),
            max_steps=3,
        )
        with cost.scoped_meter() as meter:
            checkpoint = agent.synthesize(task, ROOT)

        summary = meter.summary()
        self.assertEqual(checkpoint.generated_by, "fake/model")
        self.assertEqual(summary["by_kind"]["checkpoint_exploration"]["calls"], 1)
        self.assertEqual(summary["by_kind"]["checkpoint_synthesis"]["calls"], 1)


if __name__ == "__main__":
    unittest.main()
