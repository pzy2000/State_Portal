from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.stable_protocol import load_stable_artifact


_ROOT = Path(".warptest-cache/c1_lazarus_selection_v1")


@pytest.mark.parametrize("change_number", (13825, 13773))
def test_current_v2_downstream_blocker_is_resumable(change_number: int) -> None:
    if not _ROOT.is_dir():
        pytest.skip("local v2 artifact cache is unavailable")
    artifact = load_stable_artifact(
        _ROOT,
        artifact_id=(
            f"candidate_reclassification_blocker/luanti/{change_number}/current-v2"
        ),
        artifact_kind="candidate_reclassification_blocker",
        semantic_version="1.0",
    )
    assert artifact.payload["terminal"] is False
    assert artifact.payload["resumable"] is True
    assert artifact.payload["candidate_execution_performed"] is False
    assert artifact.payload["current_gate_status"][
        "gate_4_fixed_test_api_compatibility"
    ] == "pending_candidate_profile_capsule"
    assert all(
        state == "missing"
        for state in artifact.payload["missing_current_v2_artifacts"].values()
    )
