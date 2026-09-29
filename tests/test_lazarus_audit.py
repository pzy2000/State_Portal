from __future__ import annotations

from pathlib import Path

import yaml

from warptest.lazarus_audit import audit_lazarus_distribution, audit_lazarus_phase0
from warptest.lazarus_roster import FORMAL_LOCUS_MIX, FORMAL_TASK_COUNT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPOSITORY_ROOT / "benchmarks/p5/c1_lazarus_tasks.yaml"
PHASE0_PATH = REPOSITORY_ROOT / "benchmarks/p5/c1_lazarus_phase0_tasks.yaml"

# The published roster's host mix.  Blender is a declared formal host that never
# had a row admitted, so the audit must accept a host with no published task.
_PUBLISHED_BENCHMARKS = {"jupyterlab": 15, "luanti": 14, "openra": 1}


def _tasks() -> list[dict]:
    rows: list[dict] = [
        {
            "id": f"lz-{benchmark}-row-{position}",
            "benchmark": benchmark,
            "defect": {"defect_locus": "at_goal"},
        }
        for benchmark, count in _PUBLISHED_BENCHMARKS.items()
        for position in range(count)
    ]
    # The sole OpenRA row is the on_path pilot; the rest of the on_path share is
    # taken from JupyterLab so the Luanti pilot stays at_goal.
    on_path = {"lz-openra-row-0"} | {
        f"lz-jupyterlab-row-{position}"
        for position in range(FORMAL_LOCUS_MIX["on_path"] - 1)
    }
    for row in rows:
        if row["id"] in on_path:
            row["defect"]["defect_locus"] = "on_path"
    return rows


def _phase0_pilot_ids() -> tuple[str, ...]:
    payload = yaml.safe_load(PHASE0_PATH.read_text(encoding="utf-8"))
    return tuple(str(task["id"]) for task in payload["tasks"])


def test_distribution_accepts_the_published_roster_shape() -> None:
    result = audit_lazarus_distribution(
        _tasks(),
        pilot_task_ids=("lz-luanti-row-0", "lz-openra-row-0"),
    )

    assert result["complete"] is True
    assert result["task_count"] == FORMAL_TASK_COUNT
    assert result["defect_locus"] == FORMAL_LOCUS_MIX
    assert result["benchmarks"] == {
        "luanti": 14,
        "jupyterlab": 15,
        "openra": 1,
    }


def test_distribution_accepts_the_catalog_as_published() -> None:
    payload = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))

    result = audit_lazarus_distribution(
        payload["tasks"], pilot_task_ids=_phase0_pilot_ids()
    )

    assert result["failures"] == []
    assert result["task_count"] == FORMAL_TASK_COUNT


def test_distribution_fails_closed_on_mix_or_pilot_drift() -> None:
    tasks = _tasks()
    tasks[-1]["defect"]["defect_locus"] = "at_goal"
    result = audit_lazarus_distribution(
        tasks,
        pilot_task_ids=("lz-jupyterlab-row-0", "lz-openra-row-0"),
    )

    assert result["complete"] is False
    assert any("20 at_goal" in failure for failure in result["failures"])
    assert any("Luanti at_goal" in failure for failure in result["failures"])


def test_distribution_rejects_excluded_unity_repositories() -> None:
    tasks = _tasks()
    tasks[0]["benchmark"] = "jynew"

    result = audit_lazarus_distribution(
        tasks,
        pilot_task_ids=("lz-luanti-row-0", "lz-openra-row-0"),
    )

    assert result["complete"] is False
    assert any("unexpected benchmark ids" in failure for failure in result["failures"])


def test_phase0_audit_requires_only_the_registered_luanti_and_openra_shapes() -> None:
    tasks = [
        {
            "id": "lz-luanti-pilot",
            "benchmark": "luanti",
            "defect": {"defect_locus": "at_goal"},
        },
        {
            "id": "lz-openra-pilot",
            "benchmark": "openra",
            "defect": {"defect_locus": "on_path"},
        },
    ]

    result = audit_lazarus_phase0(tasks)

    assert result["complete"] is True
    assert result["task_count"] == 2
    assert result["pilot_task_ids"] == ["lz-luanti-pilot", "lz-openra-pilot"]
