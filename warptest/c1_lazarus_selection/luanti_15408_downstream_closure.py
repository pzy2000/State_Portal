"""Candidate-owned Irrlicht closure and bounded native build for Luanti #15408.

The #15408 fixed capsule contains the complete ``irr/`` source subtree and
the top-level CMake project builds it through ``add_subdirectory(irr)``.  This
module registers that subtree by an immutable local manifest, materializes a
fresh fixed/buggy pair from the same capsule, and performs one bounded
configure/build per variant.  It deliberately does not start Luanti, replay a
world, run tests, read old attempts, or perform a paid/formal action.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 15408}
_PROFILE_ID = "luanti-15408-collision-acceleration-v1-native-gate57"
_CAPSULE_REL = PurePosixPath("private/fixed-capsules/luanti/15408/fixed-source")
_IRR_REL = _CAPSULE_REL / "irr"
_PAIR_REL = PurePosixPath("private/materialized/luanti/15408/application-v2")
_BUILD_REL = PurePosixPath(
    "private/bounded-builds/luanti/15408/capsule-owned-irrlicht-v1"
)
_TOOLCHAIN_REL = PurePosixPath("private/dependencies/luanti-native/3.0")
_VERSION = "1.0"

_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/15408/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/15408/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/15408/preannotation/v2",
    "source_atomicity",
    "2.0",
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/15408/test_manifest/v2",
    "candidate_stage",
    "2.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/15408/fixed/v2",
    "fixed_source_capsule",
    "2.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_transition/luanti/15408/native/v1",
    "candidate_gate57_transition",
    "1.0",
)
_AGGREGATE = ArtifactReference(
    "native_regression_aggregate/luanti/15408/fixed-buggy/fresh-20260814-15408-v1",
    "native_regression_aggregate",
    "3.0",
)
_CLOSURE = ArtifactReference(
    "candidate_gate8_15408_irrlicht_dependency/luanti/15408/current-v2",
    "candidate_gate8_15408_irrlicht_dependency",
    _VERSION,
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/15408/fixed-buggy/v2",
    "candidate_materialization",
    "2.0",
)
_BUILD_KIND = "candidate_gate8_15408_native_build"
_BUILD_NAMESPACE = "capsule-owned-irrlicht-v1"
_BUILD_AGGREGATE = ArtifactReference(
    "candidate_gate8_15408_build/luanti/15408/capsule-owned-irrlicht-v1",
    "candidate_gate8_15408_build",
    _VERSION,
)
_BUILD_BLOCKER = ArtifactReference(
    "candidate_gate8_15408_build_blocker/luanti/15408/capsule-owned-irrlicht-v1",
    "candidate_gate8_15408_build_blocker",
    _VERSION,
)
_MATERIALIZATION_BLOCKER = ArtifactReference(
    "candidate_materialization_blocker/luanti/15408/fixed-buggy/v2",
    "candidate_materialization_blocker",
    _VERSION,
)


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


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


def _require_auth(authorization: AuthorizationRecord, scope: str) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != scope
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(f"#15408 downstream closure authorization {scope!r} is inactive")


def _root(artifact_root: str | Path) -> Path:
    return Path(artifact_root).absolute().resolve(strict=True)


def _ordinary_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"unsafe symlink directory: {path}")
    if not path.is_dir():
        raise RuntimeError(f"directory is missing: {path}")


def _ordinary_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"ordinary file is missing or unsafe: {path}")


def _file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_manifest(root: Path) -> tuple[dict[str, object], ...]:
    _ordinary_directory(root)
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise RuntimeError(f"capsule tree contains unsafe symlink: {relative}")
        if path.is_dir():
            entries.append({"path": relative, "kind": "directory"})
        elif path.is_file():
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "size": path.stat().st_size,
                    "fingerprint": _file_fingerprint(path),
                }
            )
        else:
            raise RuntimeError(f"capsule tree contains unsupported entry: {relative}")
    return tuple(entries)


def _manifest_fingerprint(entries: tuple[dict[str, object], ...]) -> str:
    encoded = json.dumps(list(entries), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _upstream(root: Path) -> dict[str, StableArtifact]:
    artifacts = {
        "source": _load(root, _SOURCE),
        "patch": _load(root, _PATCH),
        "atomicity": _load(root, _ATOMICITY),
        "manifest": _load(root, _MANIFEST),
        "capsule": _load(root, _CAPSULE),
        "transition": _load(root, _TRANSITION),
        "aggregate": _load(root, _AGGREGATE),
    }
    source = artifacts["source"]
    patch = artifacts["patch"]
    atomicity = artifacts["atomicity"]
    manifest = artifacts["manifest"]
    capsule = artifacts["capsule"]
    transition = artifacts["transition"]
    aggregate = artifacts["aggregate"]
    if source.payload.get("candidate") != _CANDIDATE or source.payload.get("complete") is not True:
        raise RuntimeError("#15408 source packet is not complete")
    if (
        patch.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != ("src/collision.cpp",)
        or tuple(patch.payload.get("test_paths", ())) != ("src/unittest/test_collision.cpp",)
    ):
        raise RuntimeError("#15408 patch scope is not current-v2 collision-only")
    if (
        atomicity.payload.get("candidate") != _CANDIDATE
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
    ):
        raise RuntimeError("#15408 atomicity is not accepted as one authoritative issue")
    if (
        manifest.payload.get("candidate") != _CANDIDATE
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("details", {}).get("executable_test_paths")
        != ["src/unittest/test_collision.cpp"]
    ):
        raise RuntimeError("#15408 test manifest is not passed")
    if (
        capsule.payload.get("candidate") != _CANDIDATE
        or capsule.payload.get("profile_id") != _PROFILE_ID
        or capsule.payload.get("source_tree_path") != _CAPSULE_REL.as_posix()
    ):
        raise RuntimeError("#15408 fixed capsule is not candidate-bound")
    if (
        transition.payload.get("candidate") != _CANDIDATE
        or transition.payload.get("state") != "passed"
        or transition.payload.get("outcome") != "qualified"
        or transition.payload.get("attempt_count") != 12
    ):
        raise RuntimeError("#15408 Gate 1--7 transition is not qualified")
    if (
        aggregate.payload.get("candidate") != _CANDIDATE
        or aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("attempt_count") != 12
    ):
        raise RuntimeError("#15408 native aggregate is not qualified")
    return artifacts


def register_capsule_owned_irrlicht(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Register only the ``irr/`` subtree physically owned by #15408 capsule."""

    _require_auth(authorization, "infrastructure-audit")
    root = _root(artifact_root)
    chain = _upstream(root)
    capsule_tree = root / _CAPSULE_REL
    irr_tree = root / _IRR_REL
    _ordinary_directory(capsule_tree)
    entries = _tree_manifest(irr_tree)
    top_cmake = capsule_tree / "CMakeLists.txt"
    irr_cmake = irr_tree / "CMakeLists.txt"
    irr_src_cmake = irr_tree / "src/CMakeLists.txt"
    for path in (top_cmake, irr_cmake, irr_src_cmake):
        _ordinary_file(path)
    top_text = top_cmake.read_text(encoding="utf-8")
    irr_text = irr_cmake.read_text(encoding="utf-8")
    src_text = irr_src_cmake.read_text(encoding="utf-8")
    if "add_subdirectory(irr EXCLUDE_FROM_ALL)" not in top_text:
        raise RuntimeError("#15408 top-level CMake does not add capsule-owned irr")
    if "TARGET IrrlichtMt" not in top_text or "IrrlichtMt::IrrlichtMt" not in top_text:
        raise RuntimeError("#15408 top-level CMake lacks IrrlichtMt target closure")
    if "project(Irrlicht LANGUAGES CXX)" not in irr_text or "add_subdirectory(src)" not in irr_text:
        raise RuntimeError("#15408 capsule-owned irr CMake entrypoint is incomplete")
    if "add_library(IrrlichtMt" not in src_text:
        raise RuntimeError("#15408 capsule-owned irr target definition is missing")
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "registered",
        "outcome": "candidate_capsule_owned_irrlicht_registered",
        "complete": True,
        "closure_kind": "capsule_owned_irrlicht_source",
        "profile_id": _PROFILE_ID,
        "capsule_tree_path": _CAPSULE_REL.as_posix(),
        "dependency_subtree_path": _IRR_REL.as_posix(),
        "cmake_entrypoint": {
            "top_level_add_subdirectory": "add_subdirectory(irr EXCLUDE_FROM_ALL)",
            "imported_target": "IrrlichtMt::IrrlichtMt",
            "subproject": "irr/CMakeLists.txt",
            "build_target": "IrrlichtMt",
        },
        "manifest": {
            "file_count": sum(1 for item in entries if item["kind"] == "file"),
            "directory_count": sum(1 for item in entries if item["kind"] == "directory"),
            "entries": list(entries),
            "fingerprint": _manifest_fingerprint(entries),
        },
        "candidate_owned": True,
        "other_candidate_dependency_reused": False,
        "network_fetch_performed": False,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (
        chain["source"].reference,
        chain["patch"].reference,
        chain["atomicity"].reference,
        chain["manifest"].reference,
        chain["capsule"].reference,
        chain["transition"].reference,
        chain["aggregate"].reference,
    )
    existing = _try_load(root, _CLOSURE)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#15408 Irrlicht closure receipt conflicts with capsule state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_CLOSURE.artifact_id,
        artifact_kind=_CLOSURE.artifact_kind,
        semantic_version=_CLOSURE.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _CLOSURE)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#15408 Irrlicht closure typed reload failed")
    return artifact


def _patch_sections(content: bytes) -> tuple[tuple[str, bytes], ...]:
    sections: list[tuple[str, bytes]] = []
    current_path: str | None = None
    current: list[bytes] = []
    for line in content.splitlines(keepends=True):
        if line.startswith(b"diff --git "):
            if current_path is not None:
                sections.append((current_path, b"".join(current)))
            fields = line.decode("utf-8", errors="surrogateescape").split()
            if len(fields) != 4 or not fields[2].startswith("a/") or not fields[3].startswith("b/"):
                raise RuntimeError("#15408 patch header is invalid")
            if fields[2][2:] != fields[3][2:]:
                raise RuntimeError("#15408 patch rename is outside inverse scope")
            current_path = fields[2][2:]
            current = [line]
        elif current_path is not None:
            current.append(line)
    if current_path is not None:
        sections.append((current_path, b"".join(current)))
    return tuple(sections)


def _apply_inverse(root: Path, patch: StableArtifact, buggy: Path) -> tuple[str, ...]:
    raw_path = patch.payload.get("raw_patch_path")
    if not isinstance(raw_path, str):
        raise RuntimeError("#15408 raw patch path is invalid")
    content = (root / PurePosixPath(raw_path)).read_bytes()
    sections = _patch_sections(content)
    expected = ("src/collision.cpp", "src/unittest/test_collision.cpp")
    if tuple(path for path, _ in sections) != expected:
        raise RuntimeError("#15408 raw patch scope drifted")
    production = sections[0][1]
    completed = subprocess.run(
        ("git", "apply", "-R", "--whitespace=nowarn", "-"),
        cwd=buggy,
        input=production,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "#15408 production inverse failed: "
            + completed.stderr.decode("utf-8", errors="replace")[-2000:]
        )
    return ("src/collision.cpp",)


def _copy_capsule(source: Path, destination: Path) -> None:
    _ordinary_directory(source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(source, destination, symlinks=False)
    _ordinary_directory(destination)


def _tree_summary(entries: tuple[dict[str, object], ...]) -> dict[str, int]:
    return {
        "file_count": sum(item["kind"] == "file" for item in entries),
        "directory_count": sum(item["kind"] == "directory" for item in entries),
    }


def materialize_fixed_buggy_pair(
    artifact_root: str | Path,
    *,
    closure: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Create one fresh pair from the candidate-owned capsule only."""

    _require_auth(authorization, "protocol-migration")
    root = _root(artifact_root)
    chain = _upstream(root)
    if closure.reference != _CLOSURE or closure.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#15408 Irrlicht closure binding is invalid")
    source = root / _CAPSULE_REL
    base = root / _PAIR_REL
    existing = _try_load(root, _MATERIALIZATION)
    if existing is not None:
        _validate_materialization(root, existing, closure)
        return existing
    if base.exists() or base.is_symlink():
        raise RuntimeError("#15408 materialization root already exists without typed receipt")
    staging = Path(tempfile.mkdtemp(prefix=".luanti-15408-pair-", dir=root / "private"))
    try:
        fixed = staging / "fixed"
        buggy = staging / "buggy"
        _copy_capsule(source, fixed)
        _copy_capsule(source, buggy)
        inverse_paths = _apply_inverse(root, chain["patch"], buggy)
        fixed_entries = _tree_manifest(fixed)
        buggy_entries = _tree_manifest(buggy)
        fixed_map = {item["path"]: item for item in fixed_entries}
        buggy_map = {item["path"]: item for item in buggy_entries}
        if set(fixed_map) != set(buggy_map):
            raise RuntimeError("#15408 fixed/buggy inventory differs outside inverse")
        changed = tuple(
            path
            for path in sorted(fixed_map)
            if fixed_map[path] != buggy_map[path]
        )
        if changed != inverse_paths:
            raise RuntimeError(f"#15408 inverse changed unexpected paths: {changed!r}")
        base.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(staging, base)
        fixed = base / "fixed"
        buggy = base / "buggy"
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "materialized",
        "outcome": "candidate_capsule_owned_fixed_buggy_pair_materialized",
        "complete": True,
        "materialization_mode": "full_capsule_pair_production_inverse_only",
        "fixed_tree_path": (_PAIR_REL / "fixed").as_posix(),
        "buggy_tree_path": (_PAIR_REL / "buggy").as_posix(),
        "fixed_tree_fingerprint": _manifest_fingerprint(_tree_manifest(fixed)),
        "buggy_tree_fingerprint": _manifest_fingerprint(_tree_manifest(buggy)),
        "production_inverse": {
            "paths": list(inverse_paths),
            "tests_preserved": True,
            "support_paths_touched": False,
        },
        "fixed_inventory_summary": _tree_summary(_tree_manifest(fixed)),
        "buggy_inventory_summary": _tree_summary(_tree_manifest(buggy)),
        "capsule_owned_irrlicht_closure": closure.reference.artifact_id,
        "candidate_execution_performed": False,
        "build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "downstream_start_replay": "not_claimed",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (
        chain["source"].reference,
        chain["patch"].reference,
        chain["atomicity"].reference,
        chain["manifest"].reference,
        chain["capsule"].reference,
        chain["transition"].reference,
        chain["aggregate"].reference,
        closure.reference,
    )
    write_stable_artifact(
        root,
        artifact_id=_MATERIALIZATION.artifact_id,
        artifact_kind=_MATERIALIZATION.artifact_kind,
        semantic_version=_MATERIALIZATION.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _MATERIALIZATION)
    _validate_materialization(root, artifact, closure)
    return artifact


def write_materialization_blocker(
    artifact_root: str | Path,
    *,
    closure: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Record the bounded production-inverse failure without building."""

    _require_auth(authorization, "protocol-migration")
    root = _root(artifact_root)
    chain = _upstream(root)
    if closure.reference != _CLOSURE or closure.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#15408 Irrlicht closure binding is invalid")
    pair_root = root / _PAIR_REL
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "production_inverse_changed_zero_paths",
        "failure": {
            "operation": "git apply -R --whitespace=nowarn",
            "production_path": "src/collision.cpp",
            "returncode": 0,
            "reported_inverse_paths": ["src/collision.cpp"],
            "observed_changed_paths": [],
            "fixed_buggy_pair_materialized": False,
            "pair_root_exists": pair_root.exists(),
        },
        "candidate_capsule_owned_irrlicht": closure.reference.artifact_id,
        "capsule_path": _CAPSULE_REL.as_posix(),
        "candidate_execution_performed": False,
        "configure_started": False,
        "build_started": False,
        "start_replay_performed": False,
        "old_attempts_loaded": False,
        "other_candidate_dependency_reused": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate_1_7_unchanged": True,
        "next_action": "re-audit source-preserving production inverse in a new namespace; do not reuse this failed pair",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (closure.reference, chain["patch"].reference, chain["capsule"].reference)
    existing = _try_load(root, _MATERIALIZATION_BLOCKER)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#15408 materialization blocker conflicts with exact failure")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_MATERIALIZATION_BLOCKER.artifact_id,
        artifact_kind=_MATERIALIZATION_BLOCKER.artifact_kind,
        semantic_version=_MATERIALIZATION_BLOCKER.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _MATERIALIZATION_BLOCKER)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#15408 materialization blocker typed reload failed")
    return artifact


def _validate_materialization(root: Path, artifact: StableArtifact, closure: StableArtifact) -> None:
    if (
        artifact.payload.get("candidate") != _CANDIDATE
        or artifact.payload.get("state") != "materialized"
        or artifact.payload.get("complete") is not True
        or artifact.payload.get("capsule_owned_irrlicht_closure") != closure.reference.artifact_id
    ):
        raise RuntimeError("#15408 materialization receipt is invalid")
    for variant in ("fixed", "buggy"):
        path_value = artifact.payload.get(f"{variant}_tree_path")
        if not isinstance(path_value, str):
            raise RuntimeError("#15408 materialization tree path is invalid")
        tree = root / PurePosixPath(path_value)
        _ordinary_directory(tree)
        if any(path.is_symlink() for path in tree.rglob("*")):
            raise RuntimeError("#15408 materialization contains symlink")


def _build_ref(variant: str, step: str) -> ArtifactReference:
    return ArtifactReference(
        f"{_BUILD_KIND}/luanti/15408/{variant}/{step}/{_BUILD_NAMESPACE}",
        _BUILD_KIND,
        _VERSION,
    )


def _write_process(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    variant: str,
    step: str,
    argv: tuple[str, ...],
    receipt: NativeProcessReceipt,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": _PROFILE_ID,
        "variant": variant,
        "step": step,
        "argv": list(argv),
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(receipt.stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(receipt.stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated,
        "stderr_truncated": receipt.stderr_truncated,
        "output_encoding": "base64",
        "candidate_execution_performed": False,
        "start_replay_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    existing = _try_load(root, reference)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"#15408 {variant} {step} receipt conflicts; no retry")
        return existing
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, reference)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError(f"#15408 {variant} {step} receipt typed reload failed")
    return artifact


def _rewrite_build_dir(argv: tuple[str, ...], build_dir: Path, key: str) -> tuple[str, ...]:
    result = list(argv)
    try:
        index = result.index(key)
    except ValueError as error:
        raise RuntimeError(f"#15408 build argv lacks {key}") from error
    if index + 1 >= len(result):
        raise RuntimeError(f"#15408 build argv lacks value after {key}")
    result[index + 1] = str(build_dir)
    return tuple(result)


def bounded_configure_build(
    artifact_root: str | Path,
    *,
    materialization: StableArtifact,
    closure: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Run one bounded configure/build pair for fixed and buggy trees."""

    _require_auth(authorization, "infrastructure-audit")
    root = _root(artifact_root)
    chain = _upstream(root)
    _validate_materialization(root, materialization, closure)
    aggregate_existing = _try_load(root, _BUILD_AGGREGATE)
    if aggregate_existing is not None:
        if (
            aggregate_existing.payload.get("candidate") != _CANDIDATE
            or aggregate_existing.payload.get("outcome") != "bounded_configure_build_passed"
        ):
            raise RuntimeError("#15408 existing downstream build aggregate is not passed")
        return aggregate_existing
    toolchain = root / _TOOLCHAIN_REL
    _ordinary_directory(toolchain)
    cmake = toolchain / "bin/cmake"
    ninja = toolchain / "bin/ninja"
    cxx = toolchain / "bin/c++"
    cc = toolchain / "bin/cc"
    ar = toolchain / "bin/ar"
    ranlib = toolchain / "bin/ranlib"
    linker = toolchain / "bin/ld"
    nm = toolchain / "bin/nm"
    strip = toolchain / "bin/strip"
    otool = toolchain / "bin/otool"
    install_name_tool = toolchain / "bin/install_name_tool"
    for path in (cmake, ninja, cxx, cc, ar, ranlib, linker, nm, strip, otool, install_name_tool):
        _ordinary_file(path)
    configure_base = (
        str(cmake),
        "-S",
        ".",
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_CLIENT=FALSE",
        "-DBUILD_SERVER=TRUE",
        "-DBUILD_UNITTESTS=TRUE",
        "-DBUILD_DOCUMENTATION=FALSE",
        "-DRUN_IN_PLACE=TRUE",
        "-DENABLE_CURL=FALSE",
        "-DENABLE_GETTEXT=FALSE",
        "-DENABLE_POSTGRESQL=FALSE",
        "-DENABLE_LEVELDB=FALSE",
        "-DENABLE_REDIS=FALSE",
        "-DENABLE_SPATIAL=FALSE",
        "-DENABLE_LUAJIT=FALSE",
        "-DENABLE_SYSTEM_GMP=FALSE",
        "-DENABLE_SYSTEM_JSONCPP=FALSE",
        f"-DCMAKE_MAKE_PROGRAM={ninja}",
        f"-DCMAKE_C_COMPILER={cc}",
        f"-DCMAKE_CXX_COMPILER={cxx}",
        f"-DCMAKE_AR={ar}",
        f"-DCMAKE_RANLIB={ranlib}",
        f"-DCMAKE_LINKER={linker}",
        f"-DCMAKE_NM={nm}",
        f"-DCMAKE_STRIP={strip}",
        f"-DCMAKE_OBJDUMP={otool}",
        f"-DCMAKE_INSTALL_NAME_TOOL={install_name_tool}",
        "-DENABLE_CURSES=FALSE",
        "-DENABLE_OPENSSL=FALSE",
        f"-DSQLITE3_INCLUDE_DIR={toolchain / 'packages/sqlite3/include'}",
        f"-DSQLITE3_LIBRARY={toolchain / 'packages/sqlite3/lib/libsqlite3.a'}",
        f"-DICONV_LIBRARY={toolchain / 'packages/iconv/lib/libiconv.tbd'}",
        f"-DZLIB_INCLUDE_DIR={toolchain / 'packages/zlib/include'}",
        f"-DZLIB_LIBRARY={toolchain / 'packages/zlib/lib/libz.a'}",
        f"-DZSTD_INCLUDE_DIR={toolchain / 'packages/zstd/include'}",
        f"-DZSTD_LIBRARY={toolchain / 'packages/zstd/lib/libzstd.a'}",
    )
    build_root = root / _BUILD_REL
    build_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        "HOME": str(root / "private/runtime-home/luanti-15408-downstream"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(toolchain / "bin"),
    }
    Path(environment["HOME"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    process_receipts: list[StableArtifact] = []
    failed: tuple[str, str, StableArtifact] | None = None
    for variant in ("fixed", "buggy"):
        tree = root / _PAIR_REL / variant
        _ordinary_directory(tree)
        build_dir = build_root / variant
        # Insert an explicit build directory while keeping the Ninja generator.
        configure_list = list(configure_base)
        configure_list[3:3] = ("-B", str(build_dir))
        configure = tuple(configure_list)
        build = (str(cmake), "--build", str(build_dir), "--target", "luantiserver", "--parallel", "1")
        configure_receipt = run_bounded_process(
            configure, cwd=tree, env=environment, timeout_seconds=900
        )
        configure_artifact = _write_process(
            root,
            _build_ref(variant, "configure"),
            (materialization.reference, closure.reference),
            variant,
            "configure",
            configure,
            configure_receipt,
            authorization,
        )
        process_receipts.append(configure_artifact)
        if configure_receipt.outcome != "completed" or configure_receipt.returncode != 0:
            failed = (variant, "configure", configure_artifact)
            break
        build_receipt = run_bounded_process(
            build, cwd=tree, env=environment, timeout_seconds=1800
        )
        build_artifact = _write_process(
            root,
            _build_ref(variant, "build"),
            (materialization.reference, closure.reference, configure_artifact.reference),
            variant,
            "build",
            build,
            build_receipt,
            authorization,
        )
        process_receipts.append(build_artifact)
        executable = tree / "bin/luantiserver"
        if (
            build_receipt.outcome != "completed"
            or build_receipt.returncode != 0
            or not executable.is_file()
        ):
            failed = (variant, "build", build_artifact)
            break
    if failed is not None:
        variant, step, evidence = failed
        blocker_payload: dict[str, Any] = {
            "candidate": _CANDIDATE,
            "state": "blocked",
            "outcome": "infrastructure_blocked",
            "terminal": False,
            "resumable": True,
            "reason": f"capsule_owned_irrlicht_{variant}_{step}_failed",
            "failed_variant": variant,
            "failed_step": step,
            "failed_receipt": evidence.reference.artifact_id,
            "candidate_capsule_owned_irrlicht": closure.reference.artifact_id,
            "candidate_materialization": materialization.reference.artifact_id,
            "candidate_execution_performed": False,
            "start_replay_performed": False,
            "old_attempts_loaded": False,
            "other_candidate_dependency_reused": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "does_not_invalidate_gate_1_7": True,
            "current_gate_status": {
                "gate_1_7": "qualified",
                "gate8_capsule_owned_irrlicht": "passed",
                "gate8_materialization": "passed",
                "gate8_bounded_configure_build": "blocked",
                "gate9_to_13": "pending",
            },
            "next_action": "inspect_exact_candidate_build_receipt_and_apply_only_source_preserving_repair_if_provable",
            "resource_policy": {"network": False, "paid": False, "formal_trials": False},
            "complete": True,
        }
        blocker_inputs = (
            materialization.reference,
            closure.reference,
            evidence.reference,
        )
        existing_blocker = _try_load(root, _BUILD_BLOCKER)
        if existing_blocker is None:
            write_stable_artifact(
                root,
                artifact_id=_BUILD_BLOCKER.artifact_id,
                artifact_kind=_BUILD_BLOCKER.artifact_kind,
                semantic_version=_BUILD_BLOCKER.semantic_version,
                created_at=_now(),
                inputs=blocker_inputs,
                payload=blocker_payload,
                authorization=authorization,
            )
            existing_blocker = _load(root, _BUILD_BLOCKER)
        if existing_blocker.inputs != blocker_inputs or dict(existing_blocker.payload) != blocker_payload:
            raise RuntimeError("#15408 build blocker conflicts with exact failure")
        raise RuntimeError(f"#15408 bounded {variant} {step} failed; blocker={existing_blocker.revision_path}")
    build_refs = tuple(item.reference for item in process_receipts)
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "passed",
        "outcome": "bounded_configure_build_passed",
        "complete": True,
        "candidate_capsule_owned_irrlicht": closure.reference.artifact_id,
        "candidate_materialization": materialization.reference.artifact_id,
        "variants": ["fixed", "buggy"],
        "steps_per_variant": ["configure", "build"],
        "target": "luantiserver",
        "parallelism": 1,
        "build_namespace": _BUILD_NAMESPACE,
        "output_paths": [
            (_PAIR_REL / "fixed/bin/luantiserver").as_posix(),
            (_PAIR_REL / "buggy/bin/luantiserver").as_posix(),
        ],
        "candidate_execution_performed": False,
        "start_replay_performed": False,
        "old_attempts_loaded": False,
        "other_candidate_dependency_reused": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate_1_7_unchanged": True,
        "gate9_to_13": "not_claimed",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (materialization.reference, closure.reference, *build_refs)
    existing = _try_load(root, _BUILD_AGGREGATE)
    if existing is None:
        write_stable_artifact(
            root,
            artifact_id=_BUILD_AGGREGATE.artifact_id,
            artifact_kind=_BUILD_AGGREGATE.artifact_kind,
            semantic_version=_BUILD_AGGREGATE.semantic_version,
            created_at=_now(),
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
        existing = _load(root, _BUILD_AGGREGATE)
    if existing.inputs != inputs or dict(existing.payload) != payload:
        raise RuntimeError("#15408 build aggregate typed reload conflicts")
    return existing


if __name__ == "__main__":
    root = Path(".warptest-cache/c1_lazarus_selection_v1")
    audit_auth = AuthorizationRecord(
        authorization_id="authorization/luanti-15408-irrlicht-closure-v1",
        scope="infrastructure-audit",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )
    migration_auth = AuthorizationRecord(
        authorization_id="authorization/luanti-15408-pair-materialization-v1",
        scope="protocol-migration",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )
    closure = register_capsule_owned_irrlicht(root, authorization=audit_auth)
    print("closure", closure.revision_path)
    materialization = materialize_fixed_buggy_pair(
        root, closure=closure, authorization=migration_auth
    )
    print("materialization", materialization.revision_path)
    try:
        build = bounded_configure_build(
            root,
            materialization=materialization,
            closure=closure,
            authorization=audit_auth,
        )
        print("build", build.revision_path)
    except RuntimeError as error:
        print(error)
        raise


__all__ = [
    "bounded_configure_build",
    "materialize_fixed_buggy_pair",
    "register_capsule_owned_irrlicht",
    "write_materialization_blocker",
]
