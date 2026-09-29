"""Current-v2 full source materialization for Luanti change 9722.

This adapter is deliberately narrower than the generic native builders.  It
materializes a complete fixed application tree from the retained Luanti git
mirror and derives the buggy tree by applying only the production inverse
from the current-v2 patch.  The fixed tests stay identical in both trees.

The adapter does not build, launch, replay, or create semantic observations.
It writes one typed ``candidate_materialization`` receipt whose inputs bind
the current source/patch/atomicity/manifest and fresh Gate 5--7 evidence.
Older downstream artifacts are never read or inherited.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATE = CandidateKey("luanti", 9722)
CONFIRMATION = "MATERIALIZE_LUANTI_9722_FULL_PAIR_V2"
_MIRROR = PurePosixPath(
    "private/repository-enumeration-git/luanti/mirror.git"
)
_BASE = PurePosixPath("private/materialized/luanti/9722/application-v2")
_REFERENCE = ArtifactReference(
    "candidate_materialization/luanti/9722/fixed-buggy/v2",
    "candidate_materialization",
    "2.0",
)
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/9722/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/9722/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/9722/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/9722/test_manifest/v2", "candidate_stage", "2.0"
)
_GATE57_AGGREGATE = ArtifactReference(
    "candidate_gate57_aggregate/luanti/9722/lua/v1",
    "candidate_gate57_aggregate",
    "1.0",
)
_GATE57_TRANSITION = ArtifactReference(
    "candidate_gate57_transition/luanti/9722/lua/v1",
    "candidate_gate57_transition",
    "1.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/9722/fixed/v2", "fixed_source_capsule", "2.0"
)


def materialize_luanti_9722_full_pair(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
) -> StableArtifact:
    """Materialize and persist the current-v2 fixed/buggy application pair.

    The operation is source-only.  It is idempotent after a successful
    receipt: a subsequent call validates the existing pair rather than
    overwriting it.
    """

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    chain = _load_current_chain(root)
    source_revision, base_revision = _source_revisions(root, chain[1])
    mirror = _ordinary_directory(root, _MIRROR)
    _require_commit(mirror, source_revision)
    _require_commit(mirror, base_revision)
    inverse_patch = _production_patch(root, chain[2])
    base = _registered_base(root)
    existing = _try_load(root, _REFERENCE)
    if existing is not None:
        _validate_receipt(
            root,
            existing,
            chain=chain,
            source_revision=source_revision,
            base_revision=base_revision,
            inverse_patch=inverse_patch,
        )
        return existing
    if base.exists() or base.is_symlink():
        # A prior invocation may have completed the filesystem move before a
        # receipt-schema validation failed. Reuse only that exact pair after
        # checking the current-v2 inverse boundary; never overwrite it.
        fixed = base / "fixed"
        buggy = base / "buggy"
        fixed_inventory = _tree_inventory(fixed)
        buggy_inventory = _tree_inventory(buggy)
        _validate_inverse(
            fixed,
            buggy,
            fixed_inventory=fixed_inventory,
            buggy_inventory=buggy_inventory,
        )
    else:
        parent = base.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = Path(tempfile.mkdtemp(prefix=".luanti-9722-materialize-", dir=parent))
        try:
            fixed = staging / "fixed"
            buggy = staging / "buggy"
            _extract_commit(mirror, source_revision, fixed)
            _copy_tree(fixed, buggy)
            fixed_inventory = _tree_inventory(fixed)
            _apply_inverse(buggy, inverse_patch)
            buggy_inventory = _tree_inventory(buggy)
            _validate_inverse(
                fixed,
                buggy,
                fixed_inventory=fixed_inventory,
                buggy_inventory=buggy_inventory,
            )
            os.replace(staging, base)
            fixed = base / "fixed"
            buggy = base / "buggy"
        except BaseException:
            _remove_tree(staging)
            raise
    fixed_digest = _tree_fingerprint(fixed)
    buggy_digest = _tree_fingerprint(buggy)

    payload = {
        "candidate": CANDIDATE.to_value(),
        "candidate_lifecycle": "active",
        "complete": True,
        "state": "materialized",
        "materialization_mode": "full_source_pair_no_build",
        "fixed_tree_path": f"{_BASE.as_posix()}/fixed",
        "buggy_tree_path": f"{_BASE.as_posix()}/buggy",
        "fixed_source_revision": source_revision,
        "buggy_base_revision": base_revision,
        "production_inverse": {
            "paths": ["builtin/common/serialize.lua"],
            "apply": ["git", "apply", "-R", "--whitespace=nowarn", "-"],
            "tests_preserved": True,
        },
        "fixed_tree_fingerprint": fixed_digest,
        "buggy_tree_fingerprint": buggy_digest,
        "fixed_inventory_summary": _inventory_summary(fixed_inventory),
        "buggy_inventory_summary": _inventory_summary(buggy_inventory),
        "downstream_state": "pending",
        "deferred_gates": [
            "registered_start_and_ordered_replay",
            "hidden_oracle_and_semantic_observations",
            "common_before_fixed_after_buggy_after_goldens",
            "before_equivalence_and_after_2x2_visual_cross_judgment",
            "same_lineage_construction_receipt",
        ],
        "no_build_or_launch": True,
        "no_old_artifact_inheritance": True,
    }
    created_at = _created_after(max(item.created_at for item in chain), authorization.granted_at)
    write_stable_artifact(
        root,
        artifact_id=_REFERENCE.artifact_id,
        artifact_kind=_REFERENCE.artifact_kind,
        semantic_version=_REFERENCE.semantic_version,
        created_at=created_at,
        inputs=tuple(item.reference for item in chain),
        payload=payload,
        authorization=authorization,
    )
    artifact = load_stable_artifact(
        root,
        artifact_id=_REFERENCE.artifact_id,
        artifact_kind=_REFERENCE.artifact_kind,
        semantic_version=_REFERENCE.semantic_version,
    )
    _validate_receipt(
        root,
        artifact,
        chain=chain,
        source_revision=source_revision,
        base_revision=base_revision,
        inverse_patch=inverse_patch,
    )
    return artifact


def load_luanti_9722_full_pair(
    artifact_root: str | Path,
) -> StableArtifact:
    root = _ordinary_root(Path(artifact_root).absolute())
    chain = _load_current_chain(root)
    source_revision, base_revision = _source_revisions(root, chain[1])
    inverse_patch = _production_patch(root, chain[2])
    artifact = load_stable_artifact(
        root,
        artifact_id=_REFERENCE.artifact_id,
        artifact_kind=_REFERENCE.artifact_kind,
        semantic_version=_REFERENCE.semantic_version,
    )
    _validate_receipt(
        root,
        artifact,
        chain=chain,
        source_revision=source_revision,
        base_revision=base_revision,
        inverse_patch=inverse_patch,
    )
    return artifact


def _load_current_chain(root: Path) -> tuple[StableArtifact, ...]:
    refs = (
        _ENUMERATION,
        _SOURCE,
        _PATCH,
        _ATOMICITY,
        _MANIFEST,
        _GATE57_AGGREGATE,
        _GATE57_TRANSITION,
        _CAPSULE,
    )
    artifacts = tuple(_load(root, ref) for ref in refs)
    source, patch, atomicity, manifest, aggregate, transition, capsule = artifacts[1:]
    if source.inputs != (_ENUMERATION,) or source.payload != {
        "candidate": CANDIDATE.to_value(),
        "complete": True,
        "raw_evidence_path": source.payload.get("raw_evidence_path"),
    }:
        raise RuntimeError("current 9722 source packet is invalid")
    if patch.inputs != (_ENUMERATION, _SOURCE) or patch.payload.get("candidate") != CANDIDATE.to_value():
        raise RuntimeError("current 9722 patch binding is invalid")
    if patch.payload.get("complete") is not True or patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("current 9722 patch is not eligible")
    if patch.payload.get("production_paths") != ["builtin/common/serialize.lua"]:
        raise RuntimeError("current 9722 production patch scope is invalid")
    if patch.payload.get("test_paths") != ["builtin/common/tests/serialize_spec.lua"]:
        raise RuntimeError("current 9722 test patch scope is invalid")
    if atomicity.payload.get("candidate") != CANDIDATE.to_value() or atomicity.payload.get("outcome") != "accepted" or atomicity.payload.get("complete") is not True:
        raise RuntimeError("current 9722 source atomicity is invalid")
    if manifest.payload.get("candidate") != CANDIDATE.to_value() or manifest.payload.get("stage") != "test_manifest" or manifest.payload.get("state") != "passed" or manifest.payload.get("complete") is not True:
        raise RuntimeError("current 9722 test manifest is invalid")
    if aggregate.inputs == () or aggregate.payload.get("candidate") != CANDIDATE.to_value() or aggregate.payload.get("qualified") is not True or aggregate.payload.get("all_expected") is not True or aggregate.payload.get("attempt_count") != 12:
        raise RuntimeError("fresh 9722 Gate57 aggregate is invalid")
    if transition.inputs != (_GATE57_AGGREGATE,) and transition.payload.get("state") != "passed":
        raise RuntimeError("fresh 9722 Gate57 transition is invalid")
    if transition.payload.get("candidate") != CANDIDATE.to_value() or transition.payload.get("state") != "passed" or transition.payload.get("terminal") is not False:
        raise RuntimeError("fresh 9722 Gate57 transition is invalid")
    expected_capsule = {
        "candidate": CANDIDATE.to_value(),
        "profile_id": "luanti-9722-reversible-number-serialize-v1",
        "source_tree_path": "private/fixed-capsules/luanti/9722/fixed-source-v2",
        "support_paths": ["builtin/common/vector.lua"],
        "test_paths": ["builtin/common/tests/serialize_spec.lua"],
    }
    if capsule.inputs != (_SOURCE,) or capsule.payload != expected_capsule:
        raise RuntimeError("current 9722 fixed capsule is invalid")
    return artifacts


def _source_revisions(root: Path, source: StableArtifact) -> tuple[str, str]:
    raw_path = source.payload.get("raw_evidence_path")
    if type(raw_path) is not str:
        raise RuntimeError("current 9722 source evidence path is invalid")
    raw = _strict_json(root / PurePosixPath(raw_path))
    pull = raw.get("pull")
    if type(pull) is not dict:
        raise RuntimeError("current 9722 pull evidence is invalid")
    fixed = pull.get("merge_commit_sha")
    base_obj = pull.get("base")
    base = base_obj.get("sha") if type(base_obj) is dict else None
    if not (_sha(fixed) and _sha(base)):
        raise RuntimeError("current 9722 source revisions are not closed")
    return str(fixed), str(base)


def _production_patch(root: Path, patch: StableArtifact) -> bytes:
    path = patch.payload.get("raw_patch_path")
    if type(path) is not str:
        raise RuntimeError("current 9722 raw patch path is invalid")
    content = (root / PurePosixPath(path)).read_bytes()
    sections = _patch_sections(content)
    expected = ("builtin/common/serialize.lua", "builtin/common/tests/serialize_spec.lua")
    if tuple(section[0] for section in sections) != expected:
        raise RuntimeError("current 9722 raw patch scope drifted")
    production = sections[0][1]
    if b"string.format(\"%.17g\", x)" not in production:
        raise RuntimeError("current 9722 production patch does not bind defect")
    return production


def _patch_sections(content: bytes) -> tuple[tuple[str, bytes], ...]:
    chunks: list[tuple[str, bytes]] = []
    current_path: str | None = None
    current: list[bytes] = []
    for line in content.splitlines(keepends=True):
        if line.startswith(b"diff --git "):
            if current_path is not None:
                chunks.append((current_path, b"".join(current)))
            fields = line.decode("utf-8", errors="surrogateescape").split()
            if len(fields) != 4 or not fields[2].startswith("a/") or not fields[3].startswith("b/") or fields[2][2:] != fields[3][2:]:
                raise RuntimeError("current 9722 patch header is invalid")
            current_path = fields[2][2:]
            current = [line]
        elif current_path is not None:
            current.append(line)
    if current_path is not None:
        chunks.append((current_path, b"".join(current)))
    return tuple(chunks)


def _extract_commit(mirror: Path, revision: str, destination: Path) -> None:
    archive = _run(("git", f"--git-dir={mirror}", "archive", "--format=tar", revision))
    _extract_archive(archive, destination)


def _extract_archive(content: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
        members = archive.getmembers()
        for member in members:
            _validate_member(member, destination)
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        for member in members:
            target = destination / PurePosixPath(member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=stat.S_IMODE(member.mode) or 0o700)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError("current 9722 archive file is unreadable")
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), stat.S_IMODE(member.mode) or 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    shutil.copyfileobj(source, handle)
            elif member.issym():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.symlink(member.linkname, target)
            else:
                raise RuntimeError("current 9722 archive contains unsupported entry")


def _validate_member(member: tarfile.TarInfo, destination: Path) -> None:
    pure = PurePosixPath(member.name)
    if pure.is_absolute() or "." in pure.parts or ".." in pure.parts or pure.as_posix() != member.name.rstrip("/"):
        raise RuntimeError("current 9722 archive member path is unsafe")
    if not (member.isdir() or member.isfile() or member.issym()):
        raise RuntimeError("current 9722 archive contains unsupported entry")
    if member.issym():
        link = PurePosixPath(member.linkname)
        resolved = (destination / pure.parent / link).resolve()
        if resolved != destination and destination not in resolved.parents:
            raise RuntimeError("current 9722 archive symlink escapes tree")


def _apply_inverse(tree: Path, patch: bytes) -> None:
    env = {
        "HOME": str(tree / ".closed-home"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CEILING_DIRECTORIES": str(tree.parent),
    }
    result = subprocess.run(
        ("git", "apply", "-R", "--whitespace=nowarn", "-"),
        cwd=tree,
        input=patch,
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError("current 9722 production inverse failed")


def _validate_inverse(fixed: Path, buggy: Path, *, fixed_inventory: dict[str, object], buggy_inventory: dict[str, object]) -> None:
    if set(fixed_inventory) != set(buggy_inventory):
        raise RuntimeError("current 9722 inverse changed tree membership")
    changed = [path for path in fixed_inventory if fixed_inventory[path] != buggy_inventory[path]]
    if changed != ["builtin/common/serialize.lua"]:
        raise RuntimeError("current 9722 inverse changed non-production files")
    for path in ("builtin/common/tests/serialize_spec.lua", "builtin/common/vector.lua"):
        if (fixed / path).read_bytes() != (buggy / path).read_bytes():
            raise RuntimeError("current 9722 fixed tests/support were not preserved")


def _tree_inventory(root: Path) -> dict[str, object]:
    inventory: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            resolved = path.resolve()
            if resolved != root and root not in resolved.parents:
                raise RuntimeError("current 9722 tree symlink escapes root")
            inventory[relative] = {"kind": "symlink", "target": target}
        elif stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            inventory[relative] = {"kind": "file", "content_fingerprint": digest, "mode": stat.S_IMODE(info.st_mode)}
        elif stat.S_ISDIR(info.st_mode):
            inventory[relative] = {"kind": "directory", "mode": stat.S_IMODE(info.st_mode)}
        else:
            raise RuntimeError("current 9722 tree contains unsafe entry")
    return inventory


def _tree_fingerprint(root: Path) -> str:
    inventory = _tree_inventory(root)
    encoded = repr(sorted(inventory.items())).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _inventory_summary(inventory: dict[str, object]) -> dict[str, int]:
    counts = {"entries": len(inventory), "files": 0, "directories": 0, "symlinks": 0}
    for value in inventory.values():
        kind = value.get("kind") if isinstance(value, dict) else None
        if kind == "file":
            counts["files"] += 1
        elif kind == "directory":
            counts["directories"] += 1
        elif kind == "symlink":
            counts["symlinks"] += 1
    return counts


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True)


def _validate_receipt(root: Path, artifact: StableArtifact, *, chain: tuple[StableArtifact, ...], source_revision: str, base_revision: str, inverse_patch: bytes) -> None:
    if artifact.inputs != tuple(item.reference for item in chain):
        raise RuntimeError("current 9722 materialization lineage drifted")
    payload = artifact.payload
    if payload.get("candidate") != CANDIDATE.to_value() or payload.get("state") != "materialized" or payload.get("complete") is not True or payload.get("no_old_artifact_inheritance") is not True:
        raise RuntimeError("current 9722 materialization receipt is invalid")
    if payload.get("fixed_source_revision") != source_revision or payload.get("buggy_base_revision") != base_revision:
        raise RuntimeError("current 9722 materialization revisions drifted")
    fixed = root / PurePosixPath(str(payload.get("fixed_tree_path")))
    buggy = root / PurePosixPath(str(payload.get("buggy_tree_path")))
    if fixed == buggy or not fixed.is_dir() or not buggy.is_dir() or fixed.is_symlink() or buggy.is_symlink():
        raise RuntimeError("current 9722 materialization pair is missing or unsafe")
    fixed_inventory = _tree_inventory(fixed)
    buggy_inventory = _tree_inventory(buggy)
    if payload.get("fixed_inventory_summary") != _inventory_summary(fixed_inventory) or payload.get("buggy_inventory_summary") != _inventory_summary(buggy_inventory):
        raise RuntimeError("current 9722 materialization inventory drifted")
    if payload.get("fixed_tree_fingerprint") != _tree_fingerprint(fixed) or payload.get("buggy_tree_fingerprint") != _tree_fingerprint(buggy):
        raise RuntimeError("current 9722 materialization fingerprint drifted")
    _validate_inverse(fixed, buggy, fixed_inventory=fixed_inventory, buggy_inventory=buggy_inventory)
    if not inverse_patch:
        raise RuntimeError("current 9722 production inverse is empty")


def _registered_base(root: Path) -> Path:
    current = root
    for part in _BASE.parts[:-1]:
        current /= part
        if current.exists() and (current.is_symlink() or not current.is_dir()):
            raise RuntimeError("current 9722 materialization parent is unsafe")
        current.mkdir(mode=0o700, exist_ok=True)
    return current / _BASE.parts[-1]


def _ordinary_root(root: Path) -> Path:
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return root.resolve(strict=True)


def _ordinary_directory(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("retained Luanti mirror is unsafe")
    return current.resolve(strict=True)


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _require_commit(mirror: Path, revision: str) -> None:
    _run(("git", f"--git-dir={mirror}", "show", "--no-patch", "--format=", revision))


def _run(argv: tuple[str, ...]) -> bytes:
    try:
        result = subprocess.run(argv, check=False, capture_output=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("current 9722 retained mirror command failed") from error
    if result.returncode != 0:
        raise RuntimeError("current 9722 retained mirror command failed")
    return result.stdout


def _strict_json(path: Path) -> dict[str, object]:
    import json

    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise RuntimeError("current 9722 source evidence is invalid")
    return value


def _sha(value: object) -> bool:
    return type(value) is str and len(value) == 40 and all(char in "0123456789abcdef" for char in value)


def _created_after(*values: str) -> str:
    latest = max(datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values)
    return (latest + timedelta(microseconds=1)).astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _require_authorization(value: AuthorizationRecord) -> None:
    if type(value) is not AuthorizationRecord or value.scope != "version-materialization" or value.revoked or value.local_user_id != os.getuid():
        raise PermissionError("current 9722 materialization authorization is inactive")


def _remove_tree(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            path.rmdir()
    root.rmdir()


__all__ = [
    "CANDIDATE",
    "CONFIRMATION",
    "load_luanti_9722_full_pair",
    "materialize_luanti_9722_full_pair",
]
