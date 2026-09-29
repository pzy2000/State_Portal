from __future__ import annotations

import signal
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from warptest import gui_agent


def _task(benchmark: str = "jynew") -> SimpleNamespace:
    return SimpleNamespace(
        id=f"{benchmark}-c1-test",
        benchmark=benchmark,
        state_description="legacy public task",
        preconditions="reach the declared start",
        reference_steps="perform the public UI transition",
        goal_description="INDEPENDENT_SENTINEL_GOAL",
        horizon_class="mid",
        issue=SimpleNamespace(
            id=f"{benchmark}-c1-test",
            benchmark=benchmark,
            repo=f"games/{benchmark}",
        ),
        oracle_spec=f"specs/{benchmark}/c1-test.yaml",
    )


def _call_agent_task_dispatch(
    tmp_path: Path, task: SimpleNamespace
) -> dict[str, object]:
    return gui_agent._run_agent_task.__wrapped__(
        tmp_path,
        task,
        model="worker/model",
        base_url="https://example.invalid/v1",
        api_key="unused",
        api_key_env="TEST_KEY",
        synth_model="worker/model",
        max_explore_steps=3,
        temperature=0.0,
        out_dir=tmp_path / "report",
        max_steps=5,
        headless=True,
        start_mode="from_start",
        policy_kind="agent_s",
        policy_client=MagicMock(),
        observer=None,
        seed=17,
        shared_synthesis_cache=None,
    )


@pytest.mark.parametrize("benchmark", ["jynew", "PAL3"])
def test_unity_benchmarks_dispatch_before_generic_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, benchmark: str
) -> None:
    expected = {"branch": "unity", "benchmark": benchmark}
    branch = MagicMock(return_value=expected)
    monkeypatch.setattr(gui_agent, "_run_unity_c1_agent_task", branch)

    task = _task(benchmark)
    result = _call_agent_task_dispatch(tmp_path, task)

    assert result == expected
    assert branch.call_count == 1
    assert branch.call_args.args[:2] == (tmp_path, task)
    assert branch.call_args.kwargs["headless"] is True
    assert branch.call_args.kwargs["policy_kind"] == "agent_s"
    assert branch.call_args.kwargs["seed"] == 17


@pytest.mark.parametrize(
    ("override", "expected_ok", "expected_status"),
    [
        ({}, True, "success"),
        ({"live": False}, False, "validate_error"),
        ({"source": "synthetic_probe"}, False, "validate_error"),
        ({"checks": []}, False, "validate_error"),
        ({"checks": [{"status": "failure"}]}, False, "validate_error"),
        ({"ok": False, "status": "failure"}, False, "validate_error"),
        (
            {"ok": False, "status": "failure", "checks": [{"status": "failure"}]},
            False,
            "failure",
        ),
    ],
)
def test_unity_semantic_grade_requires_live_named_nonempty_success_checks(
    override: dict[str, object], expected_ok: bool, expected_status: str
) -> None:
    report: dict[str, object] = {
        "ok": True,
        "live": True,
        "status": "success",
        "source": "jynew_live_semantic_probe_v1",
        "transition_evidence_valid": True,
        "checks": [{"name": "target", "status": "success"}],
    }
    report.update(override)
    runtime = SimpleNamespace(
        benchmark="jynew",
        semantic_start=MagicMock(return_value=report),
        semantic_goal=MagicMock(return_value=report),
    )

    ok, status, detail = gui_agent._unity_semantic_grade(
        runtime, {"target": {}}, after=False
    )

    assert ok is expected_ok
    assert status == expected_status
    assert detail["source"] == report.get("source")
    runtime.semantic_start.assert_called_once_with({"target": {}})
    runtime.semantic_goal.assert_not_called()


def test_record_unity_seed_evidence_binds_initial_and_final_live_process() -> None:
    initial = {
        "observed": True,
        "source": "unity_live_process",
        "algorithm": "not_applicable_unity_gui",
        "seed": 23,
        "process_id": 91,
        "process_alive_at_observation": True,
        "sequence": 2,
    }
    final = {**initial, "sequence": 5}
    result: dict[str, object] = {"task_id": "jynew-save-restore"}
    runtime = SimpleNamespace(seed_evidence=MagicMock(return_value=final))

    returned = gui_agent._record_unity_seed_evidence(result, runtime, initial=initial)

    assert returned == initial
    assert result["runtime_started"] is True
    assert result["seed_evidence"] == {
        "schema_version": 1,
        "source": "unity_live_process",
        "observed": True,
        "algorithm": "not_applicable_unity_gui",
        "seed": 23,
        "task_id": "jynew-save-restore",
        "initial": initial,
        "final": final,
    }
    assert result["prng"] == final
    runtime.seed_evidence.assert_called_once_with("jynew-save-restore")


def test_generic_native_runtime_is_sigstopped_during_start_visual_grade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kill = MagicMock()
    monkeypatch.setattr(gui_agent.os, "kill", kill)
    metadata: dict[str, object] = {}

    with gui_agent._hold_runtime_state_for_start_grading(
        SimpleNamespace(hold_pid=301), metadata=metadata
    ) as held:
        assert held["required"] is True
        assert held["held"] is True
        assert held["backend"] == "sigstop"
        assert held["held_pid"] == 301
        assert held["released"] is False

    assert metadata["released"] is True
    assert kill.call_args_list == [
        call(301, signal.SIGSTOP),
        call(301, signal.SIGCONT),
    ]


class _FakeNative:
    def window_bounds(self) -> SimpleNamespace:
        return SimpleNamespace(
            to_dict=lambda: {"x": 20, "y": 30, "width": 1280, "height": 720}
        )


class _FakeUnitySession:
    benchmark = "jynew"
    hold_pid = 4321

    def __init__(self) -> None:
        self.native = _FakeNative()
        self.session_id = "fake-unity-session"
        self.width = 1280
        self.height = 720
        self.desktop_captures: list[Path] = []
        self.proof_captures: list[Path] = []
        self.restore_calls: list[tuple[dict[str, object], Path | None]] = []
        self.sequence = 0

    def __enter__(self) -> _FakeUnitySession:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def clean_entry(self) -> dict[str, object]:
        self.sequence += 1
        return {
            "status": "success",
            "phase": "clean_entry",
            "actions_executed": False,
        }

    def calibration_metadata(self) -> dict[str, object]:
        return {
            "mode": "background_pixel",
            "pid": self.hold_pid,
            "frame_width": self.width,
            "frame_height": self.height,
            "protocol_version": "warptest-c1-unity-v3",
            "screenshot_source": "unity_gameview_capture",
            "input_backend": "unity_editor_queue_gameview_input_v1",
            "foreground_required": False,
        }

    def screenshot(self, path: str | Path) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"desktop-{len(self.desktop_captures)}".encode())
        self.desktop_captures.append(target)
        return str(target)

    def unity_screenshot(self, path: str | Path) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"proof-{len(self.proof_captures)}".encode())
        self.proof_captures.append(target)
        self.sequence += 1
        return str(target)

    def restore_target_only(
        self, spec: dict[str, object], spec_path: Path | None
    ) -> dict[str, object]:
        self.restore_calls.append((spec, spec_path))
        self.sequence += 1
        return {
            "status": "success",
            "phase": "target_only",
            "actions_executed": False,
        }

    def _semantic(self) -> dict[str, object]:
        self.sequence += 1
        return {
            "ok": True,
            "live": True,
            "status": "success",
            "source": "jynew_live_semantic_probe_v1",
            "transition_evidence_valid": True,
            "checks": [{"name": "live-check", "status": "success"}],
        }

    def semantic_start(self, spec: dict[str, object]) -> dict[str, object]:
        return self._semantic()

    def semantic_goal(self, spec: dict[str, object]) -> dict[str, object]:
        return self._semantic()

    def seed_evidence(self, task_id: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "observed": True,
            "source": "unity_live_process",
            "algorithm": "not_applicable_unity_gui",
            "seed": 29,
            "process_id": self.hold_pid,
            "process_alive_at_observation": True,
            "benchmark": self.benchmark,
            "task_id": task_id,
            "protocol_version": "warptest-c1-unity-v3",
            "sequence": self.sequence,
            "semantic_fingerprint": {
                "source": "jynew_live_semantic_probe_v1",
                "status": "success",
            },
        }


def test_unity_process_evidence_survives_early_clean_entry_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task("jynew")
    runtime = _FakeUnitySession()
    runtime.clean_entry = MagicMock(
        return_value={"status": "failure", "actions_executed": False}
    )
    goldens = SimpleNamespace(
        before="gold-before.png",
        after="gold-after.png",
        after_description=None,
        after_rubric=None,
    )
    monkeypatch.setattr(gui_agent, "resolve_task_repo", lambda *_: tmp_path)
    monkeypatch.setattr(
        gui_agent,
        "load_spec",
        lambda *_: {
            "checkpoint": {"expected_validity": "transition-valid"},
            "target": {"player_level": 5},
            "assertions": [{"type": "goal_assertion"}],
        },
    )
    monkeypatch.setattr(
        gui_agent, "_attach_reference_replay_audit", lambda *_, **__: True
    )
    monkeypatch.setattr(gui_agent, "lookup_cached_oracle_goldens", lambda *_: goldens)
    monkeypatch.setattr(
        gui_agent, "_oracle_goldens_payload", lambda *_: {"cache_hit": True}
    )
    monkeypatch.setattr(gui_agent, "native_runtime_lock", nullcontext)
    monkeypatch.setattr(
        gui_agent, "create_c1_unity_session", MagicMock(return_value=runtime)
    )

    result = gui_agent._run_unity_c1_agent_task(
        tmp_path,
        task,
        model="worker/model",
        base_url="https://example.invalid/v1",
        api_key="unused",
        api_key_env="TEST_KEY",
        synth_model="worker/model",
        max_explore_steps=3,
        temperature=0.0,
        out_dir=tmp_path / "report",
        max_steps=5,
        headless=False,
        start_mode="from_start",
        policy_kind="agent_s",
        policy_client=SimpleNamespace(metadata={"native_exec_enabled": True}),
        observer=None,
        seed=29,
    )

    assert result["status"] == "failed"
    assert result["incomplete"] is True
    assert result["runtime_started"] is True
    assert result["seed_evidence"]["source"] == "unity_live_process"
    assert result["seed_evidence"]["initial"]["process_id"] == runtime.hold_pid
    assert "clean entry failed closed" in result["reason"]


def test_synthesized_unity_c1_uses_target_only_restore_and_phase_b_pixels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task("jynew")
    generated_spec: dict[str, object] = {
        "target": {"player_level": 5},
        "actions": [{"type": "forbidden_setup_action"}],
        "assertions": [{"type": "goal_assertion"}],
        "experiment_seed": 29,
    }
    oracle_spec = {
        "checkpoint": {"expected_validity": "transition-valid"},
        "target": {"player_level": 5},
        "assertions": [{"type": "goal_assertion"}],
    }
    goldens = SimpleNamespace(
        before="gold-before.png",
        after="gold-after.png",
        after_description=None,
        after_rubric=None,
    )
    runtime = _FakeUnitySession()
    factory = MagicMock(return_value=runtime)
    golden_builder = MagicMock(return_value=goldens)
    browser_state_session = MagicMock(
        side_effect=AssertionError("Unity cache miss must not use StateSession")
    )
    phase_b_path = tmp_path / "phase-b-final.png"
    phase_b_path.write_bytes(b"phase-b")
    run_phase = MagicMock(
        return_value=gui_agent.PhaseResult(
            reached=False,
            steps=3,
            screenshot=str(phase_b_path),
            actions=[{"step": 1, "kind": "click"}],
            elapsed_ms=37,
            grader_ms=4,
        )
    )
    hold = {
        "required": True,
        "held": True,
        "released": True,
        "backend": "sigstop",
        "held_pid": runtime.hold_pid,
    }

    def apply_synthesis(
        result: dict[str, object], *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        result.update(
            synth_status="generated",
            checkpoint_synthesis_ms=11,
            start_construction_status="pending",
        )
        return generated_spec

    monkeypatch.setattr(gui_agent, "resolve_task_repo", lambda *_: tmp_path)
    monkeypatch.setattr(gui_agent, "load_spec", lambda *_: oracle_spec)
    monkeypatch.setattr(
        gui_agent, "_attach_reference_replay_audit", lambda *_, **__: True
    )
    monkeypatch.setattr(
        gui_agent, "_obtain_self_synthesis", lambda *_, **__: {"status": "success"}
    )
    monkeypatch.setattr(gui_agent, "_apply_self_synthesis_result", apply_synthesis)
    monkeypatch.setattr(gui_agent, "lookup_cached_oracle_goldens", lambda *_: None)
    monkeypatch.setattr(gui_agent, "_ensure_unity_checkpoint_goldens", golden_builder)
    monkeypatch.setattr(gui_agent, "StateSession", browser_state_session)
    monkeypatch.setattr(
        gui_agent, "_oracle_goldens_payload", lambda *_: {"cache_hit": True}
    )
    monkeypatch.setattr(gui_agent, "native_runtime_lock", nullcontext)
    monkeypatch.setattr(gui_agent, "create_c1_unity_session", factory)
    monkeypatch.setattr(gui_agent, "_run_phase", run_phase)
    monkeypatch.setattr(
        gui_agent,
        "_run_before_visual_gate",
        lambda **_: (
            {
                "before": {"label": "correct"},
                "after": {"label": "not_evaluated"},
            },
            hold,
        ),
    )
    monkeypatch.setattr(
        gui_agent,
        "_run_after_visual_grade",
        lambda **_: {
            "before": {"label": "correct"},
            "after": {"label": "correct"},
        },
    )

    result = gui_agent._run_unity_c1_agent_task(
        tmp_path,
        task,
        model="worker/model",
        base_url="https://example.invalid/v1",
        api_key="unused",
        api_key_env="TEST_KEY",
        synth_model="worker/model",
        max_explore_steps=3,
        temperature=0.0,
        out_dir=tmp_path / "report",
        max_steps=5,
        headless=False,
        start_mode="synthesized",
        policy_kind="agent_s",
        policy_client=SimpleNamespace(metadata={"native_exec_enabled": True}),
        observer=None,
        seed=29,
    )

    assert result["status"] == "success"
    assert result["start_construction_status"] == "reached"
    assert result["steps"] == {"phase_a": 0, "phase_b": 3, "total": 3}
    assert result["policy_received_semantic_probe"] is False
    assert result["start_grading_runtime_hold"] == hold
    assert result["seed_evidence"]["source"] == "unity_live_process"
    assert result["seed_evidence"]["initial"]["process_id"] == runtime.hold_pid
    assert result["seed_evidence"]["final"]["process_id"] == runtime.hold_pid
    assert result["calibration"]["screenshot_source"] == "unity_gameview_capture"
    assert result["calibration"]["backend"] == "unity_background_pixel_v1"
    assert len(runtime.desktop_captures) == 2
    assert len(runtime.proof_captures) == 2
    assert result["initial_state_screenshot_hash"] == gui_agent._screenshot_fingerprint(
        runtime.desktop_captures[1]
    )
    assert len(runtime.restore_calls) == 1
    restored_spec, restored_path = runtime.restore_calls[0]
    assert restored_spec is generated_spec
    assert restored_path == Path(result["synthesized_spec"])
    run_phase.assert_called_once()
    assert run_phase.call_args.kwargs["runtime"] is runtime
    assert run_phase.call_args.kwargs["max_steps"] == 5
    assert "perform the public UI transition" in run_phase.call_args.kwargs["instruction"]
    assert "INDEPENDENT_SENTINEL_GOAL" not in run_phase.call_args.kwargs["instruction"]
    factory.assert_called_once_with(
        "jynew",
        tmp_path,
        tmp_path / "report" / "tasks" / task.id / "agent",
        experiment_seed=29,
    )
    golden_builder.assert_called_once_with(
        tmp_path,
        {**oracle_spec, "experiment_seed": 29},
        (tmp_path / task.oracle_spec).resolve(),
        tmp_path / "report" / "tasks" / task.id / "goldens",
        benchmark="jynew",
        headless=False,
    )
    browser_state_session.assert_not_called()


@pytest.mark.parametrize("restore_status", ["rejected", "engine_error"])
def test_synthesized_unity_restore_failure_is_counted_not_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_status: str,
) -> None:
    task = _task("jynew")
    generated_spec: dict[str, object] = {
        "target": {"player_level": 5},
        "actions": [],
        "assertions": [],
        "experiment_seed": 29,
    }
    oracle_spec = {
        "checkpoint": {"expected_validity": "transition-valid"},
        "target": {"player_level": 5},
        "assertions": [{"type": "goal_assertion"}],
    }
    goldens = SimpleNamespace(
        before="gold-before.png",
        after="gold-after.png",
        after_description=None,
        after_rubric=None,
    )
    runtime = _FakeUnitySession()
    monkeypatch.setattr(
        runtime,
        "restore_target_only",
        lambda *_: {
            "status": restore_status,
            "detail": "candidate target could not be constructed",
            "phase": "target_only",
            "actions_executed": False,
        },
    )

    def apply_synthesis(
        result: dict[str, object], *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        result.update(
            synth_status="generated",
            checkpoint_synthesis_ms=11,
            start_construction_status="pending",
        )
        return generated_spec

    monkeypatch.setattr(gui_agent, "resolve_task_repo", lambda *_: tmp_path)
    monkeypatch.setattr(gui_agent, "load_spec", lambda *_: oracle_spec)
    monkeypatch.setattr(
        gui_agent, "_attach_reference_replay_audit", lambda *_, **__: True
    )
    monkeypatch.setattr(
        gui_agent, "_obtain_self_synthesis", lambda *_, **__: {"status": "success"}
    )
    monkeypatch.setattr(gui_agent, "_apply_self_synthesis_result", apply_synthesis)
    monkeypatch.setattr(gui_agent, "lookup_cached_oracle_goldens", lambda *_: goldens)
    monkeypatch.setattr(
        gui_agent, "_oracle_goldens_payload", lambda *_: {"cache_hit": True}
    )
    monkeypatch.setattr(gui_agent, "native_runtime_lock", nullcontext)
    monkeypatch.setattr(
        gui_agent, "create_c1_unity_session", MagicMock(return_value=runtime)
    )

    result = gui_agent._run_unity_c1_agent_task(
        tmp_path,
        task,
        model="worker/model",
        base_url="https://example.invalid/v1",
        api_key="unused",
        api_key_env="TEST_KEY",
        synth_model="worker/model",
        max_explore_steps=3,
        temperature=0.0,
        out_dir=tmp_path / "report",
        max_steps=5,
        headless=False,
        start_mode="synthesized",
        policy_kind="agent_s",
        policy_client=SimpleNamespace(metadata={"native_exec_enabled": True}),
        observer=None,
        seed=29,
    )

    assert result["status"] == "failure"
    assert result["incomplete"] is False
    assert result["restore_status"] == restore_status
    assert result["start_construction_status"] == "restore_failed"
    assert "did not restore target-only" in result["reason"]


def test_from_start_unity_c1_runs_both_phases_with_one_total_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task("jynew")
    oracle_spec = {
        "checkpoint": {"expected_validity": "transition-valid"},
        "target": {"player_level": 5},
        "assertions": [{"type": "goal_assertion"}],
    }
    goldens = SimpleNamespace(
        before="gold-before.png",
        after="gold-after.png",
        after_description=None,
        after_rubric=None,
    )
    runtime = _FakeUnitySession()
    before = tmp_path / "phase-a-final.png"
    after = tmp_path / "phase-b-final.png"
    before.write_bytes(b"phase-a")
    after.write_bytes(b"phase-b")
    run_phase = MagicMock(
        side_effect=[
            gui_agent.PhaseResult(
                reached=False,
                steps=2,
                screenshot=str(before),
                actions=[{"step": 1, "kind": "click"}],
                elapsed_ms=20,
            ),
            gui_agent.PhaseResult(
                reached=False,
                steps=3,
                screenshot=str(after),
                actions=[{"step": 1, "kind": "click"}],
                elapsed_ms=30,
            ),
        ]
    )
    hold = {
        "required": True,
        "held": True,
        "released": True,
        "backend": "sigstop",
        "held_pid": runtime.hold_pid,
    }
    monkeypatch.setattr(gui_agent, "resolve_task_repo", lambda *_: tmp_path)
    monkeypatch.setattr(gui_agent, "load_spec", lambda *_: oracle_spec)
    monkeypatch.setattr(
        gui_agent, "_attach_reference_replay_audit", lambda *_, **__: True
    )
    monkeypatch.setattr(gui_agent, "lookup_cached_oracle_goldens", lambda *_: goldens)
    monkeypatch.setattr(
        gui_agent, "_oracle_goldens_payload", lambda *_: {"cache_hit": True}
    )
    monkeypatch.setattr(gui_agent, "native_runtime_lock", nullcontext)
    monkeypatch.setattr(
        gui_agent, "create_c1_unity_session", MagicMock(return_value=runtime)
    )
    monkeypatch.setattr(gui_agent, "_run_phase", run_phase)
    monkeypatch.setattr(
        gui_agent,
        "_run_before_visual_gate",
        lambda **_: (
            {
                "before": {"label": "correct"},
                "after": {"label": "not_evaluated"},
            },
            hold,
        ),
    )
    monkeypatch.setattr(
        gui_agent,
        "_run_after_visual_grade",
        lambda **_: {
            "before": {"label": "correct"},
            "after": {"label": "correct"},
        },
    )

    result = gui_agent._run_unity_c1_agent_task(
        tmp_path,
        task,
        model="worker/model",
        base_url="https://example.invalid/v1",
        api_key="unused",
        api_key_env="TEST_KEY",
        synth_model="worker/model",
        max_explore_steps=3,
        temperature=0.0,
        out_dir=tmp_path / "report",
        max_steps=5,
        headless=True,
        start_mode="from_start",
        policy_kind="agent_s",
        policy_client=SimpleNamespace(metadata={"native_exec_enabled": True}),
        observer=None,
        seed=29,
    )

    assert result["status"] == "success"
    assert result["steps"] == {"phase_a": 2, "phase_b": 3, "total": 5}
    assert result["budget_exhausted"] is True
    assert result["checkpoint_restore_ms"] == 0
    assert runtime.restore_calls == []
    assert [entry.kwargs["max_steps"] for entry in run_phase.call_args_list] == [
        5,
        3,
    ]
    assert "Satisfy these preconditions" in run_phase.call_args_list[0].kwargs["instruction"]
    assert "perform the public UI transition" not in run_phase.call_args_list[0].kwargs["instruction"]
    assert "perform the public UI transition" in run_phase.call_args_list[1].kwargs["instruction"]
    assert "reach the declared start" not in run_phase.call_args_list[1].kwargs["instruction"]
    assert "INDEPENDENT_SENTINEL_GOAL" not in "".join(
        call.kwargs["instruction"] for call in run_phase.call_args_list
    )
    assert len(runtime.desktop_captures) == 1
    assert result["initial_state_screenshot_hash"] == gui_agent._screenshot_fingerprint(
        runtime.desktop_captures[0]
    )
