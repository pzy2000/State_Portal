from __future__ import annotations

import os

from warptest.c1_lazarus_selection.luanti_15251_gate57_blocker import (
    write_luanti_15251_gate57_blocker,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 15251}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/15251/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/15251/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/15251/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/15251/test_manifest/v2", "candidate_stage", "2.0"
)
_PRODUCTION = (
    "src/debug.cpp",
    "src/map.cpp",
    "src/map.h",
    "src/mapblock.cpp",
    "src/mapblock.h",
    "src/mapgen/dungeongen.cpp",
    "src/script/common/c_content.cpp",
    "src/script/cpp_api/s_base.cpp",
    "src/script/lua_api/l_object.cpp",
    "src/script/lua_api/l_object.h",
    "src/script/lua_api/l_playermeta.cpp",
    "src/script/lua_api/l_playermeta.h",
    "src/script/lua_api/l_vmanip.cpp",
    "src/script/lua_api/l_vmanip.h",
    "src/threading/event.cpp",
    "src/threading/mutex_auto_lock.h",
    "src/threading/thread.cpp",
    "src/unittest/mock_serveractiveobject.h",
    "src/util/stream.h",
    "src/voxel.cpp",
    "src/voxel.h",
    "src/voxelalgorithms.cpp",
)
_TEST = ("src/unittest/test_moveaction.cpp", "src/unittest/test_voxelarea.cpp")


def _auth() -> AuthorizationRecord:
    return AuthorizationRecord(
        "local-preannotation/v2-luanti-15251-test",
        "preannotation-execution",
        "2026-08-14T00:00:00Z",
        os.getuid(),
    )


def _write_chain(tmp_path):
    root = tmp_path / "artifact-root"
    root.mkdir()
    raw_source = "raw/github/luanti/15251/source-manifest.json"
    raw_patch = "raw/github/luanti/15251/pull.diff"
    (root / raw_source).parent.mkdir(parents=True)
    (root / raw_source).write_text("{}", encoding="utf-8")
    sections = []
    paths = ("doc/lua_api.md", *_PRODUCTION, *_TEST)
    for path in paths:
        if path == "src/voxel.h":
            body = (
                "@@ -1 +1 @@\n"
                "-\tvoid diff(const VoxelArea &a, std::list<VoxelArea> &result)\n"
                "+\ttemplate <typename C>\n"
                "+\tvoid diff(const VoxelArea &a, C &result) const\n"
            )
        elif path == "src/unittest/test_voxelarea.cpp":
            body = (
                "@@ -1 +1 @@\n"
                "+\tstd::vector<VoxelArea> res;\n"
                "+\tv1.diff(VoxelArea(), res);\n"
            )
        else:
            body = "@@ -1 +1 @@\n+\t/* safety */\n"
        sections.append(
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{body}"
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
            "primary_issue_number": 14435,
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
            "details": {"executable_test_paths": list(_TEST), "test_count": 2},
        },
        authorization=auth,
    )
    return root


def test_gate57_blocker_records_inventory_and_fixed_only_diff_api(tmp_path):
    root = _write_chain(tmp_path)
    artifact = write_luanti_15251_gate57_blocker(root, authorization=_auth())

    assert artifact.payload["outcome"] == "gate4_compatibility_blocked"
    assert artifact.payload["current_gate_status"] == {
        "source_authority": "passed",
        "single_defect_mapping": "passed",
        "strict_inverse_patch": "blocked_undeclared_patch_path",
        "test_api_compatibility": "blocked_fixed_only_api",
        "fresh_gate57_epoch": "blocked_before_first_attempt",
    }
    audit = artifact.payload["patch_audit"]
    assert audit["undeclared_paths"] == ["doc/lua_api.md"]
    assert audit["fixed_test_call_count"] == 1
    assert artifact.payload["fresh_epoch"]["attempt_count"] == 0
    assert artifact.payload["candidate_execution_performed"] is False

    same = write_luanti_15251_gate57_blocker(root, authorization=_auth())
    assert same.revision_path == artifact.revision_path
