"""Which C1-Lazarus rows have a provider-backed runtime, and which do not.

A row can hold a passed construction gate, a published golden triplet and an
offline probe binding and still be undispatchable in a paid run.  The offline
probe answers "does the registered replay separate the two versions"; a paid
trial additionally needs a runtime that boots the candidate, restores the Start,
lets the *policy* drive the transition, and captures live before/after frames.
Those are different capabilities, which is why ``probe_executable`` in an oracle
spec cannot answer this question: every roster row has an offline probe.

This registry is the single source of truth for the paid capability.  A new
adapter is registered here in the same change that implements it, so the gate
cannot drift away from the router in :mod:`warptest.lazarus_runtime`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .lazarus_catalog import LAZARUS_ARMS


@dataclass(frozen=True)
class LazarusRuntimeBinding:
    """One task's registered provider-backed runtime."""

    task_id: str
    benchmark: str
    defect_locus: str
    adapter: str
    arms: tuple[str, ...] = LAZARUS_ARMS

    def __post_init__(self) -> None:
        if not self.task_id.startswith("lz-"):
            raise ValueError("Lazarus runtime binding task id must be an lz-* slug")
        if self.defect_locus not in {"at_goal", "on_path"}:
            raise ValueError("Lazarus runtime binding defect_locus is invalid")
        if tuple(self.arms) != LAZARUS_ARMS:
            raise ValueError("Lazarus runtime binding must cover both arms")
        if not self.benchmark or not self.adapter:
            raise ValueError("Lazarus runtime binding must name a benchmark and adapter")


def _binding(**values: Any) -> LazarusRuntimeBinding:
    return LazarusRuntimeBinding(**values)


# Every entry here must be reachable from the runtime router.  Adding a row
# without an implementation would buy cells that can never produce a verdict.
LAZARUS_PAID_RUNTIME_BINDINGS: Mapping[str, LazarusRuntimeBinding] = {
    binding.task_id: binding
    for binding in (
        # Registered for Phase 0 only: this id is deliberately absent from the
        # formal catalog, whose vector row is lz-luanti-9572-vector-rotate.
        _binding(
            task_id="lz-luanti-11919-vector-from-string",
            benchmark="luanti",
            defect_locus="at_goal",
            adapter="luanti_live_world_v1",
        ),
        _binding(
            task_id="lz-luanti-14757-math-round",
            benchmark="luanti",
            defect_locus="at_goal",
            adapter="luanti_live_world_v1",
        ),
        _binding(
            task_id="lz-luanti-14396-metadata-set-string-nil",
            benchmark="luanti",
            defect_locus="at_goal",
            adapter="luanti_live_world_v1",
        ),
        _binding(
            task_id="lz-openra-21453-hotkey-ready-building",
            benchmark="openra",
            defect_locus="on_path",
            adapter="openra_live_gameplay_probe_v1",
        ),
    )
}


def paid_runtime_binding(task_id: str) -> LazarusRuntimeBinding | None:
    """Return the registered paid runtime for one task id, if any."""

    return LAZARUS_PAID_RUNTIME_BINDINGS.get(str(task_id or ""))


def paid_runtime_support_failures(
    tasks: Sequence[Mapping[str, Any]], *, label: str = "paid"
) -> tuple[str, ...]:
    """Return one reason per row that cannot be dispatched for pay.

    ``label`` only shapes the message so the Phase-0 pilot and the formal matrix
    can report the same decision in their own vocabulary.
    """

    failures: list[str] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            failures.append("runtime support input is not a task mapping")
            continue
        task_id = str(task.get("id") or "").strip() or "<unknown>"
        benchmark = str(task.get("benchmark") or "").strip().lower()
        binding = paid_runtime_binding(task_id)
        if binding is None:
            failures.append(f"{task_id}: no verified {label} runtime for {benchmark}")
            continue
        if binding.benchmark != benchmark:
            failures.append(f"{task_id}: benchmark drifted from its runtime binding")
            continue
        if _defect_locus(task) != binding.defect_locus:
            failures.append(f"{task_id}: defect_locus drifted from its runtime binding")
            continue
    return tuple(failures)


def paid_runtime_unbound_task_ids(
    tasks: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return the ids of rows without a dispatchable paid runtime, in order."""

    unbound: list[str] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        if paid_runtime_support_failures((task,)):
            unbound.append(task_id)
    return tuple(unbound)


def lazarus_runtime_support_report(
    tasks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a durable, provider-free coverage decision for a population."""

    rows: list[dict[str, Any]] = []
    for task in tasks:
        task = task if isinstance(task, Mapping) else {}
        task_id = str(task.get("id") or "<unknown>")
        binding = paid_runtime_binding(task_id)
        row_failures = paid_runtime_support_failures((task,))
        rows.append(
            {
                "task_id": task_id,
                "benchmark": str(task.get("benchmark") or "<unknown>"),
                "defect_locus": _defect_locus(task) or "<unknown>",
                "adapter": binding.adapter if binding is not None else None,
                "bound": not row_failures,
                "failures": list(row_failures),
            }
        )
    failures = paid_runtime_support_failures(tasks)
    return {
        "schema": "c1-lazarus-runtime-support-v1",
        "schema_version": 1,
        "complete": not failures,
        "provider_dispatch_permitted": not failures,
        "task_count": len(rows),
        "bound_count": sum(1 for row in rows if row["bound"]),
        "unbound_task_ids": list(paid_runtime_unbound_task_ids(tasks)),
        "tasks": rows,
        "failures": list(failures),
    }


def _defect_locus(task: Mapping[str, Any]) -> str:
    defect = task.get("defect")
    if not isinstance(defect, Mapping):
        return ""
    return str(defect.get("defect_locus") or "")


__all__ = [
    "LAZARUS_PAID_RUNTIME_BINDINGS",
    "LazarusRuntimeBinding",
    "lazarus_runtime_support_report",
    "paid_runtime_binding",
    "paid_runtime_support_failures",
    "paid_runtime_unbound_task_ids",
]
