"""Superseding #14464 native Gate 5--7 run after a stale target discovery.

The first bounded attempt proved that configure was healthy but used the
wrong executable target (``luantiserver``).  This retry keeps that receipt,
uses the source-declared ``minetestserver`` target, and writes all new build
and attempt evidence under a candidate-specific namespace.  No old attempt or
aggregate is loaded.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from .luanti_native_executor import (
    NativeProcessReceipt,
    _apply_production_inverse,
    _copy_full_capsule,
    _new_workspace,
    _remove_workspace,
    completed_native_test_count,
    run_bounded_process,
)
from .luanti_native_regression import (
    NativeRegressionRun,
    plan_luanti_native_regression_execution,
)
from .luanti_14464_gate57 import (
    _CANDIDATE,
    _CHAIN,
    _load,
    _load_and_validate_chain,
    _require_authorization,
)
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_14464_FRESH_NATIVE_GATE57_RETRY"
_CANDIDATE_KEY = CandidateKey("luanti", 14464)
_BUILD_KIND = "candidate_gate57_14464_build"
_ATTEMPT_KIND = "candidate_gate57_14464_attempt"
_BUILD_VERSION = "1.0"
_ATTEMPT_VERSION = "1.0"
_AGGREGATE = ArtifactReference(
    "candidate_gate57_14464_fresh_retry_aggregate/luanti/14464/native/v1",
    "candidate_gate57_14464_fresh_retry_aggregate",
    "1.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_14464_fresh_retry_transition/luanti/14464/native/v1",
    "candidate_gate57_14464_fresh_retry_transition",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_14464_fresh_retry_blocker/luanti/14464/native/v1",
    "candidate_gate57_14464_fresh_retry_blocker",
    "1.0",
)
_MAX_OUTPUT = 262_144


def execute_luanti_14464_gate57_retry(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
) -> tuple[StableArtifact, StableArtifact]:
    """Build the corrected target and execute the exact 12-cell fresh epoch."""

    _require_authorization(authorization)
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_and_validate_chain(root)
    _assert_retry_namespace(root)
    base = plan_luanti_native_regression_execution(root, candidate=_CANDIDATE_KEY)
    if not hasattr(base, "configure_argv"):
        raise RuntimeError("#14464 corrected native plan is unavailable")
    runs = tuple(
        NativeRegressionRun(
            phase=run.phase,
            selector=run.selector,
            executable_path="bin/minetestserver",
            argv=("bin/minetestserver", "--run-unittests", "--test-module", run.selector),
            repetitions=run.repetitions,
            fixed_expected_outcome=run.fixed_expected_outcome,
            buggy_expected_outcome=run.buggy_expected_outcome,
        )
        for run in base.runs
    )
    plan = replace(
        base,
        build_argv=(
            base.toolchain.cmake,
            "--build",
            "build-luanti-14464",
            "--target",
            "minetestserver",
            "--parallel",
            "1",
        ),
        runs=runs,
    )
    workspace = _new_workspace(root, _CANDIDATE_KEY)
    try:
        fixed = workspace / "fixed"
        buggy = workspace / "buggy"
        _copy_full_capsule(plan, fixed)
        _copy_full_capsule(plan, buggy)
        _apply_production_inverse(plan, fixed, buggy)
        builds = {
            variant: _build_variant(
                root,
                plan,
                variant=variant,
                tree=tree,
                authorization=authorization,
            )
            for variant, tree in (("fixed", fixed), ("buggy", buggy))
        }
        attempts = []
        for run in plan.runs:
            for variant, tree in (("fixed", fixed), ("buggy", buggy)):
                for repetition in range(1, 4):
                    attempts.append(
                        _run_attempt(
                            root,
                            plan,
                            run,
                            variant=variant,
                            repetition=repetition,
                            tree=tree,
                            build=builds[variant],
                            authorization=authorization,
                        )
                    )
        aggregate = _write_aggregate(root, chain, plan, builds, attempts, authorization)
        transition = _write_transition(root, chain, aggregate, authorization)
        return aggregate, transition
    except BaseException as error:
        blocker = _write_blocker(root, chain, error, authorization)
        return blocker, blocker
    finally:
        _remove_workspace(workspace, root)


def _build_variant(
    root: Path,
    plan: object,
    *,
    variant: str,
    tree: Path,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    # Keep the successful stale-target configure receipt as a superseded input
    # when available, but never overwrite it.
    configure_argv = tuple(str(value) for value in plan.configure_argv)
    build_argv = tuple(str(value) for value in plan.build_argv)
    configure = run_bounded_process(
        configure_argv,
        cwd=tree,
        env=dict(plan.closed_environment),
        timeout_seconds=600,
    )
    configure_ref = ArtifactReference(
        f"{_BUILD_KIND}/luanti/14464/{variant}/configure/{_BUILD_VERSION}",
        _BUILD_KIND,
        _BUILD_VERSION,
    )
    configure_artifact = _write_build(
        root,
        configure_ref,
        variant=variant,
        step="configure",
        argv=configure_argv,
        receipt=configure,
        inputs=(_CHAIN[-1],),
        authorization=authorization,
        supersedes=(
            "native_regression_build/luanti/14464/fixed/configure/v3"
            if variant == "fixed"
            else None
        ),
    )
    if configure.outcome != "completed" or configure.returncode != 0:
        raise RuntimeError(f"#14464 {variant} corrected configure failed")
    build = run_bounded_process(
        build_argv,
        cwd=tree,
        env=dict(plan.closed_environment),
        timeout_seconds=1800,
    )
    build_ref = ArtifactReference(
        f"{_BUILD_KIND}/luanti/14464/{variant}/build/{_BUILD_VERSION}",
        _BUILD_KIND,
        _BUILD_VERSION,
    )
    artifact = _write_build(
        root,
        build_ref,
        variant=variant,
        step="build",
        argv=build_argv,
        receipt=build,
        inputs=(configure_artifact.reference,),
        authorization=authorization,
        supersedes=None,
    )
    binary = tree / "bin/minetestserver"
    if build.outcome != "completed" or build.returncode != 0 or not binary.is_file():
        raise RuntimeError(f"#14464 {variant} corrected build failed")
    return artifact


def _run_attempt(
    root: Path,
    plan: object,
    run: NativeRegressionRun,
    *,
    variant: Literal["fixed", "buggy"],
    repetition: int,
    tree: Path,
    build: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    receipt = run_bounded_process(
        (str(tree / run.executable_path), *run.argv[1:]),
        cwd=tree,
        env=dict(plan.closed_environment),
        timeout_seconds=300,
    )
    count = completed_native_test_count(
        run, receipt.outcome, receipt.stdout, receipt.stderr
    )
    observed = _observed(receipt, count)
    expected = (
        run.fixed_expected_outcome
        if variant == "fixed"
        else run.buggy_expected_outcome
    )
    reference = ArtifactReference(
        f"{_ATTEMPT_KIND}/luanti/14464/{run.phase}/{variant}/{repetition}/{_ATTEMPT_VERSION}",
        _ATTEMPT_KIND,
        _ATTEMPT_VERSION,
    )
    payload = {
        "candidate": _CANDIDATE,
        "phase": run.phase,
        "variant": variant,
        "repetition": repetition,
        "selector": run.selector,
        "expected_outcome": expected,
        "observed_outcome": observed,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "completed_test_count": count,
        "stdout_base64": base64.b64encode(receipt.stdout[:_MAX_OUTPUT]).decode(),
        "stderr_base64": base64.b64encode(receipt.stderr[:_MAX_OUTPUT]).decode(),
        "stdout_truncated": receipt.stdout_truncated or len(receipt.stdout) > _MAX_OUTPUT,
        "stderr_truncated": receipt.stderr_truncated or len(receipt.stderr) > _MAX_OUTPUT,
        "output_encoding": "base64",
        "matched_expected": observed == expected,
        "build_reference": build.reference.artifact_id,
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=(build.reference,),
        payload=payload,
        authorization=authorization,
    )
    return _load(root, reference)


def _write_build(
    root: Path,
    reference: ArtifactReference,
    *,
    variant: str,
    step: str,
    argv: tuple[str, ...],
    receipt: NativeProcessReceipt,
    inputs: tuple[ArtifactReference, ...],
    authorization: AuthorizationRecord,
    supersedes: str | None,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE,
        "variant": variant,
        "step": step,
        "argv": list(argv),
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(receipt.stdout[:_MAX_OUTPUT]).decode(),
        "stderr_base64": base64.b64encode(receipt.stderr[:_MAX_OUTPUT]).decode(),
        "stdout_truncated": receipt.stdout_truncated or len(receipt.stdout) > _MAX_OUTPUT,
        "stderr_truncated": receipt.stderr_truncated or len(receipt.stderr) > _MAX_OUTPUT,
        "output_encoding": "base64",
        "target": "minetestserver",
        "supersedes": supersedes,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
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
    return _load(root, reference)


def _write_aggregate(
    root: Path,
    chain: tuple[StableArtifact, ...],
    plan: object,
    builds: dict[str, StableArtifact],
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 12:
        raise RuntimeError("#14464 retry attempt coverage is incomplete")
    groups = {
        (phase, variant): [
            item
            for item in attempts
            if item.payload["phase"] == phase and item.payload["variant"] == variant
        ]
        for phase in ("f2p", "p2p")
        for variant in ("fixed", "buggy")
    }
    stable = all(len(group) == 3 and len({item.payload["observed_outcome"] for item in group}) == 1 for group in groups.values())
    expected = all(item.payload["matched_expected"] is True and item.payload["process_outcome"] == "completed" for item in attempts)
    payload = {
        "candidate": _CANDIDATE,
        "state": "qualified" if stable and expected else "blocked",
        "outcome": "qualified" if stable and expected else "candidate_semantic_mismatch",
        "qualified": stable and expected,
        "terminal": stable and not expected,
        "resumable": not (stable and not expected),
        "attempt_count": len(attempts),
        "repetitions_per_cell": 3,
        "stable_repetitions": stable,
        "all_expected": expected,
        "old_attempts_loaded": False,
        "truth_table": {"fixed": {"f2p": "pass", "p2p": "pass"}, "buggy": {"f2p": "failure", "p2p": "pass"}},
        "cells": [
            {"phase": phase, "variant": variant, "observed_outcomes": [item.payload["observed_outcome"] for item in sorted(group, key=lambda item: item.payload["repetition"])]}
            for (phase, variant), group in groups.items()
        ],
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in chain) + tuple(builds.values()) + tuple(attempt.reference for attempt in attempts)
    write_stable_artifact(root, artifact_id=_AGGREGATE.artifact_id, artifact_kind=_AGGREGATE.artifact_kind, semantic_version=_AGGREGATE.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    return _load(root, _AGGREGATE)


def _write_transition(root: Path, chain: tuple[StableArtifact, ...], aggregate: StableArtifact, authorization: AuthorizationRecord) -> StableArtifact:
    qualified = aggregate.payload["qualified"] is True
    payload = {
        "candidate": _CANDIDATE,
        "state": "passed" if qualified else "blocked",
        "outcome": "qualified" if qualified else "candidate_semantic_mismatch",
        "aggregate_reference": aggregate.reference.artifact_id,
        "gate_1_7_status": {
            "source_authority": "passed", "single_defect_mapping": "passed", "strict_inverse_patch": "passed", "test_api_compatibility": "passed",
            "fixed_f2p_3x": "passed" if qualified else "failed", "buggy_f2p_3x": "passed_clean_failure" if qualified else "failed",
            "fixed_p2p_3x": "passed" if qualified else "failed", "buggy_p2p_3x": "passed" if qualified else "failed",
        },
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "deferred_until_after_gate5_7": ["gate8_full_versions", "gate9_start_replay_oracle_observations", "gate10_goldens", "gate11_visual_cross_judgment", "gate12_same_lineage_receipt", "gate13_freeze"],
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    write_stable_artifact(root, artifact_id=_TRANSITION.artifact_id, artifact_kind=_TRANSITION.artifact_kind, semantic_version=_TRANSITION.semantic_version, created_at=_now(), inputs=tuple(item.reference for item in chain) + (aggregate.reference,), payload=payload, authorization=authorization)
    return _load(root, _TRANSITION)


def _write_blocker(root: Path, chain: tuple[StableArtifact, ...], error: BaseException, authorization: AuthorizationRecord) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE, "state": "blocked", "outcome": "infrastructure_blocked", "terminal": False, "resumable": True,
        "reason": "corrected_native_build_or_execution_failed", "error_type": type(error).__name__, "error": str(error),
        "attempt_count": 0, "expected_attempt_count": 12, "old_attempts_loaded": False,
        "candidate_execution_performed": False, "paid_actions_authorized": False, "formal_trials_authorized": False,
    }
    write_stable_artifact(root, artifact_id=_BLOCKER.artifact_id, artifact_kind=_BLOCKER.artifact_kind, semantic_version=_BLOCKER.semantic_version, created_at=_now(), inputs=tuple(item.reference for item in chain), payload=payload, authorization=authorization)
    return _load(root, _BLOCKER)


def _observed(receipt: NativeProcessReceipt, count: int | None) -> str:
    if receipt.outcome == "timed_out":
        return "timed_out"
    if receipt.outcome == "launch_failed":
        return "launch_failed"
    if receipt.outcome != "completed" or receipt.returncode is None:
        return "unknown"
    if count != 1:
        return "invalid_test_count"
    return "pass" if receipt.returncode == 0 else "failure"


def _assert_retry_namespace(root: Path) -> None:
    for reference in (_AGGREGATE, _TRANSITION, _BLOCKER):
        if (root / "artifacts" / Path(reference.artifact_id) / reference.semantic_version).exists():
            raise RuntimeError(f"#14464 retry artifact already exists: {reference.artifact_id}")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["CONFIRMATION", "execute_luanti_14464_gate57_retry"]
