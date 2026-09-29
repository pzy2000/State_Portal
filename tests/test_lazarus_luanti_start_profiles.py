from __future__ import annotations

from typing import Any

import pytest
import yaml

from pathlib import Path

from warptest.lazarus_luanti_start import (
    LUANTI_METADATA_START,
    LUANTI_PHASE0_STARTS,
    LUANTI_ROUND_START,
    LUANTI_VECTOR_START,
    LuantiPhase0Start,
    luanti_live_assertions,
    luanti_start_for_task_id,
)
from warptest.lazarus_probes import luanti_pr14396_probe
from warptest.lazarus_runtime import (
    LazarusPhase0RuntimeUnsupportedError,
    _luanti_public_player_restore,
    _luanti_public_start_view,
    _validate_luanti_public_checkpoint,
    validate_lazarus_phase0_runtime_support,
)
from warptest.synthesis import (
    PublicIssue,
    _lazarus_luanti_start,
    _source_vocabulary_for_task,
    generated_spec_contract_errors,
    lazarus_luanti_contract,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ROUND_SPEC = (
    REPOSITORY_ROOT / "specs/luanti/lazarus/lz-luanti-14757-math-round.phase0.yaml"
)


class _Context:
    """Minimal stand-in for the coordinator cell context the runtime reads."""

    def __init__(self, task_id: str) -> None:
        self.key = type("Key", (), {"task_id": task_id, "seed": 0})()
        self.task = {"id": task_id, "benchmark": "luanti"}


def _public_issue(task_id: str) -> PublicIssue:
    return PublicIssue(
        id=task_id,
        benchmark="luanti",
        repo="games/luanti",
        issue="Half-way rounding drifts away from zero.",
        tags=["c1", "lazarus", "luanti"],
    )


def _task(task_id: str, locus: str = "at_goal") -> dict[str, Any]:
    return {
        "id": task_id,
        "benchmark": "luanti",
        "defect": {"defect_locus": locus},
    }


def test_every_live_luanti_task_is_registered() -> None:
    assert set(LUANTI_PHASE0_STARTS) == {
        "lz-luanti-11919-vector-from-string",
        "lz-luanti-14757-math-round",
        "lz-luanti-14396-metadata-set-string-nil",
    }
    assert luanti_start_for_task_id("lz-luanti-9999-absent") is None


def test_round_start_matches_the_written_phase0_oracle() -> None:
    """The runtime Start and the oracle spec must describe one world."""

    spec = yaml.safe_load(ROUND_SPEC.read_text(encoding="utf-8"))
    target = LUANTI_ROUND_START.target()
    assert spec["target"]["nodes"] == target["nodes"]
    assert spec["target"]["player"] == target["player"]
    assert spec["target"]["validations"] == target["validations"]
    assert spec["actions"] == [dict(LUANTI_ROUND_START.action)]


def test_metadata_start_matches_the_registered_offline_probe() -> None:
    """The paid Start and the offline probe must drive one identical replay.

    The offline probe is what the construction gate and the published goldens
    were produced from, so a paid trial that started anywhere else would be
    graded against frames it never had a chance to reproduce.
    """

    probe = luanti_pr14396_probe()
    target = LUANTI_METADATA_START.target()

    assert probe["target"]["nodes"] == target["nodes"]
    assert probe["target"]["player"] == target["player"]
    assert probe["target"]["validations"] == target["validations"]
    assert probe["actions"] == [dict(LUANTI_METADATA_START.action)]


def test_live_assertions_translate_the_published_audit_group() -> None:
    """One published group, two consumers, no second copy in the spec."""

    translated = luanti_live_assertions(
        LUANTI_METADATA_START,
        [
            {
                "type": "luanti_lazarus_state_audit",
                "field": "marker_node",
                "expected": "basenodes:dirt_with_grass",
            },
            {"type": "no_luanti_mod_errors"},
        ],
        label="test group",
    )

    assert translated == [
        {
            "type": "luanti_node_is",
            "pos": {"x": 0, "y": 1, "z": 0},
            "expected": "basenodes:dirt_with_grass",
        },
        {"type": "no_luanti_mod_errors"},
    ]


def test_live_assertions_pass_through_an_already_live_group() -> None:
    live = [
        {
            "type": "luanti_node_is",
            "pos": {"x": 0, "y": 1, "z": 0},
            "expected": "basenodes:dirt_with_grass",
        }
    ]

    assert luanti_live_assertions(
        LUANTI_ROUND_START, live, label="test group"
    ) == live


@pytest.mark.parametrize(
    ("checks", "message"),
    [
        ([], "check group is empty"),
        (
            [{"type": "jupyterlab_lazarus_app_audit", "field": "x", "expected": 1}],
            "no live translation",
        ),
        (
            [
                {
                    "type": "luanti_lazarus_state_audit",
                    "field": "command",
                    "expected": "/c1_lazarus_round",
                }
            ],
            "cannot assert live",
        ),
        (
            [{"type": "luanti_lazarus_state_audit", "field": "marker_node"}],
            "states no expected value",
        ),
    ],
)
def test_live_assertions_refuse_what_they_cannot_translate_exactly(
    checks: list[dict[str, Any]], message: str
) -> None:
    """Dropping an untranslatable check would grade against fewer conditions."""

    with pytest.raises(ValueError, match=message):
        luanti_live_assertions(LUANTI_METADATA_START, checks, label="test group")


def test_runtime_support_accepts_every_registered_at_goal_task() -> None:
    validate_lazarus_phase0_runtime_support(
        [_task(task_id) for task_id in LUANTI_PHASE0_STARTS]
    )


@pytest.mark.parametrize(
    "task",
    [
        _task("lz-luanti-14757-math-round", locus="on_path"),
        _task("lz-luanti-13485-falling-node"),
    ],
)
def test_runtime_support_still_refuses_unregistered_shapes(task: dict[str, Any]) -> None:
    with pytest.raises(LazarusPhase0RuntimeUnsupportedError):
        validate_lazarus_phase0_runtime_support([task])


def test_checkpoint_acceptance_is_scoped_to_the_tasks_own_start() -> None:
    round_context = _Context("lz-luanti-14757-math-round")
    accepted = LUANTI_ROUND_START.generated_spec()
    assert (
        _validate_luanti_public_checkpoint(round_context, accepted)
        is LUANTI_ROUND_START
    )
    # The other registered task's Start is a different world, so it is refused.
    with pytest.raises(ValueError, match="drift"):
        _validate_luanti_public_checkpoint(
            round_context, LUANTI_VECTOR_START.generated_spec()
        )


def test_start_view_and_player_restore_follow_the_profile() -> None:
    view = _luanti_public_start_view(LUANTI_ROUND_START)
    assert view["name"] == LUANTI_ROUND_START.view_name
    assert view["target"]["validations"] == [
        {"path": "node.0,0,0", "expected": "basenodes:sand"},
        {"path": "node.0,1,0", "expected": "basenodes:cobble"},
    ]
    assert view["assertions"][0]["type"] == "luanti_player_view_matches"
    restore = _luanti_public_player_restore(LUANTI_ROUND_START)
    assert restore["target"]["player"] == dict(LUANTI_ROUND_START.player)


def test_synthesis_offers_the_same_start_it_will_be_graded_against() -> None:
    issue = _public_issue("lz-luanti-14757-math-round")
    start = _lazarus_luanti_start(issue)
    assert start is LUANTI_ROUND_START
    vocabulary = _source_vocabulary_for_task(issue)
    assert vocabulary["node_ids"] == ["basenodes:cobble", "basenodes:sand"]
    assert vocabulary["action_types"] == ["luanti_round_half_boundary"]
    contract = lazarus_luanti_contract(start)
    assert contract["generated_spec"]["actions"] == [dict(start.action)]
    assert generated_spec_contract_errors(start.generated_spec(), task=issue) == []


def test_synthesis_refuses_the_other_tasks_checkpoint() -> None:
    issue = _public_issue("lz-luanti-14757-math-round")
    errors = generated_spec_contract_errors(
        LUANTI_VECTOR_START.generated_spec(), task=issue
    )
    assert errors


@pytest.mark.parametrize(
    "overrides",
    [
        {"task_id": "luanti-14757"},
        {"nodes": {}},
        {"validations": {}},
        {"action": {}},
    ],
)
def test_profile_construction_fails_closed(overrides: dict[str, Any]) -> None:
    fields: dict[str, Any] = {
        "task_id": LUANTI_ROUND_START.task_id,
        "nodes": dict(LUANTI_ROUND_START.nodes),
        "validations": dict(LUANTI_ROUND_START.validations),
        "player": dict(LUANTI_ROUND_START.player),
        "action": dict(LUANTI_ROUND_START.action),
        "view_name": LUANTI_ROUND_START.view_name,
        "start_note": LUANTI_ROUND_START.start_note,
        "selection_rule": LUANTI_ROUND_START.selection_rule,
    }
    fields.update(overrides)
    with pytest.raises(ValueError):
        LuantiPhase0Start(**fields)
