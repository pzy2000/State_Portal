from __future__ import annotations

import json
import os
from pathlib import Path

from warptest.c1_lazarus_selection.jupyterlab_regression import (
    plan_jupyterlab_regression_execution,
    profile_for_candidate,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.pipeline import StageDecision
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="authorization/jupyterlab-modern-profile-test/v1",
        scope="screening",
        granted_at="2026-08-13T12:00:00Z",
        local_user_id=os.getuid(),
    )


def _write(root: Path, relative: str, content: str | bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _patch(candidate: CandidateKey) -> bytes:
    profile = profile_for_candidate(candidate)
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
        marker = profile.patch_test_markers[path]
        sections.append(
            f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1,2 @@
 existing test
+{marker}
"""
        )
    return "".join(sections).encode()


def _write_chain(
    root: Path,
    candidate: CandidateKey,
    *,
    issue_body: str,
    pull_body: str,
    attachment_paths: list[str],
) -> None:
    profile = profile_for_candidate(candidate)
    issue_path = (
        f"raw/github/jupyterlab/{candidate.change_number}/"
        f"issue-{profile.primary_issue_number}/attempt-1/response.body"
    )
    pull_path = (
        f"raw/github/jupyterlab/{candidate.change_number}/pull/attempt-1/response.body"
    )
    patch_path = (
        f"raw/github/jupyterlab/{candidate.change_number}/"
        "pull-diff/attempt-1/response.body"
    )
    source_manifest = (
        f"raw/github/jupyterlab/{candidate.change_number}/"
        "source-manifest/attempt-1/source.json"
    )
    _write(root, issue_path, json.dumps({"body": issue_body}))
    _write(root, pull_path, json.dumps({"body": pull_body}))
    _write(root, patch_path, _patch(candidate))
    _write(
        root,
        source_manifest,
        json.dumps(
            {
                "attachment_paths": attachment_paths,
                "candidate": candidate.to_value(),
                "complete": True,
                "patch_raw_path": patch_path,
                "raw_paths": [pull_path, issue_path, patch_path],
            }
        ),
    )
    enumeration = ArtifactReference(
        "repository_enumeration/jupyterlab/current/v2",
        "repository_enumeration",
        "2.0",
    )
    write_stable_artifact(
        root,
        artifact_id=profile.source_packet_reference.artifact_id,
        artifact_kind=profile.source_packet_reference.artifact_kind,
        semantic_version="2.0",
        created_at="2026-08-13T12:00:01Z",
        inputs=(enumeration,),
        payload={
            "candidate": candidate.to_value(),
            "complete": True,
            "raw_evidence_path": source_manifest,
        },
        authorization=_authorization(),
    )
    write_stable_artifact(
        root,
        artifact_id=profile.patch_reference.artifact_id,
        artifact_kind=profile.patch_reference.artifact_kind,
        semantic_version="2.0",
        created_at="2026-08-13T12:00:02Z",
        inputs=(enumeration, profile.source_packet_reference),
        payload={
            "candidate": candidate.to_value(),
            "complete": True,
            "outcome": "patch_eligible",
            "production_paths": list(profile.production_paths),
            "test_paths": list(profile.patch_test_paths),
            "source_manifest_path": source_manifest,
            "raw_patch_path": patch_path,
        },
        authorization=_authorization(),
    )
    write_stable_artifact(
        root,
        artifact_id=profile.atomicity_reference.artifact_id,
        artifact_kind=profile.atomicity_reference.artifact_kind,
        semantic_version="2.0",
        created_at="2026-08-13T12:00:03Z",
        inputs=(
            enumeration,
            profile.source_packet_reference,
            profile.patch_reference,
            ArtifactReference(
                f"screening_checkpoint/jupyterlab/{candidate.change_number}/current/v2",
                "screening_checkpoint",
                "2.0",
            ),
        ),
        payload={
            "authoritative_issue_count": 1,
            "authoritative_issue_numbers": [profile.primary_issue_number],
            "candidate": candidate.to_value(),
            "complete": True,
            "linked_issue_count": 1,
            "linked_issue_numbers": [profile.primary_issue_number],
            "outcome": "accepted",
            "primary_issue_number": profile.primary_issue_number,
            "reason": None,
            "receipt_schema": "c1_lazarus_source_atomicity_v2",
            "source_manifest_path": source_manifest,
        },
        authorization=_authorization(),
    )
    write_stable_artifact(
        root,
        artifact_id=profile.test_manifest_reference.artifact_id,
        artifact_kind=profile.test_manifest_reference.artifact_kind,
        semantic_version="2.0",
        created_at="2026-08-13T12:00:04Z",
        inputs=(profile.patch_reference, profile.atomicity_reference),
        payload={
            "candidate": candidate.to_value(),
            "complete": True,
            "details": {
                "executable_test_paths": list(profile.patch_test_paths),
                "test_count": len(profile.patch_test_paths),
            },
            "outcome": None,
            "stage": "test_manifest",
            "state": "passed",
        },
        authorization=_authorization(),
    )


def test_1846_profile_records_exact_historical_test_and_runtime_contract() -> None:
    profile = profile_for_candidate(CandidateKey("jupyterlab", 1846))

    assert profile.primary_issue_number == 1834
    assert profile.package_version == "0.18.0-0"
    assert profile.required_versions["node"] == "5.12.0"
    assert profile.required_versions["typescript"] == "2.1.6"
    assert profile.f2p_selector == (
        "jupyter-ui removeMath() should handle `\\(` delimiters in GFM"
    )
    assert profile.p2p_selector == "jupyter-ui removeMath() should handle code spans"
    assert profile.authority_attachment_closure_required is True


def test_1846_fails_closed_before_runtime_when_screenshots_are_not_captured(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("jupyterlab", 1846)
    profile = profile_for_candidate(candidate)
    screenshot = (
        "https://cloud.githubusercontent.com/assets/2200743/example/screenshot.png"
    )
    _write_chain(
        tmp_path,
        candidate,
        issue_body=f'<img src="{screenshot}">',
        pull_body="Fixes #1834",
        attachment_paths=[],
    )

    result = plan_jupyterlab_regression_execution(tmp_path, candidate=candidate)

    assert result == StageDecision.terminal(
        "infrastructure_blocked",
        {
            "profile_id": profile.profile_id,
            "reason": "authority_attachment_closure_incomplete",
            "source_manifest_path": (
                "raw/github/jupyterlab/1846/source-manifest/attempt-1/source.json"
            ),
            "missing_attachment_urls": [screenshot],
        },
    )


def test_2128_profile_records_both_behaviors_but_does_not_merge_them_by_assertion() -> (
    None
):
    profile = profile_for_candidate(CandidateKey("jupyterlab", 2128))

    assert profile.primary_issue_number == 2106
    assert profile.package_version == "0.42.1"
    assert profile.declared_tool_constraints == {
        "node": "5",
        "mocha": "^3.2.0",
        "typescript": "^2.2.1",
    }
    assert profile.f2p_selectors == (
        "jupyter.services - Comm IComm #send() should pass through a buffers field",
        "kernel/validate validateMessage should handle no buffers field",
    )
    assert profile.p2p_selector == (
        "jupyter.services - Comm IComm #send() should send a message to the server"
    )
    assert profile.single_defect_contract_proven is False


def test_2128_fails_closed_as_two_authoritative_behaviors_before_runtime(
    tmp_path: Path,
) -> None:
    candidate = CandidateKey("jupyterlab", 2128)
    profile = profile_for_candidate(candidate)
    issue_body = """I noticed two issues with message buffers handling.

1. Any key of `buffers` is ignored; only the top-level buffers key should be ignored.
2. The `buffers` key in the message spec is optional, but mandatory in our typing.
"""
    _write_chain(
        tmp_path,
        candidate,
        issue_body=issue_body,
        pull_body="Fixes #2106.",
        attachment_paths=[],
    )

    result = plan_jupyterlab_regression_execution(tmp_path, candidate=candidate)

    assert result == StageDecision.terminal(
        "quality_rejected",
        {
            "profile_id": profile.profile_id,
            "reason": "single_defect_contract_unproven",
            "authoritative_issue_number": 2106,
            "distinct_behaviors": [
                "nested_buffers_payload_preservation",
                "optional_top_level_buffers_field",
            ],
            "production_paths": list(profile.production_paths),
            "test_paths": list(profile.patch_test_paths),
        },
    )
