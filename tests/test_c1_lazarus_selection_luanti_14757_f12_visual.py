from __future__ import annotations

from pathlib import Path

from PIL import Image

from warptest.c1_lazarus_selection.luanti_14757_f12_visual import (
    SERVER_TRIGGER_MARKER,
    build_client_command,
    frame_is_non_null,
    inject_delayed_server_trigger,
    parse_visual_markers,
    visual_cross_matrix,
)
from warptest.c1_lazarus_selection.luanti_14757_f12_recovery import (
    _HUD_MARKER,
    _preaction_scene_ready,
    deterministic_config_text,
    deterministic_visual_profile,
    inject_deterministic_visual_fixture,
)


_FIXTURE = """local storage = minetest.get_mod_storage()
minetest.register_on_mods_loaded(function()
  minetest.set_node({x=0,y=1,z=0}, {name="basenodes:cobble"})
  minetest.log("action", "C1_LAZARUS_14757_START marker=basenodes:cobble result_empty=true")
end)
minetest.register_chatcommand("c1_lazarus_round", {
  func = function()
    minetest.log("action", "C1_LAZARUS_14757_ACTION command=/c1_lazarus_round count=1 result=0,-0 ok=true")
  end,
})
"""


def test_delayed_trigger_keeps_common_before_window_and_action_body() -> None:
    transformed = inject_delayed_server_trigger(_FIXTURE, delay_seconds=5.0)

    assert 'minetest.register_chatcommand("c1_lazarus_round"' in transformed
    assert transformed.index("C1_LAZARUS_14757_START") < transformed.index(
        "minetest.after(5.0"
    )
    assert SERVER_TRIGGER_MARKER in transformed
    assert transformed.count("C1_LAZARUS_14757_ACTION") == 1
    assert "QuartzNativeInput" not in transformed


def test_client_command_is_fresh_gui_pair_without_terminal_or_old_root() -> None:
    command = build_client_command(
        binary=Path("/fresh/fixed-minetest.app/Contents/MacOS/minetest"),
        world=Path("/fresh/run/fixed/world"),
        config=Path("/fresh/run/fixed/minetest.conf"),
        log=Path("/fresh/run/fixed/luanti.log"),
    )

    assert command[:2] == (
        "/fresh/fixed-minetest.app/Contents/MacOS/minetest",
        "--go",
    )
    assert "--gameid" in command and "devtest" in command
    assert "--world" in command and "/fresh/run/fixed/world" in command
    assert "--config" in command and "/fresh/run/fixed/minetest.conf" in command
    assert "--logfile" in command and "/fresh/run/fixed/luanti.log" in command
    assert "--server" not in command
    assert "--terminal" not in command


def test_visual_marker_parser_requires_start_trigger_action_order() -> None:
    invalid = parse_visual_markers(
        "C1_LAZARUS_14757_ACTION command=/c1_lazarus_round count=1 result=0,-0 ok=true\n"
        "C1_LAZARUS_14757_START marker=basenodes:cobble result_empty=true\n"
    )
    assert invalid.ordered is False
    assert invalid.action_command is None

    valid = parse_visual_markers(
        "C1_LAZARUS_14757_START marker=basenodes:cobble result_empty=true\n"
        "C1_LAZARUS_14757_SERVER_TRIGGER trigger=server_after_start\n"
        "C1_LAZARUS_14757_ACTION command=/c1_lazarus_round count=1 result=0,-0 ok=true\n"
    )
    assert valid.ordered is True
    assert valid.start_marker == "basenodes:cobble"
    assert valid.action_command == "/c1_lazarus_round"


def test_preaction_scene_gate_accepts_start_before_trigger_and_action() -> None:
    assert _preaction_scene_ready(
        "C1_LAZARUS_14757_START marker=basenodes:cobble result_empty=true\n"
    )
    assert not _preaction_scene_ready(
        "C1_LAZARUS_14757_START marker=basenodes:cobble result_empty=false\n"
    )


def test_frame_quality_rejects_empty_and_accepts_real_png(tmp_path: Path) -> None:
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    assert frame_is_non_null(empty) is False

    frame = tmp_path / "frame.png"
    Image.new("RGB", (1280, 720), color=(20, 30, 40)).save(frame)
    assert frame_is_non_null(frame) is True

    partial = tmp_path / "partial.png"
    partial.write_bytes(frame.read_bytes()[:64])
    assert frame_is_non_null(partial) is False


def test_visual_cross_matrix_reports_before_equivalence_and_after_difference(
    tmp_path: Path,
) -> None:
    before_fixed = tmp_path / "before-fixed.png"
    before_buggy = tmp_path / "before-buggy.png"
    after_fixed = tmp_path / "after-fixed.png"
    after_buggy = tmp_path / "after-buggy.png"
    Image.new("RGB", (4, 4), color=(20, 30, 40)).save(before_fixed)
    Image.new("RGB", (4, 4), color=(20, 30, 40)).save(before_buggy)
    Image.new("RGB", (4, 4), color=(200, 30, 40)).save(after_fixed)
    Image.new("RGB", (4, 4), color=(20, 30, 40)).save(after_buggy)

    matrix = visual_cross_matrix(
        before_fixed=before_fixed,
        before_buggy=before_buggy,
        after_fixed=after_fixed,
        after_buggy=after_buggy,
    )
    assert matrix["before_equivalent"] is True
    assert matrix["after_fixed_buggy_different"] is True
    assert matrix["complete"] is True


def test_deterministic_recovery_profile_closes_seed_camera_and_render_contract() -> None:
    profile = deterministic_visual_profile()

    assert profile["world_seed"] == 14757
    assert profile["mapgen"] == "flat"
    assert profile["mapgen_params"] == {
        "mgflat_ground_level": 0,
        "water_level": -31000,
    }
    assert profile["world_copy_contract"] == "one-byte-identical-world-template-copied-to-both-variants"
    assert profile["player"] == {
        "pos": {"x": 0, "y": 3, "z": 6},
        "look_at": {"x": 0, "y": 2, "z": 0},
        "eye_height": 1.5,
        "gravity": 0,
    }
    assert profile["render"]["time_speed"] == 0
    assert profile["render"]["enable_clouds"] is False
    assert profile["render"]["enable_fog"] is False
    assert profile["action_visibility"]["fixed_after_node"] == "basenodes:dirt_with_grass"
    assert profile["action_visibility"]["buggy_after_node"] == "basenodes:desert_sand"


def test_deterministic_recovery_fixture_is_append_only_and_camera_authoritative() -> None:
    transformed = inject_deterministic_visual_fixture(_FIXTURE)

    # The candidate adapter deliberately relocates only the original start
    # callback behind map emergence.  The registered action body itself is
    # retained, while the original callback registration is not run at
    # mods_loaded time (that was the source of the missing-mapblock frames).
    assert "local c1_original_start = function()" in transformed
    # One registration remains for the bounded server-side trigger; the
    # original start registration has been replaced by the local function.
    assert transformed.count("minetest.register_on_mods_loaded(function()") == 1
    assert transformed.count("C1_LAZARUS_14757_ACTION") == 1
    assert "C1_LAZARUS_14757_CAMERA ready=true" in transformed
    assert _HUD_MARKER in transformed
    assert "player:hud_set_flags" in transformed
    assert "basic_debug = false" in transformed
    assert "chat = false" in transformed
    assert "minetest.register_on_joinplayer" in transformed
    assert "player:set_pos(c1_camera_pos)" in transformed
    assert "player:set_look_horizontal" in transformed
    assert "player:set_look_vertical" in transformed
    assert "minetest.get_connected_players()" in transformed
    assert "c1_repin_camera" in transformed
    assert "C1_LAZARUS_14757_SCENE before=true" in transformed
    assert "C1_LAZARUS_14757_SCENE after=true" in transformed
    assert "for x = -3, 3" in transformed
    assert "c1_update_scene_after" in transformed
    assert "minetest.emerge_area(c1_area_min, c1_area_max, c1_on_emerged)" in transformed
    assert "C1_LAZARUS_14757_SCENE_READY" in transformed
    assert "minetest.after(45.0, c1_trigger_after_scene_ready)" in transformed
    assert SERVER_TRIGGER_MARKER in transformed
    assert "QuartzNativeInput" not in transformed


def test_deterministic_configs_have_identical_common_render_lines(tmp_path: Path) -> None:
    fixed = deterministic_config_text(tmp_path / "fixed" / "frames")
    buggy = deterministic_config_text(tmp_path / "buggy" / "frames")

    def common(text: str) -> list[str]:
        return [line for line in text.splitlines() if not line.startswith("screenshot_path = ")]

    assert common(fixed) == common(buggy)
    assert "fixed_map_seed = 14757" in fixed
    assert "mg_name = flat" in fixed
    assert "mgflat_ground_level = 0" in fixed
    assert "water_level = -31000" in fixed
    assert "show_debug = false" in fixed
