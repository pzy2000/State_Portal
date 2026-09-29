from __future__ import annotations

from warptest.c1_lazarus_selection.luanti_lua_downstream import (
    describe_luanti_lua_task_mapping,
)
from warptest.c1_lazarus_selection.models import CandidateKey


def test_all_four_lua_candidates_have_at_goal_runtime_designs() -> None:
    expected = {
        9722: (
            "lz-luanti-9722-reversible-number-serialize",
            "luanti_worldmod_serialize_roundtrip",
        ),
        12703: (
            "lz-luanti-12703-contentdb-overwrite",
            "luanti_mainmenu_install_overwrite",
        ),
        13132: (
            "lz-luanti-13132-empty-separator",
            "luanti_worldmod_split_empty_separator",
        ),
        14757: (
            "lz-luanti-14757-math-round",
            "luanti_worldmod_math_round_boundary",
        ),
    }

    for number, (task_id, action_type) in expected.items():
        mapping = describe_luanti_lua_task_mapping(CandidateKey("luanti", number))
        assert mapping.task_id == task_id
        assert mapping.defect_locus == "at_goal"
        assert mapping.replay_actions[-1]["type"] == action_type
        assert mapping.goal_check != mapping.defect_signature
        assert mapping.invariant_checks
        assert mapping.fixture_files
        assert mapping.downstream_state == "pending"
        assert mapping.golden_disposition == "required_no_not_applicable"


def test_pure_lua_worldmods_store_semantic_result_before_rendered_marker() -> None:
    for number in (9722, 13132, 14757):
        mapping = describe_luanti_lua_task_mapping(CandidateKey("luanti", number))
        init = dict(mapping.fixture_files)["mods/c1_lazarus/init.lua"]
        assert "get_mod_storage" in init
        assert "set_string" in init
        assert "set_node" in init
        assert "pcall" in init


def test_contentdb_mapping_uses_mainmenu_fixture_not_a_worldmod() -> None:
    mapping = describe_luanti_lua_task_mapping(CandidateKey("luanti", 12703))

    paths = {path for path, _content in mapping.fixture_files}
    assert paths == {
        "client/menu/start.json",
        "client/menu/package.zip",
        "client/menu/target/init.lua",
    }
    assert mapping.replay_actions[-1] == {
        "type": "luanti_mainmenu_install_overwrite",
        "package": "client/menu/package.zip",
        "target": "client/menu/target",
    }


def test_14757_round_action_is_deferred_until_one_explicit_chat_trigger() -> None:
    mapping = describe_luanti_lua_task_mapping(CandidateKey("luanti", 14757))

    assert mapping.start_definition["world_nodes"][1] == {
        "pos": {"x": 0, "y": 1, "z": 0},
        "node": "basenodes:cobble",
    }
    assert mapping.start_definition["result_key"] == "result"
    init = dict(mapping.fixture_files)["mods/c1_lazarus/init.lua"]
    registration = 'minetest.register_chatcommand("c1_lazarus_round"'
    assert registration in init
    assert "\nobserve(" not in init[: init.index(registration)]
    assert init.index("math.round(") > init.index(registration)
    assert init.index("observe(", init.index(registration)) > init.index(registration)
    assert 'storage:get_string("action_executed") == "true"' in init
    assert 'storage:set_string("action_executed", "true")' in init
    assert mapping.replay_actions[-1] == {
        "type": "luanti_worldmod_math_round_boundary",
        "command": "/c1_lazarus_round",
    }
