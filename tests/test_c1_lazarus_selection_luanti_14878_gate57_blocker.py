from __future__ import annotations

import os

from warptest.c1_lazarus_selection.luanti_14878_gate57_blocker import (
    write_luanti_14878_gate57_blocker,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        "local-preannotation/v2-luanti-14878-test",
        "preannotation-execution",
        "2026-08-14T00:00:00Z",
        os.getuid(),
    )


def _write_chain(tmp_path):
    root = tmp_path / "artifact-root"
    root.mkdir()
    auth = _authorization()
    source = ArtifactReference(
        "candidate_source_packet/luanti/14878/source/v2",
        "candidate_source_packet",
        "2.0",
    )
    patch = ArtifactReference(
        "candidate_test_patch/luanti/14878/screening/v2",
        "candidate_test_patch",
        "2.0",
    )
    atomicity = ArtifactReference(
        "source_atomicity/luanti/14878/preannotation/v2",
        "source_atomicity",
        "2.0",
    )
    manifest = ArtifactReference(
        "candidate_stage/luanti/14878/test_manifest/v2",
        "candidate_stage",
        "2.0",
    )
    raw_source = "raw/github/luanti/14878/source-manifest.json"
    raw_patch = "raw/github/luanti/14878/pull.diff"
    patch_path = root / raw_patch
    patch_path.parent.mkdir(parents=True)
    patch_path.write_text(
        """diff --git a/doc/lua_api.md b/doc/lua_api.md
--- a/doc/lua_api.md
+++ b/doc/lua_api.md
@@ -1 +1 @@
+documentation
diff --git a/src/util/string.cpp b/src/util/string.cpp
--- a/src/util/string.cpp
+++ b/src/util/string.cpp
@@ -1 +1 @@
+std::string sanitize_untrusted(std::string_view value)
diff --git a/src/util/string.h b/src/util/string.h
--- a/src/util/string.h
+++ b/src/util/string.h
@@ -1 +1 @@
+std::string sanitize_untrusted(std::string_view value);
diff --git a/src/unittest/test_utilities.cpp b/src/unittest/test_utilities.cpp
--- a/src/unittest/test_utilities.cpp
+++ b/src/unittest/test_utilities.cpp
@@ -1 +1 @@
+UASSERTEQ(auto, sanitize_untrusted("x"), "x");
""",
        encoding="utf-8",
    )
    (root / raw_source).parent.mkdir(parents=True, exist_ok=True)
    (root / raw_source).write_text(
        '{"candidate": {"repository_key": "luanti", "change_number": 14878}}',
        encoding="utf-8",
    )

    candidate = {"repository_key": "luanti", "change_number": 14878}
    write_stable_artifact(
        root,
        artifact_id=source.artifact_id,
        artifact_kind=source.artifact_kind,
        semantic_version=source.semantic_version,
        created_at="2026-08-14T00:00:01Z",
        inputs=(),
        payload={
            "candidate": candidate,
            "complete": True,
            "raw_evidence_path": raw_source,
        },
        authorization=auth,
    )
    write_stable_artifact(
        root,
        artifact_id=patch.artifact_id,
        artifact_kind=patch.artifact_kind,
        semantic_version=patch.semantic_version,
        created_at="2026-08-14T00:00:02Z",
        inputs=(source,),
        payload={
            "candidate": candidate,
            "complete": True,
            "outcome": "patch_eligible",
            "production_paths": [
                "src/network/serverpackethandler.cpp",
                "src/util/string.cpp",
                "src/util/string.h",
            ],
            "test_paths": ["src/unittest/test_utilities.cpp"],
            "raw_patch_path": raw_patch,
        },
        authorization=auth,
    )
    write_stable_artifact(
        root,
        artifact_id=atomicity.artifact_id,
        artifact_kind=atomicity.artifact_kind,
        semantic_version=atomicity.semantic_version,
        created_at="2026-08-14T00:00:03Z",
        inputs=(source, patch),
        payload={
            "candidate": candidate,
            "complete": True,
            "outcome": "accepted",
            "authoritative_issue_count": 1,
            "primary_issue_number": 14875,
        },
        authorization=auth,
    )
    write_stable_artifact(
        root,
        artifact_id=manifest.artifact_id,
        artifact_kind=manifest.artifact_kind,
        semantic_version=manifest.semantic_version,
        created_at="2026-08-14T00:00:04Z",
        inputs=(patch,),
        payload={
            "candidate": candidate,
            "complete": True,
            "stage": "test_manifest",
            "state": "passed",
            "details": {
                "executable_test_paths": ["src/unittest/test_utilities.cpp"],
                "test_count": 1,
            },
        },
        authorization=auth,
    )
    return root


def test_gate57_blocker_records_fixed_only_api_and_inventory_drift(tmp_path):
    root = _write_chain(tmp_path)
    artifact = write_luanti_14878_gate57_blocker(root, authorization=_authorization())

    assert artifact.payload["outcome"] == "gate4_compatibility_blocked"
    assert artifact.payload["current_gate_status"]["test_api_compatibility"] == (
        "blocked_fixed_only_api"
    )
    audit = artifact.payload["patch_audit"]
    assert audit["undeclared_paths"] == ["doc/lua_api.md"]
    assert audit["preserved_test_fixed_api_call_count"] == 1
    assert artifact.payload["fresh_epoch"]["attempt_count"] == 0
    assert artifact.payload["candidate_execution_performed"] is False

    same = write_luanti_14878_gate57_blocker(root, authorization=_authorization())
    assert same.revision_path == artifact.revision_path
