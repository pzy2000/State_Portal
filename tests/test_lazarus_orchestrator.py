from __future__ import annotations

import pytest

from warptest.lazarus_roster import FORMAL_TASK_COUNT, FORMAL_TRIAL_COUNT
from warptest.lazarus_orchestrator import (
    DEFAULT_LAZARUS_MODEL,
    LazarusStateReuseTracker,
    LazarusTrialKey,
    LazarusTrialPlan,
    build_lazarus_trial_plan,
)


def test_formal_dry_run_contains_one_unique_cell_per_matrix_position() -> None:
    task_ids = tuple(
        f"lz-luanti-task-{index:02d}" for index in range(FORMAL_TASK_COUNT)
    )

    plan = build_lazarus_trial_plan(task_ids, seeds=(0, 1, 2))

    assert isinstance(plan, LazarusTrialPlan)
    assert plan.complete is True
    assert plan.formal is True
    assert plan.trial_count == FORMAL_TRIAL_COUNT
    assert len(set(plan.keys)) == FORMAL_TRIAL_COUNT
    assert {key.arm for key in plan.keys} == {
        "lazarus_state",
        "lazarus_agent_s",
    }
    assert {key.version for key in plan.keys} == {"buggy", "fixed"}
    assert {key.model for key in plan.keys} == {DEFAULT_LAZARUS_MODEL}
    assert {key.seed for key in plan.keys} == {0, 1, 2}

    for task_id in task_ids:
        for seed in (0, 1, 2):
            cells = [
                key
                for key in plan.keys
                if key.task_id == task_id and key.seed == seed
            ]
            assert {(key.arm, key.version) for key in cells} == {
                ("lazarus_state", "buggy"),
                ("lazarus_state", "fixed"),
                ("lazarus_agent_s", "buggy"),
                ("lazarus_agent_s", "fixed"),
            }
            state_versions = [
                key.version for key in cells if key.arm == "lazarus_state"
            ]
            assert state_versions == ["buggy", "fixed"]


def test_phase_zero_plan_is_exactly_eight_cells_and_deterministic() -> None:
    task_ids = ("lz-luanti-pilot", "lz-openra-pilot")

    first = build_lazarus_trial_plan(task_ids, seeds=(0,), formal=False)
    second = build_lazarus_trial_plan(task_ids, seeds=(0,), formal=False)

    assert first.keys == second.keys
    assert first.trial_count == 8
    assert first.formal is False
    assert first.complete is True
    assert first.rotation_rule == "buggy_before_fixed_state_v1"


def test_trial_key_identity_includes_arm_model_seed_task_and_version() -> None:
    key = LazarusTrialKey(
        arm="lazarus_state",
        model=DEFAULT_LAZARUS_MODEL,
        seed=2,
        task_id="lz-luanti-example",
        version="buggy",
    )

    assert key.identity() == (
        "lazarus_state",
        DEFAULT_LAZARUS_MODEL,
        2,
        "lz-luanti-example",
        "buggy",
    )
    assert key.invocation_id().endswith("__buggy")


def test_state_reuse_requires_buggy_checkpoint_then_zero_call_fixed_hit() -> None:
    tracker = LazarusStateReuseTracker()
    buggy = LazarusTrialKey(
        "lazarus_state", DEFAULT_LAZARUS_MODEL, 0, "lz-luanti-example", "buggy"
    )
    fixed = LazarusTrialKey(
        "lazarus_state", DEFAULT_LAZARUS_MODEL, 0, "lz-luanti-example", "fixed"
    )

    tracker.record_buggy(
        buggy, checkpoint_sha256="a" * 64, actual_synthesis_calls=1
    )
    result = tracker.record_fixed(
        fixed,
        checkpoint_sha256="a" * 64,
        synthesis_cache_hit=True,
        actual_synthesis_calls=0,
    )

    assert result.checkpoint_reuse_success is True
    assert result.actual_synthesis_calls == 0
    assert result.checkpoint_sha256 == "a" * 64


@pytest.mark.parametrize(
    ("checkpoint", "cache_hit", "calls", "message"),
    [
        ("b" * 64, True, 0, "same checkpoint"),
        ("a" * 64, False, 0, "cache hit"),
        ("a" * 64, True, 1, "zero actual synthesis"),
    ],
)
def test_state_reuse_fails_closed_on_mismatch_miss_or_new_synthesis(
    checkpoint: str, cache_hit: bool, calls: int, message: str
) -> None:
    tracker = LazarusStateReuseTracker()
    tracker.record_buggy(
        LazarusTrialKey(
            "lazarus_state",
            DEFAULT_LAZARUS_MODEL,
            0,
            "lz-luanti-example",
            "buggy",
        ),
        checkpoint_sha256="a" * 64,
        actual_synthesis_calls=1,
    )

    with pytest.raises(ValueError, match=message):
        tracker.record_fixed(
            LazarusTrialKey(
                "lazarus_state",
                DEFAULT_LAZARUS_MODEL,
                0,
                "lz-luanti-example",
                "fixed",
            ),
            checkpoint_sha256=checkpoint,
            synthesis_cache_hit=cache_hit,
            actual_synthesis_calls=calls,
        )


def test_state_reuse_rejects_fixed_before_buggy_and_agent_s_keys() -> None:
    tracker = LazarusStateReuseTracker()
    fixed = LazarusTrialKey(
        "lazarus_state", DEFAULT_LAZARUS_MODEL, 0, "lz-luanti-example", "fixed"
    )
    with pytest.raises(ValueError, match="buggy"):
        tracker.record_fixed(
            fixed,
            checkpoint_sha256="a" * 64,
            synthesis_cache_hit=True,
            actual_synthesis_calls=0,
        )
    with pytest.raises(ValueError, match="lazarus_state"):
        tracker.record_buggy(
            LazarusTrialKey(
                "lazarus_agent_s",
                DEFAULT_LAZARUS_MODEL,
                0,
                "lz-luanti-example",
                "buggy",
            ),
            checkpoint_sha256="a" * 64,
            actual_synthesis_calls=1,
        )
