"""Mechanical v2 source atomicity gate before C1 pre-annotation.

This module has no platform, model, provider, build, or candidate-execution
entry point.  It only replays the current same-root v2 screening heads and the
raw source manifest already frozen by the source provider.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .enumeration import RepositoryEnumeration
from .models import CandidateKey
from .providers.gitea import _issue_numbers as _gitea_issue_numbers
from .providers.github import (
    _explicit_closing_issue_numbers,
    _issue_numbers as _github_issue_numbers,
    _same_repository_cross_reference_issue_numbers,
    github_repository_for,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_SEMANTIC_VERSION = "2.0"
_RECEIPT_SCHEMA = "c1_lazarus_source_atomicity_v2"
_REJECTION_REASONS = frozenset(
    {
        "authoritative_issue_missing",
        "authoritative_issue_is_pull_request",
        "multiple_authoritative_issues",
        "source_manifest_recapture_required",
    }
)
_SOURCE_PAYLOAD_FIELDS = {"candidate", "raw_evidence_path", "complete"}
_PATCH_PAYLOAD_FIELDS = {
    "candidate",
    "outcome",
    "production_paths",
    "test_paths",
    "source_manifest_path",
    "raw_patch_path",
    "complete",
}
_CHECKPOINT_PAYLOAD_FIELDS = {
    "candidate",
    "state",
    "outcome",
    "stage_artifact_id",
}
_ATOMICITY_PAYLOAD_FIELDS = {
    "receipt_schema",
    "candidate",
    "source_manifest_path",
    "linked_issue_numbers",
    "linked_issue_count",
    "authoritative_issue_numbers",
    "authoritative_issue_count",
    "primary_issue_number",
    "outcome",
    "reason",
    "complete",
}
_ENUMERATION_PAYLOAD_FIELDS = {
    "repository_key",
    "merged_change_numbers",
    "authoritative_issue_link_missing",
    "repair_not_in_pinned",
    "candidates",
    "infrastructure_blocked",
    "complete",
}
_GITHUB_MANIFEST_FIELDS = {
    "candidate",
    "provider",
    "authority",
    "pull",
    "relationships",
    "issue_numbers",
    "issues",
    "raw_paths",
    "request_receipt_paths",
    "patch_raw_path",
    "attachment_paths",
    "complete",
}
_GITEA_MANIFEST_FIELDS = {
    "candidate",
    "pull",
    "relationships",
    "issue_numbers",
    "issues",
    "raw_paths",
    "patch_raw_path",
    "attachment_paths",
    "complete",
}
_ISSUE_ROW_FIELDS = {"number", "issue", "comments", "history"}


@dataclass(frozen=True, init=False)
class SourceAtomicityBatchContext:
    """Once-validated immutable repository enumeration context for one batch."""

    artifact_root: Path
    enumeration_reference: ArtifactReference
    enumeration_created_at: str
    repository_key: str
    candidate_numbers: frozenset[int]

    def __init__(self) -> None:
        raise TypeError(
            "source atomicity batch contexts come from the validating loader"
        )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.artifact_root, Path)
            or not self.artifact_root.is_absolute()
            or self.artifact_root.resolve(strict=True) != self.artifact_root
        ):
            raise ValueError("source atomicity batch root is invalid")
        expected = ArtifactReference(
            f"repository_enumeration/{self.repository_key}/current/v2",
            "repository_enumeration",
            _SEMANTIC_VERSION,
        )
        if self.enumeration_reference != expected:
            raise ValueError("source atomicity batch enumeration reference is invalid")
        if type(self.enumeration_created_at) is not str:
            raise ValueError("source atomicity batch enumeration time is invalid")
        if type(self.candidate_numbers) is not frozenset or any(
            type(number) is not int or number <= 0 for number in self.candidate_numbers
        ):
            raise ValueError("source atomicity batch candidates are invalid")


@dataclass(frozen=True)
class SourceAtomicityReceipt:
    """Typed projection of one current v2 source-atomicity artifact."""

    artifact: StableArtifact
    candidate: CandidateKey
    source_manifest_path: str
    linked_issue_numbers: tuple[int, ...]
    authoritative_issue_numbers: tuple[int, ...]
    primary_issue_number: int | None
    outcome: Literal["accepted", "rejected"]
    reason: str | None

    def __post_init__(self) -> None:
        if type(self.artifact) is not StableArtifact:
            raise ValueError("source atomicity artifact is not typed")
        if type(self.candidate) is not CandidateKey:
            raise ValueError("source atomicity candidate is not typed")
        if type(self.source_manifest_path) is not str or not self.source_manifest_path:
            raise ValueError("source atomicity manifest path is invalid")
        if not _strict_number_inventory(self.linked_issue_numbers) or not (
            _strict_number_inventory(self.authoritative_issue_numbers)
            and set(self.authoritative_issue_numbers).issubset(
                self.linked_issue_numbers
            )
        ):
            raise ValueError("source atomicity issue inventory is invalid")
        if self.outcome == "accepted":
            if (
                len(self.authoritative_issue_numbers) != 1
                or self.primary_issue_number != self.authoritative_issue_numbers[0]
                or self.reason is not None
            ):
                raise ValueError("accepted source atomicity receipt is invalid")
        elif self.outcome == "rejected":
            if (
                self.primary_issue_number is not None
                or self.reason not in _REJECTION_REASONS
            ):
                raise ValueError("rejected source atomicity receipt is invalid")
        else:
            raise ValueError("source atomicity outcome is invalid")


def _new_batch_context(
    *,
    artifact_root: Path,
    enumeration_reference: ArtifactReference,
    enumeration_created_at: str,
    repository_key: str,
    candidate_numbers: frozenset[int],
) -> SourceAtomicityBatchContext:
    context = object.__new__(SourceAtomicityBatchContext)
    object.__setattr__(context, "artifact_root", artifact_root)
    object.__setattr__(context, "enumeration_reference", enumeration_reference)
    object.__setattr__(context, "enumeration_created_at", enumeration_created_at)
    object.__setattr__(context, "repository_key", repository_key)
    object.__setattr__(context, "candidate_numbers", candidate_numbers)
    context.__post_init__()
    return context


@dataclass(frozen=True)
class _SourceReplay:
    inputs: tuple[ArtifactReference, ...]
    dependencies: tuple[StableArtifact, ...]
    source_manifest_path: str
    linked_issue_numbers: tuple[int, ...]
    authoritative_issue_numbers: tuple[int, ...]
    primary_issue_number: int | None
    outcome: Literal["accepted", "rejected"]
    reason: str | None

    def payload(self, candidate: CandidateKey) -> dict[str, Any]:
        return {
            "receipt_schema": _RECEIPT_SCHEMA,
            "candidate": candidate.to_value(),
            "source_manifest_path": self.source_manifest_path,
            "linked_issue_numbers": list(self.linked_issue_numbers),
            "linked_issue_count": len(self.linked_issue_numbers),
            "authoritative_issue_numbers": list(self.authoritative_issue_numbers),
            "authoritative_issue_count": len(self.authoritative_issue_numbers),
            "primary_issue_number": self.primary_issue_number,
            "outcome": self.outcome,
            "reason": self.reason,
            "complete": True,
        }


@dataclass(frozen=True)
class _RelationshipProjection:
    linked_issue_numbers: tuple[int, ...]
    authoritative_issue_numbers: tuple[int, ...]
    recapture_required: bool


@dataclass(frozen=True)
class _IssueInventory:
    linked_issue_numbers: tuple[int, ...]
    authoritative_issue_numbers: tuple[int, ...]
    details_complete: bool
    pull_request_masquerade: bool
    recapture_required: bool


def load_source_atomicity_batch_context(
    artifact_root: str | Path,
    *,
    repository_key: str,
) -> SourceAtomicityBatchContext:
    """Load and fully validate one v2 enumeration exactly once per batch."""

    reference = ArtifactReference(
        f"repository_enumeration/{repository_key}/current/v2",
        "repository_enumeration",
        _SEMANTIC_VERSION,
    )
    enumeration_artifact = load_stable_artifact(
        artifact_root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    enumeration = _typed_enumeration(enumeration_artifact, repository_key)
    return _new_batch_context(
        artifact_root=Path(artifact_root).resolve(strict=True),
        enumeration_reference=reference,
        enumeration_created_at=enumeration_artifact.created_at,
        repository_key=repository_key,
        candidate_numbers=frozenset(
            candidate.change_number for candidate in enumeration.candidates
        ),
    )


def write_source_atomicity_artifact(
    *,
    batch_context: SourceAtomicityBatchContext,
    candidate: CandidateKey,
    created_at: str,
    authorization: AuthorizationRecord,
) -> SourceAtomicityReceipt:
    """Replay current v2 source evidence and persist its mechanical decision."""

    _require_context_candidate(batch_context, candidate)
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
    ):
        raise PermissionError(
            "source atomicity requires candidate-screening authorization"
        )
    replay = _replay_current_v2_source(batch_context, candidate)
    _require_dependencies_not_after(
        replay.dependencies,
        created_at,
        enumeration_created_at=batch_context.enumeration_created_at,
    )
    reference = _atomicity_reference(candidate)
    write_stable_artifact(
        batch_context.artifact_root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=created_at,
        inputs=replay.inputs,
        payload=replay.payload(candidate),
        authorization=authorization,
    )
    return load_source_atomicity_artifact(
        batch_context=batch_context,
        candidate=candidate,
    )


def load_source_atomicity_artifact(
    *,
    batch_context: SourceAtomicityBatchContext,
    candidate: CandidateKey,
) -> SourceAtomicityReceipt:
    """Reload the current artifact and mechanically reproduce its conclusion."""

    _require_context_candidate(batch_context, candidate)
    reference = _atomicity_reference(candidate)
    artifact = load_stable_artifact(
        batch_context.artifact_root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    if artifact.authorization.scope != "candidate-screening":
        raise RuntimeError(
            "source atomicity current head lacks candidate-screening authorization"
        )
    replay = _replay_current_v2_source(batch_context, candidate)
    if artifact.inputs != replay.inputs:
        raise RuntimeError("source atomicity same-root input references drift")
    _require_dependencies_not_after(
        replay.dependencies,
        artifact.created_at,
        enumeration_created_at=batch_context.enumeration_created_at,
    )
    payload = artifact.payload
    expected = replay.payload(candidate)
    if type(payload) is not dict or set(payload) != _ATOMICITY_PAYLOAD_FIELDS:
        raise RuntimeError("source atomicity payload schema is invalid")
    _require_exact_json_value(
        payload,
        expected,
        "source atomicity mechanical replay",
    )
    return SourceAtomicityReceipt(
        artifact=artifact,
        candidate=candidate,
        source_manifest_path=replay.source_manifest_path,
        linked_issue_numbers=replay.linked_issue_numbers,
        authoritative_issue_numbers=replay.authoritative_issue_numbers,
        primary_issue_number=replay.primary_issue_number,
        outcome=replay.outcome,
        reason=replay.reason,
    )


def _replay_current_v2_source(
    batch_context: SourceAtomicityBatchContext,
    candidate: CandidateKey,
) -> _SourceReplay:
    root = batch_context.artifact_root
    candidate_references = _candidate_source_references(candidate)
    source, patch, checkpoint = tuple(
        load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
        for reference in candidate_references
    )
    enumeration_reference = batch_context.enumeration_reference
    references = (enumeration_reference, *candidate_references)
    _validate_source_packet(source, candidate, enumeration_reference)
    _validate_patch(
        patch,
        candidate,
        (enumeration_reference, candidate_references[0]),
    )
    _validate_checkpoint(
        checkpoint,
        candidate,
        enumeration_reference,
        candidate_references[1],
    )
    raw_evidence_path = source.payload["raw_evidence_path"]
    if patch.payload["source_manifest_path"] != raw_evidence_path:
        raise RuntimeError("source atomicity patch manifest binding drift")
    manifest_path, canonical_manifest_path = _safe_raw_manifest(
        root,
        raw_evidence_path,
    )
    manifest = _strict_json_object(manifest_path.read_bytes(), "source manifest")
    inventory = _issue_inventory(
        manifest,
        candidate,
    )
    authoritative = inventory.authoritative_issue_numbers
    if inventory.recapture_required:
        outcome: Literal["accepted", "rejected"] = "rejected"
        reason: str | None = "source_manifest_recapture_required"
        primary = None
    elif not authoritative or not inventory.details_complete:
        outcome = "rejected"
        reason = "authoritative_issue_missing"
        primary = None
    elif inventory.pull_request_masquerade:
        outcome = "rejected"
        reason = "authoritative_issue_is_pull_request"
        primary = None
    elif len(authoritative) != 1:
        outcome = "rejected"
        reason = "multiple_authoritative_issues"
        primary = None
    else:
        outcome = "accepted"
        reason = None
        primary = authoritative[0]
    return _SourceReplay(
        inputs=references,
        dependencies=(source, patch, checkpoint),
        source_manifest_path=canonical_manifest_path,
        linked_issue_numbers=inventory.linked_issue_numbers,
        authoritative_issue_numbers=authoritative,
        primary_issue_number=primary,
        outcome=outcome,
        reason=reason,
    )


def _typed_enumeration(
    artifact: StableArtifact,
    repository_key: str,
) -> RepositoryEnumeration:
    payload = artifact.payload
    if (
        artifact.inputs
        or type(payload) is not dict
        or set(payload) != _ENUMERATION_PAYLOAD_FIELDS
        or payload.get("repository_key") != repository_key
        or payload.get("complete") is not True
    ):
        raise RuntimeError("source atomicity v2 enumeration is invalid")
    try:
        return RepositoryEnumeration(
            repository_key=repository_key,
            merged_change_numbers=_number_tuple(
                payload["merged_change_numbers"],
                "merged changes",
            ),
            authoritative_issue_link_missing=_number_tuple(
                payload["authoritative_issue_link_missing"],
                "missing Issue links",
            ),
            repair_not_in_pinned=_number_tuple(
                payload["repair_not_in_pinned"],
                "unpinned repairs",
            ),
            candidates=tuple(
                CandidateKey.from_value(value)
                for value in _plain_list(payload["candidates"], "candidates")
            ),
            infrastructure_blocked=_number_tuple(
                payload["infrastructure_blocked"],
                "infrastructure blockers",
            ),
        )
    except ValueError as error:
        raise RuntimeError(
            "source atomicity enumeration partitions are invalid"
        ) from error


def _validate_source_packet(
    artifact: StableArtifact,
    candidate: CandidateKey,
    enumeration: ArtifactReference,
) -> None:
    if (
        artifact.inputs != (enumeration,)
        or type(artifact.payload) is not dict
        or set(artifact.payload) != _SOURCE_PAYLOAD_FIELDS
        or not _is_candidate_value(artifact.payload.get("candidate"), candidate)
        or artifact.payload.get("complete") is not True
        or type(artifact.payload.get("raw_evidence_path")) is not str
        or not artifact.payload["raw_evidence_path"]
    ):
        raise RuntimeError("source atomicity v2 source packet is invalid")


def _validate_patch(
    artifact: StableArtifact,
    candidate: CandidateKey,
    prerequisites: tuple[ArtifactReference, ArtifactReference],
) -> None:
    payload = artifact.payload
    if (
        artifact.inputs != prerequisites
        or type(payload) is not dict
        or set(payload) != _PATCH_PAYLOAD_FIELDS
        or not _is_candidate_value(payload.get("candidate"), candidate)
        or payload.get("outcome") != "patch_eligible"
        or payload.get("complete") is not True
        or not _nonempty_strings(payload.get("production_paths"))
        or not _nonempty_strings(payload.get("test_paths"))
        or type(payload.get("source_manifest_path")) is not str
        or not payload["source_manifest_path"]
        or type(payload.get("raw_patch_path")) is not str
        or not payload["raw_patch_path"]
    ):
        raise RuntimeError("source atomicity v2 patch is not eligible")


def _validate_checkpoint(
    artifact: StableArtifact,
    candidate: CandidateKey,
    enumeration: ArtifactReference,
    patch: ArtifactReference,
) -> None:
    payload = artifact.payload
    if (
        artifact.inputs != (enumeration, patch)
        or type(payload) is not dict
        or set(payload) != _CHECKPOINT_PAYLOAD_FIELDS
        or not _is_candidate_value(payload.get("candidate"), candidate)
        or payload.get("state") != "complete"
        or payload.get("outcome") != "patch_eligible"
        or payload.get("stage_artifact_id") != patch.artifact_id
    ):
        raise RuntimeError("source atomicity checkpoint is not patch eligible")


def _issue_inventory(
    manifest: dict[str, Any],
    candidate: CandidateKey,
) -> _IssueInventory:
    if (
        not _is_candidate_value(manifest.get("candidate"), candidate)
        or manifest.get("complete") is not True
    ):
        raise RuntimeError("source atomicity manifest candidate binding is invalid")
    pull = manifest.get("pull")
    if (
        type(pull) is not dict
        or type(pull.get("number")) is not int
        or pull["number"] != candidate.change_number
        or pull.get("merged") is not True
    ):
        raise RuntimeError("source atomicity manifest pull binding is invalid")
    raw_numbers = manifest.get("issue_numbers")
    if raw_numbers is None:
        numbers: tuple[int, ...] = ()
    elif type(raw_numbers) is not list or any(
        type(number) is not int or number <= 0 for number in raw_numbers
    ):
        raise RuntimeError("source atomicity issue inventory is invalid")
    else:
        numbers = tuple(raw_numbers)
    if numbers != tuple(sorted(set(numbers))):
        raise RuntimeError("source atomicity issue inventory is invalid")
    raw_issues = manifest.get("issues")
    if not numbers:
        if raw_issues is not None and raw_issues != []:
            raise RuntimeError("source atomicity issue inventory is invalid")
        raw_issues = []
    if type(raw_issues) is not list:
        raw_issues = []
    rows: dict[int, dict[str, Any]] = {}
    for raw_row in raw_issues:
        if type(raw_row) is not dict or set(raw_row) != _ISSUE_ROW_FIELDS:
            raise RuntimeError("source atomicity issue inventory is invalid")
        number = raw_row.get("number")
        if type(number) is not int or number <= 0 or number in rows:
            raise RuntimeError("source atomicity issue inventory is invalid")
        rows[number] = raw_row
    details_complete = tuple(sorted(rows)) == numbers
    masquerade = False
    for number, row in rows.items():
        issue = row.get("issue")
        if (
            type(issue) is not dict
            or type(issue.get("number")) is not int
            or issue["number"] != number
            or type(row.get("comments")) is not list
            or type(row.get("history")) is not list
        ):
            details_complete = False
            continue
        if (
            number == candidate.change_number
            or issue.get("pull_request") is not None
            or issue.get("__typename") == "PullRequest"
        ):
            masquerade = True
    projection = _relationship_projection(
        manifest,
        candidate,
        declared_issue_numbers=numbers,
        issue_rows=rows,
    )
    return _IssueInventory(
        linked_issue_numbers=projection.linked_issue_numbers,
        authoritative_issue_numbers=projection.authoritative_issue_numbers,
        details_complete=details_complete,
        pull_request_masquerade=masquerade,
        recapture_required=projection.recapture_required,
    )


def _relationship_projection(
    manifest: dict[str, Any],
    candidate: CandidateKey,
    *,
    declared_issue_numbers: tuple[int, ...],
    issue_rows: dict[int, dict[str, Any]],
) -> _RelationshipProjection:
    pull = manifest.get("pull")
    relationships = manifest.get("relationships")
    if type(pull) is not dict or type(relationships) is not list:
        raise RuntimeError("source atomicity authoritative relationships are invalid")
    if candidate.repository_key in {"jupyterlab", "luanti", "openra"}:
        if set(manifest) != _GITHUB_MANIFEST_FIELDS:
            raise RuntimeError("source atomicity GitHub manifest schema is invalid")
        repository = github_repository_for(candidate.repository_key)
        if manifest.get("provider") != "github" or manifest.get("authority") != {
            "provider": "github",
            "api_base": repository.api_base,
            "repository": repository.repository_name,
        }:
            raise RuntimeError("source atomicity GitHub authority is invalid")
        _validate_github_relationships(relationships)
        parsed_linked = _github_issue_numbers(pull, relationships, repository)
        linked = tuple(sorted({*declared_issue_numbers, *parsed_linked}))
        if parsed_linked != declared_issue_numbers:
            return _RelationshipProjection(linked, (), True)
        explicit = _explicit_closing_issue_numbers(pull, repository)
        if explicit:
            return _RelationshipProjection(linked, explicit, False)
        crossrefs = _same_repository_cross_reference_issue_numbers(
            relationships,
            repository,
        )
        eligible, timestamps_valid = _eligible_github_cross_references(
            pull,
            relationships,
            repository_api_url=repository.api_repository_url,
            issue_rows=issue_rows,
        )
        return _RelationshipProjection(
            linked,
            eligible,
            bool(crossrefs) and not timestamps_valid,
        )
    if candidate.repository_key == "blender":
        if set(manifest) != _GITEA_MANIFEST_FIELDS:
            raise RuntimeError("source atomicity Gitea manifest schema is invalid")
        parsed_linked = _gitea_issue_numbers(pull, relationships)
        linked = tuple(sorted({*declared_issue_numbers, *parsed_linked}))
        recapture_required = parsed_linked != declared_issue_numbers
        return _RelationshipProjection(
            linked,
            () if recapture_required else parsed_linked,
            recapture_required,
        )
    raise RuntimeError("source atomicity source provider is not registered")


def _validate_github_relationships(relationships: list[object]) -> None:
    for row in relationships:
        if type(row) is not dict:
            raise RuntimeError("source atomicity GitHub relationship is invalid")
        if row.get("event") != "cross-referenced":
            continue
        source = row.get("source")
        issue = source.get("issue") if type(source) is dict else None
        if (
            type(issue) is not dict
            or type(issue.get("number")) is not int
            or issue["number"] <= 0
            or type(issue.get("repository_url")) is not str
            or not issue["repository_url"]
            or (
                issue.get("pull_request") is not None
                and type(issue.get("pull_request")) is not dict
            )
        ):
            raise RuntimeError("source atomicity GitHub relationship is invalid")


def _eligible_github_cross_references(
    pull: dict[str, object],
    relationships: list[object],
    *,
    repository_api_url: str,
    issue_rows: dict[int, dict[str, Any]],
) -> tuple[tuple[int, ...], bool]:
    merged_at = _utc_timestamp(pull.get("merged_at"))
    if merged_at is None:
        return (), False
    eligible: set[int] = set()
    for row in relationships:
        assert type(row) is dict  # Validated by _validate_github_relationships.
        if row.get("event") != "cross-referenced":
            continue
        source = row["source"]
        assert type(source) is dict
        issue = source["issue"]
        assert type(issue) is dict
        if (
            issue["repository_url"] != repository_api_url
            or type(issue.get("pull_request")) is dict
        ):
            continue
        number = issue["number"]
        captured_row = issue_rows.get(number)
        captured_issue = (
            captured_row.get("issue") if type(captured_row) is dict else None
        )
        relationship_at = _utc_timestamp(row.get("created_at"))
        source_issue_at = _utc_timestamp(issue.get("created_at"))
        captured_issue_at = (
            _utc_timestamp(captured_issue.get("created_at"))
            if type(captured_issue) is dict
            else None
        )
        if (
            relationship_at is None
            or source_issue_at is None
            or captured_issue_at is None
            or source_issue_at != captured_issue_at
            or source_issue_at > relationship_at
        ):
            return (), False
        if relationship_at < merged_at:
            eligible.add(number)
    return tuple(sorted(eligible)), True


def _safe_raw_manifest(root: Path, value: object) -> tuple[Path, str]:
    resolved_root = root.resolve(strict=True)
    if type(value) is not str or not value:
        raise RuntimeError("source manifest path is missing")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or "." in relative.parts
        or ".." in relative.parts
        or value != relative.as_posix()
    ):
        raise RuntimeError("source manifest path is outside the artifact root")
    current = resolved_root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise RuntimeError("source manifest file is missing") from error
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("source manifest path contains a symlink")
    final = current.lstat()
    if not stat.S_ISREG(final.st_mode) or final.st_uid != os.getuid():
        raise RuntimeError("source manifest file is unsafe")
    if current.resolve(strict=True).parent != (resolved_root / relative.parent).resolve(
        strict=True
    ):
        raise RuntimeError("source manifest path is outside the artifact root")
    return current, relative.as_posix()


def _strict_json_object(content: bytes, label: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise RuntimeError(f"{label} contains duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise RuntimeError(f"{label} contains non-finite JSON: {value}")

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except UnicodeDecodeError as error:
        raise RuntimeError(f"{label} is not UTF-8 JSON") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{label} is invalid JSON") from error
    if type(value) is not dict:
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _require_dependencies_not_after(
    dependencies: tuple[StableArtifact, ...],
    created_at: str,
    *,
    enumeration_created_at: str,
) -> None:
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        dependency_times = tuple(
            datetime.fromisoformat(value.created_at.replace("Z", "+00:00"))
            for value in dependencies
        ) + (datetime.fromisoformat(enumeration_created_at.replace("Z", "+00:00")),)
    except (AttributeError, ValueError) as error:
        raise ValueError("source atomicity creation time is invalid") from error
    if any(value > created for value in dependency_times):
        raise RuntimeError("source atomicity artifact predates a current input")


def _candidate_source_references(
    candidate: CandidateKey,
) -> tuple[ArtifactReference, ...]:
    repository = candidate.repository_key
    change = candidate.change_number
    return (
        ArtifactReference(
            f"candidate_source_packet/{repository}/{change}/source/v2",
            "candidate_source_packet",
            _SEMANTIC_VERSION,
        ),
        ArtifactReference(
            f"candidate_test_patch/{repository}/{change}/screening/v2",
            "candidate_test_patch",
            _SEMANTIC_VERSION,
        ),
        ArtifactReference(
            f"screening_checkpoint/{repository}/{change}/current/v2",
            "screening_checkpoint",
            _SEMANTIC_VERSION,
        ),
    )


def _atomicity_reference(candidate: CandidateKey) -> ArtifactReference:
    return ArtifactReference(
        (
            f"source_atomicity/{candidate.repository_key}/"
            f"{candidate.change_number}/preannotation/v2"
        ),
        "source_atomicity",
        _SEMANTIC_VERSION,
    )


def _nonempty_strings(value: object) -> bool:
    return (
        type(value) is list
        and bool(value)
        and all(type(item) is str and bool(item) for item in value)
    )


def _strict_number_inventory(value: object) -> bool:
    return (
        type(value) is tuple
        and all(type(number) is int and number > 0 for number in value)
        and value == tuple(sorted(set(value)))
    )


def _utc_timestamp(value: object) -> datetime | None:
    if type(value) is not str or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        return None
    return parsed


def _require_candidate(candidate: CandidateKey) -> None:
    if type(candidate) is not CandidateKey:
        raise TypeError("source atomicity candidate must be typed")


def _require_context_candidate(
    batch_context: SourceAtomicityBatchContext,
    candidate: CandidateKey,
) -> None:
    if type(batch_context) is not SourceAtomicityBatchContext:
        raise TypeError("source atomicity batch context must be typed")
    batch_context.__post_init__()
    _require_candidate(candidate)
    if (
        candidate.repository_key != batch_context.repository_key
        or candidate.change_number not in batch_context.candidate_numbers
    ):
        raise RuntimeError("source atomicity candidate is outside the batch context")


def _plain_list(value: object, label: str) -> list[Any]:
    if type(value) is not list:
        raise ValueError(f"source atomicity enumeration {label} are invalid")
    return value


def _number_tuple(value: object, label: str) -> tuple[int, ...]:
    values = _plain_list(value, label)
    if any(type(number) is not int or number <= 0 for number in values):
        raise ValueError(f"source atomicity enumeration {label} are invalid")
    return tuple(values)


def _is_candidate_value(value: object, candidate: CandidateKey) -> bool:
    try:
        return CandidateKey.from_value(value) == candidate
    except ValueError:
        return False


def _require_exact_json_value(actual: object, expected: object, label: str) -> None:
    if type(actual) is not type(expected):
        raise RuntimeError(f"{label} exact type drift")
    if type(expected) is dict:
        if set(actual) != set(expected):
            raise RuntimeError(f"{label} structure drift")
        for key, expected_value in expected.items():
            _require_exact_json_value(actual[key], expected_value, f"{label}.{key}")
        return
    if type(expected) is list:
        if len(actual) != len(expected):
            raise RuntimeError(f"{label} structure drift")
        for index, (actual_value, expected_value) in enumerate(
            zip(actual, expected, strict=True)
        ):
            _require_exact_json_value(
                actual_value,
                expected_value,
                f"{label}[{index}]",
            )
        return
    if actual != expected:
        raise RuntimeError(f"{label} drift")


__all__ = [
    "SourceAtomicityBatchContext",
    "SourceAtomicityReceipt",
    "load_source_atomicity_batch_context",
    "load_source_atomicity_artifact",
    "write_source_atomicity_artifact",
]
