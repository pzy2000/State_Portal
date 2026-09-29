from __future__ import annotations

from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_onpath_profiles import (
    prepare_candidate,
    profile_for_candidate,
)
from warptest.c1_lazarus_selection.models import CandidateKey


_ROOT = Path(".warptest-cache/c1_lazarus_selection_v1")


@pytest.mark.parametrize(
    ("number", "issue", "test_marker"),
    (
        (
            14464,
            14463,
            "try to check that the conversion function does not accidentally keep",
        ),
        (14072, 13552, "testMoveFillStack"),
        (15687, 11991, 'SECTION("getScale")'),
    ),
)
def test_registered_profile_keeps_candidate_specific_gate_inputs(
    number: int, issue: int, test_marker: str
) -> None:
    profile = profile_for_candidate(CandidateKey("luanti", number))
    assert profile.issue == issue
    assert profile.test_paths
    assert test_marker in profile.test_markers
    assert profile.f2p_selector
    assert profile.p2p_selector


@pytest.mark.parametrize("number", (14464, 14072, 15687))
def test_current_typed_chain_passes_gates_one_to_four(number: int) -> None:
    if not _ROOT.is_dir():
        pytest.skip("local v2 artifact cache is unavailable")
    preflight = prepare_candidate(_ROOT, CandidateKey("luanti", number))
    assert preflight.gate_status["source_authority"] == "passed"
    assert preflight.gate_status["single_defect_mapping"] == "passed"
    assert preflight.gate_status["strict_inverse_patch"] == "passed"
    assert preflight.gate_status["test_api_compatibility"] == "passed"
    assert preflight.gate_status["fixed_f2p_3x"] == "downstream_pending"
    assert preflight.downstream_status == "downstream_pending"
