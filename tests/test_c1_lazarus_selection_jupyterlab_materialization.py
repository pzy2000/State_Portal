from __future__ import annotations

import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.jupyterlab_candidate_materialization import (
    CONFIRMATION,
    CandidateSourcePlan,
    materialize_fixed_and_buggy,
    write_active_downstream_plan,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="authorization/jupyterlab-materialization-test/v1",
        scope="version-materialization",
        granted_at="2026-08-13T15:00:00Z",
        local_user_id=os.getuid(),
    )


def _plan(root: Path) -> CandidateSourcePlan:
    candidate = CandidateKey("jupyterlab", 444)
    source = root / "private/fixed-capsules/jupyterlab/444/captured-merged-tree"
    production = "src/notebook/notebook/actions.ts"
    test = "test/src/notebook/notebook/actions.spec.ts"
    (source / production).parent.mkdir(parents=True)
    (source / production).write_text("fixed production\n", encoding="utf-8")
    (source / test).parent.mkdir(parents=True)
    (source / test).write_text("fixed regression test\n", encoding="utf-8")
    (source / "README.md").write_text("complete source\n", encoding="utf-8")
    reference = ArtifactReference(
        "fixed_source_capsule/jupyterlab/444/fixed/v2",
        "fixed_source_capsule",
        "2.0",
    )
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at="2026-08-13T14:00:00Z",
        inputs=(),
        payload={"candidate": candidate.to_value()},
        authorization=AuthorizationRecord(
            authorization_id="authorization/jupyterlab-capsule-test/v1",
            scope="protocol-migration",
            granted_at="2026-08-13T13:00:00Z",
            local_user_id=os.getuid(),
        ),
    )
    patch = f"""diff --git a/{production} b/{production}
--- a/{production}
+++ b/{production}
@@ -1 +1 @@
-buggy production
+fixed production
""".encode()
    return CandidateSourcePlan(
        candidate=candidate,
        profile_id="jupyterlab-444-test-v1",
        fixed_capsule_reference=reference,
        fixed_source_tree=source,
        production_paths=(production,),
        test_paths=(test,),
        reverse_patch=patch,
        apply_argv=(
            "/usr/bin/git",
            "apply",
            "-R",
            "--whitespace=nowarn",
            "-",
        ),
        runtime_dependency_id="jupyterlab-history-test",
        runtime_root="private/dependencies/jupyterlab-history-test/1.0",
    )


def test_materializes_fixed_and_buggy_but_keeps_candidate_active(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)

    artifact = materialize_fixed_and_buggy(
        artifact_root=tmp_path,
        plan=plan,
        authorization=_authorization(),
        confirmation=CONFIRMATION,
    )

    fixed = tmp_path / artifact.payload["fixed_tree_path"]
    buggy = tmp_path / artifact.payload["buggy_tree_path"]
    assert (fixed / plan.production_paths[0]).read_text() == "fixed production\n"
    assert (buggy / plan.production_paths[0]).read_text() == "buggy production\n"
    assert (fixed / plan.test_paths[0]).read_bytes() == (
        buggy / plan.test_paths[0]
    ).read_bytes()
    assert (buggy / "README.md").read_text() == "complete source\n"
    assert artifact.payload["candidate_lifecycle"] == "active"
    assert artifact.payload["downstream_state"] == "runtime_dependency_pending"
    assert artifact.payload["next_stage"] == "test_repetitions"

    resumed = materialize_fixed_and_buggy(
        artifact_root=tmp_path,
        plan=plan,
        authorization=_authorization(),
        confirmation=CONFIRMATION,
    )
    assert resumed.revision_path == artifact.revision_path

    runtime = ArtifactReference(
        "dependency_preparation/jupyterlab-history-test/current/v1",
        "dependency_preparation",
        "1.0",
    )
    write_stable_artifact(
        tmp_path,
        artifact_id=runtime.artifact_id,
        artifact_kind=runtime.artifact_kind,
        semantic_version=runtime.semantic_version,
        created_at="2026-08-13T15:00:01Z",
        inputs=(),
        payload={
            "complete": True,
            "outcome": "infrastructure_blocked",
            "dependency_id": plan.runtime_dependency_id,
            "runtime_root": plan.runtime_root,
            "missing_components": ["node", "firefox"],
            "mismatched_components": [],
        },
        authorization=AuthorizationRecord(
            authorization_id="authorization/jupyterlab-runtime-test/v1",
            scope="infrastructure-audit",
            granted_at="2026-08-13T15:00:00Z",
            local_user_id=os.getuid(),
        ),
    )
    downstream = write_active_downstream_plan(
        artifact_root=tmp_path,
        plan=plan,
        authorization=AuthorizationRecord(
            authorization_id="authorization/jupyterlab-downstream-test/v1",
            scope="preannotation-planning",
            granted_at="2026-08-13T15:00:02Z",
            local_user_id=os.getuid(),
        ),
    )
    assert downstream.payload["terminal"] is False
    assert downstream.payload["candidate_lifecycle"] == "active"
    assert downstream.payload["readiness_state"] == "runtime_dependency_pending"
    assert downstream.payload["pending_components"] == ["node", "firefox"]


def test_materialization_rejects_nonproduction_patch_changes(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    bad = CandidateSourcePlan(
        **{
            **plan.__dict__,
            "reverse_patch": plan.reverse_patch
            + b"diff --git a/README.md b/README.md\n"
            + b"--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n"
            + b"-buggy readme\n+complete source\n",
        }
    )

    with pytest.raises(RuntimeError, match="inverse patch changed non-production"):
        materialize_fixed_and_buggy(
            artifact_root=tmp_path,
            plan=bad,
            authorization=_authorization(),
            confirmation=CONFIRMATION,
        )
