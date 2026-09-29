"""Bounded local gate-5--7 executor for Luanti PR 14586.

This module is deliberately closed over the already accepted v2 source,
patch, atomicity and test-manifest chain.  It materializes the merged PR tree,
derives the buggy tree by applying only the production diff in reverse, builds
the two server binaries with the retained native toolchain, then executes the
registered F2P/P2P matrix three times per cell.  Every step and attempt is a
typed stable artifact and an interruption resumes only from a validated
receipt.  No provider, network, paid operation, visual trial, or downstream
oracle is reachable from this executor.
"""

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
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from .luanti_fixed_source_legacy_migration_reader import read_legacy_merged_revision
from .luanti_server_unit_plan import _load_candidate_chain
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("luanti", 14586)
_ROOT_MIRROR = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_TOOLCHAIN = PurePosixPath("private/dependencies/luanti-native/3.0")
_CAPSULE = PurePosixPath("private/fixed-capsules/luanti/14586/fixed-source")
_RUN_ROOT = PurePosixPath("private/bounded-runs/luanti/14586/server-unit/v1")
_BUILD_ROOT = PurePosixPath("private/bounded-builds/luanti/14586/server-unit/v1")
_MAX_OUTPUT = 262_144
_CONFIGURE_TIMEOUT = 600
_BUILD_TIMEOUT = 1_800
_TEST_TIMEOUT = 300
_CONFIRMATION = "RUN_LUANTI_14586_GATE57_V1"
_SEMANTIC_VERSION = "1.0"


def _ref(artifact_id: str, artifact_kind: str) -> ArtifactReference:
    return ArtifactReference(artifact_id, artifact_kind, _SEMANTIC_VERSION)


def materialization_reference() -> ArtifactReference:
    return _ref(
        "candidate_materialization/luanti/14586/server-unit/v1",
        "candidate_materialization",
    )


def build_reference(
    variant: Literal["fixed", "buggy"], step: Literal["configure", "build"]
) -> ArtifactReference:
    return _ref(
        f"candidate_build/luanti/14586/{variant}/{step}/server-unit/v1",
        "candidate_build",
    )


def attempt_reference(
    phase: Literal["f2p", "p2p"], variant: Literal["fixed", "buggy"], repetition: int
) -> ArtifactReference:
    if repetition not in {1, 2, 3}:
        raise ValueError("repetition is invalid")
    return _ref(
        f"candidate_test_attempt/luanti/14586/{phase}/{variant}/{repetition}/server-unit/v1",
        "candidate_test_attempt",
    )


def aggregate_reference() -> ArtifactReference:
    return _ref(
        "candidate_test_aggregate/luanti/14586/server-unit/v1",
        "candidate_test_aggregate",
    )


@dataclass(frozen=True)
class ProcessReceipt:
    outcome: Literal["completed", "timed_out", "launch_failed"]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass(frozen=True)
class Luanti14586ExecutionResult:
    aggregate: StableArtifact
    materialized: bool
    executed_attempts: int
    resumed_attempts: int
    qualified: bool


def _root(value: str | Path) -> Path:
    path = Path(value).absolute()
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return path.resolve(strict=True)


def _safe_relative(value: str | PurePosixPath) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or "." in path.parts or ".." in path.parts:
        raise RuntimeError("relative path is unsafe")
    return path


def _ordinary_directory(path: Path, *, create: bool = False) -> Path:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"directory is unsafe: {path}")
    return path


def _ordinary_file(path: Path) -> Path:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"file is unsafe: {path}")
    return path


def _tool(root: Path, name: str) -> Path:
    return _ordinary_file(root.joinpath(*_TOOLCHAIN.parts, "bin", name))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _created_after(*values: str) -> str:
    parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values]
    return (
        max(parsed)
        .astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _bounded(value: bytes) -> tuple[bytes, bool]:
    return (value, False) if len(value) <= _MAX_OUTPUT else (value[:_MAX_OUTPUT], True)


def run_bounded_process(
    argv: tuple[str, ...], *, cwd: Path, env: dict[str, str], timeout_seconds: int
) -> ProcessReceipt:
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        return ProcessReceipt("launch_failed", None, b"", str(error).encode())
    assert process.stdout is not None and process.stderr is not None
    outputs = [bytearray(), bytearray()]
    truncated = [False, False]

    def drain(stream: object, destination: bytearray, index: int) -> None:
        assert hasattr(stream, "read")
        while chunk := stream.read(65_536):
            remaining = _MAX_OUTPUT - len(destination)
            if remaining > 0:
                destination.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated[index] = True

    threads = (
        threading.Thread(target=drain, args=(process.stdout, outputs[0], 0)),
        threading.Thread(target=drain, args=(process.stderr, outputs[1], 1)),
    )
    for thread in threads:
        thread.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
        outcome: Literal["completed", "timed_out"] = "completed"
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            pass
        process.wait()
        returncode = None
        outcome = "timed_out"
    for thread in threads:
        thread.join()
    process.stdout.close()
    process.stderr.close()
    stdout, stdout_truncated = _bounded(bytes(outputs[0]))
    stderr, stderr_truncated = _bounded(bytes(outputs[1]))
    return ProcessReceipt(
        outcome,
        returncode,
        stdout,
        stderr,
        truncated[0] or stdout_truncated,
        truncated[1] or stderr_truncated,
    )


def _load_if_valid(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _write(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
    *,
    created_at: str | None = None,
) -> StableArtifact:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=created_at or _now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("typed receipt reload mismatch")
    return artifact


def _archive_bytes(root: Path, revision: str) -> bytes:
    mirror = _ordinary_directory(root.joinpath(*_ROOT_MIRROR.parts))
    git = _tool(root, "git")
    completed = subprocess.run(
        (str(git), f"--git-dir={mirror}", "archive", "--format=tar", revision),
        env={"PATH": str(git.parent), "LANG": "C", "LC_ALL": "C"},
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("retained merged source archive failed")
    return completed.stdout


def _validate_member(name: str, member: tarfile.TarInfo) -> None:
    pure = PurePosixPath(name.rstrip("/"))
    if (
        not pure.parts
        or pure.is_absolute()
        or "." in pure.parts
        or ".." in pure.parts
        or pure.as_posix() != name.rstrip("/")
        or not (member.isdir() or member.isfile() or member.issym())
    ):
        raise RuntimeError("source archive path is unsafe")


def _extract_archive(root: Path, destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("fixed source capsule destination already exists")
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
        members = tuple(archive)
        if not members:
            raise RuntimeError("merged source archive is empty")
        by_name: dict[str, tarfile.TarInfo] = {}
        for member in members:
            name = member.name.rstrip("/")
            _validate_member(name, member)
            if name in by_name:
                raise RuntimeError("source archive has duplicate entries")
            by_name[name] = member
        for name, member in by_name.items():
            if member.issym():
                target = member.linkname
                resolved = PurePosixPath(
                    os.path.normpath(os.path.join(os.path.dirname(name), target))
                )
                if (
                    not target
                    or PurePosixPath(target).is_absolute()
                    or resolved.as_posix() not in by_name
                ):
                    raise RuntimeError("source archive link is unsafe")
        with tempfile.TemporaryDirectory(
            prefix=".luanti-14586-capsule-", dir=destination.parent
        ) as temporary:
            staging = Path(temporary) / "tree"
            staging.mkdir(mode=0o700)
            for name, member in sorted(
                by_name.items(),
                key=lambda item: (len(PurePosixPath(item[0]).parts), item[0]),
            ):
                target = staging.joinpath(*PurePosixPath(name).parts)
                if member.isdir():
                    target.mkdir(mode=0o700)
                elif member.issym():
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target.symlink_to(member.linkname)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    source = archive.extractfile(member)
                    if source is None:
                        raise RuntimeError("source archive file is unreadable")
                    with source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1 << 20)
                    target.chmod(0o700 if member.mode & 0o111 else 0o600)
            os.replace(staging, destination)


def _tree_safe(root: Path) -> None:
    _ordinary_directory(root)
    resolved_root = root.resolve()
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
            continue
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            try:
                (path.parent / target).resolve(strict=False).relative_to(resolved_root)
            except ValueError as error:
                raise RuntimeError("source tree link escapes root") from error
            continue
        raise RuntimeError("source tree contains an unsupported entry")


def _tree_inventory(root: Path) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for path in root.rglob("*"):
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            result[relative] = ("directory", "")
        elif stat.S_ISREG(info.st_mode):
            result[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        elif stat.S_ISLNK(info.st_mode):
            result[relative] = ("symlink", os.readlink(path))
        else:
            raise RuntimeError("source tree inventory is invalid")
    return result


def _remove_tree(path: Path) -> None:
    if not path.exists() or path.is_symlink():
        return
    for child in sorted(
        path.rglob("*"), key=lambda item: len(item.parts), reverse=True
    ):
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()


def _materialize(
    root: Path,
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
    sections: tuple[object, ...],
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, Path, Path, bool]:
    reference = materialization_reference()
    base = root.joinpath(*_RUN_ROOT.parts)
    capsule = root.joinpath(*_CAPSULE.parts)
    fixed = base / "fixed"
    buggy = base / "buggy"
    existing = _load_if_valid(root, reference)
    if existing is not None and capsule.is_dir() and fixed.is_dir() and buggy.is_dir():
        _tree_safe(capsule)
        _tree_safe(fixed)
        _tree_safe(buggy)
        return existing, fixed, buggy, False
    if existing is not None:
        raise RuntimeError("14586 materialization receipt/tree state is inconsistent")
    if fixed.exists() or fixed.is_symlink() or buggy.exists() or buggy.is_symlink():
        raise RuntimeError("partial 14586 materialization cannot be resumed")
    revision = read_legacy_merged_revision(
        root,
        raw_manifest_path=str(source.payload["raw_evidence_path"]),
        candidate=_CANDIDATE,
    )
    capsule_created = False
    try:
        if capsule.exists() or capsule.is_symlink():
            _tree_safe(capsule)
        else:
            _extract_archive(root, capsule, _archive_bytes(root, revision))
            capsule_created = True
        _tree_safe(capsule)
        fixed.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copytree(capsule, fixed, symlinks=True)
        _tree_safe(fixed)
        buggy.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copytree(fixed, buggy, symlinks=True)
        _tree_safe(buggy)
        production = [
            section
            for section in sections
            if getattr(section, "path") == "src/inventory.cpp"
        ]
        if len(production) != 1:
            raise RuntimeError("production patch section is not unique")
        git = _tool(root, "git")
        completed = subprocess.run(
            (str(git), "apply", "-R", "--whitespace=nowarn", "-"),
            cwd=buggy,
            input=getattr(production[0], "content"),
            capture_output=True,
            timeout=60,
            env={
                "PATH": str(git.parent),
                "GIT_DIR": "/dev/null",
                "LANG": "C",
                "LC_ALL": "C",
            },
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("production inverse patch failed")
        fixed_inventory = _tree_inventory(fixed)
        buggy_inventory = _tree_inventory(buggy)
        if set(fixed_inventory) != set(buggy_inventory):
            raise RuntimeError("fixed/buggy source inventory differs")
        changed = [
            path
            for path in fixed_inventory
            if fixed_inventory[path] != buggy_inventory[path]
        ]
        if changed != ["src/inventory.cpp"]:
            raise RuntimeError(f"strict inverse changed unexpected paths: {changed}")
        test_path = "src/unittest/test_moveaction.cpp"
        if fixed_inventory[test_path] != buggy_inventory[test_path]:
            raise RuntimeError("regression test is not preserved in buggy tree")
        payload = {
            "candidate": _CANDIDATE.to_value(),
            "state": "materialized",
            "fixed_capsule_path": capsule.relative_to(root).as_posix(),
            "fixed_tree_path": fixed.relative_to(root).as_posix(),
            "buggy_tree_path": buggy.relative_to(root).as_posix(),
            "production_inverse_paths": ["src/inventory.cpp"],
            "preserved_test_paths": [test_path],
            "strict_inverse_changed_paths": changed,
            "binary_relative_path": "bin/minetestserver",
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
        }
        artifact = _write(
            root,
            reference,
            (
                source.reference,
                patch.reference,
                atomicity.reference,
                manifest.reference,
            ),
            payload,
            authorization,
            created_at=_created_after(
                source.created_at,
                patch.created_at,
                atomicity.created_at,
                manifest.created_at,
                authorization.granted_at,
            ),
        )
        return artifact, fixed, buggy, True
    except BaseException:
        _remove_tree(fixed)
        _remove_tree(buggy)
        if capsule_created:
            _remove_tree(capsule)
        raise


def _configure_argv(root: Path, tree: Path, build: Path) -> tuple[str, ...]:
    toolchain = root.joinpath(*_TOOLCHAIN.parts)
    cmake, ninja = _tool(root, "cmake"), _tool(root, "ninja")
    cc, cxx = _tool(root, "cc"), _tool(root, "c++")
    ar, ranlib = _tool(root, "ar"), _tool(root, "ranlib")
    linker, nm = _tool(root, "ld"), _tool(root, "nm")
    strip, otool = _tool(root, "strip"), _tool(root, "otool")
    install_name = _tool(root, "install_name_tool")
    packages = {
        name: toolchain / "packages" / name
        for name in ("sqlite3", "iconv", "zlib", "zstd")
    }
    return (
        str(cmake),
        "-S",
        str(tree),
        "-B",
        str(build),
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_CLIENT=FALSE",
        "-DBUILD_SERVER=TRUE",
        "-DBUILD_UNITTESTS=TRUE",
        "-DBUILD_BENCHMARKS=FALSE",
        "-DBUILD_DOCUMENTATION=FALSE",
        "-DRUN_IN_PLACE=TRUE",
        "-DENABLE_LTO=FALSE",
        "-DENABLE_CURL=FALSE",
        "-DENABLE_GETTEXT=FALSE",
        "-DENABLE_POSTGRESQL=FALSE",
        "-DENABLE_LEVELDB=FALSE",
        "-DENABLE_REDIS=FALSE",
        "-DENABLE_SPATIAL=FALSE",
        "-DENABLE_OPENSSL=FALSE",
        "-DENABLE_CURSES=FALSE",
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
        f"-DCMAKE_INSTALL_NAME_TOOL={install_name}",
        f"-DSQLITE3_INCLUDE_DIR={packages['sqlite3'] / 'include'}",
        f"-DSQLITE3_LIBRARY={packages['sqlite3'] / 'lib/libsqlite3.a'}",
        f"-DICONV_LIBRARY={packages['iconv'] / 'lib/libiconv.tbd'}",
        f"-DZLIB_INCLUDE_DIR={packages['zlib'] / 'include'}",
        f"-DZLIB_LIBRARY={packages['zlib'] / 'lib/libz.a'}",
        f"-DZSTD_INCLUDE_DIR={packages['zstd'] / 'include'}",
        f"-DZSTD_LIBRARY={packages['zstd'] / 'lib/libzstd.a'}",
    )


def _environment(root: Path) -> dict[str, str]:
    return {
        "HOME": str(root / "private" / "runtime-home" / "luanti-native"),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_DIR": "/dev/null",
        "PATH": str(root.joinpath(*_TOOLCHAIN.parts, "bin")),
    }


def _persist_build(
    root: Path,
    materialization: StableArtifact,
    variant: Literal["fixed", "buggy"],
    step: Literal["configure", "build"],
    argv: tuple[str, ...],
    receipt: ProcessReceipt,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    stdout, stdout_truncated = _bounded(receipt.stdout)
    stderr, stderr_truncated = _bounded(receipt.stderr)
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "variant": variant,
        "step": step,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "argv": list(argv),
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated or stdout_truncated,
        "stderr_truncated": receipt.stderr_truncated or stderr_truncated,
    }
    return _write(
        root,
        build_reference(variant, step),
        (materialization.reference,),
        payload,
        authorization,
    )


def _build_variant(
    root: Path,
    materialization: StableArtifact,
    tree: Path,
    variant: Literal["fixed", "buggy"],
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, bool]:
    build_dir = root.joinpath(*_BUILD_ROOT.parts, variant)
    build_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    build_dir.mkdir(mode=0o700, exist_ok=True)
    existing_build = _load_if_valid(root, build_reference(variant, "build"))
    binary = tree / "bin" / "minetestserver"
    if (
        existing_build is not None
        and existing_build.payload.get("process_outcome") == "completed"
        and existing_build.payload.get("returncode") == 0
        and binary.is_file()
        and not binary.is_symlink()
    ):
        return existing_build, True
    env = _environment(root)
    configure = _load_if_valid(root, build_reference(variant, "configure"))
    if configure is None:
        receipt = run_bounded_process(
            _configure_argv(root, tree, build_dir),
            cwd=tree,
            env=env,
            timeout_seconds=_CONFIGURE_TIMEOUT,
        )
        configure = _persist_build(
            root,
            materialization,
            variant,
            "configure",
            _configure_argv(root, tree, build_dir),
            receipt,
            authorization,
        )
    if (
        configure.payload.get("process_outcome") != "completed"
        or configure.payload.get("returncode") != 0
    ):
        return configure, False
    build = _load_if_valid(root, build_reference(variant, "build"))
    if (
        build is None
        or build.payload.get("process_outcome") != "completed"
        or build.payload.get("returncode") != 0
    ):
        argv = (
            str(_tool(root, "cmake")),
            "--build",
            str(build_dir),
            "--target",
            "minetestserver",
            "--parallel",
            "1",
        )
        receipt = run_bounded_process(
            argv, cwd=tree, env=env, timeout_seconds=_BUILD_TIMEOUT
        )
        build = _persist_build(
            root, materialization, variant, "build", argv, receipt, authorization
        )
    if (
        build.payload.get("process_outcome") == "completed"
        and build.payload.get("returncode") == 0
        and binary.is_file()
        and not binary.is_symlink()
    ):
        return build, False
    return build, False


def _persist_attempt(
    root: Path,
    materialization: StableArtifact,
    build: StableArtifact,
    tree: Path,
    phase: Literal["f2p", "p2p"],
    variant: Literal["fixed", "buggy"],
    repetition: int,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    selector = "TestMoveAction" if phase == "f2p" else "TestSettings"
    expected = "pass" if variant == "fixed" or phase == "p2p" else "failure"
    binary = tree / "bin" / "minetestserver"
    _ordinary_file(binary)
    argv = (str(binary), "--run-unittests", "--test-module", selector)
    receipt = run_bounded_process(
        argv, cwd=tree, env=_environment(root), timeout_seconds=_TEST_TIMEOUT
    )
    if receipt.outcome == "completed":
        observed = "pass" if receipt.returncode == 0 else "failure"
    elif receipt.outcome == "timed_out":
        observed = "timed_out"
    else:
        observed = "unknown"
    stdout, stdout_truncated = _bounded(receipt.stdout)
    stderr, stderr_truncated = _bounded(receipt.stderr)
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "phase": phase,
        "variant": variant,
        "repetition": repetition,
        "selector": selector,
        "expected_outcome": expected,
        "observed_outcome": observed,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "argv": list(argv),
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated or stdout_truncated,
        "stderr_truncated": receipt.stderr_truncated or stderr_truncated,
        "matched_expected": observed == expected,
    }
    return _write(
        root,
        attempt_reference(phase, variant, repetition),
        (materialization.reference, build.reference),
        payload,
        authorization,
    )


def _aggregate(
    root: Path,
    materialization: StableArtifact,
    builds: dict[str, StableArtifact],
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
    *,
    blocker: str | None = None,
) -> StableArtifact:
    cells: list[dict[str, object]] = []
    for phase in ("f2p", "p2p"):
        for variant in ("fixed", "buggy"):
            group = sorted(
                (
                    a
                    for a in attempts
                    if a.payload.get("phase") == phase
                    and a.payload.get("variant") == variant
                ),
                key=lambda a: int(a.payload.get("repetition", 0)),
            )
            if group:
                cells.append(
                    {
                        "phase": phase,
                        "variant": variant,
                        "repetitions": len(group),
                        "observed_outcomes": [
                            a.payload.get("observed_outcome") for a in group
                        ],
                        "matched_expected": [
                            a.payload.get("matched_expected") for a in group
                        ],
                    }
                )
    complete = len(attempts) == 12 and all(
        len(
            [
                a
                for a in attempts
                if a.payload.get("phase") == phase
                and a.payload.get("variant") == variant
            ]
        )
        == 3
        for phase in ("f2p", "p2p")
        for variant in ("fixed", "buggy")
    )
    stable = complete and all(
        len(set(cell["observed_outcomes"])) == 1 for cell in cells
    )
    all_expected = complete and all(all(cell["matched_expected"]) for cell in cells)
    if blocker:
        outcome = "infrastructure_blocked"
    elif complete and stable and all_expected:
        outcome = "qualified"
    elif complete:
        outcome = "candidate_semantic_mismatch"
    else:
        outcome = "incomplete_matrix"
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "state": "qualified" if outcome == "qualified" else "terminal",
        "outcome": outcome,
        "blocker": blocker,
        "expected_attempt_count": 12,
        "attempt_count": len(attempts),
        "complete": complete,
        "stable_repetitions": stable,
        "all_expected": all_expected,
        "qualified": outcome == "qualified",
        "cells": cells,
        "candidate_execution_performed": bool(attempts),
        "paid_actions_authorized": False,
    }
    inputs = [materialization.reference]
    inputs.extend(builds[v].reference for v in ("fixed", "buggy") if v in builds)
    inputs.extend(a.reference for a in attempts)
    return _write(root, aggregate_reference(), tuple(inputs), payload, authorization)


def execute_luanti_14586_server_unit(
    artifact_root: str | Path, *, authorization: AuthorizationRecord, confirmation: str
) -> Luanti14586ExecutionResult:
    if confirmation != _CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {_CONFIRMATION!r}")
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("Luanti 14586 execution authorization is inactive")
    root = _root(artifact_root)
    source, patch, atomicity, manifest, sections = _load_candidate_chain(root)
    materialization, fixed, buggy, was_materialized = _materialize(
        root, source, patch, atomicity, manifest, sections, authorization
    )
    builds: dict[str, StableArtifact] = {}
    for variant, tree in (("fixed", fixed), ("buggy", buggy)):
        build, _ = _build_variant(root, materialization, tree, variant, authorization)  # type: ignore[arg-type]
        builds[variant] = build
        if (
            build.payload.get("process_outcome") != "completed"
            or build.payload.get("returncode") != 0
        ):
            aggregate = _aggregate(
                root,
                materialization,
                builds,
                [],
                authorization,
                blocker=f"{variant}_build_failed_or_unknown",
            )
            return Luanti14586ExecutionResult(aggregate, was_materialized, 0, 0, False)
    attempts: list[StableArtifact] = []
    executed = resumed = 0
    for phase in ("f2p", "p2p"):
        for variant, tree in (("fixed", fixed), ("buggy", buggy)):
            for repetition in (1, 2, 3):
                reference = attempt_reference(phase, variant, repetition)
                existing = _load_if_valid(root, reference)
                if existing is not None:
                    attempt = existing
                    resumed += 1
                else:
                    attempt = _persist_attempt(
                        root,
                        materialization,
                        builds[variant],
                        tree,
                        phase,
                        variant,
                        repetition,
                        authorization,
                    )  # type: ignore[arg-type]
                    executed += 1
                attempts.append(attempt)
                if attempt.payload.get("process_outcome") != "completed":
                    aggregate = _aggregate(
                        root,
                        materialization,
                        builds,
                        attempts,
                        authorization,
                        blocker=f"{phase}_{variant}_{repetition}_{attempt.payload.get('observed_outcome')}",
                    )
                    return Luanti14586ExecutionResult(
                        aggregate, was_materialized, executed, resumed, False
                    )
    aggregate = _aggregate(root, materialization, builds, attempts, authorization)
    return Luanti14586ExecutionResult(
        aggregate,
        was_materialized,
        executed,
        resumed,
        bool(aggregate.payload.get("qualified")),
    )


__all__ = [
    "Luanti14586ExecutionResult",
    "ProcessReceipt",
    "_CONFIRMATION",
    "aggregate_reference",
    "attempt_reference",
    "build_reference",
    "execute_luanti_14586_server_unit",
    "materialization_reference",
    "run_bounded_process",
]
