from __future__ import annotations

import ast
import inspect
from contextlib import nullcontext
from pathlib import Path

from PIL import Image
import pytest

from warptest import c1_start_validation as start_validation
from warptest.c1_openra_runtime import OpenRASettingsGrade
from warptest.jupyterlab_runtime import JupyterLabRuntimeAdapter
from warptest.profiles import Game2048Profile, SpaceRPGProfile, DatingSimProfile


ROOT = Path(__file__).resolve().parents[1]


def _selected_tasks():
    return start_validation.load_selected_tasks(ROOT)


def _write_informative_image(path: Path) -> None:
    image = Image.new("RGB", (640, 480), (30, 30, 30))
    for x in range(80, 560):
        for y in range(100, 380):
            image.putpixel((x, y), ((x + y) % 255, x % 255, y % 255))
    image.save(path)


def test_lightweight_manifest_is_exact_two_by_ten_matrix() -> None:
    tasks, _manifest, _registry = _selected_tasks()

    counts = start_validation.validate_lightweight_matrix(tasks)

    assert len(tasks) == 20
    assert len(counts) == 10
    assert set(counts.values()) == {2}
    assert [task.id for task in tasks] == [
        "2048-merge-left",
        "2048-merge-right",
        "love-office-work-coding",
        "love-gym-workout",
        "spacerpg-playable-flight",
        "spacerpg-galaxy-jump",
        "luanti-set-node",
        "luanti-dig-node",
        "openra-disable-edge-scroll",
        "openra-target-lines-automatic",
        "jupyterlab-text-file-edit",
        "jupyterlab-notebook-cell-output",
        "libreoffice-calc-budget-rollup",
        "libreoffice-impress-status-deck",
        "blender-scene-cube-material",
        "blender-apply-bevel-modifier",
        "jynew-save-restore",
        "jynew-submap-save-restore",
        "pal3-scene-transition",
        "pal3-scene-restore",
    ]


def test_start_spec_strips_transition_and_goal_assertions() -> None:
    tasks, _manifest, _registry = _selected_tasks()
    task = tasks[0]

    original = start_validation.load_spec(task.oracle_spec)
    target_only = start_validation._start_spec(task)

    assert original["actions"]
    assert original["assertions"]
    assert target_only["target"] == original["target"]
    assert target_only["actions"] == []
    assert target_only["assertions"] == []


def test_module_has_no_gui_agent_or_provider_import() -> None:
    source = Path(start_validation.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        str(node.module)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )

    assert not any("gui_agent" in name for name in imported)
    assert not any("provider" in name for name in imported)


def test_browser_target_only_methods_do_not_call_smoke_or_oracles() -> None:
    for profile in (Game2048Profile, SpaceRPGProfile, DatingSimProfile):
        source = inspect.getsource(profile.restore_target_only)
        assert "_run_actions" not in source
        assert "_assert_oracles" not in source


def test_json_safe_preserves_openra_semantic_checks() -> None:
    grade = OpenRASettingsGrade(
        ok=True,
        status="success",
        detail={"checks": [{"key": "TargetLines", "ok": True}], "mismatches": []},
    )

    encoded = start_validation._json_safe(grade)

    assert encoded["ok"] is True
    assert encoded["detail"]["checks"][0]["key"] == "TargetLines"


def test_jupyter_network_audit_records_only_non_loopback_requests() -> None:
    runtime = object.__new__(JupyterLabRuntimeAdapter)
    runtime.network_api_calls = {"attempted": []}

    class Request:
        def __init__(self, url: str) -> None:
            self.url = url

    runtime._record_browser_request(Request("http://127.0.0.1:9000/lab"))
    runtime._record_browser_request(Request("data:text/plain,ok"))
    runtime._record_browser_request(Request("https://example.com/remote.js"))

    assert runtime.network_api_calls["attempted"] == [
        "https://example.com/remote.js"
    ]


def test_task_pass_requires_semantic_live_image_and_no_actions(tmp_path: Path) -> None:
    tasks, manifest, registry = _selected_tasks()
    task = tasks[0]
    image_path = tmp_path / "live.png"
    _write_informative_image(image_path)
    run = start_validation.StartValidationRun(
        root=ROOT,
        manifest_path=manifest,
        tasks_path=registry,
        report_dir=tmp_path / "report",
        tasks=[task],
        benchmark_counts={task.benchmark: 1},
        headless_browser=True,
    )

    start_validation._run_task(
        run,
        task,
        lambda _spec, _task_dir: {
            "runtime_started": True,
            "semantic_start_ok": True,
            "runtime_alive_at_observation": True,
            "restore_actions_executed": False,
            "assertions_evaluated": False,
            "goal_evaluated": False,
            "network_api_calls": {"attempted": []},
            "screenshot": str(image_path),
        },
    )
    result = run.finish()

    assert result["complete"] is True
    assert result["passed"] is True
    assert result["results"][0]["transition_executed"] is False
    assert result["results"][0]["goal_evaluated"] is False
    assert result["results"][0]["external_api_calls"] == 0


def test_task_fails_closed_if_runtime_attests_actions_executed(tmp_path: Path) -> None:
    tasks, manifest, registry = _selected_tasks()
    task = tasks[0]
    image_path = tmp_path / "live.png"
    _write_informative_image(image_path)
    run = start_validation.StartValidationRun(
        root=ROOT,
        manifest_path=manifest,
        tasks_path=registry,
        report_dir=tmp_path / "report",
        tasks=[task],
        benchmark_counts={task.benchmark: 1},
        headless_browser=True,
    )

    start_validation._run_task(
        run,
        task,
        lambda _spec, _task_dir: {
            "runtime_started": True,
            "semantic_start_ok": True,
            "runtime_alive_at_observation": True,
            "restore_actions_executed": True,
            "assertions_evaluated": False,
            "goal_evaluated": False,
            "network_api_calls": {"attempted": []},
            "screenshot": str(image_path),
        },
    )

    assert run.finish()["passed"] is False


def test_unity_start_screenshot_fails_closed_without_gameview_frame(
    tmp_path: Path,
) -> None:
    class Session:
        def unity_screenshot(self, _path: Path) -> str:
            raise RuntimeError("camera was flat")

        def desktop_screenshot(self, path: Path) -> str:
            _write_informative_image(path)
            return str(path)

    with pytest.raises(AttributeError, match="capture_policy_frame"):
        start_validation._unity_start_screenshot(Session(), tmp_path)


def test_explicit_subset_runs_only_requested_registered_tasks(
    tmp_path: Path, monkeypatch
) -> None:
    requested = ["pal3-scene-transition", "pal3-scene-restore"]

    monkeypatch.setattr(start_validation, "runtime_lease", lambda _repo: nullcontext())

    def complete_group(run, tasks) -> None:
        for task in tasks:
            run.update(task, status="success", passed=True)

    monkeypatch.setattr(start_validation, "_run_unity_group", complete_group)

    result = start_validation.run_start_validation(
        ROOT,
        report_dir=tmp_path / "subset",
        selected_task_ids=requested,
    )

    assert result["complete"] is True
    assert result["passed"] is True
    assert result["selection_mode"] == "explicit_subset"
    assert result["selected_task_ids"] == requested
    assert result["benchmark_task_counts"] == {"pal3": 2}
    assert result["summary"]["total"] == 2


def test_explicit_subset_rejects_unknown_task(tmp_path: Path) -> None:
    try:
        start_validation.run_start_validation(
            ROOT,
            report_dir=tmp_path / "subset",
            selected_task_ids=["not-a-registered-c1-task"],
        )
    except ValueError as error:
        assert "unknown selected task ids" in str(error)
    else:
        raise AssertionError("unknown task id must fail closed")


def test_explicit_subset_rejects_empty_list(tmp_path: Path) -> None:
    try:
        start_validation.run_start_validation(
            ROOT,
            report_dir=tmp_path / "subset",
            selected_task_ids=[],
        )
    except ValueError as error:
        assert "unique non-empty task ids" in str(error)
    else:
        raise AssertionError("empty subset must not fall back to the full matrix")
