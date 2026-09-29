"""Durable Gitea Issue and pull-request source capture."""

from __future__ import annotations

import json
import http.client
import os
import random
import re
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib.parse import urlparse

from ..models import CandidateKey
from ..resource_guard import require_write_capacity
from ..screening import SourceDecision
from ..stable_protocol import AuthorizationRecord, write_stable_artifact


_LINK = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#([1-9][0-9]*)\b",
    re.IGNORECASE,
)
_MARKDOWN_URL = re.compile(r"\[[^\]]*\]\((https://[^)\s]+)\)")
_RETRY_DELAYS = (2.0, 4.0, 8.0, 16.0)


@dataclass(frozen=True)
class GiteaRepository:
    repository_key: str
    api_base: str
    owner: str
    name: str
    attachment_hosts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not all(
            type(value) is str and value
            for value in (
                self.repository_key,
                self.api_base,
                self.owner,
                self.name,
            )
        ):
            raise ValueError("Gitea repository configuration is invalid")
        parsed = urlparse(self.api_base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Gitea API base is unsafe")
        if type(self.attachment_hosts) is not tuple or not self.attachment_hosts:
            raise ValueError("Gitea attachment hosts are invalid")
        if any(type(host) is not str or not host for host in self.attachment_hosts):
            raise ValueError("Gitea attachment host is invalid")


@dataclass(frozen=True)
class GiteaResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]

    def __post_init__(self) -> None:
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise ValueError("Gitea response status is invalid")
        if type(self.body) is not bytes:
            raise ValueError("Gitea response body is invalid")
        if type(self.headers) is not dict or any(
            type(key) is not str or type(value) is not str
            for key, value in self.headers.items()
        ):
            raise ValueError("Gitea response headers are invalid")


class GiteaTransport(Protocol):
    def execute(self, url: str) -> GiteaResponse: ...


class UrllibGiteaTransport:
    """Production HTTP transport whose credentials never enter artifacts."""

    def __init__(self, *, token: str | None = None, timeout_seconds: float = 45.0):
        self._token = token
        self._timeout_seconds = timeout_seconds

    def execute(self, url: str) -> GiteaResponse:
        headers = {"Accept": "application/json", "User-Agent": "warptest-c1/2"}
        if self._token:
            headers["Authorization"] = f"token {self._token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_seconds
            ) as response:
                return GiteaResponse(
                    status=response.status,
                    body=response.read(),
                    headers={key: value for key, value in response.headers.items()},
                )
        except urllib.error.HTTPError as error:
            return GiteaResponse(
                status=error.code,
                body=error.read(),
                headers={key: value for key, value in error.headers.items()},
            )


class _CaptureBlocked(RuntimeError):
    pass


def capture_gitea_source(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    repository: GiteaRepository,
    transport: GiteaTransport,
    authorization: AuthorizationRecord,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
) -> SourceDecision:
    """Capture one complete Gitea public source packet."""

    if candidate.repository_key != repository.repository_key:
        raise ValueError("Gitea candidate repository does not match configuration")
    root = Path(artifact_root).resolve()
    prefix = (
        f"{repository.api_base.rstrip('/')}/repos/{repository.owner}/{repository.name}"
    )
    raw_paths: list[str] = []
    try:
        pull, pull_path = _request_json(
            root,
            candidate=candidate,
            resource="pull",
            url=f"{prefix}/pulls/{candidate.change_number}",
            transport=transport,
            authorization=authorization,
            sleep=sleep,
            jitter=jitter,
        )
        raw_paths.append(pull_path)
        if type(pull) is not dict or pull.get("number") != candidate.change_number:
            raise _CaptureBlocked("gitea_pull_identity_invalid")
        diff_url = pull.get("diff_url")
        if type(diff_url) is not str or not diff_url:
            diff_url = f"{prefix}/pulls/{candidate.change_number}.diff"
        if not _safe_public_url(diff_url, repository):
            raise _CaptureBlocked("gitea_pull_diff_url_invalid")
        patch_body, patch_path = _request_bytes(
            root,
            candidate=candidate,
            resource="pull-diff",
            url=diff_url,
            transport=transport,
            authorization=authorization,
            sleep=sleep,
            jitter=jitter,
        )
        if not patch_body:
            raise _CaptureBlocked("gitea_pull_diff_empty")
        raw_paths.append(patch_path)

        relationship_rows, relationship_paths = _paged_json_list(
            root,
            candidate=candidate,
            resource="relationships",
            url_base=f"{prefix}/issues/{candidate.change_number}/timeline",
            transport=transport,
            authorization=authorization,
            sleep=sleep,
            jitter=jitter,
        )
        raw_paths.extend(relationship_paths)
        issue_numbers = _issue_numbers(pull, relationship_rows)
        if not issue_numbers:
            return SourceDecision.rejected("authoritative_issue_link_missing")

        issue_values: list[dict[str, object]] = []
        attachment_urls: list[str] = []
        for issue_number in issue_numbers:
            issue, issue_path = _request_json(
                root,
                candidate=candidate,
                resource=f"issue-{issue_number}",
                url=f"{prefix}/issues/{issue_number}",
                transport=transport,
                authorization=authorization,
                sleep=sleep,
                jitter=jitter,
            )
            raw_paths.append(issue_path)
            if type(issue) is not dict or issue.get("number") != issue_number:
                raise _CaptureBlocked("gitea_issue_identity_invalid")
            comments, comment_paths = _paged_json_list(
                root,
                candidate=candidate,
                resource=f"issue-{issue_number}-comments",
                url_base=f"{prefix}/issues/{issue_number}/comments",
                transport=transport,
                authorization=authorization,
                sleep=sleep,
                jitter=jitter,
            )
            history, history_paths = _paged_json_list(
                root,
                candidate=candidate,
                resource=f"issue-{issue_number}-history",
                url_base=f"{prefix}/issues/{issue_number}/timeline",
                transport=transport,
                authorization=authorization,
                sleep=sleep,
                jitter=jitter,
            )
            raw_paths.extend(comment_paths)
            raw_paths.extend(history_paths)
            attachment_urls.extend(_attachment_urls(issue, repository))
            for comment in comments:
                attachment_urls.extend(_attachment_urls(comment, repository))
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
            body, raw_path = _request_bytes(
                root,
                candidate=candidate,
                resource=f"attachment-{sequence}",
                url=url,
                transport=transport,
                authorization=authorization,
                sleep=sleep,
                jitter=jitter,
            )
            if not body:
                raise _CaptureBlocked("gitea_attachment_empty")
            raw_paths.append(raw_path)
            attachment_paths.append(raw_path)

        manifest = {
            "candidate": candidate.to_value(),
            "pull": pull,
            "relationships": relationship_rows,
            "issue_numbers": list(issue_numbers),
            "issues": issue_values,
            "raw_paths": raw_paths,
            "patch_raw_path": patch_path,
            "attachment_paths": attachment_paths,
            "complete": True,
        }
        manifest_path = _write_raw(
            root,
            candidate,
            "source-manifest",
            1,
            (
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode(),
            extension="json",
            created_at=_now(),
        )
        return SourceDecision.accepted(raw_evidence_path=manifest_path)
    except _CaptureBlocked as error:
        return SourceDecision.blocked(str(error))


def _request_json(
    root: Path,
    *,
    candidate: CandidateKey,
    resource: str,
    url: str,
    transport: GiteaTransport,
    authorization: AuthorizationRecord,
    sleep: Callable[[float], None],
    jitter: Callable[[], float],
) -> tuple[object, str]:
    body, raw_path = _request_bytes(
        root,
        candidate=candidate,
        resource=resource,
        url=url,
        transport=transport,
        authorization=authorization,
        sleep=sleep,
        jitter=jitter,
    )
    try:
        return _strict_json(body, resource), raw_path
    except RuntimeError as error:
        raise _CaptureBlocked(f"gitea_{resource}_json_invalid") from error


def _request_bytes(
    root: Path,
    *,
    candidate: CandidateKey,
    resource: str,
    url: str,
    transport: GiteaTransport,
    authorization: AuthorizationRecord,
    sleep: Callable[[float], None],
    jitter: Callable[[], float],
) -> tuple[bytes, str]:
    for attempt in range(1, 5):
        created_at = _now()
        try:
            response = transport.execute(url)
        except (TimeoutError, OSError, http.client.HTTPException) as error:
            _write_attempt(
                root,
                candidate=candidate,
                resource=resource,
                attempt=attempt,
                created_at=created_at,
                url=url,
                state="timeout" if isinstance(error, TimeoutError) else "failed",
                status=None,
                raw_path=None,
                failure_type=type(error).__name__,
                authorization=authorization,
            )
            if attempt == 4:
                raise _CaptureBlocked(f"gitea_{resource}_capture_exhausted") from error
            sleep(_RETRY_DELAYS[attempt - 1] + max(0.0, min(jitter(), 1.0)))
            continue

        raw_path = _write_raw(
            root,
            candidate,
            resource,
            attempt,
            response.body,
            extension="body",
            created_at=created_at,
        )
        retryable = response.status == 429 or response.status >= 500
        succeeded = 200 <= response.status < 300
        _write_attempt(
            root,
            candidate=candidate,
            resource=resource,
            attempt=attempt,
            created_at=created_at,
            url=url,
            state="succeeded" if succeeded else "retryable" if retryable else "failed",
            status=response.status,
            raw_path=raw_path,
            failure_type=None,
            authorization=authorization,
        )
        if succeeded:
            return response.body, raw_path
        if not retryable or attempt == 4:
            raise _CaptureBlocked(f"gitea_{resource}_capture_exhausted")
        sleep(_RETRY_DELAYS[attempt - 1] + max(0.0, min(jitter(), 1.0)))
    raise AssertionError("unreachable Gitea request state")


def _paged_json_list(
    root: Path,
    *,
    candidate: CandidateKey,
    resource: str,
    url_base: str,
    transport: GiteaTransport,
    authorization: AuthorizationRecord,
    sleep: Callable[[float], None],
    jitter: Callable[[], float],
) -> tuple[list[object], list[str]]:
    rows: list[object] = []
    paths: list[str] = []
    for page in range(1, 10001):
        value, path = _request_json(
            root,
            candidate=candidate,
            resource=f"{resource}-page-{page}",
            url=f"{url_base}?page={page}&limit=100",
            transport=transport,
            authorization=authorization,
            sleep=sleep,
            jitter=jitter,
        )
        paths.append(path)
        if value is None:
            return rows, paths
        if type(value) is not list:
            raise _CaptureBlocked(f"gitea_{resource}_page_invalid")
        if not value:
            return rows, paths
        rows.extend(value)
        if len(value) < 100:
            return rows, paths
    raise _CaptureBlocked(f"gitea_{resource}_pagination_unbounded")


def _write_attempt(
    root: Path,
    *,
    candidate: CandidateKey,
    resource: str,
    attempt: int,
    created_at: str,
    url: str,
    state: str,
    status: int | None,
    raw_path: str | None,
    failure_type: str | None,
    authorization: AuthorizationRecord,
) -> None:
    write_stable_artifact(
        root,
        artifact_id=(
            f"source_attempt/{candidate.repository_key}/{candidate.change_number}/"
            f"{resource}/attempt-{attempt}/v1"
        ),
        artifact_kind="source_attempt",
        semantic_version="1.0",
        created_at=created_at,
        inputs=(),
        payload={
            "candidate": candidate.to_value(),
            "provider": "gitea",
            "resource": resource,
            "attempt": attempt,
            "url": url,
            "state": state,
            "status": status,
            "raw_evidence_path": raw_path,
            "failure_type": failure_type,
        },
        authorization=authorization,
    )


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
        "gitea",
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
        raise RuntimeError("Gitea raw path escapes the artifact root")
    for ancestor in (path.parent, *path.parent.parents):
        if ancestor == root.parent:
            break
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("Gitea raw path contains a symlink")
        if ancestor == root:
            break
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RuntimeError("Gitea raw evidence path is unsafe")
        if path.read_bytes() != content:
            raise RuntimeError("Gitea raw evidence conflicts with an existing attempt")
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


def _strict_json(content: bytes, label: str) -> object:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise RuntimeError(f"Gitea {label} contains duplicate keys")
            result[key] = value
        return result

    try:
        return json.loads(
            content.decode(),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                RuntimeError(f"Gitea {label} contains a non-finite number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Gitea {label} is not JSON") from error


def _issue_numbers(pull: dict[str, object], rows: list[object]) -> tuple[int, ...]:
    numbers: set[int] = set()
    for field in ("title", "body"):
        value = pull.get(field)
        if type(value) is str:
            numbers.update(int(number) for number in _LINK.findall(value))
    for row in rows:
        if type(row) is not dict:
            continue
        event_type = row.get("type")
        if event_type == "comment_ref":
            issue = row.get("ref_issue")
        elif event_type in {
            "cross_reference",
            "cross-reference",
            "close",
            "closed",
            "referenced",
        }:
            source = row.get("source")
            issue = source.get("issue") if type(source) is dict else None
        else:
            continue
        number = issue.get("number") if type(issue) is dict else None
        if type(number) is int and number > 0:
            numbers.add(number)
    return tuple(sorted(numbers))


def _attachment_urls(value: object, repository: GiteaRepository) -> list[str]:
    urls: list[str] = []
    if type(value) is dict:
        text = value.get("body")
        if type(text) is str:
            for url in _MARKDOWN_URL.findall(text):
                parsed = urlparse(url)
                if (
                    parsed.scheme == "https"
                    and parsed.hostname in repository.attachment_hosts
                    and parsed.path.startswith("/attachments/")
                    and parsed.username is None
                    and parsed.password is None
                    and not parsed.fragment
                ):
                    urls.append(url)
    return urls


def _safe_public_url(url: str, repository: GiteaRepository) -> bool:
    parsed = urlparse(url)
    return bool(
        parsed.scheme == "https"
        and parsed.hostname in repository.attachment_hosts
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "GiteaRepository",
    "GiteaResponse",
    "GiteaTransport",
    "UrllibGiteaTransport",
    "capture_gitea_source",
]
