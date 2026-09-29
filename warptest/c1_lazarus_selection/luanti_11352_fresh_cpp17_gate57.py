"""Fresh, candidate-specific Gate 5--7 execution for Luanti #11352.

This runner deliberately uses a namespace which is independent of the older
native-regression receipts.  It reloads the complete current-v2 Gate 1--4
chain, materializes a new fixed/buggy pair, and builds only the candidate
sources with an explicit C++17 configure setting.  The production and test
trees are never edited.  A missing dependency, an unexpected unit result, or
an unbounded process is persisted as a resumable nonterminal blocker.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal

from .luanti_native_executor import (
    NativeProcessReceipt,
    completed_native_test_count,
    run_bounded_process,
)
from .luanti_native_regression import (
    NativeRegressionExecutionPlan,
    NativeRegressionRun,
    plan_luanti_native_regression_execution,
)
from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("luanti", 11352)
_SEMANTIC_VERSION = "1.0"
_CONFIRMATION = "EXECUTE_LUANTI_11352_FRESH_CPP17_GATE57"
_MAX_OUTPUT = 262_144
_CONFIGURE_TIMEOUT = 900
_BUILD_TIMEOUT = 1_800
_TEST_TIMEOUT = 300
_CAPSULE_REL = PurePosixPath("private/fixed-capsules/luanti/11352/fixed-source")
_IRRLICHT_INCLUDE = PurePosixPath(
    "private/dependencies/luanti-12602-irrlicht/1.9.0.15/include"
)
_RUN_ROOT = PurePosixPath("private/fresh-gate57/luanti/11352/cpp17-v6")
_BUILD_ROOT = PurePosixPath("private/fresh-gate57-builds/luanti/11352/cpp17-v6")
_TOOLS_ROOT = PurePosixPath("private/fresh-gate57-tools/luanti/11352/cpp17-v6")
_EPOCH = "cpp17-v6"
_TOOL_REFERENCE = ArtifactReference(
    "candidate_gate57_fresh_toolchain/luanti/11352/cpp17/v6",
    "candidate_gate57_fresh_toolchain",
    _SEMANTIC_VERSION,
)


class FreshGate57Blocker(RuntimeError):
    """A typed, resumable Gate 5--7 blocker was written."""


@dataclass(frozen=True)
class FreshGate57Result:
    aggregate: StableArtifact | None
    blocker: StableArtifact | None
    executed_attempts: int
    qualified: bool


def _ref(artifact_id: str, artifact_kind: str) -> ArtifactReference:
    return ArtifactReference(artifact_id, artifact_kind, _SEMANTIC_VERSION)


def materialization_reference() -> ArtifactReference:
    return _ref(
        "candidate_gate57_fresh_materialization/luanti/11352/cpp17/v6",
        "candidate_gate57_fresh_materialization",
    )


def build_reference(variant: Literal["fixed", "buggy"], step: Literal["configure", "build"]) -> ArtifactReference:
    if variant not in {"fixed", "buggy"} or step not in {"configure", "build"}:
        raise ValueError("fresh build identity is invalid")
    return _ref(
        f"candidate_gate57_fresh_build/luanti/11352/cpp17/{variant}/{step}/v6",
        "candidate_gate57_fresh_build",
    )


def attempt_reference(
    phase: Literal["f2p", "p2p"],
    variant: Literal["fixed", "buggy"],
    repetition: int,
) -> ArtifactReference:
    if phase not in {"f2p", "p2p"} or variant not in {"fixed", "buggy"}:
        raise ValueError("fresh attempt identity is invalid")
    if repetition not in {1, 2, 3}:
        raise ValueError("fresh attempt repetition is invalid")
    return _ref(
        f"candidate_gate57_fresh_attempt/luanti/11352/cpp17/"
        f"{phase}/{variant}/{repetition}/v6",
        "candidate_gate57_fresh_attempt",
    )


def aggregate_reference() -> ArtifactReference:
    return _ref(
        "candidate_gate57_fresh_aggregate/luanti/11352/cpp17/v6",
        "candidate_gate57_fresh_aggregate",
    )


def blocker_reference() -> ArtifactReference:
    return _ref(
        "candidate_gate57_fresh_blocker/luanti/11352/cpp17/v6",
        "candidate_gate57_fresh_blocker",
    )


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _root(value: str | Path) -> Path:
    path = Path(value).absolute()
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("fresh Gate 5--7 artifact root is unsafe")
    return path.resolve(strict=True)


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


def _write(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
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
        raise RuntimeError("fresh Gate 5--7 typed reload mismatch")
    return artifact


_CXX_LAUNCHER = PurePosixPath("cxx17-launcher")


def _register_cxx_launcher(
    root: Path,
    chain: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Register a candidate-local launcher that makes C++17 the final flag."""

    namespace = root / _TOOLS_ROOT
    if namespace.exists() or namespace.is_symlink():
        raise FreshGate57Blocker("fresh C++17 launcher namespace is not empty")
    namespace.mkdir(parents=True, mode=0o700)
    launcher = namespace / _CXX_LAUNCHER
    content = (
        b"#!/bin/sh\n"
        b"set -eu\n"
        b"compiler=\"$1\"\n"
        b"shift\n"
        b"exec \"$compiler\" \"$@\" -std=c++17 -DTextureLayer=TextureLayers\n"
    )
    launcher.write_bytes(content)
    launcher.chmod(0o700)
    info = launcher.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise FreshGate57Blocker("registered C++17 launcher is not a regular file")
    if not os.access(launcher, os.X_OK):
        raise FreshGate57Blocker("registered C++17 launcher is not executable")
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "fresh_epoch": _EPOCH,
        "tool_kind": "candidate_cxx17_launcher",
        "wrapper_path": (_TOOLS_ROOT / _CXX_LAUNCHER).as_posix(),
        "content_fingerprint": hashlib.sha256(content).hexdigest(),
        "language_standard": "c++17",
        "compatibility_defines": ["TextureLayer=TextureLayers"],
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(
        root,
        _TOOL_REFERENCE,
        tuple(item.reference for item in chain),
        payload,
        authorization,
    )


def _chain(root: Path) -> tuple[NativeRegressionExecutionPlan, tuple[StableArtifact, ...]]:
    refs = (
        ArtifactReference(
            "candidate_source_packet/luanti/11352/source/v2",
            "candidate_source_packet",
            "2.0",
        ),
        ArtifactReference(
            "candidate_test_patch/luanti/11352/screening/v2",
            "candidate_test_patch",
            "2.0",
        ),
        ArtifactReference(
            "source_atomicity/luanti/11352/preannotation/v2",
            "source_atomicity",
            "2.0",
        ),
        ArtifactReference(
            "candidate_stage/luanti/11352/test_manifest/v2",
            "candidate_stage",
            "2.0",
        ),
        ArtifactReference(
            "fixed_source_capsule/luanti/11352/fixed/v2",
            "fixed_source_capsule",
            "2.0",
        ),
    )
    chain = tuple(_load(root, reference) for reference in refs)
    source, patch, atomicity, manifest, capsule = chain
    for artifact in chain:
        if artifact.payload.get("candidate") != _CANDIDATE.to_value():
            raise FreshGate57Blocker("current-v2 chain candidate binding failed")
    if source.payload.get("complete") is not True or source.inputs != (
        ArtifactReference("repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"),
    ):
        raise FreshGate57Blocker("current-v2 source closure is incomplete")
    if patch.payload.get("complete") is not True or patch.payload.get("outcome") != "patch_eligible":
        raise FreshGate57Blocker("current-v2 patch is not eligible")
    enumeration = ArtifactReference(
        "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
    )
    if patch.inputs != (enumeration, source.reference):
        raise FreshGate57Blocker("current-v2 patch lineage is invalid")
    if atomicity.payload.get("complete") is not True or atomicity.payload.get("outcome") != "accepted":
        raise FreshGate57Blocker("current-v2 source atomicity is not accepted")
    if (
        atomicity.payload.get("authoritative_issue_count") != 1
        or not atomicity.payload.get("authoritative_issue_numbers")
        or atomicity.payload.get("primary_issue_number")
        not in atomicity.payload.get("authoritative_issue_numbers", [])
    ):
        raise FreshGate57Blocker("single authoritative issue mapping is not closed")
    if atomicity.inputs[:3] != (enumeration, source.reference, patch.reference):
        raise FreshGate57Blocker("source atomicity lineage is invalid")
    if manifest.payload.get("complete") is not True or manifest.payload.get("state") != "passed":
        raise FreshGate57Blocker("current-v2 test manifest is not passed")
    if manifest.inputs != (patch.reference,):
        raise FreshGate57Blocker("test manifest lineage is invalid")
    if capsule.payload.get("profile_id") != "luanti-11352-map-settings-hierarchy-v1":
        raise FreshGate57Blocker("fixed capsule profile binding is invalid")
    if capsule.payload.get("source_tree_path") != _CAPSULE_REL.as_posix():
        raise FreshGate57Blocker("fixed capsule source path is invalid")
    if capsule.inputs != (source.reference, patch.reference, manifest.reference):
        raise FreshGate57Blocker("fixed capsule lineage is invalid")
    capsule_path = root / _CAPSULE_REL
    if not capsule_path.is_dir() or capsule_path.is_symlink():
        raise FreshGate57Blocker("fixed capsule tree is missing or unsafe")
    plan = plan_luanti_native_regression_execution(root, candidate=_CANDIDATE)
    if isinstance(plan, StageDecision):
        raise FreshGate57Blocker(
            f"Gate 4 planner blocked: {plan.details.get('reason', 'unknown')}"
        )
    if plan.profile.profile_id != capsule.payload["profile_id"]:
        raise FreshGate57Blocker("implementation profile does not match capsule")
    return plan, chain


def _validate_links(root: Path) -> None:
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
            continue
        if not stat.S_ISLNK(info.st_mode):
            raise FreshGate57Blocker("source tree contains an unsupported entry")
        target = os.readlink(path)
        if os.path.isabs(target):
            raise FreshGate57Blocker("source tree contains an absolute link")
        try:
            (path.parent / target).resolve(strict=False).relative_to(root.resolve())
        except ValueError as error:
            raise FreshGate57Blocker("source tree link escapes root") from error


def _inventory(root: Path) -> dict[str, tuple[str, str | None]]:
    values: dict[str, tuple[str, str | None]] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            values[relative] = ("directory", None)
        elif stat.S_ISREG(info.st_mode):
            values[relative] = ("file", None)
        elif stat.S_ISLNK(info.st_mode):
            values[relative] = ("symlink", os.readlink(path))
        else:
            raise FreshGate57Blocker("source inventory contains unsupported entry")
    return values


def _files_equal(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_stream, right.open("rb") as right_stream:
        while True:
            left_chunk = left_stream.read(65_536)
            right_chunk = right_stream.read(65_536)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _materialize(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    chain: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, Path, Path]:
    reference = materialization_reference()
    base = root / _RUN_ROOT
    fixed = base / "fixed"
    buggy = base / "buggy"
    existing = _try_load(root, reference)
    if existing is not None and fixed.is_dir() and buggy.is_dir():
        _validate_links(fixed)
        _validate_links(buggy)
        return existing, fixed, buggy
    if fixed.exists() or buggy.exists() or fixed.is_symlink() or buggy.is_symlink():
        raise FreshGate57Blocker("fresh materialization is partial and cannot resume")
    fixed.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(plan.fixed_capsule.source_tree, fixed, symlinks=True)
    try:
        _validate_links(fixed)
        shutil.copytree(fixed, buggy, symlinks=True)
        _validate_links(buggy)
        git = plan.toolchain.git
        # The artifact worktree is nested inside the parent StatePortal repo.
        # Without a ceiling Git climbs to that unrelated repository and
        # silently skips these untracked capsule paths.  Bind the inverse to
        # this fresh buggy tree only.
        env = {
            "PATH": str(git.parent),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CEILING_DIRECTORIES": str(buggy.parent),
        }
        result = subprocess.run(
            (str(git), "apply", "-R", "--whitespace=nowarn", "-"),
            cwd=buggy,
            input=plan.production_inverse.patch,
            capture_output=True,
            timeout=60,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            raise FreshGate57Blocker("fresh production inverse patch failed")
        fixed_inventory = _inventory(fixed)
        buggy_inventory = _inventory(buggy)
        if set(fixed_inventory) != set(buggy_inventory):
            raise FreshGate57Blocker("fixed/buggy source inventory differs")
        production = set(plan.profile.production_paths)
        for relative in fixed_inventory:
            if relative in production:
                if fixed_inventory[relative][0] != "file" or buggy_inventory[relative][0] != "file":
                    raise FreshGate57Blocker("production source entry is not a file")
                if _files_equal(fixed / relative, buggy / relative):
                    raise FreshGate57Blocker("production inverse did not change source")
            elif fixed_inventory[relative] != buggy_inventory[relative]:
                raise FreshGate57Blocker("non-production source changed")
            elif fixed_inventory[relative][0] == "file" and not _files_equal(fixed / relative, buggy / relative):
                raise FreshGate57Blocker("non-production file content changed")
        payload = {
            "candidate": _CANDIDATE.to_value(),
            "profile_id": plan.profile.profile_id,
            "state": "materialized",
            "fresh_epoch": _EPOCH,
            "fixed_tree_path": fixed.relative_to(root).as_posix(),
            "buggy_tree_path": buggy.relative_to(root).as_posix(),
            "production_inverse_paths": list(plan.profile.production_paths),
            "test_paths": list(plan.profile.test_paths),
            "p2p_test_paths": list(plan.profile.p2p_test_paths),
            "inherit_prior_attempts": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        }
        return _write(root, reference, tuple(item.reference for item in chain), payload, authorization), fixed, buggy
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


def _configure_argv(
    root: Path,
    tree: Path,
    build: Path,
    plan: NativeRegressionExecutionPlan,
    launcher: Path,
) -> tuple[str, ...]:
    tool = plan.toolchain
    package = tool.package_roots
    return tuple(
        str(value)
        for value in (
            tool.cmake,
            "-S",
            tree,
            "-B",
            build,
            "-G",
            "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_CXX_STANDARD=17",
            "-DCMAKE_CXX_STANDARD_REQUIRED=TRUE",
            "-DCMAKE_CXX_EXTENSIONS=OFF",
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
            f"-DCMAKE_MAKE_PROGRAM={tool.ninja}",
            f"-DCMAKE_C_COMPILER={tool.c_compiler}",
            f"-DCMAKE_CXX_COMPILER={tool.cxx_compiler}",
            f"-DCMAKE_CXX_COMPILER_LAUNCHER={launcher}",
            f"-DCMAKE_AR={tool.archiver}",
            f"-DCMAKE_RANLIB={tool.ranlib}",
            f"-DCMAKE_LINKER={tool.linker}",
            f"-DCMAKE_NM={tool.symbol_tool}",
            f"-DCMAKE_STRIP={tool.strip_tool}",
            f"-DCMAKE_OBJDUMP={tool.object_inspector}",
            f"-DCMAKE_INSTALL_NAME_TOOL={tool.install_name_tool}",
            f"-DIRRLICHT_INCLUDE_DIR={root / _IRRLICHT_INCLUDE}",
            f"-DSQLITE3_INCLUDE_DIR={package['sqlite3'] / 'include'}",
            f"-DSQLITE3_LIBRARY={package['sqlite3'] / 'lib/libsqlite3.a'}",
            f"-DICONV_LIBRARY={package['iconv'] / 'lib/libiconv.tbd'}",
            f"-DZLIB_INCLUDE_DIR={package['zlib'] / 'include'}",
            f"-DZLIB_LIBRARY={package['zlib'] / 'lib/libz.a'}",
            f"-DZSTD_INCLUDE_DIR={package['zstd'] / 'include'}",
            f"-DZSTD_LIBRARY={package['zstd'] / 'lib/libzstd.a'}",
        )
    )


def _write_process(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    plan: NativeRegressionExecutionPlan,
    variant: Literal["fixed", "buggy"],
    step: Literal["configure", "build"],
    argv: tuple[str, ...],
    receipt: NativeProcessReceipt,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    stdout = receipt.stdout[:_MAX_OUTPUT]
    stderr = receipt.stderr[:_MAX_OUTPUT]
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": plan.profile.profile_id,
        "fresh_epoch": _EPOCH,
        "variant": variant,
        "step": step,
        "language_standard": "c++17",
        "toolchain_reference": _TOOL_REFERENCE.artifact_id,
        "argv": list(argv),
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated or len(receipt.stdout) > _MAX_OUTPUT,
        "stderr_truncated": receipt.stderr_truncated or len(receipt.stderr) > _MAX_OUTPUT,
        "output_encoding": "base64",
        "state": (
            "built"
            if step == "build"
            else "configured"
        ) if receipt.outcome == "completed" and receipt.returncode == 0 else "blocked",
        "outcome": "completed" if receipt.outcome == "completed" and receipt.returncode == 0 else "infrastructure_blocked",
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(root, reference, inputs, payload, authorization)


def _build_variant(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    materialization: StableArtifact,
    tree: Path,
    variant: Literal["fixed", "buggy"],
    toolchain: StableArtifact,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, Path]:
    build = root / _BUILD_ROOT / variant
    build.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {
        "HOME": str(root / "private/runtime-home/luanti-native"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(plan.toolchain.root / "bin"),
    }
    launcher = root / _TOOLS_ROOT / _CXX_LAUNCHER
    configure = _configure_argv(root, tree, build, plan, launcher)
    configure_ref = build_reference(variant, "configure")
    configure_existing = _try_load(root, configure_ref)
    if configure_existing is None:
        receipt = run_bounded_process(configure, cwd=tree, env=env, timeout_seconds=_CONFIGURE_TIMEOUT)
        configure_existing = _write_process(
            root,
            configure_ref,
            (materialization.reference, toolchain.reference),
            plan,
            variant,
            "configure",
            configure,
            receipt,
            authorization,
        )
    if configure_existing.payload.get("outcome") != "completed":
        raise FreshGate57Blocker(f"{variant} C++17 configure blocked")
    build_argv = (str(plan.toolchain.cmake), "--build", str(build), "--target", "minetestserver", "--parallel", "1")
    build_ref = build_reference(variant, "build")
    build_existing = _try_load(root, build_ref)
    if build_existing is None:
        receipt = run_bounded_process(build_argv, cwd=tree, env=env, timeout_seconds=_BUILD_TIMEOUT)
        build_existing = _write_process(
            root,
            build_ref,
            (materialization.reference, configure_ref, toolchain.reference),
            plan,
            variant,
            "build",
            build_argv,
            receipt,
            authorization,
        )
    if build_existing.payload.get("outcome") != "completed":
        raise FreshGate57Blocker(f"{variant} C++17 build blocked")
    executable = tree / "bin/minetestserver"
    if not executable.is_file() or executable.is_symlink():
        raise FreshGate57Blocker(f"{variant} server executable is missing")
    return build_existing, executable


_MODULE_SUMMARY = re.compile(
    rb"(?m)^======== Module ([A-Za-z0-9_.-]+) (passed|failed) "
    rb"\((\d+) failures / (\d+) tests\)"
)


def _selected_module_observation(
    run: NativeRegressionRun,
    receipt: NativeProcessReceipt,
) -> tuple[str, dict[str, object]]:
    if receipt.outcome == "timed_out":
        return "timed_out", {"selector_mode": "module_summary"}
    if receipt.outcome == "launch_failed":
        return "launch_failed", {"selector_mode": "module_summary"}
    summaries = [
        match
        for match in _MODULE_SUMMARY.finditer(receipt.stdout)
    ] + [
        match
        for match in _MODULE_SUMMARY.finditer(receipt.stderr)
    ]
    selected = [match for match in summaries if match.group(1).decode() == run.selector]
    if len(selected) != 1:
        return "invalid_test_count", {
            "selector_mode": "module_summary",
            "selected_module_summary_count": len(selected),
        }
    match = selected[0]
    status = match.group(2).decode()
    failures = int(match.group(3))
    tests = int(match.group(4))
    return (
        "pass" if status == "passed" else "failure",
        {
            "selector_mode": "module_summary",
            "selected_module_status": status,
            "selected_module_failures": failures,
            "selected_module_tests": tests,
            "global_returncode": receipt.returncode,
        },
    )


def _observed(run: NativeRegressionRun, receipt: NativeProcessReceipt) -> str:
    return _selected_module_observation(run, receipt)[0]


def _attempt(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    materialization: StableArtifact,
    build: StableArtifact,
    executable: Path,
    run: NativeRegressionRun,
    variant: Literal["fixed", "buggy"],
    repetition: int,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = attempt_reference(run.phase, variant, repetition)
    existing = _try_load(root, reference)
    if existing is not None:
        return existing
    # This Luanti source's native runner executes all registered modules and
    # rejects --test-module.  Gate 5--7 therefore records the selected module
    # summary while retaining the global process result for audit.
    argv = (str(executable), "--run-unittests")
    env = {
        "HOME": str(root / "private/runtime-home/luanti-native"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(plan.toolchain.root / "bin"),
    }
    receipt = run_bounded_process(argv, cwd=executable.parent.parent, env=env, timeout_seconds=_TEST_TIMEOUT)
    observed, observation_details = _selected_module_observation(run, receipt)
    expected = run.fixed_expected_outcome if variant == "fixed" else run.buggy_expected_outcome
    stdout = receipt.stdout[:_MAX_OUTPUT]
    stderr = receipt.stderr[:_MAX_OUTPUT]
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": plan.profile.profile_id,
        "fresh_epoch": _EPOCH,
        "phase": run.phase,
        "variant": variant,
        "repetition": repetition,
        "argv": list(argv),
        "selector": run.selector,
        **observation_details,
        "expected_outcome": expected,
        "observed_outcome": observed,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated or len(receipt.stdout) > _MAX_OUTPUT,
        "stderr_truncated": receipt.stderr_truncated or len(receipt.stderr) > _MAX_OUTPUT,
        "output_encoding": "base64",
        "matched_expected": observed == expected,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(root, reference, (materialization.reference, build.reference), payload, authorization)


def _blocker(
    root: Path,
    chain: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
    reason: str,
    *,
    details: dict[str, object] | None = None,
    inputs: tuple[ArtifactReference, ...] = (),
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": "luanti-11352-map-settings-hierarchy-v1",
        "fresh_epoch": _EPOCH,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": reason,
        "details": details or {},
        "inherit_prior_attempts": False,
        "candidate_execution_performed": bool(inputs),
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "repair_or_register_dependency_then_resume_fresh_cpp17_epoch",
    }
    all_inputs = tuple(item.reference for item in chain) + tuple(inputs)
    return _write(root, blocker_reference(), all_inputs, payload, authorization)


def execute_luanti_11352_fresh_cpp17_gate57(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> FreshGate57Result:
    if confirmation != _CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {_CONFIRMATION!r}")
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("fresh #11352 execution authorization is inactive")
    root = _root(artifact_root)
    try:
        plan, chain = _chain(root)
    except FreshGate57Blocker as error:
        blocker = _blocker(root, tuple(), authorization, str(error))
        return FreshGate57Result(None, blocker, 0, False)
    try:
        toolchain = _register_cxx_launcher(root, chain, authorization)
        materialization, fixed, buggy = _materialize(root, plan, chain, authorization)
        built: dict[str, tuple[StableArtifact, Path]] = {}
        for variant, tree in (("fixed", fixed), ("buggy", buggy)):
            built[variant] = _build_variant(
                root,
                plan,
                materialization,
                tree,
                variant,
                toolchain,
                authorization,
            )
        attempts: list[StableArtifact] = []
        executed = 0
        for run in plan.runs:
            for variant in ("fixed", "buggy"):
                for repetition in (1, 2, 3):
                    attempt = _attempt(
                        root,
                        plan,
                        materialization,
                        built[variant][0],
                        built[variant][1],
                        run,
                        variant,
                        repetition,
                        authorization,
                    )
                    attempts.append(attempt)
                    executed += 1
                    if attempt.payload.get("matched_expected") is not True:
                        blocker = _blocker(
                            root,
                            chain,
                            authorization,
                            "unexpected_fixed_or_buggy_test_result",
                            details={
                                "phase": run.phase,
                                "variant": variant,
                                "repetition": repetition,
                                "observed_outcome": attempt.payload.get("observed_outcome"),
                                "expected_outcome": attempt.payload.get("expected_outcome"),
                            },
                            inputs=tuple(item.reference for item in attempts),
                        )
                        return FreshGate57Result(None, blocker, executed, False)
        if len(attempts) != 12:
            blocker = _blocker(root, chain, authorization, "fresh_attempt_coverage_incomplete", inputs=tuple(item.reference for item in attempts))
            return FreshGate57Result(None, blocker, executed, False)
        cells = []
        for phase, variant in (("f2p", "fixed"), ("f2p", "buggy"), ("p2p", "fixed"), ("p2p", "buggy")):
            group = [item for item in attempts if item.payload.get("phase") == phase and item.payload.get("variant") == variant]
            cells.append({"phase": phase, "variant": variant, "observed_outcomes": [item.payload["observed_outcome"] for item in group]})
        stable = all(len(set(cell["observed_outcomes"])) == 1 for cell in cells)
        aggregate_payload = {
            "candidate": _CANDIDATE.to_value(),
            "profile_id": plan.profile.profile_id,
            "fresh_epoch": _EPOCH,
            "state": "qualified" if stable else "blocked",
            "outcome": "qualified" if stable else "infrastructure_blocked",
            "repetitions_per_cell": 3,
            "attempt_count": 12,
            "stable_repetitions": stable,
            "all_expected": True,
            "qualified": stable,
            "cells": cells,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        }
        aggregate = _write(root, aggregate_reference(), tuple(item.reference for item in attempts), aggregate_payload, authorization)
        return FreshGate57Result(aggregate, None, executed, stable)
    except FreshGate57Blocker as error:
        blocker = _blocker(root, chain, authorization, str(error))
        return FreshGate57Result(None, blocker, 0, False)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        blocker = _blocker(root, chain, authorization, "fresh_gate57_execution_error", details={"error_type": type(error).__name__, "message": str(error)[:500]})
        return FreshGate57Result(None, blocker, 0, False)


__all__ = [
    "FreshGate57Blocker",
    "FreshGate57Result",
    "_CONFIRMATION",
    "aggregate_reference",
    "attempt_reference",
    "blocker_reference",
    "build_reference",
    "execute_luanti_11352_fresh_cpp17_gate57",
    "materialization_reference",
]
