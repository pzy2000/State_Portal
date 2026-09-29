from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from warptest.c1_lazarus_selection import c1_freeze_catalog_audit as target
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization(scope: str = "infrastructure-audit") -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=f"test/c1-freeze-catalog-audit/{scope}",
        scope=scope,
        granted_at="2026-08-15T04:00:00Z",
        local_user_id=os.getuid(),
    )


def _write(
    root: Path,
    artifact_id: str,
    kind: str,
    payload: dict[str, object],
    inputs: tuple[ArtifactReference, ...] = (),
) -> ArtifactReference:
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind=kind,
        semantic_version="1.0",
        created_at="2026-08-15T03:00:00Z",
        inputs=inputs,
        payload=payload,
        authorization=_authorization(),
    )
    return ArtifactReference(artifact_id, kind, "1.0")


def _roster(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "schema": "c1-lazarus-freeze-sprint-roster-v2",
                "selection_status": "provisional_not_frozen",
                "freeze_target": {
                    "task_count": 24,
                    "defect_locus": {"at_goal": 16, "on_path": 8},
                },
                "candidates": rows,
            }
        ),
        encoding="utf-8",
    )
    return path


def _complete_candidate(root: Path, change: int, locus: str) -> dict[str, object]:
    gates = []
    for number, kind in (
        (8, "candidate_gate8_materialization"),
        (9, "candidate_gate9_replay"),
        (10, "candidate_gate10_hidden_oracle"),
        (11, "candidate_gate11_edge_observation"),
        (12, "candidate_gate12_semantic_pixel_matrix"),
        (13, "candidate_gate13_same_lineage"),
    ):
        gates.append(
            _write(
                root,
                f"{kind}/jupyterlab/{change}/v1",
                kind,
                {
                    "state": "passed",
                    "complete": True,
                    "paid_actions_authorized": False,
                    "formal_trials_authorized": False,
                },
            )
        )
    pool = _write(
        root,
        f"candidate_gate57_pool_admission/jupyterlab/{change}/v1",
        "candidate_gate57_pool_admission",
        {"state": "active_qualified", "strict_gate_1_7": True},
    )
    _write(
        root,
        f"candidate_formal_freeze_admission/jupyterlab/{change}/v1",
        "candidate_formal_freeze_admission",
        {
            "state": "admitted",
            "complete": True,
            "strict_gate_1_7": True,
            "gate8_materialization_runtime": "passed",
            "gate9_registered_start_replay": "passed",
            "gate10_hidden_oracle": "passed",
            "gate11_real_edge_observations": "passed",
            "gate12_semantic_pixel_2x2": "passed",
            "gate13_same_lineage": "passed",
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        inputs=tuple(gates) + (pool,),
    )
    return {
        "repository_key": "jupyterlab",
        "change_number": change,
        "defect_locus": locus,
        "status": "replacement_gate1_7_passed_downstream_pending",
    }


def test_audit_passes_when_every_row_has_a_complete_admission(tmp_path: Path) -> None:
    rows = [
        _complete_candidate(tmp_path, 1000 + index, "at_goal" if index < 16 else "on_path")
        for index in range(24)
    ]
    roster = _roster(tmp_path / "roster.yaml", rows)

    audits = target.audit_freeze_catalog(tmp_path, roster)

    assert len(audits) == 24
    assert all(audit.passed for audit in audits)
    assert target.roster_shape_problems(roster) == ()
    artifact = target.write_catalog_audit(
        tmp_path, roster, authorization=_authorization()
    )
    assert artifact.payload["freeze_ready"] is True
    assert artifact.payload["admitted_row_count"] == 24
    assert artifact.payload["admitted_locus_split"] == {"at_goal": 16, "on_path": 8}


def test_missing_gate_evidence_blocks_the_freeze(tmp_path: Path) -> None:
    rows = [
        _complete_candidate(tmp_path, 2000 + index, "at_goal" if index < 16 else "on_path")
        for index in range(24)
    ]
    # Re-point one admission at an incomplete closure without a Gate 10 oracle.
    partial = _write(
        tmp_path,
        "candidate_gate11_edge_observation/jupyterlab/2000/partial/v1",
        "candidate_gate11_edge_observation",
        {"state": "passed", "complete": True},
    )
    _write(
        tmp_path,
        "candidate_formal_freeze_admission/jupyterlab/2000/partial-v1",
        "candidate_formal_freeze_admission",
        {
            "state": "admitted",
            "complete": True,
            "strict_gate_1_7": True,
            "gate11_real_edge_observations": "passed",
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        inputs=(partial,),
    )
    roster = _roster(tmp_path / "roster.yaml", rows)

    artifact = target.write_catalog_audit(
        tmp_path, roster, authorization=_authorization()
    )

    assert artifact.payload["freeze_ready"] is False
    blocked = [row for row in artifact.payload["rows"] if not row["passed"]]
    assert len(blocked) == 1
    assert blocked[0]["change_number"] == 2000
    assert any("gate10" in problem for problem in blocked[0]["problems"])


def test_paid_or_inherited_evidence_blocks_the_freeze(tmp_path: Path) -> None:
    rows = [
        _complete_candidate(tmp_path, 3000 + index, "at_goal" if index < 16 else "on_path")
        for index in range(24)
    ]
    tainted = _write(
        tmp_path,
        "candidate_gate12_semantic_pixel_matrix/jupyterlab/3000/tainted/v1",
        "candidate_gate12_semantic_pixel_matrix",
        {
            "state": "passed",
            "complete": True,
            "synthetic_observation_frames": True,
            "paid_actions_authorized": True,
        },
    )
    _write(
        tmp_path,
        "candidate_formal_freeze_admission/jupyterlab/3000/tainted-v1",
        "candidate_formal_freeze_admission",
        {
            "state": "admitted",
            "complete": True,
            "strict_gate_1_7": True,
            "gate8_materialization_runtime": "passed",
            "gate9_registered_start_replay": "passed",
            "gate10_hidden_oracle": "passed",
            "gate11_real_edge_observations": "passed",
            "gate12_semantic_pixel_2x2": "passed",
            "gate13_same_lineage": "passed",
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        inputs=(tainted,),
    )
    roster = _roster(tmp_path / "roster.yaml", rows)

    artifact = target.write_catalog_audit(
        tmp_path, roster, authorization=_authorization()
    )

    assert artifact.payload["freeze_ready"] is False
    blocked = next(row for row in artifact.payload["rows"] if not row["passed"])
    assert blocked["paid_or_formal_authorized"] is True
    assert blocked["inherited_or_synthetic_evidence"] is True


def test_roster_structure_and_authorization_are_enforced(tmp_path: Path) -> None:
    rows = [_complete_candidate(tmp_path, 4000, "at_goal")]
    roster = _roster(tmp_path / "roster.yaml", rows)
    problems = target.roster_shape_problems(roster)
    assert any("rows instead of 24" in problem for problem in problems)
    assert any("defect locus split" in problem for problem in problems)
    with pytest.raises(PermissionError):
        target.write_catalog_audit(
            tmp_path, roster, authorization=_authorization("preannotation-execution")
        )
