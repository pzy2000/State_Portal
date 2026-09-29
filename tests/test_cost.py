from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from warptest import cost
from warptest.synthesis import OpenAICompatibleChatClient


class CostMeterTests(unittest.TestCase):
    def test_summary_breaks_down_kind_model_reasoning_and_cache_without_double_counting(self) -> None:
        meter = cost.CostMeter()
        meter.record(
            cost.CallRecord(
                model="reasoner",
                prompt_tokens=10,
                completion_tokens=8,
                reasoning_tokens=6,
                cached_prompt_tokens=3,
                total_tokens=18,
                latency_ms=11,
                kind="checkpoint_synthesis",
            )
        )
        meter.record(
            cost.CallRecord(
                model="judge",
                prompt_tokens=5,
                completion_tokens=4,
                reasoning_tokens=1,
                total_tokens=9,
                latency_ms=7,
                kind="visual_judge",
            )
        )

        summary = meter.summary()

        self.assertEqual(summary["llm_calls"], 2)
        self.assertEqual(summary["prompt_tokens"], 15)
        self.assertEqual(summary["completion_tokens"], 12)
        self.assertEqual(summary["reasoning_tokens"], 7)
        self.assertEqual(summary["cached_prompt_tokens"], 3)
        self.assertEqual(summary["total_tokens"], 27)
        self.assertEqual(summary["by_model"]["reasoner"]["reasoning_tokens"], 6)
        self.assertEqual(summary["by_kind"]["checkpoint_synthesis"]["total_tokens"], 18)
        self.assertEqual(
            summary["by_kind_model"]["visual_judge"]["judge"]["completion_tokens"],
            4,
        )

    def test_usage_tokens_accepts_nested_mapping_and_object_details(self) -> None:
        mapping = cost.usage_tokens(
            {
                "prompt_tokens": 20,
                "completion_tokens": 12,
                "total_tokens": 32,
                "prompt_tokens_details": {"cached_tokens": 9},
                "completion_tokens_details": {"reasoning_tokens": 10},
            }
        )
        obj = cost.usage_tokens(
            SimpleNamespace(
                input_tokens=7,
                output_tokens=5,
                input_tokens_details=SimpleNamespace(cached_tokens=2),
                output_tokens_details=SimpleNamespace(reasoning_tokens=4),
            )
        )

        self.assertEqual(
            mapping,
            {
                "prompt_tokens": 20,
                "completion_tokens": 12,
                "reasoning_tokens": 10,
                "cached_prompt_tokens": 9,
                "total_tokens": 32,
            },
        )
        self.assertEqual(obj["total_tokens"], 12)
        self.assertEqual(obj["reasoning_tokens"], 4)
        self.assertEqual(obj["cached_prompt_tokens"], 2)

    def test_usage_snapshot_preserves_absent_reasoning_for_parent_accounting(self) -> None:
        snapshot = cost.usage_snapshot(
            SimpleNamespace(
                prompt_tokens=5,
                completion_tokens=8192,
                total_tokens=8197,
                completion_tokens_details=SimpleNamespace(),
            )
        )

        self.assertEqual(
            snapshot,
            {
                "prompt_tokens": 5,
                "completion_tokens": 8192,
                "total_tokens": 8197,
                "completion_tokens_details": {},
            },
        )
        assessment = cost.assess_usage(
            snapshot,
            content="",
            finish_reason="length",
        )
        self.assertTrue(assessment.complete)
        self.assertFalse(assessment.reasoning_detail_complete)
        self.assertEqual(assessment.missing_fields, ())

    def test_visible_answer_with_unquantified_reasoning_is_bounded(self) -> None:
        assessment = cost.assess_usage(
            SimpleNamespace(
                prompt_tokens=5,
                completion_tokens=12,
                total_tokens=17,
                completion_tokens_details=SimpleNamespace(),
            ),
            content="usable answer",
            finish_reason="stop",
            reasoning_observed=True,
        )

        self.assertTrue(assessment.complete)
        self.assertFalse(assessment.reasoning_detail_complete)
        self.assertIsNone(assessment.reasoning_tokens)

    def test_inconsistent_provider_usage_fails_closed(self) -> None:
        assessment = cost.assess_usage(
            SimpleNamespace(
                prompt_tokens=5,
                completion_tokens=7,
                total_tokens=9,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=8),
            ),
            content="answer",
            finish_reason="stop",
        )

        self.assertFalse(assessment.complete)
        self.assertIn("total_tokens", assessment.missing_fields)
        self.assertEqual(assessment.reasoning_detail_status, "invalid")

    def test_reasoning_only_retry_is_billed_and_only_content_is_returned(self) -> None:
        usage_reasoning_only = SimpleNamespace(
            prompt_tokens=5,
            completion_tokens=64,
            total_tokens=69,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=64),
        )
        usage_answer = SimpleNamespace(
            prompt_tokens=5,
            completion_tokens=3,
            total_tokens=8,
            prompt_tokens_details=SimpleNamespace(cached_tokens=2),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
        )
        streams = [
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=None, reasoning_content="private"))],
                    usage=None,
                ),
                SimpleNamespace(choices=[], usage=usage_reasoning_only),
            ],
            [
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="answer"))],
                    usage=None,
                ),
                SimpleNamespace(choices=[], usage=usage_answer),
            ],
        ]

        class Completions:
            def create(self, **_kwargs: object) -> object:
                return iter(streams.pop(0))

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        client = OpenAICompatibleChatClient(
            api_key="unused",
            client=fake_client,
            cost_kind="checkpoint_synthesis",
        )
        with patch("warptest.synthesis.time.sleep"), cost.scoped_meter() as meter:
            result = client.converse(
                "reasoner",
                [{"role": "user", "content": "prompt"}],
                empty_retries=2,
            )

        summary = meter.summary()
        self.assertEqual(result.content, "answer")
        self.assertEqual(result.reasoning_tokens, 0)
        self.assertEqual(result.cached_prompt_tokens, 2)
        self.assertEqual(summary["llm_calls"], 2)
        self.assertEqual(summary["reasoning_tokens"], 64)
        self.assertEqual(summary["cached_prompt_tokens"], 2)
        self.assertEqual(summary["total_tokens"], 77)
        self.assertEqual(summary["by_kind"]["checkpoint_synthesis"]["calls"], 2)

    def test_missing_or_unquantified_reasoning_usage_is_not_recorded_as_zero(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=5,
            completion_tokens=8192,
            total_tokens=8197,
            completion_tokens_details=None,
        )
        with cost.scoped_meter() as meter:
            cost.record_usage_call(
                "reasoner",
                usage=usage,
                content="",
                finish_reason="max_tokens",
                latency_ms=7,
                kind="gui_policy",
            )
        summary = meter.summary()
        self.assertEqual(summary["llm_calls"], 1)
        self.assertTrue(summary["usage_complete"])
        self.assertEqual(summary["completion_tokens"], 8192)
        self.assertEqual(summary["unknown_reasoning_token_calls"], 1)
        self.assertIsNone(summary["reasoning_tokens"])
        self.assertEqual(summary["reasoning_tokens_observed"], 0)
        self.assertEqual(summary["reasoning_tokens_lower_bound"], 0)
        self.assertEqual(summary["reasoning_tokens_upper_bound"], 8192)
        self.assertEqual(summary["reasoning_detail_unknown_fraction"], 1.0)
        self.assertFalse(summary["reasoning_detail_within_tolerance"])
        self.assertIsNone(meter.records[0].reasoning_tokens)

    def test_reasoning_detail_completion_mass_threshold_is_inclusive(self) -> None:
        def summary(missing_completion: int, exact_completion: int) -> dict:
            meter = cost.CostMeter()
            meter.record(
                cost.CallRecord(
                    model="reasoner",
                    prompt_tokens=1,
                    completion_tokens=missing_completion,
                    reasoning_tokens=None,
                    total_tokens=1 + missing_completion,
                    reasoning_expected=True,
                    reasoning_detail_complete=False,
                    reasoning_detail_status="missing",
                    reasoning_tokens_source="missing",
                )
            )
            meter.record(
                cost.CallRecord(
                    model="reasoner",
                    prompt_tokens=1,
                    completion_tokens=exact_completion,
                    reasoning_tokens=1,
                    total_tokens=1 + exact_completion,
                    reasoning_expected=True,
                    reasoning_tokens_source="completion_tokens_details",
                )
            )
            return meter.summary()

        boundary = summary(10, 90)
        self.assertEqual(boundary["reasoning_detail_unknown_fraction"], 0.10)
        self.assertTrue(boundary["reasoning_detail_within_tolerance"])
        self.assertTrue(boundary["usage_complete"])
        self.assertFalse(cost.contains_unknown_usage(boundary))

        above = summary(11, 89)
        self.assertEqual(above["reasoning_detail_unknown_fraction"], 0.11)
        self.assertFalse(above["reasoning_detail_within_tolerance"])
        self.assertTrue(above["usage_complete"])
        self.assertFalse(cost.contains_unknown_usage(above))

    def test_thinking_disabled_absent_reasoning_is_exact_zero(self) -> None:
        assessment = cost.assess_usage(
            SimpleNamespace(
                prompt_tokens=5,
                completion_tokens=4,
                total_tokens=9,
                completion_tokens_details=SimpleNamespace(),
            ),
            content="",
            finish_reason="length",
            reasoning_disabled=True,
        )
        self.assertTrue(assessment.complete)
        self.assertTrue(assessment.reasoning_detail_complete)
        self.assertEqual(assessment.reasoning_tokens, 0)
        self.assertEqual(assessment.reasoning_detail_status, "not_expected")

    def test_invalid_reasoning_detail_does_not_invalidate_exact_primary_usage(self) -> None:
        assessment = cost.assess_usage(
            SimpleNamespace(
                prompt_tokens=5,
                completion_tokens=7,
                total_tokens=12,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=8),
            ),
            content="answer",
            finish_reason="stop",
        )
        self.assertTrue(assessment.complete)
        self.assertFalse(assessment.reasoning_detail_complete)
        self.assertEqual(assessment.reasoning_detail_status, "invalid")
        self.assertIsNone(assessment.reasoning_tokens)

    def test_call_ledger_keeps_only_nonsecret_provider_diagnostics(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=5,
            completion_tokens=2,
            total_tokens=7,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=1),
        )
        with cost.scoped_meter() as meter:
            cost.record_usage_call(
                "reasoner",
                usage=usage,
                content="answer",
                finish_reason="stop",
                reasoning_expected=True,
                latency_ms=1,
                kind="chat",
                endpoint="chat.completions",
                provider_reported_model="provider-reasoner-v2",
                provider_response_id="raw-response-id",
                system_fingerprint="fp-test",
            )
        entry = meter.summary()["calls"][0]
        self.assertEqual(entry["endpoint"], "chat.completions")
        self.assertEqual(entry["finish_reason"], "stop")
        self.assertEqual(entry["reasoning_tokens_source"], "completion_tokens_details")
        self.assertEqual(entry["provider_reported_model"], "provider-reasoner-v2")
        self.assertEqual(len(entry["provider_response_id_sha256"]), 64)
        self.assertNotIn("raw-response-id", json.dumps(entry))

    def test_ambiguous_timeout_records_unknown_but_429_records_nothing(self) -> None:
        with cost.scoped_meter() as timeout_meter, self.assertRaises(
            cost.UsageAccountingError
        ):
            cost.record_error_usage_call(
                "model",
                error=TimeoutError("API read timed out"),
                latency_ms=12,
                kind="gui_policy",
            )
        timeout_summary = timeout_meter.summary()
        self.assertFalse(timeout_summary["usage_complete"])
        self.assertEqual(timeout_summary["incomplete_calls"], 1)
        self.assertEqual(timeout_summary["unknown_total_token_calls"], 1)

        rate_error = RuntimeError("quota exceeded")
        rate_error.status_code = 429
        with cost.scoped_meter() as rate_meter:
            result = cost.record_error_usage_call(
                "model",
                error=rate_error,
                latency_ms=5,
                kind="gui_policy",
            )
        self.assertIsNone(result)
        self.assertEqual(rate_meter.records, [])

    def test_merge_summaries_preserves_all_breakdowns_without_double_counting(self) -> None:
        first = cost.CostMeter()
        first.record(
            cost.CallRecord(
                model="m1",
                prompt_tokens=10,
                completion_tokens=5,
                reasoning_tokens=3,
                total_tokens=15,
                kind="checkpoint_exploration",
            )
        )
        second = cost.CostMeter()
        second.record(
            cost.CallRecord(
                model="m1",
                prompt_tokens=4,
                completion_tokens=2,
                reasoning_tokens=None,
                total_tokens=6,
                kind="checkpoint_synthesis",
                reasoning_expected=True,
                reasoning_detail_complete=False,
                reasoning_detail_status="missing",
                reasoning_tokens_source="missing",
                missing_reasoning_detail_fields=("reasoning_tokens",),
            )
        )
        second.record(
            cost.CallRecord(
                model="judge",
                prompt_tokens=7,
                completion_tokens=1,
                reasoning_tokens=0,
                total_tokens=8,
                kind="visual_judge",
            )
        )

        merged = cost.merge_summaries([first.summary(), second.summary()])

        self.assertEqual(merged["llm_calls"], 3)
        self.assertEqual(merged["total_tokens"], 29)
        self.assertEqual(merged["prompt_tokens"], 21)
        self.assertEqual(merged["by_model"]["m1"]["calls"], 2)
        self.assertEqual(merged["by_kind"]["checkpoint_synthesis"]["calls"], 1)
        self.assertEqual(
            merged["by_kind_model"]["visual_judge"]["judge"]["total_tokens"],
            8,
        )
        self.assertTrue(merged["usage_complete"])
        self.assertEqual(merged["incomplete_calls"], 0)
        self.assertEqual(merged["unknown_reasoning_token_calls"], 1)
        self.assertIsNone(merged["reasoning_tokens"])
        self.assertEqual(merged["reasoning_tokens_observed"], 3)
        self.assertEqual(merged["reasoning_tokens_upper_bound"], 5)

    def test_merge_infers_incomplete_from_unknown_counters(self) -> None:
        summary = cost.CostMeter().summary()
        summary.pop("usage_complete")
        summary["unknown_reasoning_token_calls"] = 1
        summary["incomplete_calls"] = 1

        merged = cost.merge_summaries([summary])

        self.assertFalse(merged["usage_complete"])
        self.assertEqual(merged["unknown_reasoning_token_calls"], 1)
        self.assertIsNone(merged["est_usd"])

    def test_summary_preserves_sanitized_messages_and_response(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look here"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,AAAA",
                            "detail": "high",
                        },
                    },
                ],
            },
        ]
        with cost.scoped_meter() as meter:
            cost.record_usage_call(
                "decision",
                usage=SimpleNamespace(
                    prompt_tokens=3,
                    completion_tokens=2,
                    total_tokens=5,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
                ),
                content='{"kind":"wait"}',
                finish_reason="stop",
                latency_ms=9,
                kind="gui_policy",
                messages=messages,
                response='{"kind":"wait"}',
            )

        summary = meter.summary()
        self.assertEqual(len(summary["calls"]), 1)
        call = summary["calls"][0]
        self.assertEqual(call["kind"], "gui_policy")
        self.assertEqual(call["response"], '{"kind":"wait"}')
        self.assertEqual(call["messages"][0]["content"], "sys")
        image = call["messages"][1]["content"][1]
        self.assertEqual(image["type"], "image_url")
        self.assertEqual(image["image_url"]["url"], "[omitted image]")
        self.assertEqual(image["image_url"]["detail"], "high")
        self.assertNotIn("data:image", json.dumps(summary["calls"]))

    def test_merge_summaries_concatenates_call_ledgers(self) -> None:
        first = cost.CostMeter()
        first.record(
            cost.CallRecord(
                model="m1",
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                kind="gui_policy",
                messages=[{"role": "user", "content": "a"}],
                response="ra",
            )
        )
        second = cost.CostMeter()
        second.record(
            cost.CallRecord(
                model="m2",
                prompt_tokens=2,
                completion_tokens=2,
                total_tokens=4,
                kind="visual_observer",
                messages=[{"role": "user", "content": "b"}],
                response="rb",
            )
        )

        merged = cost.merge_summaries([first.summary(), second.summary()])
        self.assertEqual([call["response"] for call in merged["calls"]], ["ra", "rb"])
        self.assertEqual(merged["calls"][0]["messages"][0]["content"], "a")


if __name__ == "__main__":
    unittest.main()
