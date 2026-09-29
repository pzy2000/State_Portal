"""Offline v2 P3 public-source closure for a patch-eligible candidate.

The adapter replays an already persisted v2 source manifest.  It never invokes
a provider, materializes a repository, or starts an evaluation.  The only
outputs are stable-ID ``public_task`` and ``contamination_record`` artifacts.
"""

from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlparse

from warptest.issue_naturalize import TokenGuard

from .epoch import SELECTION_EPOCH_V2
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_TOKEN = re.compile(r"[a-z0-9]+")
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z"
)
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_PATH = re.compile(r"(?<![\w.-])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+")
_FILE_NAME = re.compile(
    r"\b[A-Za-z0-9_-]+\.(?:c|cc|cpp|cxx|h|hpp|java|js|jsx|json|md|py|rs|"
    r"sh|toml|ts|tsx|txt|yaml|yml)\b",
    re.IGNORECASE,
)
_TEST_NAME = re.compile(r"\b(?:test|spec)[_.-][A-Za-z0-9_.-]+\b", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_PUBLIC_FORBIDDEN = (
    "golden",
    "hidden oracle",
    "hidden_oracle",
    "oracle",
    "specs/",
    "raw/",
)


class P3SourceClosureError(RuntimeError):
    """The existing local source/patch evidence cannot safely close P3 source."""


@dataclass(frozen=True)
class V2P3SourceClosure:
    """The two durable artifacts emitted by a successful local source closure."""

    public_task: StableArtifact
    contamination: StableArtifact


@dataclass(frozen=True)
class _ScreeningRoot:
    enumeration: StableArtifact
    source: StableArtifact
    patch: StableArtifact
    checkpoint: StableArtifact
    manifest: Mapping[str, Any]
    source_manifest_path: str
    primary_issue: Mapping[str, Any]
    selection: str
    rationale: str | None
    task_title: str
    task_text: str
    issue_text: str
    issue_url: str
    issue_created_at: str
    change_url: str
    change_created_at: str
    protected_tokens: tuple[str, ...]


def write_v2_p3_source_closure(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    exact_text_readily_searchable: bool,
    coordinator_only_tokens: tuple[str, ...],
    authorization: AuthorizationRecord,
    primary_issue_number: int | None = None,
    primary_issue_rationale: str | None = None,
    created_at: str | None = None,
) -> V2P3SourceClosure:
    """Replay local evidence, then write and reload the v2 P3 source closure.

    A sole authoritative Issue is selected mechanically.  When the source
    manifest names more than one Issue, callers must make an explicit selection
    and record a non-empty rationale.  PR-only and structurally incomplete
    source packets fail closed before any new artifact is written.
    """

    root = _safe_root(artifact_root)
    _validate_candidate(candidate)
    _validate_authorization(authorization)
    if type(exact_text_readily_searchable) is not bool:
        raise ValueError("contamination searchability state is invalid")
    protected = _validate_protected_tokens(coordinator_only_tokens)
    timestamp = _now() if created_at is None else created_at

    screening = _replay_screening_root(
        root,
        candidate=candidate,
        primary_issue_number=primary_issue_number,
        primary_issue_rationale=primary_issue_rationale,
        coordinator_only_tokens=protected,
    )
    public_reference = _public_task_reference(candidate)
    contamination_reference = _contamination_reference(candidate)
    public_payload = _public_task_payload(candidate, screening)
    contamination_payload = _contamination_payload(
        candidate,
        screening,
        exact_text_readily_searchable=exact_text_readily_searchable,
    )

    write_stable_artifact(
        root,
        artifact_id=public_reference.artifact_id,
        artifact_kind=public_reference.artifact_kind,
        semantic_version=public_reference.semantic_version,
        created_at=timestamp,
        inputs=(screening.source.reference, screening.patch.reference),
        payload=public_payload,
        authorization=authorization,
    )
    write_stable_artifact(
        root,
        artifact_id=contamination_reference.artifact_id,
        artifact_kind=contamination_reference.artifact_kind,
        semantic_version=contamination_reference.semantic_version,
        created_at=timestamp,
        inputs=(
            screening.source.reference,
            screening.patch.reference,
            public_reference,
        ),
        payload=contamination_payload,
        authorization=authorization,
    )
    loaded = load_v2_p3_source_closure(root, candidate=candidate)
    if (
        loaded.public_task.payload != public_payload
        or loaded.contamination.payload != contamination_payload
    ):
        raise P3SourceClosureError("P3 source closure reload is inconsistent")
    return loaded


def load_v2_p3_source_closure(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
) -> V2P3SourceClosure:
    """Load and structurally replay a previously written v2 P3 source closure."""

    root = _safe_root(artifact_root)
    _validate_candidate(candidate)
    public_task = _load_artifact(root, _public_task_reference(candidate), "public task")
    primary = _mapping(
        public_task.payload.get("primary_issue"), "public task primary issue"
    )
    primary_number = _positive_int(primary.get("number"), "public task primary issue")
    _required_text(primary.get("selection"), "public task primary selection")
    rationale = primary.get("rationale")
    if rationale is not None:
        rationale = _required_text(rationale, "public task primary rationale")
    screening = _replay_screening_root(
        root,
        candidate=candidate,
        primary_issue_number=primary_number,
        primary_issue_rationale=rationale,
        coordinator_only_tokens=(),
    )
    expected_public_inputs = (screening.source.reference, screening.patch.reference)
    if public_task.inputs != expected_public_inputs:
        raise P3SourceClosureError(
            "public task inputs are not the current same-root source/patch"
        )
    expected_public_payload = _public_task_payload(candidate, screening)
    _validate_public_task_payload(public_task.payload, expected_public_payload)

    contamination = _load_artifact(
        root,
        _contamination_reference(candidate),
        "contamination record",
    )
    expected_contamination_inputs = (*expected_public_inputs, public_task.reference)
    if contamination.inputs != expected_contamination_inputs:
        raise P3SourceClosureError(
            "contamination inputs are not the current same-root source/patch/task"
        )
    _validate_contamination_payload(
        contamination.payload,
        candidate=candidate,
        screening=screening,
    )
    return V2P3SourceClosure(public_task=public_task, contamination=contamination)


def _replay_screening_root(
    root: Path,
    *,
    candidate: CandidateKey,
    primary_issue_number: int | None,
    primary_issue_rationale: str | None,
    coordinator_only_tokens: tuple[str, ...],
) -> _ScreeningRoot:
    enumeration_reference = SELECTION_EPOCH_V2.enumeration_reference(
        candidate.repository_key
    )
    enumeration = _load_artifact(root, enumeration_reference, "repository enumeration")
    _validate_enumeration(enumeration, candidate)

    source_reference = _source_packet_reference(candidate)
    source = _load_artifact(root, source_reference, "source packet")
    if source.inputs != (enumeration_reference,):
        raise P3SourceClosureError("source packet enumeration input is invalid")
    if (
        source.payload.get("candidate") != candidate.to_value()
        or source.payload.get("complete") is not True
    ):
        raise P3SourceClosureError("source packet is incomplete")
    raw_manifest_path = source.payload.get("raw_evidence_path")
    manifest_path, canonical_manifest_path = _safe_raw_file(
        root,
        raw_manifest_path,
        label="source manifest",
    )
    manifest = _strict_json_object(manifest_path, "source raw manifest")
    _validate_manifest_structure(root, manifest, candidate)

    patch_reference = _patch_reference(candidate)
    patch = _load_artifact(root, patch_reference, "patch screening")
    if patch.inputs != (enumeration_reference, source_reference):
        raise P3SourceClosureError("patch screening source prerequisite is invalid")
    _validate_patch(patch, candidate, manifest, canonical_manifest_path, root)

    checkpoint_reference = _checkpoint_reference(candidate)
    checkpoint = _load_artifact(root, checkpoint_reference, "screening checkpoint")
    _validate_checkpoint(checkpoint, candidate, enumeration_reference, patch_reference)

    primary_issue, selection, rationale = _select_primary_issue(
        manifest,
        primary_issue_number=primary_issue_number,
        primary_issue_rationale=primary_issue_rationale,
    )
    issue_value = _mapping(primary_issue.get("issue"), "primary Issue")
    title = _sanitize_public_text(
        _required_text(issue_value.get("title"), "Issue title")
    )
    body_raw = issue_value.get("body")
    if body_raw is None:
        body_raw = ""
    if type(body_raw) is not str:
        raise P3SourceClosureError("Issue body is invalid")
    body = _sanitize_public_text(body_raw)
    if not title:
        raise P3SourceClosureError("Issue title has no safe public text")
    task_text = title if not body else f"{title}\n\n{body}"
    issue_text = _join_source_text(
        _required_text(issue_value.get("title"), "Issue title"), body_raw
    )
    issue_url = _public_url(issue_value.get("html_url"), "Issue URL")
    issue_created_at = _timestamp(issue_value.get("created_at"), "Issue creation time")
    pull = _mapping(manifest.get("pull"), "pull metadata")
    change_url = _public_url(pull.get("html_url"), "pull URL")
    change_created_at = _timestamp(pull.get("created_at"), "pull creation time")

    protected_tokens = _protected_tokens(patch, coordinator_only_tokens)
    _run_reverse_guard(task_text, protected_tokens)
    if rationale is not None:
        _run_reverse_guard(rationale, protected_tokens)
    return _ScreeningRoot(
        enumeration=enumeration,
        source=source,
        patch=patch,
        checkpoint=checkpoint,
        manifest=manifest,
        source_manifest_path=canonical_manifest_path,
        primary_issue=primary_issue,
        selection=selection,
        rationale=rationale,
        task_title=title,
        task_text=task_text,
        issue_text=issue_text,
        issue_url=issue_url,
        issue_created_at=issue_created_at,
        change_url=change_url,
        change_created_at=change_created_at,
        protected_tokens=protected_tokens,
    )


def _validate_enumeration(artifact: StableArtifact, candidate: CandidateKey) -> None:
    if (
        artifact.payload.get("repository_key") != candidate.repository_key
        or artifact.payload.get("complete") is not True
    ):
        raise P3SourceClosureError("repository enumeration is incomplete")
    values = artifact.payload.get("candidates")
    if type(values) is not list:
        raise P3SourceClosureError("repository enumeration candidates are invalid")
    try:
        candidates = tuple(CandidateKey.from_value(value) for value in values)
    except ValueError as error:
        raise P3SourceClosureError(
            "repository enumeration candidates are invalid"
        ) from error
    if candidates != tuple(sorted(candidates)) or len(set(candidates)) != len(
        candidates
    ):
        raise P3SourceClosureError("repository enumeration candidates are unordered")
    if candidate not in candidates:
        raise P3SourceClosureError("candidate is absent from repository enumeration")


def _validate_manifest_structure(
    root: Path,
    manifest: Mapping[str, Any],
    candidate: CandidateKey,
) -> None:
    if (
        manifest.get("candidate") != candidate.to_value()
        or manifest.get("complete") is not True
    ):
        raise P3SourceClosureError("source raw manifest candidate binding is invalid")
    pull = manifest.get("pull")
    if type(pull) is not dict:
        raise P3SourceClosureError("pull metadata is missing")
    if pull.get("number") != candidate.change_number or pull.get("merged") is not True:
        raise P3SourceClosureError("pull metadata is invalid")
    _public_url(pull.get("html_url"), "pull URL")
    _timestamp(pull.get("created_at"), "pull creation time")

    raw_paths = _paths(manifest.get("raw_paths"), "source raw path inventory")
    if not raw_paths:
        raise P3SourceClosureError("source raw path inventory is empty")
    for path in raw_paths:
        _safe_raw_file(root, path, label="source raw evidence")
    patch_raw_path = manifest.get("patch_raw_path")
    _, canonical_patch_path = _safe_raw_file(root, patch_raw_path, label="source patch")
    if canonical_patch_path not in raw_paths:
        raise P3SourceClosureError("source patch is absent from raw path inventory")
    if not (root / canonical_patch_path).read_bytes():
        raise P3SourceClosureError("source patch is empty")
    attachment_paths = _paths(
        manifest.get("attachment_paths"), "source attachment paths"
    )
    if any(path not in raw_paths for path in attachment_paths):
        raise P3SourceClosureError(
            "source attachment is absent from raw path inventory"
        )

    receipt_paths = manifest.get("request_receipt_paths")
    provider = manifest.get("provider")
    if provider is not None and (type(provider) is not str or not provider):
        raise P3SourceClosureError("source provider is invalid")
    if provider == "github" and receipt_paths is None:
        raise P3SourceClosureError("GitHub source receipts are missing")
    if receipt_paths is not None:
        for receipt_path in _paths(receipt_paths, "source receipt paths"):
            _validate_receipt(root, receipt_path, provider, raw_paths)
    authority = manifest.get("authority")
    if authority is not None:
        authority_map = _mapping(authority, "source authority")
        if provider is not None and authority_map.get("provider") != provider:
            raise P3SourceClosureError("source authority provider is invalid")
        for key in ("provider", "api_base", "repository"):
            _required_text(authority_map.get(key), f"source authority {key}")

    if type(manifest.get("relationships")) is not list:
        raise P3SourceClosureError("source relationships are incomplete")

    issue_numbers = _issue_numbers(manifest.get("issue_numbers"))
    issue_values = manifest.get("issues")
    if type(issue_values) is not list:
        raise P3SourceClosureError("authoritative Issue list is invalid")
    if not issue_numbers or not issue_values:
        raise P3SourceClosureError("authoritative issue is missing")
    if len(issue_values) != len(issue_numbers):
        raise P3SourceClosureError(
            "authoritative Issue list does not match relationships"
        )
    actual_numbers: list[int] = []
    for entry in issue_values:
        item = _mapping(entry, "authoritative Issue")
        number = _positive_int(item.get("number"), "authoritative Issue")
        issue = _mapping(item.get("issue"), "authoritative Issue")
        if issue.get("number") != number:
            raise P3SourceClosureError("authoritative Issue identity is invalid")
        if type(issue.get("pull_request")) is dict:
            raise P3SourceClosureError("authoritative Issue resolves to a pull request")
        _required_text(issue.get("title"), "authoritative Issue title")
        body = issue.get("body")
        if body is not None and type(body) is not str:
            raise P3SourceClosureError("authoritative Issue body is invalid")
        _public_url(issue.get("html_url"), "authoritative Issue URL")
        _timestamp(issue.get("created_at"), "authoritative Issue creation time")
        if (
            type(item.get("comments")) is not list
            or type(item.get("history")) is not list
        ):
            raise P3SourceClosureError("authoritative Issue thread is incomplete")
        actual_numbers.append(number)
    if tuple(actual_numbers) != issue_numbers:
        raise P3SourceClosureError("authoritative Issue order is invalid")


def _validate_receipt(
    root: Path,
    receipt_path: str,
    provider: object,
    raw_paths: tuple[str, ...],
) -> None:
    path, _ = _safe_raw_file(root, receipt_path, label="source receipt")
    receipt = _strict_json_object(path, "source receipt")
    if provider is not None and receipt.get("provider") != provider:
        raise P3SourceClosureError("source receipt provider is invalid")
    canonical_url = _required_text(
        receipt.get("canonical_url"), "source receipt canonical URL"
    )
    request_path, _ = _safe_raw_file(
        root,
        receipt.get("request_raw_path"),
        label="source request",
    )
    request = _strict_json_object(request_path, "source request")
    if request.get("method") != "GET" or request.get("url") != canonical_url:
        raise P3SourceClosureError("source receipt request binding is invalid")
    _, response_path = _safe_raw_file(
        root,
        receipt.get("response_raw_path"),
        label="source response",
    )
    if response_path not in raw_paths:
        raise P3SourceClosureError(
            "source receipt response is not in raw path inventory"
        )
    status = receipt.get("status")
    if type(status) is not int or not 200 <= status < 300:
        raise P3SourceClosureError("source receipt did not complete successfully")
    if receipt.get("failure_type") is not None:
        raise P3SourceClosureError("source receipt carries a failure")


def _validate_patch(
    patch: StableArtifact,
    candidate: CandidateKey,
    manifest: Mapping[str, Any],
    source_manifest_path: str,
    root: Path,
) -> None:
    payload = patch.payload
    if (
        payload.get("candidate") != candidate.to_value()
        or payload.get("complete") is not True
        or payload.get("outcome") != "patch_eligible"
    ):
        raise P3SourceClosureError("patch screening is not eligible")
    production_paths = _paths(payload.get("production_paths"), "production patch paths")
    test_paths = _paths(payload.get("test_paths"), "test patch paths")
    if not production_paths or not test_paths:
        raise P3SourceClosureError("patch screening path inventory is incomplete")
    if payload.get("source_manifest_path") != source_manifest_path:
        raise P3SourceClosureError("patch screening source manifest binding is invalid")
    expected_patch = manifest.get("patch_raw_path")
    _, canonical_patch_path = _safe_raw_file(root, expected_patch, label="source patch")
    if payload.get("raw_patch_path") != canonical_patch_path:
        raise P3SourceClosureError("patch screening raw patch binding is invalid")


def _validate_checkpoint(
    checkpoint: StableArtifact,
    candidate: CandidateKey,
    enumeration_reference: ArtifactReference,
    patch_reference: ArtifactReference,
) -> None:
    if checkpoint.inputs != (enumeration_reference, patch_reference):
        raise P3SourceClosureError("screening checkpoint inputs are invalid")
    payload = checkpoint.payload
    if (
        payload.get("candidate") != candidate.to_value()
        or payload.get("state") != "complete"
        or payload.get("outcome") != "patch_eligible"
        or payload.get("stage_artifact_id") != patch_reference.artifact_id
    ):
        raise P3SourceClosureError("screening checkpoint is not patch eligible")


def _select_primary_issue(
    manifest: Mapping[str, Any],
    *,
    primary_issue_number: int | None,
    primary_issue_rationale: str | None,
) -> tuple[Mapping[str, Any], str, str | None]:
    issue_numbers = _issue_numbers(manifest.get("issue_numbers"))
    issue_values = manifest.get("issues")
    assert type(issue_values) is list  # Validated before selection.
    by_number = {
        _positive_int(
            _mapping(value, "authoritative Issue").get("number"), "Issue"
        ): _mapping(value, "authoritative Issue")
        for value in issue_values
    }
    if len(issue_numbers) == 1:
        only_number = issue_numbers[0]
        if primary_issue_number is not None and primary_issue_number != only_number:
            raise P3SourceClosureError("explicit primary Issue does not match source")
        if primary_issue_rationale is not None:
            raise P3SourceClosureError(
                "sole authoritative Issue does not need a rationale"
            )
        return by_number[only_number], "sole_authoritative_issue", None
    if type(primary_issue_number) is not int or primary_issue_number not in by_number:
        raise P3SourceClosureError("primary authoritative Issue is ambiguous")
    rationale = _sanitize_public_text(
        _required_text(primary_issue_rationale, "primary Issue rationale")
    )
    if not rationale:
        raise P3SourceClosureError("primary Issue rationale has no safe public text")
    return by_number[primary_issue_number], "explicit_primary_issue", rationale


def _public_task_payload(
    candidate: CandidateKey,
    screening: _ScreeningRoot,
) -> dict[str, Any]:
    primary = {
        "number": _positive_int(screening.primary_issue.get("number"), "primary Issue"),
        "url": screening.issue_url,
        "created_at": screening.issue_created_at,
        "selection": screening.selection,
    }
    if screening.rationale is not None:
        primary["rationale"] = screening.rationale
    return {
        "candidate": candidate.to_value(),
        "primary_issue": primary,
        "change": {
            "number": candidate.change_number,
            "url": screening.change_url,
            "created_at": screening.change_created_at,
        },
        "task_title": screening.task_title,
        "task_text": screening.task_text,
        "token_guard": {
            "status": "passed",
            "protected_token_count": len(screening.protected_tokens),
        },
        "complete": True,
    }


def _contamination_payload(
    candidate: CandidateKey,
    screening: _ScreeningRoot,
    *,
    exact_text_readily_searchable: bool,
) -> dict[str, Any]:
    source_tokens = frozenset(_TOKEN.findall(screening.issue_text.casefold()))
    task_tokens = frozenset(_TOKEN.findall(screening.task_text.casefold()))
    union = source_tokens | task_tokens
    overlap = len(source_tokens & task_tokens) / len(union) if union else 0.0
    return {
        "candidate": candidate.to_value(),
        "primary_issue_number": _positive_int(
            screening.primary_issue.get("number"), "primary Issue"
        ),
        "issue_created_at": screening.issue_created_at,
        "change_created_at": screening.change_created_at,
        "issue_url": screening.issue_url,
        "change_url": screening.change_url,
        "exact_text_readily_searchable": exact_text_readily_searchable,
        "source_text_overlap": overlap,
        "risk_statement": "public_training_exposure_possible",
        "complete": True,
    }


def _validate_public_task_payload(
    payload: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    actual_without_guard = dict(payload)
    expected_without_guard = dict(expected)
    actual_guard = actual_without_guard.pop("token_guard", None)
    expected_guard = expected_without_guard.pop("token_guard", None)
    if actual_without_guard != expected_without_guard:
        raise P3SourceClosureError("public task payload does not replay from source")
    if type(actual_guard) is not dict or type(expected_guard) is not dict:
        raise P3SourceClosureError("public task TokenGuard record is invalid")
    actual_count = actual_guard.get("protected_token_count")
    expected_count = expected_guard.get("protected_token_count")
    if (
        actual_guard.get("status") != "passed"
        or type(actual_count) is not int
        or type(expected_count) is not int
        or actual_count < expected_count
        or set(actual_guard) != {"status", "protected_token_count"}
    ):
        raise P3SourceClosureError("public task TokenGuard record is invalid")


def _validate_contamination_payload(
    payload: Mapping[str, Any],
    *,
    candidate: CandidateKey,
    screening: _ScreeningRoot,
) -> None:
    required = {
        "candidate",
        "primary_issue_number",
        "issue_created_at",
        "change_created_at",
        "issue_url",
        "change_url",
        "exact_text_readily_searchable",
        "source_text_overlap",
        "risk_statement",
        "complete",
    }
    if set(payload) != required:
        raise P3SourceClosureError("contamination record schema is invalid")
    searchability = payload.get("exact_text_readily_searchable")
    if type(searchability) is not bool:
        raise P3SourceClosureError("contamination searchability state is invalid")
    expected = _contamination_payload(
        candidate,
        screening,
        exact_text_readily_searchable=searchability,
    )
    if payload != expected:
        raise P3SourceClosureError("contamination record does not replay from source")


def _protected_tokens(
    patch: StableArtifact,
    coordinator_only_tokens: tuple[str, ...],
) -> tuple[str, ...]:
    locations = [
        *_paths(patch.payload.get("production_paths"), "production patch paths"),
        *_paths(patch.payload.get("test_paths"), "test patch paths"),
    ]
    values: list[str] = [*coordinator_only_tokens]
    for location in locations:
        values.append(location)
        values.append(PurePosixPath(location).name)
    return tuple(sorted(set(values)))


def _run_reverse_guard(public_text: str, protected_tokens: tuple[str, ...]) -> None:
    guard = TokenGuard({"target": {"coordinator_only": list(protected_tokens)}})
    result = guard.check_reverse(public_text, extra_forbidden=_PUBLIC_FORBIDDEN)
    if not result.ok:
        raise P3SourceClosureError(
            "TokenGuard rejected public text: "
            f"forbidden={result.forbidden!r}, leaked={result.leaked_mechanism!r}"
        )


def _sanitize_public_text(value: str) -> str:
    text = _FENCED_CODE.sub(" ", value)
    text = _INLINE_CODE.sub(" ", text)
    text = _IMAGE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _PATH.sub(" ", text)
    text = _FILE_NAME.sub(" ", text)
    text = _TEST_NAME.sub(" ", text)
    return _SPACE.sub(" ", text).strip(" -:;,.\t\r\n")


def _join_source_text(title: str, body: str) -> str:
    return title if not body.strip() else f"{title}\n\n{body}"


def _safe_root(value: str | Path) -> Path:
    path = Path(value).absolute()
    try:
        root = path.resolve(strict=True)
        info = path.lstat()
    except (FileNotFoundError, ValueError) as error:
        raise P3SourceClosureError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise P3SourceClosureError("artifact root is unsafe")
    return root


def _safe_raw_file(root: Path, value: object, *, label: str) -> tuple[Path, str]:
    if type(value) is not str or not value:
        raise P3SourceClosureError(f"{label} path is missing")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or "." in relative.parts
        or ".." in relative.parts
        or value != relative.as_posix()
    ):
        raise P3SourceClosureError(f"{label} path is unsafe")
    current = root
    try:
        for part in relative.parts:
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise P3SourceClosureError(f"{label} path contains a symlink")
        if not stat.S_ISREG(current.lstat().st_mode):
            raise P3SourceClosureError(f"{label} file is unsafe")
    except FileNotFoundError as error:
        raise P3SourceClosureError(f"{label} file is missing") from error
    return current, relative.as_posix()


def _strict_json_object(path: Path, label: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise P3SourceClosureError(f"{label} contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                P3SourceClosureError(f"{label} contains a non-finite number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise P3SourceClosureError(f"{label} is not strict JSON") from error
    if type(value) is not dict:
        raise P3SourceClosureError(f"{label} is not an object")
    return value


def _load_artifact(
    root: Path,
    reference: ArtifactReference,
    label: str,
) -> StableArtifact:
    try:
        return load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
    except (PermissionError, RuntimeError, ValueError) as error:
        raise P3SourceClosureError(f"{label} cannot be loaded") from error


def _source_packet_reference(candidate: CandidateKey) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=(
            f"candidate_source_packet/{candidate.repository_key}/"
            f"{candidate.change_number}/source/v2"
        ),
        artifact_kind="candidate_source_packet",
        semantic_version="2.0",
    )


def _patch_reference(candidate: CandidateKey) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=(
            f"candidate_test_patch/{candidate.repository_key}/"
            f"{candidate.change_number}/screening/v2"
        ),
        artifact_kind="candidate_test_patch",
        semantic_version="2.0",
    )


def _checkpoint_reference(candidate: CandidateKey) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=(
            f"screening_checkpoint/{candidate.repository_key}/"
            f"{candidate.change_number}/current/v2"
        ),
        artifact_kind="screening_checkpoint",
        semantic_version="2.0",
    )


def _public_task_reference(candidate: CandidateKey) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=(
            f"public_task/{candidate.repository_key}/{candidate.change_number}/current/v2"
        ),
        artifact_kind="public_task",
        semantic_version="2.0",
    )


def _contamination_reference(candidate: CandidateKey) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=(
            "contamination_record/"
            f"{candidate.repository_key}/{candidate.change_number}/current/v2"
        ),
        artifact_kind="contamination_record",
        semantic_version="2.0",
    )


def _paths(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list or any(
        type(item) is not str or not item for item in value
    ):
        raise P3SourceClosureError(f"{label} are invalid")
    values = tuple(value)
    if len(set(values)) != len(values):
        raise P3SourceClosureError(f"{label} are duplicated")
    return values


def _issue_numbers(value: object) -> tuple[int, ...]:
    if type(value) is not list:
        raise P3SourceClosureError("authoritative Issue numbers are invalid")
    numbers = tuple(_positive_int(item, "authoritative Issue number") for item in value)
    if numbers != tuple(sorted(numbers)) or len(set(numbers)) != len(numbers):
        raise P3SourceClosureError("authoritative Issue numbers are unordered")
    return numbers


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise P3SourceClosureError(f"{label} is invalid")
    return value


def _required_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise P3SourceClosureError(f"{label} is missing")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise P3SourceClosureError(f"{label} is invalid")
    return value


def _public_url(value: object, label: str) -> str:
    url = _required_text(value, label)
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise P3SourceClosureError(f"{label} is unsafe")
    return url


def _timestamp(value: object, label: str) -> str:
    timestamp = _required_text(value, label)
    if not _UTC_TIMESTAMP.fullmatch(timestamp):
        raise P3SourceClosureError(f"{label} is invalid")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise P3SourceClosureError(f"{label} is invalid") from error
    return timestamp


def _validate_candidate(candidate: object) -> None:
    if type(candidate) is not CandidateKey:
        raise TypeError("candidate must be a CandidateKey")


def _validate_authorization(authorization: object) -> None:
    if type(authorization) is not AuthorizationRecord:
        raise TypeError("authorization must be an AuthorizationRecord")


def _validate_protected_tokens(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or any(
        type(token) is not str or not token for token in value
    ):
        raise ValueError("coordinator-only tokens are invalid")
    if len(set(value)) != len(value):
        raise ValueError("coordinator-only tokens are duplicated")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "P3SourceClosureError",
    "V2P3SourceClosure",
    "load_v2_p3_source_closure",
    "write_v2_p3_source_closure",
]
