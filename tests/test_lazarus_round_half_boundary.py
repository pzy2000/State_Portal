from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from warptest.spec_schema import validate_spec_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORLDMOD = REPOSITORY_ROOT / "warptest/instrumentation/luanti/warptest/init.lua"
HARNESS = Path(__file__).resolve().parent / "lua/warptest_round_half_harness.lua"
ACTION = {"type": "luanti_round_half_boundary", "marker_pos": {"x": 0, "y": 1, "z": 0}}


def _spec(actions: list[dict[str, object]]) -> dict[str, object]:
    return {
        "name": "round half boundary",
        "benchmark": "luanti",
        "target": {
            "kind": "synthesized_world",
            "nodes": [
                {"pos": {"x": 0, "y": 0, "z": 0}, "node": "basenodes:sand"},
                {"pos": {"x": 0, "y": 1, "z": 0}, "node": "basenodes:cobble"},
            ],
        },
        "checkpoint": {
            "allowed_strategies": ["luanti_worldmod_state_validate"],
            "expected_validity": "save-restorable",
        },
        "actions": actions,
        "assertions": [{"type": "no_luanti_mod_errors"}],
    }


def _run_harness(mode: str, tmp_path: Path) -> dict[str, str]:
    lua = shutil.which("lua") or shutil.which("luajit")
    if lua is None:
        pytest.skip("no Lua interpreter is available for the worldmod harness")
    completed = subprocess.run(
        [
            lua,
            str(HARNESS),
            str(WORLDMOD),
            mode,
            str(tmp_path / "request.json"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPOSITORY_ROOT,
    )
    assert completed.returncode == 0, completed.stderr
    return dict(
        line.split("=", 1)
        for line in completed.stdout.splitlines()
        if "=" in line
    )


def test_schema_accepts_the_action_with_a_marker_position(tmp_path: Path) -> None:
    assert validate_spec_schema(_spec([ACTION]), tmp_path / "spec.yaml") == []


def test_schema_rejects_the_action_without_a_marker_position(tmp_path: Path) -> None:
    errors = validate_spec_schema(
        _spec([{"type": "luanti_round_half_boundary"}]), tmp_path / "spec.yaml"
    )
    assert any("marker_pos" in error for error in errors)


def test_worldmod_lua_is_syntactically_valid() -> None:
    luac = shutil.which("luac")
    if luac is None:
        pytest.skip("luac is not available")
    completed = subprocess.run(
        [luac, "-p", str(WORLDMOD)], capture_output=True, text=True, timeout=120
    )
    assert completed.returncode == 0, completed.stderr


def test_fixed_rounding_marks_the_goal_node_on_both_public_paths(tmp_path: Path) -> None:
    """Nearest-even rounding is the merged behaviour, so the marker turns to grass."""

    observed = _run_harness("nearest_even", tmp_path)
    assert observed["CHAT_OK"] == "true"
    assert observed["CHAT_MARKER"] == "basenodes:dirt_with_grass"
    assert observed["ACTION_MARKER"] == "basenodes:dirt_with_grass"
    assert observed["CHAT_STORAGE"] == "0,0"
    assert observed["ACTION_STORAGE"] == "0,0"


def test_reverted_rounding_marks_the_defect_node_on_both_public_paths(
    tmp_path: Path,
) -> None:
    """The pre-fix floor/ceil rounding pushes both half-way values away from zero."""

    observed = _run_harness("floor_ceil", tmp_path)
    assert observed["CHAT_MARKER"] == "basenodes:desert_sand"
    assert observed["ACTION_MARKER"] == "basenodes:desert_sand"
    assert observed["CHAT_STORAGE"] == "1,-1"
    assert observed["ACTION_STORAGE"] == "1,-1"


def test_the_chat_command_and_the_action_share_one_implementation() -> None:
    """Neither public path may carry a second copy of the rounding observation."""

    text = WORLDMOD.read_text(encoding="utf-8")
    assert text.count("local function round_half_boundary") == 1
    assert "pcall(round_half_boundary, { x = 0, y = 1, z = 0 })" in text
    assert "round_half_boundary(marker_pos)" in text
    # Only the one helper may call the function under test.
    assert text.count("math.round(") == 2
    assert "math.round(0.49999999999999994)" in text
    assert "math.round(-0.49999999999999994)" in text
