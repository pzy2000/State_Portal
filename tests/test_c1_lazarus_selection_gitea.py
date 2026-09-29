from __future__ import annotations

import json
import os
from http.client import IncompleteRead
from collections import defaultdict, deque
from pathlib import Path

from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.providers.gitea import (
    GiteaRepository,
    GiteaResponse,
    _issue_numbers,
    _write_raw,
    capture_gitea_source,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    load_stable_artifact,
)


class FakeTransport:
    def __init__(self, replies: dict[str, list[object]]) -> None:
        self.replies = {url: deque(values) for url, values in replies.items()}
        self.calls: defaultdict[str, int] = defaultdict(int)

    def execute(self, url: str) -> GiteaResponse:
        self.calls[url] += 1
        value = self.replies[url].popleft()
        if isinstance(value, BaseException):
            raise value
        assert type(value) is GiteaResponse
        return value


def response(value: object, *, status: int = 200) -> GiteaResponse:
    if isinstance(value, bytes):
        body = value
    else:
        body = json.dumps(value).encode()
    return GiteaResponse(status=status, body=body, headers={})


def test_gitea_capture_retries_and_persists_complete_relationship_pages(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    repository = GiteaRepository(
        repository_key="blender",
        api_base="https://projects.blender.org/api/v1",
        owner="blender",
        name="blender",
        attachment_hosts=("projects.blender.org",),
    )
    prefix = "https://projects.blender.org/api/v1/repos/blender/blender"
    attachment = "https://projects.blender.org/attachments/report.txt"
    replies = {
        f"{prefix}/pulls/123": [
            IncompleteRead(b"partial", 10),
            response(
                {
                    "number": 123,
                    "title": "Fix crash",
                    "body": "Closes #7",
                    "merged": True,
                    "merged_at": "2026-08-01T00:00:00Z",
                    "diff_url": f"{prefix}/pulls/123.diff",
                }
            ),
        ],
        f"{prefix}/pulls/123.diff": [
            response(
                b"diff --git a/source/a.cc b/source/a.cc\n"
                b"--- a/source/a.cc\n+++ b/source/a.cc\n"
                b"@@ -1 +1 @@\n-old\n+new\n"
            )
        ],
        f"{prefix}/issues/123/timeline?page=1&limit=100": [
            response(
                [
                    {
                        "type": "cross_reference",
                        "source": {"issue": {"number": 7}},
                    }
                ]
            )
        ],
        f"{prefix}/issues/7": [
            response(
                {
                    "number": 7,
                    "title": "Crash report",
                    "body": (
                        f"Steps and log [report]({attachment}); "
                        f"related [pull]({prefix}/pulls/99)"
                    ),
                    "created_at": "2026-07-01T00:00:00Z",
                    "updated_at": "2026-07-02T00:00:00Z",
                }
            )
        ],
        f"{prefix}/issues/7/comments?page=1&limit=100": [
            response(
                [
                    {
                        "id": 9,
                        "body": "Confirmed",
                        "created_at": "2026-07-01T01:00:00Z",
                        "updated_at": "2026-07-01T01:00:00Z",
                    }
                ]
            )
        ],
        f"{prefix}/issues/7/timeline?page=1&limit=100": [
            response([{"type": "comment", "id": 9}])
        ],
        attachment: [response(b"raw attachment")],
    }
    sleeps: list[float] = []
    authorization = AuthorizationRecord(
        authorization_id="local-source/blender-run-1",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )

    decision = capture_gitea_source(
        root,
        candidate=CandidateKey("blender", 123),
        repository=repository,
        transport=FakeTransport(replies),
        authorization=authorization,
        sleep=sleeps.append,
        jitter=lambda: 0.0,
    )

    assert decision.status == "accepted"
    assert sleeps == [2.0]
    manifest = json.loads((root / decision.raw_evidence_path).read_text())
    assert manifest["candidate"] == {
        "repository_key": "blender",
        "change_number": 123,
    }
    assert manifest["issue_numbers"] == [7]
    assert len(manifest["raw_paths"]) == 7
    assert (root / manifest["patch_raw_path"]).is_file()
    assert (root / manifest["attachment_paths"][0]).read_bytes() == b"raw attachment"
    assert len(manifest["attachment_paths"]) == 1

    timeout = load_stable_artifact(
        root,
        artifact_id="source_attempt/blender/123/pull/attempt-1/v1",
        artifact_kind="source_attempt",
        semantic_version="1.0",
    )
    assert timeout.payload["state"] == "failed"
    success = load_stable_artifact(
        root,
        artifact_id="source_attempt/blender/123/pull/attempt-2/v1",
        artifact_kind="source_attempt",
        semantic_version="1.0",
    )
    assert success.payload["state"] == "succeeded"
    assert (root / success.payload["raw_evidence_path"]).is_file()


def test_gitea_capture_exhaustion_returns_typed_infrastructure_blocker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    repository = GiteaRepository(
        repository_key="blender",
        api_base="https://projects.blender.org/api/v1",
        owner="blender",
        name="blender",
        attachment_hosts=("projects.blender.org",),
    )
    url = "https://projects.blender.org/api/v1/repos/blender/blender/pulls/124"
    transport = FakeTransport({url: [TimeoutError("timeout") for _index in range(4)]})
    authorization = AuthorizationRecord(
        authorization_id="local-source/blender-run-2",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )
    sleeps: list[float] = []

    decision = capture_gitea_source(
        root,
        candidate=CandidateKey("blender", 124),
        repository=repository,
        transport=transport,
        authorization=authorization,
        sleep=sleeps.append,
        jitter=lambda: 0.0,
    )

    assert decision.status == "blocked"
    assert decision.reason == "gitea_pull_capture_exhausted"
    assert sleeps == [2.0, 4.0, 8.0]
    assert transport.calls[url] == 4
    for attempt in range(1, 5):
        artifact = load_stable_artifact(
            root,
            artifact_id=(f"source_attempt/blender/124/pull/attempt-{attempt}/v1"),
            artifact_kind="source_attempt",
            semantic_version="1.0",
        )
        assert artifact.payload["state"] == "timeout"


def test_gitea_raw_attempt_keeps_created_at_revisions(tmp_path: Path) -> None:
    root = tmp_path / "selection"
    root.mkdir()
    candidate = CandidateKey("blender", 125)

    first = _write_raw(
        root,
        candidate,
        "pull",
        1,
        b"first",
        extension="body",
        created_at="2026-08-13T00:00:00.000001Z",
    )
    second = _write_raw(
        root,
        candidate,
        "pull",
        1,
        b"second",
        extension="body",
        created_at="2026-08-13T00:00:00.000002Z",
    )

    assert first != second
    assert (root / first).read_bytes() == b"first"
    assert (root / second).read_bytes() == b"second"


def test_gitea_capture_accepts_title_closing_ref_and_comment_ref(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    repository = GiteaRepository(
        repository_key="blender",
        api_base="https://projects.blender.org/api/v1",
        owner="blender",
        name="blender",
        attachment_hosts=("projects.blender.org",),
    )
    candidate = CandidateKey("blender", 156442)
    issue_number = 156441
    prefix = "https://projects.blender.org/api/v1/repos/blender/blender"
    replies = {
        f"{prefix}/pulls/{candidate.change_number}": [
            response(
                {
                    "number": candidate.change_number,
                    "title": f"Fixes #{issue_number}: crash while opening a file",
                    "body": "",
                    "diff_url": f"{prefix}/pulls/{candidate.change_number}.diff",
                }
            )
        ],
        f"{prefix}/pulls/{candidate.change_number}.diff": [
            response(b"diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n")
        ],
        f"{prefix}/issues/{candidate.change_number}/timeline?page=1&limit=100": [
            response(
                [
                    {
                        "type": "comment_ref",
                        "ref_issue": {"number": issue_number},
                        "ref_comment": {"id": 42},
                    }
                ]
            )
        ],
        f"{prefix}/issues/{issue_number}": [
            response({"number": issue_number, "title": "Crash report", "body": ""})
        ],
        f"{prefix}/issues/{issue_number}/comments?page=1&limit=100": [response([])],
        f"{prefix}/issues/{issue_number}/timeline?page=1&limit=100": [response([])],
    }
    authorization = AuthorizationRecord(
        authorization_id="local-source/blender-run-title-comment-ref",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )

    decision = capture_gitea_source(
        root,
        candidate=candidate,
        repository=repository,
        transport=FakeTransport(replies),
        authorization=authorization,
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )

    assert decision.status == "accepted"
    manifest = json.loads((root / decision.raw_evidence_path).read_text())
    assert manifest["issue_numbers"] == [issue_number]
    assert manifest["relationships"] == [
        {
            "type": "comment_ref",
            "ref_issue": {"number": issue_number},
            "ref_comment": {"id": 42},
        }
    ]


def test_gitea_capture_rejects_absent_authoritative_relation(tmp_path: Path) -> None:
    root = tmp_path / "selection"
    repository = GiteaRepository(
        repository_key="blender",
        api_base="https://projects.blender.org/api/v1",
        owner="blender",
        name="blender",
        attachment_hosts=("projects.blender.org",),
    )
    candidate = CandidateKey("blender", 156443)
    prefix = "https://projects.blender.org/api/v1/repos/blender/blender"
    related_issue_number = 156441
    replies = {
        f"{prefix}/pulls/{candidate.change_number}": [
            response(
                {
                    "number": candidate.change_number,
                    "title": f"Investigate #{related_issue_number}",
                    "body": f"Related to #{related_issue_number}",
                    "diff_url": f"{prefix}/pulls/{candidate.change_number}.diff",
                }
            )
        ],
        f"{prefix}/pulls/{candidate.change_number}.diff": [
            response(b"diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n")
        ],
        f"{prefix}/issues/{candidate.change_number}/timeline?page=1&limit=100": [
            response([{"type": "comment", "body": f"See #{related_issue_number}"}])
        ],
    }
    transport = FakeTransport(replies)
    authorization = AuthorizationRecord(
        authorization_id="local-source/blender-run-no-relation",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )

    decision = capture_gitea_source(
        root,
        candidate=candidate,
        repository=repository,
        transport=transport,
        authorization=authorization,
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )

    assert decision.status == "rejected"
    assert decision.reason == "authoritative_issue_link_missing"
    assert transport.calls[f"{prefix}/issues/{related_issue_number}"] == 0


def test_gitea_issue_numbers_accepts_closing_title_and_comment_refs() -> None:
    assert _issue_numbers(
        {"title": "Fixes #156441", "body": "See #156440"},
        [
            {"type": "comment_ref", "ref_issue": {"number": 156442}},
            {"type": "comment_ref", "ref_issue": {"number": "156443"}},
            {"type": "comment", "ref_issue": {"number": 156444}},
        ],
    ) == (156441, 156442)
