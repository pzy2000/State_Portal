"""Executable tests distilled directly from registered upstream conversations."""

from __future__ import annotations

import copy
from typing import Any


_LUANTI_PR15638_PROVENANCE = {
    "repository": "luanti-org/luanti",
    "issue": 13485,
    "pull_request": 15638,
    "source": "pull_request_body_how_to_test",
}

_LUANTI_PR11919_PROVENANCE = {
    "repository": "luanti-org/luanti",
    "pull_request": 11919,
    "source": "builtin/common/tests/vector_spec.lua",
}
_LUANTI_PR14757_PROVENANCE = {
    "repository": "luanti-org/luanti",
    "pull_request": 14757,
    "source": "builtin/common/tests/misc_helpers_spec.lua",
}
_LUANTI_PR14396_PROVENANCE = {
    "repository": "luanti-org/luanti",
    "pull_request": 14396,
    "source": "games/devtest/mods/unittests/metadata.lua",
}
_LUANTI_PR11919_PUBLIC_VIEW_ASSERTION = {
    "type": "luanti_player_view_matches",
    "pos": {"x": 0, "y": 3, "z": 6},
    "look_at": {"x": 0, "y": 1, "z": 0},
    "radius": 0.1,
    "angle_tolerance_degrees": 1.0,
}


def luanti_pr11919_public_view_assertion() -> dict[str, Any]:
    """Return the exact public, read-only player-view check for Start evidence."""

    return copy.deepcopy(_LUANTI_PR11919_PUBLIC_VIEW_ASSERTION)


def luanti_pr11919_probe() -> dict[str, Any]:
    """Project PR #11919's committed vector regression into a live devtest task."""

    marker_pos = {"x": 0, "y": 1, "z": 0}
    base_pos = {"x": 0, "y": 0, "z": 0}
    return {
        "name": "Luanti PR 11919 vector import probe",
        "benchmark": "luanti",
        "timeout_ms": 180_000,
        "provenance": copy.deepcopy(_LUANTI_PR11919_PROVENANCE),
        "target": {
            "kind": "synthesized_world",
            "nodes": [
                {"pos": copy.deepcopy(base_pos), "node": "basenodes:sand"},
                {
                    "pos": copy.deepcopy(marker_pos),
                    "node": "basenodes:stone",
                },
            ],
            "player": {
                "pos": {"x": 0, "y": 3, "z": 6},
                "hover": True,
                "look_at": copy.deepcopy(marker_pos),
            },
            "validations": [
                {"path": "node.0,0,0", "expected": "basenodes:sand"},
                {
                    "path": "node.0,1,0",
                    "expected": "basenodes:stone",
                },
            ],
        },
        "checkpoint": {
            "allowed_strategies": ["luanti_worldmod_state_validate"],
            "expected_validity": "save-restorable",
        },
        "actions": [
            {
                "type": "luanti_import_vector_string",
                "value": "(1, 2, 3.14)",
                "marker_pos": copy.deepcopy(marker_pos),
            }
        ],
        "assertions": [
            {
                "type": "luanti_node_is",
                "pos": copy.deepcopy(marker_pos),
                "expected": "basenodes:dirt_with_grass",
            },
            {"type": "no_luanti_mod_errors"},
        ],
    }


def luanti_pr14757_probe() -> dict[str, Any]:
    """Project PR #14757's committed rounding regression into a live devtest task.

    The registered action calls the public ``math.round`` on the two half-way
    values the merged fix corrects, and the prepared marker records the result.
    """

    marker_pos = {"x": 0, "y": 1, "z": 0}
    base_pos = {"x": 0, "y": 0, "z": 0}
    return {
        "name": "Luanti PR 14757 half-way rounding probe",
        "benchmark": "luanti",
        "timeout_ms": 180_000,
        "provenance": copy.deepcopy(_LUANTI_PR14757_PROVENANCE),
        "target": {
            "kind": "synthesized_world",
            "nodes": [
                {"pos": copy.deepcopy(base_pos), "node": "basenodes:sand"},
                {"pos": copy.deepcopy(marker_pos), "node": "basenodes:cobble"},
            ],
            "player": {
                "pos": {"x": 0, "y": 3, "z": 6},
                "hover": True,
                "look_at": copy.deepcopy(marker_pos),
            },
            "validations": [
                {"path": "node.0,0,0", "expected": "basenodes:sand"},
                {"path": "node.0,1,0", "expected": "basenodes:cobble"},
            ],
        },
        "checkpoint": {
            "allowed_strategies": ["luanti_worldmod_state_validate"],
            "expected_validity": "save-restorable",
        },
        "actions": [
            {
                "type": "luanti_round_half_boundary",
                "marker_pos": copy.deepcopy(marker_pos),
            }
        ],
        "assertions": [
            {
                "type": "luanti_node_is",
                "pos": copy.deepcopy(marker_pos),
                "expected": "basenodes:dirt_with_grass",
            },
            {"type": "no_luanti_mod_errors"},
        ],
    }


def luanti_pr14396_probe() -> dict[str, Any]:
    """Project PR #14396's metadata compatibility fix into a live devtest task.

    The registered action performs the undocumented ``set_string(key, nil)``
    removal the merged fix restored, and the prepared marker records whether the
    call survived and cleared the field.
    """

    marker_pos = {"x": 0, "y": 1, "z": 0}
    base_pos = {"x": 0, "y": 0, "z": 0}
    return {
        "name": "Luanti PR 14396 metadata compatibility probe",
        "benchmark": "luanti",
        "timeout_ms": 180_000,
        "provenance": copy.deepcopy(_LUANTI_PR14396_PROVENANCE),
        "target": {
            "kind": "synthesized_world",
            "nodes": [
                {"pos": copy.deepcopy(base_pos), "node": "basenodes:sand"},
                {"pos": copy.deepcopy(marker_pos), "node": "basenodes:cobble"},
            ],
            "player": {
                "pos": {"x": 0, "y": 3, "z": 6},
                "hover": True,
                "look_at": copy.deepcopy(marker_pos),
            },
            "validations": [
                {"path": "node.0,0,0", "expected": "basenodes:sand"},
                {"path": "node.0,1,0", "expected": "basenodes:cobble"},
            ],
        },
        "checkpoint": {
            "allowed_strategies": ["luanti_worldmod_state_validate"],
            "expected_validity": "save-restorable",
        },
        "actions": [
            {
                "type": "luanti_metadata_set_string_nil",
                "marker_pos": copy.deepcopy(marker_pos),
            }
        ],
        "assertions": [
            {
                "type": "luanti_node_is",
                "pos": copy.deepcopy(marker_pos),
                "expected": "basenodes:dirt_with_grass",
            },
            {"type": "no_luanti_mod_errors"},
        ],
    }


def luanti_pr15638_probe(case: str) -> dict[str, Any]:
    """Translate all three upstream How-to-test cases into engine probes."""

    cases: dict[str, dict[str, Any]] = {
        "normal-node": {
            "kind": "fail_to_pass",
            "ground_pos": {"x": 0, "y": 0, "z": 0},
            "nodes": [
                ({"x": 0, "y": 0, "z": 0}, "basenodes:cobble"),
                ({"x": 0, "y": 1, "z": 0}, "basenodes:stone"),
            ],
            "expected_pos": {"x": 0, "y": 1, "z": 0},
            "expected": "basenodes:stone",
            "invariant_pos": {"x": 0, "y": 0, "z": 0},
            "invariant": "basenodes:cobble",
        },
        "torch": {
            "kind": "pass_to_pass",
            "ground_pos": {"x": 0, "y": 0, "z": 0},
            "nodes": [
                ({"x": 0, "y": 0, "z": 0}, "basenodes:cobble"),
                ({"x": 0, "y": 1, "z": 0}, "testnodes:torchlike"),
            ],
            "expected_pos": {"x": 0, "y": 1, "z": 0},
            "expected": "basenodes:sand",
            "invariant_pos": {"x": 0, "y": 0, "z": 0},
            "invariant": "basenodes:cobble",
        },
        "liquid": {
            "kind": "pass_to_pass",
            "ground_pos": {"x": 0, "y": 0, "z": 0},
            "nodes": [
                ({"x": 0, "y": -1, "z": 0}, "basenodes:stone"),
                ({"x": 0, "y": 0, "z": 0}, "basenodes:water_source"),
            ],
            "expected_pos": {"x": 0, "y": 0, "z": 0},
            "expected": "basenodes:sand",
            "invariant_pos": {"x": 0, "y": -1, "z": 0},
            "invariant": "basenodes:stone",
        },
    }
    if case not in cases:
        raise ValueError("Luanti PR #15638 case must be normal-node, torch, liquid")
    selected = copy.deepcopy(cases[case])
    target_nodes = [
        {"pos": pos, "node": node} for pos, node in selected.pop("nodes")
    ]
    validations = [
        {
            "path": f"node.{row['pos']['x']},{row['pos']['y']},{row['pos']['z']}",
            "expected": row["node"],
        }
        for row in target_nodes
    ]
    return {
        "name": f"Luanti PR 15638 upstream {case} probe",
        "benchmark": "luanti",
        "timeout_ms": 180_000,
        "provenance": copy.deepcopy(_LUANTI_PR15638_PROVENANCE),
        "probe_case": copy.deepcopy(selected),
        "target": {
            "kind": "synthesized_world",
            "nodes": target_nodes,
            "player": {
                "pos": {"x": 0, "y": 3, "z": 6},
                "hover": True,
                "look_at": {"x": 0, "y": 1, "z": 0},
            },
            "validations": validations,
        },
        "checkpoint": {
            "allowed_strategies": ["luanti_worldmod_state_validate"],
            "expected_validity": "save-restorable",
        },
        "actions": [
            {
                "type": "luanti_drop_falling_node",
                "ground_pos": copy.deepcopy(selected["ground_pos"]),
                "node": "basenodes:sand",
            }
        ],
        "assertions": [
            {
                "type": "luanti_node_is",
                "pos": copy.deepcopy(selected["expected_pos"]),
                "expected": selected["expected"],
            },
            {
                "type": "luanti_node_is",
                "pos": copy.deepcopy(selected["invariant_pos"]),
                "expected": selected["invariant"],
            },
            {"type": "no_luanti_mod_errors"},
        ],
    }
