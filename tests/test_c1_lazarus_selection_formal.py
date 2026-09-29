from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.formal import (
    FormalTask,
    TrialCapture,
    run_formal_trials,
    write_execution_authorization,
    write_formal_schedule,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.readiness import (
    REQUIRED_READINESS_GATES,
    write_implementation_readiness,
    write_readiness_gate,
)
from warptest.c1_lazarus_selection.stable_protocol import AuthorizationRecord


def _local_authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-formal/run-1",
        scope="formal-trials",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _tasks(count: int) -> tuple[FormalTask, ...]:
    return tuple(
        FormalTask(
            task_id=f"task-luanti-{number}",
            candidate=CandidateKey("luanti", number),
            locus="at_goal" if number % 3 else "on_path",
        )
        for number in range(1, count + 1)
    )


def _ready(root: Path, scope):
    gates = tuple(
        write_readiness_gate(
            root,
            scope=scope,
            gate_name=name,
            evidence_path=f"reports/readiness/{name}.json",
            authorization=_local_authorization(),
        )
        for name in REQUIRED_READINESS_GATES
    )
    return write_implementation_readiness(
        root,
        scope=scope,
        gate_artifact_ids=tuple(gate.reference.artifact_id for gate in gates),
        authorization=_local_authorization(),
    )


def test_formal_schedule_has_288_cells_and_requires_exact_user_authorization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    schedule = write_formal_schedule(
        root,
        tasks=_tasks(24),
        authorization=_local_authorization(),
    )
    calls = 0

    def execute(*_args) -> TrialCapture:
        nonlocal calls
        calls += 1
        raise AssertionError("trial executed without user authorization")

    with pytest.raises(RuntimeError, match="authorization"):
        run_formal_trials(
            root,
            schedule_artifact_id=schedule.reference.artifact_id,
            execute=execute,
            authorization=_local_authorization(),
        )
    assert calls == 0
    assert schedule.payload["cell_count"] == 288
    assert schedule.payload["analysis_mode"] == "confirmatory"


def test_exploratory_formal_pairs_keep_buggy_checkpoint_fixed_order(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    schedule = write_formal_schedule(
        root,
        tasks=_tasks(2),
        authorization=_local_authorization(),
    )
    write_execution_authorization(
        root,
        schedule_artifact_id=schedule.reference.artifact_id,
        readiness_artifact_id=_ready(root, schedule).reference.artifact_id,
        action="exploratory_trials",
        confirmation_reference="user-confirmation/local-test-1",
        authorization=_local_authorization(),
    )
    lock = threading.Lock()
    events: dict[tuple[str, int, str], list[str]] = defaultdict(list)
    current = 0
    maximum = 0

    def execute(task, seed, arm, state, checkpoint):
        nonlocal current, maximum
        key = (task.task_id, seed, arm)
        with lock:
            current += 1
            maximum = max(maximum, current)
            events[key].append(state)
        try:
            time.sleep(0.002)
            if state == "buggy":
                assert checkpoint is None
                return TrialCapture(
                    state="buggy",
                    raw_evidence_path=f"raw/trial/{task.task_id}/{seed}/{arm}/buggy.json",
                    checkpoint_path=f"raw/checkpoint/{task.task_id}/{seed}/{arm}",
                )
            assert checkpoint == f"raw/checkpoint/{task.task_id}/{seed}/{arm}"
            return TrialCapture(
                state="fixed",
                raw_evidence_path=f"raw/trial/{task.task_id}/{seed}/{arm}/fixed.json",
                checkpoint_path=None,
            )
        finally:
            with lock:
                current -= 1

    result = run_formal_trials(
        root,
        schedule_artifact_id=schedule.reference.artifact_id,
        execute=execute,
        authorization=_local_authorization(),
        outer_workers=8,
    )
    resumed = run_formal_trials(
        root,
        schedule_artifact_id=schedule.reference.artifact_id,
        execute=lambda *_args: (_ for _ in ()).throw(
            AssertionError("completed formal pair was rerun")
        ),
        authorization=_local_authorization(),
        outer_workers=8,
    )

    assert result.completed_pairs == 6
    assert result.completed_cells == 24
    assert result.resumed_pairs == 0
    assert resumed.resumed_pairs == 6
    assert maximum <= 8
    assert all(value == ["buggy", "fixed"] for value in events.values())
