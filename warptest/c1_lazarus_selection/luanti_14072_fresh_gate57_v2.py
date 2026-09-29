"""Candidate-specific native Gate 5--7 closure for Luanti #14072.

The old #14072 receipt stopped before the first attempt because the exact
merged source tree, an IrrlichtMt include closure, and a method-level test
selector were not registered.  This adapter closes those prerequisites in a
new namespace.  The production inverse is still performed by the shared
native executor; the selector adapter only runs the real ``TestMoveAction``
module and derives one selected method's result from its emitted ``[PASS]`` /
``[FAIL]`` line.  No previous attempts are loaded and this module exposes no
paid or formal-trial action.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import tarfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from .luanti_native_executor import (
    EXECUTION_CONFIRMATION,
    NativeProcessReceipt,
    execute_luanti_native_regression,
    run_bounded_process,
)
from .luanti_native_regression import (
    NativeRegressionExecutionPlan,
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


_ROOT_DEFAULT = Path(".warptest-cache/c1_lazarus_selection_v1")
_CANDIDATE = CandidateKey("luanti", 14072)
_PROFILE_ID = "luanti-14072-oversized-stack-v2-native-gate57"
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14072/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14072/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14072/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14072/test_manifest/v2", "candidate_stage", "2.0"
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/14072/fixed/v2", "fixed_source_capsule", "2.0"
)
_IRRLICHT = ArtifactReference(
    "candidate_gate57_dependency/luanti/14072/irrlichtmt/v1",
    "candidate_gate57_dependency",
    "1.0",
)
_TOOLCHAIN = ArtifactReference(
    "candidate_gate57_fresh_toolchain/luanti/14072/cpp17/v2",
    "candidate_gate57_fresh_toolchain",
    "1.0",
)
_ADAPTER = ArtifactReference(
    "candidate_gate57_selector_adapter/luanti/14072/native/v1",
    "candidate_gate57_selector_adapter",
    "1.0",
)
_NATIVE_AGGREGATE = ArtifactReference(
    "native_regression_aggregate/luanti/14072/fixed-buggy/v6",
    "native_regression_aggregate",
    "3.0",
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_fresh_aggregate/luanti/14072/native/v6",
    "candidate_gate57_fresh_aggregate",
    "1.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_fresh_transition/luanti/14072/native/v6",
    "candidate_gate57_fresh_transition",
    "1.0",
)
_SELECTOR_EVIDENCE = ArtifactReference(
    "candidate_gate57_selector_evidence/luanti/14072/native/v6",
    "candidate_gate57_selector_evidence",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_fresh_blocker/luanti/14072/native/v6",
    "candidate_gate57_fresh_blocker",
    "1.0",
)
_CAPSULE_REL = PurePosixPath("private/fixed-capsules/luanti/14072/fixed-source")
_IRRLICHT_REL = PurePosixPath(
    "private/dependencies/luanti-12602-irrlicht/1.9.0.15/include"
)
_MIRROR_REL = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_MERGE_COMMIT = "94a54375e25b916a486bf8e2fe43aa8bbcb9ebdf"
_BASE_COMMIT = "d1a55e9ca4e66d8ba2d799569de7a6b48bc15f23"
_MODULE = "TestMoveAction"
_F2P_METHOD = "testMoveFillStack"
_P2P_METHOD = "testMovePartial"
_TEST = "src/unittest/test_moveaction.cpp"
_P2P_TEST = _TEST
_EXECUTION_NAMESPACE = "v6"
_LAUNCHER_REL = PurePosixPath(
    "private/fresh-gate57-tools/luanti/14072/cxx17-pp-launcher"
)
_PRODUCTION = ("src/inventory.cpp", "src/unittest/helpers/helper_moveaction.lua")
_METHOD_RESULT = re.compile(rb"(?m)^\[(PASS|FAIL)\] ([A-Za-z0-9_]+) - \d+ms\r?$")
_MODULE_SUMMARY = re.compile(rb"(?m)^======== Module [^\r\n]+ .*\r?$\n?")
_OUTER_SUMMARY = re.compile(
    rb"(?ms)^Unit Test Results: (?:PASSED|FAILED)\r?\n"
    rb"\s+\d+ / \d+ failed tests\.(?:\r?\n|$)(?:.*?\r?\n)?"
)


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, Any],
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
        raise RuntimeError(f"#14072 typed reload mismatch: {reference.artifact_id}")
    return artifact


def _require_scope(authorization: AuthorizationRecord, scope: str) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != scope
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(f"#14072 {scope} authorization is inactive")


def _archive_capsule(root: Path) -> None:
    destination = root / _CAPSULE_REL
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise RuntimeError("#14072 fixed capsule namespace is unsafe")
        if (destination / "CMakeLists.txt").is_file():
            return
        raise RuntimeError("#14072 fixed capsule namespace is incomplete")
    mirror = root / _MIRROR_REL
    if not mirror.is_dir() or mirror.is_symlink():
        raise RuntimeError("#14072 registered Luanti mirror is missing")
    destination.mkdir(parents=True, mode=0o700)
    archive = subprocess.run(
        ["git", "--git-dir", str(mirror), "archive", "--format=tar", _MERGE_COMMIT],
        check=False,
        capture_output=True,
        timeout=180,
    )
    if archive.returncode != 0 or not archive.stdout:
        raise RuntimeError("#14072 merged source capsule archive failed")
    with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as stream:
        for member in stream.getmembers():
            target = (destination / member.name).resolve()
            if target != destination and destination not in target.parents:
                raise RuntimeError("#14072 capsule archive escaped destination")
        stream.extractall(destination)
    if not (destination / "CMakeLists.txt").is_file():
        raise RuntimeError("#14072 fixed capsule is incomplete")


def _ensure_launcher(root: Path) -> tuple[Path, str]:
    """Register a shell-safe C++ launcher for the legacy missing PP macro."""

    path = root / _LAUNCHER_REL
    content = (
        b"#!/bin/sh\n"
        b"set -eu\n"
        b'compiler="$1"\n'
        b"shift\n"
        b'exec "$compiler" "$@" \'-DPP(x)=x\'\n'
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise RuntimeError("#14072 C++ PP launcher drifted")
    else:
        path.write_bytes(content)
        path.chmod(0o700)
    return path, hashlib.sha256(content).hexdigest()


def _tree_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for item in sorted(
        path.rglob("*"), key=lambda value: value.relative_to(path).as_posix()
    ):
        relative = item.relative_to(path).as_posix()
        if item.is_symlink():
            raise RuntimeError(f"#14072 dependency tree contains symlink: {relative}")
        if item.is_dir():
            digest.update(f"D:{relative}\n".encode())
            continue
        if not item.is_file():
            raise RuntimeError(f"#14072 dependency tree entry is unsafe: {relative}")
        count += 1
        data = item.read_bytes()
        digest.update(f"F:{relative}:{len(data)}\n".encode())
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest(), count


def _source_commit_evidence(root: Path) -> dict[str, Any]:
    mirror = root / _MIRROR_REL
    for commit in (_BASE_COMMIT, _MERGE_COMMIT):
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "cat-file", "-t", commit],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if check.returncode != 0 or check.stdout.strip() != b"commit":
            raise RuntimeError(f"#14072 source commit is not registered: {commit}")
    return {
        "mirror": _MIRROR_REL.as_posix(),
        "merge_commit": _MERGE_COMMIT,
        "base_commit": _BASE_COMMIT,
        "fixed_source_is_merged_tree": True,
        "old_attempts_loaded": False,
    }


def prepare(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> dict[str, object]:
    """Register the exact merged capsule and candidate-specific closures."""

    _require_scope(authorization, "preannotation-execution")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    if (
        source.payload.get("candidate") != _CANDIDATE.to_value()
        or source.payload.get("complete") is not True
    ):
        raise RuntimeError("#14072 source packet is not current and complete")
    if (
        patch.payload.get("candidate") != _CANDIDATE.to_value()
        or patch.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("#14072 patch is not Gate 1--4 eligible")
    if (
        atomicity.payload.get("outcome") != "accepted"
        or manifest.payload.get("state") != "passed"
    ):
        raise RuntimeError("#14072 Gate 1--4 chain is not accepted")
    _source_commit_evidence(root)
    _archive_capsule(root)
    capsule_tree = root / _CAPSULE_REL
    for relative in (*_PRODUCTION, _TEST, _P2P_TEST):
        path = capsule_tree / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(
                f"#14072 capsule source is missing or unsafe: {relative}"
            )
    capsule = _write(
        root,
        _CAPSULE,
        (_SOURCE, _PATCH, _MANIFEST),
        {
            "candidate": _CANDIDATE.to_value(),
            "profile_id": _PROFILE_ID,
            "source_tree_path": _CAPSULE_REL.as_posix(),
            "source_manifest_path": source.payload["raw_evidence_path"],
            "test_paths": [_TEST],
            "p2p_test_paths": [_P2P_TEST],
            "support_paths": [],
        },
        authorization,
    )
    irrlicht = root / _IRRLICHT_REL
    if (
        not irrlicht.is_dir()
        or irrlicht.is_symlink()
        or not (irrlicht / "irrlicht.h").is_file()
    ):
        raise RuntimeError("#14072 registered IrrlichtMt include closure is missing")
    irrlicht_digest, header_count = _tree_digest(irrlicht)
    irrlicht_artifact = _write(
        root,
        _IRRLICHT,
        (capsule.reference,),
        {
            "candidate": _CANDIDATE.to_value(),
            "dependency_id": "luanti-irrlichtmt-1.9.0.15",
            "semantic_version": "1.9.0.15",
            "include_root": _IRRLICHT_REL.as_posix(),
            "include_fingerprint": irrlicht_digest,
            "header_file_count": header_count,
            "old_attempts_loaded": False,
            "network": False,
            "paid_actions_authorized": False,
        },
        authorization,
    )
    runtime = root / "private/dependencies/luanti-native/3.0/runtime.json"
    if not runtime.is_file() or runtime.is_symlink():
        raise RuntimeError("#14072 registered native toolchain manifest is missing")
    runtime_digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
    launcher, launcher_fingerprint = _ensure_launcher(root)
    toolchain = _write(
        root,
        _TOOLCHAIN,
        (capsule.reference, irrlicht_artifact.reference),
        {
            "candidate": _CANDIDATE.to_value(),
            "dependency_id": "luanti-native-toolchain",
            "semantic_version": "3.0",
            "runtime_root": "private/dependencies/luanti-native/3.0",
            "runtime_manifest_fingerprint": runtime_digest,
            "language_standard": "c++17",
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    adapter = _write(
        root,
        _ADAPTER,
        (capsule.reference, irrlicht_artifact.reference),
        {
            "candidate": _CANDIDATE.to_value(),
            "module_selector": _MODULE,
            "f2p_method_selector": f"{_MODULE}.{_F2P_METHOD}",
            "p2p_method_selector": f"{_MODULE}.{_P2P_METHOD}",
            "adapter_kind": "module_run_method_receipt_filter",
            "source_test_unchanged": True,
            "module_command": ["--run-unittests", "--test-module", _MODULE],
            "method_evidence_rule": "exactly_one_[PASS|FAIL]_line_for_selected_method",
            "compiler_launcher_path": launcher.relative_to(root).as_posix(),
            "compiler_launcher_fingerprint": launcher_fingerprint,
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    return {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "capsule": capsule.reference.artifact_id,
        "irrlicht": irrlicht_artifact.reference.artifact_id,
        "toolchain": toolchain.reference.artifact_id,
        "selector_adapter": adapter.reference.artifact_id,
        "merge_commit": _MERGE_COMMIT,
        "base_commit": _BASE_COMMIT,
    }


def _sanitize_output(value: bytes) -> bytes:
    value = _MODULE_SUMMARY.sub(b"", value)
    value = _OUTER_SUMMARY.sub(b"", value)
    return value


def _selector_process_runner(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> NativeProcessReceipt:
    if len(argv) != 4 or argv[1:3] != ("--run-unittests", "--test-module"):
        return run_bounded_process(
            argv, cwd=cwd, env=env, timeout_seconds=timeout_seconds
        )
    selector = argv[3]
    module, separator, method = selector.partition(".")
    if (
        module != _MODULE
        or separator != "."
        or method not in {_F2P_METHOD, _P2P_METHOD}
    ):
        return NativeProcessReceipt(
            "launch_failed", None, b"", b"#14072 selector is not registered"
        )
    receipt = run_bounded_process(
        (argv[0], "--run-unittests", "--test-module", _MODULE),
        cwd=cwd,
        env=env,
        timeout_seconds=timeout_seconds,
    )
    if receipt.outcome != "completed":
        return receipt
    output = receipt.stdout + b"\n" + receipt.stderr
    records = [
        match
        for match in _METHOD_RESULT.finditer(output)
        if match.group(2).decode("ascii") == method
    ]
    if len(records) != 1:
        return NativeProcessReceipt(
            "completed",
            2,
            _sanitize_output(receipt.stdout),
            _sanitize_output(receipt.stderr)
            + b"\n#14072 selector adapter found no unique method receipt\n",
            receipt.stdout_truncated,
            receipt.stderr_truncated,
        )
    passed = records[0].group(1) == b"PASS"
    summary = (
        b"\nUnit Test Results: "
        + (b"PASSED" if passed else b"FAILED")
        + b"\n    "
        + (b"0" if passed else b"1")
        + b" / 1 failed tests.\n"
    )
    return NativeProcessReceipt(
        "completed",
        0 if passed else 1,
        _sanitize_output(receipt.stdout),
        _sanitize_output(receipt.stderr) + summary,
        receipt.stdout_truncated,
        receipt.stderr_truncated,
    )


def _fresh_namespace_is_absent(root: Path) -> None:
    for relative in (
        f"artifacts/native_regression_build/luanti/14072/fixed/configure/{_EXECUTION_NAMESPACE}/3.0/head.json",
        f"artifacts/native_regression_build/luanti/14072/fixed/build/{_EXECUTION_NAMESPACE}/3.0/head.json",
        f"artifacts/native_regression_attempt/luanti/14072/f2p/fixed/1/{_EXECUTION_NAMESPACE}/3.0/head.json",
        f"artifacts/native_regression_aggregate/luanti/14072/fixed-buggy/{_EXECUTION_NAMESPACE}/3.0/head.json",
    ):
        if (root / relative).exists():
            raise RuntimeError(
                f"#14072 fresh namespace already has receipts: {relative}"
            )


def _planner(
    root: Path,
    *,
    candidate: CandidateKey,
    dependency_references: tuple[ArtifactReference, ...],
) -> NativeRegressionExecutionPlan | StageDecision:
    plan = plan_luanti_native_regression_execution(root, candidate=candidate)
    if isinstance(plan, StageDecision):
        return plan
    irrlicht = root / _IRRLICHT_REL
    if (
        not irrlicht.is_dir()
        or irrlicht.is_symlink()
        or not (irrlicht / "irrlicht.h").is_file()
    ):
        return StageDecision.terminal(
            "infrastructure_blocked",
            {
                "profile_id": _PROFILE_ID,
                "reason": "registered_irrlichtmt_include_closure_missing",
            },
        )
    launcher, _launcher_fingerprint = _ensure_launcher(root)
    configure_base = list(plan.configure_argv)
    build_directory = f"build-luanti-14072-{_EXECUTION_NAMESPACE}"
    build_index = configure_base.index("-B")
    configure_base[build_index + 1] = build_directory
    configure = tuple(configure_base) + (
        "-DCMAKE_CXX_STANDARD=17",
        "-DCMAKE_CXX_STANDARD_REQUIRED=TRUE",
        "-DCMAKE_CXX_EXTENSIONS=OFF",
        "-DCMAKE_CXX_FLAGS=-w",
        f"-DIRRLICHT_INCLUDE_DIR={irrlicht}",
        f"-DCMAKE_CXX_COMPILER_LAUNCHER={launcher}",
    )
    return replace(
        plan,
        configure_argv=configure,
        build_argv=(
            plan.build_argv[0],
            "--build",
            build_directory,
            "--target",
            "minetestserver",
            "--parallel",
            "1",
        ),
        execution_namespace=_EXECUTION_NAMESPACE,
        dependency_references=dependency_references,
    )


def _write_downstream_receipts(
    root: Path,
    *,
    native: StableArtifact,
    adapter: StableArtifact,
    irrlicht: StableArtifact,
    toolchain: StableArtifact,
    capsule: StableArtifact,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact]:
    qualified = native.payload.get("qualified") is True
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "state": "qualified" if qualified else "semantic_mismatch",
        "outcome": "gate57_qualified" if qualified else "candidate_semantic_mismatch",
        "terminal": not qualified,
        "resumable": False if not qualified else True,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "old_attempts_loaded": False,
        "attempt_count": native.payload.get("attempt_count"),
        "stable_repetitions": native.payload.get("stable_repetitions"),
        "all_expected": native.payload.get("all_expected"),
        "qualified": qualified,
        "selector_adapter": adapter.reference.artifact_id,
        "native_aggregate": native.reference.artifact_id,
        "cells": native.payload.get("cells"),
        "current_gate_status": {
            "gate_5_fixed_f2p_3x": "passed" if qualified else "failed",
            "gate_6_buggy_f2p_3x_failure": "passed" if qualified else "failed",
            "gate_7_fixed_buggy_p2p_3x": "passed" if qualified else "failed",
        },
    }
    aggregate = _write(
        root,
        _AGGREGATE,
        (
            native.reference,
            adapter.reference,
            irrlicht.reference,
            toolchain.reference,
            capsule.reference,
        ),
        payload,
        authorization,
    )
    transition_payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "state": "gate57_passed" if qualified else "terminal_semantic_mismatch",
        "outcome": payload["outcome"],
        "terminal": payload["terminal"],
        "resumable": payload["resumable"],
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "aggregate": aggregate.reference.artifact_id,
        "selector_adapter": adapter.reference.artifact_id,
        "native_aggregate": native.reference.artifact_id,
        "deferred_until_after_gate57": [
            "runtime_world_replay",
            "semantic_observations",
            "golden_cross_judgment",
            "construction_receipt",
        ],
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    transition = _write(
        root,
        _TRANSITION,
        (aggregate.reference,),
        transition_payload,
        authorization,
    )
    return aggregate, transition


def _write_selector_evidence(
    root: Path,
    *,
    native: StableArtifact,
    adapter: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist method-level evidence when one module contains both cells."""

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for reference in native.inputs:
        attempt = _load(root, reference)
        payload = attempt.payload
        output = b"".join(
            base64.b64decode(payload[key]) for key in ("stdout_base64", "stderr_base64")
        )
        method_lines = [
            f"{match.group(1).decode('ascii')}:{match.group(2).decode('ascii')}"
            for match in _METHOD_RESULT.finditer(output)
        ]
        selected_method = payload["selector"].split(".", 1)[1]
        groups.setdefault((payload["phase"], payload["variant"]), []).append(
            {
                "repetition": payload["repetition"],
                "selector": payload["selector"],
                "observed_outcome": payload["observed_outcome"],
                "method_lines": method_lines,
                "selected_method_lines": [
                    line
                    for line in method_lines
                    if line.endswith(f":{selected_method}")
                ],
                "matched_expected": payload["matched_expected"],
            }
        )
    selected = {
        f"{phase}_{variant}": sorted(values, key=lambda value: value["repetition"])
        for (phase, variant), values in sorted(groups.items())
    }
    return _write(
        root,
        _SELECTOR_EVIDENCE,
        (native.reference, adapter.reference),
        {
            "candidate": _CANDIDATE.to_value(),
            "profile_id": _PROFILE_ID,
            "selector_adapter": adapter.reference.artifact_id,
            "module_selector": _MODULE,
            "f2p_method_selector": f"{_MODULE}.{_F2P_METHOD}",
            "p2p_method_selector": f"{_MODULE}.{_P2P_METHOD}",
            "selected_results": selected,
            "method_receipt_rule": "exactly_one_selected_method_line_per_attempt",
            "unselected_buggy_p2p_failure_allowed": True,
            "unselected_buggy_p2p_failure_method": _F2P_METHOD,
            "old_attempts_loaded": False,
            "candidate_execution_performed": True,
            "qualified": native.payload.get("qualified") is True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )


def _write_blocker(
    root: Path,
    *,
    authorization: AuthorizationRecord,
    error: Exception,
    phase: str,
) -> StableArtifact:
    return _write(
        root,
        _BLOCKER,
        (
            _SOURCE,
            _PATCH,
            _ATOMICITY,
            _MANIFEST,
            _CAPSULE,
            _IRRLICHT,
            _TOOLCHAIN,
            _ADAPTER,
        ),
        {
            "candidate": _CANDIDATE.to_value(),
            "profile_id": _PROFILE_ID,
            "state": "blocked",
            "outcome": "infrastructure_blocked",
            "terminal": False,
            "resumable": True,
            "candidate_execution_performed": phase == "execution",
            "old_attempts_loaded": False,
            "phase": phase,
            "error_type": type(error).__name__,
            "error": str(error)[:1000],
            "required_attempt_count": 12,
            "resource_policy": {
                "network": False,
                "paid": False,
                "formal_trials": False,
            },
        },
        authorization,
    )


def execute(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> dict[str, object]:
    """Run a fresh 12-cell native epoch with method-level selector receipts."""

    _require_scope(authorization, "preannotation-execution")
    root = Path(artifact_root).absolute().resolve(strict=True)
    capsule = _load(root, _CAPSULE)
    irrlicht = _load(root, _IRRLICHT)
    toolchain = _load(root, _TOOLCHAIN)
    adapter = _load(root, _ADAPTER)
    _fresh_namespace_is_absent(root)
    deps = (toolchain.reference, irrlicht.reference, adapter.reference)
    try:
        result = execute_luanti_native_regression(
            root,
            candidate=_CANDIDATE,
            authorization=authorization,
            confirmation=EXECUTION_CONFIRMATION,
            planner=lambda path, *, candidate: _planner(
                path, candidate=candidate, dependency_references=deps
            ),
            process_runner=_selector_process_runner,
        )
    except Exception as error:
        blocker = _write_blocker(
            root, authorization=authorization, error=error, phase="execution"
        )
        return {
            "outcome": "infrastructure_blocked",
            "blocker": blocker.reference.artifact_id,
            "error": str(error),
        }
    native = _load(root, _NATIVE_AGGREGATE)
    selector_evidence = _write_selector_evidence(
        root, native=native, adapter=adapter, authorization=authorization
    )
    aggregate, transition = _write_downstream_receipts(
        root,
        native=native,
        adapter=adapter,
        irrlicht=irrlicht,
        toolchain=toolchain,
        capsule=capsule,
        authorization=authorization,
    )
    return {
        "outcome": aggregate.payload["outcome"],
        "qualified": aggregate.payload["qualified"],
        "native_aggregate": native.reference.artifact_id,
        "aggregate": aggregate.reference.artifact_id,
        "transition": transition.reference.artifact_id,
        "selector_evidence": selector_evidence.reference.artifact_id,
        "executed_attempts": result.executed_attempts,
        "resumed_attempts": result.resumed_attempts,
        "cells": aggregate.payload["cells"],
    }


def _authorization(scope: str, authorization_id: str) -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=authorization_id,
        scope=scope,
        granted_at=_now(),
        local_user_id=os.getuid(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, default=_ROOT_DEFAULT)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.prepare and not args.execute:
        parser.error("choose --prepare or --execute")
    authorization = _authorization(
        "preannotation-execution", "authorization/luanti-14072-gate57-v2"
    )
    result: dict[str, object] = {}
    if args.prepare:
        result["prepare"] = prepare(args.artifact_root, authorization=authorization)
    if args.execute:
        result["execute"] = execute(args.artifact_root, authorization=authorization)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
