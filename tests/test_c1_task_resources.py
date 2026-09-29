from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from warptest import c1_task_resources
from warptest import c1_native_canary
from warptest.c1_concurrency import C1ReadyScheduler, build_ready_task_plan
from warptest.c1_start_validation import StartTask
from warptest.c1_task_resources import (
    BACKGROUND_INPUT_BENCHMARKS,
    LEGACY_NATIVE_INPUT_LOCK_TARGET,
    UNITY_LICENSE_LOCK_TARGET,
    c1_task_logical_resources,
    c1_task_resource_config,
    c1_task_resource_locks,
)
from warptest.c1_unity_canary import (
    classify_unity_concurrency_canaries,
    unity_serial_lease_target,
)


@pytest.mark.parametrize("benchmark", sorted(BACKGROUND_INPUT_BENCHMARKS))
def test_background_benchmarks_do_not_claim_native_input(
    benchmark: str,
) -> None:
    config = c1_task_resource_config(benchmark)
    expected = (
        (f"unity-serial:{UNITY_LICENSE_LOCK_TARGET}",)
        if benchmark in {"jynew", "pal3"}
        else ()
    )
    assert config.logical_resources == expected
    assert config.native_input_lock_target is None


def test_only_failed_unity_benchmark_gets_its_own_serial_resource() -> None:
    serialized = ["jynew"]
    jynew = c1_task_resource_config(
        "jynew", unity_serialized_benchmarks=serialized
    )
    pal3 = c1_task_resource_config(
        "pal3", unity_serialized_benchmarks=serialized
    )

    target = unity_serial_lease_target("jynew")
    assert jynew.unity_lease_targets == (UNITY_LICENSE_LOCK_TARGET, target)
    assert jynew.logical_resources == (
        f"unity-serial:{UNITY_LICENSE_LOCK_TARGET}",
        f"unity-serial:{target}",
    )
    assert pal3.unity_lease_targets == (UNITY_LICENSE_LOCK_TARGET,)
    assert pal3.logical_resources == (
        f"unity-serial:{UNITY_LICENSE_LOCK_TARGET}",
    )


def test_unity_canary_serializes_only_a_proven_concurrency_only_failure() -> None:
    serialized, blocking = classify_unity_concurrency_canaries(
        {
            "jynew": {
                "passed": False,
                "editor_overlap_passed": True,
                "mutable_storage_isolated": False,
            },
            "pal3": {"passed": True},
        }
    )
    assert serialized == ["jynew"]
    assert blocking == []

    jynew = c1_task_resource_config(
        "jynew", unity_serialized_benchmarks=serialized
    )
    pal3 = c1_task_resource_config(
        "pal3", unity_serialized_benchmarks=serialized
    )
    target = unity_serial_lease_target("jynew")
    assert jynew.unity_lease_targets == (UNITY_LICENSE_LOCK_TARGET, target)
    assert jynew.logical_resources == (
        f"unity-serial:{UNITY_LICENSE_LOCK_TARGET}",
        f"unity-serial:{target}",
    )
    assert pal3.unity_lease_targets == (UNITY_LICENSE_LOCK_TARGET,)
    assert pal3.logical_resources == (
        f"unity-serial:{UNITY_LICENSE_LOCK_TARGET}",
    )


@pytest.mark.parametrize(
    "failed_canary",
    [
        {"passed": False, "error": "Unity executable not found"},
        {
            "passed": False,
            "editor_overlap_passed": False,
            "mutable_storage_isolated": True,
        },
        {
            "passed": False,
            "editor_overlap_passed": True,
            "mutable_storage_isolated": False,
        },
    ],
)
def test_unity_canary_blocks_failures_not_fixed_by_serialization(
    failed_canary: dict[str, object],
) -> None:
    serialized, blocking = classify_unity_concurrency_canaries(
        {"pal3": failed_canary}
    )

    assert serialized == []
    assert blocking == ["pal3"]


def test_resource_config_drives_both_scheduler_label_and_real_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    acquired: list[Path] = []

    @contextlib.contextmanager
    def fake_runtime_lease(target: str | Path, **_kwargs: object):
        acquired.append(Path(target))
        yield Path(target)

    def unexpected_native_lock(*_args: object, **_kwargs: object):
        raise AssertionError("background Unity must not acquire the native lock")

    monkeypatch.setattr(c1_task_resources, "runtime_lease", fake_runtime_lease)
    monkeypatch.setattr(
        c1_task_resources, "native_runtime_lock", unexpected_native_lock
    )
    repo = tmp_path / "jynew-clone"
    with c1_task_resource_locks(
        repo,
        "jynew",
        unity_serialized_benchmarks=["jynew"],
    ):
        pass

    target = unity_serial_lease_target("jynew")
    assert acquired == [UNITY_LICENSE_LOCK_TARGET, target, repo]
    assert c1_task_logical_resources(
        "jynew", unity_serialized_benchmarks=["jynew"]
    ) == (
        f"unity-serial:{UNITY_LICENSE_LOCK_TARGET}",
        f"unity-serial:{target}",
    )


def test_foreground_compatibility_path_aligns_label_and_native_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native_targets: list[Path] = []

    @contextlib.contextmanager
    def fake_runtime_lease(_target: str | Path, **_kwargs: object):
        yield

    @contextlib.contextmanager
    def fake_native_lock(target: str | Path, **_kwargs: object):
        native_targets.append(Path(target))
        yield

    monkeypatch.setattr(c1_task_resources, "runtime_lease", fake_runtime_lease)
    monkeypatch.setattr(c1_task_resources, "native_runtime_lock", fake_native_lock)
    with c1_task_resource_locks(
        tmp_path / "legacy",
        "legacy-quartz",
        foreground_required=True,
    ):
        pass

    assert native_targets == [LEGACY_NATIVE_INPUT_LOCK_TARGET]
    assert c1_task_logical_resources(
        "legacy-quartz", foreground_required=True
    ) == (f"native-backend:{LEGACY_NATIVE_INPUT_LOCK_TARGET}",)


def test_cross_outer_background_tasks_and_pal3_can_be_active_together(
    tmp_path: Path,
) -> None:
    scheduler = C1ReadyScheduler(wait_timeout_s=1.0)
    luanti = SimpleNamespace(id="luanti-a", benchmark="luanti")
    pal3 = SimpleNamespace(id="pal3-a", benchmark="pal3")
    first = scheduler.register_invocation(
        "outer-0",
        outer_ordinal=0,
        registrations=build_ready_task_plan(
            [luanti], repo_for_task=lambda _task: tmp_path / "clone-a"
        ),
        resources_for_task=lambda task: c1_task_logical_resources(
            task.benchmark,
            unity_serialized_benchmarks=["jynew"],
        ),
        worker_limit=1,
    )
    second = scheduler.register_invocation(
        "outer-1",
        outer_ordinal=1,
        registrations=build_ready_task_plan(
            [pal3], repo_for_task=lambda _task: tmp_path / "clone-b"
        ),
        resources_for_task=lambda task: c1_task_logical_resources(
            task.benchmark,
            unity_serialized_benchmarks=["jynew"],
        ),
        worker_limit=1,
    )

    first_ticket = first.claim(lane_id=0)
    second_ticket = second.claim(lane_id=0)
    assert first_ticket is not None
    assert second_ticket is not None
    first.admit(first_ticket, lane_id=0)
    second.admit(second_ticket, lane_id=0)
    assert scheduler.snapshot()["max_active_tasks"] == 2
    first.complete(first_ticket)
    second.complete(second_ticket)


def test_cross_version_unity_tasks_share_the_license_resource(
    tmp_path: Path,
) -> None:
    scheduler = C1ReadyScheduler(wait_timeout_s=1.0)
    jynew = SimpleNamespace(id="jynew-a", benchmark="jynew")
    pal3 = SimpleNamespace(id="pal3-a", benchmark="pal3")
    first = scheduler.register_invocation(
        "outer-0",
        outer_ordinal=0,
        registrations=build_ready_task_plan(
            [jynew], repo_for_task=lambda _task: tmp_path / "clone-a"
        ),
        resources_for_task=lambda task: c1_task_logical_resources(
            task.benchmark,
            unity_serialized_benchmarks=["jynew"],
        ),
        worker_limit=1,
    )
    second = scheduler.register_invocation(
        "outer-1",
        outer_ordinal=1,
        registrations=build_ready_task_plan(
            [pal3], repo_for_task=lambda _task: tmp_path / "clone-b"
        ),
        resources_for_task=lambda task: c1_task_logical_resources(
            task.benchmark,
            unity_serialized_benchmarks=["jynew"],
        ),
        worker_limit=1,
    )

    first_ticket = first.claim(lane_id=0)
    assert first_ticket is not None
    first.admit(first_ticket, lane_id=0)
    second_ticket = second.claim(lane_id=0)
    assert second_ticket is not None
    assert second_ticket.queue_timed_out is True
    first.complete(first_ticket)


def test_background_canary_requires_two_distinct_overlapping_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    run_root = tmp_path / "sandboxes"
    run_root.mkdir()
    task = StartTask(
        id="luanti-canary",
        benchmark="luanti",
        repo=source,
        oracle_spec=tmp_path / "oracle.yaml",
        preconditions="ready",
    )

    class FakeManager:
        def __init__(self) -> None:
            self.run_root = run_root

        @contextlib.contextmanager
        def task_repo(self, _repo: Path, *, lane_id: int, **_kwargs: object):
            clone = tmp_path / f"clone-{lane_id}"
            clone.mkdir(exist_ok=True)
            yield SimpleNamespace(
                repo=clone,
                mode="apfs_cow_clone",
                clone_error=None,
                metadata=lambda: {"mode": "apfs_cow_clone", "repo": str(clone)},
            )

    next_pid = iter((4101, 4102))

    @contextlib.contextmanager
    def fake_runtime(
        cloned_task: StartTask,
        output_dir: Path,
        **_kwargs: object,
    ):
        runtime = SimpleNamespace(
            hold_pid=next(next_pid),
            session_id=f"session-{cloned_task.repo.name}",
            support_dir=output_dir / "support",
        )
        yield runtime

    @contextlib.contextmanager
    def fake_sentinel(_base: Path, _output_dir: Path):
        yield SimpleNamespace()

    monkeypatch.setattr(c1_native_canary, "git_fingerprint", lambda _repo: {"ok": True})
    monkeypatch.setattr(
        c1_native_canary,
        "_start_spec",
        lambda _task: {"target": {"kind": "synthesized_world"}},
    )
    monkeypatch.setattr(c1_native_canary, "runtime_lease", lambda _repo: contextlib.nullcontext())
    monkeypatch.setattr(c1_native_canary, "_native_runtime", fake_runtime)
    monkeypatch.setattr(c1_native_canary, "ForegroundSentinel", fake_sentinel)
    monkeypatch.setattr(
        c1_native_canary,
        "_exercise_runtime",
        lambda runtime, output_dir, _sentinel: {
            "passed": True,
            "session_id": runtime.session_id,
            "before": str(output_dir / "before.png"),
            "after": str(output_dir / "after.png"),
            "execution_receipt": {"accepted": True},
            "foreground_isolation": {"target_never_frontmost": True},
        },
    )

    proof = c1_native_canary._run_background_backend_concurrency_canary(
        base=tmp_path,
        task=task,
        manager=FakeManager(),
        output_dir=tmp_path / "canary",
        timeout_s=5,
    )

    assert proof["passed"] is True
    assert proof["overlap_ns"] > 0
    assert proof["distinct_pids"] is True
    assert proof["distinct_sessions"] is True
    assert proof["distinct_support_dirs"] is True
    assert proof["distinct_report_dirs"] is True


def test_background_canary_uses_first_registered_backend_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = StartTask(
        id="openra-settings",
        benchmark="openra",
        repo=tmp_path / "openra",
        oracle_spec=tmp_path / "settings.yaml",
        preconditions="settings",
    )
    later = StartTask(
        id="openra-gameplay",
        benchmark="openra",
        repo=tmp_path / "openra",
        oracle_spec=tmp_path / "gameplay.yaml",
        preconditions="gameplay",
    )
    selected: list[str] = []

    monkeypatch.setattr(
        c1_native_canary,
        "load_selected_tasks",
        lambda *_args, **_kwargs: ([first, later], tmp_path / "manifest", tmp_path / "tasks"),
    )

    def fake_backend_canary(**kwargs: object) -> dict[str, object]:
        task = kwargs["task"]
        assert isinstance(task, StartTask)
        selected.append(task.id)
        return {"benchmark": task.benchmark, "passed": True}

    monkeypatch.setattr(
        c1_native_canary,
        "_run_background_backend_concurrency_canary",
        fake_backend_canary,
    )
    result = c1_native_canary.run_background_concurrency_canary(
        tmp_path,
        output_dir=tmp_path / "reports",
        benchmarks=["openra"],
        manager=SimpleNamespace(),
        confirm=False,
    )

    assert result["passed"] is True
    assert selected == ["openra-settings"]
