"""Focused v2 P3 source-closure tests.

These fixtures intentionally use only local v2 artifacts and raw captures.  No
provider client, network request, build, or model call is involved.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.epoch import SELECTION_EPOCH_V2
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.p3_source_closure import (
    P3SourceClosureError,
    load_v2_p3_source_closure,
    write_v2_p3_source_closure,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


_CREATED_AT = "2026-08-13T00:00:00Z"


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="pytest/p3-source-closure",
        scope="c1-p3",
        granted_at=_CREATED_AT,
        local_user_id=os.getuid(),
    )


def _write_artifact(
    root: Path,
    reference: ArtifactReference,
    *,
    payload: dict[str, object],
    inputs: tuple[ArtifactReference, ...],
    created_at: str = _CREATED_AT,
) -> None:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=created_at,
        inputs=inputs,
        payload=payload,
        authorization=_authorization(),
    )


def _write_raw(root: Path, relative_path: str, payload: object) -> str:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return relative_path


def _issue(
    number: int, *, title: str | None = None, body: str | None = None
) -> dict[str, object]:
    return {
        "number": number,
        "issue": {
            "number": number,
            "title": title or "Selection state is not retained",
            "body": body
            or "Choosing an item should remain reliable after reopening the menu.",
            "html_url": f"https://github.example/luanti-org/luanti/issues/{number}",
            "created_at": "2024-01-02T03:04:05Z",
        },
        "comments": [],
        "history": [],
    }


def test_reverse_guard_does_not_confuse_workaround_with_raw_path() -> None:
    """Mechanism paths are literal; punctuation must not be normalized away."""

    from warptest.issue_naturalize import TokenGuard

    guard = TokenGuard({"target": {"coordinator_only": []}})
    safe = guard.check_reverse(
        "A practical workaround keeps the task usable.",
        extra_forbidden=("raw/",),
    )
    leaked = guard.check_reverse(
        "Read raw/private/source.json for the answer.",
        extra_forbidden=("raw/",),
    )

    assert safe.ok is True
    assert leaked.ok is False
    assert leaked.forbidden == ["raw/"]


def _seed_v2_candidate(
    root: Path,
    *,
    issues: list[dict[str, object]] | None = None,
    include_pull: bool = True,
) -> CandidateKey:
    """Write the smallest valid local v2 source/patch/checkpoint chain."""

    candidate = CandidateKey(repository_key="luanti", change_number=10)
    enumeration_reference = SELECTION_EPOCH_V2.enumeration_reference(
        candidate.repository_key
    )
    _write_artifact(
        root,
        enumeration_reference,
        payload={
            "repository_key": candidate.repository_key,
            "candidates": [candidate.to_value()],
            "complete": True,
        },
        inputs=(),
    )

    raw_prefix = "raw/github/luanti/10"
    pull_response = _write_raw(root, f"{raw_prefix}/pull/response.json", {"ok": True})
    issue_response = _write_raw(root, f"{raw_prefix}/issue/response.json", {"ok": True})
    patch_response = _write_raw(root, f"{raw_prefix}/patch/response.diff", {"ok": True})
    receipt_paths: list[str] = []
    for resource, response_path in (
        ("pull", pull_response),
        ("issue", issue_response),
        ("patch", patch_response),
    ):
        request_path = _write_raw(
            root,
            f"{raw_prefix}/{resource}/request.json",
            {"method": "GET", "url": f"https://api.github.example/{resource}"},
        )
        receipt_paths.append(
            _write_raw(
                root,
                f"{raw_prefix}/{resource}/receipt.json",
                {
                    "provider": "github",
                    "canonical_url": f"https://api.github.example/{resource}",
                    "request_raw_path": request_path,
                    "response_raw_path": response_path,
                    "status": 200,
                    "failure_type": None,
                },
            )
        )

    selected_issues = issues if issues is not None else [_issue(9)]
    manifest: dict[str, object] = {
        "candidate": candidate.to_value(),
        "provider": "github",
        "authority": {
            "provider": "github",
            "api_base": "https://api.github.example",
            "repository": "luanti-org/luanti",
        },
        "relationships": [],
        "issue_numbers": [entry["number"] for entry in selected_issues],
        "issues": selected_issues,
        "raw_paths": [pull_response, issue_response, patch_response],
        "request_receipt_paths": receipt_paths,
        "patch_raw_path": patch_response,
        "attachment_paths": [],
        "complete": True,
    }
    if include_pull:
        manifest["pull"] = {
            "number": candidate.change_number,
            "merged": True,
            "title": "Keep menu selection stable",
            "body": "",
            "html_url": "https://github.example/luanti-org/luanti/pull/10",
            "created_at": "2024-01-03T03:04:05Z",
        }
    manifest_path = _write_raw(
        root, f"{raw_prefix}/source-manifest/manifest.json", manifest
    )

    source_reference = ArtifactReference(
        artifact_id=f"candidate_source_packet/{candidate.repository_key}/{candidate.change_number}/source/v2",
        artifact_kind="candidate_source_packet",
        semantic_version="2.0",
    )
    _write_artifact(
        root,
        source_reference,
        payload={
            "candidate": candidate.to_value(),
            "raw_evidence_path": manifest_path,
            "complete": True,
        },
        inputs=(enumeration_reference,),
    )
    patch_reference = ArtifactReference(
        artifact_id=f"candidate_test_patch/{candidate.repository_key}/{candidate.change_number}/screening/v2",
        artifact_kind="candidate_test_patch",
        semantic_version="2.0",
    )
    _write_artifact(
        root,
        patch_reference,
        payload={
            "candidate": candidate.to_value(),
            "outcome": "patch_eligible",
            "production_paths": ["src/engine/selection.cpp"],
            "test_paths": ["src/unittest/test_selection.cpp"],
            "raw_patch_path": patch_response,
            "source_manifest_path": manifest_path,
            "complete": True,
        },
        inputs=(enumeration_reference, source_reference),
    )
    checkpoint_reference = ArtifactReference(
        artifact_id=f"screening_checkpoint/{candidate.repository_key}/{candidate.change_number}/current/v2",
        artifact_kind="screening_checkpoint",
        semantic_version="2.0",
    )
    _write_artifact(
        root,
        checkpoint_reference,
        payload={
            "candidate": candidate.to_value(),
            "state": "complete",
            "outcome": "patch_eligible",
            "stage_artifact_id": patch_reference.artifact_id,
        },
        inputs=(enumeration_reference, patch_reference),
    )
    return candidate


def _write_closure(root: Path, candidate: CandidateKey, **kwargs: object):
    return write_v2_p3_source_closure(
        root,
        candidate=candidate,
        exact_text_readily_searchable=True,
        coordinator_only_tokens=("answer-token",),
        authorization=_authorization(),
        created_at=_CREATED_AT,
        **kwargs,
    )


def test_writes_and_reloads_v2_source_closure_with_stable_same_root_refs(
    tmp_path: Path,
) -> None:
    candidate = _seed_v2_candidate(
        tmp_path,
        issues=[
            _issue(
                9,
                body=(
                    "Choosing an item should remain reliable after reopening the menu. "
                    "The investigation mentions src/engine/selection.cpp and "
                    "test_selection.cpp."
                ),
            )
        ],
    )

    closure = _write_closure(tmp_path, candidate)
    reloaded = load_v2_p3_source_closure(tmp_path, candidate=candidate)

    assert reloaded == closure
    assert (
        closure.public_task.reference.artifact_id == "public_task/luanti/10/current/v2"
    )
    assert (
        closure.contamination.reference.artifact_id
        == "contamination_record/luanti/10/current/v2"
    )
    assert closure.public_task.inputs == (
        ArtifactReference(
            "candidate_source_packet/luanti/10/source/v2",
            "candidate_source_packet",
            "2.0",
        ),
        ArtifactReference(
            "candidate_test_patch/luanti/10/screening/v2",
            "candidate_test_patch",
            "2.0",
        ),
    )
    assert closure.contamination.inputs == (
        closure.public_task.inputs[0],
        closure.public_task.inputs[1],
        closure.public_task.reference,
    )
    assert (
        closure.public_task.payload["primary_issue"]["selection"]
        == "sole_authoritative_issue"
    )
    assert "src/engine/selection.cpp" not in closure.public_task.payload["task_text"]
    assert "test_selection.cpp" not in closure.public_task.payload["task_text"]
    assert "source_text" not in closure.public_task.payload
    assert closure.public_task.payload["token_guard"]["status"] == "passed"
    assert closure.contamination.payload["exact_text_readily_searchable"] is True


def test_ambiguous_authoritative_issues_require_explicit_primary_selection(
    tmp_path: Path,
) -> None:
    candidate = _seed_v2_candidate(tmp_path, issues=[_issue(8), _issue(9)])

    with pytest.raises(P3SourceClosureError, match="ambiguous"):
        _write_closure(tmp_path, candidate)

    closure = _write_closure(
        tmp_path,
        candidate,
        primary_issue_number=9,
        primary_issue_rationale="The reported user-visible symptom matches the selected task scope.",
    )
    primary = closure.public_task.payload["primary_issue"]
    assert primary["number"] == 9
    assert primary["selection"] == "explicit_primary_issue"
    assert (
        primary["rationale"]
        == "The reported user-visible symptom matches the selected task scope"
    )


@pytest.mark.parametrize(
    ("issues", "include_pull", "expected"),
    [
        ([], True, "authoritative issue is missing"),
        ([_issue(9)], False, "pull metadata is missing"),
    ],
)
def test_pr_only_or_missing_pull_source_fails_closed(
    tmp_path: Path,
    issues: list[dict[str, object]],
    include_pull: bool,
    expected: str,
) -> None:
    candidate = _seed_v2_candidate(tmp_path, issues=issues, include_pull=include_pull)

    with pytest.raises(P3SourceClosureError, match=expected):
        _write_closure(tmp_path, candidate)


def test_reverse_token_guard_rejects_coordinator_only_answer_token(
    tmp_path: Path,
) -> None:
    candidate = _seed_v2_candidate(
        tmp_path,
        issues=[
            _issue(
                9,
                body="The visible behavior must not reveal answer-token to task solvers.",
            )
        ],
    )

    with pytest.raises(P3SourceClosureError, match="TokenGuard"):
        _write_closure(tmp_path, candidate)


def test_pr_object_cannot_be_used_as_an_authoritative_issue(tmp_path: Path) -> None:
    pr_like_issue = _issue(9)
    pr_like_issue["issue"]["pull_request"] = {"url": "https://github.example/pulls/9"}
    candidate = _seed_v2_candidate(tmp_path, issues=[pr_like_issue])

    with pytest.raises(P3SourceClosureError, match="resolves to a pull request"):
        _write_closure(tmp_path, candidate)


def test_source_manifest_symlink_fails_closed(tmp_path: Path) -> None:
    candidate = _seed_v2_candidate(tmp_path)
    source_reference = ArtifactReference(
        artifact_id="candidate_source_packet/luanti/10/source/v2",
        artifact_kind="candidate_source_packet",
        semantic_version="2.0",
    )
    symlink_path = tmp_path / "raw" / "unsafe-source-manifest.json"
    symlink_path.symlink_to(
        tmp_path / "raw/github/luanti/10/source-manifest/manifest.json"
    )
    _write_artifact(
        tmp_path,
        source_reference,
        payload={
            "candidate": candidate.to_value(),
            "raw_evidence_path": "raw/unsafe-source-manifest.json",
            "complete": True,
        },
        inputs=(SELECTION_EPOCH_V2.enumeration_reference("luanti"),),
        created_at="2026-08-13T00:00:01Z",
    )

    with pytest.raises(
        P3SourceClosureError,
        match="source manifest path contains a symlink",
    ):
        _write_closure(tmp_path, candidate)


def test_loader_rejects_wrong_same_root_dependency_chain(tmp_path: Path) -> None:
    candidate = _seed_v2_candidate(tmp_path)
    _write_closure(tmp_path, candidate)
    public_reference = ArtifactReference(
        artifact_id="public_task/luanti/10/current/v2",
        artifact_kind="public_task",
        semantic_version="2.0",
    )
    _write_artifact(
        tmp_path,
        public_reference,
        payload={
            "candidate": candidate.to_value(),
            "primary_issue": {"number": 9, "selection": "sole_authoritative_issue"},
            "task_title": "Selection state is not retained",
            "task_text": "Selection state is not retained",
            "token_guard": {"status": "passed"},
            "complete": True,
        },
        inputs=(
            ArtifactReference(
                "candidate_source_packet/luanti/10/source/v2",
                "candidate_source_packet",
                "2.0",
            ),
        ),
        created_at="2026-08-13T00:00:01Z",
    )

    with pytest.raises(P3SourceClosureError, match="public task inputs"):
        load_v2_p3_source_closure(tmp_path, candidate=candidate)
