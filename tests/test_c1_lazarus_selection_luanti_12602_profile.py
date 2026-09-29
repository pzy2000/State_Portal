from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection import luanti_12602_profile as module
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization(scope: str = "candidate-screening") -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="authorization/luanti-12602-profile-test/v1",
        scope=scope,
        granted_at="2026-08-13T20:00:00Z",
        local_user_id=os.getuid(),
    )


def _write(root: Path, relative: str, content: str | bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _patch() -> bytes:
    profile = module.LUANTI_12602_PROFILE
    production = """diff --git a/src/server/mods.cpp b/src/server/mods.cpp
--- a/src/server/mods.cpp
+++ b/src/server/mods.cpp
@@ -1 +1,2 @@
-old order
+new reverse order
"""
    test_lines = "\n".join(f"+{marker}" for marker in module._TEST_MARKERS)
    test = f"""diff --git a/{profile.patch_test_paths[0]} b/{profile.patch_test_paths[0]}
--- a/{profile.patch_test_paths[0]}
+++ b/{profile.patch_test_paths[0]}
@@ -1 +1,8 @@
 existing test
{test_lines}
"""
    return (production + test).encode()


def _write_chain(root: Path, *, authority: str = "luanti-org/luanti") -> None:
    profile = module.LUANTI_12602_PROFILE
    number = profile.candidate.change_number
    source_path = f"raw/github/luanti/{number}/source/source.json"
    patch_path = f"raw/github/luanti/{number}/patch/patch.diff"
    _write(
        root,
        source_path,
        json.dumps(
            {
                "authority": {"provider": "github", "repository": authority},
                "candidate": profile.candidate.to_value(),
                "complete": True,
                "issue_numbers": [profile.primary_issue_number],
                "patch_raw_path": patch_path,
                "provider": "github",
                "pull": {"merged": True, "number": number},
            }
        ),
    )
    _write(root, patch_path, _patch())
    authorization = _authorization()
    source = profile.source_reference
    patch = profile.patch_reference
    atomicity = profile.atomicity_reference
    manifest = profile.manifest_reference
    enumeration = ArtifactReference(
        "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
    )
    checkpoint = ArtifactReference(
        f"screening_checkpoint/luanti/{number}/current/v2",
        "screening_checkpoint",
        "2.0",
    )
    write_stable_artifact(
        root,
        artifact_id=source.artifact_id,
        artifact_kind=source.artifact_kind,
        semantic_version=source.semantic_version,
        created_at="2026-08-13T20:00:01Z",
        inputs=(enumeration,),
        payload={
            "candidate": profile.candidate.to_value(),
            "complete": True,
            "raw_evidence_path": source_path,
        },
        authorization=authorization,
    )
    write_stable_artifact(
        root,
        artifact_id=patch.artifact_id,
        artifact_kind=patch.artifact_kind,
        semantic_version=patch.semantic_version,
        created_at="2026-08-13T20:00:02Z",
        inputs=(enumeration, source),
        payload={
            "candidate": profile.candidate.to_value(),
            "complete": True,
            "outcome": "patch_eligible",
            "production_paths": list(profile.production_paths),
            "raw_patch_path": patch_path,
            "source_manifest_path": source_path,
            "test_paths": list(profile.patch_test_paths),
        },
        authorization=authorization,
    )
    write_stable_artifact(
        root,
        artifact_id=atomicity.artifact_id,
        artifact_kind=atomicity.artifact_kind,
        semantic_version=atomicity.semantic_version,
        created_at="2026-08-13T20:00:03Z",
        inputs=(enumeration, source, patch, checkpoint),
        payload={
            "authoritative_issue_count": 1,
            "authoritative_issue_numbers": [profile.primary_issue_number],
            "candidate": profile.candidate.to_value(),
            "complete": True,
            "linked_issue_count": 1,
            "linked_issue_numbers": [profile.primary_issue_number],
            "outcome": "accepted",
            "primary_issue_number": profile.primary_issue_number,
            "reason": None,
            "receipt_schema": "c1_lazarus_source_atomicity_v2",
            "source_manifest_path": source_path,
        },
        authorization=authorization,
    )
    write_stable_artifact(
        root,
        artifact_id=manifest.artifact_id,
        artifact_kind=manifest.artifact_kind,
        semantic_version=manifest.semantic_version,
        created_at="2026-08-13T20:00:04Z",
        inputs=(patch,),
        payload={
            "candidate": profile.candidate.to_value(),
            "complete": True,
            "details": {
                "executable_test_paths": list(profile.patch_test_paths),
                "execution_candidate_kind": "added_assertion",
                "test_count": 1,
            },
            "outcome": None,
            "stage": "test_manifest",
            "state": "passed",
        },
        authorization=authorization,
    )


def test_profile_is_registered_and_candidate_specific() -> None:
    profile = module.profile_for_candidate(CandidateKey("luanti", 12602))
    assert profile.profile_id == "luanti-12602-media-override-v1"
    assert profile.production_paths == ("src/server/mods.cpp",)
    assert profile.patch_test_paths == ("src/unittest/test_servermodmanager.cpp",)


def test_preflight_accepts_gates_1_to_4_and_leaves_downstream_pending(
    tmp_path: Path,
) -> None:
    _write_chain(tmp_path)
    preflight = module.prepare_candidate(tmp_path)
    assert preflight.test_api_markers == module._TEST_MARKERS
    assert preflight.production_inverse_patch
    assert preflight.capsule_status == "pending"
    assert preflight.downstream_status == "downstream_pending"

    profile = module.write_profile(tmp_path, authorization=_authorization())
    statuses = profile.payload["strict_gate_status"]
    assert statuses["source_authority"] == "passed"
    assert statuses["single_defect_mapping"] == "passed"
    assert statuses["strict_inverse_patch"] == "passed"
    assert statuses["test_api_compatibility"] == "passed"
    assert statuses["version_materialization"] == "downstream_pending"
    preparation = module.write_preparation(tmp_path, authorization=_authorization())
    assert preparation.payload["outcome"] == "downstream_pending"
    assert preparation.payload["candidate_execution_performed"] is False


def test_preflight_rejects_wrong_authority(tmp_path: Path) -> None:
    _write_chain(tmp_path, authority="other/project")
    with pytest.raises(RuntimeError, match="authority"):
        module.prepare_candidate(tmp_path)


def test_preflight_rejects_missing_test_api_marker(tmp_path: Path) -> None:
    _write_chain(tmp_path)
    patch_path = tmp_path / "raw/github/luanti/12602/patch/patch.diff"
    content = patch_path.read_text(encoding="utf-8")
    patch_path.write_text(
        content.replace(
            "std::find(result.begin(), result.end(),",
            "std::search(result.begin(), result.end(),",
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="test API markers"):
        module.prepare_candidate(tmp_path)
