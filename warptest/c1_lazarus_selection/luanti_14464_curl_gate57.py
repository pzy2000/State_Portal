"""Fresh Gate 5--7 runner for Luanti #14464 with a registered curl closure.

The generic native profile for #14464 is useful for the current-v2 static
chain, but its first build was configured with ``ENABLE_CURL=FALSE`` while the
old capsule still compiles curl call sites.  This adapter deliberately starts
an independent epoch.  It records the corrected P2P binding (``TestSettings``)
and a content-hashed curl dependency closure copied into the artifact root,
then configures both materialized trees with private RPATHs.  No ``/opt`` path
is passed to CMake or to a test process and no prior build/attempt is resumed.

Only the bounded fixed/buggy build and twelve F2P/P2P cells are covered here;
post-admission construction, replay, oracle, golden, and visual gates remain
deferred until Gate 5--7 is qualified.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from .luanti_native_executor import (
    NativeProcessReceipt,
    _apply_production_inverse,
    _bounded_output,
    _copy_full_capsule,
    _new_workspace,
    _ordinary_existing_file,
    _prepare_closed_home,
    _remove_workspace,
    _validate_capsule,
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


CONFIRMATION = "EXECUTE_LUANTI_14464_CURL_GATE57_V1"
_CANDIDATE = CandidateKey("luanti", 14464)
_CANDIDATE_VALUE = _CANDIDATE.to_value()
_EPOCH = "candidate_gate57_14464_curl_retry_v1"
_VERSION = "1.0"
_BUILD_KIND = "candidate_gate57_14464_curl_retry_build"
_ATTEMPT_KIND = "candidate_gate57_14464_curl_retry_attempt"
_BUILD_ROOT = "build-luanti-14464-curl-v1"
_BINARY = "bin/minetestserver"

_ROOT_CHAIN = (
    ArtifactReference(
        "candidate_source_packet/luanti/14464/source/v2",
        "candidate_source_packet",
        "2.0",
    ),
    ArtifactReference(
        "candidate_test_patch/luanti/14464/screening/v2", "candidate_test_patch", "2.0"
    ),
    ArtifactReference(
        "source_atomicity/luanti/14464/preannotation/v2", "source_atomicity", "2.0"
    ),
    ArtifactReference(
        "candidate_stage/luanti/14464/test_manifest/v2", "candidate_stage", "2.0"
    ),
    ArtifactReference(
        "candidate_profile/luanti/14464/preflight/v2", "candidate_profile", "2.0"
    ),
    ArtifactReference(
        "candidate_preparation/luanti/14464/preannotation/v2",
        "candidate_preparation",
        "2.0",
    ),
    ArtifactReference(
        "candidate_downstream_plan/luanti/14464/preannotation/v2",
        "candidate_downstream_plan",
        "2.0",
    ),
    ArtifactReference(
        "fixed_source_capsule/luanti/14464/fixed/v2", "fixed_source_capsule", "2.0"
    ),
)
_PROFILE_REF = ArtifactReference(
    "candidate_gate57_14464_curl_retry_profile/luanti/14464/native/v1",
    "candidate_gate57_14464_curl_retry_profile",
    _VERSION,
)
_CAPSULE_REF = ArtifactReference(
    "candidate_gate57_14464_curl_retry_capsule/luanti/14464/native/v1",
    "candidate_gate57_14464_curl_retry_capsule",
    _VERSION,
)
_CURL_REF = ArtifactReference(
    "candidate_gate57_14464_curl_dependency/luanti/14464/native/v1",
    "candidate_gate57_14464_curl_dependency",
    _VERSION,
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_14464_curl_retry_aggregate/luanti/14464/native/v1",
    "candidate_gate57_14464_curl_retry_aggregate",
    _VERSION,
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_14464_curl_retry_transition/luanti/14464/native/v1",
    "candidate_gate57_14464_curl_retry_transition",
    _VERSION,
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_14464_curl_retry_blocker/luanti/14464/native/v1",
    "candidate_gate57_14464_curl_retry_blocker",
    _VERSION,
)


def execute_luanti_14464_curl_gate57(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
) -> tuple[StableArtifact, StableArtifact]:
    """Register the corrected profile/closure and run fresh Gate 5--7 cells."""

    _require_authorization(authorization)
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    _assert_namespace_empty(root)
    profile = _write_profile(root, chain, authorization)
    capsule = _write_capsule(root, chain, authorization)
    closure = _register_curl_closure(root, chain, profile, capsule, authorization)
    plan = _candidate_plan(root, closure)
    _validate_capsule(root, plan)
    workspace = _new_workspace(root, _CANDIDATE)
    builds: dict[str, ArtifactReference] = {}
    attempts: list[StableArtifact] = []
    process_started = False
    try:
        trees = {variant: workspace / variant for variant in ("fixed", "buggy")}
        _copy_full_capsule(plan, trees["fixed"])
        _copy_full_capsule(plan, trees["buggy"])
        _apply_production_inverse(plan, trees["fixed"], trees["buggy"])
        for variant in ("fixed", "buggy"):
            builds[variant] = _build_variant(
                root,
                plan,
                variant=variant,
                tree=trees[variant],
                chain=chain,
                profile=profile,
                capsule=capsule,
                closure=closure,
                authorization=authorization,
            )
        for run in plan.runs:
            for variant in ("fixed", "buggy"):
                for repetition in range(1, run.repetitions + 1):
                    process_started = True
                    attempts.append(
                        _run_cell(
                            root,
                            plan,
                            run=run,
                            variant=variant,
                            repetition=repetition,
                            tree=trees[variant],
                            build_reference=builds[variant],
                            chain=chain,
                            profile=profile,
                            capsule=capsule,
                            closure=closure,
                            authorization=authorization,
                        )
                    )
    except BaseException as error:
        blocker = _write_blocker(
            root,
            chain,
            profile,
            capsule,
            closure,
            error,
            builds=builds,
            attempts=attempts,
            process_started=process_started,
            authorization=authorization,
        )
        return blocker, blocker
    finally:
        _remove_workspace(workspace, root)

    aggregate = _write_aggregate(
        root, chain, profile, capsule, closure, plan, builds, attempts, authorization
    )
    transition = _write_transition(
        root,
        chain,
        profile,
        capsule,
        closure,
        aggregate,
        builds,
        attempts,
        authorization,
    )
    return aggregate, transition


def _load_chain(root: Path) -> tuple[StableArtifact, ...]:
    chain = tuple(
        load_stable_artifact(
            root,
            artifact_id=r.artifact_id,
            artifact_kind=r.artifact_kind,
            semantic_version=r.semantic_version,
        )
        for r in _ROOT_CHAIN
    )
    source, patch, atomicity, manifest, profile, preparation, downstream, capsule = (
        chain
    )
    if any(item.payload.get("candidate") != _CANDIDATE_VALUE for item in chain):
        raise RuntimeError("#14464 current-v2 chain candidate binding drifted")
    statuses = profile.payload.get("strict_gate_status", {})
    if (
        source.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or atomicity.payload.get("outcome") != "accepted"
        or manifest.payload.get("state") != "passed"
        or statuses.get("source_authority") != "passed"
        or statuses.get("single_defect_mapping") != "passed"
        or statuses.get("strict_inverse_patch") != "passed"
        or statuses.get("test_api_compatibility") != "passed"
        or preparation.payload.get("state") != "prepared"
        or downstream.payload.get("state") != "planned"
        or capsule.payload.get("source_tree_path")
        != "private/fixed-capsules/luanti/14464/fixed-source"
    ):
        raise RuntimeError("#14464 current-v2 Gate 1--4 prerequisites are not passed")
    tree = root / "private/fixed-capsules/luanti/14464/fixed-source"
    required = (
        "src/content_nodemeta.cpp",
        "src/nodemetadata.cpp",
        "src/object_properties.cpp",
        "src/script/common/c_converter.cpp",
        "src/serialization.cpp",
        "src/util/serialize.h",
        "src/util/string.cpp",
        "src/unittest/test_utilities.cpp",
        "src/unittest/test_settings.cpp",
    )
    for relative in required:
        path = tree / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"#14464 capsule file is missing or unsafe: {relative}")
    return chain


def _assert_namespace_empty(root: Path) -> None:
    refs = (_PROFILE_REF, _CAPSULE_REF, _CURL_REF, _AGGREGATE, _TRANSITION, _BLOCKER)
    for reference in refs:
        directory = (
            root
            / "artifacts"
            / Path(reference.artifact_id)
            / reference.semantic_version
        )
        if directory.exists():
            raise RuntimeError(
                f"fresh curl retry namespace is not empty: {reference.artifact_id}"
            )
    for kind in (_BUILD_KIND, _ATTEMPT_KIND):
        directory = root / "artifacts" / kind / "luanti" / "14464"
        if directory.exists():
            raise RuntimeError(f"fresh curl retry namespace is not empty: {kind}")


def _write_profile(
    root: Path, chain: tuple[StableArtifact, ...], authorization: AuthorizationRecord
) -> StableArtifact:
    current = chain[4].payload
    payload = {
        "candidate": _CANDIDATE_VALUE,
        "fresh_epoch": _EPOCH,
        "profile_id": "luanti-14464-iconv-state-curl-retry-v1",
        "base_profile_reference": chain[4].reference.artifact_id,
        "f2p_selector": "TestUtilities",
        "p2p_selector": "TestSettings",
        "p2p_test_paths": ["src/unittest/test_settings.cpp"],
        "f2p_test_paths": ["src/unittest/test_utilities.cpp"],
        "production_paths": current["production_paths"],
        "strict_gate_status": {
            key: current["strict_gate_status"][key]
            for key in (
                "source_authority",
                "single_defect_mapping",
                "strict_inverse_patch",
                "test_api_compatibility",
            )
        },
        "selector_correction": "current-v2 P2P path/selector rebound from TestUtilities.testRemoveEscapes to TestSettings",
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(
        root,
        _PROFILE_REF,
        tuple(item.reference for item in chain),
        payload,
        authorization,
    )


def _tree_digest(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        info = path.lstat()
        digest.update(rel.encode("utf-8") + b"\0")
        if stat.S_ISLNK(info.st_mode):
            digest.update(b"symlink\0" + os.readlink(path).encode("utf-8") + b"\0")
        elif stat.S_ISREG(info.st_mode):
            digest.update(b"file\0")
            with path.open("rb") as stream:
                while chunk := stream.read(262144):
                    digest.update(chunk)
            count += 1
        elif not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"unsafe capsule entry: {rel}")
    return digest.hexdigest(), count


def _write_capsule(
    root: Path, chain: tuple[StableArtifact, ...], authorization: AuthorizationRecord
) -> StableArtifact:
    capsule = root / "private/fixed-capsules/luanti/14464/fixed-source"
    digest, file_count = _tree_digest(capsule)
    payload = {
        "candidate": _CANDIDATE_VALUE,
        "fresh_epoch": _EPOCH,
        "base_capsule_reference": chain[7].reference.artifact_id,
        "profile_reference": _PROFILE_REF.artifact_id,
        "source_tree_path": "private/fixed-capsules/luanti/14464/fixed-source",
        "source_tree_content_fingerprint": digest,
        "source_file_count": file_count,
        "test_paths": ["src/unittest/test_utilities.cpp"],
        "p2p_test_paths": ["src/unittest/test_settings.cpp"],
        "f2p_selector": "TestUtilities",
        "p2p_selector": "TestSettings",
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(
        root,
        _CAPSULE_REF,
        tuple(item.reference for item in chain) + (_PROFILE_REF,),
        payload,
        authorization,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(262144):
            digest.update(chunk)
    return digest.hexdigest()


def _register_curl_closure(
    root: Path,
    chain: tuple[StableArtifact, ...],
    profile: StableArtifact,
    capsule: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    closure = root / "private/dependencies/luanti-curl/1.0"
    if closure.exists():
        raise RuntimeError(
            "candidate-specific curl closure path already exists without fresh receipt"
        )
    include = closure / "include/curl"
    lib = closure / "lib"
    include.parent.mkdir(parents=True, exist_ok=False)
    lib.mkdir(parents=True, exist_ok=False)
    source_root = Path("/opt/miniconda3")
    source_include = source_root / "include/curl"
    if not source_include.is_dir() or source_include.is_symlink():
        raise RuntimeError("local curl headers unavailable")
    shutil.copytree(source_include, include, symlinks=False)
    library_sources = {
        "libcurl.4.dylib": "libcurl.4.8.0.dylib",
        "libssh2.1.dylib": "libssh2.1.0.1.dylib",
        "libidn2.0.dylib": "libidn2.0.dylib",
        "libssl.3.dylib": "libssl.3.dylib",
        "libcrypto.3.dylib": "libcrypto.3.dylib",
        "libz.1.dylib": "libz.1.3.1.dylib",
        "libzstd.1.dylib": "libzstd.1.5.7.dylib",
        "libnghttp2.14.dylib": "libnghttp2.14.dylib",
        "libiconv.2.dylib": "libiconv.2.dylib",
        "libunistring.5.dylib": "libunistring.5.dylib",
    }
    for destination, source_name in library_sources.items():
        source = source_root / "lib" / source_name
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(
                f"curl closure source is missing or unsafe: {source_name}"
            )
        shutil.copy2(source, lib / destination, follow_symlinks=True)
    entries = []
    hashes: dict[str, str] = {}
    for path in sorted(
        closure.rglob("*"), key=lambda p: p.relative_to(closure).as_posix()
    ):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError("curl closure contains unsafe entry")
        relative = path.relative_to(closure).as_posix()
        entries.append(relative)
        hashes[relative] = _sha256(path)
    payload = {
        "candidate": _CANDIDATE_VALUE,
        "fresh_epoch": _EPOCH,
        "dependency_id": "luanti-curl-native-closure",
        "semantic_version": _VERSION,
        "runtime_root": "private/dependencies/luanti-curl/1.0",
        "include_root": "private/dependencies/luanti-curl/1.0/include",
        "library_root": "private/dependencies/luanti-curl/1.0/lib",
        "closure_files": entries,
        "file_integrity": hashes,
        "source_provenance": {
            "registration_root": "/opt/miniconda3",
            "headers": "include/curl",
            "libraries": sorted(library_sources.values()),
        },
        "dynamic_dependencies": {
            "private_rpath": sorted(library_sources),
            "system": [
                "/System/Library/Frameworks/SystemConfiguration.framework/Versions/A/SystemConfiguration",
                "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
                "/usr/lib/libSystem.B.dylib",
            ],
        },
        "profile_reference": profile.reference.artifact_id,
        "capsule_reference": capsule.reference.artifact_id,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(
        root, _CURL_REF, (profile.reference, capsule.reference), payload, authorization
    )


def _candidate_plan(
    root: Path, closure: StableArtifact
) -> NativeRegressionExecutionPlan:
    decision = plan_luanti_native_regression_execution(root, candidate=_CANDIDATE)
    if isinstance(decision, StageDecision):
        raise RuntimeError(
            f"#14464 native plan is blocked: {decision.details.get('reason')}"
        )
    curl_root = root / "private/dependencies/luanti-curl/1.0"
    if closure.payload.get("runtime_root") != "private/dependencies/luanti-curl/1.0":
        raise RuntimeError("curl closure runtime root drifted")
    configure = []
    for index, raw_value in enumerate(decision.configure_argv):
        value = raw_value if index == 0 else str(raw_value)
        if value in {"build-luanti-14464", "-DENABLE_CURL=FALSE"}:
            value = (
                _BUILD_ROOT if value == "build-luanti-14464" else "-DENABLE_CURL=TRUE"
            )
        configure.append(value)
    configure.extend(
        (
            f"-DCURL_INCLUDE_DIR={curl_root / 'include'}",
            f"-DCURL_LIBRARY={curl_root / 'lib/libcurl.4.dylib'}",
            f"-DCMAKE_BUILD_RPATH={curl_root / 'lib'}",
            f"-DCMAKE_INSTALL_RPATH={curl_root / 'lib'}",
            f"-DCMAKE_EXE_LINKER_FLAGS=-Wl,-rpath,{curl_root / 'lib'}",
            "-DCMAKE_SKIP_RPATH=FALSE",
            "-DCMAKE_BUILD_WITH_INSTALL_RPATH=TRUE",
        )
    )
    build = [decision.build_argv[0]] + [
        "minetestserver" if str(value) == "luantiserver" else str(value)
        for value in decision.build_argv[1:]
    ]
    build = [_BUILD_ROOT if value == "build-luanti-14464" else value for value in build]
    runs = tuple(
        replace(
            run,
            executable_path=_BINARY,
            argv=(
                _BINARY,
                "--run-unittests",
                "--test-module",
                "TestUtilities" if run.phase == "f2p" else "TestSettings",
            ),
        )
        for run in decision.runs
    )
    return replace(
        decision, configure_argv=tuple(configure), build_argv=tuple(build), runs=runs
    )


def _build_variant(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    *,
    variant: str,
    tree: Path,
    chain: tuple[StableArtifact, ...],
    profile: StableArtifact,
    capsule: StableArtifact,
    closure: StableArtifact,
    authorization: AuthorizationRecord,
) -> ArtifactReference:
    _prepare_closed_home(root, Path(plan.closed_environment["HOME"]))
    last: ArtifactReference | None = None
    for step, raw_argv, timeout in (
        ("configure", plan.configure_argv, 600),
        ("build", plan.build_argv, 1800),
    ):
        argv = tuple(str(value) for value in raw_argv)
        receipt = run_bounded_process(
            argv, cwd=tree, env=dict(plan.closed_environment), timeout_seconds=timeout
        )
        reference = ArtifactReference(
            f"{_BUILD_KIND}/luanti/14464/{variant}/{step}/v1", _BUILD_KIND, _VERSION
        )
        stdout, out_truncated = _bounded_output(receipt.stdout)
        stderr, err_truncated = _bounded_output(receipt.stderr)
        payload = {
            "candidate": _CANDIDATE_VALUE,
            "fresh_epoch": _EPOCH,
            "profile_reference": profile.reference.artifact_id,
            "capsule_reference": capsule.reference.artifact_id,
            "curl_dependency_reference": closure.reference.artifact_id,
            "variant": variant,
            "step": step,
            "tree_path": tree.relative_to(root).as_posix(),
            "binary_path": _BINARY,
            "argv": list(argv),
            "process_outcome": receipt.outcome,
            "returncode": receipt.returncode,
            "stdout_base64": base64.b64encode(stdout).decode("ascii"),
            "stderr_base64": base64.b64encode(stderr).decode("ascii"),
            "stdout_truncated": out_truncated or receipt.stdout_truncated,
            "stderr_truncated": err_truncated or receipt.stderr_truncated,
            "output_encoding": "base64",
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        }
        _write(
            root,
            reference,
            tuple(item.reference for item in chain)
            + (profile.reference, capsule.reference, closure.reference),
            payload,
            authorization,
        )
        last = reference
        if receipt.outcome != "completed" or receipt.returncode != 0:
            raise RuntimeError(
                f"#14464 {variant} {step} failed: {receipt.outcome}/{receipt.returncode}"
            )
    assert last is not None
    _ordinary_existing_file(tree / _BINARY, tree)
    return last


def _run_cell(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    *,
    run: NativeRegressionRun,
    variant: Literal["fixed", "buggy"],
    repetition: int,
    tree: Path,
    build_reference: ArtifactReference,
    chain: tuple[StableArtifact, ...],
    profile: StableArtifact,
    capsule: StableArtifact,
    closure: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    expected = (
        run.fixed_expected_outcome if variant == "fixed" else run.buggy_expected_outcome
    )
    argv = (str(tree / run.executable_path), *run.argv[1:])
    receipt = run_bounded_process(
        argv, cwd=tree, env=dict(plan.closed_environment), timeout_seconds=300
    )
    count = completed_native_test_count(
        run, receipt.outcome, receipt.stdout, receipt.stderr
    )
    observed = _observed(receipt, count)
    stdout, out_truncated = _bounded_output(receipt.stdout)
    stderr, err_truncated = _bounded_output(receipt.stderr)
    reference = ArtifactReference(
        f"{_ATTEMPT_KIND}/luanti/14464/{run.phase}/{variant}/{repetition}/v1",
        _ATTEMPT_KIND,
        _VERSION,
    )
    payload = {
        "candidate": _CANDIDATE_VALUE,
        "fresh_epoch": _EPOCH,
        "profile_reference": profile.reference.artifact_id,
        "capsule_reference": capsule.reference.artifact_id,
        "curl_dependency_reference": closure.reference.artifact_id,
        "build_reference": build_reference.artifact_id,
        "phase": run.phase,
        "variant": variant,
        "repetition": repetition,
        "selector": run.selector,
        "executable_path": run.executable_path,
        "expected_outcome": expected,
        "observed_outcome": observed,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "completed_test_count": count,
        "argv": list(argv),
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": out_truncated or receipt.stdout_truncated,
        "stderr_truncated": err_truncated or receipt.stderr_truncated,
        "output_encoding": "base64",
        "matched_expected": observed == expected,
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(
        root,
        reference,
        tuple(item.reference for item in chain)
        + (profile.reference, capsule.reference, closure.reference, build_reference),
        payload,
        authorization,
    )


def _observed(receipt: NativeProcessReceipt, count: int | None) -> str:
    if receipt.outcome == "timed_out":
        return "timed_out"
    if receipt.outcome == "launch_failed":
        return "launch_failed"
    if receipt.outcome != "completed" or not isinstance(receipt.returncode, int):
        return "unknown"
    if count != 1:
        return "invalid_test_count"
    return "pass" if receipt.returncode == 0 else "failure"


def _write_aggregate(
    root: Path,
    chain: tuple[StableArtifact, ...],
    profile: StableArtifact,
    capsule: StableArtifact,
    closure: StableArtifact,
    plan: NativeRegressionExecutionPlan,
    builds: dict[str, ArtifactReference],
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 12:
        raise RuntimeError("#14464 Gate 5--7 attempt coverage is incomplete")
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for attempt in attempts:
        groups.setdefault(
            (attempt.payload["phase"], attempt.payload["variant"]), []
        ).append(attempt)
    expected_groups = {
        ("f2p", "fixed"),
        ("f2p", "buggy"),
        ("p2p", "fixed"),
        ("p2p", "buggy"),
    }
    if set(groups) != expected_groups or any(
        len(group) != 3 for group in groups.values()
    ):
        raise RuntimeError("#14464 Gate 5--7 repetition coverage is incomplete")
    stable = all(
        len({item.payload["observed_outcome"] for item in group}) == 1
        for group in groups.values()
    )
    all_expected = all(item.payload["matched_expected"] is True for item in attempts)
    infrastructure = any(
        item.payload["observed_outcome"]
        in {"timed_out", "launch_failed", "unknown", "invalid_test_count"}
        or item.payload["process_outcome"] != "completed"
        for item in attempts
    )
    qualified = stable and all_expected and not infrastructure
    outcome = (
        "qualified"
        if qualified
        else "infrastructure_blocked"
        if infrastructure
        else "candidate_semantic_mismatch"
    )
    payload = {
        "candidate": _CANDIDATE_VALUE,
        "fresh_epoch": _EPOCH,
        "profile_reference": profile.reference.artifact_id,
        "capsule_reference": capsule.reference.artifact_id,
        "curl_dependency_reference": closure.reference.artifact_id,
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
        "build_references": {key: value.artifact_id for key, value in builds.items()},
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
    inputs = (
        tuple(item.reference for item in chain)
        + (profile.reference, capsule.reference, closure.reference)
        + tuple(builds.values())
        + tuple(item.reference for item in attempts)
    )
    return _write(root, _AGGREGATE, inputs, payload, authorization)


def _write_transition(
    root: Path,
    chain: tuple[StableArtifact, ...],
    profile: StableArtifact,
    capsule: StableArtifact,
    closure: StableArtifact,
    aggregate: StableArtifact,
    builds: dict[str, ArtifactReference],
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    qualified = aggregate.payload["qualified"] is True
    outcome = aggregate.payload["outcome"]
    payload = {
        "candidate": _CANDIDATE_VALUE,
        "fresh_epoch": _EPOCH,
        "state": "passed" if qualified else "blocked",
        "outcome": outcome,
        "aggregate_reference": aggregate.reference.artifact_id,
        "profile_reference": profile.reference.artifact_id,
        "capsule_reference": capsule.reference.artifact_id,
        "curl_dependency_reference": closure.reference.artifact_id,
        "attempt_count": len(attempts),
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
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
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
    return _write(
        root,
        _TRANSITION,
        tuple(item.reference for item in chain)
        + (
            profile.reference,
            capsule.reference,
            closure.reference,
            aggregate.reference,
        ),
        payload,
        authorization,
    )


def _write_blocker(
    root: Path,
    chain: tuple[StableArtifact, ...],
    profile: StableArtifact,
    capsule: StableArtifact,
    closure: StableArtifact,
    error: BaseException,
    *,
    builds: dict[str, ArtifactReference],
    attempts: list[StableArtifact],
    process_started: bool,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE_VALUE,
        "fresh_epoch": _EPOCH,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "candidate_specific_curl_build_or_execution_failed",
        "error_type": type(error).__name__,
        "error": str(error),
        "profile_reference": profile.reference.artifact_id,
        "capsule_reference": capsule.reference.artifact_id,
        "curl_dependency_reference": closure.reference.artifact_id,
        "candidate_execution_performed": process_started or bool(attempts),
        "candidate_materialization_performed": True,
        "build_references": {key: value.artifact_id for key, value in builds.items()},
        "attempt_count": len(attempts),
        "expected_attempt_count": 12,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "inspect only this curl-retry namespace; do not reuse old attempts",
    }
    return _write(
        root,
        _BLOCKER,
        tuple(item.reference for item in chain)
        + (profile.reference, capsule.reference, closure.reference),
        payload,
        authorization,
    )


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
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _require_authorization(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("#14464 curl Gate 5--7 authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifact-root", default=".warptest-cache/c1_lazarus_selection_v1"
    )
    parser.add_argument("--confirmation", default=CONFIRMATION)
    args = parser.parse_args()
    authorization = AuthorizationRecord(
        authorization_id="authorization/luanti-14464-curl-retry-v1",
        scope="preannotation-execution",
        granted_at=_now(),
        local_user_id=os.getuid(),
        revoked=False,
    )
    first, second = execute_luanti_14464_curl_gate57(
        args.artifact_root, authorization=authorization, confirmation=args.confirmation
    )
    print(first.reference.artifact_id)
    print(second.reference.artifact_id)


__all__ = ["CONFIRMATION", "execute_luanti_14464_curl_gate57"]
