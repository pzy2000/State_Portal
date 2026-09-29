# C1 budget unification

## Change

All 61 formal tasks share one budget:

| Parameter | Value | Role |
|---|---|---|
| Decision steps | 100 | Binding constraint (hardware-independent) |
| `budget_ms` | 2400000 (2400 s) | Wall-clock guard against hangs; the measured median is ~17.5 s per step, so 100 steps need about 1750 s |
| `horizon_class` | short / mid / long | **Descriptive label only**; it affects neither the step nor the wall-clock budget |

Single source of truth: [`warptest/c1_budgets.py`](../warptest/c1_budgets.py). The task file's `budget_ms` is the authoritative wall clock; the manifest's `c1_task_timeout_s` is only a fallback for a task row that omits the field.

## Why budgets are not tiered by workload

Across 1431 historical trials, the Spearman rank correlation between the weighted action count of the reference sequence and the median number of steps actually taken is only 0.22, and nearly 0 against the p90. Deriving budgets from action counts would present a free parameter as a measurement. The old short=20 / mid=50 tiers in fact truncated the simplest tasks (such as `2048-merge-left` and `jupyterlab-text-file-edit`), while deep tasks never exhausted their 100-step budget.

## Rerun scope

`c1_provenance.py` is fail-closed: changes to the task file or harness change the content hash, so old runs **cannot** be mixed with the new budget in one formal report unless an explicit exemption path is added (none is implemented). Formal results therefore require a **full rerun**.

The subset whose behavior actually changes is the previously short/mid tasks (their step cap rises from 20/50 to 100). Long tasks already had a 100-step cap; their wall clock is relaxed from 1200 s to 2400 s, but the historical maximum wall time was 1183 s, so their execution traces are expected to be unchanged. Provenance still forbids reusing them directly.

`love-resource-action` was removed only from the formal catalog; its old results remain historical evidence and must not be mixed with the new 61-task runs.

## Post-rerun checks

After the run:

```bash
python scripts/check_c1_budget_rerun.py reports/warptest/<new-run>
```

Expected:

1. Every trial has `decision_budget == 100` and `task_timeout_s == 2400`.
2. The `budget_exhausted` rate drops from the historical ~4.8% to near 0 (and is no longer concentrated in short tasks).
3. Trials that hit the wall clock (`timed_out` with a reason matching the wall-clock limit) are in the single digits; if the wall clock becomes the main source of truncation, 2400 s is still too tight.
