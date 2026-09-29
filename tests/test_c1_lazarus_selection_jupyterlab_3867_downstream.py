from __future__ import annotations

import os
from pathlib import Path

from warptest.c1_lazarus_selection.jupyterlab_3867_downstream import (
    write_3867_downstream_pending_plan,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="authorization/jupyterlab-3867-downstream-test/v2",
        scope="preannotation-planning",
        granted_at="2026-08-14T01:00:00Z",
        local_user_id=os.getuid(),
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    payload: dict[str, object],
    *,
    inputs: tuple[ArtifactReference, ...] = (),
) -> None:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at="2026-08-14T00:00:00Z",
        inputs=inputs,
        payload=payload,
        authorization=AuthorizationRecord(
            authorization_id="authorization/jupyterlab-3867-seed-test/v1",
            scope="infrastructure-audit",
            granted_at="2026-08-13T23:00:00Z",
            local_user_id=os.getuid(),
        ),
    )


def _seed(root: Path) -> None:
    materialization = ArtifactReference(
        "candidate_materialization/jupyterlab/3867/fixed-buggy/v1",
        "candidate_materialization",
        "1.0",
    )
    runtime = ArtifactReference(
        "dependency_preparation/"
        "jupyterlab-node5-karma1.7.1-webpack2-typescript2.6.2/current/v1",
        "dependency_preparation",
        "1.0",
    )
    prior = ArtifactReference(
        "candidate_downstream_plan/jupyterlab/3867/preannotation/v1",
        "candidate_downstream_plan",
        "1.0",
    )
    fixed = "private/materialized/jupyterlab/3867/v1/fixed"
    buggy = "private/materialized/jupyterlab/3867/v1/buggy"
    for tree in (fixed, buggy):
        for relative in (
            "jupyterlab/staging/yarn.js",
            "yarn.lock",
            "tests/package.json",
            "tests/karma.conf.js",
            "tests/webpack.config.js",
            "tests/test-coreutils/src/url.spec.ts",
            "packages/coreutils/src/url.ts",
        ):
            path = root / tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("registered\n", encoding="utf-8")
    _write(
        root,
        materialization,
        {
            "candidate": {"repository_key": "jupyterlab", "change_number": 3867},
            "candidate_lifecycle": "active",
            "fixed_tree_path": fixed,
            "buggy_tree_path": buggy,
            "runtime_dependency_id": (
                "jupyterlab-node5-karma1.7.1-webpack2-typescript2.6.2"
            ),
            "runtime_root": (
                "private/dependencies/jupyterlab-3867/"
                "node5-karma1.7.1-webpack2-typescript2.6.2/1.0"
            ),
            "complete": True,
        },
    )
    _write(
        root,
        runtime,
        {
            "dependency_id": ("jupyterlab-node5-karma1.7.1-webpack2-typescript2.6.2"),
            "runtime_root": (
                "private/dependencies/jupyterlab-3867/"
                "node5-karma1.7.1-webpack2-typescript2.6.2/1.0"
            ),
            "candidate_lifecycle": "active",
            "outcome": "pending",
            "offline_runtime_buildable": False,
            "terminal": False,
            "missing_components": ["node_5_compatible_binary", "firefox"],
            "complete": True,
        },
    )
    _write(
        root,
        prior,
        {
            "candidate": {"repository_key": "jupyterlab", "change_number": 3867},
            "candidate_lifecycle": "active",
            "readiness_state": "runtime_dependency_pending",
            "terminal": False,
            "complete": True,
        },
        inputs=(materialization, runtime),
    )


def test_3867_pending_plan_is_candidate_specific_and_ready_for_future_3x(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)

    artifact = write_3867_downstream_pending_plan(
        tmp_path, authorization=_authorization()
    )

    assert artifact.reference.artifact_id == (
        "candidate_downstream_execution_plan/jupyterlab/3867/preannotation/v2"
    )
    assert artifact.payload["state"] == "downstream_pending"
    assert artifact.payload["terminal"] is False
    assert artifact.payload["candidate_execution_performed"] is False
    assert artifact.payload["execution_authorized"] is False
    assert artifact.payload["candidate_specific_workspace_root"] == (
        "private/downstream-runs/jupyterlab/3867/v2"
    )
    assert artifact.payload["repetition_contract"] == {
        "fixed_f2p": 3,
        "buggy_f2p": 3,
        "fixed_p2p": 3,
        "buggy_p2p": 3,
    }
    assert artifact.payload["disk_budget_mib"]["plan_write"] == 1
    assert artifact.payload["disk_budget_mib"]["future_peak"] == 6144
    assert (
        "complete_offline_lockfile_tarball_closure"
        in artifact.payload["pending_components"]
    )
