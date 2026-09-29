from __future__ import annotations

from pathlib import Path

from warptest.c1_lazarus_selection.luanti_14757_server_side_replay import (
    SERVER_TRIGGER_MARKER,
    ServerObservation,
    build_server_command,
    inject_server_side_trigger,
    parse_server_observation,
    _semantic_match,
)


_FIXTURE = """local storage = minetest.get_mod_storage()
minetest.register_on_mods_loaded(function()
  minetest.log("action", "C1_LAZARUS_14757_START marker=basenodes:cobble result_empty=true")
end)
minetest.register_chatcommand("c1_lazarus_round", {
  func = function()
    minetest.log("action", "C1_LAZARUS_14757_ACTION command=/c1_lazarus_round count=1 result=0,0 ok=true")
  end,
})
"""


def test_server_trigger_keeps_action_deferred_until_after_registered_start() -> None:
    transformed = inject_server_side_trigger(_FIXTURE)

    assert 'minetest.register_chatcommand("c1_lazarus_round"' in transformed
    assert transformed.index("register_chatcommand") < transformed.index(
        "minetest.after"
    )
    assert SERVER_TRIGGER_MARKER in transformed
    assert "/c1_lazarus_round" in transformed
    assert "QuartzNativeInput" not in transformed


def test_server_trigger_does_not_replace_registered_production_action() -> None:
    transformed = inject_server_side_trigger(_FIXTURE)

    # The adapter calls the registered function, so the production math.round
    # body remains the only action implementation in the copied fixture.
    assert transformed.count('register_chatcommand("c1_lazarus_round"') == 1
    assert transformed.count("C1_LAZARUS_14757_ACTION") == 1


def test_server_command_is_headless_and_confined_to_the_disposable_world() -> None:
    command = build_server_command(
        binary=Path("/candidate/fixed/Contents/MacOS/minetest"),
        world=Path("/candidate/run/fixed/world"),
        config=Path("/candidate/run/fixed/minetest.conf"),
        log=Path("/candidate/run/fixed/luanti.log"),
        port=43157,
    )

    assert command[:2] == (
        "/candidate/fixed/Contents/MacOS/minetest",
        "--server",
    )
    assert "--gameid" in command and "devtest" in command
    assert "--world" in command and str(Path("/candidate/run/fixed/world")) in command
    assert "--config" in command and str(Path("/candidate/run/fixed/minetest.conf")) in command
    assert "--logfile" in command and str(Path("/candidate/run/fixed/luanti.log")) in command
    assert "--terminal" not in command
    assert "--port" in command and "43157" in command


def test_server_observation_requires_start_before_action() -> None:
    text = (
        "C1_LAZARUS_14757_ACTION command=/c1_lazarus_round count=1 "
        "result=0,0 ok=true\n"
        "C1_LAZARUS_14757_START marker=basenodes:cobble result_empty=true\n"
    )
    observation = parse_server_observation(text)
    assert observation.start_marker is None
    assert observation.action_command is None


def test_server_observation_parses_ordered_fixed_and_buggy_markers() -> None:
    fixed = parse_server_observation(
        "C1_LAZARUS_14757_START marker=basenodes:cobble result_empty=true\n"
        "C1_LAZARUS_14757_SERVER_TRIGGER trigger=server_after_start\n"
        "C1_LAZARUS_14757_ACTION command=/c1_lazarus_round count=1 result=0,0 ok=true\n"
    )
    buggy = parse_server_observation(
        "C1_LAZARUS_14757_START marker=basenodes:cobble result_empty=true\n"
        "C1_LAZARUS_14757_SERVER_TRIGGER trigger=server_after_start\n"
        "C1_LAZARUS_14757_ACTION command=/c1_lazarus_round count=1 result=1,-1 ok=false\n"
    )

    assert fixed.start_marker == "basenodes:cobble"
    assert fixed.action_command == "/c1_lazarus_round"
    assert fixed.semantic_result == "0,0"
    assert fixed.action_ok is True
    assert fixed.server_trigger_observed is True
    assert buggy.semantic_result == "1,-1"
    assert buggy.action_ok is False


def test_fixed_numeric_zero_accepts_lua_negative_zero_text() -> None:
    observation = ServerObservation(
        process_outcome="completed",
        returncode=0,
        stdout=b"",
        stderr=b"",
        start_marker="basenodes:cobble",
        start_result_empty=True,
        server_trigger_observed=True,
        action_command="/c1_lazarus_round",
        action_count=1,
        semantic_result="0,-0",
        action_ok=True,
    )

    assert _semantic_match("fixed", observation) is True
