import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from warptest.c1_openra_runtime import (
    OpenRAGameplaySession,
    OpenRASettingsSession,
    apply_setting_actions,
    read_openra_settings,
    target_settings,
    write_openra_settings,
)


def test_openra_gameplay_can_grade_the_registered_oracle_start() -> None:
    session = object.__new__(OpenRAGameplaySession)
    session.spec = {"target": {"actors": [{"id": "generated-mcv"}]}}
    session._grade = MagicMock(return_value=object())
    oracle_spec = {
        "target": {
            "actors": [
                {"id": "registered-mcv"},
                {"type": "powr"},
            ]
        }
    }

    session.grade_target(oracle_spec)

    session._grade.assert_called_once_with(
        [{"type": "openra_actor_exists", "actor_id": "registered-mcv"}]
    )


def test_openra_gameplay_executes_actions_and_assertions_in_one_request() -> None:
    session = object.__new__(OpenRAGameplaySession)
    session._grade = MagicMock(return_value=object())
    actions = [{"type": "openra_wait_ticks", "ticks": 10}]
    assertions = [{"type": "no_openra_gameplay_probe_errors"}]

    session.execute_actions_and_grade(actions, assertions)

    session._grade.assert_called_once_with(assertions, actions=actions)


def test_openra_read_only_grades_run_during_render_ticks() -> None:
    source = (
        Path(__file__).parents[1]
        / "games"
        / "OpenRA"
        / "OpenRA.Mods.Common"
        / "Traits"
        / "World"
        / "WarptestGameplayProbe.cs"
    ).read_text(encoding="utf-8")

    assert "IWorldLoaded, ITick, ITickRender" in source
    render_tick = source.split(
        "void ITickRender.TickRender(WorldRenderer wr, Actor self)", 1
    )[1].split("void InitializeFuzzSessionWorld", 1)[0]
    assert "TryStartInteractiveRequest();" in render_tick
    assert "interactiveRequestActive && actions.Count == 0" in render_tick
    assert "CompleteInteractiveRequest();" in render_tick


def test_openra_settings_round_trip_and_action(tmp_path: Path) -> None:
    start = {
        "Game": {
            "ViewportEdgeScroll": True,
            "TargetLines": "Manual",
            "IntroductionPromptVersion": 999,
        }
    }
    path = write_openra_settings(tmp_path / "settings.yaml", start)
    assert read_openra_settings(path) == start
    after = apply_setting_actions(
        start,
        [
            {
                "type": "openra_set_setting",
                "section": "Game",
                "key": "ViewportEdgeScroll",
                "value": False,
            }
        ],
    )
    assert after["Game"]["ViewportEdgeScroll"] is False
    assert start["Game"]["ViewportEdgeScroll"] is True


def test_openra_c1_target_is_bounded() -> None:
    assert target_settings(
        {
            "kind": "openra_settings",
            "settings": {"Game": {"TargetLines": "Manual"}},
        }
    ) == {"Game": {"TargetLines": "Manual"}}
    with pytest.raises(ValueError, match="Unsupported OpenRA C1 setting action"):
        apply_setting_actions(
            {"Game": {}},
            [
                {
                    "type": "openra_set_setting",
                    "section": "Debug",
                    "key": "EnableDebugCommandsInReplays",
                    "value": True,
                }
            ],
        )


def test_openra_grade_and_seed_evidence_are_formal_and_task_bound(
    tmp_path: Path,
) -> None:
    class LiveProcess:
        pid = os.getpid()

        @staticmethod
        def poll() -> None:
            return None

    session = OpenRASettingsSession(
        tmp_path,
        tmp_path / "report",
        settings={"Game": {"TargetLines": "Manual"}},
        experiment_seed=9,
    )
    write_openra_settings(
        session.settings_path,
        {"Game": {"TargetLines": "Manual", "ViewportEdgeScroll": True}},
    )
    session._proc = LiveProcess()  # type: ignore[assignment]
    session._window_pid = os.getpid()

    grade = session.grade_settings(
        {"Game": {"TargetLines": "Manual", "ViewportEdgeScroll": False}}
    )
    evidence = session.seed_evidence("openra-edge-scroll")

    assert grade.status == "settings_mismatch"
    assert grade.detail["checks"] == [
        {
            "section": "Game",
            "key": "TargetLines",
            "expected": "Manual",
            "actual": "Manual",
            "ok": True,
        },
        {
            "section": "Game",
            "key": "ViewportEdgeScroll",
            "expected": False,
            "actual": True,
            "ok": False,
        },
    ]
    assert grade.detail["mismatches"] == [
        {
            "section": "Game",
            "key": "ViewportEdgeScroll",
            "expected": False,
            "actual": True,
        }
    ]
    assert evidence["task_id"] == "openra-edge-scroll"
    assert evidence["benchmark"] == "OpenRA"
    assert evidence["process_alive_at_observation"] is True

    write_openra_settings(
        session.settings_path,
        {"Game": {"ViewportEdgeScroll": 0}},
    )
    disguised_boolean = session.grade_settings(
        {"Game": {"ViewportEdgeScroll": False}}
    )
    assert disguised_boolean.ok is False
    assert disguised_boolean.detail["checks"][0]["ok"] is False

    session._window_pid = 999_999_999
    stale_window = session.seed_evidence("openra-edge-scroll")
    assert stale_window["process_id"] == 999_999_999
    assert stale_window["process_alive_at_observation"] is False


def test_openra_policy_frame_downsamples_retina_pixels(tmp_path: Path) -> None:
    session = object.__new__(OpenRASettingsSession)
    session.width = 1280
    session.height = 720
    session.capture_backend = "openra_renderer_save_screenshot_sync_v1"
    session.report_dir = tmp_path / "report"
    session._capture_sequence = 0
    source = session.report_dir / "background-frames" / "frame-00000001.png"

    def request(operation: str, **_fields: object) -> dict[str, object]:
        assert operation == "capture"
        source.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (2560, 1440), (10, 20, 30)).save(source)
        return {
            "status": "success",
            "accepted": True,
            "width": 1280,
            "height": 720,
            "frame_id": 9,
        }

    session._background_request = request  # type: ignore[method-assign]
    output = Path(session.capture_policy_frame(tmp_path / "policy.png"))

    with Image.open(output) as image:
        assert image.size == (1280, 720)
    assert session.last_policy_frame_capture == {
        "source_frame": [2560, 1440],
        "policy_frame": [1280, 720],
        "source_to_policy_scale": 2,
        "normalization": "integer_retina_downsample_lanczos_v1",
    }


def test_openra_policy_frame_rejects_non_integer_size_drift(tmp_path: Path) -> None:
    session = object.__new__(OpenRASettingsSession)
    session.width = 1280
    session.height = 720
    session.capture_backend = "openra_renderer_save_screenshot_sync_v1"
    session.report_dir = tmp_path / "report"
    session._capture_sequence = 0
    source = session.report_dir / "background-frames" / "frame-00000001.png"

    def request(_operation: str, **_fields: object) -> dict[str, object]:
        source.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1300, 720), (10, 20, 30)).save(source)
        return {
            "status": "success",
            "accepted": True,
            "width": 1280,
            "height": 720,
            "frame_id": 1,
        }

    session._background_request = request  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="integer Retina multiple"):
        session.capture_policy_frame(tmp_path / "policy.png")
