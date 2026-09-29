"""Fail-closed audits for the published C1-Lazarus task population."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from warptest.lazarus_catalog import LAZARUS_FORMAL_BENCHMARK_IDS
from warptest.lazarus_roster import FORMAL_LOCUS_MIX, FORMAL_TASK_COUNT


def audit_lazarus_phase0(tasks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Audit the two-task pilot without claiming formal population coverage."""

    failures: list[str] = []
    ids = [str(task.get("id") or "") for task in tasks]
    if len(tasks) != 2:
        failures.append(f"Phase 0 catalog must contain exactly two tasks, found {len(tasks)}")
    if any(not task_id for task_id in ids) or len(ids) != len(set(ids)):
        failures.append("Phase 0 task ids must be non-empty and unique")
    shapes = {
        (
            str(task.get("benchmark") or ""),
            str((task.get("defect") or {}).get("defect_locus") or ""),
        )
        for task in tasks
    }
    if ("luanti", "at_goal") not in shapes:
        failures.append("Phase 0 requires one Luanti at_goal pilot")
    if ("openra", "on_path") not in shapes:
        failures.append("Phase 0 requires one OpenRA on_path pilot")
    unexpected = {
        str(task.get("benchmark") or "") for task in tasks
    } - {"luanti", "openra"}
    if unexpected:
        failures.append(f"Phase 0 has unexpected benchmarks: {sorted(unexpected)}")
    return {
        "schema": "c1-lazarus-phase0-audit-v1",
        "schema_version": 1,
        "complete": not failures,
        "task_count": len(tasks),
        "pilot_task_ids": ids,
        "shapes": [
            {"benchmark": benchmark, "defect_locus": locus}
            for benchmark, locus in sorted(shapes)
        ],
        "failures": failures,
    }


def audit_lazarus_distribution(
    tasks: Sequence[Mapping[str, Any]],
    *,
    pilot_task_ids: Sequence[str],
) -> dict[str, Any]:
    failures: list[str] = []
    ids = [str(task.get("id") or "") for task in tasks]
    if len(tasks) != FORMAL_TASK_COUNT:
        failures.append(
            f"formal catalog must contain exactly {FORMAL_TASK_COUNT} tasks, "
            f"found {len(tasks)}"
        )
    if any(not task_id for task_id in ids) or len(ids) != len(set(ids)):
        failures.append("task ids must be non-empty and unique")

    # A declared host is allowed to carry no published row.  Admission is
    # evidence-driven rather than quota-filled, so a repository whose rows all
    # failed a mechanical gate must not make the whole roster unauditable.
    benchmark_counts = Counter(str(task.get("benchmark") or "") for task in tasks)
    unexpected = set(benchmark_counts) - set(LAZARUS_FORMAL_BENCHMARK_IDS)
    if unexpected:
        failures.append(f"unexpected benchmark ids: {sorted(unexpected)}")

    locus_counts: Counter[str] = Counter()
    for task in tasks:
        defect = task.get("defect")
        locus = defect.get("defect_locus") if isinstance(defect, Mapping) else None
        locus_counts[str(locus or "")] += 1
    if any(locus_counts[locus] != count for locus, count in FORMAL_LOCUS_MIX.items()):
        failures.append(
            f"formal family mix must be {FORMAL_LOCUS_MIX['at_goal']} at_goal and "
            f"{FORMAL_LOCUS_MIX['on_path']} on_path, found "
            f"{locus_counts['at_goal']}/{locus_counts['on_path']}"
        )
    invalid_loci = set(locus_counts) - {"at_goal", "on_path"}
    if invalid_loci:
        failures.append(f"invalid defect_locus values: {sorted(invalid_loci)}")

    pilots = tuple(str(value) for value in pilot_task_ids)
    if len(pilots) != 2 or len(set(pilots)) != 2:
        failures.append("Phase 0 must register exactly two unique pilot tasks")
    task_by_id = {str(task.get("id")): task for task in tasks}
    missing_pilots = [task_id for task_id in pilots if task_id not in task_by_id]
    if missing_pilots:
        failures.append(f"Phase 0 pilot tasks are missing: {missing_pilots}")
    else:
        pilot_shapes = {
            (
                str(task_by_id[task_id].get("benchmark") or ""),
                str((task_by_id[task_id].get("defect") or {}).get("defect_locus") or ""),
            )
            for task_id in pilots
        }
        if ("luanti", "at_goal") not in pilot_shapes:
            failures.append("Phase 0 requires one Luanti at_goal pilot")
        if ("openra", "on_path") not in pilot_shapes:
            failures.append("Phase 0 requires one OpenRA on_path pilot")

    return {
        "schema": "c1-lazarus-audit-v1",
        "schema_version": 1,
        "complete": not failures,
        "task_count": len(tasks),
        "benchmarks": {
            benchmark: benchmark_counts[benchmark]
            for benchmark in LAZARUS_FORMAL_BENCHMARK_IDS
        },
        "defect_locus": {
            "at_goal": locus_counts["at_goal"],
            "on_path": locus_counts["on_path"],
        },
        "pilot_task_ids": list(pilots),
        "failures": failures,
    }
