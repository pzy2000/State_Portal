from __future__ import annotations

import os

from warptest.c1_lazarus_selection.luanti_12879_gate57_blocker import (
    write_luanti_12879_gate57_blocker,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 12879}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/12879/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/12879/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/12879/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/12879/test_manifest/v2", "candidate_stage", "2.0"
)
_PRODUCTION = (
    "games/devtest/mods/unittests/metadata.lua",
    "src/database/database-postgresql.cpp",
    "src/database/database-postgresql.h",
    "src/server.cpp",
)
_TEST = ("src/unittest/test_modmetadatadatabase.cpp",)


def _auth() -> AuthorizationRecord:
    return AuthorizationRecord(
        "local-preannotation/v2-luanti-12879-test",
        "preannotation-execution",
        "2026-08-14T00:00:00Z",
        os.getuid(),
    )


def _write_chain(tmp_path):
    root = tmp_path / "artifact-root"
    root.mkdir()
    raw_source = "raw/github/luanti/12879/source-manifest.json"
    raw_patch = "raw/github/luanti/12879/pull.diff"
    (root / raw_source).parent.mkdir(parents=True)
    (root / raw_source).write_text("{}", encoding="utf-8")
    sections = []
    paths = (
        ".github/workflows/build.yml",
        *_PRODUCTION,
        *_TEST,
        "util/ci/common.sh",
    )
    for path in paths:
        if path == "src/database/database-postgresql.h":
            body = "+class ModMetadataDatabasePostgreSQL {};\n"
        elif path == "src/database/database-postgresql.cpp":
            body = (
                "+ModMetadataDatabasePostgreSQL::ModMetadataDatabasePostgreSQL() {}\n"
            )
        elif path == "src/unittest/test_modmetadatadatabase.cpp":
            body = "+ModMetadataDatabasePostgreSQL db(connect_string);\n"
        else:
            body = "+/* change */\n"
        sections.append(
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}"
        )
    patch_path = root / raw_patch
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text("".join(sections), encoding="utf-8")

    auth = _auth()
    write_stable_artifact(
        root,
        artifact_id=_SOURCE.artifact_id,
        artifact_kind=_SOURCE.artifact_kind,
        semantic_version=_SOURCE.semantic_version,
        created_at="2026-08-14T00:00:01Z",
        inputs=(),
        payload={
            "candidate": _CANDIDATE,
            "complete": True,
            "raw_evidence_path": raw_source,
        },
        authorization=auth,
    )
    write_stable_artifact(
        root,
        artifact_id=_PATCH.artifact_id,
        artifact_kind=_PATCH.artifact_kind,
        semantic_version=_PATCH.semantic_version,
        created_at="2026-08-14T00:00:02Z",
        inputs=(_SOURCE,),
        payload={
            "candidate": _CANDIDATE,
            "complete": True,
            "outcome": "patch_eligible",
            "production_paths": list(_PRODUCTION),
            "test_paths": list(_TEST),
            "source_manifest_path": raw_source,
            "raw_patch_path": raw_patch,
        },
        authorization=auth,
    )
    write_stable_artifact(
        root,
        artifact_id=_ATOMICITY.artifact_id,
        artifact_kind=_ATOMICITY.artifact_kind,
        semantic_version=_ATOMICITY.semantic_version,
        created_at="2026-08-14T00:00:03Z",
        inputs=(_SOURCE, _PATCH),
        payload={
            "candidate": _CANDIDATE,
            "complete": True,
            "outcome": "accepted",
            "authoritative_issue_count": 1,
            "primary_issue_number": 12873,
        },
        authorization=auth,
    )
    write_stable_artifact(
        root,
        artifact_id=_MANIFEST.artifact_id,
        artifact_kind=_MANIFEST.artifact_kind,
        semantic_version=_MANIFEST.semantic_version,
        created_at="2026-08-14T00:00:04Z",
        inputs=(_PATCH,),
        payload={
            "candidate": _CANDIDATE,
            "complete": True,
            "stage": "test_manifest",
            "state": "passed",
            "details": {"executable_test_paths": list(_TEST), "test_count": 1},
        },
        authorization=auth,
    )
    return root


def test_gate57_blocker_records_inventory_and_fixed_only_provider(tmp_path):
    root = _write_chain(tmp_path)
    artifact = write_luanti_12879_gate57_blocker(root, authorization=_auth())

    assert artifact.payload["outcome"] == "gate4_compatibility_blocked"
    assert artifact.payload["current_gate_status"]["strict_inverse_patch"] == (
        "blocked_undeclared_patch_path"
    )
    assert artifact.payload["current_gate_status"]["test_api_compatibility"] == (
        "blocked_fixed_only_api"
    )
    audit = artifact.payload["patch_audit"]
    assert audit["undeclared_paths"] == [
        ".github/workflows/build.yml",
        "util/ci/common.sh",
    ]
    assert audit["preserved_test_fixed_api_call_count"] == 1
    assert artifact.payload["fresh_epoch"]["attempt_count"] == 0
    assert artifact.payload["candidate_execution_performed"] is False

    same = write_luanti_12879_gate57_blocker(root, authorization=_auth())
    assert same.revision_path == artifact.revision_path
