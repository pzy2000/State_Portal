from __future__ import annotations

import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from warptest import gui_agent
from warptest.c1_golden_artifacts import validate_oracle_golden_artifacts


def _task(benchmark: str) -> SimpleNamespace:
    slug = benchmark.lower()
    return SimpleNamespace(
        id=f"{slug}-c1-test",
        benchmark=benchmark,
        state_description="legacy desktop task",
        preconditions="reach the declared desktop start",
        reference_steps="perform the visible desktop transition",
        goal_description="INDEPENDENT_SENTINEL_GOAL",
        horizon_class="mid",
        issue=SimpleNamespace(
            id=f"{slug}-c1-test",
            benchmark=benchmark,
            repo=f"games/{benchmark}",
        ),
        oracle_spec=f"specs/{slug}/c1-test.yaml",
    )


def _dispatch(tmp_path: Path, task: SimpleNamespace) -> dict[str, object]:
    return gui_agent._run_agent_task.__wrapped__(
        tmp_path,
        task,
        model="worker/model",
        base_url="https://example.invalid/v1",
        api_key="unused",
        api_key_env="TEST_KEY",
        synth_model="worker/model",
        max_explore_steps=3,
        temperature=1.0,
        out_dir=tmp_path / "report",
        max_steps=5,
        headless=True,
        start_mode="from_start",
        policy_kind="agent_s",
        policy_client=MagicMock(),
        observer=None,
        seed=19,
        shared_synthesis_cache=None,
    )


@pytest.mark.parametrize("benchmark", ["Blender", "LibreOffice"])
def test_desktop_benchmarks_dispatch_before_generic_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, benchmark: str
) -> None:
    expected = {"branch": "desktop", "benchmark": benchmark}
    branch = MagicMock(return_value=expected)
    monkeypatch.setattr(gui_agent, "_run_desktop_c1_agent_task", branch)

    task = _task(benchmark)
    result = _dispatch(tmp_path, task)

    assert result == expected
    assert branch.call_args.args[:2] == (tmp_path, task)
    assert branch.call_args.kwargs["policy_kind"] == "agent_s"
    assert branch.call_args.kwargs["headless"] is True
    assert branch.call_args.kwargs["seed"] == 19


@pytest.mark.parametrize(
    ("override", "expected_ok", "expected_status"),
    [
        ({}, True, "success"),
        ({"live": False}, False, "validate_error"),
        ({"source": "synthetic"}, False, "validate_error"),
        ({"checks": []}, False, "validate_error"),
        ({"checks": [{"name": "x", "ok": False}]}, False, "validate_error"),
        (
            {
                "passed": False,
                "status": "failure",
                "checks": [{"name": "x", "ok": False}],
            },
            False,
            "failure",
        ),
    ],
)
def test_desktop_semantic_probe_is_live_nonempty_and_internally_consistent(
    override: dict[str, object], expected_ok: bool, expected_status: str
) -> None:
    report: dict[str, object] = {
        "status": "success",
        "source": "blender_live_bpy_probe_v1",
        "live": True,
        "passed": True,
        "checks": [{"name": "target", "ok": True}],
    }
    report.update(override)
    runtime = SimpleNamespace(
        semantic_source="blender_live_bpy_probe_v1",
        probe_start=MagicMock(return_value=report),
        probe_goal=MagicMock(return_value=report),
    )

    ok, status, detail, returned = gui_agent._desktop_semantic_probe(
        runtime, {"target": {}}, after=False
    )

    assert ok is expected_ok
    assert status == expected_status
    assert detail["source"] == report.get("source")
    assert returned == report
    runtime.probe_start.assert_called_once_with({"target": {}})
    runtime.probe_goal.assert_not_called()


class _FakeProcess:
    pid = 7312

    def poll(self) -> None:
        return None


class _FakeNative:
    def window_bounds(self) -> SimpleNamespace:
        return SimpleNamespace(
            to_dict=lambda: {"x": 10, "y": 20, "width": 1200, "height": 800}
        )


class _FakeDesktopMixin:
    semantic_source = "desktop_live_probe_v1"
    seed_evidence_source = "desktop_native_session_v1"

    def _init_fake(self) -> None:
        self.process = _FakeProcess()
        self.native = _FakeNative()
        self.session_id = "desktop-background-session"
        self.width = 1440 if self.__class__.__name__.startswith("_FakeLibre") else 1280
        self.height = 1000 if self.width == 1440 else 800
        self.last_policy_frame_id = 0
        self.screenshots: list[Path] = []
        self.proofs: list[Path] = []
        self.restore_calls: list[dict[str, object]] = []
        self.sequence = 0

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def screenshot(self, path: str | Path) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"desktop-{len(self.screenshots)}".encode())
        self.screenshots.append(target)
        self.sequence += 1
        return str(target)

    def _success_probe(self, name: str) -> dict[str, object]:
        self.sequence += 1
        return {
            "status": "success",
            "source": self.semantic_source,
            "live": True,
            "passed": True,
            "checks": [{"name": name, "ok": True}],
        }

    def probe_liveness(self) -> dict[str, object]:
        return self._success_probe("bridge.ready")

    def probe_start(self, spec: dict[str, object]) -> dict[str, object]:
        return self._success_probe("target")

    def probe_goal(self, spec: dict[str, object]) -> dict[str, object]:
        return self._success_probe("assertions")

    def restore_target(self, spec: dict[str, object]) -> dict[str, object]:
        self.restore_calls.append(spec)
        return {
            "status": "success",
            "source": self.semantic_source,
            "live": True,
            "checks": [
                {"name": "restore.target_only", "ok": True},
                {"name": "target", "ok": True},
            ],
        }

    def seed_evidence(
        self,
        seed: int,
        probe: dict[str, object],
        task_id: str,
        stage: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": self.seed_evidence_source,
            "observed": True,
            "algorithm": "not_applicable_native_gui",
            "seed": seed,
            "process_id": self.process.pid,
            "process_alive_at_observation": True,
            "task_id": task_id,
            "semantic_stage": stage,
            "semantic_source": self.semantic_source,
            "semantic_observation": probe,
            "sequence": self.sequence,
        }


class _FakeBlenderSession(_FakeDesktopMixin, gui_agent.BlenderC1DesktopSession):
    semantic_source = "blender_live_bpy_probe_v1"
    seed_evidence_source = "blender_native_desktop_session_v1"

    def __init__(self) -> None:
        self._init_fake()

    def render_proof(
        self, path: str | Path, *, canonical_spec: dict[str, object]
    ) -> str:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"proof-{len(self.proofs)}".encode())
        self.proofs.append(target)
        return str(target)


class _FakeLibreOfficeSession(
    _FakeDesktopMixin, gui_agent.LibreOfficeC1DesktopSession
):
    semantic_source = "libreoffice_live_uno_probe_v1"
    seed_evidence_source = "libreoffice_native_desktop_session_v1"

    def __init__(self) -> None:
        self._init_fake()

    def _document_probe(
        self, name: str, *, goal: bool = False
    ) -> dict[str, object]:
        report = self._success_probe(name)
        report.update(
            document_identity="uno-document-goal" if goal else "uno-document-start",
            session_identity="office-session-1",
            document_url="file:///tmp/c1-policy-saved.ods" if goal else "",
            document_modified=False if goal else True,
            document_path="/tmp/c1-policy-saved.ods" if goal else None,
        )
        if goal:
            report["checks"] = [
                {"name": name, "ok": True},
                {"name": "transition.session_same", "ok": True},
                {"name": "transition.saved_url", "ok": True},
                {"name": "transition.persisted_file", "ok": True},
                {"name": "transition.not_modified", "ok": True},
                {"name": "transition.document_reopened", "ok": True},
            ]
        return report

    def probe_start(self, spec: dict[str, object]) -> dict[str, object]:
        return self._document_probe("target")

    def probe_goal(self, spec: dict[str, object]) -> dict[str, object]:
        return self._document_probe("assertions", goal=True)

    def restore_target(self, spec: dict[str, object]) -> dict[str, object]:
        self.restore_calls.append(spec)
        report = self._document_probe("target")
        report["checks"] = [
            {"name": "restore.target_only", "ok": True},
            {"name": "target", "ok": True},
        ]
        return report


def _goldens() -> SimpleNamespace:
    return SimpleNamespace(
        before="gold-before.png",
        after="gold-after.png",
        after_description=None,
        after_rubric=None,
    )


def test_registered_desktop_goldens_materialize_committed_pair_without_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Path(__file__).resolve().parents[1]
    source_root = project / "specs" / "libreoffice" / "visual"
    spec_root = tmp_path / "specs" / "libreoffice"
    visual_root = spec_root / "visual"
    visual_root.mkdir(parents=True)
    for name in (
        "calc-quarterly-budget-rollup.before.golden.png",
        "calc-quarterly-budget-rollup.after.golden.png",
    ):
        (visual_root / name).write_bytes((source_root / name).read_bytes())
    spec_path = spec_root / "task.yaml"
    spec_path.write_text("name: test\n", encoding="utf-8")
    spec = {
        "experiment_seed": 3,
        "visual_oracle": {
            "golden_before": "visual/calc-quarterly-budget-rollup.before.golden.png",
            "golden_after": "visual/calc-quarterly-budget-rollup.after.golden.png",
            "description": "registered pair",
        },
    }
    monkeypatch.setattr(gui_agent, "lookup_cached_oracle_goldens", lambda *_: None)
    monkeypatch.setattr(gui_agent, "_repo_git_commit", lambda *_: "a" * 40)
    monkeypatch.setattr(gui_agent, "_c1_cache_root", lambda *_: tmp_path / "cache")

    goldens = gui_agent._registered_desktop_goldens(
        tmp_path / "repo",
        spec,
        spec_path,
        tmp_path / "report",
        benchmark="LibreOffice",
    )

    before = Path(goldens.before)
    after = Path(goldens.after)
    assert before.is_file() and after.is_file()
    assert before.parent == after.parent
    assert (before.parent / "meta.json").is_file()
    assert goldens.cache_hit is False
    assert goldens.cacheable is True
    assert goldens.consistency["source"] == "committed_visual_oracle_pair_v1"
    assert goldens.quality["before"]["informative"] is True
    assert goldens.quality["after"]["informative"] is True
    assert validate_oracle_golden_artifacts(
        gui_agent._oracle_goldens_payload(goldens), reported_seed=3
    ) == []


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runtime: _FakeDesktopMixin,
    oracle_spec: dict[str, object],
) -> None:
    monkeypatch.setattr(gui_agent, "resolve_task_repo", lambda *_: tmp_path)
    monkeypatch.setattr(
        gui_agent, "find_blender_executable", lambda *_: tmp_path / "Blender"
    )
    monkeypatch.setattr(gui_agent, "load_spec", lambda *_: oracle_spec)
    monkeypatch.setattr(
        gui_agent, "_registered_desktop_goldens", lambda *_, **__: _goldens()
    )
    monkeypatch.setattr(
        gui_agent, "_oracle_goldens_payload", lambda *_: {"cache_hit": True}
    )
    monkeypatch.setattr(gui_agent, "desktop_session_for", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(gui_agent.native_window, "screen_size", lambda: (1512, 982))


def _call_desktop(
    tmp_path: Path,
    task: SimpleNamespace,
    *,
    start_mode: str,
) -> dict[str, object]:
    return gui_agent._run_desktop_c1_agent_task(
        tmp_path,
        task,
        model="worker/model",
        base_url="https://example.invalid/v1",
        api_key="unused",
        api_key_env="TEST_KEY",
        synth_model="worker/model",
        max_explore_steps=3,
        temperature=1.0,
        out_dir=tmp_path / "report",
        max_steps=5,
        headless=True,
        start_mode=start_mode,
        policy_kind="agent_s",
        policy_client=SimpleNamespace(metadata={"native_exec_enabled": True}),
        observer=None,
        seed=29,
    )


def test_desktop_runtime_is_sigstopped_during_start_visual_grade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _FakeBlenderSession()
    kill = MagicMock()
    monkeypatch.setattr(gui_agent.os, "kill", kill)
    metadata: dict[str, object] = {}

    with gui_agent._hold_runtime_state_for_start_grading(
        runtime, metadata=metadata
    ):
        assert metadata["backend"] == "sigstop"
        assert metadata["held_pid"] == runtime.process.pid
        assert metadata["held"] is True

    assert metadata["released"] is True
    assert kill.call_args_list == [
        call(runtime.process.pid, signal.SIGSTOP),
        call(runtime.process.pid, signal.SIGCONT),
    ]


def test_from_start_libreoffice_runs_both_native_pixel_phases_without_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task("LibreOffice")
    runtime = _FakeLibreOfficeSession()
    oracle_spec = {
        "checkpoint": {"expected_validity": "save-restorable"},
        "target": {"kind": "calc_spreadsheet", "sheets": {"Summary": {}}},
        "assertions": [{"type": "libreoffice_sheet_exists", "sheet": "Summary"}],
    }
    _patch_common(monkeypatch, tmp_path, runtime, oracle_spec)
    before = tmp_path / "phase-a.png"
    after = tmp_path / "phase-b.png"
    before.write_bytes(b"before")
    after.write_bytes(b"after")
    run_phase = MagicMock(
        side_effect=[
            gui_agent.PhaseResult(False, 2, str(before), elapsed_ms=20),
            gui_agent.PhaseResult(False, 3, str(after), elapsed_ms=30),
        ]
    )
    monkeypatch.setattr(gui_agent, "_run_phase", run_phase)
    hold = {
        "required": True,
        "held": True,
        "released": True,
        "backend": "sigstop",
        "held_pid": runtime.process.pid,
    }
    monkeypatch.setattr(
        gui_agent,
        "_run_before_visual_gate",
        lambda **_: (
            {"before": {"label": "correct"}, "after": {"label": "not_evaluated"}},
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

    result = _call_desktop(tmp_path, task, start_mode="from_start")

    assert result["status"] == "success"
    assert result["steps"] == {"phase_a": 2, "phase_b": 3, "total": 5}
    assert runtime.restore_calls == []
    assert [row.kwargs["max_steps"] for row in run_phase.call_args_list] == [5, 3]
    phase_a_prompt = run_phase.call_args_list[0].kwargs["instruction"]
    phase_b_prompt = run_phase.call_args_list[1].kwargs["instruction"]
    assert "reach the declared desktop start" in phase_a_prompt
    assert "perform the visible desktop transition" not in phase_a_prompt
    assert "perform the visible desktop transition" in phase_b_prompt
    assert "reach the declared desktop start" not in phase_b_prompt
    assert "INDEPENDENT_SENTINEL_GOAL" not in phase_a_prompt + phase_b_prompt
    assert result["policy_observation_source"] == "application_framebuffer_pixels"
    assert result["semantic_probe_source"] == runtime.semantic_source
    assert result["before_semantic_detail"]["document_identity"] == (
        "uno-document-start"
    )
    assert result["after_semantic_detail"]["document_identity"] == (
        "uno-document-goal"
    )
    assert result["after_semantic_detail"]["document_modified"] is False
    assert {
        row["name"] for row in result["after_semantic_detail"]["checks"]
    }.issuperset(
        {
            "transition.session_same",
            "transition.saved_url",
            "transition.persisted_file",
            "transition.not_modified",
            "transition.document_reopened",
        }
    )
    assert result["seed_evidence"]["source"] == runtime.seed_evidence_source
    assert result["seed_evidence"]["initial"]["process_id"] == runtime.process.pid
    assert result["seed_evidence"]["final"]["process_id"] == runtime.process.pid
    assert result["start_grading_runtime_hold"] == hold
    assert result["screenshot_before"] == str(before)
    assert result["screenshot_after"] == str(after)


def test_libreoffice_immediate_done_fails_read_only_goal_without_grader_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task("LibreOffice")
    runtime = _FakeLibreOfficeSession()
    runtime.execute_state_transition = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError("Agent-S grader must not execute arm-E transition")
    )

    def unchanged_goal(_spec: dict[str, object]) -> dict[str, object]:
        return {
            "status": "failure",
            "source": runtime.semantic_source,
            "live": True,
            "passed": False,
            "checks": [
                {"name": "assertions", "ok": False},
                {"name": "transition.session_same", "ok": True},
                {"name": "transition.saved_url", "ok": False},
                {"name": "transition.persisted_file", "ok": False},
                {"name": "transition.not_modified", "ok": False},
                {"name": "transition.document_reopened", "ok": False},
            ],
            "document_identity": "uno-document-start",
            "session_identity": "office-session-1",
            "document_url": "",
            "document_modified": True,
        }

    runtime.probe_goal = unchanged_goal  # type: ignore[method-assign]
    oracle_spec = {
        "checkpoint": {"expected_validity": "save-restorable"},
        "target": {"kind": "calc_spreadsheet", "sheets": {"Summary": {}}},
        "assertions": [
            {"type": "libreoffice_sheet_exists", "sheet": "Summary"}
        ],
    }
    _patch_common(monkeypatch, tmp_path, runtime, oracle_spec)
    before = tmp_path / "immediate-before.png"
    after = tmp_path / "immediate-after.png"
    before.write_bytes(b"before")
    after.write_bytes(b"after")
    monkeypatch.setattr(
        gui_agent,
        "_run_phase",
        MagicMock(
            side_effect=[
                gui_agent.PhaseResult(True, 1, str(before), elapsed_ms=5),
                gui_agent.PhaseResult(False, 1, str(after), elapsed_ms=4),
            ]
        ),
    )
    monkeypatch.setattr(
        gui_agent,
        "_run_before_visual_gate",
        lambda **_: (
            {"before": {"label": "correct"}, "after": {"label": "not_evaluated"}},
            {
                "required": True,
                "held": True,
                "released": True,
                "backend": "sigstop",
                "held_pid": runtime.process.pid,
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

    result = _call_desktop(tmp_path, task, start_mode="from_start")

    assert result["status"] == "failure"
    assert result["incomplete"] is False
    assert result["start_success"] is True
    assert result["goal_success"] is False
    assert result["after_visual_ok"] is True
    assert result["after_semantic_ok"] is False
    assert result["after_semantic_detail"]["document_identity"] == (
        "uno-document-start"
    )
    assert result["after_semantic_detail"]["document_url"] == ""
    runtime.execute_state_transition.assert_not_called()


def test_synthesized_blender_restores_target_only_and_grades_render_proofs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _task("Blender")
    runtime = _FakeBlenderSession()
    generated_spec: dict[str, object] = {
        "target": {"kind": "scene", "objects": [{"name": "WarpCube"}]},
        "actions": [{"type": "forbidden_hidden_action"}],
        "assertions": [{"type": "blender_object_exists", "object": "WarpCube"}],
    }
    oracle_spec = {
        "checkpoint": {"expected_validity": "save-restorable"},
        "target": {"kind": "scene", "objects": [{"name": "WarpCube"}]},
        "assertions": [{"type": "blender_object_exists", "object": "WarpCube"}],
    }
    _patch_common(monkeypatch, tmp_path, runtime, oracle_spec)
    trusted_executable = tmp_path / "games" / "build_darwin_lite" / "Blender"
    executable_finder = MagicMock(return_value=trusted_executable)
    session_factory = MagicMock(return_value=runtime)
    monkeypatch.setattr(gui_agent, "find_blender_executable", executable_finder)
    monkeypatch.setattr(gui_agent, "desktop_session_for", session_factory)
    monkeypatch.setattr(
        gui_agent, "_obtain_self_synthesis", lambda *_, **__: {"status": "success"}
    )

    def apply_synthesis(
        result: dict[str, object], *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        result.update(checkpoint_synthesis_ms=11, synth_status="generated")
        return generated_spec

    monkeypatch.setattr(gui_agent, "_apply_self_synthesis_result", apply_synthesis)
    after = tmp_path / "phase-b.png"
    after.write_bytes(b"after")
    run_phase = MagicMock(
        return_value=gui_agent.PhaseResult(False, 3, str(after), elapsed_ms=30)
    )
    monkeypatch.setattr(gui_agent, "_run_phase", run_phase)
    before_gate = MagicMock(
        return_value=(
            {"before": {"label": "correct"}, "after": {"label": "not_evaluated"}},
            {
                "required": True,
                "held": True,
                "released": True,
                "backend": "sigstop",
                "held_pid": runtime.process.pid,
            },
        )
    )
    after_grade = MagicMock(
        return_value={
            "before": {"label": "correct"},
            "after": {"label": "correct"},
        }
    )
    monkeypatch.setattr(gui_agent, "_run_before_visual_gate", before_gate)
    monkeypatch.setattr(gui_agent, "_run_after_visual_grade", after_grade)

    result = _call_desktop(tmp_path, task, start_mode="synthesized")

    executable_finder.assert_called_once_with((tmp_path / "games/Blender").resolve())
    session_factory.assert_called_once_with(
        "Blender",
        tmp_path,
        tmp_path / "report/tasks/blender-c1-test/agent",
        executable=trusted_executable,
    )
    assert result["status"] == "success"
    assert result["start_construction_status"] == "reached"
    assert result["restore_phase"] == "target_only"
    assert result["restore_actions_executed"] is False
    assert runtime.restore_calls == [generated_spec]
    assert result["steps"] == {"phase_a": 0, "phase_b": 3, "total": 3}
    run_phase.assert_called_once()
    assert len(runtime.proofs) == 2
    assert result["screenshot_before"] == str(runtime.proofs[0])
    assert result["screenshot_after"] == str(runtime.proofs[1])
    assert result["desktop_screenshot_before"] == str(runtime.screenshots[1])
    assert result["desktop_screenshot_after"] == str(after)
    assert before_gate.call_args.kwargs["before_shot"] == str(runtime.proofs[0])
    assert after_grade.call_args.kwargs["after_shot"] == str(runtime.proofs[1])


@pytest.mark.parametrize(
    ("benchmark", "runtime_factory", "target"),
    [
        ("Blender", _FakeBlenderSession, {"kind": "scene", "objects": []}),
        (
            "LibreOffice",
            _FakeLibreOfficeSession,
            {"kind": "calc_spreadsheet", "sheets": {"Summary": {}}},
        ),
    ],
)
def test_synthesized_desktop_semantic_restore_failure_is_complete_treatment_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    benchmark: str,
    runtime_factory: type[_FakeDesktopMixin],
    target: dict[str, object],
) -> None:
    task = _task(benchmark)
    runtime = runtime_factory()
    generated_spec = {"target": target, "actions": [], "assertions": []}
    oracle_spec = {
        "checkpoint": {"expected_validity": "save-restorable"},
        "target": target,
        "assertions": [],
    }
    _patch_common(monkeypatch, tmp_path, runtime, oracle_spec)
    monkeypatch.setattr(
        gui_agent, "_obtain_self_synthesis", lambda *_, **__: {"status": "success"}
    )

    def apply_synthesis(
        result: dict[str, object], *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        result.update(checkpoint_synthesis_ms=11, synth_status="generated")
        return generated_spec

    monkeypatch.setattr(gui_agent, "_apply_self_synthesis_result", apply_synthesis)
    failed_restore = {
        "status": "failure",
        "source": runtime.semantic_source,
        "live": True,
        "checks": [
            {
                "name": "restore.target_only",
                "ok": False,
                "detail": "model-generated target was rejected before restore",
            },
        ],
        "errors": [],
    }
    runtime.restore_target = MagicMock(return_value=failed_restore)
    run_phase = MagicMock()
    before_gate = MagicMock()
    after_grade = MagicMock()
    monkeypatch.setattr(gui_agent, "_run_phase", run_phase)
    monkeypatch.setattr(gui_agent, "_run_before_visual_gate", before_gate)
    monkeypatch.setattr(gui_agent, "_run_after_visual_grade", after_grade)

    result = _call_desktop(tmp_path, task, start_mode="synthesized")

    assert result["status"] == "failure"
    assert result["incomplete"] is False
    assert result["treatment_failure"] is True
    assert result["model_failure_stage"] == "restore"
    assert result["start_construction_status"] == "restore_failed"
    assert result["restore_status"] == "failure"
    assert result["restore_report"] is failed_restore
    assert result["visual_grading_started"] is False
    assert result["seed_evidence"]["observed"] is True
    run_phase.assert_not_called()
    before_gate.assert_not_called()
    after_grade.assert_not_called()
