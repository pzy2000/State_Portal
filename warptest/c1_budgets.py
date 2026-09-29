"""Single source of truth for the uniform formal-C1 task budgets.

Both budgets are one constant applied to every task.  They are deliberately not
functions of ``benchmark``, ``horizon_class`` or any per-task property: an
earlier design derived them from the benchmark, which made every horizon-grouped
result identical to a benchmark-grouped result.  A second design derived them
from the reference action sequence, but measured trials showed the weighted
action count explains almost none of the realised step count (Spearman 0.22
against the per-task median, 0.01 against the p90), so a derived budget would
have been a free parameter dressed up as a measurement.

``C1_STEP_BUDGET`` is the binding constraint and is hardware independent.
``C1_TASK_BUDGET_MS`` is sized so the wall clock stays slack at the observed
pace (median ~17.5s per step, so 100 steps needs ~1750s) and therefore acts as a
hang guard rather than as a second, machine-dependent budget.
"""
from __future__ import annotations

from typing import Any


C1_STEP_BUDGET = 100
C1_HORIZON_CLASSES = ("short", "mid", "long")
C1_STEP_BUDGETS = {horizon: C1_STEP_BUDGET for horizon in C1_HORIZON_CLASSES}

C1_TASK_BUDGET_MS = 2_400_000
DEFAULT_C1_TASK_TIMEOUT_S = C1_TASK_BUDGET_MS / 1000.0
# A task budget beyond this is a manifest mistake rather than a long task: the
# slowest observed trial finished in 1183s against the previous 1200s limit.
MAX_C1_TASK_TIMEOUT_S = 3600.0


def c1_step_budget(_task: Any = None) -> int:
    """Return the decision-step budget for one C1 task."""

    return C1_STEP_BUDGET


def c1_task_budget_ms(task: Any = None, *, default_ms: int | None = None) -> int:
    """Return one task's wall-clock budget in milliseconds.

    The task's own ``budget_ms`` is authoritative so the published task file
    stays the place a reader can check the budget; ``default_ms`` only covers
    task rows that predate the field.
    """

    fallback = C1_TASK_BUDGET_MS if default_ms is None else int(default_ms)
    issue = getattr(task, "issue", None)
    raw = getattr(issue, "budget_ms", None)
    if raw is None:
        raw = getattr(task, "budget_ms", None)
    if raw is None:
        return fallback
    budget_ms = int(raw)
    if budget_ms <= 0:
        raise ValueError(f"C1 task budget_ms must be positive, got {budget_ms}")
    return budget_ms
