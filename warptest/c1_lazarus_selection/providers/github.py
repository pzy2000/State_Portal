"""Durable GitHub Issue and pull-request source capture for C1 v2."""

from __future__ import annotations

import http.client
import json
import os
import random
import re
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Callable, Mapping, Protocol
from urllib.parse import parse_qsl, urlparse

from ..models import CandidateKey
from ..resource_guard import require_write_capacity
from ..screening import SourceDecision
from ..stable_protocol import (
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_GITHUB_API_BASE = "https://api.github.com"
_GITHUB_ACCEPT = "application/vnd.github+json"
_GITHUB_DIFF_ACCEPT = "application/vnd.github.v3.diff"
_GITHUB_ATTACHMENT_ACCEPT = "application/octet-stream"
_RETRY_DELAYS = (2.0, 4.0, 8.0, 16.0)
_MAX_RETRIES = len(_RETRY_DELAYS)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_RESOURCE = re.compile(r"[a-z0-9][a-z0-9-]*")
_CAPTURE_EXHAUSTED_REASON = re.compile(
    r"github_(?P<resource>[a-z0-9][a-z0-9-]*)_capture_exhausted"
)
_CLOSING_CLAUSE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*"
    r"(?P<references>#[1-9][0-9]*(?:\s*,\s*#[1-9][0-9]*|"
    r"\s*,?\s+(?:and|&)\s+#[1-9][0-9]*)*)",
    re.IGNORECASE,
)
_ISSUE_REFERENCE = re.compile(r"#([1-9][0-9]*)\b")
_CLOSING_ISSUE_URL = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*"
    r"https://github\.com/(?P<repository>"
    r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*)/"
    r"issues/(?P<number>[1-9][0-9]*)\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https://[^\s<>()\[\]\"']+")
_SENSITIVE_QUERY_NAMES = frozenset(
    {
        "access_token",
        "auth",
        "authorization",
        "password",
        "signature",
        "sig",
        "token",
        "x-amz-credential",
        "x-amz-security-token",
        "x-amz-signature",
    }
)
_SENSITIVE_HEADER_NAMES = frozenset(
    {"authorization", "cookie", "proxy-authorization", "set-cookie"}
)
_GITHUB_ATTACHMENT_HOSTS = frozenset(
    {
        "github.com",
        "github-production-repository-file-5c1aeb.s3.amazonaws.com",
        "github-production-user-asset-6210df.s3.amazonaws.com",
        "objects.githubusercontent.com",
        "private-user-images.githubusercontent.com",
        "user-images.githubusercontent.com",
    }
)


@dataclass(frozen=True)
class GitHubRepository:
    """Public GitHub authority configuration for one selected repository."""

    repository_key: str
    owner: str
    name: str
    attachment_hosts: tuple[str, ...]
    issue_repository_aliases: tuple[str, ...] = ()
    api_base: str = _GITHUB_API_BASE

    def __post_init__(self) -> None:
        if not all(
            type(value) is str and value
            for value in (
                self.repository_key,
                self.owner,
                self.name,
                self.api_base,
            )
        ):
            raise ValueError("GitHub repository configuration is invalid")
        if not _NAME.fullmatch(self.owner) or not _NAME.fullmatch(self.name):
            raise ValueError("GitHub repository name is invalid")
        parsed = urlparse(self.api_base)
        if (
            self.api_base.rstrip("/") != _GITHUB_API_BASE
            or parsed.scheme != "https"
            or parsed.hostname != "api.github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("GitHub API authority is invalid")
        if type(self.attachment_hosts) is not tuple or not self.attachment_hosts:
            raise ValueError("GitHub attachment hosts are invalid")
        if any(
            type(host) is not str
            or not host
            or host.casefold() != host
            or host not in _GITHUB_ATTACHMENT_HOSTS
            for host in self.attachment_hosts
        ):
            raise ValueError("GitHub attachment host is invalid")
        if type(self.issue_repository_aliases) is not tuple or any(
            type(alias) is not str
            or alias != alias.casefold()
            or len(alias.split("/")) != 2
            or any(not _NAME.fullmatch(segment) for segment in alias.split("/"))
            for alias in self.issue_repository_aliases
        ):
            raise ValueError("GitHub issue repository alias is invalid")

    @property
    def api_repository_url(self) -> str:
        return f"{_GITHUB_API_BASE}/repos/{self.owner}/{self.name}"

    @property
    def repository_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class GitHubRequest:
    """A credential-free, canonical request record that can be persisted."""

    url: str
    accept: str

    def __post_init__(self) -> None:
        if type(self.url) is not str or type(self.accept) is not str:
            raise ValueError("GitHub request is invalid")
        parsed = urlparse(self.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(
                key.casefold() in _SENSITIVE_QUERY_NAMES
                for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
            )
        ):
            raise ValueError("GitHub request URL is unsafe")
        if self.accept not in {
            _GITHUB_ACCEPT,
            _GITHUB_ATTACHMENT_ACCEPT,
            _GITHUB_DIFF_ACCEPT,
        }:
            raise ValueError("GitHub request accept header is invalid")

    def to_value(self) -> dict[str, str]:
        return {"method": "GET", "url": self.url, "accept": self.accept}


@dataclass(frozen=True)
class GitHubResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]

    def __post_init__(self) -> None:
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise ValueError("GitHub response status is invalid")
        if type(self.body) is not bytes:
            raise ValueError("GitHub response body is invalid")
        if type(self.headers) is not dict or any(
            type(key) is not str or type(value) is not str
            for key, value in self.headers.items()
        ):
            raise ValueError("GitHub response headers are invalid")


class GitHubTransport(Protocol):
    def execute(self, request: GitHubRequest) -> GitHubResponse: ...


class UrllibGitHubTransport:
    """Production transport. Its optional token never enters evidence records."""

    def __init__(self, *, token: str | None = None, timeout_seconds: float = 45.0):
        if token is not None and (type(token) is not str or not token):
            raise ValueError("GitHub token is invalid")
        if type(timeout_seconds) is not float or timeout_seconds <= 0:
            raise ValueError("GitHub timeout is invalid")
        self._token = token
        self._timeout_seconds = timeout_seconds

    def execute(self, request: GitHubRequest) -> GitHubResponse:
        parsed = urlparse(request.url)
        headers = {
            "Accept": request.accept,
            "User-Agent": "warptest-c1/2",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token and parsed.hostname == "api.github.com":
            headers["Authorization"] = f"Bearer {self._token}"
        raw_request = urllib.request.Request(request.url, headers=headers)
        try:
            with urllib.request.urlopen(
                raw_request, timeout=self._timeout_seconds
            ) as response:
                return GitHubResponse(
                    status=response.status,
                    body=response.read(),
                    headers={key: value for key, value in response.headers.items()},
                )
        except urllib.error.HTTPError as error:
            return GitHubResponse(
                status=error.code,
                body=error.read(),
                headers={key: value for key, value in error.headers.items()},
            )


_GITHUB_REPOSITORIES = MappingProxyType(
    {
        "luanti": GitHubRepository(
            repository_key="luanti",
            owner="luanti-org",
            name="luanti",
            attachment_hosts=(
                "github.com",
                "private-user-images.githubusercontent.com",
                "user-images.githubusercontent.com",
            ),
            issue_repository_aliases=("minetest/minetest",),
        ),
        "jupyterlab": GitHubRepository(
            repository_key="jupyterlab",
            owner="jupyterlab",
            name="jupyterlab",
            attachment_hosts=(
                "github.com",
                "private-user-images.githubusercontent.com",
                "user-images.githubusercontent.com",
            ),
        ),
        "openra": GitHubRepository(
            repository_key="openra",
            owner="OpenRA",
            name="OpenRA",
            attachment_hosts=(
                "github.com",
                "private-user-images.githubusercontent.com",
                "user-images.githubusercontent.com",
            ),
        ),
    }
)


class _CaptureBlocked(RuntimeError):
    pass


def github_repository_for(repository_key: str) -> GitHubRepository:
    """Return a registered public-GitHub configuration, never a Gitea substitute."""

    if type(repository_key) is not str:
        raise ValueError("GitHub repository key is invalid")
    try:
        return _GITHUB_REPOSITORIES[repository_key]
    except KeyError as error:
        raise ValueError("GitHub repository is not registered") from error


def validate_github_source_workers(value: int) -> int:
    """GitHub source capture is deliberately capped below Gitea's allowance."""

    if type(value) is not int or not 1 <= value <= 8:
        raise ValueError("GitHub source workers must be between 1 and 8")
    return value


def mark_github_rate_limit_checkpoint_for_redo(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Reopen exactly one v2 checkpoint proven to have hit GitHub core quota."""

    if authorization.scope != "candidate-screening":
        raise PermissionError("GitHub screening recovery authorization is invalid")
    root = _safe_root(Path(artifact_root))
    checkpoint_id = (
        f"screening_checkpoint/{candidate.repository_key}/"
        f"{candidate.change_number}/current/v2"
    )
    checkpoint = load_stable_artifact(
        root,
        artifact_id=checkpoint_id,
        artifact_kind="screening_checkpoint",
        semantic_version="2.0",
    )
    if (
        checkpoint.payload.get("candidate") != candidate.to_value()
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "infrastructure_blocked"
        or len(checkpoint.inputs) != 2
    ):
        raise RuntimeError("GitHub rate-limit checkpoint is not recoverable")
    stage = checkpoint.inputs[1]
    if (
        stage.artifact_kind != "candidate_source_terminal"
        or checkpoint.payload.get("stage_artifact_id") != stage.artifact_id
    ):
        raise RuntimeError("GitHub rate-limit terminal binding is invalid")
    terminal = load_stable_artifact(
        root,
        artifact_id=stage.artifact_id,
        artifact_kind=stage.artifact_kind,
        semantic_version=stage.semantic_version,
    )
    reason = terminal.payload.get("reason")
    match = _CAPTURE_EXHAUSTED_REASON.fullmatch(reason) if type(reason) is str else None
    if (
        match is None
        or terminal.payload.get("candidate") != candidate.to_value()
        or terminal.payload.get("outcome") != "infrastructure_blocked"
        or terminal.payload.get("complete") is not True
        or terminal.inputs != (checkpoint.inputs[0],)
    ):
        raise RuntimeError("GitHub rate-limit terminal is not recoverable")
    resource = match.group("resource")
    attempt = _last_source_attempt(root, candidate, resource)
    if attempt is None or attempt.payload.get("status") != 403:
        raise RuntimeError("GitHub rate-limit attempt evidence is missing")
    receipt_path = attempt.payload.get("request_receipt_path")
    if type(receipt_path) is not str:
        raise RuntimeError("GitHub rate-limit receipt binding is invalid")
    receipt = _strict_json(
        _safe_evidence_file(root, receipt_path).read_bytes(), "rate-limit receipt"
    )
    if type(receipt) is not dict or receipt.get("status") != 403:
        raise RuntimeError("GitHub rate-limit receipt is invalid")
    headers = receipt.get("headers")
    if type(headers) is not dict or not any(
        type(key) is str
        and type(value) is str
        and key.casefold() == "x-ratelimit-remaining"
        and value.strip() == "0"
        for key, value in headers.items()
    ):
        raise RuntimeError("GitHub rate-limit exhaustion is not proven")

    created_at = _now()
    write_stable_artifact(
        root,
        artifact_id=checkpoint_id,
        artifact_kind="screening_checkpoint",
        semantic_version="2.0",
        created_at=created_at,
        inputs=checkpoint.inputs,
        payload={
            "candidate": candidate.to_value(),
            "state": "redo_required",
            "prior_outcome": "infrastructure_blocked",
            "stage_artifact_id": stage.artifact_id,
            "reason": "github_core_rate_limit_reset",
        },
        authorization=authorization,
    )
    return load_stable_artifact(
        root,
        artifact_id=checkpoint_id,
        artifact_kind="screening_checkpoint",
        semantic_version="2.0",
    )


def mark_github_issue_link_checkpoint_for_redo(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Reopen a false missing-link terminal proven by a retained closing URL."""

    if authorization.scope != "candidate-screening":
        raise PermissionError("GitHub screening recovery authorization is invalid")
    repository = github_repository_for(candidate.repository_key)
    root = _safe_root(Path(artifact_root))
    checkpoint_id = (
        f"screening_checkpoint/{candidate.repository_key}/"
        f"{candidate.change_number}/current/v2"
    )
    checkpoint = load_stable_artifact(
        root,
        artifact_id=checkpoint_id,
        artifact_kind="screening_checkpoint",
        semantic_version="2.0",
    )
    if (
        checkpoint.payload.get("candidate") != candidate.to_value()
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("outcome") != "source_rejected"
        or len(checkpoint.inputs) != 2
    ):
        raise RuntimeError("GitHub issue-link checkpoint is not recoverable")
    terminal_reference = checkpoint.inputs[1]
    if (
        terminal_reference.artifact_kind != "candidate_source_terminal"
        or terminal_reference.semantic_version != "2.0"
        or checkpoint.payload.get("stage_artifact_id") != terminal_reference.artifact_id
    ):
        raise RuntimeError("GitHub issue-link terminal binding is invalid")
    terminal = load_stable_artifact(
        root,
        artifact_id=terminal_reference.artifact_id,
        artifact_kind=terminal_reference.artifact_kind,
        semantic_version=terminal_reference.semantic_version,
    )
    if terminal.inputs != (checkpoint.inputs[0],) or terminal.payload != {
        "candidate": candidate.to_value(),
        "outcome": "source_rejected",
        "reason": "authoritative_issue_link_missing",
        "complete": True,
    }:
        raise RuntimeError("GitHub issue-link terminal is not recoverable")
    pull_attempt = _last_source_attempt(root, candidate, "pull")
    if (
        pull_attempt is None
        or pull_attempt.payload.get("candidate") != candidate.to_value()
        or pull_attempt.payload.get("provider") != "github"
        or pull_attempt.payload.get("resource") != "pull"
        or pull_attempt.payload.get("state") != "succeeded"
        or pull_attempt.payload.get("status") != 200
    ):
        raise RuntimeError("GitHub issue-link pull evidence is missing")
    raw_path = pull_attempt.payload.get("raw_evidence_path")
    if type(raw_path) is not str:
        raise RuntimeError("GitHub issue-link pull evidence binding is invalid")
    pull = _strict_json(
        _safe_evidence_file(root, raw_path).read_bytes(), "GitHub pull evidence"
    )
    if (
        type(pull) is not dict
        or pull.get("number") != candidate.change_number
        or not _closing_issue_url_numbers(pull.get("body"), repository)
    ):
        raise RuntimeError("GitHub closing issue URL is not proven")

    write_stable_artifact(
        root,
        artifact_id=checkpoint_id,
        artifact_kind="screening_checkpoint",
        semantic_version="2.0",
        created_at=_now(),
        inputs=checkpoint.inputs,
        payload={
            "candidate": candidate.to_value(),
            "state": "redo_required",
            "prior_outcome": "source_rejected",
            "stage_artifact_id": terminal_reference.artifact_id,
            "reason": "github_closing_issue_url_parser_updated",
        },
        authorization=authorization,
    )
    return load_stable_artifact(
        root,
        artifact_id=checkpoint_id,
        artifact_kind="screening_checkpoint",
        semantic_version="2.0",
    )


def capture_github_source(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    repository: GitHubRepository,
    transport: GitHubTransport,
    authorization: AuthorizationRecord,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
) -> SourceDecision:
    """Capture one complete GitHub public source packet before any parsing."""

    if candidate.repository_key != repository.repository_key:
        raise ValueError("GitHub candidate repository does not match configuration")
    root = _safe_root(Path(artifact_root))
    raw_paths: list[str] = []
    receipt_paths: list[str] = []
    try:
        pull, pull_path, pull_receipt = _request_json(
            root,
            candidate=candidate,
            resource="pull",
            request=GitHubRequest(
                url=f"{repository.api_repository_url}/pulls/{candidate.change_number}",
                accept=_GITHUB_ACCEPT,
            ),
            transport=transport,
            authorization=authorization,
            sleep=sleep,
            jitter=jitter,
        )
        raw_paths.append(pull_path)
        receipt_paths.append(pull_receipt)
        if type(pull) is not dict or pull.get("number") != candidate.change_number:
            raise _CaptureBlocked("github_pull_identity_invalid")
        if pull.get("merged") is not True:
            raise _CaptureBlocked("github_pull_not_merged")

        patch_path, patch_receipt = _request_patch(
            root,
            candidate=candidate,
            repository=repository,
            transport=transport,
            authorization=authorization,
            sleep=sleep,
            jitter=jitter,
        )
        raw_paths.append(patch_path)
        receipt_paths.append(patch_receipt)

        relationships, relationship_paths, relationship_receipts = _paged_json_list(
            root,
            candidate=candidate,
            resource="relationships",
            url_base=(
                f"{repository.api_repository_url}/issues/{candidate.change_number}/"
                "timeline"
            ),
            transport=transport,
            authorization=authorization,
            sleep=sleep,
            jitter=jitter,
        )
        raw_paths.extend(relationship_paths)
        receipt_paths.extend(relationship_receipts)
        issue_numbers = _issue_numbers(pull, relationships, repository)
        if not issue_numbers:
            return SourceDecision.rejected("authoritative_issue_link_missing")

        issue_values: list[dict[str, object]] = []
        attachment_urls = _attachment_urls(pull, repository)
        for issue_number in issue_numbers:
            issue, issue_path, issue_receipt = _request_json(
                root,
                candidate=candidate,
                resource=f"issue-{issue_number}",
                request=GitHubRequest(
                    url=f"{repository.api_repository_url}/issues/{issue_number}",
                    accept=_GITHUB_ACCEPT,
                ),
                transport=transport,
                authorization=authorization,
                sleep=sleep,
                jitter=jitter,
            )
            raw_paths.append(issue_path)
            receipt_paths.append(issue_receipt)
            if type(issue) is not dict or issue.get("number") != issue_number:
                raise _CaptureBlocked("github_issue_identity_invalid")
            if type(issue.get("pull_request")) is dict:
                raise _CaptureBlocked("github_authoritative_reference_is_not_an_issue")
            comments, comment_paths, comment_receipts = _paged_json_list(
                root,
                candidate=candidate,
                resource=f"issue-{issue_number}-comments",
                url_base=(
                    f"{repository.api_repository_url}/issues/{issue_number}/comments"
                ),
                transport=transport,
                authorization=authorization,
                sleep=sleep,
                jitter=jitter,
            )
            history, history_paths, history_receipts = _paged_json_list(
                root,
                candidate=candidate,
                resource=f"issue-{issue_number}-history",
                url_base=(
                    f"{repository.api_repository_url}/issues/{issue_number}/timeline"
                ),
                transport=transport,
                authorization=authorization,
                sleep=sleep,
                jitter=jitter,
            )
            raw_paths.extend(comment_paths)
            raw_paths.extend(history_paths)
            receipt_paths.extend(comment_receipts)
            receipt_paths.extend(history_receipts)
            attachment_urls.extend(_attachment_urls(issue, repository))
            for value in (*comments, *history):
                attachment_urls.extend(_attachment_urls(value, repository))
            issue_values.append(
                {
                    "number": issue_number,
                    "issue": issue,
                    "comments": comments,
                    "history": history,
                }
            )

        attachment_paths: list[str] = []
        for sequence, url in enumerate(dict.fromkeys(attachment_urls), start=1):
            body, raw_path, receipt_path = _request_bytes(
                root,
                candidate=candidate,
                resource=f"attachment-{sequence}",
                request=GitHubRequest(url=url, accept=_GITHUB_ATTACHMENT_ACCEPT),
                transport=transport,
                authorization=authorization,
                sleep=sleep,
                jitter=jitter,
            )
            if not body:
                raise _CaptureBlocked("github_attachment_empty")
            raw_paths.append(raw_path)
            receipt_paths.append(receipt_path)
            attachment_paths.append(raw_path)

        manifest = {
            "candidate": candidate.to_value(),
            "provider": "github",
            "authority": {
                "provider": "github",
                "api_base": _GITHUB_API_BASE,
                "repository": repository.repository_name,
            },
            "pull": pull,
            "relationships": relationships,
            "issue_numbers": list(issue_numbers),
            "issues": issue_values,
            "raw_paths": raw_paths,
            "request_receipt_paths": receipt_paths,
            "patch_raw_path": patch_path,
            "attachment_paths": attachment_paths,
            "complete": True,
        }
        manifest_path = _write_raw(
            root,
            candidate,
            "source-manifest",
            1,
            _canonical_json(manifest),
            extension="json",
            created_at=_now(),
        )
        return SourceDecision.accepted(raw_evidence_path=manifest_path)
    except _CaptureBlocked as error:
        return SourceDecision.blocked(str(error))


def _request_patch(
    root: Path,
    *,
    candidate: CandidateKey,
    repository: GitHubRepository,
    transport: GitHubTransport,
    authorization: AuthorizationRecord,
    sleep: Callable[[float], None],
    jitter: Callable[[], float],
) -> tuple[str, str]:
    body, raw_path, receipt_path = _request_bytes(
        root,
        candidate=candidate,
        resource="pull-diff",
        request=GitHubRequest(
            url=f"{repository.api_repository_url}/pulls/{candidate.change_number}.diff",
            accept=_GITHUB_DIFF_ACCEPT,
        ),
        transport=transport,
        authorization=authorization,
        sleep=sleep,
        jitter=jitter,
    )
    if not body:
        raise _CaptureBlocked("github_pull_diff_empty")
    return raw_path, receipt_path


def _request_json(
    root: Path,
    *,
    candidate: CandidateKey,
    resource: str,
    request: GitHubRequest,
    transport: GitHubTransport,
    authorization: AuthorizationRecord,
    sleep: Callable[[float], None],
    jitter: Callable[[], float],
) -> tuple[object, str, str]:
    body, raw_path, receipt_path = _request_bytes(
        root,
        candidate=candidate,
        resource=resource,
        request=request,
        transport=transport,
        authorization=authorization,
        sleep=sleep,
        jitter=jitter,
    )
    try:
        return _strict_json(body, resource), raw_path, receipt_path
    except RuntimeError as error:
        raise _CaptureBlocked(f"github_{resource}_json_invalid") from error


def _request_bytes(
    root: Path,
    *,
    candidate: CandidateKey,
    resource: str,
    request: GitHubRequest,
    transport: GitHubTransport,
    authorization: AuthorizationRecord,
    sleep: Callable[[float], None],
    jitter: Callable[[], float],
) -> tuple[bytes, str, str]:
    """Persist the credential-free request and raw response before parsing it."""

    if not _RESOURCE.fullmatch(resource):
        raise ValueError("GitHub resource name is invalid")
    first_attempt = _next_attempt_number(root, candidate, resource)
    for offset in range(_MAX_RETRIES + 1):
        attempt = first_attempt + offset
        created_at = _now()
        request_path = _write_raw(
            root,
            candidate,
            resource,
            attempt,
            _canonical_json(request.to_value()),
            extension="request.json",
            created_at=created_at,
        )
        try:
            response = transport.execute(request)
        except (TimeoutError, OSError, http.client.HTTPException) as error:
            receipt_path = _write_receipt(
                root,
                candidate=candidate,
                resource=resource,
                attempt=attempt,
                created_at=created_at,
                request=request,
                request_path=request_path,
                response_path=None,
                status=None,
                headers={},
                failure_type=type(error).__name__,
            )
            _write_attempt(
                root,
                candidate=candidate,
                resource=resource,
                attempt=attempt,
                created_at=created_at,
                request=request,
                state="timeout" if isinstance(error, TimeoutError) else "failed",
                status=None,
                raw_path=None,
                receipt_path=receipt_path,
                failure_type=type(error).__name__,
                authorization=authorization,
            )
            if offset == _MAX_RETRIES:
                raise _CaptureBlocked(f"github_{resource}_capture_exhausted") from error
            sleep(_backoff(offset + 1, jitter))
            continue

        raw_path = _write_raw(
            root,
            candidate,
            resource,
            attempt,
            response.body,
            extension="response.body",
            created_at=created_at,
        )
        receipt_path = _write_receipt(
            root,
            candidate=candidate,
            resource=resource,
            attempt=attempt,
            created_at=created_at,
            request=request,
            request_path=request_path,
            response_path=raw_path,
            status=response.status,
            headers=_safe_headers(response.headers),
            failure_type=None,
        )
        retryable = _retryable_response(response)
        succeeded = 200 <= response.status < 300
        _write_attempt(
            root,
            candidate=candidate,
            resource=resource,
            attempt=attempt,
            created_at=created_at,
            request=request,
            state="succeeded" if succeeded else "retryable" if retryable else "failed",
            status=response.status,
            raw_path=raw_path,
            receipt_path=receipt_path,
            failure_type=None,
            authorization=authorization,
        )
        if succeeded:
            return response.body, raw_path, receipt_path
        if not retryable or offset == _MAX_RETRIES:
            raise _CaptureBlocked(f"github_{resource}_capture_exhausted")
        sleep(_backoff(offset + 1, jitter))
    raise AssertionError("unreachable GitHub request state")


def _retryable_response(response: GitHubResponse) -> bool:
    """Recognize GitHub's two documented rate-limit response shapes."""

    if response.status == 429 or response.status >= 500:
        return True
    if response.status != 403:
        return False
    return any(
        key.casefold() == "x-ratelimit-remaining" and value.strip() == "0"
        for key, value in response.headers.items()
    )


def _next_attempt_number(root: Path, candidate: CandidateKey, resource: str) -> int:
    """Append after prior attempts so a recovery never replaces failed evidence."""

    for attempt in range(1, 10_001):
        try:
            load_stable_artifact(
                root,
                artifact_id=(
                    f"source_attempt/{candidate.repository_key}/"
                    f"{candidate.change_number}/{resource}/attempt-{attempt}/v2"
                ),
                artifact_kind="source_attempt",
                semantic_version="2.0",
            )
        except RuntimeError as error:
            if str(error) in {
                "artifact directory is missing",
                "artifact current head is missing",
            }:
                return attempt
            raise
    raise RuntimeError("GitHub source attempt sequence is unbounded")


def _last_source_attempt(
    root: Path, candidate: CandidateKey, resource: str
) -> StableArtifact | None:
    last: StableArtifact | None = None
    for attempt in range(1, 10_001):
        try:
            current = load_stable_artifact(
                root,
                artifact_id=(
                    f"source_attempt/{candidate.repository_key}/"
                    f"{candidate.change_number}/{resource}/attempt-{attempt}/v2"
                ),
                artifact_kind="source_attempt",
                semantic_version="2.0",
            )
        except RuntimeError as error:
            if str(error) in {
                "artifact directory is missing",
                "artifact current head is missing",
            }:
                return last
            raise
        if (
            current.payload.get("candidate") != candidate.to_value()
            or current.payload.get("provider") != "github"
            or current.payload.get("resource") != resource
            or current.payload.get("attempt") != attempt
        ):
            raise RuntimeError("GitHub source attempt sequence is invalid")
        last = current
    raise RuntimeError("GitHub source attempt sequence is unbounded")


def _safe_evidence_file(root: Path, value: str) -> Path:
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise RuntimeError("GitHub evidence path is invalid")
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise RuntimeError("GitHub evidence file is missing") from error
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("GitHub evidence path contains a symlink")
    if not stat.S_ISREG(current.lstat().st_mode):
        raise RuntimeError("GitHub evidence path is not a regular file")
    return current


def _paged_json_list(
    root: Path,
    *,
    candidate: CandidateKey,
    resource: str,
    url_base: str,
    transport: GitHubTransport,
    authorization: AuthorizationRecord,
    sleep: Callable[[float], None],
    jitter: Callable[[], float],
) -> tuple[list[object], list[str], list[str]]:
    rows: list[object] = []
    paths: list[str] = []
    receipts: list[str] = []
    for page in range(1, 10001):
        value, path, receipt = _request_json(
            root,
            candidate=candidate,
            resource=f"{resource}-page-{page}",
            request=GitHubRequest(
                url=f"{url_base}?per_page=100&page={page}",
                accept=_GITHUB_ACCEPT,
            ),
            transport=transport,
            authorization=authorization,
            sleep=sleep,
            jitter=jitter,
        )
        paths.append(path)
        receipts.append(receipt)
        if type(value) is not list:
            raise _CaptureBlocked(f"github_{resource}_page_invalid")
        if not value or len(value) < 100:
            return rows + value, paths, receipts
        rows.extend(value)
    raise _CaptureBlocked(f"github_{resource}_pagination_unbounded")


def _issue_numbers(
    pull: dict[str, object],
    rows: list[object],
    repository: GitHubRepository,
) -> tuple[int, ...]:
    return tuple(
        sorted(
            {
                *_explicit_closing_issue_numbers(pull, repository),
                *_same_repository_cross_reference_issue_numbers(rows, repository),
            }
        )
    )


def _explicit_closing_issue_numbers(
    pull: dict[str, object],
    repository: GitHubRepository,
) -> tuple[int, ...]:
    numbers: set[int] = set()
    body = pull.get("body")
    if type(body) is str:
        for match in _CLOSING_CLAUSE.finditer(body):
            numbers.update(
                int(value) for value in _ISSUE_REFERENCE.findall(match["references"])
            )
        numbers.update(_closing_issue_url_numbers(body, repository))
    return tuple(sorted(numbers))


def _same_repository_cross_reference_issue_numbers(
    rows: list[object],
    repository: GitHubRepository,
) -> tuple[int, ...]:
    numbers: set[int] = set()
    for row in rows:
        if type(row) is not dict or row.get("event") != "cross-referenced":
            continue
        source = row.get("source")
        issue = source.get("issue") if type(source) is dict else None
        if type(issue) is not dict:
            continue
        number = issue.get("number")
        if (
            type(number) is int
            and number > 0
            and issue.get("repository_url") == repository.api_repository_url
            and not isinstance(issue.get("pull_request"), dict)
        ):
            numbers.add(number)
    return tuple(sorted(numbers))


def _closing_issue_url_numbers(
    body: object,
    repository: GitHubRepository,
) -> tuple[int, ...]:
    if type(body) is not str:
        return ()
    permitted_repositories = {
        repository.repository_name.casefold(),
        *repository.issue_repository_aliases,
    }
    return tuple(
        sorted(
            {
                int(match["number"])
                for match in _CLOSING_ISSUE_URL.finditer(body)
                if match["repository"].casefold() in permitted_repositories
            }
        )
    )


def _attachment_urls(value: object, repository: GitHubRepository) -> list[str]:
    if type(value) is not dict:
        return []
    body = value.get("body")
    if type(body) is not str:
        return []
    urls: list[str] = []
    for raw_url in _URL.findall(body):
        url = raw_url.rstrip(".,;:!?")
        parsed = urlparse(url)
        if not _looks_like_attachment(parsed, repository):
            continue
        if not _safe_attachment_url(parsed, repository):
            raise _CaptureBlocked("github_attachment_url_invalid")
        urls.append(url)
    return urls


def _looks_like_attachment(parsed, repository: GitHubRepository) -> bool:
    host = parsed.hostname.casefold() if parsed.hostname else ""
    path = parsed.path
    return bool(
        host in _GITHUB_ATTACHMENT_HOSTS
        and (
            host
            in {
                "private-user-images.githubusercontent.com",
                "user-images.githubusercontent.com",
            }
            or path.startswith("/user-attachments/assets/")
            or path.startswith(f"/{repository.owner}/{repository.name}/files/")
        )
    )


def _safe_attachment_url(parsed, repository: GitHubRepository) -> bool:
    return bool(
        parsed.scheme == "https"
        and parsed.hostname in repository.attachment_hosts
        and parsed.username is None
        and parsed.password is None
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


def _write_attempt(
    root: Path,
    *,
    candidate: CandidateKey,
    resource: str,
    attempt: int,
    created_at: str,
    request: GitHubRequest,
    state: str,
    status: int | None,
    raw_path: str | None,
    receipt_path: str,
    failure_type: str | None,
    authorization: AuthorizationRecord,
) -> None:
    write_stable_artifact(
        root,
        artifact_id=(
            f"source_attempt/{candidate.repository_key}/{candidate.change_number}/"
            f"{resource}/attempt-{attempt}/v2"
        ),
        artifact_kind="source_attempt",
        semantic_version="2.0",
        created_at=created_at,
        inputs=(),
        payload={
            "candidate": candidate.to_value(),
            "provider": "github",
            "resource": resource,
            "attempt": attempt,
            "canonical_url": request.url,
            "state": state,
            "status": status,
            "raw_evidence_path": raw_path,
            "request_receipt_path": receipt_path,
            "failure_type": failure_type,
        },
        authorization=authorization,
    )


def _write_receipt(
    root: Path,
    *,
    candidate: CandidateKey,
    resource: str,
    attempt: int,
    created_at: str,
    request: GitHubRequest,
    request_path: str,
    response_path: str | None,
    status: int | None,
    headers: dict[str, str],
    failure_type: str | None,
) -> str:
    return _write_raw(
        root,
        candidate,
        resource,
        attempt,
        _canonical_json(
            {
                "provider": "github",
                "canonical_url": request.url,
                "request_raw_path": request_path,
                "response_raw_path": response_path,
                "status": status,
                "headers": headers,
                "failure_type": failure_type,
            }
        ),
        extension="receipt.json",
        created_at=created_at,
    )


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in sorted(headers.items())
        if key.casefold() not in _SENSITIVE_HEADER_NAMES
    }


def _backoff(attempt: int, jitter: Callable[[], float]) -> float:
    return _RETRY_DELAYS[attempt - 1] + max(0.0, min(jitter(), 1.0))


def _write_raw(
    root: Path,
    candidate: CandidateKey,
    resource: str,
    attempt: int,
    content: bytes,
    *,
    extension: str,
    created_at: str,
) -> str:
    require_write_capacity(root)
    relative = Path(
        "raw",
        "github",
        candidate.repository_key,
        str(candidate.change_number),
        resource,
        f"attempt-{attempt}",
        created_at.replace(":", "-") + f".{extension}",
    )
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.resolve(strict=True)
    if root.resolve(strict=True) not in parent.parents:
        raise RuntimeError("GitHub raw path escapes the artifact root")
    for ancestor in (path.parent, *path.parent.parents):
        if ancestor == root.parent:
            break
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("GitHub raw path contains a symlink")
        if ancestor == root:
            break
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RuntimeError("GitHub raw evidence path is unsafe")
        if path.read_bytes() != content:
            raise RuntimeError("GitHub raw evidence conflicts with an existing attempt")
        return relative.as_posix()
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return relative.as_posix()


def _safe_root(root: Path) -> Path:
    root = root.absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("GitHub artifact root is unsafe")
    return root.resolve(strict=True)


def _strict_json(content: bytes, label: str) -> object:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise RuntimeError(f"GitHub {label} contains duplicate keys")
            result[key] = value
        return result

    try:
        return json.loads(
            content.decode(),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                RuntimeError(f"GitHub {label} contains a non-finite number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"GitHub {label} is not JSON") from error


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "GitHubRepository",
    "GitHubRequest",
    "GitHubResponse",
    "GitHubTransport",
    "UrllibGitHubTransport",
    "capture_github_source",
    "github_repository_for",
    "mark_github_issue_link_checkpoint_for_redo",
    "mark_github_rate_limit_checkpoint_for_redo",
    "validate_github_source_workers",
]
