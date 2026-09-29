"""Authoritative candidate-owned IrrlichtMt 1.9.0mt9 source closure for #13132."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


REMOTE_URL = "https://github.com/minetest/irrlicht.git"
TAG = "1.9.0mt9"
COMMIT = "ce0d29df933fd03c5de218e159976629113257b9"
TREE = "7a85087dab624a147ee8b047678fed3637d1d548"
_CANDIDATE = {"repository_key": "luanti", "change_number": 13132}
_BASE = PurePosixPath("private/dependencies/luanti-13132-irrlicht/1.9.0mt9")
_REPOSITORY = _BASE / "upstream.git"
_SOURCE_TREE = _BASE / "source"
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/13132/source/v2", "candidate_source_packet", "2.0"
)
_BLOCKER = ArtifactReference(
    "candidate_gate8_13132_irrlichtmt9_blocker/luanti/13132/current-v2/v1",
    "candidate_gate8_13132_irrlichtmt9_blocker",
    "1.0",
)
_DEPENDENCY = ArtifactReference(
    "candidate_gate8_13132_irrlichtmt9_source/luanti/13132/current-v2/v1",
    "candidate_gate8_13132_irrlichtmt9_source",
    "1.0",
)


def register_luanti_13132_irrlichtmt9_source(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Validate the fetched upstream tag and persist its exact source inventory."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#13132 IrrlichtMt source authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source = _load(root, _SOURCE)
    blocker = _load(root, _BLOCKER)
    if (
        source.payload.get("candidate") != _CANDIDATE
        or source.payload.get("complete") is not True
        or blocker.payload.get("candidate") != _CANDIDATE
        or blocker.payload.get("terminal") is not False
        or blocker.payload.get("required_dependency", {}).get("version") != TAG
    ):
        raise RuntimeError("#13132 source/blocker chain drifted")

    repository = _ordinary_dir(root / _REPOSITORY, root)
    tree = _ordinary_dir(root / _SOURCE_TREE, root)
    remote = _git(repository, "remote", "get-url", "origin")
    tag_commit = _git(repository, "rev-parse", f"refs/tags/{TAG}^{{commit}}")
    head_commit = _git(repository, "rev-parse", "HEAD")
    tree_hash = _git(repository, "rev-parse", f"{COMMIT}^{{tree}}")
    if remote != REMOTE_URL or tag_commit != COMMIT or head_commit != COMMIT or tree_hash != TREE:
        raise RuntimeError("#13132 authoritative IrrlichtMt tag identity mismatch")
    cmake = _ordinary_file(tree / "CMakeLists.txt", root).read_text(encoding="utf-8")
    if "set(IRRLICHTMT_REVISION 9)" not in cmake or "VERSION 1.9.0.${IRRLICHTMT_REVISION}" not in cmake:
        raise RuntimeError("#13132 IrrlichtMt source version markers drifted")
    inventory = _inventory(tree)
    if len(inventory) != 373 or not any(item["path"] == "include/irrlicht.h" for item in inventory):
        raise RuntimeError("#13132 IrrlichtMt source inventory is incomplete")
    inventory_hash = hashlib.sha256(
        "".join(
            f"{item['path']}\0{item['mode']}\0{item['size']}\0{item['content_fingerprint']}\n"
            for item in inventory
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        "candidate": _CANDIDATE,
        "state": "registered",
        "outcome": "authoritative_tag_source_closed",
        "complete": True,
        "remote_url": REMOTE_URL,
        "tag": TAG,
        "tag_kind": "lightweight",
        "commit": COMMIT,
        "tree": TREE,
        "repository_path": _REPOSITORY.as_posix(),
        "source_tree_path": _SOURCE_TREE.as_posix(),
        "file_count": len(inventory),
        "file_inventory_fingerprint": inventory_hash,
        "file_inventory": inventory,
        "candidate_specific_namespace": True,
        "other_candidate_source_reused": False,
        "network_access": "read_only_upstream_fetch",
        "network_external_writes": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "materialize #13132 fixed/buggy pair and build each side with source-preserving C++17 override",
    }
    inputs = (_SOURCE, _BLOCKER)
    existing = _try_load(root, _DEPENDENCY)
    if existing is None:
        write_stable_artifact(
            root,
            artifact_id=_DEPENDENCY.artifact_id,
            artifact_kind=_DEPENDENCY.artifact_kind,
            semantic_version=_DEPENDENCY.semantic_version,
            created_at=_now(),
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#13132 IrrlichtMt source receipt conflicts")
    artifact = _load(root, _DEPENDENCY)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#13132 IrrlichtMt source typed reload failed")
    return artifact


def _inventory(root: Path) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if stat.S_ISLNK(info.st_mode):
            content = os.readlink(path).encode("utf-8", errors="surrogateescape")
        elif stat.S_ISREG(info.st_mode):
            content = path.read_bytes()
        else:
            raise RuntimeError("#13132 IrrlichtMt inventory contains special file")
        values.append(
            {
                "path": path.relative_to(root).as_posix(),
                "mode": stat.S_IMODE(info.st_mode),
                "size": len(content),
                "content_fingerprint": hashlib.sha256(content).hexdigest(),
            }
        )
    return values


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *args),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("#13132 IrrlichtMt git identity probe failed")
    return result.stdout.decode("utf-8").strip()


def _ordinary_dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError("#13132 IrrlichtMt directory is unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError("#13132 IrrlichtMt file is unsafe")
    return resolved


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
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["register_luanti_13132_irrlichtmt9_source"]
