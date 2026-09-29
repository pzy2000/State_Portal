from __future__ import annotations

from pathlib import Path

import yaml

from warptest.runner import load_spec


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "benchmarks/p5/c1_tasks.yaml"
TASK_IDS = {
    "love-elena-dialogue-memory-consolidation",
    "love-elena-partner-ending-unlock",
    "luanti-self-sufficient-homestead-roundtrip",
    "luanti-workshop-order-staging",
}


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


def _tasks() -> dict[str, dict]:
    return {task["id"]: task for task in _manifest()["tasks"]}


def _spec(task: dict) -> dict:
    return load_spec(ROOT / task["oracle_spec"])


def _inventory_counts(spec: dict) -> dict[str, int]:
    return {
        row["item"]: row["count"]
        for row in spec["target"]["player"]["inventory"]
    }


def test_fixed_tasks_remain_in_c1_v5_61_task_manifest() -> None:
    manifest = _manifest()
    tasks = _tasks()
    assert manifest["schema_version"] == 5
    assert manifest["task_schema"] == "c1_v5"
    assert len(tasks) == 61
    assert TASK_IDS <= tasks.keys()


def test_love_phase_b_uses_only_public_dom_actions() -> None:
    tasks = _tasks()
    allowed = {"click", "wait", "settle_events"}
    for task_id in (
        "love-elena-dialogue-memory-consolidation",
        "love-elena-partner-ending-unlock",
    ):
        spec = _spec(tasks[task_id])
        assert {action["type"] for action in spec["actions"]} <= allowed
        serialized = yaml.safe_dump(spec, sort_keys=True)
        assert "patchState" not in serialized
        assert "applyRelationshipUpdates" not in serialized


def test_love_partner_start_and_public_commit_action_are_legal() -> None:
    task = _tasks()["love-elena-partner-ending-unlock"]
    spec = _spec(task)
    assert spec["target"]["patch"]["player"]["relationships"]["char_1"]["status"] == "dating"
    selectors = [
        action.get("selector")
        for action in spec["actions"]
        if action["type"] == "click"
    ]
    assert '[data-action-id="date_coffee"]' in selectors
    assert '[data-action-id="commit_elena_partner"]' in selectors
    source = (ROOT / "games/DatingSim/src/engine/ActionSystem.ts").read_text(
        encoding="utf-8"
    )
    assert "id: 'commit_elena_partner'" in source
    assert "elena_partner_ending_ready: false" in source
    assert "playerTitle: 'Committed Partner'" in source


def test_luanti_fixed_tasks_have_no_mod_storage_contract() -> None:
    tasks = _tasks()
    for task_id in (
        "luanti-self-sufficient-homestead-roundtrip",
        "luanti-workshop-order-staging",
    ):
        task = tasks[task_id]
        spec = _spec(task)
        serialized = yaml.safe_dump(spec, sort_keys=True)
        public_text = f"{task['preconditions']}\n{task['reference_steps']}"
        assert "mod_storage" not in serialized
        assert "warptest_homestead_stage" not in serialized
        assert "warptest_order" not in serialized
        assert "homestead stage" not in public_text.lower()
        assert "order state" not in public_text.lower()


def test_homestead_supplies_and_partial_roof_match_public_steps() -> None:
    spec = _spec(_tasks()["luanti-self-sufficient-homestead-roundtrip"])
    assert {
        "from": {"x": -1, "y": 4, "z": 0},
        "to": {"x": 6, "y": 4, "z": 3},
        "node": "stairs:slab_cobble",
        "param2": 0,
    } in spec["target"]["fill"]
    assert _inventory_counts(spec) == {
        "stairs:slab_cobble": 24,
        "stairs:stair_cobble": 1,
        "basenodes:junglegrass": 24,
        "basenodes:cobble": 64,
        "basenodes:tree": 32,
        "basenodes:apple": 14,
        "basetools:pick_steel": 1,
    }
    roof_assertion = next(
        assertion
        for assertion in spec["assertions"]
        if assertion["type"] == "luanti_node_count"
        and assertion["node"] == "stairs:slab_cobble"
    )
    assert roof_assertion["min_count"] == 56


def test_workshop_player_has_exact_restocking_delta() -> None:
    spec = _spec(_tasks()["luanti-workshop-order-staging"])
    assert spec["target"]["player"]["pos"] == {"x": -2, "y": 2, "z": 3}
    assert _inventory_counts(spec) == {
        "basenodes:stone": 40,
        "basenodes:cobble": 40,
        "basenodes:tree": 24,
        "basenodes:leaves": 24,
        "basetools:pick_steel": 1,
        "basenodes:mossycobble": 26,
    }
    final_inventory = next(
        action
        for action in spec["actions"]
        if action["type"] == "luanti_set_player_inventory"
    )
    assert final_inventory["items"] == []
