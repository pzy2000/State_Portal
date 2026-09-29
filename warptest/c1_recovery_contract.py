"""Shared identities and protocol constants for formal C1 recovery."""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple

from . import cost


BACKGROUND_RESOURCE_MIGRATION_ID = "c1_background_resource_partition_v1"
JUPYTER_RUNTIME_RECOVERY_STAGE = "c1_jupyterlab_runtime_environment_v1"
JYNEW_EXIT_TIMEOUT_RECOVERY_STAGE = "c1_jynew_success_report_exit_timeout_v1"
SCREEN_SIZE_PROBE_CACHE_RECOVERY_STAGE = "c1_screen_size_probe_cache_v1"
TERMINAL_LEDGER_RECOVERY_STAGE = "c1_terminal_nonretryable_ledger_v1"
REGISTERED_BACKGROUND_RECOVERY_STAGES = frozenset(
    {
        JUPYTER_RUNTIME_RECOVERY_STAGE,
        JYNEW_EXIT_TIMEOUT_RECOVERY_STAGE,
        SCREEN_SIZE_PROBE_CACHE_RECOVERY_STAGE,
        TERMINAL_LEDGER_RECOVERY_STAGE,
    }
)


class TrialKey(NamedTuple):
    """Immutable identity of one formal C1 trial.

    A ``NamedTuple`` keeps compatibility with persisted four-item tuple keys
    while giving recovery code one explicit, field-addressable identity type.
    """

    arm: str
    model: Any
    seed: Any
    task_id: str

    @classmethod
    def from_row(cls, arm: str, row: Mapping[str, Any]) -> TrialKey:
        return cls(
            str(arm),
            row.get("model"),
            row.get("seed"),
            str(row.get("task_id") or ""),
        )


def background_recovery_stage(migration: Mapping[str, Any] | None) -> str | None:
    """Return a registered background recovery stage, if one is declared."""

    if not isinstance(migration, Mapping):
        return None
    if migration.get("migration_id") != BACKGROUND_RESOURCE_MIGRATION_ID:
        return None
    stage = migration.get("migration_stage")
    return str(stage) if stage in REGISTERED_BACKGROUND_RECOVERY_STAGES else None


def interrupted_unknown_usage_snapshot() -> dict[str, Any]:
    """Return the canonical fail-closed row for an interrupted provider meter."""

    snapshot = cost.CostMeter().summary()
    snapshot.update(
        usage_complete=False,
        incomplete_calls=1,
        unknown_prompt_token_calls=1,
        unknown_completion_token_calls=1,
        unknown_reasoning_token_calls=1,
        unknown_total_token_calls=1,
        reasoning_detail_complete=False,
        reasoning_detail_missing_calls=1,
        reasoning_detail_unknown_fraction=1.0,
        reasoning_detail_within_tolerance=False,
    )
    snapshot["calls"] = [
        {
            "kind": "externally_interrupted_unpersisted_attempt",
            "usage_complete": False,
            "prompt_tokens": None,
            "completion_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
        }
    ]
    return snapshot
