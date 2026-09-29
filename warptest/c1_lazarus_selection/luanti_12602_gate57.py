"""Fresh candidate-specific Gate 5--7 execution for Luanti #12602.

The historical #12602 executor stopped before configure because its retained
materialisation had no IrrlichtMt headers and its fixed/buggy trees were
already contaminated.  This adapter creates a new, immutable fixed capsule
from the retained #12602 mirror ref, copies only a separately registered
IrrlichtMt header closure, applies the production inverse to a new buggy tree,
and executes the exact 3 x F2P/P2P matrix.  It never loads the old
``server-unit-v2`` receipts or performs downstream gates.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .luanti_12602_profile import load_candidate_chain
from .luanti_native_executor import (
    NativeProcessReceipt,
    run_bounded_process,
)
from .luanti_native_regression import NativeRegressionRun
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_12602_GATE57_NATIVE_V1"
_CANDIDATE = CandidateKey("luanti", 12602)
_VALUE = _CANDIDATE.to_value()
_EPOCH = "candidate_gate57_12602_native_v9"
_VERSION = "1.0"
_MAX_OUTPUT = 262_144
_MIRROR = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_TOOLCHAIN = PurePosixPath("private/dependencies/luanti-native/3.0")
_CAPSULE_TREE = PurePosixPath(
    "private/fixed-capsules/luanti/12602/captured-merged-tree"
)
_RUN_ROOT = PurePosixPath("private/bounded-runs/luanti/12602/gate57-v9")
_BUILD_ROOT = PurePosixPath("private/bounded-builds/luanti/12602/gate57-v9")
_TOOLS_ROOT = PurePosixPath("private/bounded-tools/luanti/12602/gate57-v9")
_CXX_LAUNCHER = _TOOLS_ROOT / "cxx17-launcher"
_IRR_SOURCE = PurePosixPath("private/fixed-capsules/luanti/14464/fixed-source/irr")
_IRR_ROOT = PurePosixPath("private/dependencies/luanti-12602-irrlicht/1.9.0.15")
_IRR_INCLUDE = _IRR_ROOT / "include"
_BINARY = "bin/minetestserver"
_PROFILE = "luanti-12602-media-override-v1"
_SOURCE_REV = "5892fcf27837480143353d7fa156d9a9fbfa8c74"
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/12602/fixed/v2", "fixed_source_capsule", "2.0"
)
_IRR_DEP = ArtifactReference(
    "candidate_gate57_12602_irrlicht_dependency/luanti/12602/native/v1",
    "candidate_gate57_12602_irrlicht_dependency",
    _VERSION,
)
_BUILD_KIND = "candidate_gate57_12602_build_v9"
_ATTEMPT_KIND = "candidate_gate57_12602_attempt_v9"
_AGGREGATE = ArtifactReference(
    "candidate_gate57_12602_aggregate/luanti/12602/native/v9",
    "candidate_gate57_12602_aggregate",
    _VERSION,
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_12602_transition/luanti/12602/native/v9",
    "candidate_gate57_12602_transition",
    _VERSION,
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_12602_blocker/luanti/12602/native/v9",
    "candidate_gate57_12602_blocker",
    _VERSION,
)
_SUMMARY = re.compile(
    rb"(?m)^Unit Test Results: (PASSED|FAILED)\r?\n"
    rb"\s+(\d+) / (\d+) failed tests\.\r?$"
)
_MODULE_SUMMARY = re.compile(
    rb"(?m)^======== Module ([A-Za-z0-9_.-]+) (passed|failed) "
    rb"\((\d+) failures / (\d+) tests\)"
)
_TOOLCHAIN_ARTIFACT = ArtifactReference(
    "candidate_gate57_12602_cxx17_toolchain/luanti/12602/native/v9",
    "candidate_gate57_12602_cxx17_toolchain",
    _VERSION,
)


def execute_luanti_12602_gate57(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
) -> tuple[StableArtifact, StableArtifact]:
    """Run a new 12-cell native epoch, returning aggregate and transition."""

    _require_authorization(authorization)
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    _assert_fresh_namespace(root)
    try:
        capsule = _load_or_materialize(
            root, _CAPSULE, _CAPSULE_TREE, _materialize_capsule, chain, authorization
        )
        irr = _load_or_materialize(
            root,
            _IRR_DEP,
            _IRR_ROOT,
            _materialize_irrlicht_dependency,
            chain,
            authorization,
        )
        launcher = _ensure_cxx17_launcher(
            root, chain, capsule, irr, authorization
        )
        trees = _materialize_pair(root, chain, capsule, irr)
        builds = {
            variant: _build_variant(
                root,
                chain,
                capsule,
                irr,
                launcher,
                variant,
                trees[variant],
                authorization,
            )
            for variant in ("fixed", "buggy")
        }
        attempts: list[StableArtifact] = []
        for phase, selector, buggy_expected in (
            ("f2p", "TestServerModManager.testGetModMediaPaths", "failure"),
            ("p2p", "TestServerModManager.testGetModNames", "pass"),
        ):
            run = NativeRegressionRun(
                phase=phase,  # type: ignore[arg-type]
                selector=selector,
                executable_path=_BINARY,
                argv=(_BINARY, "--run-unittests", "--test-module", selector),
                repetitions=3,
                fixed_expected_outcome="pass",
                buggy_expected_outcome=buggy_expected,  # type: ignore[arg-type]
            )
            for variant in ("fixed", "buggy"):
                for repetition in (1, 2, 3):
                    attempts.append(
                        _run_cell(
                            root,
                            chain,
                            capsule,
                            irr,
                            launcher,
                            builds[variant],
                            run,
                            variant,
                            repetition,
                            trees[variant],
                            authorization,
                        )
                    )
    except BaseException as error:
        blocker = _write_blocker(
            root,
            chain,
            error,
            authorization=authorization,
        )
        return blocker, blocker

    aggregate = _write_aggregate(
        root, chain, capsule, irr, launcher, builds, attempts, authorization
    )
    transition = _write_transition(
        root, chain, capsule, irr, launcher, aggregate, attempts, authorization
    )
    return aggregate, transition


def _load_chain(root: Path) -> tuple[StableArtifact, ...]:
    chain = tuple(
        load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
        for reference in (
            ArtifactReference(
                "candidate_source_packet/luanti/12602/source/v2",
                "candidate_source_packet",
                "2.0",
            ),
            ArtifactReference(
                "candidate_test_patch/luanti/12602/screening/v2",
                "candidate_test_patch",
                "2.0",
            ),
            ArtifactReference(
                "source_atomicity/luanti/12602/preannotation/v2",
                "source_atomicity",
                "2.0",
            ),
            ArtifactReference(
                "candidate_stage/luanti/12602/test_manifest/v2",
                "candidate_stage",
                "2.0",
            ),
            ArtifactReference(
                "candidate_profile/luanti/12602/preflight/v2",
                "candidate_profile",
                "2.0",
            ),
            ArtifactReference(
                "candidate_preparation/luanti/12602/preannotation/v2",
                "candidate_preparation",
                "2.0",
            ),
            ArtifactReference(
                "candidate_downstream_plan/luanti/12602/preannotation/v2",
                "candidate_downstream_plan",
                "2.0",
            ),
        )
    )
    source, patch, atomicity, manifest, profile, preparation, downstream = chain
    load_candidate_chain(root)
    if any(item.payload.get("candidate") != _VALUE for item in chain):
        raise RuntimeError("#12602 current-v2 chain candidate binding drifted")
    statuses = profile.payload.get("strict_gate_status")
    if (
        source.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or atomicity.payload.get("outcome") != "accepted"
        or manifest.payload.get("state") != "passed"
        or not isinstance(statuses, dict)
        or any(
            statuses.get(key) != "passed"
            for key in (
                "source_authority",
                "single_defect_mapping",
                "strict_inverse_patch",
                "test_api_compatibility",
            )
        )
        or preparation.payload.get("state") != "prepared"
        or downstream.payload.get("state") != "planned"
        or profile.payload.get("profile_id") != _PROFILE
    ):
        raise RuntimeError("#12602 current-v2 Gate 1--4 prerequisites are not passed")
    return chain


def _assert_fresh_namespace(root: Path) -> None:
    # The source capsule and registered dependency are immutable candidate
    # inputs.  A prior attempt may have materialized them successfully before
    # a later inverse/build step failed; reusing those typed inputs does not
    # inherit any old attempt or build receipt.  All execution namespaces must
    # still be empty for this epoch.
    for reference in (_AGGREGATE, _TRANSITION, _BLOCKER):
        if (
            root
            / "artifacts"
            / Path(reference.artifact_id)
            / reference.semantic_version
        ).exists():
            raise RuntimeError(
                f"fresh namespace already exists: {reference.artifact_id}"
            )
    for kind in (_BUILD_KIND, _ATTEMPT_KIND):
        directory = root / "artifacts" / kind / "luanti" / "12602"
        if directory.exists() and any(directory.rglob("*.json")):
            raise RuntimeError(f"fresh namespace is not empty: {kind}")
    for destination, reference in ((_CAPSULE_TREE, _CAPSULE), (_IRR_ROOT, _IRR_DEP)):
        if (root / destination).exists():
            try:
                load_stable_artifact(
                    root,
                    artifact_id=reference.artifact_id,
                    artifact_kind=reference.artifact_kind,
                    semantic_version=reference.semantic_version,
                )
            except RuntimeError as error:
                raise RuntimeError(
                    "candidate-specific materialization exists without a valid receipt"
                ) from error


def _load_or_materialize(
    root: Path,
    reference: ArtifactReference,
    destination: PurePosixPath,
    materialize,
    chain: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    destination_path = root / destination
    try:
        artifact = load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
    except RuntimeError:
        artifact = None
    if artifact is not None and destination_path.is_dir():
        return artifact
    if artifact is not None or destination_path.exists():
        raise RuntimeError("candidate-specific materialization receipt/tree mismatch")
    return materialize(root, chain, authorization)


def _materialize_capsule(
    root: Path, chain: tuple[StableArtifact, ...], authorization: AuthorizationRecord
) -> StableArtifact:
    source = chain[0]
    manifest_path = root / PurePosixPath(source.payload["raw_evidence_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pull = manifest.get("pull")
    if not isinstance(pull, dict) or pull.get("head", {}).get("sha") != _SOURCE_REV:
        raise RuntimeError("#12602 source head does not match retained mirror revision")
    mirror = root / _MIRROR
    git = root / _TOOLCHAIN / "bin/git"
    if not mirror.is_dir() or not git.is_file():
        raise RuntimeError("#12602 retained mirror or registered git is unavailable")
    ref = subprocess.run(
        (str(git), f"--git-dir={mirror}", "rev-parse", "refs/c1/pull/12602/head"),
        capture_output=True,
        check=False,
        env={"PATH": str(git.parent), "LANG": "C", "LC_ALL": "C"},
    )
    if ref.returncode != 0 or ref.stdout.decode().strip() != _SOURCE_REV:
        raise RuntimeError("#12602 retained mirror head revision drifted")
    destination = root / _CAPSULE_TREE
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix="lz12602-capsule-", dir=destination.parent
    ) as temp:
        staging = Path(temp) / "tree"
        staging.mkdir(mode=0o700)
        archive = subprocess.Popen(
            (str(git), f"--git-dir={mirror}", "archive", "--format=tar", _SOURCE_REV),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": str(git.parent), "LANG": "C", "LC_ALL": "C"},
        )
        assert archive.stdout is not None and archive.stderr is not None
        try:
            with tarfile.open(fileobj=archive.stdout, mode="r|") as stream:
                for member in stream:
                    _safe_archive_member(member)
                    target = staging / PurePosixPath(member.name)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    elif member.issym():
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        target.symlink_to(member.linkname)
                    elif member.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        source_file = stream.extractfile(member)
                        if source_file is None:
                            raise RuntimeError(
                                "#12602 source archive member is unreadable"
                            )
                        with source_file, target.open("wb") as output:
                            shutil.copyfileobj(source_file, output)
                        target.chmod(0o700 if member.mode & 0o111 else 0o600)
            stderr = archive.stderr.read()
            returncode = archive.wait(timeout=60)
        except BaseException:
            archive.kill()
            archive.wait()
            raise
        if returncode != 0:
            raise RuntimeError(
                "#12602 source archive failed: " + stderr.decode(errors="replace")[:300]
            )
        _validate_tree(staging)
        os.replace(staging, destination)
    payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE,
        "source_tree_path": _CAPSULE_TREE.as_posix(),
        "source_revision": _SOURCE_REV,
        "mirror_ref": "refs/c1/pull/12602/head",
        "source_manifest_path": source.payload["raw_evidence_path"],
        "tree_fingerprint": _tree_sha256(destination),
        "source_materialization": "git_archive_from_retained_mirror",
        "network_used": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "state": "materialized",
        "complete": True,
    }
    return _write(
        root,
        _CAPSULE,
        tuple(item.reference for item in chain[:4]),
        payload,
        authorization,
    )


def _materialize_irrlicht_dependency(
    root: Path, chain: tuple[StableArtifact, ...], authorization: AuthorizationRecord
) -> StableArtifact:
    source = root / _IRR_SOURCE
    include = source / "include"
    cmake = source / "CMakeLists.txt"
    if (
        not include.is_dir()
        or not (include / "irrlicht.h").is_file()
        or not cmake.is_file()
    ):
        raise RuntimeError("registered IrrlichtMt closure is missing")
    cmake_text = cmake.read_text(encoding="utf-8")
    if (
        "set(IRRLICHTMT_REVISION 15)" not in cmake_text
        or "VERSION 1.9.0.${IRRLICHTMT_REVISION}" not in cmake_text
    ):
        raise RuntimeError("registered IrrlichtMt closure version drifted")
    destination = root / _IRR_INCLUDE
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(include, destination, symlinks=False)
    _validate_tree(destination)
    payload = {
        "candidate": _VALUE,
        "dependency_id": "irrlichtmt-headers",
        "dependency_version": "1.9.0.15",
        "source_tree_path": _IRR_SOURCE.as_posix(),
        "destination_root": _IRR_ROOT.as_posix(),
        "include_path": _IRR_INCLUDE.as_posix(),
        "source_tree_fingerprint": _tree_sha256(source),
        "include_tree_fingerprint": _tree_sha256(destination),
        "historical_requirement": {
            "candidate_source_revision": _SOURCE_REV,
            "requirement": "CMake server path requires irrlicht.h headers; no version pin",
            "cmake_module_fingerprint": "5cf6238dacdda20782a2153203d75e15ccf39b7390c57cb534ab45b59034bd2c",
        },
        "copied_file_count": sum(
            1 for path in destination.rglob("*") if path.is_file()
        ),
        "network_used": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "state": "materialized",
        "complete": True,
    }
    return _write(root, _IRR_DEP, (), payload, authorization)


def _ensure_cxx17_launcher(
    root: Path,
    chain: tuple[StableArtifact, ...],
    capsule: StableArtifact,
    irr: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Register a source-preserving C++17 compiler launcher for this epoch.

    The retained #12602 source is not edited.  CMake is still configured with
    an explicit C++17 target and this launcher appends final ``-std=c++17`` and
    the registered IrrlichtMt compatibility alias to every C++ invocation, so
    the dependency cannot silently fall back to the old C++14/API defaults.
    """

    path = root / _CXX_LAUNCHER
    content = (
        b"#!/bin/sh\n"
        b"set -eu\n"
        b"compiler=\"$1\"\n"
        b"shift\n"
        b"exec \"$compiler\" \"$@\" -std=c++17 -DTextureLayer=TextureLayers\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != content:
            raise RuntimeError("#12602 C++17 launcher drifted")
    else:
        path.write_bytes(content)
        path.chmod(0o700)
    payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE,
        "fresh_epoch": _EPOCH,
        "tool_kind": "candidate_cxx17_launcher",
        "wrapper_path": path.relative_to(root).as_posix(),
        "content_fingerprint": hashlib.sha256(content).hexdigest(),
        "language_standard": "c++17",
        "compatibility_defines": ["TextureLayer=TextureLayers"],
        "source_preserving": True,
        "target": "minetestserver",
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(
        root,
        _TOOLCHAIN_ARTIFACT,
        tuple(item.reference for item in chain)
        + (capsule.reference, irr.reference),
        payload,
        authorization,
    )


def _materialize_pair(
    root: Path,
    chain: tuple[StableArtifact, ...],
    capsule: StableArtifact,
    irr: StableArtifact,
) -> dict[str, Path]:
    base = root / _RUN_ROOT
    fixed, buggy = base / "fixed", base / "buggy"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(root / _CAPSULE_TREE, fixed, symlinks=True)
    shutil.copytree(fixed, buggy, symlinks=True)
    patch = chain[1]
    raw_patch = root / PurePosixPath(patch.payload["raw_patch_path"])
    content = raw_patch.read_bytes()
    marker = b"diff --git a/src/server/mods.cpp b/src/server/mods.cpp"
    start = content.index(marker)
    end = content.find(b"diff --git ", start + len(marker))
    production = content[start:] if end < 0 else content[start:end]
    git = root / _TOOLCHAIN / "bin/git"
    env = {
        "PATH": str(git.parent),
        "LANG": "C",
        "LC_ALL": "C",
        # Keep git from discovering the parent StatePortal repository while
        # applying a patch inside the unversioned candidate tree.
        "GIT_DIR": "/dev/null",
    }
    result = subprocess.run(
        (str(git), "apply", "-R", "--whitespace=nowarn", "-"),
        cwd=buggy,
        input=production,
        capture_output=True,
        check=False,
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError("#12602 candidate-specific production inverse failed")
    _validate_pair(fixed, buggy, "src/server/mods.cpp")
    return {"fixed": fixed, "buggy": buggy}


def _build_variant(
    root: Path,
    chain: tuple[StableArtifact, ...],
    capsule: StableArtifact,
    irr: StableArtifact,
    launcher: StableArtifact,
    variant: str,
    tree: Path,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    tc = root / _TOOLCHAIN
    build = root / _BUILD_ROOT / variant
    build.mkdir(parents=True, exist_ok=True, mode=0o700)
    packages = {
        name: tc / "packages" / name for name in ("sqlite3", "iconv", "zlib", "zstd")
    }
    cmake = tc / "bin/cmake"
    configure = (
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
        f"-DCMAKE_MAKE_PROGRAM={tc / 'bin/ninja'}",
        f"-DCMAKE_C_COMPILER={tc / 'bin/cc'}",
        f"-DCMAKE_CXX_COMPILER={tc / 'bin/c++'}",
        f"-DCMAKE_AR={tc / 'bin/ar'}",
        f"-DCMAKE_RANLIB={tc / 'bin/ranlib'}",
        f"-DCMAKE_LINKER={tc / 'bin/ld'}",
        f"-DCMAKE_NM={tc / 'bin/nm'}",
        f"-DCMAKE_STRIP={tc / 'bin/strip'}",
        f"-DCMAKE_OBJDUMP={tc / 'bin/otool'}",
        f"-DCMAKE_INSTALL_NAME_TOOL={tc / 'bin/install_name_tool'}",
        f"-DSQLITE3_INCLUDE_DIR={packages['sqlite3'] / 'include'}",
        f"-DSQLITE3_LIBRARY={packages['sqlite3'] / 'lib/libsqlite3.a'}",
        f"-DICONV_LIBRARY={packages['iconv'] / 'lib/libiconv.tbd'}",
        f"-DZLIB_INCLUDE_DIR={packages['zlib'] / 'include'}",
        f"-DZLIB_LIBRARY={packages['zlib'] / 'lib/libz.a'}",
        f"-DZSTD_INCLUDE_DIR={packages['zstd'] / 'include'}",
        f"-DZSTD_LIBRARY={packages['zstd'] / 'lib/libzstd.a'}",
        f"-DIRRLICHT_INCLUDE_DIR={root / _IRR_INCLUDE}",
        "-DCMAKE_CXX_STANDARD=17",
        "-DCMAKE_CXX_STANDARD_REQUIRED=TRUE",
        "-DCMAKE_CXX_EXTENSIONS=OFF",
        "-DCMAKE_CXX_FLAGS=-w",
        f"-DCMAKE_CXX_COMPILER_LAUNCHER={root / _CXX_LAUNCHER}",
    )
    build_argv = (
        str(cmake),
        "--build",
        str(build),
        "--target",
        "minetestserver",
        "--parallel",
        "1",
    )
    env = {
        "HOME": str(root / "private/runtime-home/luanti-12602-gate57"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(tc / "bin"),
        "GIT_DIR": "/dev/null",
    }
    receipts: list[dict[str, object]] = []
    for step, argv, timeout in (
        ("configure", configure, 600),
        ("build", build_argv, 1800),
    ):
        receipt = run_bounded_process(argv, cwd=tree, env=env, timeout_seconds=timeout)
        receipts.append(_process_payload(variant, step, argv, receipt))
        reference = ArtifactReference(
            f"{_BUILD_KIND}/luanti/12602/{variant}/{step}/v1", _BUILD_KIND, _VERSION
        )
        payload = {
            "candidate": _VALUE,
            "profile_id": _PROFILE,
            "fresh_epoch": _EPOCH,
            "variant": variant,
            "step": step,
            "tree_path": tree.relative_to(root).as_posix(),
            "binary_path": (tree / _BINARY).relative_to(root).as_posix(),
            "irrlicht_dependency": _IRR_DEP.artifact_id,
            "cxx17_toolchain": launcher.reference.artifact_id,
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            **receipts[-1],
        }
        _write(
            root,
            reference,
        tuple(item.reference for item in chain)
        + (capsule.reference, irr.reference, launcher.reference),
            payload,
            authorization,
        )
        if receipt.outcome != "completed" or receipt.returncode != 0:
            raise RuntimeError(f"#12602 {variant} {step} failed")
    binary = tree / _BINARY
    if not binary.is_file() or binary.is_symlink():
        raise RuntimeError(f"#12602 {variant} server binary is missing")
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _run_cell(
    root: Path,
    chain: tuple[StableArtifact, ...],
    capsule: StableArtifact,
    irr: StableArtifact,
    launcher: StableArtifact,
    build: StableArtifact,
    run: NativeRegressionRun,
    variant: str,
    repetition: int,
    tree: Path,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    binary = tree / _BINARY
    # This historical Luanti binary predates --test-module.  Execute the
    # registered suite once and bind the cell to the unique
    # TestServerModManager module summary instead of inventing a selector.
    argv = (str(binary), "--run-unittests")
    expected = (
        run.fixed_expected_outcome if variant == "fixed" else run.buggy_expected_outcome
    )
    env = {
        "HOME": str(root / "private/runtime-home/luanti-12602-gate57"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(root / _TOOLCHAIN / "bin"),
        "GIT_DIR": "/dev/null",
    }
    receipt = run_bounded_process(argv, cwd=tree, env=env, timeout_seconds=300)
    module_name = run.selector.split(".", 1)[0]
    summaries = [
        match
        for stream in (receipt.stdout, receipt.stderr)
        for match in _MODULE_SUMMARY.finditer(stream)
        if match.group(1).decode("utf-8", errors="strict") == module_name
    ]
    if receipt.outcome == "timed_out":
        observed = "timed_out"
    elif receipt.outcome == "launch_failed":
        observed = "launch_failed"
    elif len(summaries) != 1:
        observed = "invalid_test_count"
    else:
        observed = "pass" if summaries[0].group(2) == b"passed" else "failure"
    count = int(summaries[0].group(4)) if len(summaries) == 1 else 0
    reference = ArtifactReference(
        f"{_ATTEMPT_KIND}/luanti/12602/{run.phase}/{variant}/{repetition}/v1",
        _ATTEMPT_KIND,
        _VERSION,
    )
    stdout, stdout_truncated = _bounded_output(receipt.stdout)
    stderr, stderr_truncated = _bounded_output(receipt.stderr)
    payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE,
        "fresh_epoch": _EPOCH,
        "phase": run.phase,
        "variant": variant,
        "repetition": repetition,
        "selector": run.selector,
        "expected_outcome": expected,
        "observed_outcome": observed,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "completed_test_count": count,
        "selector_mode": "module_summary",
        "selected_module": module_name,
        "selected_module_summary_count": len(summaries),
        "selected_module_failures": (
            int(summaries[0].group(3)) if len(summaries) == 1 else None
        ),
        "selected_module_tests": (
            int(summaries[0].group(4)) if len(summaries) == 1 else None
        ),
        "argv": list(argv),
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated or stdout_truncated,
        "stderr_truncated": receipt.stderr_truncated or stderr_truncated,
        "output_encoding": "base64",
        "matched_expected": observed == expected,
        "tree_path": tree.relative_to(root).as_posix(),
        "build_reference": build.reference.artifact_id,
        "irrlicht_dependency": _IRR_DEP.artifact_id,
        "cxx17_toolchain": launcher.reference.artifact_id,
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(
        root,
        reference,
        tuple(item.reference for item in chain)
        + (capsule.reference, irr.reference, launcher.reference, build.reference),
        payload,
        authorization,
    )


def _write_aggregate(
    root: Path,
    chain: tuple[StableArtifact, ...],
    capsule: StableArtifact,
    irr: StableArtifact,
    launcher: StableArtifact,
    builds: dict[str, StableArtifact],
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for item in attempts:
        groups.setdefault((item.payload["phase"], item.payload["variant"]), []).append(
            item
        )
    expected_groups = {
        ("f2p", "fixed"),
        ("f2p", "buggy"),
        ("p2p", "fixed"),
        ("p2p", "buggy"),
    }
    if (
        len(attempts) != 12
        or set(groups) != expected_groups
        or any(len(group) != 3 for group in groups.values())
    ):
        raise RuntimeError("#12602 Gate 5--7 attempt coverage is incomplete")
    stable = all(
        len({item.payload["observed_outcome"] for item in group}) == 1
        for group in groups.values()
    )
    infrastructure = any(
        item.payload["observed_outcome"]
        in {"timed_out", "launch_failed", "unknown", "invalid_test_count"}
        or item.payload["process_outcome"] != "completed"
        for item in attempts
    )
    all_expected = all(item.payload["matched_expected"] is True for item in attempts)
    qualified = stable and all_expected and not infrastructure
    outcome = (
        "qualified"
        if qualified
        else "infrastructure_blocked"
        if infrastructure
        else "candidate_semantic_mismatch"
    )
    payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE,
        "fresh_epoch": _EPOCH,
        "state": "qualified" if qualified else "blocked",
        "outcome": outcome,
        "qualified": qualified,
        "terminal": outcome == "candidate_semantic_mismatch",
        "resumable": outcome != "candidate_semantic_mismatch",
        "attempt_count": 12,
        "expected_attempt_count": 12,
        "repetitions_per_cell": 3,
        "stable_repetitions": stable,
        "all_expected": all_expected,
        "no_timeout_or_unknown": not infrastructure,
        "old_attempts_loaded": False,
        "build_references": {
            key: value.reference.artifact_id for key, value in builds.items()
        },
        "cxx17_toolchain": launcher.reference.artifact_id,
        "cells": [
            {
                "phase": phase,
                "variant": variant,
                "observed_outcomes": [
                    item.payload["observed_outcome"]
                    for item in sorted(
                        groups[(phase, variant)], key=lambda x: x.payload["repetition"]
                    )
                ],
                "matched_expected": [
                    item.payload["matched_expected"]
                    for item in sorted(
                        groups[(phase, variant)], key=lambda x: x.payload["repetition"]
                    )
                ],
            }
            for phase, variant in sorted(expected_groups)
        ],
        "truth_table": {
            "fixed": {"f2p": "pass", "p2p": "pass"},
            "buggy": {"f2p": "failure", "p2p": "pass"},
        },
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    refs = (
        tuple(item.reference for item in chain)
        + (capsule.reference, irr.reference, launcher.reference)
        + tuple(item.reference for item in builds.values())
        + tuple(item.reference for item in attempts)
    )
    return _write(root, _AGGREGATE, refs, payload, authorization)


def _write_transition(
    root: Path,
    chain: tuple[StableArtifact, ...],
    capsule: StableArtifact,
    irr: StableArtifact,
    launcher: StableArtifact,
    aggregate: StableArtifact,
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    qualified = aggregate.payload["qualified"] is True
    payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE,
        "fresh_epoch": _EPOCH,
        "state": "passed" if qualified else "blocked",
        "outcome": aggregate.payload["outcome"],
        "aggregate_reference": aggregate.reference.artifact_id,
        "cxx17_toolchain": launcher.reference.artifact_id,
        "attempt_count": len(attempts),
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "gate_1_7_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
            "fixed_f2p_3x": "passed" if qualified else "failed",
            "buggy_f2p_3x": "passed_clean_failure" if qualified else "failed",
            "fixed_p2p_3x": "passed" if qualified else "failed",
            "buggy_p2p_3x": "passed" if qualified else "failed",
        },
        "deferred_until_after_gate5_7": [
            "gate8_full_versions",
            "gate9_start_replay_oracle_observations",
            "gate10_goldens",
            "gate11_visual_cross_judgment",
            "gate12_same_lineage_receipt",
            "gate13_freeze",
        ],
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    refs = tuple(item.reference for item in chain) + (
        capsule.reference,
        irr.reference,
        launcher.reference,
        aggregate.reference,
    )
    return _write(root, _TRANSITION, refs, payload, authorization)


def _write_blocker(
    root: Path,
    chain: tuple[StableArtifact, ...],
    error: BaseException,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE,
        "fresh_epoch": _EPOCH,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": False,
        "reason": "candidate_specific_native_build_or_execution_failed",
        "error_type": type(error).__name__,
        "error": str(error),
        "candidate_materialization_performed": True,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "next_action": "inspect candidate-specific receipts; do not reuse old attempts",
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(
        root, _BLOCKER, tuple(item.reference for item in chain), payload, authorization
    )


def _process_payload(
    variant: str, step: str, argv: tuple[str, ...], receipt: NativeProcessReceipt
) -> dict[str, object]:
    stdout, stdout_truncated = _bounded_output(receipt.stdout)
    stderr, stderr_truncated = _bounded_output(receipt.stderr)
    return {
        "variant": variant,
        "step": step,
        "argv": list(argv),
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated or stdout_truncated,
        "stderr_truncated": receipt.stderr_truncated or stderr_truncated,
        "output_encoding": "base64",
    }


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
    artifact = load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("#12602 typed receipt reload mismatch")
    return artifact


def _validate_pair(fixed: Path, buggy: Path, production: str) -> None:
    _validate_tree(fixed)
    _validate_tree(buggy)
    for left in fixed.rglob("*"):
        relative = left.relative_to(fixed)
        right = buggy / relative
        if left.is_dir() and not left.is_symlink():
            continue
        if relative.as_posix() == production:
            if _same_file(left, right):
                raise RuntimeError(
                    "#12602 production inverse did not change production"
                )
        elif not _same_file(left, right):
            raise RuntimeError(
                "#12602 production inverse changed a non-production path"
            )


def _same_file(left: Path, right: Path) -> bool:
    if left.is_symlink() or right.is_symlink():
        return (
            left.is_symlink()
            and right.is_symlink()
            and left.readlink() == right.readlink()
        )
    if left.is_file() != right.is_file() or not left.is_file():
        return left.is_dir() and right.is_dir()
    return (
        left.stat().st_size == right.stat().st_size
        and left.read_bytes() == right.read_bytes()
    )


def _validate_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("#12602 tree is unsafe")
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
            continue
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            if os.path.isabs(target):
                raise RuntimeError("#12602 tree contains an absolute symlink")
            try:
                (path.parent / target).resolve(strict=False).relative_to(root.resolve())
            except ValueError as error:
                raise RuntimeError("#12602 tree symlink escapes root") from error
            continue
        raise RuntimeError("#12602 tree contains an unsupported entry")


def _safe_archive_member(member: tarfile.TarInfo) -> None:
    name = member.name.rstrip("/")
    pure = PurePosixPath(name)
    if (
        not pure.parts
        or pure.is_absolute()
        or "." in pure.parts
        or ".." in pure.parts
        or pure.as_posix() != name
        or not (member.isdir() or member.isfile() or member.issym())
    ):
        raise RuntimeError("#12602 source archive path is unsafe")
    # A repository symlink may legitimately use ``..`` to point to another
    # file within the archive (for example the historical favicon link).  The
    # extracted tree is checked with ``_validate_tree`` below, which resolves
    # every relative target and rejects links that escape the capsule root.
    if member.issym() and PurePosixPath(member.linkname).is_absolute():
        raise RuntimeError("#12602 source archive symlink is unsafe")


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    entries = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if not path.is_dir()
    )
    for relative in entries:
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode("utf-8"))
        else:
            digest.update(b"file\0")
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _bounded_output(value: bytes) -> tuple[bytes, bool]:
    return value[:_MAX_OUTPUT], len(value) > _MAX_OUTPUT


def _observed(receipt: NativeProcessReceipt, count: int | None) -> str:
    if receipt.outcome == "timed_out":
        return "timed_out"
    if receipt.outcome == "launch_failed":
        return "launch_failed"
    if receipt.outcome != "completed" or type(receipt.returncode) is not int:
        return "unknown"
    if count != 1:
        return "invalid_test_count"
    return "pass" if receipt.returncode == 0 else "failure"


def _require_authorization(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("#12602 Gate 5--7 authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CONFIRMATION", "execute_luanti_12602_gate57"]
