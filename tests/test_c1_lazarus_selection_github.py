from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import defaultdict, deque
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.epoch import SELECTION_EPOCH_V2
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.providers.github import (
    GitHubRepository,
    GitHubRequest,
    GitHubResponse,
    _explicit_closing_issue_numbers,
    _issue_numbers,
    _request_bytes,
    _same_repository_cross_reference_issue_numbers,
    capture_github_source,
    github_repository_for,
    mark_github_issue_link_checkpoint_for_redo,
    mark_github_rate_limit_checkpoint_for_redo,
    validate_github_source_workers,
)
from warptest.c1_lazarus_selection.screening import (
    PatchDecision,
    run_screening_batch,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


class FakeTransport:
    def __init__(self, replies: dict[str, list[object]]) -> None:
        self.replies = {url: deque(values) for url, values in replies.items()}
        self.calls: defaultdict[str, int] = defaultdict(int)
        self.requests: list[GitHubRequest] = []

    def execute(self, request: GitHubRequest) -> GitHubResponse:
        self.requests.append(request)
        self.calls[request.url] += 1
        value = self.replies[request.url].popleft()
        if isinstance(value, BaseException):
            raise value
        assert type(value) is GitHubResponse
        return value


def response(
    value: object,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> GitHubResponse:
    if isinstance(value, bytes):
        body = value
    else:
        body = json.dumps(value).encode()
    return GitHubResponse(
        status=status,
        body=body,
        headers=headers or {"X-RateLimit-Limit": "60"},
    )


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-source/github-test",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _repository() -> GitHubRepository:
    return GitHubRepository(
        repository_key="jupyterlab",
        owner="jupyterlab",
        name="jupyterlab",
        attachment_hosts=("github.com", "user-images.githubusercontent.com"),
    )


def test_github_capture_persists_canonical_request_response_and_all_pages(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 101)
    repository = _repository()
    prefix = "https://api.github.com/repos/jupyterlab/jupyterlab"
    attachment_one = "https://github.com/user-attachments/assets/attachment-one"
    attachment_two = "https://user-images.githubusercontent.com/42/attachment-two.png"
    comments = [{"id": index, "body": "confirmed"} for index in range(1, 101)]
    comments.append(
        {
            "id": 101,
            "body": f'<img alt="log" src="{attachment_two}">',
        }
    )
    replies = {
        f"{prefix}/pulls/101": [
            response(
                {
                    "number": 101,
                    "title": "Fix an editor failure",
                    "body": "Fixes #7",
                    "merged": True,
                },
                headers={
                    "Authorization": "Bearer secret-token",
                    "Set-Cookie": "session=secret-token",
                    "X-RateLimit-Limit": "60",
                },
            )
        ],
        f"{prefix}/pulls/101.diff": [
            response(
                b"diff --git a/src/a.ts b/src/a.ts\n"
                b"--- a/src/a.ts\n+++ b/src/a.ts\n"
                b"@@ -1 +1 @@\n-old\n+new\n"
            )
        ],
        f"{prefix}/issues/101/timeline?per_page=100&page=1": [
            response(
                [
                    {
                        "event": "cross-referenced",
                        "source": {
                            "issue": {
                                "number": 7,
                                "repository_url": prefix,
                            }
                        },
                    }
                ]
            )
        ],
        f"{prefix}/issues/7": [
            response(
                {
                    "number": 7,
                    "title": "Editor crash",
                    "body": f"Steps and [archive]({attachment_one})",
                }
            )
        ],
        f"{prefix}/issues/7/comments?per_page=100&page=1": [response(comments[:100])],
        f"{prefix}/issues/7/comments?per_page=100&page=2": [response(comments[100:])],
        f"{prefix}/issues/7/timeline?per_page=100&page=1": [
            response([{"event": "edited", "id": 9}])
        ],
        attachment_one: [response(b"attachment one")],
        attachment_two: [response(b"attachment two")],
    }
    transport = FakeTransport(replies)

    decision = capture_github_source(
        root,
        candidate=candidate,
        repository=repository,
        transport=transport,
        authorization=_authorization(),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )

    assert decision.status == "accepted"
    manifest = json.loads((root / decision.raw_evidence_path).read_text())
    assert manifest["candidate"] == candidate.to_value()
    assert manifest["authority"] == {
        "api_base": "https://api.github.com",
        "provider": "github",
        "repository": "jupyterlab/jupyterlab",
    }
    assert manifest["issue_numbers"] == [7]
    assert len(manifest["issues"][0]["comments"]) == 101
    assert len(manifest["request_receipt_paths"]) == 9
    assert len(manifest["raw_paths"]) == 9
    assert len(manifest["attachment_paths"]) == 2
    assert (root / manifest["patch_raw_path"]).read_bytes().startswith(b"diff --git")

    receipt = json.loads(
        (root / manifest["request_receipt_paths"][0]).read_text(encoding="utf-8")
    )
    request = json.loads((root / receipt["request_raw_path"]).read_text())
    assert request == {
        "accept": "application/vnd.github+json",
        "method": "GET",
        "url": f"{prefix}/pulls/101",
    }
    assert receipt["status"] == 200
    assert (root / receipt["response_raw_path"]).is_file()
    assert all("authorization" not in value.lower() for value in receipt["headers"])

    attempt = load_stable_artifact(
        root,
        artifact_id="source_attempt/jupyterlab/101/pull/attempt-1/v2",
        artifact_kind="source_attempt",
        semantic_version="2.0",
    )
    assert attempt.payload["provider"] == "github"
    assert attempt.payload["request_receipt_path"] in manifest["request_receipt_paths"]
    assert attempt.payload["state"] == "succeeded"
    assert all(
        "secret-token" not in path.read_text(errors="ignore")
        for path in root.rglob("*")
        if path.is_file()
    )


def test_github_capture_rejects_nonclosing_title_or_plain_reference(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 102)
    prefix = "https://api.github.com/repos/jupyterlab/jupyterlab"
    related_issue = 7
    transport = FakeTransport(
        {
            f"{prefix}/pulls/102": [
                response(
                    {
                        "number": 102,
                        "title": f"Fixes #{related_issue}",
                        "body": f"Related to #{related_issue}",
                        "merged": True,
                    }
                )
            ],
            f"{prefix}/pulls/102.diff": [response(b"diff --git a/a b/a\n")],
            f"{prefix}/issues/102/timeline?per_page=100&page=1": [
                response([{"event": "commented", "body": f"See #{related_issue}"}])
            ],
        }
    )

    decision = capture_github_source(
        root,
        candidate=candidate,
        repository=_repository(),
        transport=transport,
        authorization=_authorization(),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )

    assert decision.status == "rejected"
    assert decision.reason == "authoritative_issue_link_missing"
    assert transport.calls[f"{prefix}/issues/{related_issue}"] == 0


def test_github_capture_exhaustion_records_four_backoffs_and_typed_attempts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 103)
    prefix = "https://api.github.com/repos/jupyterlab/jupyterlab"
    transport = FakeTransport(
        {
            f"{prefix}/pulls/103": [
                TimeoutError("timeout"),
                response({"message": "slow down"}, status=429),
                response({"message": "unavailable"}, status=503),
                TimeoutError("timeout"),
                TimeoutError("timeout"),
            ]
        }
    )
    sleeps: list[float] = []

    decision = capture_github_source(
        root,
        candidate=candidate,
        repository=_repository(),
        transport=transport,
        authorization=_authorization(),
        sleep=sleeps.append,
        jitter=lambda: 0.0,
    )

    assert decision.status == "blocked"
    assert decision.reason == "github_pull_capture_exhausted"
    assert sleeps == [2.0, 4.0, 8.0, 16.0]
    assert transport.calls[f"{prefix}/pulls/103"] == 5
    states = []
    for attempt_number in range(1, 6):
        artifact = load_stable_artifact(
            root,
            artifact_id=(
                f"source_attempt/jupyterlab/103/pull/attempt-{attempt_number}/v2"
            ),
            artifact_kind="source_attempt",
            semantic_version="2.0",
        )
        states.append(artifact.payload["state"])
    assert states == ["timeout", "retryable", "retryable", "timeout", "timeout"]


def test_github_request_helper_uses_all_four_bounded_backoffs(tmp_path: Path) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 104)
    url = "https://api.github.com/repos/jupyterlab/jupyterlab/pulls/104"
    transport = FakeTransport(
        {
            url: [response({"message": "slow"}, status=429) for _ in range(4)]
            + [response(b"ok")]
        }
    )
    sleeps: list[float] = []

    body, raw_path, receipt_path = _request_bytes(
        root,
        candidate=candidate,
        resource="pull",
        request=GitHubRequest(url=url, accept="application/vnd.github+json"),
        transport=transport,
        authorization=_authorization(),
        sleep=sleeps.append,
        jitter=lambda: 0.0,
    )

    assert body == b"ok"
    assert (root / raw_path).read_bytes() == b"ok"
    assert (root / receipt_path).is_file()
    assert sleeps == [2.0, 4.0, 8.0, 16.0]
    assert transport.calls[url] == 5


def test_github_core_rate_limit_403_retries_and_appends_attempts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 106)
    url = "https://api.github.com/repos/jupyterlab/jupyterlab/pulls/106"
    transport = FakeTransport(
        {
            url: [
                response(
                    {"message": "API rate limit exceeded"},
                    status=403,
                    headers={
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": "1786610546",
                    },
                ),
                response(b"ok"),
            ]
        }
    )
    sleeps: list[float] = []

    body, _raw_path, _receipt_path = _request_bytes(
        root,
        candidate=candidate,
        resource="pull",
        request=GitHubRequest(url=url, accept="application/vnd.github+json"),
        transport=transport,
        authorization=_authorization(),
        sleep=sleeps.append,
        jitter=lambda: 0.0,
    )

    assert body == b"ok"
    assert sleeps == [2.0]
    first = load_stable_artifact(
        root,
        artifact_id="source_attempt/jupyterlab/106/pull/attempt-1/v2",
        artifact_kind="source_attempt",
        semantic_version="2.0",
    )
    second = load_stable_artifact(
        root,
        artifact_id="source_attempt/jupyterlab/106/pull/attempt-2/v2",
        artifact_kind="source_attempt",
        semantic_version="2.0",
    )
    assert first.payload["state"] == "retryable"
    assert first.payload["status"] == 403
    assert second.payload["state"] == "succeeded"


def test_github_ordinary_403_is_not_retried(tmp_path: Path) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 107)
    url = "https://api.github.com/repos/jupyterlab/jupyterlab/pulls/107"
    transport = FakeTransport(
        {
            url: [
                response(
                    {"message": "forbidden"},
                    status=403,
                    headers={"X-RateLimit-Remaining": "4999"},
                )
            ]
        }
    )
    sleeps: list[float] = []

    with pytest.raises(RuntimeError, match="github_pull_capture_exhausted"):
        _request_bytes(
            root,
            candidate=candidate,
            resource="pull",
            request=GitHubRequest(url=url, accept="application/vnd.github+json"),
            transport=transport,
            authorization=_authorization(),
            sleep=sleeps.append,
            jitter=lambda: 0.0,
        )

    assert sleeps == []
    assert transport.calls[url] == 1


def test_github_recovery_starts_after_existing_attempt_head(tmp_path: Path) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 108)
    url = "https://api.github.com/repos/jupyterlab/jupyterlab/pulls/108"
    request = GitHubRequest(url=url, accept="application/vnd.github+json")
    first_transport = FakeTransport(
        {
            url: [
                response(
                    {"message": "forbidden"},
                    status=403,
                    headers={"X-RateLimit-Remaining": "4999"},
                )
            ]
        }
    )
    with pytest.raises(RuntimeError, match="github_pull_capture_exhausted"):
        _request_bytes(
            root,
            candidate=candidate,
            resource="pull",
            request=request,
            transport=first_transport,
            authorization=_authorization(),
            sleep=lambda _seconds: None,
            jitter=lambda: 0.0,
        )
    first_before = load_stable_artifact(
        root,
        artifact_id="source_attempt/jupyterlab/108/pull/attempt-1/v2",
        artifact_kind="source_attempt",
        semantic_version="2.0",
    )

    second_transport = FakeTransport({url: [response(b"recovered")]})
    body, _raw_path, _receipt_path = _request_bytes(
        root,
        candidate=candidate,
        resource="pull",
        request=request,
        transport=second_transport,
        authorization=_authorization(),
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )

    first_after = load_stable_artifact(
        root,
        artifact_id="source_attempt/jupyterlab/108/pull/attempt-1/v2",
        artifact_kind="source_attempt",
        semantic_version="2.0",
    )
    second = load_stable_artifact(
        root,
        artifact_id="source_attempt/jupyterlab/108/pull/attempt-2/v2",
        artifact_kind="source_attempt",
        semantic_version="2.0",
    )
    assert body == b"recovered"
    assert first_after == first_before
    assert second.payload["state"] == "succeeded"


def test_github_rate_limit_checkpoint_redo_requires_exact_403_receipt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 109)
    authorization = _authorization()
    enumeration = ArtifactReference(
        "repository_enumeration/jupyterlab/current/v2",
        "repository_enumeration",
        "2.0",
    )
    terminal = ArtifactReference(
        "candidate_source_terminal/jupyterlab/109/source/v2",
        "candidate_source_terminal",
        "2.0",
    )
    write_stable_artifact(
        root,
        artifact_id=enumeration.artifact_id,
        artifact_kind=enumeration.artifact_kind,
        semantic_version=enumeration.semantic_version,
        created_at="2026-08-13T00:00:00Z",
        inputs=(),
        payload={
            "repository_key": "jupyterlab",
            "candidates": [candidate.to_value()],
            "complete": True,
        },
        authorization=authorization,
    )
    write_stable_artifact(
        root,
        artifact_id=terminal.artifact_id,
        artifact_kind=terminal.artifact_kind,
        semantic_version=terminal.semantic_version,
        created_at="2026-08-13T00:00:01Z",
        inputs=(enumeration,),
        payload={
            "candidate": candidate.to_value(),
            "outcome": "infrastructure_blocked",
            "reason": "github_pull_capture_exhausted",
            "complete": True,
        },
        authorization=authorization,
    )
    write_stable_artifact(
        root,
        artifact_id="screening_checkpoint/jupyterlab/109/current/v2",
        artifact_kind="screening_checkpoint",
        semantic_version="2.0",
        created_at="2026-08-13T00:00:01Z",
        inputs=(enumeration, terminal),
        payload={
            "candidate": candidate.to_value(),
            "state": "complete",
            "outcome": "infrastructure_blocked",
            "stage_artifact_id": terminal.artifact_id,
        },
        authorization=authorization,
    )
    receipt_relative = Path(
        "raw/github/jupyterlab/109/pull/attempt-1/rate-limit.receipt.json"
    )
    receipt = root / receipt_relative
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        json.dumps(
            {
                "status": 403,
                "headers": {
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "1786610546",
                },
            }
        )
    )
    write_stable_artifact(
        root,
        artifact_id="source_attempt/jupyterlab/109/pull/attempt-1/v2",
        artifact_kind="source_attempt",
        semantic_version="2.0",
        created_at="2026-08-13T00:00:01Z",
        inputs=(),
        payload={
            "candidate": candidate.to_value(),
            "provider": "github",
            "resource": "pull",
            "attempt": 1,
            "canonical_url": (
                "https://api.github.com/repos/jupyterlab/jupyterlab/pulls/109"
            ),
            "state": "failed",
            "status": 403,
            "raw_evidence_path": None,
            "request_receipt_path": receipt_relative.as_posix(),
            "failure_type": None,
        },
        authorization=authorization,
    )

    redo = mark_github_rate_limit_checkpoint_for_redo(
        root,
        candidate=candidate,
        authorization=authorization,
    )

    assert redo.inputs == (enumeration, terminal)
    assert redo.payload == {
        "candidate": candidate.to_value(),
        "state": "redo_required",
        "prior_outcome": "infrastructure_blocked",
        "stage_artifact_id": terminal.artifact_id,
        "reason": "github_core_rate_limit_reset",
    }


def test_github_rate_limit_checkpoint_redo_rejects_unproven_403(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 110)
    authorization = _authorization()
    enumeration = ArtifactReference(
        "repository_enumeration/jupyterlab/current/v2",
        "repository_enumeration",
        "2.0",
    )
    terminal = ArtifactReference(
        "candidate_source_terminal/jupyterlab/110/source/v2",
        "candidate_source_terminal",
        "2.0",
    )
    for reference, created_at, inputs, payload in (
        (
            enumeration,
            "2026-08-13T00:00:00Z",
            (),
            {
                "repository_key": "jupyterlab",
                "candidates": [candidate.to_value()],
                "complete": True,
            },
        ),
        (
            terminal,
            "2026-08-13T00:00:01Z",
            (enumeration,),
            {
                "candidate": candidate.to_value(),
                "outcome": "infrastructure_blocked",
                "reason": "github_pull_capture_exhausted",
                "complete": True,
            },
        ),
    ):
        write_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
            created_at=created_at,
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
    write_stable_artifact(
        root,
        artifact_id="screening_checkpoint/jupyterlab/110/current/v2",
        artifact_kind="screening_checkpoint",
        semantic_version="2.0",
        created_at="2026-08-13T00:00:01Z",
        inputs=(enumeration, terminal),
        payload={
            "candidate": candidate.to_value(),
            "state": "complete",
            "outcome": "infrastructure_blocked",
            "stage_artifact_id": terminal.artifact_id,
        },
        authorization=authorization,
    )
    receipt_relative = Path(
        "raw/github/jupyterlab/110/pull/attempt-1/forbidden.receipt.json"
    )
    receipt = root / receipt_relative
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        json.dumps({"status": 403, "headers": {"X-RateLimit-Remaining": "4999"}})
    )
    write_stable_artifact(
        root,
        artifact_id="source_attempt/jupyterlab/110/pull/attempt-1/v2",
        artifact_kind="source_attempt",
        semantic_version="2.0",
        created_at="2026-08-13T00:00:01Z",
        inputs=(),
        payload={
            "candidate": candidate.to_value(),
            "provider": "github",
            "resource": "pull",
            "attempt": 1,
            "canonical_url": (
                "https://api.github.com/repos/jupyterlab/jupyterlab/pulls/110"
            ),
            "state": "failed",
            "status": 403,
            "raw_evidence_path": None,
            "request_receipt_path": receipt_relative.as_posix(),
            "failure_type": None,
        },
        authorization=authorization,
    )

    with pytest.raises(RuntimeError, match="exhaustion is not proven"):
        mark_github_rate_limit_checkpoint_for_redo(
            root,
            candidate=candidate,
            authorization=authorization,
        )


def test_github_configs_are_public_authority_only_and_cover_three_repositories() -> (
    None
):
    assert github_repository_for("luanti").owner == "luanti-org"
    assert github_repository_for("jupyterlab").name == "jupyterlab"
    assert github_repository_for("openra").owner == "OpenRA"
    with pytest.raises(ValueError, match="GitHub API authority"):
        GitHubRepository(
            repository_key="jupyterlab",
            owner="jupyterlab",
            name="jupyterlab",
            attachment_hosts=("github.com",),
            api_base="https://projects.blender.org/api/v1",
        )
    assert validate_github_source_workers(8) == 8
    with pytest.raises(ValueError, match="between 1 and 8"):
        validate_github_source_workers(9)


def test_github_issue_links_require_a_closing_clause_or_same_repository_crossref() -> (
    None
):
    repository = _repository()
    assert _issue_numbers(
        {"body": "Fixes #7, #8 and #9. Related to #10."},
        [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 11,
                        "repository_url": repository.api_repository_url,
                    }
                },
            },
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 12,
                        "repository_url": "https://api.github.com/repos/other/repo",
                    }
                },
            },
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 13,
                        "repository_url": repository.api_repository_url,
                        "pull_request": {},
                    }
                },
            },
        ],
        repository,
    ) == (7, 8, 9, 11)

    luanti = github_repository_for("luanti")
    assert _issue_numbers(
        {
            "body": (
                "Fixes https://github.com/minetest/minetest/issues/12870. "
                "Related to https://github.com/other/repo/issues/9."
            )
        },
        [],
        luanti,
    ) == (12870,)


def test_github_issue_link_projection_separates_closing_from_crossrefs() -> None:
    repository = _repository()
    pull = {
        "body": (
            "Fixes #11351. Resolves "
            "https://github.com/jupyterlab/jupyterlab/issues/11353."
        )
    }
    relationships = [
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": 11834,
                    "repository_url": repository.api_repository_url,
                }
            },
        }
    ]

    assert _explicit_closing_issue_numbers(pull, repository) == (11351, 11353)
    assert _same_repository_cross_reference_issue_numbers(
        relationships,
        repository,
    ) == (11834,)
    assert _issue_numbers(pull, relationships, repository) == (11351, 11353, 11834)


def test_github_issue_link_redo_requires_retained_allowed_closing_url(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("luanti", 12984)
    authorization = _authorization()
    enumeration = ArtifactReference(
        "repository_enumeration/luanti/current/v2",
        "repository_enumeration",
        "2.0",
    )
    terminal = ArtifactReference(
        "candidate_source_terminal/luanti/12984/source/v2",
        "candidate_source_terminal",
        "2.0",
    )
    write_stable_artifact(
        root,
        artifact_id=enumeration.artifact_id,
        artifact_kind=enumeration.artifact_kind,
        semantic_version=enumeration.semantic_version,
        created_at="2026-08-13T00:00:00Z",
        inputs=(),
        payload={
            "repository_key": "luanti",
            "candidates": [candidate.to_value()],
            "complete": True,
        },
        authorization=authorization,
    )
    write_stable_artifact(
        root,
        artifact_id=terminal.artifact_id,
        artifact_kind=terminal.artifact_kind,
        semantic_version=terminal.semantic_version,
        created_at="2026-08-13T00:00:01Z",
        inputs=(enumeration,),
        payload={
            "candidate": candidate.to_value(),
            "outcome": "source_rejected",
            "reason": "authoritative_issue_link_missing",
            "complete": True,
        },
        authorization=authorization,
    )
    checkpoint_id = "screening_checkpoint/luanti/12984/current/v2"
    write_stable_artifact(
        root,
        artifact_id=checkpoint_id,
        artifact_kind="screening_checkpoint",
        semantic_version="2.0",
        created_at="2026-08-13T00:00:01Z",
        inputs=(enumeration, terminal),
        payload={
            "candidate": candidate.to_value(),
            "state": "complete",
            "outcome": "source_rejected",
            "stage_artifact_id": terminal.artifact_id,
        },
        authorization=authorization,
    )
    raw_path = Path("raw/github/luanti/12984/pull/attempt-1/pull.response.body")
    (root / raw_path).parent.mkdir(parents=True)
    (root / raw_path).write_text(
        json.dumps(
            {
                "number": 12984,
                "body": ("Fixes https://github.com/minetest/minetest/issues/12870."),
                "merged": True,
            }
        )
    )
    write_stable_artifact(
        root,
        artifact_id="source_attempt/luanti/12984/pull/attempt-1/v2",
        artifact_kind="source_attempt",
        semantic_version="2.0",
        created_at="2026-08-13T00:00:01Z",
        inputs=(),
        payload={
            "candidate": candidate.to_value(),
            "provider": "github",
            "resource": "pull",
            "attempt": 1,
            "canonical_url": (
                "https://api.github.com/repos/luanti-org/luanti/pulls/12984"
            ),
            "state": "succeeded",
            "status": 200,
            "raw_evidence_path": raw_path.as_posix(),
            "request_receipt_path": (
                "raw/github/luanti/12984/pull/attempt-1/pull.receipt.json"
            ),
            "failure_type": None,
        },
        authorization=authorization,
    )

    redo = mark_github_issue_link_checkpoint_for_redo(
        root,
        candidate=candidate,
        authorization=authorization,
    )

    assert redo.inputs == (enumeration, terminal)
    assert redo.payload == {
        "candidate": candidate.to_value(),
        "state": "redo_required",
        "prior_outcome": "source_rejected",
        "stage_artifact_id": terminal.artifact_id,
        "reason": "github_closing_issue_url_parser_updated",
    }


def test_screening_cli_rejects_nine_github_source_workers(tmp_path: Path) -> None:
    workspace = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": str(workspace)}
    completed = subprocess.run(
        (
            sys.executable,
            str(workspace / "scripts/run_c1_lazarus_selection_screening.py"),
            "--artifact-root",
            str(tmp_path / "selection"),
            "--repository",
            "jupyterlab",
            "--authorization-id",
            "local-source/github-cli-worker-limit",
            "--execute",
            "--source-workers",
            "9",
        ),
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert completed.returncode != 0
    assert "GitHub source workers must be between 1 and 8" in completed.stderr
    assert not (tmp_path / "selection").exists()


def test_github_source_manifest_binds_to_v2_screening_packet(tmp_path: Path) -> None:
    root = tmp_path / "selection"
    authorization = _authorization()
    candidate = CandidateKey("jupyterlab", 105)
    prefix = "https://api.github.com/repos/jupyterlab/jupyterlab"
    write_stable_artifact(
        root,
        artifact_id="repository_enumeration/jupyterlab/current/v2",
        artifact_kind="repository_enumeration",
        semantic_version="2.0",
        created_at="2026-08-13T00:00:00Z",
        inputs=(),
        payload={
            "repository_key": "jupyterlab",
            "candidates": [candidate.to_value()],
            "complete": True,
        },
        authorization=authorization,
    )
    transport = FakeTransport(
        {
            f"{prefix}/pulls/105": [
                response({"number": 105, "body": "Closes #7", "merged": True})
            ],
            f"{prefix}/pulls/105.diff": [response(b"diff --git a/a b/a\n")],
            f"{prefix}/issues/105/timeline?per_page=100&page=1": [response([])],
            f"{prefix}/issues/7": [response({"number": 7, "body": "report"})],
            f"{prefix}/issues/7/comments?per_page=100&page=1": [response([])],
            f"{prefix}/issues/7/timeline?per_page=100&page=1": [response([])],
        }
    )

    result = run_screening_batch(
        root,
        repository_key="jupyterlab",
        source_capture=lambda value: capture_github_source(
            root,
            candidate=value,
            repository=_repository(),
            transport=transport,
            authorization=authorization,
            sleep=lambda _seconds: None,
            jitter=lambda: 0.0,
        ),
        patch_capture=lambda _candidate, _source: PatchDecision.rejected(
            "no_regression_test"
        ),
        authorization=authorization,
        selection_epoch=SELECTION_EPOCH_V2,
        source_workers=1,
        patch_workers=1,
        persistence_workers=1,
        reload_workers=1,
        queue_limit=1,
    )

    assert result.patch_rejected == 1
    source_packet = load_stable_artifact(
        root,
        artifact_id="candidate_source_packet/jupyterlab/105/source/v2",
        artifact_kind="candidate_source_packet",
        semantic_version="2.0",
    )
    assert source_packet.payload["complete"] is True
    manifest = json.loads(
        (root / source_packet.payload["raw_evidence_path"]).read_text()
    )
    assert manifest["provider"] == "github"
    assert manifest["complete"] is True
