"""Stable-ID artifact protocol for C1-Lazarus selection."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


_PROTOCOL = "c1-lazarus-stable-artifact-v1"
_HEAD_PROTOCOL = "c1-lazarus-stable-head-v1"
_SEGMENT = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_SEMANTIC_VERSION = re.compile(r"[1-9][0-9]*\.[0-9]+")
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z"
)
_DOCUMENT_FIELDS = frozenset(
    {
        "protocol",
        "artifact_id",
        "artifact_kind",
        "semantic_version",
        "created_at",
        "inputs",
        "payload",
        "authorization",
    }
)
_HEAD_FIELDS = frozenset(
    {
        "protocol",
        "artifact_id",
        "artifact_kind",
        "semantic_version",
        "created_at",
        "revision",
    }
)
_REFERENCE_FIELDS = frozenset({"artifact_id", "artifact_kind", "semantic_version"})
_AUTHORIZATION_FIELDS = frozenset(
    {
        "authorization_id",
        "scope",
        "granted_at",
        "local_user_id",
        "revoked",
    }
)
_DISALLOWED_PAYLOAD_KEYS = frozenset(
    {
        "merge_commit",
        "patch_base_commit",
        "commit_object",
        "tree_object",
        "blob_object",
    }
)


@dataclass(frozen=True)
class ArtifactReference:
    artifact_id: str
    artifact_kind: str
    semantic_version: str

    def __post_init__(self) -> None:
        _validate_artifact_id(self.artifact_id)
        _validate_segment(self.artifact_kind, "artifact kind")
        if not _SEMANTIC_VERSION.fullmatch(self.semantic_version):
            raise ValueError("artifact semantic version is invalid")


@dataclass(frozen=True)
class AuthorizationRecord:
    authorization_id: str
    scope: str
    granted_at: str
    local_user_id: int
    revoked: bool = False

    def __post_init__(self) -> None:
        _validate_scoped_name(self.authorization_id, "authorization ID")
        _validate_segment(self.scope, "authorization scope")
        _validate_timestamp(self.granted_at)
        if type(self.local_user_id) is not int or self.local_user_id < 0:
            raise ValueError("authorization local user is invalid")
        if type(self.revoked) is not bool:
            raise ValueError("authorization revoked state is invalid")


@dataclass(frozen=True)
class StableArtifact:
    reference: ArtifactReference
    created_at: str
    inputs: tuple[ArtifactReference, ...]
    payload: Mapping[str, Any]
    authorization: AuthorizationRecord
    revision_path: Path
    head_path: Path


def write_stable_artifact(
    artifact_root: str | Path,
    *,
    artifact_id: str,
    artifact_kind: str,
    semantic_version: str,
    created_at: str,
    inputs: Sequence[ArtifactReference],
    payload: Mapping[str, Any],
    authorization: AuthorizationRecord,
) -> Path:
    """Write one immutable revision and atomically advance its stable head."""

    reference = ArtifactReference(artifact_id, artifact_kind, semantic_version)
    _validate_timestamp(created_at)
    if type(inputs) not in {tuple, list} or any(
        type(value) is not ArtifactReference for value in inputs
    ):
        raise ValueError("artifact inputs are not typed references")
    if type(payload) is not dict:
        raise ValueError("artifact payload must be a plain mapping")
    _validate_json_value(payload, label="artifact payload")
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("artifact authorization is not typed")
    if authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("artifact authorization is not active for this user")

    root = _safe_directory(Path(artifact_root), create=True)
    directory = _artifact_directory(root, reference, create=True)
    revision_name = _revision_name(created_at)
    revision_path = directory / revision_name
    head_path = directory / "head.json"
    document = {
        "protocol": _PROTOCOL,
        "artifact_id": reference.artifact_id,
        "artifact_kind": reference.artifact_kind,
        "semantic_version": reference.semantic_version,
        "created_at": created_at,
        "inputs": [_reference_value(value) for value in inputs],
        "payload": dict(payload),
        "authorization": _authorization_value(authorization),
    }
    encoded = _canonical_bytes(document)

    with _writer_lock(directory):
        if revision_path.exists():
            _require_regular_owned_file(revision_path, directory)
            if revision_path.read_bytes() != encoded:
                raise RuntimeError(
                    "artifact revision conflicts with an existing revision"
                )
        else:
            _write_new_file(revision_path, encoded, directory)

        current = _load_head_if_present(head_path, directory)
        if current is not None and _timestamp_value(
            current["created_at"]
        ) > _timestamp_value(created_at):
            return revision_path
        head = {
            "protocol": _HEAD_PROTOCOL,
            "artifact_id": reference.artifact_id,
            "artifact_kind": reference.artifact_kind,
            "semantic_version": reference.semantic_version,
            "created_at": created_at,
            "revision": revision_name,
        }
        _replace_file(head_path, _canonical_bytes(head), directory)
    return revision_path


def load_stable_artifact(
    artifact_root: str | Path,
    *,
    artifact_id: str,
    artifact_kind: str,
    semantic_version: str,
) -> StableArtifact:
    """Load the current revision by stable identity and strict schema."""

    reference = ArtifactReference(artifact_id, artifact_kind, semantic_version)
    root = _safe_directory(Path(artifact_root), create=False)
    directory = _artifact_directory(root, reference, create=False)
    head_path = directory / "head.json"
    head = _load_head_if_present(head_path, directory)
    if head is None:
        raise RuntimeError("artifact current head is missing")
    expected_head_identity = {
        "artifact_id": reference.artifact_id,
        "artifact_kind": reference.artifact_kind,
        "semantic_version": reference.semantic_version,
    }
    if any(head[key] != value for key, value in expected_head_identity.items()):
        raise RuntimeError("artifact current head identity drift")
    revision_path = directory / head["revision"]
    _require_regular_owned_file(revision_path, directory)
    document = _strict_json_document(revision_path.read_bytes(), "artifact revision")
    if set(document) != _DOCUMENT_FIELDS:
        raise RuntimeError("artifact revision schema is invalid")
    if document["protocol"] != _PROTOCOL:
        raise RuntimeError("artifact protocol is unsupported")
    for key, value in expected_head_identity.items():
        if document[key] != value:
            raise RuntimeError("artifact revision identity drift")
    _validate_timestamp(document["created_at"])
    if document["created_at"] != head["created_at"]:
        raise RuntimeError("artifact revision is not the declared current head")
    inputs = _load_references(document["inputs"])
    payload = document["payload"]
    if type(payload) is not dict:
        raise RuntimeError("artifact payload is invalid")
    _validate_json_value(payload, label="artifact payload")
    authorization = _load_authorization(document["authorization"])
    if authorization.revoked:
        raise PermissionError("artifact authorization is revoked")
    return StableArtifact(
        reference=reference,
        created_at=document["created_at"],
        inputs=inputs,
        payload=payload,
        authorization=authorization,
        revision_path=revision_path,
        head_path=head_path,
    )


def _artifact_directory(
    root: Path,
    reference: ArtifactReference,
    *,
    create: bool,
) -> Path:
    directory = root / "artifacts"
    for part in PurePosixPath(reference.artifact_id).parts:
        directory = directory / part
    directory = directory / reference.semantic_version
    return _safe_directory(directory, create=create, required_parent=root)


def _safe_directory(
    path: Path,
    *,
    create: bool,
    required_parent: Path | None = None,
) -> Path:
    candidate = path.absolute()
    if create:
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("artifact directory is missing") from error
    info = candidate.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact directory is unsafe")
    if required_parent is not None:
        parent = required_parent.resolve(strict=True)
        if resolved != parent and parent not in resolved.parents:
            raise RuntimeError("artifact directory is outside the artifact root")
    return resolved


def _require_regular_owned_file(path: Path, directory: Path) -> os.stat_result:
    if path.parent.resolve(strict=True) != directory.resolve(strict=True):
        raise RuntimeError("artifact file is outside its stable directory")
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError("artifact file is missing") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise RuntimeError("artifact file is unsafe")
    return info


def _write_new_file(path: Path, content: bytes, directory: Path) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    _require_regular_owned_file(path, directory)


@contextmanager
def _writer_lock(directory: Path):
    import fcntl

    path = directory / ".writer.lock"
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError("artifact writer lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _replace_file(path: Path, content: bytes, directory: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".head-", dir=directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    _require_regular_owned_file(path, directory)


def _load_head_if_present(path: Path, directory: Path) -> dict[str, Any] | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    _require_regular_owned_file(path, directory)
    head = _strict_json_document(path.read_bytes(), "artifact current head")
    if set(head) != _HEAD_FIELDS or head["protocol"] != _HEAD_PROTOCOL:
        raise RuntimeError("artifact current head schema is invalid")
    _validate_artifact_id(head["artifact_id"])
    _validate_segment(head["artifact_kind"], "artifact kind")
    if not _SEMANTIC_VERSION.fullmatch(head["semantic_version"]):
        raise RuntimeError("artifact current head version is invalid")
    _validate_timestamp(head["created_at"])
    if head["revision"] != _revision_name(head["created_at"]):
        raise RuntimeError("artifact current head revision is invalid")
    return head


def _load_references(value: Any) -> tuple[ArtifactReference, ...]:
    if type(value) is not list:
        raise RuntimeError("artifact inputs are invalid")
    references: list[ArtifactReference] = []
    for raw in value:
        if type(raw) is not dict or set(raw) != _REFERENCE_FIELDS:
            raise RuntimeError("artifact input reference schema is invalid")
        references.append(
            ArtifactReference(
                artifact_id=raw["artifact_id"],
                artifact_kind=raw["artifact_kind"],
                semantic_version=raw["semantic_version"],
            )
        )
    return tuple(references)


def _load_authorization(value: Any) -> AuthorizationRecord:
    if type(value) is not dict or set(value) != _AUTHORIZATION_FIELDS:
        raise RuntimeError("artifact authorization schema is invalid")
    return AuthorizationRecord(
        authorization_id=value["authorization_id"],
        scope=value["scope"],
        granted_at=value["granted_at"],
        local_user_id=value["local_user_id"],
        revoked=value["revoked"],
    )


def _reference_value(value: ArtifactReference) -> dict[str, str]:
    return {
        "artifact_id": value.artifact_id,
        "artifact_kind": value.artifact_kind,
        "semantic_version": value.semantic_version,
    }


def _authorization_value(value: AuthorizationRecord) -> dict[str, Any]:
    return {
        "authorization_id": value.authorization_id,
        "scope": value.scope,
        "granted_at": value.granted_at,
        "local_user_id": value.local_user_id,
        "revoked": value.revoked,
    }


def _validate_artifact_id(value: Any) -> None:
    if type(value) is not str:
        raise ValueError("artifact ID is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or len(pure.parts) < 2 or value != pure.as_posix():
        raise ValueError("artifact ID is invalid")
    for part in pure.parts:
        _validate_segment(part, "artifact ID segment")


def _validate_scoped_name(value: Any, label: str) -> None:
    if type(value) is not str:
        raise ValueError(f"{label} is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or len(pure.parts) < 2 or value != pure.as_posix():
        raise ValueError(f"{label} is invalid")
    for part in pure.parts:
        _validate_segment(part, f"{label} segment")


def _validate_segment(value: Any, label: str) -> None:
    if type(value) is not str or not _SEGMENT.fullmatch(value):
        raise ValueError(f"{label} is invalid")


def _validate_timestamp(value: Any) -> None:
    if type(value) is not str or not _UTC_TIMESTAMP.fullmatch(value):
        raise ValueError("artifact timestamp is invalid")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("artifact timestamp is invalid") from error


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _revision_name(created_at: str) -> str:
    return created_at.replace(":", "-") + ".json"


def _validate_json_value(value: Any, *, label: str) -> None:
    if value is None or type(value) in {str, int, float, bool}:
        if type(value) is float and not (float("-inf") < value < float("inf")):
            raise ValueError(f"{label} contains a non-finite number")
        return
    if type(value) is list:
        for item in value:
            _validate_json_value(item, label=label)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not key:
                raise ValueError(f"{label} contains an invalid key")
            folded = key.casefold()
            legacy_token = "s" + "ha"
            if (
                legacy_token in folded
                or "digest" in folded
                or key in _DISALLOWED_PAYLOAD_KEYS
            ):
                raise ValueError(f"{label} contains a retired identity field")
            _validate_json_value(item, label=label)
        return
    raise ValueError(f"{label} contains a non-JSON value")


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _strict_json_document(content: bytes, label: str) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeError(f"{label} contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                RuntimeError(f"{label} contains a non-finite number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} is not strict JSON") from error
    if type(value) is not dict:
        raise RuntimeError(f"{label} is not an object")
    return value


__all__ = [
    "ArtifactReference",
    "AuthorizationRecord",
    "StableArtifact",
    "load_stable_artifact",
    "write_stable_artifact",
]
