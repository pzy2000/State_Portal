"""Bounded native server-unit execution for Luanti change 12602.

The executor is intentionally candidate-specific and offline.  It reloads the
gates-1--4 profile, materializes the fixed merged tree from the retained local
mirror, applies only the production part of the patch in reverse to make the
buggy tree, builds one server binary per variant, and runs the two registered
unit-test selectors three times per cell.  Every build and attempt is written
before the next cell is scheduled, so an interrupted run can resume from
typed receipts.

No provider, paid model, formal trial, or external repository is contacted.
"""

from __future__ import annotations

import base64
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

from .luanti_12602_profile import (
    LUANTI_12602_PROFILE,
    Luanti12602Preflight,
    prepare_candidate,
)
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("luanti", 12602)
_SEMANTIC_VERSION = "2.0"
_MIRROR = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_TOOLCHAIN = PurePosixPath("private/dependencies/luanti-native/3.0")
_RUN_ROOT = PurePosixPath("private/bounded-runs/luanti/12602/server-unit-v2")
_MAX_OUTPUT = 262_144
_CONFIGURE_TIMEOUT = 600
_BUILD_TIMEOUT = 1800
_TEST_TIMEOUT = 300
_CONFIRMATION = "EXECUTE_LUANTI_12602_SERVER_UNIT"


class BoundedExecutionError(RuntimeError):
    """Raised when a bounded build or test cannot produce a valid receipt."""


@dataclass(frozen=True)
class ProcessReceipt:
    outcome: Literal["completed", "timed_out", "launch_failed"]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def __post_init__(self) -> None:
        if self.outcome == "completed" and type(self.returncode) is not int:
            raise ValueError("completed process receipt lacks return code")
        if self.outcome != "completed" and self.returncode is not None:
            raise ValueError("incomplete process receipt has return code")
        if type(self.stdout) is not bytes or type(self.stderr) is not bytes:
            raise ValueError("process receipt streams are invalid")
        if len(self.stdout) > _MAX_OUTPUT or len(self.stderr) > _MAX_OUTPUT:
            raise ValueError("process receipt streams exceed bound")
        if self.stdout_truncated and len(self.stdout) != _MAX_OUTPUT:
            raise ValueError("truncated stdout does not fill bound")
        if self.stderr_truncated and len(self.stderr) != _MAX_OUTPUT:
            raise ValueError("truncated stderr does not fill bound")


@dataclass(frozen=True)
class Luanti12602ExecutionResult:
    candidate: CandidateKey
    aggregate: StableArtifact
    materialized: bool
    built_variants: int
    executed_attempts: int
    resumed_attempts: int
    qualified: bool


def _ref(artifact_id: str, artifact_kind: str) -> ArtifactReference:
    return ArtifactReference(artifact_id, artifact_kind, _SEMANTIC_VERSION)


def materialization_reference() -> ArtifactReference:
    return _ref(
        "candidate_materialization/luanti/12602/server-unit/v2",
        "candidate_materialization",
    )


def build_reference(variant: Literal["fixed", "buggy"]) -> ArtifactReference:
    if variant not in {"fixed", "buggy"}:
        raise ValueError("variant is invalid")
    return _ref(
        f"candidate_build/luanti/12602/{variant}/server-unit/v2",
        "candidate_build",
    )


def attempt_reference(
    phase: Literal["f2p", "p2p"],
    variant: Literal["fixed", "buggy"],
    repetition: int,
) -> ArtifactReference:
    if phase not in {"f2p", "p2p"} or variant not in {"fixed", "buggy"}:
        raise ValueError("attempt identity is invalid")
    if repetition not in {1, 2, 3}:
        raise ValueError("attempt repetition is invalid")
    return _ref(
        f"candidate_test_attempt/luanti/12602/{phase}/{variant}/{repetition}/v2",
        "candidate_test_attempt",
    )


def aggregate_reference() -> ArtifactReference:
    return _ref(
        "candidate_test_aggregate/luanti/12602/server-unit/v2",
        "candidate_test_aggregate",
    )


def _root(value: str | Path) -> Path:
    path = Path(value).absolute()
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BoundedExecutionError("artifact root is unsafe")
    return path.resolve(strict=True)


def _safe_relative(value: str | PurePosixPath) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or "." in path.parts
        or ".." in path.parts
        or path.as_posix() != str(value)
    ):
        raise BoundedExecutionError("relative path is unsafe")
    return path


def _ordinary_directory(path: Path, *, create: bool = False) -> Path:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BoundedExecutionError(f"directory is unsafe: {path}")
    return path


def _ordinary_file(path: Path) -> Path:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BoundedExecutionError(f"file is unsafe: {path}")
    return path


def _tool(root: Path, name: str) -> Path:
    path = root.joinpath(*_TOOLCHAIN.parts, "bin", name)
    return _ordinary_file(path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _created_after(*values: str) -> str:
    parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values]
    return max(parsed).astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _bounded(value: bytes) -> tuple[bytes, bool]:
    if len(value) <= _MAX_OUTPUT:
        return value, False
    return value[:_MAX_OUTPUT], True


def run_bounded_process(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> ProcessReceipt:
    """Run without a shell and cap each output stream at 256 KiB."""

    if not argv or timeout_seconds <= 0:
        raise ValueError("bounded process invocation is invalid")
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

    assert process.stdout is not None and process.stderr is not None
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


def _validate_archive_member(name: str, member: tarfile.TarInfo) -> None:
    pure = PurePosixPath(name.rstrip("/"))
    if (
        not pure.parts
        or pure.is_absolute()
        or "." in pure.parts
        or ".." in pure.parts
        or pure.as_posix() != name.rstrip("/")
        or not (member.isdir() or member.isfile() or member.issym())
    ):
        raise BoundedExecutionError("retained source archive path is unsafe")


def _extract_archive(root: Path, destination: Path) -> None:
    mirror = _ordinary_directory(root.joinpath(*_MIRROR.parts))
    git = _tool(root, "git")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists() or destination.is_symlink():
        raise BoundedExecutionError("materialization destination already exists")
    with tempfile.TemporaryDirectory(prefix=".luanti-12602-archive-", dir=destination.parent) as name:
        staging = Path(name) / "tree"
        staging.mkdir(mode=0o700)
        process = subprocess.Popen(
            (str(git), f"--git-dir={mirror}", "archive", "--format=tar", "refs/c1/pull/12602/head"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": str(git.parent), "LANG": "C", "LC_ALL": "C"},
        )
        assert process.stdout is not None and process.stderr is not None
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                members = []
                for member in archive:
                    name_value = member.name.rstrip("/")
                    _validate_archive_member(name_value, member)
                    members.append(member)
                    target = staging.joinpath(*PurePosixPath(name_value).parts)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    elif member.issym():
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        target.symlink_to(member.linkname)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        source = archive.extractfile(member)
                        if source is None:
                            raise BoundedExecutionError("source archive member unreadable")
                        with source, target.open("wb") as output:
                            shutil.copyfileobj(source, output, length=1024 * 1024)
                        target.chmod(0o700 if member.mode & 0o111 else 0o600)
            stderr = process.stderr.read()
            returncode = process.wait(timeout=30)
        except BaseException:
            process.kill()
            process.wait()
            raise
        if returncode != 0:
            raise BoundedExecutionError(
                "retained source archive failed: " + stderr.decode(errors="replace")[:200]
            )
        os.replace(staging, destination)


def _tree_safe(root: Path) -> None:
    _ordinary_directory(root)
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
            continue
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            if os.path.isabs(target):
                raise BoundedExecutionError("source tree contains an absolute link")
            try:
                (path.parent / target).resolve(strict=False).relative_to(root.resolve())
            except ValueError as error:
                raise BoundedExecutionError("source tree link escapes root") from error
            continue
        raise BoundedExecutionError("source tree contains an unsupported entry")


def _tree_inventory(root: Path) -> dict[str, tuple[str, int | str | None]]:
    inventory: dict[str, tuple[str, int | str | None]] = {}
    for path in root.rglob("*"):
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            inventory[relative] = ("directory", None)
        elif stat.S_ISREG(info.st_mode):
            inventory[relative] = ("file", info.st_size)
        elif stat.S_ISLNK(info.st_mode):
            inventory[relative] = ("symlink", os.readlink(path))
        else:
            raise BoundedExecutionError("source tree inventory is invalid")
    return inventory


def _write_and_reload(
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
        raise BoundedExecutionError("typed receipt reload mismatch")
    return artifact


def _load_if_valid(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _materialize_pair(
    root: Path,
    preflight: Luanti12602Preflight,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, Path, Path, bool]:
    reference = materialization_reference()
    base = root.joinpath(*_RUN_ROOT.parts)
    fixed = base / "fixed"
    buggy = base / "buggy"
    existing = _load_if_valid(root, reference)
    if existing is not None and fixed.is_dir() and buggy.is_dir():
        _tree_safe(fixed)
        _tree_safe(buggy)
        return existing, fixed, buggy, False
    if fixed.exists() or fixed.is_symlink() or buggy.exists() or buggy.is_symlink():
        raise BoundedExecutionError("partial 12602 materialization cannot be resumed")
    _extract_archive(root, fixed)
    try:
        _tree_safe(fixed)
        shutil.copytree(fixed, buggy, symlinks=True)
        _tree_safe(buggy)
        inverse = preflight.production_inverse_patch
        git = _tool(root, "git")
        env = {"PATH": str(git.parent), "LANG": "C", "LC_ALL": "C"}
        completed = subprocess.run(
            (str(git), "apply", "-R", "--whitespace=nowarn", "-"),
            cwd=buggy,
            input=inverse,
            capture_output=True,
            env=env,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            raise BoundedExecutionError(
                "12602 production inverse failed: "
                + completed.stderr.decode(errors="replace")[:300]
            )
        fixed_inventory = _tree_inventory(fixed)
        buggy_inventory = _tree_inventory(buggy)
        if set(fixed_inventory) != set(buggy_inventory):
            raise BoundedExecutionError("fixed/buggy source inventory differs")
        for path in fixed_inventory:
            if path in preflight.profile.production_paths:
                if fixed_inventory[path][0] != "file" or buggy_inventory[path][0] != "file":
                    raise BoundedExecutionError("production source type changed")
            elif fixed_inventory[path] != buggy_inventory[path]:
                raise BoundedExecutionError("non-production source changed")
        payload = {
            "candidate": _CANDIDATE.to_value(),
            "profile_id": preflight.profile.profile_id,
            "state": "materialized",
            "fixed_tree_path": fixed.relative_to(root).as_posix(),
            "buggy_tree_path": buggy.relative_to(root).as_posix(),
            "production_inverse_paths": list(preflight.profile.production_paths),
            "preserved_test_paths": list(preflight.profile.patch_test_paths),
            "binary_relative_path": "bin/luantiserver",
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
        }
        artifact = _write_and_reload(
            root,
            reference,
            (
                preflight.chain.source.reference,
                preflight.chain.patch.reference,
                preflight.chain.atomicity.reference,
                preflight.chain.manifest.reference,
            ),
            payload,
            authorization,
            created_at=_created_after(
                preflight.chain.source.created_at,
                preflight.chain.patch.created_at,
                preflight.chain.atomicity.created_at,
                preflight.chain.manifest.created_at,
                authorization.granted_at,
            ),
        )
        return artifact, fixed, buggy, True
    except BaseException:
        _remove_tree(fixed)
        _remove_tree(buggy)
        raise


def _remove_tree(path: Path) -> None:
    if not path.exists() or path.is_symlink():
        return
    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()


def _configure_argv(root: Path, tree: Path, build: Path) -> tuple[str, ...]:
    toolchain = root.joinpath(*_TOOLCHAIN.parts)
    cmake = _tool(root, "cmake")
    ninja = _tool(root, "ninja")
    cc = _tool(root, "cc")
    cxx = _tool(root, "c++")
    ar = _tool(root, "ar")
    ranlib = _tool(root, "ranlib")
    linker = _tool(root, "ld")
    nm = _tool(root, "nm")
    strip = _tool(root, "strip")
    otool = _tool(root, "otool")
    install_name = _tool(root, "install_name_tool")
    packages = {name: toolchain / "packages" / name for name in ("sqlite3", "iconv", "zlib", "zstd")}
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


def _build_variant(
    root: Path,
    materialization: StableArtifact,
    tree: Path,
    variant: Literal["fixed", "buggy"],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = build_reference(variant)
    existing = _load_if_valid(root, reference)
    binary = tree / "bin" / "luantiserver"
    if existing is not None and binary.is_file() and not binary.is_symlink():
        return existing
    build = root / "private" / "bounded-builds" / "luanti" / "12602" / variant
    _ordinary_directory(build.parent, create=True)
    build.mkdir(mode=0o700, exist_ok=True)
    env = {
        "HOME": str(root / "private" / "runtime-home" / "luanti-native"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(root.joinpath(*_TOOLCHAIN.parts, "bin")),
    }
    steps = (
        ("configure", _configure_argv(root, tree, build), _CONFIGURE_TIMEOUT),
        (
            "build",
            (
                str(_tool(root, "cmake")),
                "--build",
                str(build),
                "--target",
                "luantiserver",
                "--parallel",
                "1",
            ),
            _BUILD_TIMEOUT,
        ),
    )
    receipts: list[dict[str, object]] = []
    for step, argv, timeout in steps:
        receipt = run_bounded_process(tuple(argv), cwd=tree, env=env, timeout_seconds=timeout)
        stdout, stdout_truncated = _bounded(receipt.stdout)
        stderr, stderr_truncated = _bounded(receipt.stderr)
        receipts.append(
            {
                "step": step,
                "argv": list(argv),
                "outcome": receipt.outcome,
                "returncode": receipt.returncode,
                "stdout_base64": base64.b64encode(stdout).decode("ascii"),
                "stderr_base64": base64.b64encode(stderr).decode("ascii"),
                "stdout_truncated": receipt.stdout_truncated or stdout_truncated,
                "stderr_truncated": receipt.stderr_truncated or stderr_truncated,
            }
        )
        if receipt.outcome != "completed" or receipt.returncode != 0:
            payload = {
                "candidate": _CANDIDATE.to_value(),
                "profile_id": LUANTI_12602_PROFILE.profile_id,
                "variant": variant,
                "state": "terminal",
                "outcome": "infrastructure_blocked",
                "steps": receipts,
                "binary_path": binary.relative_to(root).as_posix(),
                "candidate_execution_performed": False,
                "paid_actions_authorized": False,
            }
            return _write_and_reload(
                root,
                reference,
                (materialization.reference,),
                payload,
                authorization,
            )
    if not binary.is_file() or binary.is_symlink():
        raise BoundedExecutionError(f"{variant} server binary is missing after build")
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": LUANTI_12602_PROFILE.profile_id,
        "variant": variant,
        "state": "built",
        "outcome": "completed",
        "steps": receipts,
        "binary_path": binary.relative_to(root).as_posix(),
        "build_directory": build.relative_to(root).as_posix(),
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    return _write_and_reload(root, reference, (materialization.reference,), payload, authorization)


def _summary_outcome(receipt: ProcessReceipt, *, phase: str, variant: str) -> str:
    if receipt.outcome == "timed_out":
        return "timed_out"
    if receipt.outcome == "launch_failed":
        return "launch_failed"
    if receipt.returncode == 0:
        return "pass"
    if phase == "f2p" and variant == "buggy":
        return "failure"
    return "failure"


def _run_attempt(
    root: Path,
    materialization: StableArtifact,
    build: StableArtifact,
    tree: Path,
    phase: Literal["f2p", "p2p"],
    variant: Literal["fixed", "buggy"],
    repetition: int,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, bool]:
    reference = attempt_reference(phase, variant, repetition)
    existing = _load_if_valid(root, reference)
    if existing is not None:
        return existing, True
    binary_relative = build.payload.get("binary_path")
    if type(binary_relative) is not str:
        raise BoundedExecutionError("build receipt binary path is invalid")
    binary = root / _safe_relative(binary_relative)
    _ordinary_file(binary)
    selector = (
        LUANTI_12602_PROFILE.f2p_selector
        if phase == "f2p"
        else LUANTI_12602_PROFILE.p2p_selector
    )
    argv = (str(binary), "--run-unittests", "--test-module", selector)
    env = {
        "HOME": str(root / "private" / "runtime-home" / "luanti-native"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(root.joinpath(*_TOOLCHAIN.parts, "bin")),
    }
    receipt = run_bounded_process(argv, cwd=tree, env=env, timeout_seconds=_TEST_TIMEOUT)
    observed = _summary_outcome(receipt, phase=phase, variant=variant)
    expected = "pass" if variant == "fixed" or phase == "p2p" else "failure"
    stdout, stdout_truncated = _bounded(receipt.stdout)
    stderr, stderr_truncated = _bounded(receipt.stderr)
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": LUANTI_12602_PROFILE.profile_id,
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
        "output_encoding": "base64",
        "matched_expected": observed == expected,
    }
    artifact = _write_and_reload(
        root,
        reference,
        (materialization.reference, build_reference(variant)),
        payload,
        authorization,
    )
    return artifact, False


def _aggregate(
    root: Path,
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 12:
        raise BoundedExecutionError("attempt coverage is incomplete")
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for artifact in attempts:
        key = (artifact.payload["phase"], artifact.payload["variant"])
        groups.setdefault(key, []).append(artifact)
    expected_groups = {("f2p", "fixed"), ("f2p", "buggy"), ("p2p", "fixed"), ("p2p", "buggy")}
    if set(groups) != expected_groups or any(len(group) != 3 for group in groups.values()):
        raise BoundedExecutionError("attempt cell coverage is incomplete")
    cells = []
    for phase, variant in sorted(expected_groups):
        group = sorted(groups[(phase, variant)], key=lambda item: item.payload["repetition"])
        cells.append(
            {
                "phase": phase,
                "variant": variant,
                "observed_outcomes": [item.payload["observed_outcome"] for item in group],
                "matched_expected": [item.payload["matched_expected"] for item in group],
            }
        )
    stable = all(len(set(cell["observed_outcomes"])) == 1 for cell in cells)
    all_expected = all(all(cell["matched_expected"]) for cell in cells)
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": LUANTI_12602_PROFILE.profile_id,
        "state": "qualified" if stable and all_expected else "terminal",
        "outcome": "qualified" if stable and all_expected else "infrastructure_blocked",
        "repetitions_per_cell": 3,
        "attempt_count": 12,
        "stable_repetitions": stable,
        "all_expected": all_expected,
        "qualified": stable and all_expected,
        "cells": cells,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
    }
    return _write_and_reload(
        root,
        aggregate_reference(),
        tuple(item.reference for item in attempts),
        payload,
        authorization,
    )


def execute_luanti_12602_server_unit(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> Luanti12602ExecutionResult:
    """Execute or resume the bounded 12-cell #12602 server-unit matrix."""

    if confirmation != _CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {_CONFIRMATION!r}")
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("Luanti 12602 execution authorization is inactive")
    root = _root(artifact_root)
    preflight = prepare_candidate(root)
    materialization, fixed, buggy, materialized = _materialize_pair(
        root, preflight, authorization
    )
    builds = {
        "fixed": _build_variant(root, materialization, fixed, "fixed", authorization),
        "buggy": _build_variant(root, materialization, buggy, "buggy", authorization),
    }
    if any(build.payload.get("state") != "built" for build in builds.values()):
        raise BoundedExecutionError("candidate build did not complete")
    attempts: list[StableArtifact] = []
    executed = resumed = 0
    for phase in ("f2p", "p2p"):
        for variant, tree in (("fixed", fixed), ("buggy", buggy)):
            for repetition in (1, 2, 3):
                artifact, was_resumed = _run_attempt(
                    root,
                    materialization,
                    builds[variant],
                    tree,
                    phase,
                    variant,
                    repetition,
                    authorization,
                )
                attempts.append(artifact)
                if was_resumed:
                    resumed += 1
                else:
                    executed += 1
    aggregate = _aggregate(root, attempts, authorization)
    return Luanti12602ExecutionResult(
        candidate=_CANDIDATE,
        aggregate=aggregate,
        materialized=materialized,
        built_variants=2,
        executed_attempts=executed,
        resumed_attempts=resumed,
        qualified=aggregate.payload["qualified"] is True,
    )


__all__ = [
    "BoundedExecutionError",
    "Luanti12602ExecutionResult",
    "ProcessReceipt",
    "_CONFIRMATION",
    "aggregate_reference",
    "attempt_reference",
    "build_reference",
    "execute_luanti_12602_server_unit",
    "materialization_reference",
    "run_bounded_process",
]
