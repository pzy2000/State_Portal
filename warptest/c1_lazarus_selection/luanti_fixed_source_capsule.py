"""Closed offline migration of two Luanti fixed source capsules to v2."""

from __future__ import annotations

import io
import os
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from .luanti_fixed_source_legacy_migration_reader import (
    read_legacy_merged_revision,
)
from .luanti_lua_regression import LuaRegressionProfile, profile_for_candidate
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "MIGRATE_LUANTI_FIXED_SOURCE_CAPSULE"
_MIRROR_PATH = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2",
    "repository_enumeration",
    "2.0",
)


def migrate_fixed_source_capsule(
    *,
    artifact_root: str | Path,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> StableArtifact:
    """Migrate one registered fixed tree without executing candidate code."""

    profile = profile_for_candidate(candidate)
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("fixed source migration authorization is invalid")
    if authorization.scope != "protocol-migration":
        raise PermissionError("fixed source migration authorization scope is invalid")
    root = _ordinary_root(Path(artifact_root).absolute())
    packet = _load_source_packet(root, profile)
    revision = read_legacy_merged_revision(
        root,
        raw_manifest_path=packet.payload["raw_evidence_path"],
        candidate=candidate,
    )
    mirror = _ordinary_directory(root, _MIRROR_PATH, create=False)
    destination_relative = PurePosixPath(profile.fixed_source_tree_path)
    destination_parent = _ordinary_directory(
        root,
        PurePosixPath(*destination_relative.parts[:-1]),
        create=True,
    )
    destination = destination_parent / destination_relative.name
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("fixed source destination already exists")

    paths = (
        *profile.production_paths,
        *profile.support_paths,
        *profile.test_paths,
    )
    _require_revision(mirror, revision)
    archive = _read_archive(mirror, revision, paths)
    _validate_archive(archive, paths)
    _materialize(destination_parent, destination, mirror, revision, paths)

    payload = {
        "candidate": candidate.to_value(),
        "profile_id": profile.profile_id,
        "source_tree_path": profile.fixed_source_tree_path,
        "support_paths": list(profile.support_paths),
        "test_paths": list(profile.test_paths),
    }
    try:
        write_stable_artifact(
            root,
            artifact_id=profile.fixed_capsule_reference.artifact_id,
            artifact_kind=profile.fixed_capsule_reference.artifact_kind,
            semantic_version=profile.fixed_capsule_reference.semantic_version,
            created_at=_created_after(packet.created_at),
            inputs=(profile.source_packet_reference,),
            payload=payload,
            authorization=authorization,
        )
        capsule = load_stable_artifact(
            root,
            artifact_id=profile.fixed_capsule_reference.artifact_id,
            artifact_kind=profile.fixed_capsule_reference.artifact_kind,
            semantic_version=profile.fixed_capsule_reference.semantic_version,
        )
    except BaseException:
        _remove_new_tree(destination)
        raise
    if (
        capsule.inputs != (profile.source_packet_reference,)
        or capsule.payload != payload
    ):
        _remove_new_tree(destination)
        raise RuntimeError("fixed source capsule typed reload is invalid")
    return capsule


def _load_source_packet(root: Path, profile: LuaRegressionProfile) -> StableArtifact:
    packet = load_stable_artifact(
        root,
        artifact_id=profile.source_packet_reference.artifact_id,
        artifact_kind=profile.source_packet_reference.artifact_kind,
        semantic_version=profile.source_packet_reference.semantic_version,
    )
    if (
        packet.inputs != (_ENUMERATION,)
        or set(packet.payload) != {"candidate", "complete", "raw_evidence_path"}
        or packet.payload["candidate"] != profile.candidate.to_value()
        or packet.payload["complete"] is not True
        or type(packet.payload["raw_evidence_path"]) is not str
    ):
        raise RuntimeError("fixed source migration v2 source packet is invalid")
    return packet


def _ordinary_root(root: Path) -> Path:
    try:
        info = root.lstat()
        resolved = root.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return resolved


def _ordinary_directory(
    root: Path,
    relative: PurePosixPath,
    *,
    create: bool,
) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                raise RuntimeError("retained Luanti mirror is unavailable")
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("registered directory is unsafe")
    return current


def _run_read_only(argv: tuple[str, ...], *, error: str) -> bytes:
    try:
        completed = subprocess.run(argv, check=False, capture_output=True)
    except OSError as cause:
        raise RuntimeError(error) from cause
    if completed.returncode != 0:
        raise RuntimeError(error)
    return completed.stdout


def _require_revision(mirror: Path, revision: str) -> None:
    _run_read_only(
        (
            "git",
            f"--git-dir={mirror}",
            "show",
            "--no-patch",
            "--format=",
            revision,
        ),
        error="fixed source revision is unavailable in retained mirror",
    )


def _read_archive(mirror: Path, revision: str, paths: tuple[str, ...]) -> bytes:
    return _run_read_only(
        (
            "git",
            f"--git-dir={mirror}",
            "archive",
            "--format=tar",
            revision,
            "--",
            *paths,
        ),
        error="fixed source archive is unavailable",
    )


def _validate_archive(content: bytes, paths: tuple[str, ...]) -> None:
    expected = set(paths)
    found: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            for member in archive:
                pure = PurePosixPath(member.name)
                if (
                    pure.is_absolute()
                    or "." in pure.parts
                    or ".." in pure.parts
                    or pure.as_posix().rstrip("/") != member.name.rstrip("/")
                ):
                    raise RuntimeError("fixed source archive path is unsafe")
                name = member.name.rstrip("/")
                if member.isdir():
                    if not any(path.startswith(name + "/") for path in expected):
                        raise RuntimeError(
                            "fixed source archive contains an extra entry"
                        )
                    continue
                if not member.isfile() or name not in expected or name in found:
                    raise RuntimeError("fixed source profile requires ordinary files")
                found.add(name)
    except (tarfile.TarError, OSError) as error:
        raise RuntimeError("fixed source archive is invalid") from error
    if found != expected:
        raise RuntimeError("fixed source archive is incomplete")


def _materialize(
    parent: Path,
    destination: Path,
    mirror: Path,
    revision: str,
    paths: tuple[str, ...],
) -> None:
    with tempfile.TemporaryDirectory(prefix=".fixed-source-", dir=parent) as name:
        staging = Path(name)
        for relative in paths:
            content = _run_read_only(
                (
                    "git",
                    f"--git-dir={mirror}",
                    "show",
                    f"{revision}:{relative}",
                ),
                error="fixed source profile file is unavailable",
            )
            target = staging.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if not stat.S_ISREG(target.lstat().st_mode):
                raise RuntimeError("fixed source output is unsafe")
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("fixed source destination already exists")
        os.replace(staging, destination)


def _remove_new_tree(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            path.rmdir()
    root.rmdir()


def _created_after(value: str) -> str:
    from datetime import datetime, timedelta

    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        (timestamp + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CONFIRMATION", "migrate_fixed_source_capsule"]
