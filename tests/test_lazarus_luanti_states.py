from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import pytest

from warptest import lazarus_luanti_states as target


@dataclass(frozen=True)
class _Reference:
    artifact_id: str
    artifact_kind: str


@dataclass(frozen=True)
class _Artifact:
    reference: _Reference
    payload: Mapping[str, Any]
    created_at: str


def _artifact(artifact_id: str, payload: Mapping[str, Any], created_at: str) -> _Artifact:
    kind = artifact_id.split("/")[0]
    return _Artifact(_Reference(artifact_id, kind), payload, created_at)


def _extract(artifacts: list[_Artifact], change_number: int = 14757) -> target.LuantiLazarusStates:
    """Drive the readers directly: the closure walk itself needs typed artifacts."""

    closure = tuple(sorted(artifacts, key=lambda item: item.created_at))
    scoped = tuple(a for a in closure if f"/{change_number}/" in a.reference.artifact_id)
    for reader, in ((target._explicit_oracle,), (target._semantic_oracle,), (target._version_tagged_attempts,)):
        recovered = reader(scoped)
        if recovered is None:
            continue
        fixed, buggy, sources, layout = recovered
        if fixed == buggy:
            continue
        fail, passing = target._upstream_tests(scoped)
        return target.LuantiLazarusStates(
            change_number=change_number,
            fixed=fixed,
            buggy=buggy,
            source_artifact_ids=sources,
            layout=layout,
            fail_to_pass=fail,
            pass_to_pass=passing,
            admission_id="candidate_formal_freeze_admission/luanti/14757/v1",
        )
    raise target.LuantiLazarusStateError("no separating pair")


def test_named_gate10_observations_outrank_other_layouts() -> None:
    states = _extract(
        [
            _artifact(
                "candidate_gate11_attempt/luanti/14757/fixed/1/v1",
                {"semantic_observation": "attempt_fixed"},
                "2026-08-14T01:00:00Z",
            ),
            _artifact(
                "candidate_gate11_attempt/luanti/14757/buggy/1/v1",
                {"semantic_observation": "attempt_buggy"},
                "2026-08-14T01:00:01Z",
            ),
            _artifact(
                "candidate_gate10_oracle/luanti/14757/v1",
                {
                    "fixed_goal_observation": "goal_rounded",
                    "buggy_defect_observation": "defect_truncated",
                },
                "2026-08-14T02:00:00Z",
            ),
        ]
    )
    assert (states.fixed, states.buggy) == ("goal_rounded", "defect_truncated")
    assert states.layout == "fixed_goal_observation_pair"


def test_version_keyed_mapping_is_read_when_no_named_pair_exists() -> None:
    states = _extract(
        [
            _artifact(
                "candidate_gate10_oracle/luanti/14757/v1",
                {"observed": {"fixed": {"ok": True}, "buggy": {"ok": False}}},
                "2026-08-14T02:00:00Z",
            )
        ]
    )
    assert (states.fixed, states.buggy) == ({"ok": True}, {"ok": False})
    assert states.layout == "observed_version_keyed"


def test_version_tagged_attempts_are_paired_by_their_own_ids() -> None:
    states = _extract(
        [
            _artifact(
                "candidate_gate11_attempt/luanti/14757/fixed/1/v1",
                {"semantic_observation": {"x": 0.0}},
                "2026-08-14T01:00:00Z",
            ),
            _artifact(
                "candidate_gate11_attempt/luanti/14757/buggy/1/v1",
                {"semantic_observation": {"x": 5.0}},
                "2026-08-14T01:00:01Z",
            ),
        ]
    )
    assert (states.fixed, states.buggy) == ({"x": 0.0}, {"x": 5.0})
    assert states.layout.endswith("_version_tagged")


def test_identical_sides_are_refused() -> None:
    with pytest.raises(target.LuantiLazarusStateError):
        _extract(
            [
                _artifact(
                    "candidate_gate10_oracle/luanti/14757/v1",
                    {
                        "fixed_goal_observation": "same",
                        "buggy_defect_observation": "same",
                    },
                    "2026-08-14T02:00:00Z",
                )
            ]
        )


def test_selectors_are_attributed_by_the_role_in_the_artifact_id() -> None:
    states = _extract(
        [
            _artifact(
                "candidate_gate10_oracle/luanti/14757/v1",
                {
                    "fixed_goal_observation": "goal",
                    "buggy_defect_observation": "defect",
                },
                "2026-08-14T02:00:00Z",
            ),
            _artifact(
                "native_regression_attempt/luanti/14757/f2p/fixed/1/v1",
                {"selector": "math round()"},
                "2026-08-14T01:00:00Z",
            ),
            _artifact(
                "native_regression_attempt/luanti/14757/p2p/fixed/1/v1",
                {"selector": "formspec_escape escapes"},
                "2026-08-14T01:00:01Z",
            ),
        ]
    )
    assert states.fail_to_pass == ("math round()",)
    assert states.pass_to_pass == ("formspec_escape escapes",)


def test_named_truth_table_ids_outrank_selectors() -> None:
    states = _extract(
        [
            _artifact(
                "candidate_gate10_oracle/luanti/14555/v1",
                {
                    "fixed_goal_observation": "goal",
                    "buggy_defect_observation": "defect",
                },
                "2026-08-14T02:00:00Z",
            ),
            _artifact(
                "curated_task_revalidation/luanti/14555/v1",
                {
                    "fixed_tests": {
                        "luanti-pr14555-voxel-line-iterator-f2p": True,
                        "luanti-vector-zero-construction-p2p": True,
                    }
                },
                "2026-08-14T03:00:00Z",
            ),
        ],
        change_number=14555,
    )
    assert states.fail_to_pass == ("luanti-pr14555-voxel-line-iterator-f2p",)
    assert states.pass_to_pass == ("luanti-vector-zero-construction-p2p",)


def test_state_accessor_rejects_an_unknown_version() -> None:
    states = _extract(
        [
            _artifact(
                "candidate_gate10_oracle/luanti/14757/v1",
                {
                    "fixed_goal_observation": "goal",
                    "buggy_defect_observation": "defect",
                },
                "2026-08-14T02:00:00Z",
            )
        ]
    )
    assert states.state("fixed") == "goal"
    assert states.state("buggy") == "defect"
    with pytest.raises(target.LuantiLazarusStateError):
        states.state("candidate")


def test_truth_table_is_the_frozen_fixed_buggy_contract() -> None:
    assert target.luanti_truth_table() == {
        "fixed": {"f2p": True, "p2p": True},
        "buggy": {"f2p": False, "p2p": True},
    }
