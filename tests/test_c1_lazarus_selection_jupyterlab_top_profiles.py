from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection import jupyterlab_top_profiles as module
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization(scope: str = "candidate-screening") -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="authorization/jupyterlab-top-test/v1",
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


def _patch(profile: module.JupyterLabTopProfile) -> bytes:
    sections: list[str] = []
    for path in profile.production_paths:
        sections.append(
            f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1 @@
-buggy production
+fixed production
"""
        )
    for path in profile.patch_test_paths:
        sections.append(
            f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1,2 @@
 existing test
+{profile.selected_test_marker}
"""
        )
    return "".join(sections).encode()


def _write_chain(root: Path, profile: module.JupyterLabTopProfile) -> None:
    number = profile.candidate.change_number
    issue_path = f"raw/github/jupyterlab/{number}/issue/response.body"
    patch_path = f"raw/github/jupyterlab/{number}/pull-diff/response.body"
    source_path = f"raw/github/jupyterlab/{number}/source-manifest/source.json"
    _write(root, issue_path, json.dumps({"number": profile.primary_issue_number}))
    _write(root, patch_path, _patch(profile))
    _write(
        root,
        source_path,
        json.dumps(
            {
                "authority": {
                    "provider": "github",
                    "repository": "jupyterlab/jupyterlab",
                },
                "candidate": profile.candidate.to_value(),
                "complete": True,
                "issue_numbers": [profile.primary_issue_number],
                "patch_raw_path": patch_path,
                "provider": "github",
                "pull": {"merged": True, "number": number},
            }
        ),
    )
    source = profile.source_reference
    patch = profile.patch_reference
    atomicity = profile.atomicity_reference
    manifest = profile.manifest_reference
    enumeration = ArtifactReference(
        "repository_enumeration/jupyterlab/current/v2",
        "repository_enumeration",
        "2.0",
    )
    checkpoint = ArtifactReference(
        f"screening_checkpoint/jupyterlab/{number}/current/v2",
        "screening_checkpoint",
        "2.0",
    )
    authorization = _authorization()
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
        inputs=(patch, atomicity),
        payload={
            "candidate": profile.candidate.to_value(),
            "complete": True,
            "details": {
                "executable_test_paths": list(profile.patch_test_paths),
                "test_count": len(profile.patch_test_paths),
            },
            "outcome": None,
            "stage": "test_manifest",
            "state": "passed",
        },
        authorization=authorization,
    )


def test_top_profiles_are_the_source_patch_qualified_candidates() -> None:
    assert [profile.candidate.change_number for profile in module.top_profiles()] == [
        2437,
        2752,
        3867,
    ]
    assert all(profile.tier == "at_goal_candidate" for profile in module.top_profiles())
    assert all(profile.primary_issue_number > 0 for profile in module.top_profiles())


def test_attachment_pending_candidates_are_not_in_frozen_roster() -> None:
    assert [
        profile.candidate.change_number for profile in module.pending_profiles()
    ] == [1846, 4796]
    assert {
        profile.candidate.change_number for profile in module.top_profiles()
    }.isdisjoint(
        profile.candidate.change_number for profile in module.pending_profiles()
    )


def test_uncaptured_authority_attachment_blocks_gate_one(tmp_path: Path) -> None:
    raw = "raw/github/jupyterlab/1846/issue/response.body"
    _write(
        tmp_path,
        raw,
        '{"body":"![screen](https://cloud.githubusercontent.com/assets/x.png)"}',
    )
    with pytest.raises(RuntimeError, match="attachment closure is incomplete"):
        module._validate_attachment_closure(
            tmp_path, {"raw_paths": [raw], "attachment_paths": []}
        )


def test_replacement_pool_is_separate_from_frozen_roster() -> None:
    assert [
        profile.candidate.change_number for profile in module.replacement_profiles()
    ] == [285, 574, 738, 2469, 3662, 3663]
    assert all(
        profile.tier == "replacement_candidate"
        for profile in module.replacement_profiles()
    )
    assert {
        profile.candidate.change_number for profile in module.top_profiles()
    }.isdisjoint(
        profile.candidate.change_number for profile in module.replacement_profiles()
    )


def test_static_inverse_never_contains_test_sections() -> None:
    for profile in module.top_profiles():
        sections = dict(module._parse_patch_sections(_patch(profile)))
        inverse = b"".join(sections[path] for path in profile.production_paths)
        assert inverse
        for test_path in profile.patch_test_paths:
            assert test_path.encode() not in inverse


@pytest.mark.parametrize(
    "number", [2437, 2752, 3867, 285, 574, 738, 2469, 3662, 3663]
)
def test_chain_profile_and_preparation_reload_without_downstream_rejection(
    tmp_path: Path, number: int
) -> None:
    profile = module.profile_for_top_candidate(CandidateKey("jupyterlab", number))
    _write_chain(tmp_path, profile)

    chain = module.load_top_candidate_chain(tmp_path, profile.candidate)
    assert chain.patch_paths == profile.production_paths + profile.patch_test_paths
    preparation = module.prepare_top_candidate(tmp_path, profile.candidate)
    assert preparation.downstream_status == "downstream_pending"
    assert preparation.runtime_status == "pending"

    profile_artifact = module.write_top_profile(
        tmp_path, profile.candidate, authorization=_authorization()
    )
    expected_tier = (
        "replacement_candidate"
        if number in {285, 574, 738, 2469, 3662, 3663}
        else "at_goal_candidate"
    )
    assert profile_artifact.payload["tier"] == expected_tier
    assert profile_artifact.payload["downstream_status"] == "downstream_pending"
    preparation_artifact = module.write_top_preparation(
        tmp_path, profile.candidate, authorization=_authorization()
    )
    assert preparation_artifact.payload["outcome"] == "downstream_pending"
    assert preparation_artifact.payload["candidate_execution_performed"] is False


def test_static_patch_parser_rejects_duplicate_paths() -> None:
    profile = module.profile_for_top_candidate(CandidateKey("jupyterlab", 3867))
    duplicate = _patch(profile) + _patch(profile).split(b"diff --git ", 1)[1].join(
        [b"diff --git ", b""]
    )
    with pytest.raises(RuntimeError, match="duplicate"):
        module._parse_patch_sections(duplicate)


def test_3867_runtime_gap_is_active_pending_not_rejection(tmp_path: Path) -> None:
    candidate = CandidateKey("jupyterlab", 3867)
    profile = module.profile_for_top_candidate(candidate)
    _write_chain(tmp_path, profile)
    fixed = tmp_path / profile.fixed_source_tree_path
    _write(
        fixed,
        "tests/package.json",
        json.dumps(
            {
                "version": "0.2.4",
                "devDependencies": {
                    "karma": "~1.7.1",
                    "mocha": "~3.5.3",
                    "karma-mocha": "~0.2.2",
                    "webpack": "~2.7.0",
                    "typescript": "~2.6.2",
                },
            }
        ),
    )
    _write(
        fixed,
        profile.package_metadata_path,
        json.dumps({"version": "0.1.5"}),
    )
    _write(fixed, ".travis.yml", "node_js:\n- '5'\n  firefox: latest\n")
    _write(
        fixed,
        "jupyterlab/staging/yarn.js",
        'module.exports = {"version": "1.3.2"};\n',
    )
    profile_artifact = module.write_top_profile(
        tmp_path, candidate, authorization=_authorization()
    )
    preparation = module.write_top_preparation(
        tmp_path, candidate, authorization=_authorization()
    )
    write_stable_artifact(
        tmp_path,
        artifact_id=profile.capsule_reference.artifact_id,
        artifact_kind=profile.capsule_reference.artifact_kind,
        semantic_version=profile.capsule_reference.semantic_version,
        created_at="2026-08-13T20:00:10Z",
        inputs=(profile_artifact.reference,),
        payload={"candidate": candidate.to_value()},
        authorization=_authorization("protocol-migration"),
    )

    artifact = module.write_top_runtime_pending(
        tmp_path,
        candidate,
        authorization=_authorization("infrastructure-audit"),
    )

    assert preparation.payload["outcome"] == "downstream_pending"
    assert artifact.inputs == (preparation.reference, profile.capsule_reference)
    assert artifact.payload["outcome"] == "pending"
    assert artifact.payload["candidate_lifecycle"] == "active"
    assert artifact.payload["terminal"] is False
    assert artifact.payload["captured_components"] == {
        "yarn": "1.3.2",
        "yarn_path": "jupyterlab/staging/yarn.js",
    }
    assert (
        "historical_firefox_latest_resolution_and_binary"
        in artifact.payload["missing_components"]
    )
    assert artifact.payload["candidate_execution_performed"] is False
