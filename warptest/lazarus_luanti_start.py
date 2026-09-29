"""Registered public Start shapes for the Luanti Lazarus Phase-0 tasks.

A Luanti Phase-0 task is graded from a live world, so the coordinator must know
exactly which world the public task text describes: the model's synthesized
checkpoint is accepted only when it reproduces that Start, and the runtime
restores the same Start before it replays the public action.

Both the synthesis contract (:mod:`warptest.synthesis`) and the runtime
(:mod:`warptest.lazarus_runtime`) read these profiles, so a task can never be
offered one Start and then graded against another.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Mapping


PublicPosition = tuple[int, int, int]


@dataclass(frozen=True)
class LuantiPhase0Start:
    """One task's public fixed-seed Start and its single public action."""

    task_id: str
    nodes: Mapping[PublicPosition, str]
    validations: Mapping[str, str]
    player: Mapping[str, Any]
    action: Mapping[str, Any]
    view_name: str
    start_note: str
    selection_rule: str
    # The audit key a published oracle names when it states the outcome as a
    # probe observation.  ``marker_node`` is the marker-world convention: the
    # replay rewrites the marker node, so the observed node *is* the outcome.
    audit_field: str = "marker_node"

    def __post_init__(self) -> None:
        if not self.task_id.startswith("lz-luanti-"):
            raise ValueError("Luanti Start profile task id is invalid")
        if not self.nodes or not self.validations:
            raise ValueError("Luanti Start profile must register nodes and validations")
        if str(self.action.get("type") or "") == "":
            raise ValueError("Luanti Start profile must register one public action")
        if not self.audit_field:
            raise ValueError("Luanti Start profile must register an audit field")
        if not isinstance(self.action.get("marker_pos"), Mapping):
            raise ValueError("Luanti Start profile action must register a marker_pos")

    @property
    def marker_pos(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.action["marker_pos"]))

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.nodes.values())))

    def target(self) -> dict[str, Any]:
        """Return the synthesized-world target the public task text describes."""

        return {
            "kind": "synthesized_world",
            "nodes": [
                {"pos": {"x": pos[0], "y": pos[1], "z": pos[2]}, "node": node}
                for pos, node in sorted(self.nodes.items())
            ],
            "player": copy.deepcopy(dict(self.player)),
            "validations": [
                {"path": path, "expected": expected}
                for path, expected in sorted(self.validations.items())
            ],
        }

    def generated_spec(self) -> dict[str, Any]:
        """Return the exact checkpoint shape the state arm will accept."""

        return {
            "name": "non-empty string",
            "benchmark": "luanti",
            "target": self.target(),
            "checkpoint": {
                "allowed_strategies": ["luanti_worldmod_state_validate"],
                "expected_validity": "save-restorable",
            },
            "actions": [copy.deepcopy(dict(self.action))],
            "assertions": [{"type": "no_luanti_mod_errors"}],
        }

    def source_vocabulary(self) -> dict[str, Any]:
        """Return the public vocabulary the synthesis agent may draw from."""

        return {
            "checkpoint_strategies": ["luanti_worldmod_state_validate"],
            "node_ids": list(self.node_ids),
            "player_view": copy.deepcopy(dict(self.player)),
            "action_types": [str(self.action["type"])],
            "assertion_types": ["no_luanti_mod_errors"],
            "note": self.start_note,
        }


LUANTI_VECTOR_START = LuantiPhase0Start(
    task_id="lz-luanti-11919-vector-from-string",
    nodes={(0, 0, 0): "basenodes:sand", (0, 1, 0): "basenodes:stone"},
    validations={
        "node.0,0,0": "basenodes:sand",
        "node.0,1,0": "basenodes:stone",
    },
    player={
        "pos": {"x": 0, "y": 3, "z": 6},
        "hover": True,
        "look_at": {"x": 0, "y": 1, "z": 0},
    },
    action={
        "type": "luanti_import_vector_string",
        "value": "(1, 2, 3.14)",
        "marker_pos": {"x": 0, "y": 1, "z": 0},
    },
    view_name="Public Luanti PR 11919 fixed-seed Start observation",
    start_note=(
        "The public fixed-seed Start is the stone marker above the sand base named "
        "in the issue. Use only fields stated in the public task."
    ),
    selection_rule=(
        "This exact Lazarus task uses only the public fixed-seed marker Start and "
        "the public coordinate-import action. Runtime evaluation owns the result."
    ),
)
LUANTI_ROUND_START = LuantiPhase0Start(
    task_id="lz-luanti-14757-math-round",
    nodes={(0, 0, 0): "basenodes:sand", (0, 1, 0): "basenodes:cobble"},
    validations={
        "node.0,0,0": "basenodes:sand",
        "node.0,1,0": "basenodes:cobble",
    },
    player={
        "pos": {"x": 0, "y": 3, "z": 6},
        "hover": True,
        "look_at": {"x": 0, "y": 1, "z": 0},
    },
    action={
        "type": "luanti_round_half_boundary",
        "marker_pos": {"x": 0, "y": 1, "z": 0},
    },
    view_name="Public Luanti PR 14757 fixed-seed Start observation",
    start_note=(
        "The public fixed-seed Start is the cobble marker above the sand base named "
        "in the issue. Use only fields stated in the public task."
    ),
    selection_rule=(
        "This exact Lazarus task uses only the public fixed-seed marker Start and "
        "the public rounding action. Runtime evaluation owns the result."
    ),
)
LUANTI_METADATA_START = LuantiPhase0Start(
    task_id="lz-luanti-14396-metadata-set-string-nil",
    nodes={(0, 0, 0): "basenodes:sand", (0, 1, 0): "basenodes:cobble"},
    validations={
        "node.0,0,0": "basenodes:sand",
        "node.0,1,0": "basenodes:cobble",
    },
    player={
        "pos": {"x": 0, "y": 3, "z": 6},
        "hover": True,
        "look_at": {"x": 0, "y": 1, "z": 0},
    },
    action={
        "type": "luanti_metadata_set_string_nil",
        "marker_pos": {"x": 0, "y": 1, "z": 0},
    },
    view_name="Public Luanti PR 14396 fixed-seed Start observation",
    start_note=(
        "The public fixed-seed Start is the cobble marker above the sand base named "
        "in the issue. Use only fields stated in the public task."
    ),
    selection_rule=(
        "This exact Lazarus task uses only the public fixed-seed marker Start and "
        "the public metadata setter action. Runtime evaluation owns the result."
    ),
)
LUANTI_PHASE0_STARTS: dict[str, LuantiPhase0Start] = {
    start.task_id: start
    for start in (LUANTI_VECTOR_START, LUANTI_ROUND_START, LUANTI_METADATA_START)
}


def luanti_start_for_task_id(task_id: str) -> LuantiPhase0Start | None:
    """Return the registered Start profile for one task id, if any."""

    return LUANTI_PHASE0_STARTS.get(str(task_id or ""))


def luanti_live_assertions(
    start: LuantiPhase0Start,
    checks: Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> list[dict[str, Any]]:
    """Translate one published check group into live-world session assertions.

    A published oracle states its hidden groups in the vocabulary of the offline
    probe, which compares fields of an observed audit mapping.  A live trial
    instead asserts against the running world.  Both describe the same fact, so
    the group is translated here rather than duplicated in the spec, where the
    two copies could disagree about the outcome of the same replay.

    A check this function cannot translate exactly is refused.  Silently dropping
    it would grade a trial against fewer conditions than the oracle registered.
    """

    translated: list[dict[str, Any]] = []
    for index, check in enumerate(checks):
        if not isinstance(check, Mapping):
            raise ValueError(f"{label} check[{index}] is not a mapping")
        kind = str(check.get("type") or "")
        if kind in {"no_luanti_mod_errors", "luanti_node_is"}:
            translated.append(copy.deepcopy(dict(check)))
            continue
        if kind != "luanti_lazarus_state_audit":
            raise ValueError(
                f"{label} check[{index}] has no live translation: {kind!r}"
            )
        field = str(check.get("field") or "")
        if field != start.audit_field:
            raise ValueError(
                f"{label} check[{index}] observes {field!r}, which this Start "
                f"cannot assert live; it registers {start.audit_field!r}"
            )
        if "expected" not in check:
            raise ValueError(f"{label} check[{index}] states no expected value")
        translated.append(
            {
                "type": "luanti_node_is",
                "pos": start.marker_pos,
                "expected": check["expected"],
            }
        )
    if not translated:
        raise ValueError(f"{label} check group is empty")
    return translated


__all__ = [
    "LUANTI_METADATA_START",
    "LUANTI_PHASE0_STARTS",
    "LUANTI_ROUND_START",
    "LUANTI_VECTOR_START",
    "LuantiPhase0Start",
    "luanti_live_assertions",
    "luanti_start_for_task_id",
]
