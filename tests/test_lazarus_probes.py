from __future__ import annotations

import pytest

from warptest.lazarus_probes import luanti_pr11919_probe, luanti_pr15638_probe
from warptest.spec_schema import validate_spec_schema


@pytest.mark.parametrize("case", ["normal-node", "torch", "liquid"])
def test_luanti_pr15638_cases_are_executable_upstream_test_specs(case: str) -> None:
    spec = luanti_pr15638_probe(case)

    assert validate_spec_schema(spec, f"pr15638-{case}.yaml") == []
    assert spec["provenance"] == {
        "repository": "luanti-org/luanti",
        "issue": 13485,
        "pull_request": 15638,
        "source": "pull_request_body_how_to_test",
    }
    assert spec["actions"] == [
        {
            "type": "luanti_drop_falling_node",
            "ground_pos": spec["probe_case"]["ground_pos"],
            "node": "basenodes:sand",
        }
    ]


def test_luanti_pr15638_normal_node_is_f2p_and_others_are_p2p() -> None:
    f2p = luanti_pr15638_probe("normal-node")
    torch = luanti_pr15638_probe("torch")
    liquid = luanti_pr15638_probe("liquid")

    assert f2p["probe_case"]["kind"] == "fail_to_pass"
    assert f2p["assertions"][0]["expected"] == "basenodes:stone"
    assert torch["probe_case"]["kind"] == "pass_to_pass"
    assert torch["assertions"][0]["expected"] == "basenodes:sand"
    assert liquid["probe_case"]["kind"] == "pass_to_pass"
    assert liquid["assertions"][0]["expected"] == "basenodes:sand"


def test_luanti_pr15638_probe_rejects_unregistered_case() -> None:
    with pytest.raises(ValueError, match="normal-node, torch, liquid"):
        luanti_pr15638_probe("invented")


def test_luanti_pr11919_probe_exercises_the_upstream_vector_regression() -> None:
    spec = luanti_pr11919_probe()

    assert validate_spec_schema(spec, "pr11919-vector-from-string.yaml") == []
    assert spec["provenance"] == {
        "repository": "luanti-org/luanti",
        "pull_request": 11919,
        "source": "builtin/common/tests/vector_spec.lua",
    }
    assert spec["actions"] == [
        {
            "type": "luanti_import_vector_string",
            "value": "(1, 2, 3.14)",
            "marker_pos": {"x": 0, "y": 1, "z": 0},
        }
    ]
    assert spec["target"]["nodes"] == [
        {
            "pos": {"x": 0, "y": 0, "z": 0},
            "node": "basenodes:sand",
        },
        {
            "pos": {"x": 0, "y": 1, "z": 0},
            "node": "basenodes:stone",
        },
    ]
    assert spec["target"]["player"] == {
        "pos": {"x": 0, "y": 3, "z": 6},
        "hover": True,
        "look_at": {"x": 0, "y": 1, "z": 0},
    }
    assert spec["assertions"][0] == {
        "type": "luanti_node_is",
        "pos": {"x": 0, "y": 1, "z": 0},
        "expected": "basenodes:dirt_with_grass",
    }
