#!/usr/bin/env python3
"""Commit one offline-recomputed enumeration into the isolated v2 epoch.

This command only reads the retained GitHub capture population and local Git
mirror.  It never fetches a network resource or starts source, patch, or
candidate-batch work.  A pre-existing v2 enumeration is resumed only when its
typed population exactly matches the newly derived offline proposal.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from warptest.c1_lazarus_selection.enumeration import (
    RepositoryEnumeration,
    load_repository_enumeration,
    recompute_repository_enumeration_from_gitea_captures,
    recompute_repository_enumeration_from_github_captures,
    write_enumeration_audit_population,
    write_repository_enumeration,
)
from warptest.c1_lazarus_selection.epoch import SELECTION_EPOCH_V2
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    load_stable_artifact,
)


_BLENDER_GITEA_API_BASE = "https://projects.blender.org/api/v1"
_BLENDER_REPOSITORY_NAME = "blender/blender"


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.confirm:
        _parser().error("v2 enumeration commit requires the explicit --confirm flag")
    authorization = AuthorizationRecord(
        authorization_id=arguments.authorization_id,
        scope="repository-enumeration",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )
    baseline = load_repository_enumeration(
        arguments.artifact_root,
        repository_key=arguments.repository,
    )
    _require_explicit_mirror_ref(arguments.pinned_ref)
    proposal = _recompute_offline(arguments, baseline)
    if proposal.baseline != baseline:
        raise RuntimeError("offline recomputation baseline does not match the v1 head")
    if proposal.target_reference != SELECTION_EPOCH_V2.enumeration_reference(
        arguments.repository
    ):
        raise RuntimeError("offline recomputation target does not match the v2 head")

    existing = _load_v2_or_none(arguments.artifact_root, arguments.repository)
    if existing is None:
        write_repository_enumeration(
            arguments.artifact_root,
            proposal.recomputed,
            authorization=authorization,
            selection_epoch=SELECTION_EPOCH_V2,
        )
        enumeration = load_repository_enumeration(
            arguments.artifact_root,
            repository_key=arguments.repository,
            selection_epoch=SELECTION_EPOCH_V2,
        )
        action = "written"
    else:
        enumeration = existing
        action = "resumed"
    if enumeration != proposal.recomputed:
        raise RuntimeError(
            "existing v2 enumeration diverges from the offline recomputation"
        )

    audit = _load_audit_population_or_none(
        arguments.artifact_root,
        enumeration,
        persistence_workers=arguments.persistence_workers,
    )
    if audit is None:
        audit = write_enumeration_audit_population(
            arguments.artifact_root,
            enumeration,
            authorization=authorization,
            persistence_workers=arguments.persistence_workers,
            selection_epoch=SELECTION_EPOCH_V2,
        )
        _require_audit_population(
            arguments.artifact_root,
            enumeration,
            persistence_workers=arguments.persistence_workers,
        )
        audit_action = "written"
    else:
        audit_action = "resumed"
    reloaded = load_repository_enumeration(
        arguments.artifact_root,
        repository_key=arguments.repository,
        selection_epoch=SELECTION_EPOCH_V2,
    )
    if reloaded != enumeration:
        raise RuntimeError(
            "v2 enumeration changed while audit terminals were persisted"
        )
    print(
        json.dumps(
            {
                "repository_key": arguments.repository,
                "enumeration_action": action,
                "audit_action": audit_action,
                "merged_count": audit.merged_count,
                "audit_count": audit.audit_count,
                "terminal_count": audit.terminal_count,
                "deep_screen_count": audit.deep_screen_count,
                "persistence_workers": arguments.persistence_workers,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--pinned-ref", required=True)
    parser.add_argument("--expected-repository-name", required=True)
    parser.add_argument("--expected-api-base")
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--persistence-workers", type=int, choices=(1, 2), default=2)
    return parser


def _recompute_offline(
    arguments: argparse.Namespace,
    baseline: RepositoryEnumeration,
):
    if arguments.repository == "blender":
        if arguments.expected_api_base != _BLENDER_GITEA_API_BASE:
            raise RuntimeError(
                "Blender v2 recomputation requires "
                f"--expected-api-base {_BLENDER_GITEA_API_BASE}"
            )
        if arguments.expected_repository_name != _BLENDER_REPOSITORY_NAME:
            raise RuntimeError(
                "Blender v2 recomputation requires "
                f"--expected-repository-name {_BLENDER_REPOSITORY_NAME}"
            )
        return recompute_repository_enumeration_from_gitea_captures(
            baseline,
            capture_root=arguments.capture_root,
            mirror_path=arguments.mirror,
            pinned_ref=arguments.pinned_ref,
            expected_repository_name=arguments.expected_repository_name,
            expected_api_base=arguments.expected_api_base,
        )
    if arguments.expected_api_base is not None:
        raise RuntimeError("--expected-api-base is only valid for Blender Gitea")
    return recompute_repository_enumeration_from_github_captures(
        baseline,
        capture_root=arguments.capture_root,
        mirror_path=arguments.mirror,
        pinned_ref=arguments.pinned_ref,
        expected_repository_name=arguments.expected_repository_name,
    )


def _require_explicit_mirror_ref(value: object) -> None:
    if (
        type(value) is not str
        or not value.startswith("refs/remotes/origin/")
        or any(character.isspace() for character in value)
    ):
        raise RuntimeError(
            "v2 enumeration recomputation requires an explicit mirror ref"
        )


def _load_v2_or_none(
    artifact_root: Path, repository_key: str
) -> RepositoryEnumeration | None:
    try:
        return load_repository_enumeration(
            artifact_root,
            repository_key=repository_key,
            selection_epoch=SELECTION_EPOCH_V2,
        )
    except RuntimeError as error:
        if str(error) == "artifact directory is missing":
            return None
        raise


def _load_audit_population_or_none(
    artifact_root: Path,
    enumeration: RepositoryEnumeration,
    *,
    persistence_workers: int,
) -> object | None:
    try:
        _require_audit_population(
            artifact_root,
            enumeration,
            persistence_workers=persistence_workers,
        )
    except _AuditArtifactMissing:
        return None
    return _audit_summary(enumeration)


def _require_audit_population(
    artifact_root: Path,
    enumeration: RepositoryEnumeration,
    *,
    persistence_workers: int,
) -> None:
    """Typed-load the complete v2 audit/terminal population with <=2 readers."""

    reference = SELECTION_EPOCH_V2.enumeration_reference(enumeration.repository_key)
    missing = set(enumeration.authoritative_issue_link_missing)
    not_pinned = set(enumeration.repair_not_in_pinned)
    blocked = set(enumeration.infrastructure_blocked)

    def load(change_number: int) -> None:
        candidate = CandidateKey(enumeration.repository_key, change_number)
        audit_id = (
            f"enumeration_audit/{enumeration.repository_key}/{change_number}/current/v2"
        )
        _require_artifact(
            artifact_root,
            artifact_id=audit_id,
            artifact_kind="enumeration_audit",
            expected_inputs=(reference,),
            expected_payload={"candidate": candidate.to_value(), "complete": True},
        )
        reason, stage = _terminal_reason_and_stage(
            change_number,
            missing=missing,
            not_pinned=not_pinned,
            blocked=blocked,
        )
        if reason is None:
            return
        terminal_id = (
            f"enumeration_terminal/{enumeration.repository_key}/{change_number}/"
            f"{stage}/v2"
        )
        _require_artifact(
            artifact_root,
            artifact_id=terminal_id,
            artifact_kind="enumeration_terminal",
            expected_inputs=(reference,),
            expected_payload={
                "candidate": candidate.to_value(),
                "reason": reason,
                "complete": True,
            },
        )

    with ThreadPoolExecutor(max_workers=persistence_workers) as pool:
        list(pool.map(load, enumeration.merged_change_numbers))


def _require_artifact(
    artifact_root: Path,
    *,
    artifact_id: str,
    artifact_kind: str,
    expected_inputs: tuple[object, ...],
    expected_payload: dict[str, object],
) -> None:
    try:
        artifact = load_stable_artifact(
            artifact_root,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            semantic_version=SELECTION_EPOCH_V2.semantic_version,
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
            "artifact file is missing",
        }:
            raise _AuditArtifactMissing() from error
        raise
    if artifact.inputs != expected_inputs or artifact.payload != expected_payload:
        raise RuntimeError(
            "v2 enumeration audit artifact is not the declared population"
        )


def _terminal_reason_and_stage(
    change_number: int,
    *,
    missing: set[int],
    not_pinned: set[int],
    blocked: set[int],
) -> tuple[str | None, str | None]:
    if change_number in missing:
        return "authoritative_issue_link_missing", "issue-link"
    if change_number in not_pinned:
        return "repair_not_in_pinned", "not-in-pinned"
    if change_number in blocked:
        return "infrastructure_blocked", "infrastructure-blocked"
    return None, None


def _audit_summary(enumeration: RepositoryEnumeration):
    terminal_count = (
        len(enumeration.authoritative_issue_link_missing)
        + len(enumeration.repair_not_in_pinned)
        + len(enumeration.infrastructure_blocked)
    )
    return _AuditSummary(
        merged_count=len(enumeration.merged_change_numbers),
        audit_count=len(enumeration.merged_change_numbers),
        terminal_count=terminal_count,
        deep_screen_count=len(enumeration.candidates),
    )


class _AuditArtifactMissing(RuntimeError):
    """The v2 audit population is incomplete and may be persisted once."""


class _AuditSummary:
    def __init__(
        self,
        *,
        merged_count: int,
        audit_count: int,
        terminal_count: int,
        deep_screen_count: int,
    ) -> None:
        self.merged_count = merged_count
        self.audit_count = audit_count
        self.terminal_count = terminal_count
        self.deep_screen_count = deep_screen_count


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
