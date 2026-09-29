"""Fresh exact-method Gate 5--7 rerun for Luanti #12602.

The historical #12602 binary predates ``--test-module``.  The v9 runner
therefore classified the whole ``TestServerModManager`` module, which made
the F2P failure contaminate the buggy P2P cell.  This adapter starts a fresh
process for every cell and classifies only the unique named ``[PASS]`` or
``[FAIL]`` line for the registered method.  It does not reinterpret old
output and it records that the process itself still executes the historical
full suite.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .luanti_12602_gate57 import _load_chain
from .luanti_native_executor import run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_12602_EXACT_METHOD_GATE57_V10"
_VERSION = "1.0"
_EPOCH = "candidate_gate57_12602_exact_method_v10"
_PROFILE = "luanti-12602-media-override-v1"
_CANDIDATE = {"repository_key": "luanti", "change_number": 12602}
_SOURCE_ROOT = PurePosixPath("private/bounded-runs/luanti/12602/gate57-v9")
_RUN_ROOT = PurePosixPath("private/bounded-runs/luanti/12602/gate57-v10-exact-method")
_HOME_ROOT = PurePosixPath("private/runtime-home/luanti-12602-exact-method-gate57-v10")
_BINARY = PurePosixPath("bin/minetestserver")
_TOOLCHAIN = PurePosixPath("private/dependencies/luanti-native/3.0")
_MAX_OUTPUT = 262_144

_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/12602/fixed/v2", "fixed_source_capsule", "2.0"
)
_IRRLICHT = ArtifactReference(
    "candidate_gate57_12602_irrlicht_dependency/luanti/12602/native/v1",
    "candidate_gate57_12602_irrlicht_dependency",
    _VERSION,
)
_TOOLCHAIN_RECEIPT = ArtifactReference(
    "candidate_gate57_12602_cxx17_toolchain/luanti/12602/native/v9",
    "candidate_gate57_12602_cxx17_toolchain",
    _VERSION,
)
_BUILD_KIND = "candidate_gate57_12602_build_v9"
_ATTEMPT_KIND = "candidate_gate57_12602_exact_method_attempt"
_AGGREGATE = ArtifactReference(
    "candidate_gate57_12602_exact_method_aggregate/luanti/12602/native/v10",
    "candidate_gate57_12602_exact_method_aggregate",
    _VERSION,
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_12602_exact_method_transition/luanti/12602/native/v10",
    "candidate_gate57_12602_exact_method_transition",
    _VERSION,
)
_RUNS = (
    ("f2p", "testGetModMediaPaths", "pass", "failure"),
    ("p2p", "testGetModNames", "pass", "pass"),
)


def execute_luanti_12602_exact_method_gate57(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
) -> tuple[StableArtifact, StableArtifact]:
    """Execute a fresh 12-cell matrix with an exact named-method oracle."""

    _require_authorization(authorization)
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    capsule = _load(root, _CAPSULE)
    irrlicht = _load(root, _IRRLICHT)
    toolchain = _load(root, _TOOLCHAIN_RECEIPT)
    builds = {variant: _load_build(root, variant) for variant in ("fixed", "buggy")}
    trees = _materialize_runtime_pair(root)

    attempts: list[StableArtifact] = []
    common_inputs = tuple(item.reference for item in chain) + (
        capsule.reference,
        irrlicht.reference,
        toolchain.reference,
    )
    for phase, method, fixed_expected, buggy_expected in _RUNS:
        for variant, expected in (
            ("fixed", fixed_expected),
            ("buggy", buggy_expected),
        ):
            for repetition in (1, 2, 3):
                attempts.append(
                    _run_cell(
                        root,
                        trees[variant],
                        builds[variant],
                        common_inputs,
                        phase,
                        method,
                        variant,
                        repetition,
                        expected,
                        authorization,
                    )
                )

    aggregate = _write_aggregate(root, common_inputs, builds, attempts, authorization)
    transition = _write_transition(
        root, common_inputs, aggregate, attempts, authorization
    )
    return aggregate, transition


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _load_build(root: Path, variant: str) -> StableArtifact:
    reference = ArtifactReference(
        f"{_BUILD_KIND}/luanti/12602/{variant}/build/v1",
        _BUILD_KIND,
        _VERSION,
    )
    artifact = _load(root, reference)
    if (
        artifact.payload.get("process_outcome") != "completed"
        or artifact.payload.get("returncode") != 0
    ):
        raise RuntimeError(f"#12602 v9 {variant} build is not qualified")
    return artifact


def _materialize_runtime_pair(root: Path) -> dict[str, Path]:
    source_root = root / _SOURCE_ROOT
    run_root = root / _RUN_ROOT
    if run_root.exists():
        raise RuntimeError("#12602 exact-method runtime namespace is not fresh")
    trees: dict[str, Path] = {}
    try:
        for variant in ("fixed", "buggy"):
            source = source_root / variant
            binary = source / _BINARY
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise RuntimeError(f"#12602 v9 {variant} binary is unavailable")
            destination = run_root / variant
            shutil.copytree(source, destination, symlinks=True)
            copied_binary = destination / _BINARY
            if not copied_binary.is_file() or not os.access(copied_binary, os.X_OK):
                raise RuntimeError(f"#12602 copied {variant} binary is unavailable")
            trees[variant] = destination
    except BaseException:
        if run_root.exists():
            shutil.rmtree(run_root)
        raise
    return trees


def _run_cell(
    root: Path,
    tree: Path,
    build: StableArtifact,
    common_inputs: tuple[ArtifactReference, ...],
    phase: str,
    method: str,
    variant: str,
    repetition: int,
    expected: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    home = root / _HOME_ROOT / phase / variant / str(repetition)
    home.mkdir(parents=True, exist_ok=False)
    argv = (str(tree / _BINARY), "--run-unittests")
    env = {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(root / _TOOLCHAIN / "bin"),
        "GIT_DIR": "/dev/null",
    }
    receipt = run_bounded_process(argv, cwd=tree, env=env, timeout_seconds=300)
    pattern = re.compile(
        rb"(?m)^\[(PASS|FAIL)\] "
        + re.escape(method.encode("ascii"))
        + rb" - [^\r\n]+\r?$"
    )
    matches = [
        match
        for stream in (receipt.stdout, receipt.stderr)
        for match in pattern.finditer(stream)
    ]
    if receipt.outcome == "timed_out":
        observed = "timed_out"
    elif receipt.outcome == "launch_failed":
        observed = "launch_failed"
    elif receipt.outcome != "completed":
        observed = "unknown"
    elif len(matches) != 1:
        observed = "invalid_test_count"
    else:
        observed = "pass" if matches[0].group(1) == b"PASS" else "failure"

    stdout, stdout_truncated = _bounded(receipt.stdout)
    stderr, stderr_truncated = _bounded(receipt.stderr)
    reference = ArtifactReference(
        f"{_ATTEMPT_KIND}/luanti/12602/{phase}/{variant}/{repetition}/v10",
        _ATTEMPT_KIND,
        _VERSION,
    )
    payload: dict[str, object] = {
        "candidate": _CANDIDATE,
        "profile_id": _PROFILE,
        "fresh_epoch": _EPOCH,
        "phase": phase,
        "variant": variant,
        "repetition": repetition,
        "selector": f"TestServerModManager.{method}",
        "selector_mode": "exact_named_test_line_fresh_process",
        "historical_binary_executes_full_suite": True,
        "exact_method_oracle_isolated": True,
        "expected_outcome": expected,
        "observed_outcome": observed,
        "matched_expected": observed == expected,
        "selected_test_summary_count": len(matches),
        "completed_test_count": 1 if len(matches) == 1 else 0,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "argv": list(argv),
        "tree_path": tree.relative_to(root).as_posix(),
        "build_reference": build.reference.artifact_id,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated or stdout_truncated,
        "stderr_truncated": receipt.stderr_truncated or stderr_truncated,
        "output_encoding": "base64",
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(
        root,
        reference,
        common_inputs + (build.reference,),
        payload,
        authorization,
    )


def _write_aggregate(
    root: Path,
    common_inputs: tuple[ArtifactReference, ...],
    builds: dict[str, StableArtifact],
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for attempt in attempts:
        key = (str(attempt.payload["phase"]), str(attempt.payload["variant"]))
        groups.setdefault(key, []).append(attempt)
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
        raise RuntimeError("#12602 exact-method attempt coverage is incomplete")
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
    payload: dict[str, object] = {
        "candidate": _CANDIDATE,
        "profile_id": _PROFILE,
        "fresh_epoch": _EPOCH,
        "state": "qualified" if qualified else "blocked",
        "outcome": outcome,
        "qualified": qualified,
        "terminal": outcome == "candidate_semantic_mismatch",
        "resumable": outcome != "candidate_semantic_mismatch",
        "attempt_count": len(attempts),
        "expected_attempt_count": 12,
        "repetitions_per_cell": 3,
        "stable_repetitions": stable,
        "all_expected": all_expected,
        "no_timeout_or_unknown": not infrastructure,
        "selector_mode": "exact_named_test_line_fresh_process",
        "historical_binary_executes_full_suite": True,
        "exact_method_oracle_isolated": True,
        "old_attempts_loaded": False,
        "build_references": {
            variant: build.reference.artifact_id for variant, build in builds.items()
        },
        "cells": [
            {
                "phase": phase,
                "variant": variant,
                "observed_outcomes": [
                    item.payload["observed_outcome"]
                    for item in sorted(
                        groups[(phase, variant)],
                        key=lambda value: int(value.payload["repetition"]),
                    )
                ],
                "matched_expected": [
                    item.payload["matched_expected"]
                    for item in sorted(
                        groups[(phase, variant)],
                        key=lambda value: int(value.payload["repetition"]),
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
        common_inputs
        + tuple(build.reference for build in builds.values())
        + tuple(attempt.reference for attempt in attempts)
    )
    return _write(root, _AGGREGATE, inputs, payload, authorization)


def _write_transition(
    root: Path,
    common_inputs: tuple[ArtifactReference, ...],
    aggregate: StableArtifact,
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    qualified = aggregate.payload["qualified"] is True
    payload: dict[str, object] = {
        "candidate": _CANDIDATE,
        "profile_id": _PROFILE,
        "fresh_epoch": _EPOCH,
        "state": "passed" if qualified else "blocked",
        "outcome": aggregate.payload["outcome"],
        "aggregate_reference": aggregate.reference.artifact_id,
        "attempt_count": len(attempts),
        "selector_mode": "exact_named_test_line_fresh_process",
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
    return _write(
        root,
        _TRANSITION,
        common_inputs
        + (aggregate.reference,)
        + tuple(attempt.reference for attempt in attempts),
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
    artifact = _load(root, reference)
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("#12602 exact-method typed receipt reload mismatch")
    return artifact


def _bounded(value: bytes) -> tuple[bytes, bool]:
    return value[:_MAX_OUTPUT], len(value) > _MAX_OUTPUT


def _require_authorization(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("#12602 exact-method authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CONFIRMATION", "execute_luanti_12602_exact_method_gate57"]
