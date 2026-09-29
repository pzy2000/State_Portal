"""Mechanical defect-history mining and immutable upstream provenance."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from warptest.issue_naturalize import TokenGuard


UPSTREAM_REPOSITORIES: dict[str, tuple[str, str]] = {
    "luanti": ("github", "luanti-org/luanti"),
    "blender": ("gitea", "blender/blender"),
    "jupyterlab": ("github", "jupyterlab/jupyterlab"),
    "openra": ("github", "OpenRA/OpenRA"),
    "jynew": ("github", "jynew/jynew"),
    "pal3": ("github", "0x7c13/Pal3.Unity"),
}
FORMAL_UPSTREAM_REPOSITORIES: dict[str, tuple[str, str]] = {
    benchmark: UPSTREAM_REPOSITORIES[benchmark]
    for benchmark in ("luanti", "blender", "jupyterlab", "openra")
}

REPOSITORY_PATHS: dict[str, str] = {
    "luanti": "games/luanti",
    "blender": "games/blender",
    "jupyterlab": "games/jupyterlab",
    "openra": "games/OpenRA",
    "jynew": "games/jynew",
    "pal3": "games/Pal3.Unity",
}

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION_HINTS = (
    "bug",
    "buggy",
    "broken",
    "defect",
    "failure",
    "fix",
    "fixed",
    "regression",
    "revert",
    "reverted",
    "version",
)
_CPP_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".inl"})


@dataclass(frozen=True)
class CandidateFacts:
    benchmark: str
    pinned_revision: str
    fix_commit: str
    touched_files: tuple[str, ...]
    additions: int
    deletions: int
    reverse_check_passed: bool
    observable: bool
    has_authoritative_conversation: bool
    has_f2p: bool
    has_p2p: bool

    @property
    def diff_loc(self) -> int:
        return self.additions + self.deletions

    @property
    def build_class(self) -> str:
        return classify_build_class(self.benchmark, self.touched_files)


def classify_build_class(benchmark: str, touched_files: Sequence[str]) -> str:
    if benchmark not in UPSTREAM_REPOSITORIES:
        raise ValueError(f"unsupported Lazarus benchmark: {benchmark!r}")
    if not touched_files:
        raise ValueError("cannot classify an empty patch")
    normalized = tuple(str(path).replace("\\", "/") for path in touched_files)
    if benchmark == "luanti":
        if all(
            (path.startswith("builtin/") or path.startswith("games/"))
            and path.endswith(".lua")
            for path in normalized
        ):
            return "none"
        if any(Path(path).suffix.lower() in _CPP_SUFFIXES for path in normalized):
            return "incremental_cpp"
        return "unsupported"
    if benchmark == "blender":
        if all(path.startswith("scripts/") and path.endswith(".py") for path in normalized):
            return "none"
        if any(Path(path).suffix.lower() in _CPP_SUFFIXES for path in normalized):
            return "incremental_cpp"
        return "unsupported"
    if benchmark == "jupyterlab":
        python_only = all(
            path.endswith(".py")
            and not path.startswith(("packages/", "galata/", "ui-components/"))
            for path in normalized
        )
        return "none" if python_only else "jlpm"
    if benchmark == "openra":
        return "incremental_dotnet"
    return "unity"


def parse_fix_references(benchmark: str, message: str) -> dict[str, list[int]]:
    if benchmark not in UPSTREAM_REPOSITORIES:
        raise ValueError(f"unsupported Lazarus benchmark: {benchmark!r}")
    text = str(message)
    if benchmark == "blender":
        issues = sorted(
            {int(value) for value in re.findall(r"(?i)\bfix(?:e[sd])?\s+#(\d+)", text)}
        )
        return {"issue_numbers": issues, "pr_numbers": []}

    prs = {
        int(value)
        for value in re.findall(r"(?i)\b(?:pull request|pr)\s+#(\d+)", text)
    }
    subject = text.splitlines()[0] if text.splitlines() else text
    prs.update(int(value) for value in re.findall(r"\(#(\d+)\)[ \t]*$", subject))
    issues = {
        int(value)
        for value in re.findall(
            r"(?i)\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+#(\d+)", text
        )
    }
    issues -= prs
    return {
        "issue_numbers": sorted(issues),
        "pr_numbers": sorted(prs),
    }


def validate_candidate_facts(facts: CandidateFacts) -> list[str]:
    errors: list[str] = []
    if facts.benchmark not in FORMAL_UPSTREAM_REPOSITORIES:
        errors.append("benchmark is outside the formal four-repository scope")
    if not _FULL_SHA.fullmatch(facts.pinned_revision):
        errors.append("pinned_revision is not a full SHA")
    if not _FULL_SHA.fullmatch(facts.fix_commit):
        errors.append("fix_commit is not a full SHA")
    if not facts.touched_files or len(facts.touched_files) > 3:
        errors.append("touched_files must contain between one and three paths")
    if facts.diff_loc > 120:
        errors.append(f"diff_loc exceeds 120: {facts.diff_loc}")
    if facts.additions < 0 or facts.deletions < 0:
        errors.append("diff counts must be non-negative")
    if not facts.reverse_check_passed:
        errors.append("exact reverse patch check did not pass")
    if not facts.observable:
        errors.append("candidate is not semantically or visually observable")
    if not facts.has_authoritative_conversation:
        errors.append("authoritative upstream conversation is missing")
    if not facts.has_f2p:
        errors.append("fail-to-pass evidence is missing")
    if not facts.has_p2p:
        errors.append("pass-to-pass evidence is missing")
    return errors


def scan_repository(
    benchmark: str,
    repo_path: str | Path,
    *,
    limit: int = 2_000,
) -> list[dict[str, Any]]:
    """Scan one permitted local submodule without mutating its working tree."""

    if benchmark not in UPSTREAM_REPOSITORIES:
        raise ValueError(f"unsupported Lazarus benchmark: {benchmark!r}")
    repo = Path(repo_path).expanduser().resolve()
    _require_clean_repository(repo)
    pinned = _git_text(repo, "rev-parse", "HEAD")
    raw = _git_text(
        repo,
        "log",
        f"-{int(limit)}",
        "--format=%H%x00%s%x00%b%x1e",
    )
    rows: list[dict[str, Any]] = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split("\x00", 2)
        if len(parts) != 3:
            continue
        commit, subject, body = parts
        message = f"{subject}\n{body}".strip()
        references = parse_fix_references(benchmark, message)
        if not references["issue_numbers"] and not references["pr_numbers"]:
            continue
        try:
            parent = _git_text(repo, "rev-parse", f"{commit}^1")
            patch = _git_bytes(repo, "diff", "--binary", "--full-index", parent, commit, "--")
        except RuntimeError:
            continue
        files, additions, deletions = _patch_stats(repo, parent, commit)
        reverse = _check_reverse_patch(repo, patch)
        try:
            distance = int(_git_text(repo, "rev-list", "--count", f"{commit}..{pinned}"))
        except (RuntimeError, ValueError):
            distance = -1
        try:
            build_class = classify_build_class(benchmark, files)
        except ValueError:
            build_class = "unsupported"
        rows.append(
            {
                "benchmark": benchmark,
                "pinned_revision": pinned,
                "fix_commit": commit,
                "subject": subject,
                "references": references,
                "touched_files": list(files),
                "touched_file_count": len(files),
                "additions": additions,
                "deletions": deletions,
                "diff_loc": additions + deletions,
                "build_class": build_class,
                "distance_from_pinned": distance,
                "reverse_check_passed": reverse,
                "mechanical_patch_gate": build_class != "unsupported"
                and bool(patch)
                and reverse
                and len(files) <= 3
                and additions + deletions <= 120,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            not row["mechanical_patch_gate"],
            row["build_class"] not in {"none", "jlpm", "incremental_dotnet", "unity"},
            row["diff_loc"],
            row["distance_from_pinned"] if row["distance_from_pinned"] >= 0 else 10**12,
        ),
    )


def freeze_upstream_bundle(
    *,
    task_id: str,
    benchmark: str,
    repo_path: str | Path,
    patch_base: str,
    patch_head: str,
    issue: Mapping[str, Any] | None,
    pull_request: Mapping[str, Any] | None,
    comments: Sequence[Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]],
    tests: Sequence[Mapping[str, Any]],
    retrieved_at: str,
    destination: str | Path,
    observable: bool,
    upstream_commits: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Freeze an exact forward fix patch plus complete authoritative dialogue."""

    if benchmark not in UPSTREAM_REPOSITORIES:
        raise ValueError(f"unsupported Lazarus benchmark: {benchmark!r}")
    repo = Path(repo_path).expanduser().resolve()
    _require_clean_repository(repo)
    pinned = _git_text(repo, "rev-parse", "HEAD")
    base = _resolve_commit(repo, patch_base)
    head = _resolve_commit(repo, patch_head)
    patch = _git_bytes(repo, "diff", "--binary", "--full-index", base, head, "--")
    if not patch:
        raise ValueError("selected fix range produces an empty patch")
    files, additions, deletions = _patch_stats(repo, base, head)
    reverse_ok = _check_reverse_patch(repo, patch)
    kinds = {str(test.get("kind") or "") for test in tests}
    facts = CandidateFacts(
        benchmark=benchmark,
        pinned_revision=pinned,
        fix_commit=head,
        touched_files=files,
        additions=additions,
        deletions=deletions,
        reverse_check_passed=reverse_ok,
        observable=observable,
        has_authoritative_conversation=issue is not None or pull_request is not None,
        has_f2p="fail_to_pass" in kinds,
        has_p2p="pass_to_pass" in kinds,
    )
    errors = validate_candidate_facts(facts)
    if errors:
        raise ValueError("candidate failed mechanical freeze gate: " + "; ".join(errors))
    patch_sha = hashlib.sha256(patch).hexdigest()
    target = Path(destination).expanduser().resolve()
    frozen_tests, test_capsules = _freeze_fix_commit_test_sources(
        repo,
        head,
        tests,
    )
    authority, repository = UPSTREAM_REPOSITORIES[benchmark]
    upstream = build_upstream_record(
        task_id=task_id,
        benchmark=benchmark,
        authority=authority,
        repository=repository,
        issue=issue,
        pull_request=pull_request,
        comments=comments,
        reviews=reviews,
        fix_commit=head,
        pinned_revision=pinned,
        touched_files=files,
        patch_sha256=patch_sha,
        tests=frozen_tests,
        retrieved_at=retrieved_at,
        upstream_commits=upstream_commits,
    )
    target.mkdir(parents=True, exist_ok=True)
    patch_path = target / "revert.patch"
    upstream_path = target / "upstream.json"
    _atomic_write_bytes(patch_path, patch)
    for relative, content in sorted(test_capsules.items()):
        _atomic_write_bytes(target / relative, content)
    _atomic_write_bytes(
        upstream_path,
        (json.dumps(upstream, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )
    return {
        "task_id": task_id,
        "benchmark": benchmark,
        "fix_commit": head,
        "patch_base": base,
        "pinned_revision": pinned,
        "patch_path": str(patch_path),
        "upstream_path": str(upstream_path),
        "patch_sha256": patch_sha,
        "record_sha256": upstream["record_sha256"],
        "touched_files": list(files),
        "build_class": facts.build_class,
        "diff_loc": facts.diff_loc,
        "reverse_check_passed": reverse_ok,
        "frozen_test_sources": sorted(test_capsules),
    }


def backfill_local_frozen_test_capsules(
    *,
    benchmark: str,
    repo_path: str | Path,
    fix_commit: str,
    tests: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[Path, bytes]]:
    """Backfill construction-test capsules from a clean local Git history.

    This is intentionally narrower than :func:`freeze_upstream_bundle`: it
    does not fetch authority data, inspect a PR, or alter a reverse patch.  It
    only repairs historical evidence records that omitted the fixed-version
    source files needed by the construction gate.  The caller owns persistence
    of the returned test declarations and capsules, so a data migration can
    update an upstream record and its mirrored authoring descriptor atomically.

    All construction F2P/P2P sources are read with ``git show`` at exactly the
    supplied fix commit.  Test command arguments that used a workspace source
    checkout are rewritten to paths relative to the disposable clone.  This
    prevents an overlaid capsule from being silently bypassed by a command that
    still reads ``{harness_root}/games/<repository>``.
    """

    if benchmark not in UPSTREAM_REPOSITORIES:
        raise ValueError(f"unsupported Lazarus benchmark: {benchmark!r}")
    repo = Path(repo_path).expanduser().resolve()
    _require_clean_repository(repo)
    if not _FULL_SHA.fullmatch(str(fix_commit)):
        raise ValueError("fix_commit must be a full SHA")
    resolved_fix = _resolve_commit(repo, str(fix_commit))
    if resolved_fix != fix_commit:
        raise ValueError("fix_commit must resolve exactly in the local repository")
    if not isinstance(tests, Sequence) or isinstance(tests, (str, bytes)):
        raise ValueError("tests must be a sequence of mappings")

    authority, repository = UPSTREAM_REPOSITORIES[benchmark]
    if authority != "github":
        raise ValueError(
            "local frozen-test capsule backfill currently supports GitHub authorities only"
        )
    repository_path = Path(REPOSITORY_PATHS[benchmark])
    frozen: list[dict[str, Any]] = []
    capsules: dict[Path, bytes] = {}
    construction_count = 0
    for index, raw in enumerate(tests):
        if not isinstance(raw, Mapping):
            raise ValueError(f"tests[{index}] must be a mapping")
        value = copy.deepcopy(dict(raw))
        kind = str(value.get("kind") or "")
        origin = str(value.get("origin") or "")
        if origin != "upstream" or kind not in {"fail_to_pass", "pass_to_pass"}:
            frozen.append(value)
            continue
        construction_count += 1
        primary = _local_backfill_relative_path(
            value.get("upstream_test_path"), label=f"tests[{index}] upstream test"
        )
        raw_files = value.get("upstream_test_files")
        if raw_files is None:
            raw_files = [primary.as_posix()]
        if not isinstance(raw_files, list) or not raw_files:
            raise ValueError(f"tests[{index}] upstream_test_files must be a non-empty list")

        source_entries: list[dict[str, str]] = []
        frozen_entries: list[dict[str, str]] = []
        for file_index, raw_file in enumerate(raw_files):
            file_value = (
                raw_file.get("path") if isinstance(raw_file, Mapping) else raw_file
            )
            relative = _local_backfill_relative_path(
                file_value,
                label=(
                    f"tests[{index}].upstream_test_files[{file_index}]"
                ),
            )
            _require_regular_git_file(repo, resolved_fix, relative)
            try:
                content = _git_bytes(
                    repo, "show", f"{resolved_fix}:{relative.as_posix()}"
                )
            except RuntimeError as error:
                raise ValueError(
                    f"tests[{index}] upstream test is absent at the fix commit"
                ) from error
            sha256 = hashlib.sha256(content).hexdigest()
            source_url = (
                f"https://github.com/{repository}/blob/{resolved_fix}/"
                f"{relative.as_posix()}"
            )
            capsule = Path("upstream-tests") / relative
            existing = capsules.get(capsule)
            if existing is not None and existing != content:
                raise ValueError(
                    f"tests[{index}] conflicts with another frozen upstream test source"
                )
            capsules[capsule] = content
            source_entries.append(
                {
                    "path": relative.as_posix(),
                    "sha256": sha256,
                    "source_url": source_url,
                }
            )
            frozen_entries.append(
                {
                    "source_path": capsule.as_posix(),
                    "destination_path": relative.as_posix(),
                    "sha256": sha256,
                }
            )

        primary_entry = next(
            entry
            for entry in source_entries
            if entry["path"] == primary.as_posix()
        )
        value["upstream_test_path"] = primary_entry["path"]
        value["upstream_test_sha256"] = primary_entry["sha256"]
        value["source_url"] = primary_entry["source_url"]
        value["upstream_test_files"] = source_entries
        value["frozen_test_sources"] = frozen_entries
        value["argv"] = _rewrite_clone_relative_test_argv(
            value.get("argv"),
            repository_path=repository_path,
            source_path=primary,
            label=f"tests[{index}].argv",
        )
        value["command"] = _rewrite_clone_relative_test_argv(
            value.get("command"),
            repository_path=repository_path,
            source_path=primary,
            label=f"tests[{index}].command",
        )
        frozen.append(value)
    if construction_count == 0:
        raise ValueError("no upstream construction F2P/P2P test was supplied")
    return frozen, capsules


def _local_backfill_relative_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} path is missing")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} path escapes the repository")
    return Path(relative.as_posix())


def _require_regular_git_file(repo: Path, commit: str, relative: Path) -> None:
    """Reject symlink/submodule test paths before reading a local Git blob."""

    listing = _git_text(repo, "ls-tree", commit, "--", relative.as_posix())
    mode = listing.split(maxsplit=1)[0] if listing else ""
    if mode not in {"100644", "100755"}:
        raise ValueError(
            f"frozen upstream test source must be a regular non-symlink file: "
            f"{relative.as_posix()}"
        )


def _rewrite_clone_relative_test_argv(
    value: object,
    *,
    repository_path: Path,
    source_path: Path,
    label: str,
) -> list[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(part, str) and part for part in value
    ):
        raise ValueError(f"{label} must be a non-empty argv list")
    legacy_root = "{harness_root}/" + repository_path.as_posix().rstrip("/") + "/"
    legacy_source = legacy_root + source_path.as_posix()
    rewritten: list[str] = []
    for part in value:
        if part == legacy_source:
            rewritten.append(source_path.as_posix())
        elif part.startswith(legacy_root):
            raise ValueError(
                f"{label} reads a workspace repository path other than its frozen test"
            )
        else:
            rewritten.append(part)
    return rewritten


def fetch_authoritative_conversation(
    benchmark: str,
    *,
    issue_number: int | None,
    pr_number: int | None,
) -> dict[str, Any]:
    """Fetch complete upstream bodies/comments from the registered authority."""

    authority, repository = UPSTREAM_REPOSITORIES.get(benchmark, (None, None))
    if authority is None:
        raise ValueError(f"unsupported Lazarus benchmark: {benchmark!r}")
    if issue_number is None and pr_number is None:
        raise ValueError("at least one issue or PR number is required")
    if authority == "github":
        result = _fetch_github_conversation(
            str(repository), issue_number=issue_number, pr_number=pr_number
        )
    else:
        if pr_number is not None:
            raise ValueError("Blender provenance is issue-based on projects.blender.org")
        result = _fetch_gitea_issue(str(repository), int(issue_number))
    result["retrieved_at"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    return result


def _freeze_fix_commit_test_sources(
    repo: Path,
    fix_commit: str,
    tests: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[Path, bytes]]:
    """Capture the fix commit's exact upstream test bytes for F2P replay.

    An inverse patch often reverts the regression test together with production
    code.  Construction gates must therefore run one immutable test capsule
    against both versions.  The capsule is coordinator evidence, never part of
    the model-visible tree or per-trial P2P path.
    """

    frozen: list[dict[str, Any]] = []
    capsules: dict[Path, bytes] = {}
    for index, raw in enumerate(tests):
        value = copy.deepcopy(dict(raw))
        if (
            str(value.get("origin") or "") != "upstream"
            or str(value.get("kind") or "") == "per_trial_p2p"
        ):
            frozen.append(value)
            continue
        raw_files = value.get("upstream_test_files")
        if raw_files is None:
            raw_files = [
                {
                    "path": value.get("upstream_test_path"),
                    "sha256": value.get("upstream_test_sha256"),
                }
            ]
        if not isinstance(raw_files, list) or not raw_files:
            raise ValueError(f"tests[{index}] upstream test files must be a non-empty list")
        frozen_entries: list[dict[str, str]] = []
        for file_index, raw_file in enumerate(raw_files):
            if not isinstance(raw_file, Mapping):
                raise ValueError(
                    f"tests[{index}].upstream_test_files[{file_index}] must be a mapping"
                )
            path_value = raw_file.get("path")
            declared_sha = str(raw_file.get("sha256") or "")
            if not isinstance(path_value, str) or not path_value.strip():
                raise ValueError(
                    f"tests[{index}].upstream_test_files[{file_index}] path is missing"
                )
            relative = Path(path_value)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(
                    f"tests[{index}] upstream test path escapes the source tree"
                )
            relative = Path(relative.as_posix())
            try:
                content = _git_bytes(
                    repo, "show", f"{fix_commit}:{relative.as_posix()}"
                )
            except RuntimeError as error:
                raise ValueError(
                    f"tests[{index}] upstream test is absent at the fix commit"
                ) from error
            actual_sha = hashlib.sha256(content).hexdigest()
            if declared_sha != actual_sha:
                raise ValueError(
                    f"tests[{index}] upstream test SHA does not bind the fix commit"
                )
            capsule = Path("upstream-tests") / relative
            existing = capsules.get(capsule)
            if existing is not None and existing != content:
                raise ValueError(
                    f"tests[{index}] conflicts with another frozen upstream test source"
                )
            capsules[capsule] = content
            frozen_entries.append(
                {
                    "source_path": capsule.as_posix(),
                    "destination_path": relative.as_posix(),
                    "sha256": actual_sha,
                }
            )
        primary_path = str(value.get("upstream_test_path") or "")
        primary_sha = str(value.get("upstream_test_sha256") or "")
        if not any(
            entry["destination_path"] == primary_path
            and entry["sha256"] == primary_sha
            for entry in frozen_entries
        ):
            raise ValueError(
                f"tests[{index}] primary upstream test is absent from its frozen sources"
            )
        value["frozen_test_sources"] = frozen_entries
        frozen.append(value)
    return frozen, capsules


def _fetch_github_conversation(
    repository: str, *, issue_number: int | None, pr_number: int | None
) -> dict[str, Any]:
    issue: dict[str, Any] | None = None
    pull_request: dict[str, Any] | None = None
    comments: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    commits: list[dict[str, Any]] = []
    if issue_number is not None:
        raw_issue = _gh_json(f"repos/{repository}/issues/{int(issue_number)}")
        issue = _github_subject(raw_issue)
        comments.extend(
            _github_comments(
                _gh_json_pages(
                    f"repos/{repository}/issues/{int(issue_number)}/comments"
                ),
                source="issue_comment",
            )
        )
    if pr_number is not None:
        raw_pr = _gh_json(f"repos/{repository}/pulls/{int(pr_number)}")
        pull_request = _github_subject(raw_pr)
        comments.extend(
            _github_comments(
                _gh_json_pages(
                    f"repos/{repository}/issues/{int(pr_number)}/comments"
                ),
                source="pull_request_comment",
            )
        )
        comments.extend(
            _github_comments(
                _gh_json_pages(
                    f"repos/{repository}/pulls/{int(pr_number)}/comments"
                ),
                source="pull_request_inline_comment",
            )
        )
        for value in _gh_json_pages(
            f"repos/{repository}/pulls/{int(pr_number)}/reviews"
        ):
            reviews.append(
                {
                    "source": "pull_request_review",
                    "id": value.get("id"),
                    "url": value.get("html_url") or pull_request.get("url"),
                    "author": (value.get("user") or {}).get("login"),
                    "created_at": value.get("submitted_at"),
                    "state": value.get("state"),
                    "body": value.get("body") or "",
                }
            )
        for value in _gh_json_pages(
            f"repos/{repository}/pulls/{int(pr_number)}/commits"
        ):
            commit = value.get("commit") or {}
            author = commit.get("author") or {}
            commits.append(
                {
                    "sha": value.get("sha"),
                    "url": value.get("html_url"),
                    "author": author.get("name"),
                    "authored_at": author.get("date"),
                    "body": commit.get("message") or "",
                }
            )
    return {
        "issue": issue,
        "pull_request": pull_request,
        "comments": comments,
        "reviews": reviews,
        "upstream_commits": commits,
    }


def _github_subject(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "number": value.get("number"),
        "url": value.get("html_url"),
        "title": value.get("title"),
        "state": value.get("state"),
        "author": (value.get("user") or {}).get("login"),
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
        "closed_at": value.get("closed_at"),
        "merged_at": value.get("merged_at"),
        "body": value.get("body") or "",
    }


def _github_comments(
    values: Sequence[Mapping[str, Any]], *, source: str
) -> list[dict[str, Any]]:
    return [
        {
            "source": source,
            "id": value.get("id"),
            "url": value.get("html_url"),
            "author": (value.get("user") or {}).get("login"),
            "created_at": value.get("created_at"),
            "updated_at": value.get("updated_at"),
            "body": value.get("body") or "",
        }
        for value in values
    ]


def _gh_json(endpoint: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"GitHub API request failed for {endpoint}: {detail}")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError(f"GitHub API endpoint did not return an object: {endpoint}")
    return value


def _gh_json_pages(endpoint: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(1, 101):
        separator = "&" if "?" in endpoint else "?"
        completed = subprocess.run(
            ["gh", "api", f"{endpoint}{separator}per_page=100&page={page}"],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"GitHub API request failed for {endpoint}: {detail}")
        value = json.loads(completed.stdout)
        if not isinstance(value, list):
            raise RuntimeError(f"GitHub API endpoint did not return a list: {endpoint}")
        rows.extend(item for item in value if isinstance(item, dict))
        if len(value) < 100:
            return rows
    raise RuntimeError(f"GitHub API pagination exceeded 100 pages: {endpoint}")


def _fetch_gitea_issue(repository: str, issue_number: int) -> dict[str, Any]:
    base = f"https://projects.blender.org/api/v1/repos/{repository}"
    raw_issue = _url_json(f"{base}/issues/{issue_number}")
    raw_comments = _url_json_pages(f"{base}/issues/{issue_number}/comments")
    issue = {
        "number": raw_issue.get("number"),
        "url": raw_issue.get("html_url"),
        "title": raw_issue.get("title"),
        "state": raw_issue.get("state"),
        "author": (raw_issue.get("user") or {}).get("login"),
        "created_at": raw_issue.get("created_at"),
        "updated_at": raw_issue.get("updated_at"),
        "closed_at": raw_issue.get("closed_at"),
        "body": raw_issue.get("body") or "",
    }
    comments = [
        {
            "source": "issue_comment",
            "id": value.get("id"),
            "url": value.get("html_url"),
            "author": (value.get("user") or {}).get("login"),
            "created_at": value.get("created_at"),
            "updated_at": value.get("updated_at"),
            "body": value.get("body") or "",
        }
        for value in raw_comments
    ]
    return {
        "issue": issue,
        "pull_request": None,
        "comments": comments,
        "reviews": [],
        "upstream_commits": [],
    }


def _url_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "C1-Lazarus/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Gitea API request failed for {url}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Gitea API endpoint did not return an object: {url}")
    return value


def _url_json_pages(url: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(1, 101):
        value = _url_json_list(f"{url}?limit=100&page={page}")
        rows.extend(value)
        if len(value) < 100:
            return rows
    raise RuntimeError(f"Gitea API pagination exceeded 100 pages: {url}")


def _url_json_list(url: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(url, headers={"User-Agent": "C1-Lazarus/1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Gitea API request failed for {url}: {error}") from error
    if not isinstance(value, list):
        raise RuntimeError(f"Gitea API endpoint did not return a list: {url}")
    return [item for item in value if isinstance(item, dict)]


def _require_clean_repository(repo: Path) -> None:
    if not repo.is_dir():
        raise ValueError(f"repository does not exist: {repo}")
    head = _git_text(repo, "rev-parse", "HEAD")
    if not _FULL_SHA.fullmatch(head):
        raise ValueError(f"repository HEAD is not a full SHA: {repo}")
    status = _git_text(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise ValueError(f"repository must be clean before mining: {repo}")


def _resolve_commit(repo: Path, revision: str) -> str:
    value = _git_text(repo, "rev-parse", f"{revision}^{{commit}}")
    if not _FULL_SHA.fullmatch(value):
        raise ValueError(f"revision did not resolve to a full commit: {revision!r}")
    return value


def _git_text(repo: Path, *args: str) -> str:
    return _git_bytes(repo, *args).decode("utf-8", errors="replace").strip()


def _git_bytes(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            "git command failed: " + " ".join(args) + (f": {detail}" if detail else "")
        )
    return completed.stdout


def _patch_stats(repo: Path, base: str, head: str) -> tuple[tuple[str, ...], int, int]:
    names = tuple(
        sorted(
            line
            for line in _git_text(repo, "diff", "--name-only", base, head, "--").splitlines()
            if line
        )
    )
    additions = 0
    deletions = 0
    for line in _git_text(repo, "diff", "--numstat", base, head, "--").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        if parts[0] == "-" or parts[1] == "-":
            additions += 121
            continue
        additions += int(parts[0])
        deletions += int(parts[1])
    return names, additions, deletions


def _check_reverse_patch(repo: Path, patch: bytes) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo), "apply", "--reverse", "--check", "-"],
        input=patch,
        capture_output=True,
    )
    return completed.returncode == 0


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalize_body(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _freeze_body_record(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    body_raw = str(value.get("body") or value.get("body_raw") or "")
    result = {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if key not in {"body", "body_raw", "body_normalized", "body_sha256"}
    }
    result.update(
        {
            "body_raw": body_raw,
            "body_normalized": _normalize_body(body_raw),
            "body_sha256": _sha256_text(body_raw),
        }
    )
    return result


def build_upstream_record(
    *,
    task_id: str,
    benchmark: str,
    authority: str,
    repository: str,
    issue: Mapping[str, Any] | None,
    pull_request: Mapping[str, Any] | None,
    comments: Sequence[Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]],
    fix_commit: str,
    pinned_revision: str,
    touched_files: Sequence[str],
    patch_sha256: str,
    tests: Sequence[Mapping[str, Any]],
    retrieved_at: str,
    upstream_commits: Sequence[Mapping[str, Any]] = (),
    # Supplied only by the P2P-only track, where no fail-to-pass test exists.  The
    # declaration is what lets the record omit that evidence.
    f2p_absence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    expected_authority, expected_repository = UPSTREAM_REPOSITORIES.get(
        benchmark, (None, None)
    )
    if (authority, repository) != (expected_authority, expected_repository):
        raise ValueError("upstream authority/repository does not match benchmark")
    if not _FULL_SHA.fullmatch(fix_commit) or not _FULL_SHA.fullmatch(pinned_revision):
        raise ValueError("fix_commit and pinned_revision must be full SHAs")
    if not _SHA256.fullmatch(patch_sha256):
        raise ValueError("patch_sha256 must be a lowercase SHA-256")
    payload: dict[str, Any] = {
        "schema": "c1-lazarus-upstream-v1",
        "schema_version": 1,
        "task_id": str(task_id),
        "benchmark": benchmark,
        "authority": authority,
        "repository": repository,
        "retrieved_at": str(retrieved_at),
        "issue": _freeze_body_record(issue),
        "pull_request": _freeze_body_record(pull_request),
        "comments": [_freeze_body_record(value) for value in comments],
        "reviews": [_freeze_body_record(value) for value in reviews],
        "upstream_commits": [
            _freeze_body_record(value) for value in upstream_commits
        ],
        "fix_commit": fix_commit,
        "pinned_revision": pinned_revision,
        "touched_files": [str(value) for value in touched_files],
        "patch_sha256": patch_sha256,
        "tests": [copy.deepcopy(dict(value)) for value in tests],
    }
    if f2p_absence is not None:
        payload["f2p_absence"] = copy.deepcopy(dict(f2p_absence))
    errors = _validate_upstream_payload(payload, require_record_hash=False)
    if errors:
        raise ValueError("invalid upstream record: " + "; ".join(errors))
    payload["record_sha256"] = _canonical_sha256(payload)
    return payload


def validate_upstream_record(record: Mapping[str, Any]) -> list[str]:
    return _validate_upstream_payload(record, require_record_hash=True)


def validate_lazarus_trial_provenance(
    record: Mapping[str, Any],
    *,
    source_root: str | Path | None = None,
    harness_root: str | Path | None = None,
    frozen_test_root: str | Path | None = None,
) -> list[str]:
    """Enforce the experiment's stronger F2P/P2P provenance contract.

    A frozen conversation can describe a manual reproduction, but that is not
    an upstream regression test.  C1-Lazarus permits one narrow exception: an
    OpenRA *fail-to-pass* GUI differential explicitly labelled as such.  Every
    other construction and per-trial test must identify a committed upstream
    or repository test suite, including a content hash of its in-repository
    source.  This gate intentionally sits above the generic
    archival validator, which remains able to preserve historical dialogue for
    mining and audit purposes.
    """

    errors = validate_upstream_record(record)
    benchmark = str(record.get("benchmark") or "").strip().lower()
    root = Path(source_root).expanduser().resolve() if source_root is not None else None
    harness = (
        Path(harness_root).expanduser().resolve()
        if harness_root is not None
        else None
    )
    frozen_root = (
        Path(frozen_test_root).expanduser().resolve()
        if frozen_test_root is not None
        else None
    )
    if root is not None and not root.is_dir():
        errors.append("upstream test source root does not exist")
        root = None
    tests = record.get("tests")
    if not isinstance(tests, list):
        return errors
    for index, test in enumerate(tests):
        if not isinstance(test, Mapping):
            continue
        kind = str(test.get("kind") or "").strip()
        origin = str(test.get("origin") or "").strip()
        provenance = str(test.get("provenance") or "").strip().lower()
        label = f"tests[{index}]"
        if origin == "issue_derived_gui_differential":
            if benchmark != "openra" or kind != "fail_to_pass":
                errors.append(
                    f"{label} issue-derived GUI differential is permitted only "
                    "for OpenRA fail_to_pass"
                )
            continue
        if origin != "upstream":
            errors.append(f"{label} must declare origin=upstream")
            continue
        if not provenance or any(
            token in provenance
            for token in ("manual", "pull_request_body", "issue_body", "how_to_test")
        ):
            errors.append(
                f"{label} manual conversation steps cannot be claimed as an upstream test"
            )
            continue
        if not any(token in provenance for token in ("test", "suite", "repository")):
            errors.append(
                f"{label} upstream test provenance must identify a repository test or suite"
            )
        path_value = test.get("upstream_test_path")
        declared_sha = str(test.get("upstream_test_sha256") or "")
        source_url = str(test.get("source_url") or "")
        frozen_source_values = test.get("frozen_test_sources")
        if not isinstance(path_value, str) or not path_value.strip():
            errors.append(f"{label} upstream test path is missing")
            continue
        relative = Path(path_value)
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"{label} upstream test path escapes the source tree")
            continue
        if not _SHA256.fullmatch(declared_sha):
            errors.append(f"{label} upstream test content SHA is invalid")
        if not source_url.startswith("https://"):
            errors.append(f"{label} upstream test source URL is missing")
        argv = test.get("argv")
        argv = argv if isinstance(argv, list) else test.get("command")
        if isinstance(argv, list) and any(
            "{harness_root}/scripts/run_lazarus_probe.py" in str(part)
            for part in argv
        ):
            errors.append(
                f"{label} upstream test cannot dispatch through the Lazarus probe harness"
            )
        external_runner = (
            isinstance(argv, list)
            and bool(argv)
            and str(argv[0]).startswith("{harness_root}/")
        )
        if external_runner:
            runner_root_value = test.get("upstream_runner_root")
            runner_tree_sha = str(test.get("upstream_runner_tree_sha256") or "")
            if not isinstance(runner_root_value, str) or not runner_root_value.strip():
                errors.append(f"{label} external upstream runner root is missing")
            elif not _SHA256.fullmatch(runner_tree_sha):
                errors.append(f"{label} external upstream runner tree SHA is invalid")
            elif harness is None:
                errors.append(f"{label} external upstream runner requires a harness root")
            else:
                relative_runner_root = Path(runner_root_value)
                if relative_runner_root.is_absolute() or ".." in relative_runner_root.parts:
                    errors.append(f"{label} external upstream runner root escapes the harness")
                else:
                    runner_root = (harness / relative_runner_root).resolve()
                    runner_argv = Path(str(argv[0]).removeprefix("{harness_root}/"))
                    runner_path = (harness / runner_argv).resolve()
                    if harness != runner_root and harness not in runner_root.parents:
                        errors.append(
                            f"{label} external upstream runner root escapes the harness"
                        )
                    elif runner_root != runner_path and runner_root not in runner_path.parents:
                        errors.append(
                            f"{label} external upstream command escapes its runner root"
                        )
                    elif not runner_path.is_file():
                        errors.append(f"{label} external upstream runner is missing")
                    elif _runner_tree_sha256(runner_root) != runner_tree_sha:
                        errors.append(f"{label} runner tool tree SHA drift")
        absolute_runner = (
            isinstance(argv, list)
            and bool(argv)
            and Path(str(argv[0])).is_absolute()
        )
        if absolute_runner:
            runner_path = Path(str(argv[0])).expanduser()
            declared_runner_sha = str(
                test.get("upstream_runner_executable_sha256") or ""
            )
            version_argv = test.get("upstream_runner_version_argv")
            version_stdout_sha = str(
                test.get("upstream_runner_version_stdout_sha256") or ""
            )
            version_stderr_sha = str(
                test.get("upstream_runner_version_stderr_sha256") or ""
            )
            if not runner_path.is_file():
                errors.append(f"{label} absolute upstream runner is missing")
            elif not _SHA256.fullmatch(declared_runner_sha):
                errors.append(f"{label} runner executable SHA is invalid")
            elif hashlib.sha256(runner_path.read_bytes()).hexdigest() != declared_runner_sha:
                errors.append(f"{label} runner executable SHA drift")
            if (
                not isinstance(version_argv, list)
                or not version_argv
                or not all(isinstance(part, str) and part for part in version_argv)
                or str(version_argv[0]) != str(argv[0])
            ):
                errors.append(f"{label} runner version command is invalid")
            elif not _SHA256.fullmatch(version_stdout_sha) or not _SHA256.fullmatch(
                version_stderr_sha
            ):
                errors.append(f"{label} runner version output SHA is invalid")
            elif runner_path.is_file():
                try:
                    version_result = subprocess.run(
                        version_argv,
                        capture_output=True,
                        timeout=10,
                        shell=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    errors.append(f"{label} runner version command failed")
                else:
                    if version_result.returncode != 0:
                        errors.append(f"{label} runner version command failed")
                    if (
                        hashlib.sha256(version_result.stdout).hexdigest()
                        != version_stdout_sha
                        or hashlib.sha256(version_result.stderr).hexdigest()
                        != version_stderr_sha
                    ):
                        errors.append(f"{label} runner version identity drift")
        frozen_source_valid = False
        if frozen_source_values is not None:
            if not isinstance(frozen_source_values, list) or not frozen_source_values:
                errors.append(f"{label} frozen test sources are invalid")
            else:
                seen_frozen_destinations: set[str] = set()
                for frozen_index, frozen_source in enumerate(frozen_source_values):
                    frozen_label = f"{label}.frozen_test_sources[{frozen_index}]"
                    if not isinstance(frozen_source, Mapping):
                        errors.append(f"{frozen_label} must be a mapping")
                        continue
                    source_value = str(frozen_source.get("source_path") or "")
                    destination_value = str(
                        frozen_source.get("destination_path") or ""
                    )
                    frozen_sha = str(frozen_source.get("sha256") or "")
                    source_relative = Path(source_value)
                    destination_relative = Path(destination_value)
                    if (
                        not source_value
                        or source_relative.is_absolute()
                        or ".." in source_relative.parts
                    ):
                        errors.append(
                            f"{frozen_label} source escapes its evidence root"
                        )
                        continue
                    if (
                        not destination_value
                        or destination_relative.is_absolute()
                        or ".." in destination_relative.parts
                    ):
                        errors.append(
                            f"{frozen_label} destination escapes the source tree"
                        )
                        continue
                    if destination_value in seen_frozen_destinations:
                        errors.append(f"{frozen_label} destination is duplicated")
                    seen_frozen_destinations.add(destination_value)
                    if not _SHA256.fullmatch(frozen_sha):
                        errors.append(f"{frozen_label} content SHA is invalid")
                        continue
                    if frozen_root is None:
                        errors.append(f"{label} frozen test source root is missing")
                        continue
                    frozen_candidate = (frozen_root / source_relative).resolve()
                    if (
                        frozen_root != frozen_candidate
                        and frozen_root not in frozen_candidate.parents
                    ):
                        errors.append(
                            f"{frozen_label} source escapes its evidence root"
                        )
                    elif not frozen_candidate.is_file():
                        errors.append(f"{frozen_label} source is missing")
                    elif (
                        hashlib.sha256(frozen_candidate.read_bytes()).hexdigest()
                        != frozen_sha
                    ):
                        errors.append(f"{label} frozen test content SHA drift")
                    elif destination_value == relative.as_posix() and frozen_sha == declared_sha:
                        frozen_source_valid = True
                if not frozen_source_valid:
                    errors.append(
                        f"{label} primary upstream test is not bound by its frozen sources"
                    )
        if root is not None:
            candidate = (root / relative).resolve()
            if root != candidate and root not in candidate.parents:
                errors.append(f"{label} upstream test path escapes the source tree")
            elif not candidate.is_file():
                errors.append(f"{label} upstream test source file is missing")
            elif not frozen_source_valid and _SHA256.fullmatch(declared_sha) and (
                hashlib.sha256(candidate.read_bytes()).hexdigest() != declared_sha
            ):
                errors.append(f"{label} upstream test content SHA drift")
    return list(dict.fromkeys(errors))


def _runner_tree_sha256(root: Path) -> str:
    if not root.is_dir():
        return ""
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        return ""
    for path in files:
        resolved = path.resolve()
        if root != resolved and root not in resolved.parents:
            return ""
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_upstream_payload(
    record: Mapping[str, Any], *, require_record_hash: bool
) -> list[str]:
    errors: list[str] = []
    if record.get("schema") != "c1-lazarus-upstream-v1" or record.get(
        "schema_version"
    ) != 1:
        errors.append("upstream schema must be c1-lazarus-upstream-v1/schema_version 1")
    benchmark = record.get("benchmark")
    expected = UPSTREAM_REPOSITORIES.get(str(benchmark))
    if expected != (record.get("authority"), record.get("repository")):
        errors.append("upstream authority/repository mismatch")
    for name in ("fix_commit", "pinned_revision"):
        if not _FULL_SHA.fullmatch(str(record.get(name) or "")):
            errors.append(f"{name} must be a full SHA")
    if not _SHA256.fullmatch(str(record.get("patch_sha256") or "")):
        errors.append("patch_sha256 must be a lowercase SHA-256")

    conversation_count = 0
    for field in ("issue", "pull_request"):
        value = record.get(field)
        if value is not None:
            conversation_count += 1
            errors.extend(_validate_body_record(value, field))
    for field in ("comments", "reviews", "upstream_commits"):
        rows = record.get(field)
        if not isinstance(rows, list):
            errors.append(f"{field} must be a list")
            continue
        conversation_count += len(rows)
        for index, row in enumerate(rows):
            errors.extend(_validate_body_record(row, f"{field}[{index}]"))
    if not conversation_count:
        errors.append("authoritative conversation is empty")

    tests = record.get("tests")
    kinds: set[str] = set()
    ids: set[str] = set()
    if not isinstance(tests, list) or not tests:
        errors.append("tests must be a non-empty list")
    else:
        for index, test in enumerate(tests):
            if not isinstance(test, Mapping):
                errors.append(f"tests[{index}] must be a mapping")
                continue
            test_id = str(test.get("id") or "")
            if not test_id or test_id in ids:
                errors.append(f"tests[{index}] has a missing or duplicate id")
            ids.add(test_id)
            kind = str(test.get("kind") or "")
            if kind not in {"fail_to_pass", "pass_to_pass", "per_trial_p2p"}:
                errors.append(f"tests[{index}] has invalid kind")
            kinds.add(kind)
            command = test.get("command")
            if not isinstance(command, list) or not command or not all(
                isinstance(part, str) and part for part in command
            ):
                errors.append(f"tests[{index}] command must be a non-empty argv list")
            if not str(test.get("provenance") or ""):
                errors.append(f"tests[{index}] provenance is missing")
    # A record may omit fail-to-pass evidence only by stating why none exists.
    # Without that declaration the pair stays mandatory, so a record graded from
    # an F2P differential cannot lose it by dropping one list entry.
    absence = record.get("f2p_absence")
    declares_absence = isinstance(absence, Mapping) and bool(
        str(absence.get("reason") or "").strip()
    )
    if declares_absence and "fail_to_pass" in kinds:
        errors.append(
            "tests must not include fail_to_pass evidence when f2p_absence is "
            "declared"
        )
    required_kinds = {"pass_to_pass"} if declares_absence else {
        "fail_to_pass",
        "pass_to_pass",
    }
    if not required_kinds <= kinds:
        errors.append(
            "tests must include "
            + " and ".join(sorted(required_kinds))
            + " evidence"
        )

    if require_record_hash:
        declared = str(record.get("record_sha256") or "")
        unhashed = {key: copy.deepcopy(value) for key, value in record.items() if key != "record_sha256"}
        if declared != _canonical_sha256(unhashed):
            errors.append("record_sha256 mismatch")
    return errors


def _validate_body_record(value: object, label: str) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"{label} must be a mapping"]
    raw = value.get("body_raw")
    if not isinstance(raw, str):
        return [f"{label}.body_raw must be a string"]
    errors: list[str] = []
    if value.get("body_normalized") != _normalize_body(raw):
        errors.append(f"{label} body_normalized mismatch")
    if value.get("body_sha256") != _sha256_text(raw):
        errors.append(f"{label} body_sha256 mismatch")
    return errors


_EXPECTED_KEYS = frozenset(
    {
        "expected",
        "expected_value",
        "value",
        "equals",
        "min_count",
        "max_count",
        "count",
        "contains",
    }
)


def _group_expected_values(group: Any) -> set[str]:
    values: set[str] = set()

    def visit(value: Any, expected: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                visit(child, expected or str(key) in _EXPECTED_KEYS)
        elif isinstance(value, list):
            for child in value:
                visit(child, expected)
        elif expected and value is not None:
            values.add(str(value))

    visit(group)
    return values


def distinguishing_expected_values(spec: Mapping[str, Any]) -> set[str]:
    """Return the expectation values that separate the fixed run from the buggy run.

    A value that both ``goal_checks`` and ``defect_signature`` expect holds in
    either version, so quoting it publicly cannot reveal which version a task
    is running. Those shared values describe the registered start rather than
    the answer, and public preconditions need them to be specific enough for a
    checkpoint to be synthesized. Everything else stays coordinator-only.
    """

    lazarus = (
        (spec.get("evaluation") or {}).get("lazarus")
        if isinstance(spec.get("evaluation"), Mapping)
        else None
    )
    if not isinstance(lazarus, Mapping):
        raise ValueError("spec is missing evaluation.lazarus")
    goal = _group_expected_values(lazarus.get("goal_checks"))
    defect = _group_expected_values(lazarus.get("defect_signature"))
    invariant = _group_expected_values(lazarus.get("invariant_checks"))
    return (goal | defect | invariant) - (goal & defect)


def _hidden_values(spec: Mapping[str, Any]) -> list[str]:
    return sorted(distinguishing_expected_values(spec))


def verify_public_text_has_no_hidden_tokens(
    public_text: str,
    *,
    spec: Mapping[str, Any],
    touched_files: Sequence[str],
) -> None:
    hidden = _hidden_values(spec)
    locations: list[str] = []
    for path in touched_files:
        locations.extend((str(path), Path(str(path)).name))
    guard = TokenGuard(
        {
            "target": {
                "expected_values": hidden,
                "fix_locations": sorted(set(locations)),
            }
        }
    )
    result = guard.check_reverse(
        str(public_text),
        extra_forbidden=_VERSION_HINTS,
    )
    if not result.ok:
        details = result.leaked_mechanism + result.forbidden
        raise ValueError(
            "public text leaks coordinator-only token(s): " + ", ".join(details)
        )
