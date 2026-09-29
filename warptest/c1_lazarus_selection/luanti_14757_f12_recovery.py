"""Deterministic recovery pair for Luanti #14757 F12 visual closure.

The first bounded F12 pair produced valid PNGs but used an empty world with no
map seed or authoritative camera. The two variants therefore rendered
different spawn terrain before the action. This adapter prepares one
candidate-specific world snapshot (fixed seed, flat mapgen, frozen lighting,
and the registered start camera) and copies it byte-for-byte into fresh fixed
and buggy namespaces. The registered action is unchanged; it still changes
the marker node from cobble to the fixed/buggy result, so the after cross
matrix remains a real engine-rendered semantic difference.

Only one fresh fixed-then-buggy pair may be executed through
execute_deterministic_recovery_pair. It never loads the previous F12 run,
touches protected processes, synthesizes frames, or authorizes paid/formal
trials.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ..native_input import NativeInputUnavailableError, require_unlocked_native_session
from .luanti_14757_f12_visual import (
    CANDIDATE,
    VisualObservation,
    VisualPairResult,
    VisualSpec,
    _capture_f12,
    _frame_dimensions,
    _load,
    _ordinary_directory,
    _ordinary_file,
    _ordinary_root,
    _read_log,
    _reserve_run_root,
    _START_RE,
    _stop_owned_process_group,
    _validate_receipts,
    _write,
    build_client_command,
    frame_is_non_null,
    parse_visual_markers,
    visual_cross_matrix,
)
from .stable_protocol import ArtifactReference, AuthorizationRecord, StableArtifact

EXECUTION_CONFIRMATION = "EXECUTE_LUANTI_14757_F12_SCENE_RECOVERY_V2"
_V1 = "1.0"
_V2 = "2.0"
_V3 = "3.0"
_V4 = "4.0"
_WATCHDOG_SECONDS = 60
_ACTION_DELAY_SECONDS = 45.0
_PROTECTED_PIDS = (20946, 21627, 30865, 70361, 60839)
_RUN_PARENT = PurePosixPath("private/bounded-runs/luanti/14757")
_WORLD_SEED = 14757
_MAPGEN = "flat"
_PLAYER_POS = {"x": 0, "y": 3, "z": 6}
_LOOK_AT = {"x": 0, "y": 2, "z": 0}
_SCREEN = (1280, 720)
_CAMERA_MARKER = "C1_LAZARUS_14757_CAMERA ready=true"
_HUD_MARKER = "C1_LAZARUS_14757_HUD stable=true"
_SCENE_BEFORE_MARKER = "C1_LAZARUS_14757_SCENE before=true"
_SCENE_AFTER_MARKER = "C1_LAZARUS_14757_SCENE after=true"

_GATE_AGGREGATE = ArtifactReference("lua_regression_aggregate/luanti/14757/fixed-buggy/v3", "lua_regression_aggregate", _V3)
_GATE_TRANSITION = ArtifactReference("luanti_lua_regression_transition/luanti/14757/test-repetitions/v2", "luanti_lua_regression_transition", _V2)
_MAPPING = ArtifactReference("lazarus_task_mapping/luanti/14757/lz-luanti-14757-math-round/v4", "lazarus_task_mapping", _V4)
_REPACKAGE = ArtifactReference("candidate_downstream_repackage_receipt/luanti/14757/repackage-v3/v1", "candidate_downstream_repackage_receipt", _V1)
_GATE9 = ArtifactReference("candidate_gate9_server_side_replay/luanti/14757/repackage-v3/v1", "candidate_gate9_server_side_replay", _V1)
_GATE10_OBSERVATION = ArtifactReference("candidate_gate10_server_side_observation/luanti/14757/repackage-v3/v1", "candidate_gate10_server_side_observation", _V1)
_GATE10_ORACLE = ArtifactReference("candidate_gate10_server_side_oracle/luanti/14757/repackage-v3/v1", "candidate_gate10_server_side_oracle", _V1)
_PREVIOUS_BLOCKER = ArtifactReference(
    "candidate_gate12_f12_blackfix_blocker/luanti/14757/repackage-v3/v1",
    "candidate_gate12_f12_blackfix_blocker",
    _V1,
)

# The scene-v2 and blackfix-v3 receipts above are immutable historical
# evidence.  The HUD-stability recovery gets another stable-ID family so no
# old head is advanced or accidentally treated as a golden.
_PLAN = ArtifactReference(
    "candidate_gate11_f12_hudfix_plan/luanti/14757/repackage-v3/v1",
    "candidate_gate11_f12_hudfix_plan",
    _V1,
)
_CONSTRUCTION = ArtifactReference(
    "candidate_gate13_f12_hudfix_construction/luanti/14757/repackage-v3/v1",
    "candidate_gate13_f12_hudfix_construction",
    _V1,
)
_OBSERVATION = ArtifactReference(
    "candidate_gate11_f12_hudfix_observation/luanti/14757/repackage-v3/v1",
    "candidate_gate11_f12_hudfix_observation",
    _V1,
)
_MATRIX = ArtifactReference(
    "candidate_gate12_f12_hudfix_matrix/luanti/14757/repackage-v3/v1",
    "candidate_gate12_f12_hudfix_matrix",
    _V1,
)
_BLOCKER = ArtifactReference(
    "candidate_gate12_f12_hudfix_blocker/luanti/14757/repackage-v3/v1",
    "candidate_gate12_f12_hudfix_blocker",
    _V1,
)
_FINAL_RECEIPT = ArtifactReference(
    "candidate_gate13_f12_hudfix_receipt/luanti/14757/repackage-v3/v1",
    "candidate_gate13_f12_hudfix_receipt",
    _V1,
)
_REPAIR_FIXED = ArtifactReference(
    "candidate_gate11_f12_groundlevel_attempt/luanti/14757/repackage-v3/fixed/1/v1",
    "candidate_gate11_f12_groundlevel_attempt",
    _V1,
)
_REPAIR_BUGGY = ArtifactReference(
    "candidate_gate11_f12_groundlevel_attempt/luanti/14757/repackage-v3/buggy/1/v1",
    "candidate_gate11_f12_groundlevel_attempt",
    _V1,
)
_REPAIR_RECEIPT = ArtifactReference(
    "candidate_gate12_f12_groundlevel_repair/luanti/14757/repackage-v3/v1",
    "candidate_gate12_f12_groundlevel_repair",
    _V1,
)


def deterministic_visual_profile() -> dict[str, object]:
    return {
        "world_seed": _WORLD_SEED,
        "mapgen": _MAPGEN,
        "mapgen_params": {"mgflat_ground_level": 0, "water_level": -31000},
        "world_copy_contract": "one-byte-identical-world-template-copied-to-both-variants",
        "player": {"pos": dict(_PLAYER_POS), "look_at": dict(_LOOK_AT), "eye_height": 1.5, "gravity": 0},
        "render": {
            "screen": list(_SCREEN),
            "time_speed": 0,
            "enable_clouds": False,
            "enable_fog": False,
            "fullscreen": False,
            "capture_backend": "luanti_builtin_f12_engine_png_v1",
            "synthetic_frames": False,
        },
        "action_visibility": {
            "marker": dict(_LOOK_AT),
            "start_node": "basenodes:cobble",
            "fixed_after_node": "basenodes:dirt_with_grass",
            "buggy_after_node": "basenodes:desert_sand",
            "scene_panel": "7x4 real engine node panel centered on marker",
            "before_scene_marker": _SCENE_BEFORE_MARKER,
            "after_scene_marker": _SCENE_AFTER_MARKER,
        },
    }


def deterministic_config_text(frames: Path) -> str:
    """Build the common map/render config for both app copies."""
    return (
        "server_announce = false\n"
        "creative_mode = true\n"
        "enable_damage = false\n"
        "time_speed = 0\n"
        f"fixed_map_seed = {_WORLD_SEED}\n"
        f"mg_name = {_MAPGEN}\n"
        "mgflat_ground_level = 0\n"
        "water_level = -31000\n"
        "enable_clouds = false\n"
        "enable_fog = false\n"
        "show_debug = false\n"
        "screen_w = 1280\n"
        "screen_h = 720\n"
        "fullscreen = false\n"
        f"screenshot_path = {frames}\n"
        "screenshot_format = png\n"
    )


def _defer_fixture_start_callback(fixture_text: str) -> str:
    """Move the fixture's initial set_node callback behind map emergence.

    The source fixture registers its start callback before this adapter is
    appended.  Running that callback immediately after ``mods_loaded`` can
    write into an ungenerated mapblock, which was the direct cause of the
    black F12 frames.  The callback body is preserved byte-for-byte; only its
    delivery mechanism is changed by the candidate-specific adapter.
    """
    pattern = re.compile(
        r"minetest\.register_on_mods_loaded\(function\(\)\n(?P<body>.*?)\nend\)",
        re.DOTALL,
    )
    matches = list(pattern.finditer(fixture_text))
    if len(matches) != 1:
        raise ValueError("#14757 fixture must have exactly one initial start callback")
    match = matches[0]
    body = match.group("body")
    if "C1_LAZARUS_14757_START" not in body or "minetest.set_node(" not in body:
        raise ValueError("#14757 fixture start callback does not match the registered task")
    replacement = f"local c1_original_start = function()\n{body}\nend"
    return fixture_text[: match.start()] + replacement + fixture_text[match.end() :]


def inject_deterministic_visual_fixture(
    fixture_text: str,
) -> str:
    """Add authoritative camera and visual delivery without changing action body."""
    if type(fixture_text) is not str:
        raise TypeError("#14757 deterministic visual fixture must be text")
    if (
        'minetest.register_chatcommand("c1_lazarus_round"' not in fixture_text
        or "C1_LAZARUS_14757_ACTION" not in fixture_text
    ):
        raise ValueError("#14757 deterministic visual fixture lacks registered action")
    fixture_text = _defer_fixture_start_callback(fixture_text)
    camera = f"""

-- Candidate-specific deterministic camera; production action body is unchanged.
local c1_camera_pos = {{x = {_PLAYER_POS["x"]}, y = {_PLAYER_POS["y"]}, z = {_PLAYER_POS["z"]}}}
local c1_camera_target = {{x = {_LOOK_AT["x"]}, y = {_LOOK_AT["y"]}, z = {_LOOK_AT["z"]}}}
local c1_hud_stabilized = false
local function c1_set_camera(player)
  if player == nil or not player:is_player() then
    return
  end
  player:set_pos(c1_camera_pos)
  pcall(function() player:set_physics_override({{gravity = 0}}) end)
  local hud_ok = pcall(function()
    player:hud_set_flags({{basic_debug = false, chat = false}})
  end)
  if hud_ok and not c1_hud_stabilized then
    c1_hud_stabilized = true
    minetest.log("action", "{_HUD_MARKER}")
  end
  local dx = c1_camera_target.x - c1_camera_pos.x
  local dy = c1_camera_target.y - (c1_camera_pos.y + 1.5)
  local dz = c1_camera_target.z - c1_camera_pos.z
  local horiz = math.sqrt(dx * dx + dz * dz)
  player:set_look_horizontal(minetest.dir_to_yaw({{x = dx, y = 0, z = dz}}))
  player:set_look_vertical(-math.atan2(dy, horiz))
  minetest.log("action", "{_CAMERA_MARKER}")
end
minetest.register_on_joinplayer(function(player)
  c1_set_camera(player)
  minetest.after(0.2, function() c1_set_camera(player) end)
end)

-- Candidate-specific real node panel.  The source fixture's initial start
-- callback is invoked only after the map area has been generated and loaded.
-- This avoids writing into an "ignore"/missing mapblock before the client can
-- receive a renderable mesh, which previously produced black F12 frames.
local c1_scene_storage = minetest.get_mod_storage()
local c1_area_min = {{x = -16, y = -16, z = -16}}
local c1_area_max = {{x = 16, y = 16, z = 16}}
local c1_scene_before_started = false
local c1_scene_ready = false
local c1_scene_after_done = false
local c1_emerge_finished = false
_G.C1_LAZARUS_14757_SCENE_READY = function()
  return c1_scene_ready
end

local function c1_panel_matches(name)
  for x = -3, 3 do
    for y = 1, 4 do
      local node = minetest.get_node_or_nil({{x = x, y = y, z = 0}})
      if node == nil or node.name ~= name then
        return false
      end
    end
  end
  return true
end

local function c1_build_scene()
  if c1_scene_before_started then
    return
  end
  c1_scene_before_started = true
  c1_original_start()
  -- Re-apply the authoritative camera after emerge_area has completed,
  -- immediately before publishing the scene to the client.
  for _, player in ipairs(minetest.get_connected_players()) do
    c1_set_camera(player)
  end
  local function c1_repin_camera()
    for _, player in ipairs(minetest.get_connected_players()) do
      c1_set_camera(player)
    end
    if not c1_scene_after_done then
      minetest.after(0.5, c1_repin_camera)
    end
  end
  -- Keep the camera authoritative while the client receives the fresh
  -- mapblock and mesh updates.
  minetest.after(0.5, c1_repin_camera)
  for x = -3, 3 do
    for y = 1, 4 do
      minetest.set_node({{x = x, y = y, z = 0}}, {{name = "basenodes:cobble"}})
    end
    minetest.set_node({{x = x, y = 0, z = 0}}, {{name = "basenodes:stone"}})
    minetest.set_node({{x = x, y = 0, z = 1}}, {{name = "basenodes:stone"}})
  end
  pcall(function() minetest.fix_light(c1_area_min, c1_area_max) end)

  local function publish_before()
    if not c1_panel_matches("basenodes:cobble") then
      minetest.after(0.2, publish_before)
      return
    end
    -- Give the server-to-client mapblock update one full second before the
    -- marker is emitted.  The runner captures only after this marker.
    minetest.after(1.0, function()
      if c1_panel_matches("basenodes:cobble") then
        c1_scene_ready = true
        minetest.log("action", "{_SCENE_BEFORE_MARKER}")
      else
        publish_before()
      end
    end)
  end
  publish_before()
end

local function c1_update_scene_after()
  if c1_scene_after_done or not c1_scene_ready then
    if not c1_scene_after_done then
      minetest.after(0.2, c1_update_scene_after)
    end
    return
  end
  local result = c1_scene_storage:get_string("result")
  if result == "" then
    minetest.after(0.1, c1_update_scene_after)
    return
  end
  local node = result == "0,-0" and "basenodes:dirt_with_grass" or "basenodes:desert_sand"
  for x = -3, 3 do
    for y = 1, 4 do
      minetest.set_node({{x = x, y = y, z = 0}}, {{name = node}})
    end
  end
  pcall(function() minetest.fix_light(c1_area_min, c1_area_max) end)

  local function publish_after()
    if not c1_panel_matches(node) then
      minetest.after(0.2, publish_after)
      return
    end
    minetest.after(1.0, function()
      if c1_panel_matches(node) then
        c1_scene_after_done = true
        minetest.log("action", "{_SCENE_AFTER_MARKER}")
      else
        publish_after()
      end
    end)
  end
  publish_after()
end

local function c1_on_emerged(_, action, calls_remaining)
  if action == minetest.EMERGE_CANCELLED or action == minetest.EMERGE_ERRORED then
    minetest.log("error", "C1_LAZARUS_14757_MAP_EMERGE failed=true")
    return
  end
  if calls_remaining ~= 0 or c1_emerge_finished then
    return
  end
  c1_emerge_finished = true
  pcall(function() minetest.load_area(c1_area_min, c1_area_max) end)
  minetest.after(0, c1_build_scene)
end

minetest.register_on_joinplayer(function()
  -- The panel spans the x=-1 and x=0 mapblocks.  Transient forceload keeps
  -- both blocks active while the client receives their mesh.
  pcall(function() minetest.forceload_block({{x = -8, y = 0, z = 0}}, true) end)
  pcall(function() minetest.forceload_block({{x = 8, y = 0, z = 0}}, true) end)
  minetest.emerge_area(c1_area_min, c1_area_max, c1_on_emerged)
  minetest.after(0.1, c1_update_scene_after)
end)

"""
    from .luanti_14757_f12_visual import inject_delayed_server_trigger

    return inject_delayed_server_trigger(
        fixture_text.rstrip() + camera,
        delay_seconds=_ACTION_DELAY_SECONDS,
        ready_global="C1_LAZARUS_14757_SCENE_READY",
    )


def write_deterministic_recovery_plan(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write the static/TDD-approved plan, without launching either app."""
    _require_authorization(authorization, scope="infrastructure-audit")
    root = _ordinary_root(Path(artifact_root).absolute())
    refs = (
        _GATE_AGGREGATE,
        _GATE_TRANSITION,
        _MAPPING,
        _REPACKAGE,
        _GATE9,
        _GATE10_OBSERVATION,
        _GATE10_ORACLE,
        _PREVIOUS_BLOCKER,
    )
    loaded = {ref.artifact_id: _load(root, ref) for ref in refs}
    _validate_receipts({ref.artifact_id: loaded[ref.artifact_id] for ref in refs[:-1]})
    if loaded[_PREVIOUS_BLOCKER.artifact_id].payload.get("outcome") != "visual_common_before_or_2x2_blocked":
        raise RuntimeError("#14757 scene recovery requires the prior deterministic blocker")
    preflight = require_unlocked_native_session()
    payload = {
        "candidate": CANDIDATE,
        "state": "ready_for_single_bounded_scene_recovery_pair",
        "outcome": "downstream_pending",
        "terminal": False,
        "resumable": True,
        "complete": True,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "previous_blocker_reference": _PREVIOUS_BLOCKER.artifact_id,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "protected_pids": list(_PROTECTED_PIDS),
        "capture_backend": "luanti_builtin_f12_engine_png_v1",
        "native_input_preflight": {
            "console_unlocked": preflight.get("console_unlocked") is True,
            "accessibility_trusted": preflight.get("accessibility_trusted") is True,
            "event_post_access": preflight.get("event_post_access") is True,
        },
        "world_camera_render_profile": deterministic_visual_profile(),
        "action_delivery": {
            "mode": "server_side_after_start_timer",
            "delay_seconds": _ACTION_DELAY_SECONDS,
            "mapping_action_unchanged": True,
            "native_chat_input": False,
        },
        "candidate_adapter": {
            "source_module": "warptest/c1_lazarus_selection/luanti_14757_f12_recovery.py",
            "source_registered_in_production": False,
            "start_callback_deferred_until_emerge": True,
            "scene_ready_global": "C1_LAZARUS_14757_SCENE_READY",
            "camera_repin_interval_seconds": 0.5,
            "panel_node_assertions": True,
            "fix_light_after_scene_writes": True,
            "client_show_debug": False,
            "server_hud_flags": {"basic_debug": False, "chat": False},
            "hud_stability_marker": _HUD_MARKER,
        },
        "run_contract": {
            "new_root_parent": str(_RUN_PARENT),
            "new_namespace_suffix": "f12-visual-hudfix-v4",
            "copy_repackage_v3_before_launch": True,
            "world_snapshot_copied_to_both_variants": True,
            "owned_process_group_only": True,
            "launch_at_most_one_fixed_and_one_buggy": True,
            "retain_raw_observations": True,
            "do_not_reuse_previous_f12_visual_v1": True,
        },
        "static_tdd_requirements": [
            "fixture action body remains unchanged after camera/timer adapter append",
            "fixed and buggy config common lines are identical except screenshot path",
            "shared world snapshot is byte-identical before launch",
            "camera and scene-before markers are required before the before frame",
            "after scene marker is required before the after frame",
            "fixed/buggy real node panels provide a large after visual difference",
            "start callback is not invoked until emerge_area completion",
            "scene-ready global gates the server-side action timer",
            "camera is repinned while the client receives the scene mesh",
            "mapgen ground level and water level keep the panel above ground",
            "client show_debug is false for both fresh configs",
            "server basic_debug and chat HUD flags are false before capture",
            "HUD stability marker is required before the before frame",
        ],
        "minimum_missing_evidence": [
            "fresh_common_before_fixed_after_buggy_after_pngs",
            "before_visual_equivalence",
            "after_2x2_cross_matrix",
        ],
    }
    from .luanti_14757_f12_visual import _try_load

    existing = _try_load(root, _PLAN)
    if existing is None:
        _write(root, _PLAN, refs, payload, authorization)
        existing = _load(root, _PLAN)
    if existing.inputs != refs or dict(existing.payload) != payload:
        raise RuntimeError("#14757 deterministic recovery plan typed reload drifted")
    return existing


def execute_deterministic_recovery_pair(
    artifact_root: str | Path,
    *,
    run_root: str | Path,
    authorization: AuthorizationRecord,
    confirmation: str,
    watchdog_seconds: int = _WATCHDOG_SECONDS,
) -> VisualPairResult:
    """Run the one authorized fresh fixed-then-buggy deterministic pair."""
    _require_authorization(authorization, scope="preannotation-execution")
    if confirmation != EXECUTION_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {EXECUTION_CONFIRMATION!r}")
    if type(watchdog_seconds) is not int or not 5 <= watchdog_seconds <= 600:
        raise ValueError("#14757 deterministic recovery watchdog is invalid")
    root = _ordinary_root(Path(artifact_root).absolute())
    plan = _load(root, _PLAN)
    if plan.payload.get("state") != "ready_for_single_bounded_scene_recovery_pair":
        raise RuntimeError("#14757 scene recovery plan is not executable")
    refs = (
        _GATE_AGGREGATE,
        _GATE_TRANSITION,
        _MAPPING,
        _REPACKAGE,
        _GATE9,
        _GATE10_OBSERVATION,
        _GATE10_ORACLE,
    )
    loaded = {ref.artifact_id: _load(root, ref) for ref in refs}
    _validate_receipts(loaded)
    native_status = require_unlocked_native_session()
    if not all(
        native_status.get(key) is True
        for key in ("console_unlocked", "accessibility_trusted", "event_post_access")
    ):
        raise RuntimeError("#14757 deterministic recovery native preflight did not pass")
    run = _reserve_run_root(root, Path(run_root).absolute())
    shared_world = _prepare_shared_world_template(root, run, loaded[_MAPPING.artifact_id])
    attempts: list[StableArtifact] = []
    specs: list[VisualSpec] = []
    construction: StableArtifact | None = None
    try:
        for variant in ("fixed", "buggy"):
            spec = _prepare_recovery_variant(
                root, run, shared_world, loaded[_REPACKAGE.artifact_id], variant, watchdog_seconds
            )
            specs.append(spec)
            observation = _run_recovery_visual(spec)
            attempt = _write_recovery_attempt(
                root, plan=plan, spec=spec, observation=observation, authorization=authorization
            )
            attempts.append(attempt)
            if not _recovery_semantic_pass(variant, observation):
                blocker = _write_recovery_blocker(
                    root,
                    plan=plan,
                    attempts=tuple(attempts),
                    construction=construction,
                    observation=None,
                    matrix=None,
                    failed_variant=variant,
                    authorization=authorization,
                    reason="deterministic_recovery_semantic_capture_failed",
                )
                return VisualPairResult(plan, construction, tuple(attempts), None, None, blocker)
        construction = _write_recovery_construction(
            root, plan=plan, specs=tuple(specs), authorization=authorization
        )
        observation_artifact = _write_recovery_observation(
            root, plan=plan, construction=construction, attempts=tuple(attempts), authorization=authorization
        )
        matrix = _write_recovery_matrix(
            root,
            plan=plan,
            construction=construction,
            attempts=tuple(attempts),
            observation=observation_artifact,
            authorization=authorization,
        )
        matrix_values = matrix.payload["matrix"]
        if (
            matrix_values.get("before_equivalent") is not True
            or matrix_values.get("after_fixed_buggy_different") is not True
        ):
            blocker = _write_recovery_blocker(
                root,
                plan=plan,
                attempts=tuple(attempts),
                construction=construction,
                observation=observation_artifact,
                matrix=matrix,
                failed_variant="pair",
                authorization=authorization,
                reason="deterministic_recovery_visual_cross_matrix_failed",
            )
            return VisualPairResult(
                plan, construction, tuple(attempts), observation_artifact, matrix, blocker
            )
        _write_recovery_final_receipt(
            root,
            plan=plan,
            construction=construction,
            attempts=tuple(attempts),
            observation=observation_artifact,
            matrix=matrix,
            authorization=authorization,
        )
        return VisualPairResult(
            plan, construction, tuple(attempts), observation_artifact, matrix, None
        )
    except NativeInputUnavailableError as error:
        blocker = _write_recovery_blocker(
            root,
            plan=plan,
            attempts=tuple(attempts),
            construction=construction,
            observation=None,
            matrix=None,
            failed_variant=specs[-1].variant if specs else "fixed",
            authorization=authorization,
            reason=f"native_input_unavailable:{error}",
        )
        return VisualPairResult(plan, construction, tuple(attempts), None, None, blocker)


def _prepare_shared_world_template(root: Path, run: Path, mapping: StableArtifact) -> Path:
    template = run / "shared-world-snapshot"
    template.mkdir(mode=0o700)
    mod = template / "worldmods/c1_lazarus"
    mod.mkdir(parents=True, mode=0o700)
    fixture = root / PurePosixPath(mapping.payload["fixture_root"])
    source_mod = fixture / "mods/c1_lazarus"
    source_init = source_mod / "init.lua"
    mod.joinpath("mod.conf").write_text(
        source_mod.joinpath("mod.conf").read_text(encoding="utf-8"), encoding="utf-8"
    )
    mod.joinpath("init.lua").write_text(
        inject_deterministic_visual_fixture(source_init.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    (template / "world.mt").write_text(
        "gameid = devtest\nbackend = sqlite3\nplayer_backend = sqlite3\nauth_backend = sqlite3\n",
        encoding="utf-8",
    )
    return template


def _prepare_recovery_variant(
    root: Path,
    run: Path,
    shared_world: Path,
    repackage: StableArtifact,
    variant: str,
    watchdog_seconds: int,
) -> VisualSpec:
    if variant not in {"fixed", "buggy"}:
        raise ValueError("#14757 deterministic recovery variant is invalid")
    source_app = root / PurePosixPath(repackage.payload[f"binding_application_{variant}_path"])
    _ordinary_directory(source_app, root)
    target = run / variant
    target.mkdir(parents=True, mode=0o700)
    app = target / f"{variant}-minetest.app"
    shutil.copytree(source_app, app, symlinks=True)
    binary = _ordinary_file(app / "Contents/MacOS/minetest", app)
    if not os.access(binary, os.X_OK):
        raise RuntimeError("#14757 deterministic recovery app is not executable")
    world = target / "world"
    shutil.copytree(shared_world, world, symlinks=True)
    frames = target / "frames"
    frames.mkdir(mode=0o700)
    config = target / "minetest.conf"
    config.write_text(deterministic_config_text(frames), encoding="utf-8")
    log = target / "luanti.log"
    return VisualSpec(variant, target, app, binary, world, config, log, frames, watchdog_seconds)  # type: ignore[arg-type]


def _run_recovery_visual(spec: VisualSpec) -> VisualObservation:
    stdout_path = spec.run_root / "process.stdout"
    stderr_path = spec.run_root / "process.stderr"
    argv = build_client_command(
        binary=spec.binary, world=spec.world, config=spec.config, log=spec.log
    )
    env = dict(os.environ)
    home = spec.run_root / "home"
    home.mkdir(mode=0o700)
    env.update({"HOME": str(home), "LANG": "C", "LC_ALL": "C"})
    try:
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            process = subprocess.Popen(
                argv,
                cwd=spec.app_root,
                env=env,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError as error:
        return VisualObservation(
            "launch_failed", None, parse_visual_markers(""), None, None, None, str(error)
        )
    if process.pid in _PROTECTED_PIDS:
        _stop_owned_process_group(process)
        raise RuntimeError("#14757 deterministic recovery reused a protected PID")
    deadline = time.monotonic() + spec.watchdog_seconds
    while time.monotonic() < deadline:
        text = _read_log(spec.log)
        if (
            _preaction_scene_ready(text)
            and _CAMERA_MARKER in text
            and _HUD_MARKER in text
            and _SCENE_BEFORE_MARKER in text
        ):
            break
        if process.poll() is not None:
            return _finish_recovery_failed(
                process, spec, "client_exited_before_start_camera_or_scene"
            )
        time.sleep(0.05)
    else:
        _stop_owned_process_group(process)
        return VisualObservation(
            "timed_out",
            process.returncode,
            parse_visual_markers(_read_log(spec.log)),
            None,
            None,
            None,
            "start_camera_hud_or_scene_timeout",
        )
    time.sleep(0.2)
    before = _capture_f12(spec, process.pid, known=set())
    if before is None:
        _stop_owned_process_group(process)
        return _finish_recovery_failed(process, spec, "before_f12_png_missing")
    while time.monotonic() < deadline:
        markers = parse_visual_markers(_read_log(spec.log))
        if markers.action_command is not None and _SCENE_AFTER_MARKER in _read_log(spec.log):
            break
        if process.poll() is not None:
            return _finish_recovery_failed(
                process, spec, "client_exited_before_action_or_scene", before=before
            )
        time.sleep(0.05)
    else:
        _stop_owned_process_group(process)
        return VisualObservation(
            "timed_out",
            process.returncode,
            parse_visual_markers(_read_log(spec.log)),
            before,
            None,
            _frame_dimensions(before),
            "action_or_scene_timeout",
        )
    time.sleep(0.4)
    after = _capture_f12(spec, process.pid, known={before})
    if after is None:
        _stop_owned_process_group(process)
        return _finish_recovery_failed(
            process, spec, "after_f12_png_missing", before=before
        )
    time.sleep(0.2)
    _stop_owned_process_group(process)
    return VisualObservation(
        "completed",
        process.returncode,
        parse_visual_markers(_read_log(spec.log)),
        before,
        after,
        _frame_dimensions(after),
        None,
        stdout_path.read_bytes() if stdout_path.is_file() else b"",
        stderr_path.read_bytes() if stderr_path.is_file() else b"",
    )


def _preaction_scene_ready(text: str) -> bool:
    """Recognize Start before the later Trigger/Action markers exist."""
    start = _START_RE.search(text)
    return bool(
        start
        and start.group(1) == "basenodes:cobble"
        and start.group(2) == "true"
    )


def _finish_recovery_failed(
    process: subprocess.Popen[bytes],
    spec: VisualSpec,
    reason: str,
    *,
    before: Path | None = None,
) -> VisualObservation:
    _stop_owned_process_group(process)
    return VisualObservation(
        "launch_failed",
        process.returncode,
        parse_visual_markers(_read_log(spec.log)),
        before,
        None,
        _frame_dimensions(before),
        reason,
    )


def _recovery_semantic_pass(variant: str, observation: VisualObservation) -> bool:
    return (
        observation.process_outcome == "completed"
        and observation.start.ordered
        and observation.start.start_marker == "basenodes:cobble"
        and observation.start.start_result_empty is True
        and observation.start.server_trigger_observed
        and observation.start.action_command == "/c1_lazarus_round"
        and observation.start.action_count == 1
        and observation.before_frame is not None
        and observation.after_frame is not None
        and frame_is_non_null(observation.before_frame)
        and frame_is_non_null(observation.after_frame)
        and observation.start.action_ok is (variant == "fixed")
    )


def _write_recovery_attempt(
    root: Path,
    *,
    plan: StableArtifact,
    spec: VisualSpec,
    observation: VisualObservation,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    ref = ArtifactReference(
        f"candidate_gate11_f12_hudfix_attempt/luanti/14757/repackage-v3/{spec.variant}/1/v1",
        "candidate_gate11_f12_hudfix_attempt",
        _V1,
    )
    payload = {
        "candidate": CANDIDATE,
        "variant": spec.variant,
        "state": "downstream_pending",
        "process_outcome": observation.process_outcome,
        "returncode": observation.returncode,
        "markers": asdict(observation.start),
        "camera_marker": _CAMERA_MARKER,
        "camera_required_before_capture": True,
        "camera_marker_observed": _CAMERA_MARKER in _read_log(spec.log),
        "hud_stability_marker": _HUD_MARKER,
        "hud_stability_marker_observed": _HUD_MARKER in _read_log(spec.log),
        "dynamic_hud_disabled": {
            "client_show_debug": False,
            "server_basic_debug": False,
            "server_chat": False,
        },
        "scene_before_marker": _SCENE_BEFORE_MARKER,
        "scene_after_marker": _SCENE_AFTER_MARKER,
        "scene_markers_observed": {
            "before": _SCENE_BEFORE_MARKER in _read_log(spec.log),
            "after": _SCENE_AFTER_MARKER in _read_log(spec.log),
        },
        "before_frame": str(observation.before_frame.relative_to(root)) if observation.before_frame else None,
        "after_frame": str(observation.after_frame.relative_to(root)) if observation.after_frame else None,
        "frame_dimensions": list(observation.frame_dimensions) if observation.frame_dimensions else None,
        "world_camera_render_profile": deterministic_visual_profile(),
        "synthetic_frames": False,
        "visual_semantic_match": _recovery_semantic_pass(spec.variant, observation),
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(root, ref, (plan.reference,), payload, authorization)


def _write_recovery_construction(
    root: Path,
    *,
    plan: StableArtifact,
    specs: tuple[VisualSpec, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": CANDIDATE,
        "state": "qualified",
        "source_repackage": _REPACKAGE.artifact_id,
        "copied_fixed_app": str(specs[0].app_root.relative_to(root)),
        "copied_buggy_app": str(specs[1].app_root.relative_to(root)),
        "world_snapshot": str(
            (specs[0].run_root.parent / "shared-world-snapshot").relative_to(root)
        ),
        "same_lineage_source_pair": True,
        "production_test_bytes_changed": False,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "candidate_execution_performed": True,
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(root, _CONSTRUCTION, (plan.reference, _REPACKAGE), payload, authorization)


def _write_recovery_observation(
    root: Path,
    *,
    plan: StableArtifact,
    construction: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": CANDIDATE,
        "state": "passed",
        "gate11_status": "semantic_frames_captured",
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "synthetic_frames": False,
        "capture_backend": "luanti_builtin_f12_engine_png_v1",
        "world_camera_render_profile": deterministic_visual_profile(),
        "fixed": attempts[0].payload["markers"],
        "buggy": attempts[1].payload["markers"],
        "fixed_before": attempts[0].payload["before_frame"],
        "fixed_after": attempts[0].payload["after_frame"],
        "buggy_before": attempts[1].payload["before_frame"],
        "buggy_after": attempts[1].payload["after_frame"],
        "construction_reference": construction.reference.artifact_id,
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(
        root,
        _OBSERVATION,
        (plan.reference, construction.reference, attempts[0].reference, attempts[1].reference),
        payload,
        authorization,
    )


def _write_recovery_matrix(
    root: Path,
    *,
    plan: StableArtifact,
    construction: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    observation: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    paths = [
        root / PurePosixPath(attempts[index].payload[key])
        for index, key in (
            (0, "before_frame"),
            (1, "before_frame"),
            (0, "after_frame"),
            (1, "after_frame"),
        )
    ]
    matrix = visual_cross_matrix(
        before_fixed=paths[0],
        before_buggy=paths[1],
        after_fixed=paths[2],
        after_buggy=paths[3],
    )
    status = (
        "passed_before_and_after_2x2"
        if matrix["before_equivalent"] and matrix["after_fixed_buggy_different"]
        else "blocked_before_or_after_2x2"
    )
    payload = {
        "candidate": CANDIDATE,
        "state": "passed" if status == "passed_before_and_after_2x2" else "downstream_pending",
        "gate12_status": status,
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "matrix": matrix,
        "human_or_model_visual_judgment": "pending",
        "observation_reference": observation.reference.artifact_id,
        "construction_reference": construction.reference.artifact_id,
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(
        root,
        _MATRIX,
        (plan.reference, construction.reference, observation.reference),
        payload,
        authorization,
    )


def _write_recovery_final_receipt(
    root: Path,
    *,
    plan: StableArtifact,
    construction: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    observation: StableArtifact,
    matrix: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist the typed Gate11-13 closure after the quantitative matrix passes."""
    matrix_values = matrix.payload["matrix"]
    payload = {
        "candidate": CANDIDATE,
        "state": "passed",
        "outcome": "visual_black_screen_repaired_and_cross_matrix_passed",
        "gate11_status": "passed_real_engine_frames",
        "gate12_status": "passed_before_and_after_2x2",
        "gate13_status": "pending_mainline_final_checkpoint",
        "fixed_attempt_reference": attempts[0].reference.artifact_id,
        "buggy_attempt_reference": attempts[1].reference.artifact_id,
        "observation_reference": observation.reference.artifact_id,
        "matrix_reference": matrix.reference.artifact_id,
        "construction_reference": construction.reference.artifact_id,
        "matrix": matrix_values,
        "capture_backend": "luanti_builtin_f12_engine_png_v1",
        "synthetic_frames": False,
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "production_test_bytes_changed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "formal_freeze_eligible": False,
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(
        root,
        _FINAL_RECEIPT,
        (
            plan.reference,
            construction.reference,
            observation.reference,
            matrix.reference,
            attempts[0].reference,
            attempts[1].reference,
        ),
        payload,
        authorization,
    )


def _write_recovery_blocker(
    root: Path,
    *,
    plan: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    construction: StableArtifact | None,
    observation: StableArtifact | None,
    matrix: StableArtifact | None,
    failed_variant: str,
    authorization: AuthorizationRecord,
    reason: str,
) -> StableArtifact:
    refs = (plan.reference, *(item.reference for item in attempts))
    if construction is not None:
        refs += (construction.reference,)
    if observation is not None:
        refs += (observation.reference,)
    if matrix is not None:
        refs += (matrix.reference,)
    payload = {
        "candidate": CANDIDATE,
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked" if reason.startswith("native_input") else "visual_common_before_or_2x2_blocked",
        "reason": reason,
        "failed_variant": failed_variant,
        "candidate_execution_performed": bool(attempts),
        "single_bounded_pair_consumed": bool(attempts),
        "additional_run_authorized_in_this_receipt": False,
        "attempts_written": [item.reference.artifact_id for item in attempts],
        "construction_reference": construction.reference.artifact_id if construction else None,
        "observation_reference": observation.reference.artifact_id if observation else None,
        "matrix_reference": matrix.reference.artifact_id if matrix else None,
        "world_camera_render_profile": deterministic_visual_profile(),
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "protected_pids": list(_PROTECTED_PIDS),
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "terminal": False,
        "resumable": True,
        "complete": True,
        "minimal_recovery_path": {
            "state": "deferred_no_second_run",
            "requires": ["inspect raw log/frame evidence", "repair only candidate-specific adapter if needed", "obtain new explicit authorization before any future fresh pair"],
            "do_not_reuse_current_attempts_as_goldens": True,
        },
    }
    return _write(root, _BLOCKER, refs, payload, authorization)


def write_groundlevel_repair_receipt(
    artifact_root: str | Path,
    *,
    run_root: str | Path,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist the successful fresh ground-level visual repair evidence."""
    _require_authorization(authorization, scope="preannotation-execution")
    root = _ordinary_root(Path(artifact_root).absolute())
    run = _ordinary_directory(Path(run_root).absolute(), root)
    if run.name != "f12-visual-timer45-v1":
        raise RuntimeError("#14757 ground-level repair run namespace is unexpected")
    refs = (
        _GATE_AGGREGATE,
        _GATE_TRANSITION,
        _MAPPING,
        _REPACKAGE,
        _GATE9,
        _GATE10_OBSERVATION,
        _GATE10_ORACLE,
        _PREVIOUS_BLOCKER,
    )
    loaded = {ref.artifact_id: _load(root, ref) for ref in refs}
    _validate_receipts(
        {ref.artifact_id: loaded[ref.artifact_id] for ref in refs[:-1]}
    )
    old_blocker = loaded[_PREVIOUS_BLOCKER.artifact_id]
    if (
        old_blocker.payload.get("state") != "downstream_pending"
        or old_blocker.payload.get("terminal") is not False
    ):
        raise RuntimeError("#14757 prior visual blocker is not supersedable")

    attempts: list[StableArtifact] = []
    for variant, ref in (("fixed", _REPAIR_FIXED), ("buggy", _REPAIR_BUGGY)):
        variant_root = _ordinary_directory(run / variant, run)
        log = _ordinary_file(variant_root / "luanti.log", variant_root)
        frames = sorted(
            (path for path in (variant_root / "frames").glob("*.png")),
            key=lambda path: path.name,
        )
        if len(frames) != 2 or not all(frame_is_non_null(path) for path in frames):
            raise RuntimeError("#14757 repair requires exactly two complete real PNGs")
        markers = parse_visual_markers(log.read_text(encoding="utf-8", errors="replace"))
        if not (
            markers.ordered
            and markers.start_marker == "basenodes:cobble"
            and markers.start_result_empty is True
            and markers.server_trigger_observed
            and markers.action_command == "/c1_lazarus_round"
            and markers.action_count == 1
            and markers.action_ok is (variant == "fixed")
            and markers.semantic_result == ("0,-0" if variant == "fixed" else "1,-1")
        ):
            raise RuntimeError("#14757 repair semantic markers do not match")
        payload = {
            "candidate": CANDIDATE,
            "variant": variant,
            "state": "passed",
            "process_outcome": "completed",
            "markers": asdict(markers),
            "before_frame": str(frames[0].relative_to(root)),
            "after_frame": str(frames[1].relative_to(root)),
            "frame_dimensions": list(_frame_dimensions(frames[0]) or ()),
            "run_root": str(run.relative_to(root)),
            "mapgen_ground_level": 0,
            "water_level": -31000,
            "action_delay_seconds": _ACTION_DELAY_SECONDS,
            "capture_backend": "luanti_builtin_f12_engine_png_v1",
            "synthetic_frames": False,
            "candidate_execution_performed": True,
            "old_attempts_loaded": False,
            "old_construction_loaded": False,
            "production_test_bytes_changed": False,
            "terminal": False,
            "resumable": True,
            "complete": True,
        }
        attempts.append(_write(root, ref, refs, payload, authorization))

    fixed_before = root / PurePosixPath(attempts[0].payload["before_frame"])
    buggy_before = root / PurePosixPath(attempts[1].payload["before_frame"])
    fixed_after = root / PurePosixPath(attempts[0].payload["after_frame"])
    buggy_after = root / PurePosixPath(attempts[1].payload["after_frame"])
    matrix = visual_cross_matrix(
        before_fixed=fixed_before,
        before_buggy=buggy_before,
        after_fixed=fixed_after,
        after_buggy=buggy_after,
    )
    if (
        matrix.get("before_equivalent") is not True
        or matrix.get("after_fixed_buggy_different") is not True
    ):
        raise RuntimeError("#14757 repair visual matrix did not pass")
    payload = {
        "candidate": CANDIDATE,
        "state": "passed",
        "outcome": "visual_black_frame_and_cross_matrix_repaired",
        "gate11_status": "passed_real_engine_frames",
        "gate12_status": "passed_before_and_after_2x2",
        "gate13_status": "pending_mainline_final_checkpoint",
        "matrix": matrix,
        "fixed_attempt_reference": attempts[0].reference.artifact_id,
        "buggy_attempt_reference": attempts[1].reference.artifact_id,
            "supersedes_nonterminal_blocker": _PREVIOUS_BLOCKER.artifact_id,
        "root_cause": "flat_mapgen_default_ground_level_buried_camera_and_panel",
        "repair": {
            "mgflat_ground_level": 0,
            "water_level": -31000,
            "map_emergence_required_before_scene": True,
            "bounded_before_window_seconds": _ACTION_DELAY_SECONDS,
            "partial_png_polling": True,
        },
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "synthetic_frames": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "formal_freeze_eligible": False,
        "next_action": "ingest_repair_receipt_into_mainline_checkpoint",
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    receipt = _write(
        root,
        _REPAIR_RECEIPT,
        (attempts[0].reference, attempts[1].reference, _PREVIOUS_BLOCKER),
        payload,
        authorization,
    )
    reloaded = _load(root, _REPAIR_RECEIPT)
    if reloaded.inputs != receipt.inputs or dict(reloaded.payload) != payload:
        raise RuntimeError("#14757 ground-level repair receipt reload drifted")
    return reloaded


def _require_authorization(authorization: AuthorizationRecord, *, scope: str) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != scope
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(f"#14757 deterministic recovery authorization requires {scope}")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "CANDIDATE",
    "EXECUTION_CONFIRMATION",
    "deterministic_config_text",
    "deterministic_visual_profile",
    "execute_deterministic_recovery_pair",
    "inject_deterministic_visual_fixture",
    "write_groundlevel_repair_receipt",
    "write_deterministic_recovery_plan",
]
