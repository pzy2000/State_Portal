"""Aggregate P5 arm reports into one evidence summary (C1 / C2 / C3).

Consumes the raw reports produced by the existing benchmarks (state-loop,
gui-agent, evo phase3 suite, memory-baseline suite, state-fuzzing) and folds them
into a single ``p5-scale-summary.json`` plus Markdown, with bootstrap CIs and
paired significance tests attached.

Design choices that encode the agreed P5 boundaries:

* C1: A/B and C/D isolate checkpoint benefit within one GUI policy; A/C and B/D
  test whether Agent-S changes the conclusion. E (state-managed) appears only in
  the same-task token/time/success Pareto. Trial identity is
  ``(arm, model, seed, task_id)``; McNemar uses all paired seed outcomes while
  effect CIs resample task clusters. Unverified USD rates never enter the main
  table.
* C2 (transfer): main metric is held-out strict success; the gain is reported
  both vs A5 (evolution_no_memory) from the phase3 suite and vs A-MEM / Mem0 from
  the memory-baseline suite. Scaffold success is carried only as a gap diagnostic.
* C3 (robustness): main metric is UNIQUE defects (deduped by fingerprint across
  seeds); raw defect mutations, robust, rejected, defect types, and the
  browser/engine defect-path split are kept as transparency counters.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import subprocess
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

import yaml

from . import cost, p5_stats, run_context
from .lazarus_aggregate import lazarus_from_reports  # noqa: F401 - public Lazarus API
from .c1_budgets import C1_HORIZON_CLASSES, C1_STEP_BUDGET, C1_STEP_BUDGETS
from .agent_s_bridge import (
    AGENT_S_CONTROL_REQUEST_TIMEOUT_S,  # noqa: F401 - public provenance constant
    AGENT_S_MAX_PROVIDER_ATTEMPTS_PER_PREDICT,
    AGENT_S_PREDICT_TIMEOUT_MODE,
    AGENT_S_PROVIDER_TIMEOUT_S,
    AGENT_S_PROTOCOL_VERSION,
    PINNED_AGENT_S_COMMIT,
    PINNED_AGENT_S_VERSION,
)
from .c1_experiment import (
    calibration_grounder_model,
    grounder_follows_decision_model,
    grounder_preflight_probes,
    resolve_grounder_model,
)
from .c1_evaluation import formal_surface_contract
from .c1_provenance import (
    c1_application_revisions,
    c1_harness_hash,
    c1_task_manifest_hash,
    validate_c1_environment,
)
from .c1_provider_runtime import manifest_provider_timeout_s
from .c1_recovery_contract import (
    BACKGROUND_RESOURCE_MIGRATION_ID,
    JUPYTER_RUNTIME_RECOVERY_STAGE,
    JYNEW_EXIT_TIMEOUT_RECOVERY_STAGE,
    SCREEN_SIZE_PROBE_CACHE_RECOVERY_STAGE,
    TERMINAL_LEDGER_RECOVERY_STAGE,
    TrialKey,
    background_recovery_stage,
    interrupted_unknown_usage_snapshot as _interrupted_unknown_usage_snapshot,
)
from .c1_state_metrics import (
    STATE_STAGE_METRICS_SCHEMA_VERSION,
    derive_state_stage_metrics,
    explicit_state_stage_metric_issues,
)
from .c1_golden_artifacts import (
    OracleGoldenArtifactValidationCache,
    validate_oracle_golden_artifacts,
)
from .model_request import ModelRequestProfile, PROVIDER_DEFAULT, THINKING_DISABLED
from .runner import load_spec
from .state_loop import (
    _apply_experiment_seed,
    _oracle_fingerprint,
    _oracle_spec_digest,
    _repo_git_commit,
    load_state_loop_tasks,
)
from .task_schema import resolve_task_repo


_RUNTIME_LOCK_SCOPE = "machine-shared"


def _trial_key(arm: str, row: dict[str, Any]) -> TrialKey:
    """Return the canonical immutable identity for one C1 index row."""

    return TrialKey.from_row(arm, row)


def load_report(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- C1


def _task_cost(result: dict[str, Any]) -> dict[str, Any]:
    cost = result.get("cost") if isinstance(result.get("cost"), dict) else {}
    return {
        "est_usd": cost.get("est_usd"),
        "llm_calls": cost.get("llm_calls"),
        "prompt_tokens": cost.get("prompt_tokens"),
        "completion_tokens": cost.get("completion_tokens"),
        "total_tokens": cost.get("total_tokens"),
        "llm_latency_ms": cost.get("llm_latency_ms"),
        "reasoning_tokens": cost.get("reasoning_tokens"),
        "reasoning_tokens_observed": cost.get("reasoning_tokens_observed"),
        "reasoning_tokens_lower_bound": cost.get("reasoning_tokens_lower_bound"),
        "reasoning_tokens_upper_bound": cost.get("reasoning_tokens_upper_bound"),
        "reasoning_detail_complete": cost.get("reasoning_detail_complete"),
        "reasoning_detail_unknown_fraction": cost.get(
            "reasoning_detail_unknown_fraction"
        ),
        "reasoning_detail_within_tolerance": cost.get(
            "reasoning_detail_within_tolerance"
        ),
        "cached_prompt_tokens": cost.get("cached_prompt_tokens"),
        "by_kind": cost.get("by_kind") if isinstance(cost.get("by_kind"), dict) else {},
        "by_model": cost.get("by_model") if isinstance(cost.get("by_model"), dict) else {},
        "by_kind_model": cost.get("by_kind_model") if isinstance(cost.get("by_kind_model"), dict) else {},
    }


_COST_PARTITION_FIELDS = {
    "calls": "llm_calls",
    "prompt_tokens": "prompt_tokens",
    "completion_tokens": "completion_tokens",
    "reasoning_tokens_observed": "reasoning_tokens_observed",
    "reasoning_tokens_lower_bound": "reasoning_tokens_lower_bound",
    "reasoning_tokens_upper_bound": "reasoning_tokens_upper_bound",
    "cached_prompt_tokens": "cached_prompt_tokens",
    "total_tokens": "total_tokens",
    "latency_ms": "llm_latency_ms",
    "incomplete_calls": "incomplete_calls",
    "unknown_prompt_token_calls": "unknown_prompt_token_calls",
    "unknown_completion_token_calls": "unknown_completion_token_calls",
    "unknown_reasoning_token_calls": "unknown_reasoning_token_calls",
    "unknown_total_token_calls": "unknown_total_token_calls",
    "reasoning_detail_missing_calls": "reasoning_detail_missing_calls",
    "reasoning_detail_unknown_completion_tokens": "reasoning_detail_unknown_completion_tokens",
    "reasoning_detail_expected_completion_tokens": "reasoning_detail_expected_completion_tokens",
}


def _cost_leaf_buckets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    if "calls" in value or "total_tokens" in value:
        return [value]
    leaves: list[dict[str, Any]] = []
    for child in value.values():
        leaves.extend(_cost_leaf_buckets(child))
    return leaves


def _cost_summary_issues(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["missing cost summary"]
    issues: list[str] = []

    def validate_tokens(bucket: dict[str, Any], label: str, *, top: bool) -> None:
        names = {
            "calls": "llm_calls" if top else "calls",
            "prompt": "prompt_tokens",
            "completion": "completion_tokens",
            "cached": "cached_prompt_tokens",
            "total": "total_tokens",
            "latency": "llm_latency_ms" if top else "latency_ms",
        }
        parsed = {key: _integer(bucket.get(field)) for key, field in names.items()}
        for key, number in parsed.items():
            if number is None or number < 0:
                issues.append(f"{label} has invalid non-negative integer {names[key]}")
        if all(parsed[key] is not None for key in ("prompt", "completion", "total")):
            if parsed["total"] != parsed["prompt"] + parsed["completion"]:
                issues.append(f"{label} total_tokens does not equal prompt + completion")
        reasoning = bucket.get("reasoning_tokens")
        if reasoning is not None:
            parsed_reasoning = _integer(reasoning)
            if parsed_reasoning is None or parsed_reasoning < 0:
                issues.append(f"{label} has invalid reasoning_tokens")
            elif (
                parsed["completion"] is not None
                and parsed_reasoning > parsed["completion"]
            ):
                issues.append(f"{label} reasoning_tokens exceeds completion_tokens")
        if (
            parsed["cached"] is not None
            and parsed["prompt"] is not None
            and parsed["cached"] > parsed["prompt"]
        ):
            issues.append(f"{label} cached_prompt_tokens exceeds prompt_tokens")
        if "reasoning_detail_complete" not in bucket:
            return
        detail_fields = {
            "observed": "reasoning_tokens_observed",
            "lower": "reasoning_tokens_lower_bound",
            "upper": "reasoning_tokens_upper_bound",
            "missing_calls": "reasoning_detail_missing_calls",
            "unknown_completion": "reasoning_detail_unknown_completion_tokens",
            "expected_completion": "reasoning_detail_expected_completion_tokens",
            "unknown_calls": "unknown_reasoning_token_calls",
        }
        detail = {
            name: _integer(bucket.get(field)) for name, field in detail_fields.items()
        }
        for name, number in detail.items():
            if number is None or number < 0:
                issues.append(
                    f"{label} has invalid non-negative integer {detail_fields[name]}"
                )
        if all(number is not None for number in detail.values()):
            if detail["lower"] != detail["observed"]:
                issues.append(f"{label} reasoning lower bound is not observed total")
            if detail["upper"] != detail["observed"] + detail["unknown_completion"]:
                issues.append(f"{label} reasoning upper bound is inconsistent")
            if detail["unknown_calls"] != detail["missing_calls"]:
                issues.append(f"{label} reasoning missing-call counters disagree")
            expected_fraction = (
                detail["unknown_completion"] / detail["expected_completion"]
                if detail["expected_completion"] > 0
                else 0.0
            )
            fraction = _number(bucket.get("reasoning_detail_unknown_fraction"))
            if fraction is None or not math.isclose(
                fraction, expected_fraction, rel_tol=0.0, abs_tol=1e-12
            ):
                issues.append(f"{label} reasoning missing fraction is inconsistent")
            tolerance = _number(bucket.get("reasoning_detail_tolerance_fraction"))
            if tolerance is None or not math.isclose(
                tolerance,
                cost.REASONING_DETAIL_TOLERANCE_FRACTION,
                rel_tol=0.0,
                abs_tol=0.0,
            ):
                issues.append(f"{label} reasoning tolerance is not registered v2 value")
            if bucket.get("reasoning_detail_complete") is not (
                detail["missing_calls"] == 0
            ):
                issues.append(f"{label} reasoning detail completeness is inconsistent")
            if bucket.get("reasoning_detail_within_tolerance") is not (
                expected_fraction <= cost.REASONING_DETAIL_TOLERANCE_FRACTION
            ):
                issues.append(f"{label} reasoning threshold status is inconsistent")
            expected_exact = (
                detail["observed"] if detail["missing_calls"] == 0 else None
            )
            if bucket.get("reasoning_tokens") != expected_exact:
                issues.append(f"{label} publishes a pseudo-exact reasoning total")

    validate_tokens(value, "cost summary", top=True)
    if value.get("usage_complete") is not True:
        issues.append("cost usage accounting is incomplete")
    for field in (
        "incomplete_calls",
        "unknown_prompt_token_calls",
        "unknown_completion_token_calls",
        "unknown_total_token_calls",
    ):
        if _integer(value.get(field)) != 0:
            issues.append(f"cost {field} must be zero")
    for partition in ("by_model", "by_kind", "by_kind_model"):
        mapping = value.get(partition)
        if not isinstance(mapping, dict):
            issues.append(f"missing v2 cost breakdown: {partition}")
            continue
        leaves = _cost_leaf_buckets(mapping)
        for index, bucket in enumerate(leaves):
            validate_tokens(bucket, f"{partition} bucket {index}", top=False)
            if bucket.get("usage_complete") is not True:
                issues.append(f"{partition} contains incomplete usage bucket")
            if _integer(bucket.get("incomplete_calls")) != 0:
                issues.append(f"{partition} contains incomplete calls")
            fields = (
                _COST_PARTITION_FIELDS
                if "reasoning_detail_complete" in value
                else {
                    key: val
                    for key, val in _COST_PARTITION_FIELDS.items()
                    if not key.startswith("reasoning_")
                }
            )
            for field in fields:
                if _integer(bucket.get(field)) is None:
                    issues.append(f"{partition} bucket missing {field}")
                    break
        fields = (
            _COST_PARTITION_FIELDS
            if "reasoning_detail_complete" in value
            else {
                key: val
                for key, val in _COST_PARTITION_FIELDS.items()
                if not key.startswith("reasoning_")
            }
        )
        for bucket_field, top_field in fields.items():
            top_value = _integer(value.get(top_field))
            if top_value is None:
                issues.append(f"cost summary missing integer {top_field}")
                continue
            partition_total = sum(_integer(bucket.get(bucket_field)) or 0 for bucket in leaves)
            if partition_total != top_value:
                issues.append(
                    f"cost {partition}.{bucket_field} total {partition_total} "
                    f"does not match {top_field} {top_value}"
                )
    by_kind = value.get("by_kind")
    by_model = value.get("by_model")
    by_kind_model = value.get("by_kind_model")
    if (
        isinstance(by_kind, dict)
        and isinstance(by_model, dict)
        and isinstance(by_kind_model, dict)
    ):
        if set(by_kind_model) != set(by_kind):
            issues.append("cost by_kind_model kind keys do not match by_kind")
        nested_models = {
            str(model)
            for model_map in by_kind_model.values()
            if isinstance(model_map, dict)
            for model in model_map
        }
        if nested_models != {str(model) for model in by_model}:
            issues.append("cost by_kind_model model keys do not match by_model")
        fields = (
            _COST_PARTITION_FIELDS
            if "reasoning_detail_complete" in value
            else {
                key: val
                for key, val in _COST_PARTITION_FIELDS.items()
                if not key.startswith("reasoning_")
            }
        )
        for bucket_field in fields:
            for kind, kind_bucket in by_kind.items():
                if not isinstance(kind_bucket, dict):
                    continue
                nested = by_kind_model.get(kind)
                nested_total = sum(
                    _integer(bucket.get(bucket_field)) or 0
                    for bucket in (nested.values() if isinstance(nested, dict) else [])
                    if isinstance(bucket, dict)
                )
                if nested_total != (_integer(kind_bucket.get(bucket_field)) or 0):
                    issues.append(
                        f"cost by_kind_model {kind}.{bucket_field} marginal "
                        "does not match by_kind"
                    )
            for model, model_bucket in by_model.items():
                if not isinstance(model_bucket, dict):
                    continue
                nested_total = sum(
                    _integer(model_map.get(model, {}).get(bucket_field)) or 0
                    for model_map in by_kind_model.values()
                    if isinstance(model_map, dict)
                    and isinstance(model_map.get(model), dict)
                )
                if nested_total != (_integer(model_bucket.get(bucket_field)) or 0):
                    issues.append(
                        f"cost by_kind_model {model}.{bucket_field} marginal "
                        "does not match by_model"
                    )
    return list(dict.fromkeys(issues))


def _cost_signature(value: Any) -> tuple[int, ...] | None:
    """Return the auditable top-level usage counters for exact comparisons."""

    if not isinstance(value, dict):
        return None
    fields = _cost_signature_fields(value)
    parsed = tuple(_integer(value.get(field)) for field in fields)
    if any(number is None for number in parsed):
        return None
    return tuple(int(number) for number in parsed if number is not None)


def _cost_signature_fields(value: dict[str, Any]) -> tuple[str, ...]:
    if "reasoning_detail_complete" in value:
        return tuple(_COST_PARTITION_FIELDS.values())
    return (
        "llm_calls",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cached_prompt_tokens",
        "total_tokens",
        "llm_latency_ms",
        "incomplete_calls",
        "unknown_prompt_token_calls",
        "unknown_completion_token_calls",
        "unknown_reasoning_token_calls",
        "unknown_total_token_calls",
    )


def _cost_is_zero(value: Any) -> bool:
    signature = _cost_signature(value)
    return signature is not None and not any(signature)


def _cost_component_issues(
    component: Any,
    total: Any,
    *,
    component_label: str,
    total_label: str = "trial cost",
) -> list[str]:
    """Require a component ledger to be a component-wise subset of its total."""

    component_signature = _cost_signature(component)
    total_signature = _cost_signature(total)
    if component_signature is None or total_signature is None:
        return [f"cannot compare {component_label} with {total_label}"]
    issues: list[str] = []
    component_fields = _cost_signature_fields(component)
    total_fields = _cost_signature_fields(total)
    if component_fields != total_fields:
        return [f"cannot compare {component_label} with {total_label} schemas"]
    for field, component_value, total_value in zip(
        component_fields, component_signature, total_signature
    ):
        if component_value > total_value:
            issues.append(
                f"{component_label} {field} exceeds {total_label} {field}"
            )
    return issues


_COST_LEDGER_FIELDS = (
    "llm_calls",
    "prompt_tokens",
    "completion_tokens",
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
    "cached_prompt_tokens",
    "total_tokens",
    "llm_latency_ms",
    "est_usd",
    "usage_complete",
    "incomplete_calls",
    "unknown_prompt_token_calls",
    "unknown_completion_token_calls",
    "unknown_reasoning_token_calls",
    "unknown_total_token_calls",
    "by_model",
    "by_kind",
    "by_kind_model",
)


def _cost_ledger_view(value: Any) -> dict[str, Any] | None:
    """Drop attempt metadata while retaining every auditable cost partition."""

    if not isinstance(value, dict):
        return None
    return {field: value.get(field) for field in _COST_LEDGER_FIELDS}


def _compact_attempt_costs(value: Any) -> list[dict[str, Any]] | None:
    """Keep aggregate-relevant attempt provenance without duplicating call logs."""

    if not isinstance(value, list):
        return None
    compact: list[dict[str, Any]] = []
    for attempt in value:
        if not isinstance(attempt, dict):
            continue
        accounting = attempt.get("synthesis_accounting")
        compact_accounting = None
        if isinstance(accounting, dict):
            compact_accounting = {
                field: _cost_ledger_view(accounting.get(field))
                for field in (
                    "actual_cost",
                    "transient_actual_cost",
                    "attributed_cost",
                )
            }
        compact.append(
            {
                **_cost_ledger_view(attempt),
                "attempt_ordinal": attempt.get("attempt_ordinal"),
                "attempt_status": attempt.get("attempt_status"),
                "retry_category": attempt.get("retry_category"),
                "retry_reason": attempt.get("retry_reason"),
                "previous_attempt_hash": attempt.get("previous_attempt_hash"),
                "attempt_hash": attempt.get("attempt_hash"),
                "task_actual_cost": _cost_ledger_view(
                    attempt.get("task_actual_cost")
                ),
                "synthesis_accounting": compact_accounting,
            }
        )
    return compact


def _preserved_model_failure_kind(
    *, result: dict[str, Any], report: dict[str, Any]
) -> str | None:
    """Recognize exact-usage, non-infrastructure settled model failures."""

    actual_cost = result.get("actual_cost")
    if not isinstance(actual_cost, dict):
        actual_cost = result.get("cost")
    if not (
        isinstance(actual_cost, dict)
        and actual_cost.get("usage_complete") is True
        and all(
            actual_cost.get(field) == 0
            for field in (
                "unknown_prompt_token_calls",
                "unknown_completion_token_calls",
                "unknown_total_token_calls",
            )
        )
        and result.get("incomplete") is True
        and report.get("complete") is False
    ):
        return None
    attempts = result.get("attempt_costs")
    latest_attempt = attempts[-1] if isinstance(attempts, list) and attempts else None
    if (
        result.get("retryable_incomplete") is True
        and result.get("infrastructure_error") in {True, None}
        and report.get("retryable_incomplete") is True
        and isinstance(latest_attempt, dict)
        and latest_attempt.get("attempt_status") == "retryable_infrastructure"
        and latest_attempt.get("retry_category") == "infra"
        and isinstance(latest_attempt.get("retry_reason"), str)
        and bool(latest_attempt["retry_reason"].strip())
    ):
        return "exact_usage_exhausted_retryable_infrastructure"
    explicit_nonretryable = bool(
        result.get("retryable_incomplete") is False
        and result.get("infrastructure_error") is False
        and report.get("retryable_incomplete") is False
        and isinstance(result.get("reason"), str)
        and bool(result["reason"].strip())
    )
    ledger_nonretryable = bool(
        result.get("retryable_incomplete") is False
        and result.get("infrastructure_error") in {False, None}
        and result.get("timed_out") is not True
        and report.get("retryable_incomplete") is False
        and report.get("incomplete_category")
        == "nonretryable_incomplete_evidence"
        and isinstance(latest_attempt, dict)
        and latest_attempt.get("attempt_status") == "nonretryable_incomplete"
        and latest_attempt.get("retry_category") is None
        and latest_attempt.get("retry_reason") is None
    )
    if not (explicit_nonretryable or ledger_nonretryable):
        return None
    return (
        "exact_usage_nonretryable_task_timeout"
        if result.get("timed_out") is True
        else "exact_usage_nonretryable_model_failure"
    )


def _attempt_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _c1_attempt_cost_issues(
    result: dict[str, Any],
    *,
    is_state: bool,
    allow_nonretryable_terminal: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    """Recompute primary, actual, and transient cost from the attempt ledger.

    The final treatment attempt supplies the method-comparison burden. Earlier
    usage-complete infrastructure attempts are real provider spend, but are
    reported separately so a random gateway outage cannot penalize one arm.
    GUI synthesis keeps its existing counterfactual attribution semantics.
    """

    issues: list[str] = []
    attempts = result.get("attempt_costs")
    attempt_count = _integer(result.get("attempt_count"))
    if not isinstance(attempts, list) or not attempts:
        issues.append("missing non-empty attempt_costs ledger")
        attempts = []
    if attempt_count is None or attempt_count < 1:
        issues.append("missing positive integer attempt_count")
    elif attempt_count != len(attempts):
        issues.append("attempt_count does not equal len(attempt_costs)")

    task_actual_costs: list[dict[str, Any]] = []
    attempt_synthesis_actual: list[dict[str, Any]] = []
    attempt_synthesis_transient: list[dict[str, Any]] = []
    attempt_synthesis_attributed: list[dict[str, Any]] = []
    attempt_hashes: list[str] = []
    previous_attempt_hash: str | None = None
    for ordinal, raw_attempt in enumerate(attempts):
        ordinal_1 = ordinal + 1
        if not isinstance(raw_attempt, dict):
            issues.append(f"attempt_costs[{ordinal}] is not an object")
            continue
        issues.extend(
            f"attempt_costs[{ordinal}]: {issue}"
            for issue in _cost_summary_issues(raw_attempt)
        )
        if _integer(raw_attempt.get("attempt_ordinal")) != ordinal_1:
            issues.append(f"attempt_costs[{ordinal}] has invalid attempt_ordinal")
        expected_status = (
            (
                "nonretryable_incomplete"
                if allow_nonretryable_terminal
                else "treatment"
            )
            if ordinal == len(attempts) - 1
            else "retryable_infrastructure"
        )
        if raw_attempt.get("attempt_status") != expected_status:
            issues.append(
                f"attempt_costs[{ordinal}] status must be {expected_status}"
            )
        retry_category = raw_attempt.get("retry_category")
        retry_reason = raw_attempt.get("retry_reason")
        if expected_status == "retryable_infrastructure":
            if retry_category not in {"infra", "rate_limit"}:
                issues.append(
                    f"attempt_costs[{ordinal}] has invalid retry_category"
                )
            if not isinstance(retry_reason, str) or not retry_reason.strip():
                issues.append(f"attempt_costs[{ordinal}] is missing retry_reason")
        elif retry_category is not None or retry_reason is not None:
            issues.append(
                f"attempt_costs[{ordinal}] {expected_status} must not carry retry metadata"
            )
        task_actual = raw_attempt.get("task_actual_cost")
        issues.extend(
            f"attempt_costs[{ordinal}].task_actual_cost: {issue}"
            for issue in _cost_summary_issues(task_actual)
        )
        if isinstance(task_actual, dict):
            task_actual_costs.append(task_actual)
        # Every synthesizing arm, state included, carries the same ledger so the
        # cost axis stays comparable when one arm's checkpoint came from cache.
        accounting = raw_attempt.get("synthesis_accounting")
        if not isinstance(accounting, dict):
            issues.append(
                f"attempt_costs[{ordinal}] is missing synthesis_accounting"
            )
            accounting = {}
        actual_synthesis = accounting.get("actual_cost")
        transient_synthesis = accounting.get("transient_actual_cost")
        attributed = accounting.get("attributed_cost")
        for label, summary, sink in (
            ("actual_cost", actual_synthesis, attempt_synthesis_actual),
            (
                "transient_actual_cost",
                transient_synthesis,
                attempt_synthesis_transient,
            ),
            ("attributed_cost", attributed, attempt_synthesis_attributed),
        ):
            issues.extend(
                f"attempt_costs[{ordinal}].synthesis_accounting.{label}: {issue}"
                for issue in _cost_summary_issues(summary)
            )
            if isinstance(summary, dict):
                sink.append(summary)
        expected_attempt = (
            cost.merge_summaries([task_actual, attributed])
            if isinstance(task_actual, dict) and isinstance(attributed, dict)
            else None
        )
        if (
            expected_attempt is not None
            and _cost_ledger_view(raw_attempt) != _cost_ledger_view(expected_attempt)
        ):
            issues.append(
                f"attempt_costs[{ordinal}] does not equal task actual plus "
                "attempt synthesis attribution"
            )
        if raw_attempt.get("previous_attempt_hash") != previous_attempt_hash:
            issues.append(
                f"attempt_costs[{ordinal}] previous_attempt_hash mismatch"
            )
        hash_payload = {
            "task_id": str(result.get("task_id") or ""),
            "attempt_ordinal": raw_attempt.get("attempt_ordinal"),
            "attempt_status": raw_attempt.get("attempt_status"),
            "retry_category": raw_attempt.get("retry_category"),
            "retry_reason": raw_attempt.get("retry_reason"),
            "previous_attempt_hash": raw_attempt.get("previous_attempt_hash"),
            "task_actual_cost": raw_attempt.get("task_actual_cost"),
            "synthesis_accounting": raw_attempt.get("synthesis_accounting"),
        }
        expected_attempt_hash = _attempt_sha256(hash_payload)
        if raw_attempt.get("attempt_hash") != expected_attempt_hash:
            issues.append(f"attempt_costs[{ordinal}] attempt_hash mismatch")
        attempt_hashes.append(expected_attempt_hash)
        previous_attempt_hash = expected_attempt_hash

    expected_ledger_hash = _attempt_sha256(
        {
            "task_id": str(result.get("task_id") or ""),
            "attempt_hashes": attempt_hashes,
        }
    )
    if result.get("attempt_ledger_hash") != expected_ledger_hash:
        issues.append("attempt_ledger_hash mismatch")

    empty = cost.CostMeter().summary()
    if len(task_actual_costs) != len(attempts) or not task_actual_costs:
        expected_primary = empty
        expected_actual = empty
        expected_transient = empty
    else:
        synthesis_attributed = cost.merge_summaries(
            attempt_synthesis_attributed
        )
        synthesis_actual = cost.merge_summaries(attempt_synthesis_actual)
        synthesis_transient = cost.merge_summaries(
            attempt_synthesis_transient
        )
        for field, expected in (
            ("synthesis_attributed_cost", synthesis_attributed),
            ("synthesis_actual_cost", synthesis_actual),
            ("synthesis_transient_actual_cost", synthesis_transient),
        ):
            observed = result.get(field)
            if _cost_ledger_view(observed) != _cost_ledger_view(expected):
                issues.append(f"{field} does not match attempt synthesis ledger")
        expected_primary = cost.merge_summaries(
            [task_actual_costs[-1], synthesis_attributed]
        )
        expected_actual = cost.merge_summaries(
            [*task_actual_costs, synthesis_actual]
        )
        expected_transient = cost.merge_summaries(
            [*task_actual_costs[:-1], synthesis_transient]
        )

    for field, expected in (
        ("cost", expected_primary),
        ("actual_cost", expected_actual),
        ("transient_infrastructure_cost", expected_transient),
    ):
        observed = result.get(field)
        issues.extend(
            f"{field}: {issue}" for issue in _cost_summary_issues(observed)
        )
        if _cost_ledger_view(observed) != _cost_ledger_view(expected):
            issues.append(f"{field} does not match the recomputed attempt ledger")

    return expected_primary, expected_actual, expected_transient, list(
        dict.fromkeys(issues)
    )


def _local_settled_terminal_evidence(
    *,
    arm: str,
    entry: dict[str, Any],
    report: dict[str, Any],
    actual_report_sha: str,
    has_current_overhead: bool,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate a local exact-usage terminal model failure end to end."""

    issues: list[str] = []
    if (
        entry.get("status") != "error"
        or not isinstance(entry.get("replacement_for"), dict)
        or entry.get("source_provenance") is not None
        or entry.get("imported") is True
    ):
        issues.append("invalid local terminal row provenance")
    if entry.get("report_sha256") != actual_report_sha:
        issues.append("report_sha256 mismatch")
    results = report.get("results")
    result = (
        results[0]
        if isinstance(results, list)
        and len(results) == 1
        and isinstance(results[0], dict)
        else None
    )
    if result is None:
        issues.append("missing exact one-trial result")
    elif result.get("task_id") != entry.get("task_id"):
        issues.append("terminal result task_id mismatch")
    elif (
        _preserved_model_failure_kind(result=result, report=report)
        != "exact_usage_nonretryable_model_failure"
    ):
        issues.append(
            "not an exact-usage nonretryable terminal model failure"
        )
    if isinstance(result, dict):
        issues.extend(
            _c1_attempt_cost_issues(
                result,
                is_state=arm == "c1_state",
                allow_nonretryable_terminal=True,
            )[3]
        )
    if has_current_overhead:
        issues.append("current terminal attempt also has operational overhead")
    return result, list(dict.fromkeys(issues))


# Formal C1 arms C/D/E. GuiAgent A/B labels remain for historical / fixture
# reports but are outside the formal registered set.
_C1_ARM_LABELS = {
    "c1_agent_s": "agent_s_from_start",
    "c1_agent_s_synth": "agent_s_synthesized",
    "c1_state": "state_managed",
    "c1_agent_scalecua": "scalecua_from_start",
    "c1_agent_scalecua_synth": "scalecua_synthesized",
}
_C1_OPTIONAL_ARM_LABELS = {
    "c1_gui": "gui_client_from_start",
    "c1_gui_synth": "gui_client_synthesized",
}
_C1_DIAGNOSTIC_ARM_LABELS = {"c1_state_minimal_agent"}
# ScaleCUA pixel-baseline extension (F/G): registered alongside C/D/E and
# rotated in the same model-seed Latin square as the paper's formal gate.
_C1_ALL_ARM_LABELS = {
    **_C1_ARM_LABELS,
    **_C1_OPTIONAL_ARM_LABELS,
}
# The pre-registered Latin-square rotation covers every registered formal arm.
_C1_SCHEDULE_ARM_LABELS = dict(_C1_ARM_LABELS)
_C1_COMPARISONS = (
    ("A_vs_B", "c1_gui", "c1_gui_synth"),
    ("C_vs_D", "c1_agent_s", "c1_agent_s_synth"),
    ("A_vs_C", "c1_gui", "c1_agent_s"),
    ("B_vs_D", "c1_gui_synth", "c1_agent_s_synth"),
)

_C1_CHECKPOINT_COMPARISONS = (
    ("A_vs_B", "c1_gui", "c1_gui_synth"),
    ("C_vs_D", "c1_agent_s", "c1_agent_s_synth"),
)

_C1_SEED_EVIDENCE_PAIRS = (
    ("A_vs_C", "c1_gui", "c1_agent_s"),
    ("B_vs_D", "c1_gui_synth", "c1_agent_s_synth"),
)

# Arms that self-synthesize their start from one shared cache entry. Their Start
# rates are reported side by side, so the report has to show they really did
# restore the same start and grade it the same way.
_C1_START_PARITY_PAIRS = (
    ("E_vs_D", "c1_state", "c1_agent_s_synth"),
    ("E_vs_B", "c1_state", "c1_gui_synth"),
    ("B_vs_D", "c1_gui_synth", "c1_agent_s_synth"),
)

_C1_NATIVE_DESKTOP_CONTRACTS = {
    "blender": (
        "blender_native_desktop_session_v1",
        "blender_live_bpy_probe_v1",
    ),
    "libreoffice": (
        "libreoffice_native_desktop_session_v1",
        "libreoffice_live_uno_probe_v1",
    ),
}
_C1_NATIVE_DESKTOP_BACKGROUND_BACKENDS = {
    "blender": (
        "blender_bpy_screen_screenshot_v1",
        "blender_window_event_simulate_v1",
    ),
    "libreoffice": (
        "libreoffice_svp_composited_v1",
        "libreoffice_vcl_post_event_v1",
    ),
}
_C1_NATIVE_DESKTOP_ALGORITHM = "not_applicable_native_gui"
_C1_STATE_PAIR_CONTRACTS = {
    "openra": {
        "pair_source": "openra_state_process_pair",
        "snapshot_source": "openra_state_live_process",
        "algorithm": "not_applicable_public_settings_pair",
        "semantic_source": "openra_public_settings_yaml",
        "benchmark": "openra",
        "reported_benchmark": "openra",
        "protocol": None,
        "pair_semantics": "separate_registered_before_after_processes",
    },
    "openragameplay": {
        "pair_source": "openra_gameplay_state_live_pair",
        "snapshot_source": "openra_state_live_process",
        "algorithm": "live_registered_gameplay_transition_v1",
        "semantic_source": "openra_live_gameplay_probe_v1",
        "benchmark": "openra_gameplay",
        "reported_benchmark": "openra",
        "protocol": None,
        "pair_semantics": "same_persistent_registered_gameplay_process",
    },
    "jynew": {
        "pair_source": "unity_state_checkpoint_process_pair",
        "snapshot_source": "unity_state_checkpoint_process",
        "algorithm": "not_applicable_unity_checkpoint_pair",
        "semantic_source": "jynew_checkpoint_utility_report_v1",
        "benchmark": "jynew",
        "reported_benchmark": "jynew",
        "protocol": "warptest-unity-checkpoint-state-v1",
        "pair_semantics": "separate_registered_before_after_processes",
    },
    "pal3": {
        "pair_source": "unity_state_checkpoint_process_pair",
        "snapshot_source": "unity_state_checkpoint_process",
        "algorithm": "not_applicable_unity_checkpoint_pair",
        "semantic_source": "pal3_checkpoint_utility_report_v1",
        "benchmark": "pal3",
        "reported_benchmark": "pal3",
        "protocol": "warptest-unity-checkpoint-state-v1",
        "pair_semantics": "separate_registered_before_after_processes",
    },
    "pal3unity": {
        "pair_source": "unity_state_checkpoint_process_pair",
        "snapshot_source": "unity_state_checkpoint_process",
        "algorithm": "not_applicable_unity_checkpoint_pair",
        "semantic_source": "pal3_checkpoint_utility_report_v1",
        "benchmark": "pal3",
        "reported_benchmark": "pal3",
        "protocol": "warptest-unity-checkpoint-state-v1",
        "pair_semantics": "separate_registered_before_after_processes",
    },
}
# Formal task-level semantic shapes are loaded from evaluation-v1 YAML.  These
# helpers deliberately accept only task ids and return defensive copies from the
# trusted parent-repository catalog; no oracle material is read from a task
# checkout or exposed to the policy process.
def _c1_formal_gui_surface(task_id: str) -> dict[str, Any] | None:
    try:
        surface = formal_surface_contract(task_id, "gui")
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        return None
    return copy.deepcopy(surface) if isinstance(surface, dict) else None


def _c1_expected_semantic_checks(
    task_id: str, stage: str
) -> tuple[str | None, list[dict[str, Any]] | None]:
    surface = _c1_formal_gui_surface(task_id)
    if surface is None:
        return None, None
    phase_name = "start" if stage == "before" else "goal"
    phase = surface.get(phase_name)
    expected = phase.get("expected_checks") if isinstance(phase, dict) else None
    if not isinstance(expected, list):
        return str(surface.get("adapter") or "") or None, None
    if not all(isinstance(item, dict) for item in expected):
        return str(surface.get("adapter") or "") or None, None
    return str(surface.get("adapter") or "") or None, copy.deepcopy(expected)


def _c1_save_load_requirement(task_id: str) -> dict[str, Any] | None:
    surface = _c1_formal_gui_surface(task_id)
    goal = surface.get("goal") if isinstance(surface, dict) else None
    requirements = goal.get("requirements") if isinstance(goal, dict) else None
    matches = [
        item
        for item in (requirements if isinstance(requirements, list) else [])
        if isinstance(item, dict)
        and item.get("name") == "c1.public_save_load_witness"
    ]
    return copy.deepcopy(matches[0]) if len(matches) == 1 else None


_C1_STEP_BUDGETS = dict(C1_STEP_BUDGETS)
# Administrative censoring times, fixed before outcomes are observed: one
# allowance per registered decision step, plus a fixed non-synthesis overhead and
# a synthesis allowance. Because the step budget is uniform, all three horizons
# resolve to the same tau; the per-horizon shape is kept so a report keyed by
# horizon still validates.
_C1_RMST_TAU_MS_PER_DECISION_STEP = 1_200_000
_C1_RMST_TAU_FIXED_OVERHEAD_MS = 600_000
_C1_RMST_TAU_SYNTHESIS_ALLOWANCE_MS = 7_200_000


def _default_c1_administrative_rmst_tau_ms() -> dict[str, dict[str, int]]:
    excluding = (
        C1_STEP_BUDGET * _C1_RMST_TAU_MS_PER_DECISION_STEP
        + _C1_RMST_TAU_FIXED_OVERHEAD_MS
    )
    including = excluding + _C1_RMST_TAU_SYNTHESIS_ALLOWANCE_MS
    return {
        horizon: {
            "e2e_excluding_synthesis": excluding,
            "e2e_including_synthesis": including,
            "termination": including,
        }
        for horizon in C1_HORIZON_CLASSES
    }


_DEFAULT_C1_ADMINISTRATIVE_RMST_TAU_MS = _default_c1_administrative_rmst_tau_ms()
# v4 freezes one pair-level Start verdict and requires per-arm local restore
# attestation. Declared here rather than imported so report validation does not
# depend on the orchestrator module.
C1_START_GATE_PROTOCOL_SCHEMA_VERSION = 4

_FORMAL_C1_GAME_COUNTS = {
    "2048": 8,
    "datingsim": 7,
    "spacerpg": 8,
    "luanti": 8,
    "openra": 5,
    "jupyterlab": 5,
    "libreoffice": 5,
    "blender": 5,
    "jynew": 5,
    "pal3": 5,
}

_GUI_V2_BOOL_FIELDS = (
    "before_visual_ok",
    "before_semantic_ok",
    "after_visual_ok",
    "after_semantic_ok",
    "start_success",
    "goal_success",
    "e2e_success",
    "policy_phase_started",
    "visual_grading_started",
)

_GUI_V2_TIME_FIELDS = (
    "time_to_start_ms",
    "time_start_to_goal_ms",
    "time_e2e_excluding_synthesis_ms",
    "time_e2e_including_synthesis_ms",
    "checkpoint_synthesis_ms",
    "checkpoint_restore_ms",
    "runtime_bootstrap_ms",
    "grader_ms",
    "termination_time_ms",
)


def _clean_model(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.split("@", 1)[0]


def _valid_generated_at(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _is_sha256(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _valid_calibration_gate(
    value: Any,
    *,
    expected_headless: bool | None = None,
    expected_api_provider: Any = None,
    expected_observer_model: str | None = None,
    expected_grounder_model: str | None = None,
    expected_provider_timeout_s: float | None = None,
) -> bool:
    if not isinstance(value, dict) or value.get("passed") is not True:
        return False
    backends = value.get("backends") if isinstance(value.get("backends"), dict) else {}
    isolation_fields = (
        "target_never_frontmost",
        "system_pointer_unchanged",
        "system_clipboard_unchanged",
        "foreground_text_unchanged",
    )
    backend_proofs_valid = bool(
        len(backends) == 10
        and all(
            isinstance(proof, dict)
            and proof.get("passed") is True
            and isinstance(proof.get("capture_backend"), str)
            and isinstance(proof.get("input_backend"), str)
            and isinstance(proof.get("frame"), list)
            and len(proof["frame"]) == 2
            and all(isinstance(size, int) and size > 0 for size in proof["frame"])
            and isinstance(proof.get("foreground_isolation"), dict)
            and all(
                proof["foreground_isolation"].get(field) is True
                for field in isolation_fields
            )
            for proof in backends.values()
        )
    )
    valid = bool(
        value.get("schema_version") == 4
        and value.get("schema") == "c1-calibration-v4"
        and value.get("artifact")
        and _is_sha256(value.get("artifact_sha256"))
        and _valid_generated_at(value.get("generated_at_utc"))
        and value.get("git_commit")
        and value.get("manifest_hash")
        and value.get("harness_hash")
        and _is_sha256(value.get("background_pixel_protocol_sha256"))
        and backend_proofs_valid
    )
    del expected_headless
    if expected_api_provider is not None:
        valid = valid and value.get("api_provider") == expected_api_provider
    if expected_observer_model is not None:
        valid = valid and value.get("observer_model") == expected_observer_model
    if expected_grounder_model is not None:
        valid = valid and value.get("grounder_model") == expected_grounder_model
    if expected_provider_timeout_s is not None:
        valid = valid and value.get("provider_timeout_s") == expected_provider_timeout_s
    return valid


def _valid_preflight_gate(
    value: Any,
    *,
    expected_git_commit: Any,
    expected_manifest_hash: Any,
    expected_harness_hash: Any,
    expected_api_provider: Any,
    expected_profiles: Any,
    expected_profiles_sha256: Any,
    expected_probes: list[dict[str, Any]],
    expected_usage_accounting: Any = None,
    expected_provider_timeout_s: float | None = None,
    expected_temperature: float | None = None,
) -> bool:
    if not isinstance(value, dict) or value.get("complete") is not True:
        return False
    results = value.get("results")
    provider_name = (
        expected_api_provider.get("name")
        if isinstance(expected_api_provider, dict)
        else None
    )
    temperature_contract = {
        "configured": expected_temperature,
        "applies_to": "decision_probes_only",
        "fallback": "omit_only_after_explicit_http_400_temperature_rejection",
    }
    if not bool(
        _is_sha256(value.get("artifact_sha256"))
        and value.get("schema_version")
        == (4 if expected_usage_accounting is not None else 2)
        and _valid_generated_at(value.get("generated_at_utc"))
        and value.get("git_commit") == expected_git_commit
        and value.get("manifest_hash") == expected_manifest_hash
        and value.get("harness_hash") == expected_harness_hash
        and value.get("api_provider") == expected_api_provider
        and (
            expected_provider_timeout_s is None
            or value.get("provider_timeout_s") == expected_provider_timeout_s
        )
        and value.get("model_request_profiles") == expected_profiles
        and _is_sha256(value.get("model_request_profiles_sha256"))
        and value.get("model_request_profiles_sha256") == expected_profiles_sha256
        and (
            expected_usage_accounting is None
            or value.get("usage_accounting") == expected_usage_accounting
        )
        and _integer(value.get("initial_max_tokens")) == 8192
        and _integer(value.get("retry_max_tokens")) == 16384
        and value.get("retry_rule")
        == "one_retry_only_if_empty_content_and_explicit_output_limit"
        and _integer(value.get("probe_count")) == len(expected_probes)
        and len(expected_probes) >= 4
        and isinstance(results, list)
        and len(results) == len(expected_probes)
        and (
            expected_temperature is None
            or value.get("temperature_contract") == temperature_contract
        )
    ):
        return False
    profiles = expected_profiles if isinstance(expected_profiles, dict) else {}

    def valid_usage(usage: Any) -> tuple[int, ...] | None:
        if not isinstance(usage, dict):
            return None
        if "reasoning_tokens_observed" not in usage:
            parsed_legacy = tuple(
                _integer(usage.get(field))
                for field in (
                    "prompt_tokens",
                    "completion_tokens",
                    "reasoning_tokens",
                    "cached_prompt_tokens",
                    "total_tokens",
                )
            )
            if any(number is None or number < 0 for number in parsed_legacy):
                return None
            prompt, completion, reasoning, cached, total = parsed_legacy
            if total != prompt + completion or reasoning > completion or cached > prompt:
                return None
            return tuple(int(number) for number in parsed_legacy if number is not None)
        parsed = tuple(
            _integer(usage.get(field))
            for field in (
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens_observed",
                "cached_prompt_tokens",
                "total_tokens",
            )
        )
        if any(number is None or number < 0 for number in parsed):
            return None
        prompt, completion, reasoning_observed, cached, total = parsed
        if not (
            total == prompt + completion
            and reasoning_observed <= completion
            and cached <= prompt
        ):
            return None
        missing = _integer(usage.get("reasoning_detail_missing_calls"))
        unknown = _integer(
            usage.get("reasoning_detail_unknown_completion_tokens")
        )
        expected = _integer(
            usage.get("reasoning_detail_expected_completion_tokens")
        )
        if any(number is None or number < 0 for number in (missing, unknown, expected)):
            return None
        fraction = unknown / expected if expected > 0 else 0.0
        if (
            usage.get("reasoning_tokens_lower_bound") != reasoning_observed
            or usage.get("reasoning_tokens_upper_bound")
            != reasoning_observed + unknown
            or usage.get("reasoning_detail_complete") is not (missing == 0)
            or usage.get("reasoning_tokens")
            != (reasoning_observed if missing == 0 else None)
            or not math.isclose(
                _number(usage.get("reasoning_detail_unknown_fraction")) or 0.0,
                fraction,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            return None
        return tuple(int(number) for number in parsed if number is not None)

    def valid_temperature_attempts(
        row: dict[str, Any],
        records: list[Any],
        probe_temperature: float | None,
        reasoning_disabled: bool,
    ) -> bool:
        attempts = _integer(row.get("attempts"))
        if attempts not in {1, 2, 3} or len(records) != attempts:
            return False
        summed = [0, 0, 0, 0, 0]
        successful: list[dict[str, Any]] = []
        fallback_indexes: list[int] = []
        for index, record in enumerate(records):
            if (
                not isinstance(record, dict)
                or _integer(record.get("ordinal")) != index + 1
                or _integer(record.get("max_tokens")) not in {8192, 16384}
                or record.get("temperature_configured") != probe_temperature
            ):
                return False
            if record.get("request_succeeded") is True:
                record_usage = valid_usage(record.get("usage"))
                if (
                    not isinstance(record.get("content_nonempty"), bool)
                    or not isinstance(record.get("reasoning_observed"), bool)
                    or not isinstance(
                        record.get("reasoning_content_observed"), bool
                    )
                    or not isinstance(record.get("reasoning_token_anomaly"), bool)
                    or not isinstance(record.get("request_contract_passed"), bool)
                    or not isinstance(record.get("reasoning_exhausted"), bool)
                    or record_usage is None
                ):
                    return False
                expected_contract = bool(
                    record["content_nonempty"]
                    and not (
                        reasoning_disabled
                        and record["reasoning_content_observed"]
                    )
                )
                expected_anomaly = bool(
                    reasoning_disabled and record_usage[2] > 0
                )
                if (
                    record["request_contract_passed"] != expected_contract
                    or record["reasoning_token_anomaly"] != expected_anomaly
                ):
                    return False
                omitted = (
                    record.get("temperature_omitted_after_rejection") is True
                )
                requested = (
                    None
                    if probe_temperature is None or omitted
                    else probe_temperature
                )
                if record.get("temperature_requested") != requested:
                    return False
                content_nonempty = bool(record["content_nonempty"])
                finish_reason = str(record.get("finish_reason") or "").lower()
                expected_exhausted = (
                    not content_nonempty
                    and finish_reason in {"length", "max_tokens"}
                )
                if record["reasoning_exhausted"] != expected_exhausted:
                    return False
                successful.append(record)
                for position, number in enumerate(record_usage):
                    summed[position] += number
                continue
            if (
                probe_temperature is None
                or record.get("request_succeeded") is not False
                or _integer(record.get("status_code")) != 400
                or not isinstance(record.get("error_type"), str)
                or record.get("temperature_requested") != probe_temperature
                or record.get("temperature_fallback_triggered") is not True
            ):
                return False
            fallback_indexes.append(index)
            failed_usage = record.get("usage")
            if failed_usage is not None:
                parsed = valid_usage(failed_usage)
                if parsed is None:
                    return False
                for position, number in enumerate(parsed):
                    summed[position] += number
        if len(fallback_indexes) > 1:
            return False
        if fallback_indexes:
            index = fallback_indexes[0]
            if (
                index + 1 >= len(records)
                or not isinstance(records[index + 1], dict)
                or records[index + 1].get("request_succeeded") is not True
                or records[index + 1].get("max_tokens")
                != records[index].get("max_tokens")
                or records[index + 1].get("temperature_requested") is not None
                or records[index + 1].get(
                    "temperature_omitted_after_rejection"
                )
                is not True
                or row.get("temperature_fallback_triggered") is not True
                or row.get("temperature_omitted_after_rejection") is not True
            ):
                return False
        elif (
            row.get("temperature_fallback_triggered") is not False
            or row.get("temperature_omitted_after_rejection") is not False
            or any(
                not isinstance(record, dict)
                or record.get("request_succeeded") is not True
                for record in records
            )
        ):
            return False
        if not successful or len(successful) > 2:
            return False
        if _integer(successful[0].get("max_tokens")) != 8192:
            return False
        if len(successful) == 1:
            if successful[0].get("content_nonempty") is not True:
                return False
        else:
            first, second = successful
            if (
                _integer(second.get("max_tokens")) != 16384
                or first.get("content_nonempty") is not False
                or first.get("reasoning_exhausted") is not True
                or second.get("content_nonempty") is not True
            ):
                return False
        reasoning_content_observed = any(
            bool(record.get("reasoning_content_observed"))
            for record in successful
        )
        row_usage = valid_usage(row.get("usage"))
        return bool(
            row.get("temperature_configured") == probe_temperature
            and tuple(summed) == row_usage
            and row.get("finish_reason") == successful[-1].get("finish_reason")
            and row.get("request_contract_passed") is True
            and row.get("reasoning_content_observed")
            is reasoning_content_observed
            and (not reasoning_disabled or not reasoning_content_observed)
            and row.get("reasoning_token_anomaly")
            is bool(reasoning_disabled and row_usage is not None and row_usage[2] > 0)
        )

    for wanted, row in zip(expected_probes, results, strict=True):
        if not isinstance(row, dict) or any(
            row.get(field) != wanted.get(field)
            for field in ("name", "model", "modality")
        ):
            return False
        model = str(wanted.get("model") or "")
        profile = profiles.get(model, PROVIDER_DEFAULT)
        expected_profile = ModelRequestProfile(model, profile).provenance()
        reasoning_disabled = profile == THINKING_DISABLED
        usage = valid_usage(row.get("usage"))
        attempts = _integer(row.get("attempts"))
        if not (
            row.get("provider") == provider_name
            and row.get("success") is True
            and row.get("request_profile") == expected_profile
            and attempts
            in ({1, 2, 3} if expected_temperature is not None else {1, 2})
            and usage is not None
        ):
            return False
        records = row.get("attempt_records")
        if not isinstance(records, list) or len(records) != attempts:
            return False
        if expected_temperature is not None:
            probe_temperature = (
                expected_temperature
                if str(wanted.get("name") or "").startswith("decision:")
                else None
            )
            if not valid_temperature_attempts(
                row, records, probe_temperature, reasoning_disabled
            ):
                return False
            continue
        summed = [0, 0, 0, 0, 0]
        for index, record in enumerate(records):
            expected_budget = 8192 if index == 0 else 16384
            record_usage = valid_usage(record.get("usage")) if isinstance(record, dict) else None
            if not (
                isinstance(record, dict)
                and _integer(record.get("ordinal")) == index + 1
                and _integer(record.get("max_tokens")) == expected_budget
                and record.get("request_succeeded") is True
                and isinstance(record.get("content_nonempty"), bool)
                and isinstance(record.get("reasoning_observed"), bool)
                and isinstance(record.get("reasoning_content_observed"), bool)
                and isinstance(record.get("reasoning_token_anomaly"), bool)
                and isinstance(record.get("request_contract_passed"), bool)
                and isinstance(record.get("reasoning_exhausted"), bool)
                and record_usage is not None
            ):
                return False
            expected_contract = bool(
                record["content_nonempty"]
                and not (
                    reasoning_disabled
                    and record["reasoning_content_observed"]
                )
            )
            if (
                record["request_contract_passed"] != expected_contract
                or record["reasoning_token_anomaly"]
                is not bool(reasoning_disabled and record_usage[2] > 0)
            ):
                return False
            content_nonempty = bool(record["content_nonempty"])
            finish_reason = str(record.get("finish_reason") or "").lower()
            expected_exhausted = (
                not content_nonempty
                and finish_reason in {"length", "max_tokens"}
            )
            if record["reasoning_exhausted"] != expected_exhausted:
                return False
            if index == 0 and attempts == 2 and not expected_exhausted:
                return False
            if index == attempts - 1 and not content_nonempty:
                return False
            for position, number in enumerate(record_usage):
                summed[position] += number
        reasoning_content_observed = any(
            bool(record.get("reasoning_content_observed")) for record in records
        )
        if (
            tuple(summed) != usage
            or row.get("finish_reason") != records[-1].get("finish_reason")
            or row.get("request_contract_passed") is not True
            or row.get("reasoning_content_observed")
            is not reasoning_content_observed
            or (reasoning_disabled and reasoning_content_observed)
            or row.get("reasoning_token_anomaly")
            is not bool(reasoning_disabled and usage[2] > 0)
        ):
            return False
    return True


def _report_model(report: dict[str, Any]) -> str:
    context = report.get("run_context") if isinstance(report.get("run_context"), dict) else {}
    # The orchestrator model retains the explicit provider tag; producer payloads
    # may echo only the provider-clean model id after ``resolve_model``.
    return str(context.get("model") or report.get("model") or report.get("synthesis_model") or "model")


def _report_seed(report: dict[str, Any], result: dict[str, Any]) -> int:
    context = report.get("run_context") if isinstance(report.get("run_context"), dict) else {}
    raw = result.get("seed", report.get("seed", context.get("seed", 0)))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value) if value is not None else None
        return parsed if parsed is not None and math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        parsed = int(value)
        return parsed if float(value) == parsed else None
    except (TypeError, ValueError):
        return None


def _same_json_value(left: Any, right: Any) -> bool:
    """Compare evidence scalars/containers without bool/int coercion."""

    return json.dumps(
        left,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ) == json.dumps(
        right,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _state_time(result: dict[str, Any], name: str) -> float | None:
    explicit = _number(result.get(name))
    if explicit is not None:
        return explicit
    before = result.get("restore_timing") if isinstance(result.get("restore_timing"), dict) else {}
    after = result.get("after_timing") if isinstance(result.get("after_timing"), dict) else {}
    start = _number(before.get("warm_restore_ms") or before.get("restore_ms"))
    goal = _number(after.get("warm_restore_ms") or after.get("restore_ms"))
    if name == "time_to_start_ms":
        return start
    if name == "time_start_to_goal_ms":
        return goal
    if name == "time_e2e_excluding_synthesis_ms" and start is not None and goal is not None:
        return start + goal
    return None


def _browser_seed_snapshot_issues(
    value: Any,
    *,
    label: str,
    seed: int,
) -> list[str]:
    if not isinstance(value, dict):
        return [f"browser seed evidence missing {label} runtime snapshot"]
    issues: list[str] = []
    if value.get("source") != "browser_page_runtime" or value.get("observed") is not True:
        issues.append(f"browser {label} seed snapshot was not observed from the live page")
    if value.get("algorithm") != "mulberry32":
        issues.append(f"browser {label} seed snapshot has invalid algorithm")
    if _integer(value.get("seed")) != seed:
        issues.append(f"browser {label} observed seed does not match trial seed {seed}")
    calls = _integer(value.get("calls"))
    if calls is None or calls < 0:
        issues.append(f"browser {label} seed snapshot missing non-negative call counter")
    viewport = value.get("viewport")
    if viewport != {"width": 1280, "height": 800}:
        issues.append(f"browser {label} seed snapshot viewport is not 1280x800")
    if _number(value.get("device_scale_factor")) != 1.0:
        issues.append(f"browser {label} seed snapshot DPR is not 1")
    return issues


def _c1_web_stable_semantic(value: Any) -> Any:
    """Remove volatile presentation fields while retaining live app semantics."""

    if isinstance(value, dict):
        return {
            str(key): _c1_web_stable_semantic(item)
            for key, item in value.items()
            if str(key) != "ui"
        }
    if isinstance(value, list):
        return [_c1_web_stable_semantic(item) for item in value]
    return value


def _c1_web_semantic_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _c1_web_seed_snapshot_issues(
    value: Any,
    *,
    label: str,
    benchmark: str,
    seed: int,
    task_id: str,
    require_semantic_fingerprint: bool,
) -> list[str]:
    """Validate one headless JupyterLab background-pixel observation."""

    if not isinstance(value, dict):
        return [f"C1 web seed evidence missing {label} runtime snapshot"]
    issues: list[str] = []
    normalized = str(benchmark).strip().lower().replace("-", "").replace("_", "")
    expected_source = {
        "jupyterlab": "jupyterlab_contents_kernel_live_v1",
    }.get(normalized)
    if _integer(value.get("schema_version")) != 1:
        issues.append(f"C1 web {label} seed snapshot schema_version must be 1")
    if (
        value.get("source") != "c1_web_background_pixel_snapshot_v1"
        or value.get("observed") is not True
    ):
        issues.append(
            f"C1 web {label} seed snapshot was not observed from the live browser"
        )
    if value.get("algorithm") != "not_applicable_web_gui":
        issues.append(f"C1 web {label} seed snapshot has invalid algorithm")
    if _integer(value.get("seed")) != seed:
        issues.append(f"C1 web {label} observed seed does not match trial seed {seed}")
    if str(value.get("task_id") or "") != task_id:
        issues.append(f"C1 web {label} seed snapshot is bound to the wrong task")
    observed_benchmark = (
        str(value.get("benchmark") or "")
        .strip()
        .lower()
        .replace("-", "")
        .replace("_", "")
    )
    if expected_source is None or observed_benchmark != normalized:
        issues.append(f"C1 web {label} seed snapshot is bound to the wrong benchmark")
    if value.get("headless") is not True:
        issues.append(f"C1 web {label} seed snapshot is not headless")
    if value.get("foreground_required") is not False:
        issues.append(f"C1 web {label} seed snapshot requires the foreground")
    if value.get("execution_backend") != "background_pixel_v1":
        issues.append(f"C1 web {label} seed snapshot has the wrong execution backend")
    if value.get("coordinate_space") != "frame_pixels_top_left_v1":
        issues.append(f"C1 web {label} seed snapshot has the wrong coordinate space")
    if value.get("capture_backend") != "playwright_headless_page_v1":
        issues.append(f"C1 web {label} seed snapshot has the wrong capture backend")
    if value.get("process_alive_at_observation") is not True:
        issues.append(f"C1 web {label} runtime process was not alive at observation")
    process_id = _integer(value.get("process_id"))
    if process_id is None or process_id <= 0:
        issues.append(f"C1 web {label} seed snapshot lacks a positive process_id")
    for field in ("app_url", "page_url"):
        if not isinstance(value.get(field), str) or not str(value[field]).startswith(
            ("http://", "https://")
        ):
            issues.append(f"C1 web {label} seed snapshot has invalid {field}")
    viewport = value.get("viewport")
    if not isinstance(viewport, dict) or any(
        (_integer(viewport.get(field)) or 0) <= 0 for field in ("width", "height")
    ):
        issues.append(f"C1 web {label} seed snapshot has invalid viewport")
    screen = value.get("screen")
    if not isinstance(screen, dict) or any(
        (_integer(screen.get(field)) or 0) <= 0 for field in ("width", "height")
    ):
        issues.append(f"C1 web {label} seed snapshot has invalid desktop screen")
    if (_number(value.get("device_scale_factor")) or 0) <= 0:
        issues.append(f"C1 web {label} seed snapshot has invalid DPR")
    calibration = value.get("calibration")
    if not isinstance(calibration, dict) or calibration.get("passed") is not True:
        issues.append(f"C1 web {label} seed snapshot lacks passed calibration")
    elif calibration.get("backend") != "playwright_headless_page_v1":
        issues.append(f"C1 web {label} calibration has the wrong backend")
    else:
        screenshot = calibration.get("screenshot")
        click_space = calibration.get("click_space")
        if screenshot != screen or click_space != screen:
            issues.append(
                f"C1 web {label} screenshot/click calibration does not match the desktop"
            )
        if calibration.get("page_visible") is not True:
            issues.append(f"C1 web {label} calibration did not observe a visible page")

    semantic = value.get("semantic_fingerprint")
    if require_semantic_fingerprint and (
        not isinstance(semantic, dict) or not semantic
    ):
        issues.append(
            f"C1 web {label} seed snapshot lacks a live semantic fingerprint"
        )
    elif semantic is not None and not isinstance(semantic, dict):
        issues.append(f"C1 web {label} semantic fingerprint is malformed")
    if isinstance(semantic, dict) and semantic:
        observation = semantic.get("observation")
        fingerprint = semantic.get("fingerprint")
        stable_fingerprint = semantic.get("stable_fingerprint")
        status = semantic.get("status")
        ok = semantic.get("ok")
        if semantic.get("source") != expected_source:
            issues.append(f"C1 web {label} semantic fingerprint has the wrong source")
        if not isinstance(ok, bool):
            issues.append(f"C1 web {label} semantic fingerprint lacks boolean ok")
        if status not in {
            "success",
            "validate_failed",
            "assert_failed",
            "failure",
        }:
            issues.append(f"C1 web {label} semantic fingerprint has invalid status")
        elif isinstance(ok, bool) and ok != (status == "success"):
            issues.append(
                f"C1 web {label} semantic fingerprint status disagrees with ok"
            )
        if not isinstance(observation, dict) or not observation:
            issues.append(f"C1 web {label} semantic observation is missing")
        else:
            if fingerprint != _c1_web_semantic_digest(observation):
                issues.append(
                    f"C1 web {label} semantic fingerprint does not match observation"
                )
            expected_stable = _c1_web_semantic_digest(
                _c1_web_stable_semantic(observation)
            )
            if stable_fingerprint != expected_stable:
                issues.append(
                    f"C1 web {label} stable semantic fingerprint does not match observation"
                )
    return issues


def _native_process_seed_snapshot_issues(
    value: Any,
    *,
    label: str,
    benchmark: str,
    source: str,
    algorithm: str,
    seed: int,
    task_id: str,
    expected_semantic_stage: str | None,
    require_semantic_fingerprint: bool,
) -> list[str]:
    """Validate one observation from a headed native C1 process."""

    if not isinstance(value, dict):
        return [f"native seed evidence missing {label} runtime snapshot"]
    issues: list[str] = []
    if type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        issues.append(f"native {label} seed snapshot schema_version must be 1")
    if value.get("source") != source or value.get("observed") is not True:
        issues.append(
            f"native {label} seed snapshot was not observed from {source}"
        )
    if value.get("algorithm") != algorithm:
        issues.append(f"native {label} seed snapshot has invalid algorithm")
    if type(value.get("seed")) is not int or value.get("seed") != seed:
        issues.append(f"native {label} observed seed does not match trial seed {seed}")
    process_id = (
        value.get("process_id") if type(value.get("process_id")) is int else None
    )
    if process_id is None or process_id <= 0:
        issues.append(f"native {label} seed snapshot lacks a positive process_id")

    semantic_fingerprint = value.get("semantic_fingerprint")
    desktop_seed_sources = {
        contract[0] for contract in _C1_NATIVE_DESKTOP_CONTRACTS.values()
    }
    if source in desktop_seed_sources:
        if require_semantic_fingerprint and (
            not isinstance(semantic_fingerprint, str)
            or len(semantic_fingerprint) != 64
        ):
            issues.append(
                f"native {label} seed snapshot lacks a live semantic fingerprint"
            )
        elif semantic_fingerprint is not None and not isinstance(
            semantic_fingerprint, str
        ):
            issues.append(
                f"native {label} seed snapshot has malformed semantic fingerprint"
            )
    elif require_semantic_fingerprint and (
        not isinstance(semantic_fingerprint, dict) or not semantic_fingerprint
    ):
        issues.append(
            f"native {label} seed snapshot lacks a live semantic fingerprint"
        )
    elif semantic_fingerprint is not None and not isinstance(
        semantic_fingerprint, dict
    ):
        issues.append(
            f"native {label} seed snapshot has malformed semantic fingerprint"
        )

    if source in {
        "openra_live_settings_process",
        "openra_live_gameplay_process",
    }:
        expected_fields = {
            "schema_version",
            "source",
            "observed",
            "algorithm",
            "seed",
            "process_id",
            "process_alive_at_observation",
            "benchmark",
            "task_id",
            "semantic_fingerprint",
        }
        if set(value) != expected_fields:
            issues.append(f"OpenRA {label} seed snapshot has invalid fields")
        if str(benchmark).strip().lower() != "openra":
            issues.append("OpenRA native seed evidence is bound to the wrong benchmark")
        if str(value.get("benchmark") or "").strip().lower() != "openra":
            issues.append(f"OpenRA {label} seed snapshot benchmark does not match trial")
        if value.get("task_id") != task_id:
            issues.append(f"OpenRA {label} seed snapshot is bound to the wrong task")
        if value.get("process_alive_at_observation") is not True:
            issues.append(
                f"OpenRA {label} seed evidence was not captured from a live process"
            )
        if source == "openra_live_gameplay_process":
            semantic = value.get("semantic_fingerprint")
            if (
                not isinstance(semantic, dict)
                or semantic.get("source") != "openra_live_gameplay_probe_v1"
                or not isinstance(semantic.get("checks"), list)
            ):
                issues.append(
                    f"OpenRA {label} live gameplay semantic evidence is malformed"
                )
        return issues

    normalized_benchmark = (
        str(benchmark).strip().lower().replace("-", "").replace("_", "")
    )
    desktop_contract = _C1_NATIVE_DESKTOP_CONTRACTS.get(normalized_benchmark)
    if desktop_contract is not None:
        expected_seed_source, expected_semantic_source = desktop_contract
        if source != expected_seed_source:
            issues.append("native desktop seed evidence source/benchmark mismatch")
        if str(value.get("benchmark") or "").strip().lower() != normalized_benchmark:
            issues.append(
                f"native desktop {label} seed snapshot benchmark does not match trial"
            )
        if str(value.get("task_id") or "") != task_id:
            issues.append(
                f"native desktop {label} seed snapshot is bound to the wrong task"
            )
        if value.get("process_alive_at_observation") is not True:
            issues.append(
                f"native desktop {label} seed evidence was not captured from a live process"
            )
        stage = value.get("semantic_stage")
        if not isinstance(stage, str) or not stage.strip():
            issues.append(
                f"native desktop {label} seed snapshot lacks a semantic stage"
            )
        elif expected_semantic_stage is not None and stage != expected_semantic_stage:
            issues.append(
                f"native desktop {label} semantic evidence was captured at the wrong trial stage"
            )
        if value.get("semantic_source") != expected_semantic_source:
            issues.append(
                f"native desktop {label} semantic snapshot has the wrong live source"
            )
        if value.get("semantic_observed") is not True:
            issues.append(
                f"native desktop {label} semantic snapshot was not observed"
            )
        expected_capture, expected_input = _C1_NATIVE_DESKTOP_BACKGROUND_BACKENDS[
            normalized_benchmark
        ]
        if value.get("execution_backend") != "background_pixel_v1":
            issues.append(
                f"native desktop {label} snapshot has the wrong execution backend"
            )
        if value.get("foreground_required") is not False:
            issues.append(
                f"native desktop {label} snapshot requires the foreground"
            )
        if value.get("coordinate_space") != "frame_pixels_top_left_v1":
            issues.append(
                f"native desktop {label} snapshot has the wrong coordinate space"
            )
        if value.get("capture_backend") != expected_capture:
            issues.append(
                f"native desktop {label} snapshot has the wrong capture backend"
            )
        if value.get("input_backend") != expected_input:
            issues.append(
                f"native desktop {label} snapshot has the wrong input backend"
            )
        if value.get("randomness") != "not_applicable" or value.get(
            "seed_role"
        ) != "trial_pairing_only":
            issues.append(
                f"native desktop {label} snapshot has invalid randomness provenance"
            )
        repo_commit = value.get("repo_commit")
        if not (
            isinstance(repo_commit, str)
            and len(repo_commit) == 40
            and all(character in "0123456789abcdef" for character in repo_commit)
        ):
            issues.append(
                f"native desktop {label} snapshot lacks a pinned repo commit"
            )
        executable_path = value.get("executable_path")
        if not isinstance(executable_path, str) or not Path(executable_path).is_absolute():
            issues.append(
                f"native desktop {label} snapshot lacks an absolute executable path"
            )
        if (_integer(value.get("executable_size")) or 0) <= 0:
            issues.append(
                f"native desktop {label} snapshot lacks executable size provenance"
            )
        if (_integer(value.get("executable_mtime_ns")) or 0) <= 0:
            issues.append(
                f"native desktop {label} snapshot lacks executable mtime provenance"
            )

        observation = value.get("semantic_observation")
        fingerprint = value.get("semantic_fingerprint")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            issues.append(
                f"native desktop {label} semantic fingerprint is missing or malformed"
            )
        if not isinstance(observation, dict) or not observation:
            issues.append(
                f"native desktop {label} semantic observation is missing"
            )
        else:
            checks = observation.get("checks")
            checks_well_formed = bool(
                isinstance(checks, list)
                and checks
                and all(
                    isinstance(check, dict)
                    and isinstance(check.get("name"), str)
                    and bool(check.get("name"))
                    and type(check.get("ok")) is bool
                    for check in checks
                )
            )
            errors = observation.get("errors", [])
            errors_well_formed = bool(
                isinstance(errors, list)
                and all(isinstance(error, str) for error in errors)
            )
            status = observation.get("status")
            checks_ok = bool(
                checks_well_formed and all(check["ok"] for check in checks)
            )
            if (
                observation.get("source") != expected_semantic_source
                or observation.get("live") is not True
                or status not in {"success", "failure"}
                or not checks_well_formed
                or not errors_well_formed
                or type(observation.get("passed")) is not bool
                or observation.get("passed") is not (
                    status == "success" and checks_ok
                )
                or (status == "success") is not checks_ok
            ):
                issues.append(
                    f"native desktop {label} semantic observation is malformed or inconsistent"
                )
            canonical = json.dumps(
                observation,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                default=str,
            ).encode("utf-8")
            if fingerprint != hashlib.sha256(canonical).hexdigest():
                issues.append(
                    f"native desktop {label} semantic fingerprint does not match observation"
                )
        for alias, expected in (
            ("initial_semantic_stage", stage),
            ("initial_semantic_source", expected_semantic_source),
            ("initial_semantic_fingerprint", fingerprint),
            ("initial_semantic_observation", observation),
        ):
            if value.get(alias) != expected:
                issues.append(
                    f"native desktop {label} {alias} alias is inconsistent"
                )
        return issues

    expected_benchmark = "pal3" if normalized_benchmark == "pal3unity" else normalized_benchmark
    expected_fields = {
        "schema_version",
        "source",
        "observed",
        "algorithm",
        "seed",
        "process_id",
        "process_alive_at_observation",
        "benchmark",
        "task_id",
        "protocol_version",
        "sequence",
        "semantic_fingerprint",
    }
    if set(value) != expected_fields:
        issues.append(f"Unity {label} seed snapshot has invalid fields")
    if expected_benchmark not in {"jynew", "pal3"}:
        issues.append("Unity native seed evidence is bound to the wrong benchmark")
    if str(value.get("benchmark") or "").strip().lower() != expected_benchmark:
        issues.append(f"Unity {label} seed snapshot benchmark does not match trial")
    if value.get("task_id") != task_id:
        issues.append(f"Unity {label} seed snapshot is bound to the wrong task")
    if value.get("process_alive_at_observation") is not True:
        issues.append(
            f"Unity {label} seed evidence was not captured from a live process"
        )
    if value.get("protocol_version") != "warptest-c1-unity-v3":
        issues.append(f"Unity {label} seed snapshot has invalid protocol version")
    sequence = value.get("sequence") if type(value.get("sequence")) is int else None
    if sequence is None or sequence < 0:
        issues.append(f"Unity {label} seed snapshot lacks a non-negative sequence")
    if isinstance(semantic_fingerprint, dict) and semantic_fingerprint:
        expected_semantic_source = f"{expected_benchmark}_live_semantic_probe_v1"
        if semantic_fingerprint.get("source") != expected_semantic_source:
            issues.append(
                f"Unity {label} semantic fingerprint has the wrong live source"
            )
        checks = semantic_fingerprint.get("checks")
        if not isinstance(checks, list) or not checks:
            issues.append(
                f"Unity {label} semantic fingerprint lacks non-empty checks"
            )
        elif any(
            not isinstance(check, dict)
            or check.get("status") not in {"success", "failure"}
            for check in checks
        ):
            issues.append(
                f"Unity {label} semantic fingerprint has malformed checks"
            )
        if semantic_fingerprint.get("status") not in {"success", "failure"}:
            issues.append(
                f"Unity {label} semantic fingerprint has invalid status"
            )
        if not isinstance(semantic_fingerprint.get("ok"), bool):
            issues.append(
                f"Unity {label} semantic fingerprint lacks boolean ok"
            )
    return issues


def _runtime_seed_evidence_issues(
    value: dict[str, Any],
    *,
    benchmark: str,
    seed: int,
    task_id: str,
    expected_semantic_stage: str,
    require_final: bool,
) -> list[str]:
    """Validate readback evidence without accepting requested/default seed values."""

    issues: list[str] = []
    if _integer(value.get("schema_version")) != 1:
        issues.append("runtime seed evidence schema_version must be 1")
    if value.get("observed") is not True:
        issues.append("runtime seed evidence is not observed")
    if str(benchmark).lower() in {"luanti", "minetest"}:
        if value.get("source") != "luanti_warm_session":
            issues.append("Luanti seed evidence source is not the live warm session")
        if value.get("algorithm") != "luanti_fixed_map_seed":
            issues.append("Luanti seed evidence has invalid algorithm")
        if _integer(value.get("seed")) != seed or _integer(value.get("map_seed")) != seed:
            issues.append(f"Luanti observed map seed does not match trial seed {seed}")
        if value.get("process_alive_at_observation") is not True:
            issues.append("Luanti seed evidence was not captured from a live process")
        if value.get("render") is not True:
            issues.append("Luanti GUI seed evidence was not captured from rendered mode")
        if value.get("frame") != {"width": 1280, "height": 720}:
            issues.append("Luanti seed evidence frame is not 1280x720")
        if value.get("map_backend") != "sqlite3":
            issues.append("Luanti seed evidence map backend is not sqlite3")
        if value.get("gameid") != "devtest":
            issues.append("Luanti seed evidence gameid is not devtest")
        if value.get("creative_mode") is not True:
            issues.append("Luanti seed evidence creative_mode is not true")
        for field in ("git_commit", "world_path", "config_path", "config_fingerprint"):
            if not isinstance(value.get(field), str) or not str(value[field]).strip():
                issues.append(f"Luanti seed evidence missing {field}")
        if value.get("semantic_observed") is not True:
            issues.append("Luanti initial semantic state was not observed")
        if value.get("initial_semantic_source") != "luanti_live_semantic_probe":
            issues.append("Luanti initial semantic evidence source is not the live worldmod")
        if value.get("initial_semantic_stage") != expected_semantic_stage:
            issues.append(
                "Luanti initial semantic evidence was captured at the wrong trial stage"
            )
        fingerprint = value.get("initial_semantic_fingerprint")
        snapshot = value.get("initial_semantic_observation")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            issues.append("Luanti initial semantic fingerprint is missing or malformed")
        if not isinstance(snapshot, dict):
            issues.append("Luanti initial semantic observation is missing")
        else:
            if snapshot.get("source") != "luanti_live_semantic_probe":
                issues.append("Luanti semantic observation did not come from the live worldmod")
            if snapshot.get("task_id") != task_id:
                issues.append("Luanti semantic observation is not bound to the trial task")
            node_rows = list(snapshot.get("nodes") or [])
            for range_row in snapshot.get("ranges") or []:
                if isinstance(range_row, dict) and isinstance(range_row.get("nodes"), list):
                    node_rows.extend(range_row["nodes"])
            if not node_rows or not all(
                isinstance(node, dict)
                and isinstance(node.get("name"), str)
                and isinstance(node.get("pos"), dict)
                for node in node_rows
            ):
                issues.append("Luanti semantic observation has no live task-relevant nodes")
            canonical = json.dumps(
                snapshot,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
            if fingerprint != hashlib.sha256(canonical).hexdigest():
                issues.append("Luanti initial semantic fingerprint does not match observation")
        return issues

    normalized_benchmark = (
        str(benchmark).strip().lower().replace("-", "").replace("_", "")
    )
    if normalized_benchmark == "jupyterlab":
        if value.get("source") != "c1_web_background_pixel":
            issues.append(
                "C1 web seed evidence source is not the background-pixel browser"
            )
        if value.get("algorithm") != "not_applicable_web_gui":
            issues.append("C1 web seed evidence has invalid algorithm")
        if _integer(value.get("seed")) != seed:
            issues.append(
                f"C1 web observed seed does not match trial seed {seed}"
            )
        if str(value.get("task_id") or "") != task_id:
            issues.append("C1 web seed evidence is bound to the wrong task")
        initial = value.get("initial")
        issues.extend(
            _c1_web_seed_snapshot_issues(
                initial,
                label="initial",
                benchmark=benchmark,
                seed=seed,
                task_id=task_id,
                require_semantic_fingerprint=require_final,
            )
        )
        final = value.get("final")
        if require_final:
            issues.extend(
                _c1_web_seed_snapshot_issues(
                    final,
                    label="final",
                    benchmark=benchmark,
                    seed=seed,
                    task_id=task_id,
                    require_semantic_fingerprint=True,
                )
            )
        elif final is not None:
            issues.extend(
                _c1_web_seed_snapshot_issues(
                    final,
                    label="final",
                    benchmark=benchmark,
                    seed=seed,
                    task_id=task_id,
                    require_semantic_fingerprint=False,
                )
            )
        if isinstance(initial, dict) and isinstance(final, dict):
            if _integer(initial.get("process_id")) != _integer(
                final.get("process_id")
            ):
                issues.append("C1 web final process_id differs from initial process_id")
            if initial.get("screen") != final.get("screen"):
                issues.append("C1 web desktop screen changed during the trial")
        return issues

    native_contract: tuple[str, str] | None = None
    if normalized_benchmark == "openra":
        native_contract = (
            "openra_live_settings_process",
            "not_applicable_public_settings",
        )
    elif normalized_benchmark in {"jynew", "pal3", "pal3unity"}:
        native_contract = ("unity_live_process", "not_applicable_unity_gui")
    elif normalized_benchmark in _C1_NATIVE_DESKTOP_CONTRACTS:
        native_contract = (
            _C1_NATIVE_DESKTOP_CONTRACTS[normalized_benchmark][0],
            _C1_NATIVE_DESKTOP_ALGORITHM,
        )
    elif value.get("source") in {
        "openra_live_settings_process",
        "unity_live_process",
        *(
            contract[0]
            for contract in _C1_NATIVE_DESKTOP_CONTRACTS.values()
        ),
    }:
        source = str(value.get("source"))
        native_contract = (
            source,
            (
                "not_applicable_public_settings"
                if source == "openra_live_settings_process"
                else "not_applicable_unity_gui"
                if source == "unity_live_process"
                else _C1_NATIVE_DESKTOP_ALGORITHM
            ),
        )

    if native_contract is not None:
        source, algorithm = native_contract
        if source in {"openra_live_settings_process", "unity_live_process"}:
            expected_fields = {
                "schema_version",
                "source",
                "observed",
                "algorithm",
                "seed",
                "task_id",
                "random_control",
                "randomness",
                "seed_role",
                "initial",
                "final",
            }
            legacy_fields = expected_fields - {
                "random_control",
                "randomness",
                "seed_role",
            }
            if frozenset(value) not in {
                frozenset(expected_fields),
                frozenset(legacy_fields),
            }:
                issues.append("native seed evidence has invalid fields")
            if (
                type(value.get("schema_version")) is not int
                or value.get("schema_version") != 1
            ):
                issues.append("native seed evidence schema_version must be integer 1")
        if value.get("source") != source:
            issues.append(f"native seed evidence source must be {source}")
        if value.get("algorithm") != algorithm:
            issues.append("native seed evidence has invalid algorithm")
        if type(value.get("seed")) is not int or value.get("seed") != seed:
            issues.append(f"native observed seed does not match trial seed {seed}")
        if (
            source in {"openra_live_settings_process", "unity_live_process"}
            and value.get("task_id") != task_id
        ):
            issues.append("native seed evidence is bound to the wrong task")
        initial = value.get("initial")
        snapshot_source = (
            "openra_live_gameplay_process"
            if source == "openra_live_settings_process"
            and isinstance(initial, dict)
            and initial.get("source") == "openra_live_gameplay_process"
            else source
        )
        snapshot_algorithm = (
            "not_applicable_live_gameplay"
            if snapshot_source == "openra_live_gameplay_process"
            else algorithm
        )
        issues.extend(
            _native_process_seed_snapshot_issues(
                initial,
                label="initial",
                benchmark=benchmark,
                source=snapshot_source,
                algorithm=snapshot_algorithm,
                seed=seed,
                task_id=task_id,
                expected_semantic_stage=expected_semantic_stage,
                require_semantic_fingerprint=require_final,
            )
        )
        final = value.get("final")
        if require_final:
            issues.extend(
                _native_process_seed_snapshot_issues(
                    final,
                    label="final",
                    benchmark=benchmark,
                    source=snapshot_source,
                    algorithm=snapshot_algorithm,
                    seed=seed,
                    task_id=task_id,
                    expected_semantic_stage=None,
                    require_semantic_fingerprint=True,
                )
            )
        elif final is not None:
            issues.extend(
                _native_process_seed_snapshot_issues(
                    final,
                    label="final",
                    benchmark=benchmark,
                    source=snapshot_source,
                    algorithm=snapshot_algorithm,
                    seed=seed,
                    task_id=task_id,
                    expected_semantic_stage=None,
                    require_semantic_fingerprint=False,
                )
            )
        if isinstance(initial, dict) and isinstance(final, dict):
            initial_pid = _integer(initial.get("process_id"))
            final_pid = _integer(final.get("process_id"))
            if initial_pid is not None and final_pid is not None and final_pid != initial_pid:
                issues.append("native final process_id differs from initial process_id")
            if source == "unity_live_process":
                initial_sequence = _integer(initial.get("sequence"))
                final_sequence = _integer(final.get("sequence"))
                if (
                    initial_sequence is not None
                    and final_sequence is not None
                    and final_sequence < initial_sequence
                ):
                    issues.append("Unity final sequence precedes initial sequence")
        return issues

    if value.get("source") != "browser_page_runtime":
        issues.append("browser seed evidence source is not the live page")
    if value.get("algorithm") != "mulberry32":
        issues.append("browser seed evidence has invalid algorithm")
    if _integer(value.get("seed")) != seed:
        issues.append(f"browser observed seed does not match trial seed {seed}")
    initial = value.get("initial")
    issues.extend(_browser_seed_snapshot_issues(initial, label="initial", seed=seed))
    final = value.get("final")
    if require_final:
        issues.extend(_browser_seed_snapshot_issues(final, label="final", seed=seed))
        if isinstance(initial, dict) and isinstance(final, dict):
            initial_calls = _integer(initial.get("calls"))
            final_calls = _integer(final.get("calls"))
            if (
                initial_calls is not None
                and final_calls is not None
                and final_calls < initial_calls
            ):
                issues.append("browser final PRNG call counter precedes initial counter")
    elif final is not None:
        issues.extend(_browser_seed_snapshot_issues(final, label="final", seed=seed))
    return issues


def _c1_state_pair_snapshot_issues(
    value: Any,
    *,
    label: str,
    contract: dict[str, Any],
    seed: int,
    task_id: str,
) -> list[str]:
    if not isinstance(value, dict):
        return [f"native State pair is missing its {label} live snapshot"]
    issues: list[str] = []
    expected_fields = {
        "schema_version",
        "source",
        "observed",
        "algorithm",
        "seed",
        "task_id",
        "benchmark",
        "stage",
        "process_id",
        "process_alive_at_observation",
        "semantic_observation",
        "semantic_fingerprint",
        "expected_task_id",
        "expected_seed",
    }
    if contract["protocol"] is not None:
        expected_fields.update(
            {"protocol_version", "expected_stage", "expected_benchmark"}
        )
    if set(value) != expected_fields:
        issues.append(f"native State {label} snapshot has invalid fields")
    if type(value.get("schema_version")) is not int or value.get(
        "schema_version"
    ) != 1:
        issues.append(f"native State {label} snapshot schema_version must be 1")
    if value.get("source") != contract["snapshot_source"]:
        issues.append(f"native State {label} snapshot has the wrong source")
    if value.get("observed") is not True:
        issues.append(f"native State {label} snapshot was not observed live")
    if value.get("algorithm") != contract["algorithm"]:
        issues.append(f"native State {label} snapshot has the wrong algorithm")
    if type(value.get("seed")) is not int or value.get("seed") != seed:
        issues.append(f"native State {label} snapshot is bound to the wrong seed")
    if value.get("task_id") != task_id:
        issues.append(f"native State {label} snapshot is bound to the wrong task")
    if value.get("expected_task_id") != task_id:
        issues.append(
            f"native State {label} expected-task binding is inconsistent"
        )
    if (
        type(value.get("expected_seed")) is not int
        or value.get("expected_seed") != seed
    ):
        issues.append(
            f"native State {label} expected-seed binding is inconsistent"
        )
    if str(value.get("benchmark") or "").strip().lower() != str(
        contract["reported_benchmark"]
    ).strip().lower():
        issues.append(f"native State {label} snapshot has the wrong benchmark")
    expected_stage = "before" if label == "initial" else "after"
    if value.get("stage") != expected_stage:
        issues.append(f"native State {label} snapshot has the wrong stage")
    if contract["protocol"] is not None:
        if value.get("protocol_version") != contract["protocol"]:
            issues.append(
                f"Unity State {label} snapshot has the wrong evidence protocol"
            )
        if value.get("expected_stage") != expected_stage:
            issues.append(
                f"Unity State {label} expected-stage binding is inconsistent"
            )
        if str(value.get("expected_benchmark") or "").strip().lower() != str(
            contract["reported_benchmark"]
        ).strip().lower():
            issues.append(
                f"Unity State {label} expected-benchmark binding is inconsistent"
            )
    process_id = value.get("process_id")
    if type(process_id) is not int or process_id <= 0:
        issues.append(f"native State {label} snapshot lacks a positive process_id")
    if value.get("process_alive_at_observation") is not True:
        issues.append(
            f"native State {label} snapshot was not captured from a live process"
        )

    observation = value.get("semantic_observation")
    fingerprint = value.get("semantic_fingerprint")
    if not (
        isinstance(fingerprint, str)
        and len(fingerprint) == 64
        and all(character in "0123456789abcdef" for character in fingerprint)
    ):
        issues.append(
            f"native State {label} semantic fingerprint is missing or malformed"
        )
    if not isinstance(observation, dict) or not observation:
        issues.append(f"native State {label} semantic observation is missing")
        return issues
    canonical = json.dumps(
        observation,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    if fingerprint != hashlib.sha256(canonical).hexdigest():
        issues.append(
            f"native State {label} semantic fingerprint does not match observation"
        )
    if observation.get("source") != contract["semantic_source"]:
        issues.append(f"native State {label} semantic observation has the wrong source")
    if observation.get("live") is not True:
        issues.append(f"native State {label} semantic observation is not live")
    status = observation.get("status")
    checks = observation.get("checks")

    if contract["benchmark"] == "openra_gameplay":
        checks_well_formed = bool(
            isinstance(checks, list)
            and checks
            and all(
                isinstance(check, dict)
                and isinstance(check.get("name"), str)
                and bool(check.get("name"))
                and check.get("status") in {"success", "failure"}
                and isinstance(check.get("detail"), str)
                and bool(check.get("detail"))
                and set(check).issubset(
                    {"name", "status", "detail", "expected", "actual"}
                )
                for check in checks
            )
        )
        if not checks_well_formed:
            issues.append(
                f"OpenRA gameplay State {label} semantic checks are malformed"
            )
            return issues
        names = [str(check["name"]) for check in checks]
        if len(names) != len(set(names)):
            issues.append(
                f"OpenRA gameplay State {label} semantic checks contain duplicates"
            )
        errors = observation.get("errors")
        if not isinstance(errors, list) or any(
            not isinstance(error, str) for error in errors
        ):
            issues.append(
                f"OpenRA gameplay State {label} errors are malformed"
            )
            errors = []
        checks_ok = all(check["status"] == "success" for check in checks)
        expected_ok = checks_ok and not errors
        if observation.get("ok") is not expected_ok:
            issues.append(
                f"OpenRA gameplay State {label} semantic ok disagrees with checks"
            )
        if status != ("success" if expected_ok else "gameplay_mismatch"):
            issues.append(
                f"OpenRA gameplay State {label} status disagrees with checks"
            )
        return issues

    if contract["benchmark"] == "openra":
        checks_well_formed = bool(
            isinstance(checks, list)
            and checks
            and all(
                isinstance(check, dict)
                and set(check)
                == {"section", "key", "expected", "actual", "ok"}
                and isinstance(check.get("section"), str)
                and bool(check.get("section"))
                and isinstance(check.get("key"), str)
                and bool(check.get("key"))
                and "expected" in check
                and "actual" in check
                and type(check.get("ok")) is bool
                for check in checks
            )
        )
        if not checks_well_formed:
            issues.append(f"OpenRA State {label} semantic checks are malformed")
            return issues
        check_keys = [(check["section"], check["key"]) for check in checks]
        if len(check_keys) != len(set(check_keys)):
            issues.append(f"OpenRA State {label} semantic checks contain duplicates")
        failed = [
            {
                "section": check["section"],
                "key": check["key"],
                "expected": check["expected"],
                "actual": check["actual"],
            }
            for check in checks
            if not check["ok"]
        ]
        if observation.get("mismatches") != failed:
            issues.append(
                f"OpenRA State {label} semantic mismatches disagree with checks"
            )
        checks_ok = all(check["ok"] for check in checks)
        if observation.get("ok") is not checks_ok:
            issues.append(f"OpenRA State {label} semantic ok disagrees with checks")
        expected_status = "success" if checks_ok else "settings_mismatch"
        if status != expected_status:
            issues.append(
                f"OpenRA State {label} semantic status disagrees with checks"
            )
        settings = observation.get("settings")
        if not isinstance(settings, dict) or not settings:
            issues.append(
                f"OpenRA State {label} semantic observation lacks live settings"
            )
        else:
            for check in checks:
                section_settings = settings.get(check["section"])
                if (
                    not isinstance(section_settings, dict)
                    or check["key"] not in section_settings
                    or section_settings[check["key"]] != check["actual"]
                ):
                    issues.append(
                        f"OpenRA State {label} live settings disagree with semantic checks"
                    )
                    break
        return issues

    checks_well_formed = bool(
        isinstance(checks, list)
        and checks
        and all(
            isinstance(check, dict)
            and isinstance(check.get("name"), str)
            and bool(check.get("name"))
            and check.get("status") in {"success", "failure"}
            and isinstance(check.get("detail"), str)
            and bool(check.get("detail"))
            for check in checks
        )
    )
    if not checks_well_formed:
        issues.append(f"Unity State {label} semantic checks are malformed")
        return issues
    check_names = [check["name"] for check in checks]
    if len(check_names) != len(set(check_names)):
        issues.append(f"Unity State {label} semantic checks contain duplicates")
    checks_ok = all(check["status"] == "success" for check in checks)
    if status != ("success" if checks_ok else "failure"):
        issues.append(f"Unity State {label} semantic status disagrees with checks")
    detail = observation.get("detail")
    if not isinstance(detail, str) or not detail:
        issues.append(f"Unity State {label} semantic detail is missing")
    elif (detail == "All checks passed.") is not checks_ok:
        issues.append(f"Unity State {label} semantic detail disagrees with checks")
    screenshot_status = observation.get("screenshot_status")
    screenshot_source = observation.get("screenshot_source")
    if screenshot_status not in {"success", "failure", "skipped"}:
        issues.append(f"Unity State {label} screenshot status is malformed")
    expected_screenshot_source = {
        "success": "unity_capture",
        "failure": "capture_failure",
        "skipped": "",
    }.get(screenshot_status)
    if screenshot_source != expected_screenshot_source:
        issues.append(
            f"Unity State {label} screenshot source disagrees with capture status"
        )
    return issues


def _c1_state_process_pair_issues(
    evidence: dict[str, Any],
    *,
    contract: dict[str, Any],
    seed: int,
    task_id: str,
    allow_start_terminal: bool = False,
) -> list[str]:
    issues: list[str] = []
    pair_fields = {
        "schema_version",
        "source",
        "observed",
        "algorithm",
        "seed",
        "task_id",
        "benchmark",
        "random_control",
        "randomness",
        "seed_role",
        "pair_semantics",
        "initial",
        "final",
    }
    start_fields = pair_fields | {"terminal_stage"}
    schema_version = evidence.get("schema_version")
    start_terminal = schema_version == 2
    expected_fields = start_fields if start_terminal else pair_fields
    legacy_fields = expected_fields - {
        "random_control",
        "randomness",
        "seed_role",
    }
    if frozenset(evidence) not in {
        frozenset(expected_fields),
        frozenset(legacy_fields),
    }:
        issues.append("native State pair has invalid fields")
    if type(schema_version) is not int or schema_version not in {1, 2}:
        issues.append("native State pair schema_version must be 1 or 2")
    if start_terminal and not allow_start_terminal:
        issues.append("native State start-terminal evidence is not allowed here")
    if evidence.get("source") != contract["pair_source"]:
        issues.append("native State pair has the wrong source")
    if evidence.get("observed") is not True:
        issues.append("native State pair evidence was not observed")
    if evidence.get("algorithm") != contract["algorithm"]:
        issues.append("native State pair has the wrong algorithm")
    if type(evidence.get("seed")) is not int or evidence.get("seed") != seed:
        issues.append("native State pair is bound to the wrong seed")
    if evidence.get("task_id") != task_id:
        issues.append("native State pair is bound to the wrong task")
    if str(evidence.get("benchmark") or "").strip().lower() != str(
        contract["reported_benchmark"]
    ).strip().lower():
        issues.append("native State pair is bound to the wrong benchmark")
    expected_pair_semantics = (
        "start_gate_terminal"
        if start_terminal
        else contract.get(
            "pair_semantics", "separate_registered_before_after_processes"
        )
    )
    if evidence.get("pair_semantics") != expected_pair_semantics:
        issues.append("native State pair has invalid process topology semantics")
    if start_terminal:
        if evidence.get("terminal_stage") != "start":
            issues.append("native State start-terminal evidence has invalid stage")
        if evidence.get("final") is not None:
            issues.append("native State start-terminal evidence must not contain final")
    for label in (("initial",) if start_terminal else ("initial", "final")):
        issues.extend(
            _c1_state_pair_snapshot_issues(
                evidence.get(label),
                label=label,
                contract=contract,
                seed=seed,
                task_id=task_id,
            )
        )
    if (
        not start_terminal
        and expected_pair_semantics
        == "same_persistent_registered_gameplay_process"
        and isinstance(evidence.get("initial"), dict)
        and isinstance(evidence.get("final"), dict)
        and evidence["initial"].get("process_id")
        != evidence["final"].get("process_id")
    ):
        issues.append("native State gameplay pair changed live process")
    # Deliberately no PID-equality condition: these registered arm-E backends
    # launch independent before/after processes.  Each PID must still be live and
    # positive at its own observation boundary.
    return issues


def _c1_state_runtime_evidence_issues(
    result: dict[str, Any],
    *,
    benchmark: str,
    seed: int,
    task_id: str,
) -> list[str]:
    """Validate runtime provenance emitted by state-managed arm E.

    State implementations have benchmark-specific process topology.  Blender and
    LibreOffice use one persistent headed process and therefore require the same
    native initial/final snapshot contract as their GUI arms, including identical
    PIDs.  Other registered state backends can extend this dispatcher with their
    own pair contract instead of weakening the desktop invariant.
    """

    normalized = str(benchmark).strip().lower().replace("-", "").replace("_", "")
    desktop_contract = _C1_NATIVE_DESKTOP_CONTRACTS.get(normalized)
    pair_contract = _C1_STATE_PAIR_CONTRACTS.get(normalized)
    emitted = any(
        field in result for field in ("runtime_started", "prng", "seed_evidence")
    )
    if desktop_contract is None and pair_contract is None and not emitted:
        return []

    issues: list[str] = []
    runtime_started = result.get("runtime_started")
    raw_prng = result.get("prng")
    prng = dict(raw_prng) if isinstance(raw_prng, dict) else None
    raw_evidence = result.get("seed_evidence")
    evidence = dict(raw_evidence) if isinstance(raw_evidence, dict) else None
    if not isinstance(runtime_started, bool):
        issues.append("state runtime_started must be an explicit boolean")
        return issues
    if not isinstance(evidence, dict):
        issues.append("state runtime is missing seed_evidence")
        return issues

    if (
        normalized == "openra"
        and evidence.get("source") == "openra_gameplay_state_live_pair"
    ):
        pair_contract = _C1_STATE_PAIR_CONTRACTS["openragameplay"]

    goal_evaluation_status = result.get("goal_evaluation_status")
    start_terminal = goal_evaluation_status == "not_evaluated_start_failed"
    pre_goal_terminal = bool(
        isinstance(goal_evaluation_status, str)
        and goal_evaluation_status.startswith("not_evaluated")
    )

    if runtime_started is False:
        if evidence.get("observed") is not False:
            issues.append("state runtime-not-started seed evidence must be unobserved")
        if evidence.get("source") != "runtime_not_started":
            issues.append("state runtime-not-started seed evidence has invalid source")
        if pair_contract is not None:
            expected_fields = {
                "schema_version",
                "source",
                "observed",
                "algorithm",
                "seed",
                "task_id",
                "benchmark",
                "random_control",
                "randomness",
                "seed_role",
            }
            legacy_fields = expected_fields - {
                "random_control",
                "randomness",
                "seed_role",
            }
            if frozenset(evidence) not in {
                frozenset(expected_fields),
                frozenset(legacy_fields),
            }:
                issues.append(
                    "native State runtime-not-started evidence has invalid fields"
                )
            if (
                type(evidence.get("schema_version")) is not int
                or evidence.get("schema_version") != 1
            ):
                issues.append(
                    "native State runtime-not-started schema_version must be 1"
                )
            if evidence.get("algorithm") != "not_applicable":
                issues.append(
                    "native State runtime-not-started evidence has invalid algorithm"
                )
            if (
                type(evidence.get("seed")) is not int
                or evidence.get("seed") != seed
            ):
                issues.append(
                    "native State runtime-not-started evidence is bound to the wrong seed"
                )
            if evidence.get("task_id") != task_id:
                issues.append(
                    "native State runtime-not-started evidence is bound to the wrong task"
                )
            if str(evidence.get("benchmark") or "").strip().lower() != str(
                pair_contract["reported_benchmark"]
            ).strip().lower():
                issues.append(
                    "native State runtime-not-started evidence is bound to the wrong benchmark"
                )
        if prng is not None:
            issues.append("state runtime-not-started trial must not claim prng")
        return issues

    if not isinstance(prng, dict):
        issues.append("state runtime started but prng evidence is missing")
    if pair_contract is not None:
        issues.extend(
            _c1_state_process_pair_issues(
                evidence,
                contract=pair_contract,
                seed=seed,
                task_id=task_id,
                allow_start_terminal=start_terminal,
            )
        )
    else:
        issues.extend(
            _runtime_seed_evidence_issues(
                evidence,
                benchmark=benchmark,
                seed=seed,
                task_id=task_id,
                expected_semantic_stage="restored",
                require_final=desktop_contract is not None and not pre_goal_terminal,
            )
        )
    if isinstance(prng, dict):
        if prng.get("algorithm") != evidence.get("algorithm"):
            issues.append("state prng algorithm differs from seed_evidence")
        if _integer(prng.get("seed")) != _integer(evidence.get("seed")):
            issues.append("state prng seed differs from seed_evidence")
        final = evidence.get("final")
        expected_snapshot = (
            evidence.get("initial")
            if pre_goal_terminal and final is None
            else final
        )
        if (
            desktop_contract is not None or pair_contract is not None
        ) and prng != expected_snapshot:
            issues.append("native State prng must equal terminal live snapshot")
    return issues


def _normalize_c1_trial(
    arm: str,
    report: dict[str, Any],
    result: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    model = _report_model(report)
    seed = _report_seed(report, result)
    task_id = str(result.get("task_id") or "")
    issues: list[str] = []
    is_state = arm == "c1_state"
    preserved_failure_kind = _preserved_model_failure_kind(
        result=result, report=report
    )
    raw_cost, actual_cost, transient_infrastructure_cost, attempt_issues = (
        _c1_attempt_cost_issues(
            result,
            is_state=is_state,
            allow_nonretryable_terminal=(
                preserved_failure_kind
                == "exact_usage_nonretryable_model_failure"
            ),
        )
    )
    issues.extend(attempt_issues)
    cost_fields = _task_cost({"cost": raw_cost})
    if is_state:
        state_metrics = derive_state_stage_metrics(result)
        start_success = state_metrics["start_success"] is True
        goal_success = state_metrics["goal_success"] is True
        e2e_success = state_metrics["e2e_success"] is True
        before_visual_ok = state_metrics["before_visual_ok"] is True
        after_visual_ok = state_metrics["after_visual_ok"] is True
        before_semantic_ok = state_metrics["before_semantic_ok"] is True
        after_semantic_ok = state_metrics["after_semantic_ok"] is True
        if (
            state_metrics["stage_metrics_source"] == "explicit_v1"
            and not state_metrics["stage_metrics_complete"]
        ):
            issues.extend(explicit_state_stage_metric_issues(result))
        if state_metrics["stage_metrics_source"] == "explicit_v1":
            expected_contract = {
                "schema_version": STATE_STAGE_METRICS_SCHEMA_VERSION,
                "start_gate_boundary": "before_registered_transition",
                "start_success": "before_semantic_and_before_visual",
                "goal_success": "after_semantic_and_after_visual",
                "e2e_success": "start_success_and_goal_success",
            }
            if report.get("state_stage_metrics") != expected_contract:
                issues.append("missing or invalid State stage-metrics report contract")
        if isinstance(result.get("e2e_success"), bool) and bool(
            result.get("result")
        ) != result["e2e_success"]:
            issues.append("State result disagrees with e2e_success")
        timing_values = {
            field: _number(result.get(field)) for field in _GUI_V2_TIME_FIELDS
        }
        missing_time = [
            field for field, value in timing_values.items() if value is None
        ]
        if missing_time:
            issues.append("missing unified state time fields: " + ",".join(missing_time))
        time_to_start = timing_values["time_to_start_ms"]
        time_start_to_goal = timing_values["time_start_to_goal_ms"]
        time_excluding = timing_values["time_e2e_excluding_synthesis_ms"]
        time_including = _number(result.get("time_e2e_including_synthesis_ms"))
        termination_time = _number(result.get("termination_time_ms"))
        if not isinstance(result.get("result"), bool):
            issues.append("missing boolean result")
        for field, value in timing_values.items():
            if value is not None and value < 0:
                issues.append(f"negative unified state time field: {field}")
        if None not in (time_to_start, time_start_to_goal, time_excluding) and not math.isclose(
            float(time_excluding),
            float(time_to_start) + float(time_start_to_goal),
            rel_tol=0.0,
            abs_tol=1.0,
        ):
            issues.append("state time_e2e_excluding_synthesis_ms is inconsistent")
        synthesis_time = timing_values.get("checkpoint_synthesis_ms")
        if None not in (time_excluding, time_including, synthesis_time) and not math.isclose(
            float(time_including),
            float(time_excluding) + float(synthesis_time),
            rel_tol=0.0,
            abs_tol=1.0,
        ):
            issues.append("state time_e2e_including_synthesis_ms is inconsistent")
        if None not in (termination_time, time_including) and not math.isclose(
            float(termination_time), float(time_including), rel_tol=0.0, abs_tol=1.0
        ):
            issues.append("state termination_time_ms is inconsistent with E2E time")
        restore_time = timing_values.get("checkpoint_restore_ms")
        if restore_time is not None and time_to_start is not None and restore_time > time_to_start + 1.0:
            issues.append("state checkpoint_restore_ms exceeds time_to_start_ms")
        if cost_fields.get("total_tokens") is None:
            issues.append("missing Pareto token cost")
        raw_prng = result.get("prng")
        prng = dict(raw_prng) if isinstance(raw_prng, dict) else None
        runtime_started = result.get("runtime_started")
        raw_seed_evidence = result.get("seed_evidence")
        seed_evidence = (
            dict(raw_seed_evidence)
            if isinstance(raw_seed_evidence, dict)
            else None
        )
        issues.extend(
            _c1_state_runtime_evidence_issues(
                result,
                benchmark=str(result.get("benchmark") or ""),
                seed=seed,
                task_id=task_id,
            )
        )
        initial_state_fingerprint = result.get("initial_state_fingerprint")
        decision_budget = None
        steps = None
        budget_exhausted = None
        synthesis_actual_cost = result.get("synthesis_actual_cost")
        synthesis_transient_actual_cost = result.get(
            "synthesis_transient_actual_cost"
        )
        synthesis_attributed_cost = result.get("synthesis_attributed_cost")
        synthesis_cache_fingerprint = result.get("synthesis_cache_fingerprint")
        synthesis_attributed_fingerprints = None
        synthesis_attributed_burdens = None
        synthesis_cache_hit = result.get("synthesis_cache_hit")
        synthesis_actual_incurred_this_attempt = None
        policy_phase_started = None
        for field, value in (
            ("synthesis_actual_cost", synthesis_actual_cost),
            ("synthesis_transient_actual_cost", synthesis_transient_actual_cost),
            ("synthesis_attributed_cost", synthesis_attributed_cost),
        ):
            if not isinstance(value, dict):
                issues.append(f"state arm missing {field}")
        if not isinstance(synthesis_cache_hit, bool):
            issues.append("state arm missing boolean synthesis_cache_hit")
        visual_grading_started = result.get("visual_grading_started")
        if not isinstance(visual_grading_started, bool):
            issues.append("missing boolean visual_grading_started")
        if isinstance(result.get("pair_verdict"), dict) and visual_grading_started is not True:
            issues.append("pair_verdict requires visual_grading_started=true")
    else:
        missing_bool = [field for field in _GUI_V2_BOOL_FIELDS if not isinstance(result.get(field), bool)]
        missing_time = [field for field in _GUI_V2_TIME_FIELDS if _number(result.get(field)) is None]
        if missing_bool:
            issues.append("missing v2 bool fields: " + ",".join(missing_bool))
        if missing_time:
            issues.append("missing v2 time fields: " + ",".join(missing_time))
        before_visual_ok = bool(result.get("before_visual_ok"))
        before_semantic_ok = bool(result.get("before_semantic_ok"))
        after_visual_ok = bool(result.get("after_visual_ok"))
        after_semantic_ok = bool(result.get("after_semantic_ok"))
        start_success = before_visual_ok and before_semantic_ok
        goal_success = after_visual_ok and after_semantic_ok
        e2e_success = start_success and goal_success
        if isinstance(result.get("start_success"), bool) and result["start_success"] != start_success:
            issues.append("inconsistent start_success")
        if isinstance(result.get("goal_success"), bool) and result["goal_success"] != goal_success:
            issues.append("inconsistent goal_success")
        if isinstance(result.get("e2e_success"), bool) and result["e2e_success"] != e2e_success:
            issues.append("inconsistent e2e_success")
        time_to_start = _number(result.get("time_to_start_ms"))
        time_start_to_goal = _number(result.get("time_start_to_goal_ms"))
        time_excluding = _number(result.get("time_e2e_excluding_synthesis_ms"))
        time_including = _number(result.get("time_e2e_including_synthesis_ms"))
        termination_time = _number(result.get("termination_time_ms"))
        timing_values = {
            field: _number(result.get(field))
            for field in _GUI_V2_TIME_FIELDS
        }
        for field, value in timing_values.items():
            if value is not None and value < 0:
                issues.append(f"negative v2 time field: {field}")
        if None not in (time_to_start, time_start_to_goal, time_excluding):
            if not math.isclose(
                float(time_excluding),
                float(time_to_start) + float(time_start_to_goal),
                rel_tol=0.0,
                abs_tol=1.0,
            ):
                issues.append("time_e2e_excluding_synthesis_ms is inconsistent")
        synthesis_time = timing_values.get("checkpoint_synthesis_ms")
        if None not in (time_excluding, time_including, synthesis_time):
            if not math.isclose(
                float(time_including),
                float(time_excluding) + float(synthesis_time),
                rel_tol=0.0,
                abs_tol=1.0,
            ):
                issues.append("time_e2e_including_synthesis_ms is inconsistent")
        if None not in (termination_time, time_including) and not math.isclose(
            float(termination_time),
            float(time_including),
            rel_tol=0.0,
            abs_tol=1.0,
        ):
            issues.append("termination_time_ms is inconsistent with E2E time")
        restore_time = timing_values.get("checkpoint_restore_ms")
        if (
            restore_time is not None
            and time_to_start is not None
            and restore_time > time_to_start + 1.0
            and result.get("start_construction_status") != "restore_failed"
        ):
            issues.append("checkpoint_restore_ms exceeds time_to_start_ms")
        required_cost_fields = (
            "total_tokens",
            "cached_prompt_tokens",
            "reasoning_tokens_observed",
            "reasoning_tokens_lower_bound",
            "reasoning_tokens_upper_bound",
            "reasoning_detail_complete",
            "reasoning_detail_unknown_fraction",
            "reasoning_detail_within_tolerance",
        )
        missing_cost = [
            field for field in required_cost_fields if field not in raw_cost
        ]
        if missing_cost:
            issues.append("missing v2 cost fields: " + ",".join(missing_cost))
        synthesis_actual_cost = result.get("synthesis_actual_cost")
        synthesis_transient_actual_cost = result.get(
            "synthesis_transient_actual_cost"
        )
        synthesis_attributed_cost = result.get("synthesis_attributed_cost")
        for field, summary in (
            ("synthesis_actual_cost", synthesis_actual_cost),
            ("synthesis_transient_actual_cost", synthesis_transient_actual_cost),
            ("synthesis_attributed_cost", synthesis_attributed_cost),
        ):
            if not isinstance(summary, dict):
                issues.append(f"missing v2 cost field: {field}")
                continue
            nested_issues = _cost_summary_issues(summary)
            issues.extend(f"{field}: {issue}" for issue in nested_issues)
        if isinstance(synthesis_attributed_cost, dict):
            issues.extend(
                _cost_component_issues(
                    synthesis_attributed_cost,
                    raw_cost,
                    component_label="synthesis_attributed_cost",
                )
            )
        if isinstance(synthesis_transient_actual_cost, dict) and isinstance(
            synthesis_actual_cost, dict
        ):
            issues.extend(
                _cost_component_issues(
                    synthesis_transient_actual_cost,
                    synthesis_actual_cost,
                    component_label="synthesis_transient_actual_cost",
                    total_label="synthesis_actual_cost",
                )
            )
        actual_signature = _cost_signature(synthesis_actual_cost)
        transient_signature = _cost_signature(synthesis_transient_actual_cost)
        attributed_signature = _cost_signature(synthesis_attributed_cost)
        if None not in (actual_signature, transient_signature, attributed_signature):
            assert actual_signature is not None
            assert transient_signature is not None
            assert attributed_signature is not None
            core_actual = tuple(
                actual - transient
                for actual, transient in zip(actual_signature, transient_signature)
            )
            if any(value < 0 for value in core_actual) or core_actual not in {
                tuple(0 for _ in core_actual),
                attributed_signature,
            }:
                issues.append(
                    "synthesis core actual cost must be zero or equal attributed cost"
                )
        horizon = str(result.get("horizon_class") or "")
        expected_budget = _C1_STEP_BUDGETS.get(horizon)
        report_budgets = report.get("step_budgets")
        normalized_report_budgets = None
        if isinstance(report_budgets, dict):
            try:
                normalized_report_budgets = {
                    str(key): int(value) for key, value in report_budgets.items()
                }
            except (TypeError, ValueError):
                normalized_report_budgets = None
        if normalized_report_budgets != _C1_STEP_BUDGETS:
            issues.append(
                f"report step_budgets must be uniform {C1_STEP_BUDGET} for every horizon"
            )
        if expected_budget is None:
            issues.append("missing or invalid horizon_class")

        decision_budget = _integer(result.get("decision_budget"))
        if decision_budget is None:
            issues.append("missing integer decision_budget")
        elif decision_budget != C1_STEP_BUDGET:
            issues.append(
                f"decision_budget {decision_budget} does not match the uniform "
                f"budget {C1_STEP_BUDGET}"
            )

        raw_steps = result.get("steps")
        steps = dict(raw_steps) if isinstance(raw_steps, dict) else None
        step_values = {
            field: _integer(raw_steps.get(field)) if isinstance(raw_steps, dict) else None
            for field in ("phase_a", "phase_b", "total")
        }
        if any(value is None or value < 0 for value in step_values.values()):
            issues.append("steps must contain non-negative integer phase_a/phase_b/total")
        elif step_values["total"] != step_values["phase_a"] + step_values["phase_b"]:
            issues.append("steps.total does not equal phase_a + phase_b")
        elif decision_budget is not None and step_values["total"] > decision_budget:
            issues.append("steps.total exceeds decision_budget")
        if arm in {"c1_gui_synth", "c1_agent_s_synth"} and step_values["phase_a"] != 0:
            issues.append("synthesized arm must have steps.phase_a=0")
        if arm in {"c1_gui", "c1_agent_s"}:
            if timing_values.get("checkpoint_synthesis_ms") not in {0, 0.0}:
                issues.append("from-start arm must have checkpoint_synthesis_ms=0")
            if isinstance(synthesis_actual_cost, dict) and not _cost_is_zero(synthesis_actual_cost):
                issues.append("from-start arm must have zero synthesis_actual_cost")
            if isinstance(synthesis_transient_actual_cost, dict) and not _cost_is_zero(
                synthesis_transient_actual_cost
            ):
                issues.append(
                    "from-start arm must have zero synthesis_transient_actual_cost"
                )
            if isinstance(synthesis_attributed_cost, dict) and not _cost_is_zero(synthesis_attributed_cost):
                issues.append("from-start arm must have zero synthesis_attributed_cost")

        budget_exhausted = result.get("budget_exhausted")
        if not isinstance(budget_exhausted, bool):
            issues.append("missing boolean budget_exhausted")
        elif decision_budget is not None and step_values["total"] is not None:
            expected_exhausted = step_values["total"] >= decision_budget
            if budget_exhausted != expected_exhausted:
                issues.append("budget_exhausted is inconsistent with steps and decision_budget")

        raw_prng = result.get("prng")
        prng = dict(raw_prng) if isinstance(raw_prng, dict) else None
        runtime_started = result.get("runtime_started")
        policy_phase_started = result.get("policy_phase_started")
        start_grading_started = result.get("start_grading_started")
        visual_grading_started = result.get("visual_grading_started")
        goal_evaluation_status = result.get("goal_evaluation_status")
        explicit_start_gate_schema = isinstance(goal_evaluation_status, str)
        if not isinstance(start_grading_started, bool):
            if explicit_start_gate_schema:
                issues.append("missing boolean start_grading_started")
            else:
                # Compatibility for non-formal synthetic fixtures. Formal C1 is
                # independently gated on the schema-2 trial policy and live
                # reports always carry the explicit start-gate fields.
                start_grading_started = bool(visual_grading_started)
        raw_seed_evidence = result.get("seed_evidence")
        seed_evidence = (
            dict(raw_seed_evidence) if isinstance(raw_seed_evidence, dict) else None
        )
        if not isinstance(runtime_started, bool):
            issues.append("missing boolean runtime_started")
        if not isinstance(seed_evidence, dict):
            issues.append("missing runtime-observed seed_evidence")
        elif runtime_started is True:
            if not isinstance(prng, dict):
                issues.append("runtime started but missing observed prng metadata")
            issues.extend(
                _runtime_seed_evidence_issues(
                    seed_evidence,
                    benchmark=str(result.get("benchmark") or ""),
                    seed=seed,
                    task_id=task_id,
                    expected_semantic_stage=(
                        "restored"
                        if arm in {"c1_gui_synth", "c1_agent_s_synth"}
                        else "entry"
                    ),
                    require_final=bool((step_values.get("total") or 0) > 0),
                )
            )
            if isinstance(prng, dict):
                if _integer(prng.get("seed")) != _integer(seed_evidence.get("seed")):
                    issues.append("prng seed differs from runtime seed evidence")
                if seed_evidence.get("source") in {
                    "openra_live_settings_process",
                    "unity_live_process",
                }:
                    if prng != seed_evidence.get("final"):
                        issues.append(
                            "native prng metadata is not the exact final task-bound snapshot"
                        )
                elif prng.get("algorithm") != seed_evidence.get("algorithm"):
                    issues.append("prng algorithm differs from runtime seed evidence")
        elif runtime_started is False:
            if seed_evidence.get("observed") is not False:
                issues.append("runtime-not-started seed evidence must be unobserved")
            if seed_evidence.get("source") != "runtime_not_started":
                issues.append("runtime-not-started seed evidence has invalid source")
            if prng is not None:
                issues.append("runtime-not-started trial must not claim prng observation")
        if (step_values.get("total") or 0) > 0 and runtime_started is not True:
            issues.append("steps.total > 0 requires runtime_started=true")
        if (step_values.get("total") or 0) > 0 and policy_phase_started is not True:
            issues.append("steps.total > 0 requires policy_phase_started=true")
        if policy_phase_started is True and runtime_started is not True:
            issues.append("policy_phase_started=true requires runtime_started=true")
        if start_grading_started is True and runtime_started is not True:
            issues.append("start_grading_started=true requires runtime_started=true")
        if visual_grading_started is True and runtime_started is not True:
            issues.append("visual_grading_started=true requires runtime_started=true")
        if visual_grading_started is True and start_grading_started is not True:
            issues.append("visual_grading_started=true requires start_grading_started=true")
        if (
            visual_grading_started is True
            and policy_phase_started is not True
            and goal_evaluation_status != "not_evaluated_start_failed"
        ):
            issues.append(
                "visual grading before policy is allowed only for a failed start gate"
            )
        if goal_evaluation_status == "not_evaluated_start_failed":
            if start_success or goal_success or e2e_success:
                issues.append("start-gated trial cannot report success")
            if result.get("after_visual_status") != "not_evaluated":
                issues.append("start-gated trial must not evaluate after visual evidence")
            if result.get("after_semantic_status") != "not_evaluated":
                issues.append("start-gated trial must not evaluate after semantic evidence")
            if step_values.get("phase_b") != 0:
                issues.append("start-gated trial must have zero phase_b steps")
            if time_start_to_goal not in {0, 0.0}:
                issues.append("start-gated trial must have zero time_start_to_goal_ms")
        if isinstance(result.get("pair_verdict"), dict) and visual_grading_started is not True:
            issues.append("pair_verdict requires visual_grading_started=true")
        initial_state_fingerprint = result.get("initial_state_fingerprint")
        if not isinstance(initial_state_fingerprint, str) or not initial_state_fingerprint.strip():
            issues.append("missing initial_state_fingerprint")
        synthesis_cache_fingerprint = result.get("synthesis_cache_fingerprint")
        synthesis_attributed_fingerprints = result.get("synthesis_attributed_fingerprints")
        synthesis_attributed_burdens = result.get("synthesis_attributed_burdens")
        synthesis_cache_hit = result.get("synthesis_cache_hit")
        synthesis_actual_incurred_this_attempt = result.get(
            "synthesis_actual_incurred_this_attempt"
        )
        if arm in {"c1_gui_synth", "c1_agent_s_synth"}:
            if not isinstance(synthesis_cache_fingerprint, str) or not synthesis_cache_fingerprint:
                issues.append("synthesized arm missing synthesis_cache_fingerprint")
            if not isinstance(synthesis_attributed_fingerprints, list):
                issues.append("synthesized arm missing synthesis_attributed_fingerprints")
            elif synthesis_cache_fingerprint not in synthesis_attributed_fingerprints:
                issues.append("synthesis cache fingerprint is not attributed")
            if not isinstance(synthesis_attributed_burdens, list):
                issues.append("synthesized arm missing synthesis_attributed_burdens")
            if not isinstance(synthesis_cache_hit, bool):
                issues.append("synthesized arm missing boolean synthesis_cache_hit")
            if not isinstance(synthesis_actual_incurred_this_attempt, bool):
                issues.append(
                    "synthesized arm missing boolean synthesis_actual_incurred_this_attempt"
                )
            elif (
                synthesis_actual_incurred_this_attempt
                and isinstance(synthesis_actual_cost, dict)
                and _cost_is_zero(synthesis_actual_cost)
            ):
                issues.append(
                    "synthesis_actual_incurred_this_attempt has zero actual cost"
                )
    if not task_id:
        issues.append("missing task_id")
    if seed < 0:
        issues.append("missing seed")
    trial = {
        "arm": arm,
        "method": _C1_ALL_ARM_LABELS.get(arm, arm),
        "model": model,
        "seed": seed,
        "task_id": task_id,
        "result_status": result.get("status"),
        "raw_result": result.get("result"),
        "result_reason": result.get("reason"),
        "incomplete": result.get("incomplete"),
        "start_construction_status": result.get("start_construction_status"),
        "visual_only_evaluation_status": result.get(
            "visual_only_evaluation_status"
        ),
        "benchmark": result.get("benchmark"),
        "horizon_class": result.get("horizon_class"),
        "prompt_revision": result.get("prompt_revision"),
        "random_control": copy.deepcopy(result.get("random_control")),
        "random_control_evidence_valid": result.get(
            "random_control_evidence_valid"
        ),
        "reported_task_metadata": {
            "benchmark": result.get("benchmark"),
            "horizon_class": result.get("horizon_class"),
            "prompt_revision": result.get("prompt_revision"),
            "random_control": copy.deepcopy(result.get("random_control")),
        },
        "reference_reachable": result.get("reference_reachable"),
        "validity_class": result.get("validity_class"),
        "before_visual_ok": before_visual_ok,
        "before_semantic_ok": before_semantic_ok,
        "after_visual_ok": after_visual_ok,
        "after_semantic_ok": after_semantic_ok,
        "start_success": start_success,
        "goal_success": goal_success,
        "e2e_success": e2e_success,
        "time_to_start_ms": time_to_start,
        "time_start_to_goal_ms": time_start_to_goal,
        "time_e2e_excluding_synthesis_ms": time_excluding,
        "time_e2e_including_synthesis_ms": time_including,
        "termination_time_ms": termination_time,
        "checkpoint_synthesis_ms": timing_values.get("checkpoint_synthesis_ms"),
        "checkpoint_restore_ms": timing_values.get("checkpoint_restore_ms"),
        "runtime_bootstrap_ms": timing_values.get("runtime_bootstrap_ms"),
        "grader_ms": timing_values.get("grader_ms"),
        "prng": prng,
        "runtime_started": runtime_started,
        "policy_phase_started": policy_phase_started,
        "start_grading_started": (
            start_grading_started
            if not is_state
            else (
                result.get("start_grading_started")
                if isinstance(result.get("start_grading_started"), bool)
                else state_metrics["start_evaluated"]
            )
        ),
        "visual_grading_started": visual_grading_started,
        "goal_evaluation_status": (
            result.get("goal_evaluation_status")
            if not is_state
            else (
                result.get("goal_evaluation_status")
                if isinstance(result.get("goal_evaluation_status"), str)
                else (
                    "evaluated"
                    if state_metrics["goal_evaluated"]
                    else "not_evaluated"
                )
            )
        ),
        "state_stage_metrics_schema_version": (
            result.get("state_stage_metrics_schema_version") if is_state else None
        ),
        "stage_metrics_source": (
            state_metrics["stage_metrics_source"] if is_state else "explicit_gui"
        ),
        "stage_metrics_complete": (
            state_metrics["stage_metrics_complete"] if is_state else True
        ),
        "start_evaluated": (
            state_metrics["start_evaluated"] if is_state else start_grading_started
        ),
        "goal_evaluated": (
            state_metrics["goal_evaluated"]
            if is_state
            else result.get("goal_evaluation_status") == "evaluated"
        ),
        "start_gate_status": result.get("start_gate_status"),
        "before_visual_status": result.get("before_visual_status"),
        "after_visual_status": result.get("after_visual_status"),
        "before_semantic_status": result.get("before_semantic_status"),
        "after_semantic_status": result.get("after_semantic_status"),
        "start_grading_runtime_hold": result.get("start_grading_runtime_hold"),
        "pair_verdict_present": isinstance(result.get("pair_verdict"), dict),
        "pair_verdict": copy.deepcopy(result.get("pair_verdict")),
        "pair_start_gate_cache_hit": result.get("pair_start_gate_cache_hit"),
        "accepted_start_id": result.get("accepted_start_id"),
        "pair_start_id": result.get("pair_start_id"),
        "pair_start_fingerprint": result.get("pair_start_fingerprint"),
        "planner_called": result.get("planner_called"),
        "planner_called_after_start": result.get("planner_called_after_start"),
        "action_execution_started": result.get("action_execution_started"),
        "minimal_core_audit": copy.deepcopy(result.get("minimal_core_audit")),
        "local_start_attestation": copy.deepcopy(
            result.get("local_start_attestation")
        ),
        "screenshot_after": result.get("screenshot_after"),
        "phase_b_actions": result.get("phase_b_actions"),
        "semantic_probe_source": result.get("semantic_probe_source"),
        "policy_action_interface": result.get("policy_action_interface"),
        "policy_received_semantic_probe": result.get(
            "policy_received_semantic_probe"
        ),
        "policy_observation_source": result.get("policy_observation_source"),
        "scripted_transition_executed": result.get(
            "scripted_transition_executed"
        ),
        "policy_runtime_attestation": result.get("policy_runtime_attestation"),
        "before_semantic_detail": result.get("before_semantic_detail"),
        "after_semantic_detail": result.get(
            "after_semantic_detail", result.get("semantic_detail")
        ),
        "seed_evidence": seed_evidence,
        "initial_state_fingerprint": initial_state_fingerprint,
        "generated_spec_fingerprint": result.get("generated_spec_fingerprint"),
        # The state arm records the same whole-spec hash under its own key so the
        # two self-synthesizing arms' starts can be compared directly.
        "generated_spec_hash": result.get("generated_spec_hash"),
        "start_contract": result.get("start_contract"),
        "oracle_goldens": result.get("oracle_goldens"),
        "decision_budget": decision_budget,
        "steps": steps,
        "budget_exhausted": budget_exhausted,
        "synthesis_actual_cost": synthesis_actual_cost,
        "synthesis_transient_actual_cost": synthesis_transient_actual_cost,
        "synthesis_attributed_cost": synthesis_attributed_cost,
        "synthesis_cache_fingerprint": synthesis_cache_fingerprint,
        "synthesis_attributed_fingerprints": synthesis_attributed_fingerprints,
        "synthesis_attributed_burdens": synthesis_attributed_burdens,
        "synthesis_cache_hit": synthesis_cache_hit,
        "synthesis_actual_incurred_this_attempt": synthesis_actual_incurred_this_attempt,
        "attempt_count": result.get("attempt_count"),
        "attempt_costs": _compact_attempt_costs(result.get("attempt_costs")),
        "cost": _cost_ledger_view(raw_cost),
        "actual_cost": _cost_ledger_view(actual_cost),
        "transient_infrastructure_cost": _cost_ledger_view(
            transient_infrastructure_cost
        ),
        **cost_fields,
        "complete": not issues,
    }
    return trial, issues


def _index_c1_reports(
    arm: str,
    reports: list[dict[str, Any]],
    *,
    expected_inner_workers: int | None = None,
    formal_report_schema: bool = False,
) -> tuple[dict[str, dict[tuple[str, int], dict[str, Any]]], list[dict[str, Any]], list[str]]:
    by_model: dict[str, dict[tuple[str, int], dict[str, Any]]] = {}
    trials: list[dict[str, Any]] = []
    issues: list[str] = []
    resource_intervals: dict[
        tuple[str, str], list[tuple[int, int, str]]
    ] = {}
    for report in reports:
        preindexed = report.get("_p5_preindexed_c1")
        if isinstance(preindexed, dict):
            preindexed_issues = preindexed.get("issues")
            if isinstance(preindexed_issues, list):
                issues.extend(str(issue) for issue in preindexed_issues)
            for trial in preindexed.get("trials") or []:
                if not isinstance(trial, dict):
                    issues.append(f"{arm}: malformed preindexed trial")
                    continue
                model = str(trial.get("model") or "unknown")
                key = (str(trial.get("task_id") or ""), int(trial.get("seed", -1)))
                index = by_model.setdefault(model, {})
                if key in index:
                    issues.append(
                        f"{arm}/{model}/{key[0]}/seed{key[1]}: duplicate trial"
                    )
                    trial["complete"] = False
                else:
                    index[key] = trial
                trials.append(trial)
            continue
        model = _report_model(report)
        if expected_inner_workers is not None:
            scheduler = report.get("scheduler")
            if not isinstance(scheduler, dict):
                issues.append(f"{arm}/{model}: C1 scheduler provenance missing")
            else:
                scheduler_schema = scheduler.get("schema_version")
                if scheduler_schema not in {1, 2}:
                    issues.append(
                        f"{arm}/{model}: C1 scheduler schema_version must be 1 or 2"
                    )
                if scheduler.get("requested_inner_workers") != expected_inner_workers:
                    issues.append(
                        f"{arm}/{model}: requested inner workers differ from run index"
                    )
                effective = _integer(scheduler.get("effective_inner_workers"))
                if effective is None or not 1 <= effective <= expected_inner_workers:
                    issues.append(f"{arm}/{model}: effective inner workers are invalid")
                if scheduler_schema == 1:
                    if scheduler.get("lane_policy") != "game_interleaved_weighted_v1":
                        issues.append(f"{arm}/{model}: C1 lane policy is invalid")
                    if not isinstance(scheduler.get("lanes"), list):
                        issues.append(
                            f"{arm}/{model}: C1 lane registration is missing"
                        )
                else:
                    if (
                        scheduler.get("lane_policy")
                        != "global_resource_ready_round_robin_v2"
                    ):
                        issues.append(
                            f"{arm}/{model}: C1 ready scheduler policy is invalid"
                        )
                    registrations = scheduler.get("task_registration")
                    dispatches = scheduler.get("dispatch_history")
                    worker_limit = scheduler.get("worker_limit")
                    if (
                        worker_limit is not None
                        and worker_limit != effective
                    ):
                        issues.append(
                            f"{arm}/{model}: scheduler worker limit differs from effective workers"
                        )
                    active_tasks = _integer(scheduler.get("active_tasks"))
                    peak_tasks = _integer(scheduler.get("max_active_tasks"))
                    if active_tasks is not None or peak_tasks is not None:
                        if (
                            active_tasks is None
                            or peak_tasks is None
                            or effective is None
                            or not 0 <= active_tasks <= peak_tasks <= effective
                        ):
                            issues.append(
                                f"{arm}/{model}: invocation active-task counts are invalid"
                            )
                    if not isinstance(registrations, list):
                        issues.append(
                            f"{arm}/{model}: C1 task registration is missing"
                        )
                    elif len(
                        {
                            str(row.get("task_id"))
                            for row in registrations
                            if isinstance(row, dict)
                        }
                    ) != len(registrations):
                        issues.append(
                            f"{arm}/{model}: C1 task registration is not unique"
                        )
                    if not isinstance(dispatches, list):
                        issues.append(
                            f"{arm}/{model}: C1 dispatch history is missing"
                        )
                    else:
                        registration_by_task = {
                            str(row.get("task_id")): row
                            for row in registrations or []
                            if isinstance(row, dict)
                        }
                        seen_dispatch_tasks: set[str] = set()
                        for dispatch in dispatches:
                            lane_id = (
                                _integer(dispatch.get("lane_id"))
                                if isinstance(dispatch, dict)
                                else None
                            )
                            if (
                                lane_id is None
                                or effective is None
                                or not 0 <= lane_id < effective
                            ):
                                issues.append(
                                    f"{arm}/{model}: C1 dispatch lane is invalid"
                                )
                                break
                            task_id = str(dispatch.get("task_id") or "")
                            if not task_id or task_id in seen_dispatch_tasks:
                                issues.append(
                                    f"{arm}/{model}: C1 dispatch tickets are not unique"
                                )
                                break
                            seen_dispatch_tasks.add(task_id)
                            registration = registration_by_task.get(task_id)
                            if not isinstance(registration, dict):
                                issues.append(
                                    f"{arm}/{model}: C1 dispatch is not registered"
                                )
                                break
                            started_ns = _integer(
                                dispatch.get("admitted_monotonic_ns")
                            )
                            completed_ns = _integer(
                                dispatch.get("completed_monotonic_ns")
                            )
                            if (
                                dispatch.get("status") == "complete"
                                and (
                                    started_ns is None
                                    or completed_ns is None
                                    or completed_ns < started_ns
                                )
                            ):
                                issues.append(
                                    f"{arm}/{model}: C1 resource interval is invalid"
                                )
                                break
                            if started_ns is not None and completed_ns is not None:
                                for resource in registration.get("resources") or []:
                                    if not isinstance(resource, dict):
                                        continue
                                    resource_key = (
                                        str(resource.get("resource_kind") or ""),
                                        str(resource.get("resource_sha256") or ""),
                                    )
                                    resource_intervals.setdefault(
                                        resource_key, []
                                    ).append(
                                        (
                                            started_ns,
                                            completed_ns,
                                            f"{arm}/{model}/{task_id}",
                                        )
                                    )
        schema_version = int(report.get("schema_version", 0) or 0)
        supported_report_schemas = (
            ({4} if arm == "c1_state" else {5})
            if formal_report_schema
            else ({3, 4} if arm == "c1_state" else {4, 5})
        )
        if schema_version not in supported_report_schemas:
            issues.append(
                f"{arm}/{model}: unsupported C1 report schema_version "
                f"{schema_version}; expected one of "
                f"{sorted(supported_report_schemas)}"
            )
        if arm in {"c1_agent_s", "c1_agent_s_synth"}:
            policy = report.get("policy_runtime_metadata")
            required_policy = {
                "protocol_version": 2,
                "execution_backend": "background_pixel_v1",
                "coordinate_space": "frame_pixels_top_left_v1",
                "raw_screenshot_observation": True,
                "action_execution_owner": "parent_runtime",
                "foreground_required": False,
                "local_code_enabled": False,
                "native_exec_enabled": False,
            }
            if not isinstance(policy, dict):
                issues.append(f"{arm}/{model}: background policy provenance missing")
            else:
                for field, expected in required_policy.items():
                    if policy.get(field) != expected:
                        issues.append(
                            f"{arm}/{model}: background policy {field} does not match protocol"
                        )
        if report.get("complete") is not True:
            issues.append(f"{arm}/{model}/seed{_report_seed(report, {})}: report incomplete")
        results = report.get("results")
        if not isinstance(results, list):
            issues.append(f"{arm}/{model}: missing results list")
            continue
        for result in results:
            if not isinstance(result, dict):
                issues.append(f"{arm}/{model}: malformed result")
                continue
            trial, trial_issues = _normalize_c1_trial(arm, report, result)
            key = (trial["task_id"], trial["seed"])
            invocation_seed = _report_seed(report, {})
            if invocation_seed >= 0 and trial["seed"] != invocation_seed:
                trial_issues.append(
                    f"trial seed {trial['seed']} does not match invocation seed {invocation_seed}"
                )
                trial["complete"] = False
            if result.get("arm") not in {None, arm}:
                trial_issues.append(f"trial arm {result.get('arm')} does not match {arm}")
                trial["complete"] = False
            if result.get("incomplete") is True:
                trial_issues.append("trial marked incomplete")
                trial["complete"] = False
            index = by_model.setdefault(model, {})
            if key in index:
                issues.append(f"{arm}/{model}/{key[0]}/seed{key[1]}: duplicate trial")
                trial["complete"] = False
            else:
                index[key] = trial
            if trial_issues:
                issues.append(f"{arm}/{model}/{key[0]}/seed{key[1]}: " + "; ".join(trial_issues))
            trials.append(trial)
    for resource, intervals in resource_intervals.items():
        ordered_intervals = sorted(intervals)
        for previous, current in zip(
            ordered_intervals, ordered_intervals[1:]
        ):
            if current[0] < previous[1]:
                issues.append(
                    "C1 exclusive resource intervals overlap for "
                    f"{resource[0]}/{resource[1]}: "
                    f"{previous[2]} and {current[2]}"
                )
    return by_model, trials, issues


def _sum_nested_token_rows(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    totals: dict[str, int] = {}

    def add(prefix: str, value: Any) -> None:
        if isinstance(value, dict) and "total_tokens" not in value:
            for child_key, child_value in value.items():
                add(f"{prefix}/{child_key}" if prefix else str(child_key), child_value)
            return
        amount = value.get("total_tokens", 0) if isinstance(value, dict) else value
        try:
            totals[prefix] = totals.get(prefix, 0) + int(amount or 0)
        except (TypeError, ValueError):
            return

    for row in rows:
        mapping = row.get(field)
        if not isinstance(mapping, dict):
            continue
        for key, value in mapping.items():
            add(str(key), value)
    return totals


def _sum_nested_cost_rows(
    rows: list[dict[str, Any]], field: str
) -> dict[str, dict[str, Any]]:
    """Flatten and sum complete cost buckets while preserving token subfields."""

    totals: dict[str, dict[str, Any]] = {}

    def add(prefix: str, value: Any) -> None:
        if not isinstance(value, dict):
            return
        if "calls" not in value and "total_tokens" not in value:
            for child_key, child_value in value.items():
                add(f"{prefix}/{child_key}" if prefix else str(child_key), child_value)
            return
        bucket = totals.setdefault(
            prefix,
            {
                name: 0
                for name in _COST_PARTITION_FIELDS
            },
        )
        for name in _COST_PARTITION_FIELDS:
            bucket[name] += _integer(value.get(name)) or 0
        bucket["usage_complete"] = bool(
            bucket.get("usage_complete", True)
            and value.get("usage_complete") is True
        )

    for row in rows:
        mapping = row.get(field)
        if not isinstance(mapping, dict):
            continue
        for key, value in mapping.items():
            add(str(key), value)
    for bucket in totals.values():
        _finalize_reasoning_cost_bucket(bucket)
    return totals


def _finalize_reasoning_cost_bucket(bucket: dict[str, Any]) -> None:
    observed = _integer(bucket.get("reasoning_tokens_observed")) or 0
    missing = _integer(bucket.get("reasoning_detail_missing_calls")) or 0
    unknown = (
        _integer(bucket.get("reasoning_detail_unknown_completion_tokens")) or 0
    )
    expected = (
        _integer(bucket.get("reasoning_detail_expected_completion_tokens")) or 0
    )
    fraction = unknown / expected if expected > 0 else 0.0
    bucket["reasoning_tokens"] = None if missing else observed
    bucket["reasoning_tokens_observed"] = observed
    bucket["reasoning_tokens_lower_bound"] = observed
    bucket["reasoning_tokens_upper_bound"] = observed + unknown
    bucket["reasoning_detail_complete"] = missing == 0
    bucket["reasoning_detail_unknown_fraction"] = fraction
    bucket["reasoning_detail_tolerance_fraction"] = (
        cost.REASONING_DETAIL_TOLERANCE_FRACTION
    )
    bucket["reasoning_detail_within_tolerance"] = (
        fraction <= cost.REASONING_DETAIL_TOLERANCE_FRACTION
    )


def _sum_cost_summary_field(
    rows: list[dict[str, Any]], field: str
) -> dict[str, Any]:
    """Sum complete cost summaries used only for synthesis accounting diagnostics."""

    summaries = [row.get(field) for row in rows if isinstance(row.get(field), dict)]
    top_fields = tuple(dict.fromkeys(_COST_PARTITION_FIELDS.values()))
    result: dict[str, Any] = {
        name: sum(_integer(summary.get(name)) or 0 for summary in summaries)
        for name in top_fields
    }
    result["summaries"] = len(summaries)
    result["usage_complete"] = all(
        summary.get("usage_complete") is True for summary in summaries
    )
    _finalize_reasoning_cost_bucket(result)
    usd_values = [_number(summary.get("est_usd")) for summary in summaries]
    result["est_usd"] = (
        sum(value for value in usd_values if value is not None)
        if summaries and all(value is not None for value in usd_values)
        else None
    )
    for partition in ("by_kind", "by_model", "by_kind_model"):
        result[partition] = _sum_nested_cost_rows(
            [{partition: summary.get(partition)} for summary in summaries],
            partition,
        )
    return result


def _trial_token_split(row: dict[str, Any]) -> tuple[int, int]:
    """Return (agent, evaluation) tokens without double-counting reasoning.

    ``total_tokens`` already contains reasoning tokens.  The visual judge and
    grader buckets are evaluation; observer, policy, reflection, grounding and
    synthesis remain part of the tested agent/method cost.
    """

    per_kind = _sum_nested_token_rows([row], "by_kind")
    evaluation = sum(
        amount
        for kind, amount in per_kind.items()
        if kind == "visual_judge" or kind.startswith("grader")
    )
    total = _integer(row.get("total_tokens"))
    if total is None:
        total = sum(per_kind.values())
    return max(0, total - evaluation), evaluation


_C1_COST_KINDS = {
    "checkpoint_exploration",
    "checkpoint_synthesis",
    "visual_observer",
    "gui_policy",
    "agent_s_worker",
    "agent_s_reflection",
    "agent_s_grounding",
    "visual_judge",
    "source_action_exploration",
    "source_action_synthesis",
    "source_action_format_repair",
}


def _visual_evidence_without_llm_judge(trial: Mapping[str, Any]) -> bool:
    verdict = trial.get("pair_verdict")
    if not isinstance(verdict, Mapping):
        return False
    evaluated = [
        half
        for half in (verdict.get("before"), verdict.get("after"))
        if isinstance(half, Mapping)
        and half.get("label") in {"correct", "incorrect"}
    ]
    if not evaluated:
        return False
    return all(
        half.get("verdict_source") == "normalized_image_equality_v1"
        or (
            half is verdict.get("before")
            and trial.get("pair_start_gate_cache_hit") is True
        )
        for half in evaluated
    )


def _c1_cost_module_issues(
    trial: dict[str, Any],
    *,
    expected_observer_model: str | None,
    expected_grounder_model: str | None,
    expected_judge_model: str | None,
) -> list[str]:
    """Validate that the formal ledger names the actual ablatable modules."""

    arm = str(trial.get("arm"))
    state_arm = arm in {"c1_state", "c1_state_minimal_agent"}
    label = f"{arm}/{trial.get('model')}/{trial.get('task_id')}/seed{trial.get('seed')}"
    by_kind = trial.get("by_kind") if isinstance(trial.get("by_kind"), dict) else {}
    kinds = set(by_kind)
    def active(kind: str) -> bool:
        bucket = by_kind.get(kind)
        return bool(isinstance(bucket, dict) and (_integer(bucket.get("calls")) or 0) > 0)

    issues = [
        f"{label}: unregistered C1 cost kind {kind}"
        for kind in sorted(kinds - _C1_COST_KINDS)
    ]
    calls = _integer(trial.get("llm_calls")) or 0
    if calls and not kinds:
        issues.append(f"{label}: nonzero cost has no module breakdown")
    if not state_arm and trial.get("runtime_started") is True:
        if trial.get("policy_phase_started") is True and not active("visual_observer"):
            issues.append(f"{label}: policy phase has no visual_observer cost bucket")
        steps = trial.get("steps") if isinstance(trial.get("steps"), dict) else {}
        if (_integer(steps.get("total")) or 0) > 0:
            policy_kinds = (
                {"gui_policy"}
                if arm in {"c1_gui", "c1_gui_synth"}
                else {"agent_s_worker", "agent_s_reflection"}
            )
            if not any(active(kind) for kind in policy_kinds):
                issues.append(f"{label}: policy steps have no policy cost bucket")
        if trial.get("visual_grading_started") is True and not active("visual_judge"):
            issues.append(f"{label}: visual grading has no visual_judge cost bucket")
    if not state_arm:
        policy_cost_kinds = {
            "visual_observer",
            "gui_policy",
            "agent_s_worker",
            "agent_s_reflection",
            "agent_s_grounding",
        }
        if trial.get("policy_phase_started") is False and any(
            active(kind) for kind in policy_cost_kinds
        ):
            issues.append(f"{label}: policy-not-started trial contains policy/observer spend")
        if trial.get("visual_grading_started") is False and active("visual_judge"):
            issues.append(f"{label}: visual-grading-not-started trial contains judge spend")
    if arm in {"c1_gui", "c1_agent_s"} and kinds & {
        "checkpoint_exploration", "checkpoint_synthesis"
    }:
        issues.append(f"{label}: from-start trial contains checkpoint cost modules")
    if arm in {"c1_gui_synth", "c1_agent_s_synth"}:
        attributed = trial.get("synthesis_attributed_cost")
        if isinstance(attributed, dict) and not _cost_is_zero(attributed):
            if not any(
                active(kind)
                for kind in {"checkpoint_exploration", "checkpoint_synthesis"}
            ):
                issues.append(f"{label}: attributed synthesis is absent from trial cost modules")
            checkpoint_buckets = [
                by_kind[kind]
                for kind in ("checkpoint_exploration", "checkpoint_synthesis")
                if isinstance(by_kind.get(kind), dict)
            ]
            checkpoint_signature = tuple(
                sum(_integer(bucket.get(bucket_field)) or 0 for bucket in checkpoint_buckets)
                for bucket_field in _COST_PARTITION_FIELDS
            )
            if checkpoint_signature != _cost_signature(attributed):
                issues.append(
                    f"{label}: checkpoint module costs do not equal synthesis attribution"
                )
    if state_arm and calls:
        if not any(
            active(kind)
            for kind in {"checkpoint_exploration", "checkpoint_synthesis"}
        ):
            issues.append(f"{label}: state-managed cost lacks checkpoint module")
    if (
        state_arm
        and trial.get("visual_grading_started") is True
        and not active("visual_judge")
        and not _visual_evidence_without_llm_judge(trial)
    ):
        issues.append(f"{label}: state-managed cost lacks visual_judge module")
    if (
        state_arm
        and trial.get("visual_grading_started") is False
        and active("visual_judge")
    ):
        issues.append(f"{label}: state visual grading did not start but judge spend exists")
    if state_arm:
        allowed_state_kinds = {
            "checkpoint_exploration",
            "checkpoint_synthesis",
            "visual_judge",
            "source_action_exploration",
            "source_action_synthesis",
            "source_action_format_repair",
        }
        unexpected_active = sorted(
            kind for kind in kinds - allowed_state_kinds if active(kind)
        )
        if unexpected_active:
            issues.append(
                f"{label}: state-managed trial contains GUI module spend "
                + ",".join(unexpected_active)
            )

    tested_model = _clean_model(trial.get("model"))
    grounding_model = (
        tested_model
        if grounder_follows_decision_model(expected_grounder_model)
        else _clean_model(expected_grounder_model)
    )
    expected_by_kind = {
        "checkpoint_exploration": tested_model,
        "checkpoint_synthesis": tested_model,
        "gui_policy": tested_model,
        "agent_s_worker": tested_model,
        "agent_s_reflection": tested_model,
        "visual_observer": _clean_model(expected_observer_model),
        "agent_s_grounding": grounding_model,
        "visual_judge": _clean_model(expected_judge_model),
        "source_action_exploration": tested_model,
        "source_action_synthesis": tested_model,
        "source_action_format_repair": tested_model,
    }
    by_kind_model = (
        trial.get("by_kind_model")
        if isinstance(trial.get("by_kind_model"), dict)
        else {}
    )
    for kind, model_buckets in by_kind_model.items():
        if not isinstance(model_buckets, dict):
            continue
        expected_model = expected_by_kind.get(str(kind))
        actual_models = {
            _clean_model(model)
            for model, bucket in model_buckets.items()
            if isinstance(bucket, dict) and (_integer(bucket.get("calls")) or 0) > 0
        }
        actual_models.discard(None)
        if expected_model is None or actual_models != {expected_model}:
            issues.append(
                f"{label}: {kind} cost model routing {sorted(actual_models)} "
                f"does not match {expected_model}"
            )
    return issues


def _c1_public_semantic_trial_issues(trial: dict[str, Any]) -> list[str]:
    """Fail formal Love/SpaceRPG GUI trials closed on private-grader asymmetry."""

    arm = str(trial.get("arm") or "")
    benchmark = str(trial.get("benchmark") or "").strip().lower()
    if arm == "c1_state" or benchmark not in {"datingsim", "spacerpg"}:
        return []
    if _is_valid_synthesized_fair_zero(trial):
        return []
    prefix = f"{arm}/{trial.get('model')}/{trial.get('task_id')}/seed{trial.get('seed')}"
    expected_source = (
        "love_production_save_roundtrip_v1"
        if benchmark == "datingsim"
        else "spacerpg_production_dom_save_v1"
    )
    issues: list[str] = []
    if trial.get("semantic_probe_source") != expected_source:
        issues.append(
            f"{prefix}: semantic_probe_source must be {expected_source}"
        )
    if trial.get("policy_action_interface") != "pixel_mouse_keyboard_only":
        issues.append(f"{prefix}: policy action interface is not pixel-only")
    if trial.get("policy_received_semantic_probe") is not False:
        issues.append(f"{prefix}: semantic probe leakage attestation missing or failed")
    goal_evaluated = trial.get("goal_evaluation_status") in {None, "evaluated"}
    semantic_pairs = [
        ("before_semantic_detail", "before_semantic_ok"),
    ]
    if goal_evaluated:
        semantic_pairs.append(("after_semantic_detail", "after_semantic_ok"))
    for field, semantic_field in semantic_pairs:
        detail = trial.get(field)
        if not isinstance(detail, dict):
            issues.append(f"{prefix}: {field} is missing public semantic evidence")
            continue
        if detail.get("source") != expected_source:
            issues.append(f"{prefix}: {field} has the wrong public semantic source")
        checks = detail.get("checks")
        if not isinstance(checks, list) or not checks:
            issues.append(f"{prefix}: {field}.checks must be a non-empty list")
        elif any(
            not isinstance(check, dict) or type(check.get("ok")) is not bool
            for check in checks
        ):
            issues.append(f"{prefix}: {field}.checks contain invalid ok evidence")
        else:
            checks_ok = all(check["ok"] for check in checks)
            if trial.get(semantic_field) is not checks_ok:
                issues.append(
                    f"{prefix}: {semantic_field} does not match all({field}.checks.ok)"
                )
        if not isinstance(detail.get("network"), dict):
            issues.append(f"{prefix}: {field}.network evidence is missing")
        if not isinstance(detail.get("errors"), dict):
            issues.append(f"{prefix}: {field}.errors evidence is missing")
        if field == "after_semantic_detail":
            trace = detail.get("transition_trace")
            if not isinstance(trace, dict):
                issues.append(f"{prefix}: {field}.transition_trace is missing")
            else:
                if trace.get("errors") != []:
                    issues.append(
                        f"{prefix}: {field}.transition_trace contains instrumentation errors"
                    )
                for trace_field in ("screens", "events"):
                    if not isinstance(trace.get(trace_field), list):
                        issues.append(
                            f"{prefix}: {field}.transition_trace.{trace_field} "
                            "evidence is missing"
                        )
    if arm in {"c1_gui", "c1_agent_s"}:
        attestation = trial.get("policy_runtime_attestation")
        expected_attestation = (
            {
                "required": True,
                "passed": True,
                "entry_surface": "public_ai_query_mock",
                "private_hook_present": False,
                "ai_query_mode": "mocked",
                "ai_storage_mode": None,
            }
            if benchmark == "datingsim"
            else {
                "required": True,
                "passed": True,
                "entry_surface": "public_offline_economy",
                "private_hook_present": False,
                "economy_backend_disabled": True,
                "hook_storage_enabled": False,
            }
        )
        if not isinstance(attestation, dict):
            issues.append(f"{prefix}: from-start private-hook attestation is missing")
        else:
            for field, expected in expected_attestation.items():
                if field not in attestation or attestation[field] != expected:
                    issues.append(
                        f"{prefix}: policy_runtime_attestation.{field} must be "
                        f"{expected!r}"
                    )
    semantic_detail_fields = ["before_semantic_detail"]
    if goal_evaluated:
        semantic_detail_fields.append("after_semantic_detail")
    if benchmark == "datingsim":
        for field in semantic_detail_fields:
            detail = trial.get(field)
            snapshot = detail.get("snapshot") if isinstance(detail, dict) else None
            if not isinstance(snapshot, dict) or snapshot.get("prior_restored") is not True:
                issues.append(f"{prefix}: {field} production-save roundtrip was not restored")
    else:
        for field in semantic_detail_fields:
            detail = trial.get(field)
            snapshot = detail.get("snapshot") if isinstance(detail, dict) else None
            if not isinstance(snapshot, dict):
                issues.append(f"{prefix}: {field}.snapshot evidence is missing")
                continue
            if not isinstance(snapshot.get("dom"), dict):
                issues.append(f"{prefix}: {field}.snapshot.dom evidence is missing")
            if not isinstance(snapshot.get("storage"), dict):
                issues.append(f"{prefix}: {field}.snapshot.storage evidence is missing")
    return issues


def _c1_openra_semantic_trial_issues(trial: dict[str, Any]) -> list[str]:
    """Fail formal OpenRA GUI trials closed on bounded live evidence."""

    arm = str(trial.get("arm") or "")
    normalized = (
        str(trial.get("benchmark") or "")
        .strip()
        .lower()
        .replace("-", "")
        .replace("_", "")
    )
    if arm == "c1_state" or normalized != "openra":
        return []
    if _is_valid_synthesized_fair_zero(trial):
        return []
    prefix = (
        f"{arm}/{trial.get('model')}/{trial.get('task_id')}"
        f"/seed{trial.get('seed')}"
    )
    issues: list[str] = []
    task_id = str(trial.get("task_id") or "")
    adapter, before_contract = _c1_expected_semantic_checks(task_id, "before")
    expected_source = adapter or ""
    gameplay_contract = adapter == "openra_live_gameplay_probe_v1"
    settings_contract = adapter == "openra_public_settings_yaml"
    if not (gameplay_contract or settings_contract) or before_contract is None:
        issues.append(f"{prefix}: OpenRA task id has no registered semantic contract")
    if trial.get("semantic_probe_source") != expected_source:
        issues.append(f"{prefix}: OpenRA semantic_probe_source is invalid")
    if trial.get("policy_action_interface") != "pixel_mouse_keyboard_only":
        issues.append(f"{prefix}: OpenRA policy action interface is not pixel-only")
    if trial.get("policy_received_semantic_probe") is not False:
        issues.append(f"{prefix}: OpenRA semantic probe leakage attestation failed")
    if trial.get("policy_observation_source") != "application_framebuffer_pixels":
        issues.append(f"{prefix}: OpenRA policy did not observe application framebuffer pixels")
    if trial.get("scripted_transition_executed") is not False:
        issues.append(f"{prefix}: OpenRA scripted transition attestation failed")

    if gameplay_contract:
        pairs = [
            (
                "before_semantic_detail",
                "before_semantic_ok",
                "before_semantic_status",
                "before",
            )
        ]
        if trial.get("goal_evaluation_status") == "evaluated":
            pairs.append(
                (
                    "after_semantic_detail",
                    "after_semantic_ok",
                    "after_semantic_status",
                    "after",
                )
            )
        for detail_field, outcome_field, status_field, stage in pairs:
            detail = trial.get(detail_field)
            if not isinstance(detail, dict):
                issues.append(
                    f"{prefix}: {detail_field} is missing OpenRA gameplay evidence"
                )
                continue
            if set(detail) != {
                "source",
                "live",
                "process_alive",
                "report_path",
                "sequence",
                "checks",
                "errors",
                "world_tick",
            }:
                issues.append(
                    f"{prefix}: {detail_field} has invalid OpenRA gameplay fields"
                )
            if detail.get("source") != expected_source:
                issues.append(
                    f"{prefix}: {detail_field} has the wrong OpenRA gameplay source"
                )
            if detail.get("live") is not True or detail.get("process_alive") is not True:
                issues.append(
                    f"{prefix}: {detail_field} was not read from a live gameplay process"
                )
            report_path = detail.get("report_path")
            if (
                not isinstance(report_path, str)
                or not Path(report_path).is_absolute()
                or Path(report_path).name != "openra-live-gameplay-report.json"
            ):
                issues.append(f"{prefix}: {detail_field}.report_path is malformed")
            if type(detail.get("sequence")) is not int or detail["sequence"] < 1:
                issues.append(f"{prefix}: {detail_field}.sequence is malformed")
            if type(detail.get("world_tick")) is not int or detail["world_tick"] < 0:
                issues.append(f"{prefix}: {detail_field}.world_tick is malformed")
            checks = detail.get("checks")
            well_formed = bool(
                isinstance(checks, list)
                and checks
                and all(
                    isinstance(check, dict)
                    and isinstance(check.get("name"), str)
                    and check.get("status") in {"success", "failure"}
                    for check in checks
                )
            )
            if not well_formed:
                issues.append(
                    f"{prefix}: {detail_field}.checks are malformed or empty"
                )
                continue
            names = tuple(check["name"] for check in checks)
            stage_adapter, declared_checks = _c1_expected_semantic_checks(
                task_id, stage
            )
            expected_names = (
                tuple(str(item.get("name") or "") for item in declared_checks)
                if stage_adapter == expected_source
                and isinstance(declared_checks, list)
                else None
            )
            if names != expected_names:
                issues.append(
                    f"{prefix}: {detail_field}.checks do not match the registered task"
                )
            checks_ok = all(check["status"] == "success" for check in checks)
            if trial.get(outcome_field) is not checks_ok:
                issues.append(
                    f"{prefix}: {outcome_field} disagrees with {detail_field}.checks"
                )
            expected_status = "success" if checks_ok else "gameplay_mismatch"
            if trial.get(status_field) != expected_status:
                issues.append(
                    f"{prefix}: {status_field} disagrees with {detail_field}.checks"
                )
            errors = detail.get("errors")
            if not isinstance(errors, list) or (checks_ok and errors):
                issues.append(
                    f"{prefix}: {detail_field}.errors disagrees with gameplay checks"
                )
            seed_evidence = trial.get("seed_evidence")
            snapshot = (
                seed_evidence.get("initial" if stage == "before" else "final")
                if isinstance(seed_evidence, dict)
                else None
            )
            fingerprint = (
                snapshot.get("semantic_fingerprint")
                if isinstance(snapshot, dict)
                else None
            )
            if (
                not isinstance(fingerprint, dict)
                or fingerprint.get("source") != expected_source
                or not _same_json_value(fingerprint.get("checks"), checks)
            ):
                issues.append(
                    f"{prefix}: {detail_field} disagrees with live gameplay seed snapshot"
                )
        return issues

    pairs = [
        (
            "before_semantic_detail",
            "before_semantic_ok",
            "before_semantic_status",
            "before",
        )
    ]
    if trial.get("goal_evaluation_status") == "evaluated":
        pairs.append(
            (
                "after_semantic_detail",
                "after_semantic_ok",
                "after_semantic_status",
                "after",
            )
        )
    for detail_field, outcome_field, status_field, stage in pairs:
        detail = trial.get(detail_field)
        if not isinstance(detail, dict):
            issues.append(f"{prefix}: {detail_field} is missing OpenRA semantic evidence")
            continue
        if set(detail) != {
            "source",
            "live",
            "process_alive",
            "settings_path",
            "checks",
            "mismatches",
        }:
            issues.append(f"{prefix}: {detail_field} has invalid OpenRA fields")
        if detail.get("source") != expected_source:
            issues.append(f"{prefix}: {detail_field} has the wrong OpenRA source")
        if detail.get("live") is not True or detail.get("process_alive") is not True:
            issues.append(f"{prefix}: {detail_field} was not read from a live process")
        settings_path = detail.get("settings_path")
        if (
            not isinstance(settings_path, str)
            or not Path(settings_path).is_absolute()
            or Path(settings_path).name != "settings.yaml"
        ):
            issues.append(f"{prefix}: {detail_field}.settings_path is malformed")
        checks = detail.get("checks")
        checks_well_formed = bool(
            isinstance(checks, list)
            and checks
            and all(
                isinstance(check, dict)
                and isinstance(check.get("section"), str)
                and bool(check.get("section"))
                and isinstance(check.get("key"), str)
                and bool(check.get("key"))
                and "expected" in check
                and "actual" in check
                and type(check.get("ok")) is bool
                for check in checks
            )
        )
        if not checks_well_formed:
            issues.append(f"{prefix}: {detail_field}.checks are malformed or empty")
            continue
        check_keys = [(check["section"], check["key"]) for check in checks]
        if len(check_keys) != len(set(check_keys)):
            issues.append(f"{prefix}: {detail_field}.checks contain duplicate settings")
        stage_adapter, declared_checks = _c1_expected_semantic_checks(task_id, stage)
        expected_checks = (
            {
                (str(item.get("section") or ""), str(item.get("key") or "")): item.get("expected")
                for item in declared_checks
            }
            if stage_adapter == expected_source and isinstance(declared_checks, list)
            else None
        )
        actual_contract = {
            (check["section"], check["key"]): check["expected"]
            for check in checks
        }
        contract_matches = bool(
            isinstance(expected_checks, dict)
            and set(actual_contract) == set(expected_checks)
            and all(
                _same_json_value(actual_contract[key], expected_checks[key])
                for key in actual_contract
            )
        )
        if not contract_matches:
            issues.append(
                f"{prefix}: {detail_field}.checks do not match the registered task"
            )
        for check in checks:
            if check["ok"] is not _same_json_value(
                check["actual"], check["expected"]
            ):
                issues.append(
                    f"{prefix}: {detail_field} check ok disagrees with actual/expected"
                )
                break
        expected_mismatches = [
            {
                "section": check["section"],
                "key": check["key"],
                "expected": check["expected"],
                "actual": check["actual"],
            }
            for check in checks
            if not check["ok"]
        ]
        mismatches = detail.get("mismatches")
        if not isinstance(mismatches, list) or not _same_json_value(
            mismatches, expected_mismatches
        ):
            issues.append(
                f"{prefix}: {detail_field}.mismatches disagrees with failed checks"
            )
        checks_ok = all(check["ok"] for check in checks)
        if trial.get(outcome_field) is not checks_ok:
            issues.append(
                f"{prefix}: {outcome_field} disagrees with {detail_field}.checks"
            )
        expected_status = "success" if checks_ok else "settings_mismatch"
        if trial.get(status_field) != expected_status:
            issues.append(
                f"{prefix}: {status_field} disagrees with {detail_field}.checks"
            )

        seed_evidence = trial.get("seed_evidence")
        snapshot = (
            seed_evidence.get("initial" if stage == "before" else "final")
            if isinstance(seed_evidence, dict)
            else None
        )
        fingerprint = (
            snapshot.get("semantic_fingerprint")
            if isinstance(snapshot, dict)
            else None
        )
        if not isinstance(fingerprint, dict) or not fingerprint:
            issues.append(
                f"{prefix}: {detail_field} lacks its task-bound live settings snapshot"
            )
        else:
            for check in checks:
                section = fingerprint.get(check["section"])
                if (
                    not isinstance(section, dict)
                    or check["key"] not in section
                    or not _same_json_value(
                        section[check["key"]], check["actual"]
                    )
                ):
                    issues.append(
                        f"{prefix}: {detail_field} disagrees with live seed snapshot"
                    )
                    break
    return issues


def _c1_unity_transition_evidence_issues(
    evidence: Any,
    *,
    benchmark: str,
    task_id: str,
    stage: str,
    checks: list[dict[str, Any]],
) -> list[str]:
    requirement = _c1_save_load_requirement(task_id)
    expected_slot = (
        requirement.get("slot") if isinstance(requirement, dict) else None
    )
    ordered_events = (
        requirement.get("ordered_events") if isinstance(requirement, dict) else None
    )
    if not isinstance(evidence, dict):
        return [f"Unity {stage} transition evidence is missing"]
    issues: list[str] = []
    expected_fields = {
        "required",
        "kind",
        "slot",
        "source",
        "armed_sequence",
        "observed_sequence",
        "save_observed",
        "load_observed",
        "save_frame",
        "load_frame",
        "ordered",
    }
    if benchmark == "pal3":
        expected_fields.update(
            {
                "reset_observed",
                "scene_load_observed",
                "reset_frame",
                "scene_load_frame",
            }
        )
    if set(evidence) != expected_fields:
        issues.append(f"Unity {stage} transition evidence has invalid fields")

    def strict_int(value: Any) -> int | None:
        return value if type(value) is int else None

    required = requirement is not None
    if evidence.get("required") is not required:
        issues.append(f"Unity {stage} transition required flag is incorrect")
    expected_source = f"{benchmark}_public_ui_save_load_witness_v1"
    if evidence.get("source") != expected_source:
        issues.append(f"Unity {stage} transition evidence has the wrong source")
    if not required:
        if (
            evidence.get("kind") not in {"", None}
            or strict_int(evidence.get("slot")) != -1
        ):
            issues.append(f"Unity {stage} non-save task armed a transition witness")
        if strict_int(evidence.get("armed_sequence")) != -1:
            issues.append(
                f"Unity {stage} non-save transition witness has an armed sequence"
            )
        observed = strict_int(evidence.get("observed_sequence"))
        if observed is None or observed < 0:
            issues.append(
                f"Unity {stage} non-save transition observation sequence is malformed"
            )
        inactive_fields = {
            "save_observed": False,
            "load_observed": False,
            "save_frame": -1,
            "load_frame": -1,
            "ordered": False,
        }
        if benchmark == "pal3":
            inactive_fields.update(
                {
                    "reset_observed": False,
                    "scene_load_observed": False,
                    "reset_frame": -1,
                    "scene_load_frame": -1,
                }
            )
        if any(
            (
                evidence.get(field) is not expected
                if type(expected) is bool
                else strict_int(evidence.get(field)) != expected
            )
            for field, expected in inactive_fields.items()
        ):
            issues.append(
                f"Unity {stage} non-save transition witness contains transition events"
            )
        if any(
            isinstance(check, dict)
            and str(check.get("name") or "").startswith("c1.transition.")
            for check in checks
        ):
            issues.append(
                f"Unity {stage} non-save semantic checks contain transition claims"
            )
        return issues
    if (
        evidence.get("kind") != "save_then_load"
        or strict_int(evidence.get("slot")) != expected_slot
    ):
        issues.append(f"Unity {stage} transition evidence is bound to the wrong slot")
    armed = strict_int(evidence.get("armed_sequence"))
    observed = strict_int(evidence.get("observed_sequence"))
    if armed is None or armed < 0 or observed is None or observed < armed:
        issues.append(f"Unity {stage} transition sequence evidence is malformed")
    elif stage == "before" and observed != armed:
        issues.append("Unity before transition witness was not freshly armed")
    elif stage == "after" and observed <= armed:
        issues.append("Unity after transition witness does not follow semantic_start")

    save_observed = evidence.get("save_observed")
    load_observed = evidence.get("load_observed")
    save_frame = strict_int(evidence.get("save_frame"))
    load_frame = strict_int(evidence.get("load_frame"))
    if type(save_observed) is not bool or type(load_observed) is not bool:
        issues.append(f"Unity {stage} transition event flags are malformed")
        return issues
    if (save_observed and (save_frame is None or save_frame < 0)) or (
        not save_observed and save_frame != -1
    ):
        issues.append(f"Unity {stage} save frame disagrees with save_observed")
    if (load_observed and (load_frame is None or load_frame < 0)) or (
        not load_observed and load_frame != -1
    ):
        issues.append(f"Unity {stage} load frame disagrees with load_observed")
    ordered = bool(
        save_observed
        and load_observed
        and save_frame is not None
        and load_frame is not None
        and load_frame >= save_frame
    )
    if ordered_events == ["save", "reset", "scene_load", "load"]:
        reset_observed = evidence.get("reset_observed")
        scene_load_observed = evidence.get("scene_load_observed")
        reset_frame = strict_int(evidence.get("reset_frame"))
        scene_load_frame = strict_int(evidence.get("scene_load_frame"))
        if type(reset_observed) is not bool or type(scene_load_observed) is not bool:
            issues.append(f"Unity {stage} PAL3 replay flags are malformed")
            return issues
        if (reset_observed and (reset_frame is None or reset_frame < 0)) or (
            not reset_observed and reset_frame != -1
        ):
            issues.append(f"Unity {stage} PAL3 reset frame is inconsistent")
        if (scene_load_observed and (scene_load_frame is None or scene_load_frame < 0)) or (
            not scene_load_observed and scene_load_frame != -1
        ):
            issues.append(f"Unity {stage} PAL3 scene replay frame is inconsistent")
        ordered = bool(
            ordered
            and reset_observed
            and scene_load_observed
            and reset_frame is not None
            and scene_load_frame is not None
            and reset_frame >= save_frame
            and scene_load_frame >= reset_frame
            and load_frame >= scene_load_frame
        )
    if evidence.get("ordered") is not ordered:
        issues.append(f"Unity {stage} ordered flag disagrees with transition events")
    statuses = {
        str(check.get("name")): check.get("status")
        for check in checks
        if isinstance(check, dict)
    }
    if stage == "before":
        if save_observed or load_observed or ordered:
            issues.append("Unity before transition witness contains pre-start events")
        if statuses.get("c1.transition.armed") != "success":
            issues.append("Unity before transition witness was not armed successfully")
    else:
        if statuses.get("c1.transition.expectation") != "success":
            issues.append("Unity goal transition expectation check failed")
        if statuses.get("c1.transition.save") != (
            "success" if save_observed else "failure"
        ):
            issues.append("Unity goal save check disagrees with transition evidence")
        if statuses.get("c1.transition.load") != (
            "success" if ordered else "failure"
        ):
            issues.append("Unity goal load check disagrees with transition evidence")
    return issues


def _c1_unity_semantic_trial_issues(trial: dict[str, Any]) -> list[str]:
    """Fail formal jynew/PAL3 GUI trials closed on live Unity evidence."""

    arm = str(trial.get("arm") or "")
    normalized = (
        str(trial.get("benchmark") or "")
        .strip()
        .lower()
        .replace("-", "")
        .replace("_", "")
    )
    benchmark = "pal3" if normalized == "pal3unity" else normalized
    if arm == "c1_state" or benchmark not in {"jynew", "pal3"}:
        return []
    if _is_valid_synthesized_fair_zero(trial):
        return []
    prefix = (
        f"{arm}/{trial.get('model')}/{trial.get('task_id')}"
        f"/seed{trial.get('seed')}"
    )
    issues: list[str] = []
    task_id = str(trial.get("task_id") or "")
    surface = _c1_formal_gui_surface(task_id)
    expected_adapter = f"{benchmark}_unity_live_v1"
    if surface is None:
        issues.append(f"{prefix}: Unity task id has no registered semantic contract")
    elif surface.get("adapter") != expected_adapter:
        issues.append(f"{prefix}: Unity task id is bound to the wrong benchmark")
    expected_source = f"{benchmark}_live_semantic_probe_v1"
    if trial.get("semantic_probe_source") != expected_source:
        issues.append(f"{prefix}: Unity semantic_probe_source is invalid")
    if trial.get("policy_action_interface") != "pixel_mouse_keyboard_only":
        issues.append(f"{prefix}: Unity policy action interface is not pixel-only")
    if trial.get("policy_received_semantic_probe") is not False:
        issues.append(f"{prefix}: Unity semantic probe leakage attestation failed")
    if trial.get("policy_observation_source") != "application_framebuffer_pixels":
        issues.append(f"{prefix}: Unity policy did not observe application framebuffer pixels")
    if trial.get("scripted_transition_executed") is not False:
        issues.append(f"{prefix}: Unity scripted transition attestation failed")

    pairs = [
        (
            "before_semantic_detail",
            "before_semantic_ok",
            "before_semantic_status",
            "before",
        )
    ]
    if trial.get("goal_evaluation_status") == "evaluated":
        pairs.append(
            (
                "after_semantic_detail",
                "after_semantic_ok",
                "after_semantic_status",
                "after",
            )
        )
    for detail_field, outcome_field, status_field, stage in pairs:
        detail = trial.get(detail_field)
        if not isinstance(detail, dict):
            issues.append(f"{prefix}: {detail_field} is missing Unity semantic evidence")
            continue
        if set(detail) != {
            "source",
            "live",
            "detail",
            "checks",
            "transition_evidence",
            "transition_evidence_valid",
        }:
            issues.append(f"{prefix}: {detail_field} has invalid Unity fields")
        if detail.get("source") != expected_source:
            issues.append(f"{prefix}: {detail_field} has the wrong Unity source")
        if detail.get("live") is not True:
            issues.append(f"{prefix}: {detail_field} is not a live Unity probe")
        if not isinstance(detail.get("detail"), str) or not detail.get("detail"):
            issues.append(f"{prefix}: {detail_field}.detail is missing")
        if "error" in detail:
            issues.append(f"{prefix}: {detail_field} contains validation errors")
        checks = detail.get("checks")
        checks_well_formed = bool(
            isinstance(checks, list)
            and checks
            and all(
                isinstance(check, dict)
                and set(check) == {"name", "status", "detail"}
                and isinstance(check.get("name"), str)
                and bool(check.get("name"))
                and check.get("status") in {"success", "failure"}
                and isinstance(check.get("detail"), str)
                and bool(check.get("detail"))
                for check in checks
            )
        )
        if not checks_well_formed:
            issues.append(f"{prefix}: {detail_field}.checks are malformed or empty")
            continue
        check_names = [check["name"] for check in checks]
        if len(check_names) != len(set(check_names)):
            issues.append(f"{prefix}: {detail_field}.checks contain duplicate names")
        stage_adapter, declared_checks = _c1_expected_semantic_checks(task_id, stage)
        expected_groups = (
            tuple(
                tuple(str(name) for name in item.get("any_of", []))
                for item in declared_checks
            )
            if stage_adapter == expected_adapter
            and isinstance(declared_checks, list)
            else None
        )
        if not isinstance(expected_groups, tuple):
            issues.append(
                f"{prefix}: {detail_field} has no registered task check shape"
            )
        else:
            shape_valid = bool(
                len(check_names) == len(expected_groups)
                and all(
                    sum(name in alternatives for name in check_names) == 1
                    for alternatives in expected_groups
                )
                and all(
                    sum(name in alternatives for alternatives in expected_groups)
                    == 1
                    for name in check_names
                )
            )
            transition_requirement = _c1_save_load_requirement(task_id)
            runtime_failure_names = {"c1.target.runtime"}
            if stage == "before" and transition_requirement is not None:
                runtime_failure_names.add("c1.transition.armed")
            runtime_failure_shape = bool(
                stage == "before"
                and set(check_names) == runtime_failure_names
                and any(
                    check["name"] == "c1.target.runtime"
                    and check["status"] == "failure"
                    for check in checks
                )
            )
            if not shape_valid and not runtime_failure_shape:
                issues.append(
                    f"{prefix}: {detail_field}.checks do not match the registered task"
                )
        checks_ok = all(check["status"] == "success" for check in checks)
        if trial.get(outcome_field) is not checks_ok:
            issues.append(f"{prefix}: {outcome_field} disagrees with Unity checks")
        expected_status = "success" if checks_ok else "failure"
        if trial.get(status_field) != expected_status:
            issues.append(f"{prefix}: {status_field} disagrees with Unity checks")
        if detail.get("transition_evidence_valid") is not True:
            issues.append(f"{prefix}: {detail_field} transition evidence was not validated")
        transition_evidence = detail.get("transition_evidence")
        for issue in _c1_unity_transition_evidence_issues(
            transition_evidence,
            benchmark=benchmark,
            task_id=task_id,
            stage=stage,
            checks=checks,
        ):
            issues.append(f"{prefix}: {issue}")

        seed_evidence = trial.get("seed_evidence")
        snapshot = (
            seed_evidence.get("initial" if stage == "before" else "final")
            if isinstance(seed_evidence, dict)
            else None
        )
        fingerprint = (
            snapshot.get("semantic_fingerprint")
            if isinstance(snapshot, dict)
            else None
        )
        expected_fingerprint = {
            "source": expected_source,
            "status": trial.get(status_field),
            "ok": trial.get(outcome_field),
            "checks": checks,
            "transition_evidence": transition_evidence,
            "transition_evidence_valid": detail.get("transition_evidence_valid"),
        }
        if fingerprint != expected_fingerprint:
            issues.append(
                f"{prefix}: {detail_field} disagrees with live Unity seed snapshot"
            )
        elif (
            type(snapshot.get("sequence")) is not int
            or not isinstance(transition_evidence, dict)
            or snapshot.get("sequence")
            != transition_evidence.get("observed_sequence")
        ):
            issues.append(
                f"{prefix}: {detail_field} sequence is not bound to live Unity evidence"
            )
    return issues


def _c1_native_desktop_semantic_trial_issues(
    trial: dict[str, Any],
) -> list[str]:
    """Fail formal Blender/LibreOffice GUI trials closed on live evidence."""

    arm = str(trial.get("arm") or "")
    normalized = (
        str(trial.get("benchmark") or "")
        .strip()
        .lower()
        .replace("-", "")
        .replace("_", "")
    )
    contract = _C1_NATIVE_DESKTOP_CONTRACTS.get(normalized)
    if arm == "c1_state" or contract is None:
        return []
    if _is_valid_synthesized_fair_zero(trial):
        return []
    prefix = (
        f"{arm}/{trial.get('model')}/{trial.get('task_id')}"
        f"/seed{trial.get('seed')}"
    )
    _seed_source, expected_semantic_source = contract
    issues: list[str] = []
    if trial.get("semantic_probe_source") != expected_semantic_source:
        issues.append(
            f"{prefix}: semantic_probe_source must be {expected_semantic_source}"
        )
    if trial.get("policy_action_interface") != "pixel_mouse_keyboard_only":
        issues.append(f"{prefix}: policy action interface is not pixel-only")
    if trial.get("policy_received_semantic_probe") is not False:
        issues.append(f"{prefix}: semantic probe leakage attestation missing or failed")
    if trial.get("policy_observation_source") != "application_framebuffer_pixels":
        issues.append(f"{prefix}: policy did not observe application framebuffer pixels")

    goal_status = trial.get("goal_evaluation_status")
    goal_evaluated = bool(
        goal_status == "evaluated"
        or (
            goal_status in {None, ""}
            and isinstance(trial.get("after_semantic_detail"), dict)
            and bool(trial.get("after_semantic_detail"))
        )
    )
    pairs = [("before_semantic_detail", "before_semantic_ok")]
    if goal_evaluated:
        pairs.append(("after_semantic_detail", "after_semantic_ok"))
    for detail_field, outcome_field in pairs:
        detail = trial.get(detail_field)
        if not isinstance(detail, dict):
            issues.append(f"{prefix}: {detail_field} is missing live semantic evidence")
            continue
        checks = detail.get("checks")
        checks_well_formed = bool(
            isinstance(checks, list)
            and checks
            and all(
                isinstance(check, dict)
                and isinstance(check.get("name"), str)
                and bool(check.get("name"))
                and type(check.get("ok")) is bool
                for check in checks
            )
        )
        errors = detail.get("errors", [])
        errors_well_formed = bool(
            isinstance(errors, list)
            and all(isinstance(error, str) for error in errors)
        )
        if detail.get("source") != expected_semantic_source:
            issues.append(f"{prefix}: {detail_field} has the wrong live source")
        if detail.get("live") is not True:
            issues.append(f"{prefix}: {detail_field} is not a live semantic probe")
        if not checks_well_formed:
            issues.append(f"{prefix}: {detail_field}.checks are malformed or empty")
        if not errors_well_formed:
            issues.append(f"{prefix}: {detail_field}.errors are malformed")
        if checks_well_formed:
            checks_ok = all(check["ok"] for check in checks)
            if trial.get(outcome_field) is not checks_ok:
                issues.append(
                    f"{prefix}: {outcome_field} does not match all({detail_field}.checks.ok)"
                )
    if normalized == "libreoffice" and goal_evaluated:
        before = trial.get("before_semantic_detail")
        after = trial.get("after_semantic_detail")
        if isinstance(before, dict) and isinstance(after, dict):
            start_identity = before.get("document_identity")
            start_session = before.get("session_identity")
            if not isinstance(start_identity, str) or not start_identity.strip():
                issues.append(f"{prefix}: LibreOffice start document identity is missing")
            if not isinstance(start_session, str) or not start_session.strip():
                issues.append(f"{prefix}: LibreOffice start session identity is missing")
            if not isinstance(before.get("document_url"), str):
                issues.append(f"{prefix}: LibreOffice start document URL state is missing")
            if type(before.get("document_modified")) is not bool:
                issues.append(
                    f"{prefix}: LibreOffice start modification state is missing"
                )

            after_checks = after.get("checks")
            by_name = {
                str(check.get("name")): check.get("ok")
                for check in after_checks
                if isinstance(check, dict)
                and isinstance(check.get("name"), str)
                and type(check.get("ok")) is bool
            } if isinstance(after_checks, list) else {}
            transition_names = {
                "transition.session_same",
                "transition.saved_url",
                "transition.persisted_file",
                "transition.not_modified",
                "transition.document_reopened",
            }
            missing = sorted(transition_names - set(by_name))
            if missing:
                issues.append(
                    f"{prefix}: LibreOffice goal lacks save/reopen checks: {missing}"
                )

            after_identity = after.get("document_identity")
            after_session = after.get("session_identity")
            same_session = bool(
                isinstance(start_session, str)
                and start_session
                and after_session == start_session
            )
            saved_url = bool(
                isinstance(after.get("document_url"), str)
                and str(after.get("document_url")).startswith("file:")
            )
            persisted_path = after.get("document_path")
            persisted = bool(
                isinstance(persisted_path, str)
                and persisted_path
                and Path(persisted_path).is_absolute()
            )
            not_modified = after.get("document_modified") is False
            reopened = bool(
                isinstance(start_identity, str)
                and start_identity
                and isinstance(after_identity, str)
                and after_identity
                and after_identity != start_identity
            )
            expected_checks = {
                "transition.session_same": same_session,
                "transition.saved_url": saved_url,
                "transition.persisted_file": persisted,
                "transition.not_modified": not_modified,
                "transition.document_reopened": reopened,
            }
            for name, expected in expected_checks.items():
                if name in by_name and by_name[name] is not expected:
                    issues.append(
                        f"{prefix}: {name} is inconsistent with live document state"
                    )

            if trial.get("after_semantic_ok") is True and not all(
                expected_checks.values()
            ):
                issues.append(
                    f"{prefix}: successful LibreOffice goal lacks persisted save/reopen proof"
                )

            seed_evidence = trial.get("seed_evidence")
            final_snapshot = (
                seed_evidence.get("final")
                if isinstance(seed_evidence, dict)
                else None
            )
            final_observation = (
                final_snapshot.get("semantic_observation")
                if isinstance(final_snapshot, dict)
                else None
            )
            if isinstance(final_observation, dict):
                for field in (
                    "document_identity",
                    "document_url",
                    "document_modified",
                    "session_identity",
                    "document_path",
                ):
                    if final_observation.get(field) != after.get(field):
                        issues.append(
                            f"{prefix}: LibreOffice goal {field} differs from seed evidence"
                        )
            else:
                issues.append(
                    f"{prefix}: LibreOffice final seed observation is missing"
                )
    return issues


def _c1_web_semantic_trial_issues(trial: dict[str, Any]) -> list[str]:
    """Bind JupyterLab result fields to live seed observations."""

    arm = str(trial.get("arm") or "")
    normalized = (
        str(trial.get("benchmark") or "")
        .strip()
        .lower()
        .replace("-", "")
        .replace("_", "")
    )
    expected_source = {
        "jupyterlab": "jupyterlab_contents_kernel_live_v1",
    }.get(normalized)
    if arm not in {"c1_agent_s", "c1_agent_s_synth"} or expected_source is None:
        return []
    if _is_valid_synthesized_fair_zero(trial):
        return []
    prefix = (
        f"{arm}/{trial.get('model')}/{trial.get('task_id')}"
        f"/seed{trial.get('seed')}"
    )
    issues: list[str] = []
    if trial.get("semantic_probe_source") != expected_source:
        issues.append(f"{prefix}: C1 web semantic_probe_source is invalid")
    if trial.get("policy_action_interface") != "pixel_mouse_keyboard_only":
        issues.append(f"{prefix}: C1 web policy action interface is not pixel-only")
    if trial.get("policy_received_semantic_probe") is not False:
        issues.append(f"{prefix}: C1 web semantic probe leaked to the policy")
    if trial.get("policy_observation_source") != "playwright_page_pixels":
        issues.append(f"{prefix}: C1 web policy did not observe Playwright page pixels")
    if trial.get("scripted_transition_executed") is not False:
        issues.append(f"{prefix}: C1 web scripted transition attestation failed")

    evidence = trial.get("seed_evidence")
    if not isinstance(evidence, dict) or evidence.get("source") != "c1_web_background_pixel":
        issues.append(f"{prefix}: C1 web background-pixel seed evidence is missing")
    snapshots = {
        "before_semantic_detail": (
            evidence.get("initial") if isinstance(evidence, dict) else None
        ),
        "after_semantic_detail": (
            evidence.get("final") if isinstance(evidence, dict) else None
        ),
    }
    fields = [("before_semantic_detail", "before_semantic_ok")]
    if trial.get("goal_evaluation_status") == "evaluated":
        fields.append(("after_semantic_detail", "after_semantic_ok"))
    for detail_field, outcome_field in fields:
        detail = trial.get(detail_field)
        if not isinstance(detail, dict):
            issues.append(f"{prefix}: {detail_field} is missing")
            continue
        observation = detail.get("observation")
        fingerprint = detail.get("fingerprint")
        stable_fingerprint = detail.get("stable_fingerprint")
        status = detail.get("status")
        ok = detail.get("ok")
        if detail.get("source") != expected_source:
            issues.append(f"{prefix}: {detail_field} has the wrong live source")
        if detail.get("provenance_valid") is not True:
            issues.append(f"{prefix}: {detail_field} provenance is not valid")
        if not isinstance(ok, bool) or ok != (status == "success"):
            issues.append(f"{prefix}: {detail_field} status/ok is inconsistent")
        if isinstance(trial.get(outcome_field), bool) and ok != trial.get(outcome_field):
            issues.append(f"{prefix}: {detail_field} differs from {outcome_field}")
        if not isinstance(observation, dict) or not observation:
            issues.append(f"{prefix}: {detail_field} observation is missing")
            continue
        if fingerprint != _c1_web_semantic_digest(observation):
            issues.append(f"{prefix}: {detail_field} fingerprint is invalid")
        if stable_fingerprint != _c1_web_semantic_digest(
            _c1_web_stable_semantic(observation)
        ):
            issues.append(
                f"{prefix}: {detail_field} stable fingerprint is invalid"
            )
        snapshot = snapshots[detail_field]
        bound = (
            snapshot.get("semantic_fingerprint")
            if isinstance(snapshot, dict)
            else None
        )
        expected_bound = {
            "source": detail.get("source"),
            "status": status,
            "ok": ok,
            "observation": observation,
            "fingerprint": fingerprint,
            "stable_fingerprint": stable_fingerprint,
        }
        if bound != expected_bound:
            issues.append(
                f"{prefix}: {detail_field} differs from seed-bound semantic evidence"
            )
    return issues


def _c1_formal_start_gate_issues(trial: dict[str, Any]) -> list[str]:
    """Validate the explicit start-gate state machine for every GUI game."""

    if type(trial.get("incomplete")) is not bool:
        return [
            f"{trial.get('arm')}/{trial.get('model')}/{trial.get('task_id')}"
            f"/seed{trial.get('seed')}: incomplete must be an explicit boolean"
        ]
    prefix = (
        f"{trial.get('arm')}/{trial.get('model')}/{trial.get('task_id')}"
        f"/seed{trial.get('seed')}"
    )
    if trial.get("arm") == "c1_state":
        # The state arm has its own stage-metric state machine
        # (``explicit_state_stage_metric_issues``), but its Start is compared
        # directly against the synthesized GUI arm's, so the runtime hold around
        # the Start judge has to be validated here too.
        return _c1_start_grading_hold_issues(trial, prefix)
    issues: list[str] = []
    construction = trial.get("start_construction_status")
    if trial.get("arm") in {"c1_gui", "c1_agent_s"}:
        if construction != "not_applicable":
            issues.append(f"{prefix}: from-start arm has invalid start construction status")
    elif trial.get("arm") in {"c1_gui_synth", "c1_agent_s_synth"}:
        if construction in {"synthesis_failed", "restore_failed"}:
            return _c1_synthesized_fair_zero_issues(trial)
        if construction != "reached":
            issues.append(f"{prefix}: synthesized arm has nonterminal start construction status")
    if not isinstance(trial.get("start_grading_started"), bool):
        issues.append(f"{prefix}: start_grading_started must be boolean")
    gate = trial.get("start_gate_status")
    goal_status = trial.get("goal_evaluation_status")
    if gate not in {"passed", "semantic_failed", "visual_failed"}:
        issues.append(f"{prefix}: invalid start_gate_status {gate!r}")
    if goal_status not in {"evaluated", "not_evaluated_start_failed"}:
        issues.append(f"{prefix}: invalid goal_evaluation_status {goal_status!r}")
    before_visual_status = trial.get("before_visual_status")
    after_visual_status = trial.get("after_visual_status")
    before_semantic_status = trial.get("before_semantic_status")
    after_semantic_status = trial.get("after_semantic_status")
    if trial.get("start_grading_started") is not True:
        issues.append(f"{prefix}: formal GUI trial did not start start grading")
    if goal_status == "evaluated":
        if gate != "passed":
            issues.append(f"{prefix}: evaluated goal requires passed start gate")
        if before_visual_status != "evaluated" or after_visual_status != "evaluated":
            issues.append(f"{prefix}: evaluated goal requires both visual grades")
        if before_semantic_status in {None, "not_evaluated", "not_run"}:
            issues.append(f"{prefix}: evaluated goal lacks before semantic status")
        if after_semantic_status in {None, "not_evaluated", "not_run"}:
            issues.append(f"{prefix}: evaluated goal lacks after semantic status")
    elif goal_status == "not_evaluated_start_failed":
        if gate not in {"semantic_failed", "visual_failed"}:
            issues.append(f"{prefix}: non-evaluated goal lacks failed start gate")
        if after_visual_status != "not_evaluated" or after_semantic_status != "not_evaluated":
            issues.append(f"{prefix}: start failure must not carry after evidence")
        if gate == "semantic_failed":
            if before_visual_status != "not_evaluated_semantic_failed":
                issues.append(f"{prefix}: semantic start failure must skip visual grading")
            if trial.get("visual_grading_started") is not False:
                issues.append(f"{prefix}: semantic start failure started visual grading")
        if gate == "visual_failed":
            if before_visual_status != "evaluated":
                issues.append(f"{prefix}: visual start failure lacks before visual grade")
            if trial.get("visual_grading_started") is not True:
                issues.append(f"{prefix}: visual start failure did not start visual grading")

    return issues + _c1_start_grading_hold_issues(trial, prefix)


def _c1_start_grading_hold_issues(trial: dict[str, Any], prefix: str) -> list[str]:
    """Require the live runtime to be frozen while the Start judge runs.

    Applies to every arm whose Start rate is compared: a judge request that runs
    against a live simulation grades a different state than one that runs against
    a held one.
    """

    issues: list[str] = []
    hold = trial.get("start_grading_runtime_hold")
    if trial.get("visual_grading_started") is not True:
        if hold is not None:
            issues.append(
                f"{prefix}: skipped visual start grade must not claim runtime hold"
            )
        return issues

    normalized_benchmark = (
        str(trial.get("benchmark") or "")
        .strip()
        .lower()
        .replace("-", "")
        .replace("_", "")
    )
    unity_benchmark = normalized_benchmark in {"jynew", "pal3", "pal3unity"}
    native_process_benchmark = normalized_benchmark in {
        "openra",
        "jynew",
        "pal3",
        "pal3unity",
        "blender",
        "libreoffice",
    }
    expected_backend = (
        "sigstop"
        if normalized_benchmark
        in {
            "luanti",
            "minetest",
            "openra",
            "jynew",
            "pal3",
            "pal3unity",
            "blender",
            "libreoffice",
        }
        else "chromium_cdp_script_pause"
    )
    if not isinstance(hold, dict):
        issues.append(f"{prefix}: start visual grading lacks runtime hold evidence")
        return issues

    # The state arm's Unity path closes its adapter before grading, so the graded
    # start cannot drift and there is no process left to suspend. That is a
    # stronger guarantee than a hold, recorded under its own backend.
    if (
        trial.get("arm") == "c1_state"
        and unity_benchmark
        and hold.get("backend") == "runtime_terminated_before_grading"
    ):
        for field, expected in (("required", False), ("held", False)):
            if hold.get(field) != expected:
                issues.append(
                    f"{prefix}: terminated-runtime start grading hold {field} "
                    f"must be {expected!r}"
                )
        return issues

    expected_hold = {
        "required": True,
        "held": True,
        "released": True,
        "backend": expected_backend,
    }
    for field, expected in expected_hold.items():
        if hold.get(field) != expected:
            issues.append(
                f"{prefix}: start grading runtime hold {field} must be {expected!r}"
            )
    if not native_process_benchmark:
        return issues
    held_pid = _integer(hold.get("held_pid"))
    if held_pid is None or held_pid <= 0:
        issues.append(
            f"{prefix}: native start grading hold lacks a positive held_pid"
        )
    if trial.get("arm") == "c1_state":
        # The state arm records its process evidence through a different code
        # path, so only the GUI arms cross-reference the held pid here.
        return issues
    seed_evidence = trial.get("seed_evidence")
    initial = seed_evidence.get("initial") if isinstance(seed_evidence, dict) else None
    observed_pid = (
        _integer(initial.get("process_id")) if isinstance(initial, dict) else None
    )
    if observed_pid is None or observed_pid <= 0:
        issues.append(f"{prefix}: native runtime seed evidence lacks process_id")
    elif held_pid is not None and held_pid != observed_pid:
        issues.append(
            f"{prefix}: held_pid does not match the observed native process"
        )
    return issues


def _c1_synthesized_fair_zero_issues(trial: dict[str, Any]) -> list[str]:
    """Validate the only two complete B/D failures before start grading."""

    prefix = (
        f"{trial.get('arm')}/{trial.get('model')}/{trial.get('task_id')}"
        f"/seed{trial.get('seed')}"
    )
    status = trial.get("start_construction_status")
    issues: list[str] = []
    if trial.get("arm") not in {"c1_gui_synth", "c1_agent_s_synth"}:
        issues.append(f"{prefix}: fair pre-start zero is exclusive to synthesized arms")
    if status not in {"synthesis_failed", "restore_failed"}:
        issues.append(f"{prefix}: invalid fair pre-start construction status")
    if type(trial.get("incomplete")) is not bool or trial.get("incomplete") is not False:
        issues.append(f"{prefix}: fair pre-start zero must be complete")
    if trial.get("start_gate_status") != "not_evaluated_start_construction_failed":
        issues.append(f"{prefix}: fair pre-start zero has invalid start_gate_status")
    if trial.get("goal_evaluation_status") != "not_evaluated_start_construction_failed":
        issues.append(f"{prefix}: fair pre-start zero has invalid goal status")
    if trial.get("visual_only_evaluation_status") != "not_evaluated_start_construction_failed":
        issues.append(f"{prefix}: fair pre-start zero has invalid visual-only status")
    if trial.get("result_status") != "failure" or trial.get("raw_result") is not False:
        issues.append(f"{prefix}: fair pre-start zero must be a counted failure")
    for field in (
        "policy_phase_started",
        "start_grading_started",
        "visual_grading_started",
        "before_visual_ok",
        "before_semantic_ok",
        "after_visual_ok",
        "after_semantic_ok",
        "start_success",
        "goal_success",
        "e2e_success",
    ):
        if trial.get(field) is not False:
            issues.append(f"{prefix}: fair pre-start zero requires {field}=false")
    steps = trial.get("steps") if isinstance(trial.get("steps"), dict) else {}
    if any(_integer(steps.get(field)) != 0 for field in ("phase_a", "phase_b", "total")):
        issues.append(f"{prefix}: fair pre-start zero must have zero steps")
    if trial.get("time_start_to_goal_ms") not in {0, 0.0}:
        issues.append(f"{prefix}: fair pre-start zero has nonzero goal time")
    if trial.get("pair_verdict_present") is not False:
        issues.append(f"{prefix}: fair pre-start zero must not contain a visual verdict")
    if trial.get("screenshot_after") is not None:
        issues.append(f"{prefix}: fair pre-start zero must not contain an after screenshot")
    if (
        trial.get("phase_b_actions") is not None
        and trial.get("phase_b_actions") != []
        and trial.get("phase_b_actions") != ()
    ):
        issues.append(f"{prefix}: fair pre-start zero must not contain phase-B actions")
    if (
        trial.get("after_semantic_detail") is not None
        and trial.get("after_semantic_detail") != {}
    ):
        issues.append(f"{prefix}: fair pre-start zero must not contain after semantic evidence")
    for field in (
        "before_visual_status",
        "after_visual_status",
        "before_semantic_status",
        "after_semantic_status",
    ):
        if trial.get(field) != "not_evaluated":
            issues.append(f"{prefix}: fair pre-start zero must not carry {field}")
    if trial.get("start_grading_runtime_hold") is not None:
        issues.append(f"{prefix}: fair pre-start zero must not claim runtime hold")
    seed_evidence = trial.get("seed_evidence")
    if status == "synthesis_failed":
        if trial.get("runtime_started") is not False:
            issues.append(f"{prefix}: synthesis failure must precede runtime start")
        if trial.get("prng") is not None:
            issues.append(f"{prefix}: synthesis failure must not claim PRNG evidence")
        if (
            not isinstance(seed_evidence, dict)
            or seed_evidence.get("observed") is not False
            or seed_evidence.get("source") != "runtime_not_started"
        ):
            issues.append(f"{prefix}: synthesis failure seed evidence is invalid")
        if trial.get("checkpoint_restore_ms") not in {0, 0.0}:
            issues.append(f"{prefix}: synthesis failure has nonzero restore time")
    elif status == "restore_failed":
        if trial.get("runtime_started") is not True:
            issues.append(f"{prefix}: restore failure must record a started runtime")
        if not isinstance(trial.get("prng"), dict):
            issues.append(f"{prefix}: restore failure lacks PRNG evidence")
        if not isinstance(seed_evidence, dict) or seed_evidence.get("observed") is not True:
            issues.append(f"{prefix}: restore failure lacks observed seed evidence")
        if _number(trial.get("checkpoint_restore_ms")) is None:
            issues.append(f"{prefix}: restore failure lacks restore timing")
    return issues


def _is_valid_synthesized_fair_zero(trial: dict[str, Any]) -> bool:
    return (
        trial.get("start_construction_status")
        in {"synthesis_failed", "restore_failed"}
        and not _c1_synthesized_fair_zero_issues(trial)
    )


def _is_state_pre_golden_synthesis_failure(trial: dict[str, Any]) -> bool:
    return bool(
        trial.get("arm") == "c1_state"
        and trial.get("result_status") == "failed"
        and trial.get("incomplete") is False
        and isinstance(trial.get("result_reason"), str)
        and trial["result_reason"].startswith("config synthesis failed: ")
        and trial.get("oracle_goldens") is None
    )


def _c1_golden_provenance_issues(
    trial: dict[str, Any],
    *,
    expected: dict[str, str] | None = None,
    artifact_cache: OracleGoldenArtifactValidationCache | None = None,
) -> list[str]:
    prefix = (
        f"{trial.get('arm')}/{trial.get('model')}/{trial.get('task_id')}"
        f"/seed{trial.get('seed')}"
    )
    value = trial.get("oracle_goldens")
    if not isinstance(value, dict):
        return [f"{prefix}: oracle_goldens provenance is missing"]
    issues: list[str] = []
    if not isinstance(value.get("fingerprint"), str) or not value["fingerprint"].strip():
        issues.append(f"{prefix}: oracle golden fingerprint is missing")
    if not isinstance(value.get("cache_hit"), bool):
        issues.append(f"{prefix}: oracle golden cache_hit must be boolean")
    if not isinstance(value.get("cacheable"), bool):
        issues.append(f"{prefix}: oracle golden cacheable must be boolean")
    consistency = value.get("consistency")
    quality = value.get("quality")
    if not isinstance(consistency, dict) or not consistency:
        issues.append(f"{prefix}: oracle golden consistency evidence is missing")
    if not isinstance(quality, dict) or not quality:
        issues.append(f"{prefix}: oracle golden quality evidence is missing")
    else:
        for stage in ("before", "after"):
            stage_quality = quality.get(stage)
            if not isinstance(stage_quality, dict) or stage_quality.get("informative") is not True:
                issues.append(f"{prefix}: oracle golden {stage} is not informative")
    if value.get("cache_hit") is True:
        if value.get("cacheable") is not True:
            issues.append(f"{prefix}: oracle cache hit is not cacheable")
        if not isinstance(consistency, dict) or consistency.get("source") != "verified_cache":
            issues.append(f"{prefix}: oracle cache hit lacks verified_cache evidence")
    for stage in ("before", "after"):
        if not isinstance(value.get(stage), str) or not value[stage]:
            issues.append(f"{prefix}: oracle golden {stage} path is missing")
    if expected is not None:
        required_expected = {
            "fingerprint",
            "git_commit",
            "oracle_spec_sha256",
        }
        if not required_expected.issubset(expected):
            issues.append(f"{prefix}: expected oracle golden provenance is incomplete")
    issues.extend(
        validate_oracle_golden_artifacts(
            value,
            reported_seed=trial.get("seed"),
            expected_fingerprint=(expected or {}).get("fingerprint"),
            expected_git_commit=(expected or {}).get("git_commit"),
            expected_oracle_spec_sha256=(expected or {}).get(
                "oracle_spec_sha256"
            ),
            cache=artifact_cache,
            issue_prefix=f"{prefix}: oracle_goldens",
        )
    )
    return issues


def _stratum_key(value: Any) -> str:
    if value is None:
        return "unclassified"
    return str(value).lower() if isinstance(value, bool) else str(value)


def _c1_sensitivity(
    indices: dict[str, dict[str, dict[tuple[str, int], dict[str, Any]]]],
    task_ids: list[str],
    rng: random.Random,
    *,
    iters: int,
    ci: float,
    rmst_tau: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Model-specific checkpoint effects stratified without pooling arm E."""

    allowed = set(task_ids)
    dimensions = {
        "game": "benchmark",
        "horizon": "horizon_class",
        "reference_reachability": "reference_reachable",
        "validity_class": "validity_class",
    }
    checkpoint_effects: list[dict[str, Any]] = []
    for comparison, arm_a, arm_b in _C1_CHECKPOINT_COMPARISONS:
        for model in sorted(set(indices.get(arm_a) or {}) & set(indices.get(arm_b) or {})):
            a_index = indices[arm_a][model]
            b_index = indices[arm_b][model]
            shared = {
                key for key in set(a_index) & set(b_index)
                if key[0] in allowed
            }
            by_dimension: dict[str, dict[str, Any]] = {}
            for dimension, field in dimensions.items():
                strata: dict[str, set[tuple[str, int]]] = {}
                for key in shared:
                    value = a_index[key].get(field)
                    strata.setdefault(_stratum_key(value), set()).add(key)
                by_dimension[dimension] = {}
                for stratum, keys in sorted(strata.items()):
                    time_effect = _clustered_paired_administrative_rmst_diff(
                        b_index,
                        a_index,
                        keys,
                        field="time_e2e_excluding_synthesis_ms",
                        tau_by_horizon=rmst_tau,
                        fallback_tau=None,
                        iterations=iters,
                        ci=ci,
                        rng=rng,
                    )
                    by_dimension[dimension][stratum] = {
                        "shared_trials": len(keys),
                        "shared_tasks": len({task_id for task_id, _ in keys}),
                        "start_effect_b_minus_a": p5_stats.clustered_paired_bootstrap_diff(
                            {key: b_index[key]["start_success"] for key in keys},
                            {key: a_index[key]["start_success"] for key in keys},
                            iterations=iters, ci=ci, rng=rng,
                        ),
                        "conditional_goal_effect_b_minus_a": p5_stats.clustered_conditional_rate_diff(
                            {key: b_index[key]["goal_success"] for key in keys},
                            {key: b_index[key]["start_success"] for key in keys},
                            {key: a_index[key]["goal_success"] for key in keys},
                            {key: a_index[key]["start_success"] for key in keys},
                            iterations=iters, ci=ci, rng=rng,
                        ),
                        "e2e_effect_b_minus_a": p5_stats.clustered_paired_bootstrap_diff(
                            {key: b_index[key]["e2e_success"] for key in keys},
                            {key: a_index[key]["e2e_success"] for key in keys},
                            iterations=iters, ci=ci, rng=rng,
                        ),
                        "time_effect_method": "task_horizon_administrative_restricted_mean",
                        "termination_time_effect_b_minus_a_ms": time_effect,
                        "rmst_time_e2e_excluding_synthesis_effect_b_minus_a_ms": time_effect,
                        "token_effect_b_minus_a": p5_stats.clustered_paired_bootstrap_diff(
                            {key: b_index[key].get("total_tokens") for key in keys},
                            {key: a_index[key].get("total_tokens") for key in keys},
                            iterations=iters, ci=ci, rng=rng,
                        ),
                    }
            checkpoint_effects.append({
                "comparison": comparison,
                "arm_a": arm_a,
                "arm_b": arm_b,
                "model": model,
                "dimensions": by_dimension,
            })

    state_by_model: list[dict[str, Any]] = []
    for model, index in sorted(indices["c1_state"].items()):
        selected = {key: row for key, row in index.items() if key[0] in allowed}
        by_dimension: dict[str, dict[str, Any]] = {}
        for dimension, field in dimensions.items():
            strata: dict[str, dict[tuple[str, int], dict[str, Any]]] = {}
            for key, row in selected.items():
                strata.setdefault(_stratum_key(row.get(field)), {})[key] = row
            by_dimension[dimension] = {
                stratum: {
                    "trials": len(rows),
                    "tasks": len({task_id for task_id, _ in rows}),
                    "e2e_success": p5_stats.clustered_mean_ci(
                        {key: row["e2e_success"] for key, row in rows.items()},
                        iterations=iters, ci=ci, rng=rng,
                    ),
                }
                for stratum, rows in sorted(strata.items())
            }
        state_by_model.append({"model": model, "dimensions": by_dimension})

    equal_weight_game_macro: list[dict[str, Any]] = []
    for arm, by_model in sorted(indices.items()):
        for model, index in sorted(by_model.items()):
            selected = {
                key: row for key, row in index.items() if key[0] in allowed
            }
            by_game: dict[str, list[dict[str, Any]]] = {}
            for row in selected.values():
                by_game.setdefault(str(row.get("benchmark") or "unknown"), []).append(row)
            per_game: dict[str, dict[str, Any]] = {}
            for game, rows in sorted(by_game.items()):
                starts = [float(bool(row.get("start_success"))) for row in rows]
                e2e = [float(bool(row.get("e2e_success"))) for row in rows]
                started = [row for row in rows if row.get("start_success") is True]
                per_game[game] = {
                    "trials": len(rows),
                    "start_success": sum(starts) / len(starts),
                    "goal_success_given_start": (
                        sum(float(bool(row.get("goal_success"))) for row in started)
                        / len(started)
                        if started
                        else None
                    ),
                    "e2e_success": sum(e2e) / len(e2e),
                }
            goal_values = [
                value["goal_success_given_start"]
                for value in per_game.values()
                if value["goal_success_given_start"] is not None
            ]
            equal_weight_game_macro.append(
                {
                    "arm": arm,
                    "model": model,
                    "games": len(per_game),
                    "weighting": "equal_weight_per_game",
                    "start_success": (
                        sum(value["start_success"] for value in per_game.values())
                        / len(per_game)
                        if per_game
                        else None
                    ),
                    "goal_success_given_start": (
                        sum(goal_values) / len(goal_values) if goal_values else None
                    ),
                    "e2e_success": (
                        sum(value["e2e_success"] for value in per_game.values())
                        / len(per_game)
                        if per_game
                        else None
                    ),
                    "per_game": per_game,
                }
            )

    by_capability: list[dict[str, Any]] = []
    for arm, by_model in sorted(indices.items()):
        for model, index in sorted(by_model.items()):
            selected = {
                key: row for key, row in index.items() if key[0] in allowed
            }
            capabilities = sorted({
                str(capability)
                for row in selected.values()
                for capability in (row.get("capabilities") or [])
            })
            for capability in capabilities:
                rows = {
                    key: row
                    for key, row in selected.items()
                    if capability in (row.get("capabilities") or [])
                }
                starts = {
                    key: row["start_success"] for key, row in rows.items()
                }
                goals = {
                    key: row["goal_success"] for key, row in rows.items()
                }
                passes = {
                    key: row["e2e_success"] for key, row in rows.items()
                }
                by_capability.append({
                    "arm": arm,
                    "model": model,
                    "capability": capability,
                    "tasks": len({task_id for task_id, _ in rows}),
                    "trials": len(rows),
                    "start_success": p5_stats.clustered_mean_ci(
                        starts, iterations=iters, ci=ci, rng=rng
                    ),
                    "goal_success_given_start": (
                        p5_stats.clustered_conditional_mean_ci(
                            goals,
                            starts,
                            iterations=iters,
                            ci=ci,
                            rng=rng,
                        )
                    ),
                    "e2e_success": p5_stats.clustered_mean_ci(
                        passes, iterations=iters, ci=ci, rng=rng
                    ),
                })
    return {
        "checkpoint_effects": checkpoint_effects,
        "state_managed_by_model": state_by_model,
        "equal_weight_game_macro": equal_weight_game_macro,
        "by_capability": by_capability,
        "capability_membership": "non_exclusive",
        "capability_cluster_unit": "task_id",
        "state_managed_pooled_with_gui": False,
    }


def _seed_evidence_issues(
    indices: dict[str, dict[str, dict[tuple[str, int], dict[str, Any]]]],
    task_ids: list[str],
) -> list[str]:
    def signature(trial: dict[str, Any]) -> tuple[Any, ...] | None:
        evidence = trial.get("seed_evidence")
        if not isinstance(evidence, dict) or evidence.get("observed") is not True:
            return None
        if evidence.get("source") == "browser_page_runtime":
            initial = evidence.get("initial")
            if not isinstance(initial, dict):
                return None
            viewport = initial.get("viewport")
            return (
                "browser_page_runtime",
                evidence.get("algorithm"),
                _integer(evidence.get("seed")),
                _integer(initial.get("calls")),
                _integer(viewport.get("width")) if isinstance(viewport, dict) else None,
                _integer(viewport.get("height")) if isinstance(viewport, dict) else None,
                _number(initial.get("device_scale_factor")),
            )
        if evidence.get("source") == "luanti_warm_session":
            frame = evidence.get("frame")
            return (
                "luanti_warm_session",
                evidence.get("algorithm"),
                _integer(evidence.get("seed")),
                _integer(evidence.get("map_seed")),
                evidence.get("map_backend"),
                evidence.get("gameid"),
                evidence.get("creative_mode"),
                _integer(frame.get("width")) if isinstance(frame, dict) else None,
                _integer(frame.get("height")) if isinstance(frame, dict) else None,
                evidence.get("git_commit"),
                evidence.get("config_fingerprint"),
                evidence.get("initial_semantic_source"),
                evidence.get("initial_semantic_stage"),
                evidence.get("initial_semantic_fingerprint"),
            )
        if evidence.get("source") == "c1_web_background_pixel":
            initial = evidence.get("initial")
            if not isinstance(initial, dict):
                return None
            viewport = initial.get("viewport")
            screen = initial.get("screen")
            semantic = initial.get("semantic_fingerprint")
            if not isinstance(semantic, dict):
                return None
            return (
                "c1_web_background_pixel",
                evidence.get("algorithm"),
                _integer(evidence.get("seed")),
                str(initial.get("benchmark") or "").strip().lower(),
                str(initial.get("task_id") or ""),
                _integer(viewport.get("width"))
                if isinstance(viewport, dict)
                else None,
                _integer(viewport.get("height"))
                if isinstance(viewport, dict)
                else None,
                _integer(screen.get("width")) if isinstance(screen, dict) else None,
                _integer(screen.get("height")) if isinstance(screen, dict) else None,
                semantic.get("source"),
                semantic.get("stable_fingerprint"),
            )
        if evidence.get("source") in {
            "openra_live_settings_process",
            "unity_live_process",
            *(
                contract[0]
                for contract in _C1_NATIVE_DESKTOP_CONTRACTS.values()
            ),
        }:
            initial = evidence.get("initial")
            if not isinstance(initial, dict):
                return None
            semantic_fingerprint = initial.get("semantic_fingerprint")
            if evidence.get("source") in {
                contract[0]
                for contract in _C1_NATIVE_DESKTOP_CONTRACTS.values()
            }:
                if not isinstance(semantic_fingerprint, str):
                    return None
                semantic_signature = semantic_fingerprint
                protocol_or_task = str(initial.get("task_id") or "")
            else:
                try:
                    semantic_signature = json.dumps(
                        semantic_fingerprint,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
                except (TypeError, ValueError):
                    return None
                protocol_or_task = (
                    str(initial.get("task_id") or ""),
                    initial.get("protocol_version"),
                )
            return (
                evidence.get("source"),
                evidence.get("algorithm"),
                _integer(evidence.get("seed")),
                str(initial.get("benchmark") or "").strip().lower() or None,
                protocol_or_task,
                semantic_signature,
            )
        return None

    issues: list[str] = []
    allowed = set(task_ids)
    for comparison, arm_a, arm_b in _C1_SEED_EVIDENCE_PAIRS:
        for model in sorted(set(indices[arm_a]) & set(indices[arm_b])):
            a_index = indices[arm_a][model]
            b_index = indices[arm_b][model]
            for key in sorted(set(a_index) & set(b_index)):
                if key[0] not in allowed:
                    continue
                a_trial, b_trial = a_index[key], b_index[key]
                if comparison == "B_vs_D":
                    if (
                        a_trial.get("initial_state_fingerprint")
                        != b_trial.get("initial_state_fingerprint")
                    ):
                        issues.append(
                            f"{comparison}/{model}/{key[0]}/seed{key[1]}: "
                            "self-synthesized start contract differs across paired policies"
                        )
                    if a_trial.get("runtime_started") is True and b_trial.get("runtime_started") is True:
                        if (
                            not a_trial.get("generated_spec_fingerprint")
                            or a_trial.get("generated_spec_fingerprint")
                            != b_trial.get("generated_spec_fingerprint")
                        ):
                            issues.append(
                                f"{comparison}/{model}/{key[0]}/seed{key[1]}: "
                                "generated start spec differs across paired policies"
                            )

                # Runtime parity is established only by readback from both real
                # runtimes.  A synthesis failure before launch is allowed to remain a
                # fair scored zero and must not be dressed up with planned defaults.
                if not (
                    a_trial.get("runtime_started") is True
                    and b_trial.get("runtime_started") is True
                ):
                    continue
                a_evidence = signature(a_trial)
                b_evidence = signature(b_trial)
                if a_evidence is None or b_evidence is None:
                    issues.append(
                        f"{comparison}/{model}/{key[0]}/seed{key[1]}: "
                        "paired runtime seed evidence is not observed"
                    )
                elif a_evidence != b_evidence:
                    issues.append(
                        f"{comparison}/{model}/{key[0]}/seed{key[1]}: "
                        "runtime-observed seed evidence differs across paired policies"
                    )
    return issues


def _synthesis_accounting_issues(
    indices: dict[str, dict[str, dict[tuple[str, int], dict[str, Any]]]],
    task_ids: list[str],
) -> list[str]:
    """Enforce the shared B/D counterfactual burden when both synth arms are present."""

    issues: list[str] = []
    allowed = set(task_ids)
    b_arm, d_arm = "c1_gui_synth", "c1_agent_s_synth"
    if b_arm not in indices or d_arm not in indices:
        return issues
    if not indices[b_arm] or not indices[d_arm]:
        return issues
    for model in sorted(set(indices[b_arm]) & set(indices[d_arm])):
        b_index = indices[b_arm][model]
        d_index = indices[d_arm][model]
        for key in sorted(set(b_index) & set(d_index)):
            if key[0] not in allowed:
                continue
            b_trial, d_trial = b_index[key], d_index[key]
            label = f"B_vs_D/{model}/{key[0]}/seed{key[1]}"
            if b_trial.get("synthesis_cache_fingerprint") != d_trial.get(
                "synthesis_cache_fingerprint"
            ):
                issues.append(f"{label}: synthesis cache fingerprints differ")
            if b_trial.get("synthesis_attributed_fingerprints") != d_trial.get(
                "synthesis_attributed_fingerprints"
            ):
                issues.append(f"{label}: attributed synthesis fingerprints differ")
            if b_trial.get("synthesis_attributed_burdens") != d_trial.get(
                "synthesis_attributed_burdens"
            ):
                issues.append(f"{label}: attributed synthesis burdens differ")
            b_attr = _cost_signature(b_trial.get("synthesis_attributed_cost"))
            d_attr = _cost_signature(d_trial.get("synthesis_attributed_cost"))
            if b_attr is None or d_attr is None or b_attr != d_attr:
                issues.append(f"{label}: counterfactual attributed costs differ")
                continue
            b_actual = _cost_signature(b_trial.get("synthesis_actual_cost"))
            d_actual = _cost_signature(d_trial.get("synthesis_actual_cost"))
            b_transient = _cost_signature(
                b_trial.get("synthesis_transient_actual_cost")
            )
            d_transient = _cost_signature(
                d_trial.get("synthesis_transient_actual_cost")
            )
            if None in (b_actual, d_actual, b_transient, d_transient):
                issues.append(f"{label}: actual synthesis costs are not comparable")
            else:
                assert b_actual is not None
                assert d_actual is not None
                assert b_transient is not None
                assert d_transient is not None
                combined_actual = tuple(x + y for x, y in zip(b_actual, d_actual))
                expected_actual = tuple(
                    attr + b_extra + d_extra
                    for attr, b_extra, d_extra in zip(
                        b_attr, b_transient, d_transient
                    )
                )
                if combined_actual != expected_actual:
                    issues.append(
                        f"{label}: combined actual synthesis cost is not exactly one "
                        "counterfactual burden plus recorded transient infrastructure "
                        "overhead"
                    )
            b_time = _number(b_trial.get("checkpoint_synthesis_ms"))
            d_time = _number(d_trial.get("checkpoint_synthesis_ms"))
            if b_time is None or d_time is None or not math.isclose(
                b_time, d_time, rel_tol=0.0, abs_tol=1.0
            ):
                issues.append(f"{label}: attributed synthesis times differ")
    return issues


def _c1_start_spec_hash(trial: dict[str, Any]) -> str | None:
    """Return the restored start-spec hash, or ``None`` when there was no spec.

    The synthesized GUI arms record the sentinel ``no-usable-model-spec`` when
    synthesis produced nothing restorable; the state arm records no hash at all.
    Both mean the same thing and must not be compared as if they were hashes.
    """

    for field in ("generated_spec_hash", "generated_spec_fingerprint"):
        value = trial.get(field)
        if isinstance(value, str) and value and value != "no-usable-model-spec":
            return value
    return None


def _start_parity_issues(
    indices: dict[str, dict[str, dict[tuple[str, int], dict[str, Any]]]],
    task_ids: list[str],
) -> list[str]:
    """Require the self-synthesizing arms to share one start and one Start verdict.

    The two arms once keyed their checkpoint cache per consumer and therefore
    rolled the synthesis model independently, so their Start rates differed by
    sampling noise on top of any real difference. They now read one accepted spec
    from one cache, which is only a comparison if the report can show it: equal
    specs, equal synthesis burden, and -- because the Start gate is now the same
    predicate under the same rubric and the same runtime hold -- equal Start
    verdicts.
    """

    issues: list[str] = []
    allowed = set(task_ids)
    for label_prefix, arm_a, arm_b in _C1_START_PARITY_PAIRS:
        if arm_a not in indices or arm_b not in indices:
            continue
        if not indices[arm_a] or not indices[arm_b]:
            continue
        for model in sorted(set(indices[arm_a]) & set(indices[arm_b])):
            a_index = indices[arm_a][model]
            b_index = indices[arm_b][model]
            for key in sorted(set(a_index) & set(b_index)):
                if key[0] not in allowed:
                    continue
                a_trial, b_trial = a_index[key], b_index[key]
                label = f"{label_prefix}/{model}/{key[0]}/seed{key[1]}"
                if a_trial.get("incomplete") is True or b_trial.get("incomplete") is True:
                    continue
                a_spec = _c1_start_spec_hash(a_trial)
                b_spec = _c1_start_spec_hash(b_trial)
                # One shared cache entry means a synthesis failure is shared too:
                # both arms then legitimately have no spec to restore, which is
                # agreement rather than a missing record. Only one arm holding a
                # usable spec is a parity violation.
                if a_spec is None and b_spec is None:
                    continue
                if a_spec is None or b_spec is None:
                    issues.append(
                        f"{label}: only one paired arm restored a self-synthesized "
                        "start spec"
                    )
                    continue
                if a_spec != b_spec:
                    issues.append(
                        f"{label}: paired arms restored different self-synthesized "
                        "start specs"
                    )
                    # Divergent starts make every downstream comparison moot.
                    continue
                # Synthesis burden equality is left to the per-arm cost gates:
                # the arms attribute the shared checkpoint through different
                # accounting models, so comparing the totals here would report
                # bookkeeping shape rather than start parity.
                if any(
                    trial.get("stage_metrics_source")
                    not in {"explicit_v1", "explicit_gui"}
                    for trial in (a_trial, b_trial)
                ):
                    # A legacy report records only a terminal result, so its stage
                    # booleans are absent rather than false and there is no Start
                    # verdict to compare.
                    continue
                for field in ("before_semantic_ok", "before_visual_ok", "start_success"):
                    a_value, b_value = a_trial.get(field), b_trial.get(field)
                    if type(a_value) is not bool or type(b_value) is not bool:
                        # A trial that never reached the gate has nothing to compare.
                        continue
                    if a_value != b_value:
                        issues.append(
                            f"{label}: paired arms disagree on {field} "
                            "for the same start spec"
                        )
    return issues


def _pareto_group(points: list[dict[str, Any]]) -> dict[str, Any]:
    annotated: list[dict[str, Any]] = []
    candidates: list[tuple[dict[str, Any], tuple[float, float, float]]] = []
    for point in points:
        identity = f"{point.get('arm')}:{point.get('model')}"
        metrics = (
            _number(point.get("e2e_success", {}).get("mean")),
            _number(point.get("mean_tokens", {}).get("mean")),
            _number(point.get("rmst_e2e_ms", {}).get("rmst")),
        )
        row = {
            "id": identity,
            "arm": point.get("arm"),
            "method": point.get("method"),
            "model": point.get("model"),
            "e2e_success": metrics[0],
            "mean_tokens": metrics[1],
            "rmst_e2e_ms": metrics[2],
            "eligible": all(value is not None and math.isfinite(value) for value in metrics),
            "dominated_by": [],
            "is_frontier": False,
        }
        annotated.append(row)
        if row["eligible"]:
            candidates.append((row, (float(metrics[0]), float(metrics[1]), float(metrics[2]))))

    for point, (success, tokens, rmst) in candidates:
        for other, (other_success, other_tokens, other_rmst) in candidates:
            if other is point:
                continue
            weakly_better = (
                other_success >= success
                and other_tokens <= tokens
                and other_rmst <= rmst
            )
            strictly_better = (
                other_success > success
                or other_tokens < tokens
                or other_rmst < rmst
            )
            if weakly_better and strictly_better:
                point["dominated_by"].append(other["id"])
        point["is_frontier"] = not point["dominated_by"]
    return {
        "points": annotated,
        "frontier_ids": [row["id"] for row in annotated if row["is_frontier"]],
    }


def _pareto_summary(points: list[dict[str, Any]]) -> dict[str, Any]:
    gui = [point for point in points if point.get("arm") != "c1_state"]
    state = [point for point in points if point.get("arm") == "c1_state"]
    return {
        "axes": {
            "maximize": "e2e_success",
            "minimize": ["mean_tokens", "rmst_e2e_ms"],
        },
        "gui": _pareto_group(gui),
        "state_managed": {
            "semantic": "semantic state-managed execution; not pooled into GUI dominance",
            **_pareto_group(state),
        },
        "cross_semantic_dominance_computed": False,
    }


def _administrative_tau(
    row: dict[str, Any],
    tau_by_horizon: dict[str, float] | None,
    fallback_tau: float | None,
) -> float | None:
    if isinstance(tau_by_horizon, dict):
        value = _number(tau_by_horizon.get(str(row.get("horizon_class") or "")))
        if value is not None and value > 0:
            return value
    value = _number(fallback_tau)
    return value if value is not None and value > 0 else None


def _clustered_administrative_rmst(
    indexed: dict[tuple[str, int], dict[str, Any]],
    keys: set[tuple[str, int]],
    *,
    field: str,
    tau_by_horizon: dict[str, float] | None,
    fallback_tau: float | None,
    iterations: int,
    ci: float,
    rng: random.Random,
) -> dict[str, Any]:
    restricted: dict[tuple[str, int], float] = {}
    events = 0
    for key in keys:
        row = indexed[key]
        duration = _number(row.get(field))
        tau = _administrative_tau(row, tau_by_horizon, fallback_tau)
        if duration is None or tau is None:
            continue
        event = row.get("e2e_success") is True and duration <= tau
        restricted[key] = duration if event else tau
        events += int(event)
    stat = p5_stats.clustered_mean_ci(
        restricted, iterations=iterations, ci=ci, rng=rng
    )
    return {
        **stat,
        "rmst": stat.get("mean"),
        "events": events,
        "tau": fallback_tau if tau_by_horizon is None else None,
        "tau_by_horizon": dict(tau_by_horizon or {}),
        "method": "task_horizon_administrative_restricted_mean",
    }


def _clustered_paired_administrative_rmst_diff(
    b_index: dict[tuple[str, int], dict[str, Any]],
    a_index: dict[tuple[str, int], dict[str, Any]],
    keys: set[tuple[str, int]],
    *,
    field: str,
    tau_by_horizon: dict[str, float] | None,
    fallback_tau: float | None,
    iterations: int,
    ci: float,
    rng: random.Random,
) -> dict[str, Any]:
    b_values: dict[tuple[str, int], float] = {}
    a_values: dict[tuple[str, int], float] = {}
    b_events = 0
    a_events = 0
    for key in keys:
        b_row, a_row = b_index[key], a_index[key]
        b_tau = _administrative_tau(b_row, tau_by_horizon, fallback_tau)
        a_tau = _administrative_tau(a_row, tau_by_horizon, fallback_tau)
        b_duration = _number(b_row.get(field))
        a_duration = _number(a_row.get(field))
        if None in (b_tau, a_tau, b_duration, a_duration):
            continue
        if not math.isclose(float(b_tau), float(a_tau), rel_tol=0.0, abs_tol=0.0):
            continue
        tau = float(b_tau)
        b_event = b_row.get("e2e_success") is True and float(b_duration) <= tau
        a_event = a_row.get("e2e_success") is True and float(a_duration) <= tau
        b_values[key] = float(b_duration) if b_event else tau
        a_values[key] = float(a_duration) if a_event else tau
        b_events += int(b_event)
        a_events += int(a_event)
    stat = p5_stats.clustered_paired_bootstrap_diff(
        b_values,
        a_values,
        iterations=iterations,
        ci=ci,
        rng=rng,
    )
    return {
        **stat,
        "tau": fallback_tau if tau_by_horizon is None else None,
        "tau_by_horizon": dict(tau_by_horizon or {}),
        "b_events": b_events,
        "a_events": a_events,
        "method": "task_horizon_administrative_restricted_mean",
    }


def _method_point(
    indexed: dict[tuple[str, int], dict[str, Any]],
    task_ids: list[str],
    rng: random.Random,
    *,
    iters: int,
    ci: float,
    rmst_tau: float | None = None,
    rmst_tau_including_synthesis: float | None = None,
    rmst_tau_by_horizon: dict[str, float] | None = None,
    rmst_tau_including_synthesis_by_horizon: dict[str, float] | None = None,
) -> dict[str, Any]:
    allowed = set(task_ids)
    selected = {key: row for key, row in indexed.items() if key[0] in allowed}
    rows = list(selected.values())

    def values(field: str, *, when: str | None = None) -> dict[tuple[str, int], Any]:
        return {
            key: row.get(field)
            for key, row in selected.items()
            if (when is None or bool(row.get(when))) and row.get(field) is not None
        }

    starts = {key: 1.0 if row["start_success"] else 0.0 for key, row in selected.items()}
    goals = {key: 1.0 if row["goal_success"] else 0.0 for key, row in selected.items()}
    passes = {key: 1.0 if row["e2e_success"] else 0.0 for key, row in selected.items()}
    successful_times = [
        row.get("time_e2e_excluding_synthesis_ms")
        for row in rows
        if row["e2e_success"] and row.get("time_e2e_excluding_synthesis_ms") is not None
    ]
    cost_by_kind = _sum_nested_cost_rows(rows, "by_kind")
    cost_by_model = _sum_nested_cost_rows(rows, "by_model")
    cost_by_kind_model = _sum_nested_cost_rows(rows, "by_kind_model")
    tokens_by_kind = {
        kind: int(bucket.get("total_tokens", 0) or 0)
        for kind, bucket in cost_by_kind.items()
    }
    evaluation_tokens = sum(_trial_token_split(row)[1] for row in rows)
    agent_tokens = sum(_trial_token_split(row)[0] for row in rows)
    agent_token_values: dict[tuple[str, int], int] = {}
    evaluation_token_values: dict[tuple[str, int], int] = {}
    for key, row in selected.items():
        agent, evaluation = _trial_token_split(row)
        evaluation_token_values[key] = evaluation
        agent_token_values[key] = agent
    cluster_kwargs = {"iterations": iters, "ci": ci, "rng": rng}
    selected_keys = set(selected)
    rmst = _clustered_administrative_rmst(
        selected,
        selected_keys,
        field="time_e2e_excluding_synthesis_ms",
        tau_by_horizon=rmst_tau_by_horizon,
        fallback_tau=rmst_tau,
        iterations=iters,
        ci=ci,
        rng=rng,
    )
    rmst_including = _clustered_administrative_rmst(
        selected,
        selected_keys,
        field="time_e2e_including_synthesis_ms",
        tau_by_horizon=rmst_tau_including_synthesis_by_horizon,
        fallback_tau=rmst_tau_including_synthesis,
        iterations=iters,
        ci=ci,
        rng=rng,
    )
    reasoning_detail_complete = all(
        row.get("reasoning_detail_complete") is True for row in rows
    )
    exact_reasoning_stat = (
        p5_stats.clustered_mean_ci(values("reasoning_tokens"), **cluster_kwargs)
        if reasoning_detail_complete
        else {
            "available": False,
            "reason": "reasoning detail is bounded rather than exact",
        }
    )
    return {
        "tasks": len({task_id for task_id, _ in indexed if task_id in allowed}),
        "trials": len(rows),
        "start_success": p5_stats.clustered_mean_ci(starts, **cluster_kwargs),
        "goal_success": p5_stats.clustered_mean_ci(goals, **cluster_kwargs),
        "goal_success_given_start": p5_stats.clustered_conditional_mean_ci(
            goals,
            {key: row["start_success"] for key, row in selected.items()},
            **cluster_kwargs,
        ),
        "e2e_success": p5_stats.clustered_mean_ci(passes, **cluster_kwargs),
        "deep_state_pass": p5_stats.clustered_mean_ci(passes, **cluster_kwargs),
        "mean_cost_usd": p5_stats.clustered_mean_ci(values("est_usd"), **cluster_kwargs),
        "mean_tokens": p5_stats.clustered_mean_ci(values("total_tokens"), **cluster_kwargs),
        "mean_reasoning_tokens": exact_reasoning_stat,
        "mean_reasoning_tokens_observed": p5_stats.clustered_mean_ci(
            values("reasoning_tokens_observed"), **cluster_kwargs
        ),
        "mean_reasoning_tokens_lower_bound": p5_stats.clustered_mean_ci(
            values("reasoning_tokens_lower_bound"), **cluster_kwargs
        ),
        "mean_reasoning_tokens_upper_bound": p5_stats.clustered_mean_ci(
            values("reasoning_tokens_upper_bound"), **cluster_kwargs
        ),
        "mean_cached_prompt_tokens": p5_stats.clustered_mean_ci(values("cached_prompt_tokens"), **cluster_kwargs),
        "mean_agent_tokens": p5_stats.clustered_mean_ci(agent_token_values, **cluster_kwargs),
        "mean_evaluation_tokens": p5_stats.clustered_mean_ci(
            evaluation_token_values, **cluster_kwargs
        ),
        "mean_wall_ms": p5_stats.clustered_mean_ci(values("time_e2e_excluding_synthesis_ms"), **cluster_kwargs),
        "mean_time_to_start_ms": p5_stats.clustered_mean_ci(values("time_to_start_ms"), **cluster_kwargs),
        "mean_time_start_to_goal_ms": p5_stats.clustered_mean_ci(
            values("time_start_to_goal_ms", when="start_success"), **cluster_kwargs
        ),
        "mean_time_e2e_excluding_synthesis_ms": p5_stats.clustered_mean_ci(
            values("time_e2e_excluding_synthesis_ms"), **cluster_kwargs
        ),
        "mean_time_e2e_including_synthesis_ms": p5_stats.clustered_mean_ci(
            values("time_e2e_including_synthesis_ms"), **cluster_kwargs
        ),
        "rmst_e2e_ms": rmst,
        "rmst_e2e_including_synthesis_ms": rmst_including,
        "median_success_time_ms": median(successful_times) if successful_times else None,
        "successful_events": len(successful_times),
        "termination_time_ms": p5_stats.clustered_mean_ci(values("termination_time_ms"), **cluster_kwargs),
        "agent_tokens": agent_tokens,
        "evaluation_tokens": evaluation_tokens,
        "tokens_by_kind": tokens_by_kind,
        "tokens_by_model": _sum_nested_token_rows(rows, "by_model"),
        "tokens_by_kind_model": _sum_nested_token_rows(rows, "by_kind_model"),
        "cost_by_kind": cost_by_kind,
        "cost_by_model": cost_by_model,
        "cost_by_kind_model": cost_by_kind_model,
        "synthesis_actual_cost": _sum_cost_summary_field(
            rows, "synthesis_actual_cost"
        ),
        "synthesis_transient_actual_cost": _sum_cost_summary_field(
            rows, "synthesis_transient_actual_cost"
        ),
        "synthesis_attributed_cost": _sum_cost_summary_field(
            rows, "synthesis_attributed_cost"
        ),
        "actual_cost": _sum_cost_summary_field(rows, "actual_cost"),
        "transient_infrastructure_cost": _sum_cost_summary_field(
            rows, "transient_infrastructure_cost"
        ),
    }


def c1_from_reports(
    state_reports: list[dict[str, Any]],
    gui_reports: list[dict[str, Any]],
    *,
    gui_synth_reports: list[dict[str, Any]] | None = None,
    agent_s_reports: list[dict[str, Any]] | None = None,
    agent_s_synth_reports: list[dict[str, Any]] | None = None,
    scalecua_reports: list[dict[str, Any]] | None = None,
    scalecua_synth_reports: list[dict[str, Any]] | None = None,
    subset_ids: list[str] | None,
    rng: random.Random,
    iters: int,
    ci: float,
    expected_models: list[str] | None = None,
    expected_seeds: int | None = None,
    expected_arms: list[str] | None = None,
    run_failures: list[str] | None = None,
    calibration_gate: dict[str, Any] | None = None,
    formal: bool = False,
    expected_headless: bool | None = None,
    expected_task_metadata: dict[str, dict[str, Any]] | None = None,
    expected_game_counts: dict[str, int] | None = None,
    expected_golden_provenance: dict[tuple[str, int], dict[str, str]] | None = None,
    administrative_rmst_tau_ms: dict[str, Any] | None = None,
    expected_api_provider: Any = None,
    expected_observer_model: str | None = None,
    expected_grounder_model: str | None = None,
    expected_judge_model: str | None = None,
    expected_provider_timeout_s: float | None = None,
    expected_retry_max_attempts: int | None = None,
    require_trial_start_gate_schema: bool = False,
    expected_inner_workers: int | None = None,
) -> dict[str, Any]:
    reports_by_arm = {
        "c1_state": state_reports,
        "c1_gui": gui_reports,
        "c1_gui_synth": gui_synth_reports or [],
        "c1_agent_s": agent_s_reports or [],
        "c1_agent_s_synth": agent_s_synth_reports or [],
        "c1_agent_scalecua": scalecua_reports or [],
        "c1_agent_scalecua_synth": scalecua_synth_reports or [],
    }
    indices: dict[str, dict[str, dict[tuple[str, int], dict[str, Any]]]] = {}
    trials: list[dict[str, Any]] = []
    issues = list(run_failures or [])
    if expected_models is not None and not _valid_calibration_gate(
        calibration_gate,
        expected_headless=expected_headless,
        expected_api_provider=expected_api_provider,
        expected_observer_model=expected_observer_model,
        expected_grounder_model=(
            calibration_grounder_model(
                expected_grounder_model,
                observer_model=str(
                    expected_observer_model or "claude-sonnet-5@relay"
                ),
            )
            if expected_grounder_model is not None
            else None
        ),
        expected_provider_timeout_s=expected_provider_timeout_s,
    ):
        issues.append("run-level 10-backend c1-calibration-v4 gate missing or failed")
    for arm, reports in reports_by_arm.items():
        indices[arm], arm_trials, arm_issues = _index_c1_reports(
            arm,
            reports,
            expected_inner_workers=expected_inner_workers,
            formal_report_schema=formal,
        )
        trials.extend(arm_trials)
        issues.extend(arm_issues)
    if expected_retry_max_attempts is not None:
        if expected_retry_max_attempts < 1:
            issues.append("registered retry max_attempts must be positive")
        for trial in trials:
            attempt_count = _integer(trial.get("attempt_count"))
            if (
                attempt_count is not None
                and attempt_count > expected_retry_max_attempts
            ):
                issues.append(
                    f"{trial.get('arm')}/{trial.get('model')}/"
                    f"{trial.get('task_id')}/seed{trial.get('seed')}: "
                    "attempt_count exceeds registered retry max_attempts"
                )

    # Reference replay is a task-level property. A complete model failure in B/D
    # can happen before that arm opens a runtime; inherit the audit from A/C/E
    # instead of misclassifying the model failure as missing harness data.
    for task_id in sorted({trial["task_id"] for trial in trials if trial["task_id"]}):
        task_trials = [trial for trial in trials if trial["task_id"] == task_id]
        reachable_values = {
            trial["reference_reachable"]
            for trial in task_trials
            if isinstance(trial.get("reference_reachable"), bool)
        }
        validity_values = {
            str(trial["validity_class"])
            for trial in task_trials
            if trial.get("validity_class") is not None
        }
        benchmark_values = {
            str(trial["benchmark"])
            for trial in task_trials
            if trial.get("benchmark") is not None
        }
        horizon_values = {
            str(trial["horizon_class"])
            for trial in task_trials
            if trial.get("horizon_class") is not None
        }
        if not reachable_values:
            issues.append(f"{task_id}: no task-level reference reachability audit")
            for trial in task_trials:
                trial["complete"] = False
        elif len(reachable_values) > 1:
            issues.append(f"{task_id}: conflicting task-level reference reachability audits")
        else:
            reachable = next(iter(reachable_values))
            for trial in task_trials:
                if trial.get("reference_reachable") is None:
                    trial["reference_reachable"] = reachable
        if len(validity_values) == 1:
            validity = next(iter(validity_values))
            for trial in task_trials:
                if trial.get("validity_class") is None:
                    trial["validity_class"] = validity
        elif len(validity_values) > 1:
            issues.append(f"{task_id}: conflicting validity classes")

        for field, values in (("benchmark", benchmark_values), ("horizon_class", horizon_values)):
            if not values:
                issues.append(f"{task_id}: missing task-level {field}")
            elif len(values) > 1:
                issues.append(f"{task_id}: conflicting task-level {field}")
            else:
                value = next(iter(values))
                for trial in task_trials:
                    if trial.get(field) is None:
                        trial[field] = value

    align_ids = list(subset_ids or sorted({trial["task_id"] for trial in trials if trial["task_id"]}))
    if formal and not isinstance(expected_task_metadata, dict):
        issues.append("formal C1 requires task metadata from the registered task source")
    if isinstance(expected_task_metadata, dict):
        for task_id in align_ids:
            expected_meta = expected_task_metadata.get(task_id)
            if not isinstance(expected_meta, dict):
                issues.append(f"{task_id}: missing registered task metadata")
                continue
            for field in ("benchmark", "horizon_class", "prompt_revision"):
                expected_value = expected_meta.get(field)
                if not isinstance(expected_value, str) or not expected_value:
                    issues.append(f"{task_id}: registered task metadata missing {field}")
                    continue
                for trial in (row for row in trials if row["task_id"] == task_id):
                    reported = trial.get("reported_task_metadata")
                    actual_value = (
                        reported.get(field) if isinstance(reported, dict) else None
                    )
                    if actual_value != expected_value:
                        issues.append(
                            f"{trial['arm']}/{trial['model']}/{task_id}/seed{trial['seed']}: "
                            f"{field} {actual_value!r} does not match registered "
                            f"value {expected_value!r}"
                        )
                        trial["complete"] = False
            expected_random_control = expected_meta.get("random_control")
            if not isinstance(expected_random_control, dict) or not expected_random_control:
                issues.append(
                    f"{task_id}: registered task metadata missing random_control"
                )
            else:
                for trial in (row for row in trials if row["task_id"] == task_id):
                    label = (
                        f"{trial['arm']}/{trial['model']}/{task_id}/"
                        f"seed{trial['seed']}"
                    )
                    reported = trial.get("reported_task_metadata")
                    actual_control = (
                        reported.get("random_control")
                        if isinstance(reported, dict)
                        else None
                    )
                    evidence = trial.get("seed_evidence")
                    evidence_control = (
                        evidence.get("random_control")
                        if isinstance(evidence, dict)
                        else None
                    )
                    settled_evidence = trial.get("_p5_settled_evidence") is True
                    control_valid = actual_control == expected_random_control
                    fair_prestart_zero = bool(
                        trial.get("start_construction_status")
                        == "synthesis_failed"
                        and _is_valid_synthesized_fair_zero(trial)
                    )
                    if fair_prestart_zero:
                        control_valid = bool(
                            control_valid
                            and evidence_control == expected_random_control
                            and isinstance(evidence, dict)
                            and evidence.get("observed") is False
                            and evidence.get("source") == "runtime_not_started"
                        )
                    elif not settled_evidence:
                        control_valid = bool(
                            control_valid
                            and evidence_control == expected_random_control
                            and trial.get("random_control_evidence_valid") is True
                        )
                    if (
                        control_valid
                        and not settled_evidence
                        and not fair_prestart_zero
                        and expected_random_control.get("mode") == "experiment_seed"
                    ):
                        control_valid = bool(
                            isinstance(evidence, dict)
                            and evidence.get("observed") is True
                            and evidence.get("algorithm")
                            == expected_random_control.get("algorithm")
                            and _integer(evidence.get("seed")) == trial["seed"]
                            and trial["seed"]
                            in (expected_random_control.get("seeds") or [])
                        )
                    elif (
                        control_valid
                        and not settled_evidence
                        and not fair_prestart_zero
                        and expected_random_control.get("mode") == "not_applicable"
                    ):
                        control_valid = bool(
                            isinstance(evidence, dict)
                            and evidence.get("observed") is True
                            and evidence.get("randomness") == "not_applicable"
                            and evidence.get("seed_role") == "trial_pairing_only"
                            and _integer(evidence.get("seed")) == trial["seed"]
                        )
                    if not control_valid:
                        issues.append(
                            f"{label}: random_control or runtime seed evidence "
                            "does not match the registered task contract"
                        )
                        trial["complete"] = False
            capabilities = expected_meta.get("capabilities")
            if (
                not isinstance(capabilities, list)
                or not capabilities
                or not all(
                    isinstance(capability, str) and capability
                    for capability in capabilities
                )
                or len(capabilities) != len(set(capabilities))
            ):
                issues.append(
                    f"{task_id}: registered task metadata has invalid capabilities"
                )
                continue
            for trial in (row for row in trials if row["task_id"] == task_id):
                trial["capabilities"] = list(capabilities)
    formal_game_counts: dict[str, int] | None = None
    if formal:
        registered_game_counts = (
            {str(game): int(count) for game, count in expected_game_counts.items()}
            if isinstance(expected_game_counts, dict) and expected_game_counts
            else dict(_FORMAL_C1_GAME_COUNTS)
        )
        formal_task_count = sum(registered_game_counts.values())
        if len(align_ids) != formal_task_count or len(set(align_ids)) != formal_task_count:
            issues.append(
                "formal C1 subset must contain exactly "
                f"{formal_task_count} unique tasks"
            )
        models = list(expected_models or [])
        if len(models) != 3 or len(set(models)) != 3:
            issues.append("formal C1 requires exactly 3 unique models")
        if expected_seeds != 3:
            issues.append("formal C1 requires exactly 3 seeds")
        if set(expected_arms or []) != set(_C1_ARM_LABELS):
            issues.append("formal C1 requires exactly the five registered arms")
        task_games: dict[str, str] = {
            task_id: str(expected_task_metadata[task_id]["benchmark"])
            for task_id in align_ids
            if isinstance(expected_task_metadata, dict)
            and isinstance(expected_task_metadata.get(task_id), dict)
            and expected_task_metadata[task_id].get("benchmark") is not None
        }
        game_counts = {
            game: sum(value == game for value in task_games.values())
            for game in registered_game_counts
        }
        formal_game_counts = game_counts
        unexpected_games = sorted(set(task_games.values()) - set(registered_game_counts))
        if game_counts != registered_game_counts or unexpected_games:
            issues.append(
                "formal C1 task distribution differs from the registered "
                "c1_benchmark_task_counts"
            )

    issues.extend(_seed_evidence_issues(indices, align_ids))
    issues.extend(_synthesis_accounting_issues(indices, align_ids))
    issues.extend(_start_parity_issues(indices, align_ids))
    if formal:
        golden_artifact_cache = OracleGoldenArtifactValidationCache()
        for trial in trials:
            if require_trial_start_gate_schema:
                issues.extend(_c1_formal_start_gate_issues(trial))
                if not (
                    trial.get("start_construction_status") == "synthesis_failed"
                    and _is_valid_synthesized_fair_zero(trial)
                ) and not _is_state_pre_golden_synthesis_failure(trial):
                    expected_golden = (
                        expected_golden_provenance.get(
                            (str(trial.get("task_id")), int(trial.get("seed")))
                        )
                        if isinstance(expected_golden_provenance, dict)
                        and isinstance(trial.get("seed"), int)
                        else None
                    )
                    if expected_golden is None:
                        issues.append(
                            f"{trial.get('arm')}/{trial.get('model')}/"
                            f"{trial.get('task_id')}/seed{trial.get('seed')}: "
                            "registered oracle golden provenance is missing"
                        )
                    issues.extend(
                        _c1_golden_provenance_issues(
                            trial,
                            expected=expected_golden,
                            artifact_cache=golden_artifact_cache,
                        )
                    )
            issues.extend(_c1_public_semantic_trial_issues(trial))
            issues.extend(_c1_openra_semantic_trial_issues(trial))
            issues.extend(_c1_unity_semantic_trial_issues(trial))
            issues.extend(_c1_native_desktop_semantic_trial_issues(trial))
            issues.extend(_c1_web_semantic_trial_issues(trial))
            issues.extend(
                _c1_cost_module_issues(
                    trial,
                    expected_observer_model=expected_observer_model,
                    expected_grounder_model=expected_grounder_model,
                    expected_judge_model=expected_judge_model,
                )
            )
        if require_trial_start_gate_schema:
            for task_seed in sorted(
                {(trial["task_id"], trial["seed"]) for trial in trials},
                key=str,
            ):
                rows = [
                    trial
                    for trial in trials
                    if (trial["task_id"], trial["seed"]) == task_seed
                ]
                fingerprints = {
                    trial["oracle_goldens"].get("fingerprint")
                    for trial in rows
                    if isinstance(trial.get("oracle_goldens"), dict)
                    and isinstance(
                        trial["oracle_goldens"].get("fingerprint"), str
                    )
                    and trial["oracle_goldens"]["fingerprint"].strip()
                }
                if len(fingerprints) != 1:
                    issues.append(
                        f"{task_seed[0]}/seed{task_seed[1]}: oracle golden "
                        "fingerprints differ across C1 arms/models"
                    )

    requested_taus = administrative_rmst_tau_ms
    aligned = set(align_ids)
    tau_values: dict[str, dict[str, float]] = {
        endpoint: {} for endpoint in (
            "e2e_excluding_synthesis", "e2e_including_synthesis", "termination"
        )
    }
    for horizon, defaults in _DEFAULT_C1_ADMINISTRATIVE_RMST_TAU_MS.items():
        configured = requested_taus.get(horizon) if isinstance(requested_taus, dict) else None
        for endpoint, default in defaults.items():
            value = _number(configured.get(endpoint)) if isinstance(configured, dict) else None
            if value is None or value <= 0:
                if formal:
                    issues.append(
                        "formal C1 requires positive pre-registered RMST tau: "
                        f"{horizon}.{endpoint}"
                    )
                value = float(default)
            tau_values[endpoint][horizon] = value
    gui_common_tau = tau_values["e2e_excluding_synthesis"]
    gui_common_tau_including_synthesis = tau_values["e2e_including_synthesis"]
    gui_common_tau_termination = tau_values["termination"]

    points: list[dict[str, Any]] = []
    for arm, by_model in indices.items():
        for model, index in sorted(by_model.items()):
            point = _method_point(
                index,
                align_ids,
                rng,
                iters=iters,
                ci=ci,
                rmst_tau_by_horizon=gui_common_tau,
                rmst_tau_including_synthesis_by_horizon=(
                    gui_common_tau_including_synthesis
                ),
            )
            point.update({
                "arm": arm,
                "method": _C1_ALL_ARM_LABELS.get(arm, arm),
                "model": model,
            })
            points.append(point)

    expected = {
        (arm, model, seed, task_id)
        for arm in (expected_arms or list(_C1_ARM_LABELS))
        for model in (expected_models or sorted({trial["model"] for trial in trials}))
        for seed in range(expected_seeds if expected_seeds is not None else 1)
        for task_id in align_ids
    }
    actual = {(trial["arm"], trial["model"], trial["seed"], trial["task_id"]) for trial in trials}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected) if expected else []
    if missing:
        issues.append(f"missing {len(missing)} expected C1 trials")
    if unexpected:
        issues.append(f"found {len(unexpected)} unexpected C1 trials")
    if formal and len(actual) != len(expected):
        issues.append(
            f"formal C1 observed trial identity set must equal {len(expected)}, "
            f"got {len(actual)}"
        )

    comparisons: list[dict[str, Any]] = []
    for comparison_id, arm_a, arm_b in _C1_COMPARISONS:
        by_model_a = indices.get(arm_a) or {}
        by_model_b = indices.get(arm_b) or {}
        family_rows: list[dict[str, Any]] = []
        for model in sorted(set(by_model_a) & set(by_model_b)):
            a_index = by_model_a[model]
            b_index = by_model_b[model]
            shared = {
                key for key in set(a_index) & set(b_index)
                if key[0] in aligned
            }
            both_started = {
                key
                for key in shared
                if a_index[key]["start_success"] and b_index[key]["start_success"]
            }

            def paired_binary(field: str, keys: set[tuple[str, int]]) -> tuple[dict[str, Any], dict[str, Any]]:
                a_values = {key: bool(a_index[key][field]) for key in keys}
                b_values = {key: bool(b_index[key][field]) for key in keys}
                return (
                    p5_stats.paired_binary_from_maps(a_values, b_values),
                    p5_stats.clustered_paired_bootstrap_diff(
                        b_values,
                        a_values,
                        iterations=iters,
                        ci=ci,
                        rng=rng,
                    ),
                )

            start_mcnemar, start_effect = paired_binary("start_success", shared)
            goal_both_mcnemar, goal_both_effect = paired_binary(
                "goal_success", both_started
            )
            goal_effect = p5_stats.clustered_conditional_rate_diff(
                {key: b_index[key]["goal_success"] for key in shared},
                {key: b_index[key]["start_success"] for key in shared},
                {key: a_index[key]["goal_success"] for key in shared},
                {key: a_index[key]["start_success"] for key in shared},
                iterations=iters,
                ci=ci,
                rng=rng,
            )
            e2e_mcnemar, e2e_effect = paired_binary("e2e_success", shared)

            def paired_rmst(field: str, tau_by_horizon: dict[str, float]) -> dict[str, Any]:
                return _clustered_paired_administrative_rmst_diff(
                    b_index,
                    a_index,
                    shared,
                    field=field,
                    tau_by_horizon=tau_by_horizon,
                    fallback_tau=None,
                    iterations=iters,
                    ci=ci,
                    rng=rng,
                )

            time_effect = paired_rmst(
                "time_e2e_excluding_synthesis_ms", gui_common_tau
            )
            time_including_effect = paired_rmst(
                "time_e2e_including_synthesis_ms",
                gui_common_tau_including_synthesis,
            )
            termination_effect = paired_rmst(
                "termination_time_ms", gui_common_tau_termination
            )

            token_fields: dict[str, tuple[dict[tuple[str, int], Any], dict[tuple[str, int], Any]]] = {
                "total": (
                    {key: b_index[key].get("total_tokens") for key in shared},
                    {key: a_index[key].get("total_tokens") for key in shared},
                ),
                "agent": (
                    {key: _trial_token_split(b_index[key])[0] for key in shared},
                    {key: _trial_token_split(a_index[key])[0] for key in shared},
                ),
                "evaluation": (
                    {key: _trial_token_split(b_index[key])[1] for key in shared},
                    {key: _trial_token_split(a_index[key])[1] for key in shared},
                ),
                "reasoning_observed": (
                    {key: b_index[key].get("reasoning_tokens_observed") for key in shared},
                    {key: a_index[key].get("reasoning_tokens_observed") for key in shared},
                ),
                "reasoning_lower_bound": (
                    {key: b_index[key].get("reasoning_tokens_lower_bound") for key in shared},
                    {key: a_index[key].get("reasoning_tokens_lower_bound") for key in shared},
                ),
                "reasoning_upper_bound": (
                    {key: b_index[key].get("reasoning_tokens_upper_bound") for key in shared},
                    {key: a_index[key].get("reasoning_tokens_upper_bound") for key in shared},
                ),
                "cached_prompt": (
                    {key: b_index[key].get("cached_prompt_tokens") for key in shared},
                    {key: a_index[key].get("cached_prompt_tokens") for key in shared},
                ),
            }
            token_effects = {
                name: p5_stats.clustered_paired_bootstrap_diff(
                    b_values,
                    a_values,
                    iterations=iters,
                    ci=ci,
                    rng=rng,
                )
                for name, (b_values, a_values) in token_fields.items()
            }
            row = {
                "comparison": comparison_id,
                "arm_a": arm_a,
                "arm_b": arm_b,
                "model": model,
                "shared_trials": len(shared),
                "shared_tasks": len({task_id for task_id, _ in shared}),
                "arm_a_start_successes": sum(a_index[key]["start_success"] for key in shared),
                "arm_b_start_successes": sum(b_index[key]["start_success"] for key in shared),
                "arm_a_goal_successes_given_own_start": sum(
                    a_index[key]["goal_success"]
                    for key in shared
                    if a_index[key]["start_success"]
                ),
                "arm_b_goal_successes_given_own_start": sum(
                    b_index[key]["goal_success"]
                    for key in shared
                    if b_index[key]["start_success"]
                ),
                "both_started_trials": len(both_started),
                "arm_a_goal_successes_given_both_started": sum(
                    a_index[key]["goal_success"] for key in both_started
                ),
                "arm_b_goal_successes_given_both_started": sum(
                    b_index[key]["goal_success"] for key in both_started
                ),
                "arm_a_e2e_successes": sum(a_index[key]["e2e_success"] for key in shared),
                "arm_b_e2e_successes": sum(b_index[key]["e2e_success"] for key in shared),
                "start_mcnemar": start_mcnemar,
                "start_effect_b_minus_a": start_effect,
                "goal_given_start_estimand": (
                    "P_B(goal|B_start)-P_A(goal|A_start); task-cluster bootstrap"
                ),
                "goal_given_start_effect_b_minus_a": goal_effect,
                "goal_given_both_start_mcnemar": goal_both_mcnemar,
                "goal_given_both_start_effect_b_minus_a": goal_both_effect,
                "e2e_mcnemar": e2e_mcnemar,
                "e2e_effect_b_minus_a": e2e_effect,
                "time_effect_method": "task_horizon_administrative_restricted_mean",
                "time_effect_b_minus_a_ms": time_effect,
                "time_including_synthesis_effect_b_minus_a_ms": time_including_effect,
                "token_effect_b_minus_a": token_effects["total"],
                "token_effects_b_minus_a": token_effects,
                "termination_time_effect_b_minus_a_ms": termination_effect,
            }
            family_rows.append(row)
            comparisons.append(row)
        for metric in ("start", "goal_given_both_start", "e2e"):
            adjusted = p5_stats.holm_adjust(
                {
                    row["model"]: row[f"{metric}_mcnemar"]["p_value"]
                    for row in family_rows
                }
            )
            for row in family_rows:
                row[f"{metric}_mcnemar"]["holm_p_value"] = adjusted.get(
                    row["model"]
                )

    per_arm: dict[str, Any] = {}
    for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_gui", "c1_gui_synth"):
        if arm not in _C1_ALL_ARM_LABELS:
            continue
        if arm not in _C1_ARM_LABELS and not any(trial["arm"] == arm for trial in trials):
            continue
        arm_trials = [trial for trial in trials if trial["arm"] == arm]
        starts = sum(1 for trial in arm_trials if trial["start_success"])
        goals = sum(1 for trial in arm_trials if trial["goal_success"])
        e2e = sum(1 for trial in arm_trials if trial["e2e_success"])
        per_arm[_C1_ALL_ARM_LABELS[arm]] = {
            "trials": len(arm_trials),
            "start_success": starts,
            "goal_success": goals,
            "goal_success_given_start": (sum(1 for trial in arm_trials if trial["start_success"] and trial["goal_success"]) / starts) if starts else None,
            "e2e_success": e2e,
        }

    per_arm_model = [
        {
            "arm": point["arm"],
            "method": point["method"],
            "model": point["model"],
            "trials": point["trials"],
            "start_success": point["start_success"],
            "goal_success_given_start": point["goal_success_given_start"],
            "e2e_success": point["e2e_success"],
            "rmst_e2e_ms": point["rmst_e2e_ms"],
            "rmst_e2e_including_synthesis_ms": point[
                "rmst_e2e_including_synthesis_ms"
            ],
            "median_success_time_ms": point["median_success_time_ms"],
            "termination_time_ms": point["termination_time_ms"],
            "mean_time_to_start_ms": point["mean_time_to_start_ms"],
            "mean_time_start_to_goal_ms": point["mean_time_start_to_goal_ms"],
            "mean_time_e2e_excluding_synthesis_ms": point[
                "mean_time_e2e_excluding_synthesis_ms"
            ],
            "mean_time_e2e_including_synthesis_ms": point[
                "mean_time_e2e_including_synthesis_ms"
            ],
            "mean_tokens": point["mean_tokens"],
            "mean_agent_tokens": point["mean_agent_tokens"],
            "mean_evaluation_tokens": point["mean_evaluation_tokens"],
            "mean_reasoning_tokens": point["mean_reasoning_tokens"],
            "mean_reasoning_tokens_observed": point["mean_reasoning_tokens_observed"],
            "mean_reasoning_tokens_lower_bound": point["mean_reasoning_tokens_lower_bound"],
            "mean_reasoning_tokens_upper_bound": point["mean_reasoning_tokens_upper_bound"],
            "mean_cached_prompt_tokens": point["mean_cached_prompt_tokens"],
            "cost_by_kind": point["cost_by_kind"],
            "cost_by_model": point["cost_by_model"],
            "cost_by_kind_model": point["cost_by_kind_model"],
            "synthesis_actual_cost": point["synthesis_actual_cost"],
            "synthesis_transient_actual_cost": point[
                "synthesis_transient_actual_cost"
            ],
            "synthesis_attributed_cost": point["synthesis_attributed_cost"],
            "actual_cost": point["actual_cost"],
            "transient_infrastructure_cost": point[
                "transient_infrastructure_cost"
            ],
        }
        for point in points
    ]
    # Arm E synthesizes and caches its checkpoint on the same terms as arm D, so
    # it belongs in the synthesis spend accounting rather than being excluded.
    synthesizing_trials = [
        trial for trial in trials if trial["arm"] != "c1_agent_s"
    ]
    synthesis_cost_accounting = {
        "primary_arm_cost_basis": (
            "trial.cost with per-arm counterfactual synthesis attribution"
        ),
        "actual_provider_incurred_semantics": (
            "sum synthesis_actual_cost for every paid provider attempt; transient "
            "infrastructure overhead is retained separately and excluded from the "
            "counterfactual arm-comparison burden"
        ),
        "counterfactual_attributed_semantics": (
            "each synthesized arm receives the same synthesis burden for a fair "
            "within-policy comparison"
        ),
        "actual_provider_incurred": _sum_cost_summary_field(
            synthesizing_trials, "synthesis_actual_cost"
        ),
        "transient_infrastructure_overhead": _sum_cost_summary_field(
            synthesizing_trials, "synthesis_transient_actual_cost"
        ),
        "counterfactual_attributed_total": _sum_cost_summary_field(
            synthesizing_trials, "synthesis_attributed_cost"
        ),
        "by_arm_model": [
            {
                "arm": row["arm"],
                "method": row["method"],
                "model": row["model"],
                "actual_provider_incurred": row["synthesis_actual_cost"],
                "transient_infrastructure_overhead": row[
                    "synthesis_transient_actual_cost"
                ],
                "counterfactual_attributed": row["synthesis_attributed_cost"],
            }
            for row in per_arm_model
        ],
        "all_provider_attempts": _sum_cost_summary_field(
            trials, "actual_cost"
        ),
        "retry_transient_infrastructure_overhead": _sum_cost_summary_field(
            trials, "transient_infrastructure_cost"
        ),
        "primary_treatment_burden": _sum_cost_summary_field(trials, "cost"),
        "all_provider_attempts_semantics": (
            "every usage-complete provider attempt; primary treatment burden "
            "and retry transient infrastructure overhead are reported separately"
        ),
    }
    reasoning_by_model = _sum_nested_cost_rows(trials, "by_model")
    reasoning_detail_audit: dict[str, Any] = {
        "schema": "c1-usage-accounting-v2",
        "scope": "provider_canonical_model",
        "provider": expected_api_provider,
        "tolerance_fraction": cost.REASONING_DETAIL_TOLERANCE_FRACTION,
        "models": {
            model: {
                key: bucket.get(key)
                for key in (
                    "reasoning_detail_complete",
                    "reasoning_detail_missing_calls",
                    "reasoning_detail_unknown_completion_tokens",
                    "reasoning_detail_expected_completion_tokens",
                    "reasoning_detail_unknown_fraction",
                    "reasoning_detail_tolerance_fraction",
                    "reasoning_detail_within_tolerance",
                    "reasoning_tokens_observed",
                    "reasoning_tokens_lower_bound",
                    "reasoning_tokens_upper_bound",
                )
            }
            for model, bucket in sorted(reasoning_by_model.items())
        },
    }
    reasoning_detail_audit["affected_models"] = [
        model
        for model, bucket in sorted(reasoning_by_model.items())
        if (bucket.get("reasoning_detail_missing_calls") or 0) > 0
    ]
    reasoning_detail_audit["above_tolerance_models"] = [
        model
        for model, bucket in sorted(reasoning_by_model.items())
        if bucket.get("reasoning_detail_within_tolerance") is False
    ]
    reasoning_detail_audit["within_tolerance"] = not reasoning_detail_audit[
        "above_tolerance_models"
    ]

    return {
        "complete": not issues and bool(expected),
        "formal": formal,
        "incomplete_reasons": issues,
        "coverage": {
            "expected_trials": len(expected),
            "observed_trials": len(actual & expected) if expected else len(actual),
            "missing_trials": len(missing),
            "unexpected_trials": len(unexpected),
            **({
                "formal_target_trials": len(expected),
                "formal_game_counts": formal_game_counts,
            } if formal else {}),
        },
        "x_axis": "total_tokens_and_time",
        "y_axis": "e2e_success",
        "time_analysis": {
            "primary": "rmst_e2e_ms",
            "event": "e2e_success_only",
            "censoring": (
                "every non-success, including early model failure, and every "
                "completion after its task-horizon restriction is censored at the "
                "pre-registered administrative tau"
            ),
            "gui_tau_rule": (
                "manifest-pre-registered administrative horizon, independent of outcomes"
            ),
            "tau_by_horizon_ms": gui_common_tau,
            "tau_including_synthesis_by_horizon_ms": gui_common_tau_including_synthesis,
            "termination_tau_by_horizon_ms": gui_common_tau_termination,
            "state_tau_rule": (
                "same manifest-pre-registered administrative horizon as GUI Pareto"
            ),
            "state_tau_by_horizon_ms": gui_common_tau,
            "state_tau_including_synthesis_by_horizon_ms": (
                gui_common_tau_including_synthesis
            ),
            "successful_time_summary": "median_success_time_ms",
            "termination_time_summary": "all-trial task-cluster mean diagnostic",
        },
        "align_on": "c1_task_file" if subset_ids else "observed_union",
        "align_task_ids": align_ids,
        "points": points,
        "pareto": _pareto_summary(points),
        "same_start": {
            "metric": "start_success / goal_success_given_start / e2e_success",
            "per_arm": per_arm,
            "per_arm_is_pooled_counts_diagnostic": True,
            "per_arm_model": per_arm_model,
            "comparisons": comparisons,
            "state_managed_excluded": True,
        },
        "cost_accounting": synthesis_cost_accounting,
        "reasoning_detail_audit": reasoning_detail_audit,
        "sensitivity": _c1_sensitivity(
            indices,
            align_ids,
            rng,
            iters=iters,
            ci=ci,
            rmst_tau=gui_common_tau,
        ),
        "trials": sorted(
            (
                {
                    key: value
                    for key, value in row.items()
                    if key != "_p5_settled_evidence"
                }
                for row in trials
            ),
            key=lambda row: (
                row["arm"],
                row["model"],
                row["seed"],
                row["task_id"],
            ),
        ),
    }


# --------------------------------------------------------------------------- C2


def _c2_memory_payload_issues(report: Any) -> list[str]:
    """Validate the effect-bearing portion of a C2 memory report."""

    issues: list[str] = []
    if not isinstance(report, dict):
        return ["report payload is not an object"]
    if report.get("experiment") != "memory_baseline_transfer_suite":
        issues.append("report experiment is not memory_baseline_transfer_suite")
    validity = report.get("formal_validity")
    if not isinstance(validity, dict) or validity.get("status") != "valid":
        issues.append("formal_validity.status is not valid")

    summary = report.get("summary")
    if not isinstance(summary, dict):
        issues.append("report summary is missing")
    else:
        if summary.get("anchor_present") is not True:
            issues.append("report summary has no anchor result")
        if _number(summary.get("anchor_heldout_strict_success")) is None:
            issues.append("anchor held-out strict success is not numeric")
        deltas = summary.get("anchor_delta_vs_baseline")
        if not isinstance(deltas, dict) or not deltas:
            issues.append("anchor baseline deltas are missing")
        elif any(_number(value) is None for value in deltas.values()):
            issues.append("anchor baseline deltas contain a nonnumeric value")
    return issues


def _c2_memory_report_issues(
    entry: dict[str, Any],
    report_raw: bytes,
    report: Any,
    run_index: dict[str, Any],
) -> list[str]:
    """Fail closed before an indexed C2 report contributes an effect size."""

    issues: list[str] = []
    expected_report_sha = entry.get("report_sha256")
    actual_report_sha = hashlib.sha256(report_raw).hexdigest()
    if not _is_sha256(expected_report_sha):
        issues.append("missing or invalid report_sha256")
    elif expected_report_sha != actual_report_sha:
        issues.append("report_sha256 mismatch")
    if not isinstance(report, dict):
        return issues + _c2_memory_payload_issues(report)
    expected_harness_hash = run_index.get("c2_harness_hash")
    context = report.get("run_context")
    actual_harness_hash = (
        context.get("c2_harness_hash") if isinstance(context, dict) else None
    )
    if not _is_sha256(expected_harness_hash):
        issues.append("run index is missing a valid c2_harness_hash")
    elif actual_harness_hash != expected_harness_hash:
        issues.append("report c2_harness_hash does not match the run index")
    # c2_manifest_fingerprint isolates just the C2 arm/models/seeds/api/retry
    # manifest fields (see c2_provenance.c2_manifest_fingerprint). Checking it
    # alongside c2_harness_hash catches config drift even if a future harness
    # change accidentally stopped covering the manifest, instead of silently
    # aggregating a report produced under a different C2 configuration.
    expected_fingerprint = run_index.get("c2_manifest_fingerprint")
    actual_fingerprint = (
        context.get("c2_manifest_fingerprint") if isinstance(context, dict) else None
    )
    if not _is_sha256(expected_fingerprint):
        issues.append("run index is missing a valid c2_manifest_fingerprint")
    elif actual_fingerprint != expected_fingerprint:
        issues.append("report c2_manifest_fingerprint does not match the run index")
    expected_profiles = run_index.get("model_request_profiles")
    expected_profiles_sha256 = run_index.get("model_request_profiles_sha256")
    if not isinstance(expected_profiles, dict):
        issues.append("run index is missing model_request_profiles")
    elif context.get("model_request_profiles") != expected_profiles:
        issues.append("report model_request_profiles do not match the run index")
    if not _is_sha256(expected_profiles_sha256):
        issues.append("run index is missing a valid model_request_profiles_sha256")
    elif context.get("model_request_profiles_sha256") != expected_profiles_sha256:
        issues.append(
            "report model_request_profiles_sha256 does not match the run index"
        )
    clean_model = _clean_model(entry.get("model"))
    if clean_model is None or not isinstance(expected_profiles, dict):
        issues.append("cannot resolve the indexed C2 model request profile")
    else:
        configured_profile = expected_profiles.get(clean_model, PROVIDER_DEFAULT)
        expected_profile = ModelRequestProfile(
            clean_model, str(configured_profile)
        ).provenance()
        if context.get("model_request_profile") != expected_profile:
            issues.append("report model_request_profile does not match the run index")
    return issues + _c2_memory_payload_issues(report)


def c2_from_reports(
    phase3_reports: list[dict[str, Any]],
    memory_reports: list[dict[str, Any]],
    *,
    rng: random.Random,
    iters: int,
    ci: float,
    run_failures: list[str] | None = None,
    expected_memory_reports: int | None = None,
) -> dict[str, Any]:
    incomplete_reasons = list(run_failures or [])
    # Transfer gain vs A5 from the phase3 suite (per model), then aggregated.
    a5_vals: list[float] = []
    a9_vals: list[float] = []
    gain_vals: list[float] = []
    phase3_rows: list[dict[str, Any]] = []
    for rep in phase3_reports:
        model = str(rep.get("run_context", {}).get("model") or "model")
        rows = {str(r.get("id")): r for r in rep.get("summary", {}).get("variants", [])}
        a5 = rows.get("A5", {})
        a9 = rows.get("A9", {})
        a5_strict = float(a5.get("heldout_strict_success", 0) or 0)
        a9_strict = float(a9.get("heldout_strict_success", 0) or 0)
        gain = float(a9.get("transfer_gain_vs_A5", a9_strict - a5_strict) or 0)
        a5_vals.append(a5_strict)
        a9_vals.append(a9_strict)
        gain_vals.append(gain)
        phase3_rows.append(
            {
                "model": model,
                "A5_heldout_strict": a5_strict,
                "A9_heldout_strict": a9_strict,
                "transfer_gain_vs_A5": gain,
                "heldout_tasks": a9.get("heldout_tasks") or a5.get("heldout_tasks"),
            }
        )

    # Anchor vs A-MEM / Mem0 from the memory-baseline suite (per model).
    anchor_vals: list[float] = []
    baseline_deltas: dict[str, list[float]] = {}
    memory_rows: list[dict[str, Any]] = []
    valid_memory_reports: list[dict[str, Any]] = []
    for report_index, rep in enumerate(memory_reports):
        payload_issues = _c2_memory_payload_issues(rep)
        if payload_issues:
            incomplete_reasons.extend(
                f"C2 memory report {report_index}: {issue}"
                for issue in payload_issues
            )
            continue
        valid_memory_reports.append(rep)
    for rep in valid_memory_reports:
        model = str(rep.get("run_context", {}).get("model") or "model")
        summary = rep.get("summary", {})
        anchor_strict = summary.get("anchor_heldout_strict_success")
        if anchor_strict is not None:
            anchor_vals.append(float(anchor_strict))
        deltas = summary.get("anchor_delta_vs_baseline", {}) or {}
        for backend, delta in deltas.items():
            baseline_deltas.setdefault(backend, []).append(float(delta))
        memory_rows.append(
            {
                "model": model,
                "anchor_backend": rep.get("anchor_backend"),
                "anchor_heldout_strict": anchor_strict,
                "anchor_delta_vs_baseline": deltas,
            }
        )

    if (
        expected_memory_reports is not None
        and len(valid_memory_reports) != expected_memory_reports
    ):
        incomplete_reasons.append(
            "C2 memory-baseline coverage mismatch: expected "
            f"{expected_memory_reports} valid reports, observed {len(valid_memory_reports)}"
        )

    return {
        "complete": not incomplete_reasons,
        "incomplete_reasons": incomplete_reasons,
        "memory_baseline_coverage": {
            "expected_reports": expected_memory_reports,
            "valid_reports": len(valid_memory_reports),
            "excluded_reports": max(
                0,
                (expected_memory_reports or len(memory_reports))
                - len(valid_memory_reports),
            ),
        },
        "primary_metric": "heldout_strict_success",
        "phase3_rows": phase3_rows,
        "phase3_aggregate": {
            "A5_heldout_strict": p5_stats.mean_ci(a5_vals, iterations=iters, ci=ci, rng=rng),
            "A9_heldout_strict": p5_stats.mean_ci(a9_vals, iterations=iters, ci=ci, rng=rng),
            "transfer_gain_vs_A5": p5_stats.mean_ci(gain_vals, iterations=iters, ci=ci, rng=rng),
            "transfer_gain_permutation": p5_stats.paired_permutation_test(a9_vals, a5_vals, iterations=iters, rng=rng),
        },
        "memory_baseline_rows": memory_rows,
        "memory_baseline_aggregate": {
            "anchor_heldout_strict": p5_stats.mean_ci(anchor_vals, iterations=iters, ci=ci, rng=rng),
            "anchor_minus_baseline": {
                backend: p5_stats.mean_ci(vals, iterations=iters, ci=ci, rng=rng)
                for backend, vals in sorted(baseline_deltas.items())
            },
        },
    }


# --------------------------------------------------------------------------- C3


def c3_from_reports(
    fuzz_reports: list[dict[str, Any]],
    *,
    rng: random.Random,
    iters: int,
    ci: float,
    run_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # Unique defects are deduped by fingerprint ACROSS all seeds/reports.
    all_results: list[dict[str, Any]] = []
    raw_defects_per_report: list[float] = []
    unique_per_report: list[float] = []
    defect_types: dict[str, int] = {}
    defect_paths: dict[str, int] = {}
    by_benchmark_unique: dict[str, set[str]] = {}
    raw_by_reachability = {"player-reachable": 0, "injection-only": 0, "unclassified": 0}
    # A verified player-reachable reproduction is stronger evidence than a prior
    # injection-only observation of the same fingerprint.  Old reports did not
    # carry reachability and intentionally remain visible as ``unclassified``.
    reachability_by_fingerprint: dict[str, str] = {}
    reachability_rank = {"unclassified": 0, "injection-only": 1, "player-reachable": 2}
    task_failures: list[dict[str, str]] = []
    for report_index, rep in enumerate(fuzz_reports):
        results = rep.get("results")
        if not isinstance(results, list) or not results:
            task_failures.append(
                {
                    "report": str(report_index),
                    "task_id": "<none>",
                    "reason": "fuzz report contains no task results",
                }
            )
            results = []
        for result in results:
            if not isinstance(result, dict):
                task_failures.append({"report": str(report_index), "task_id": "<malformed>", "reason": "malformed task result"})
                continue
            # Older reports did not carry per-task status; preserve their
            # aggregate compatibility while never hiding an explicit failure.
            if result.get("status") not in (None, "success"):
                task_failures.append(
                    {
                        "report": str(report_index),
                        "task_id": str(result.get("task_id") or "<unknown-task>"),
                        "reason": str(result.get("reason") or result.get("status")),
                    }
                )
            all_results.append(result)
        summary = rep.get("summary", {})
        raw_defects_per_report.append(float(summary.get("defects", 0) or 0))
        unique_per_report.append(float(summary.get("unique_defects", 0) or 0))

    run_failures: list[dict[str, str]] = []
    expected_reports = len(fuzz_reports)
    if run_entries is not None:
        expected_reports = 0
        for index, entry in enumerate(run_entries):
            if not isinstance(entry, dict):
                run_failures.append({"run": str(index), "reason": "malformed P5 run-index entry"})
                continue
            status = entry.get("status")
            if status not in (None, "ok"):
                run_failures.append(
                    {
                        "run": str(index),
                        "reason": str(entry.get("error") or f"P5 arm status {status}"),
                    }
                )
                continue
            if not entry.get("report"):
                run_failures.append({"run": str(index), "reason": "P5 arm has no fuzz report path"})
                continue
            expected_reports += 1
        if len(fuzz_reports) != expected_reports:
            run_failures.append(
                {
                    "run": "reports",
                    "reason": f"loaded {len(fuzz_reports)} of {expected_reports} expected C3 fuzz reports",
                }
            )

    # Global dedupe + cumulative discovery curve.
    seen: set[str] = set()
    curve: list[dict[str, int]] = []
    raw_index = 0
    for result in all_results:
        bench = str(result.get("benchmark"))
        for mut in result.get("mutations", []) or []:
            if mut.get("verdict") != "defect":
                continue
            raw_index += 1
            fp = mut.get("fingerprint")
            dt = mut.get("defect_type") or "unknown"
            path = mut.get("defect_path") or "browser"
            reachability = str(mut.get("reachability") or "unclassified")
            if reachability not in raw_by_reachability:
                reachability = "unclassified"
            defect_types[dt] = defect_types.get(dt, 0) + 1
            defect_paths[path] = defect_paths.get(path, 0) + 1
            raw_by_reachability[reachability] += 1
            if fp:
                by_benchmark_unique.setdefault(bench, set()).add(fp)
                if fp not in seen:
                    seen.add(fp)
                previous = reachability_by_fingerprint.get(fp, "unclassified")
                if reachability_rank[reachability] >= reachability_rank[previous]:
                    reachability_by_fingerprint[fp] = reachability
            curve.append({"raw_defects": raw_index, "unique_defects": len(seen)})

    unique_by_reachability = {"player-reachable": 0, "injection-only": 0, "unclassified": 0}
    for reachability in reachability_by_fingerprint.values():
        unique_by_reachability[reachability] += 1

    return {
        "primary_metric": "unique_defects",
        "complete": not task_failures and not run_failures,
        "task_failures": task_failures,
        "run_failures": run_failures,
        "coverage": {"loaded_reports": len(fuzz_reports), "expected_reports": expected_reports},
        "unique_defects_total": len(seen),
        "raw_defects_total": raw_index,
        "defect_types": defect_types,
        "defect_paths": defect_paths,
        "raw_by_reachability": raw_by_reachability,
        "unique_by_reachability": unique_by_reachability,
        "unique_by_benchmark": {b: len(s) for b, s in sorted(by_benchmark_unique.items())},
        "cumulative_curve": curve,
        "per_seed": {
            "raw_defects": p5_stats.mean_ci(raw_defects_per_report, iterations=iters, ci=ci, rng=rng),
            "unique_defects": p5_stats.mean_ci(unique_per_report, iterations=iters, ci=ci, rng=rng),
            "reports": len(fuzz_reports),
        },
    }


# ----------------------------------------------------------------------- driver


def _resolve(report_root: Path, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (report_root / p)


def _visual_bridge_provenance(
    run_index: dict[str, Any], manifest: dict[str, Any] | None
) -> dict[str, Any]:
    raw = run_index.get("visual_bridge")
    if not isinstance(raw, dict):
        raw = (manifest or {}).get("visual_bridge")
    if not isinstance(raw, dict):
        return {}
    result = {
        field: raw.get(field)
        for field in ("observer_model", "grounder_model", "base_url")
        if raw.get(field) is not None
    }
    if "base_url" not in result:
        provider = run_index.get("api_provider")
        if isinstance(provider, dict) and provider.get("base_url"):
            result["base_url"] = provider["base_url"]
    return result


def c1_minimal_agent_diagnostic_from_reports(
    state_reports: list[dict[str, Any]],
    minimal_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize the non-formal E-vs-E-prime diagnostic without pooling it into C1."""

    def rows(reports: list[dict[str, Any]]) -> dict[tuple[str, int, str], dict[str, Any]]:
        indexed: dict[tuple[str, int, str], dict[str, Any]] = {}
        for report in reports:
            if not isinstance(report, dict):
                continue
            model = str(report.get("model") or report.get("synthesis_model") or "")
            seed = _integer(report.get("seed"))
            if seed is None:
                continue
            for result in report.get("results") or []:
                if not isinstance(result, dict) or not result.get("task_id"):
                    continue
                indexed[(model, seed, str(result["task_id"]))] = result
        return indexed

    protocol_rows = {
        (
            str(report.get("schema") or "c1-state-minimal-agent-v1"),
            int(report.get("schema_version", 1) or 1),
        )
        for report in minimal_reports
        if isinstance(report, dict)
    }
    if len(protocol_rows) > 1:
        raise ValueError(
            "C1 E-prime diagnostic cannot mix minimal-agent-v1 and "
            "source-transition-agent-v2 reports"
        )
    protocol = next(
        iter(protocol_rows), ("c1-state-minimal-agent-v1", 1)
    )
    source_v2 = protocol == ("c1-state-source-agent-v2", 2)
    planner_field = (
        "source_transition_agent" if source_v2 else "minimal_smoke_agent"
    )
    state = rows(state_reports)
    minimal = rows(minimal_reports)
    paired_keys = sorted(set(state) & set(minimal))
    state_only_keys = sorted(set(state) - set(minimal))
    eprime_only_keys = sorted(set(minimal) - set(state))
    complete_minimal = [
        row for row in minimal.values() if row.get("incomplete") is not True
    ]
    core_invalid_pairs: list[dict[str, Any]] = []
    audit_warnings: list[dict[str, Any]] = []
    treatment_eligible_pairs = 0
    if source_v2:
        for key in state_only_keys:
            core_invalid_pairs.append(
                {
                    "model": key[0],
                    "seed": key[1],
                    "task_id": key[2],
                    "failed_fields": ["eprime_pair_missing"],
                }
            )
        for key in eprime_only_keys:
            core_invalid_pairs.append(
                {
                    "model": key[0],
                    "seed": key[1],
                    "task_id": key[2],
                    "failed_fields": ["state_pair_missing"],
                }
            )
        for key in paired_keys:
            left = state[key]
            right = minimal[key]
            failed: list[str] = []
            warnings: list[str] = []
            accepted_left = left.get("accepted_start_id") or left.get(
                "pair_start_id"
            )
            accepted_right = right.get("accepted_start_id") or right.get(
                "pair_start_id"
            )
            if accepted_left is None or accepted_right is None:
                # Older v2 reports predate the stable run-scoped ID. Their
                # already-paired (model, seed, task_id) key remains usable, but
                # the missing convenience identity is disclosed.
                warnings.append("accepted_start_id_missing_legacy_fallback")
            elif accepted_left != accepted_right:
                failed.append("accepted_start_id_mismatch")

            for field in (
                "before_semantic_ok",
                "before_visual_ok",
                "start_success",
            ):
                left_value = left.get(field)
                right_value = right.get(field)
                if not isinstance(left_value, bool) or not isinstance(
                    right_value, bool
                ):
                    failed.append(f"{field}_missing")
                elif left_value != right_value:
                    failed.append(f"{field}_mismatch")

            if left.get("incomplete") is True or right.get("incomplete") is True:
                failed.append("incomplete_trial")
            for label, row in (("state", left), ("eprime", right)):
                for field in ("goal_success", "e2e_success"):
                    if not isinstance(row.get(field), bool):
                        failed.append(f"{label}_{field}_missing")
                if all(
                    isinstance(row.get(field), bool)
                    for field in ("start_success", "goal_success", "e2e_success")
                ) and row["e2e_success"] != bool(
                    row["start_success"] and row["goal_success"]
                ):
                    failed.append(f"{label}_e2e_success_inconsistent")

                row_cost = row.get("cost")
                if not isinstance(row_cost, dict):
                    failed.append(f"{label}_usage_evidence_missing")
                elif row_cost.get("usage_complete") is not True:
                    failed.append(f"{label}_usage_incomplete")

            planner = right.get(planner_field)
            planner = planner if isinstance(planner, dict) else {}
            planner_status = planner.get("status")
            planner_called = right.get("planner_called")
            if not isinstance(planner_called, bool):
                failed.append("planner_called_missing")
                planner_called = planner.get("status") not in {
                    None,
                    "not_called",
                }
            elif planner_called != (planner_status not in {None, "not_called"}):
                failed.append("planner_called_status_mismatch")
            planner_after_start = right.get("planner_called_after_start")
            if planner_called and not isinstance(planner_after_start, bool):
                failed.append("planner_called_after_start_missing")
            if planner_called and (
                right.get("start_success") is not True
                or planner_after_start is not True
            ):
                failed.append("planner_called_before_start_pass")

            action_started = right.get("action_execution_started")
            if not isinstance(action_started, bool):
                failed.append("action_execution_started_missing")
                action_started = False
            if action_started and right.get("start_success") is not True:
                failed.append("action_execution_before_start_pass")
            if action_started and planner_status not in {"generated", "repaired"}:
                failed.append("action_execution_without_usable_plan")
            if (
                right.get("start_success") is True
                and planner_status in {"generated", "repaired"}
                and action_started is not True
            ):
                failed.append("action_execution_not_started")

            if left.get("start_success") is True:
                treatment_eligible_pairs += 1

            # Content/file digests are optional tamper diagnostics. Missing
            # values must never turn an otherwise complete treatment into an
            # invalid comparison.
            for label, value in (
                ("pair_start_fingerprint_state", left.get("pair_start_fingerprint")),
                ("pair_start_fingerprint_eprime", right.get("pair_start_fingerprint")),
                (
                    "original_generated_actions_sha256",
                    right.get("original_generated_actions_sha256"),
                ),
                ("pair_start_artifact_state", left.get("pair_start_artifact_path")),
                ("pair_start_artifact_eprime", right.get("pair_start_artifact_path")),
            ):
                if value in {None, ""}:
                    warnings.append(f"optional_{label}_missing")

            if failed:
                core_invalid_pairs.append(
                    {
                        "model": key[0],
                        "seed": key[1],
                        "task_id": key[2],
                        "failed_fields": sorted(set(failed)),
                    }
                )
            if warnings:
                audit_warnings.append(
                    {
                        "model": key[0],
                        "seed": key[1],
                        "task_id": key[2],
                        "warnings": sorted(set(warnings)),
                    }
                )
    comparison_valid = source_v2 and not core_invalid_pairs and bool(paired_keys)
    return {
        "schema": (
            "c1-state-source-agent-diagnostic-v2"
            if source_v2
            else "c1-state-minimal-agent-diagnostic-v1"
        ),
        "schema_version": 2 if source_v2 else 1,
        "source_report_protocol": {
            "schema": protocol[0],
            "schema_version": protocol[1],
        },
        "formal": False,
        "excluded_from_formal": True,
        "validity_protocol": (
            "c1-minimal-core-outcome-validity-v1" if source_v2 else None
        ),
        "comparison_valid": comparison_valid if source_v2 else None,
        "paired_goal_inference_allowed": comparison_valid if source_v2 else None,
        "core_audit": (
            {
                "paired_tasks": len(paired_keys),
                "state_only_tasks": len(state_only_keys),
                "eprime_only_tasks": len(eprime_only_keys),
                "treatment_eligible_pairs": treatment_eligible_pairs,
                "invalid_pair_count": len(core_invalid_pairs),
                "invalid_pairs": core_invalid_pairs,
                "audit_warning_count": sum(
                    len(row["warnings"]) for row in audit_warnings
                ),
                "audit_warnings": audit_warnings,
                "sha_policy": "optional_diagnostic_never_comparison_gate",
                "legacy_pair_id_fallback": "model_seed_task_id",
            }
            if source_v2
            else None
        ),
        "total_trials": len(minimal),
        "complete_trials": len(complete_minimal),
        "incomplete_trials": sum(
            row.get("incomplete") is True for row in minimal.values()
        ),
        "start_successes": sum(
            row.get("start_success") is True for row in minimal.values()
        ),
        "action_plan_generated": sum(
            isinstance(row.get(planner_field), dict)
            and row[planner_field].get("status")
            in {"generated", "repaired"}
            for row in minimal.values()
        ),
        "auxiliary_assertions_generated": sum(
            source_v2
            and isinstance(row.get(planner_field), dict)
            and bool(row[planner_field].get("assertions"))
            for row in minimal.values()
        ),
        "smoke_passes": sum(
            row.get("smoke_action_verdict") == "pass" for row in minimal.values()
        ),
        "e2e_successes": sum(
            row.get("e2e_success") is True for row in minimal.values()
        ),
        "paired_with_state": {
            "n": len(paired_keys),
            "state_e2e_successes": sum(
                state[key].get("e2e_success") is True for key in paired_keys
            ),
            "minimal_e2e_successes": sum(
                minimal[key].get("e2e_success") is True for key in paired_keys
            ),
            "minimal_only_successes": sum(
                minimal[key].get("e2e_success") is True
                and state[key].get("e2e_success") is not True
                for key in paired_keys
            ),
            "state_only_successes": sum(
                state[key].get("e2e_success") is True
                and minimal[key].get("e2e_success") is not True
                for key in paired_keys
            ),
        },
    }


def _visual_judge_provenance(
    run_index: dict[str, Any], manifest: dict[str, Any] | None
) -> dict[str, Any]:
    indexed = run_index.get("visual_judge")
    configured = (manifest or {}).get("visual_judge")
    indexed = indexed if isinstance(indexed, dict) else {}
    configured = configured if isinstance(configured, dict) else {}
    model = (
        indexed.get("model")
        or run_index.get("visual_judge_model")
        or configured.get("model")
        or (manifest or {}).get("visual_judge_model")
    )
    base_url = (
        indexed.get("base_url")
        or run_index.get("visual_judge_base_url")
        or configured.get("base_url")
    )
    return {
        field: value
        for field, value in (("model", model), ("base_url", base_url))
        if value is not None
    }



def _c1_tasks_path_from_manifest(manifest: dict[str, Any]) -> str | None:
    arms = manifest.get("arms", {}) if isinstance(manifest.get("arms"), dict) else {}
    for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state", "c1_gui", "c1_gui_synth"):
        cfg = arms.get(arm)
        if isinstance(cfg, dict) and isinstance(cfg.get("tasks"), str) and cfg["tasks"]:
            return str(cfg["tasks"])
    return None


def _c1_subset_from_manifest(manifest: dict[str, Any]) -> list[Any] | None:
    arms = manifest.get("arms", {}) if isinstance(manifest.get("arms"), dict) else {}
    for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state", "c1_gui", "c1_gui_synth"):
        cfg = arms.get(arm)
        if isinstance(cfg, dict) and isinstance(cfg.get("subset"), list):
            return cfg["subset"]
    return None


def _expected_c1_task_set_provenance(
    run_index: dict[str, Any],
    manifest: dict[str, Any] | None,
    report_root: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    """Recompute the selected task identity from the committed manifest source."""

    if not isinstance(manifest, dict):
        return None, None
    tasks_path = _c1_tasks_path_from_manifest(manifest)
    if not isinstance(tasks_path, str):
        return None, "C1 manifest is missing the registered task source"
    source = _resolve(report_root, tasks_path).resolve()
    canonical = (report_root / "benchmarks/p5/c1_tasks.yaml").resolve()
    if (
        int(manifest.get("c1_task_count", 0) or 0) > 0
        and canonical.is_file()
        and source != canonical
    ):
        return None, "formal C1 manifest does not use benchmarks/p5/c1_tasks.yaml"
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except Exception as error:
        return None, f"cannot read registered C1 task source: {error}"
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list):
        return None, "registered C1 task source has no tasks list"
    by_id = {
        str(task.get("id")): task
        for task in tasks
        if isinstance(task, dict) and task.get("id")
    }
    registered_ids = [
        str(task.get("id"))
        for task in tasks
        if isinstance(task, dict) and task.get("id")
    ]
    if len(registered_ids) != len(tasks) or len(registered_ids) != len(set(registered_ids)):
        return None, "registered C1 task source has missing or duplicate ids"
    selected_ids = [str(task_id) for task_id in (run_index.get("task_ids") or [])]
    if not selected_ids:
        selected_ids = registered_ids
    if any(task_id not in by_id for task_id in selected_ids):
        return None, "run index selects tasks outside the registered C1 task source"
    selected_tasks = [by_id[task_id] for task_id in selected_ids]
    blob = json.dumps(
        {"tasks": selected_tasks},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return (
        {
            "task_ids": selected_ids,
            "task_metadata": {
                task_id: {
                    "benchmark": str(by_id[task_id].get("benchmark") or ""),
                    "horizon_class": str(by_id[task_id].get("horizon_class") or ""),
                    "capabilities": list(by_id[task_id].get("capabilities") or []),
                    "prompt_revision": str(
                        by_id[task_id].get("prompt_revision") or ""
                    ),
                    "random_control": copy.deepcopy(
                        by_id[task_id].get("random_control")
                    ),
                }
                for task_id in selected_ids
            },
            "source_task_file": str(source),
            "source_task_file_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "task_set_hash": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16],
        },
        None,
    )


def _expected_c1_golden_provenance(
    run_index: dict[str, Any],
    manifest: dict[str, Any] | None,
    report_root: Path,
) -> tuple[dict[tuple[str, int], dict[str, str]] | None, list[str]]:
    """Recompute golden identity from each exact seeded spec and child repo HEAD."""

    if not isinstance(manifest, dict):
        return None, ["formal C1 cannot derive registered oracle golden provenance"]
    tasks_path = _c1_tasks_path_from_manifest(manifest)
    if not isinstance(tasks_path, str) or not tasks_path:
        return None, ["formal C1 manifest is missing the golden task source"]
    try:
        tasks = load_state_loop_tasks(report_root, _resolve(report_root, tasks_path))
    except Exception as error:
        return None, [f"cannot load tasks for oracle golden provenance: {error}"]

    selected_ids = [str(value) for value in (run_index.get("task_ids") or [])]
    selected = set(selected_ids or [task.id for task in tasks])
    task_by_id = {task.id: task for task in tasks if task.id in selected}
    if set(task_by_id) != selected:
        return None, ["oracle golden provenance task set differs from registered selection"]
    seed_count = _integer(run_index.get("seeds"))
    if seed_count is None or seed_count <= 0:
        return None, ["oracle golden provenance requires a positive seed count"]

    issues: list[str] = []
    expected: dict[tuple[str, int], dict[str, str]] = {}
    child_commits: dict[Path, str | None] = {}
    for task_id in selected_ids or sorted(task_by_id):
        task = task_by_id[task_id]
        try:
            child_repo = resolve_task_repo(report_root, task.issue).resolve()
            if child_repo not in child_commits:
                child_commits[child_repo] = _repo_git_commit(child_repo)
            git_commit = child_commits[child_repo]
            if not isinstance(git_commit, str) or not git_commit:
                issues.append(f"{task_id}: child repository git commit is unavailable")
                continue
            raw_spec = load_spec((report_root / task.oracle_spec).resolve())
            for seed in range(seed_count):
                seeded_spec = _apply_experiment_seed(
                    raw_spec,
                    seed,
                    benchmark=task.benchmark,
                )
                expected[(task_id, seed)] = {
                    "fingerprint": _oracle_fingerprint(git_commit, seeded_spec),
                    "git_commit": git_commit,
                    "oracle_spec_sha256": _oracle_spec_digest(seeded_spec),
                }
        except Exception as error:
            issues.append(f"{task_id}: cannot derive oracle golden provenance: {error}")
    return expected, issues


def _c1_schedule_issues(run_index: dict[str, Any]) -> list[str]:
    models = [str(model) for model in (run_index.get("models") or [])]
    try:
        seeds = int(run_index.get("seeds") or 0)
    except (TypeError, ValueError):
        seeds = 0
    base_order = list(_C1_SCHEDULE_ARM_LABELS)
    expected: list[tuple[str, str, int]] = []
    expected_registration: list[dict[str, Any]] = []
    expected_blocks: list[dict[str, Any]] = []
    for model_index, model in enumerate(models):
        for seed in range(seeds):
            offset = (model_index * seeds + seed) % len(base_order)
            ordered = base_order[offset:] + base_order[:offset]
            expected.extend((arm, model, seed) for arm in ordered)
            expected_blocks.append(
                {
                    "model": model,
                    "model_index": model_index,
                    "seed": seed,
                    "offset": offset,
                    "arms": ordered,
                }
            )
            for position, arm in enumerate(ordered):
                expected_registration.append(
                    {
                        "ordinal": len(expected_registration),
                        "arm": arm,
                        "model": model,
                        "seed": seed,
                        "model_index": model_index,
                        "offset": offset,
                        "position": position,
                        "ordered_arms": ordered,
                    }
                )
    actual: list[tuple[str, str, int]] = []
    for row in (run_index.get("execution_order") or []):
        if not isinstance(row, dict):
            continue
        try:
            seed = int(row.get("seed", -1))
        except (TypeError, ValueError):
            seed = -1
        actual.append((str(row.get("arm")), str(row.get("model")), seed))
    latin = run_index.get("latin_square") if isinstance(run_index.get("latin_square"), dict) else {}
    issues: list[str] = []
    if actual != expected:
        issues.append("C1 execution order does not match the pre-registered model-seed Latin square")
    if latin.get("base_order") != base_order or len(latin.get("blocks") or []) != len(models) * seeds:
        issues.append("C1 Latin-square metadata is missing or inconsistent")
    if "c1_outer_worker" not in run_index and "c1_scheduler" not in run_index:
        return issues

    registration_fields = tuple(expected_registration[0]) if expected_registration else (
        "ordinal",
        "arm",
        "model",
        "seed",
        "model_index",
        "offset",
        "position",
        "ordered_arms",
    )
    actual_registration = [
        {field: row.get(field) for field in registration_fields}
        for row in (run_index.get("execution_order") or [])
        if isinstance(row, dict)
    ]
    if actual_registration != expected_registration:
        issues.append("C1 canonical outer registration provenance is invalid")
    expected_latin = {
        "base_order": base_order,
        "offset_rule": f"(model_index * seeds + seed) mod {len(base_order)}",
        "blocks": expected_blocks,
    }
    if latin != expected_latin:
        issues.append("C1 Latin-square registration provenance is invalid")

    scheduler = run_index.get("c1_scheduler")
    requested = _integer(run_index.get("c1_outer_worker"))
    if not isinstance(scheduler, dict):
        issues.append("C1 outer scheduler provenance is missing")
        return issues
    if scheduler.get("schema_version") != 2:
        issues.append("C1 outer scheduler schema_version must be 2")
    if requested is None or requested < 1:
        issues.append("C1 run index has an invalid requested outer worker count")
        return issues
    if scheduler.get("requested_outer_workers") != requested:
        issues.append("C1 requested outer workers differ from scheduler provenance")
    effective = _integer(scheduler.get("effective_outer_workers"))
    if effective is None or not 0 <= effective <= requested:
        issues.append("C1 effective outer workers are invalid")
    peak = _integer(scheduler.get("max_active_invocations"))
    if peak is None or not 0 <= peak <= requested:
        issues.append("C1 observed outer concurrency is invalid")
    resource_scheduler = run_index.get("c1_resource_scheduler")
    if resource_scheduler is not None:
        if not isinstance(resource_scheduler, dict):
            issues.append("C1 resource scheduler provenance is invalid")
        else:
            if resource_scheduler.get("schema_version") != 1:
                issues.append("C1 resource scheduler schema_version must be 1")
            if (
                resource_scheduler.get("policy")
                != "global_resource_ready_round_robin_v2"
            ):
                issues.append("C1 resource scheduler policy is invalid")
            active_tasks = _integer(resource_scheduler.get("active_tasks"))
            max_active_tasks = _integer(
                resource_scheduler.get("max_active_tasks")
            )
            if (
                active_tasks is None
                or active_tasks < 0
                or max_active_tasks is None
                or max_active_tasks < active_tasks
            ):
                issues.append("C1 resource scheduler active-task counts are invalid")
            invocation_snapshots = resource_scheduler.get("invocations")
            if invocation_snapshots is not None:
                if not isinstance(invocation_snapshots, dict):
                    issues.append("C1 resource scheduler invocations are invalid")
                else:
                    inner_limit = _integer(run_index.get("c1_inner_worker"))
                    for invocation_id, invocation in invocation_snapshots.items():
                        label = f"C1 resource invocation {invocation_id}"
                        if not isinstance(invocation, dict):
                            issues.append(f"{label} is malformed")
                            continue
                        worker_limit = _integer(invocation.get("worker_limit"))
                        invocation_active = _integer(
                            invocation.get("active_tasks")
                        )
                        invocation_peak = _integer(
                            invocation.get("max_active_tasks")
                        )
                        if (
                            inner_limit is None
                            or worker_limit is None
                            or not 1 <= worker_limit <= inner_limit
                            or invocation_active is None
                            or invocation_peak is None
                            or not 0
                            <= invocation_active
                            <= invocation_peak
                            <= worker_limit
                        ):
                            issues.append(f"{label} worker/active counts are invalid")
                        for dispatch in invocation.get("dispatch_history") or []:
                            lane_id = (
                                _integer(dispatch.get("lane_id"))
                                if isinstance(dispatch, dict)
                                else None
                            )
                            if (
                                lane_id is None
                                or worker_limit is None
                                or not 0 <= lane_id < worker_limit
                            ):
                                issues.append(f"{label} dispatch lane is invalid")
                                break

    start_ordinals: list[int] = []
    completion_ordinals: list[int] = []
    imported_sources = run_index.get("c1_sources")
    for row in (run_index.get("execution_order") or []):
        if not isinstance(row, dict):
            continue
        label = f"{row.get('arm')}/{row.get('model')}/seed{row.get('seed')}"
        history = row.get("dispatch_history")
        if not isinstance(history, list):
            issues.append(f"{label}: outer dispatch_history is missing")
            continue
        if any(not isinstance(dispatch, dict) for dispatch in history):
            issues.append(f"{label}: outer dispatch_history contains a non-mapping")
            continue
        expected_dispatch_ordinals = list(range(len(history)))
        actual_dispatch_ordinals = [
            _integer(dispatch.get("dispatch_ordinal")) for dispatch in history
        ]
        if actual_dispatch_ordinals != expected_dispatch_ordinals:
            issues.append(f"{label}: outer dispatch ordinals are invalid")
        for dispatch in history:
            start = _integer(dispatch.get("start_ordinal"))
            started_ns = _integer(dispatch.get("started_monotonic_ns"))
            if start is None or start < 0 or started_ns is None or started_ns < 0:
                issues.append(f"{label}: outer dispatch start provenance is invalid")
            else:
                start_ordinals.append(start)
            completion = dispatch.get("completion_ordinal")
            completed_ns = dispatch.get("completed_monotonic_ns")
            if completion is None and completed_ns is None:
                if dispatch.get("status") not in {"running", "interrupted_on_resume"}:
                    issues.append(f"{label}: terminal outer dispatch lacks completion provenance")
            else:
                parsed_completion = _integer(completion)
                parsed_completed_ns = _integer(completed_ns)
                if (
                    parsed_completion is None
                    or parsed_completion < 0
                    or parsed_completed_ns is None
                    or parsed_completed_ns < 0
                ):
                    issues.append(f"{label}: outer dispatch completion provenance is invalid")
                else:
                    completion_ordinals.append(parsed_completion)
                    if started_ns is not None and parsed_completed_ns < started_ns:
                        issues.append(
                            f"{label}: outer dispatch completion precedes its start"
                        )
        status = row.get("status")
        if status == "imported_complete":
            if (
                history
                or not isinstance(imported_sources, list)
                or not imported_sources
                or row.get("source_provenance") != imported_sources
            ):
                issues.append(f"{label}: imported completion lacks clean-root source provenance")
        elif status == "complete":
            if not history or (
                history[-1].get("status") != "complete"
                and row.get("reconciled_from_evidence") is not True
            ):
                issues.append(f"{label}: completed registration lacks a successful terminal dispatch")
        else:
            issues.append(f"{label}: outer registration is not complete")
        if history:
            latest = history[-1]
            for field in ("start_ordinal", "started_monotonic_ns"):
                if row.get(field) != latest.get(field):
                    issues.append(f"{label}: top-level {field} differs from latest dispatch")
            if latest.get("completion_ordinal") is not None:
                for field in ("completion_ordinal", "completed_monotonic_ns"):
                    if row.get(field) != latest.get(field):
                        issues.append(
                            f"{label}: top-level {field} differs from latest dispatch"
                        )

    if len(start_ordinals) != len(set(start_ordinals)) or sorted(start_ordinals) != list(
        range(len(start_ordinals))
    ):
        issues.append("C1 outer start ordinals are duplicated, missing, or non-contiguous")
    if len(completion_ordinals) != len(set(completion_ordinals)) or sorted(
        completion_ordinals
    ) != list(range(len(completion_ordinals))):
        issues.append("C1 outer completion ordinals are duplicated, missing, or non-contiguous")
    return issues


def _recovery_preflight_contract(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        field: value.get(field)
        for field in (
            "complete",
            "git_commit",
            "manifest_hash",
            "harness_hash",
            "c1_application_revisions",
            "c1_environment",
            "api_provider",
            "provider_timeout_s",
            "model_request_profiles",
            "model_request_profiles_sha256",
            "probe_count",
            "initial_max_tokens",
            "retry_max_tokens",
            "retry_rule",
            "temperature_contract",
        )
    }


def _recovery_calibration_contract(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    browser = value.get("browser") if isinstance(value.get("browser"), dict) else {}
    luanti = value.get("luanti") if isinstance(value.get("luanti"), dict) else {}
    native_session = (
        value.get("native_session")
        if isinstance(value.get("native_session"), dict)
        else {}
    )
    text_input = (
        luanti.get("text_input")
        if isinstance(luanti.get("text_input"), dict)
        else {}
    )
    return {
        "schema_version": value.get("schema_version"),
        "passed": value.get("passed"),
        "git_commit": value.get("git_commit"),
        "manifest_hash": value.get("manifest_hash"),
        "harness_hash": value.get("harness_hash"),
        "c1_application_revisions": value.get("c1_application_revisions"),
        "c1_environment": value.get("c1_environment"),
        "api_provider": value.get("api_provider"),
        "provider_timeout_s": value.get("provider_timeout_s"),
        "observer_model": value.get("observer_model"),
        "grounder_model": value.get("grounder_model"),
        "keyboard_input_sources_sha256": native_session.get(
            "keyboard_input_sources_sha256"
        ),
        "browser": {
            "passed": browser.get("passed"),
            "frame": browser.get("frame"),
            "headless": browser.get("headless"),
        },
        "luanti": {
            "passed": luanti.get("passed"),
            "frame": luanti.get("frame"),
            "worldmod_click_event": luanti.get("worldmod_click_event"),
            "text_input_protocol": text_input.get("protocol"),
            "text_input_transport": text_input.get("transport"),
        },
    }


def _quarantined_usage_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    candidates: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("cost"), dict):
            candidates.append(payload["cost"])
        for result in payload.get("results") or []:
            if isinstance(result, dict) and isinstance(result.get("cost"), dict):
                candidates.append(result["cost"])
    if not candidates:
        return None
    fields = (
        "calls",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "reasoning_tokens_observed",
        "reasoning_tokens_lower_bound",
        "reasoning_tokens_upper_bound",
        "cached_prompt_tokens",
        "total_tokens",
        "usage_complete",
        "incomplete_calls",
        "unknown_prompt_token_calls",
        "unknown_completion_token_calls",
        "unknown_reasoning_token_calls",
        "unknown_total_token_calls",
        "reasoning_detail_complete",
        "reasoning_detail_missing_calls",
        "reasoning_detail_unknown_completion_tokens",
        "reasoning_detail_expected_completion_tokens",
        "reasoning_detail_unknown_fraction",
        "reasoning_detail_tolerance_fraction",
        "reasoning_detail_within_tolerance",
    )
    snapshot = {
        field: candidates[0].get(field)
        for field in fields
        if field in candidates[0]
    }
    for partition in ("by_kind", "by_model", "by_kind_model"):
        if isinstance(candidates[0].get(partition), dict):
            snapshot[partition] = json.loads(json.dumps(candidates[0][partition]))
    return snapshot


def _registered_retry_task_plan_migration(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
) -> tuple[set[TrialKey], list[str]]:
    """Re-audit the exact retry-plan recovery hop during aggregation."""

    migration = run_index.get("c1_source_contract_migration")
    request = run_index.get("c1_source_contract_migration_request")
    if not isinstance(migration, dict) or migration.get("migration_id") != (
        "c1_retry_task_plan_resume_v1"
    ):
        return set(), []

    affected_value = migration.get("affected_trials")
    affected = (
        affected_value
        if isinstance(affected_value, list)
        and all(isinstance(row, dict) for row in affected_value)
        else []
    )
    affected_sha = hashlib.sha256(
        json.dumps(affected, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    changed_paths = [
        "tests/test_c1_attempt_costs.py",
        "tests/test_c1_trial_persistence.py",
        "warptest/gui_agent.py",
        "warptest/p5_scale.py",
        "warptest/state_loop.py",
    ]
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": "c1_retry_task_plan_resume_v1",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "source_git_commit": "7bdf4d095d6809c03e50f11f77954c58a734d958",
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "current_git_commit": run_index.get("git_commit"),
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "preserved_model_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "treatment_semantics_unchanged": True,
        "changed_paths": changed_paths,
    }
    issues: list[str] = []
    if any(migration.get(field) != value for field, value in expected.items()):
        issues.append("C1 retry-plan migration metadata is invalid")
    if affected_sha != (
        "cd445eb9376156247452eb954e41b796ffeb62fc66f758a1f4ace889cbd4e9a0"
    ):
        issues.append("C1 retry-plan migration affected-trial binding is invalid")
        affected = []
    if not isinstance(migration.get("reason"), str) or not migration["reason"].strip():
        issues.append("C1 retry-plan migration reason is missing")
    if (
        source.get("run_id") != "p5-d65ab3c4aa7c"
        or source.get("git_commit") != expected["source_git_commit"]
        or source.get("harness_hash")
        != "cf3fba87e98cc0a4513f7148162590f7e6df461a5454978ef808ef0a36c5e3ab"
    ):
        issues.append("C1 retry-plan migration source provenance is invalid")
    if source_meta.get("contract_migration") != migration:
        issues.append("C1 retry-plan source metadata migration binding is invalid")

    request_path = (
        Path(request).expanduser()
        if isinstance(request, str) and request
        else None
    )
    if request_path is None or not request_path.is_file():
        issues.append("C1 retry-plan migration artifact is missing")
    else:
        raw = request_path.read_bytes()
        try:
            artifact_payload = json.loads(raw)
        except json.JSONDecodeError:
            artifact_payload = None
        metadata_payload = {
            key: value
            for key, value in migration.items()
            if key not in {"artifact", "artifact_sha256"}
        }
        if (
            migration.get("artifact") != str(request_path.resolve())
            or migration.get("artifact_sha256") != hashlib.sha256(raw).hexdigest()
            or artifact_payload != metadata_payload
        ):
            issues.append("C1 retry-plan migration artifact binding is invalid")

    affected_keys = {
        TrialKey.from_row(str(row["arm"]), row) for row in affected
    }
    source_rows: dict[TrialKey, dict[str, Any]] = {}
    current_rows: dict[TrialKey, dict[str, Any]] = {}
    for payload, target in ((source, source_rows), (run_index, current_rows)):
        for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state"):
            for row in (payload.get("arms", {}).get(arm) or []):
                if not isinstance(row, dict):
                    continue
                key = _trial_key(arm, row)
                if key[3] and key not in target:
                    target[key] = row
    source_errors = {
        key for key, row in source_rows.items() if row.get("status") == "error"
    }
    if (
        len(source_rows) != 183
        or sum(row.get("status") == "ok" for row in source_rows.values()) != 57
        or sum(row.get("status") == "pending" for row in source_rows.values()) != 124
        or source_errors != affected_keys
        or set(current_rows) != set(source_rows)
    ):
        issues.append("C1 retry-plan migration source coverage is invalid")

    for key, source_row in source_rows.items():
        current_row = current_rows.get(key)
        if not isinstance(current_row, dict):
            continue
        if source_row.get("status") == "ok":
            provenance = current_row.get("source_provenance")
            immutable = (
                "model",
                "seed",
                "task_id",
                "trial_attempt",
                "attempt_id",
                "report",
                "report_sha256",
            )
            if (
                current_row.get("status") != "ok"
                or current_row.get("imported") is not True
                or any(
                    current_row.get(field) != source_row.get(field)
                    for field in immutable
                )
                or current_row.get("replacement_for") is not None
                or not isinstance(provenance, dict)
                or provenance.get("source_index_sha256") != source_sha
                or provenance.get("source_run_id") != source.get("run_id")
            ):
                issues.append(
                    f"C1 retry-plan completed tuple was not imported immutably: {key}"
                )
        elif key in affected_keys:
            report_path = Path(str(source_row.get("report") or "")).expanduser()
            report = None
            report_sha = None
            if report_path.is_file():
                raw = report_path.read_bytes()
                report_sha = hashlib.sha256(raw).hexdigest()
                try:
                    report = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
            results = report.get("results") if isinstance(report, dict) else None
            result = (
                results[0]
                if isinstance(results, list)
                and len(results) == 1
                and isinstance(results[0], dict)
                else None
            )
            usage = (
                result.get("actual_cost") or result.get("cost")
                if isinstance(result, dict)
                else None
            )
            exact_harness_failure = bool(
                isinstance(report, dict)
                and report.get("complete") is False
                and isinstance(result, dict)
                and result.get("incomplete") is True
                and isinstance(usage, dict)
                and usage.get("usage_complete") is True
                and all(
                    usage.get(field) == 0
                    for field in (
                        "unknown_prompt_token_calls",
                        "unknown_completion_token_calls",
                        "unknown_total_token_calls",
                    )
                )
                and source_row.get("report_sha256") in {None, report_sha}
            )
            expected_replacement = {
                "source_index": str(source_path),
                "source_index_sha256": source_sha,
                "source_run_id": source.get("run_id"),
                "source_status": "error",
                "source_report": source_row.get("report"),
                "source_report_sha256": report_sha,
            }
            expected_attempt_id = (
                f"{run_index.get('run_id')}:{key[0]}:{key[1] or 'na'}:"
                f"{key[2] if key[2] is not None else 'na'}:{key[3]}:attempt0"
            )
            if not exact_harness_failure:
                issues.append(
                    f"C1 retry-plan source harness failure is invalid: {key}"
                )
            if (
                current_row.get("status") != "ok"
                or current_row.get("imported") is True
                or current_row.get("replacement_for") != expected_replacement
                or current_row.get("source_provenance") is not None
                or current_row.get("trial_attempt") != 0
                or current_row.get("attempt_id") != expected_attempt_id
            ):
                issues.append(
                    f"C1 retry-plan affected tuple was not requeued exactly: {key}"
                )

    overhead_by_key = {
        _trial_key(str(row.get("arm") or ""), row): row
        for row in (run_index.get("c1_operational_overhead") or [])
        if isinstance(row, dict)
        and isinstance(row.get("source_provenance"), dict)
        and row["source_provenance"].get("source_index_sha256") == source_sha
    }
    for key in affected_keys:
        overhead = overhead_by_key.get(key)
        if (
            not isinstance(overhead, dict)
            or overhead.get("status")
            != "replaced_registered_exact_usage_harness_failure"
            or overhead.get("replacement_for")
            != current_rows.get(key, {}).get("replacement_for")
        ):
            issues.append(f"C1 retry-plan replacement overhead is invalid: {key}")
    return affected_keys, issues


def _registered_openra_render_grade_migration(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
) -> tuple[set[TrialKey], list[str]]:
    """Re-audit the exact OpenRA read-only grader migration for aggregation."""

    migration = run_index.get("c1_source_contract_migration")
    request = run_index.get("c1_source_contract_migration_request")
    if not isinstance(migration, dict) or migration.get("migration_id") != (
        "c1_openra_readonly_render_grade_v1"
    ):
        return set(), []

    affected = [
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "openra-allied-combined-arms-rally",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s_synth",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "openra-soviet-rocket-infantry-chain",
            "source_status": "error",
        },
    ]
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "allowed_migrations": [
            "c1_application_revisions_rebind",
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
            "task_manifest_hash_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "migration_id": "c1_openra_readonly_render_grade_v1",
        "source_git_commit": "9b981e327ba001b422bc2381f68df3532ad841ce",
        "current_git_commit": run_index.get("git_commit"),
        "changed_paths": [
            "benchmarks/p5/c1_tasks.yaml",
            "games/OpenRA",
            "tests/test_c1_openra_runtime.py",
            "tests/test_c1_v5_catalog.py",
            "warptest/p5_scale.py",
        ],
        "affected_trials": affected,
        "preserved_model_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "treatment_semantics_unchanged": True,
    }
    issues: list[str] = []
    if any(migration.get(field) != value for field, value in expected.items()):
        issues.append("C1 OpenRA grader migration metadata is invalid")
    changed_paths = migration.get("changed_paths")
    changed_paths_sha = hashlib.sha256(
        json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if migration.get("changed_paths_sha256") != changed_paths_sha:
        issues.append("C1 OpenRA grader migration changed-path binding is invalid")
    if source_meta.get("contract_migration") != migration:
        issues.append("C1 OpenRA grader source metadata migration binding is invalid")

    request_path = (
        Path(request).expanduser()
        if isinstance(request, str) and request
        else None
    )
    if request_path is None or not request_path.is_file():
        issues.append("C1 OpenRA grader migration artifact is missing")
    else:
        raw = request_path.read_bytes()
        artifact_payload = None
        try:
            artifact_payload = json.loads(raw)
        except json.JSONDecodeError:
            pass
        metadata_payload = {
            key: value
            for key, value in migration.items()
            if key not in {"artifact", "artifact_sha256"}
        }
        if (
            migration.get("artifact") != str(request_path.resolve())
            or migration.get("artifact_sha256") != hashlib.sha256(raw).hexdigest()
            or artifact_payload != metadata_payload
        ):
            issues.append("C1 OpenRA grader migration artifact binding is invalid")

    affected_keys = {
        TrialKey.from_row(str(row["arm"]), row) for row in affected
    }
    source_errors = {
        _trial_key(arm, row)
        for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state")
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict) and row.get("status") == "error"
    }
    source_rows = [
        row
        for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state")
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    if (
        source.get("run_id") != "p5-e92822c0dfa1"
        or source.get("git_commit") != expected["source_git_commit"]
        or source.get("harness_hash")
        != "7dd8db55c5f367367d62d6cd636e87d512d0cf7401d8854692302c20480fa0a6"
        or len(source_rows) != 183
        or sum(row.get("status") == "ok" for row in source_rows) != 161
        or sum(row.get("status") == "pending" for row in source_rows) != 20
        or source_errors != affected_keys
    ):
        issues.append("C1 OpenRA grader migration source coverage is invalid")
    return affected_keys, issues


def _repository_changed_paths(
    repository_root: Path | None,
    source_commit: Any,
    current_commit: Any,
) -> list[str] | None:
    """Return the commit-grounded path set used by a registered migration."""

    if (
        repository_root is None
        or not isinstance(source_commit, str)
        or not source_commit
        or not isinstance(current_commit, str)
        or not current_commit
    ):
        return None
    try:
        completed = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                source_commit,
                current_commit,
                "--",
            ],
            cwd=str(repository_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return sorted(
        path for path in completed.stdout.splitlines() if path
    )


def _recursive_artifact_sha256(root: Path) -> str | None:
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    try:
        for path in files:
            relative = path.relative_to(root).as_posix()
            file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            digest.update(f"{file_sha}  {relative}\n".encode("utf-8"))
    except OSError:
        return None
    return digest.hexdigest()


def _registered_jupyter_runtime_recovery_stage(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
    repository_root: Path | None,
) -> tuple[set[TrialKey], list[str]]:
    """Re-audit the exact Jupyter environment pause and replacement set."""

    migration = run_index.get("c1_source_contract_migration")
    if background_recovery_stage(migration) != JUPYTER_RUNTIME_RECOVERY_STAGE:
        return set(), []
    issues: list[str] = []
    rows = [
        (arm, row)
        for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state")
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    pending_keys = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("status") == "pending"
    }
    error_key = TrialKey(
        "c1_state",
        "kimi-k2.6@relay",
        0,
        "jupyterlab-debug-notebook-rerun",
    )
    error_keys = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("status") == "error"
    }
    affected_keys = pending_keys | error_keys
    if (
        source.get("run_id") != "p5-b9c6a7b91801"
        or source.get("git_commit")
        != "21f0fb9ab273f7dfaf51efe8354371bb434ade8d"
        or source.get("harness_hash")
        != "b35cb4ef39d93c02d56cbb81f34b3f7279f6b0e70b1ae786a757c55809700201"
        or len(rows) != 549
        or len(pending_keys) != 146
        or sum(row.get("status") == "ok" for _arm, row in rows) != 402
        or error_keys != {error_key}
    ):
        issues.append("C1 Jupyter-runtime recovery source coverage is invalid")

    source_root = source_path.parent.parent
    pause_path = (
        source_root / "gates/c1-pause-audit-jupyter-runtime.json"
    ).resolve()
    canary_path = (
        source_root / "gates/c1-jupyterlab-runtime-canary.json"
    ).resolve()
    pause_raw: bytes | None = None
    pause_payload: dict[str, Any] | None = None
    canary_raw: bytes | None = None
    canary: dict[str, Any] | None = None
    try:
        pause_raw = pause_path.read_bytes()
        decoded_pause = json.loads(pause_raw)
        pause_payload = decoded_pause if isinstance(decoded_pause, dict) else None
        canary_raw = canary_path.read_bytes()
        decoded_canary = json.loads(canary_raw)
        canary = decoded_canary if isinstance(decoded_canary, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    interrupted = (
        pause_payload.get("interrupted_unknown_usage_attempts")
        if isinstance(pause_payload, dict)
        else None
    )
    if (
        pause_raw is None
        or pause_payload is None
        or hashlib.sha256(pause_raw).hexdigest()
        != migration.get("pause_audit_sha256")
        or pause_payload.get("paused_index_sha256") != source_sha
        or pause_payload.get("run_id") != source.get("run_id")
        or not isinstance(interrupted, list)
        or len(interrupted) != 4
        or interrupted != migration.get("interrupted_unknown_usage_attempts")
    ):
        issues.append("C1 Jupyter-runtime recovery pause audit is invalid")
        interrupted = []
    interrupted_keys: set[TrialKey] = set()
    for item in interrupted:
        if not isinstance(item, dict):
            issues.append("C1 Jupyter interrupted-attempt row is malformed")
            continue
        key = _trial_key(str(item.get("arm") or ""), item)
        attempt_dir = Path(str(item.get("attempt_dir") or "")).expanduser()
        files = [path for path in attempt_dir.rglob("*") if path.is_file()]
        if (
            key in interrupted_keys
            or key not in pending_keys
            or item.get("trial_attempt") != 0
            or item.get("usage_status") != "unknown"
            or item.get("disposition")
            != "quarantine_primary_attempt_and_register_replacement"
            or item.get("file_count") != len(files)
            or item.get("total_bytes")
            != sum(path.stat().st_size for path in files)
            or _recursive_artifact_sha256(attempt_dir)
            != item.get("recursive_artifact_sha256")
        ):
            issues.append("C1 Jupyter interrupted-attempt artifact is invalid")
        interrupted_keys.add(key)

    runtime_python = "/opt/miniconda3/bin/python3.13"
    checks = canary.get("checks") if isinstance(canary, dict) else None
    expected_specs = (
        [
            (
                "specs/jupyterlab/text-file-edit.yaml",
                (
                    repository_root / "specs/jupyterlab/text-file-edit.yaml"
                ).resolve(),
            ),
            (
                "specs/jupyterlab/c1-debug-notebook-rerun.yaml",
                (
                    repository_root
                    / "specs/jupyterlab/c1-debug-notebook-rerun.yaml"
                ).resolve(),
            ),
        ]
        if repository_root is not None
        else []
    )
    canary_valid = bool(
        canary_raw is not None
        and isinstance(canary, dict)
        and canary.get("passed") is True
        and canary.get("python_executable") == runtime_python
        and canary.get("python_version") == "3.13.11"
        and canary.get("jupyterlab_version") == "4.7.0a0"
        and canary.get("jupyter_server_version") == "2.20.0"
        and canary.get("jupyter_client_version") == "8.9.1"
        and isinstance(checks, list)
        and len(checks) == 2
        and len(expected_specs) == 2
        and hashlib.sha256(canary_raw).hexdigest()
        == migration.get("runtime_canary_sha256")
    )
    if canary_valid:
        for check, (relative_spec, expected_spec) in zip(
            checks, expected_specs, strict=True
        ):
            if not isinstance(check, dict):
                canary_valid = False
                break
            report_path = Path(str(check.get("report") or "")).expanduser()
            recorded_spec = Path(str(check.get("spec") or "")).expanduser()
            try:
                report_raw = report_path.read_bytes()
                report = json.loads(report_raw)
                recorded_spec_raw = recorded_spec.read_bytes()
                expected_spec_raw = expected_spec.read_bytes()
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                canary_valid = False
                break
            if (
                check.get("spec_relative_path") != relative_spec
                or check.get("spec_sha256")
                != hashlib.sha256(recorded_spec_raw).hexdigest()
                or recorded_spec_raw != expected_spec_raw
                or check.get("report_sha256")
                != hashlib.sha256(report_raw).hexdigest()
                or check.get("overall_status") != "success"
                or not isinstance(report, dict)
                or report.get("overall_status") != "success"
                or report.get("semantic_status") != "success"
                or Path(str(report.get("spec_path") or "")).resolve()
                != recorded_spec.resolve()
                or not str(report.get("launch_command") or "").startswith(
                    runtime_python + " -m jupyterlab "
                )
            ):
                canary_valid = False
                break
    if not canary_valid:
        issues.append("C1 Jupyter-runtime recovery canary is invalid")

    row_by_key = {
        _trial_key(arm, row): row
        for arm, row in rows
    }
    audited_failures = (
        pause_payload.get("registered_exact_usage_harness_failures")
        if isinstance(pause_payload, dict)
        else None
    )
    failure_rows = [
        {
            "arm": "c1_state",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "jupyterlab-debug-notebook-rerun",
            "source_status": "error",
        }
    ]
    harness_failure_valid = bool(
        isinstance(audited_failures, list) and len(audited_failures) == 1
    )
    if harness_failure_valid:
        audited = audited_failures[0]
        source_error = row_by_key.get(error_key, {})
        report_path = Path(str(source_error.get("report") or "")).expanduser()
        try:
            report_raw = report_path.read_bytes()
            report = json.loads(report_raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            harness_failure_valid = False
        else:
            results = report.get("results") if isinstance(report, dict) else None
            result = (
                results[0]
                if isinstance(results, list)
                and len(results) == 1
                and isinstance(results[0], dict)
                else None
            )
            actual_cost = (
                result.get("actual_cost") if isinstance(result, dict) else None
            )
            harness_failure_valid = bool(
                isinstance(audited, dict)
                and {
                    field: audited.get(field)
                    for field in (
                        "arm",
                        "model",
                        "seed",
                        "task_id",
                        "source_status",
                    )
                }
                == failure_rows[0]
                and audited.get("report") == str(report_path)
                and audited.get("report_sha256")
                == hashlib.sha256(report_raw).hexdigest()
                and source_error.get("report_sha256")
                == hashlib.sha256(report_raw).hexdigest()
                and isinstance(report, dict)
                and report.get("complete") is False
                and isinstance(result, dict)
                and result.get("incomplete") is True
                and isinstance(actual_cost, dict)
                and actual_cost.get("usage_complete") is True
                and all(
                    actual_cost.get(field) == 0
                    for field in (
                        "unknown_prompt_token_calls",
                        "unknown_completion_token_calls",
                        "unknown_total_token_calls",
                    )
                )
                and "JupyterLab runtime dependencies are not installed"
                in str(result.get("reason") or "")
                and "No module named 'jupyter_server'"
                in str(result.get("reason") or "")
            )
    if not harness_failure_valid:
        issues.append("C1 Jupyter-runtime harness failure is invalid")

    changed_paths = migration.get("changed_paths")
    required_paths = {
        "tests/test_c1_trial_persistence.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
    }
    actual_changed_paths = _repository_changed_paths(
        repository_root,
        source.get("git_commit"),
        run_index.get("git_commit"),
    )
    changed_paths_valid = bool(
        isinstance(changed_paths, list)
        and changed_paths == sorted(set(changed_paths))
        and set(changed_paths) == required_paths
        and actual_changed_paths == changed_paths
    )
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": BACKGROUND_RESOURCE_MIGRATION_ID,
        "migration_stage": JUPYTER_RUNTIME_RECOVERY_STAGE,
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "source_git_commit": source.get("git_commit"),
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "current_git_commit": run_index.get("git_commit"),
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "runtime_environment_variable": "WARPTEST_JUPYTERLAB_PYTHON",
        "runtime_python": runtime_python,
        "runtime_python_version": "3.13.11",
        "runtime_packages": {
            "jupyterlab": "4.7.0a0",
            "jupyter_server": "2.20.0",
            "jupyter_client": "8.9.1",
        },
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "recursive_lineage_preserved": True,
        "treatment_semantics_unchanged": True,
        "preserved_model_failure_trials": [],
        "registered_harness_failure_trials": failure_rows,
        "pause_audit": str(pause_path),
        "pause_audit_sha256": (
            hashlib.sha256(pause_raw).hexdigest()
            if pause_raw is not None
            else None
        ),
        "interrupted_unknown_usage_attempts": interrupted,
        "runtime_canary": str(canary_path),
        "runtime_canary_sha256": (
            hashlib.sha256(canary_raw).hexdigest()
            if canary_raw is not None
            else None
        ),
        "canary_policy": "two_real_no_model_jupyterlab_specs_v1",
    }
    if any(migration.get(field) != value for field, value in expected.items()):
        issues.append("C1 Jupyter-runtime recovery migration metadata is invalid")
    if not changed_paths_valid or migration.get(
        "changed_paths_sha256"
    ) != hashlib.sha256(
        json.dumps(changed_paths or [], separators=(",", ":")).encode("utf-8")
    ).hexdigest():
        issues.append("C1 Jupyter-runtime recovery changed paths are invalid")

    request = run_index.get("c1_source_contract_migration_request")
    request_path = (
        Path(request).expanduser()
        if isinstance(request, str) and request
        else None
    )
    artifact_valid = False
    if request_path is not None and request_path.is_file():
        raw = request_path.read_bytes()
        try:
            artifact_payload = json.loads(raw)
        except json.JSONDecodeError:
            artifact_payload = None
        metadata_payload = {
            key: value
            for key, value in migration.items()
            if key not in {"artifact", "artifact_sha256"}
        }
        artifact_valid = bool(
            migration.get("artifact") == str(request_path.resolve())
            and migration.get("artifact_sha256")
            == hashlib.sha256(raw).hexdigest()
            and artifact_payload == metadata_payload
            and source_meta.get("contract_migration") == migration
        )
    if not artifact_valid:
        issues.append("C1 Jupyter-runtime recovery migration artifact is invalid")

    imported_overhead = {
        _trial_key(str(row.get("arm") or ""), row): row
        for row in (run_index.get("c1_operational_overhead") or [])
        if isinstance(row, dict)
        and isinstance(row.get("source_provenance"), dict)
        and row["source_provenance"].get("source_index_sha256") == source_sha
        and isinstance(row["source_provenance"].get("interrupted_attempt"), dict)
    }
    if set(imported_overhead) != interrupted_keys:
        issues.append("C1 Jupyter-runtime quarantine overhead is incomplete")
    for key in interrupted_keys & set(imported_overhead):
        row = imported_overhead[key]
        if (
            row.get("status") != "quarantined_unknown_usage"
            or row.get("report") != str(pause_path)
            or row.get("report_sha256") != migration.get("pause_audit_sha256")
            or row.get("known_and_unknown_usage")
            != _interrupted_unknown_usage_snapshot()
        ):
            issues.append("C1 Jupyter-runtime quarantine overhead is invalid")
    harness_overhead = [
        row
        for row in (run_index.get("c1_operational_overhead") or [])
        if isinstance(row, dict)
        and _trial_key(str(row.get("arm") or ""), row) == error_key
        and row.get("status")
        == "replaced_registered_exact_usage_harness_failure"
        and isinstance(row.get("source_provenance"), dict)
        and row["source_provenance"].get("source_index_sha256") == source_sha
    ]
    if len(harness_overhead) != 1:
        issues.append("C1 Jupyter-runtime harness overhead is incomplete")
    if any(
        source_meta.get(field) != value
        for field, value in {
            "source_index": str(source_path),
            "source_index_sha256": source_sha,
            "source_run_id": source.get("run_id"),
            "completed_trials": 402,
            "replacement_trials": 147,
            "quarantined_trials": 4,
            "exact_usage_timeout_trials": 0,
            "exact_usage_retryable_infrastructure_trials": 1,
            "imported_nonretryable_model_failure_trials": 3,
            "contract_migration": migration,
        }.items()
    ):
        issues.append("C1 Jupyter-runtime recovery source metadata is invalid")
    return affected_keys, issues


def _registered_jynew_exit_timeout_recovery_stage(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
    repository_root: Path | None,
) -> tuple[set[TrialKey], list[str]]:
    """Re-audit the Jynew exit-timeout pause, canaries, and replacements."""

    migration = run_index.get("c1_source_contract_migration")
    if background_recovery_stage(migration) != JYNEW_EXIT_TIMEOUT_RECOVERY_STAGE:
        return set(), []
    issues: list[str] = []
    rows = [
        (arm, row)
        for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state")
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    pending_keys = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("status") == "pending"
    }
    failure_rows = [
        {
            "arm": "c1_agent_s_synth",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": "jynew-sidequest-trade-submap",
            "source_status": "error",
        },
        {
            "arm": "c1_state",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "jynew-sidequest-trade-submap",
            "source_status": "error",
        },
    ]
    error_keys = {
        TrialKey.from_row(str(item["arm"]), item)
        for item in failure_rows
    }
    actual_error_keys = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("status") == "error"
    }
    affected_keys = pending_keys | actual_error_keys
    if (
        source.get("run_id") != "p5-2ade80dafbeb"
        or source.get("git_commit")
        != "d89608314b6767cd2a26815a357e2c8aa567be1f"
        or source.get("harness_hash")
        != "b3a22bd498c982aa436bae6ddb2e51da8f4e7f6197a9328484539cbb77db611e"
        or len(rows) != 549
        or len(pending_keys) != 99
        or sum(row.get("status") == "ok" for _arm, row in rows) != 448
        or actual_error_keys != error_keys
    ):
        issues.append("C1 Jynew exit-timeout recovery source coverage is invalid")

    source_root = source_path.parent.parent
    pause_path = (
        source_root / "gates/c1-pause-audit-jynew-exit-timeout.json"
    ).resolve()
    canary_path = (
        source_root
        / "gates/c1-jynew-exit-timeout-canary/validated-summary.json"
    ).resolve()
    pause_raw: bytes | None = None
    pause_payload: dict[str, Any] | None = None
    canary_raw: bytes | None = None
    canary: dict[str, Any] | None = None
    try:
        pause_raw = pause_path.read_bytes()
        decoded_pause = json.loads(pause_raw)
        pause_payload = decoded_pause if isinstance(decoded_pause, dict) else None
        canary_raw = canary_path.read_bytes()
        decoded_canary = json.loads(canary_raw)
        canary = decoded_canary if isinstance(decoded_canary, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    interrupted = (
        pause_payload.get("interrupted_unknown_usage_attempts")
        if isinstance(pause_payload, dict)
        else None
    )
    if (
        pause_raw is None
        or pause_payload is None
        or hashlib.sha256(pause_raw).hexdigest()
        != migration.get("pause_audit_sha256")
        or pause_payload.get("paused_index_sha256") != source_sha
        or pause_payload.get("run_id") != source.get("run_id")
        or pause_payload.get("tuple_ledger")
        != {"total": 549, "ok": 448, "error": 2, "pending": 99}
        or not isinstance(interrupted, list)
        or len(interrupted) != 8
        or interrupted != migration.get("interrupted_unknown_usage_attempts")
    ):
        issues.append("C1 Jynew exit-timeout pause audit is invalid")
        interrupted = []
    interrupted_keys: set[TrialKey] = set()
    for item in interrupted:
        if not isinstance(item, dict):
            issues.append("C1 Jynew interrupted-attempt row is malformed")
            continue
        key = _trial_key(str(item.get("arm") or ""), item)
        attempt_dir = Path(str(item.get("attempt_dir") or "")).expanduser()
        files = [path for path in attempt_dir.rglob("*") if path.is_file()]
        if (
            key in interrupted_keys
            or key not in pending_keys
            or item.get("trial_attempt") != 0
            or item.get("persisted_trial_report") is not None
            or item.get("usage_status") != "unknown"
            or item.get("disposition")
            != "quarantine_primary_attempt_and_register_replacement"
            or item.get("file_count") != len(files)
            or item.get("total_bytes")
            != sum(path.stat().st_size for path in files)
            or _recursive_artifact_sha256(attempt_dir)
            != item.get("recursive_artifact_sha256")
        ):
            issues.append("C1 Jynew interrupted-attempt artifact is invalid")
        interrupted_keys.add(key)

    row_by_key = {
        _trial_key(arm, row): row
        for arm, row in rows
    }
    audited_failures = (
        pause_payload.get("registered_exact_usage_harness_failures")
        if isinstance(pause_payload, dict)
        else None
    )
    audited_by_key = {
        _trial_key(str(item.get("arm") or ""), item): item
        for item in (audited_failures or [])
        if isinstance(item, dict)
    }
    harness_failures_valid = bool(
        isinstance(audited_failures, list)
        and len(audited_failures) == 2
        and set(audited_by_key) == error_keys
    )
    for expected in failure_rows:
        key = TrialKey.from_row(str(expected["arm"]), expected)
        audited = audited_by_key.get(key, {})
        source_error = row_by_key.get(key, {})
        report_path = Path(str(source_error.get("report") or "")).expanduser()
        unity_report_path = Path(
            str(audited.get("successful_unity_report_after_timeout") or "")
        ).expanduser()
        screenshot_path = Path(
            str(audited.get("successful_unity_screenshot") or "")
        ).expanduser()
        try:
            report_raw = report_path.read_bytes()
            report = json.loads(report_raw)
            unity_raw = unity_report_path.read_bytes()
            unity_report = json.loads(unity_raw)
            screenshot_raw = screenshot_path.read_bytes()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            harness_failures_valid = False
            continue
        results = report.get("results") if isinstance(report, dict) else None
        result = (
            results[0]
            if isinstance(results, list)
            and len(results) == 1
            and isinstance(results[0], dict)
            else None
        )
        actual_cost = result.get("actual_cost") if isinstance(result, dict) else None
        unity_checks = (
            unity_report.get("checks") if isinstance(unity_report, dict) else None
        )
        if (
            {
                field: audited.get(field)
                for field in ("arm", "model", "seed", "task_id", "source_status")
            }
            != expected
            or audited.get("report") != str(report_path)
            or audited.get("report_sha256")
            != hashlib.sha256(report_raw).hexdigest()
            or source_error.get("report_sha256")
            != hashlib.sha256(report_raw).hexdigest()
            or not isinstance(report, dict)
            or report.get("complete") is not False
            or not isinstance(result, dict)
            or result.get("incomplete") is not True
            or not isinstance(actual_cost, dict)
            or actual_cost.get("usage_complete") is not True
            or any(
                actual_cost.get(field) != 0
                for field in (
                    "unknown_prompt_token_calls",
                    "unknown_completion_token_calls",
                    "unknown_reasoning_token_calls",
                    "unknown_total_token_calls",
                )
            )
            or "Unity oracle" not in str(result.get("reason") or "")
            or "timed out after 300.0 seconds" not in str(result.get("reason") or "")
            or audited.get("successful_unity_report_after_timeout_sha256")
            != hashlib.sha256(unity_raw).hexdigest()
            or not isinstance(unity_report, dict)
            or unity_report.get("status") != "success"
            or unity_report.get("screenshot_status") != "success"
            or unity_report.get("screenshot_source") != "unity_capture"
            or not isinstance(unity_checks, list)
            or not unity_checks
            or any(check.get("status") != "success" for check in unity_checks)
            or audited.get("successful_unity_report_check_count")
            != len(unity_checks)
            or Path(str(unity_report.get("screenshot_path") or "")).resolve()
            != screenshot_path.resolve()
            or len(screenshot_raw) < 2_000
            or audited.get("successful_unity_screenshot_sha256")
            != hashlib.sha256(screenshot_raw).hexdigest()
        ):
            harness_failures_valid = False
    if not harness_failures_valid:
        issues.append("C1 Jynew exit-timeout harness failures are invalid")

    if not _jynew_exit_timeout_canary_valid(
        canary=canary,
        canary_raw=canary_raw,
        migration=migration,
        source_root=source_root,
        repository_root=repository_root,
    ):
        issues.append("C1 Jynew exit-timeout canary is invalid")

    changed_paths = migration.get("changed_paths")
    required_paths = {
        "tests/test_c1_trial_persistence.py",
        "tests/test_visual.py",
        "warptest/jynew_runtime.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
    }
    actual_changed_paths = _repository_changed_paths(
        repository_root,
        source.get("git_commit"),
        run_index.get("git_commit"),
    )
    changed_paths_valid = bool(
        isinstance(changed_paths, list)
        and changed_paths == sorted(set(changed_paths))
        and set(changed_paths) == required_paths
        and actual_changed_paths == changed_paths
    )
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": BACKGROUND_RESOURCE_MIGRATION_ID,
        "migration_stage": JYNEW_EXIT_TIMEOUT_RECOVERY_STAGE,
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "source_git_commit": source.get("git_commit"),
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "current_git_commit": run_index.get("git_commit"),
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "lifecycle_policy": "fresh_success_report_after_exit_timeout_v1",
        "stale_report_policy": "delete_before_launch_and_match_request_evidence_v1",
        "visual_timeout_recovery_policy": "require_informative_unity_capture_v1",
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "recursive_lineage_preserved": True,
        "treatment_semantics_unchanged": True,
        "preserved_model_failure_trials": [],
        "registered_harness_failure_trials": failure_rows,
        "pause_audit": str(pause_path),
        "pause_audit_sha256": (
            hashlib.sha256(pause_raw).hexdigest()
            if pause_raw is not None
            else None
        ),
        "interrupted_unknown_usage_attempts": interrupted,
        "runtime_canary": str(canary_path),
        "runtime_canary_sha256": (
            hashlib.sha256(canary_raw).hexdigest()
            if canary_raw is not None
            else None
        ),
        "canary_policy": "real_unity_headed_visual_plus_fault_injected_semantic_v1",
    }
    if any(migration.get(field) != value for field, value in expected.items()):
        issues.append("C1 Jynew exit-timeout migration metadata is invalid")
    if not changed_paths_valid or migration.get(
        "changed_paths_sha256"
    ) != hashlib.sha256(
        json.dumps(changed_paths or [], separators=(",", ":")).encode("utf-8")
    ).hexdigest():
        issues.append("C1 Jynew exit-timeout changed paths are invalid")

    request = run_index.get("c1_source_contract_migration_request")
    request_path = (
        Path(request).expanduser()
        if isinstance(request, str) and request
        else None
    )
    artifact_valid = False
    if request_path is not None and request_path.is_file():
        raw = request_path.read_bytes()
        try:
            artifact_payload = json.loads(raw)
        except json.JSONDecodeError:
            artifact_payload = None
        metadata_payload = {
            key: value
            for key, value in migration.items()
            if key not in {"artifact", "artifact_sha256"}
        }
        artifact_valid = bool(
            migration.get("artifact") == str(request_path.resolve())
            and migration.get("artifact_sha256")
            == hashlib.sha256(raw).hexdigest()
            and artifact_payload == metadata_payload
            and source_meta.get("contract_migration") == migration
        )
    if not artifact_valid:
        issues.append("C1 Jynew exit-timeout migration artifact is invalid")

    imported_overhead = {
        _trial_key(str(row.get("arm") or ""), row): row
        for row in (run_index.get("c1_operational_overhead") or [])
        if isinstance(row, dict)
        and isinstance(row.get("source_provenance"), dict)
        and row["source_provenance"].get("source_index_sha256") == source_sha
        and isinstance(row["source_provenance"].get("interrupted_attempt"), dict)
    }
    if set(imported_overhead) != interrupted_keys:
        issues.append("C1 Jynew exit-timeout quarantine overhead is incomplete")
    for key in interrupted_keys & set(imported_overhead):
        row = imported_overhead[key]
        if (
            row.get("status") != "quarantined_unknown_usage"
            or row.get("report") != str(pause_path)
            or row.get("report_sha256") != migration.get("pause_audit_sha256")
            or row.get("known_and_unknown_usage")
            != _interrupted_unknown_usage_snapshot()
        ):
            issues.append("C1 Jynew exit-timeout quarantine overhead is invalid")
    harness_overhead = {
        _trial_key(str(row.get("arm") or ""), row)
        for row in (run_index.get("c1_operational_overhead") or [])
        if isinstance(row, dict)
        and row.get("status")
        == "replaced_exact_usage_retryable_infrastructure"
        and isinstance(row.get("source_provenance"), dict)
        and row["source_provenance"].get("source_index_sha256") == source_sha
    }
    if harness_overhead != error_keys:
        issues.append("C1 Jynew exit-timeout harness overhead is incomplete")
    if any(
        source_meta.get(field) != value
        for field, value in {
            "source_index": str(source_path),
            "source_index_sha256": source_sha,
            "source_run_id": source.get("run_id"),
            "completed_trials": 448,
            "replacement_trials": 101,
            "quarantined_trials": 8,
            "exact_usage_timeout_trials": 0,
            "exact_usage_retryable_infrastructure_trials": 2,
            "imported_nonretryable_model_failure_trials": 3,
            "contract_migration": migration,
        }.items()
    ):
        issues.append("C1 Jynew exit-timeout recovery source metadata is invalid")
    return affected_keys, issues


def _registered_screen_size_probe_cache_recovery_stage(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
    repository_root: Path | None,
) -> tuple[set[TrialKey], list[str]]:
    """Re-audit the pre-provider screen probe failure and single-flight fix."""

    migration = run_index.get("c1_source_contract_migration")
    if (
        background_recovery_stage(migration)
        != SCREEN_SIZE_PROBE_CACHE_RECOVERY_STAGE
    ):
        return set(), []
    issues: list[str] = []
    rows = [
        (arm, row)
        for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state")
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    pending_keys = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("status") == "pending"
    }
    quarantine_keys = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("status") == "quarantined_unknown_usage"
    }
    error_keys = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("status") == "error"
    }
    expected_errors = {
        TrialKey(
            "c1_agent_s",
            "kimi-k2.6@relay",
            0,
            "luanti-water-source",
        ),
        TrialKey(
            "c1_agent_s_synth",
            "kimi-k2.6@relay",
            0,
            "2048-merge-up",
        ),
        TrialKey(
            "c1_agent_s_synth",
            "kimi-k2.6@relay",
            0,
            "love-shopping-mall-spa",
        ),
    }
    affected_keys = pending_keys | quarantine_keys | error_keys
    if (
        source.get("run_id") != "p5-ecb702f63cf4"
        or source.get("git_commit")
        != "d18542757cdf178976eb33922c407ab2745dc813"
        or source.get("harness_hash")
        != "59b63b0407cf85414d930bcdfb25ba0c62442abce4a188a226c83e4e57b22237"
        or len(rows) != 549
        or len(pending_keys) != 64
        or len(quarantine_keys) != 2
        or error_keys != expected_errors
        or sum(row.get("status") == "ok" for _arm, row in rows) != 480
        or len(affected_keys) != 69
    ):
        issues.append("C1 screen-size probe recovery source coverage is invalid")

    source_root = source_path.parent.parent
    audit_path = (
        source_root / "gates/c1-screen-size-probe-timeout-audit.json"
    ).resolve()
    canary_path = (
        source_root / "gates/c1-screen-size-probe-cache-canary.json"
    ).resolve()
    audit_raw: bytes | None = None
    audit: dict[str, Any] | None = None
    canary_raw: bytes | None = None
    canary: dict[str, Any] | None = None
    try:
        audit_raw = audit_path.read_bytes()
        decoded_audit = json.loads(audit_raw)
        audit = decoded_audit if isinstance(decoded_audit, dict) else None
        canary_raw = canary_path.read_bytes()
        decoded_canary = json.loads(canary_raw)
        canary = decoded_canary if isinstance(decoded_canary, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    run_log_path = Path(str((audit or {}).get("run_log") or "")).expanduser()
    try:
        run_log_raw = run_log_path.read_bytes()
    except OSError:
        run_log_raw = b""
    audited_attempts = (
        audit.get("registered_attempts") if isinstance(audit, dict) else None
    )
    audited_by_key = {
        _trial_key(str(item.get("arm") or ""), item): item
        for item in (audited_attempts or [])
        if isinstance(item, dict)
    }
    audit_valid = bool(
        audit_raw is not None
        and isinstance(audit, dict)
        and hashlib.sha256(audit_raw).hexdigest()
        == migration.get("pause_audit_sha256")
        and audit.get("run_id") == source.get("run_id")
        and audit.get("source_index") == str(source_path)
        and audit.get("source_index_sha256") == source_sha
        and audit.get("tuple_ledger")
        == {
            "total": 549,
            "ok": 480,
            "pending": 64,
            "error": 3,
            "quarantined_unknown_usage": 2,
        }
        and audit.get("interrupted_unknown_usage_attempts") == []
        and audit.get("failure", {}).get("provider_calls_before_failure") == 0
        and audit.get("failure", {}).get("process_exit_was_autonomous") is True
        and audit.get("failure", {}).get("usage_disposition")
        == "exact_zero_retryable_infrastructure"
        and audit.get("run_log_sha256")
        == hashlib.sha256(run_log_raw).hexdigest()
        and b"screencapture" in run_log_raw
        and b"timed out after 15 seconds" in run_log_raw
        and set(audited_by_key) == expected_errors
    )
    row_by_key = {
        _trial_key(arm, row): row
        for arm, row in rows
    }
    for key in expected_errors:
        audited = audited_by_key.get(key, {})
        source_error = row_by_key.get(key, {})
        report_path = Path(str(source_error.get("report") or "")).expanduser()
        sidecar_path = report_path.parent / "p5-invocation-context.json"
        try:
            report_raw = report_path.read_bytes()
            report = json.loads(report_raw)
            sidecar_raw = sidecar_path.read_bytes()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            audit_valid = False
            continue
        results = report.get("results") if isinstance(report, dict) else None
        result = (
            results[0]
            if isinstance(results, list)
            and len(results) == 1
            and isinstance(results[0], dict)
            else None
        )
        actual_cost = result.get("actual_cost") if isinstance(result, dict) else None
        attempts = result.get("attempt_costs") if isinstance(result, dict) else None
        latest = attempts[-1] if isinstance(attempts, list) and attempts else None
        if (
            not isinstance(result, dict)
            or report.get("complete") is not False
            or report.get("retryable_incomplete") is not True
            or result.get("incomplete") is not True
            or result.get("retryable_incomplete") is not True
            or result.get("infrastructure_error") is not True
            or result.get("timed_out") is True
            or not isinstance(actual_cost, dict)
            or actual_cost.get("usage_complete") is not True
            or actual_cost.get("llm_calls") != 0
            or not isinstance(latest, dict)
            or latest.get("attempt_status") != "retryable_infrastructure"
            or latest.get("retry_category") != "infra"
            or audited.get("report") != str(report_path)
            or audited.get("report_sha256")
            != hashlib.sha256(report_raw).hexdigest()
            or source_error.get("report_sha256")
            != hashlib.sha256(report_raw).hexdigest()
            or audited.get("sidecar_sha256")
            != hashlib.sha256(sidecar_raw).hexdigest()
        ):
            audit_valid = False
    if not audit_valid:
        issues.append("C1 screen-size probe timeout audit is invalid")

    if not (
        canary_raw is not None
        and isinstance(canary, dict)
        and hashlib.sha256(canary_raw).hexdigest()
        == migration.get("runtime_canary_sha256")
        and canary.get("passed") is True
        and canary.get("policy")
        == "real_screencapture_32_thread_single_flight_v1"
        and canary.get("thread_count") == 32
        and canary.get("repetitions_per_thread") == 8
        and canary.get("physical_capture_count") == 1
        and canary.get("unique_dimensions") == [[3024, 1964]]
        and canary.get("errors") == []
        and canary.get("git_commit") == run_index.get("git_commit")
        and canary.get("harness_hash") == run_index.get("harness_hash")
    ):
        issues.append("C1 screen-size probe single-flight canary is invalid")

    changed_paths = migration.get("changed_paths")
    required_paths = {
        "tests/test_c1_trial_persistence.py",
        "tests/test_native_window.py",
        "warptest/native_window.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
    }
    actual_changed_paths = _repository_changed_paths(
        repository_root,
        source.get("git_commit"),
        run_index.get("git_commit"),
    )
    changed_paths_valid = bool(
        isinstance(changed_paths, list)
        and changed_paths == sorted(required_paths)
        and actual_changed_paths == changed_paths
    )
    failure_rows = [
        {
            "arm": key[0],
            "model": key[1],
            "seed": key[2],
            "task_id": key[3],
            "source_status": "error",
        }
        for key in sorted(expected_errors)
    ]
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": BACKGROUND_RESOURCE_MIGRATION_ID,
        "migration_stage": SCREEN_SIZE_PROBE_CACHE_RECOVERY_STAGE,
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "source_git_commit": source.get("git_commit"),
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "current_git_commit": run_index.get("git_commit"),
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "probe_policy": (
            "process_wide_single_flight_cached_backing_store_measurement_v1"
        ),
        "failure_usage_policy": (
            "pre_provider_exact_zero_retryable_infrastructure_v1"
        ),
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "recursive_lineage_preserved": True,
        "treatment_semantics_unchanged": True,
        "preserved_model_failure_trials": [],
        "registered_harness_failure_trials": failure_rows,
        "pause_audit": str(audit_path),
        "pause_audit_sha256": (
            hashlib.sha256(audit_raw).hexdigest()
            if audit_raw is not None
            else None
        ),
        "interrupted_unknown_usage_attempts": [],
        "runtime_canary": str(canary_path),
        "runtime_canary_sha256": (
            hashlib.sha256(canary_raw).hexdigest()
            if canary_raw is not None
            else None
        ),
        "canary_policy": "real_screencapture_32_thread_single_flight_v1",
    }
    if any(migration.get(field) != value for field, value in expected.items()):
        issues.append("C1 screen-size probe migration metadata is invalid")
    if not changed_paths_valid or migration.get(
        "changed_paths_sha256"
    ) != hashlib.sha256(
        json.dumps(changed_paths or [], separators=(",", ":")).encode("utf-8")
    ).hexdigest():
        issues.append("C1 screen-size probe changed paths are invalid")

    request = run_index.get("c1_source_contract_migration_request")
    request_path = (
        Path(request).expanduser()
        if isinstance(request, str) and request
        else None
    )
    artifact_valid = False
    if request_path is not None and request_path.is_file():
        raw = request_path.read_bytes()
        try:
            artifact_payload = json.loads(raw)
        except json.JSONDecodeError:
            artifact_payload = None
        metadata_payload = {
            key: value
            for key, value in migration.items()
            if key not in {"artifact", "artifact_sha256"}
        }
        artifact_valid = bool(
            migration.get("artifact") == str(request_path.resolve())
            and migration.get("artifact_sha256")
            == hashlib.sha256(raw).hexdigest()
            and artifact_payload == metadata_payload
            and source_meta.get("contract_migration") == migration
        )
    if not artifact_valid:
        issues.append("C1 screen-size probe migration artifact is invalid")

    imported_error_overhead = {
        _trial_key(str(row.get("arm") or ""), row)
        for row in (run_index.get("c1_operational_overhead") or [])
        if isinstance(row, dict)
        and row.get("status") == "replaced_exact_usage_retryable_infrastructure"
        and isinstance(row.get("source_provenance"), dict)
        and row["source_provenance"].get("source_index_sha256") == source_sha
        and _trial_key(str(row.get("arm") or ""), row) in expected_errors
    }
    if imported_error_overhead != expected_errors:
        issues.append("C1 screen-size probe imported overhead is incomplete")
    if any(
        source_meta.get(field) != value
        for field, value in {
            "source_index": str(source_path),
            "source_index_sha256": source_sha,
            "source_run_id": source.get("run_id"),
            "completed_trials": 480,
            "replacement_trials": 69,
            "quarantined_trials": 2,
            "exact_usage_timeout_trials": 0,
            "exact_usage_retryable_infrastructure_trials": 3,
            "imported_nonretryable_model_failure_trials": 3,
            "contract_migration": migration,
        }.items()
    ):
        issues.append("C1 screen-size probe recovery source metadata is invalid")
    return affected_keys, issues


def _jynew_exit_timeout_canary_valid(
    *,
    canary: dict[str, Any] | None,
    canary_raw: bytes | None,
    migration: dict[str, Any],
    source_root: Path,
    repository_root: Path | None,
) -> bool:
    if (
        not isinstance(canary, dict)
        or canary_raw is None
        or canary.get("passed") is not True
        or canary.get("policy")
        != "real_unity_headed_visual_plus_fault_injected_semantic_v1"
        or hashlib.sha256(canary_raw).hexdigest()
        != migration.get("runtime_canary_sha256")
    ):
        return False
    canary_root = (
        source_root / "gates/c1-jynew-exit-timeout-canary"
    ).resolve()
    paths = {
        "headed_visual_canary": canary_root / "headed-visual-summary.json",
        "fault_injected_semantic_canary": canary_root
        / "fault-injected-semantic-summary.json",
        "invalid_visual_rejection_canary": canary_root
        / "fault-injected-summary.json",
        "fault_wrapper": canary_root / "unity-success-then-hold.sh",
    }
    loaded: dict[str, Any] = {}
    try:
        for field, expected_path in paths.items():
            if Path(str(canary.get(field) or "")).expanduser().resolve() != expected_path:
                return False
            raw = expected_path.read_bytes()
            if canary.get(field + "_sha256") != hashlib.sha256(raw).hexdigest():
                return False
            loaded[field] = raw if field == "fault_wrapper" else json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    visual = loaded["headed_visual_canary"]
    semantic = loaded["fault_injected_semantic_canary"]
    negative = loaded["invalid_visual_rejection_canary"]
    if not all(isinstance(item, dict) for item in (visual, semantic, negative)):
        return False
    spec_sha = None
    if repository_root is not None:
        spec_path = repository_root / "specs/jynew/c1-sidequest-trade-submap.yaml"
        try:
            spec_sha = hashlib.sha256(spec_path.read_bytes()).hexdigest()
        except OSError:
            return False

    def file_bound(payload: dict[str, Any], value: Any, field: str) -> bool:
        try:
            raw = Path(str(value or "")).expanduser().read_bytes()
        except OSError:
            return False
        return payload.get(field) == hashlib.sha256(raw).hexdigest()

    visual_result = visual.get("result")
    visual_utility = (
        visual_result.get("utility_report")
        if isinstance(visual_result, dict)
        else None
    )
    visual_checks = (
        visual_result.get("checks") if isinstance(visual_result, dict) else None
    )
    semantic_result = semantic.get("result")
    semantic_utility = (
        semantic_result.get("utility_report")
        if isinstance(semantic_result, dict)
        else None
    )
    semantic_checks = (
        semantic_result.get("checks")
        if isinstance(semantic_result, dict)
        else None
    )
    negative_result = negative.get("result")
    negative_detail = (
        negative_result.get("utility_report", {}).get("detail")
        if isinstance(negative_result, dict)
        else ""
    )
    return bool(
        visual.get("passed") is True
        and visual.get("headless") is False
        and visual.get("visual_capture") is True
        and visual.get("source_unchanged") is True
        and visual.get("sandbox_cleaned") is True
        and (spec_sha is None or visual.get("spec_sha256") == spec_sha)
        and isinstance(visual_result, dict)
        and visual_result.get("status") == "success"
        and visual_result.get("live_source") == "unity_capture"
        and visual_result.get("screenshot_status") == "success"
        and visual_result.get("screenshot_source") == "unity_capture"
        and isinstance(visual_utility, dict)
        and visual_utility.get("evidence_task_id")
        == "jynew-sidequest-trade-submap"
        and visual_utility.get("evidence_stage") == "canary"
        and isinstance(visual_checks, list)
        and bool(visual_checks)
        and all(check.get("status") == "success" for check in visual_checks)
        and file_bound(
            visual, visual_result.get("report_path"), "result_report_sha256"
        )
        and file_bound(
            visual, visual_utility.get("screenshot_path"), "screenshot_sha256"
        )
        and semantic.get("passed") is True
        and semantic.get("fault_injection")
        == "real_unity_success_then_launcher_hold_v1"
        and semantic.get("source_unchanged") is True
        and semantic.get("sandbox_cleaned") is True
        and (spec_sha is None or semantic.get("spec_sha256") == spec_sha)
        and isinstance(semantic_result, dict)
        and semantic_result.get("status") == "success"
        and semantic_result.get("process_exit_status")
        == "timed_out_after_success_report"
        and semantic_result.get("report_recovered_after_exit_timeout") is True
        and isinstance(semantic_utility, dict)
        and semantic_utility.get("status") == "success"
        and semantic_utility.get("evidence_task_id")
        == "jynew-sidequest-trade-submap"
        and semantic_utility.get("evidence_stage") == "canary"
        and isinstance(semantic_checks, list)
        and len(semantic_checks) == 16
        and all(check.get("status") == "success" for check in semantic_checks)
        and file_bound(
            semantic,
            semantic_result.get("report_path"),
            "result_report_sha256",
        )
        and negative.get("passed") is False
        and negative.get("source_unchanged") is True
        and negative.get("sandbox_cleaned") is True
        and isinstance(negative_result, dict)
        and negative_result.get("status") == "failure"
        and negative_result.get("process_exit_status")
        == "timed_out_without_valid_success_report"
        and "informative Unity capture" in str(negative_detail or "")
        and isinstance(canary.get("checks"), dict)
        and bool(canary["checks"])
        and all(value is True for value in canary["checks"].values())
    )


def _registered_terminal_nonretryable_ledger_stage(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
    repository_root: Path | None = None,
) -> tuple[set[TrialKey], list[str]]:
    """Audit the treatment-neutral terminal-ledger continuation stage."""

    migration = run_index.get("c1_source_contract_migration")
    if (
        not isinstance(migration, dict)
        or background_recovery_stage(migration) != TERMINAL_LEDGER_RECOVERY_STAGE
    ):
        return set(), []
    preserved = [
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "pal3-story-party-scene-roundtrip",
            "source_status": "error",
        }
    ]
    terminal_keys = {
        TrialKey.from_row(str(row["arm"]), row)
        for row in preserved
    }
    changed_paths = [
        "tests/test_c1_trial_persistence.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
    ]
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": BACKGROUND_RESOURCE_MIGRATION_ID,
        "migration_stage": TERMINAL_LEDGER_RECOVERY_STAGE,
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "source_git_commit": source.get("git_commit"),
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "current_git_commit": run_index.get("git_commit"),
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "terminal_ledger_policy": "nonretryable_report_is_terminal_row_v1",
        "affected_trials": [],
        "preserved_model_failure_trials": preserved,
        "registered_harness_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "recursive_lineage_preserved": True,
        "treatment_semantics_unchanged": True,
        "changed_paths": changed_paths,
        "changed_paths_sha256": hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    issues: list[str] = []
    if any(migration.get(field) != value for field, value in expected.items()):
        issues.append("C1 terminal-ledger migration metadata is invalid")
    if (
        source.get("run_id") != "p5-1a85a3d52fc4"
        or source.get("git_commit")
        != "bc7ef6f90f0148a6ed7a44a53e65068d1de80338"
        or source.get("harness_hash")
        != "009890a3f956929258ef9fc101b12fde15dea6bef31b41bbe904eb1627535d17"
    ):
        issues.append("C1 terminal-ledger migration source mismatch")
    rows = [
        (arm, row)
        for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state")
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    errors = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("status") == "error"
    }
    if (
        len(rows) != 549
        or sum(row.get("status") == "ok" for _arm, row in rows) != 536
        or sum(row.get("status") == "pending" for _arm, row in rows) != 12
        or errors != terminal_keys
    ):
        issues.append("C1 terminal-ledger migration coverage is invalid")
    actual_changed_paths = _repository_changed_paths(
        repository_root,
        source.get("git_commit"),
        run_index.get("git_commit"),
    )
    if actual_changed_paths != changed_paths:
        issues.append("C1 terminal-ledger migration diff binding is invalid")
    request = run_index.get("c1_source_contract_migration_request")
    request_path = Path(request).expanduser() if isinstance(request, str) and request else None
    if request_path is None or not request_path.is_file():
        issues.append("C1 terminal-ledger migration artifact is missing")
    else:
        raw = request_path.read_bytes()
        try:
            artifact_payload = json.loads(raw)
        except json.JSONDecodeError:
            artifact_payload = None
        metadata_payload = {
            key: value
            for key, value in migration.items()
            if key not in {"artifact", "artifact_sha256"}
        }
        if (
            migration.get("artifact") != str(request_path.resolve())
            or migration.get("artifact_sha256") != hashlib.sha256(raw).hexdigest()
            or artifact_payload != metadata_payload
            or source_meta.get("contract_migration") != migration
        ):
            issues.append("C1 terminal-ledger migration binding is invalid")
    return terminal_keys, issues


def _registered_background_resource_partition_migration(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
    repository_root: Path | None = None,
) -> tuple[set[TrialKey], list[str]]:
    """Audit the background-input partition and pause ledger independently."""

    migration = run_index.get("c1_source_contract_migration")
    if not isinstance(migration, dict) or migration.get(
        "migration_id"
    ) != BACKGROUND_RESOURCE_MIGRATION_ID:
        return set(), []
    migration_stage = background_recovery_stage(migration)
    if migration_stage == JUPYTER_RUNTIME_RECOVERY_STAGE:
        return _registered_jupyter_runtime_recovery_stage(
            run_index=run_index,
            source=source,
            source_path=source_path,
            source_sha=source_sha,
            source_meta=source_meta,
            repository_root=repository_root,
        )
    if migration_stage == JYNEW_EXIT_TIMEOUT_RECOVERY_STAGE:
        return _registered_jynew_exit_timeout_recovery_stage(
            run_index=run_index,
            source=source,
            source_path=source_path,
            source_sha=source_sha,
            source_meta=source_meta,
            repository_root=repository_root,
        )
    if migration_stage == SCREEN_SIZE_PROBE_CACHE_RECOVERY_STAGE:
        return _registered_screen_size_probe_cache_recovery_stage(
            run_index=run_index,
            source=source,
            source_path=source_path,
            source_sha=source_sha,
            source_meta=source_meta,
            repository_root=repository_root,
        )
    if migration_stage == TERMINAL_LEDGER_RECOVERY_STAGE:
        return _registered_terminal_nonretryable_ledger_stage(
            run_index=run_index,
            source=source,
            source_path=source_path,
            source_sha=source_sha,
            source_meta=source_meta,
            repository_root=repository_root,
        )
    issues: list[str] = []
    preserved = [
        {
            "arm": "c1_agent_s_synth",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": "jynew-submap-save-restore",
            "source_status": "error",
        },
        {
            "arm": "c1_state",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "2048-merge-down",
            "source_status": "error",
        },
    ]
    rows = [
        (arm, row)
        for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state")
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    pending_keys = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("status") == "pending"
    }
    expected_error_keys = {
        TrialKey.from_row(str(item["arm"]), item)
        for item in preserved
    }
    actual_error_keys = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("status") == "error"
    }
    if (
        source.get("run_id") != "p5-f845d991a94a"
        or source.get("git_commit")
        != "aa5098967387ff07d4e4c1f3775c9346ddc036f8"
        or source.get("harness_hash")
        != "25b4df907d41cb6fedf5ab3f74f05a36d2891360aaa1cf95cf5816e3d2f962aa"
        or len(rows) != 549
        or len(pending_keys) != 172
        or sum(row.get("status") == "ok" for _arm, row in rows) != 375
        or actual_error_keys != expected_error_keys
    ):
        issues.append("C1 background-partition source coverage is invalid")

    pause_path = Path(str(migration.get("pause_audit") or "")).expanduser()
    pause_raw: bytes | None = None
    pause_payload: dict[str, Any] | None = None
    try:
        pause_raw = pause_path.read_bytes()
        decoded = json.loads(pause_raw)
        pause_payload = decoded if isinstance(decoded, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    interrupted = (
        pause_payload.get("interrupted_unknown_usage_attempts")
        if isinstance(pause_payload, dict)
        else None
    )
    if (
        pause_raw is None
        or hashlib.sha256(pause_raw).hexdigest()
        != migration.get("pause_audit_sha256")
        or not isinstance(interrupted, list)
        or len(interrupted) != 5
        or interrupted != migration.get("interrupted_unknown_usage_attempts")
        or pause_payload.get("paused_index_sha256") != source_sha
    ):
        issues.append("C1 background-partition pause audit binding is invalid")
        interrupted = []
    interrupted_keys: set[TrialKey] = set()
    for item in interrupted:
        if not isinstance(item, dict):
            issues.append("C1 background-partition interrupted row is malformed")
            continue
        key = _trial_key(str(item.get("arm") or ""), item)
        if (
            key in interrupted_keys
            or key not in pending_keys
            or item.get("trial_attempt") != 0
            or item.get("usage_status") != "unknown"
            or item.get("disposition")
            != "quarantine_primary_attempt_and_register_replacement"
            or _recursive_artifact_sha256(
                Path(str(item.get("attempt_dir") or "")).expanduser()
            )
            != item.get("recursive_artifact_sha256")
        ):
            issues.append("C1 background-partition interrupted artifact is invalid")
        interrupted_keys.add(key)

    canary_root = (
        repository_root
        / "reports/warptest/c1-background-resource-partition-canary-20260807"
        if repository_root is not None
        else None
    )
    background_canary_path = (
        (canary_root / "background-final/summary.json").resolve()
        if canary_root is not None
        else None
    )
    unity_canary_path = (
        (canary_root / "unity-final/summary.json").resolve()
        if canary_root is not None
        else None
    )
    background_canary_raw: bytes | None = None
    unity_canary_raw: bytes | None = None
    background_canary: dict[str, Any] | None = None
    unity_canary: dict[str, Any] | None = None
    try:
        if background_canary_path is not None and unity_canary_path is not None:
            background_canary_raw = background_canary_path.read_bytes()
            decoded_background = json.loads(background_canary_raw)
            background_canary = (
                decoded_background
                if isinstance(decoded_background, dict)
                else None
            )
            unity_canary_raw = unity_canary_path.read_bytes()
            decoded_unity = json.loads(unity_canary_raw)
            unity_canary = decoded_unity if isinstance(decoded_unity, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    background_results = (
        background_canary.get("results")
        if isinstance(background_canary, dict)
        else None
    )
    unity_rows = (
        unity_canary.get("benchmarks")
        if isinstance(unity_canary, dict)
        else None
    )
    if (
        background_canary_raw is None
        or unity_canary_raw is None
        or not isinstance(background_canary, dict)
        or background_canary.get("passed") is not True
        or background_canary.get("benchmarks") != ["luanti", "openra"]
        or not isinstance(background_results, list)
        or len(background_results) != 2
        or not all(isinstance(row, dict) for row in background_results)
        or any(
            row.get("passed") is not True
            or row.get("overlap_ns", 0) <= 0
            or row.get("distinct_pids") is not True
            or row.get("distinct_sessions") is not True
            or row.get("distinct_support_dirs") is not True
            or row.get("distinct_report_dirs") is not True
            or row.get("source_unchanged") is not True
            or row.get("sandboxes_cleaned") is not True
            for row in background_results
        )
        or not isinstance(unity_canary, dict)
        or not isinstance(unity_rows, dict)
        or unity_canary.get("serialized_benchmarks") != ["jynew"]
        or unity_canary.get("blocking_benchmarks") != []
        or unity_canary.get("paid_recovery_safe") is not True
        or unity_rows.get("pal3", {}).get("passed") is not True
        or unity_rows.get("jynew", {}).get("editor_overlap_passed") is not True
        or unity_rows.get("jynew", {}).get("mutable_storage_isolated") is not False
    ):
        issues.append("C1 background-partition concurrency canary is invalid")

    changed_paths = migration.get("changed_paths")
    allowed_paths = {
        "docs/c1_concurrency.md",
        "tests/test_c1_concurrency.py",
        "tests/test_c1_task_resources.py",
        "tests/test_c1_trial_persistence.py",
        "tests/test_p5.py",
        "tests/test_runtime_isolation.py",
        "warptest/c1_native_canary.py",
        "warptest/c1_task_resources.py",
        "warptest/c1_unity_canary.py",
        "warptest/gui_agent.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
        "warptest/state_loop.py",
    }
    required_paths = {
        "warptest/c1_native_canary.py",
        "warptest/c1_task_resources.py",
        "warptest/c1_unity_canary.py",
        "warptest/gui_agent.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
        "warptest/state_loop.py",
    }
    actual_changed_paths = _repository_changed_paths(
        repository_root,
        source.get("git_commit"),
        run_index.get("git_commit"),
    )
    changed_paths_valid = bool(
        isinstance(changed_paths, list)
        and changed_paths == sorted(set(changed_paths))
        and required_paths.issubset(changed_paths)
        and set(changed_paths).issubset(allowed_paths)
        and actual_changed_paths == changed_paths
    )
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": BACKGROUND_RESOURCE_MIGRATION_ID,
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "source_git_commit": source.get("git_commit"),
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "current_git_commit": run_index.get("git_commit"),
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "resource_policy": "background_native_partition_v1",
        "scheduler_policy": "global_resource_ready_round_robin_v2",
        "scheduler_lease_binding": "c1_task_resource_config_v1",
        "native_input_compatibility": "explicit_foreground_required_v1",
        "unity_serialization_policy": "per_benchmark_canary_fallback_v1",
        "serialized_unity_benchmarks": ["jynew"],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "recursive_lineage_preserved": True,
        "treatment_semantics_unchanged": True,
        "preserved_model_failure_trials": preserved,
        "registered_harness_failure_trials": [],
        "pause_audit": str(pause_path),
        "pause_audit_sha256": (
            hashlib.sha256(pause_raw).hexdigest() if pause_raw is not None else None
        ),
        "interrupted_unknown_usage_attempts": interrupted,
        "background_concurrency_canary": (
            str(background_canary_path)
            if background_canary_path is not None
            else None
        ),
        "background_concurrency_canary_sha256": (
            hashlib.sha256(background_canary_raw).hexdigest()
            if background_canary_raw is not None
            else None
        ),
        "unity_concurrency_canary": (
            str(unity_canary_path) if unity_canary_path is not None else None
        ),
        "unity_concurrency_canary_sha256": (
            hashlib.sha256(unity_canary_raw).hexdigest()
            if unity_canary_raw is not None
            else None
        ),
        "canary_policy": "no_provider_all_background_backends_v1",
    }
    if any(migration.get(field) != value for field, value in expected.items()):
        issues.append("C1 background-partition migration metadata is invalid")
    if not changed_paths_valid or migration.get(
        "changed_paths_sha256"
    ) != hashlib.sha256(
        json.dumps(changed_paths or [], separators=(",", ":")).encode("utf-8")
    ).hexdigest():
        issues.append("C1 background-partition changed paths are invalid")
    if (
        run_index.get("c1_unity_serialized_benchmarks") != ["jynew"]
        or run_index.get("c1_unity_serialized") is not True
    ):
        issues.append("C1 background-partition Unity fallback is invalid")

    request = run_index.get("c1_source_contract_migration_request")
    request_path = (
        Path(request).expanduser()
        if isinstance(request, str) and request
        else None
    )
    artifact_valid = False
    if request_path is not None and request_path.is_file():
        raw = request_path.read_bytes()
        try:
            artifact_payload = json.loads(raw)
        except json.JSONDecodeError:
            artifact_payload = None
        metadata_payload = {
            key: value
            for key, value in migration.items()
            if key not in {"artifact", "artifact_sha256"}
        }
        artifact_valid = bool(
            migration.get("artifact") == str(request_path.resolve())
            and migration.get("artifact_sha256")
            == hashlib.sha256(raw).hexdigest()
            and artifact_payload == metadata_payload
            and source_meta.get("contract_migration") == migration
        )
    if not artifact_valid:
        issues.append("C1 background-partition migration artifact is invalid")

    imported_overhead = {
        _trial_key(str(row.get("arm") or ""), row): row
        for row in (run_index.get("c1_operational_overhead") or [])
        if isinstance(row, dict)
        and isinstance(row.get("source_provenance"), dict)
        and row["source_provenance"].get("source_index_sha256") == source_sha
        and isinstance(row["source_provenance"].get("interrupted_attempt"), dict)
    }
    if set(imported_overhead) != interrupted_keys:
        issues.append("C1 background-partition quarantine overhead is incomplete")
    for key in interrupted_keys & set(imported_overhead):
        row = imported_overhead[key]
        if (
            row.get("status") != "quarantined_unknown_usage"
            or row.get("report") != str(pause_path)
            or row.get("report_sha256") != migration.get("pause_audit_sha256")
            or row.get("known_and_unknown_usage")
            != _interrupted_unknown_usage_snapshot()
        ):
            issues.append("C1 background-partition quarantine overhead is invalid")
    if any(
        source_meta.get(field) != value
        for field, value in {
            "source_index": str(source_path),
            "source_index_sha256": source_sha,
            "source_run_id": source.get("run_id"),
            "completed_trials": 377,
            "replacement_trials": 172,
            "quarantined_trials": 5,
            "imported_nonretryable_model_failure_trials": 2,
            "contract_migration": migration,
        }.items()
    ):
        issues.append("C1 background-partition source metadata is invalid")
    return pending_keys, issues


def _registered_recovery_parallel_migration(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
    repository_root: Path | None = None,
) -> tuple[set[TrialKey], list[str]]:
    """Independently validate the generic recovery inner-lane migration."""

    migration = run_index.get("c1_source_contract_migration")
    if not isinstance(migration, dict) or migration.get("migration_id") != (
        "c1_recovery_parallel_scheduler_v1"
    ):
        return set(), []
    affected = sorted(
        (
            {
                "arm": str(arm),
                "model": row.get("model"),
                "seed": row.get("seed"),
                "task_id": str(row.get("task_id") or ""),
                "source_status": str(row.get("status") or ""),
            }
            for arm in source.get("arms_run", [])
            for row in (source.get("arms", {}).get(arm) or [])
            if isinstance(row, dict) and row.get("status") != "ok"
        ),
        key=lambda row: (
            row["arm"],
            str(row["model"]),
            str(row["seed"]),
            row["task_id"],
        ),
    )
    affected_keys = {
        TrialKey.from_row(str(row["arm"]), row)
        for row in affected
    }
    changed_paths = migration.get("changed_paths")
    allowed_paths = {
        "README.md",
        "docs/c1_concurrency.md",
        "tests/test_c1_concurrency.py",
        "tests/test_c1_trial_persistence.py",
        "tests/test_p5.py",
        "warptest/c1_concurrency.py",
        "warptest/gui_agent.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
        "warptest/state_loop.py",
    }
    required_runtime_paths = {
        "warptest/c1_concurrency.py",
        "warptest/gui_agent.py",
        "warptest/p5_scale.py",
        "warptest/state_loop.py",
    }
    allowed_migrations = ["git_commit_rebind", "harness_rebind"]
    if _recovery_preflight_contract(source.get("preflight")) != (
        _recovery_preflight_contract(run_index.get("preflight"))
    ):
        allowed_migrations.append("preflight_rebind")
    if _recovery_calibration_contract(source.get("calibration")) != (
        _recovery_calibration_contract(run_index.get("calibration"))
    ):
        allowed_migrations.append("calibration_rebind")
    allowed_migrations.sort()
    actual_changed_paths = _repository_changed_paths(
        repository_root,
        source.get("git_commit"),
        run_index.get("git_commit"),
    )
    changed_paths_valid = bool(
        isinstance(changed_paths, list)
        and all(isinstance(path, str) and path for path in changed_paths)
        and changed_paths == sorted(set(changed_paths))
        and required_runtime_paths.issubset(changed_paths)
        and set(changed_paths).issubset(allowed_paths)
        and actual_changed_paths == changed_paths
    )
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": "c1_recovery_parallel_scheduler_v1",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "source_git_commit": source.get("git_commit"),
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "current_git_commit": run_index.get("git_commit"),
        "allowed_migrations": allowed_migrations,
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "scheduler_policy": "global_resource_ready_round_robin_v2",
        "recovery_execution": "one_task_fresh_invocation_v1",
        "recovery_dispatch": "two_level_resource_ready_v1",
        "affected_trials": affected,
        "preserved_model_failure_trials": [],
        "registered_harness_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "normal_execution_unchanged": True,
        "treatment_semantics_unchanged": True,
    }
    issues: list[str] = []
    if not affected_keys or any(
        migration.get(field) != value for field, value in expected.items()
    ):
        issues.append("C1 recovery-parallel migration metadata is invalid")
    if not isinstance(migration.get("reason"), str) or not migration["reason"].strip():
        issues.append("C1 recovery-parallel migration reason is missing")
    for arm in source.get("arms_run", []):
        for row in source.get("arms", {}).get(arm, []) or []:
            if not isinstance(row, dict) or row.get("status") == "ok":
                continue
            status = str(row.get("status") or "")
            if status in {"pending", "quarantined_unknown_usage"}:
                continue
            if status != "error":
                issues.append(
                    "C1 recovery-parallel migration contains an unregistered "
                    "unfinished source status"
                )
                continue
            report_path = Path(str(row.get("report") or "")).expanduser()
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                report = None
            results = report.get("results") if isinstance(report, dict) else None
            result = (
                results[0]
                if isinstance(results, list)
                and len(results) == 1
                and isinstance(results[0], dict)
                else None
            )
            failure_kind = (
                _preserved_model_failure_kind(result=result, report=report)
                if isinstance(result, dict) and isinstance(report, dict)
                else None
            )
            task_id = str(row.get("task_id") or "")
            timeout_s = source.get("c1_task_timeout_s")
            timeout_reason = (
                f"C1 task {task_id} exceeded its {timeout_s:g}s wall-clock limit"
                if isinstance(timeout_s, (int, float))
                else None
            )
            valid_timeout = bool(
                failure_kind == "exact_usage_nonretryable_task_timeout"
                and timeout_reason is not None
                and result.get("reason") == timeout_reason
            )
            valid_retryable_infrastructure = bool(
                failure_kind
                == "exact_usage_exhausted_retryable_infrastructure"
            )
            if not (valid_timeout or valid_retryable_infrastructure):
                issues.append(
                    "C1 recovery-parallel migration contains an unregistered "
                    "source error"
                )
    if not changed_paths_valid or migration.get(
        "changed_paths_sha256"
    ) != hashlib.sha256(
        json.dumps(changed_paths or [], separators=(",", ":")).encode("utf-8")
    ).hexdigest():
        issues.append("C1 recovery-parallel migration changed paths are invalid")
    request = run_index.get("c1_source_contract_migration_request")
    request_path = (
        Path(request).expanduser()
        if isinstance(request, str) and request
        else None
    )
    artifact_valid = False
    if request_path is not None and request_path.is_file():
        raw = request_path.read_bytes()
        try:
            artifact_payload = json.loads(raw)
        except json.JSONDecodeError:
            artifact_payload = None
        metadata_payload = {
            key: value
            for key, value in migration.items()
            if key not in {"artifact", "artifact_sha256"}
        }
        artifact_valid = bool(
            migration.get("artifact") == str(request_path.resolve())
            and migration.get("artifact_sha256")
            == hashlib.sha256(raw).hexdigest()
            and artifact_payload == metadata_payload
        )
    if not artifact_valid:
        issues.append("C1 recovery-parallel migration artifact binding is invalid")
    if any(
        source_meta.get(field) != value
        for field, value in {
            "source_index": str(source_path),
            "source_index_sha256": source_sha,
            "source_run_id": source.get("run_id"),
            "contract_migration": migration,
        }.items()
    ):
        issues.append("C1 recovery-parallel source metadata is invalid")
    return affected_keys, issues


def _registered_scheduler_lineage_migration_issues(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
) -> list[str]:
    """Re-audit the scheduler migration retained inside flattened lineage."""

    migration = run_index.get("c1_source_contract_migration")
    if not isinstance(migration, dict) or migration.get("migration_id") != (
        "c1_resource_ready_scheduler_v2"
    ):
        return ["C1 flattened scheduler migration metadata is missing"]
    affected = [
        {
            "arm": "c1_state",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": "libreoffice-impress-status-deck",
            "source_status": "error",
        }
    ]
    changed_paths = [
        "tests/test_c1_concurrency.py",
        "warptest/c1_concurrency.py",
        "warptest/gui_agent.py",
        "warptest/state_loop.py",
    ]
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": "c1_resource_ready_scheduler_v2",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "source_git_commit": source.get("git_commit"),
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "current_git_commit": run_index.get("git_commit"),
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "scheduler_policy": "global_resource_ready_round_robin_v2",
        "affected_trials": affected,
        "preserved_model_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "treatment_semantics_unchanged": True,
        "changed_paths": changed_paths,
        "changed_paths_sha256": hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    issues: list[str] = []
    if any(migration.get(field) != value for field, value in expected.items()):
        issues.append("C1 flattened scheduler migration metadata is invalid")
    if (
        source.get("run_id") != "p5-b2c01ad1eff9"
        or source.get("git_commit")
        != "4ea16e460860eada08b58c93d0cc855f43978cc9"
        or source.get("harness_hash")
        != "3594191732cbba44f97f3633b677db8f6c19db410cc381e2658f0ce98b3521e4"
    ):
        issues.append("C1 flattened scheduler migration source mismatch")
    rows = [
        (arm, row)
        for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state")
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    errors = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("status") == "error"
    }
    affected_keys = {
        TrialKey.from_row(str(row["arm"]), row)
        for row in affected
    }
    if (
        len(rows) != 549
        or sum(row.get("status") == "ok" for _arm, row in rows) != 227
        or sum(row.get("status") == "pending" for _arm, row in rows) != 321
        or errors != affected_keys
    ):
        issues.append("C1 flattened scheduler migration coverage is invalid")
    request = run_index.get("c1_source_contract_migration_request")
    request_path = (
        Path(request).expanduser()
        if isinstance(request, str) and request
        else None
    )
    if request_path is None or not request_path.is_file():
        issues.append("C1 flattened scheduler migration artifact is missing")
    else:
        raw = request_path.read_bytes()
        try:
            artifact_payload = json.loads(raw)
        except json.JSONDecodeError:
            artifact_payload = None
        metadata_payload = {
            key: value
            for key, value in migration.items()
            if key not in {"artifact", "artifact_sha256"}
        }
        if (
            migration.get("artifact") != str(request_path.resolve())
            or migration.get("artifact_sha256")
            != hashlib.sha256(raw).hexdigest()
            or artifact_payload != metadata_payload
            or source_meta.get("contract_migration") != migration
        ):
            issues.append("C1 flattened scheduler migration binding is invalid")
    return issues


def _registered_retryable_ledger_migration(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
) -> tuple[set[TrialKey], list[str]]:
    """Re-audit the source-bound retryable attempt-ledger migration."""

    migration = run_index.get("c1_source_contract_migration")
    if not isinstance(migration, dict) or migration.get("migration_id") != (
        "c1_retryable_recovery_ledger_v1"
    ):
        return set(), []
    affected = [
        {
            "arm": "c1_agent_s",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": "libreoffice-impress-executive-restructure",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "openra-disable-edge-scroll",
            "source_status": "error",
        },
        {
            "arm": "c1_state",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": "libreoffice-impress-status-deck",
            "source_status": "error",
        },
        {
            "arm": "c1_state",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "luanti-set-node",
            "source_status": "error",
        },
    ]
    changed_paths = [
        "tests/test_c1_trial_persistence.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
    ]
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": "c1_retryable_recovery_ledger_v1",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "source_git_commit": source.get("git_commit"),
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "current_git_commit": run_index.get("git_commit"),
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "recovery_ledger_policy": "retryable_infrastructure_top_level_v1",
        "affected_trials": affected,
        "preserved_model_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "treatment_semantics_unchanged": True,
        "changed_paths": changed_paths,
        "changed_paths_sha256": hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    issues: list[str] = []
    if any(migration.get(field) != value for field, value in expected.items()):
        issues.append("C1 retryable-ledger migration metadata is invalid")
    if (
        source.get("run_id") != "p5-4198cb3ebfef"
        or source.get("git_commit")
        != "4033aa6de91647cb4c4ff0fe05b890dd5361ef7c"
        or source.get("harness_hash")
        != "7e423719cc29493b503844ef6ce8831abb439cee83a7cd750d52bde89cc7aa87"
    ):
        issues.append("C1 retryable-ledger migration source mismatch")
    affected_keys = {
        TrialKey.from_row(str(row["arm"]), row)
        for row in affected
    }
    rows = [
        (arm, row)
        for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state")
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    errors = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("status") == "error"
    }
    if (
        len(rows) != 549
        or sum(row.get("status") == "ok" for _arm, row in rows) != 419
        or sum(row.get("status") == "pending" for _arm, row in rows) != 126
        or errors != affected_keys
    ):
        issues.append("C1 retryable-ledger migration coverage is invalid")
    request = run_index.get("c1_source_contract_migration_request")
    request_path = (
        Path(request).expanduser()
        if isinstance(request, str) and request
        else None
    )
    if request_path is None or not request_path.is_file():
        issues.append("C1 retryable-ledger migration artifact is missing")
    else:
        raw = request_path.read_bytes()
        try:
            artifact_payload = json.loads(raw)
        except json.JSONDecodeError:
            artifact_payload = None
        metadata_payload = {
            key: value
            for key, value in migration.items()
            if key not in {"artifact", "artifact_sha256"}
        }
        if (
            migration.get("artifact") != str(request_path.resolve())
            or migration.get("artifact_sha256")
            != hashlib.sha256(raw).hexdigest()
            or artifact_payload != metadata_payload
            or source_meta.get("contract_migration") != migration
        ):
            issues.append("C1 retryable-ledger migration binding is invalid")
    return affected_keys, issues


def _registered_retryable_runtime_marker_migration(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
) -> tuple[set[TrialKey], list[str]]:
    """Re-audit the missing top-level infrastructure-marker migration."""

    migration = run_index.get("c1_source_contract_migration")
    if not isinstance(migration, dict) or migration.get("migration_id") != (
        "c1_retryable_runtime_marker_v1"
    ):
        return set(), []
    affected = [
        {
            "arm": "c1_state",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": "libreoffice-impress-executive-restructure",
            "source_status": "error",
        }
    ]
    changed_paths = [
        "tests/test_c1_trial_persistence.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
    ]
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": "c1_retryable_runtime_marker_v1",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "source_git_commit": source.get("git_commit"),
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "current_git_commit": run_index.get("git_commit"),
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "recovery_ledger_policy": (
            "retryable_infrastructure_missing_marker_v1"
        ),
        "flattened_overhead_policy": (
            "preserve_all_sha_verified_recursive_attempts_v1"
        ),
        "affected_trials": affected,
        "preserved_model_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "treatment_semantics_unchanged": True,
        "changed_paths": changed_paths,
        "changed_paths_sha256": hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    issues: list[str] = []
    if any(migration.get(field) != value for field, value in expected.items()):
        issues.append("C1 retryable-marker migration metadata is invalid")
    if (
        source.get("run_id") != "p5-8f5b4e9120ad"
        or source.get("git_commit")
        != "be82fed06f3def79a4449e6f1a5523b29af99902"
        or source.get("harness_hash")
        != "0053699cc37471f861d7eaad5e7d46b9c62f11651c866516a1ef08f155083066"
    ):
        issues.append("C1 retryable-marker migration source mismatch")
    affected_keys = {
        TrialKey.from_row(str(row["arm"]), row)
        for row in affected
    }
    expected_errors = {
        TrialKey(
            "c1_agent_s",
            "gemini-3.5-flash@relay",
            0,
            "luanti-workshop-order-staging",
        ),
        TrialKey(
            "c1_agent_s",
            "kimi-k2.6@relay",
            0,
            "jupyterlab-notebook-cell-output",
        ),
        TrialKey(
            "c1_state",
            "gemini-3.5-flash@relay",
            0,
            "libreoffice-impress-executive-restructure",
        ),
    }
    rows = [
        (arm, row)
        for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state")
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    errors = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("status") == "error"
    }
    if (
        len(rows) != 549
        or sum(row.get("status") == "ok" for _arm, row in rows) != 483
        or sum(row.get("status") == "pending" for _arm, row in rows) != 63
        or errors != expected_errors
        or any(
            row.get("status") == "quarantined_unknown_usage"
            for _arm, row in rows
        )
    ):
        issues.append("C1 retryable-marker migration coverage is invalid")
    overhead = source.get("c1_operational_overhead") or []
    duplicate_key = TrialKey(
        "c1_state",
        "gemini-3.5-flash@relay",
        0,
        "libreoffice-impress-status-deck",
    )
    duplicate_attempts = {
        row.get("attempt_id")
        for row in overhead
        if isinstance(row, dict)
        and _trial_key(str(row.get("arm") or ""), row) == duplicate_key
    }
    if (
        len(overhead) != 7
        or duplicate_attempts
        != {
            "p5-b2c01ad1eff9:c1_state:gemini-3.5-flash@relay:0:"
            "libreoffice-impress-status-deck:attempt0",
            "p5-4198cb3ebfef:c1_state:gemini-3.5-flash@relay:0:"
            "libreoffice-impress-status-deck:attempt0",
        }
    ):
        issues.append("C1 retryable-marker migration overhead is invalid")
    request = run_index.get("c1_source_contract_migration_request")
    request_path = (
        Path(request).expanduser()
        if isinstance(request, str) and request
        else None
    )
    if request_path is None or not request_path.is_file():
        issues.append("C1 retryable-marker migration artifact is missing")
    else:
        raw = request_path.read_bytes()
        try:
            artifact_payload = json.loads(raw)
        except json.JSONDecodeError:
            artifact_payload = None
        metadata_payload = {
            key: value
            for key, value in migration.items()
            if key not in {"artifact", "artifact_sha256"}
        }
        if (
            migration.get("artifact") != str(request_path.resolve())
            or migration.get("artifact_sha256")
            != hashlib.sha256(raw).hexdigest()
            or artifact_payload != metadata_payload
            or source_meta.get("contract_migration") != migration
        ):
            issues.append("C1 retryable-marker migration binding is invalid")
    return affected_keys, issues


def _registered_exhausted_recovery_terminal_migration(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
) -> tuple[set[TrialKey], list[str]]:
    """Re-audit the exhausted exact-usage terminal-failure migration."""

    migration = run_index.get("c1_source_contract_migration")
    if not isinstance(migration, dict) or migration.get("migration_id") != (
        "c1_exhausted_recovery_terminal_v1"
    ):
        return set(), []
    preserved = [
        {
            "arm": "c1_state",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": "libreoffice-impress-executive-restructure",
            "source_status": "error",
        }
    ]
    terminal_keys = {
        TrialKey.from_row(str(row["arm"]), row)
        for row in preserved
    }
    changed_paths = [
        "tests/test_c1_trial_persistence.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
    ]
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": "c1_exhausted_recovery_terminal_v1",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "source_git_commit": source.get("git_commit"),
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "current_git_commit": run_index.get("git_commit"),
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "recovery_scheduler_policy": "skip_exhausted_exact_usage_failure_v1",
        "affected_trials": [],
        "preserved_model_failure_trials": preserved,
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "treatment_semantics_unchanged": True,
        "changed_paths": changed_paths,
        "changed_paths_sha256": hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    issues: list[str] = []
    if any(migration.get(field) != value for field, value in expected.items()):
        issues.append("C1 exhausted-terminal migration metadata is invalid")
    if (
        source.get("run_id") != "p5-16eb039f7cf4"
        or source.get("git_commit")
        != "d958da794879b62ebd80ff75eefad4b89d06c0c2"
        or source.get("harness_hash")
        != "80eeff20b3f5a6f1d14c518db158229a5b07fac12ef105d7d7d8cda61cd07741"
    ):
        issues.append("C1 exhausted-terminal migration source mismatch")
    rows = [
        (arm, row)
        for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state")
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    errors = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("status") == "error"
    }
    terminal_attempts = {
        row.get("trial_attempt")
        for row in (source.get("c1_operational_overhead") or [])
        if isinstance(row, dict)
        and _trial_key(str(row.get("arm") or ""), row) in terminal_keys
        and row.get("source_provenance") is None
    }
    if (
        len(rows) != 549
        or sum(row.get("status") == "ok" for _arm, row in rows) != 539
        or sum(row.get("status") == "pending" for _arm, row in rows) != 9
        or errors != terminal_keys
        or terminal_attempts != {0, 1, 2}
    ):
        issues.append("C1 exhausted-terminal migration coverage is invalid")
    request = run_index.get("c1_source_contract_migration_request")
    request_path = Path(request).expanduser() if isinstance(request, str) and request else None
    if request_path is None or not request_path.is_file():
        issues.append("C1 exhausted-terminal migration artifact is missing")
    else:
        raw = request_path.read_bytes()
        try:
            artifact_payload = json.loads(raw)
        except json.JSONDecodeError:
            artifact_payload = None
        metadata_payload = {
            key: value
            for key, value in migration.items()
            if key not in {"artifact", "artifact_sha256"}
        }
        if (
            migration.get("artifact") != str(request_path.resolve())
            or migration.get("artifact_sha256") != hashlib.sha256(raw).hexdigest()
            or artifact_payload != metadata_payload
            or source_meta.get("contract_migration") != migration
        ):
            issues.append("C1 exhausted-terminal migration binding is invalid")
    return terminal_keys, issues


def _registered_resume_settled_failure_migration(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
) -> tuple[set[TrialKey], list[str]]:
    """Re-audit the source-specific settled-failure resume migration."""

    migration = run_index.get("c1_source_contract_migration")
    if not isinstance(migration, dict) or migration.get("migration_id") != (
        "c1_resume_settled_failure_v1"
    ):
        return set(), []
    preserved = [
        {
            "arm": "c1_state",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": "luanti-replace-node",
            "source_status": "error",
        }
    ]
    terminal_keys = {
        TrialKey.from_row(str(row["arm"]), row)
        for row in preserved
    }
    changed_paths = [
        "tests/test_c1_trial_persistence.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
    ]
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": "c1_resume_settled_failure_v1",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "source_git_commit": source.get("git_commit"),
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "current_git_commit": run_index.get("git_commit"),
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "recovery_scheduler_policy": "preserve_settled_exact_usage_failure_v1",
        "affected_trials": [],
        "preserved_model_failure_trials": preserved,
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "treatment_semantics_unchanged": True,
        "changed_paths": changed_paths,
        "changed_paths_sha256": hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "reason": "preserve one exact-usage settled failure and resume pending trials",
    }
    issues: list[str] = []
    if any(migration.get(field) != value for field, value in expected.items()):
        issues.append("C1 settled-failure migration metadata is invalid")
    if (
        source.get("run_id") != "p5-f5a2dcca1df7"
        or source.get("git_commit")
        != "3719208e1b2cd755114fc005752fc5225aeded4d"
        or source.get("harness_hash")
        != "a141e52b0d7b08499b477b016d26dfd89e8d896862df8f3279044aad57e8d732"
    ):
        issues.append("C1 settled-failure migration source mismatch")
    recovery_path = Path(str(migration.get("recovery_index") or "")).expanduser()
    try:
        recovery_raw = recovery_path.read_bytes()
        recovery = json.loads(recovery_raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        recovery_raw = b""
        recovery = None
    upstream = recovery.get("c1_sources") if isinstance(recovery, dict) else None
    expected_lineage = {
        "schema_version": 1,
        "kind": "c1_flattened_recovery_source",
        "derived_from_index": str(recovery_path),
        "derived_from_index_sha256": hashlib.sha256(recovery_raw).hexdigest(),
        "upstream_sources": copy.deepcopy(upstream),
    }
    expected_source = copy.deepcopy(recovery) if isinstance(recovery, dict) else None
    if isinstance(expected_source, dict):
        expected_source.pop("c1_sources", None)
        expected_source["flattened_source_lineage"] = expected_lineage
    if (
        not isinstance(recovery, dict)
        or not isinstance(upstream, list)
        or len(upstream) != 1
        or migration.get("recovery_index_sha256")
        != hashlib.sha256(recovery_raw).hexdigest()
        or migration.get("recovery_run_id") != recovery.get("run_id")
        or migration.get("recovery_git_commit") != recovery.get("git_commit")
        or migration.get("recovery_harness_hash") != recovery.get("harness_hash")
        or source != expected_source
    ):
        issues.append("C1 settled-failure flattened recovery binding is invalid")
    upstream_record = upstream[0] if isinstance(upstream, list) and len(upstream) == 1 else None
    if not isinstance(upstream_record, dict) or any(
        upstream_record.get(field) != migration.get(f"upstream_{field}")
        for field in ("source_index", "source_index_sha256", "source_run_id")
    ):
        issues.append("C1 settled-failure upstream binding is invalid")
    rows = [
        (arm, row)
        for arm in (run_index.get("arms_run") or [])
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    errors = {
        _trial_key(str(arm), row)
        for arm, row in rows
        if row.get("status") == "error"
    }
    if (
        len(rows) != 732
        or sum(row.get("status") == "ok" for _arm, row in rows) != 512
        or sum(row.get("status") == "pending" for _arm, row in rows) != 219
        or errors != terminal_keys
        or any(
            row.get("status") == "quarantined_unknown_usage"
            for _arm, row in rows
        )
    ):
        issues.append("C1 settled-failure migration coverage is invalid")
    request = run_index.get("c1_source_contract_migration_request")
    request_path = Path(request).expanduser() if isinstance(request, str) and request else None
    if request_path is None or not request_path.is_file():
        issues.append("C1 settled-failure migration artifact is missing")
    else:
        raw = request_path.read_bytes()
        try:
            artifact_payload = json.loads(raw)
        except json.JSONDecodeError:
            artifact_payload = None
        metadata_payload = {
            key: value
            for key, value in migration.items()
            if key not in {"artifact", "artifact_sha256"}
        }
        if (
            migration.get("artifact") != str(request_path.resolve())
            or migration.get("artifact_sha256") != hashlib.sha256(raw).hexdigest()
            or artifact_payload != metadata_payload
            or source_meta.get("contract_migration") != migration
        ):
            issues.append("C1 settled-failure migration artifact binding is invalid")
    return terminal_keys, issues


def _registered_state_recovery_settled_migration(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
) -> tuple[set[TrialKey], list[str]]:
    """Re-audit nine SHA-bound terminal State reports with older seed fields."""

    migration = run_index.get("c1_source_contract_migration")
    if not isinstance(migration, dict) or migration.get("migration_id") != (
        "c1_state_recovery_settled_v1"
    ):
        return set(), []
    affected_task_ids = [
        "luanti-mine-bridge-outpost-build",
        "jynew-party-training-roundtrip",
        "pal3-story-party-scene-roundtrip",
        "luanti-self-sufficient-homestead-roundtrip",
        "jynew-sidequest-trade-submap",
        "pal3-world-region-travel-roundtrip",
        "luanti-workshop-order-staging",
        "jynew-battle-ready-loadout",
        "pal3-companion-ending-preparation",
    ]
    affected = [
        {
            "arm": "c1_state",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": task_id,
            "source_status": "ok",
        }
        for task_id in affected_task_ids
    ]
    settled_keys = {
        TrialKey.from_row(str(row["arm"]), row)
        for row in affected
    }
    changed_paths = [
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
    ]
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "migration_id": "c1_state_recovery_settled_v1",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "source_git_commit": source.get("git_commit"),
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "current_git_commit": run_index.get("git_commit"),
        "allowed_migrations": [
            "calibration_rebind",
            "git_commit_rebind",
            "harness_rebind",
            "preflight_rebind",
        ],
        "replaceable_source_statuses": [
            "pending",
            "quarantined_unknown_usage",
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
            "registered_exact_usage_harness_failure",
        ],
        "settled_report_policy": "sha_verified_terminal_state_schema_rebind_v1",
        "affected_trials": affected,
        "preserved_model_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": False,
        "treatment_semantics_unchanged": True,
        "changed_paths": changed_paths,
        "changed_paths_sha256": hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    issues: list[str] = []
    if any(migration.get(field) != value for field, value in expected.items()):
        issues.append("C1 state-recovery migration metadata is invalid")
    if (
        source.get("run_id") != "p5-e630e64eb662"
        or source.get("git_commit")
        != "42257450e2ed3cab4d33f7b8a682964e184aed91"
        or source.get("harness_hash")
        != "7ffe4a553cac4dd278c473f041a6231b9bde9b7eac244ae16e62fb8567a62393"
    ):
        issues.append("C1 state-recovery migration source mismatch")
    rows = [
        (arm, row)
        for arm in ("c1_agent_s", "c1_agent_s_synth", "c1_state")
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    actual_affected = {
        _trial_key(arm, row)
        for arm, row in rows
        if row.get("replacement_for") and row.get("source_provenance") is None
    }
    if (
        len(rows) != 549
        or sum(row.get("status") == "ok" for _arm, row in rows) != 549
        or actual_affected != settled_keys
        or any(
            row.get("status") == "quarantined_unknown_usage"
            for _arm, row in rows
        )
    ):
        issues.append("C1 state-recovery migration coverage is invalid")
    for arm, row in rows:
        key = _trial_key(arm, row)
        if key not in settled_keys:
            continue
        report_path = Path(str(row.get("report") or "")).expanduser()
        try:
            report_raw = report_path.read_bytes()
            report = json.loads(report_raw)
        except (OSError, json.JSONDecodeError):
            report = None
            report_raw = b""
        results = report.get("results") if isinstance(report, dict) else None
        result = (
            results[0]
            if isinstance(results, list)
            and len(results) == 1
            and isinstance(results[0], dict)
            else None
        )
        if (
            row.get("status") != "ok"
            or row.get("report_sha256") != hashlib.sha256(report_raw).hexdigest()
            or not isinstance(report, dict)
            or report.get("complete") is not True
            or cost.contains_unknown_usage(report)
            or not isinstance(result, dict)
            or result.get("task_id") != row.get("task_id")
            or result.get("status") not in {"failed", "failure"}
            or result.get("result") is not False
        ):
            issues.append(
                "C1 state-recovery migration evidence is not an exact "
                f"terminal failure: {key}"
            )
    request = run_index.get("c1_source_contract_migration_request")
    request_path = (
        Path(request).expanduser()
        if isinstance(request, str) and request
        else None
    )
    if request_path is None or not request_path.is_file():
        issues.append("C1 state-recovery migration artifact is missing")
    else:
        raw = request_path.read_bytes()
        try:
            artifact_payload = json.loads(raw)
        except json.JSONDecodeError:
            artifact_payload = None
        metadata_payload = {
            key: value
            for key, value in migration.items()
            if key not in {"artifact", "artifact_sha256"}
        }
        if (
            migration.get("artifact") != str(request_path.resolve())
            or migration.get("artifact_sha256")
            != hashlib.sha256(raw).hexdigest()
            or artifact_payload != metadata_payload
            or source_meta.get("contract_migration") != migration
        ):
            issues.append("C1 state-recovery migration binding is invalid")
    return settled_keys, issues


def _registered_source_action_sidecar_migration(
    *, run_index: dict[str, Any], source: dict[str, Any], source_path: Path,
    source_sha: str, source_meta: dict[str, Any], repository_root: Path | None,
) -> tuple[set[TrialKey], list[str]]:
    """Re-read the precise sidecar-reader migration and its preserved failures."""
    migration = run_index.get("c1_source_contract_migration")
    if not isinstance(migration, dict) or migration.get("migration_id") != "c1_source_action_sidecar_validation_v1":
        return set(), []
    issues: list[str] = []
    expected = {
        "schema_version": 2,
        "kind": "c1_source_contract_migration",
        "source_index": str(source_path.resolve()),
        "source_index_sha256": source_sha,
        "source_run_id": source.get("run_id"),
        "source_manifest_hash": source.get("manifest_hash"),
        "source_harness_hash": source.get("harness_hash"),
        "source_git_commit": source.get("git_commit"),
        "current_manifest_hash": run_index.get("manifest_hash"),
        "current_harness_hash": run_index.get("harness_hash"),
        "current_git_commit": run_index.get("git_commit"),
        "allowed_migrations": ["calibration_rebind", "git_commit_rebind", "harness_rebind", "preflight_rebind"],
        "replaceable_source_statuses": ["pending", "quarantined_unknown_usage", "exact_usage_task_timeout", "exact_usage_retryable_infrastructure", "registered_exact_usage_harness_failure"],
    }
    if any(migration.get(key) != value for key, value in expected.items()):
        issues.append("C1 source-action sidecar migration binding mismatch")
    request = run_index.get("c1_source_contract_migration_request")
    try:
        request_path = Path(str(request)).resolve()
        raw = request_path.read_bytes()
        artifact = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        issues.append("C1 source-action sidecar migration artifact is unreadable")
    else:
        if (
            migration.get("artifact") != str(request_path)
            or migration.get("artifact_sha256") != hashlib.sha256(raw).hexdigest()
            or artifact != {key: value for key, value in migration.items() if key not in {"artifact", "artifact_sha256"}}
            or source_meta.get("contract_migration") != migration
        ):
            issues.append("C1 source-action sidecar migration artifact mismatch")
    if repository_root is None:
        issues.append("C1 source-action sidecar migration requires repository audit")
    else:
        from .p5_scale import _validate_source_action_sidecar_migration
        try:
            _validate_source_action_sidecar_migration(
                base=repository_root, source=source, migration=migration
            )
        except (OSError, ValueError, RuntimeError) as error:
            issues.append(f"C1 source-action sidecar migration audit failed: {error}")
    preserved = migration.get("preserved_operational_failure_trials")
    keys = {
        TrialKey.from_row(str(row.get("arm")), row) for row in preserved
        if isinstance(row, dict)
    } if isinstance(preserved, list) else set()
    if source_meta.get("imported_terminal_operational_failure_trials") != len(keys):
        issues.append("C1 source-action operational failure summary mismatch")
    return keys, issues


def _c1_flattened_trial_recovery_issues(
    *,
    run_index: dict[str, Any],
    source: dict[str, Any],
    source_path: Path,
    source_sha: str,
    source_meta: dict[str, Any],
    current_rows: dict[TrialKey, dict[str, Any]],
    arms: list[str],
    repository_root: Path | None = None,
) -> list[str]:
    """Audit a SHA-bound flattened recovery source and its recursive lineage.

    Flattened sources deliberately retain immutable attempt IDs and reports from
    the run that produced each tuple.  Treating every row as if the immediate
    flattened file were an original run rejects valid multi-generation
    recoveries.  This mirrors the importer's recursive evidence resolution while
    independently re-reading every lineage hop and migration artifact.
    """

    issues: list[str] = []
    lineage = source.get("flattened_source_lineage")
    if not isinstance(lineage, dict):
        return ["C1 flattened recovery lineage is malformed"]
    derived_path_raw = lineage.get("derived_from_index")
    derived_sha = lineage.get("derived_from_index_sha256")
    records = lineage.get("upstream_sources")
    if (
        lineage.get("schema_version") != 1
        or lineage.get("kind") != "c1_flattened_recovery_source"
        or not isinstance(derived_path_raw, str)
        or not derived_path_raw
        or not _is_sha256(derived_sha)
        or not isinstance(records, list)
        or len(records) != 1
        or not isinstance(records[0], dict)
    ):
        return ["C1 flattened recovery lineage is malformed"]

    try:
        derived_path = Path(derived_path_raw).expanduser()
        derived_raw = derived_path.read_bytes()
        derived = json.loads(derived_raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return ["C1 flattened recovery derivation is unreadable"]
    if (
        not isinstance(derived, dict)
        or hashlib.sha256(derived_raw).hexdigest() != derived_sha
    ):
        return ["C1 flattened recovery derivation SHA mismatch"]
    expected_source = copy.deepcopy(derived)
    upstream = expected_source.pop("c1_sources", None)
    expected_lineage = {
        "schema_version": 1,
        "kind": "c1_flattened_recovery_source",
        "derived_from_index": str(derived_path),
        "derived_from_index_sha256": derived_sha,
        "upstream_sources": copy.deepcopy(upstream),
    }
    expected_source["flattened_source_lineage"] = expected_lineage
    if (
        not isinstance(upstream, list)
        or len(upstream) != 1
        or source != expected_source
    ):
        return ["C1 flattened recovery source was not derived exactly"]

    upstream_cache: dict[str, tuple[Path, dict[str, Any], str]] = {}

    def normalize_persisted_source(
        path: Path, payload: dict[str, Any], sha: str
    ) -> dict[str, Any]:
        upstream_sources = payload.get("c1_sources")
        if not upstream_sources:
            return payload
        if (
            not isinstance(upstream_sources, list)
            or len(upstream_sources) != 1
            or not isinstance(upstream_sources[0], dict)
        ):
            raise ValueError("lineage")
        flattened = copy.deepcopy(payload)
        flattened.pop("c1_sources", None)
        flattened["flattened_source_lineage"] = {
            "schema_version": 1,
            "kind": "c1_flattened_recovery_source",
            "derived_from_index": str(path),
            "derived_from_index_sha256": sha,
            "upstream_sources": copy.deepcopy(upstream_sources),
        }
        return flattened

    def load_upstream(record: dict[str, Any]) -> tuple[Path, dict[str, Any], str]:
        raw_path = record.get("source_index")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("path")
        path = Path(raw_path).expanduser()
        key = str(path)
        cached = upstream_cache.get(key)
        if cached is None:
            raw = path.read_bytes()
            payload = json.loads(raw)
            sha = hashlib.sha256(raw).hexdigest()
            if not isinstance(payload, dict):
                raise ValueError("payload")
            payload = normalize_persisted_source(path, payload, sha)
            cached = (path, payload, sha)
            upstream_cache[key] = cached
        if (
            cached[2] != record.get("source_index_sha256")
            or cached[1].get("run_id") != record.get("source_run_id")
        ):
            raise ValueError("binding")
        return cached

    def row_for_key(
        payload: dict[str, Any],
        key: TrialKey,
    ) -> dict[str, Any]:
        rows = payload.get("arms", {}).get(key[0], []) or []
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and (
                key[0],
                row.get("model"),
                row.get("seed"),
                str(row.get("task_id") or ""),
            )
            == key
        ]
        if len(matches) != 1:
            raise ValueError("tuple")
        return matches[0]

    def resolve_evidence(
        *,
        payload_path: Path,
        payload: dict[str, Any],
        payload_sha: str,
        row: dict[str, Any],
        key: TrialKey,
        visited: set[tuple[str, str]],
    ) -> tuple[Path, dict[str, Any], str, dict[str, Any], str]:
        visit = (str(payload_path), payload_sha)
        if visit in visited:
            raise ValueError("cycle")
        visited = {*visited, visit}
        if row.get("imported") is not True:
            if row.get("source_provenance") is not None:
                raise ValueError("fabricated")
            return payload_path, payload, payload_sha, row, str(row.get("status"))
        payload_lineage = payload.get("flattened_source_lineage")
        payload_records = (
            payload_lineage.get("upstream_sources")
            if isinstance(payload_lineage, dict)
            else None
        )
        provenance = row.get("source_provenance")
        if (
            not isinstance(payload_records, list)
            or len(payload_records) != 1
            or not isinstance(payload_records[0], dict)
            or not isinstance(provenance, dict)
        ):
            raise ValueError("provenance")
        upstream_path, upstream_payload, upstream_sha = load_upstream(
            payload_records[0]
        )
        if (
            str(Path(str(provenance.get("source_index") or "")).expanduser())
            != str(upstream_path)
            or provenance.get("source_index_sha256") != upstream_sha
            or provenance.get("source_run_id") != upstream_payload.get("run_id")
        ):
            raise ValueError("origin")
        upstream_row = row_for_key(upstream_payload, key)
        immutable = (
            "model",
            "seed",
            "task_id",
            "trial_attempt",
            "attempt_id",
            "report",
            "report_sha256",
            "replacement_for",
        )
        if any(row.get(field) != upstream_row.get(field) for field in immutable):
            raise ValueError("immutable")
        evidence = resolve_evidence(
            payload_path=upstream_path,
            payload=upstream_payload,
            payload_sha=upstream_sha,
            row=upstream_row,
            key=key,
            visited=visited,
        )
        flattened = provenance.get("flattened_source_lineage")
        expected_flattened = {
            "evidence_source_index": str(evidence[0]),
            "evidence_source_index_sha256": evidence[2],
            "evidence_source_run_id": evidence[1].get("run_id"),
            "prior_source_provenance": copy.deepcopy(
                upstream_row.get("source_provenance")
            ),
        }
        direct_original = (
            flattened is None
            and upstream_row.get("imported") is not True
            and evidence[0] == upstream_path
            and evidence[2] == upstream_sha
        )
        if not direct_original and flattened != expected_flattened:
            raise ValueError("recursive")
        status = str(provenance.get("source_status") or evidence[4])
        return evidence[0], evidence[1], evidence[2], evidence[3], status

    try:
        intermediate_source_path, intermediate_source, intermediate_source_sha = (
            load_upstream(records[0])
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return ["C1 flattened upstream source provenance is invalid"]

    intermediate_migration = derived.get("c1_source_contract_migration")
    if intermediate_migration is not None:
        if (
            isinstance(intermediate_migration, dict)
            and intermediate_migration.get("migration_id")
            == "c1_recovery_parallel_scheduler_v1"
        ):
            _, intermediate_migration_issues = (
                _registered_recovery_parallel_migration(
                    run_index=derived,
                    source=intermediate_source,
                    source_path=intermediate_source_path,
                    source_sha=intermediate_source_sha,
                    source_meta=records[0],
                    repository_root=repository_root,
                )
            )
            issues.extend(intermediate_migration_issues)
        elif (
            isinstance(intermediate_migration, dict)
            and intermediate_migration.get("migration_id")
            == "c1_retry_task_plan_resume_v1"
        ):
            _, intermediate_migration_issues = (
                _registered_retry_task_plan_migration(
                    run_index=derived,
                    source=intermediate_source,
                    source_path=intermediate_source_path,
                    source_sha=intermediate_source_sha,
                    source_meta=records[0],
                )
            )
            issues.extend(intermediate_migration_issues)
        elif (
            isinstance(intermediate_migration, dict)
            and intermediate_migration.get("migration_id")
            == "c1_resource_ready_scheduler_v2"
        ):
            issues.extend(
                _registered_scheduler_lineage_migration_issues(
                    run_index=derived,
                    source=intermediate_source,
                    source_path=intermediate_source_path,
                    source_sha=intermediate_source_sha,
                    source_meta=records[0],
                )
            )
        elif (
            isinstance(intermediate_migration, dict)
            and intermediate_migration.get("migration_id")
            == "c1_retryable_recovery_ledger_v1"
        ):
            _, intermediate_migration_issues = (
                _registered_retryable_ledger_migration(
                    run_index=derived,
                    source=intermediate_source,
                    source_path=intermediate_source_path,
                    source_sha=intermediate_source_sha,
                    source_meta=records[0],
                )
            )
            issues.extend(intermediate_migration_issues)
        elif (
            isinstance(intermediate_migration, dict)
            and intermediate_migration.get("migration_id")
            == "c1_retryable_runtime_marker_v1"
        ):
            _, intermediate_migration_issues = (
                _registered_retryable_runtime_marker_migration(
                    run_index=derived,
                    source=intermediate_source,
                    source_path=intermediate_source_path,
                    source_sha=intermediate_source_sha,
                    source_meta=records[0],
                )
            )
            issues.extend(intermediate_migration_issues)
        elif (
            isinstance(intermediate_migration, dict)
            and intermediate_migration.get("migration_id")
            == "c1_exhausted_recovery_terminal_v1"
        ):
            _, intermediate_migration_issues = (
                _registered_exhausted_recovery_terminal_migration(
                    run_index=derived,
                    source=intermediate_source,
                    source_path=intermediate_source_path,
                    source_sha=intermediate_source_sha,
                    source_meta=records[0],
                )
            )
            issues.extend(intermediate_migration_issues)
        elif (
            isinstance(intermediate_migration, dict)
            and intermediate_migration.get("migration_id")
            == "c1_resume_settled_failure_v1"
        ):
            _, intermediate_migration_issues = (
                _registered_resume_settled_failure_migration(
                    run_index=derived,
                    source=intermediate_source,
                    source_path=intermediate_source_path,
                    source_sha=intermediate_source_sha,
                    source_meta=records[0],
                )
            )
            issues.extend(intermediate_migration_issues)
        elif (
            isinstance(intermediate_migration, dict)
            and intermediate_migration.get("migration_id")
            == "c1_state_recovery_settled_v1"
        ):
            _, intermediate_migration_issues = (
                _registered_state_recovery_settled_migration(
                    run_index=derived,
                    source=intermediate_source,
                    source_path=intermediate_source_path,
                    source_sha=intermediate_source_sha,
                    source_meta=records[0],
                )
            )
            issues.extend(intermediate_migration_issues)
        elif (
            isinstance(intermediate_migration, dict)
            and intermediate_migration.get("migration_id")
            == BACKGROUND_RESOURCE_MIGRATION_ID
        ):
            _, intermediate_migration_issues = (
                _registered_background_resource_partition_migration(
                    run_index=derived,
                    source=intermediate_source,
                    source_path=intermediate_source_path,
                    source_sha=intermediate_source_sha,
                    source_meta=records[0],
                    repository_root=repository_root,
                )
            )
            issues.extend(intermediate_migration_issues)
        else:
            issues.append(
                "C1 flattened intermediate contract migration is not registered"
            )

    audited_nested_sources: set[tuple[str, str]] = set()

    def audit_nested_flattened_migrations(
        payload_path: Path,
        payload: dict[str, Any],
        payload_sha: str,
    ) -> None:
        lineage_payload = payload.get("flattened_source_lineage")
        if not isinstance(lineage_payload, dict):
            return
        audit_key = (str(payload_path), payload_sha)
        if audit_key in audited_nested_sources:
            issues.append("C1 flattened migration lineage contains a cycle")
            return
        audited_nested_sources.add(audit_key)
        nested_derived_raw = lineage_payload.get("derived_from_index")
        nested_derived_sha = lineage_payload.get("derived_from_index_sha256")
        nested_records = lineage_payload.get("upstream_sources")
        if (
            lineage_payload.get("schema_version") != 1
            or lineage_payload.get("kind") != "c1_flattened_recovery_source"
            or not isinstance(nested_derived_raw, str)
            or not nested_derived_raw
            or not _is_sha256(nested_derived_sha)
            or not isinstance(nested_records, list)
            or len(nested_records) != 1
            or not isinstance(nested_records[0], dict)
        ):
            issues.append("C1 nested flattened migration lineage is malformed")
            return
        try:
            nested_derived_path = Path(nested_derived_raw).expanduser()
            nested_derived_bytes = nested_derived_path.read_bytes()
            nested_derived = json.loads(nested_derived_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            issues.append("C1 nested flattened migration derivation is unreadable")
            return
        if not isinstance(nested_derived, dict):
            issues.append("C1 nested flattened migration derivation is invalid")
            return
        expected_nested = copy.deepcopy(nested_derived)
        nested_upstream = expected_nested.pop("c1_sources", None)
        expected_nested["flattened_source_lineage"] = {
            "schema_version": 1,
            "kind": "c1_flattened_recovery_source",
            "derived_from_index": str(nested_derived_path),
            "derived_from_index_sha256": nested_derived_sha,
            "upstream_sources": copy.deepcopy(nested_upstream),
        }
        if (
            hashlib.sha256(nested_derived_bytes).hexdigest() != nested_derived_sha
            or nested_upstream != nested_records
            or payload != expected_nested
        ):
            issues.append("C1 nested flattened migration derivation is invalid")
            return
        try:
            nested_source_path, nested_source, nested_source_sha = load_upstream(
                nested_records[0]
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            issues.append("C1 nested flattened migration source is invalid")
            return
        nested_migration = nested_derived.get("c1_source_contract_migration")
        if nested_migration is not None:
            if (
                isinstance(nested_migration, dict)
                and nested_migration.get("migration_id")
                == "c1_recovery_parallel_scheduler_v1"
            ):
                _, nested_issues = _registered_recovery_parallel_migration(
                    run_index=nested_derived,
                    source=nested_source,
                    source_path=nested_source_path,
                    source_sha=nested_source_sha,
                    source_meta=nested_records[0],
                    repository_root=repository_root,
                )
                issues.extend(nested_issues)
            elif (
                isinstance(nested_migration, dict)
                and nested_migration.get("migration_id")
                == "c1_retry_task_plan_resume_v1"
            ):
                _, nested_issues = _registered_retry_task_plan_migration(
                    run_index=nested_derived,
                    source=nested_source,
                    source_path=nested_source_path,
                    source_sha=nested_source_sha,
                    source_meta=nested_records[0],
                )
                issues.extend(nested_issues)
            elif (
                isinstance(nested_migration, dict)
                and nested_migration.get("migration_id")
                == "c1_resource_ready_scheduler_v2"
            ):
                issues.extend(
                    _registered_scheduler_lineage_migration_issues(
                        run_index=nested_derived,
                        source=nested_source,
                        source_path=nested_source_path,
                        source_sha=nested_source_sha,
                        source_meta=nested_records[0],
                    )
                )
            elif (
                isinstance(nested_migration, dict)
                and nested_migration.get("migration_id")
                == "c1_retryable_recovery_ledger_v1"
            ):
                _, nested_issues = _registered_retryable_ledger_migration(
                    run_index=nested_derived,
                    source=nested_source,
                    source_path=nested_source_path,
                    source_sha=nested_source_sha,
                    source_meta=nested_records[0],
                )
                issues.extend(nested_issues)
            elif (
                isinstance(nested_migration, dict)
                and nested_migration.get("migration_id")
                == "c1_retryable_runtime_marker_v1"
            ):
                _, nested_issues = _registered_retryable_runtime_marker_migration(
                    run_index=nested_derived,
                    source=nested_source,
                    source_path=nested_source_path,
                    source_sha=nested_source_sha,
                    source_meta=nested_records[0],
                )
                issues.extend(nested_issues)
            elif (
                isinstance(nested_migration, dict)
                and nested_migration.get("migration_id")
                == "c1_exhausted_recovery_terminal_v1"
            ):
                _, nested_issues = _registered_exhausted_recovery_terminal_migration(
                    run_index=nested_derived,
                    source=nested_source,
                    source_path=nested_source_path,
                    source_sha=nested_source_sha,
                    source_meta=nested_records[0],
                )
                issues.extend(nested_issues)
            elif (
                isinstance(nested_migration, dict)
                and nested_migration.get("migration_id")
                == "c1_resume_settled_failure_v1"
            ):
                _, nested_issues = _registered_resume_settled_failure_migration(
                    run_index=nested_derived,
                    source=nested_source,
                    source_path=nested_source_path,
                    source_sha=nested_source_sha,
                    source_meta=nested_records[0],
                )
                issues.extend(nested_issues)
            elif (
                isinstance(nested_migration, dict)
                and nested_migration.get("migration_id")
                == "c1_state_recovery_settled_v1"
            ):
                _, nested_issues = _registered_state_recovery_settled_migration(
                    run_index=nested_derived,
                    source=nested_source,
                    source_path=nested_source_path,
                    source_sha=nested_source_sha,
                    source_meta=nested_records[0],
                )
                issues.extend(nested_issues)
            elif (
                isinstance(nested_migration, dict)
                and nested_migration.get("migration_id")
                == BACKGROUND_RESOURCE_MIGRATION_ID
            ):
                _, nested_issues = (
                    _registered_background_resource_partition_migration(
                        run_index=nested_derived,
                        source=nested_source,
                        source_path=nested_source_path,
                        source_sha=nested_source_sha,
                        source_meta=nested_records[0],
                        repository_root=repository_root,
                    )
                )
                issues.extend(nested_issues)
            else:
                issues.append(
                    "C1 nested flattened contract migration is not registered"
                )
        audit_nested_flattened_migrations(
            nested_source_path,
            nested_source,
            nested_source_sha,
        )

    audit_nested_flattened_migrations(
        intermediate_source_path,
        intermediate_source,
        intermediate_source_sha,
    )

    if source.get("c1_trial_persistence") != run_index.get("c1_trial_persistence"):
        issues.append("C1 recovery source trial policy mismatch")

    migration = run_index.get("c1_source_contract_migration")
    migration_request = run_index.get("c1_source_contract_migration_request")
    affected_migration_keys, migration_issues = (
        _registered_background_resource_partition_migration(
            run_index=run_index,
            source=source,
            source_path=source_path,
            source_sha=source_sha,
            source_meta=source_meta,
            repository_root=repository_root,
        )
    )
    if not affected_migration_keys and not migration_issues:
        affected_migration_keys, migration_issues = (
            _registered_recovery_parallel_migration(
                run_index=run_index,
                source=source,
                source_path=source_path,
                source_sha=source_sha,
                source_meta=source_meta,
                repository_root=repository_root,
            )
        )
    preserved_terminal_keys: set[TrialKey] = set()
    if not affected_migration_keys and not migration_issues:
        preserved_terminal_keys, migration_issues = _registered_source_action_sidecar_migration(
            run_index=run_index, source=source, source_path=source_path,
            source_sha=source_sha, source_meta=source_meta,
            repository_root=repository_root,
        )
    if not affected_migration_keys and not preserved_terminal_keys and not migration_issues:
        preserved_terminal_keys, migration_issues = (
            _registered_exhausted_recovery_terminal_migration(
                run_index=run_index,
                source=source,
                source_path=source_path,
                source_sha=source_sha,
                source_meta=source_meta,
            )
        )
    if (
        not affected_migration_keys
        and not preserved_terminal_keys
        and not migration_issues
    ):
        preserved_terminal_keys, migration_issues = (
            _registered_resume_settled_failure_migration(
                run_index=run_index,
                source=source,
                source_path=source_path,
                source_sha=source_sha,
                source_meta=source_meta,
            )
        )
    if (
        not affected_migration_keys
        and not preserved_terminal_keys
        and not migration_issues
    ):
        preserved_terminal_keys, migration_issues = (
            _registered_state_recovery_settled_migration(
                run_index=run_index,
                source=source,
                source_path=source_path,
                source_sha=source_sha,
                source_meta=source_meta,
            )
        )
    if (
        not affected_migration_keys
        and not preserved_terminal_keys
        and not migration_issues
    ):
        affected_migration_keys, migration_issues = (
            _registered_retryable_runtime_marker_migration(
                run_index=run_index,
                source=source,
                source_path=source_path,
                source_sha=source_sha,
                source_meta=source_meta,
            )
        )
    if (
        not preserved_terminal_keys
        and not affected_migration_keys
        and not migration_issues
    ):
        affected_migration_keys, migration_issues = (
            _registered_retryable_ledger_migration(
                run_index=run_index,
                source=source,
                source_path=source_path,
                source_sha=source_sha,
                source_meta=source_meta,
            )
        )
    if (
        not preserved_terminal_keys
        and not affected_migration_keys
        and not migration_issues
    ):
        affected_migration_keys, migration_issues = (
            _registered_openra_render_grade_migration(
                run_index=run_index,
                source=source,
                source_path=source_path,
                source_sha=source_sha,
                source_meta=source_meta,
            )
        )
    issues.extend(migration_issues)
    registered_migration = bool(
        (preserved_terminal_keys or affected_migration_keys)
        and not migration_issues
    )
    scientific_fields = (
        "manifest_hash",
        "task_manifest_hash",
        "harness_hash",
        "git_commit",
        "models",
        "seeds",
        "task_ids",
        "arms_run",
        "agent_kind",
        "headless",
        "model_request_profiles",
        "model_request_profiles_sha256",
        "runtime_lock",
        "provider_timeout_s",
        "c1_task_timeout_s",
        "c1_start_gate_protocol",
        "c1_background_pixel_protocol",
        "c1_background_pixel_protocol_sha256",
        "c1_application_revisions",
        "c1_environment",
        "c1_prompt_revisions",
        "c1_random_controls",
        "usage_accounting",
    )
    incompatible = [
        field for field in scientific_fields
        if source.get(field) != run_index.get(field)
    ]
    recovery_parallel_migration = bool(
        isinstance(migration, dict)
        and migration.get("migration_id")
        == "c1_recovery_parallel_scheduler_v1"
    )
    allowed_incompatible = (
        {"harness_hash", "git_commit"}
        if recovery_parallel_migration
        else {
            "task_manifest_hash",
            "harness_hash",
            "git_commit",
            "c1_application_revisions",
        }
    )
    unexpected_incompatible = (
        [field for field in incompatible if field not in allowed_incompatible]
        if registered_migration
        else incompatible
    )
    if unexpected_incompatible:
        issues.append(
            "C1 recovery source/current scientific contract differs: "
            + ", ".join(unexpected_incompatible)
        )
    source_preflight = _recovery_preflight_contract(source.get("preflight"))
    current_preflight = _recovery_preflight_contract(run_index.get("preflight"))
    source_calibration = _recovery_calibration_contract(source.get("calibration"))
    current_calibration = _recovery_calibration_contract(run_index.get("calibration"))

    def without_registered_rebinds(value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return {
            key: item
            for key, item in value.items()
            if key
            not in {
                "git_commit",
                "manifest_hash",
                "harness_hash",
                "c1_application_revisions",
            }
        }

    preflight_differs = source_preflight != current_preflight
    calibration_differs = source_calibration != current_calibration
    if preflight_differs and not (
        registered_migration
        and without_registered_rebinds(source_preflight)
        == without_registered_rebinds(current_preflight)
    ):
        issues.append("C1 recovery preflight scientific contract differs")
    if calibration_differs and not (
        registered_migration
        and without_registered_rebinds(source_calibration)
        == without_registered_rebinds(current_calibration)
    ):
        issues.append("C1 recovery calibration scientific contract differs")
    if not registered_migration and (migration is not None or migration_request is not None):
        issues.append("C1 recovery contract migration is present but unnecessary")

    source_rows: dict[TrialKey, dict[str, Any]] = {}
    for arm in arms:
        for row in source.get("arms", {}).get(arm, []) or []:
            if not isinstance(row, dict):
                issues.append("C1 recovery source contains a malformed row")
                continue
            key = _trial_key(arm, row)
            if not key[3] or key in source_rows:
                issues.append("C1 recovery source contains a missing/duplicate tuple")
                continue
            source_rows[key] = row
    if set(source_rows) != set(current_rows):
        issues.append("C1 recovery source/current tuple matrices differ")

    interrupted_by_key = {
        _trial_key(str(item.get("arm") or ""), item): item
        for item in (
            migration.get("interrupted_unknown_usage_attempts")
            if isinstance(migration, dict)
            and migration.get("migration_id")
            == BACKGROUND_RESOURCE_MIGRATION_ID
            and isinstance(
                migration.get("interrupted_unknown_usage_attempts"), list
            )
            else []
        )
        if isinstance(item, dict)
    }
    for key in set(source_rows) & set(current_rows):
        source_row = source_rows[key]
        current_row = current_rows[key]
        try:
            evidence = resolve_evidence(
                payload_path=source_path,
                payload=source,
                payload_sha=source_sha,
                row=source_row,
                key=key,
                visited=set(),
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            issues.append(f"C1 flattened source tuple lineage mismatch: {key}")
            continue
        evidence_status = evidence[4]
        source_status = str(source_row.get("status"))
        provenance = current_row.get("source_provenance")
        if source_status == "pending" or (
            registered_migration and key in affected_migration_keys
        ):
            interrupted_attempt = interrupted_by_key.get(key)
            expected_replacement = {
                "source_index": str(source_path),
                "source_index_sha256": source_sha,
                "source_run_id": source.get("run_id"),
                "source_status": (
                    "quarantined_unknown_usage"
                    if interrupted_attempt is not None
                    else source_status
                ),
                "source_report": (
                    migration.get("pause_audit")
                    if interrupted_attempt is not None
                    else source_row.get("report")
                ),
                "source_report_sha256": (
                    migration.get("pause_audit_sha256")
                    if interrupted_attempt is not None
                    else source_row.get("report_sha256")
                ),
            }
            if (
                current_row.get("status") != "ok"
                or current_row.get("imported") is True
                or current_row.get("replacement_for") != expected_replacement
                or current_row.get("source_provenance") is not None
            ):
                issues.append(f"replacement tuple provenance mismatch: {key}")
            continue
        expected_flattened = {
            "evidence_source_index": str(evidence[0]),
            "evidence_source_index_sha256": evidence[2],
            "evidence_source_run_id": evidence[1].get("run_id"),
            "prior_source_provenance": copy.deepcopy(
                source_row.get("source_provenance")
            ),
        }
        immutable = (
            "model",
            "seed",
            "task_id",
            "trial_attempt",
            "attempt_id",
            "report",
            "report_sha256",
            "replacement_for",
        )
        operational_terminal = bool(
            key in preserved_terminal_keys
            and migration.get("migration_id")
            == "c1_source_action_sidecar_validation_v1"
        )
        if (
            current_row.get("status") != ("error" if operational_terminal else "ok")
            or current_row.get("imported") is not True
            or any(current_row.get(field) != source_row.get(field) for field in immutable)
            or not isinstance(provenance, dict)
            or provenance.get("source_index") != str(source_path)
            or provenance.get("source_index_sha256") != source_sha
            or provenance.get("source_run_id") != source.get("run_id")
            or provenance.get("flattened_source_lineage") != expected_flattened
        ):
            issues.append(f"completed source tuple was not imported immutably: {key}")
        if evidence_status == "error" and (
            not isinstance(provenance, dict)
            or provenance.get("source_status") != "error"
            or not isinstance(provenance.get("completion_class"), str)
        ):
            issues.append(f"preserved source model failure provenance mismatch: {key}")
        elif evidence_status == "error" and isinstance(provenance, dict):
            try:
                failure_report = json.loads(
                    Path(str(current_row.get("report") or "")).read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                failure_report = None
            failure_results = (
                failure_report.get("results")
                if isinstance(failure_report, dict)
                else None
            )
            failure_result = (
                failure_results[0]
                if isinstance(failure_results, list)
                and len(failure_results) == 1
                and isinstance(failure_results[0], dict)
                else None
            )
            kind = (
                "exact_usage_exhausted_operational_failure"
                if operational_terminal
                and isinstance(failure_result, dict)
                and isinstance(failure_report, dict)
                and not cost.contains_unknown_usage(failure_report)
                and failure_report.get("complete") is False
                and failure_result.get("incomplete") is True
                else (
                    _preserved_model_failure_kind(
                        result=failure_result, report=failure_report
                    )
                    if isinstance(failure_result, dict)
                    and isinstance(failure_report, dict)
                    else None
                )
            )
            if kind != provenance.get("completion_class"):
                issues.append(
                    f"preserved source model failure classification mismatch: {key}"
                )

    return issues


def _c1_trial_recovery_issues(
    run_index: dict[str, Any], *, repository_root: Path | None = None
) -> list[str]:
    """Recompute trial-source composition instead of trusting import-time rows."""

    if not isinstance(run_index.get("c1_trial_persistence"), dict):
        return []
    issues: list[str] = []
    arms = [str(arm) for arm in (run_index.get("arms_run") or []) if arm in _C1_ARM_LABELS]
    current_rows: dict[TrialKey, dict[str, Any]] = {}
    for arm in arms:
        for row in run_index.get("arms", {}).get(arm, []) or []:
            if not isinstance(row, dict):
                issues.append("trial-atomic run index contains a malformed row")
                continue
            key = _trial_key(arm, row)
            if not key[3] or key in current_rows:
                issues.append("trial-atomic run index contains a missing/duplicate tuple")
                continue
            current_rows[key] = row
    sources = run_index.get("c1_sources") or []
    if not sources:
        if run_index.get("c1_source_index_request") is not None:
            issues.append("original trial-atomic run claims a recovery source request")
        if run_index.get("c1_operational_overhead") not in (None, []):
            issues.append("completed original trial-atomic run has operational overhead")
        run_id = run_index.get("run_id")
        for key, row in current_rows.items():
            arm, model, seed, task_id = key
            expected_attempt_id = (
                f"{run_id}:{arm}:{model or 'na'}:"
                f"{seed if seed is not None else 'na'}:{task_id}:attempt0"
            )
            if row.get("status") != "ok":
                issues.append(f"original trial tuple is not complete: {key}")
            if row.get("trial_attempt") != 0 or row.get(
                "attempt_id"
            ) != expected_attempt_id:
                issues.append(f"original trial tuple attempt provenance mismatch: {key}")
            if (
                row.get("imported") not in (None, False)
                or row.get("replacement_for") is not None
                or row.get("source_provenance") is not None
            ):
                issues.append(f"original trial tuple has recovery provenance: {key}")
            report_raw = row.get("report")
            if not isinstance(report_raw, str) or not report_raw:
                issues.append(f"original trial tuple report is missing: {key}")
                continue
            report_path = Path(report_raw).expanduser()
            if not report_path.is_file():
                issues.append(f"original trial tuple report disappeared: {key}")
                continue
            raw = report_path.read_bytes()
            if not _is_sha256(row.get("report_sha256")) or hashlib.sha256(
                raw
            ).hexdigest() != row.get("report_sha256"):
                issues.append(f"original trial tuple report SHA mismatch: {key}")
                continue
            try:
                report = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                issues.append(f"original trial tuple report is unreadable: {key}")
                continue
            results = report.get("results") if isinstance(report, dict) else None
            if (
                not isinstance(report, dict)
                or report.get("complete") is not True
                or cost.contains_unknown_usage(report)
                or not isinstance(results, list)
                or len(results) != 1
                or not isinstance(results[0], dict)
                or str(results[0].get("task_id") or "") != task_id
            ):
                issues.append(f"original trial tuple report is not exact evidence: {key}")
        return issues
    if len(sources) != 1 or not isinstance(sources[0], dict):
        return ["formal C1 permits exactly one registered recovery source"]
    source_meta = sources[0]
    source_path_raw = source_meta.get("source_index")
    source_sha = source_meta.get("source_index_sha256")
    if not isinstance(source_path_raw, str) or not source_path_raw:
        return ["C1 recovery source path is missing"]
    try:
        source_path = Path(source_path_raw).expanduser()
        source_path_resolved = source_path.resolve()
        if not source_path.is_file():
            return ["C1 recovery source index is missing"]
        source_raw = source_path.read_bytes()
    except (OSError, RuntimeError, ValueError):
        return ["C1 recovery source path is invalid"]
    source_request = run_index.get("c1_source_index_request")
    try:
        request_matches = bool(
            isinstance(source_request, str)
            and source_request
            and Path(source_request).expanduser().resolve() == source_path_resolved
        )
    except (OSError, RuntimeError, ValueError):
        request_matches = False
    if not request_matches:
        issues.append("C1 recovery source request does not bind the registered source")
    if not _is_sha256(source_sha) or hashlib.sha256(source_raw).hexdigest() != source_sha:
        return ["C1 recovery source index SHA mismatch"]
    try:
        source = json.loads(source_raw)
    except json.JSONDecodeError:
        return ["C1 recovery source index is unreadable"]
    if not isinstance(source, dict):
        return ["C1 recovery source index is malformed"]
    migration = run_index.get("c1_source_contract_migration")
    if (
        source.get("c1_sources")
        and isinstance(migration, dict)
        and migration.get("migration_id")
        in {BACKGROUND_RESOURCE_MIGRATION_ID, "c1_source_action_sidecar_validation_v1"}
    ):
        upstream_sources = copy.deepcopy(source.get("c1_sources"))
        source = copy.deepcopy(source)
        source.pop("c1_sources", None)
        source["flattened_source_lineage"] = {
            "schema_version": 1,
            "kind": "c1_flattened_recovery_source",
            "derived_from_index": str(source_path),
            "derived_from_index_sha256": str(source_sha),
            "upstream_sources": upstream_sources,
        }
    if isinstance(source.get("flattened_source_lineage"), dict):
        return issues + _c1_flattened_trial_recovery_issues(
            run_index=run_index,
            source=source,
            source_path=source_path,
            source_sha=str(source_sha),
            source_meta=source_meta,
            current_rows=current_rows,
            arms=arms,
            repository_root=repository_root,
        )
    if source.get("c1_sources"):
        issues.append("C1 recovery source chaining is not registered")
    if source.get("c1_trial_persistence") != run_index.get("c1_trial_persistence"):
        issues.append("C1 recovery source trial policy mismatch")
    migration_request = run_index.get("c1_source_contract_migration_request")
    retry_migration_requested = bool(
        isinstance(migration, dict)
        and migration.get("migration_id") == "c1_retry_task_plan_resume_v1"
    )
    parallel_migration_requested = bool(
        isinstance(migration, dict)
        and migration.get("migration_id")
        == "c1_recovery_parallel_scheduler_v1"
    )
    registered_retry_keys, retry_migration_issues = (
        _registered_retry_task_plan_migration(
            run_index=run_index,
            source=source,
            source_path=source_path,
            source_sha=str(source_sha),
            source_meta=source_meta,
        )
    )
    issues.extend(retry_migration_issues)
    registered_retry_migration = bool(
        retry_migration_requested
        and registered_retry_keys
        and not retry_migration_issues
    )
    registered_parallel_keys, parallel_migration_issues = (
        _registered_recovery_parallel_migration(
            run_index=run_index,
            source=source,
            source_path=source_path,
            source_sha=str(source_sha),
            source_meta=source_meta,
            repository_root=repository_root,
        )
    )
    issues.extend(parallel_migration_issues)
    registered_parallel_migration = bool(
        parallel_migration_requested
        and registered_parallel_keys
        and not parallel_migration_issues
    )
    registered_harness_migration = bool(
        registered_retry_migration or registered_parallel_migration
    )
    compatibility_fields = (
        "manifest_hash",
        "task_manifest_hash",
        "harness_hash",
        "git_commit",
        "models",
        "seeds",
        "task_ids",
        "arms_run",
        "agent_kind",
        "headless",
        "model_request_profiles",
        "model_request_profiles_sha256",
        "runtime_lock",
        "provider_timeout_s",
        "c1_task_timeout_s",
        "c1_start_gate_protocol",
        "c1_background_pixel_protocol",
        "c1_background_pixel_protocol_sha256",
        "c1_application_revisions",
        "c1_environment",
        "c1_prompt_revisions",
        "c1_random_controls",
    )
    incompatible = [
        field for field in compatibility_fields
        if source.get(field) != run_index.get(field)
    ]
    unexpected_incompatible = (
        [field for field in incompatible if field not in {"harness_hash", "git_commit"}]
        if registered_harness_migration
        else incompatible
    )
    if unexpected_incompatible:
        issues.append(
            "C1 recovery source/current scientific contract differs: "
            + ", ".join(unexpected_incompatible)
        )
    expected_migrations: list[str] = []
    source_usage = copy.deepcopy(source.get("usage_accounting"))
    current_usage = run_index.get("usage_accounting")
    if source_usage != current_usage:
        removed = None
        if isinstance(source_usage, dict):
            detail = source_usage.get("reasoning_detail")
            if isinstance(detail, dict):
                removed = detail.pop("above_threshold", None)
        if removed == "paper_ready_false" and source_usage == current_usage:
            expected_migrations.append("usage_accounting_remove_paper_ready")
        else:
            issues.append("C1 recovery source usage-accounting contract differs")

    source_preflight = _recovery_preflight_contract(source.get("preflight"))
    current_preflight = _recovery_preflight_contract(run_index.get("preflight"))
    source_calibration = _recovery_calibration_contract(source.get("calibration"))
    current_calibration = _recovery_calibration_contract(run_index.get("calibration"))

    def without_registered_harness_rebinds(
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if value is None or not registered_harness_migration:
            return value
        return {
            key: item
            for key, item in value.items()
            if key not in {"git_commit", "manifest_hash", "harness_hash"}
        }

    if source_preflight != current_preflight and (
        without_registered_harness_rebinds(source_preflight)
        != without_registered_harness_rebinds(current_preflight)
    ):
        issues.append("C1 recovery preflight scientific contract differs")
    if source_calibration != current_calibration and (
        without_registered_harness_rebinds(source_calibration)
        != without_registered_harness_rebinds(current_calibration)
    ):
        issues.append("C1 recovery calibration scientific contract differs")
    expected_migrations = sorted(set(expected_migrations))
    if registered_harness_migration and expected_migrations:
        issues.append(
            "C1 registered recovery has an additional unregistered usage migration"
        )
    if expected_migrations and not registered_harness_migration:
        migration_path = (
            Path(migration_request).expanduser()
            if isinstance(migration_request, str) and migration_request
            else None
        )
        artifact_valid = False
        if migration_path is not None and migration_path.is_file():
            raw = migration_path.read_bytes()
            artifact_valid = bool(
                isinstance(migration, dict)
                and migration.get("artifact") == str(migration_path.resolve())
                and migration.get("artifact_sha256")
                == hashlib.sha256(raw).hexdigest()
            )
        expected_migration_fields = {
            "schema_version": 1,
            "kind": "c1_source_contract_migration",
            "source_index": str(source_path_resolved),
            "source_index_sha256": source_sha,
            "source_run_id": source.get("run_id"),
            "source_manifest_hash": source.get("manifest_hash"),
            "source_harness_hash": source.get("harness_hash"),
            "current_manifest_hash": run_index.get("manifest_hash"),
            "current_harness_hash": run_index.get("harness_hash"),
            "allowed_migrations": expected_migrations,
            "replaceable_source_statuses": [
                "pending",
                "quarantined_unknown_usage",
                "exact_usage_task_timeout",
                "exact_usage_retryable_infrastructure",
            ],
        }
        if (
            not artifact_valid
            or any(
                migration.get(field) != value
                for field, value in expected_migration_fields.items()
            )
            or source_meta.get("contract_migration") != migration
        ):
            issues.append("C1 recovery contract migration provenance is invalid")
    elif not registered_harness_migration and (
        migration is not None or migration_request is not None
    ):
        issues.append("C1 recovery contract migration is present but unnecessary")
    source_cache = source.get("c1_cache_policy")
    current_cache = run_index.get("c1_cache_policy")
    source_cache_root = source_cache.get("root") if isinstance(source_cache, dict) else None
    current_cache_root = current_cache.get("root") if isinstance(current_cache, dict) else None
    if not isinstance(source_cache_root, str) or not isinstance(current_cache_root, str):
        issues.append("C1 recovery cache provenance is missing")
    else:
        source_run_dir = Path(source_cache_root).expanduser().resolve().parent
        current_run_dir = Path(current_cache_root).expanduser().resolve().parent
        if (
            source.get("run_id") == run_index.get("run_id")
            or source_run_dir == current_run_dir
            or source_path_resolved.parent != source_run_dir
        ):
            issues.append("C1 recovery source is not a distinct original clean root")
    source_rows: dict[TrialKey, dict[str, Any]] = {}
    for arm in arms:
        for row in source.get("arms", {}).get(arm, []) or []:
            if not isinstance(row, dict):
                issues.append("C1 recovery source contains a malformed row")
                continue
            key = _trial_key(arm, row)
            if not key[3] or key in source_rows:
                issues.append("C1 recovery source contains a missing/duplicate tuple")
                continue
            source_rows[key] = row
    if set(source_rows) != set(current_rows):
        issues.append("C1 recovery source/current tuple matrices differ")
    quarantine_keys: set[TrialKey] = set()
    exact_timeout_keys: set[TrialKey] = set()
    retryable_infrastructure_keys: set[TrialKey] = set()
    registered_harness_failure_keys: set[TrialKey] = set()
    replacement_count = 0
    replacement_rows: dict[
        TrialKey, tuple[dict[str, Any], dict[str, Any]]
    ] = {}
    policy = run_index.get("c1_trial_persistence")
    maximum_attempts = (
        policy.get("max_recovery_attempts_per_tuple")
        if isinstance(policy, dict)
        else None
    )
    if (
        not isinstance(maximum_attempts, int)
        or isinstance(maximum_attempts, bool)
        or maximum_attempts <= 0
    ):
        issues.append("C1 recovery attempt bound is invalid")
        maximum_attempts = 0
    for key in set(source_rows) & set(current_rows):
        source_row = source_rows[key]
        current_row = current_rows[key]
        source_status = source_row.get("status")
        expected_source_attempt_id = (
            f"{source.get('run_id')}:{key[0]}:{key[1] or 'na'}:"
            f"{key[2] if key[2] is not None else 'na'}:{key[3]}:attempt0"
        )
        if (
            source_row.get("imported") not in (None, False)
            or source_row.get("replacement_for") is not None
            or source_row.get("source_provenance") is not None
        ):
            issues.append(f"C1 recovery source tuple has non-original provenance: {key}")
        if source_status == "pending":
            if source_row.get("trial_attempt") is not None or source_row.get(
                "attempt_id"
            ) is not None:
                issues.append(f"C1 recovery source pending tuple claims an attempt: {key}")
        elif (
            source_status in {"ok", "quarantined_unknown_usage", "error"}
            and (
                source_row.get("trial_attempt") != 0
                or source_row.get("attempt_id") != expected_source_attempt_id
            )
        ):
            issues.append(f"C1 recovery source tuple attempt provenance mismatch: {key}")
        if source_status == "ok":
            provenance = current_row.get("source_provenance")
            if (
                current_row.get("status") != "ok"
                or current_row.get("imported") is not True
                or current_row.get("report") != source_row.get("report")
                or current_row.get("report_sha256") != source_row.get("report_sha256")
                or not isinstance(provenance, dict)
                or provenance.get("source_index_sha256") != source_sha
                or provenance.get("source_run_id") != source.get("run_id")
                or current_row.get("replacement_for") is not None
                or current_row.get("trial_attempt") != source_row.get("trial_attempt")
                or current_row.get("attempt_id") != source_row.get("attempt_id")
            ):
                issues.append(
                    f"completed source tuple was not imported immutably: {key}"
                )
        elif source_status in {"pending", "quarantined_unknown_usage", "error"}:
            replacement_count += 1
            if source_status == "quarantined_unknown_usage":
                quarantine_keys.add(key)
            if source_status == "error":
                source_error_path = Path(
                    str(source_row.get("report") or "")
                ).expanduser()
                source_error_report = None
                if source_error_path.is_file():
                    try:
                        source_error_report = json.loads(
                            source_error_path.read_text(encoding="utf-8")
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        pass
                source_error_results = (
                    source_error_report.get("results")
                    if isinstance(source_error_report, dict)
                    else None
                )
                source_error_result = (
                    source_error_results[0]
                    if isinstance(source_error_results, list)
                    and len(source_error_results) == 1
                    and isinstance(source_error_results[0], dict)
                    else None
                )
                source_error_usage = (
                    source_error_result.get("actual_cost")
                    or source_error_result.get("cost")
                    if isinstance(source_error_result, dict)
                    else None
                )
                exact_usage = bool(
                    isinstance(source_error_usage, dict)
                    and source_error_usage.get("usage_complete") is True
                    and all(
                        source_error_usage.get(field) == 0
                        for field in (
                            "unknown_prompt_token_calls",
                            "unknown_completion_token_calls",
                            "unknown_total_token_calls",
                        )
                    )
                )
                attempts = (
                    source_error_result.get("attempt_costs")
                    if isinstance(source_error_result, dict)
                    else None
                )
                latest_attempt = (
                    attempts[-1]
                    if isinstance(attempts, list) and attempts
                    else None
                )
                timeout_s = source.get("c1_task_timeout_s")
                expected_reason = (
                    f"C1 task {key[3]} exceeded its {timeout_s:g}s wall-clock limit"
                    if isinstance(timeout_s, (int, float))
                    else ""
                )
                if (
                    exact_usage
                    and isinstance(source_error_report, dict)
                    and source_error_report.get("complete") is False
                    and source_error_report.get("retryable_incomplete") is False
                    and isinstance(source_error_result, dict)
                    and source_error_result.get("incomplete") is True
                    and source_error_result.get("timed_out") is True
                    and source_error_result.get("infrastructure_error") is False
                    and source_error_result.get("retryable_incomplete") is False
                    and str(source_error_result.get("reason") or "")
                    == expected_reason
                ):
                    exact_timeout_keys.add(key)
                elif (
                    exact_usage
                    and isinstance(source_error_report, dict)
                    and source_error_report.get("complete") is False
                    and isinstance(
                        source_error_report.get("retryable_incomplete"), bool
                    )
                    and isinstance(source_error_result, dict)
                    and source_error_result.get("incomplete") is True
                    and source_error_result.get("timed_out") is not True
                    and (
                        source_error_result.get("infrastructure_error") is True
                        or source_error_result.get("infrastructure_error") is None
                    )
                    and source_error_result.get("retryable_incomplete") is True
                    and isinstance(latest_attempt, dict)
                    and latest_attempt.get("attempt_status")
                    == "retryable_infrastructure"
                    and latest_attempt.get("retry_category") == "infra"
                    and isinstance(latest_attempt.get("retry_reason"), str)
                    and bool(latest_attempt["retry_reason"].strip())
                    and isinstance(source_error_result.get("reason"), str)
                    and bool(source_error_result["reason"].strip())
                ):
                    retryable_infrastructure_keys.add(key)
                elif (
                    key in registered_retry_keys
                    and exact_usage
                    and isinstance(source_error_report, dict)
                    and source_error_report.get("complete") is False
                    and isinstance(source_error_result, dict)
                    and source_error_result.get("incomplete") is True
                ):
                    registered_harness_failure_keys.add(key)
                else:
                    issues.append(
                        f"source error is not a registered exact-usage replacement: {key}"
                    )
            source_report_sha = source_row.get("report_sha256")
            if source_status == "error":
                source_report_path = Path(
                    str(source_row.get("report") or "")
                ).expanduser()
                if source_report_path.is_file():
                    source_report_sha = hashlib.sha256(
                        source_report_path.read_bytes()
                    ).hexdigest()
            expected_replacement = {
                "source_index": str(source_path),
                "source_index_sha256": source_sha,
                "source_run_id": source.get("run_id"),
                "source_status": source_status,
                "source_report": source_row.get("report"),
                "source_report_sha256": source_report_sha,
            }
            current_status_valid = current_row.get("status") == "ok"
            if current_row.get("status") == "error":
                terminal_issues: list[str] = []
                try:
                    current_report_path = _resolve(
                        repository_root or Path.cwd(),
                        current_row.get("report"),
                    )
                    current_report_raw = current_report_path.read_bytes()
                    current_report = json.loads(current_report_raw)
                    if not isinstance(current_report, dict):
                        raise ValueError("report JSON must be an object")
                except (
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                ) as error:
                    terminal_issues.append(f"unreadable terminal report: {error}")
                else:
                    current_attempt = current_row.get("trial_attempt")
                    has_current_overhead = any(
                        isinstance(overhead, dict)
                        and overhead.get("arm") == key[0]
                        and overhead.get("model") == key[1]
                        and overhead.get("seed") == key[2]
                        and overhead.get("task_id") == key[3]
                        and overhead.get("trial_attempt") == current_attempt
                        and overhead.get("source_provenance") is None
                        for overhead in (
                            run_index.get("c1_operational_overhead") or []
                        )
                    )
                    _, terminal_issues = _local_settled_terminal_evidence(
                        arm=key[0],
                        entry=current_row,
                        report=current_report,
                        actual_report_sha=hashlib.sha256(
                            current_report_raw
                        ).hexdigest(),
                        has_current_overhead=has_current_overhead,
                    )
                current_status_valid = not terminal_issues
                if terminal_issues:
                    issues.append(
                        f"invalid local settled terminal evidence: {key}: "
                        + "; ".join(terminal_issues)
                    )
            if (
                not current_status_valid
                or current_row.get("imported") is True
                or current_row.get("replacement_for") != expected_replacement
                or current_row.get("source_provenance") is not None
            ):
                issues.append(f"replacement tuple provenance mismatch: {key}")
            attempt = current_row.get("trial_attempt")
            expected_attempt_id = (
                f"{run_index.get('run_id')}:{key[0]}:{key[1] or 'na'}:"
                f"{key[2] if key[2] is not None else 'na'}:{key[3]}:"
                f"attempt{attempt}"
            )
            if (
                not isinstance(attempt, int)
                or isinstance(attempt, bool)
                or attempt < 0
                or attempt >= maximum_attempts
                or current_row.get("attempt_id") != expected_attempt_id
            ):
                issues.append(f"replacement tuple attempt provenance mismatch: {key}")
            replacement_rows[key] = (current_row, expected_replacement)
        else:
            issues.append(f"source tuple has non-replaceable status {source_status}: {key}")
    def overhead_map(rows: Any, *, imported: bool) -> dict[TrialKey, dict[str, Any]]:
        mapping: dict[TrialKey, dict[str, Any]] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            provenance = row.get("source_provenance")
            if imported and (
                not isinstance(provenance, dict)
                or provenance.get("source_index_sha256") != source_sha
            ):
                continue
            key = _trial_key(str(row.get("arm") or ""), row)
            if key in mapping:
                issues.append("C1 recovery overhead contains duplicate tuples")
            mapping[key] = row
        return mapping

    source_overhead = overhead_map(
        source.get("c1_operational_overhead"), imported=False
    )
    current_source_overhead = overhead_map(
        run_index.get("c1_operational_overhead"), imported=True
    )
    if set(source_overhead) != quarantine_keys or set(
        current_source_overhead
    ) != (
        quarantine_keys
        | exact_timeout_keys
        | retryable_infrastructure_keys
        | registered_harness_failure_keys
    ):
        issues.append(
            "C1 recovery overhead does not match source quarantine/replacement tuples"
        )
    for key in quarantine_keys & set(source_overhead) & set(current_source_overhead):
        source_row = source_rows[key]
        source_cost_row = source_overhead[key]
        current_cost_row = current_source_overhead[key]
        report_path = Path(str(source_row.get("report") or "")).expanduser()
        valid_report = bool(
            report_path.is_file()
            and _is_sha256(source_row.get("report_sha256"))
            and hashlib.sha256(report_path.read_bytes()).hexdigest()
            == source_row.get("report_sha256")
        )
        report_payload = None
        if valid_report:
            try:
                report_payload = json.loads(report_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                valid_report = False
        if (
            not valid_report
            or not isinstance(report_payload, dict)
            or report_payload.get("complete") is True
            or not cost.contains_unknown_usage(report_payload)
        ):
            issues.append(f"source quarantine report is not valid unknown evidence: {key}")
            continue
        expected_usage = _quarantined_usage_snapshot(report_path)
        if (
            source_cost_row.get("report") != source_row.get("report")
            or source_cost_row.get("report_sha256") != source_row.get("report_sha256")
            or source_cost_row.get("known_and_unknown_usage") != expected_usage
        ):
            issues.append(f"source quarantine overhead payload mismatch: {key}")
        current_without_provenance = {
            field: value
            for field, value in current_cost_row.items()
            if field != "source_provenance"
        }
        if current_without_provenance != source_cost_row:
            issues.append(f"imported quarantine overhead differs from source: {key}")
    replaceable_error_keys = (
        exact_timeout_keys
        | retryable_infrastructure_keys
        | registered_harness_failure_keys
    )
    for key in replaceable_error_keys & set(current_source_overhead):
        source_row = source_rows[key]
        overhead = current_source_overhead[key]
        report_path = Path(str(source_row.get("report") or "")).expanduser()
        report_payload = None
        report_sha = None
        if report_path.is_file():
            raw = report_path.read_bytes()
            report_sha = hashlib.sha256(raw).hexdigest()
            try:
                report_payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        results = (
            report_payload.get("results")
            if isinstance(report_payload, dict)
            else None
        )
        result = results[0] if isinstance(results, list) and len(results) == 1 else None
        usage = (
            result.get("actual_cost") or result.get("cost")
            if isinstance(result, dict)
            else None
        )
        is_timeout = key in exact_timeout_keys
        timeout_s = source.get("c1_task_timeout_s")
        expected_timeout_reason = (
            f"C1 task {key[3]} exceeded its {timeout_s:g}s wall-clock limit"
            if isinstance(timeout_s, (int, float))
            else ""
        )
        attempts = result.get("attempt_costs") if isinstance(result, dict) else None
        latest_attempt = (
            attempts[-1] if isinstance(attempts, list) and attempts else None
        )
        is_registered_harness_failure = key in registered_harness_failure_keys
        expected_status = (
            "replaced_exact_usage_task_timeout"
            if is_timeout
            else (
                "replaced_registered_exact_usage_harness_failure"
                if is_registered_harness_failure
                else "replaced_exact_usage_retryable_infrastructure"
            )
        )
        if (
            not isinstance(report_payload, dict)
            or report_payload.get("complete") is not False
            or not isinstance(report_payload.get("retryable_incomplete"), bool)
            or cost.contains_unknown_usage(report_payload)
            or not isinstance(result, dict)
            or str(result.get("task_id") or "") != key[3]
            or result.get("incomplete") is not True
            or not isinstance(usage, dict)
            or usage.get("usage_complete") is not True
            or any(
                usage.get(field) != 0
                for field in (
                    "unknown_prompt_token_calls",
                    "unknown_completion_token_calls",
                    "unknown_total_token_calls",
                )
            )
            or (
                is_timeout
                and (
                    report_payload.get("retryable_incomplete") is not False
                    or result.get("timed_out") is not True
                    or result.get("infrastructure_error") is not False
                    or result.get("retryable_incomplete") is not False
                    or str(result.get("reason") or "")
                    != expected_timeout_reason
                )
            )
            or (
                not is_timeout
                and not is_registered_harness_failure
                and (
                    result.get("timed_out") is True
                    or result.get("infrastructure_error") is False
                    or result.get("retryable_incomplete") is not True
                    or not isinstance(latest_attempt, dict)
                    or latest_attempt.get("attempt_status")
                    != "retryable_infrastructure"
                    or latest_attempt.get("retry_category") != "infra"
                    or not isinstance(latest_attempt.get("retry_reason"), str)
                    or not latest_attempt["retry_reason"].strip()
                    or not isinstance(result.get("reason"), str)
                    or not result["reason"].strip()
                )
            )
            or overhead.get("status") != expected_status
            or overhead.get("report_sha256") != report_sha
            or overhead.get("known_and_unknown_usage") != usage
        ):
            issues.append(f"source exact-usage replacement evidence is invalid: {key}")

    local_overhead: dict[
        TrialKey, dict[int, dict[str, Any]]
    ] = {}
    for raw in run_index.get("c1_operational_overhead") or []:
        if not isinstance(raw, dict):
            issues.append("C1 recovery operational overhead contains a malformed row")
            continue
        provenance = raw.get("source_provenance")
        if provenance is not None:
            if (
                not isinstance(provenance, dict)
                or provenance.get("source_index_sha256") != source_sha
            ):
                issues.append(
                    "C1 recovery imported overhead has invalid source provenance"
                )
            continue
        key = (
            str(raw.get("arm") or ""),
            raw.get("model"),
            raw.get("seed"),
            str(raw.get("task_id") or ""),
        )
        attempt = raw.get("trial_attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool):
            issues.append("C1 local recovery overhead has an invalid attempt ordinal")
            continue
        attempts = local_overhead.setdefault(key, {})
        if attempt in attempts:
            issues.append("C1 local recovery overhead contains a duplicate attempt")
        attempts[attempt] = raw
    if not set(local_overhead).issubset(set(replacement_rows)):
        issues.append("C1 local recovery overhead exists outside replacement tuples")
    for key, (current_row, expected_replacement) in replacement_rows.items():
        attempt = current_row.get("trial_attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
            continue
        status = current_row.get("status")
        attempts = local_overhead.get(key, {})
        expected_ordinals = set(
            range(
                attempt
                + (
                    status == "quarantined_unknown_usage"
                    or (status == "error" and attempt in attempts)
                )
            )
        )
        if set(attempts) != expected_ordinals:
            issues.append(
                f"C1 recovery noncompleted-attempt ledger is not contiguous: {key}"
            )
        for ordinal, overhead in attempts.items():
            expected_attempt_id = (
                f"{run_index.get('run_id')}:{key[0]}:{key[1] or 'na'}:"
                f"{key[2] if key[2] is not None else 'na'}:{key[3]}:"
                f"attempt{ordinal}"
            )
            overhead_status = overhead.get("status")
            if (
                ordinal < 0
                or ordinal >= maximum_attempts
                or overhead.get("attempt_id") != expected_attempt_id
                or overhead_status
                not in {
                    "quarantined_unknown_usage",
                    "exact_usage_task_timeout",
                    "exact_usage_retryable_infrastructure",
                }
                or overhead.get("replacement_for") != expected_replacement
            ):
                issues.append(
                    f"C1 recovery noncompleted-attempt provenance mismatch: {key}"
                )
            report_path = Path(str(overhead.get("report") or "")).expanduser()
            valid_report = bool(
                report_path.is_file()
                and _is_sha256(overhead.get("report_sha256"))
                and hashlib.sha256(report_path.read_bytes()).hexdigest()
                == overhead.get("report_sha256")
            )
            report_payload = None
            if valid_report:
                try:
                    report_payload = json.loads(report_path.read_bytes())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    valid_report = False
            results = (
                report_payload.get("results")
                if isinstance(report_payload, dict)
                else None
            )
            report_context = (
                report_payload.get("run_context")
                if isinstance(report_payload, dict)
                else None
            )
            result = (
                results[0]
                if isinstance(results, list)
                and len(results) == 1
                and isinstance(results[0], dict)
                else None
            )
            common_invalid = (
                not valid_report
                or not isinstance(report_payload, dict)
                or not isinstance(result, dict)
                or str(result.get("task_id") or "") != key[3]
                or not isinstance(report_context, dict)
                or report_context.get("run_id") != expected_attempt_id
                or report_context.get("trial_attempt") != ordinal
            )
            if overhead_status == "quarantined_unknown_usage":
                if (
                    common_invalid
                    or report_payload.get("complete") is True
                    or not cost.contains_unknown_usage(report_payload)
                ):
                    issues.append(
                        f"C1 recovery unknown-attempt report is invalid: {key}"
                    )
                elif overhead.get(
                    "known_and_unknown_usage"
                ) != _quarantined_usage_snapshot(report_path):
                    issues.append(
                        f"C1 recovery unknown-attempt usage mismatch: {key}"
                    )
                continue
            usage = (
                result.get("actual_cost") or result.get("cost")
                if isinstance(result, dict)
                else None
            )
            timeout_s = run_index.get("c1_task_timeout_s")
            expected_timeout_reason = (
                f"C1 task {key[3]} exceeded its {timeout_s:g}s wall-clock limit"
                if isinstance(timeout_s, (int, float))
                else ""
            )
            result_attempts = (
                result.get("attempt_costs") if isinstance(result, dict) else None
            )
            latest_result_attempt = (
                result_attempts[-1]
                if isinstance(result_attempts, list) and result_attempts
                else None
            )
            exact_invalid = (
                common_invalid
                or report_payload.get("complete") is not False
                or not isinstance(report_payload.get("retryable_incomplete"), bool)
                or cost.contains_unknown_usage(report_payload)
                or result.get("incomplete") is not True
                or not isinstance(usage, dict)
                or usage.get("usage_complete") is not True
                or any(
                    usage.get(field) != 0
                    for field in (
                        "unknown_prompt_token_calls",
                        "unknown_completion_token_calls",
                        "unknown_total_token_calls",
                    )
                )
                or (
                    overhead_status == "exact_usage_task_timeout"
                    and (
                        report_payload.get("retryable_incomplete") is not False
                        or result.get("timed_out") is not True
                        or result.get("infrastructure_error") is not False
                        or result.get("retryable_incomplete") is not False
                        or str(result.get("reason") or "")
                        != expected_timeout_reason
                    )
                )
                or (
                    overhead_status
                    == "exact_usage_retryable_infrastructure"
                    and (
                        result.get("timed_out") is True
                        or result.get("infrastructure_error") is False
                        or result.get("retryable_incomplete") is not True
                        or not isinstance(latest_result_attempt, dict)
                        or latest_result_attempt.get("attempt_status")
                        != "retryable_infrastructure"
                        or latest_result_attempt.get("retry_category") != "infra"
                        or not isinstance(
                            latest_result_attempt.get("retry_reason"), str
                        )
                        or not latest_result_attempt["retry_reason"].strip()
                    )
                )
            )
            if exact_invalid:
                issues.append(
                    f"C1 recovery exact-usage attempt report is invalid: {key}"
                )
            elif overhead.get("known_and_unknown_usage") != usage:
                issues.append(
                    f"C1 recovery exact-usage attempt usage mismatch: {key}"
                )
        if status in {"quarantined_unknown_usage", "error"} and attempt in attempts:
            latest = attempts[attempt]
            if any(
                latest.get(field) != current_row.get(field)
                for field in (
                    "attempt_id",
                    "trial_attempt",
                    "report",
                    "report_sha256",
                    "error",
                    "replacement_for",
                )
            ):
                issues.append(
                    f"latest C1 recovery noncompleted row differs from ledger: {key}"
                )
    expected_completed_trials = sum(
        row.get("status") == "ok" for row in source_rows.values()
    )
    if (
        isinstance(migration, dict)
        and migration.get("migration_id")
        == "c1_source_action_sidecar_validation_v1"
    ):
        expected_completed_trials += len(
            migration.get("preserved_operational_failure_trials") or []
        )
    if (
        source_meta.get("completed_trials") != expected_completed_trials
        or source_meta.get("replacement_trials") != replacement_count
        or source_meta.get("quarantined_trials") != len(quarantine_keys)
        or source_meta.get("exact_usage_timeout_trials") != len(exact_timeout_keys)
        or source_meta.get("exact_usage_retryable_infrastructure_trials")
        != len(retryable_infrastructure_keys | registered_harness_failure_keys)
    ):
        issues.append("C1 recovery source summary counts are inconsistent")
    return issues


def _formal_cache_policy_issues(
    run_index: dict[str, Any], report_root: Path
) -> list[str]:
    policy = run_index.get("c1_cache_policy")
    if not isinstance(policy, dict):
        return ["formal C1 run index is missing c1_cache_policy"]
    issues: list[str] = []
    if policy.get("golden_and_reference") != "run_scoped":
        issues.append("formal C1 golden/reference cache policy must be run_scoped")
    if policy.get("visual_judge") != "disabled":
        issues.append("formal C1 visual-judge cache policy must be disabled")
    cache_paths: dict[str, Path] = {}
    for field, basename in (
        ("root", "c1-artifact-cache"),
        ("self_synthesis", "c1-self-synthesis-cache"),
    ):
        raw = policy.get(field)
        if not isinstance(raw, str) or not raw:
            issues.append(f"formal C1 cache policy missing {field}")
            continue
        path = Path(raw)
        if not path.is_absolute() or path.name != basename:
            issues.append(
                f"formal C1 cache policy {field} must be an absolute run-scoped {basename} path"
            )
            continue
        cache_paths[field] = path.resolve()
    if len(cache_paths) == 2 and cache_paths["root"].parent != cache_paths["self_synthesis"].parent:
        issues.append("formal C1 cache paths do not share one run directory")
    if cache_paths:
        run_dir = next(iter(cache_paths.values())).parent
        for arm in _C1_ARM_LABELS:
            for entry in run_index.get("arms", {}).get(arm, []) or []:
                report = entry.get("report") if isinstance(entry, dict) else None
                if not isinstance(report, str) or not report:
                    continue
                resolved = _resolve(report_root, report).resolve()
                allowed_run_dirs = [run_dir]
                source_provenance = entry.get("source_provenance")
                if isinstance(source_provenance, dict):
                    source_policy = source_provenance.get("source_cache_policy")
                    source_root = (
                        source_policy.get("root")
                        if isinstance(source_policy, dict)
                        else None
                    )
                    if isinstance(source_root, str) and source_root:
                        allowed_run_dirs.append(Path(source_root).expanduser().resolve().parent)
                try:
                    if not any(
                        resolved.is_relative_to(candidate)
                        for candidate in allowed_run_dirs
                    ):
                        raise ValueError
                except (AttributeError, ValueError):
                    issues.append(
                        f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                        "report is outside the cache policy run directory"
                    )
    return issues


def _formal_runtime_lock_policy_issues(
    run_index: dict[str, Any], manifest: dict[str, Any]
) -> list[str]:
    """Require one manifest-registered, resolved lock namespace for formal C1."""

    configured = manifest.get("runtime_lock")
    if not isinstance(configured, dict):
        return ["formal C1 manifest is missing runtime_lock registration"]
    issues: list[str] = []
    scope = configured.get("scope")
    if scope != _RUNTIME_LOCK_SCOPE:
        issues.append(
            f"formal C1 manifest runtime_lock.scope must be {_RUNTIME_LOCK_SCOPE}"
        )
    raw_dir = configured.get("lock_dir")
    expected_dir: str | None = None
    if not isinstance(raw_dir, str) or not raw_dir.strip():
        issues.append("formal C1 manifest runtime_lock.lock_dir is missing")
    else:
        registered = Path(raw_dir).expanduser()
        if not registered.is_absolute():
            issues.append(
                "formal C1 manifest runtime_lock.lock_dir must be absolute"
            )
        else:
            expected_dir = str(registered.resolve())
    raw_wait_timeout = configured.get("wait_timeout_s")
    expected_wait_timeout: float | None = None
    if (
        not isinstance(raw_wait_timeout, (int, float))
        or isinstance(raw_wait_timeout, bool)
        or not math.isfinite(float(raw_wait_timeout))
        or float(raw_wait_timeout) <= 0
    ):
        issues.append(
            "formal C1 manifest runtime_lock.wait_timeout_s must be positive and finite"
        )
    else:
        expected_wait_timeout = float(raw_wait_timeout)

    actual = run_index.get("runtime_lock")
    if not isinstance(actual, dict):
        issues.append("formal C1 run index is missing runtime_lock provenance")
        return issues
    if set(actual) != {
        "scope",
        "lock_dir",
        "wait_timeout_s",
        "task_clock_policy",
    }:
        issues.append(
            "formal C1 run index runtime_lock must contain exactly scope, lock_dir, "
            "wait_timeout_s, and task_clock_policy"
        )
    if actual.get("scope") != scope:
        issues.append("formal C1 run index runtime_lock.scope differs from manifest")
    if expected_dir is not None and actual.get("lock_dir") != expected_dir:
        issues.append(
            "formal C1 run index runtime_lock.lock_dir differs from manifest"
        )
    if (
        expected_wait_timeout is not None
        and actual.get("wait_timeout_s") != expected_wait_timeout
    ):
        issues.append(
            "formal C1 run index runtime_lock.wait_timeout_s differs from manifest"
        )
    if (
        actual.get("task_clock_policy")
        != "starts_after_resource_acquisition"
    ):
        issues.append(
            "formal C1 run index runtime_lock.task_clock_policy is invalid"
        )
    return issues


def _formal_live_provenance_issues(
    run_index: dict[str, Any], manifest: dict[str, Any], report_root: Path
) -> list[str]:
    """Recompute publication-critical provenance instead of trusting the index."""

    issues: list[str] = []
    expected_manifest = run_context.manifest_hash(manifest)
    if run_index.get("manifest_hash") != expected_manifest:
        issues.append("formal C1 manifest hash does not match the current manifest")
    selected_ids = run_index.get("task_ids")
    try:
        expected_task_manifest = c1_task_manifest_hash(
            report_root,
            manifest=manifest,
            arms=list(run_index.get("arms_run") or []),
            task_ids=(
                [str(task_id) for task_id in selected_ids]
                if isinstance(selected_ids, list)
                else None
            ),
            manifest_hash=expected_manifest,
        )
    except Exception as error:
        issues.append(f"formal C1 task manifest hash cannot be recomputed: {error}")
        expected_task_manifest = None
    if run_index.get("task_manifest_hash") != expected_task_manifest:
        issues.append("formal C1 task manifest hash does not match the selected task set")
    registered_task_set, task_set_error = _expected_c1_task_set_provenance(
        run_index,
        manifest,
        report_root,
    )
    if task_set_error is not None or not isinstance(registered_task_set, dict):
        issues.append(
            "formal C1 prompt/random provenance cannot be derived from the "
            f"registered task source: {task_set_error or 'unknown error'}"
        )
    else:
        task_metadata = registered_task_set.get("task_metadata")
        if not isinstance(task_metadata, dict):
            issues.append("formal C1 registered task metadata is missing")
        else:
            expected_prompt_revisions = {
                "arms": {
                    str(arm): {
                        key: str(
                            (
                                manifest.get("arms", {})
                                .get(str(arm), {})
                                .get(key)
                                or ""
                            )
                        )
                        for key in (
                            "prompt_revision",
                            "phase_b_prompt_revision",
                        )
                    }
                    for arm in (run_index.get("arms_run") or [])
                    if str(arm) in _C1_ALL_ARM_LABELS
                },
                "tasks": {
                    task_id: str(metadata.get("prompt_revision") or "")
                    for task_id, metadata in task_metadata.items()
                    if isinstance(metadata, dict)
                },
            }
            expected_random_controls = {
                task_id: copy.deepcopy(metadata.get("random_control"))
                for task_id, metadata in task_metadata.items()
                if isinstance(metadata, dict)
            }
            if run_index.get("c1_prompt_revisions") != expected_prompt_revisions:
                issues.append(
                    "formal C1 prompt revisions do not match the registered tasks"
                )
            if run_index.get("c1_random_controls") != expected_random_controls:
                issues.append(
                    "formal C1 random controls do not match the registered tasks"
                )
    background_protocol = manifest.get("c1_background_pixel_protocol")
    if not isinstance(background_protocol, dict):
        issues.append("formal C1 manifest is missing the background-pixel protocol")
    else:
        canonical_background = json.dumps(
            background_protocol,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        background_sha256 = hashlib.sha256(
            canonical_background.encode("utf-8")
        ).hexdigest()
        if run_index.get("c1_background_pixel_protocol") != background_protocol:
            issues.append(
                "formal C1 background-pixel protocol does not match the manifest"
            )
        if (
            run_index.get("c1_background_pixel_protocol_sha256")
            != background_sha256
        ):
            issues.append(
                "formal C1 background-pixel protocol hash does not match the manifest"
            )
    manifest_profiles = manifest.get("model_request_profiles")
    canonical_profiles = (
        dict(sorted(manifest_profiles.items()))
        if isinstance(manifest_profiles, dict)
        else None
    )
    profiles_blob = (
        json.dumps(canonical_profiles, sort_keys=True, separators=(",", ":"))
        if canonical_profiles is not None
        else None
    )
    profiles_sha256 = (
        hashlib.sha256(profiles_blob.encode("utf-8")).hexdigest()
        if profiles_blob is not None
        else None
    )
    if run_index.get("model_request_profiles") != canonical_profiles:
        issues.append("formal C1 model request profiles do not match the manifest")
    if run_index.get("model_request_profiles_sha256") != profiles_sha256:
        issues.append("formal C1 model request profile hash does not match the manifest")
    try:
        expected_provider_timeout_s = manifest_provider_timeout_s(manifest)
    except ValueError as error:
        issues.append(f"formal C1 provider timeout is invalid: {error}")
    else:
        if run_index.get("provider_timeout_s") != expected_provider_timeout_s:
            issues.append("formal C1 provider timeout does not match the manifest")
    current_commit = run_context.git_commit(report_root)
    if not current_commit or run_index.get("git_commit") != current_commit:
        issues.append("formal C1 git commit does not match the current checkout")
    try:
        current_harness = c1_harness_hash(report_root)
    except Exception as error:
        issues.append(f"formal C1 harness provenance cannot be recomputed: {error}")
    else:
        if run_index.get("harness_hash") != current_harness:
            issues.append("formal C1 harness hash does not match the current harness")
    try:
        current_revisions = c1_application_revisions(report_root)
    except Exception as error:
        issues.append(f"formal C1 application revisions cannot be recomputed: {error}")
    else:
        if run_index.get("c1_application_revisions") != current_revisions:
            issues.append(
                "formal C1 application revisions do not match the current checkout"
            )
    try:
        current_environment = validate_c1_environment(
            report_root,
            manifest=manifest,
        )
    except Exception as error:
        issues.append(f"formal C1 environment cannot be verified: {error}")
    else:
        if run_index.get("c1_environment") != current_environment:
            issues.append("formal C1 environment fingerprint does not match this machine")
    return issues


def _formal_gate_artifact_issues(run_index: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for label in ("preflight", "calibration"):
        metadata = run_index.get(label)
        if not isinstance(metadata, dict):
            issues.append(f"formal C1 {label} artifact metadata is missing")
            continue
        raw_path = metadata.get("artifact")
        expected_sha = metadata.get("artifact_sha256")
        if not isinstance(raw_path, str) or not raw_path or not _is_sha256(expected_sha):
            issues.append(f"formal C1 {label} artifact path/hash is invalid")
            continue
        path = Path(raw_path)
        if not path.is_file():
            issues.append(f"formal C1 {label} artifact is missing")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
            issues.append(f"formal C1 {label} artifact hash mismatch")
    return issues


def _c1_sidecar_issues(
    path: Path,
    *,
    arm: str,
    entry: dict[str, Any],
    run_index: dict[str, Any],
    report: dict[str, Any],
    visual_bridge: dict[str, Any] | None = None,
    visual_judge: dict[str, Any] | None = None,
    expected_task_set: dict[str, Any] | None = None,
) -> list[str]:
    """Validate the producer-adjacent invocation provenance before aggregation."""

    sidecar_path = path.parent / "p5-invocation-context.json"
    if not sidecar_path.is_file():
        return [f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: missing invocation sidecar"]
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except Exception:
        return [f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: unreadable invocation sidecar"]
    if not isinstance(sidecar, dict):
        return [f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: malformed invocation sidecar"]

    issues: list[str] = []
    evidence_index = run_index
    imported_task_file_rebind = bool(
        entry.get("imported") is True
        and isinstance(entry.get("source_provenance"), dict)
        and isinstance(run_index.get("c1_source_contract_migration"), dict)
        and "task_manifest_hash_rebind"
        in (
            run_index["c1_source_contract_migration"].get(
                "allowed_migrations"
            )
            or []
        )
    )
    registered_provider_timeout_s = evidence_index.get(
        "provider_timeout_s", AGENT_S_PROVIDER_TIMEOUT_S
    )
    source_provenance = entry.get("source_provenance")
    if isinstance(source_provenance, dict):
        source_path_raw = source_provenance.get("source_index")
        source_sha = source_provenance.get("source_index_sha256")
        if not isinstance(source_path_raw, str) or not source_path_raw:
            return [f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: imported trial missing source index"]
        source_path = Path(source_path_raw).expanduser()
        if not source_path.is_file():
            return [f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: imported source index missing"]
        source_raw = source_path.read_bytes()
        if not _is_sha256(source_sha) or hashlib.sha256(source_raw).hexdigest() != source_sha:
            return [f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: imported source index SHA mismatch"]
        try:
            source_index = json.loads(source_raw)
        except json.JSONDecodeError:
            return [f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: imported source index unreadable"]
        if not isinstance(source_index, dict) or source_index.get(
            "c1_trial_persistence"
        ) != run_index.get("c1_trial_persistence"):
            return [f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: imported source schema mismatch"]
        if any(
            source_provenance.get(field) != source_index.get(source_field)
            for field, source_field in (
                ("source_run_id", "run_id"),
                ("source_cache_policy", "c1_cache_policy"),
                ("source_preflight", "preflight"),
                ("source_calibration", "calibration"),
            )
        ):
            return [f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: imported source provenance mismatch"]
        matching_rows = [
            row
            for row in source_index.get("arms", {}).get(arm, []) or []
            if isinstance(row, dict)
            and row.get("model") == entry.get("model")
            and row.get("seed") == entry.get("seed")
            and row.get("task_id") == entry.get("task_id")
            and row.get("status") in {"ok", "error"}
        ]
        if len(matching_rows) != 1:
            return [f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: imported tuple is absent or duplicated in source"]
        source_row = matching_rows[0]
        if (
            source_row.get("report_sha256") != entry.get("report_sha256")
            or Path(str(source_row.get("report") or "")).expanduser().resolve()
            != path.resolve()
        ):
            return [f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: imported tuple differs from source row"]
        flattened = source_provenance.get("flattened_source_lineage")
        if isinstance(flattened, dict):
            evidence_path_raw = flattened.get("evidence_source_index")
            evidence_sha = flattened.get("evidence_source_index_sha256")
            if not isinstance(evidence_path_raw, str) or not evidence_path_raw:
                return [
                    f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                    "flattened evidence index is missing"
                ]
            evidence_path = Path(evidence_path_raw).expanduser()
            if not evidence_path.is_file():
                return [
                    f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                    "flattened evidence index disappeared"
                ]
            evidence_raw = evidence_path.read_bytes()
            if (
                not _is_sha256(evidence_sha)
                or hashlib.sha256(evidence_raw).hexdigest() != evidence_sha
            ):
                return [
                    f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                    "flattened evidence index SHA mismatch"
                ]
            try:
                evidence_payload = json.loads(evidence_raw)
            except json.JSONDecodeError:
                return [
                    f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                    "flattened evidence index is unreadable"
                ]
            if (
                not isinstance(evidence_payload, dict)
                or evidence_payload.get("run_id")
                != flattened.get("evidence_source_run_id")
            ):
                return [
                    f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                    "flattened evidence run provenance mismatch"
                ]
            evidence_rows = [
                row
                for row in evidence_payload.get("arms", {}).get(arm, []) or []
                if isinstance(row, dict)
                and row.get("model") == entry.get("model")
                and row.get("seed") == entry.get("seed")
                and row.get("task_id") == entry.get("task_id")
                and row.get("status") in {"ok", "error"}
            ]
            if len(evidence_rows) != 1:
                return [
                    f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                    "flattened evidence tuple is absent or duplicated"
                ]
            evidence_row = evidence_rows[0]
            immutable_fields = (
                "model",
                "seed",
                "task_id",
                "trial_attempt",
                "attempt_id",
                "report",
                "report_sha256",
                "replacement_for",
            )
            if any(
                evidence_row.get(field) != source_row.get(field)
                for field in immutable_fields
            ):
                return [
                    f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                    "flattened evidence tuple differs from source row"
                ]
            evidence_index = evidence_payload
            entry = evidence_row
            visual_bridge = _visual_bridge_provenance(evidence_index, None)
            visual_judge = _visual_judge_provenance(evidence_index, None)
        expected_sidecar_sha = source_provenance.get("source_sidecar_sha256")
        if (
            not _is_sha256(expected_sidecar_sha)
            or hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
            != expected_sidecar_sha
        ):
            return [f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: imported sidecar SHA mismatch"]
        if not isinstance(flattened, dict):
            evidence_index = source_index
            entry = source_row

    atomic_evidence = report.get("atomic_trial_evidence")
    shared_snapshot = False
    if atomic_evidence is not None:
        shared_snapshot = bool(
            isinstance(atomic_evidence, dict)
            and atomic_evidence.get("schema_version") == 3
            and atomic_evidence.get("mode")
            == "shared_invocation_incremental_snapshot_v1"
            and atomic_evidence.get("task_id") == entry.get("task_id")
            and _is_sha256(
                atomic_evidence.get("source_invocation_report_sha256")
            )
        )
        if not shared_snapshot:
            issues.append(
                f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                "malformed atomic_trial_evidence"
            )
        else:
            source_invocation = Path(
                str(atomic_evidence.get("source_invocation_report") or "")
            ).expanduser()
            source_cache = evidence_index.get("c1_cache_policy")
            source_cache_root = (
                source_cache.get("root") if isinstance(source_cache, dict) else None
            )
            source_run_dir = (
                Path(source_cache_root).expanduser().resolve().parent
                if isinstance(source_cache_root, str) and source_cache_root
                else None
            )
            if (
                not source_invocation.is_file()
                or source_run_dir is None
                or not source_invocation.resolve().is_relative_to(source_run_dir)
                or hashlib.sha256(source_invocation.read_bytes()).hexdigest()
                != atomic_evidence.get("source_invocation_report_sha256")
            ):
                issues.append(
                    f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                    "atomic source invocation SHA mismatch"
                )
            else:
                try:
                    source_payload = json.loads(
                        source_invocation.read_text(encoding="utf-8")
                    )
                except json.JSONDecodeError:
                    source_payload = None
                source_results = (
                    source_payload.get("results")
                    if isinstance(source_payload, dict)
                    else None
                )
                snapshot_results = report.get("results")
                matching = [
                    value
                    for value in (source_results or [])
                    if isinstance(value, dict)
                    and value.get("task_id") == entry.get("task_id")
                ]
                if (
                    len(matching) != 1
                    or not isinstance(snapshot_results, list)
                    or len(snapshot_results) != 1
                    or matching[0] != snapshot_results[0]
                ):
                    issues.append(
                        f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                        "atomic snapshot result differs from source invocation"
                    )
    if evidence_index.get("c1_trial_persistence"):
        is_replacement = isinstance(entry.get("replacement_for"), dict)
        if is_replacement and shared_snapshot:
            issues.append(
                f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                "replacement trial must use a fresh one-task invocation"
            )
        if not is_replacement and not shared_snapshot:
            issues.append(
                f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                "healthy original trial lacks schema-3 shared-invocation snapshot evidence"
            )
        expected_trial_attempt = entry.get("trial_attempt") if is_replacement else 0
        policy = evidence_index.get("c1_trial_persistence")
        maximum_attempts = (
            policy.get("max_recovery_attempts_per_tuple")
            if isinstance(policy, dict)
            else None
        )
        if (
            not isinstance(expected_trial_attempt, int)
            or isinstance(expected_trial_attempt, bool)
            or expected_trial_attempt < 0
            or (
                is_replacement
                and (
                    not isinstance(maximum_attempts, int)
                    or isinstance(maximum_attempts, bool)
                    or expected_trial_attempt >= maximum_attempts
                )
            )
        ):
            issues.append(
                f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                "trial attempt is outside the registered bound"
            )
            expected_trial_attempt = 0
    else:
        expected_trial_attempt = 0

    expected = {
        "schema_version": (
            6 if evidence_index.get("c1_trial_persistence") else 4
        ),
        "kind": "p5_c1_invocation_context",
        "run_id": evidence_index.get("run_id"),
        "arm": arm,
        "model": entry.get("model"),
        "seed": entry.get("seed"),
        "task_id": None if shared_snapshot else entry.get("task_id"),
        "trial_attempt": expected_trial_attempt,
        "git_commit": evidence_index.get("git_commit"),
        "manifest_hash": evidence_index.get("manifest_hash"),
        "task_manifest_hash": evidence_index.get("task_manifest_hash"),
        "harness_hash": evidence_index.get("harness_hash"),
        "preflight": (
            {
                key: value
                for key, value in evidence_index.get("preflight", {}).items()
                if key != "artifact"
            }
            if isinstance(evidence_index.get("preflight"), dict)
            else evidence_index.get("preflight")
        ),
        "api_provider": evidence_index.get("api_provider"),
        "model_request_profiles": evidence_index.get("model_request_profiles"),
        "model_request_profiles_sha256": evidence_index.get(
            "model_request_profiles_sha256"
        ),
        "runtime_lock": evidence_index.get("runtime_lock"),
        "cache_policy": evidence_index.get("c1_cache_policy"),
        "usage_accounting": evidence_index.get("usage_accounting"),
        "c1_trial_persistence": evidence_index.get("c1_trial_persistence"),
        "c1_start_gate_protocol": evidence_index.get("c1_start_gate_protocol"),
        "c1_background_pixel_protocol": evidence_index.get(
            "c1_background_pixel_protocol"
        ),
        "c1_background_pixel_protocol_sha256": evidence_index.get(
            "c1_background_pixel_protocol_sha256"
        ),
        "c1_prompt_revisions": evidence_index.get("c1_prompt_revisions"),
        "c1_random_controls": evidence_index.get("c1_random_controls"),
        "c1_application_revisions": evidence_index.get(
            "c1_application_revisions"
        ),
        "c1_environment": evidence_index.get("c1_environment"),
        "calibration": (
            {
                key: value
                for key, value in evidence_index.get("calibration", {}).items()
                if key != "artifact"
            }
            if isinstance(evidence_index.get("calibration"), dict)
            else evidence_index.get("calibration")
        ),
    }
    if "provider_timeout_s" in evidence_index:
        expected["provider_timeout_s"] = evidence_index.get("provider_timeout_s")
    if not evidence_index.get("c1_trial_persistence"):
        expected.pop("task_id", None)
        expected.pop("trial_attempt", None)
        expected.pop("c1_trial_persistence", None)
        expected.pop("c1_start_gate_protocol", None)
        expected.pop("usage_accounting", None)
        expected.pop("c1_background_pixel_protocol", None)
        expected.pop("c1_background_pixel_protocol_sha256", None)
        expected.pop("c1_prompt_revisions", None)
        expected.pop("c1_random_controls", None)
        expected.pop("c1_application_revisions", None)
        expected.pop("c1_environment", None)
        expected.pop("calibration", None)

    def expected_request_profile(model: Any) -> dict[str, Any] | None:
        clean = _clean_model(model)
        if clean is None:
            return None
        profiles = evidence_index.get("model_request_profiles")
        configured = profiles.get(clean) if isinstance(profiles, dict) else None
        if isinstance(configured, dict):
            configured = configured.get("profile")
        profile = configured if isinstance(configured, str) else PROVIDER_DEFAULT
        return ModelRequestProfile(clean, profile).provenance()
    expected["model_request_profile"] = expected_request_profile(entry.get("model"))
    issues.extend(
        [
        f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: sidecar {field} mismatch"
        for field, value in expected.items()
        if sidecar.get(field) != value
        ]
    )
    expected_judge = visual_judge or {}
    actual_judge = sidecar.get("visual_judge")
    if expected_judge and not isinstance(actual_judge, dict):
        issues.append(
            f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
            "sidecar visual_judge missing"
        )
    elif isinstance(actual_judge, dict):
        for field, value in expected_judge.items():
            if actual_judge.get(field) != value:
                issues.append(
                    f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                    f"sidecar visual_judge.{field} mismatch"
                )
    actual_task_set = sidecar.get("task_set")
    if expected_task_set and not isinstance(actual_task_set, dict):
        issues.append(
            f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: sidecar task_set missing"
        )
    elif isinstance(actual_task_set, dict):
        atomic_single = bool(
            evidence_index.get("c1_trial_persistence")
            and actual_task_set.get("task_ids") == [entry.get("task_id")]
            and expected_task_set
            and entry.get("task_id") in (expected_task_set.get("task_ids") or [])
        )
        fields = (
            ("source_task_file", "source_task_file_sha256")
            if atomic_single
            else (
                "task_ids",
                "source_task_file",
                "source_task_file_sha256",
                "task_set_hash",
            )
        )
        if imported_task_file_rebind:
            # The registered migration independently proves that the task-file
            # change is limited to the repaired OpenRA application revision.
            # Imported reports remain immutable and retain their source-era
            # task-file SHA by design.
            fields = tuple(
                field for field in fields if field != "source_task_file_sha256"
            )
        for field in fields:
            if expected_task_set and actual_task_set.get(field) != expected_task_set.get(field):
                issues.append(
                    f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                    f"sidecar task_set.{field} mismatch"
                )
    if evidence_index.get("c1_trial_persistence"):
        cache_policy_for_goldens = evidence_index.get("c1_cache_policy")
        raw_cache_root = (
            cache_policy_for_goldens.get("root")
            if isinstance(cache_policy_for_goldens, dict)
            else None
        )
        cache_root = (
            Path(raw_cache_root).expanduser().resolve()
            if isinstance(raw_cache_root, str) and raw_cache_root
            else None
        )
        run_dir = cache_root.parent if cache_root is not None else None
        for result in report.get("results") or []:
            if not isinstance(result, dict):
                continue
            goldens = result.get("oracle_goldens")
            if not isinstance(goldens, dict):
                continue
            allowed_root = cache_root if goldens.get("cache_hit") is True else run_dir
            for stage in ("before", "after"):
                raw_path = goldens.get(stage)
                if not isinstance(raw_path, str) or allowed_root is None:
                    continue
                try:
                    candidate = Path(raw_path).expanduser().resolve()
                except (OSError, RuntimeError, ValueError):
                    issues.append(
                        f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                        f"oracle golden {stage} path is invalid"
                    )
                    continue
                if not candidate.is_relative_to(allowed_root):
                    issues.append(
                        f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                        f"oracle golden {stage} path escapes registered "
                        f"{'cache' if goldens.get('cache_hit') is True else 'run'} root"
                    )
    if arm != "c1_state":
        expected_policy = (
            "agent_s" if arm in {"c1_agent_s", "c1_agent_s_synth"} else "gui_agent"
        )
        expected_start = (
            "synthesized"
            if arm in {"c1_gui_synth", "c1_agent_s_synth"}
            else "from_start"
        )
        bridge = visual_bridge or {}
        resolved_grounder_model = resolve_grounder_model(
            bridge.get("grounder_model"),
            observer_model=str(
                bridge.get("observer_model") or "claude-sonnet-5@relay"
            ),
            decision_model=str(entry.get("model")),
        )
        cache_policy = evidence_index.get("c1_cache_policy")
        cache_policy = cache_policy if isinstance(cache_policy, dict) else {}
        invocation = sidecar.get("invocation")
        expected_invocation = {
            "start_mode": expected_start,
            "policy_kind": expected_policy,
            "step_budgets": _C1_STEP_BUDGETS,
            "observer_model": bridge.get("observer_model"),
            "grounder_model": resolved_grounder_model,
            "synth_model": entry.get("model"),
            "shared_synthesis_cache": cache_policy.get("self_synthesis"),
            "start_gate_protocol": evidence_index.get(
                "c1_start_gate_protocol"
            ),
        }
        if "provider_timeout_s" in evidence_index:
            expected_invocation["provider_timeout_s"] = evidence_index.get(
                "provider_timeout_s", registered_provider_timeout_s
            )
        if not isinstance(invocation, dict):
            issues.append(
                f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                "sidecar invocation missing"
            )
        else:
            for field, value in expected_invocation.items():
                if invocation.get(field) != value:
                    issues.append(
                        f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                        f"sidecar invocation.{field} mismatch"
                    )

        report_expected = {
            "policy_kind": expected_policy,
            "start_mode": expected_start,
            "step_budgets": _C1_STEP_BUDGETS,
            "observer_model": bridge.get("observer_model"),
            "grounder_model": (
                resolved_grounder_model if expected_policy == "agent_s" else None
            ),
        }
        if "provider_timeout_s" in evidence_index:
            report_expected["provider_timeout_s"] = evidence_index.get(
                "provider_timeout_s", registered_provider_timeout_s
            )
        for field, value in report_expected.items():
            if report.get(field) != value:
                issues.append(
                    f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                    f"report {field} runtime contract mismatch"
                )
        metadata = report.get("policy_runtime_metadata")
        if not isinstance(metadata, dict):
            issues.append(
                f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                "report policy_runtime_metadata missing"
            )
        elif expected_policy == "agent_s":
            provider = evidence_index.get("api_provider")
            provider_name = provider.get("name") if isinstance(provider, dict) else None
            expected_metadata = {
                "policy_kind": "agent_s",
                "model": _clean_model(entry.get("model")),
                "provider": provider_name,
                "grounder_model": _clean_model(resolved_grounder_model),
                "grounder_provider": provider_name,
                "protocol_version": AGENT_S_PROTOCOL_VERSION,
                "agent_s_version": PINNED_AGENT_S_VERSION,
                "agent_s_commit": PINNED_AGENT_S_COMMIT,
                "runtime_mode": "background_pixel",
                "execution_backend": "background_pixel_v1",
                "coordinate_space": "frame_pixels_top_left_v1",
                "raw_screenshot_observation": True,
                "action_execution_owner": "parent_runtime",
                "foreground_required": False,
                "upstream_worker": True,
                "reflection_enabled": True,
                "local_code_enabled": False,
                "worker_observation_mode": "screenshot",
                "native_exec_enabled": False,
                "predict_timeout_mode": AGENT_S_PREDICT_TIMEOUT_MODE,
                "max_provider_attempts_per_predict": (
                    AGENT_S_MAX_PROVIDER_ATTEMPTS_PER_PREDICT
                ),
                "model_request_profiles": {
                    "worker": expected_request_profile(entry.get("model")),
                    "reflection": expected_request_profile(entry.get("model")),
                    "grounding": expected_request_profile(
                        resolved_grounder_model
                    ),
                },
            }
            if "provider_timeout_s" in evidence_index:
                expected_metadata["provider_timeout_s"] = evidence_index.get(
                    "provider_timeout_s", registered_provider_timeout_s
                )
            for field, value in expected_metadata.items():
                if field not in metadata or metadata.get(field) != value:
                    issues.append(
                        f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                        f"Agent-S runtime metadata {field} mismatch"
                    )
        else:
            provider = evidence_index.get("api_provider")
            provider_name = provider.get("name") if isinstance(provider, dict) else None
            expected_metadata = {
                "policy_kind": "gui_agent",
                "model": _clean_model(entry.get("model")),
                "provider": provider_name,
                "model_request_profile": expected_request_profile(
                    entry.get("model")
                ),
                "max_tokens": 8192,
                "reasoning_retry_max_tokens": 16384,
                "sdk_max_retries": 0,
            }
            if "provider_timeout_s" in evidence_index:
                expected_metadata["provider_timeout_s"] = evidence_index.get(
                    "provider_timeout_s", registered_provider_timeout_s
                )
            for field, value in expected_metadata.items():
                if field not in metadata or metadata.get(field) != value:
                    issues.append(
                        f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                        f"GuiAgent runtime metadata {field} mismatch"
                    )
    elif evidence_index.get("c1_trial_persistence"):
        expected_invocation = {
            "explore": True,
            "max_iters": 1,
            "seeds": 1,
            "restore_diagnostics": False,
            "restore_diagnostic_task_ids": [],
        }
        invocation = sidecar.get("invocation")
        if not isinstance(invocation, dict):
            issues.append(
                f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                "state sidecar invocation missing"
            )
        else:
            for field, expected_value in expected_invocation.items():
                if invocation.get(field) != expected_value:
                    issues.append(
                        f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                        f"state sidecar invocation.{field} mismatch"
                    )
        diagnostic_config = report.get("restore_diagnostics")
        if diagnostic_config != {"enabled": False, "task_ids": []}:
            issues.append(
                f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                "state report restore_diagnostics must be disabled"
            )
        for result in report.get("results") or []:
            if (
                not isinstance(result, dict)
                or result.get("restore_diagnostics_enabled") is not False
            ):
                issues.append(
                    f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                    "state result restore diagnostics must be disabled"
                )
                break
    run_calibration = evidence_index.get("calibration")
    if isinstance(run_calibration, dict):
        expected_calibration = {
            key: value
            for key, value in run_calibration.items()
            if key != "artifact"
        }
        if sidecar.get("calibration") != expected_calibration:
            issues.append(
                f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: sidecar calibration mismatch"
            )

    report_context = report.get("run_context")
    if not isinstance(report_context, dict):
        issues.append(
            f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: report missing run_context"
        )
        return issues
    report_expected = {
        "arm": arm,
        "model": entry.get("model"),
        "seed": entry.get("seed"),
        "task_id": None if shared_snapshot else entry.get("task_id"),
        "git_commit": evidence_index.get("git_commit"),
        "task_manifest_hash": evidence_index.get("task_manifest_hash"),
        "runtime_lock": evidence_index.get("runtime_lock"),
        "preflight": (
            {
                key: value
                for key, value in evidence_index.get("preflight", {}).items()
                if key != "artifact"
            }
            if isinstance(evidence_index.get("preflight"), dict)
            else evidence_index.get("preflight")
        ),
        "calibration": evidence_index.get("calibration"),
    }
    if "provider_timeout_s" in evidence_index:
        report_expected["provider_timeout_s"] = evidence_index.get(
            "provider_timeout_s", registered_provider_timeout_s
        )
    if evidence_index.get("c1_trial_persistence"):
        report_expected["trial_attempt"] = expected_trial_attempt
        if isinstance(entry.get("replacement_for"), dict):
            report_expected["run_id"] = entry.get("attempt_id")
    else:
        report_expected.pop("task_id", None)
    for field, value in report_expected.items():
        if report_context.get(field) != value:
            issues.append(
                f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: report {field} provenance mismatch"
            )
    return issues


def aggregate(
    run_index: dict[str, Any],
    *,
    manifest: dict[str, Any] | None = None,
    report_root: Path = Path.cwd(),
    rng_seed: int = 1234,
    iters: int = 10000,
    ci: float = 0.95,
) -> dict[str, Any]:
    arms = run_index.get("arms", {})
    c1_run_failures: list[str] = _c1_schedule_issues(run_index)
    c1_run_failures.extend(
        _c1_trial_recovery_issues(
            run_index,
            repository_root=report_root,
        )
    )
    c2_run_failures: list[str] = []
    visual_bridge_provenance = _visual_bridge_provenance(run_index, manifest)
    visual_judge_provenance = _visual_judge_provenance(run_index, manifest)
    expected_task_set, task_set_error = _expected_c1_task_set_provenance(
        run_index, manifest, report_root
    )
    if task_set_error:
        c1_run_failures.append(task_set_error)

    def reports_for(arm: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for entry in arms.get(arm, []) or []:
            label = f"{arm}/{entry.get('model')}/seed{entry.get('seed')}"
            local_settled_candidate = bool(
                arm in _C1_ARM_LABELS
                and isinstance(run_index.get("c1_trial_persistence"), dict)
                and entry.get("status") == "error"
                and isinstance(entry.get("replacement_for"), dict)
                and entry.get("source_provenance") is None
                and entry.get("imported") is not True
            )
            status_ok = (
                entry.get("status") == "ok"
                if arm == "c2_memory_baseline"
                else (
                    entry.get("status") == "ok" or local_settled_candidate
                    if arm in _C1_ARM_LABELS
                    and isinstance(run_index.get("c1_trial_persistence"), dict)
                    else entry.get("status") in {None, "ok"}
                )
            )
            if not status_ok:
                if arm in _C1_ARM_LABELS:
                    c1_run_failures.append(
                        f"{label}: "
                        f"run status {entry.get('status')}: {entry.get('error')}"
                    )
                elif arm == "c2_memory_baseline":
                    c2_run_failures.append(
                        f"{label}: run status {entry.get('status')}: "
                        f"{entry.get('error')}"
                    )
                continue
            report_path = entry.get("report")
            if not report_path:
                if arm in _C1_ARM_LABELS:
                    c1_run_failures.append(
                        f"{label}: missing report path"
                    )
                elif arm == "c2_memory_baseline":
                    c2_run_failures.append(f"{label}: missing report path")
                continue
            try:
                resolved = _resolve(report_root, report_path)
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                if arm in _C1_ARM_LABELS:
                    c1_run_failures.append(f"{label}: invalid report path: {error}")
                elif arm == "c2_memory_baseline":
                    c2_run_failures.append(f"{label}: invalid report path: {error}")
                continue
            if not resolved.is_file():
                if arm in _C1_ARM_LABELS:
                    c1_run_failures.append(
                        f"{label}: report file missing or not a regular file"
                    )
                elif arm == "c2_memory_baseline":
                    c2_run_failures.append(
                        f"{label}: report file missing or not a regular file"
                    )
                continue
            try:
                report_raw = resolved.read_bytes()
            except OSError as error:
                if arm in _C1_ARM_LABELS:
                    c1_run_failures.append(
                        f"{label}: report file unreadable: {error}"
                    )
                elif arm == "c2_memory_baseline":
                    c2_run_failures.append(
                        f"{label}: report file unreadable: {error}"
                    )
                continue
            actual_report_sha = hashlib.sha256(report_raw).hexdigest()
            if arm in _C1_ARM_LABELS and formal:
                expected_report_sha = entry.get("report_sha256")
                if not isinstance(expected_report_sha, str) or not expected_report_sha:
                    c1_run_failures.append(
                        f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                        "missing report_sha256"
                    )
                elif expected_report_sha != actual_report_sha:
                    c1_run_failures.append(
                        f"{arm}/{entry.get('model')}/seed{entry.get('seed')}: "
                        "report_sha256 mismatch"
                    )
            try:
                data = json.loads(report_raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                if arm == "c2_memory_baseline":
                    c2_run_failures.append(
                        f"{label}: unreadable report JSON: {error}"
                    )
                    continue
                if arm in _C1_ARM_LABELS:
                    c1_run_failures.append(
                        f"{label}: unreadable report JSON: {error}"
                    )
                    continue
                raise
            if not isinstance(data, dict):
                if arm in _C1_ARM_LABELS:
                    c1_run_failures.append(
                        f"{label}: malformed report JSON: expected an object"
                    )
                    continue
                if arm == "c2_memory_baseline":
                    c2_run_failures.append(
                        f"{label}: malformed report JSON: expected an object"
                    )
                    continue
            if arm == "c2_memory_baseline":
                report_issues = _c2_memory_report_issues(
                    entry,
                    report_raw,
                    data,
                    run_index,
                )
                if report_issues:
                    c2_run_failures.extend(
                        f"{label}: {issue}" for issue in report_issues
                    )
                    continue
            if arm in _C1_ARM_LABELS and run_index.get("harness_hash"):
                c1_run_failures.extend(
                    _c1_sidecar_issues(
                        resolved,
                        arm=arm,
                        entry=entry,
                        run_index=run_index,
                        report=data,
                        visual_bridge=visual_bridge_provenance,
                        visual_judge=visual_judge_provenance,
                        expected_task_set=expected_task_set,
                    )
                )
            # Carry the orchestrator-recorded model onto the payload so per-model
            # aggregation works even for arms that don't echo the model field.
            context = data.setdefault("run_context", {})
            if "model" in entry:
                context.setdefault("model", entry["model"])
            if "seed" in entry:
                context.setdefault("seed", entry["seed"])
            context.setdefault("arm", arm)
            if arm in _C1_ARM_LABELS:
                if local_settled_candidate:
                    current_attempt = entry.get("trial_attempt")
                    has_current_overhead = any(
                        overhead.get("arm") == arm
                        and overhead.get("model") == entry.get("model")
                        and overhead.get("seed") == entry.get("seed")
                        and overhead.get("task_id") == entry.get("task_id")
                        and overhead.get("trial_attempt") == current_attempt
                        and overhead.get("source_provenance") is None
                        for overhead in (run_index.get("c1_operational_overhead") or [])
                        if isinstance(overhead, dict)
                    )
                    result, terminal_issues = _local_settled_terminal_evidence(
                        arm=arm,
                        entry=entry,
                        report=data,
                        actual_report_sha=actual_report_sha,
                        has_current_overhead=has_current_overhead,
                    )
                    if terminal_issues:
                        c1_run_failures.append(
                            f"{label}: invalid local settled terminal evidence: "
                            + "; ".join(terminal_issues)
                        )
                        continue
                    assert isinstance(result, dict)
                    trial, _ = _normalize_c1_trial(arm, data, result)
                    trial["complete"] = True
                    trial["_p5_settled_evidence"] = True
                    out.append(
                        {
                            "_p5_preindexed_c1": {
                                "trials": [trial],
                                "issues": [],
                            }
                        }
                    )
                    continue
                source_provenance = entry.get("source_provenance")
                completion_class = (
                    source_provenance.get("completion_class")
                    if isinstance(source_provenance, dict)
                    else None
                )
                if isinstance(completion_class, str):
                    results = data.get("results")
                    result = (
                        results[0]
                        if isinstance(results, list)
                        and len(results) == 1
                        and isinstance(results[0], dict)
                        else None
                    )
                    kind = (
                        _preserved_model_failure_kind(result=result, report=data)
                        if isinstance(result, dict)
                        else None
                    )
                    if kind != completion_class:
                        c1_run_failures.append(
                            f"{label}: preserved model failure classification mismatch"
                        )
                        continue
                    trial, _ = _normalize_c1_trial(arm, data, result)
                    trial["complete"] = True
                    trial["_p5_settled_evidence"] = True
                    out.append(
                        {
                            "_p5_preindexed_c1": {
                                "trials": [trial],
                                "issues": [],
                            }
                        }
                    )
                    continue
                if entry.get("imported") is True and isinstance(
                    source_provenance, dict
                ):
                    results = data.get("results")
                    result = (
                        results[0]
                        if isinstance(results, list)
                        and len(results) == 1
                        and isinstance(results[0], dict)
                        else None
                    )
                    if (
                        data.get("complete") is not True
                        or cost.contains_unknown_usage(data)
                        or not isinstance(result, dict)
                        or str(result.get("task_id") or "")
                        != str(entry.get("task_id") or "")
                    ):
                        c1_run_failures.append(
                            f"{label}: imported settled report is not exact evidence"
                        )
                        continue
                    trial, _ = _normalize_c1_trial(arm, data, result)
                    # The source harness, SHA-bound sidecar, and recursive
                    # migration validated this immutable tuple when it settled.
                    # Later aggregate-only schema checks must not retroactively
                    # invalidate an older treatment-neutral result.
                    trial["complete"] = True
                    trial["_p5_settled_evidence"] = True
                    out.append(
                        {
                            "_p5_preindexed_c1": {
                                "trials": [trial],
                                "issues": [],
                            }
                        }
                    )
                    continue
                _, preindexed_trials, preindexed_issues = _index_c1_reports(
                    arm,
                    [data],
                    expected_inner_workers=(
                        int(run_index["c1_inner_worker"])
                        if formal
                        and isinstance(run_index.get("c1_inner_worker"), int)
                        and not isinstance(run_index.get("c1_inner_worker"), bool)
                        else None
                    ),
                    formal_report_schema=formal,
                )
                out.append(
                    {
                        "_p5_preindexed_c1": {
                            "trials": preindexed_trials,
                            "issues": preindexed_issues,
                        }
                    }
                )
            else:
                out.append(data)
        return out

    subset_ids = None
    registered_task_ids: list[str] | None = None
    if isinstance(run_index.get("task_ids"), list) and run_index["task_ids"]:
        subset_ids = [str(task_id) for task_id in run_index["task_ids"]]
    if manifest:
        tasks_path = _c1_tasks_path_from_manifest(manifest)
        if isinstance(tasks_path, str):
            resolved_tasks = _resolve(report_root, tasks_path)
            if resolved_tasks.exists():
                task_data = yaml.safe_load(resolved_tasks.read_text(encoding="utf-8"))
                if isinstance(task_data, dict) and isinstance(task_data.get("tasks"), list):
                    registered_task_ids = [
                        str(item.get("id")) for item in task_data["tasks"]
                        if isinstance(item, dict) and item.get("id")
                    ]
                    if subset_ids is None:
                        subset_ids = list(registered_task_ids)
        if subset_ids is None:
            subset = _c1_subset_from_manifest(manifest)
            if isinstance(subset, list):
                subset_ids = [str(item.get("id")) for item in subset if isinstance(item, dict) and item.get("id")]

    rng = random.Random(rng_seed)
    declared_task_count = int((manifest or {}).get("c1_task_count", 0) or 0)
    formal = bool(
        manifest
        and declared_task_count > 0
        and registered_task_ids is not None
        and len(registered_task_ids) == declared_task_count
        and len(set(registered_task_ids)) == declared_task_count
        and subset_ids == registered_task_ids
        and [str(model) for model in (run_index.get("models") or [])]
        == [str(model) for model in (manifest.get("models") or [])]
        and int(run_index.get("seeds") or 0) == int(manifest.get("seeds") or 0) == 3
        and set(_C1_ARM_LABELS).issubset(set(run_index.get("arms_run") or []))
        and set(run_index.get("arms_run") or []).issubset(
            set(_C1_ARM_LABELS) | _C1_DIAGNOSTIC_ARM_LABELS
        )
    )
    expected_provider_timeout_s: float | None = None
    if manifest is not None:
        try:
            expected_provider_timeout_s = manifest_provider_timeout_s(manifest)
        except ValueError:
            pass
    if formal and manifest is not None:
        manifest_trial_policy = manifest.get("c1_trial_persistence")
        if (
            not isinstance(manifest_trial_policy, dict)
            or manifest_trial_policy.get("schema_version") != 3
            or run_index.get("c1_trial_persistence") != manifest_trial_policy
        ):
            c1_run_failures.append(
                "formal C1 run index trial-persistence policy does not match the manifest"
            )
        manifest_usage_accounting = manifest.get("usage_accounting")
        if manifest_usage_accounting is not None and (
            not isinstance(manifest_usage_accounting, dict)
            or manifest_usage_accounting.get("schema") != "c1-usage-accounting-v2"
            or run_index.get("usage_accounting") != manifest_usage_accounting
        ):
            c1_run_failures.append(
                "formal C1 usage-accounting policy does not match the manifest"
            )
        manifest_start_gate = manifest.get("c1_start_gate_protocol")
        if (
            not isinstance(manifest_start_gate, dict)
            or manifest_start_gate.get("schema_version")
            != C1_START_GATE_PROTOCOL_SCHEMA_VERSION
            or run_index.get("c1_start_gate_protocol") != manifest_start_gate
        ):
            c1_run_failures.append(
                "formal C1 start-gate protocol does not match the manifest"
            )
        c1_run_failures.extend(
            _formal_live_provenance_issues(run_index, manifest, report_root)
        )
        c1_run_failures.extend(_formal_cache_policy_issues(run_index, report_root))
        c1_run_failures.extend(
            _formal_runtime_lock_policy_issues(run_index, manifest)
        )
        c1_run_failures.extend(_formal_gate_artifact_issues(run_index))
        expected_preflight_probes = [
            {
                "name": f"decision:{_clean_model(model)}",
                "model": _clean_model(model),
                "modality": "vision",
            }
            for model in (manifest.get("models") or [])
        ]
        expected_preflight_probes.extend(
            {
                "name": f"synthesis:{_clean_model(model)}",
                "model": _clean_model(model),
                "modality": "text",
            }
            for model in (manifest.get("models") or [])
        )
        expected_preflight_probes.append(
            {
                "name": "visual_observer",
                "model": _clean_model(visual_bridge_provenance.get("observer_model")),
                "modality": "vision",
            }
        )
        expected_preflight_probes.extend(
            {
                "name": probe.name,
                "model": _clean_model(probe.model),
                "modality": "vision",
            }
            for probe in grounder_preflight_probes(
                [str(model) for model in (manifest.get("models") or [])],
                observer=str(
                    visual_bridge_provenance.get("observer_model")
                    or "claude-sonnet-5@relay"
                ),
                configured=visual_bridge_provenance.get("grounder_model"),
            )
        )
        expected_preflight_probes.extend(
            [
                {
                    "name": "visual_judge",
                    "model": _clean_model(visual_judge_provenance.get("model")),
                    "modality": "vision",
                },
            ]
        )
        if not _valid_preflight_gate(
            run_index.get("preflight"),
            expected_git_commit=run_index.get("git_commit"),
            expected_manifest_hash=run_index.get("manifest_hash"),
            expected_harness_hash=run_index.get("harness_hash"),
            expected_api_provider=run_index.get("api_provider"),
            expected_profiles=run_index.get("model_request_profiles"),
            expected_profiles_sha256=run_index.get("model_request_profiles_sha256"),
            expected_usage_accounting=run_index.get("usage_accounting"),
            expected_probes=expected_preflight_probes,
            expected_provider_timeout_s=(
                expected_provider_timeout_s
                if expected_provider_timeout_s is not None
                else float("nan")
            ),
            expected_temperature=(
                float(manifest["api"]["temperature"])
                if isinstance(manifest.get("api"), dict)
                and isinstance(manifest["api"].get("temperature"), (int, float))
                and not isinstance(manifest["api"].get("temperature"), bool)
                else None
            ),
        ):
            c1_run_failures.append("formal C1 paid-model preflight gate missing or failed")
    expected_golden_provenance: dict[tuple[str, int], dict[str, str]] | None = None
    if formal:
        expected_golden_provenance, golden_provenance_issues = (
            _expected_c1_golden_provenance(run_index, manifest, report_root)
        )
        c1_run_failures.extend(golden_provenance_issues)
    expected_headless = run_index.get("headless")
    if formal and not isinstance(expected_headless, bool):
        c1_run_failures.append("formal C1 run index is missing boolean headless mode")
    if not isinstance(expected_headless, bool):
        expected_headless = None
    calibration_gate = run_index.get("calibration") if isinstance(run_index.get("calibration"), dict) else None
    if isinstance(calibration_gate, dict):
        expected_calibration = {
            "git_commit": run_index.get("git_commit"),
            "manifest_hash": run_index.get("manifest_hash", run_index.get("task_manifest_hash")),
            "harness_hash": run_index.get("harness_hash"),
        }
        if any(
            calibration_gate.get(field) != value
            for field, value in expected_calibration.items()
        ):
            calibration_gate = {**calibration_gate, "passed": False}
    c1 = c1_from_reports(
        reports_for("c1_state"),
        reports_for("c1_gui"),
        gui_synth_reports=reports_for("c1_gui_synth"),
        agent_s_reports=reports_for("c1_agent_s"),
        agent_s_synth_reports=reports_for("c1_agent_s_synth"),
        scalecua_reports=reports_for("c1_agent_scalecua"),
        scalecua_synth_reports=reports_for("c1_agent_scalecua_synth"),
        subset_ids=subset_ids,
        rng=rng,
        iters=iters,
        ci=ci,
        expected_models=[str(model) for model in (run_index.get("models") or [])],
        expected_seeds=int(run_index.get("seeds") or 0),
        expected_arms=list(_C1_ARM_LABELS),
        run_failures=c1_run_failures,
        calibration_gate=calibration_gate,
        formal=formal,
        expected_headless=expected_headless,
        expected_task_metadata=(
            expected_task_set.get("task_metadata")
            if isinstance(expected_task_set, dict)
            and isinstance(expected_task_set.get("task_metadata"), dict)
            else None
        ),
        expected_game_counts=(
            {
                str(game): int(count)
                for game, count in manifest.get(
                    "c1_benchmark_task_counts", {}
                ).items()
            }
            if isinstance(manifest, dict)
            and isinstance(manifest.get("c1_benchmark_task_counts"), dict)
            and manifest.get("c1_benchmark_task_counts")
            else None
        ),
        expected_golden_provenance=expected_golden_provenance,
        administrative_rmst_tau_ms=(
            manifest.get("stats", {}).get("c1_administrative_rmst_tau_ms")
            if isinstance(manifest, dict)
            and isinstance(manifest.get("stats"), dict)
            else None
        ),
        expected_api_provider=run_index.get("api_provider"),
        expected_observer_model=visual_bridge_provenance.get("observer_model"),
        expected_grounder_model=visual_bridge_provenance.get("grounder_model"),
        expected_judge_model=visual_judge_provenance.get("model"),
        expected_provider_timeout_s=expected_provider_timeout_s,
        expected_retry_max_attempts=(
            int(manifest.get("retry", {}).get("max_attempts"))
            if isinstance(manifest, dict)
            and isinstance(manifest.get("retry"), dict)
            and isinstance(manifest["retry"].get("max_attempts"), int)
            and not isinstance(manifest["retry"].get("max_attempts"), bool)
            else None
        ),
        require_trial_start_gate_schema=bool(
            run_index.get("c1_trial_persistence")
        ),
        expected_inner_workers=(
            int(run_index["c1_inner_worker"])
            if formal
            and isinstance(run_index.get("c1_inner_worker"), int)
            and not isinstance(run_index.get("c1_inner_worker"), bool)
            else None
        ),
    )
    cost_verified = bool((manifest or {}).get("cost", {}).get("verified", False))
    c1["usd_enabled"] = cost_verified
    if not cost_verified:
        for point in c1.get("points", []):
            point["mean_cost_usd"] = {
                "disabled": True,
                "reason": "manifest cost table is not verified",
            }
    minimal_agent_diagnostic = c1_minimal_agent_diagnostic_from_reports(
        reports_for("c1_state"),
        reports_for("c1_state_minimal_agent"),
    )
    c2_memory_entries = arms.get("c2_memory_baseline", []) or []
    c2_memory_requested = bool(
        "c2_memory_baseline" in arms
        or "c2_memory_baseline" in (run_index.get("arms_run") or [])
    )
    expected_c2_memory_reports: int | None = None
    if c2_memory_requested:
        model_count = len(run_index.get("models") or [])
        seed_count = _integer(run_index.get("seeds"))
        if model_count and seed_count is not None and seed_count > 0:
            expected_c2_memory_reports = model_count * seed_count
        else:
            expected_c2_memory_reports = len(c2_memory_entries)
        if expected_c2_memory_reports <= 0:
            c2_run_failures.append(
                "C2 memory baseline was requested but no expected reports were registered"
            )
    c2 = c2_from_reports(
        reports_for("c2_phase3"),
        reports_for("c2_memory_baseline"),
        rng=rng,
        iters=iters,
        ci=ci,
        run_failures=c2_run_failures,
        expected_memory_reports=expected_c2_memory_reports,
    )
    aggregate_complete = bool(
        c1.get("complete", False)
        and (not c2_memory_requested or c2.get("complete") is True)
    )
    c3 = c3_from_reports(
        reports_for("c3_fuzz"),
        rng=rng,
        iters=iters,
        ci=ci,
        run_entries=arms.get("c3_fuzz", []) or [],
    )

    return {
        "schema_version": 2,
        "complete": aggregate_complete,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": "p5_scale_summary",
        "provenance": {
            "experiment_id": run_index.get("experiment_id"),
            "run_id": run_index.get("run_id"),
            "git_commit": run_index.get("git_commit"),
            "task_manifest_hash": run_index.get("task_manifest_hash"),
            "manifest_hash": run_index.get("manifest_hash"),
            "harness_hash": run_index.get("harness_hash"),
            "c2_harness_hash": run_index.get("c2_harness_hash"),
            "c2_manifest_fingerprint": run_index.get("c2_manifest_fingerprint"),
            "api_provider": run_index.get("api_provider"),
            "runtime_lock": run_index.get("runtime_lock"),
            "c1_background_pixel_protocol": run_index.get(
                "c1_background_pixel_protocol"
            ),
            "c1_background_pixel_protocol_sha256": run_index.get(
                "c1_background_pixel_protocol_sha256"
            ),
            "visual_bridge": visual_bridge_provenance,
            "visual_judge": visual_judge_provenance,
            "c1_task_set": expected_task_set,
            "cost_accounting": {
                "primary_c1_arm_cost": "counterfactual_attributed",
                "experiment_provider_spend": "actual_incurred_once",
                "reasoning_tokens_included_in_total": True,
            },
            "calibration": run_index.get("calibration"),
            "c1_cache_policy": run_index.get("c1_cache_policy"),
            "task_ids": run_index.get("task_ids"),
            "models": run_index.get("models"),
            "seeds": run_index.get("seeds"),
            "headless": run_index.get("headless"),
            "formal_c1": formal,
            "formal_c2_memory": c2_memory_requested,
            "stats": {"bootstrap_iterations": iters, "ci": ci, "rng_seed": rng_seed},
        },
        "c1_cost_accuracy": c1,
        "c1_state_minimal_agent_diagnostic": minimal_agent_diagnostic,
        "c2_transfer": c2,
        "c3_robustness": c3,
    }


def _fmt_ci(stat: dict[str, Any]) -> str:
    mean = stat.get("mean", stat.get("mean_diff"))
    lo, hi, n = stat.get("lo"), stat.get("hi"), stat.get("n")
    if mean is None or (isinstance(mean, float) and mean != mean):
        return "n/a"
    if lo is None or (isinstance(lo, float) and lo != lo):
        return f"{mean:.4g} (n={n})"
    return f"{mean:.4g} [{lo:.4g}, {hi:.4g}] (n={n})"


def _fmt_p(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if math.isnan(number):
        return "n/a"
    return f"{number:.3g}"


def _fmt_effect_test(effect: dict[str, Any], test: dict[str, Any]) -> str:
    return (
        f"{_fmt_ci(effect)}; p={_fmt_p(test.get('p_value'))}, "
        f"Holm={_fmt_p(test.get('holm_p_value'))}"
    )


def render_markdown(summary: dict[str, Any]) -> str:
    prov = summary.get("provenance", {})
    c1 = summary.get("c1_cost_accuracy", {})
    c2 = summary.get("c2_transfer", {})
    c3 = summary.get("c3_robustness", {})
    lines = [
        "# P5 Full-Scale Summary",
        "",
        f"- Generated: `{summary.get('generated_at_utc')}`",
        f"- Run: `{prov.get('experiment_id')}` / `{prov.get('run_id')}`",
        f"- Git: `{prov.get('git_commit')}` | manifest hash: `{prov.get('task_manifest_hash')}`",
        f"- Models: `{prov.get('models')}` | seeds: `{prov.get('seeds')}`",
        f"- Visual bridge: `{prov.get('visual_bridge')}` | visual judge: "
        f"`{prov.get('visual_judge')}`",
        f"- Stats: bootstrap `{prov.get('stats', {}).get('bootstrap_iterations')}` iters, "
        f"CI `{prov.get('stats', {}).get('ci')}`, rng `{prov.get('stats', {}).get('rng_seed')}`",
        "",
        "## C1 Cost-Accuracy (Pareto)",
        f"- Status: `{'complete' if c1.get('complete') else 'INCOMPLETE'}` | coverage `{c1.get('coverage')}`",
        f"- Aligned on: `{c1.get('align_on')}` over `{len(c1.get('align_task_ids', []))}` task ids",
        "",
    ]
    usd_enabled = c1.get("usd_enabled") is True
    lines += [
        "| Method | Model | Tasks | Start | Goal given start | E2E | "
        "Time to start ms | Start to goal ms | E2E excl synth ms | "
        "E2E incl synth ms | RMST E2E excl synth ms |",
        "|---|---|---:|---|---|---|---|---|---|---|---|",
    ]
    for point in c1.get("points", []):
        cells = [
            str(point.get("method")),
            str(point.get("model")),
            str(point.get("tasks")),
            _fmt_ci(point.get("start_success", {})),
            _fmt_ci(point.get("goal_success_given_start", {})),
            _fmt_ci(point.get("e2e_success", {})),
            _fmt_ci(point.get("mean_time_to_start_ms", {})),
            _fmt_ci(point.get("mean_time_start_to_goal_ms", {})),
            _fmt_ci(point.get("mean_time_e2e_excluding_synthesis_ms", {})),
            _fmt_ci(point.get("mean_time_e2e_including_synthesis_ms", {})),
            _fmt_ci(point.get("rmst_e2e_ms", {})),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "Time means above are descriptive all-trial fields; the task-horizon "
        "pre-registered administrative restricted mean is the censor-aware "
        "primary time endpoint.",
        "",
    ]
    token_header = (
        "| Method | Model | Mean total tokens | Mean agent tokens | "
        "Mean evaluation tokens | Mean reasoning tokens | Mean cached prompt tokens |"
    )
    token_separator = "|---|---|---|---|---|---|---|"
    if usd_enabled:
        token_header = token_header[:-1] + " Mean USD |"
        token_separator = token_separator[:-1] + "---|"
    lines += [token_header, token_separator]
    for point in c1.get("points", []):
        cells = [
            str(point.get("method")),
            str(point.get("model")),
            _fmt_ci(point.get("mean_tokens", {})),
            _fmt_ci(point.get("mean_agent_tokens", {})),
            _fmt_ci(point.get("mean_evaluation_tokens", {})),
            _fmt_ci(point.get("mean_reasoning_tokens", {})),
            _fmt_ci(point.get("mean_cached_prompt_tokens", {})),
        ]
        if usd_enabled:
            cells.append(_fmt_ci(point.get("mean_cost_usd", {})))
        lines.append("| " + " | ".join(cells) + " |")
    cost_accounting = c1.get("cost_accounting", {})
    if cost_accounting.get("by_arm_model"):
        actual = cost_accounting.get("actual_provider_incurred", {})
        transient = cost_accounting.get("transient_infrastructure_overhead", {})
        attributed = cost_accounting.get("counterfactual_attributed_total", {})
        all_attempts = cost_accounting.get("all_provider_attempts", {})
        retry_transient = cost_accounting.get(
            "retry_transient_infrastructure_overhead", {}
        )
        primary_burden = cost_accounting.get("primary_treatment_burden", {})
        lines += [
            "",
            "### Provider-attempt and synthesis cost accounting diagnostic",
            "",
            "Primary method columns exclude usage-complete infrastructure retry "
            "attempts. Actual provider spend retains every attempt and reports "
            "retry overhead separately with the same module/model partitions.",
            "",
            f"- All provider attempts: `{all_attempts.get('llm_calls', 0)}` calls, "
            f"`{all_attempts.get('total_tokens', 0)}` tokens.",
            f"- Retry transient infrastructure: "
            f"`{retry_transient.get('llm_calls', 0)}` calls, "
            f"`{retry_transient.get('total_tokens', 0)}` tokens.",
            f"- Primary treatment burden: `{primary_burden.get('llm_calls', 0)}` calls, "
            f"`{primary_burden.get('total_tokens', 0)}` tokens.",
            "",
            "Primary arm token columns use counterfactual-attributed `cost` for "
            "fair B/D comparison. Actual provider-incurred synthesis includes every "
            "paid attempt; transient infrastructure overhead is shown separately "
            "and is not assigned to an experimental arm burden.",
            "",
            f"- Actual provider-incurred synthesis: `{actual.get('llm_calls', 0)}` calls, "
            f"`{actual.get('total_tokens', 0)}` tokens.",
            f"- Transient infrastructure overhead: "
            f"`{transient.get('llm_calls', 0)}` calls, "
            f"`{transient.get('total_tokens', 0)}` tokens.",
            f"- Counterfactual attributed across synthesized arms: "
            f"`{attributed.get('llm_calls', 0)}` calls, "
            f"`{attributed.get('total_tokens', 0)}` tokens (comparison burden, not spend).",
            "",
            "| Arm | Model | Actual calls | Actual tokens | Transient calls | Transient tokens | Attributed calls | Attributed tokens |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for row in cost_accounting["by_arm_model"]:
            row_actual = row.get("actual_provider_incurred", {})
            row_transient = row.get("transient_infrastructure_overhead", {})
            row_attributed = row.get("counterfactual_attributed", {})
            lines.append(
                f"| {row.get('method')} | {row.get('model')} | "
                f"{row_actual.get('llm_calls', 0)} | "
                f"{row_actual.get('total_tokens', 0)} | "
                f"{row_transient.get('llm_calls', 0)} | "
                f"{row_transient.get('total_tokens', 0)} | "
                f"{row_attributed.get('llm_calls', 0)} | "
                f"{row_attributed.get('total_tokens', 0)} |"
            )
    same_start = c1.get("same_start", {})
    if same_start.get("per_arm_model"):
        lines += ["", "### Same-start GUI arm summaries", ""]
        lines += [
            "| Arm | Model | Trials | Start | Goal given start | E2E | "
            "Time to start ms | Start to goal ms | E2E excl synth ms | "
            "E2E incl synth ms | RMST ms | Total tokens | Agent tokens | "
            "Eval tokens | Reasoning | Cached prompt |",
            "|---|---|---:|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for row in same_start["per_arm_model"]:
            lines.append(
                f"| {row.get('method')} | {row.get('model')} | {row.get('trials')} | "
                f"{_fmt_ci(row.get('start_success', {}))} | "
                f"{_fmt_ci(row.get('goal_success_given_start', {}))} | "
                f"{_fmt_ci(row.get('e2e_success', {}))} | "
                f"{_fmt_ci(row.get('mean_time_to_start_ms', {}))} | "
                f"{_fmt_ci(row.get('mean_time_start_to_goal_ms', {}))} | "
                f"{_fmt_ci(row.get('mean_time_e2e_excluding_synthesis_ms', {}))} | "
                f"{_fmt_ci(row.get('mean_time_e2e_including_synthesis_ms', {}))} | "
                f"{_fmt_ci(row.get('rmst_e2e_ms', {}))} | "
                f"{_fmt_ci(row.get('mean_tokens', {}))} | "
                f"{_fmt_ci(row.get('mean_agent_tokens', {}))} | "
                f"{_fmt_ci(row.get('mean_evaluation_tokens', {}))} | "
                f"{_fmt_ci(row.get('mean_reasoning_tokens', {}))} | "
                f"{_fmt_ci(row.get('mean_cached_prompt_tokens', {}))} |"
            )
        if same_start.get("comparisons"):
            comparison_groups = (
                (
                    "### Primary checkpoint comparisons",
                    {"A_vs_B", "C_vs_D"},
                ),
                (
                    "### Secondary GUI-framework comparisons",
                    {"A_vs_C", "B_vs_D"},
                ),
            )
            for title, comparison_ids in comparison_groups:
                selected = [
                    row
                    for row in same_start["comparisons"]
                    if row.get("comparison") in comparison_ids
                ]
                if not selected:
                    continue
                lines += ["", title, ""]
                lines += [
                    "| Comparison | Model | Trials | Start effect B-A; McNemar | "
                    "Goal given own start effect B-A (main) | "
                    "Goal given both start effect B-A; McNemar (sensitivity) | "
                    "E2E effect B-A; McNemar | RMST excl synth B-A ms | "
                    "RMST incl synth B-A ms | Token delta total | Agent | Eval | "
                    "Reasoning | Cached prompt |",
                    "|---|---|---:|---|---|---|---|---|---|---|---|---|---|---|",
                ]
                for row in selected:
                    token_effects = row.get("token_effects_b_minus_a", {})
                    lines.append(
                        f"| {row.get('comparison')} | {row.get('model')} | "
                        f"{row.get('shared_trials')} | "
                        f"{_fmt_effect_test(row.get('start_effect_b_minus_a', {}), row.get('start_mcnemar', {}))} | "
                        f"{_fmt_ci(row.get('goal_given_start_effect_b_minus_a', {}))} | "
                        f"{_fmt_effect_test(row.get('goal_given_both_start_effect_b_minus_a', {}), row.get('goal_given_both_start_mcnemar', {}))} | "
                        f"{_fmt_effect_test(row.get('e2e_effect_b_minus_a', {}), row.get('e2e_mcnemar', {}))} | "
                        f"{_fmt_ci(row.get('time_effect_b_minus_a_ms', {}))} | "
                        f"{_fmt_ci(row.get('time_including_synthesis_effect_b_minus_a_ms', {}))} | "
                        f"{_fmt_ci(token_effects.get('total', {}))} | "
                        f"{_fmt_ci(token_effects.get('agent', {}))} | "
                        f"{_fmt_ci(token_effects.get('evaluation', {}))} | "
                        f"{_fmt_ci(token_effects.get('reasoning', {}))} | "
                        f"{_fmt_ci(token_effects.get('cached_prompt', {}))} |"
                    )
    if c1.get("incomplete_reasons"):
        lines += ["", f"- C1 incomplete reasons: `{c1.get('incomplete_reasons')}`"]
    lines += [
        "",
        "## C2 Transfer (held-out strict)",
        f"- Status: `{'complete' if c2.get('complete') else 'INCOMPLETE'}` | "
        f"memory coverage `{c2.get('memory_baseline_coverage')}`",
        f"- vs A5: A5 {_fmt_ci(c2.get('phase3_aggregate', {}).get('A5_heldout_strict', {}))}, "
        f"A9 {_fmt_ci(c2.get('phase3_aggregate', {}).get('A9_heldout_strict', {}))}, "
        f"gain {_fmt_ci(c2.get('phase3_aggregate', {}).get('transfer_gain_vs_A5', {}))} "
        f"(perm p=`{c2.get('phase3_aggregate', {}).get('transfer_gain_permutation', {}).get('p_value')}`)",
        f"- anchor held-out strict: {_fmt_ci(c2.get('memory_baseline_aggregate', {}).get('anchor_heldout_strict', {}))}",
    ]
    for backend, stat in (c2.get("memory_baseline_aggregate", {}).get("anchor_minus_baseline", {}) or {}).items():
        lines.append(f"- anchor - `{backend}`: {_fmt_ci(stat)}")
    if c2.get("incomplete_reasons"):
        lines.append(f"- C2 incomplete reasons: `{c2.get('incomplete_reasons')}`")
    lines += [
        "",
        "## C3 Robustness (unique defects)",
        f"- Status: `{'complete' if c3.get('complete') else 'INCOMPLETE'}` | coverage `{c3.get('coverage')}`",
        f"- Unique defects: `{c3.get('unique_defects_total')}` (raw `{c3.get('raw_defects_total')}`)",
        f"- Unique by reachability: `{c3.get('unique_by_reachability')}` (raw `{c3.get('raw_by_reachability')}`)",
        f"- Defect paths: `{c3.get('defect_paths')}`",
        f"- Defect types: `{c3.get('defect_types')}`",
        f"- Unique by benchmark: `{c3.get('unique_by_benchmark')}`",
    ]
    if c3.get("task_failures") or c3.get("run_failures"):
        lines.append(f"- Failures: tasks `{c3.get('task_failures')}`, runs `{c3.get('run_failures')}`")
    return "\n".join(lines) + "\n"
