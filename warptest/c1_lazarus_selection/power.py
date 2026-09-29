"""Frozen Monte Carlo power for the complete registered H3 event."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SamplingPowerResult:
    task_count: int
    simulations: int
    h3_p_only_power: float
    h3_complete_event_power: float
    stop_status: Literal[
        "confirmatory_power_eligible",
        "exploratory_power_only",
        "insufficient_population",
    ]


def evaluate_sampling_power(
    *,
    task_count: int,
    simulations: int,
    random_seed: int,
    standardized_time_effect: float,
    standardized_token_effect: float,
    expected_time_ratio: float,
    expected_token_ratio: float,
) -> SamplingPowerResult:
    """Simulate p, direction, observed effect, and ratio jointly."""

    if type(task_count) is not int or task_count < 0:
        raise ValueError("power task count is invalid")
    if type(simulations) is not int or simulations <= 0:
        raise ValueError("power simulation count is invalid")
    if type(random_seed) is not int:
        raise ValueError("power random seed is invalid")
    effects = (standardized_time_effect, standardized_token_effect)
    ratios = (expected_time_ratio, expected_token_ratio)
    if any(type(value) is not float or value <= 0 for value in effects):
        raise ValueError("power effect is invalid")
    if any(type(value) is not float or not 0 < value < 1 for value in ratios):
        raise ValueError("power ratio is invalid")
    if task_count == 0:
        return SamplingPowerResult(
            task_count=0,
            simulations=simulations,
            h3_p_only_power=0.0,
            h3_complete_event_power=0.0,
            stop_status="insufficient_population",
        )

    generator = random.Random(random_seed)
    p_only = 0
    complete = 0
    for _iteration in range(simulations):
        p_events: list[bool] = []
        complete_events: list[bool] = []
        for population_effect, expected_ratio in zip(effects, ratios, strict=True):
            sample = [
                generator.gauss(population_effect, 1.0) for _ in range(task_count)
            ]
            mean = sum(sample) / task_count
            variance = sum((value - mean) ** 2 for value in sample) / max(
                1, task_count - 1
            )
            standard_deviation = math.sqrt(variance)
            observed_effect = (
                mean / standard_deviation if standard_deviation > 0 else math.inf
            )
            statistic = observed_effect * math.sqrt(task_count)
            p_value = 1.0 - _normal_cdf(statistic)
            p_event = mean > 0 and p_value <= 0.05 / 3.0
            ratio = math.exp(-mean * (-math.log(expected_ratio)) / population_effect)
            p_events.append(p_event)
            complete_events.append(p_event and observed_effect >= 0.8 and ratio <= 0.5)
        p_only += all(p_events)
        complete += all(complete_events)
    p_power = p_only / simulations
    complete_power = complete / simulations
    stop_status = (
        "confirmatory_power_eligible"
        if task_count == 24 and complete_power >= 0.8
        else "exploratory_power_only"
    )
    return SamplingPowerResult(
        task_count=task_count,
        simulations=simulations,
        h3_p_only_power=p_power,
        h3_complete_event_power=complete_power,
        stop_status=stop_status,
    )


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


__all__ = ["SamplingPowerResult", "evaluate_sampling_power"]
