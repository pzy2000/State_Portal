from __future__ import annotations

import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_14757_mainline_closure import (
    CANDIDATE,
    _blocked_decision,
    _catalog_contains,
    _decision_for,
    _require_executable_regular,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/luanti-14757-mainline",
        scope="screening",
        granted_at="2026-08-14T08:00:00Z",
        local_user_id=os.getuid(),
    )


def _bridge(state: str = "passed") -> StableArtifact:
    reference = ArtifactReference(
        "candidate_mainline_evidence/luanti/14757/test/v2",
        "candidate_mainline_evidence",
        "2.0",
    )
    return StableArtifact(
        reference=reference,
        created_at="2026-08-14T08:00:01Z",
        inputs=(),
        payload={"state": state, "complete": True, "evidence_status": "test"},
        authorization=_authorization(),
        revision_path=Path("bridge.json"),
        head_path=Path("head.json"),
    )


def test_mainline_bridge_requires_candidate_and_passed_state() -> None:
    decision = _decision_for(CANDIDATE, _bridge())
    assert decision.state == "passed"
    assert decision.outcome is None
    assert decision.details["evidence_status"] == "test"
    with pytest.raises(RuntimeError, match="wrong candidate"):
        _decision_for(type(CANDIDATE)("luanti", 14758), _bridge())
    with pytest.raises(RuntimeError, match="not passed"):
        _decision_for(CANDIDATE, _bridge("downstream_pending"))


def test_mainline_admission_blocker_is_nonterminal() -> None:
    decision = _blocked_decision(CANDIDATE, _bridge())
    assert decision.state == "terminal"
    assert decision.outcome == "infrastructure_blocked"
    assert decision.details["evidence_status"] == "curated_admission_schema_blocked"


def test_materialized_binary_rejects_symlink_and_accepts_regular(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "fixed"
    binary.write_bytes(b"registered")
    binary.chmod(0o700)
    _require_executable_regular(tmp_path, "fixed")
    linked = tmp_path / "linked"
    linked.symlink_to(binary)
    with pytest.raises(RuntimeError, match="escapes root"):
        _require_executable_regular(tmp_path, "linked")


def test_current_catalog_admits_14757() -> None:
    workspace = Path.cwd()
    assert _catalog_contains(
        catalog_path=workspace / "benchmarks/p5/lazarus/curated.yaml",
        workspace_root=workspace,
        task_id="lz-luanti-14757-math-round",
    )
    assert not _catalog_contains(
        catalog_path=workspace / "benchmarks/p5/lazarus/curated.yaml",
        workspace_root=workspace,
        task_id="lz-luanti-14586-full-crafting-shift-move",
    )
