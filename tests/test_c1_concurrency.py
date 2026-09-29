from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from warptest import cost, gui_agent, p5_aggregate, p5_scale, state_loop
from warptest.c1_concurrency import (
    C1GlobalStop,
    C1ReadyInvocationLifecycle,
    C1ReadyScheduler,
    C1TaskDispatchCancelled,
    build_lane_plan,
    build_ready_task_plan,
    retry_ready_invocation_id,
)
from warptest.c1_sandbox import (
    C1SandboxManager,
    C1SandboxError,
    StrictCloneError,
    _safe_remove,
    git_fingerprint,
    preflight_c1_sandboxes,
    strict_clone_tree,
)
from warptest.c1_unity_canary import run_unity_concurrency_canary
from warptest.state_loop import StateLoopTask
from warptest.task_schema import PublicIssue, resolve_task_repo, task_repo_override


def _task(root: Path, task_id: str, benchmark: str, repo: str, horizon: str = "mid") -> StateLoopTask:
    return StateLoopTask(
        issue=PublicIssue(
            id=task_id,
            benchmark=benchmark,
            repo=repo,
            issue=f"do {task_id}",
            source_path=str(root / "tasks.yaml"),
        ),
        oracle_spec=f"specs/{task_id}.yaml",
        state_description=f"do {task_id}",
        horizon_class=horizon,
    )


class LanePlanTests(unittest.TestCase):
    def test_ready_scheduler_retry_uses_distinct_deterministic_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks = [
                _task(root, "done", "spacerpg", "game-a"),
                _task(root, "retry", "spacerpg", "game-a"),
            ]
            registrations = build_ready_task_plan(
                tasks, repo_for_task=lambda task: root / task.issue.repo
            )
            scheduler = C1ReadyScheduler(wait_timeout_s=1)
            first = scheduler.register_invocation(
                "invocation",
                outer_ordinal=0,
                registrations=registrations,
                resources_for_task=lambda _task: (),
            )
            completed = first.claim(lane_id=0)
            first.admit(completed, lane_id=0)
            first.complete(completed)
            first.cancel()

            retry_registrations = registrations[1:]
            retry_id = retry_ready_invocation_id(
                "invocation", retry_registrations, {"retry": 1}
            )
            self.assertEqual(
                retry_id,
                retry_ready_invocation_id(
                    "invocation", retry_registrations, {"retry": 1}
                ),
            )
            self.assertNotEqual(retry_id, "invocation")
            retry = scheduler.register_invocation(
                retry_id,
                outer_ordinal=0,
                registrations=retry_registrations,
                resources_for_task=lambda _task: (),
            )
            retried = retry.claim(lane_id=0)
            self.assertEqual(retried.task_id, "retry")

    def test_global_stop_keeps_the_first_reason(self) -> None:
        stop = C1GlobalStop()

        self.assertEqual(stop.request("first"), "first")
        self.assertEqual(stop.request("second"), "first")
        self.assertTrue(stop.is_set())
        self.assertEqual(stop.reason, "first")

    def test_cancel_task_is_idempotent_after_sibling_cancelled_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = _task(root, "a", "spacerpg", "game-a")
            scheduler = C1ReadyScheduler(wait_timeout_s=1)
            handle = scheduler.register_invocation(
                "invocation",
                outer_ordinal=0,
                registrations=build_ready_task_plan(
                    [task], repo_for_task=lambda item: root / item.issue.repo
                ),
                resources_for_task=lambda _task: (),
            )
            ticket = handle.claim(lane_id=0)
            self.assertIsNotNone(ticket)
            handle.cancel()
            self.assertEqual(ticket.state, "cancelled")
            lifecycle = C1ReadyInvocationLifecycle(
                component="GUI",
                handle=handle,
                requested_workers=1,
                fallback_workers=1,
                atomic_defer=False,
                serialized_unity_benchmarks=(),
                global_stop_event=None,
            )

            self.assertTrue(lifecycle.cancel_task(ticket, task_id="a"))
            self.assertEqual(ticket.state, "cancelled")

    def test_plan_interleaves_games_and_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks = [
                _task(root, "a1", "a", "game-a", "short"),
                _task(root, "a2", "a", "game-a", "short"),
                _task(root, "b1", "b", "game-b", "long"),
                _task(root, "b2", "b", "game-b", "long"),
                _task(root, "c1", "c", "game-c", "mid"),
            ]
            def resolver(task):
                return root / task.issue.repo
            first = build_lane_plan(tasks, workers=3, repo_for_task=resolver)
            second = build_lane_plan(tasks, workers=3, repo_for_task=resolver)
            self.assertEqual(
                [[item.metadata() for item in lane] for lane in first],
                [[item.metadata() for item in lane] for lane in second],
            )
            dispatch = sorted(
                (item for lane in first for item in lane),
                key=lambda item: item.dispatch_ordinal,
            )
            self.assertEqual([item.task.id for item in dispatch[:3]], ["a1", "b1", "c1"])

    def test_ready_scheduler_runs_unblocked_b_before_busy_a(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks = [
                _task(root, "a", "luanti", "game-a"),
                _task(root, "b", "spacerpg", "game-b"),
            ]
            scheduler = C1ReadyScheduler(
                wait_timeout_s=1,
                initial_backoff_s=0.001,
                maximum_backoff_s=0.001,
            )
            handle = scheduler.register_invocation(
                "invocation",
                outer_ordinal=0,
                registrations=build_ready_task_plan(
                    tasks, repo_for_task=lambda task: root / task.issue.repo
                ),
                resources_for_task=lambda task: (
                    ("native-backend:test",)
                    if task.id == "a"
                    else ()
                ),
            )

            first = handle.claim(lane_id=0)
            self.assertEqual(first.task_id, "a")
            handle.defer(
                first,
                resource_kind="native-backend",
                resource_sha256="busy",
            )
            second = handle.claim(lane_id=0)
            self.assertEqual(second.task_id, "b")
            handle.admit(second, lane_id=0)
            handle.complete(second)
            time.sleep(0.002)
            retried = handle.claim(lane_id=1)
            self.assertEqual(retried.task_id, "a")
            handle.admit(retried, lane_id=1)
            handle.complete(retried)
            snapshot = handle.snapshot()
            self.assertEqual(snapshot["schema_version"], 2)
            self.assertEqual(
                snapshot["lane_policy"],
                "global_resource_ready_round_robin_v2",
            )
            self.assertEqual(
                [row["task_id"] for row in snapshot["dispatch_history"]],
                ["a", "b"],
            )
            self.assertEqual(
                snapshot["dispatch_history"][0]["resource_queue"][
                    "defer_count"
                ],
                1,
            )

    def test_ready_scheduler_round_robins_active_invocations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scheduler = C1ReadyScheduler(wait_timeout_s=1)

            def register(invocation_id: str, outer: int, prefix: str):
                tasks = [
                    _task(root, f"{prefix}1", prefix, f"game-{prefix}-1"),
                    _task(root, f"{prefix}2", prefix, f"game-{prefix}-2"),
                ]
                return scheduler.register_invocation(
                    invocation_id,
                    outer_ordinal=outer,
                    registrations=build_ready_task_plan(
                        tasks,
                        repo_for_task=lambda task: root / task.issue.repo,
                    ),
                    resources_for_task=lambda _task: (),
                )

            first = register("first", 0, "a")
            second = register("second", 1, "b")
            first_ticket = first.claim(lane_id=0)
            first.admit(first_ticket, lane_id=0)
            second_ticket = second.claim(lane_id=0)
            second.admit(second_ticket, lane_id=0)
            first.complete(first_ticket)
            second.complete(second_ticket)
            self.assertEqual(first_ticket.global_start_ordinal, 0)
            self.assertEqual(second_ticket.global_start_ordinal, 1)

    def test_ready_scheduler_resume_preserves_global_ordinals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = _task(root, "a", "spacerpg", "game-a")
            first = C1ReadyScheduler(wait_timeout_s=1)
            first_handle = first.register_invocation(
                "first",
                outer_ordinal=0,
                registrations=build_ready_task_plan(
                    [task], repo_for_task=lambda item: root / item.issue.repo
                ),
                resources_for_task=lambda _task: (),
            )
            first_ticket = first_handle.claim(lane_id=0)
            first_handle.admit(first_ticket, lane_id=0)
            first_handle.complete(first_ticket)

            resumed = C1ReadyScheduler(wait_timeout_s=1)
            resumed.restore_provenance(first.snapshot())
            second_handle = resumed.register_invocation(
                "second",
                outer_ordinal=1,
                registrations=build_ready_task_plan(
                    [task], repo_for_task=lambda item: root / item.issue.repo
                ),
                resources_for_task=lambda _task: (),
            )
            second_ticket = second_handle.claim(lane_id=0)
            second_handle.admit(second_ticket, lane_id=0)
            second_handle.complete(second_ticket)
            snapshot = resumed.snapshot()
            self.assertEqual(second_ticket.global_start_ordinal, 1)
            self.assertEqual(second_ticket.global_completion_ordinal, 1)
            self.assertEqual(len(snapshot["resume_history"]), 1)

    def test_logically_busy_task_does_not_hide_unrelated_ready_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks = [
                _task(root, "a1", "luanti", "game-a"),
                _task(root, "a2", "luanti", "game-a"),
                _task(root, "b", "spacerpg", "game-a"),
            ]
            scheduler = C1ReadyScheduler(
                wait_timeout_s=1,
                initial_backoff_s=0.001,
                maximum_backoff_s=0.001,
            )
            handle = scheduler.register_invocation(
                "invocation",
                outer_ordinal=0,
                registrations=build_ready_task_plan(
                    tasks, repo_for_task=lambda task: root / task.issue.repo
                ),
                resources_for_task=lambda task: (
                    ("native-backend:test",)
                    if task.benchmark == "luanti"
                    else ()
                ),
            )
            running = handle.claim(lane_id=0)
            handle.admit(running, lane_id=0)
            unrelated = handle.claim(lane_id=1)
            self.assertEqual(unrelated.task_id, "b")
            handle.admit(unrelated, lane_id=1)
            handle.complete(unrelated)
            handle.complete(running)
            time.sleep(0.002)
            waiting = handle.claim(lane_id=0)
            self.assertEqual(waiting.task_id, "a2")
            self.assertGreaterEqual(waiting.defer_count, 1)
            handle.admit(waiting, lane_id=0)
            handle.complete(waiting)

    def test_ready_scheduler_worker_limit_close_and_invocation_peak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tasks = [
                _task(root, f"t{index}", "spacerpg", f"game-{index}")
                for index in range(3)
            ]
            scheduler = C1ReadyScheduler(wait_timeout_s=1)
            handle = scheduler.register_invocation(
                "invocation",
                outer_ordinal=0,
                registrations=build_ready_task_plan(
                    tasks, repo_for_task=lambda task: root / task.issue.repo
                ),
                resources_for_task=lambda _task: (),
                worker_limit=2,
            )
            first = handle.claim(lane_id=0)
            second = handle.claim(lane_id=1)
            handle.admit(first, lane_id=0)
            handle.admit(second, lane_id=1)
            handle.close_pending_dispatch(reason="local invocation failure")
            with self.assertRaisesRegex(ValueError, "worker_limit"):
                handle.claim(lane_id=2)
            handle.complete(first)
            handle.complete(second)

            snapshot = handle.snapshot()
            self.assertEqual(snapshot["worker_limit"], 2)
            self.assertEqual(snapshot["active_tasks"], 0)
            self.assertEqual(snapshot["max_active_tasks"], 2)
            self.assertTrue(snapshot["dispatch_closed"])
            self.assertEqual(
                snapshot["dispatch_close_reason"], "local invocation failure"
            )
            self.assertIsNone(handle.claim(lane_id=0))

    def test_recovery_coordinator_uses_inner_lanes_and_records_peak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                {"arms": {"c1_state": {}}},
                run_dir=root / "run",
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_inner_worker=2,
            )
            tasks = [
                {"id": f"t{index}", "benchmark": "spacerpg", "repo": f"game-{index}"}
                for index in range(4)
            ]
            barrier = threading.Barrier(2)
            active = 0
            peak = 0
            lock = threading.Lock()
            lanes: set[int] = set()

            def execute(_task, ticket, lane_id, handle):
                nonlocal active, peak
                handle.admit(ticket, lane_id=lane_id)
                with lock:
                    lanes.add(lane_id)
                    active += 1
                    peak = max(peak, active)
                barrier.wait(timeout=2)
                time.sleep(0.01)
                with lock:
                    active -= 1
                handle.complete(ticket)
                return None

            orchestrator._run_c1_recovery_invocation(
                invocation_id="recovery",
                recovery_tasks=tasks,
                execute_task=execute,
            )

            snapshot = orchestrator.c1_ready_scheduler.snapshot()["invocations"][
                "recovery"
            ]
            self.assertEqual(peak, 2)
            self.assertEqual(lanes, {0, 1})
            self.assertEqual(snapshot["worker_limit"], 2)
            self.assertEqual(snapshot["max_active_tasks"], 2)
            self.assertEqual(len(snapshot["dispatch_history"]), 4)

    def test_recovery_workers_inherit_latin_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                {"arms": {"c1_state": {}}},
                run_dir=root / "run",
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_inner_worker=2,
            )
            expected = {"outer_ordinal": 7, "position": 1}
            orchestrator._latin_local.value = copy.deepcopy(expected)
            seen: list[dict[str, int] | None] = []
            lock = threading.Lock()

            def execute(_task, ticket, lane_id, handle):
                with lock:
                    seen.append(copy.deepcopy(orchestrator._latin_local.value))
                handle.admit(ticket, lane_id=lane_id)
                handle.complete(ticket)
                return None

            orchestrator._run_c1_recovery_invocation(
                invocation_id="recovery-latin",
                recovery_tasks=[
                    {"id": "t1", "benchmark": "spacerpg", "repo": "game-a"},
                    {"id": "t2", "benchmark": "spacerpg", "repo": "game-b"},
                ],
                execute_task=execute,
            )

            self.assertEqual(seen, [expected, expected])
            self.assertEqual(orchestrator._latin_local.value, expected)

    def test_cancelled_atomic_dispatch_closes_recovery_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                {"arms": {"c1_state": {}}},
                run_dir=root / "run",
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_inner_worker=1,
            )
            called: list[str] = []

            def execute(task, _ticket, _lane_id, _handle):
                called.append(task["id"])
                return "cancelled"

            orchestrator._run_c1_recovery_invocation(
                invocation_id="recovery-cancelled",
                recovery_tasks=[
                    {"id": "t1", "benchmark": "spacerpg", "repo": "game-a"},
                    {"id": "t2", "benchmark": "spacerpg", "repo": "game-b"},
                ],
                execute_task=execute,
            )

            self.assertEqual(called, ["t1"])
            snapshot = orchestrator.c1_ready_scheduler.snapshot()["invocations"][
                "recovery-cancelled"
            ]
            self.assertTrue(snapshot["dispatch_closed"])
            self.assertEqual(
                snapshot["dispatch_close_reason"], "global C1 stop requested"
            )
            self.assertEqual(snapshot["active_tasks"], 0)

    def test_execute_returns_atomic_dispatch_cancellation_without_recording_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                {"arms": {"c1_state": {}}},
                run_dir=root / "run",
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
            )

            def cancel(_run_meta):
                raise C1TaskDispatchCancelled("t1")

            with (
                patch.object(orchestrator, "_require_preflight_gate"),
                patch.object(
                    orchestrator,
                    "_prepare_c1_invocation",
                    return_value=(root / "context.json", {}),
                ),
                patch.object(orchestrator, "_validate_c1_invocation_context"),
                patch.object(orchestrator, "_run_meta", return_value={}),
            ):
                outcome = orchestrator._execute(
                    "c1_state",
                    model="m",
                    seed=0,
                    task_id="t1",
                    invocation_dir=root / "invocation",
                    tasks_path=root / "tasks.yaml",
                    invocation={"atomic_defer": True},
                    fn=cancel,
                )

            self.assertEqual(outcome, "cancelled")
            self.assertEqual(orchestrator.index["c1_state"], [])

    def test_execute_discards_sidecar_after_atomic_defer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invocation_dir = root / "invocation"
            invocation_dir.mkdir()
            context_path = invocation_dir / "p5-invocation-context.json"
            context_path.write_text("{}", encoding="utf-8")
            orchestrator = p5_scale.P5Orchestrator(
                root,
                {"arms": {"c1_state": {}}},
                run_dir=root / "run",
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
            )

            def defer(_run_meta):
                raise p5_scale.C1TaskDeferred("busy")

            with (
                patch.object(orchestrator, "_require_preflight_gate"),
                patch.object(
                    orchestrator,
                    "_prepare_c1_invocation",
                    return_value=(context_path, {}),
                ),
                patch.object(orchestrator, "_validate_c1_invocation_context"),
                patch.object(orchestrator, "_run_meta", return_value={}),
            ):
                outcome = orchestrator._execute(
                    "c1_state",
                    model="m",
                    seed=0,
                    task_id="t1",
                    invocation_dir=invocation_dir,
                    tasks_path=root / "tasks.yaml",
                    invocation={"atomic_defer": True},
                    fn=defer,
                )

            self.assertEqual(outcome, "deferred")
            self.assertFalse(invocation_dir.exists())

    def test_recovery_failure_closes_pending_and_settles_running_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                {"arms": {"c1_state": {}}},
                run_dir=root / "run",
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_inner_worker=2,
            )
            tasks = [
                {"id": f"t{index}", "benchmark": "spacerpg", "repo": f"game-{index}"}
                for index in range(3)
            ]
            barrier = threading.Barrier(2)
            called: list[str] = []
            settled: list[str] = []
            lock = threading.Lock()

            def execute(task, ticket, lane_id, handle):
                with lock:
                    called.append(task["id"])
                handle.admit(ticket, lane_id=lane_id)
                barrier.wait(timeout=2)
                if task["id"] == "t0":
                    raise RuntimeError("local infra failure")
                time.sleep(0.02)
                settled.append(task["id"])
                handle.complete(ticket)
                return None

            with self.assertRaisesRegex(RuntimeError, "local infra failure"):
                orchestrator._run_c1_recovery_invocation(
                    invocation_id="recovery-failure",
                    recovery_tasks=tasks,
                    execute_task=execute,
                )

            self.assertCountEqual(called, ["t0", "t1"])
            self.assertEqual(settled, ["t1"])
            snapshot = orchestrator.c1_ready_scheduler.snapshot()["invocations"][
                "recovery-failure"
            ]
            self.assertTrue(snapshot["dispatch_closed"])
            self.assertEqual(snapshot["active_tasks"], 0)

    def test_recovery_defer_requeues_ticket_without_blocking_ready_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                {"arms": {"c1_state": {}}},
                run_dir=root / "run",
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_inner_worker=1,
            )
            orchestrator.c1_ready_scheduler = C1ReadyScheduler(
                wait_timeout_s=1,
                initial_backoff_s=0.001,
                maximum_backoff_s=0.001,
            )
            tasks = [
                {"id": "busy", "benchmark": "spacerpg", "repo": "game-a"},
                {"id": "ready", "benchmark": "spacerpg", "repo": "game-b"},
            ]
            calls: list[str] = []

            def execute(task, ticket, lane_id, handle):
                calls.append(task["id"])
                if task["id"] == "busy" and calls.count("busy") == 1:
                    handle.defer(
                        ticket,
                        resource_kind="native-backend",
                        resource_sha256="busy",
                    )
                    raise p5_scale.C1TaskDeferred("busy")
                handle.admit(ticket, lane_id=lane_id)
                handle.complete(ticket)
                return None

            orchestrator._run_c1_recovery_invocation(
                invocation_id="recovery-defer",
                recovery_tasks=tasks,
                execute_task=execute,
            )

            self.assertEqual(calls, ["busy", "ready", "busy"])
            snapshot = orchestrator.c1_ready_scheduler.snapshot()["invocations"][
                "recovery-defer"
            ]
            by_task = {
                row["task_id"]: row for row in snapshot["dispatch_history"]
            }
            self.assertEqual(by_task["busy"]["resource_queue"]["defer_count"], 1)

    def test_outer_invocations_overlap_and_record_ordinals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                {"arms": {"c1_state": {}}, "retry": {"max_attempts": 1}},
                run_dir=root / "run",
                models=["m1", "m2"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_outer_worker=2,
                c1_inner_worker=1,
            )
            active = 0
            max_active = 0
            lock = threading.Lock()

            def invocation(_model: str, _seed: int) -> None:
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with lock:
                    active -= 1

            orchestrator._run_c1_state_invocation = invocation  # type: ignore[method-assign]
            orchestrator._require_preflight_gate = lambda: None  # type: ignore[method-assign]
            orchestrator._register_c1_trial_matrix = lambda _arms: None  # type: ignore[method-assign]
            orchestrator._import_c1_source_trials = lambda: None  # type: ignore[method-assign]
            orchestrator._run_c1_latin_square(["c1_state"])
            self.assertEqual(max_active, 2)
            self.assertEqual(
                orchestrator.c1_outer_scheduler["schema_version"], 2
            )
            self.assertEqual(orchestrator.c1_outer_scheduler["max_active_invocations"], 2)
            self.assertEqual(
                [row["status"] for row in orchestrator.execution_order],
                ["complete", "complete"],
            )
            self.assertCountEqual(
                [row["completion_ordinal"] for row in orchestrator.execution_order],
                [0, 1],
            )
            self.assertTrue(
                all(
                    len(row["dispatch_history"]) == 1
                    for row in orchestrator.execution_order
                )
            )

    def test_recovery_outer_and_inner_limits_apply_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                {"arms": {"c1_state": {}}, "retry": {"max_attempts": 1}},
                run_dir=root / "run",
                models=["m1", "m2"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_outer_worker=2,
                c1_inner_worker=2,
            )
            barrier = threading.Barrier(4)
            active = 0
            peak = 0
            lock = threading.Lock()

            def invocation(model: str, _seed: int) -> None:
                tasks = [
                    {
                        "id": f"{model}-t{index}",
                        "benchmark": "spacerpg",
                        "repo": f"{model}-game-{index}",
                    }
                    for index in range(2)
                ]

                def execute(_task, ticket, lane_id, handle):
                    nonlocal active, peak
                    handle.admit(ticket, lane_id=lane_id)
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    barrier.wait(timeout=2)
                    with lock:
                        active -= 1
                    handle.complete(ticket)
                    return None

                orchestrator._run_c1_recovery_invocation(
                    invocation_id=f"recovery-{model}",
                    recovery_tasks=tasks,
                    execute_task=execute,
                )

            orchestrator._run_c1_state_invocation = invocation  # type: ignore[method-assign]
            orchestrator._require_preflight_gate = lambda: None  # type: ignore[method-assign]
            orchestrator._register_c1_trial_matrix = lambda _arms: None  # type: ignore[method-assign]
            orchestrator._import_c1_source_trials = lambda: None  # type: ignore[method-assign]
            orchestrator._run_c1_latin_square(["c1_state"])

            self.assertEqual(peak, 4)
            self.assertEqual(
                orchestrator.c1_outer_scheduler["max_active_invocations"], 2
            )
            snapshots = orchestrator.c1_ready_scheduler.snapshot()["invocations"]
            self.assertEqual(
                {snapshot["max_active_tasks"] for snapshot in snapshots.values()},
                {2},
            )

    def test_single_recovery_invocation_keeps_inner_parallelism(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = p5_scale.P5Orchestrator(
                root,
                {"arms": {"c1_state": {}}, "retry": {"max_attempts": 1}},
                run_dir=root / "run",
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_outer_worker=4,
                c1_inner_worker=2,
            )
            barrier = threading.Barrier(2)

            def invocation(model: str, _seed: int) -> None:
                tasks = [
                    {"id": f"t{index}", "benchmark": "spacerpg", "repo": f"game-{index}"}
                    for index in range(2)
                ]

                def execute(_task, ticket, lane_id, handle):
                    handle.admit(ticket, lane_id=lane_id)
                    barrier.wait(timeout=2)
                    handle.complete(ticket)
                    return None

                orchestrator._run_c1_recovery_invocation(
                    invocation_id=f"recovery-{model}",
                    recovery_tasks=tasks,
                    execute_task=execute,
                )

            orchestrator._run_c1_state_invocation = invocation  # type: ignore[method-assign]
            orchestrator._require_preflight_gate = lambda: None  # type: ignore[method-assign]
            orchestrator._register_c1_trial_matrix = lambda _arms: None  # type: ignore[method-assign]
            orchestrator._import_c1_source_trials = lambda: None  # type: ignore[method-assign]
            orchestrator._run_c1_latin_square(["c1_state"])

            self.assertEqual(
                orchestrator.c1_outer_scheduler["effective_outer_workers"], 1
            )
            snapshot = orchestrator.c1_ready_scheduler.snapshot()["invocations"][
                "recovery-m"
            ]
            self.assertEqual(snapshot["max_active_tasks"], 2)

    def test_resume_restores_registration_and_skips_valid_completed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            manifest = {"arms": {"c1_state": {}}, "retry": {"max_attempts": 1}}
            first = p5_scale.P5Orchestrator(
                root,
                manifest,
                run_dir=run_dir,
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_outer_worker=1,
                c1_inner_worker=1,
            )
            first._run_c1_state_invocation = lambda *_args: None  # type: ignore[method-assign]
            first._require_preflight_gate = lambda: None  # type: ignore[method-assign]
            first._register_c1_trial_matrix = lambda _arms: None  # type: ignore[method-assign]
            first._import_c1_source_trials = lambda: None  # type: ignore[method-assign]
            first._run_c1_latin_square(["c1_state"])
            first.write_index()
            original_execution = copy.deepcopy(first.execution_order)
            original_latin = copy.deepcopy(first.latin_square)

            resumed = p5_scale.P5Orchestrator(
                root,
                manifest,
                run_dir=run_dir,
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_outer_worker=1,
                c1_inner_worker=1,
                resume=True,
            )
            invocation = MagicMock()
            resumed._run_c1_state_invocation = invocation  # type: ignore[method-assign]
            resumed._require_preflight_gate = lambda: None  # type: ignore[method-assign]
            resumed._register_c1_trial_matrix = lambda _arms: None  # type: ignore[method-assign]
            resumed._import_c1_source_trials = lambda: None  # type: ignore[method-assign]
            resumed._completed = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
            resumed._run_c1_latin_square(["c1_state"])

            invocation.assert_not_called()
            self.assertEqual(resumed.execution_order, original_execution)
            self.assertEqual(resumed.latin_square, original_latin)
            self.assertEqual(resumed._outer_started, 1)
            self.assertEqual(resumed._outer_completed, 1)

    def test_schema_v1_scheduler_migrates_only_complete_timing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            manifest = {"arms": {"c1_state": {}}, "retry": {"max_attempts": 1}}
            first = p5_scale.P5Orchestrator(
                root,
                manifest,
                run_dir=run_dir,
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_outer_worker=1,
                c1_inner_worker=1,
            )
            first._run_c1_state_invocation = lambda *_args: None  # type: ignore[method-assign]
            first._require_preflight_gate = lambda: None  # type: ignore[method-assign]
            first._register_c1_trial_matrix = lambda _arms: None  # type: ignore[method-assign]
            first._import_c1_source_trials = lambda: None  # type: ignore[method-assign]
            first._run_c1_latin_square(["c1_state"])
            payload = first.write_index()
            payload["c1_scheduler"]["schema_version"] = 1
            payload["execution_order"][0].pop("dispatch_history")
            index_path = run_dir / "p5-run-index.json"
            index_path.write_text(json.dumps(payload), encoding="utf-8")

            migrated = p5_scale.P5Orchestrator(
                root,
                manifest,
                run_dir=run_dir,
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_outer_worker=1,
                c1_inner_worker=1,
                resume=True,
            )
            self.assertEqual(migrated.c1_outer_scheduler["schema_version"], 2)
            self.assertEqual(len(migrated.execution_order[0]["dispatch_history"]), 1)

            broken = json.loads(index_path.read_text(encoding="utf-8"))
            broken["execution_order"][0].pop("started_monotonic_ns")
            index_path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "incomplete start provenance"):
                p5_scale.P5Orchestrator(
                    root,
                    manifest,
                    run_dir=run_dir,
                    models=["m"],
                    seeds=1,
                    arms=["c1_state"],
                    agent_kind="deterministic",
                    c1_outer_worker=1,
                    c1_inner_worker=1,
                    resume=True,
                )

    def test_partial_resume_appends_dispatch_without_duplicate_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            manifest = {"arms": {"c1_state": {}}, "retry": {"max_attempts": 1}}
            first = p5_scale.P5Orchestrator(
                root,
                manifest,
                run_dir=run_dir,
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_outer_worker=1,
                c1_inner_worker=1,
            )
            first._run_c1_state_invocation = MagicMock(
                side_effect=RuntimeError("temporary worker interruption")
            )  # type: ignore[method-assign]
            first._require_preflight_gate = lambda: None  # type: ignore[method-assign]
            first._register_c1_trial_matrix = lambda _arms: None  # type: ignore[method-assign]
            first._import_c1_source_trials = lambda: None  # type: ignore[method-assign]
            with self.assertRaisesRegex(RuntimeError, "temporary worker interruption"):
                first._run_c1_latin_square(["c1_state"])
            first.write_index()

            resumed = p5_scale.P5Orchestrator(
                root,
                manifest,
                run_dir=run_dir,
                models=["m"],
                seeds=1,
                arms=["c1_state"],
                agent_kind="deterministic",
                c1_outer_worker=1,
                c1_inner_worker=1,
                resume=True,
            )
            resumed._run_c1_state_invocation = lambda *_args: None  # type: ignore[method-assign]
            resumed._require_preflight_gate = lambda: None  # type: ignore[method-assign]
            resumed._register_c1_trial_matrix = lambda _arms: None  # type: ignore[method-assign]
            resumed._import_c1_source_trials = lambda: None  # type: ignore[method-assign]
            resumed._run_c1_latin_square(["c1_state"])

            self.assertEqual(len(resumed.execution_order), 1)
            history = resumed.execution_order[0]["dispatch_history"]
            self.assertEqual(len(history), 2)
            self.assertEqual([row["start_ordinal"] for row in history], [0, 1])
            self.assertEqual(
                [row["completion_ordinal"] for row in history], [0, 1]
            )
            self.assertEqual(resumed.execution_order[0]["status"], "complete")

    def test_formal_aggregation_requires_scheduler_provenance(self) -> None:
        _index, _trials, issues = p5_aggregate._index_c1_reports(
            "c1_state",
            [{"schema_version": 2, "model": "m", "complete": True, "results": []}],
            expected_inner_workers=4,
        )
        self.assertTrue(any("scheduler provenance missing" in issue for issue in issues))

    def test_outer_scheduler_provenance_rejects_duplicate_dispatch_ordinals(self) -> None:
        arms = list(p5_scale.C1_ARMS)
        execution = []
        for ordinal, arm in enumerate(arms):
            execution.append(
                {
                    "ordinal": ordinal,
                    "arm": arm,
                    "model": "m",
                    "seed": 0,
                    "model_index": 0,
                    "offset": 0,
                    "position": ordinal,
                    "ordered_arms": arms,
                    "status": "complete",
                    "start_ordinal": ordinal,
                    "completion_ordinal": ordinal,
                    "started_monotonic_ns": ordinal + 1,
                    "completed_monotonic_ns": ordinal + 2,
                    "dispatch_history": [
                        {
                            "dispatch_ordinal": 0,
                            "start_ordinal": ordinal,
                            "completion_ordinal": ordinal,
                            "started_monotonic_ns": ordinal + 1,
                            "completed_monotonic_ns": ordinal + 2,
                            "status": "complete",
                        }
                    ],
                }
            )
        run_index = {
            "models": ["m"],
            "seeds": 1,
            "c1_outer_worker": 2,
            "c1_scheduler": {
                "schema_version": 2,
                "requested_outer_workers": 2,
                "effective_outer_workers": 2,
                "max_active_invocations": 2,
            },
            "execution_order": execution,
            "latin_square": {
                "base_order": arms,
                "offset_rule": f"(model_index * seeds + seed) mod {len(arms)}",
                "blocks": [
                    {
                        "model": "m",
                        "model_index": 0,
                        "seed": 0,
                        "offset": 0,
                        "arms": arms,
                    }
                ],
            },
        }
        self.assertEqual(p5_aggregate._c1_schedule_issues(run_index), [])
        invalid_registration = copy.deepcopy(run_index)
        invalid_registration["execution_order"][1]["position"] = 0
        self.assertTrue(
            any(
                "canonical outer registration" in issue
                for issue in p5_aggregate._c1_schedule_issues(invalid_registration)
            )
        )
        invalid_dispatch = copy.deepcopy(run_index)
        invalid_dispatch["execution_order"][1]["dispatch_history"][0][
            "start_ordinal"
        ] = 0
        self.assertTrue(
            any(
                "start ordinals" in issue
                for issue in p5_aggregate._c1_schedule_issues(invalid_dispatch)
            )
        )

    def test_outer_scheduler_validates_imported_source_provenance(self) -> None:
        arms = list(p5_scale.C1_ARMS)
        source_provenance = [{"source_index": "/clean/source/p5-run-index.json"}]
        execution = [
            {
                "ordinal": ordinal,
                "arm": arm,
                "model": "m",
                "seed": 0,
                "model_index": 0,
                "offset": 0,
                "position": ordinal,
                "ordered_arms": arms,
                "status": "imported_complete",
                "dispatch_history": [],
                "source_provenance": source_provenance,
            }
            for ordinal, arm in enumerate(arms)
        ]
        run_index = {
            "models": ["m"],
            "seeds": 1,
            "c1_outer_worker": 2,
            "c1_scheduler": {
                "schema_version": 2,
                "requested_outer_workers": 2,
                "effective_outer_workers": 0,
                "max_active_invocations": 0,
            },
            "c1_sources": source_provenance,
            "execution_order": execution,
            "latin_square": {
                "base_order": arms,
                "offset_rule": f"(model_index * seeds + seed) mod {len(arms)}",
                "blocks": [
                    {
                        "model": "m",
                        "model_index": 0,
                        "seed": 0,
                        "offset": 0,
                        "arms": arms,
                    }
                ],
            },
        }
        self.assertEqual(p5_aggregate._c1_schedule_issues(run_index), [])
        run_index["execution_order"][0]["source_provenance"] = []
        self.assertTrue(
            any(
                "source provenance" in issue
                for issue in p5_aggregate._c1_schedule_issues(run_index)
            )
        )


class TaskRepoOverrideTests(unittest.TestCase):
    def test_overrides_are_thread_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            left = root / "left"
            right = root / "right"
            for path in (source, left, right):
                path.mkdir()
            issue = PublicIssue("t", "b", "source", "do t")
            barrier = threading.Barrier(2)
            observed: list[Path] = []

            def worker(target: Path) -> None:
                with task_repo_override(source, target):
                    barrier.wait(timeout=5)
                    observed.append(resolve_task_repo(root, issue))

            threads = [threading.Thread(target=worker, args=(target,)) for target in (left, right)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertCountEqual(observed, [left.resolve(), right.resolve()])
            self.assertEqual(resolve_task_repo(root, issue), source.resolve())


class SandboxTests(unittest.TestCase):
    def test_cleanup_ignores_unity_child_disappearing_during_owned_rmtree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = C1SandboxManager(
                run_id="test-run", sandbox_root=root / "sandboxes"
            )
            task_root = manager.run_root / "inv" / "lane-0" / "unity-task"
            task_root.mkdir(parents=True)
            owner = {
                "schema_version": 1,
                "run_id": "test-run",
                "nonce": "nonce",
                "source_repo": str(root / "source"),
            }
            (task_root / ".warptest-c1-sandbox-owner.json").write_text(
                json.dumps(owner, sort_keys=True) + "\n", encoding="utf-8"
            )
            (task_root / "repo").mkdir()
            real_rmtree = shutil.rmtree

            def racy_rmtree(path: Path, *, onerror=None) -> None:
                self.assertIsNotNone(onerror)
                try:
                    raise FileNotFoundError(2, "No such file or directory", "Temp 2")
                except FileNotFoundError:
                    assert onerror is not None
                    onerror(os.unlink, "Temp 2", sys.exc_info())
                real_rmtree(path)

            with patch("warptest.c1_sandbox.shutil.rmtree", side_effect=racy_rmtree):
                self.assertEqual(manager.cleanup_stale(), 1)
            self.assertFalse(task_root.exists())

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS APFS clonefile")
    def test_strict_clone_isolated_and_manager_cleans_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "value.txt").write_text("source", encoding="utf-8")
            clone = root / "clone"
            evidence = strict_clone_tree(source, clone)
            self.assertEqual(evidence, {"was_cloned": True, "copied_bytes": 0})
            (clone / "value.txt").write_text("clone", encoding="utf-8")
            self.assertEqual((source / "value.txt").read_text(encoding="utf-8"), "source")

            manager = C1SandboxManager(run_id="test-run", sandbox_root=root / "sandboxes")
            with manager.task_repo(
                source, invocation_id="inv", lane_id=0, task_id="task"
            ) as sandbox:
                self.assertEqual(sandbox.mode, "apfs_cow_clone")
                sandbox_root = sandbox.root
                self.assertTrue(sandbox.repo.is_dir())
            assert sandbox_root is not None
            self.assertFalse(sandbox_root.exists())

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS APFS clonefile")
    def test_concurrent_task_clones_isolate_same_file_and_preserve_source_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "value.txt").write_text("source\n", encoding="utf-8")
            subprocess.run(
                ["git", "init", "-q", str(source)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "config", "user.name", "WarpTest"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source),
                    "config",
                    "user.email",
                    "warptest@example.invalid",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "add", "value.txt"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(source), "commit", "-qm", "fixture"],
                check=True,
            )
            source_before = git_fingerprint(source)
            manager = C1SandboxManager(
                run_id="isolation-run", sandbox_root=root / "sandboxes"
            )
            barrier = threading.Barrier(2)
            observed: dict[int, str] = {}
            roots: list[Path] = []
            failures: list[BaseException] = []

            def worker(lane_id: int) -> None:
                try:
                    with manager.task_repo(
                        source,
                        invocation_id="inv",
                        lane_id=lane_id,
                        task_id="same-task",
                        allow_source_fallback=False,
                    ) as sandbox:
                        assert sandbox.root is not None
                        roots.append(sandbox.root)
                        (sandbox.repo / "value.txt").write_text(
                            f"lane-{lane_id}\n", encoding="utf-8"
                        )
                        barrier.wait(timeout=10)
                        observed[lane_id] = (sandbox.repo / "value.txt").read_text(
                            encoding="utf-8"
                        )
                except BaseException as error:  # noqa: BLE001 - collect thread failures
                    failures.append(error)

            threads = [threading.Thread(target=worker, args=(lane,)) for lane in (0, 1)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            self.assertEqual(failures, [])
            self.assertEqual(observed, {0: "lane-0\n", 1: "lane-1\n"})
            self.assertEqual(len(set(roots)), 2)
            self.assertTrue(all(not path.exists() for path in roots))
            self.assertEqual((source / "value.txt").read_text(encoding="utf-8"), "source\n")
            self.assertEqual(git_fingerprint(source), source_before)

    def test_success_exception_timeout_and_cancellation_clean_task_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            manager = C1SandboxManager(
                run_id="cleanup-run", sandbox_root=root / "sandboxes"
            )

            def fake_strict_clone(_source: Path, destination: Path) -> dict:
                destination.mkdir()
                return {"was_cloned": True, "copied_bytes": 0}

            with patch(
                "warptest.c1_sandbox.strict_clone_tree",
                side_effect=fake_strict_clone,
            ):
                with manager.task_repo(
                    source,
                    invocation_id="inv",
                    lane_id=0,
                    task_id="success",
                    allow_source_fallback=False,
                ) as sandbox:
                    success_root = sandbox.root
                assert success_root is not None
                self.assertFalse(success_root.exists())

                for index, error_type in enumerate(
                    (RuntimeError, TimeoutError, asyncio.CancelledError), start=1
                ):
                    task_root: Path | None = None
                    with self.assertRaises(error_type):
                        with manager.task_repo(
                            source,
                            invocation_id="inv",
                            lane_id=index,
                            task_id=error_type.__name__,
                            allow_source_fallback=False,
                        ) as sandbox:
                            task_root = sandbox.root
                            raise error_type("fixture")
                    assert task_root is not None
                    self.assertFalse(task_root.exists())

            self.assertEqual(
                list(manager.run_root.rglob(".warptest-c1-sandbox-owner.json")),
                [],
            )

    def test_safe_cleanup_refuses_missing_tampered_wrong_run_and_broad_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = C1SandboxManager(
                run_id="owned-run", sandbox_root=root / "sandboxes"
            )
            manager.run_root.mkdir(parents=True)
            owner = {
                "schema_version": 1,
                "run_id": "owned-run",
                "nonce": "nonce",
                "source_repo": str(root / "source"),
            }

            missing = manager.run_root / "inv" / "missing"
            missing.mkdir(parents=True)
            with self.assertRaisesRegex(C1SandboxError, "unowned"):
                _safe_remove(missing, manager.run_root, owner)

            tampered = manager.run_root / "inv" / "tampered"
            tampered.mkdir(parents=True)
            (tampered / ".warptest-c1-sandbox-owner.json").write_text(
                json.dumps({**owner, "nonce": "different"}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(C1SandboxError, "marker mismatch"):
                _safe_remove(tampered, manager.run_root, owner)
            (tampered / ".warptest-c1-sandbox-owner.json").write_text(
                json.dumps({**owner, "run_id": "another-run"}) + "\n",
                encoding="utf-8",
            )

            wrong_run = manager.run_root / "inv" / "wrong-run"
            wrong_run.mkdir(parents=True)
            (wrong_run / ".warptest-c1-sandbox-owner.json").write_text(
                json.dumps({**owner, "run_id": "another-run"}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(manager.cleanup_stale(), 0)
            self.assertTrue(wrong_run.exists())

            outside = root / "outside"
            outside.mkdir()
            (outside / ".warptest-c1-sandbox-owner.json").write_text(
                json.dumps(owner) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(C1SandboxError, "unsafe sandbox cleanup"):
                _safe_remove(outside, manager.run_root, owner)
            with self.assertRaisesRegex(C1SandboxError, "unsafe sandbox cleanup"):
                _safe_remove(manager.run_root, manager.run_root, owner)

    def test_clone_fingerprint_mismatch_cleans_partial_clone_without_copy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            manager = C1SandboxManager(
                run_id="fingerprint-run", sandbox_root=root / "sandboxes"
            )

            def fake_strict_clone(_source: Path, destination: Path) -> dict:
                destination.mkdir()
                return {"was_cloned": True, "copied_bytes": 0}

            with (
                patch(
                    "warptest.c1_sandbox.strict_clone_tree",
                    side_effect=fake_strict_clone,
                ),
                patch(
                    "warptest.c1_sandbox.git_fingerprint",
                    side_effect=[
                        {"head": "source", "status": ""},
                        {"head": "clone", "status": ""},
                    ],
                ),
            ):
                with manager.task_repo(
                    source,
                    invocation_id="inv",
                    lane_id=0,
                    task_id="task",
                ) as sandbox:
                    self.assertEqual(sandbox.mode, "repo_serial_fallback")
                    self.assertIn("fingerprint mismatch", sandbox.clone_error or "")

            self.assertFalse(any(manager.run_root.rglob("repo")))
            clone_source = inspect.getsource(strict_clone_tree)
            self.assertIn("clonefile", clone_source)
            self.assertNotIn("copytree", clone_source)
            self.assertNotIn("copy2", clone_source)

    def test_clone_failure_uses_source_without_copy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            manager = C1SandboxManager(run_id="test-run", sandbox_root=root / "sandboxes")
            with patch(
                "warptest.c1_sandbox.strict_clone_tree",
                side_effect=StrictCloneError("ENOTSUP"),
            ):
                with manager.task_repo(
                    source, invocation_id="inv", lane_id=1, task_id="task"
                ) as sandbox:
                    self.assertEqual(sandbox.mode, "repo_serial_fallback")
                    self.assertEqual(sandbox.repo, source.resolve())
                    self.assertIn("ENOTSUP", sandbox.clone_error or "")
            self.assertFalse(any((root / "sandboxes").rglob("repo")))

    def test_clone_failure_never_falls_back_for_mutable_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            manager = C1SandboxManager(run_id="test-run", sandbox_root=root / "sandboxes")
            with patch(
                "warptest.c1_sandbox.strict_clone_tree",
                side_effect=StrictCloneError("timed out"),
            ):
                with self.assertRaisesRegex(
                    StrictCloneError, "source-checkout fallback is forbidden"
                ):
                    with manager.task_repo(
                        source,
                        invocation_id="inv",
                        lane_id=1,
                        task_id="unity-task",
                        allow_source_fallback=False,
                    ):
                        self.fail("forbidden source fallback must not yield")
            self.assertFalse(any((root / "sandboxes").rglob("repo")))

    def test_clone_failure_serializes_the_full_source_task_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            manager = C1SandboxManager(run_id="test-run", sandbox_root=root / "sandboxes")
            first_entered = threading.Event()
            release_first = threading.Event()
            second_entered = threading.Event()
            active = 0
            max_active = 0
            active_lock = threading.Lock()

            def worker(lane_id: int) -> None:
                nonlocal active, max_active
                with manager.task_repo(
                    source,
                    invocation_id="inv",
                    lane_id=lane_id,
                    task_id=f"task-{lane_id}",
                ):
                    with active_lock:
                        active += 1
                        max_active = max(max_active, active)
                    if lane_id == 0:
                        first_entered.set()
                        release_first.wait(timeout=5)
                    else:
                        second_entered.set()
                    with active_lock:
                        active -= 1

            with patch(
                "warptest.c1_sandbox.strict_clone_tree",
                side_effect=StrictCloneError("ENOTSUP"),
            ):
                first = threading.Thread(target=worker, args=(0,))
                second = threading.Thread(target=worker, args=(1,))
                first.start()
                self.assertTrue(first_entered.wait(timeout=5))
                second.start()
                time.sleep(0.05)
                self.assertFalse(second_entered.is_set())
                release_first.set()
                first.join(timeout=5)
                second.join(timeout=5)
            self.assertTrue(second_entered.is_set())
            self.assertEqual(max_active, 1)

    def test_preflight_rejects_less_than_50_gib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            usage = SimpleNamespace(
                total=100 * 1024**3,
                used=51 * 1024**3,
                free=49 * 1024**3,
            )
            with (
                patch("warptest.c1_sandbox._filesystem_type", return_value="apfs"),
                patch("warptest.c1_sandbox.shutil.disk_usage", return_value=usage),
                patch(
                    "warptest.c1_sandbox._important_available_bytes",
                    return_value=49 * 1024**3,
                ),
            ):
                with self.assertRaisesRegex(C1SandboxError, "at least"):
                    preflight_c1_sandboxes([repo], sandbox_root=root / "sandboxes")

    def test_preflight_rejects_non_apfs_and_cross_device_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            sandbox_root = root / "sandboxes"
            repo.mkdir()
            with patch("warptest.c1_sandbox._filesystem_type", return_value="hfs"):
                with self.assertRaisesRegex(C1SandboxError, "not APFS"):
                    preflight_c1_sandboxes([repo], sandbox_root=sandbox_root)

            sandbox_root.mkdir(exist_ok=True)
            repo_path = str(repo.resolve())
            real_stat = Path.stat

            def different_device(path: Path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                if str(path) == repo_path:
                    return SimpleNamespace(st_dev=result.st_dev + 1)
                return result

            with (
                patch("warptest.c1_sandbox._filesystem_type", return_value="apfs"),
                patch.object(Path, "stat", different_device),
            ):
                with self.assertRaisesRegex(C1SandboxError, "share the sandbox APFS device"):
                    preflight_c1_sandboxes([repo], sandbox_root=sandbox_root)

    def test_preflight_accepts_exactly_50_gib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            available = 50 * 1024**3
            usage = SimpleNamespace(
                total=100 * 1024**3,
                used=available,
                free=available,
            )
            with (
                patch("warptest.c1_sandbox._filesystem_type", return_value="apfs"),
                patch("warptest.c1_sandbox.shutil.disk_usage", return_value=usage),
                patch(
                    "warptest.c1_sandbox._important_available_bytes",
                    return_value=available,
                ),
            ):
                result = preflight_c1_sandboxes(
                    [repo], sandbox_root=root / "sandboxes"
                )

            self.assertEqual(result["minimum_free_bytes"], available)
            self.assertEqual(result["eligible_free_bytes"], available)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS APFS clonefile")
    def test_unity_canary_proves_pal3_overlap_and_cleans_clones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "unity-project"
            source.mkdir()
            (source / "ProjectSettings").mkdir()
            (source / "ProjectSettings" / "ProjectVersion.txt").write_text(
                "m_EditorVersion: test", encoding="utf-8"
            )
            manager = C1SandboxManager(
                run_id="canary-run", sandbox_root=root / "sandboxes"
            )
            pid_lock = threading.Lock()
            next_pid = 100

            class FakeSession:
                def __init__(self) -> None:
                    nonlocal next_pid
                    with pid_lock:
                        next_pid += 1
                        self.hold_pid = next_pid

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def clean_entry(self):
                    return {"status": "success", "actions_executed": False}

            with patch(
                "warptest.c1_unity_canary.create_c1_unity_session",
                side_effect=lambda *_args, **_kwargs: FakeSession(),
            ):
                evidence = run_unity_concurrency_canary(
                    benchmark="pal3",
                    source_repo=source,
                    manager=manager,
                    report_dir=root / "reports",
                    timeout_s=10,
                )
            self.assertTrue(evidence["passed"])
            self.assertTrue(evidence["editor_overlap_passed"])
            self.assertTrue(evidence["mutable_storage_isolated"])
            self.assertTrue(evidence["distinct_pids"])
            self.assertGreater(evidence["overlap_ns"], 0)
            self.assertTrue(evidence["sandboxes_cleaned"])

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS APFS clonefile")
    def test_unity_canary_rejects_jynew_shared_persistent_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "unity-project"
            source.mkdir()
            (source / "ProjectSettings").mkdir()
            (source / "ProjectSettings" / "ProjectVersion.txt").write_text(
                "m_EditorVersion: test", encoding="utf-8"
            )
            manager = C1SandboxManager(
                run_id="canary-run", sandbox_root=root / "sandboxes"
            )
            pid_lock = threading.Lock()
            next_pid = 200

            class FakeSession:
                def __init__(self) -> None:
                    nonlocal next_pid
                    with pid_lock:
                        next_pid += 1
                        self.hold_pid = next_pid

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def clean_entry(self):
                    return {"status": "success", "actions_executed": False}

            with patch(
                "warptest.c1_unity_canary.create_c1_unity_session",
                side_effect=lambda *_args, **_kwargs: FakeSession(),
            ):
                evidence = run_unity_concurrency_canary(
                    benchmark="jynew",
                    source_repo=source,
                    manager=manager,
                    report_dir=root / "reports",
                    timeout_s=10,
                )
            self.assertFalse(evidence["passed"])
            self.assertTrue(evidence["editor_overlap_passed"])
            self.assertFalse(evidence["mutable_storage_isolated"])
            self.assertIn("persistentDataPath", evidence["parallel_rejection_reason"])
            self.assertTrue(evidence["sandboxes_cleaned"])


class AgentSLaneLifecycleTests(unittest.TestCase):
    def test_each_lane_owns_one_client_and_tasks_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("game-a", "game-b"):
                (root / name).mkdir()
            tasks = [
                _task(root, "a1", "a", "game-a"),
                _task(root, "a2", "a", "game-a"),
                _task(root, "b1", "b", "game-b"),
                _task(root, "b2", "b", "game-b"),
            ]
            active = 0
            max_active = 0
            active_lock = threading.Lock()
            clients: list[SimpleNamespace] = []

            def build_client(**_kwargs):
                client = SimpleNamespace(resets=[], closed=False)
                client.reset = lambda task: client.resets.append(task)
                client.close = lambda: setattr(client, "closed", True)
                clients.append(client)
                return client

            def run_task(_base, task, **kwargs):
                nonlocal active, max_active
                kwargs["policy_client"].reset(task=task.id)
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.04)
                with active_lock:
                    active -= 1
                return {"task_id": task.id, "status": "success", "incomplete": False}

            empty = cost.CostMeter().summary()
            with (
                patch.dict(os.environ, {"TEST_API_KEY": "secret"}, clear=False),
                patch("warptest.gui_agent.resolve_model", return_value=("m", SimpleNamespace(api_key_env="TEST_API_KEY"))),
                patch("warptest.gui_agent.load_state_loop_tasks", return_value=tasks),
                patch("warptest.gui_agent.FixedVisualBridge", return_value=object()),
                patch("warptest.gui_agent._build_policy_client", side_effect=build_client),
                patch("warptest.gui_agent._run_agent_task", side_effect=run_task),
                patch("warptest.gui_agent._gui_attempt_cost", return_value={
                    "task_actual_cost": empty,
                    "synthesis_accounting": {
                        "actual_cost": empty,
                        "transient_actual_cost": empty,
                        "attributed_cost": empty,
                        "attributed": False,
                    },
                }),
            ):
                report = gui_agent.run_gui_agent_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    model="m",
                    policy_kind="agent_s",
                    report_dir=root / "report",
                    inner_workers=2,
                )
            self.assertTrue(report["complete"])
            self.assertEqual(report["scheduler"]["effective_inner_workers"], 2)
            self.assertEqual(report["scheduler"]["max_active_tasks"], 2)
            self.assertGreaterEqual(max_active, 2)
            self.assertEqual(len(clients), 2)
            self.assertTrue(all(client.closed for client in clients))
            self.assertCountEqual(
                [task for client in clients for task in client.resets],
                [task.id for task in tasks],
            )
            persisted = json.loads((root / "report" / "gui-agent.json").read_text())
            self.assertEqual([row["task_id"] for row in persisted["results"]], [task.id for task in tasks])

    def test_incomplete_task_closes_start_gate_before_report_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("game-a", "game-b"):
                (root / name).mkdir()
            tasks = [
                _task(root, "a1", "a", "game-a"),
                _task(root, "a2", "a", "game-a"),
                _task(root, "b1", "b", "game-b"),
                _task(root, "b2", "b", "game-b"),
            ]
            barrier = threading.Barrier(2)
            incomplete_seen = threading.Event()
            calls: list[str] = []
            calls_lock = threading.Lock()
            empty = cost.CostMeter().summary()

            def run_task(_base, task, **_kwargs):
                with calls_lock:
                    calls.append(task.id)
                if task.id == "a1":
                    barrier.wait(timeout=5)
                    incomplete_seen.set()
                    return {
                        "task_id": task.id,
                        "status": "incomplete",
                        "incomplete": True,
                        "reason": "HTTP 503 service unavailable",
                    }
                if task.id == "b1":
                    barrier.wait(timeout=5)
                    incomplete_seen.wait(timeout=5)
                    time.sleep(0.05)
                return {"task_id": task.id, "status": "success", "incomplete": False}

            client = SimpleNamespace(close=lambda: None)
            with (
                patch.dict(os.environ, {"TEST_API_KEY": "secret"}, clear=False),
                patch("warptest.gui_agent.resolve_model", return_value=("m", SimpleNamespace(api_key_env="TEST_API_KEY"))),
                patch("warptest.gui_agent.load_state_loop_tasks", return_value=tasks),
                patch("warptest.gui_agent.FixedVisualBridge", return_value=object()),
                patch("warptest.gui_agent._build_policy_client", return_value=client),
                patch("warptest.gui_agent._run_agent_task", side_effect=run_task),
                patch("warptest.gui_agent._gui_attempt_cost", return_value={
                    "task_actual_cost": empty,
                    "synthesis_accounting": {
                        "actual_cost": empty,
                        "transient_actual_cost": empty,
                        "attributed_cost": empty,
                        "attributed": False,
                    },
                }),
            ):
                report = gui_agent.run_gui_agent_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    model="m",
                    report_dir=root / "report",
                    inner_workers=2,
                )
            self.assertCountEqual(calls, ["a1", "b1"])
            self.assertTrue(report["scheduler"]["cancelled"])
            self.assertIn("incomplete task a1", report["scheduler"]["cancellation_reason"])

    def test_global_stop_prevents_next_gui_task_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "game-a").mkdir()
            tasks = [
                _task(root, "a1", "a", "game-a"),
                _task(root, "a2", "a", "game-a"),
            ]
            global_stop = threading.Event()
            calls: list[str] = []
            empty = cost.CostMeter().summary()

            def run_task(_base, task, **_kwargs):
                calls.append(task.id)
                global_stop.set()
                return {
                    "task_id": task.id,
                    "status": "success",
                    "incomplete": False,
                }

            client = SimpleNamespace(close=lambda: None)
            with (
                patch.dict(os.environ, {"TEST_API_KEY": "secret"}, clear=False),
                patch(
                    "warptest.gui_agent.resolve_model",
                    return_value=(
                        "m",
                        SimpleNamespace(api_key_env="TEST_API_KEY"),
                    ),
                ),
                patch("warptest.gui_agent.load_state_loop_tasks", return_value=tasks),
                patch("warptest.gui_agent.FixedVisualBridge", return_value=object()),
                patch(
                    "warptest.gui_agent._build_policy_client",
                    return_value=client,
                ),
                patch("warptest.gui_agent._run_agent_task", side_effect=run_task),
                patch(
                    "warptest.gui_agent._gui_attempt_cost",
                    return_value={
                        "task_actual_cost": empty,
                        "synthesis_accounting": {
                            "actual_cost": empty,
                            "transient_actual_cost": empty,
                            "attributed_cost": empty,
                            "attributed": False,
                        },
                    },
                ),
            ):
                report = gui_agent.run_gui_agent_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    model="m",
                    report_dir=root / "report",
                    inner_workers=1,
                    global_stop_event=global_stop,
                )

            self.assertEqual(calls, ["a1"])
            self.assertFalse(report["complete"])
            self.assertTrue(report["scheduler"]["cancelled"])
            self.assertEqual(
                report["scheduler"]["cancellation_reason"],
                "global C1 stop requested",
            )

    def test_atomic_gui_dispatch_cancellation_propagates_to_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "game-a").mkdir()
            tasks = [_task(root, "a1", "a", "game-a")]
            scheduler = C1ReadyScheduler(wait_timeout_s=1)
            handle = scheduler.register_invocation(
                "atomic-gui",
                outer_ordinal=0,
                registrations=build_ready_task_plan(
                    tasks, repo_for_task=lambda task: root / task.issue.repo
                ),
                resources_for_task=lambda _task: (),
                worker_limit=1,
            )
            ticket = handle.claim(lane_id=0)
            global_stop = threading.Event()
            global_stop.set()
            client = SimpleNamespace(close=lambda: None)

            with (
                patch.dict(os.environ, {"TEST_API_KEY": "secret"}, clear=False),
                patch(
                    "warptest.gui_agent.resolve_model",
                    return_value=(
                        "m",
                        SimpleNamespace(api_key_env="TEST_API_KEY"),
                    ),
                ),
                patch("warptest.gui_agent.load_state_loop_tasks", return_value=tasks),
                patch("warptest.gui_agent.FixedVisualBridge", return_value=object()),
                patch(
                    "warptest.gui_agent._build_policy_client",
                    return_value=client,
                ),
                patch("warptest.gui_agent._run_agent_task") as run_task,
            ):
                with self.assertRaises(C1TaskDispatchCancelled):
                    gui_agent.run_gui_agent_benchmark(
                        root,
                        tasks_path="tasks.yaml",
                        model="m",
                        report_dir=root / "report",
                        inner_workers=1,
                        global_stop_event=global_stop,
                        ready_handle=handle,
                        ready_ticket=ticket,
                        atomic_defer=True,
                    )

            run_task.assert_not_called()
            self.assertEqual(ticket.state, "claimed")
            handle.complete(ticket, status="cancelled")

    def test_gui_dispatch_cancellation_releases_claimed_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "game-a").mkdir()
            tasks = [_task(root, "a1", "a", "game-a")]
            scheduler = C1ReadyScheduler(wait_timeout_s=1)
            handle = scheduler.register_invocation(
                "gui-cancel",
                outer_ordinal=0,
                registrations=build_ready_task_plan(
                    tasks, repo_for_task=lambda task: root / task.issue.repo
                ),
                resources_for_task=lambda _task: ("native-backend:test",),
                worker_limit=1,
            )
            global_stop = threading.Event()
            global_stop.set()
            client = SimpleNamespace(close=lambda: None)

            with (
                patch.dict(os.environ, {"TEST_API_KEY": "secret"}, clear=False),
                patch(
                    "warptest.gui_agent.resolve_model",
                    return_value=(
                        "m",
                        SimpleNamespace(api_key_env="TEST_API_KEY"),
                    ),
                ),
                patch("warptest.gui_agent.load_state_loop_tasks", return_value=tasks),
                patch("warptest.gui_agent.FixedVisualBridge", return_value=object()),
                patch("warptest.gui_agent._build_policy_client", return_value=client),
                patch("warptest.gui_agent._run_agent_task") as run_task,
            ):
                report = gui_agent.run_gui_agent_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    model="m",
                    report_dir=root / "report",
                    inner_workers=1,
                    global_stop_event=global_stop,
                    ready_handle=handle,
                )

            run_task.assert_not_called()
            self.assertFalse(report["complete"])
            snapshot = handle.snapshot()
            self.assertTrue(snapshot["dispatch_closed"])
            self.assertEqual(snapshot["active_tasks"], 0)
            self.assertEqual(snapshot["dispatch_history"][0]["status"], "cancelled")

    def test_unknown_gui_usage_publishes_global_stop_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "game-a").mkdir()
            tasks = [
                _task(root, "a1", "a", "game-a"),
                _task(root, "a2", "a", "game-a"),
            ]
            global_stop = C1GlobalStop()
            calls: list[str] = []
            unknown = cost.CostMeter().summary()
            unknown.update(
                usage_complete=False,
                incomplete_calls=1,
                unknown_total_token_calls=1,
            )

            def run_task(_base, task, **_kwargs):
                calls.append(task.id)
                return {
                    "task_id": task.id,
                    "status": "success",
                    "incomplete": False,
                }

            client = SimpleNamespace(close=lambda: None)
            with (
                patch.dict(os.environ, {"TEST_API_KEY": "secret"}, clear=False),
                patch(
                    "warptest.gui_agent.resolve_model",
                    return_value=(
                        "m",
                        SimpleNamespace(api_key_env="TEST_API_KEY"),
                    ),
                ),
                patch("warptest.gui_agent.load_state_loop_tasks", return_value=tasks),
                patch("warptest.gui_agent.FixedVisualBridge", return_value=object()),
                patch(
                    "warptest.gui_agent._build_policy_client",
                    return_value=client,
                ),
                patch("warptest.gui_agent._run_agent_task", side_effect=run_task),
                patch(
                    "warptest.gui_agent._gui_attempt_cost",
                    return_value={
                        "task_actual_cost": unknown,
                        "synthesis_accounting": {
                            "actual_cost": cost.CostMeter().summary(),
                            "transient_actual_cost": cost.CostMeter().summary(),
                            "attributed_cost": cost.CostMeter().summary(),
                            "attributed": False,
                        },
                    },
                ),
            ):
                report = gui_agent.run_gui_agent_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    model="m",
                    report_dir=root / "report",
                    inner_workers=1,
                    global_stop_event=global_stop,
                )

            self.assertEqual(calls, ["a1"])
            self.assertFalse(report["complete"])
            self.assertTrue(global_stop.is_set())
            self.assertEqual(
                global_stop.reason,
                "permanently unknown provider usage",
            )


class RecoveryOutcomeClassificationTests(unittest.TestCase):
    def test_ledger_nonretryable_exact_usage_is_preserved(self) -> None:
        report = {
            "complete": False,
            "retryable_incomplete": False,
            "incomplete_category": "nonretryable_incomplete_evidence",
        }
        exact_cost = {
            "usage_complete": True,
            "calls": 1,
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "reasoning_tokens": 0,
            "cached_prompt_tokens": 0,
            "total_tokens": 3,
            "incomplete_calls": 0,
            "unknown_prompt_token_calls": 0,
            "unknown_completion_token_calls": 0,
            "unknown_reasoning_token_calls": 0,
            "unknown_total_token_calls": 0,
        }
        result = {
            "incomplete": True,
            "retryable_incomplete": False,
            "actual_cost": exact_cost,
            "attempt_costs": [
                {
                    "attempt_status": "nonretryable_incomplete",
                    "retry_category": None,
                    "retry_reason": None,
                }
            ],
        }

        expected = "exact_usage_nonretryable_model_failure"
        self.assertEqual(
            p5_scale.P5Orchestrator._registered_source_model_failure_kind(
                result=result,
                report=report,
            ),
            expected,
        )
        self.assertEqual(
            p5_aggregate._preserved_model_failure_kind(
                result=result,
                report=report,
            ),
            expected,
        )

        result["attempt_costs"] = []
        self.assertIsNone(
            p5_scale.P5Orchestrator._registered_source_model_failure_kind(
                result=result,
                report=report,
            )
        )
        self.assertIsNone(
            p5_aggregate._preserved_model_failure_kind(
                result=result,
                report=report,
            )
        )


class StateLaneLifecycleTests(unittest.TestCase):
    def test_each_lane_owns_one_synthesis_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("game-a", "game-b"):
                (root / name).mkdir()
            tasks = [
                _task(root, "a1", "a", "game-a"),
                _task(root, "a2", "a", "game-a"),
                _task(root, "b1", "b", "game-b"),
                _task(root, "b2", "b", "game-b"),
            ]
            agents: list[SimpleNamespace] = []
            task_agents: dict[str, int] = {}
            task_source_repos: dict[str, Path] = {}
            active = 0
            max_active = 0
            lock = threading.Lock()
            empty = cost.CostMeter().summary()

            def build_agent(*_args, **_kwargs):
                agent = SimpleNamespace(agent_id=len(agents), closed=False)
                agent.close = lambda: setattr(agent, "closed", True)
                agents.append(agent)
                return agent

            def run_task(_base, task, agent, **_kwargs):
                nonlocal active, max_active
                task_agents[task.id] = agent.agent_id
                task_source_repos[task.id] = _kwargs["source_repo"]
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.04)
                with lock:
                    active -= 1
                return {"task_id": task.id, "status": "success", "cost": empty}

            with (
                patch("warptest.state_loop.load_state_loop_tasks", return_value=tasks),
                patch("warptest.state_loop.build_synthesis_agent", side_effect=build_agent),
                patch("warptest.state_loop._run_single_task", side_effect=run_task),
            ):
                report = state_loop.run_state_loop_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    agent_kind="deterministic",
                    report_dir=root / "report",
                    inner_workers=2,
                )
            self.assertTrue(report["complete"])
            self.assertEqual(report["scheduler"]["max_active_tasks"], 2)
            self.assertGreaterEqual(max_active, 2)
            self.assertEqual(len(agents), 2)
            self.assertTrue(all(agent.closed for agent in agents))
            self.assertEqual(set(task_agents), {task.id for task in tasks})
            self.assertEqual(
                task_source_repos,
                {
                    task.id: (root / task.issue.repo).resolve()
                    for task in tasks
                },
            )
            self.assertLessEqual(len(set(task_agents.values())), 2)

    def test_incomplete_task_prevents_later_state_lane_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("game-a", "game-b"):
                (root / name).mkdir()
            tasks = [
                _task(root, "a1", "a", "game-a"),
                _task(root, "a2", "a", "game-a"),
                _task(root, "b1", "b", "game-b"),
                _task(root, "b2", "b", "game-b"),
            ]
            barrier = threading.Barrier(2)
            incomplete_seen = threading.Event()
            calls: list[str] = []
            calls_lock = threading.Lock()
            empty = cost.CostMeter().summary()
            agent = SimpleNamespace(close=lambda: None)

            def run_task(_base, task, _agent, **_kwargs):
                with calls_lock:
                    calls.append(task.id)
                if task.id == "a1":
                    barrier.wait(timeout=5)
                    incomplete_seen.set()
                    return {
                        "task_id": task.id,
                        "benchmark": task.benchmark,
                        "status": "failed",
                        "incomplete": True,
                        "reason": "HTTP 503 service unavailable",
                        "cost": empty,
                    }
                if task.id == "b1":
                    barrier.wait(timeout=5)
                    incomplete_seen.wait(timeout=5)
                    time.sleep(0.05)
                return {
                    "task_id": task.id,
                    "benchmark": task.benchmark,
                    "status": "success",
                    "cost": empty,
                }

            with (
                patch("warptest.state_loop.load_state_loop_tasks", return_value=tasks),
                patch("warptest.state_loop.build_synthesis_agent", return_value=agent),
                patch("warptest.state_loop._run_single_task", side_effect=run_task),
            ):
                report = state_loop.run_state_loop_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    agent_kind="deterministic",
                    report_dir=root / "report",
                    inner_workers=2,
                )
            self.assertCountEqual(calls, ["a1", "b1"])
            self.assertTrue(report["scheduler"]["cancelled"])
            self.assertIn("incomplete task a1", report["scheduler"]["cancellation_reason"])

    def test_global_stop_prevents_next_state_task_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "game-a").mkdir()
            tasks = [
                _task(root, "a1", "a", "game-a"),
                _task(root, "a2", "a", "game-a"),
            ]
            global_stop = threading.Event()
            calls: list[str] = []
            empty = cost.CostMeter().summary()
            agent = SimpleNamespace(close=lambda: None)

            def run_task(_base, task, _agent, **_kwargs):
                calls.append(task.id)
                global_stop.set()
                return {
                    "task_id": task.id,
                    "benchmark": task.benchmark,
                    "status": "success",
                    "cost": empty,
                }

            with (
                patch("warptest.state_loop.load_state_loop_tasks", return_value=tasks),
                patch(
                    "warptest.state_loop.build_synthesis_agent",
                    return_value=agent,
                ),
                patch(
                    "warptest.state_loop._run_single_task",
                    side_effect=run_task,
                ),
            ):
                report = state_loop.run_state_loop_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    agent_kind="deterministic",
                    report_dir=root / "report",
                    inner_workers=1,
                    global_stop_event=global_stop,
                )

            self.assertEqual(calls, ["a1"])
            self.assertFalse(report["complete"])
            self.assertTrue(report["scheduler"]["cancelled"])
            self.assertEqual(
                report["scheduler"]["cancellation_reason"],
                "global C1 stop requested",
            )

    def test_atomic_state_dispatch_cancellation_propagates_to_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "game-a").mkdir()
            tasks = [_task(root, "a1", "a", "game-a")]
            scheduler = C1ReadyScheduler(wait_timeout_s=1)
            handle = scheduler.register_invocation(
                "atomic-state",
                outer_ordinal=0,
                registrations=build_ready_task_plan(
                    tasks, repo_for_task=lambda task: root / task.issue.repo
                ),
                resources_for_task=lambda _task: (),
                worker_limit=1,
            )
            ticket = handle.claim(lane_id=0)
            global_stop = threading.Event()
            global_stop.set()
            agent = SimpleNamespace(close=lambda: None)

            with (
                patch("warptest.state_loop.load_state_loop_tasks", return_value=tasks),
                patch(
                    "warptest.state_loop.build_synthesis_agent",
                    return_value=agent,
                ),
                patch("warptest.state_loop._run_single_task") as run_task,
            ):
                with self.assertRaises(C1TaskDispatchCancelled):
                    state_loop.run_state_loop_benchmark(
                        root,
                        tasks_path="tasks.yaml",
                        agent_kind="deterministic",
                        report_dir=root / "report",
                        inner_workers=1,
                        global_stop_event=global_stop,
                        ready_handle=handle,
                        ready_ticket=ticket,
                        atomic_defer=True,
                    )

            run_task.assert_not_called()
            self.assertEqual(ticket.state, "claimed")
            handle.complete(ticket, status="cancelled")

    def test_state_dispatch_cancellation_releases_claimed_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "game-a").mkdir()
            tasks = [_task(root, "a1", "a", "game-a")]
            scheduler = C1ReadyScheduler(wait_timeout_s=1)
            handle = scheduler.register_invocation(
                "state-cancel",
                outer_ordinal=0,
                registrations=build_ready_task_plan(
                    tasks, repo_for_task=lambda task: root / task.issue.repo
                ),
                resources_for_task=lambda _task: ("native-backend:test",),
                worker_limit=1,
            )
            global_stop = threading.Event()
            global_stop.set()
            agent = SimpleNamespace(close=lambda: None)

            with (
                patch("warptest.state_loop.load_state_loop_tasks", return_value=tasks),
                patch(
                    "warptest.state_loop.build_synthesis_agent",
                    return_value=agent,
                ),
                patch("warptest.state_loop._run_single_task") as run_task,
            ):
                report = state_loop.run_state_loop_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    agent_kind="deterministic",
                    report_dir=root / "report",
                    inner_workers=1,
                    global_stop_event=global_stop,
                    ready_handle=handle,
                )

            run_task.assert_not_called()
            self.assertFalse(report["complete"])
            snapshot = handle.snapshot()
            self.assertTrue(snapshot["dispatch_closed"])
            self.assertEqual(snapshot["active_tasks"], 0)
            self.assertEqual(snapshot["dispatch_history"][0]["status"], "cancelled")

    def test_unknown_state_usage_publishes_global_stop_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "game-a").mkdir()
            tasks = [
                _task(root, "a1", "a", "game-a"),
                _task(root, "a2", "a", "game-a"),
            ]
            global_stop = C1GlobalStop()
            calls: list[str] = []
            unknown = cost.CostMeter().summary()
            unknown.update(
                usage_complete=False,
                incomplete_calls=1,
                unknown_total_token_calls=1,
            )
            agent = SimpleNamespace(close=lambda: None)

            def run_task(_base, task, _agent, **_kwargs):
                calls.append(task.id)
                return {
                    "task_id": task.id,
                    "benchmark": task.benchmark,
                    "status": "failed",
                    "incomplete": True,
                    "reason": "provider usage is unknown",
                    "cost": unknown,
                }

            with (
                patch("warptest.state_loop.load_state_loop_tasks", return_value=tasks),
                patch(
                    "warptest.state_loop.build_synthesis_agent",
                    return_value=agent,
                ),
                patch(
                    "warptest.state_loop._run_single_task",
                    side_effect=run_task,
                ),
            ):
                report = state_loop.run_state_loop_benchmark(
                    root,
                    tasks_path="tasks.yaml",
                    agent_kind="deterministic",
                    report_dir=root / "report",
                    inner_workers=1,
                    global_stop_event=global_stop,
                )

            self.assertEqual(calls, ["a1"])
            self.assertFalse(report["complete"])
            self.assertTrue(global_stop.is_set())
            self.assertEqual(
                global_stop.reason,
                "permanently unknown provider usage",
            )


if __name__ == "__main__":
    unittest.main()


def test_unity_canary_keeps_ready_editor_for_slow_peer_within_total_budget(tmp_path):
    """A 180-second startup skew must fit the 900-second canary budget."""
    from contextlib import contextmanager
    from warptest import c1_unity_canary as canary

    scale = 1000.0
    real_barrier = threading.Barrier(2)
    first_ready = threading.Event()

    class ScaledBarrier:
        def __init__(self, parties):
            assert parties == 2

        def wait(self, timeout):
            return real_barrier.wait(timeout=timeout / scale)

        def abort(self):
            real_barrier.abort()

    @contextmanager
    def task_repo(source, *, lane_id, **kwargs):
        yield SimpleNamespace(mode="apfs_cow_clone", repo=tmp_path / str(lane_id),
                              metadata=lambda: {}, clone_error=None)

    @contextmanager
    def session(benchmark, repo, output, **kwargs):
        lane = int(repo.name)
        def clean_entry():
            if lane == 0:
                first_ready.set()
            else:
                assert first_ready.wait(timeout=1)
                time.sleep(180.0 / scale)
            return {"status": "success"}
        yield SimpleNamespace(hold_pid=100 + lane, clean_entry=clean_entry)

    clock = SimpleNamespace(monotonic=lambda: time.monotonic() * scale,
                            monotonic_ns=time.monotonic_ns, sleep=lambda seconds: None)
    with patch.object(canary, "time", clock), patch.object(
        canary, "threading", SimpleNamespace(Barrier=ScaledBarrier, Lock=threading.Lock)
    ), patch.object(canary, "git_fingerprint", return_value={"head": "fixed", "status": ""}), patch.object(
        canary, "create_c1_unity_session", session
    ):
        result = canary.run_unity_concurrency_canary(
            benchmark="pal3", source_repo=tmp_path,
            manager=SimpleNamespace(task_repo=task_repo, run_root=tmp_path / "sandboxes"),
            report_dir=tmp_path / "reports", timeout_s=900,
        )
    assert result["passed"]
    assert result["editor_overlap_passed"]
    assert result["overlap_ns"] > 0
