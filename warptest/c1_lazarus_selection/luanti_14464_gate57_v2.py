"""Fresh, candidate-specific Gate 5--7 execution for Luanti #14464.

The first #14464 native attempt used the generic Luanti plan, which assumed
the newer ``luantiserver`` target.  The #14464 capsule is an older Minetest
tree whose registered server target is ``minetestserver``.  This adapter keeps
the current-v2 typed chain and the production-only inverse from the generic
profile, but gives the candidate a new, immutable artifact namespace and the
correct target/binary.  It always executes all twelve cells in this epoch; it
does not resume or inherit any old attempt.

This module is deliberately limited to Gate 5--7.  It performs no runtime,
replay, visual, construction, paid, or formal work.
"""

from __future__ import annotations

import base64
import os
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


CONFIRMATION = "EXECUTE_LUANTI_14464_NATIVE_GATE57_V2"
_CANDIDATE = CandidateKey("luanti", 14464)
_CANDIDATE_VALUE = _CANDIDATE.to_value()
_EPOCH = "candidate_gate57_14464_native_v2"
_VERSION = "1.0"
_ROOT_CHAIN = (
    ArtifactReference(
        "candidate_source_packet/luanti/14464/source/v2",
        "candidate_source_packet",
        "2.0",
    ),
    ArtifactReference(
        "candidate_test_patch/luanti/14464/screening/v2",
        "candidate_test_patch",
        "2.0",
    ),
    ArtifactReference(
        "source_atomicity/luanti/14464/preannotation/v2",
        "source_atomicity",
        "2.0",
    ),
    ArtifactReference(
        "candidate_stage/luanti/14464/test_manifest/v2",
        "candidate_stage",
        "2.0",
    ),
    ArtifactReference(
        "candidate_profile/luanti/14464/preflight/v2",
        "candidate_profile",
        "2.0",
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
        "fixed_source_capsule/luanti/14464/fixed/v2",
        "fixed_source_capsule",
        "2.0",
    ),
)
_BUILD_KIND = "candidate_gate57_14464_fresh_build"
_ATTEMPT_KIND = "candidate_gate57_14464_fresh_attempt"
_BUILD_ROOT = "build-luanti-14464-v2"
_BINARY = "bin/minetestserver"
_AGGREGATE = ArtifactReference(
    "candidate_gate57_14464_fresh_aggregate/luanti/14464/native/v2",
    "candidate_gate57_14464_fresh_aggregate",
    _VERSION,
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_14464_fresh_transition/luanti/14464/native/v2",
    "candidate_gate57_14464_fresh_transition",
    _VERSION,
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_14464_fresh_blocker/luanti/14464/native/v2",
    "candidate_gate57_14464_fresh_blocker",
    _VERSION,
)


def execute_luanti_14464_gate57_v2(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
) -> tuple[StableArtifact, StableArtifact]:
    """Build fixed/buggy trees and execute the exact twelve fresh cells.

    A configure/build/process infrastructure failure is represented by a
    typed non-terminal blocker.  Once all twelve cells have observations, an
    aggregate and transition are written; a clean expected truth table is the
    only way this epoch qualifies Gate 5--7.
    """

    _require_authorization(authorization)
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    _assert_fresh_namespace(root)
    plan = _candidate_plan(root)
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
                            authorization=authorization,
                        )
                    )
    except BaseException as error:
        blocker = _write_blocker(
            root,
            chain,
            error,
            builds=builds,
            attempts=attempts,
            process_started=process_started,
            authorization=authorization,
        )
        return blocker, blocker
    finally:
        _remove_workspace(workspace, root)

    aggregate = _write_aggregate(root, chain, plan, builds, attempts, authorization)
    transition = _write_transition(root, chain, aggregate, builds, attempts, authorization)
    return aggregate, transition


def _candidate_plan(root: Path) -> NativeRegressionExecutionPlan:
    decision = plan_luanti_native_regression_execution(root, candidate=_CANDIDATE)
    if isinstance(decision, StageDecision):
        raise RuntimeError(
            f"#14464 native plan is blocked: {decision.details.get('reason')}"
        )
    # The source's project() is ``minetest``.  Do not infer this from PATH or
    # from an older build receipt: bind both target and executable here.
    runs = tuple(
        replace(
            run,
            executable_path=_BINARY,
            argv=(_BINARY, "--run-unittests", "--test-module", run.selector),
        )
        for run in decision.runs
    )
    build_argv = tuple(
        "minetestserver" if value == "luantiserver" else value
        for value in decision.build_argv
    )
    plan = replace(decision, build_argv=build_argv, runs=runs)
    if plan.build_argv[-3] != "minetestserver" or any(
        run.executable_path != _BINARY for run in plan.runs
    ):
        raise RuntimeError("#14464 candidate-specific minetestserver binding drifted")
    return plan


def _load_chain(root: Path) -> tuple[StableArtifact, ...]:
    chain = tuple(
        load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
        for reference in _ROOT_CHAIN
    )
    source, patch, atomicity, manifest, profile, preparation, downstream, capsule = chain
    if any(item.payload.get("candidate") != _CANDIDATE_VALUE for item in chain):
        raise RuntimeError("#14464 current-v2 chain candidate binding drifted")
    if (
        source.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or atomicity.payload.get("outcome") != "accepted"
        or manifest.payload.get("state") != "passed"
        or profile.payload.get("strict_gate_status", {}).get("source_authority") != "passed"
        or profile.payload.get("strict_gate_status", {}).get("single_defect_mapping") != "passed"
        or profile.payload.get("strict_gate_status", {}).get("strict_inverse_patch") != "passed"
        or profile.payload.get("strict_gate_status", {}).get("test_api_compatibility") != "passed"
        or preparation.payload.get("state") != "prepared"
        or downstream.payload.get("state") != "planned"
        or capsule.payload.get("source_tree_path")
        != "private/fixed-capsules/luanti/14464/fixed-source"
    ):
        raise RuntimeError("#14464 current-v2 Gate 1--4 prerequisites are not passed")
    tree = root / "private/fixed-capsules/luanti/14464/fixed-source"
    for relative in (
        "src/content_nodemeta.cpp",
        "src/nodemetadata.cpp",
        "src/object_properties.cpp",
        "src/script/common/c_converter.cpp",
        "src/serialization.cpp",
        "src/util/serialize.h",
        "src/util/string.cpp",
        "src/unittest/test_utilities.cpp",
        "src/unittest/test_settings.cpp",
    ):
        path = tree / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"#14464 capsule file is missing or unsafe: {relative}")
    return chain


def _assert_fresh_namespace(root: Path) -> None:
    for reference in (_AGGREGATE, _TRANSITION, _BLOCKER):
        if (root / "artifacts" / Path(reference.artifact_id) / reference.semantic_version).exists():
            raise RuntimeError(f"#14464 fresh v2 artifact already exists: {reference.artifact_id}")
    # This namespace is deliberately independent of the failed generic v3
    # namespace and contains no historical attempts to resume.
    for kind in (_BUILD_KIND, _ATTEMPT_KIND):
        directory = root / "artifacts" / kind / "luanti" / "14464"
        if directory.exists() and any(directory.rglob("*.json")):
            raise RuntimeError(f"#14464 fresh v2 namespace is not empty: {kind}")


def _build_variant(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    *,
    variant: str,
    tree: Path,
    chain: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> ArtifactReference:
    build_dir = tree / _BUILD_ROOT
    env = dict(plan.closed_environment)
    # Keep the build directory and HOME inside the registered artifact root.
    _prepare_closed_home(root, Path(env["HOME"]))
    configure_argv = tuple(str(value) for value in plan.configure_argv)
    # The generic plan uses a relative build directory; bind it to this exact
    # candidate-specific v2 name while retaining all closed toolchain flags.
    configure_argv = tuple(
        _BUILD_ROOT if value == "build-luanti-14464" else value
        for value in configure_argv
    )
    build_argv = tuple(str(value) for value in plan.build_argv)
    build_argv = tuple(
        _BUILD_ROOT if value == "build-luanti-14464" else value
        for value in build_argv
    )
    inputs = tuple(item.reference for item in chain)
    last: ArtifactReference | None = None
    for step, argv, timeout in (
        ("configure", configure_argv, 600),
        ("build", build_argv, 1800),
    ):
        receipt = run_bounded_process(
            argv,
            cwd=tree,
            env=env,
            timeout_seconds=timeout,
        )
        reference = ArtifactReference(
            f"{_BUILD_KIND}/luanti/14464/{variant}/{step}/v2",
            _BUILD_KIND,
            _VERSION,
        )
        payload = _process_payload(
            variant=variant,
            step=step,
            argv=argv,
            receipt=receipt,
            candidate_execution_performed=False,
        )
        payload.update(
            {
                "candidate": _CANDIDATE_VALUE,
                "profile_id": plan.profile.profile_id,
                "fresh_epoch": _EPOCH,
                "tree_path": tree.relative_to(root).as_posix(),
                "binary_path": _BINARY,
                "old_attempts_loaded": False,
                "paid_actions_authorized": False,
                "formal_trials_authorized": False,
            }
        )
        _write(root, reference, inputs, payload, authorization)
        last = reference
        if receipt.outcome != "completed" or receipt.returncode != 0:
            raise RuntimeError(f"#14464 {variant} {step} failed")
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
    authorization: AuthorizationRecord,
) -> StableArtifact:
    expected = (
        run.fixed_expected_outcome
        if variant == "fixed"
        else run.buggy_expected_outcome
    )
    argv = (str(tree / run.executable_path), *run.argv[1:])
    receipt = run_bounded_process(
        argv,
        cwd=tree,
        env=dict(plan.closed_environment),
        timeout_seconds=300,
    )
    completed_count = completed_native_test_count(
        run, receipt.outcome, receipt.stdout, receipt.stderr
    )
    observed = _observed(receipt, completed_count)
    reference = ArtifactReference(
        f"{_ATTEMPT_KIND}/luanti/14464/{run.phase}/{variant}/{repetition}/v2",
        _ATTEMPT_KIND,
        _VERSION,
    )
    payload = {
        "candidate": _CANDIDATE_VALUE,
        "profile_id": plan.profile.profile_id,
        "fresh_epoch": _EPOCH,
        "phase": run.phase,
        "variant": variant,
        "repetition": repetition,
        "selector": run.selector,
        "executable_path": run.executable_path,
        "expected_outcome": expected,
        "observed_outcome": observed,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "completed_test_count": completed_count,
        "argv": list(argv),
        "stdout_base64": base64.b64encode(_bounded_output(receipt.stdout)[0]).decode("ascii"),
        "stderr_base64": base64.b64encode(_bounded_output(receipt.stderr)[0]).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated or len(receipt.stdout) > 262144,
        "stderr_truncated": receipt.stderr_truncated or len(receipt.stderr) > 262144,
        "output_encoding": "base64",
        "matched_expected": observed == expected,
        "build_reference": build_reference.artifact_id,
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(
        root,
        reference,
        tuple(item.reference for item in chain) + (build_reference,),
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
    plan: NativeRegressionExecutionPlan,
    builds: dict[str, ArtifactReference],
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 12:
        raise RuntimeError("#14464 Gate 5--7 attempt coverage is incomplete")
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for attempt in attempts:
        key = (attempt.payload["phase"], attempt.payload["variant"])
        groups.setdefault(key, []).append(attempt)
    expected_groups = {
        ("f2p", "fixed"),
        ("f2p", "buggy"),
        ("p2p", "fixed"),
        ("p2p", "buggy"),
    }
    if set(groups) != expected_groups or any(len(group) != 3 for group in groups.values()):
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
        "profile_id": plan.profile.profile_id,
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
        "old_attempts_loaded": False,
        "build_references": {key: value.artifact_id for key, value in builds.items()},
        "cells": [
            {
                "phase": phase,
                "variant": variant,
                "observed_outcomes": [
                    item.payload["observed_outcome"]
                    for item in sorted(
                        groups[(phase, variant)],
                        key=lambda value: value.payload["repetition"],
                    )
                ],
                "matched_expected": [
                    item.payload["matched_expected"]
                    for item in sorted(
                        groups[(phase, variant)],
                        key=lambda value: value.payload["repetition"],
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
    inputs = tuple(item.reference for item in chain) + tuple(builds.values()) + tuple(
        item.reference for item in attempts
    )
    return _write(root, _AGGREGATE, inputs, payload, authorization)


def _write_transition(
    root: Path,
    chain: tuple[StableArtifact, ...],
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
    inputs = tuple(item.reference for item in chain) + (aggregate.reference,)
    return _write(root, _TRANSITION, inputs, payload, authorization)


def _write_blocker(
    root: Path,
    chain: tuple[StableArtifact, ...],
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
        "reason": "candidate_specific_native_build_or_execution_failed",
        "error_type": type(error).__name__,
        "error": str(error),
        "candidate_execution_performed": process_started or bool(attempts),
        "candidate_materialization_performed": True,
        "build_references": {key: value.artifact_id for key, value in builds.items()},
        "attempt_count": len(attempts),
        "expected_attempt_count": 12,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "inspect candidate-specific v2 build/attempt receipts; do not reuse old attempts",
    }
    return _write(root, _BLOCKER, tuple(item.reference for item in chain), payload, authorization)


def _process_payload(
    *,
    variant: str,
    step: str,
    argv: tuple[str, ...],
    receipt: NativeProcessReceipt,
    candidate_execution_performed: bool,
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
        "stdout_truncated": stdout_truncated or receipt.stdout_truncated,
        "stderr_truncated": stderr_truncated or receipt.stderr_truncated,
        "output_encoding": "base64",
        "candidate_execution_performed": candidate_execution_performed,
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
        raise PermissionError("#14464 Gate 5--7 authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CONFIRMATION", "execute_luanti_14464_gate57_v2"]
