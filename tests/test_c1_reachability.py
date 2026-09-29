from __future__ import annotations

import threading
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from warptest import gui_agent
from warptest.state_loop import (
    StateLoopTask,
    _run_luanti_task,
    _set_state_method_timing,
    audit_scripted_reference_replay,
)
from warptest.task_schema import PublicIssue


def _task(*, benchmark: str = "2048") -> StateLoopTask:
    return StateLoopTask(
        issue=PublicIssue("c1-task", benchmark, f"games/{benchmark}", "public issue"),
        oracle_spec="oracle.yaml",
        state_description="public issue",
    )


def _write_informative_png(path: Path, level: int) -> None:
    """Write a frame the start gate accepts, which rejects flat or absent frames."""
    import random

    from PIL import Image

    rng = random.Random(level)
    image = Image.new("RGB", (256, 256))
    image.putdata(
        [
            (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            for _ in range(256 * 256)
        ]
    )
    image.save(path)


def _inspection(*, profile: str = "Game2048Profile") -> SimpleNamespace:
    return SimpleNamespace(
        profile=profile,
        metadata=SimpleNamespace(
            git_commit="deadbeef",
            launch_command="serve --port {port}",
        ),
    )


def _plan(*, unsupported_reason: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        strategy_id="scripted_replay",
        validity="transition-valid",
        unsupported_reason=unsupported_reason,
    )


def _report(*, reached: bool) -> SimpleNamespace:
    status = "success" if reached else "failure"
    return SimpleNamespace(
        target_validated_before_smoke=reached,
        checkpoint_restoration=SimpleNamespace(status=status, detail="restore detail"),
        validation=SimpleNamespace(status=status, detail="validation detail"),
        overall_status=status,
        metrics={"actions_replayed": 3, "time_to_target_ms": 42},
    )


def test_reference_replay_unsupported_is_false_without_starting_runtime(
    tmp_path: Path,
) -> None:
    profile = MagicMock()
    profile.strategy_plan.return_value = _plan(unsupported_reason="no replay plan")

    with (
        patch("warptest.state_loop.RepositoryInspector") as inspector_cls,
        patch("warptest.state_loop.profile_for", return_value=profile),
        patch("warptest.state_loop.BrowserRuntimeAdapter") as runtime_cls,
    ):
        inspector_cls.return_value.inspect.return_value = _inspection(profile="SpaceRPGProfile")
        evidence = audit_scripted_reference_replay(
            tmp_path,
            _task(),
            tmp_path,
            {
                "name": "oracle",
                "replay_from_start": {"actions": []},
                "checkpoint": {},
                "actions": [],
            },
            tmp_path / "oracle.yaml",
            seed=7,
            headless=True,
            cache_root=tmp_path / "cache",
        )

    assert evidence["status"] == "unsupported"
    assert evidence["reference_reachable"] is False
    assert Path(evidence["evidence_file"]).exists()
    runtime_cls.assert_not_called()


def test_reference_replay_executes_once_then_reuses_auditable_cache(
    tmp_path: Path,
) -> None:
    profile = MagicMock()
    profile.strategy_plan.return_value = _plan()
    profile.run.return_value = _report(reached=True)
    runtime = MagicMock()
    runtime_cm = MagicMock()
    runtime_cm.__enter__.return_value = runtime
    runtime_cls = MagicMock(return_value=runtime_cm)

    with (
        patch("warptest.state_loop.RepositoryInspector") as inspector_cls,
        patch("warptest.state_loop.profile_for", return_value=profile),
        patch("warptest.state_loop.BrowserRuntimeAdapter", runtime_cls),
        patch("warptest.state_loop.runtime_lease", return_value=nullcontext()),
    ):
        inspector_cls.return_value.inspect.return_value = _inspection()
        kwargs = {
            "root": tmp_path,
            "task": _task(),
            "repo": tmp_path,
            "oracle_spec": {
                "name": "oracle",
                "replay_from_start": {"actions": [{"type": "key", "key": "ArrowLeft"}]},
                "checkpoint": {},
                "actions": [],
            },
            "oracle_spec_path": tmp_path / "oracle.yaml",
            "seed": 3,
            "headless": True,
            "cache_root": tmp_path / "cache",
        }
        first = audit_scripted_reference_replay(**kwargs)
        second = audit_scripted_reference_replay(**kwargs)

    assert first["status"] == "success"
    assert first["reference_reachable"] is True
    assert first["cache_hit"] is False
    assert second["reference_reachable"] is True
    assert second["cache_hit"] is True
    runtime_cls.assert_called_once()
    assert runtime_cls.call_args.kwargs["experiment_seed"] == 3
    profile.run.assert_called_once_with(
        {**kwargs["oracle_spec"], "actions": [], "assertions": []},
        kwargs["oracle_spec_path"],
        runtime,
        mode="replay_from_start",
    )


def test_concurrent_cache_misses_run_the_reference_replay_exactly_once(
    tmp_path: Path,
) -> None:
    """Two arms missing the cache together must not both launch the replay.

    The evidence is shared across arms and models, and the replay writes engine
    IPC handshake files inside the shared evidence directory, so a concurrent
    second launch made the two sessions reject each other's nonce. Each arm also
    runs in its own sandbox clone, so the per-checkout lease cannot serialise
    them.
    """

    profile = MagicMock()
    profile.strategy_plan.return_value = _plan()
    profile.run.return_value = _report(reached=True)
    concurrent = threading.Barrier(2)
    live = 0
    peak = 0
    guard = threading.Lock()

    def enter(_self: object) -> MagicMock:
        nonlocal live, peak
        with guard:
            live += 1
            peak = max(peak, live)
        # Hold the runtime open long enough for a racing caller to collide.
        time.sleep(0.2)
        return MagicMock()

    def exit_(_self: object, *_args: object) -> None:
        nonlocal live
        with guard:
            live -= 1

    runtime_cm = MagicMock()
    runtime_cm.__enter__ = enter
    runtime_cm.__exit__ = exit_
    runtime_cls = MagicMock(return_value=runtime_cm)

    kwargs = {
        "root": tmp_path,
        "task": _task(),
        "repo": tmp_path,
        "oracle_spec": {
            "name": "oracle",
            "replay_from_start": {"actions": [{"type": "key", "key": "ArrowLeft"}]},
            "checkpoint": {},
            "actions": [],
        },
        "oracle_spec_path": tmp_path / "oracle.yaml",
        "seed": 3,
        "headless": True,
        "cache_root": tmp_path / "cache",
    }
    results: list[dict[str, object]] = []

    def audit() -> None:
        concurrent.wait(timeout=10)
        results.append(audit_scripted_reference_replay(**kwargs))

    with (
        patch("warptest.state_loop.RepositoryInspector") as inspector_cls,
        patch("warptest.state_loop.profile_for", return_value=profile),
        patch("warptest.state_loop.BrowserRuntimeAdapter", runtime_cls),
        patch("warptest.state_loop.runtime_lease", return_value=nullcontext()),
    ):
        inspector_cls.return_value.inspect.return_value = _inspection()
        threads = [threading.Thread(target=audit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert len(results) == 2
    assert all(row["reference_reachable"] is True for row in results)
    assert peak == 1, "the shared replay must never run in two runtimes at once"
    runtime_cls.assert_called_once()
    assert sorted(bool(row["cache_hit"]) for row in results) == [False, True]


def test_missing_scripted_replay_is_cached_false_without_profile_execution(
    tmp_path: Path,
) -> None:
    profile = MagicMock()
    with (
        patch("warptest.state_loop.RepositoryInspector") as inspector_cls,
        patch("warptest.state_loop.profile_for", return_value=profile),
        patch("warptest.state_loop.BrowserRuntimeAdapter") as runtime_cls,
    ):
        inspector_cls.return_value.inspect.return_value = _inspection()
        evidence = audit_scripted_reference_replay(
            tmp_path,
            _task(),
            tmp_path,
            {"name": "oracle", "checkpoint": {}, "actions": []},
            tmp_path / "oracle.yaml",
            seed=1,
            headless=True,
            cache_root=tmp_path / "cache",
        )

    assert evidence["status"] == "unsupported"
    assert evidence["reference_reachable"] is False
    assert Path(evidence["evidence_file"]).exists()
    profile.strategy_plan.assert_not_called()
    runtime_cls.assert_not_called()


def test_native_desktop_sandbox_reference_replay_is_unsupported_not_inspection_error(
    tmp_path: Path,
) -> None:
    cases = (
        (
            "LibreOffice",
            (
                "configure.ac",
                "desktop/source/app/app.cxx",
                "include/LibreOfficeKit/LibreOfficeKit.h",
            ),
        ),
        (
            "Blender",
            (
                "source/creator/creator_args.cc",
                "scripts/startup/.keep",
            ),
        ),
    )
    for benchmark, marker_names in cases:
        repo = tmp_path / benchmark / "repo"
        for marker_name in marker_names:
            marker = repo / marker_name
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("", encoding="utf-8")

        evidence = audit_scripted_reference_replay(
            tmp_path,
            _task(benchmark=benchmark),
            repo,
            {
                "name": "oracle",
                "replay_from_start": {"actions": []},
                "checkpoint": {},
                "actions": [],
            },
            tmp_path / benchmark / "oracle.yaml",
            seed=0,
            headless=False,
            cache_root=tmp_path / "cache" / benchmark,
        )

        assert evidence["status"] == "unsupported"
        assert evidence["reference_reachable"] is False
        assert "repository inspection failed" not in evidence["reason"]


def test_reference_replay_validation_failure_is_cached_false(tmp_path: Path) -> None:
    profile = MagicMock()
    profile.strategy_plan.return_value = _plan()
    profile.run.return_value = _report(reached=False)
    runtime_cm = MagicMock()
    runtime_cm.__enter__.return_value = MagicMock()

    with (
        patch("warptest.state_loop.RepositoryInspector") as inspector_cls,
        patch("warptest.state_loop.profile_for", return_value=profile),
        patch("warptest.state_loop.BrowserRuntimeAdapter", return_value=runtime_cm),
        patch("warptest.state_loop.runtime_lease", return_value=nullcontext()),
    ):
        inspector_cls.return_value.inspect.return_value = _inspection()
        evidence = audit_scripted_reference_replay(
            tmp_path,
            _task(),
            tmp_path,
            {
                "name": "oracle",
                "replay_from_start": {"actions": []},
                "checkpoint": {},
                "actions": [],
            },
            tmp_path / "oracle.yaml",
            seed=1,
            headless=True,
            cache_root=tmp_path / "cache",
        )

    assert evidence["status"] == "failure"
    assert evidence["reference_reachable"] is False
    assert evidence["target_validated_before_smoke"] is False


def test_openra_gameplay_reference_replay_uses_live_start_adapter(
    tmp_path: Path,
) -> None:
    profile = MagicMock()
    profile.strategy_plan.return_value = _plan()
    runtime = MagicMock()
    runtime.grade_target.return_value = SimpleNamespace(
        ok=True,
        status="success",
        detail={"world_tick": 100, "checks": [{"status": "success"}]},
    )

    def screenshot(path: Path) -> str:
        path.write_bytes(b"openra-reference-frame")
        return str(path)

    runtime.screenshot.side_effect = screenshot
    runtime_cm = MagicMock()
    runtime_cm.__enter__.return_value = runtime
    runtime_cls = MagicMock(return_value=runtime_cm)
    spec = {
        "name": "OpenRA start",
        "target": {
            "kind": "openra_map",
            "mod": "ra",
            "map": "allies-08a",
            "actors": [{"id": "mcv", "type": "mcv", "owner": "Greece"}],
        },
        "replay_from_start": {
            "actions": [
                {"type": "openra_launch_map", "mod": "ra", "map": "allies-08a"}
            ]
        },
        "actions": [{"type": "openra_wait_ticks", "ticks": 100}],
        "assertions": [{"type": "no_openra_gameplay_probe_errors"}],
    }

    with (
        patch("warptest.state_loop.RepositoryInspector") as inspector_cls,
        patch("warptest.state_loop.profile_for", return_value=profile),
        patch("warptest.state_loop.OpenRAGameplaySession", runtime_cls),
        patch("warptest.state_loop.runtime_lease", return_value=nullcontext()),
        patch("warptest.state_loop.native_runtime_lock", return_value=nullcontext()),
    ):
        inspector_cls.return_value.inspect.return_value = _inspection(
            profile="OpenRAProfile"
        )
        evidence = audit_scripted_reference_replay(
            tmp_path,
            _task(benchmark="OpenRA"),
            tmp_path,
            spec,
            tmp_path / "openra.yaml",
            seed=5,
            headless=False,
            cache_root=tmp_path / "cache",
        )

    assert evidence["status"] == "success"
    assert evidence["reference_reachable"] is True
    assert evidence["reference_spec_mode"] == "clean_public_gameplay_start"
    assert evidence["metrics"]["world_tick"] == 100
    runtime_cls.assert_called_once()
    assert runtime_cls.call_args.kwargs["experiment_seed"] == 5
    passed_spec = runtime_cls.call_args.kwargs["spec"]
    assert passed_spec["actions"] == []
    assert passed_spec["assertions"] == []
    runtime.grade_target.assert_called_once()


def test_state_method_timing_excludes_bootstrap_cold_probe_and_grader() -> None:
    result = {
        "checkpoint_synthesis_ms": 50,
        "cold_restore_ms": 999,
        "session_bootstrap_ms": 888,
        "grader_ms": 777,
        "restore_timing": {
            "warm_restore_ms": 100,
            "fast_restore_ms": 10,
            "fast_restore_used": True,
        },
        "after_timing": {
            "warm_restore_ms": 80,
            "fast_restore_ms": 20,
            "fast_restore_used": True,
        },
    }

    _set_state_method_timing(result)

    assert result["time_to_start_ms"] == 100
    assert result["time_start_to_goal_ms"] == 20
    assert result["time_e2e_excluding_synthesis_ms"] == 120
    assert result["time_e2e_including_synthesis_ms"] == 170
    assert result["checkpoint_restore_ms"] == 100
    assert result["runtime_bootstrap_ms"] == 888
    assert result["grader_ms"] == 777


def test_luanti_state_arm_does_not_fall_back_to_public_scaffold(
    tmp_path: Path,
) -> None:
    scaffold = {
        "name": "hidden-scaffold",
        "benchmark": "luanti",
        "target": {},
        "checkpoint": {},
        "actions": [],
        "assertions": [],
    }
    agent = MagicMock()
    agent.synthesize.return_value = SimpleNamespace(
        generated_spec=scaffold,
        model_generated_spec=None,
        used_public_scaffold=True,
    )
    result = {"result": False, "iterations": []}

    with (
        patch("warptest.state_loop._measure_luanti_cold") as cold_measure,
        patch("warptest.state_loop.LuantiWarmSession") as session_cls,
    ):
        actual = _run_luanti_task(
            tmp_path,
            _task(benchmark="luanti"),
            agent,
            tmp_path,
            scaffold,
            tmp_path / "oracle.yaml",
            result,
            task_dir=tmp_path / "report",
            seed=11,
        )

    assert actual["status"] == "failure"
    assert actual["iterations"][0]["status"] == "synthesis_failure"
    assert actual["used_public_scaffold"] is True
    assert actual["injected_spec_source"] == "none"
    assert actual["time_to_start_ms"] == 0
    assert actual["time_start_to_goal_ms"] == 0
    assert actual["time_e2e_including_synthesis_ms"] == actual["checkpoint_synthesis_ms"]
    assert not (tmp_path / "report" / "v1" / "spec.yaml").exists()
    cold_measure.assert_not_called()
    session_cls.assert_not_called()


def test_luanti_state_timing_only_sums_synthesis_restore_and_action_path(
    tmp_path: Path,
) -> None:
    model_spec = {
        "name": "model-spec",
        "benchmark": "luanti",
        "target": {},
        "checkpoint": {},
        "actions": [{"type": "set_node"}],
        "assertions": [],
    }
    agent = MagicMock()
    agent.synthesize.return_value = SimpleNamespace(
        generated_spec=model_spec,
        model_generated_spec=model_spec,
        used_public_scaffold=False,
    )
    before_path = tmp_path / "before.png"
    after_path = tmp_path / "after.png"
    _write_informative_png(before_path, 40)
    _write_informative_png(after_path, 200)
    session = MagicMock()
    session.start.return_value = session
    session.bootstrap_ms = 900
    session.restore.side_effect = [
        SimpleNamespace(status="success", elapsed_ms=100, detail=""),
        SimpleNamespace(status="success", elapsed_ms=20, detail=""),
    ]
    session.fast_restore.return_value = SimpleNamespace(
        status="success",
        elapsed_ms=10,
    )
    session.screenshot.side_effect = lambda name: (
        str(before_path) if "before" in name else str(after_path)
    )
    goldens = SimpleNamespace(
        before="gold-before.png",
        after="gold-after.png",
        fingerprint="fp",
        quality={},
        after_description=None,
        after_rubric=None,
    )

    with (
        patch("warptest.state_loop._measure_luanti_cold", return_value=999),
        patch("warptest.state_loop.LuantiWarmSession", return_value=session),
        patch("warptest.state_loop._ensure_luanti_goldens", return_value=goldens),
        patch("warptest.state_loop.run_visual_pair_audit", return_value={"result": True}),
    ):
        actual = _run_luanti_task(
            tmp_path,
            _task(benchmark="luanti"),
            agent,
            tmp_path,
            model_spec,
            tmp_path / "oracle.yaml",
            {"result": False, "iterations": []},
            task_dir=tmp_path / "report",
            seed=11,
        )

    assert actual["status"] == "success"
    assert actual["time_to_start_ms"] == 100
    assert actual["time_start_to_goal_ms"] == 20
    assert actual["time_e2e_excluding_synthesis_ms"] == 120
    assert actual["time_e2e_including_synthesis_ms"] == (
        actual["checkpoint_synthesis_ms"] + 120
    )
    assert actual["termination_time_ms"] == actual["time_e2e_including_synthesis_ms"]
    assert actual["cold_restore_ms"] == 999
    assert actual["runtime_bootstrap_ms"] == 900
    assert actual["restore_timing"]["fast_restore_ms"] == 10
    assert actual["checkpoint_restore_ms"] == 100


def test_gui_reference_audit_preserves_false_and_fails_closed_on_unknown(
    tmp_path: Path,
) -> None:
    task = _task()
    common = {
        "base": tmp_path,
        "task": task,
        "repo": tmp_path,
        "oracle_spec": {"checkpoint": {}},
        "oracle_spec_path": tmp_path / "oracle.yaml",
        "seed": 4,
        "headless": True,
    }
    false_result: dict[str, object] = {}
    with patch.object(
        gui_agent,
        "audit_scripted_reference_replay",
        return_value={
            "status": "unsupported",
            "reference_reachable": False,
            "reason": "no replay",
            "evidence_file": "audit.json",
        },
    ):
        assert gui_agent._attach_reference_replay_audit(false_result, **common)
    assert false_result["reference_reachable"] is False
    assert false_result["reference_replay"]["evidence_file"] == "audit.json"

    unknown_result: dict[str, object] = {}
    with patch.object(
        gui_agent,
        "audit_scripted_reference_replay",
        return_value={
            "status": "error",
            "reference_reachable": None,
            "reason": "runtime unavailable",
        },
    ):
        assert not gui_agent._attach_reference_replay_audit(unknown_result, **common)
    assert unknown_result["incomplete"] is True
    assert unknown_result["status"] == "failed"
