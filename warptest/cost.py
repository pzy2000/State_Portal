"""Cross-cutting cost accounting for LLM-driven loops.

The agent paper's efficiency claim (C1) needs a fair, uniform cost ledger across
the state-managed loop and the GUI-agent baseline: how many LLM calls, how many
tokens, how much wall-clock, and an optional dollar estimate. This module provides
a process-scoped meter that the OpenAI-compatible client records into automatically,
so every call site (synthesis, ReAct exploration, repair, GUI agent) is counted
without threading a meter through every function.

Usage:
    from . import cost
    with cost.scoped_meter() as meter:
        ... run a task ...
    report["cost"] = meter.summary()

A dollar estimate is only produced when ``WARPTEST_LLM_USD_PER_MTOK`` (USD per
million tokens) is set, because gateway pricing varies; otherwise tokens/calls are
the reported currency.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping


_OMITTED_IMAGE = "[omitted image]"
REASONING_DETAIL_TOLERANCE_FRACTION = 0.10


@dataclass
class CallRecord:
    model: str
    prompt_tokens: int | None = 0
    completion_tokens: int | None = 0
    reasoning_tokens: int | None = 0
    cached_prompt_tokens: int | None = 0
    total_tokens: int | None = 0
    latency_ms: int = 0
    kind: str = "chat"
    usage_complete: bool = True
    missing_usage_fields: tuple[str, ...] = ()
    reasoning_expected: bool = False
    reasoning_detail_complete: bool = True
    reasoning_detail_status: str = "exact"
    reasoning_tokens_source: str = "not_expected"
    missing_reasoning_detail_fields: tuple[str, ...] = ()
    endpoint: str | None = None
    finish_reason: str | None = None
    provider_reported_model: str | None = None
    provider_response_id_sha256: str | None = None
    system_fingerprint: str | None = None
    # Request messages (images redacted) and assistant text for audit/debug.
    # Never store provider reasoning_content.
    messages: list[dict[str, Any]] | None = None
    response: str | None = None
    # UTC ISO8601 timestamp captured immediately before the provider request was
    # sent (not when the ledger entry was written), so the call log can be
    # replayed against real wall-clock time -- e.g. to line up a mid-run
    # provider outage/rate-limit window with the exact calls it hit.
    requested_at: str | None = None


class UsageAccountingError(RuntimeError):
    """A provider call occurred but its token usage cannot be audited."""

    usage_accounting_complete = False


@dataclass(frozen=True)
class UsageAssessment:
    prompt_tokens: int | None
    completion_tokens: int | None
    reasoning_tokens: int | None
    cached_prompt_tokens: int | None
    total_tokens: int | None
    complete: bool
    missing_fields: tuple[str, ...] = ()
    reasoning_expected: bool = False
    reasoning_detail_complete: bool = True
    reasoning_detail_status: str = "exact"
    reasoning_tokens_source: str = "not_expected"
    missing_reasoning_detail_fields: tuple[str, ...] = ()

    def complete_fields(self) -> dict[str, Any]:
        if not self.complete:
            raise UsageAccountingError(
                "provider usage is incomplete: " + ", ".join(self.missing_fields)
            )
        return {
            "prompt_tokens": int(self.prompt_tokens or 0),
            "completion_tokens": int(self.completion_tokens or 0),
            "reasoning_tokens": (
                None if self.reasoning_tokens is None else int(self.reasoning_tokens)
            ),
            "cached_prompt_tokens": int(self.cached_prompt_tokens or 0),
            "total_tokens": int(self.total_tokens or 0),
        }


@dataclass
class CostMeter:
    records: list[CallRecord] = field(default_factory=list)

    def record(self, rec: CallRecord) -> None:
        self.records.append(rec)
        for observer in _call_observers.get():
            if observer is not self:
                observer.records.append(rec)

    @property
    def calls(self) -> int:
        return len(self.records)

    def summary(self) -> dict[str, Any]:
        prompt = _sum_known(r.prompt_tokens for r in self.records)
        completion = _sum_known(r.completion_tokens for r in self.records)
        reasoning_detail = _reasoning_detail_metrics(self.records)
        cached_prompt = _sum_known(r.cached_prompt_tokens for r in self.records)
        # Provider usage schemas report reasoning as a subset of completion tokens
        # and cached prompt tokens as a subset of prompt tokens. Keep both as
        # diagnostic breakouts, but never add either one to total_tokens.
        total = sum(_record_total(r) for r in self.records if _record_total(r) is not None)
        latency = sum(r.latency_ms for r in self.records)
        incomplete_calls = sum(not r.usage_complete for r in self.records)
        by_model: dict[str, dict[str, Any]] = {}
        by_kind: dict[str, dict[str, Any]] = {}
        by_kind_model: dict[str, dict[str, dict[str, Any]]] = {}
        for rec in self.records:
            _accumulate(by_model.setdefault(rec.model, _empty_bucket()), rec)
            _accumulate(by_kind.setdefault(rec.kind, _empty_bucket()), rec)
            _accumulate(
                by_kind_model.setdefault(rec.kind, {}).setdefault(rec.model, _empty_bucket()),
                rec,
            )
        for bucket in by_model.values():
            _finalize_reasoning_bucket(bucket)
        for bucket in by_kind.values():
            _finalize_reasoning_bucket(bucket)
        for model_buckets in by_kind_model.values():
            for bucket in model_buckets.values():
                _finalize_reasoning_bucket(bucket)
        # Per-model dollar attribution: agent/decision LLMs and the visual judge are
        # priced separately so the C1 cost axis is comparable across models.
        rate_table = _model_rate_table()
        model_usd_total = 0.0
        any_model_usd = False
        for model, bucket in by_model.items():
            bucket_usd = None if not bucket["usage_complete"] else _model_est_usd(
                model, bucket["prompt_tokens"], bucket["completion_tokens"], rate_table
            )
            bucket["est_usd"] = round(bucket_usd, 6) if bucket_usd is not None else None
            if bucket_usd is not None:
                any_model_usd = True
                model_usd_total += bucket_usd
        for kind, model_buckets in by_kind_model.items():
            kind_usd = 0.0
            any_kind_usd = False
            for model, bucket in model_buckets.items():
                bucket_usd = None if not bucket["usage_complete"] else _model_est_usd(
                    model, bucket["prompt_tokens"], bucket["completion_tokens"], rate_table
                )
                bucket["est_usd"] = round(bucket_usd, 6) if bucket_usd is not None else None
                if bucket_usd is not None:
                    any_kind_usd = True
                    kind_usd += bucket_usd
            by_kind[kind]["est_usd"] = round(kind_usd, 6) if any_kind_usd else None
        run_est_usd = None if incomplete_calls else (
            round(model_usd_total, 6) if any_model_usd else estimate_usd(prompt, completion)
        )
        return {
            "llm_calls": self.calls,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            **reasoning_detail,
            "cached_prompt_tokens": cached_prompt,
            "total_tokens": total,
            "llm_latency_ms": latency,
            "est_usd": run_est_usd,
            "usage_complete": incomplete_calls == 0,
            "incomplete_calls": incomplete_calls,
            "unknown_prompt_token_calls": sum(r.prompt_tokens is None for r in self.records),
            "unknown_completion_token_calls": sum(r.completion_tokens is None for r in self.records),
            "unknown_reasoning_token_calls": reasoning_detail[
                "reasoning_detail_missing_calls"
            ],
            "unknown_total_token_calls": sum(r.total_tokens is None for r in self.records),
            "by_model": by_model,
            "by_kind": by_kind,
            "by_kind_model": by_kind_model,
            "calls": [_call_ledger_entry(rec) for rec in self.records],
        }


_SUMMARY_SUM_FIELDS = (
    "llm_calls",
    "prompt_tokens",
    "completion_tokens",
    "cached_prompt_tokens",
    "total_tokens",
    "llm_latency_ms",
    "incomplete_calls",
    "unknown_prompt_token_calls",
    "unknown_completion_token_calls",
    "unknown_reasoning_token_calls",
    "unknown_total_token_calls",
    "reasoning_tokens_observed",
    "reasoning_tokens_lower_bound",
    "reasoning_tokens_upper_bound",
    "reasoning_detail_missing_calls",
    "reasoning_detail_unknown_completion_tokens",
    "reasoning_detail_expected_completion_tokens",
)
_BUCKET_SUM_FIELDS = (
    "calls",
    "prompt_tokens",
    "completion_tokens",
    "cached_prompt_tokens",
    "total_tokens",
    "latency_ms",
    "incomplete_calls",
    "unknown_prompt_token_calls",
    "unknown_completion_token_calls",
    "unknown_reasoning_token_calls",
    "unknown_total_token_calls",
    "reasoning_tokens_observed",
    "reasoning_tokens_lower_bound",
    "reasoning_tokens_upper_bound",
    "reasoning_detail_missing_calls",
    "reasoning_detail_unknown_completion_tokens",
    "reasoning_detail_expected_completion_tokens",
)


def merge_summaries(summaries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge disjoint call-ledger summaries without summing nested totals twice.

    Callers remain responsible for passing each attempt exactly once. Top-level,
    by-model, by-kind, and by-kind/model aggregates are merged independently from
    their corresponding source fields; nested buckets never feed top-level totals.
    """

    items = [item for item in summaries if isinstance(item, Mapping)]
    if not items:
        return CostMeter().summary()
    merged: dict[str, Any] = {
        field: sum(_summary_int(item.get(field)) for item in items)
        for field in _SUMMARY_SUM_FIELDS
    }
    _finalize_reasoning_bucket(merged)
    merged["usage_complete"] = all(_summary_usage_complete(item) for item in items)
    merged["est_usd"] = _merge_usd(items, complete=merged["usage_complete"])
    merged["by_model"] = _merge_bucket_maps(items, "by_model")
    merged["by_kind"] = _merge_bucket_maps(items, "by_kind")
    merged["by_kind_model"] = _merge_nested_bucket_maps(items, "by_kind_model")
    merged["calls"] = _merge_call_ledgers(items)
    return merged


def _merge_call_ledgers(summaries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for summary in summaries:
        calls = summary.get("calls")
        if not isinstance(calls, list):
            continue
        for entry in calls:
            if isinstance(entry, Mapping):
                merged.append(dict(entry))
    return merged


def sanitize_messages_for_ledger(messages: Any) -> list[dict[str, Any]] | None:
    """Copy chat messages for the cost ledger, redacting image payloads.

    Base64 ``data:`` image URLs must never enter reports or the parent/child
    JSONL bridge; text and structural fields are preserved.
    """

    if messages is None:
        return None
    if not isinstance(messages, list):
        return None
    sanitized: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, Mapping):
            sanitized.append(_sanitize_message_for_ledger(message))
    return sanitized


def _sanitize_message_for_ledger(message: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    role = message.get("role")
    if role is not None:
        out["role"] = str(role)
    for key in ("name", "tool_call_id"):
        if key in message and message[key] is not None:
            out[key] = message[key]
    if "content" in message:
        out["content"] = _sanitize_message_content(message.get("content"))
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        out["tool_calls"] = [
            dict(item) if isinstance(item, Mapping) else item for item in tool_calls
        ]
    # Intentionally omit reasoning_content and any other provider-private fields.
    return out


def _sanitize_message_content(content: Any) -> Any:
    if content is None or isinstance(content, (str, int, float, bool)):
        return content
    if isinstance(content, list):
        return [_sanitize_content_part(part) for part in content]
    if isinstance(content, Mapping):
        return _sanitize_content_part(content)
    return str(content)


def _sanitize_content_part(part: Any) -> Any:
    if part is None or isinstance(part, (str, int, float, bool)):
        return part
    if not isinstance(part, Mapping):
        return str(part)
    part_type = str(part.get("type") or "")
    if part_type == "image_url" or "image_url" in part:
        image = part.get("image_url")
        sanitized_image: dict[str, Any] = {"url": _OMITTED_IMAGE}
        if isinstance(image, Mapping) and image.get("detail") is not None:
            sanitized_image["detail"] = image.get("detail")
        return {"type": "image_url", "image_url": sanitized_image}
    if part_type in {"image", "input_image"} or "image" in part:
        return {"type": part_type or "image", "image": _OMITTED_IMAGE}
    if part_type == "text" or "text" in part:
        text = part.get("text")
        return {"type": "text", "text": "" if text is None else str(text)}
    # Preserve unknown structured parts without copying huge binary-looking values.
    copied: dict[str, Any] = {}
    for key, value in part.items():
        if isinstance(value, str) and (
            value.startswith("data:image") or len(value) > 4096
        ):
            copied[key] = _OMITTED_IMAGE
        elif isinstance(value, (str, int, float, bool)) or value is None:
            copied[key] = value
        elif isinstance(value, Mapping):
            copied[key] = _sanitize_content_part(value)
        elif isinstance(value, list):
            copied[key] = [_sanitize_content_part(item) for item in value]
        else:
            copied[key] = str(value)
    return copied


def _call_ledger_entry(rec: CallRecord) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "model": rec.model,
        "kind": rec.kind,
        "prompt_tokens": rec.prompt_tokens,
        "completion_tokens": rec.completion_tokens,
        "reasoning_tokens": rec.reasoning_tokens,
        "cached_prompt_tokens": rec.cached_prompt_tokens,
        "total_tokens": rec.total_tokens,
        "latency_ms": rec.latency_ms,
        "usage_complete": rec.usage_complete,
        "reasoning_expected": rec.reasoning_expected,
        "reasoning_detail_complete": rec.reasoning_detail_complete,
        "reasoning_detail_status": rec.reasoning_detail_status,
        "reasoning_tokens_source": rec.reasoning_tokens_source,
    }
    if rec.missing_usage_fields:
        entry["missing_usage_fields"] = list(rec.missing_usage_fields)
    if rec.missing_reasoning_detail_fields:
        entry["missing_reasoning_detail_fields"] = list(
            rec.missing_reasoning_detail_fields
        )
    for key, value in (
        ("endpoint", rec.endpoint),
        ("finish_reason", rec.finish_reason),
        ("provider_reported_model", rec.provider_reported_model),
        ("provider_response_id_sha256", rec.provider_response_id_sha256),
        ("system_fingerprint", rec.system_fingerprint),
    ):
        if value is not None:
            entry[key] = value
    if rec.messages is not None:
        entry["messages"] = rec.messages
    if rec.response is not None:
        entry["response"] = rec.response
    if rec.requested_at is not None:
        entry["requested_at"] = rec.requested_at
    return entry


def _merge_bucket_maps(
    summaries: Iterable[Mapping[str, Any]], key: str
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for summary in summaries:
        value = summary.get(key)
        if not isinstance(value, Mapping):
            continue
        for name, bucket in value.items():
            if isinstance(bucket, Mapping):
                grouped.setdefault(str(name), []).append(bucket)
    return {name: _merge_buckets(buckets) for name, buckets in grouped.items()}


def _merge_nested_bucket_maps(
    summaries: Iterable[Mapping[str, Any]], key: str
) -> dict[str, dict[str, dict[str, Any]]]:
    grouped: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    for summary in summaries:
        value = summary.get(key)
        if not isinstance(value, Mapping):
            continue
        for outer_name, inner in value.items():
            if not isinstance(inner, Mapping):
                continue
            for inner_name, bucket in inner.items():
                if isinstance(bucket, Mapping):
                    grouped.setdefault(str(outer_name), {}).setdefault(
                        str(inner_name), []
                    ).append(bucket)
    return {
        outer: {inner: _merge_buckets(buckets) for inner, buckets in inner_map.items()}
        for outer, inner_map in grouped.items()
    }


def _merge_buckets(buckets: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    items = list(buckets)
    merged = {
        field: sum(_summary_int(item.get(field)) for item in items)
        for field in _BUCKET_SUM_FIELDS
    }
    merged["usage_complete"] = all(_summary_usage_complete(item) for item in items)
    _finalize_reasoning_bucket(merged)
    merged["est_usd"] = _merge_usd(items, complete=merged["usage_complete"])
    return merged


def _merge_usd(items: Iterable[Mapping[str, Any]], *, complete: bool) -> float | None:
    if not complete:
        return None
    values = [item.get("est_usd") for item in items]
    if not values or any(value is None for value in values):
        return None
    try:
        return round(sum(float(value) for value in values), 6)
    except (TypeError, ValueError):
        return None


def _summary_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _summary_usage_complete(item: Mapping[str, Any]) -> bool:
    """Infer completeness from both the flag and auditable unknown counters.

    Resume may merge reports written by an earlier process that omitted the
    boolean flag but already carried incomplete/unknown counters.  Treat those
    counters as authoritative so a merge can never turn an incomplete ledger
    back into a formally complete one.
    """

    unknown = _summary_int(item.get("incomplete_calls")) > 0 or any(
        _summary_int(item.get(field)) > 0
        for field in (
            "unknown_prompt_token_calls",
            "unknown_completion_token_calls",
            "unknown_total_token_calls",
        )
    )
    return bool(item.get("usage_complete", True)) and not unknown


def summary_usage_complete(item: Mapping[str, Any]) -> bool:
    """Public fail-closed check used before resuming a paid attempt ledger."""

    return _summary_usage_complete(item)


def contains_unknown_usage(value: Any) -> bool:
    """Recursively detect a permanently unauditable provider-usage ledger."""

    if isinstance(value, Mapping):
        if value.get("usage_accounting_complete") is False:
            return True
        if _summary_int(value.get("incomplete_calls")) > 0 or any(
            _summary_int(value.get(field)) > 0
            for field in (
                "unknown_prompt_token_calls",
                "unknown_completion_token_calls",
                "unknown_total_token_calls",
            )
        ):
            return True
        if "usage_complete" in value and not _summary_usage_complete(value):
            return True
        return any(contains_unknown_usage(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_unknown_usage(item) for item in value)
    return False


def _empty_bucket() -> dict[str, Any]:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cached_prompt_tokens": 0,
        "total_tokens": 0,
        "latency_ms": 0,
        "usage_complete": True,
        "incomplete_calls": 0,
        "unknown_prompt_token_calls": 0,
        "unknown_completion_token_calls": 0,
        "unknown_reasoning_token_calls": 0,
        "unknown_total_token_calls": 0,
        "reasoning_tokens_observed": 0,
        "reasoning_tokens_lower_bound": 0,
        "reasoning_tokens_upper_bound": 0,
        "reasoning_detail_missing_calls": 0,
        "reasoning_detail_unknown_completion_tokens": 0,
        "reasoning_detail_expected_completion_tokens": 0,
    }


def _accumulate(bucket: dict[str, Any], rec: CallRecord) -> None:
    bucket["calls"] += 1
    bucket["prompt_tokens"] += rec.prompt_tokens or 0
    bucket["completion_tokens"] += rec.completion_tokens or 0
    observed = rec.reasoning_tokens or 0
    expected = _record_reasoning_expected(rec)
    missing = _record_reasoning_detail_missing(rec)
    bucket["reasoning_tokens_observed"] += observed
    bucket["reasoning_tokens_lower_bound"] += observed
    bucket["reasoning_tokens_upper_bound"] += observed + (
        (rec.completion_tokens or 0) if missing else 0
    )
    bucket["reasoning_detail_missing_calls"] += int(missing)
    bucket["reasoning_detail_unknown_completion_tokens"] += (
        (rec.completion_tokens or 0) if missing else 0
    )
    bucket["reasoning_detail_expected_completion_tokens"] += (
        (rec.completion_tokens or 0) if expected else 0
    )
    bucket["cached_prompt_tokens"] += rec.cached_prompt_tokens or 0
    bucket["total_tokens"] += _record_total(rec) or 0
    bucket["latency_ms"] += rec.latency_ms
    bucket["usage_complete"] = bucket["usage_complete"] and rec.usage_complete
    bucket["incomplete_calls"] += int(not rec.usage_complete)
    bucket["unknown_prompt_token_calls"] += int(rec.prompt_tokens is None)
    bucket["unknown_completion_token_calls"] += int(rec.completion_tokens is None)
    bucket["unknown_reasoning_token_calls"] += int(missing)
    bucket["unknown_total_token_calls"] += int(rec.total_tokens is None)


def _record_reasoning_expected(rec: CallRecord) -> bool:
    return bool(rec.reasoning_expected or (rec.reasoning_tokens or 0) > 0)


def _record_reasoning_detail_missing(rec: CallRecord) -> bool:
    return bool(
        not rec.reasoning_detail_complete or rec.reasoning_tokens is None
    )


def _reasoning_detail_metrics(records: Iterable[CallRecord]) -> dict[str, Any]:
    bucket = _empty_bucket()
    for rec in records:
        observed = rec.reasoning_tokens or 0
        expected = _record_reasoning_expected(rec)
        missing = _record_reasoning_detail_missing(rec)
        bucket["reasoning_tokens_observed"] += observed
        bucket["reasoning_tokens_lower_bound"] += observed
        bucket["reasoning_tokens_upper_bound"] += observed + (
            (rec.completion_tokens or 0) if missing else 0
        )
        bucket["reasoning_detail_missing_calls"] += int(missing)
        bucket["reasoning_detail_unknown_completion_tokens"] += (
            (rec.completion_tokens or 0) if missing else 0
        )
        bucket["reasoning_detail_expected_completion_tokens"] += (
            (rec.completion_tokens or 0) if expected else 0
        )
    _finalize_reasoning_bucket(bucket)
    return {
        key: bucket[key]
        for key in (
            "reasoning_tokens",
            "reasoning_tokens_observed",
            "reasoning_tokens_lower_bound",
            "reasoning_tokens_upper_bound",
            "reasoning_detail_complete",
            "reasoning_detail_missing_calls",
            "reasoning_detail_unknown_completion_tokens",
            "reasoning_detail_expected_completion_tokens",
            "reasoning_detail_unknown_fraction",
            "reasoning_detail_tolerance_fraction",
            "reasoning_detail_within_tolerance",
        )
    }


def _finalize_reasoning_bucket(bucket: dict[str, Any]) -> None:
    observed = _summary_int(bucket.get("reasoning_tokens_observed"))
    missing = _summary_int(bucket.get("reasoning_detail_missing_calls"))
    unknown_completion = _summary_int(
        bucket.get("reasoning_detail_unknown_completion_tokens")
    )
    expected_completion = _summary_int(
        bucket.get("reasoning_detail_expected_completion_tokens")
    )
    fraction = (
        float(unknown_completion) / float(expected_completion)
        if expected_completion > 0
        else 0.0
    )
    bucket["reasoning_tokens"] = None if missing else observed
    bucket["reasoning_tokens_observed"] = observed
    bucket["reasoning_tokens_lower_bound"] = observed
    bucket["reasoning_tokens_upper_bound"] = observed + unknown_completion
    bucket["reasoning_detail_complete"] = missing == 0
    bucket["reasoning_detail_unknown_fraction"] = fraction
    bucket["reasoning_detail_tolerance_fraction"] = (
        REASONING_DETAIL_TOLERANCE_FRACTION
    )
    bucket["reasoning_detail_within_tolerance"] = (
        fraction <= REASONING_DETAIL_TOLERANCE_FRACTION
    )


def _sum_known(values: Iterator[int | None]) -> int:
    return sum(value for value in values if value is not None)


def _record_total(rec: CallRecord) -> int | None:
    if rec.total_tokens is not None and rec.total_tokens > 0:
        return rec.total_tokens
    if rec.prompt_tokens is None or rec.completion_tokens is None:
        return None
    return rec.prompt_tokens + rec.completion_tokens


_current: contextvars.ContextVar["CostMeter | None"] = contextvars.ContextVar(
    "warptest_cost_meter", default=None
)


def get_meter() -> "CostMeter | None":
    return _current.get()


_call_observers: contextvars.ContextVar[tuple[CostMeter, ...]] = contextvars.ContextVar(
    "warptest_cost_call_observers", default=()
)


@contextlib.contextmanager
def observe_calls() -> Iterator[CostMeter]:
    """Audit nested phase meters without changing their attribution or totals."""
    meter = CostMeter()
    token = _call_observers.set((*_call_observers.get(), meter))
    try:
        yield meter
    finally:
        _call_observers.reset(token)


@contextlib.contextmanager
def scoped_meter(meter: "CostMeter | None" = None) -> Iterator[CostMeter]:
    """Activate ``meter`` (or a fresh one) for the duration of the block.

    Nesting is supported: the previous meter is restored on exit. Records go only
    to the innermost active meter, so per-task accounting can nest inside a run.
    """

    active = meter or CostMeter()
    token = _current.set(active)
    try:
        yield active
    finally:
        _current.reset(token)


class LLMCallForbiddenError(RuntimeError):
    """A provider call was made inside a block declared to be free of LLM calls."""


@dataclass
class NoLLMAttestation:
    """Positive evidence that a block issued no provider calls.

    ``observed_calls`` counts every ``record_call`` seen while the guard was
    active, independent of which cost meter was innermost at the time. A meter
    alone cannot prove this: meters nest, and records reach only the innermost
    one, so an outer meter reporting zero is also what a leaked inner meter
    looks like.
    """

    observed_calls: int = 0
    first_call_model: str | None = None
    first_call_kind: str | None = None

    @property
    def clean(self) -> bool:
        return self.observed_calls == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": "no_llm_attestation_v1",
            "observed_calls": self.observed_calls,
            "clean": self.clean,
            "first_call_model": self.first_call_model,
            "first_call_kind": self.first_call_kind,
        }


_no_llm: contextvars.ContextVar["NoLLMAttestation | None"] = contextvars.ContextVar(
    "warptest_no_llm_guard", default=None
)


@contextlib.contextmanager
def no_llm_guard(*, raise_on_call: bool = True) -> Iterator[NoLLMAttestation]:
    """Forbid provider calls for the duration of the block.

    Set ``raise_on_call=False`` to count violations without aborting the block;
    the caller is then responsible for treating a non-clean attestation as a
    failure.
    """

    attestation = NoLLMAttestation()
    token = _no_llm.set(attestation)
    try:
        yield attestation
    finally:
        _no_llm.reset(token)
    if raise_on_call and not attestation.clean:
        raise LLMCallForbiddenError(
            f"{attestation.observed_calls} provider call(s) occurred inside a "
            f"no-LLM block; first was {attestation.first_call_kind!r} on "
            f"{attestation.first_call_model!r}"
        )


def record_call(
    model: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    reasoning_tokens: int = 0,
    cached_prompt_tokens: int = 0,
    total_tokens: int = 0,
    latency_ms: int = 0,
    kind: str = "chat",
    messages: list[dict[str, Any]] | None = None,
    response: str | None = None,
    requested_at: str | None = None,
) -> None:
    guard = _no_llm.get()
    if guard is not None:
        guard.observed_calls += 1
        if guard.first_call_model is None:
            guard.first_call_model = str(model)
            guard.first_call_kind = str(kind or "chat")
        raise LLMCallForbiddenError(
            f"provider call {kind!r} on {model!r} is not allowed inside a no-LLM block"
        )
    meter = _current.get()
    if meter is None:
        return
    meter.record(
        CallRecord(
            model=str(model),
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
            reasoning_tokens=int(reasoning_tokens or 0),
            reasoning_expected=int(reasoning_tokens or 0) > 0,
            reasoning_tokens_source=(
                "top_level" if int(reasoning_tokens or 0) > 0 else "not_expected"
            ),
            cached_prompt_tokens=int(cached_prompt_tokens or 0),
            total_tokens=int(total_tokens or 0),
            latency_ms=int(latency_ms or 0),
            kind=str(kind or "chat"),
            messages=sanitize_messages_for_ledger(messages),
            response=None if response is None else str(response),
            requested_at=requested_at,
        )
    )


def assess_usage(
    usage: Any,
    *,
    content: str | None,
    finish_reason: str | None,
    reasoning_observed: bool = False,
    reasoning_expected: bool = False,
    reasoning_disabled: bool = False,
) -> UsageAssessment:
    """Separate exact billable totals from the optional reasoning breakout."""

    if usage is None:
        missing = ("prompt_tokens", "completion_tokens", "total_tokens")
        return UsageAssessment(
            None,
            None,
            None,
            None,
            None,
            False,
            missing,
            reasoning_expected=reasoning_expected,
            reasoning_detail_complete=not reasoning_expected,
            reasoning_detail_status="missing" if reasoning_expected else "not_expected",
            reasoning_tokens_source="missing" if reasoning_expected else "not_expected",
            missing_reasoning_detail_fields=("reasoning_tokens",)
            if reasoning_expected
            else (),
        )

    prompt_raw = _first_usage_value(usage, "prompt_tokens", "input_tokens")
    completion_raw = _first_usage_value(usage, "completion_tokens", "output_tokens")
    total_raw = _first_usage_value(usage, "total_tokens")
    prompt = _optional_usage_int(prompt_raw)
    completion = _optional_usage_int(completion_raw)
    total = _optional_usage_int(total_raw)
    prompt_details = _usage_value(usage, "prompt_tokens_details")
    input_details = _usage_value(usage, "input_tokens_details")
    completion_details = _usage_value(usage, "completion_tokens_details")
    output_details = _usage_value(usage, "output_tokens_details")
    reasoning_source = "missing"
    reasoning_raw = _usage_value(usage, "reasoning_tokens")
    if reasoning_raw is not None:
        reasoning_source = "top_level"
    else:
        reasoning_raw = _usage_value(completion_details, "reasoning_tokens")
        if reasoning_raw is not None:
            reasoning_source = "completion_tokens_details"
        else:
            reasoning_raw = _usage_value(output_details, "reasoning_tokens")
            if reasoning_raw is not None:
                reasoning_source = "output_tokens_details"
    reasoning = _optional_usage_int(reasoning_raw)
    cached = max(
        _usage_int(usage, "cached_prompt_tokens", "cached_tokens"),
        _usage_int(prompt_details, "cached_tokens"),
        _usage_int(input_details, "cached_tokens"),
    )

    empty = not str(content or "").strip()
    finish = str(finish_reason or "").strip().lower()
    reasoning_obvious = bool(
        not reasoning_disabled
        and (
            reasoning_expected
            or reasoning_observed
            or (reasoning or 0) > 0
            or (
                empty
                and (
                    finish in {"length", "max_tokens"}
                    or (completion or 0) > 0
                )
            )
        )
    )
    missing: list[str] = []
    if prompt is None:
        missing.append("prompt_tokens")
    if completion is None:
        missing.append("completion_tokens")
    if total is None:
        missing.append("total_tokens")
    elif (
        prompt is not None
        and completion is not None
        and total != prompt + completion
    ):
        # OpenAI-compatible usage defines total as input + output.  Accepting an
        # inconsistent aggregate would make top-level and partition ledgers
        # impossible to reconcile exactly.
        missing.append("total_tokens")
        total = None
    reasoning_detail_status = "exact"
    reasoning_detail_complete = True
    missing_reasoning: list[str] = []
    if reasoning_raw is None:
        if reasoning_obvious:
            reasoning = None
            reasoning_detail_status = "missing"
            reasoning_detail_complete = False
            missing_reasoning.append("reasoning_tokens")
        else:
            reasoning = 0
            reasoning_source = "not_expected"
            reasoning_detail_status = "not_expected"
    elif reasoning is None or (
        completion is not None and reasoning > completion
    ) or (reasoning_observed and reasoning <= 0):
        reasoning = None
        reasoning_detail_status = "invalid"
        reasoning_detail_complete = False
        missing_reasoning.append("reasoning_tokens")
    missing = list(dict.fromkeys(missing))
    return UsageAssessment(
        prompt_tokens=prompt,
        completion_tokens=completion,
        reasoning_tokens=reasoning,
        cached_prompt_tokens=cached,
        total_tokens=total,
        complete=not missing,
        missing_fields=tuple(missing),
        reasoning_expected=(reasoning_obvious or not reasoning_detail_complete),
        reasoning_detail_complete=reasoning_detail_complete,
        reasoning_detail_status=reasoning_detail_status,
        reasoning_tokens_source=reasoning_source,
        missing_reasoning_detail_fields=tuple(missing_reasoning),
    )


def record_usage_call(
    model: str,
    *,
    usage: Any,
    content: str | None,
    finish_reason: str | None,
    reasoning_observed: bool = False,
    reasoning_expected: bool = False,
    reasoning_disabled: bool = False,
    latency_ms: int,
    kind: str,
    messages: list[dict[str, Any]] | None = None,
    response: str | None = None,
    requested_at: str | None = None,
    endpoint: str | None = None,
    provider_reported_model: str | None = None,
    provider_response_id: str | None = None,
    system_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Record a call, failing only when primary billable usage is unknown."""

    assessment = assess_usage(
        usage,
        content=content,
        finish_reason=finish_reason,
        reasoning_observed=reasoning_observed,
        reasoning_expected=reasoning_expected,
        reasoning_disabled=reasoning_disabled,
    )
    meter = _current.get()
    if meter is not None:
        meter.record(
            CallRecord(
                model=str(model),
                prompt_tokens=assessment.prompt_tokens,
                completion_tokens=assessment.completion_tokens,
                reasoning_tokens=assessment.reasoning_tokens,
                cached_prompt_tokens=assessment.cached_prompt_tokens,
                total_tokens=assessment.total_tokens,
                latency_ms=int(latency_ms or 0),
                kind=str(kind or "chat"),
                usage_complete=assessment.complete,
                missing_usage_fields=assessment.missing_fields,
                reasoning_expected=assessment.reasoning_expected,
                reasoning_detail_complete=assessment.reasoning_detail_complete,
                reasoning_detail_status=assessment.reasoning_detail_status,
                reasoning_tokens_source=assessment.reasoning_tokens_source,
                missing_reasoning_detail_fields=(
                    assessment.missing_reasoning_detail_fields
                ),
                endpoint=endpoint,
                finish_reason=finish_reason,
                provider_reported_model=provider_reported_model,
                provider_response_id_sha256=_sha256_optional(provider_response_id),
                system_fingerprint=system_fingerprint,
                messages=sanitize_messages_for_ledger(messages),
                response=None if response is None else str(response),
                requested_at=requested_at,
            )
        )
    if not assessment.complete:
        raise UsageAccountingError(
            f"{kind} usage is incomplete for {model}: "
            + ", ".join(assessment.missing_fields)
        )
    return assessment.complete_fields()


def _sha256_optional(value: str | None) -> str | None:
    if value is None or not str(value):
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def response_has_reasoning_content(response: Any) -> bool:
    """Inspect only the presence bit of provider reasoning, never retain its text."""

    choices = _usage_value(response, "choices")
    if not isinstance(choices, (list, tuple)) or not choices:
        return False
    message = _usage_value(choices[0], "message")
    try:
        value = _usage_value(message, "reasoning_content")
    except Exception:
        # Exotic test doubles/proxies may deliberately forbid reading the field.
        # Real OpenAI-compatible response objects expose it as a normal attribute.
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return value is not None


def extract_usage(value: Any) -> Any:
    """Find provider usage attached to a response or raised SDK error."""

    direct = _usage_value(value, "usage")
    if direct is not None:
        return direct
    response = _usage_value(value, "response")
    nested = _usage_value(response, "usage")
    if nested is not None:
        return nested
    body = _usage_value(value, "body")
    return _usage_value(body, "usage")


def error_may_have_generated(error: BaseException) -> bool:
    """Whether an exception without usage may follow a billable provider request.

    Explicit rate/quota rejection, provider 5xx, and connection establishment
    failure are treated as pre-generation infrastructure failures. Read/API and
    parent wall-clock timeouts are ambiguous: the request may have completed at
    the provider after the caller stopped waiting, so formal accounting must add
    an unknown, permanently incomplete attempt.
    """

    name = type(error).__name__.lower()
    text = str(error).lower()
    status = _error_status(error)
    connect_failure = any(
        marker in name or marker in text
        for marker in (
            "connecterror",
            "connectionerror",
            "connecttimeout",
            "connection refused",
            "failed to connect",
            "could not connect",
            "name resolution",
            "dns failure",
        )
    )
    if connect_failure:
        return False
    if status == 429 or (isinstance(status, int) and status >= 500):
        return False
    if any(
        marker in text
        for marker in (
            "rate limit",
            "ratelimit",
            "quota exceeded",
            "insufficient quota",
            "too many requests",
        )
    ):
        return False
    return bool(
        status == 408
        or "timeout" in name
        or "timed out" in text
        or "read timeout" in text
        or "api timeout" in text
    )


def record_error_usage_call(
    model: str,
    *,
    error: BaseException,
    latency_ms: int,
    kind: str,
    messages: list[dict[str, Any]] | None = None,
    requested_at: str | None = None,
    endpoint: str = "chat.completions",
) -> dict[str, Any] | None:
    """Record provider-error usage, or an unknown row for ambiguous timeouts.

    Returns ``None`` for a definite pre-generation failure. Incomplete/unknown
    usage is recorded first and then raises :class:`UsageAccountingError` via
    :func:`record_usage_call`.
    """

    usage = extract_usage(error)
    if usage is None and not error_may_have_generated(error):
        return None
    try:
        return record_usage_call(
            model,
            usage=usage,
            content=None,
            finish_reason=None,
            latency_ms=latency_ms,
            kind=kind,
            messages=messages,
            response=None,
            requested_at=requested_at,
            endpoint=endpoint,
        )
    except UsageAccountingError as accounting_error:
        if usage is None:
            raise UsageAccountingError(
                f"{kind} provider request timed out after dispatch; token usage is unknown"
            ) from accounting_error
        raise


def _error_status(error: BaseException) -> int | None:
    for container in (error, _usage_value(error, "response")):
        for name in ("status_code", "status"):
            value = _usage_value(container, name)
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                continue
    return None


def usage_snapshot(usage: Any) -> dict[str, Any] | None:
    """Return a process-safe usage mapping without inventing absent fields.

    ``usage_tokens`` is intentionally convenient for non-formal diagnostics and
    normalizes missing values to zero.  Formal paid-call accounting must preserve
    absence, especially when an isolated worker sends usage back to its parent.
    This helper copies only the usage fields understood by :func:`assess_usage`
    and keeps missing fields missing so the parent can fail closed.
    """

    if usage is None:
        return None
    snapshot: dict[str, Any] = {}
    for name in (
        "prompt_tokens",
        "input_tokens",
        "completion_tokens",
        "output_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cached_prompt_tokens",
        "cached_tokens",
    ):
        value = _usage_value(usage, name)
        if value is not None:
            snapshot[name] = _portable_usage_value(value)
    for name, fields in (
        ("prompt_tokens_details", ("cached_tokens",)),
        ("input_tokens_details", ("cached_tokens",)),
        ("completion_tokens_details", ("reasoning_tokens",)),
        ("output_tokens_details", ("reasoning_tokens",)),
    ):
        details = _usage_value(usage, name)
        if details is None:
            continue
        copied: dict[str, Any] = {}
        for field_name in fields:
            value = _usage_value(details, field_name)
            if value is not None:
                copied[field_name] = _portable_usage_value(value)
        snapshot[name] = copied
    return snapshot


def _portable_usage_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def mark_records() -> tuple[CostMeter, int] | None:
    meter = _current.get()
    return (meter, len(meter.records)) if meter is not None else None


def reclassify_since(
    marker: tuple[CostMeter, int] | None,
    *,
    model: str,
    from_kind: str,
    to_kind: str,
) -> None:
    """Reclassify all retry records belonging to one immediately-following call."""

    if marker is None:
        return
    meter, start = marker
    for record in meter.records[start:]:
        if record.model == model and record.kind == from_kind:
            record.kind = to_kind


def usage_tokens(usage: Any) -> dict[str, int]:
    """Normalize token usage from OpenAI-compatible response objects or mappings.

    Gateways vary between ``prompt/completion`` and ``input/output`` field names,
    and expose cache/reasoning details as either nested objects or dictionaries.
    The returned reasoning and cache values are informational subsets; callers
    must not add them to ``total_tokens``.
    """

    prompt_tokens = _usage_int(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _usage_int(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_int(usage, "total_tokens")
    if not total_tokens and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens

    prompt_details = _usage_value(usage, "prompt_tokens_details")
    input_details = _usage_value(usage, "input_tokens_details")
    completion_details = _usage_value(usage, "completion_tokens_details")
    output_details = _usage_value(usage, "output_tokens_details")
    cached_prompt_tokens = max(
        _usage_int(usage, "cached_prompt_tokens", "cached_tokens"),
        _usage_int(prompt_details, "cached_tokens"),
        _usage_int(input_details, "cached_tokens"),
    )
    reasoning_tokens = max(
        _usage_int(usage, "reasoning_tokens"),
        _usage_int(completion_details, "reasoning_tokens"),
        _usage_int(output_details, "reasoning_tokens"),
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "total_tokens": total_tokens,
    }


def _usage_value(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None) if value is not None else None


def _first_usage_value(
    usage: Any,
    *names: str,
    containers: tuple[Any, ...] = (),
) -> Any:
    for container in (usage, *containers):
        for name in names:
            value = _usage_value(container, name)
            if value is not None:
                return value
    return None


def _optional_usage_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError):
        return None


def _usage_int(value: Any, *names: str) -> int:
    for name in names:
        raw = _usage_value(value, name)
        if raw is None:
            continue
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return 0


def estimate_usd(prompt_tokens: int, completion_tokens: int) -> float | None:
    rate = os.environ.get("WARPTEST_LLM_USD_PER_MTOK")
    if not rate:
        return None
    try:
        per_mtok = float(rate)
    except ValueError:
        return None
    return round((int(prompt_tokens) + int(completion_tokens)) / 1_000_000 * per_mtok, 6)


def _parse_rate(value: Any) -> "float | dict[str, float] | None":
    """A rate is USD per million tokens: a flat number, or a per-side mapping.

    ``{"prompt": x, "completion": y}`` lets prompt and completion be priced
    differently (real gateway pricing usually differs by side).
    """

    if isinstance(value, dict):
        sided: dict[str, float] = {}
        for side in ("prompt", "completion"):
            raw = value.get(side)
            if raw is None:
                continue
            try:
                sided[side] = float(raw)
            except (TypeError, ValueError):
                continue
        return sided or None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _model_rate_table() -> dict[str, "float | dict[str, float]"]:
    """Per-model USD/Mtok table from ``WARPTEST_LLM_USD_PER_MTOK_BY_MODEL`` (JSON).

    Values may be a flat number or ``{"prompt": x, "completion": y}``. A ``default``
    key applies to any model not listed explicitly.
    """

    raw = os.environ.get("WARPTEST_LLM_USD_PER_MTOK_BY_MODEL")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    table: dict[str, Any] = {}
    for key, value in data.items():
        parsed = _parse_rate(value)
        if parsed is not None:
            table[str(key)] = parsed
    return table


def _resolve_model_rate(
    model: str, table: "dict[str, float | dict[str, float]] | None" = None
) -> "float | dict[str, float] | None":
    table = _model_rate_table() if table is None else table
    if model in table:
        return table[model]
    if "default" in table:
        return table["default"]
    flat = os.environ.get("WARPTEST_LLM_USD_PER_MTOK")
    return _parse_rate(flat) if flat else None


def _model_est_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    table: "dict[str, float | dict[str, float]] | None" = None,
) -> float | None:
    """Unrounded USD estimate for one model's tokens, or None if no rate applies."""

    rate = _resolve_model_rate(model, table)
    if rate is None:
        return None
    if isinstance(rate, dict):
        usd = 0.0
        if "prompt" in rate:
            usd += int(prompt_tokens) / 1_000_000 * rate["prompt"]
        if "completion" in rate:
            usd += int(completion_tokens) / 1_000_000 * rate["completion"]
        return usd
    return (int(prompt_tokens) + int(completion_tokens)) / 1_000_000 * float(rate)
