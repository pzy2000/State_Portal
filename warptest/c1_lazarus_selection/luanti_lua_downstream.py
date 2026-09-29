"""Registered pending task designs for admitted Luanti Lua regressions.

These definitions close the authoring ambiguity without claiming execution.
They specify a deterministic at-goal Start, action, semantic differential and
renderable marker for each candidate.  Version materialization, replay, raw
observations, goldens and construction receipts remain pending until a trusted
runner executes the designs against the complete fixed and buggy applications.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .models import CandidateKey


@dataclass(frozen=True)
class LuantiLuaTaskMapping:
    candidate: CandidateKey
    issue_number: int
    task_id: str
    profile_id: str
    defect_locus: str
    start_definition: Mapping[str, object]
    replay_actions: tuple[Mapping[str, object], ...]
    goal_check: str
    defect_signature: str
    invariant_checks: tuple[str, ...]
    fixture_files: tuple[tuple[str, str], ...]
    downstream_state: str = "pending"
    golden_disposition: str = "required_no_not_applicable"


_WORLDMOD_HEADER = """local storage = minetest.get_mod_storage()
local marker = {x = 0, y = 1, z = 0}
local function observe(value, ok)
  storage:set_string("result", tostring(value))
  storage:set_string("ok", ok and "true" or "false")
  minetest.set_node(marker, {name = ok and "basenodes:dirt_with_grass" or "basenodes:desert_sand"})
end
"""


_MAPPINGS = {
    9722: LuantiLuaTaskMapping(
        candidate=CandidateKey("luanti", 9722),
        issue_number=7925,
        task_id="lz-luanti-9722-reversible-number-serialize",
        profile_id="luanti-9722-reversible-number-serialize-v1",
        defect_locus="at_goal",
        start_definition={
            "world_nodes": [
                {"pos": {"x": 0, "y": 0, "z": 0}, "node": "basenodes:stone"},
                {"pos": {"x": 0, "y": 1, "z": 0}, "node": "basenodes:cobble"},
            ],
            "player": {
                "pos": {"x": 0, "y": 3, "z": 6},
                "look_at": {"x": 0, "y": 1, "z": 0},
            },
            "result_key": "result",
        },
        replay_actions=(
            {"type": "restore_registered_start"},
            {
                "type": "luanti_worldmod_serialize_roundtrip",
                "values": [0.2695949158945771, 269594915894577],
            },
        ),
        goal_check="both registered numbers survive serialize then deserialize exactly",
        defect_signature="at least one registered number changes after serialize then deserialize",
        invariant_checks=(
            "the world remains responsive",
            "the marker node and mod-storage result belong to the same replay",
        ),
        fixture_files=(
            (
                "mods/c1_lazarus/init.lua",
                _WORLDMOD_HEADER
                + """local values = {0.2695949158945771, 269594915894577}
local success, restored = pcall(function()
  local text = minetest.serialize(values)
  return minetest.deserialize(text)
end)
local ok = success and restored and restored[1] == values[1] and restored[2] == values[2]
observe(success and minetest.serialize(restored) or "runtime_error", ok)
""",
            ),
            ("mods/c1_lazarus/mod.conf", "name = c1_lazarus\n"),
        ),
    ),
    12703: LuantiLuaTaskMapping(
        candidate=CandidateKey("luanti", 12703),
        issue_number=12303,
        task_id="lz-luanti-12703-contentdb-overwrite",
        profile_id="luanti-12703-contentdb-overwrite-v1",
        defect_locus="at_goal",
        start_definition={
            "surface": "mainmenu_contentdb",
            "existing_package_path": "client/menu/target",
            "overwrite_dialog_open": True,
        },
        replay_actions=(
            {"type": "restore_registered_start"},
            {
                "type": "luanti_mainmenu_install_overwrite",
                "package": "client/menu/package.zip",
                "target": "client/menu/target",
            },
        ),
        goal_check="overwrite completes and the installed package content is replaced",
        defect_signature="overwrite produces the registered package-manager error or leaves old content",
        invariant_checks=(
            "the main menu remains responsive",
            "the install source and target are confined to the disposable profile",
        ),
        fixture_files=(
            ("client/menu/start.json", '{"overwrite_dialog_open":true}\n'),
            (
                "client/menu/package.zip",
                "registered fixture placeholder; package writer must replace this before execution\n",
            ),
            ("client/menu/target/init.lua", "return 'old-content'\n"),
        ),
    ),
    13132: LuantiLuaTaskMapping(
        candidate=CandidateKey("luanti", 13132),
        issue_number=13128,
        task_id="lz-luanti-13132-empty-separator",
        profile_id="luanti-13132-empty-separator-v1",
        defect_locus="at_goal",
        start_definition={
            "world_nodes": [
                {"pos": {"x": 0, "y": 0, "z": 0}, "node": "basenodes:stone"},
                {"pos": {"x": 0, "y": 1, "z": 0}, "node": "basenodes:cobble"},
            ],
            "player": {
                "pos": {"x": 0, "y": 3, "z": 6},
                "look_at": {"x": 0, "y": 1, "z": 0},
            },
            "watchdog_required": True,
        },
        replay_actions=(
            {"type": "restore_registered_start"},
            {
                "type": "luanti_worldmod_split_empty_separator",
                "value": "",
                "separator": "",
            },
        ),
        goal_check="the registered action returns a bounded argument error while the server remains responsive",
        defect_signature="the registered action exceeds its watchdog bound and the server stops responding",
        invariant_checks=(
            "the Start marker is equal before the action",
            "no unrelated world node changes before watchdog classification",
        ),
        fixture_files=(
            (
                "mods/c1_lazarus/init.lua",
                _WORLDMOD_HEADER
                + """local success, value = pcall(function() return string.split("", "") end)
observe(success and tostring(value) or tostring(value), not success)
""",
            ),
            ("mods/c1_lazarus/mod.conf", "name = c1_lazarus\n"),
        ),
    ),
    14757: LuantiLuaTaskMapping(
        candidate=CandidateKey("luanti", 14757),
        issue_number=14755,
        task_id="lz-luanti-14757-math-round",
        profile_id="luanti-14757-math-round-v1",
        defect_locus="at_goal",
        start_definition={
            "world_nodes": [
                {"pos": {"x": 0, "y": 0, "z": 0}, "node": "basenodes:stone"},
                {"pos": {"x": 0, "y": 1, "z": 0}, "node": "basenodes:cobble"},
            ],
            "player": {
                "pos": {"x": 0, "y": 3, "z": 6},
                "look_at": {"x": 0, "y": 1, "z": 0},
            },
            "result_key": "result",
        },
        replay_actions=(
            {"type": "restore_registered_start"},
            {
                "type": "luanti_worldmod_math_round_boundary",
                "command": "/c1_lazarus_round",
            },
        ),
        goal_check="both boundary values round to zero",
        defect_signature="either boundary value rounds away from zero",
        invariant_checks=(
            "the world remains responsive",
            "the marker node and mod-storage result belong to the same replay",
        ),
        fixture_files=(
            (
                "mods/c1_lazarus/init.lua",
                _WORLDMOD_HEADER
                + """minetest.register_on_mods_loaded(function()
  storage:set_string("result", "")
  storage:set_string("action_executed", "")
  minetest.set_node(marker, {name = "basenodes:cobble"})
  minetest.log("action", "C1_LAZARUS_14757_START marker=basenodes:cobble result_empty=true")
end)

minetest.register_chatcommand("c1_lazarus_round", {
  description = "Run the registered C1 Lazarus math.round boundary action",
  func = function()
    if storage:get_string("action_executed") == "true" then
      return false, "action already executed"
    end
    storage:set_string("action_executed", "true")
    local success, values = pcall(function()
  return {math.round(0.49999999999999994), math.round(-0.49999999999999994)}
    end)
    local ok = success and values[1] == 0 and values[2] == 0
    local result = success and table.concat(values, ",") or "runtime_error"
    observe(result, ok)
    minetest.log("action", "C1_LAZARUS_14757_ACTION command=/c1_lazarus_round count=1 result=" .. result .. " ok=" .. tostring(ok))
    return true, result
  end,
})
""",
            ),
            ("mods/c1_lazarus/mod.conf", "name = c1_lazarus\n"),
        ),
    ),
}


def describe_luanti_lua_task_mapping(candidate: CandidateKey) -> LuantiLuaTaskMapping:
    if type(candidate) is not CandidateKey or candidate.repository_key != "luanti":
        raise ValueError("Luanti Lua task mapping candidate is invalid")
    try:
        return _MAPPINGS[candidate.change_number]
    except KeyError as error:
        raise ValueError("Luanti Lua task mapping candidate is unsupported") from error


__all__ = ["LuantiLuaTaskMapping", "describe_luanti_lua_task_mapping"]
