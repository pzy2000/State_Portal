from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.enumeration import (
    RepositoryEnumeration,
    recompute_repository_enumeration_from_gitea_captures,
    recompute_repository_enumeration_from_github_captures,
)
from warptest.c1_lazarus_selection.models import CandidateKey


def test_recompute_uses_merge_revision_not_pr_head_and_keeps_missing_data_closed(
    tmp_path: Path,
) -> None:
    repository, revisions = _make_repository(tmp_path)
    captures = tmp_path / "captures"
    _write_response(
        captures / "page-1.response.json",
        repository_name="example/project",
        total_count=4,
        nodes=[
            _node(
                1,
                merge_revision=revisions["squash_merge"],
                pr_head_revision=revisions["feature_tip"],
                issue_number=11,
            ),
            _node(
                2,
                merge_revision=revisions["unmerged_side"],
                pr_head_revision=revisions["squash_merge"],
                issue_number=12,
            ),
            _node(
                3,
                merge_revision=None,
                pr_head_revision=revisions["squash_merge"],
                issue_number=13,
            ),
            _node(
                4,
                merge_revision=revisions["squash_merge"],
                pr_head_revision=revisions["feature_tip"],
                issue_number=None,
            ),
        ],
    )
    original = RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1, 2, 3, 4),
        authoritative_issue_link_missing=(),
        repair_not_in_pinned=(1, 2, 3, 4),
        candidates=(),
    )

    result = recompute_repository_enumeration_from_github_captures(
        original,
        capture_root=captures,
        mirror_path=repository,
        pinned_ref="refs/remotes/origin/main",
        expected_repository_name="example/project",
    )

    assert result.recomputed == RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1, 2, 3, 4),
        authoritative_issue_link_missing=(4,),
        repair_not_in_pinned=(2,),
        candidates=(CandidateKey("example", 1),),
        infrastructure_blocked=(3,),
    )
    assert result.promoted_to_candidate_numbers == (1,)
    assert result.reclassified_to_issue_link_missing_numbers == (4,)
    assert result.genuine_nonancestor_numbers == (2,)
    assert result.blocked_numbers == (3,)
    assert result.blocked_merge_unavailable_numbers == (3,)
    assert result.blocked_merge_capture_missing_numbers == (3,)
    assert result.blocked_merge_object_missing_numbers == ()
    assert result.blocked_capture_structural_numbers == ()
    assert result.blocked_relationship_unverified_numbers == ()
    assert result.blocked_ancestry_unavailable_numbers == ()
    assert result.dry_run_value()["target"] == {
        "artifact_id": "repository_enumeration/example/current/v2",
        "artifact_kind": "repository_enumeration",
        "semantic_version": "2.0",
    }
    assert result.dry_run_value()["write_performed"] is False
    assert result.dry_run_value()["v2_write_permitted"] is True
    assert result.dry_run_value()["blocked_reason_buckets"] == {
        "capture_structural_numbers": [],
        "merge_unavailable_numbers": [3],
        "merge_capture_missing_numbers": [3],
        "merge_object_missing_numbers": [],
        "relationship_unverified_numbers": [],
        "ancestry_unavailable_numbers": [],
    }


def test_recompute_refuses_conflicting_authoritative_capture_without_promoting(
    tmp_path: Path,
) -> None:
    repository, revisions = _make_repository(tmp_path)
    captures = tmp_path / "captures"
    node = _node(
        1,
        merge_revision=revisions["squash_merge"],
        pr_head_revision=revisions["feature_tip"],
        issue_number=11,
    )
    _write_response(
        captures / "page-1.response.json",
        repository_name="example/project",
        total_count=1,
        nodes=[node],
    )
    _write_response(
        captures / "page-2.response.json",
        repository_name="example/project",
        total_count=1,
        nodes=[
            _node(
                1,
                merge_revision=revisions["unmerged_side"],
                pr_head_revision=revisions["squash_merge"],
                issue_number=11,
            )
        ],
    )
    original = RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1,),
        authoritative_issue_link_missing=(),
        repair_not_in_pinned=(1,),
        candidates=(),
    )

    result = recompute_repository_enumeration_from_github_captures(
        original,
        capture_root=captures,
        mirror_path=repository,
        pinned_ref="refs/remotes/origin/main",
        expected_repository_name="example/project",
    )

    assert result.recomputed == RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1,),
        authoritative_issue_link_missing=(),
        repair_not_in_pinned=(),
        candidates=(),
        infrastructure_blocked=(1,),
    )
    assert result.blocked_numbers == (1,)
    assert result.blocked_capture_structural_numbers == (1,)
    assert result.promoted_to_candidate_numbers == ()


def test_recompute_moves_reachable_absent_relationship_to_issue_link_partition(
    tmp_path: Path,
) -> None:
    repository, revisions = _make_repository(tmp_path)
    captures = tmp_path / "captures"
    _write_response(
        captures / "page-1.response.json",
        repository_name="example/project",
        total_count=2,
        nodes=[
            _node(
                1,
                merge_revision=revisions["squash_merge"],
                pr_head_revision=revisions["feature_tip"],
                issue_number=11,
            ),
            _node(
                2,
                merge_revision=revisions["squash_merge"],
                pr_head_revision=revisions["feature_tip"],
                issue_number=None,
            ),
        ],
    )
    original = RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1, 2),
        authoritative_issue_link_missing=(),
        repair_not_in_pinned=(1, 2),
        candidates=(),
    )

    result = recompute_repository_enumeration_from_github_captures(
        original,
        capture_root=captures,
        mirror_path=repository,
        pinned_ref="refs/remotes/origin/main",
        expected_repository_name="example/project",
    )

    assert result.recomputed == RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1, 2),
        authoritative_issue_link_missing=(2,),
        repair_not_in_pinned=(),
        candidates=(CandidateKey("example", 1),),
        infrastructure_blocked=(),
    )
    assert result.reclassified_to_issue_link_missing_numbers == (2,)
    assert result.dry_run_value()["v2_write_permitted"] is True


def test_recompute_keeps_unverified_relationship_as_a_distinct_terminal_partition(
    tmp_path: Path,
) -> None:
    repository, revisions = _make_repository(tmp_path)
    captures = tmp_path / "captures"
    node = _node(
        1,
        merge_revision=revisions["squash_merge"],
        pr_head_revision=revisions["feature_tip"],
        issue_number=11,
    )
    node.pop("crossReferencedIssues")
    _write_response(
        captures / "page-1.response.json",
        repository_name="example/project",
        total_count=1,
        nodes=[node],
    )
    original = RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1,),
        authoritative_issue_link_missing=(),
        repair_not_in_pinned=(1,),
        candidates=(),
    )

    result = recompute_repository_enumeration_from_github_captures(
        original,
        capture_root=captures,
        mirror_path=repository,
        pinned_ref="refs/remotes/origin/main",
        expected_repository_name="example/project",
    )

    assert result.recomputed.infrastructure_blocked == (1,)
    assert result.blocked_relationship_unverified_numbers == (1,)
    assert result.promoted_to_candidate_numbers == ()


def test_recompute_reports_missing_merge_objects_separately_from_capture_gaps(
    tmp_path: Path,
) -> None:
    repository, revisions = _make_repository(tmp_path)
    captures = tmp_path / "captures"
    _write_response(
        captures / "page-1.response.json",
        repository_name="example/project",
        total_count=1,
        nodes=[
            _node(
                1,
                merge_revision="0" * 40,
                pr_head_revision=revisions["feature_tip"],
                issue_number=11,
            )
        ],
    )
    original = RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1,),
        authoritative_issue_link_missing=(),
        repair_not_in_pinned=(1,),
        candidates=(),
    )

    result = recompute_repository_enumeration_from_github_captures(
        original,
        capture_root=captures,
        mirror_path=repository,
        pinned_ref="refs/remotes/origin/main",
        expected_repository_name="example/project",
    )

    assert result.blocked_merge_unavailable_numbers == (1,)
    assert result.blocked_merge_object_missing_numbers == (1,)
    assert (
        result.dry_run_value()["blocked_reason_buckets"][
            "merge_capture_missing_numbers"
        ]
        == []
    )


def test_recompute_requires_complete_same_repository_raw_population(
    tmp_path: Path,
) -> None:
    repository, revisions = _make_repository(tmp_path)
    captures = tmp_path / "captures"
    _write_response(
        captures / "page-1.response.json",
        repository_name="wrong/project",
        total_count=1,
        nodes=[
            _node(
                1,
                merge_revision=revisions["squash_merge"],
                pr_head_revision=revisions["feature_tip"],
                issue_number=11,
            )
        ],
    )
    original = RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1,),
        authoritative_issue_link_missing=(),
        repair_not_in_pinned=(1,),
        candidates=(),
    )

    with pytest.raises(RuntimeError, match="repository identity"):
        recompute_repository_enumeration_from_github_captures(
            original,
            capture_root=captures,
            mirror_path=repository,
            pinned_ref="refs/remotes/origin/main",
            expected_repository_name="example/project",
        )


def test_recompute_rejects_ambiguous_head_reference_before_reading_cache(
    tmp_path: Path,
) -> None:
    repository, _ = _make_repository(tmp_path)
    original = RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1,),
        authoritative_issue_link_missing=(),
        repair_not_in_pinned=(1,),
        candidates=(),
    )

    with pytest.raises(ValueError, match="pinned mirror ref"):
        recompute_repository_enumeration_from_github_captures(
            original,
            capture_root=tmp_path / "missing",
            mirror_path=repository,
            pinned_ref="HEAD",
            expected_repository_name="example/project",
        )


def test_gitea_recompute_uses_merge_revision_and_title_comment_ref_relationships(
    tmp_path: Path,
) -> None:
    repository, revisions = _make_repository(tmp_path)
    captures = tmp_path / "captures"
    api_base = "https://gitea.example/api/v1"
    _write_gitea_response(
        captures / "pulls-page-1.gitea-response.json",
        url=(
            f"{api_base}/repos/example/project/pulls?"
            "state=closed&sort=oldest&limit=100&page=1"
        ),
        headers={"x-total-count": "4"},
        body=[
            _gitea_pull(
                1,
                merge_revision=revisions["squash_merge"],
                title="Fix #11: regression",
            ),
            _gitea_pull(
                2,
                merge_revision=revisions["squash_merge"],
                title="No title relation",
            ),
            _gitea_pull(3, merge_revision=None, title="Fix #13"),
            _gitea_pull(
                4,
                merge_revision=revisions["squash_merge"],
                title="No issue reference",
            ),
        ],
    )
    _write_gitea_timeline(
        captures,
        api_base=api_base,
        number=1,
        rows=[],
    )
    _write_gitea_timeline(
        captures,
        api_base=api_base,
        number=2,
        rows=[
            {
                "type": "comment_ref",
                "ref_issue": {
                    "number": 12,
                    "url": f"{api_base}/repos/example/project/issues/12",
                },
            }
        ],
    )
    _write_gitea_timeline(
        captures,
        api_base=api_base,
        number=3,
        rows=[],
    )
    _write_gitea_timeline(
        captures,
        api_base=api_base,
        number=4,
        rows=[],
    )
    _write_gitea_response(
        captures / "superseded-pull-pages" / "old-page-1.gitea-response.json",
        url=(
            f"{api_base}/repos/example/project/pulls?"
            "state=closed&sort=oldest&limit=100&page=1"
        ),
        headers={"x-total-count": "1"},
        body=[
            _gitea_pull(
                1,
                merge_revision=revisions["unmerged_side"],
                title="Fix #11: superseded response",
            )
        ],
    )
    original = RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1, 2, 3, 4),
        authoritative_issue_link_missing=(),
        repair_not_in_pinned=(1, 2, 3, 4),
        candidates=(),
    )

    result = recompute_repository_enumeration_from_gitea_captures(
        original,
        capture_root=captures,
        mirror_path=repository,
        pinned_ref="refs/remotes/origin/main",
        expected_repository_name="example/project",
        expected_api_base=api_base,
    )

    assert result.recomputed == RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1, 2, 3, 4),
        authoritative_issue_link_missing=(4,),
        repair_not_in_pinned=(),
        candidates=(CandidateKey("example", 1), CandidateKey("example", 2)),
        infrastructure_blocked=(3,),
    )
    assert result.promoted_to_candidate_numbers == (1, 2)
    assert result.reclassified_to_issue_link_missing_numbers == (4,)
    assert result.blocked_merge_capture_missing_numbers == (3,)
    assert result.capture_file_count == 5  # The historical subdirectory is ignored.


def test_gitea_recompute_rejects_duplicate_canonical_pull_page(
    tmp_path: Path,
) -> None:
    repository, revisions = _make_repository(tmp_path)
    captures = tmp_path / "captures"
    api_base = "https://gitea.example/api/v1"
    _write_gitea_response(
        captures / "pulls-page-1.gitea-response.json",
        url=(
            f"{api_base}/repos/example/project/pulls?"
            "state=closed&sort=oldest&limit=100&page=1"
        ),
        headers={"x-total-count": "1"},
        body=[
            _gitea_pull(
                1,
                merge_revision=revisions["squash_merge"],
                title="No title relation",
            )
        ],
    )
    _write_gitea_timeline(
        captures,
        api_base=api_base,
        number=1,
        rows=[],
    )
    _write_gitea_response(
        captures / "history-duplicate.gitea-response.json",
        url=(
            f"{api_base}/repos/example/project/pulls?"
            "state=closed&sort=oldest&limit=100&page=1"
        ),
        headers={"x-total-count": "1"},
        body=[
            _gitea_pull(
                1,
                merge_revision=revisions["unmerged_side"],
                title="Fix #11",
            )
        ],
    )
    original = RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1,),
        authoritative_issue_link_missing=(),
        repair_not_in_pinned=(1,),
        candidates=(),
    )

    with pytest.raises(RuntimeError, match="duplicate pull page"):
        recompute_repository_enumeration_from_gitea_captures(
            original,
            capture_root=captures,
            mirror_path=repository,
            pinned_ref="refs/remotes/origin/main",
            expected_repository_name="example/project",
            expected_api_base=api_base,
        )


def test_gitea_recompute_fails_closed_for_malformed_comment_ref(
    tmp_path: Path,
) -> None:
    repository, revisions = _make_repository(tmp_path)
    captures = tmp_path / "captures"
    api_base = "https://gitea.example/api/v1"
    _write_gitea_response(
        captures / "pulls-page-1.gitea-response.json",
        url=(
            f"{api_base}/repos/example/project/pulls?"
            "state=closed&sort=oldest&limit=100&page=1"
        ),
        headers={"x-total-count": "1"},
        body=[
            _gitea_pull(
                1,
                merge_revision=revisions["squash_merge"],
                title="No title relation",
            )
        ],
    )
    _write_gitea_timeline(
        captures,
        api_base=api_base,
        number=1,
        rows=[{"type": "comment_ref", "ref_issue": {"number": 11}}],
    )
    original = RepositoryEnumeration(
        repository_key="example",
        merged_change_numbers=(1,),
        authoritative_issue_link_missing=(),
        repair_not_in_pinned=(1,),
        candidates=(),
    )

    result = recompute_repository_enumeration_from_gitea_captures(
        original,
        capture_root=captures,
        mirror_path=repository,
        pinned_ref="refs/remotes/origin/main",
        expected_repository_name="example/project",
        expected_api_base=api_base,
    )

    assert result.recomputed.infrastructure_blocked == (1,)
    assert result.blocked_relationship_unverified_numbers == (1,)


def _write_gitea_response(
    path: Path,
    *,
    url: str,
    headers: dict[str, str],
    body: object,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "body_base64": base64.b64encode(
                    json.dumps(body, sort_keys=True).encode("utf-8")
                ).decode("ascii"),
                "final_url": url,
                "headers": headers,
                "status_code": 200,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _write_gitea_timeline(
    captures: Path,
    *,
    api_base: str,
    number: int,
    rows: list[object],
) -> None:
    _write_gitea_response(
        captures / f"timeline-{number}.gitea-response.json",
        url=(
            f"{api_base}/repos/example/project/issues/{number}/timeline?"
            "limit=100&page=1"
        ),
        headers={"x-total-count": str(len(rows))},
        body=rows,
    )


def _gitea_pull(
    number: int,
    *,
    merge_revision: str | None,
    title: str,
) -> dict[str, object]:
    return {
        "number": number,
        "state": "closed",
        "merged": True,
        "merged_at": "2026-08-12T00:00:00Z",
        "merge_commit_sha": merge_revision,
        "title": title,
        "body": "",
        "base": {"repo": {"full_name": "example/project"}},
    }


def _make_repository(path: Path) -> tuple[Path, dict[str, str]]:
    repository = path / "mirror.git"
    _git(path, "init", "--bare", str(repository))
    worktree = path / "worktree"
    _git(path, "clone", str(repository), str(worktree))
    _git(worktree, "config", "user.name", "C1 Test")
    _git(worktree, "config", "user.email", "c1@example.invalid")
    (worktree / "value.txt").write_text("base\n", encoding="utf-8")
    _git(worktree, "add", "value.txt")
    _git(worktree, "commit", "-m", "base")
    _git(worktree, "branch", "-M", "main")
    _git(worktree, "push", "origin", "main")
    _git(repository, "symbolic-ref", "HEAD", "refs/heads/main")

    _git(worktree, "checkout", "-b", "feature")
    (worktree / "value.txt").write_text("feature\n", encoding="utf-8")
    _git(worktree, "commit", "-am", "feature")
    feature_tip = _git_output(worktree, "rev-parse", "HEAD")

    _git(worktree, "checkout", "main")
    _git(worktree, "merge", "--squash", "feature")
    _git(worktree, "commit", "-m", "squash feature")
    squash_merge = _git_output(worktree, "rev-parse", "HEAD")
    _git(worktree, "push", "origin", "main")
    _git(repository, "update-ref", "refs/remotes/origin/main", squash_merge)

    _git(worktree, "checkout", "-b", "unmerged-side", "main~1")
    (worktree / "side.txt").write_text("side\n", encoding="utf-8")
    _git(worktree, "add", "side.txt")
    _git(worktree, "commit", "-m", "unmerged side")
    unmerged_side = _git_output(worktree, "rev-parse", "HEAD")
    _git(worktree, "push", "origin", "unmerged-side")

    return repository, {
        "feature_tip": feature_tip,
        "squash_merge": squash_merge,
        "unmerged_side": unmerged_side,
    }


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _git_output(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _write_response(
    path: Path,
    *,
    repository_name: str,
    total_count: int,
    nodes: list[dict[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "data": {
                    "repository": {
                        "nameWithOwner": repository_name,
                        "pullRequests": {"totalCount": total_count, "nodes": nodes},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _node(
    number: int,
    *,
    merge_revision: str | None,
    pr_head_revision: str,
    issue_number: int | None,
) -> dict[str, object]:
    return {
        "number": number,
        "state": "MERGED",
        "mergeCommit": None if merge_revision is None else {"oid": merge_revision},
        "headRefOid": pr_head_revision,
        "closingIssuesReferences": {
            "nodes": []
            if issue_number is None
            else [
                {
                    "number": issue_number,
                    "url": (
                        f"https://github.com/example/project/issues/{issue_number}"
                    ),
                }
            ]
        },
        "crossReferencedIssues": {"nodes": []},
    }
