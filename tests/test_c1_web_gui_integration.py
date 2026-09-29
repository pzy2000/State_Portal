from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from warptest import gui_agent, p5_aggregate
from warptest.c1_web_runtime import (
    C1WebBindingResult,
    C1WebRestoreResult,
    C1WebSemanticGrade,
)


def _task(benchmark: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"{benchmark.lower()}-c1-test",
        benchmark=benchmark,
        state_description="public state task",
        preconditions="reach the declared live start",
        reference_steps="perform the declared visible transition",
        goal_description="INDEPENDENT_SENTINEL_GOAL",
        horizon_class="mid",
        issue=SimpleNamespace(
            id=f"{benchmark.lower()}-c1-test",
            benchmark=benchmark,
            repo=f"games/{benchmark.lower()}",
        ),
        oracle_spec=f"specs/{benchmark.lower()}/c1-test.yaml",
    )


def _grade(benchmark: str, value: int) -> C1WebSemanticGrade:
    source = "jupyterlab_contents_kernel_live_v1"
    observation = {"value": value, "ui": {"url": "http://127.0.0.1/live"}}
    return C1WebSemanticGrade(
        ok=True,
        status="success",
        source=source,
        detail="live",
        observation=observation,
        fingerprint=gui_agent._c1_web_semantic_digest(observation),
    )


class _FakeWebSession:
    def __init__(self, benchmark: str) -> None:
        self.benchmark = benchmark
        self.app_url = "http://127.0.0.1:9999/live"
        self.calls: list[str] = []
        self.transition_calls = 0
        self.runtime = SimpleNamespace(headless=True)
        self.page = SimpleNamespace()

    def __enter__(self):
        self.calls.append("start")
        return self

    def __exit__(self, *_exc):
        self.calls.append("close")

    def clean_entry(self) -> C1WebRestoreResult:
        self.calls.append("clean")
        return C1WebRestoreResult(
            benchmark=self.benchmark,
            phase="clean_entry",
            app_url=self.app_url,
            detail="clean",
            artifacts={},
            observation={"live": True},
        )

    def bind_live_target_for_grading(self, _spec, _spec_path):
        self.calls.append("bind")
        return C1WebBindingResult(
            benchmark=self.benchmark,
            app_url=self.app_url,
            detail="bound",
            artifacts={"attachment_path": "/tmp/public-input.png"},
            observation={"live": True},
        )

    def restore_target_only(self, _spec, _spec_path):
        self.calls.append("restore")
        return C1WebRestoreResult(
            benchmark=self.benchmark,
            phase="target_only",
            app_url=self.app_url,
            detail="restored",
            artifacts={"attachment_path": "/tmp/public-input.png"},
            observation={"live": True},
        )

    def semantic_start(self, _spec):
        self.calls.append("semantic_start")
        return _grade(self.benchmark, 1)

    def semantic_goal(self, _spec):
        self.calls.append("semantic_goal")
        return _grade(self.benchmark, 2)

    def apply_registered_transition(self, _spec):
        self.transition_calls += 1
        raise AssertionError("Agent-S must never call the registered State transition")


def _native_policy() -> SimpleNamespace:
    return SimpleNamespace(
        metadata={
            "runtime_mode": "background_pixel",
            "execution_backend": "background_pixel_v1",
            "native_exec_enabled": False,
            "worker_observation_mode": "screenshot",
            "raw_screenshot_observation": True,
            "action_execution_owner": "parent_runtime",
            "foreground_required": False,
            "coordinate_space": "frame_pixels_top_left_v1",
            "local_code_enabled": False,
            "native_screen_width": 1280,
            "native_screen_height": 800,
            "native_click_width": 1280,
            "native_click_height": 800,
        }
    )


def test_shared_agent_s_surface_rebinds_from_native_720_to_web_800(
    tmp_path: Path,
) -> None:
    screenshot = tmp_path / "web-frame.png"
    Image.new("RGB", (1280, 800), color=(20, 30, 40)).save(screenshot)
    policy = _native_policy()
    policy.metadata["native_screen_height"] = 720
    policy.metadata["native_click_height"] = 720
    calls: list[tuple[int, int]] = []

    def configure_surface(*, width: int, height: int) -> dict[str, object]:
        calls.append((width, height))
        policy.metadata.update(
            native_screen_width=width,
            native_screen_height=height,
            native_click_width=width,
            native_click_height=height,
        )
        return dict(policy.metadata)

    policy.configure_surface = configure_surface

    metadata = gui_agent._bind_c1_web_policy_surface(policy, screenshot)

    assert calls == [(1280, 800)]
    assert metadata["native_screen_height"] == 800
    assert metadata["native_click_height"] == 800


def _patch_web_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runtime: _FakeWebSession,
    *,
    phases: list[gui_agent.PhaseResult],
) -> None:
    before = tmp_path / "gold-before.png"
    after = tmp_path / "gold-after.png"
    initial = tmp_path / "initial.png"
    boundary = tmp_path / "boundary.png"
    goal = tmp_path / "goal.png"
    for path in (before, after, initial, boundary, goal):
        Image.new("RGB", (1280, 800), color=(20, 30, 40)).save(path)
    goldens = SimpleNamespace(
        before=str(before),
        after=str(after),
        after_description=None,
        after_rubric=None,
    )
    monkeypatch.setattr(gui_agent, "resolve_task_repo", lambda *_: tmp_path)
    monkeypatch.setattr(
        gui_agent,
        "load_spec",
        lambda *_: {
            "benchmark": runtime.benchmark,
            "checkpoint": {"expected_validity": "save-restorable"},
            "target": {"kind": "text_file"},
            "actions": [],
            "assertions": [{"type": "live_goal"}],
            "c1": {"reference_steps": "perform exact public input"},
        },
    )
    monkeypatch.setattr(
        gui_agent, "_attach_reference_replay_audit", lambda *_, **__: True
    )
    monkeypatch.setattr(
        gui_agent, "_ensure_c1_web_goldens", lambda *_, **__: goldens
    )
    monkeypatch.setattr(
        gui_agent, "_oracle_goldens_payload", lambda *_: {"cache_hit": True}
    )
    monkeypatch.setattr(
        gui_agent, "create_c1_web_session", lambda *_, **__: runtime
    )
    monkeypatch.setattr(gui_agent.native_window, "focus_runtime", lambda *_: None)
    captures = iter((str(initial), str(boundary), str(goal)))
    monkeypatch.setattr(gui_agent, "_capture_runtime", lambda *_, **__: next(captures))
    monkeypatch.setattr(
        gui_agent,
        "_calibrate_c1_web_agent_runtime",
        lambda *_, **__: {
            "passed": True,
            "backend": "playwright_headless_page_v1",
            "screenshot": {"width": 1280, "height": 800},
            "click_space": {"width": 1280, "height": 800},
        },
    )

    def record(result, _runtime, **kwargs):
        snapshot = {
            "observed": True,
            "process_id": 7,
            "screen": {"width": 1440, "height": 900},
            "algorithm": "not_applicable_web_gui",
            "seed": kwargs["seed"],
        }
        first = dict(kwargs.get("initial") or snapshot)
        result["runtime_started"] = True
        result["seed_evidence"] = {
            "schema_version": 1,
            "source": "c1_web_background_pixel",
            "observed": True,
            "algorithm": "not_applicable_web_gui",
            "seed": kwargs["seed"],
            "initial": first,
            "final": snapshot,
        }
        result["prng"] = snapshot
        return first

    monkeypatch.setattr(gui_agent, "_record_c1_web_seed_evidence", record)
    monkeypatch.setattr(gui_agent, "_run_phase", MagicMock(side_effect=phases))
    before_verdict = {
        "before": {"label": "correct"},
        "after": {"label": "not_evaluated"},
    }
    monkeypatch.setattr(
        gui_agent,
        "_run_before_visual_gate",
        lambda **_: (
            before_verdict,
            {
                "required": True,
                "held": True,
                "released": True,
                "backend": "chromium_cdp_script_pause",
            },
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


def _run(
    tmp_path: Path,
    task: SimpleNamespace,
    *,
    start_mode: str,
    policy: object | None = None,
):
    return gui_agent._run_web_c1_agent_task(
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
        start_mode=start_mode,
        policy_kind="agent_s",
        policy_client=policy or _native_policy(),
        observer=None,
        seed=17,
    )


@pytest.mark.parametrize("benchmark", ["JupyterLab"])
def test_web_benchmarks_dispatch_before_generic_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, benchmark: str
) -> None:
    branch = MagicMock(return_value={"branch": "web"})
    monkeypatch.setattr(gui_agent, "_run_web_c1_agent_task", branch)
    task = _task(benchmark)

    result = gui_agent._run_agent_task.__wrapped__(
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
        policy_client=_native_policy(),
        observer=None,
        seed=17,
    )

    assert result == {"branch": "web"}
    assert branch.call_args.args[:2] == (tmp_path, task)


def test_from_start_web_agent_s_binds_then_runs_both_pixel_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task("JupyterLab")
    runtime = _FakeWebSession("JupyterLab")
    phase_a = gui_agent.PhaseResult(
        reached=True,
        steps=2,
        screenshot=str(tmp_path / "boundary.png"),
        actions=[{"kind": "click"}],
        elapsed_ms=20,
    )
    phase_b = gui_agent.PhaseResult(
        reached=True,
        steps=3,
        screenshot=str(tmp_path / "goal.png"),
        actions=[{"kind": "type"}],
        elapsed_ms=30,
    )
    _patch_web_runner(monkeypatch, tmp_path, runtime, phases=[phase_a, phase_b])

    result = _run(tmp_path, task, start_mode="from_start")

    assert result["status"] == "success"
    assert result["e2e_success"] is True
    assert result["steps"] == {"phase_a": 2, "phase_b": 3, "total": 5}
    assert runtime.calls.index("bind") < runtime.calls.index("semantic_start")
    assert runtime.transition_calls == 0
    run_phase = gui_agent._run_phase
    assert run_phase.call_count == 2  # type: ignore[attr-defined]
    phase_a_prompt = run_phase.call_args_list[0].kwargs["instruction"]  # type: ignore[attr-defined]
    phase_b_prompt = run_phase.call_args_list[1].kwargs["instruction"]  # type: ignore[attr-defined]
    assert "reach the declared live start" in phase_a_prompt
    assert "perform the declared visible transition" not in phase_a_prompt
    assert "reach the declared live start" not in phase_b_prompt
    assert "INDEPENDENT_SENTINEL_GOAL" not in phase_a_prompt + phase_b_prompt
    assert "/tmp/public-input.png" in phase_b_prompt
    assert result["scripted_transition_executed"] is False
    assert result["policy_observation_source"] == "playwright_page_pixels"


def test_synthesized_web_agent_s_restores_target_only_and_runs_phase_b_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task("JupyterLab")
    runtime = _FakeWebSession("JupyterLab")
    phase_b = gui_agent.PhaseResult(
        reached=True,
        steps=2,
        screenshot=str(tmp_path / "goal.png"),
        actions=[{"kind": "key"}],
        elapsed_ms=25,
    )
    _patch_web_runner(monkeypatch, tmp_path, runtime, phases=[phase_b])
    generated = {
        "benchmark": "JupyterLab",
        "target": {"kind": "text_file"},
        "actions": [{"type": "must_not_execute"}],
        "assertions": [{"type": "live_goal"}],
    }

    def apply_synthesis(result, *_args, **_kwargs):
        result["checkpoint_synthesis_ms"] = 7
        return generated

    monkeypatch.setattr(gui_agent, "_obtain_self_synthesis", lambda *_, **__: {})
    monkeypatch.setattr(gui_agent, "_apply_self_synthesis_result", apply_synthesis)

    result = _run(tmp_path, task, start_mode="synthesized")

    assert result["status"] == "success"
    assert result["steps"] == {"phase_a": 0, "phase_b": 2, "total": 2}
    assert result["restore_phase"] == "target_only"
    assert result["restore_actions_executed"] is False
    assert "bind" not in runtime.calls
    assert runtime.transition_calls == 0
    assert gui_agent._run_phase.call_count == 1  # type: ignore[attr-defined]


def test_web_semantic_grade_rejects_forged_fingerprint() -> None:
    runtime = _FakeWebSession("JupyterLab")
    forged = _grade("JupyterLab", 1)
    runtime.semantic_start = MagicMock(
        return_value=C1WebSemanticGrade(
            **{**forged.to_dict(), "fingerprint": "0" * 64}
        )
    )

    ok, status, detail = gui_agent._c1_web_semantic_grade(
        runtime, {"target": {}}, after=False
    )

    assert ok is False
    assert status == "validate_error"
    assert detail["provenance_valid"] is False


def test_web_runner_rejects_non_native_policy_before_any_pixel_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task("JupyterLab")
    runtime = _FakeWebSession("JupyterLab")
    _patch_web_runner(monkeypatch, tmp_path, runtime, phases=[])
    policy = SimpleNamespace(metadata={"native_exec_enabled": False})

    result = _run(tmp_path, task, start_mode="from_start", policy=policy)

    assert result["status"] == "failed"
    assert result["incomplete"] is True
    assert "background-pixel Agent-S metadata" in result["reason"]
    assert gui_agent._run_phase.call_count == 0  # type: ignore[attr-defined]
    assert runtime.transition_calls == 0


def _web_seed_evidence(
    *,
    benchmark: str = "JupyterLab",
    task_id: str = "task",
    seed: int = 17,
    value: int = 1,
    ui_url: str = "http://127.0.0.1:9999/live",
) -> dict[str, object]:
    source = "jupyterlab_contents_kernel_live_v1"
    observation = {"value": value, "ui": {"url": ui_url}}
    semantic = {
        "source": source,
        "status": "success",
        "ok": True,
        "observation": observation,
        "fingerprint": gui_agent._c1_web_semantic_digest(observation),
        "stable_fingerprint": gui_agent._c1_web_semantic_digest(
            gui_agent._c1_web_stable_semantic(observation)
        ),
    }
    screen = {"width": 1280, "height": 800}
    snapshot = {
        "schema_version": 1,
        "source": "c1_web_background_pixel_snapshot_v1",
        "observed": True,
        "algorithm": "not_applicable_web_gui",
        "seed": seed,
        "benchmark": benchmark,
        "task_id": task_id,
        "headless": True,
        "foreground_required": False,
        "execution_backend": "background_pixel_v1",
        "coordinate_space": "frame_pixels_top_left_v1",
        "capture_backend": "playwright_headless_page_v1",
        "process_id": 701,
        "process_alive_at_observation": True,
        "app_url": "http://127.0.0.1:9999/live",
        "page_url": ui_url,
        "viewport": {"width": 1280, "height": 800},
        "device_scale_factor": 1,
        "screen": screen,
        "calibration": {
            "passed": True,
            "backend": "playwright_headless_page_v1",
            "page_visible": True,
            "screenshot": screen,
            "click_space": screen,
        },
        "semantic_fingerprint": semantic,
    }
    return {
        "schema_version": 1,
        "source": "c1_web_background_pixel",
        "observed": True,
        "algorithm": "not_applicable_web_gui",
        "seed": seed,
        "benchmark": benchmark,
        "task_id": task_id,
        "initial": snapshot,
        "final": {**snapshot},
    }


def test_web_seed_snapshot_records_headless_background_pixel_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _FakeWebSession("JupyterLab")
    process = SimpleNamespace(pid=701, poll=lambda: None)
    monkeypatch.setattr(gui_agent, "_c1_web_process", lambda _runtime: process)
    monkeypatch.setattr(
        gui_agent,
        "_c1_web_page_metrics",
        lambda _runtime: {
            "document_visibility": "visible",
            "body_present": True,
            "page_url": runtime.app_url,
            "viewport": {"width": 1280, "height": 800},
            "device_scale_factor": 1,
            "window": {"outer_width": 1280, "outer_height": 800},
        },
    )
    screen = {"width": 1280, "height": 800}

    snapshot = gui_agent._c1_web_seed_snapshot(
        runtime,
        task=_task("JupyterLab"),
        seed=17,
        calibration={
            "passed": True,
            "backend": "playwright_headless_page_v1",
            "screenshot": screen,
            "click_space": screen,
        },
        semantic_detail=None,
    )

    assert snapshot["observed"] is True
    assert snapshot["headless"] is True
    assert snapshot["foreground_required"] is False
    assert snapshot["execution_backend"] == "background_pixel_v1"
    assert snapshot["calibration"]["click_space"] == screen


@pytest.mark.parametrize("benchmark", ["JupyterLab"])
def test_p5_accepts_valid_background_web_seed_provenance(benchmark: str) -> None:
    evidence = _web_seed_evidence(benchmark=benchmark)

    issues = p5_aggregate._runtime_seed_evidence_issues(
        evidence,
        benchmark=benchmark,
        seed=17,
        task_id="task",
        expected_semantic_stage="entry",
        require_final=True,
    )

    assert issues == []


def test_p5_web_seed_provenance_fails_closed_on_tampering() -> None:
    evidence = _web_seed_evidence()
    evidence["initial"]["semantic_fingerprint"]["observation"]["value"] = 9  # type: ignore[index]
    evidence["final"]["headless"] = False  # type: ignore[index]
    evidence["final"]["calibration"]["click_space"] = {  # type: ignore[index]
        "width": 1,
        "height": 1,
    }

    issues = p5_aggregate._runtime_seed_evidence_issues(
        evidence,
        benchmark="JupyterLab",
        seed=17,
        task_id="task",
        expected_semantic_stage="entry",
        require_final=True,
    )

    assert any("semantic fingerprint does not match observation" in issue for issue in issues)
    assert any("seed snapshot is not headless" in issue for issue in issues)
    assert any("screenshot/click calibration" in issue for issue in issues)


def test_p5_web_pairing_uses_stable_semantics_not_ephemeral_page_url() -> None:
    a = {
        "seed_evidence": _web_seed_evidence(ui_url="http://127.0.0.1:1001/live"),
        "runtime_started": True,
    }
    c = {
        "seed_evidence": _web_seed_evidence(ui_url="http://127.0.0.1:1002/live"),
        "runtime_started": True,
    }
    indices = {
        "c1_gui": {"model": {("task", 17): a}},
        "c1_gui_synth": {},
        "c1_agent_s": {"model": {("task", 17): c}},
        "c1_agent_s_synth": {},
    }

    assert p5_aggregate._seed_evidence_issues(indices, ["task"]) == []

    c["seed_evidence"] = _web_seed_evidence(value=2)
    issues = p5_aggregate._seed_evidence_issues(indices, ["task"])
    assert any("runtime-observed seed evidence differs" in issue for issue in issues)


def test_p5_web_trial_binds_result_semantics_and_no_script_attestation() -> None:
    evidence = _web_seed_evidence()
    initial = evidence["initial"]["semantic_fingerprint"]  # type: ignore[index]
    final = evidence["final"]["semantic_fingerprint"]  # type: ignore[index]
    trial = {
        "arm": "c1_agent_s",
        "model": "model",
        "task_id": "task",
        "seed": 17,
        "benchmark": "JupyterLab",
        "semantic_probe_source": "jupyterlab_contents_kernel_live_v1",
        "policy_action_interface": "pixel_mouse_keyboard_only",
        "policy_received_semantic_probe": False,
        "policy_observation_source": "playwright_page_pixels",
        "scripted_transition_executed": False,
        "before_semantic_ok": True,
        "after_semantic_ok": True,
        "goal_evaluation_status": "evaluated",
        "before_semantic_detail": {
            **initial,
            "detail": "live start",
            "provenance_valid": True,
        },
        "after_semantic_detail": {
            **final,
            "detail": "live goal",
            "provenance_valid": True,
        },
        "seed_evidence": evidence,
    }

    assert p5_aggregate._c1_web_semantic_trial_issues(trial) == []

    trial["scripted_transition_executed"] = True
    trial["after_semantic_detail"]["observation"] = {"forged": True}
    issues = p5_aggregate._c1_web_semantic_trial_issues(trial)
    assert any("scripted transition attestation failed" in issue for issue in issues)
    assert any("fingerprint is invalid" in issue for issue in issues)
    assert any("differs from seed-bound" in issue for issue in issues)
