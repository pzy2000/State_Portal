"""Offline materialization of closed historical JupyterLab fixed trees."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from .jupyterlab_fixed_source_legacy_migration_reader import (
    read_legacy_merged_revision,
)
from .jupyterlab_regression import JupyterLabRegressionProfile, profile_for_candidate
from .models import CandidateKey
from .stable_protocol import (
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "MATERIALIZE_JUPYTERLAB_FIXED_SOURCE_V2"
_MIRROR = PurePosixPath("private/repository-enumeration-git/jupyterlab/mirror.git")
_WEBPACK_CONFIG_NAMES = ("webpack.conf.js", "webpack.config.js")


def materialize_jupyterlab_fixed_source(
    *,
    artifact_root: str | Path,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> StableArtifact:
    """Materialize the captured merged revision without executing its code."""

    profile = profile_for_candidate(candidate)
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("JupyterLab fixed source authorization is invalid")
    if authorization.scope != "protocol-migration":
        raise PermissionError("JupyterLab fixed source authorization scope is invalid")

    root = _ordinary_root(Path(artifact_root).absolute())
    prerequisites = _load_prerequisites(root, profile)
    mirror = _ordinary_directory(root, _MIRROR, create=False)
    if profile.captured_merged_revision_required:
        source = prerequisites[0]
        mirror_ref = read_legacy_merged_revision(
            root,
            raw_manifest_path=source.payload["raw_evidence_path"],
            candidate=candidate,
        )
    else:
        mirror_ref = f"refs/c1/pull/{candidate.change_number}/head"
    archive = _read_archive(mirror, mirror_ref)
    files = _validated_archive_files(archive)
    harness = PurePosixPath(profile.test_harness_root)
    required = {
        "package.json",
        profile.package_metadata_path,
        (harness / "karma.conf.js").as_posix(),
        *profile.production_paths,
        *profile.patch_test_paths,
    }
    webpack_paths = {
        (harness / name).as_posix() for name in _WEBPACK_CONFIG_NAMES
    }
    if not required.issubset(files) or not webpack_paths.intersection(files):
        raise RuntimeError("captured JupyterLab fixed tree is incomplete")
    _validate_package(archive, profile)

    relative = PurePosixPath(profile.fixed_source_tree_path)
    parent = _ordinary_directory(root, PurePosixPath(*relative.parts[:-1]), create=True)
    destination = parent / relative.name
    created_tree = False
    if destination.exists() or destination.is_symlink():
        if not _tree_matches_archive(destination, archive):
            raise RuntimeError("JupyterLab fixed source destination conflicts")
    else:
        _materialize_archive(parent, destination, archive)
        created_tree = True

    payload = {
        "candidate": candidate.to_value(),
        "profile_id": profile.profile_id,
        "source_origin": "captured_merged_revision",
        "source_tree_path": profile.fixed_source_tree_path,
        "production_paths": list(profile.production_paths),
        "test_paths": list(profile.patch_test_paths),
    }
    created_at = _created_after(
        *(artifact.created_at for artifact in prerequisites),
        authorization.granted_at,
    )
    try:
        write_stable_artifact(
            root,
            artifact_id=profile.fixed_capsule_reference.artifact_id,
            artifact_kind=profile.fixed_capsule_reference.artifact_kind,
            semantic_version=profile.fixed_capsule_reference.semantic_version,
            created_at=created_at,
            inputs=tuple(artifact.reference for artifact in prerequisites),
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
        if created_tree:
            _remove_tree(destination)
        raise
    if artifact.payload != payload or artifact.inputs != tuple(
        item.reference for item in prerequisites
    ):
        if created_tree:
            _remove_tree(destination)
        raise RuntimeError("JupyterLab fixed source typed reload is invalid")
    return artifact


def _load_prerequisites(
    root: Path, profile: JupyterLabRegressionProfile
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    references = (
        profile.source_packet_reference,
        profile.patch_reference,
        profile.atomicity_reference,
        profile.test_manifest_reference,
    )
    artifacts = tuple(
        load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
        for reference in references
    )
    source, patch, atomicity, manifest = artifacts
    if (
        source.payload.get("candidate") != profile.candidate.to_value()
        or source.payload.get("complete") is not True
        or patch.inputs[-1:] != (source.reference,)
        or patch.payload.get("candidate") != profile.candidate.to_value()
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths") != list(profile.production_paths)
        or patch.payload.get("test_paths") != list(profile.patch_test_paths)
        or atomicity.payload.get("candidate") != profile.candidate.to_value()
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != profile.primary_issue_number
        or manifest.inputs != (patch.reference, atomicity.reference)
        or manifest.payload.get("candidate") != profile.candidate.to_value()
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
    ):
        raise RuntimeError("JupyterLab fixed source prerequisites are invalid")
    return artifacts


def _read_archive(mirror: Path, mirror_ref: str) -> bytes:
    try:
        completed = subprocess.run(
            (
                "git",
                f"--git-dir={mirror}",
                "archive",
                "--format=tar",
                mirror_ref,
            ),
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise RuntimeError("retained JupyterLab mirror is unavailable") from error
    if completed.returncode != 0 or not completed.stdout:
        raise RuntimeError("captured JupyterLab merged revision is unavailable")
    return completed.stdout


def _validated_archive_files(content: bytes) -> frozenset[str]:
    files: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or "." in path.parts
                    or ".." in path.parts
                    or path.as_posix().rstrip("/") != member.name.rstrip("/")
                ):
                    raise RuntimeError("captured JupyterLab archive path is unsafe")
                if member.isdir():
                    continue
                if not member.isfile() or member.name in files:
                    raise RuntimeError("captured JupyterLab archive entry is unsafe")
                files.add(member.name)
    except (tarfile.TarError, OSError) as error:
        raise RuntimeError("captured JupyterLab archive is invalid") from error
    if not files:
        raise RuntimeError("captured JupyterLab archive is empty")
    return frozenset(files)


def _validate_package(content: bytes, profile: JupyterLabRegressionProfile) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            member = archive.getmember(profile.package_metadata_path)
            handle = archive.extractfile(member)
            if handle is None:
                raise RuntimeError("captured JupyterLab package metadata is missing")
            package = json.loads(handle.read().decode("utf-8"))
            harness_member = archive.getmember(
                (PurePosixPath(profile.test_harness_root) / "package.json").as_posix()
            )
            harness_handle = archive.extractfile(harness_member)
            if harness_handle is None:
                raise RuntimeError("captured JupyterLab package metadata is missing")
            harness_package = json.loads(harness_handle.read().decode("utf-8"))
    except (
        KeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        tarfile.TarError,
    ) as error:
        raise RuntimeError("captured JupyterLab package metadata is invalid") from error
    scripts = package.get("scripts")
    dependencies: dict[str, object] = {}
    for metadata in (harness_package, package):
        for field in ("dependencies", "devDependencies"):
            values = metadata.get(field) if type(metadata) is dict else None
            if type(values) is dict:
                dependencies.update(values)
    if (
        package.get("version") != profile.package_version
        or type(scripts) is not dict
        or type(scripts.get(profile.build_script)) is not str
        or any(
            type(dependencies.get(name)) is not str
            for name in ("karma", "karma-mocha", "mocha", "typescript", "webpack")
        )
    ):
        raise RuntimeError("captured JupyterLab package metadata is invalid")


def _materialize_archive(parent: Path, destination: Path, content: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix=".jupyterlab-fixed-", dir=parent) as name:
        staging = Path(name)
        try:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
                for member in archive:
                    if member.isdir():
                        continue
                    source = archive.extractfile(member)
                    if source is None:
                        raise RuntimeError("captured JupyterLab archive is invalid")
                    target = staging.joinpath(*PurePosixPath(member.name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    descriptor = os.open(
                        target,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o700 if member.mode & 0o111 else 0o600,
                    )
                    with os.fdopen(descriptor, "wb") as output:
                        while block := source.read(1024 * 1024):
                            output.write(block)
                        output.flush()
                        os.fsync(output.fileno())
            if destination.exists() or destination.is_symlink():
                raise RuntimeError("JupyterLab fixed source destination already exists")
            os.replace(staging, destination)
        except BaseException:
            _remove_tree(staging)
            raise


def _tree_matches_archive(destination: Path, content: bytes) -> bool:
    try:
        info = destination.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return False
        actual: dict[str, Path] = {}
        for path in destination.rglob("*"):
            entry = path.lstat()
            if stat.S_ISLNK(entry.st_mode) or not (
                stat.S_ISDIR(entry.st_mode) or stat.S_ISREG(entry.st_mode)
            ):
                return False
            if stat.S_ISREG(entry.st_mode):
                actual[path.relative_to(destination).as_posix()] = path
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            expected = {member.name: member for member in archive if member.isfile()}
            if set(actual) != set(expected):
                return False
            for name, member in expected.items():
                source = archive.extractfile(member)
                if source is None or actual[name].read_bytes() != source.read():
                    return False
                executable = bool(actual[name].stat().st_mode & 0o111)
                if executable != bool(member.mode & 0o111):
                    return False
    except (OSError, tarfile.TarError):
        return False
    return True


def _ordinary_root(root: Path) -> Path:
    try:
        info = root.lstat()
        resolved = root.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return resolved


def _ordinary_directory(root: Path, relative: PurePosixPath, *, create: bool) -> Path:
    current = root
    for part in relative.parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                raise RuntimeError("retained JupyterLab mirror is unavailable")
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("registered JupyterLab directory is unsafe")
    return current.resolve(strict=True)


def _remove_tree(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            path.rmdir()
    root.rmdir()


def _created_after(*values: str) -> str:
    latest = max(
        datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values
    )
    return (
        (latest + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CONFIRMATION", "materialize_jupyterlab_fixed_source"]
