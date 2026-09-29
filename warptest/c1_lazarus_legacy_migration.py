"""Read-only adapter from retired C1 digest artifacts to stable identities.

This is the only module permitted to understand the retired field names and
content-addressed layout.  It never writes those fields into the new protocol.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from base64 import b64decode
from binascii import Error as Base64Error
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)
from .c1_lazarus_selection.enumeration import (
    RepositoryEnumeration,
    write_repository_enumeration,
)
from .c1_lazarus_selection.models import CandidateKey


_LEGACY_IDENTITY = re.compile(r"[0-9a-f]{64}")
_RETIRED_KEYS = frozenset(
    {
        "artifact_sha256",
        "merge_commit",
        "patch_base_commit",
        "frame_sha256",
        "producer_implementation",
        "production_authority",
        "authority_mac_sha256",
    }
)


@dataclass(frozen=True)
class MigrationResult:
    repository_key: str
    reused_change_numbers: tuple[int, ...]
    redo_change_numbers: tuple[int, ...]
    batch_artifact_path: Path


def migrate_repository_enumeration(
    legacy_artifact_path: str | Path,
    *,
    stable_root: str | Path,
    authorization: AuthorizationRecord,
) -> Path:
    """Stream one retired enumeration into exact stable numeric partitions."""

    path = Path(legacy_artifact_path).resolve(strict=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("legacy enumeration path is unsafe")
    repository_key = _jq_scalar(path, ".payload.repository.key")
    merged_count = int(_jq_scalar(path, ".payload.merged_discovered"))
    merged = tuple(
        sorted(
            int(value)
            for value in _jq_lines(path, ".payload.discovered_changes[].number")
        )
    )
    excluded = tuple(
        sorted(
            int(value) for value in _jq_lines(path, ".payload.excluded_not_in_pinned[]")
        )
    )
    missing: list[int] = []
    candidates: list[CandidateKey] = []
    query = (
        r'.payload.candidates[] | "\(.change.number)\t\(.gate_failures | join(","))"'
    )
    for line in _jq_lines(path, query):
        number_text, separator, failures = line.partition("\t")
        if not separator:
            raise RuntimeError("legacy candidate partition is invalid")
        number = int(number_text)
        if not failures:
            candidates.append(CandidateKey(repository_key, number))
        elif failures == "authoritative_issue_link_missing":
            missing.append(number)
        else:
            raise RuntimeError("legacy enumeration has an unsupported gate outcome")
    if len(merged) != merged_count:
        raise RuntimeError("legacy enumeration merged coverage is inconsistent")
    enumeration = RepositoryEnumeration(
        repository_key=repository_key,
        merged_change_numbers=merged,
        authoritative_issue_link_missing=tuple(sorted(missing)),
        repair_not_in_pinned=excluded,
        candidates=tuple(sorted(candidates)),
    )
    return write_repository_enumeration(
        stable_root,
        enumeration,
        authorization=authorization,
    )


def migrate_screening_checkpoint(
    checkpoint_path: str | Path,
    *,
    legacy_artifact_root: str | Path,
    stable_root: str | Path,
    authorization: AuthorizationRecord,
    redo_change_numbers: frozenset[int] = frozenset(),
) -> MigrationResult:
    """Migrate one old checkpoint without validating retired digests."""

    checkpoint = _read_regular_json(Path(checkpoint_path))
    artifact_root = Path(legacy_artifact_root).resolve(strict=True)
    if not artifact_root.is_dir():
        raise RuntimeError("legacy artifact root is invalid")
    repository_key = _plain_name(checkpoint.get("repository_key"), "repository")
    records = checkpoint.get("records")
    if type(records) is not list:
        raise RuntimeError("legacy checkpoint records are invalid")
    candidates: list[dict[str, Any]] = []
    reused: list[int] = []
    redo: list[int] = []
    for raw in records:
        if type(raw) is not dict:
            raise RuntimeError("legacy checkpoint record is invalid")
        candidate = raw.get("candidate")
        if type(candidate) is not dict:
            raise RuntimeError("legacy checkpoint candidate is invalid")
        if candidate.get("repository_key") != repository_key:
            raise RuntimeError("legacy checkpoint candidate repository drift")
        change_number = candidate.get("change_number")
        if type(change_number) is not int or change_number <= 0:
            raise RuntimeError("legacy checkpoint candidate number is invalid")
        outcome = raw.get("outcome")
        if outcome not in {"source_rejected", "patch_rejected", "patch_eligible"}:
            raise RuntimeError("legacy checkpoint outcome is invalid")
        candidate_value = {
            "repository_key": repository_key,
            "change_number": change_number,
        }
        if change_number in redo_change_numbers:
            checkpoint_path_value = write_stable_artifact(
                stable_root,
                artifact_id=(
                    f"screening_checkpoint/{repository_key}/{change_number}/current/v1"
                ),
                artifact_kind="screening_checkpoint",
                semantic_version="1.0",
                created_at=authorization.granted_at,
                inputs=(),
                payload={
                    "candidate": candidate_value,
                    "state": "redo_required",
                    "reason": "targeted_runtime_repair",
                },
                authorization=authorization,
            )
            redo.append(change_number)
            candidates.append(
                {
                    **candidate_value,
                    "state": "redo_required",
                    "checkpoint_id": _artifact_id_from_path(
                        checkpoint_path_value, stable_root
                    ),
                }
            )
            continue

        legacy_identity = raw.get("stage_artifact_sha256")
        if type(legacy_identity) is not str or not _LEGACY_IDENTITY.fullmatch(
            legacy_identity
        ):
            raise RuntimeError("legacy stage identity is invalid")
        legacy_stage = _read_regular_json(
            artifact_root / f"{legacy_identity}.json",
            required_parent=artifact_root,
        )
        created_at = authorization.granted_at
        stage_kind, stage_id = _stable_stage_identity(
            repository_key,
            change_number,
            outcome,
        )
        stage_inputs: tuple[ArtifactReference, ...] = ()
        stage_payload = {
            "candidate": candidate_value,
            "outcome": outcome,
            "complete": True,
            "migration_state": "reused",
        }
        if outcome in {"patch_rejected", "patch_eligible"}:
            source_reference = _migrate_issue_source_dependency(
                legacy_stage,
                artifact_root=artifact_root,
                stable_root=stable_root,
                repository_key=repository_key,
                change_number=change_number,
                authorization=authorization,
            )
            stage_inputs = (source_reference,)
            stage_payload.update(
                _migrate_patch_projection(
                    legacy_stage,
                    stable_root=stable_root,
                    repository_key=repository_key,
                    change_number=change_number,
                    outcome=outcome,
                )
            )
        elif outcome == "source_rejected":
            legacy_payload = legacy_stage.get("payload")
            if type(legacy_payload) is dict:
                reason = legacy_payload.get("failure_code")
                if type(reason) is str and reason:
                    stage_payload["reason"] = reason
        sanitized = _sanitize_legacy_value(legacy_stage.get("payload", {}))
        if type(sanitized) is dict and sanitized.get("complete") is True:
            stage_payload["complete"] = True
        stage_path = write_stable_artifact(
            stable_root,
            artifact_id=stage_id,
            artifact_kind=stage_kind,
            semantic_version="1.0",
            created_at=created_at,
            inputs=stage_inputs,
            payload=stage_payload,
            authorization=authorization,
        )
        stage_reference = ArtifactReference(stage_id, stage_kind, "1.0")
        checkpoint_id = (
            f"screening_checkpoint/{repository_key}/{change_number}/current/v1"
        )
        checkpoint_path_value = write_stable_artifact(
            stable_root,
            artifact_id=checkpoint_id,
            artifact_kind="screening_checkpoint",
            semantic_version="1.0",
            created_at=created_at,
            inputs=(stage_reference,),
            payload={
                "candidate": candidate_value,
                "state": "complete",
                "outcome": outcome,
                "stage_artifact_id": stage_id,
            },
            authorization=authorization,
        )
        reused.append(change_number)
        candidates.append(
            {
                **candidate_value,
                "state": "complete",
                "outcome": outcome,
                "stage_artifact_id": _artifact_id_from_path(stage_path, stable_root),
                "checkpoint_id": _artifact_id_from_path(
                    checkpoint_path_value, stable_root
                ),
            }
        )

    candidates.sort(key=lambda value: value["change_number"])
    batch_path = write_stable_artifact(
        stable_root,
        artifact_id=f"screening_batch/{repository_key}/current/v1",
        artifact_kind="screening_batch",
        semantic_version="1.0",
        created_at=authorization.granted_at,
        inputs=(),
        payload={
            "repository_key": repository_key,
            "candidates": candidates,
            "reused_count": len(reused),
            "redo_count": len(redo),
            "complete": not redo,
        },
        authorization=authorization,
    )
    return MigrationResult(
        repository_key=repository_key,
        reused_change_numbers=tuple(sorted(reused)),
        redo_change_numbers=tuple(sorted(redo)),
        batch_artifact_path=batch_path,
    )


def _migrate_issue_source_dependency(
    legacy_stage: dict[str, Any],
    *,
    artifact_root: Path,
    stable_root: str | Path,
    repository_key: str,
    change_number: int,
    authorization: AuthorizationRecord,
) -> ArtifactReference:
    inputs = legacy_stage.get("inputs")
    if type(inputs) is not list or len(inputs) != 1 or type(inputs[0]) is not dict:
        raise RuntimeError("legacy patch lacks its exact source dependency")
    source_input = inputs[0]
    if source_input.get("artifact_kind") != "issue_source_packet":
        raise RuntimeError("legacy patch source dependency kind is invalid")
    source_identity = source_input.get("artifact_sha256")
    if type(source_identity) is not str or not _LEGACY_IDENTITY.fullmatch(
        source_identity
    ):
        raise RuntimeError("legacy source dependency identity is invalid")
    source = _read_regular_json(
        artifact_root / f"{source_identity}.json",
        required_parent=artifact_root,
    )
    if source.get("artifact_kind") != "issue_source_packet":
        raise RuntimeError("legacy source dependency artifact is invalid")
    payload = source.get("payload")
    if type(payload) is not dict or payload.get("complete") is not True:
        raise RuntimeError("legacy source dependency is incomplete")
    change = payload.get("change")
    repository = payload.get("repository")
    if (
        type(change) is not dict
        or change.get("number") != change_number
        or type(repository) is not dict
        or repository.get("key") != repository_key
    ):
        raise RuntimeError("legacy source dependency candidate drift")
    source_evidence = _sanitize_legacy_value(payload)
    if type(source_evidence) is not dict:
        raise RuntimeError("legacy source evidence cannot be projected")
    artifact_id = f"candidate_source_packet/{repository_key}/{change_number}/source/v1"
    created_at = authorization.granted_at
    write_stable_artifact(
        stable_root,
        artifact_id=artifact_id,
        artifact_kind="candidate_source_packet",
        semantic_version="1.0",
        created_at=created_at,
        inputs=(),
        payload={
            "candidate": {
                "repository_key": repository_key,
                "change_number": change_number,
            },
            "complete": True,
            "migration_state": "reused",
            "source_evidence": source_evidence,
        },
        authorization=authorization,
    )
    return ArtifactReference(artifact_id, "candidate_source_packet", "1.0")


def _migrate_patch_projection(
    legacy_stage: dict[str, Any],
    *,
    stable_root: str | Path,
    repository_key: str,
    change_number: int,
    outcome: str,
) -> dict[str, Any]:
    payload = legacy_stage.get("payload")
    if type(payload) is not dict:
        raise RuntimeError("legacy patch payload is invalid")
    production = _path_list(payload.get("production_files"), "production")
    tests = _path_list(payload.get("test_files"), "test")
    support = _path_list(payload.get("support_files"), "support")
    encoded_patch: Any = payload.get("raw_patch_base64")
    if outcome == "patch_eligible":
        capture = payload.get("capture")
        if type(capture) is not dict or capture.get("exit_code") != 0:
            raise RuntimeError("legacy eligible patch capture is invalid")
        response = capture.get("response_canonical_json")
        if type(response) is not str:
            raise RuntimeError("legacy eligible patch response is missing")
        try:
            parsed_response = json.loads(response)
        except json.JSONDecodeError as exc:
            raise RuntimeError("legacy eligible patch response is invalid") from exc
        if type(parsed_response) is not dict:
            raise RuntimeError("legacy eligible patch response is invalid")
        encoded_patch = parsed_response.get("patch_base64")
    if type(encoded_patch) is not str or not encoded_patch:
        raise RuntimeError("legacy patch bytes are missing")
    try:
        patch_bytes = b64decode(encoded_patch, validate=True)
    except (Base64Error, ValueError) as exc:
        raise RuntimeError("legacy patch bytes are invalid") from exc
    if not patch_bytes:
        raise RuntimeError("legacy patch bytes are empty")
    relative_path = Path(
        "raw", "migrated", repository_key, str(change_number), "patch.diff"
    )
    _write_immutable_raw(Path(stable_root), relative_path, patch_bytes)
    result: dict[str, Any] = {
        "production_paths": production,
        "test_paths": tests,
        "support_paths": support,
        "raw_patch_path": relative_path.as_posix(),
    }
    if outcome == "patch_rejected":
        reason = payload.get("rejection_reason")
        if type(reason) is not str or not reason:
            raise RuntimeError("legacy patch rejection reason is missing")
        result["reason"] = reason
    return result


def _path_list(value: Any, label: str) -> list[str]:
    if type(value) is not list:
        raise RuntimeError(f"legacy {label} path list is invalid")
    paths: list[str] = []
    for item in value:
        if (
            type(item) is not str
            or not item
            or item.startswith("/")
            or ".." in Path(item).parts
        ):
            raise RuntimeError(f"legacy {label} path is invalid")
        paths.append(item)
    return paths


def _write_immutable_raw(root: Path, relative_path: Path, content: bytes) -> None:
    resolved_root = root.resolve()
    destination = resolved_root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        info = destination.lstat()
        if not stat.S_ISREG(info.st_mode) or destination.read_bytes() != content:
            raise RuntimeError("migrated raw evidence conflicts with current content")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _stable_stage_identity(
    repository_key: str,
    change_number: int,
    outcome: str,
) -> tuple[str, str]:
    if outcome == "patch_eligible":
        return (
            "candidate_test_patch",
            f"candidate_test_patch/{repository_key}/{change_number}/screening/v1",
        )
    stage = "source" if outcome == "source_rejected" else "patch"
    return (
        f"candidate_{stage}_terminal",
        f"candidate_{stage}_terminal/{repository_key}/{change_number}/{stage}/v1",
    )


def _sanitize_legacy_value(value: Any) -> Any:
    if value is None or type(value) in {str, int, float, bool}:
        return value
    if type(value) is list:
        return [_sanitize_legacy_value(item) for item in value]
    if type(value) is dict:
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if (
                type(key) is not str
                or key in _RETIRED_KEYS
                or "sha" in key.casefold()
                or "digest" in key.casefold()
                or key in {"commit", "tree", "blob"}
            ):
                continue
            if key == "candidate" and type(item) is dict:
                repository = item.get("repository_key")
                number = item.get("change_number")
                if type(repository) is str and type(number) is int:
                    sanitized[key] = {
                        "repository_key": repository,
                        "change_number": number,
                    }
                continue
            sanitized[key] = _sanitize_legacy_value(item)
        return sanitized
    return None


def _read_regular_json(
    path: Path,
    *,
    required_parent: Path | None = None,
) -> dict[str, Any]:
    candidate = path.absolute()
    if required_parent is not None:
        parent = required_parent.resolve(strict=True)
        if candidate.parent.resolve(strict=True) != parent:
            raise RuntimeError("legacy artifact is outside its root")
    info = candidate.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("legacy JSON path is unsafe")
    value = json.loads(candidate.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise RuntimeError("legacy JSON document is invalid")
    return value


def _plain_name(value: Any, label: str) -> str:
    if type(value) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", value):
        raise RuntimeError(f"legacy {label} is invalid")
    return value


def _artifact_id_from_path(path: Path, stable_root: str | Path) -> str:
    parts = (
        path.resolve(strict=True)
        .relative_to(Path(stable_root).resolve(strict=True) / "artifacts")
        .parts
    )
    if len(parts) < 3:
        raise RuntimeError("migrated artifact path is invalid")
    return "/".join(parts[:-2])


def _jq_scalar(path: Path, query: str) -> str:
    values = _jq_lines(path, query)
    if len(values) != 1 or not values[0]:
        raise RuntimeError("legacy enumeration scalar is invalid")
    return values[0]


def _jq_lines(path: Path, query: str) -> tuple[str, ...]:
    completed = subprocess.run(
        ("jq", "-r", query, str(path)),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("legacy enumeration cannot be streamed")
    return tuple(line for line in completed.stdout.splitlines() if line)


__all__ = [
    "MigrationResult",
    "migrate_repository_enumeration",
    "migrate_screening_checkpoint",
]
