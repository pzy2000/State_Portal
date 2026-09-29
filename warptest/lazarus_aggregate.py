"""Paired, fail-closed aggregation for the C1-Lazarus matrix."""

from __future__ import annotations

import itertools
import math
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from . import p5_stats
from .lazarus_catalog import LAZARUS_ARMS, LAZARUS_VERSIONS
from .lazarus_roster import FORMAL_LOCUS_MIX, FORMAL_TASK_COUNT


def lazarus_from_reports(
    reports: list[dict[str, Any]],
    *,
    expected_task_ids: Sequence[str],
    expected_seeds: Sequence[int],
    expected_model: str,
    expected_provenance: Mapping[tuple[str, str], str],
    formal: bool,
    rng: random.Random,
    iters: int,
    ci: float,
) -> dict[str, Any]:
    """Aggregate four cells per ``(task_id, seed)`` and test H1--H3."""

    tasks = tuple(str(value) for value in expected_task_ids)
    seeds = tuple(expected_seeds)
    issues: list[str] = []
    if not tasks or len(tasks) != len(set(tasks)):
        issues.append("expected task ids must be non-empty and unique")
    if not seeds or len(seeds) != len(set(seeds)):
        issues.append("expected seeds must be non-empty and unique")
    if formal:
        if len(tasks) != FORMAL_TASK_COUNT:
            issues.append(
                f"formal Lazarus aggregation requires {FORMAL_TASK_COUNT} tasks"
            )
        if seeds != (0, 1, 2):
            issues.append("formal Lazarus aggregation requires seeds 0, 1, 2")

    expected_keys = {
        (task_id, seed, arm, version)
        for task_id in tasks
        for seed in seeds
        for arm in LAZARUS_ARMS
        for version in LAZARUS_VERSIONS
    }
    index: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for position, report in enumerate(reports):
        if report.get("excluded_from_formal") is True:
            continue
        task_id = str(report.get("task_id") or "")
        model = str(report.get("model") or "")
        arm = str(report.get("arm") or "")
        version = str(report.get("version") or "")
        seed = report.get("seed")
        label = f"report[{position}]"
        if model != expected_model:
            issues.append(f"{label}: model does not match registered decision model")
        if task_id not in tasks or arm not in LAZARUS_ARMS or version not in LAZARUS_VERSIONS:
            issues.append(f"{label}: trial identity is outside the registered matrix")
            continue
        if not isinstance(seed, int) or isinstance(seed, bool) or seed not in seeds:
            issues.append(f"{label}: seed is outside the registered matrix")
            continue
        key = (task_id, seed, arm, version)
        if key in index:
            issues.append(f"duplicate cell {key!r}")
            continue
        index[key] = report
        _validate_report(
            report,
            key=key,
            expected_provenance=expected_provenance,
            issues=issues,
        )

    for key in sorted(expected_keys - set(index), key=str):
        issues.append(f"missing cell {key!r}")
    for key in sorted(set(index) - expected_keys, key=str):
        issues.append(f"unexpected cell {key!r}")

    loci: dict[str, str] = {}
    for task_id in tasks:
        values = {
            str(report.get("defect_locus"))
            for key, report in index.items()
            if key[0] == task_id and report.get("defect_locus") in {"at_goal", "on_path"}
        }
        if len(values) != 1:
            issues.append(f"{task_id}: missing or conflicting defect_locus")
        else:
            loci[task_id] = next(iter(values))
    if formal:
        locus_counts = {
            locus: sum(value == locus for value in loci.values())
            for locus in ("at_goal", "on_path")
        }
        if locus_counts != dict(FORMAL_LOCUS_MIX):
            issues.append(
                "formal Lazarus defect_locus quota must be "
                f"at_goal={FORMAL_LOCUS_MIX['at_goal']}/"
                f"on_path={FORMAL_LOCUS_MIX['on_path']}"
            )

    pair_keys = [(task_id, seed) for task_id in tasks for seed in seeds]
    discrimination: dict[str, dict[tuple[str, int], bool]] = {
        arm: {} for arm in LAZARUS_ARMS
    }
    buggy_start: dict[str, dict[tuple[str, int], bool]] = {
        arm: {} for arm in LAZARUS_ARMS
    }
    fixed_start: dict[str, dict[tuple[str, int], bool]] = {
        arm: {} for arm in LAZARUS_ARMS
    }
    for pair_key in pair_keys:
        task_id, seed = pair_key
        for arm in LAZARUS_ARMS:
            buggy = index.get((task_id, seed, arm, "buggy"))
            fixed = index.get((task_id, seed, arm, "fixed"))
            if buggy is None or fixed is None:
                continue
            if not all(
                type(row.get(field)) is bool
                for row in (buggy, fixed)
                for field in ("start_success", "goal_success", "defect_exposed")
            ):
                issues.append(f"{task_id}/seed{seed}/{arm}: binary verdict is missing")
                continue
            discrimination[arm][pair_key] = bool(
                buggy["defect_exposed"] and fixed["goal_success"]
            )
            buggy_start[arm][pair_key] = bool(buggy["start_success"])
            fixed_start[arm][pair_key] = bool(fixed["start_success"])

    h1 = p5_stats.paired_binary_from_maps(
        discrimination["lazarus_state"], discrimination["lazarus_agent_s"]
    )
    on_path_keys = {key for key in pair_keys if loci.get(key[0]) == "on_path"}
    h2_state = {
        key: value
        for key, value in buggy_start["lazarus_state"].items()
        if key in on_path_keys
    }
    h2_agent = {
        key: value
        for key, value in buggy_start["lazarus_agent_s"].items()
        if key in on_path_keys
    }
    h2 = p5_stats.paired_binary_from_maps(h2_state, h2_agent)
    h2_agent_fixed = {
        key: value
        for key, value in fixed_start["lazarus_agent_s"].items()
        if key in on_path_keys
    }
    h2_version_gap = p5_stats.paired_binary_from_maps(h2_agent_fixed, h2_agent)
    # H2 requires both a method gap on buggy software and the corresponding
    # Agent-S fixed-versus-buggy reachability gap.  The maximum component p is
    # the conservative intersection-union p value registered for Holm.
    h2_intersection_union_p_value = max(
        h2["p_value"], h2_version_gap["p_value"]
    )

    fixed_tokens: dict[str, dict[tuple[str, int], float]] = {
        arm: {} for arm in LAZARUS_ARMS
    }
    fixed_times: dict[str, dict[tuple[str, int], float]] = {
        arm: {} for arm in LAZARUS_ARMS
    }
    reuse_successes = 0
    reuse_n = 0
    state_zero_calls = True
    for task_id, seed in pair_keys:
        key = (task_id, seed)
        for arm in LAZARUS_ARMS:
            report = index.get((task_id, seed, arm, "fixed"))
            if report is None:
                continue
            token_value = _number(report.get("treatment_tokens"))
            time_value = _number(report.get("treatment_time_ms"))
            if token_value is None or time_value is None:
                issues.append(f"{task_id}/seed{seed}/{arm}/fixed: marginal cost missing")
            else:
                fixed_tokens[arm][key] = token_value
                fixed_times[arm][key] = time_value
            if arm == "lazarus_state":
                reuse_n += 1
                reuse = (
                    report.get("checkpoint_reuse_success") is True
                    and report.get("synthesis_cache_hit") is True
                    and report.get("actual_synthesis_calls") == 0
                )
                reuse_successes += int(reuse)
                state_zero_calls = state_zero_calls and report.get(
                    "actual_synthesis_calls"
                ) == 0
                if not reuse:
                    issues.append(
                        f"{task_id}/seed{seed}/lazarus_state/fixed: "
                        "checkpoint reuse or zero-synthesis invariant failed"
                    )

    token_effect = p5_stats.clustered_paired_bootstrap_diff(
        fixed_tokens["lazarus_state"],
        fixed_tokens["lazarus_agent_s"],
        iterations=iters,
        ci=ci,
        rng=rng,
    )
    time_effect = p5_stats.clustered_paired_bootstrap_diff(
        fixed_times["lazarus_state"],
        fixed_times["lazarus_agent_s"],
        iterations=iters,
        ci=ci,
        rng=rng,
    )
    h3 = _clustered_paired_permutation(
        fixed_tokens["lazarus_state"],
        fixed_tokens["lazarus_agent_s"],
        iterations=iters,
        rng=rng,
    )
    raw_p = {
        "H1": h1["p_value"],
        "H2": h2_intersection_union_p_value,
        "H3": h3["p_value"],
    }

    h2_rates: dict[str, dict[str, float]] = {}
    h2_gaps: dict[str, float] = {}
    for arm in LAZARUS_ARMS:
        buggy_values = [
            int(value)
            for key, value in buggy_start[arm].items()
            if key in on_path_keys
        ]
        fixed_values = [
            int(value)
            for key, value in fixed_start[arm].items()
            if key in on_path_keys
        ]
        buggy_rate = _mean(buggy_values)
        fixed_rate = _mean(fixed_values)
        h2_rates[arm] = {"buggy": buggy_rate, "fixed": fixed_rate}
        h2_gaps[arm] = fixed_rate - buggy_rate

    result = {
        "schema_version": 1,
        "complete": not issues,
        "formal": formal,
        "model": expected_model,
        "expected_cells": len(expected_keys),
        "observed_cells": len(index),
        "issues": list(dict.fromkeys(issues)),
        "metrics": {
            "defect_discrimination": {
                arm: _binary_summary(values)
                for arm, values in discrimination.items()
            },
            "false_alarms": sum(
                report.get("defect_exposed") is True
                for key, report in index.items()
                if key[3] == "fixed"
            ),
            "h2_start_success": h2_rates,
            "h2_start_success_gap": h2_gaps,
            "h3_second_version": {
                "checkpoint_reuse_success": {
                    "successes": reuse_successes,
                    "n": reuse_n,
                },
                "state_zero_actual_synthesis_calls": bool(reuse_n and state_zero_calls),
                "tokens_state_minus_agent_s": token_effect,
                "time_state_minus_agent_s_ms": time_effect,
            },
        },
        "statistics": {
            "h1_discrimination_mcnemar": h1,
            "h2_on_path_buggy_start_mcnemar": h2,
            "h2_on_path_agent_s_fixed_vs_buggy_start_mcnemar": h2_version_gap,
            "h2_on_path_intersection_union": {
                "method": "max_component_exact_mcnemar_p_value",
                "components": {
                    "buggy_arm_difference": h2["p_value"],
                    "agent_s_fixed_vs_buggy_gap": h2_version_gap["p_value"],
                },
                "p_value": h2_intersection_union_p_value,
            },
            "h2_intersection_union_p_value": h2_intersection_union_p_value,
            "h3_second_version_token_permutation": h3,
            "raw_p_values": raw_p,
            "holm_adjusted_p_values": p5_stats.holm_adjust(raw_p),
        },
    }
    return result


def _validate_report(
    report: Mapping[str, Any],
    *,
    key: tuple[str, int, str, str],
    expected_provenance: Mapping[tuple[str, str], str],
    issues: list[str],
) -> None:
    task_id, seed, arm, version = key
    label = f"{task_id}/seed{seed}/{arm}/{version}"
    if report.get("complete") is not True:
        issues.append(f"{label}: trial is incomplete")
    if report.get("provenance_valid") is not True:
        issues.append(f"{label}: provenance gate is not valid")
    expected_sha = expected_provenance.get((task_id, version))
    if expected_sha is None or report.get("provenance_sha256") != expected_sha:
        issues.append(f"{label}: provenance SHA does not match registered version")
    if report.get("per_trial_p2p_ok") is not True and not _has_start_failed_p2p_status(report):
        issues.append(f"{label}: per-trial P2P is missing or failed")
    if version == "fixed" and (
        report.get("defect_exposed") is True or report.get("false_alarm") is True
    ):
        issues.append(f"{label}: fixed-version false alarm")


def _has_start_failed_p2p_status(report: Mapping[str, Any]) -> bool:
    """Accept only the explicit H2 Start-failure P2P exception."""

    if report.get("start_success") is not False or report.get("per_trial_p2p_ok") is not False:
        return False
    evidence = report.get("per_trial_p2p_evidence")
    if not isinstance(evidence, Mapping):
        return False
    if (
        evidence.get("schema_version") != 1
        or evidence.get("completed") is not False
        or evidence.get("passed") is not False
        or evidence.get("isolation") != "not_executed_start_failed"
        or evidence.get("results") != []
        or evidence.get("failures") != ["not_executed_start_failed"]
    ):
        return False
    grading = report.get("grading")
    result = grading.get("result") if isinstance(grading, Mapping) else None
    return isinstance(result, Mapping) and result.get("after_statuses") == {
        "goal": "not_executed_start_failed",
        "defect": "not_executed_start_failed",
    }


def _binary_summary(values: Mapping[tuple[str, int], bool]) -> dict[str, Any]:
    n = len(values)
    successes = sum(bool(value) for value in values.values())
    return {"successes": successes, "n": n, "rate": successes / n if n else float("nan")}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _mean(values: Sequence[int]) -> float:
    return sum(values) / len(values) if values else float("nan")


def _clustered_paired_permutation(
    a: Mapping[tuple[str, int], float],
    b: Mapping[tuple[str, int], float],
    *,
    iterations: int,
    rng: random.Random,
) -> dict[str, Any]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for key in set(a) & set(b):
        grouped[str(key[0])].append(float(a[key]) - float(b[key]))
    clusters = sorted(grouped)
    observed_values = [value for cluster in clusters for value in grouped[cluster]]
    if not observed_values:
        return {
            "p_value": float("nan"),
            "observed_mean_diff": float("nan"),
            "n": 0,
            "clusters": 0,
            "method": "task_cluster_sign_flip",
        }
    observed = sum(observed_values) / len(observed_values)
    threshold = abs(observed) - 1e-12

    def statistic(signs: Sequence[int]) -> float:
        values = [
            sign * value
            for sign, cluster in zip(signs, clusters)
            for value in grouped[cluster]
        ]
        return sum(values) / len(values)

    if len(clusters) <= 16:
        samples = itertools.product((-1, 1), repeat=len(clusters))
        total = 0
        extreme = 0
        for signs in samples:
            total += 1
            if abs(statistic(signs)) >= threshold:
                extreme += 1
        p_value = extreme / total
        method = "exact_task_cluster_sign_flip"
    else:
        total = max(1, int(iterations))
        extreme = 0
        for _ in range(total):
            signs = [1 if rng.random() < 0.5 else -1 for _ in clusters]
            if abs(statistic(signs)) >= threshold:
                extreme += 1
        p_value = (extreme + 1) / (total + 1)
        method = "monte_carlo_task_cluster_sign_flip"
    return {
        "p_value": p_value,
        "observed_mean_diff": observed,
        "n": len(observed_values),
        "clusters": len(clusters),
        "method": method,
    }
