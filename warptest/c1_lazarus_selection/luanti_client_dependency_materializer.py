"""Offline materialization of the registered Luanti client dependencies."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


MATERIALIZATION_CONFIRMATION = "MATERIALIZE_LUANTI_CLIENT_DEPENDENCIES"
_VERSION = "2.0"
_PLAN = ArtifactReference(
    "client_dependency_packaging_plan/luanti/15519/darwin-arm64/v2",
    "client_dependency_packaging_plan",
    _VERSION,
)
_CLOSURE = ArtifactReference(
    "client_dependency_closure/luanti/15519/darwin-arm64/v2",
    "client_dependency_closure",
    _VERSION,
)
_DESTINATION_ROOT = PurePosixPath("private/dependencies/luanti-client/2.0")


@dataclass(frozen=True)
class ClientDependencyMaterializationResult:
    artifact: StableArtifact
    copied_files: int
    resumed: bool


@dataclass(frozen=True)
class _CopyEntry:
    source: Path
    destination: PurePosixPath


def load_luanti_client_dependency_closure(
    artifact_root: str | Path,
) -> StableArtifact:
    """Strictly reload the registered closure and its exact local inventory."""

    root = _ordinary_root(Path(artifact_root).absolute())
    plan = _load(root, _PLAN)
    entries = _load_plan(plan)
    artifact = _load(root, _CLOSURE)
    _validate_closure(root / _DESTINATION_ROOT, entries, artifact)
    return artifact


def materialize_luanti_client_dependency_closure(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> ClientDependencyMaterializationResult:
    """Copy the exact registered inventory; never build or access a network."""

    if confirmation != MATERIALIZATION_CONFIRMATION:
        raise RuntimeError(
            f"explicit confirmation must be {MATERIALIZATION_CONFIRMATION!r}"
        )
    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    plan = _load(root, _PLAN)
    entries = _load_plan(plan)
    destination = root / _DESTINATION_ROOT
    existing = _try_load(root, _CLOSURE)
    if existing is not None:
        _validate_closure(destination, entries, existing)
        return ClientDependencyMaterializationResult(existing, 0, True)
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("unreceipted client dependency destination already exists")

    parent = destination.parent
    _make_safe_directory(parent, root)
    temporary = Path(tempfile.mkdtemp(prefix="luanti-client-closure-", dir=parent))
    try:
        for entry in entries:
            target = temporary / entry.destination
            _make_safe_directory(target.parent, temporary)
            if target.exists() or target.is_symlink():
                raise RuntimeError("duplicate client dependency destination")
            shutil.copyfile(entry.source, target)
            target.chmod(0o600)
            _ordinary_destination_file(target, temporary)
        _validate_inventory(temporary, entries)
        os.rename(temporary, destination)
    except Exception:
        if temporary.exists() and temporary.parent == parent:
            shutil.rmtree(temporary)
        raise

    payload: dict[str, object] = {
        "candidate": {"repository_key": "luanti", "change_number": 15519},
        "host": "darwin-arm64",
        "destination_root": _DESTINATION_ROOT.as_posix(),
        "materialized_inventory": [
            {
                "source": str(entry.source),
                "destination": entry.destination.as_posix(),
                "size_bytes": entry.source.stat().st_size,
            }
            for entry in entries
        ],
        "copied_file_count": len(entries),
        "packaging_performed": True,
        "install_name_rewrites_applied": False,
        "install_name_rewrites_deferred_to_client_packaging": True,
        "client_build_started": False,
        "candidate_execution_performed": False,
        "network_used": False,
        "paid_actions_authorized": False,
        "state": "materialized",
        "complete": True,
    }
    write_stable_artifact(
        root,
        artifact_id=_CLOSURE.artifact_id,
        artifact_kind=_CLOSURE.artifact_kind,
        semantic_version=_CLOSURE.semantic_version,
        created_at=_now(),
        inputs=(plan.reference,),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _CLOSURE)
    _validate_closure(destination, entries, artifact)
    return ClientDependencyMaterializationResult(artifact, len(entries), False)


def _load_plan(plan: StableArtifact) -> tuple[_CopyEntry, ...]:
    payload = plan.payload
    if (
        payload.get("candidate") != {"repository_key": "luanti", "change_number": 15519}
        or payload.get("host") != "darwin-arm64"
        or payload.get("feasible_offline") is not True
        or payload.get("destination_root") != _DESTINATION_ROOT.as_posix()
        or payload.get("packaging_performed") is not False
        or payload.get("client_build_started") is not False
        or payload.get("network_required") is not False
        or payload.get("downstream_state") != "ready_for_offline_packaging"
        or payload.get("complete") is not True
    ):
        raise RuntimeError("client dependency packaging plan is invalid")
    raw_entries = payload.get("copy_entries")
    if type(raw_entries) is not list or not raw_entries:
        raise RuntimeError("client dependency copy inventory is invalid")
    entries: list[_CopyEntry] = []
    destinations: set[PurePosixPath] = set()
    for raw in raw_entries:
        if type(raw) is not dict or set(raw) != {"source", "destination"}:
            raise RuntimeError("client dependency copy entry is invalid")
        source_value = raw["source"]
        destination_value = raw["destination"]
        if type(source_value) is not str or type(destination_value) is not str:
            raise RuntimeError("client dependency copy entry is invalid")
        source = _ordinary_source_file(Path(source_value))
        destination = _safe_destination(destination_value)
        if destination in destinations:
            raise RuntimeError("duplicate client dependency destination")
        destinations.add(destination)
        entries.append(_CopyEntry(source, destination))
    return tuple(entries)


def _safe_destination(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0] != "packages"
        or "." in path.parts
        or ".." in path.parts
        or value != path.as_posix()
    ):
        raise RuntimeError("client dependency destination is unsafe")
    return path


def _validate_closure(
    destination: Path,
    entries: tuple[_CopyEntry, ...],
    artifact: StableArtifact,
) -> None:
    if artifact.inputs != (_PLAN,):
        raise RuntimeError("client dependency closure binding is invalid")
    payload = artifact.payload
    expected_inventory = [
        {
            "source": str(entry.source),
            "destination": entry.destination.as_posix(),
            "size_bytes": entry.source.stat().st_size,
        }
        for entry in entries
    ]
    if (
        payload.get("candidate") != {"repository_key": "luanti", "change_number": 15519}
        or payload.get("destination_root") != _DESTINATION_ROOT.as_posix()
        or payload.get("materialized_inventory") != expected_inventory
        or payload.get("copied_file_count") != len(entries)
        or payload.get("packaging_performed") is not True
        or payload.get("client_build_started") is not False
        or payload.get("candidate_execution_performed") is not False
        or payload.get("network_used") is not False
        or payload.get("state") != "materialized"
        or payload.get("complete") is not True
    ):
        raise RuntimeError("client dependency closure receipt is invalid")
    _validate_inventory(destination, entries)


def _validate_inventory(root: Path, entries: tuple[_CopyEntry, ...]) -> None:
    _ordinary_directory(root)
    expected = {entry.destination.as_posix(): entry for entry in entries}
    actual: dict[str, Path] = {}
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("materialized client dependency contains a symlink")
        if stat.S_ISREG(info.st_mode):
            actual[path.relative_to(root).as_posix()] = path
        elif not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("materialized client dependency entry is unsafe")
    if set(actual) != set(expected):
        raise RuntimeError("materialized client dependency inventory changed")
    for relative, entry in expected.items():
        if not _files_equal(entry.source, actual[relative]):
            raise RuntimeError("materialized client dependency content changed")


def _files_equal(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_stream, right.open("rb") as right_stream:
        while True:
            left_chunk = left_stream.read(65_536)
            right_chunk = right_stream.read(65_536)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _ordinary_root(path: Path) -> Path:
    resolved = _ordinary_directory(path)
    if path.is_symlink():
        raise RuntimeError("artifact root is unsafe")
    return resolved


def _ordinary_directory(path: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("required directory is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("required directory is unsafe")
    return resolved


def _ordinary_source_file(path: Path) -> Path:
    if not path.is_absolute():
        raise RuntimeError("client dependency source is unsafe")
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("client dependency source is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or resolved != path:
        raise RuntimeError("client dependency source is unsafe")
    return resolved


def _ordinary_destination_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("materialized client dependency file is unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("materialized client dependency file is unsafe")
    return resolved


def _make_safe_directory(path: Path, root: Path) -> None:
    root = root.resolve(strict=True)
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as error:
        raise RuntimeError("client dependency destination escapes root") from error
    current = root
    for part in relative.parts:
        current /= part
        if current.exists() or current.is_symlink():
            _ordinary_directory(current)
        else:
            current.mkdir(mode=0o700)


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("preannotation execution authorization is required")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "ClientDependencyMaterializationResult",
    "MATERIALIZATION_CONFIRMATION",
    "load_luanti_client_dependency_closure",
    "materialize_luanti_client_dependency_closure",
]
