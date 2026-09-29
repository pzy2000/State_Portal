from __future__ import annotations

from warptest.c1_lazarus_selection.power import evaluate_sampling_power


def test_h3_power_uses_joint_p_effect_and_ratio_event() -> None:
    result = evaluate_sampling_power(
        task_count=24,
        simulations=4000,
        random_seed=20260812,
        standardized_time_effect=0.8,
        standardized_token_effect=0.8,
        expected_time_ratio=0.5,
        expected_token_ratio=0.5,
    )

    assert result.h3_p_only_power > 0.8
    assert result.h3_complete_event_power < result.h3_p_only_power
    assert result.h3_complete_event_power < 0.8
    assert result.stop_status == "exploratory_power_only"


def test_power_below_24_is_never_confirmatory() -> None:
    result = evaluate_sampling_power(
        task_count=12,
        simulations=1000,
        random_seed=20260812,
        standardized_time_effect=2.0,
        standardized_token_effect=2.0,
        expected_time_ratio=0.2,
        expected_token_ratio=0.2,
    )

    assert result.stop_status == "exploratory_power_only"
