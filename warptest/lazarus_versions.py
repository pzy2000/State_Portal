"""Exact inverse-patch materialization for trusted C1-Lazarus versions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .c1_sandbox import strict_clone_tree


LAZARUS_VERSION_SCHEMA = 1
_NO_BUILD_FINGERPRINT = hashlib.sha256(b"c1-lazarus-no-build-v1").hexdigest()
_TASK_ID = re.compile(r"^lzp?-[a-z0-9]+(?:-[a-z0-9]+)*$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUILD_CLASSES = frozenset(
    {"none", "jlpm", "incremental_cpp", "incremental_dotnet", "unity"}
)


class LazarusVersionError(RuntimeError):
    """Raised when a version pair cannot be constructed without ambiguity."""


@dataclass(frozen=True)
class LazarusVersionRequest:
    task_id: str
    source_repo: str | Path
    fixed_revision: str
    revert_patch: str | Path
    patch_sha256: str
    declared_files: tuple[str, ...]
    build_class: str
    build_fingerprint: str = _NO_BUILD_FINGERPRINT


@dataclass(frozen=True)
class LazarusVersionArtifact:
    task_id: str
    version: str
    root: Path
    git_metadata: Path
    source_revision: str
    patch_sha256: str
    tree_sha256: str
    changed_files: tuple[str, ...]
    build_identity: dict[str, Any]
    cache_hit: bool


@dataclass(frozen=True)
class LazarusVersionPair:
    buggy: LazarusVersionArtifact
    fixed: LazarusVersionArtifact
    version_switch_ms: float


BuildCallback = Callable[[str, Path], Mapping[str, Any]]


def materialize_lazarus_versions(
    request: LazarusVersionRequest,
    *,
    cache_root: str | Path,
    build: BuildCallback | None = None,
) -> LazarusVersionPair:
    """Create or verify a fixed/buggy pair below the trusted prebuilt cache.

    The buggy checkout is produced only by a successful exact
    ``git apply --reverse --check`` followed by ``git apply --reverse``.  No
    three-way or fuzzy fallback exists.  Git metadata is moved to a trusted
    sibling tree before either version root is returned.
    """

    started = time.monotonic()
    normalized = _validate_request(request, build=build)
    cache = Path(cache_root).expanduser().resolve()
    prebuilt_root = cache / "prebuilt" / normalized.task_id
    metadata_root = cache / "metadata" / normalized.task_id
    manifest_path = metadata_root / "manifest.json"

    if prebuilt_root.exists() or metadata_root.exists():
        if not prebuilt_root.is_dir() or not manifest_path.is_file():
            raise LazarusVersionError(
                f"incomplete Lazarus cache bundle for {normalized.task_id}"
            )
        return _load_cached_pair(
            normalized,
            prebuilt_root=prebuilt_root,
            metadata_root=metadata_root,
            manifest_path=manifest_path,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )

    cache.mkdir(parents=True, exist_ok=True)
    staging_parent = cache / ".staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f"{normalized.task_id}-", dir=staging_parent)
    ).resolve()
    staged_prebuilt = staging / "prebuilt"
    staged_metadata = staging / "metadata"
    fixed_root = staged_prebuilt / "fixed"
    buggy_root = staged_prebuilt / "buggy"
    try:
        staged_prebuilt.mkdir(parents=True)
        staged_metadata.mkdir(parents=True)
        strict_clone_tree(normalized.source_repo, fixed_root)
        _stage_declared_revision(
            fixed_root,
            revision=normalized.fixed_revision,
            label="fixed staging checkout",
        )
        _verify_git_checkout(
            fixed_root,
            revision=normalized.fixed_revision,
            clean=True,
            label="fixed staging checkout",
        )
        strict_clone_tree(fixed_root, buggy_root)
        _apply_exact_reverse_patch(buggy_root, normalized.revert_patch)
        changed_files = _git_changed_files(buggy_root)
        if changed_files != normalized.declared_files:
            raise LazarusVersionError(
                "inverse patch changed a different file set: "
                f"declared={list(normalized.declared_files)!r}, "
                f"actual={list(changed_files)!r}"
            )
        _git(
            buggy_root,
            "diff",
            "--check",
            error_label="inverse patch produced an invalid working-tree diff",
        )

        build_identity: dict[str, dict[str, Any]] = {}
        for version, root in (("fixed", fixed_root), ("buggy", buggy_root)):
            if build is None:
                build_identity[version] = {
                    "build_class": normalized.build_class,
                    "status": "not_required",
                }
            else:
                value = build(version, root)
                if not isinstance(value, Mapping):
                    raise LazarusVersionError(
                        f"{version} build callback must return a mapping"
                    )
                build_identity[version] = copy_json_mapping(value)

        git_paths: dict[str, Path] = {}
        for version, root in (("fixed", fixed_root), ("buggy", buggy_root)):
            git_path = root / ".git"
            if not git_path.exists():
                raise LazarusVersionError(
                    f"{version} staging checkout lost .git before isolation"
                )
            trusted_path = staged_metadata / f"{version}.git"
            os.replace(git_path, trusted_path)
            if (root / ".git").exists() or not trusted_path.exists():
                raise LazarusVersionError(f"failed to isolate {version} Git metadata")
            git_paths[version] = trusted_path

        tree_hashes = {
            "fixed": tree_fingerprint(fixed_root),
            "buggy": tree_fingerprint(buggy_root),
        }
        if tree_hashes["fixed"] == tree_hashes["buggy"]:
            raise LazarusVersionError("inverse patch did not change the content fingerprint")
        manifest = {
            "schema_version": LAZARUS_VERSION_SCHEMA,
            "task_id": normalized.task_id,
            "source_repo": str(normalized.source_repo),
            "source_revision": normalized.fixed_revision,
            "patch_sha256": normalized.patch_sha256,
            "declared_files": list(normalized.declared_files),
            "build_class": normalized.build_class,
            "build_fingerprint": normalized.build_fingerprint,
            "versions": {
                "fixed": {
                    "tree_sha256": tree_hashes["fixed"],
                    "changed_files": [],
                    "build_identity": build_identity["fixed"],
                },
                "buggy": {
                    "tree_sha256": tree_hashes["buggy"],
                    "changed_files": list(changed_files),
                    "build_identity": build_identity["buggy"],
                },
            },
        }
        (staged_metadata / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        prebuilt_root.parent.mkdir(parents=True, exist_ok=True)
        metadata_root.parent.mkdir(parents=True, exist_ok=True)
        # Metadata is published first.  The prebuilt directory is the commit
        # marker; any interruption remains visibly incomplete and fails closed.
        os.replace(staged_metadata, metadata_root)
        os.replace(staged_prebuilt, prebuilt_root)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return _pair_from_manifest(
            manifest,
            prebuilt_root=prebuilt_root,
            metadata_root=metadata_root,
            cache_hit=False,
            elapsed_ms=elapsed_ms,
        )
    except LazarusVersionError:
        raise
    except Exception as error:
        raise LazarusVersionError(
            f"cannot materialize {normalized.task_id}: {type(error).__name__}: {error}"
        ) from error
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def tree_fingerprint(root: str | Path) -> str:
    """Hash all agent-visible paths and bytes in a materialized version root."""

    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise LazarusVersionError(f"tree fingerprint root is not a directory: {base}")
    digest = hashlib.sha256()
    for path in sorted(base.rglob("*"), key=lambda item: item.relative_to(base).as_posix()):
        relative = path.relative_to(base).as_posix()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            kind = b"d"
            content = b""
        elif stat.S_ISLNK(info.st_mode):
            kind = b"l"
            content = os.readlink(path).encode("utf-8", errors="surrogateescape")
        elif stat.S_ISREG(info.st_mode):
            kind = b"f"
            content = path.read_bytes()
        else:
            raise LazarusVersionError(
                f"unsupported file type in materialized tree: {path}"
            )
        digest.update(kind)
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(stat.S_IMODE(info.st_mode)).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def _validate_request(
    request: LazarusVersionRequest, *, build: BuildCallback | None
) -> LazarusVersionRequest:
    task_id = str(request.task_id).strip()
    if not _TASK_ID.fullmatch(task_id):
        raise LazarusVersionError("task_id must be a lowercase lz-* slug")
    source = Path(request.source_repo).expanduser().resolve()
    patch = Path(request.revert_patch).expanduser().resolve()
    if not source.is_dir() or not patch.is_file():
        raise LazarusVersionError("source repository and revert patch must exist")
    revision = str(request.fixed_revision).strip()
    if not _FULL_SHA.fullmatch(revision):
        raise LazarusVersionError("fixed revision must be a full SHA")
    patch_sha = str(request.patch_sha256).strip()
    if not _SHA256.fullmatch(patch_sha) or hashlib.sha256(patch.read_bytes()).hexdigest() != patch_sha:
        raise LazarusVersionError("revert patch SHA does not match declared patch SHA")
    if request.build_class not in _BUILD_CLASSES:
        raise LazarusVersionError(f"unsupported build class {request.build_class!r}")
    if request.build_class != "none" and build is None:
        raise LazarusVersionError(
            f"build class {request.build_class!r} requires a prebuild callback"
        )
    build_fingerprint = _build_fingerprint(build)
    declared: list[str] = []
    for value in request.declared_files:
        path = Path(str(value))
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise LazarusVersionError(f"invalid declared patch path {value!r}")
        declared.append(path.as_posix())
    normalized_files = tuple(sorted(set(declared)))
    if not normalized_files or len(normalized_files) != len(declared):
        raise LazarusVersionError("declared patch files must be non-empty and unique")

    # The source must be clean and must *contain* the declared revision.  It no
    # longer has to be sitting on it: a row whose defect predates the pinned
    # submodule revision is materialized by moving the disposable staging clone
    # to the revision the row registers, never by moving the shared checkout.
    _verify_git_clean(source, label="source checkout")
    _verify_git_contains(source, revision=revision, label="source checkout")
    return LazarusVersionRequest(
        task_id=task_id,
        source_repo=source,
        fixed_revision=revision,
        revert_patch=patch,
        patch_sha256=patch_sha,
        declared_files=normalized_files,
        build_class=request.build_class,
        build_fingerprint=build_fingerprint,
    )


def _build_fingerprint(build: BuildCallback | None) -> str:
    if build is None:
        return _NO_BUILD_FINGERPRINT
    fingerprint = getattr(build, "lazarus_build_fingerprint", None)
    if not isinstance(fingerprint, str) or not _SHA256.fullmatch(fingerprint):
        raise LazarusVersionError(
            "prebuild callback must declare a lowercase SHA-256 build fingerprint"
        )
    return fingerprint


def _apply_exact_reverse_patch(repo: Path, patch: Path) -> None:
    checked = subprocess.run(
        ["git", "-C", str(repo), "apply", "--reverse", "--check", str(patch)],
        capture_output=True,
        text=True,
    )
    if checked.returncode != 0:
        detail = checked.stderr.strip() or checked.stdout.strip()
        raise LazarusVersionError(
            "git apply --reverse --check rejected the true fix patch"
            + (f": {detail}" if detail else "")
        )
    applied = subprocess.run(
        ["git", "-C", str(repo), "apply", "--reverse", str(patch)],
        capture_output=True,
        text=True,
    )
    if applied.returncode != 0:
        detail = applied.stderr.strip() or applied.stdout.strip()
        raise LazarusVersionError(
            "git apply --reverse failed after its exact check"
            + (f": {detail}" if detail else "")
        )


def _verify_git_checkout(
    repo: Path, *, revision: str, clean: bool, label: str
) -> None:
    head = _git(repo, "rev-parse", "HEAD", error_label=f"{label} has no Git HEAD")
    if head != revision:
        raise LazarusVersionError(
            f"{label} revision mismatch: expected {revision}, found {head}"
        )
    if clean:
        _verify_git_clean(repo, label=label)


def _verify_git_clean(repo: Path, *, label: str) -> None:
    status_value = _git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        error_label=f"cannot inspect {label} status",
    )
    if status_value:
        raise LazarusVersionError(f"{label} must be clean before materialization")


def _verify_git_contains(repo: Path, *, revision: str, label: str) -> None:
    """Require the repository to know the revision the row registers."""

    completed = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{revision}^{{commit}}"],
        capture_output=True,
        shell=False,
    )
    if completed.returncode != 0:
        raise LazarusVersionError(
            f"{label} does not contain the registered revision {revision}"
        )


def _stage_declared_revision(repo: Path, *, revision: str, label: str) -> None:
    """Move a disposable clone onto the revision its row registers.

    The clone carries its own Git directory, so this detaches only the clone.
    It is the one place a Lazarus revision is checked out, and it is never the
    shared submodule.
    """

    head = _git(repo, "rev-parse", "HEAD", error_label=f"{label} has no Git HEAD")
    if head == revision:
        return
    _git(
        repo,
        "checkout",
        "--detach",
        "--force",
        revision,
        error_label=f"cannot stage {label} at {revision}",
    )
    _git(
        repo,
        "clean",
        "-ffdx",
        error_label=f"cannot clean {label} after staging {revision}",
    )


def _git_changed_files(repo: Path) -> tuple[str, ...]:
    value = _git(
        repo,
        "diff",
        "--name-only",
        "--no-ext-diff",
        error_label="cannot enumerate inverse-patch files",
    )
    return tuple(sorted(line.strip() for line in value.splitlines() if line.strip()))


def _git(repo: Path, *args: str, error_label: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise LazarusVersionError(error_label + (f": {detail}" if detail else ""))
    return completed.stdout.strip()


def _load_cached_pair(
    request: LazarusVersionRequest,
    *,
    prebuilt_root: Path,
    metadata_root: Path,
    manifest_path: Path,
    elapsed_ms: float,
) -> LazarusVersionPair:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LazarusVersionError(f"cannot read cached version manifest: {error}") from error
    expected = {
        "schema_version": LAZARUS_VERSION_SCHEMA,
        "task_id": request.task_id,
        "source_repo": str(request.source_repo),
        "source_revision": request.fixed_revision,
        "patch_sha256": request.patch_sha256,
        "declared_files": list(request.declared_files),
        "build_class": request.build_class,
        "build_fingerprint": request.build_fingerprint,
    }
    if not isinstance(manifest, dict):
        raise LazarusVersionError("cached Lazarus manifest must be a mapping")
    if manifest.get("build_fingerprint") != request.build_fingerprint:
        raise LazarusVersionError(
            "cached Lazarus build fingerprint does not match the registered prebuild inputs"
        )
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise LazarusVersionError("cached Lazarus manifest does not match request")
    versions = manifest.get("versions")
    if not isinstance(versions, dict) or set(versions) != {"fixed", "buggy"}:
        raise LazarusVersionError("cached Lazarus manifest has invalid versions")
    for version in ("fixed", "buggy"):
        root = prebuilt_root / version
        git_path = metadata_root / f"{version}.git"
        record = versions.get(version)
        if not root.is_dir() or (root / ".git").exists() or not git_path.exists():
            raise LazarusVersionError(f"cached {version} version is not isolated")
        if not isinstance(record, dict) or tree_fingerprint(root) != record.get("tree_sha256"):
            raise LazarusVersionError(f"cached {version} tree fingerprint mismatch")
    return _pair_from_manifest(
        manifest,
        prebuilt_root=prebuilt_root,
        metadata_root=metadata_root,
        cache_hit=True,
        elapsed_ms=elapsed_ms,
    )


def _pair_from_manifest(
    manifest: Mapping[str, Any],
    *,
    prebuilt_root: Path,
    metadata_root: Path,
    cache_hit: bool,
    elapsed_ms: float,
) -> LazarusVersionPair:
    artifacts: dict[str, LazarusVersionArtifact] = {}
    for version in ("fixed", "buggy"):
        value = manifest["versions"][version]
        artifacts[version] = LazarusVersionArtifact(
            task_id=str(manifest["task_id"]),
            version=version,
            root=(prebuilt_root / version).resolve(),
            git_metadata=(metadata_root / f"{version}.git").resolve(),
            source_revision=str(manifest["source_revision"]),
            patch_sha256=str(manifest["patch_sha256"]),
            tree_sha256=str(value["tree_sha256"]),
            changed_files=tuple(str(item) for item in value["changed_files"]),
            build_identity=copy_json_mapping(value["build_identity"]),
            cache_hit=cache_hit,
        )
    return LazarusVersionPair(
        buggy=artifacts["buggy"],
        fixed=artifacts["fixed"],
        version_switch_ms=float(elapsed_ms),
    )


def copy_json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        copied = json.loads(json.dumps(dict(value), sort_keys=True))
    except (TypeError, ValueError) as error:
        raise LazarusVersionError(f"build identity is not JSON-serializable: {error}") from error
    if not isinstance(copied, dict):
        raise LazarusVersionError("build identity must be a JSON mapping")
    return copied
