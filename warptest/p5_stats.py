"""Lightweight statistics for the P5 figures and summary.

Pure-Python (stdlib ``random`` + ``math``) so it adds no scientific dependency
and is fully deterministic given an explicit RNG seed. Provides exactly what the
P5 claims need:

  * ``mean_ci``                -- mean with a percentile bootstrap CI (error bars).
  * ``paired_bootstrap_diff``  -- mean paired difference with a bootstrap CI
                                  (state-managed vs GUI on shared task ids; anchor
                                  vs baseline transfer gain).
  * ``paired_permutation_test``-- two-sided sign-flip permutation p-value for a
                                  paired difference (continuous, e.g. cost/pass).
  * ``mcnemar_exact``          -- exact two-sided McNemar p-value for paired binary
                                  outcomes (per-task pass/fail under two methods).
  * ``clustered_paired_bootstrap_diff`` -- paired effects resampled by task while
                                  retaining all game seeds in each cluster.
  * ``clustered_mean_ci``       -- per-arm means with the same task-cluster unit.
  * ``clustered_conditional_mean_ci`` -- conditional rates without flattening seeds.
  * ``clustered_conditional_rate_diff`` -- difference of two arm-specific
                                  conditional rates with distinct denominators.
  * ``clustered_rmst_ci``       -- task-cluster CI around a censor-aware RMST.
  * ``clustered_paired_rmst_diff`` -- paired task-cluster CI for a common-tau
                                  RMST difference between two methods.
  * ``holm_adjust``            -- family-wise p-value correction across models.
  * ``restricted_mean_survival_time`` -- right-censor-aware completion time.

The bootstrap and permutation routines never raise on small/empty inputs; they
return NaN / p=1.0 sentinels so the aggregation can keep going and label the
result as undersampled rather than crashing a long run.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Any, Hashable, Mapping, Sequence


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_vals[int(pos)])
    frac = pos - lo
    return float(sorted_vals[lo]) * (1 - frac) + float(sorted_vals[hi]) * frac


def _clean(values: Sequence[Any]) -> list[float]:
    out: list[float] = []
    for value in values:
        if value is None:
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def mean_ci(
    values: Sequence[Any],
    *,
    iterations: int = 10000,
    ci: float = 0.95,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Mean and a percentile bootstrap confidence interval."""

    vals = _clean(values)
    n = len(vals)
    result: dict[str, Any] = {
        "n": n,
        "mean": (sum(vals) / n) if n else float("nan"),
        "lo": float("nan"),
        "hi": float("nan"),
        "ci": ci,
    }
    if n <= 1:
        if n == 1:
            result["lo"] = result["hi"] = vals[0]
        return result
    rng = rng or random.Random(0)
    means: list[float] = []
    for _ in range(max(1, iterations)):
        total = 0.0
        for _ in range(n):
            total += vals[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    alpha = (1.0 - ci) / 2.0
    result["lo"] = _percentile(means, alpha)
    result["hi"] = _percentile(means, 1.0 - alpha)
    return result


def paired_bootstrap_diff(
    a: Sequence[Any],
    b: Sequence[Any],
    *,
    iterations: int = 10000,
    ci: float = 0.95,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Mean of paired differences ``a_i - b_i`` with a bootstrap CI.

    ``a`` and ``b`` must be aligned (same unit per index); unequal lengths are
    truncated to the shorter, and index pairs with a missing value are dropped.
    """

    deltas: list[float] = []
    for x, y in zip(a, b):
        if x is None or y is None:
            continue
        try:
            deltas.append(float(x) - float(y))
        except (TypeError, ValueError):
            continue
    stats = mean_ci(deltas, iterations=iterations, ci=ci, rng=rng)
    return {
        "n": stats["n"],
        "mean_diff": stats["mean"],
        "lo": stats["lo"],
        "hi": stats["hi"],
        "ci": ci,
        "excludes_zero": bool(
            stats["n"] >= 2
            and not (math.isnan(stats["lo"]) or math.isnan(stats["hi"]))
            and (stats["lo"] > 0 or stats["hi"] < 0)
        ),
    }


def paired_permutation_test(
    a: Sequence[Any],
    b: Sequence[Any],
    *,
    iterations: int = 10000,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Two-sided sign-flip permutation test for a paired difference."""

    deltas: list[float] = []
    for x, y in zip(a, b):
        if x is None or y is None:
            continue
        try:
            deltas.append(float(x) - float(y))
        except (TypeError, ValueError):
            continue
    n = len(deltas)
    if n == 0:
        return {"p_value": float("nan"), "observed_mean_diff": float("nan"), "n": 0}
    observed = sum(deltas) / n
    rng = rng or random.Random(0)
    extreme = 0
    threshold = abs(observed) - 1e-12
    for _ in range(max(1, iterations)):
        total = 0.0
        for d in deltas:
            total += d if rng.random() < 0.5 else -d
        if abs(total / n) >= threshold:
            extreme += 1
    # +1 smoothing keeps the p-value strictly in (0, 1].
    p_value = (extreme + 1) / (max(1, iterations) + 1)
    return {"p_value": p_value, "observed_mean_diff": observed, "n": n}


def mcnemar_exact(b: int, c: int) -> dict[str, Any]:
    """Exact two-sided McNemar test on discordant paired-binary counts.

    ``b`` = pairs where method A passed and B failed; ``c`` = the reverse. Uses an
    exact binomial(n, 0.5) tail over the ``n = b + c`` discordant pairs.
    """

    b = int(b)
    c = int(c)
    n = b + c
    if n == 0:
        return {"p_value": 1.0, "b": b, "c": c, "discordant": 0}
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return {"p_value": min(1.0, 2.0 * tail), "b": b, "c": c, "discordant": n}


def paired_binary_from_maps(
    a_pass: Mapping[Hashable, bool],
    b_pass: Mapping[Hashable, bool],
) -> dict[str, Any]:
    """Build McNemar discordant counts from two task_id -> pass maps.

    Only task ids present in BOTH maps are compared, so a method that simply ran
    fewer tasks cannot look better by omission.
    """

    shared = sorted(set(a_pass) & set(b_pass), key=str)
    b = sum(1 for t in shared if a_pass[t] and not b_pass[t])
    c = sum(1 for t in shared if (not a_pass[t]) and b_pass[t])
    both = sum(1 for t in shared if a_pass[t] and b_pass[t])
    neither = sum(1 for t in shared if (not a_pass[t]) and (not b_pass[t]))
    result = mcnemar_exact(b, c)
    result.update({"shared_tasks": len(shared), "both_pass": both, "neither_pass": neither})
    return result


def clustered_paired_bootstrap_diff(
    a: Mapping[Hashable, Any],
    b: Mapping[Hashable, Any],
    *,
    iterations: int = 10000,
    ci: float = 0.95,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Paired mean difference with task-cluster bootstrap confidence intervals.

    Formal C1 keys are ``(task_id, seed)``. Resampling therefore draws task ids
    with replacement and carries *all* paired seed observations for each drawn
    task, avoiding the pseudo-replication caused by treating three game seeds as
    independent tasks. Non-tuple keys are treated as one-observation clusters.
    """

    grouped: dict[Hashable, list[float]] = defaultdict(list)
    for key in set(a) & set(b):
        try:
            delta = float(a[key]) - float(b[key])
        except (TypeError, ValueError):
            continue
        cluster = key[0] if isinstance(key, tuple) and key else key
        grouped[cluster].append(delta)
    clusters = sorted(grouped, key=str)
    observed = [value for cluster in clusters for value in grouped[cluster]]
    n = len(observed)
    result: dict[str, Any] = {
        "n": n,
        "clusters": len(clusters),
        "mean_diff": (sum(observed) / n) if n else float("nan"),
        "lo": float("nan"),
        "hi": float("nan"),
        "ci": ci,
        "excludes_zero": False,
    }
    if not clusters:
        return result
    if len(clusters) == 1:
        result["lo"] = result["hi"] = result["mean_diff"]
        return result
    rng = rng or random.Random(0)
    samples: list[float] = []
    for _ in range(max(1, iterations)):
        values: list[float] = []
        for _ in clusters:
            sampled = clusters[rng.randrange(len(clusters))]
            values.extend(grouped[sampled])
        samples.append(sum(values) / len(values))
    samples.sort()
    alpha = (1.0 - ci) / 2.0
    result["lo"] = _percentile(samples, alpha)
    result["hi"] = _percentile(samples, 1.0 - alpha)
    result["excludes_zero"] = bool(result["lo"] > 0 or result["hi"] < 0)
    return result


def _cluster_key(key: Hashable) -> Hashable:
    return key[0] if isinstance(key, tuple) and key else key


def clustered_mean_ci(
    values: Mapping[Hashable, Any],
    *,
    iterations: int = 10000,
    ci: float = 0.95,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Mean with a task-cluster bootstrap that retains every seed per task."""

    grouped: dict[Hashable, list[float]] = defaultdict(list)
    for key, raw in values.items():
        if raw is None:
            continue
        try:
            grouped[_cluster_key(key)].append(float(raw))
        except (TypeError, ValueError):
            continue
    clusters = sorted(grouped, key=str)
    observed = [value for cluster in clusters for value in grouped[cluster]]
    result: dict[str, Any] = {
        "n": len(observed),
        "clusters": len(clusters),
        "mean": (sum(observed) / len(observed)) if observed else float("nan"),
        "lo": float("nan"),
        "hi": float("nan"),
        "ci": ci,
    }
    if not clusters:
        return result
    if len(clusters) == 1:
        result["lo"] = result["hi"] = result["mean"]
        return result
    rng = rng or random.Random(0)
    samples: list[float] = []
    for _ in range(max(1, iterations)):
        sample: list[float] = []
        for _ in clusters:
            sample.extend(grouped[clusters[rng.randrange(len(clusters))]])
        samples.append(sum(sample) / len(sample))
    samples.sort()
    alpha = (1.0 - ci) / 2.0
    result["lo"] = _percentile(samples, alpha)
    result["hi"] = _percentile(samples, 1.0 - alpha)
    return result


def clustered_conditional_mean_ci(
    values: Mapping[Hashable, Any],
    conditions: Mapping[Hashable, Any],
    *,
    iterations: int = 10000,
    ci: float = 0.95,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Conditional mean/rate with task-cluster resampling.

    All task clusters enter the bootstrap, including tasks with no condition-positive
    seed. Samples whose denominator is zero are omitted rather than being scored 0.
    """

    grouped: dict[Hashable, list[tuple[float, bool]]] = defaultdict(list)
    for key in set(values) & set(conditions):
        try:
            value = float(values[key])
        except (TypeError, ValueError):
            continue
        grouped[_cluster_key(key)].append((value, bool(conditions[key])))
    clusters = sorted(grouped, key=str)
    conditioned = [value for cluster in clusters for value, keep in grouped[cluster] if keep]
    result: dict[str, Any] = {
        "n": len(conditioned),
        "clusters": len(clusters),
        "condition_positive": len(conditioned),
        "mean": (sum(conditioned) / len(conditioned)) if conditioned else float("nan"),
        "lo": float("nan"),
        "hi": float("nan"),
        "ci": ci,
    }
    if not clusters or not conditioned:
        return result
    if len(clusters) == 1:
        result["lo"] = result["hi"] = result["mean"]
        return result
    rng = rng or random.Random(0)
    samples: list[float] = []
    for _ in range(max(1, iterations)):
        sample: list[float] = []
        for _ in clusters:
            sampled = clusters[rng.randrange(len(clusters))]
            sample.extend(value for value, keep in grouped[sampled] if keep)
        if sample:
            samples.append(sum(sample) / len(sample))
    samples.sort()
    alpha = (1.0 - ci) / 2.0
    result["lo"] = _percentile(samples, alpha)
    result["hi"] = _percentile(samples, 1.0 - alpha)
    return result


def clustered_conditional_rate_diff(
    b_values: Mapping[Hashable, Any],
    b_conditions: Mapping[Hashable, Any],
    a_values: Mapping[Hashable, Any],
    a_conditions: Mapping[Hashable, Any],
    *,
    iterations: int = 10000,
    ci: float = 0.95,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Estimate ``E[B | B_condition] - E[A | A_condition]`` by task cluster.

    The two arms deliberately retain their own condition denominators.  This is
    the appropriate estimand for effects such as
    ``P_B(goal | B_start) - P_A(goal | A_start)``; intersecting the two
    conditions would instead estimate a different, both-start sensitivity
    quantity.  Formal C1 observations are aligned by ``(task_id, seed)`` and
    the bootstrap resamples task ids, retaining every seed for each sampled
    task.  Replicates in which either arm has a zero condition denominator are
    omitted rather than imputed as zero.
    """

    shared = set(b_values) & set(b_conditions) & set(a_values) & set(a_conditions)
    grouped: dict[Hashable, list[tuple[float, bool, float, bool]]] = defaultdict(list)
    for key in shared:
        try:
            b_value = float(b_values[key])
            a_value = float(a_values[key])
        except (TypeError, ValueError):
            continue
        grouped[_cluster_key(key)].append(
            (b_value, bool(b_conditions[key]), a_value, bool(a_conditions[key]))
        )
    clusters = sorted(grouped, key=str)
    observed = [row for cluster in clusters for row in grouped[cluster]]

    def statistic(rows: Sequence[tuple[float, bool, float, bool]]) -> tuple[float, float, float, int, int]:
        b_conditioned = [b_value for b_value, keep, _, _ in rows if keep]
        a_conditioned = [a_value for _, _, a_value, keep in rows if keep]
        b_n = len(b_conditioned)
        a_n = len(a_conditioned)
        b_mean = (sum(b_conditioned) / b_n) if b_n else float("nan")
        a_mean = (sum(a_conditioned) / a_n) if a_n else float("nan")
        mean_diff = b_mean - a_mean if b_n and a_n else float("nan")
        return mean_diff, b_mean, a_mean, b_n, a_n

    observed_diff, b_mean, a_mean, b_n, a_n = statistic(observed)
    result: dict[str, Any] = {
        "n": len(observed),
        "clusters": len(clusters),
        "b_condition_positive": b_n,
        "a_condition_positive": a_n,
        "b_mean": b_mean,
        "a_mean": a_mean,
        "mean_diff": observed_diff,
        "lo": float("nan"),
        "hi": float("nan"),
        "ci": ci,
        "valid_bootstrap_samples": 0,
        "excludes_zero": False,
    }
    if not clusters or not (b_n and a_n):
        return result
    if len(clusters) == 1:
        result["lo"] = result["hi"] = observed_diff
        return result

    rng = rng or random.Random(0)
    samples: list[float] = []
    for _ in range(max(1, iterations)):
        sample: list[tuple[float, bool, float, bool]] = []
        for _ in clusters:
            sample.extend(grouped[clusters[rng.randrange(len(clusters))]])
        sample_diff, _, _, sample_b_n, sample_a_n = statistic(sample)
        if sample_b_n and sample_a_n:
            samples.append(sample_diff)
    samples.sort()
    result["valid_bootstrap_samples"] = len(samples)
    alpha = (1.0 - ci) / 2.0
    result["lo"] = _percentile(samples, alpha)
    result["hi"] = _percentile(samples, 1.0 - alpha)
    result["excludes_zero"] = bool(
        samples and (result["lo"] > 0 or result["hi"] < 0)
    )
    return result


def clustered_rmst_ci(
    durations: Mapping[Hashable, Any],
    events: Mapping[Hashable, Any],
    *,
    tau: float | None = None,
    iterations: int = 10000,
    ci: float = 0.95,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Censor-aware RMST with task-cluster bootstrap confidence intervals."""

    grouped: dict[Hashable, list[tuple[float, bool]]] = defaultdict(list)
    for key in set(durations) & set(events):
        try:
            duration = max(0.0, float(durations[key]))
        except (TypeError, ValueError):
            continue
        grouped[_cluster_key(key)].append((duration, bool(events[key])))
    clusters = sorted(grouped, key=str)
    observed = [row for cluster in clusters for row in grouped[cluster]]
    observed_stat = restricted_mean_survival_time(
        [duration for duration, _ in observed],
        [event for _, event in observed],
        tau=tau,
    )
    result: dict[str, Any] = {
        **observed_stat,
        "clusters": len(clusters),
        "mean": observed_stat["rmst"],
        "lo": float("nan"),
        "hi": float("nan"),
        "ci": ci,
    }
    if not clusters:
        return result
    if len(clusters) == 1:
        result["lo"] = result["hi"] = result["rmst"]
        return result
    rng = rng or random.Random(0)
    samples: list[float] = []
    for _ in range(max(1, iterations)):
        sample: list[tuple[float, bool]] = []
        for _ in clusters:
            sample.extend(grouped[clusters[rng.randrange(len(clusters))]])
        stat = restricted_mean_survival_time(
            [duration for duration, _ in sample],
            [event for _, event in sample],
            tau=observed_stat["tau"],
        )
        samples.append(float(stat["rmst"]))
    samples.sort()
    alpha = (1.0 - ci) / 2.0
    result["lo"] = _percentile(samples, alpha)
    result["hi"] = _percentile(samples, 1.0 - alpha)
    return result


def clustered_paired_rmst_diff(
    b_durations: Mapping[Hashable, Any],
    b_events: Mapping[Hashable, Any],
    a_durations: Mapping[Hashable, Any],
    a_events: Mapping[Hashable, Any],
    *,
    tau: float,
    iterations: int = 10000,
    ci: float = 0.95,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Task-clustered paired difference ``RMST(B) - RMST(A)`` at one tau.

    Every non-event is right-censored at ``tau`` irrespective of the wall time
    at which an agent or provider failed.  Consequently an early failure cannot
    look faster than a successful completion.  Task clusters are resampled as
    aligned A/B units, retaining every seed observation inside each task.
    """

    shared = set(b_durations) & set(b_events) & set(a_durations) & set(a_events)
    grouped: dict[Hashable, list[tuple[float, bool, float, bool]]] = defaultdict(list)
    limit = max(0.0, float(tau))
    for key in shared:
        try:
            b_duration = max(0.0, float(b_durations[key]))
            a_duration = max(0.0, float(a_durations[key]))
        except (TypeError, ValueError):
            continue
        grouped[_cluster_key(key)].append(
            (b_duration, bool(b_events[key]), a_duration, bool(a_events[key]))
        )
    clusters = sorted(grouped, key=str)

    def statistic(rows: list[tuple[float, bool, float, bool]]) -> float:
        if not rows:
            return float("nan")
        b = restricted_mean_survival_time(
            [duration if event else limit for duration, event, _, _ in rows],
            [event for _, event, _, _ in rows],
            tau=limit,
        )["rmst"]
        a = restricted_mean_survival_time(
            [duration if event else limit for _, _, duration, event in rows],
            [event for _, _, _, event in rows],
            tau=limit,
        )["rmst"]
        return float(b) - float(a)

    observed_rows = [row for cluster in clusters for row in grouped[cluster]]
    observed = statistic(observed_rows)
    result = {
        "n": len(observed_rows),
        "clusters": len(clusters),
        "tau": limit,
        "b_events": sum(
            1 for duration, event, _, _ in observed_rows
            if event and duration <= limit
        ),
        "a_events": sum(
            1 for _, _, duration, event in observed_rows
            if event and duration <= limit
        ),
        "mean_diff": observed,
        "lo": float("nan"),
        "hi": float("nan"),
        "ci": ci,
        "excludes_zero": False,
    }
    if not clusters:
        return result
    if len(clusters) == 1:
        result["lo"] = result["hi"] = observed
        return result
    rng = rng or random.Random(0)
    samples: list[float] = []
    for _ in range(max(1, iterations)):
        sample: list[tuple[float, bool, float, bool]] = []
        for _ in clusters:
            sample.extend(grouped[clusters[rng.randrange(len(clusters))]])
        samples.append(statistic(sample))
    samples.sort()
    alpha = (1.0 - ci) / 2.0
    result["lo"] = _percentile(samples, alpha)
    result["hi"] = _percentile(samples, 1.0 - alpha)
    result["excludes_zero"] = bool(result["lo"] > 0 or result["hi"] < 0)
    return result


def holm_adjust(p_values: Mapping[Hashable, Any]) -> dict[Hashable, float]:
    """Holm step-down family-wise correction, keyed like ``p_values``."""

    valid: list[tuple[Hashable, float]] = []
    invalid: dict[Hashable, float] = {}
    for key, raw in p_values.items():
        try:
            value = float(raw)
        except (TypeError, ValueError):
            invalid[key] = float("nan")
            continue
        if math.isnan(value):
            invalid[key] = value
        else:
            valid.append((key, min(1.0, max(0.0, value))))
    valid.sort(key=lambda item: item[1])
    adjusted: dict[Hashable, float] = {}
    running = 0.0
    total = len(valid)
    for position, (key, value) in enumerate(valid):
        running = max(running, (total - position) * value)
        adjusted[key] = min(1.0, running)
    adjusted.update(invalid)
    return adjusted


def restricted_mean_survival_time(
    durations: Sequence[Any],
    events: Sequence[Any],
    *,
    tau: float | None = None,
) -> dict[str, Any]:
    """Kaplan-Meier restricted mean time, including right-censored failures."""

    rows: list[tuple[float, bool]] = []
    for duration, event in zip(durations, events):
        try:
            value = max(0.0, float(duration))
        except (TypeError, ValueError):
            continue
        rows.append((value, bool(event)))
    if not rows:
        return {"n": 0, "events": 0, "tau": tau, "rmst": float("nan")}
    limit = max(value for value, _ in rows) if tau is None else max(0.0, float(tau))
    event_times = sorted({value for value, event in rows if event and value <= limit})
    survival = 1.0
    previous = 0.0
    area = 0.0
    for time in event_times:
        area += survival * (time - previous)
        at_risk = sum(1 for value, _ in rows if value >= time)
        occurred = sum(1 for value, event in rows if event and value == time)
        if at_risk:
            survival *= 1.0 - occurred / at_risk
        previous = time
    area += survival * max(0.0, limit - previous)
    return {
        "n": len(rows),
        "events": sum(1 for value, event in rows if event and value <= limit),
        "tau": limit,
        "rmst": area,
    }
