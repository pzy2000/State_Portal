from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from warptest import spacerpg_gui_baseline, gui_agent
from warptest.c1_evaluation import (
    EvaluationContext,
    evaluate_checks,
    evaluation_contract,
    formal_evaluation_contract,
    formal_spec,
    formal_surface_contract,
    materialize_legacy_spec,
    semantic_assertions,
    validate_evaluation_contract,
    visual_oracle,
)
from warptest.runner import load_raw_spec, load_spec
from warptest.spec_schema import validate_spec_schema
from warptest.state_loop import (
    _oracle_fingerprint,
    _oracle_spec_digest,
    load_state_loop_tasks,
)
from warptest.synthesis import (
    build_explore_initial_prompt,
    build_explore_system_prompt,
    build_llm_checkpoint_prompt,
)


ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = ROOT / "benchmarks/p5/c1_tasks.yaml"
PUBLIC_ROWS_SHA256 = "7271d1f5f81304d7f185b75c34c7dfdcffe35e76b33959008c8de5d4d27e7d87"


def _task_rows() -> list[dict]:
    payload = yaml.safe_load(TASKS_PATH.read_text(encoding="utf-8"))
    return payload["tasks"]


def _raw_spec(row: dict) -> tuple[Path, dict]:
    path = ROOT / row["oracle_spec"]
    return path, load_raw_spec(path)


def test_formal_public_rows_are_unchanged() -> None:
    rows = _task_rows()
    public_rows = [
        {key: value for key, value in row.items() if key != "oracle_spec"}
        for row in rows
    ]
    digest = hashlib.sha256(
        json.dumps(
            public_rows,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

    assert len(rows) == 61
    assert len({row["id"] for row in rows}) == 61
    assert digest == PUBLIC_ROWS_SHA256


def test_all_formal_specs_use_strict_evaluation_v1_and_existing_visuals() -> None:
    rows = _task_rows()
    paths: set[Path] = set()
    before_after_images: list[Path] = []
    optional_baselines: list[str] = []

    for row in rows:
        path, spec = _raw_spec(row)
        assert path not in paths
        paths.add(path)
        assert "assertions" not in spec
        assert "visual_oracle" not in spec
        assert evaluation_contract(spec)["schema_version"] == 1
        assert validate_evaluation_contract(spec, path) == []
        assert validate_spec_schema(spec, path, visual_required=True) == []

        surfaces = spec["evaluation"]["semantic"]["surfaces"]
        assert {"gui", "state"} <= set(surfaces)
        assert set(surfaces) <= {
            "gui",
            "state",
            "gui_from_start_baseline",
        }
        if "gui_from_start_baseline" in surfaces:
            optional_baselines.append(row["id"])
        for surface in surfaces.values():
            for phase_name in ("start", "goal"):
                names = [
                    check["name"]
                    for check in surface[phase_name].get("checks", [])
                ]
                assert len(names) == len(set(names))

        oracle = visual_oracle(spec)
        assert isinstance(oracle, dict)
        for key in ("golden_image", "golden_before", "golden_after"):
            image = (path.parent / oracle[key]).resolve()
            assert image.is_file(), f"{row['id']} has missing {key}: {image}"
            if key in {"golden_before", "golden_after"}:
                before_after_images.append(image)

    assert len(paths) == 61
    assert len(before_after_images) == 122
    assert optional_baselines == [
        "spacerpg-playable-flight",
        "spacerpg-galaxy-jump",
        "spacerpg-station-save",
        "spacerpg-save-restore",
        "spacerpg-trading-cargo",
    ]


def test_playable_flight_declares_throttle_metric_and_exactly_one_intent() -> None:
    spec = load_raw_spec(ROOT / "specs/spacerpg/playable-flight.yaml")
    checks = spec["evaluation"]["semantic"]["surfaces"]["gui"]["goal"][
        "checks"
    ]
    throttle = [
        check for check in checks if check["name"].startswith("transition.throttle")
    ]

    assert throttle == [
        {
            "name": "transition.throttle_delta",
            "metric": "greater_than",
            "actual": {"probe": "spacerpg.throttle_percent", "phase": "after"},
            "expected": {"probe": "spacerpg.throttle_percent", "phase": "before"},
        },
        {
            "name": "transition.throttle_intent_count",
            "metric": "equals",
            "actual": {"probe": "spacerpg.throttle_intent_count"},
            "expected": {"value": 1},
        },
    ]


@pytest.mark.parametrize(
    ("name", "visible", "save_auto", "save_manual", "reason"),
    [
        (
            "playable-flight.yaml",
            {"screen": "flight", "body_text": "Flight HUD Helion Reach"},
            None,
            None,
            "Expected visible Helion Reach flight screen.",
        ),
        (
            "galaxy-jump.yaml",
            {"screen": "flight", "body_text": "Flight HUD Kuro Belt"},
            None,
            None,
            "Expected GUI route to reach Kuro Belt flight.",
        ),
        (
            "station-save.yaml",
            {"screen": "station", "body_text": "Helion Prime"},
            {"screen": "station", "currentStationId": "helion-prime"},
            None,
            "Expected Helion Prime station with auto save.",
        ),
        (
            "save-restore.yaml",
            {"screen": "station", "body_text": "Helion Prime"},
            None,
            {"screen": "station", "currentStationId": "helion-prime"},
            "Expected manual-1 station save created and restorable.",
        ),
        (
            "trading-cargo.yaml",
            {"screen": "station", "body_text": "Helion Prime Market"},
            {"player": {"cargo": {"basic-food": 2}, "credits": 1726}},
            None,
            "Expected Basic Food cargo to be 2 and credits >= 1726 after GUI sell.",
        ),
    ],
)
def test_spacerpg_from_start_baseline_grades_from_declared_surface(
    name: str,
    visible: dict,
    save_auto: dict | None,
    save_manual: dict | None,
    reason: str,
) -> None:
    page = MagicMock()
    page.evaluate.side_effect = [visible, save_auto, save_manual]
    result = spacerpg_gui_baseline._grade_spacerpg_gui_baseline(
        SimpleNamespace(page=page),
        SimpleNamespace(id="must-not-drive-grading"),
        load_spec(ROOT / "specs/spacerpg" / name),
    )

    assert result["status"] == "success"
    assert result["reason"] == reason
    assert result["data"] == {
        "visible": visible,
        "save_auto": save_auto,
        "save_manual_1": save_manual,
    }


def test_spacerpg_from_start_baseline_single_check_failure_remains_not_reached() -> None:
    page = MagicMock()
    page.evaluate.side_effect = [
        {"screen": "flight", "body_text": "Flight HUD Helion Reach"},
        None,
        None,
    ]
    result = spacerpg_gui_baseline._grade_spacerpg_gui_baseline(
        SimpleNamespace(page=page),
        SimpleNamespace(id="must-not-drive-grading"),
        load_spec(ROOT / "specs/spacerpg/galaxy-jump.yaml"),
    )

    assert result["status"] == "not_reached"
    assert result["reason"] == "Expected GUI route to reach Kuro Belt flight."


def test_accessors_are_read_only_copies_and_legacy_specs_stay_root_shaped() -> None:
    task_id = "spacerpg-playable-flight"
    raw = formal_spec(task_id)
    contract = formal_evaluation_contract(task_id)
    surface = formal_surface_contract(task_id)
    assert raw is not None and contract is not None and surface is not None
    assert "assertions" not in raw and "visual_oracle" not in raw
    raw["evaluation"]["schema_version"] = 999
    contract["schema_version"] = 999
    surface["adapter"] = "tampered"
    assert formal_spec(task_id)["evaluation"]["schema_version"] == 1
    assert formal_evaluation_contract(task_id)["schema_version"] == 1
    assert formal_surface_contract(task_id)["adapter"] == (
        "spacerpg_production_dom_save_v1"
    )

    materialized = formal_spec(task_id, materialized=True)
    assert materialized["assertions"] == semantic_assertions(formal_spec(task_id))
    assert materialized["visual_oracle"] == visual_oracle(formal_spec(task_id))

    legacy = {
        "assertions": [{"type": "state_equals", "path": "x", "value": 1}],
        "visual_oracle": {"description": "legacy"},
    }
    projected = materialize_legacy_spec(legacy)
    assert projected == legacy
    assert projected is not legacy
    projected["assertions"].append({"type": "no_severe_console"})
    assert len(legacy["assertions"]) == 1


def test_oracle_provenance_uses_evaluation_v1_disk_shape() -> None:
    path = ROOT / "specs/2048/merge-left.yaml"
    raw = load_raw_spec(path)
    materialized = load_spec(path)
    raw["experiment_seed"] = 0
    materialized["experiment_seed"] = 0

    assert set(materialized) - set(raw) == {"assertions", "visual_oracle"}
    assert _oracle_spec_digest(materialized) == _oracle_spec_digest(raw)
    assert _oracle_fingerprint("child-commit", materialized) == _oracle_fingerprint(
        "child-commit", raw
    )


def test_comparison_engine_preserves_order_and_report_fields() -> None:
    declarations = [
        {
            "name": "equal.none",
            "metric": "equals",
            "actual": {"probe": "common.path", "phase": "current", "path": "none"},
            "expected": {"value": None},
        },
        {
            "name": "greater",
            "metric": "greater_than",
            "actual": {"probe": "common.path", "phase": "current", "path": "high"},
            "expected": {"value": 2},
        },
        {
            "name": "minimum",
            "metric": "greater_or_equal",
            "actual": {"probe": "common.path", "phase": "current", "path": "minimum"},
            "expected": {"value": 3},
        },
        {
            "name": "substring",
            "metric": "contains",
            "actual": {"probe": "common.path", "phase": "current", "path": "label"},
            "expected": {"value": "Reach"},
        },
    ]
    rows = evaluate_checks(
        declarations,
        EvaluationContext(
            spec={},
            current={"none": None, "high": 3, "minimum": 3, "label": "Helion Reach"},
        ),
    )

    assert rows == [
        {"name": "equal.none", "ok": True, "actual": None, "expected": None},
        {"name": "greater", "ok": True, "actual": 3, "baseline": 2},
        {"name": "minimum", "ok": True, "actual": 3, "expected_min": 3},
        {
            "name": "substring",
            "ok": True,
            "actual": "Helion Reach",
            "expected": "Reach",
        },
    ]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda spec: spec.update(assertions=[]), "must not mix root"),
        (
            lambda spec: spec["evaluation"].update(schema_version=2),
            "schema_version must be 1",
        ),
        (
            lambda spec: spec["evaluation"]["semantic"]["surfaces"]["gui"].update(
                adapter="unknown_adapter"
            ),
            "registered adapter",
        ),
        (
            lambda spec: spec["evaluation"]["semantic"]["surfaces"]["gui"][
                "start"
            ]["checks"][0]["actual"].update(probe="unknown.probe"),
            "registered probe",
        ),
        (
            lambda spec: spec["evaluation"]["semantic"]["surfaces"]["gui"][
                "start"
            ]["checks"][0]["actual"].update(probe="love.new_log_entries"),
            "not registered for adapter",
        ),
        (
            lambda spec: spec["evaluation"]["semantic"]["surfaces"]["gui"][
                "start"
            ]["checks"][0].update(metric="eval"),
            "registered comparison",
        ),
        (
            lambda spec: spec["evaluation"]["semantic"]["surfaces"]["gui"][
                "start"
            ]["checks"].append(
                copy.deepcopy(
                    spec["evaluation"]["semantic"]["surfaces"]["gui"][
                        "start"
                    ]["checks"][0]
                )
            ),
            "duplicate name",
        ),
        (
            lambda spec: spec["evaluation"]["semantic"]["surfaces"]["gui"][
                "start"
            ]["checks"][0].update(actual={"expression": "__import__('os')"}),
            "exactly one of value or probe",
        ),
        (
            lambda spec: spec["evaluation"]["semantic"]["surfaces"]["gui"][
                "start"
            ]["checks"][0].update(import_="os"),
            "exactly name, metric, actual, expected",
        ),
        (
            lambda spec: spec["evaluation"]["semantic"]["surfaces"]["gui"][
                "start"
            ]["checks"][0]["actual"].pop("phase"),
            "requires exactly arguments",
        ),
    ],
)
def test_contract_rejects_unregistered_or_executable_dsl(
    mutate,
    message: str,
) -> None:
    path = ROOT / "specs/spacerpg/playable-flight.yaml"
    spec = load_raw_spec(path)
    mutate(spec)

    assert any(message in error for error in validate_evaluation_contract(spec, path))


def test_formal_runtime_and_aggregator_have_no_task_shape_inference_tables() -> None:
    public_source = (ROOT / "warptest/c1_public_semantics.py").read_text(
        encoding="utf-8"
    )
    aggregate_source = (ROOT / "warptest/p5_aggregate.py").read_text(
        encoding="utf-8"
    )
    unity_source = (ROOT / "warptest/c1_unity_runtime.py").read_text(
        encoding="utf-8"
    )
    baseline_source = (ROOT / "warptest/spacerpg_gui_baseline.py").read_text(
        encoding="utf-8"
    )

    for forbidden in (
        "_is_spacerpg_",
        "_love_action_id",
        "_love_action_index",
        "_count_throttle_intents",
    ):
        assert forbidden not in public_source
    for forbidden in (
        "_C1_OPENRA_SEMANTIC_CHECK_CONTRACTS",
        "_C1_OPENRA_GAMEPLAY_SEMANTIC_CHECK_CONTRACTS",
        "_C1_UNITY_SEMANTIC_CHECK_CONTRACTS",
        "_C1_UNITY_SAVE_LOAD_TASK_SLOTS",
    ):
        assert forbidden not in aggregate_source
    for task_id in (
        "spacerpg-playable-flight",
        "jynew-save-restore",
        "pal3-scene-restore",
    ):
        assert task_id not in public_source
        assert task_id not in aggregate_source
        assert task_id not in unity_source
        assert task_id not in baseline_source


def test_hidden_evaluation_contract_does_not_enter_agent_prompts() -> None:
    task = next(
        task
        for task in load_state_loop_tasks(ROOT, "benchmarks/p5/c1_tasks.yaml")
        if task.id == "spacerpg-playable-flight"
    )
    rendered = "\n".join(
        [
            gui_agent._task_preconditions(task),
            gui_agent._public_phase_b_instruction(task),
            build_llm_checkpoint_prompt(task.issue, {}),
            build_explore_system_prompt(task.issue),
            build_explore_initial_prompt(
                task.issue,
                ROOT / "games/SpaceRPG",
                "public repo map",
            ),
        ]
    )

    for hidden in (
        "transition.throttle_delta",
        "transition.throttle_intent_count",
        "spacerpg.throttle_percent",
        "gui_from_start_baseline",
        "golden_after",
        "visual_oracle",
        "evaluation:",
    ):
        assert hidden not in rendered
    # Model-generated specs deliberately keep their historical root interface.
    assert "assertions:" in rendered
