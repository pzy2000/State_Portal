"""Candidate-owned materialization and single-thread native build for #13132."""

from __future__ import annotations

import base64
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

from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "BUILD_LUANTI_13132_FIXED_BUGGY_SINGLE_THREAD"
_CANDIDATE = {"repository_key": "luanti", "change_number": 13132}
_REVISION = "ab1fe80150a8031fb702b9ff776db2db4dea89a4"
_BASE_REVISION = "a3177b89d8e68d39b8e2ad24e88cd87820d1135a"
_MIRROR = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_PAIR = PurePosixPath("private/materialized/luanti/13132/application-v1")
_IRR = PurePosixPath(
    "private/dependencies/luanti-13132-irrlicht/1.9.0mt9/source"
)
_TOOLCHAIN = PurePosixPath("private/dependencies/luanti-native/3.0")
_BUILD_ROOT = PurePosixPath("private/bounded-builds/luanti/13132/native-v1")
_LAUNCHER = PurePosixPath("private/tools/luanti/13132/cxx17-v1/launcher")
_LAUNCHER_CONTENT = (
    b'#!/bin/sh\nset -eu\ncompiler="$1"\nshift\nexec "$compiler" "$@" -std=c++17\n'
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/13132/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/13132/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/13132/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/13132/test_manifest/v2", "candidate_stage", "2.0"
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_13132_timeout_repair_aggregate/luanti/13132/lua/v1",
    "candidate_gate57_13132_timeout_repair_aggregate",
    "1.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_13132_timeout_repair_transition/luanti/13132/lua/v1",
    "candidate_gate57_13132_timeout_repair_transition",
    "1.0",
)
_DEPENDENCY = ArtifactReference(
    "candidate_gate8_13132_irrlichtmt9_source/luanti/13132/current-v2/v1",
    "candidate_gate8_13132_irrlichtmt9_source",
    "1.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/13132/fixed-buggy/v1",
    "candidate_materialization",
    "1.0",
)
_BUILD = ArtifactReference(
    "candidate_gate8_13132_native_build/luanti/13132/current-v2/v1",
    "candidate_gate8_13132_native_build",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate8_13132_native_build_blocker/luanti/13132/current-v2/v1",
    "candidate_gate8_13132_native_build_blocker",
    "1.0",
)


def materialize_luanti_13132_pair(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    _require_auth(authorization, {"infrastructure-audit", "preannotation-execution"})
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    mirror = _ordinary_dir(root / _MIRROR, root)
    _require_commit(mirror, _REVISION)
    _require_commit(mirror, _BASE_REVISION)
    production_patch = _production_patch(root, chain[1])
    pair = root / _PAIR
    existing = _try_load(root, _MATERIALIZATION)
    if existing is None and not pair.exists():
        pair.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = Path(tempfile.mkdtemp(prefix=".13132-pair-", dir=pair.parent))
        try:
            fixed = staging / "fixed"
            buggy = staging / "buggy"
            _extract_commit(mirror, _REVISION, fixed)
            shutil.copytree(fixed, buggy, symlinks=True)
            result = subprocess.run(
                ("patch", "-p1", "-R", "--batch"),
                cwd=buggy,
                input=production_patch,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("#13132 production inverse failed")
            os.replace(staging, pair)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    fixed = _ordinary_dir(pair / "fixed", root)
    buggy = _ordinary_dir(pair / "buggy", root)
    _validate_pair(fixed, buggy)
    payload = {
        "candidate": _CANDIDATE,
        "state": "materialized",
        "complete": True,
        "fixed_tree_path": f"{_PAIR.as_posix()}/fixed",
        "buggy_tree_path": f"{_PAIR.as_posix()}/buggy",
        "fixed_revision": _REVISION,
        "base_revision": _BASE_REVISION,
        "production_inverse_paths": ["builtin/common/misc_helpers.lua"],
        "fixed_tests_preserved_on_buggy": True,
        "fixed_tree_fingerprint": _tree_fingerprint(fixed),
        "buggy_tree_fingerprint": _tree_fingerprint(buggy),
        "candidate_owned_dependency": _DEPENDENCY.artifact_id,
        "other_candidate_source_reused": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "single-thread fixed/buggy server build with final C++17 launcher override",
    }
    inputs = tuple(item.reference for item in chain)
    if existing is None:
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
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#13132 materialization receipt conflicts")
    return _load(root, _MATERIALIZATION)


def build_luanti_13132_pair(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> StableArtifact:
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _require_auth(authorization, {"preannotation-execution"})
    root = Path(artifact_root).absolute().resolve(strict=True)
    materialization = materialize_luanti_13132_pair(root, authorization=authorization)
    dependency = _load(root, _DEPENDENCY)
    launcher = root / _LAUNCHER
    if launcher.exists() or launcher.is_symlink():
        if launcher.is_symlink() or launcher.read_bytes() != _LAUNCHER_CONTENT:
            raise RuntimeError("#13132 C++17 launcher drifted")
    else:
        launcher.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        launcher.write_bytes(_LAUNCHER_CONTENT)
        launcher.chmod(0o700)
    existing = _try_load(root, _BUILD)
    if existing is not None:
        return existing
    steps: list[StableArtifact] = []
    binaries: dict[str, str] = {}
    for variant in ("fixed", "buggy"):
        tree = _ordinary_dir(root / _PAIR / variant, root)
        build = root / _BUILD_ROOT / variant
        build.mkdir(parents=True, exist_ok=True, mode=0o700)
        step = _load_success_step(root, variant, "configure", materialization, dependency)
        if step is None:
            configure = _configure_argv(root, tree, build, launcher)
            receipt = run_bounded_process(
                configure,
                cwd=tree,
                env=_environment(root, variant),
                timeout_seconds=900,
            )
            step = _write_step(
                root, variant, "configure", configure, receipt, materialization, dependency, authorization
            )
        else:
            receipt = NativeProcessReceipt("completed", 0, b"", b"")
        steps.append(step)
        if not _ok(receipt):
            return _write_blocker(root, materialization, dependency, steps, variant, "configure", receipt, authorization)
        build_argv = (
            str(root / _TOOLCHAIN / "bin/cmake"),
            "--build",
            str(build),
            "--target",
            "minetestserver",
            "--",
            "-j1",
        )
        step = _load_success_step(root, variant, "build", materialization, dependency)
        if step is None:
            receipt = run_bounded_process(
                build_argv,
                cwd=tree,
                env=_environment(root, variant),
                timeout_seconds=3600,
            )
            step = _write_step(
                root, variant, "build", build_argv, receipt, materialization, dependency, authorization
            )
        else:
            receipt = NativeProcessReceipt("completed", 0, b"", b"")
        steps.append(step)
        if not _ok(receipt):
            return _write_blocker(root, materialization, dependency, steps, variant, "build", receipt, authorization)
        candidates = (
            tree / "bin/minetestserver",
            tree / "bin/luantiserver",
            build / "bin/minetestserver",
            build / "bin/luantiserver",
        )
        binary = next((path for path in candidates if path.is_file()), None)
        if binary is None:
            raise RuntimeError("#13132 server binary missing after successful build")
        binaries[variant] = binary.relative_to(root).as_posix()
    payload = {
        "candidate": _CANDIDATE,
        "state": "built",
        "outcome": "fixed_buggy_native_build_passed",
        "complete": True,
        "single_threaded": True,
        "build_parallelism": 1,
        "language_standard": "c++17",
        "source_preserving_launcher": _LAUNCHER.as_posix(),
        "launcher_fingerprint": hashlib.sha256(_LAUNCHER_CONTENT).hexdigest(),
        "production_or_test_source_modified_by_override": False,
        "fixed_binary": binaries["fixed"],
        "buggy_binary": binaries["buggy"],
        "fixed_binary_fingerprint": _file_fingerprint(root / binaries["fixed"]),
        "buggy_binary_fingerprint": _file_fingerprint(root / binaries["buggy"]),
        "step_count": 4,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "registered Start, ordered replay, hidden oracle, semantic observations, goldens and visual cross-judgment",
    }
    inputs = (materialization.reference, dependency.reference, *(item.reference for item in steps))
    write_stable_artifact(
        root,
        artifact_id=_BUILD.artifact_id,
        artifact_kind=_BUILD.artifact_kind,
        semantic_version=_BUILD.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    return _load(root, _BUILD)


def _load_chain(root: Path) -> tuple[StableArtifact, ...]:
    refs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _AGGREGATE, _TRANSITION, _DEPENDENCY)
    chain = tuple(_load(root, ref) for ref in refs)
    if any(item.payload.get("candidate") != _CANDIDATE for item in chain):
        raise RuntimeError("#13132 materialization chain candidate drifted")
    if chain[1].payload.get("production_paths") != ["builtin/common/misc_helpers.lua"]:
        raise RuntimeError("#13132 production inverse scope drifted")
    if chain[2].payload.get("outcome") != "accepted" or chain[3].payload.get("state") != "passed":
        raise RuntimeError("#13132 Gate 1--4 chain drifted")
    if chain[4].payload.get("qualified") is not True or chain[4].payload.get("no_timeout_or_unknown") is not True:
        raise RuntimeError("#13132 strict Gate 5--7 chain drifted")
    if chain[5].payload.get("qualified") is not True or chain[6].payload.get("commit") != "ce0d29df933fd03c5de218e159976629113257b9":
        raise RuntimeError("#13132 transition/dependency chain drifted")
    return chain


def _production_patch(root: Path, patch: StableArtifact) -> bytes:
    raw = patch.payload.get("raw_patch_path")
    if type(raw) is not str:
        raise RuntimeError("#13132 raw patch path is invalid")
    content = (root / PurePosixPath(raw)).read_bytes()
    markers = [index for index, line in enumerate(content.splitlines(keepends=True)) if line.startswith(b"diff --git ")]
    lines = content.splitlines(keepends=True)
    if not markers or not lines[markers[0]].startswith(b"diff --git a/builtin/common/misc_helpers.lua "):
        raise RuntimeError("#13132 production patch header drifted")
    end = markers[1] if len(markers) > 1 else len(lines)
    result = b"".join(lines[markers[0]:end])
    if b'error("string.split separator is empty", 2)' not in result:
        raise RuntimeError("#13132 production patch defect binding drifted")
    return result


def _validate_pair(fixed: Path, buggy: Path) -> None:
    production = PurePosixPath("builtin/common/misc_helpers.lua")
    test = PurePosixPath("builtin/common/tests/misc_helpers_spec.lua")
    if (fixed / production).read_bytes() == (buggy / production).read_bytes():
        raise RuntimeError("#13132 production inverse did not change buggy tree")
    if (fixed / test).read_bytes() != (buggy / test).read_bytes():
        raise RuntimeError("#13132 fixed test changed on buggy tree")
    if b'string.split separator is empty' not in (fixed / production).read_bytes():
        raise RuntimeError("#13132 fixed production guard is missing")
    if b'string.split separator is empty' in (buggy / production).read_bytes():
        raise RuntimeError("#13132 buggy production guard was not removed")


def _configure_argv(root: Path, tree: Path, build: Path, launcher: Path) -> tuple[str, ...]:
    tc = root / _TOOLCHAIN
    packages = tc / "packages"
    return (
        str(tc / "bin/cmake"), "-S", str(tree), "-B", str(build), "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_CLIENT=FALSE", "-DBUILD_SERVER=TRUE",
        "-DBUILD_UNITTESTS=FALSE", "-DBUILD_DOCUMENTATION=FALSE", "-DRUN_IN_PLACE=TRUE",
        "-DENABLE_LTO=FALSE", "-DENABLE_SOUND=FALSE", "-DENABLE_CURL=FALSE",
        "-DENABLE_GETTEXT=FALSE", "-DENABLE_CURSES=FALSE", "-DENABLE_POSTGRESQL=FALSE",
        "-DENABLE_LEVELDB=FALSE", "-DENABLE_REDIS=FALSE", "-DENABLE_SPATIAL=FALSE",
        "-DENABLE_LUAJIT=FALSE", "-DENABLE_SYSTEM_GMP=FALSE", "-DENABLE_SYSTEM_JSONCPP=FALSE",
        f"-DCMAKE_MAKE_PROGRAM={tc / 'bin/ninja'}", f"-DCMAKE_C_COMPILER={tc / 'bin/cc'}",
        f"-DCMAKE_CXX_COMPILER={tc / 'bin/c++'}", f"-DCMAKE_AR={tc / 'bin/ar'}",
        f"-DCMAKE_RANLIB={tc / 'bin/ranlib'}", f"-DIRRLICHT_INCLUDE_DIR={root / _IRR / 'include'}",
        f"-DSQLITE3_INCLUDE_DIR={packages / 'sqlite3/include'}", f"-DSQLITE3_LIBRARY={packages / 'sqlite3/lib/libsqlite3.a'}",
        f"-DZLIB_INCLUDE_DIR={packages / 'zlib/include'}", f"-DZLIB_LIBRARY={packages / 'zlib/lib/libz.a'}",
        f"-DZSTD_INCLUDE_DIR={packages / 'zstd/include'}", f"-DZSTD_LIBRARY={packages / 'zstd/lib/libzstd.a'}",
        f"-DICONV_LIBRARY={packages / 'iconv/lib/libiconv.tbd'}", "-DCMAKE_CXX_STANDARD=17",
        "-DCMAKE_CXX_STANDARD_REQUIRED=TRUE", "-DCMAKE_CXX_EXTENSIONS=OFF",
        f"-DCMAKE_CXX_COMPILER_LAUNCHER={launcher}",
    )


def _environment(root: Path, variant: str) -> dict[str, str]:
    home = root / "private/runtime-home/luanti-13132-native-v1" / variant
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {"HOME": str(home), "LANG": "C", "LC_ALL": "C", "PATH": str(root / _TOOLCHAIN / "bin")}


def _write_step(root: Path, variant: str, stage: str, argv: tuple[str, ...], receipt: NativeProcessReceipt, materialization: StableArtifact, dependency: StableArtifact, authorization: AuthorizationRecord) -> StableArtifact:
    ref = _step_reference(variant, stage)
    payload = {
        "candidate": _CANDIDATE, "variant": variant, "stage": stage,
        "argv": list(argv), "outcome": receipt.outcome, "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(receipt.stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(receipt.stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated, "stderr_truncated": receipt.stderr_truncated,
        "single_threaded": stage != "build" or argv[-1] == "-j1",
        "paid_actions_authorized": False, "formal_trials_authorized": False,
    }
    existing = _try_load(root, ref)
    inputs = (materialization.reference, dependency.reference)
    if existing is None:
        write_stable_artifact(root, artifact_id=ref.artifact_id, artifact_kind=ref.artifact_kind, semantic_version=ref.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#13132 native step conflicts")
    return _load(root, ref)


def _step_reference(variant: str, stage: str) -> ArtifactReference:
    return ArtifactReference(
        f"candidate_gate8_13132_native_step/luanti/13132/{variant}/{stage}/v1",
        "candidate_gate8_13132_native_step",
        "1.0",
    )


def _load_success_step(
    root: Path,
    variant: str,
    stage: str,
    materialization: StableArtifact,
    dependency: StableArtifact,
) -> StableArtifact | None:
    artifact = _try_load(root, _step_reference(variant, stage))
    if artifact is None:
        return None
    if (
        artifact.inputs != (materialization.reference, dependency.reference)
        or artifact.payload.get("candidate") != _CANDIDATE
        or artifact.payload.get("variant") != variant
        or artifact.payload.get("stage") != stage
        or artifact.payload.get("outcome") != "completed"
        or artifact.payload.get("returncode") != 0
    ):
        raise RuntimeError("#13132 resumable native step is invalid")
    return artifact


def _write_blocker(root: Path, materialization: StableArtifact, dependency: StableArtifact, steps: list[StableArtifact], variant: str, stage: str, receipt: NativeProcessReceipt, authorization: AuthorizationRecord) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE, "state": "blocked", "outcome": "infrastructure_blocked",
        "terminal": False, "resumable": True, "failed_variant": variant, "failed_stage": stage,
        "process_outcome": receipt.outcome, "returncode": receipt.returncode,
        "does_not_invalidate_gate_1_7": True, "gate9_to_13": "pending_not_rejection",
        "paid_actions_authorized": False, "formal_trials_authorized": False,
    }
    inputs = (materialization.reference, dependency.reference, *(item.reference for item in steps))
    existing = _try_load(root, _BLOCKER)
    if existing is None:
        write_stable_artifact(root, artifact_id=_BLOCKER.artifact_id, artifact_kind=_BLOCKER.artifact_kind, semantic_version=_BLOCKER.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    return _load(root, _BLOCKER)


def _extract_commit(mirror: Path, revision: str, destination: Path) -> None:
    result = subprocess.run(("git", f"--git-dir={mirror}", "archive", "--format=tar", revision), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("#13132 archive contains unsafe path")
        archive.extractall(destination, filter="data")


def _tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.relative_to(root).parts[0] == "bin":
            continue
        if path.is_dir():
            continue
        rel = path.relative_to(root).as_posix().encode("utf-8")
        content = os.readlink(path).encode("utf-8") if path.is_symlink() else path.read_bytes()
        digest.update(rel + b"\0" + content + b"\0")
    return digest.hexdigest()


def _file_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_commit(mirror: Path, revision: str) -> None:
    result = subprocess.run(("git", f"--git-dir={mirror}", "cat-file", "-e", f"{revision}^{{commit}}"), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    if result.returncode != 0:
        raise RuntimeError("#13132 retained mirror is missing required commit")


def _ordinary_dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError("#13132 directory is unsafe")
    return resolved


def _ok(receipt: NativeProcessReceipt) -> bool:
    return receipt.outcome == "completed" and receipt.returncode == 0


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _require_auth(authorization: AuthorizationRecord, scopes: set[str]) -> None:
    if type(authorization) is not AuthorizationRecord or authorization.scope not in scopes or authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("#13132 native closure authorization is inactive")


def _now() -> str:
    return ((datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z"))


__all__ = ["CONFIRMATION", "build_luanti_13132_pair", "materialize_luanti_13132_pair"]
