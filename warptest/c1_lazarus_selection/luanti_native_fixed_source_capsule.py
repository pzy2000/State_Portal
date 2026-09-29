"""Offline full-tree fixed source capsule for Luanti native candidate 15519."""

from __future__ import annotations

import io
import os
import posixpath
import stat
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from .luanti_fixed_source_legacy_migration_reader import (
    read_legacy_merged_revision,
)
from .luanti_native_regression import NativeRegressionProfile, profile_for_candidate
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "MIGRATE_LUANTI_15519_NATIVE_FIXED_SOURCE_CAPSULE"
CONFIRMATION_14464 = "MIGRATE_LUANTI_14464_NATIVE_FIXED_SOURCE_CAPSULE"
CONFIRMATION_15687 = "MIGRATE_LUANTI_15687_NATIVE_FIXED_SOURCE_CAPSULE"
CONFIRMATION_11866 = "MIGRATE_LUANTI_11866_NATIVE_FIXED_SOURCE_CAPSULE"
CONFIRMATION_14726 = "MIGRATE_LUANTI_14726_NATIVE_FIXED_SOURCE_CAPSULE"
_MIRROR_PATH = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2",
    "repository_enumeration",
    "2.0",
)


def migrate_native_fixed_source_capsule(
    *,
    artifact_root: str | Path,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> StableArtifact:
    """Materialize and bind one registered merged Luanti source tree."""

    expected_confirmation = (
        CONFIRMATION_14726
        if candidate == CandidateKey("luanti", 14726)
        else CONFIRMATION_11866
        if candidate == CandidateKey("luanti", 11866)
        else CONFIRMATION_15687
        if candidate == CandidateKey("luanti", 15687)
        else CONFIRMATION_14464
        if candidate == CandidateKey("luanti", 14464)
        else CONFIRMATION
        if candidate == CandidateKey("luanti", 15519)
        else None
    )
    if expected_confirmation is None:
        raise ValueError(
            "native fixed source capsule is only registered for luanti/11866, luanti/15519, luanti/14464, luanti/15687, or luanti/14726"
        )
    if confirmation != expected_confirmation:
        raise RuntimeError(f"explicit confirmation must be {expected_confirmation!r}")
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("native fixed source authorization is invalid")
    if authorization.scope != "protocol-migration":
        raise PermissionError("native fixed source authorization scope is invalid")

    profile = profile_for_candidate(candidate)
    root = _ordinary_root(Path(artifact_root).absolute())
    source, patch, test_manifest = _load_chain(root, profile)
    revision = read_legacy_merged_revision(
        root,
        raw_manifest_path=source.payload["raw_evidence_path"],
        candidate=candidate,
    )
    mirror = _ordinary_directory(root, _MIRROR_PATH, create=False)
    _require_revision(mirror, revision)

    destination_relative = PurePosixPath(profile.fixed_source_tree_path)
    destination_parent = _ordinary_directory(
        root,
        PurePosixPath(*destination_relative.parts[:-1]),
        create=True,
    )
    destination = destination_parent / destination_relative.name
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("native fixed source destination already exists")

    archive = _read_archive(mirror, revision)
    members = _validated_members(archive)
    _materialize(destination_parent, destination, archive, members)

    payload = {
        "candidate": candidate.to_value(),
        "profile_id": profile.profile_id,
        "source_tree_path": profile.fixed_source_tree_path,
        "source_manifest_path": source.payload["raw_evidence_path"],
        "test_paths": list(profile.test_paths),
        "p2p_test_paths": list(profile.p2p_test_paths),
        "support_paths": list(profile.support_paths),
    }
    inputs = (
        profile.source_packet_reference,
        profile.patch_reference,
        profile.test_manifest_reference,
    )
    try:
        write_stable_artifact(
            root,
            artifact_id=profile.fixed_capsule_reference.artifact_id,
            artifact_kind=profile.fixed_capsule_reference.artifact_kind,
            semantic_version=profile.fixed_capsule_reference.semantic_version,
            created_at=_created_after(
                source.created_at,
                patch.created_at,
                test_manifest.created_at,
                authorization.granted_at,
            ),
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
        artifact = load_stable_artifact(
            root,
            artifact_id=profile.fixed_capsule_reference.artifact_id,
            artifact_kind=profile.fixed_capsule_reference.artifact_kind,
            semantic_version=profile.fixed_capsule_reference.semantic_version,
        )
    except BaseException:
        _remove_new_tree(destination)
        raise
    if artifact.inputs != inputs or artifact.payload != payload:
        _remove_new_tree(destination)
        raise RuntimeError("native fixed source capsule typed reload is invalid")
    return artifact


def _load_chain(
    root: Path,
    profile: NativeRegressionProfile,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    source = _load(root, profile.source_packet_reference)
    patch = _load(root, profile.patch_reference)
    test_manifest = _load(root, profile.test_manifest_reference)
    candidate = profile.candidate.to_value()
    if (
        source.inputs != (_ENUMERATION,)
        or set(source.payload) != {"candidate", "complete", "raw_evidence_path"}
        or source.payload.get("candidate") != candidate
        or source.payload.get("complete") is not True
        or type(source.payload.get("raw_evidence_path")) is not str
    ):
        raise RuntimeError("native fixed source v2 source packet is invalid")
    if (
        patch.inputs != (_ENUMERATION, profile.source_packet_reference)
        or patch.payload.get("candidate") != candidate
        or patch.payload.get("complete") is not True
    ):
        raise RuntimeError("native fixed source v2 patch is invalid")
    if (
        test_manifest.inputs != (profile.patch_reference,)
        or test_manifest.payload.get("candidate") != candidate
        or test_manifest.payload.get("complete") is not True
    ):
        raise RuntimeError("native fixed source v2 test manifest is invalid")
    return source, patch, test_manifest


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


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
        error="native fixed source revision is unavailable in retained mirror",
    )


def _read_archive(mirror: Path, revision: str) -> bytes:
    return _run_read_only(
        ("git", f"--git-dir={mirror}", "archive", "--format=tar", revision),
        error="native fixed source archive is unavailable",
    )


def _validated_members(content: bytes) -> tuple[tarfile.TarInfo, ...]:
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            members = tuple(archive)
    except (tarfile.TarError, OSError) as error:
        raise RuntimeError("native fixed source archive is invalid") from error
    if not members:
        raise RuntimeError("native fixed source archive is empty")
    by_name: dict[str, tarfile.TarInfo] = {}
    for member in members:
        name = member.name.rstrip("/")
        pure = PurePosixPath(name)
        if (
            not name
            or pure.is_absolute()
            or "." in pure.parts
            or ".." in pure.parts
            or pure.as_posix() != name
            or name in by_name
        ):
            raise RuntimeError("native fixed source archive path is unsafe")
        if not (member.isdir() or member.isfile() or member.issym()):
            raise RuntimeError("native fixed source archive entry is unsupported")
        by_name[name] = member
    for name, member in by_name.items():
        if not member.issym():
            continue
        target = member.linkname
        if not target or PurePosixPath(target).is_absolute():
            raise RuntimeError("native fixed source archive link is unsafe")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
        if resolved == ".." or resolved.startswith("../"):
            raise RuntimeError("native fixed source archive link escapes source root")
        target_member = by_name.get(resolved)
        if target_member is None or not target_member.isfile():
            raise RuntimeError("native fixed source archive link target is invalid")
    return members


def _materialize(
    parent: Path,
    destination: Path,
    content: bytes,
    members: tuple[tarfile.TarInfo, ...],
) -> None:
    with tempfile.TemporaryDirectory(
        prefix=".native-fixed-source-", dir=parent
    ) as name:
        staging = Path(name)
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            by_name = {member.name.rstrip("/"): member for member in members}
            for member_name, member in sorted(
                by_name.items(),
                key=lambda item: (len(PurePosixPath(item[0]).parts), item[0]),
            ):
                target = staging.joinpath(*PurePosixPath(member_name).parts)
                if member.isdir():
                    target.mkdir(mode=0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if member.isfile():
                    source = archive.extractfile(member)
                    if source is None:
                        raise RuntimeError(
                            "native fixed source archive file is unreadable"
                        )
                    descriptor = os.open(
                        target,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o700 if member.mode & 0o111 else 0o600,
                    )
                    with source, os.fdopen(descriptor, "wb") as handle:
                        while chunk := source.read(1024 * 1024):
                            handle.write(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if not stat.S_ISREG(target.lstat().st_mode):
                        raise RuntimeError("native fixed source output is unsafe")
                else:
                    os.symlink(member.linkname, target)
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("native fixed source destination already exists")
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


def _created_after(*values: str) -> str:
    timestamp = max(
        datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values
    )
    return (
        (timestamp + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "CONFIRMATION",
    "CONFIRMATION_14464",
    "CONFIRMATION_15687",
    "CONFIRMATION_14726",
    "migrate_native_fixed_source_capsule",
]
