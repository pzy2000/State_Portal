"""Fresh selector-corrected Gate 5--7 epoch for Luanti #9572.

The first #9572 epoch is retained as an infrastructure blocker because its
short Busted name selected zero tests.  This module records that correction in
a new stable profile and writes a completely separate set of twelve receipts;
it never loads or reuses the prior attempts or aggregate.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from .luanti_9572_gate57 import (
    _CAPSULE,
    _F2P,
    _P2P,
    _PREPARATION,
    _PROFILE,
    _PRODUCTION,
    _TEST,
    _apply_inverse,
    _assert_stable_absent,
    _bounded,
    _copy_capsule,
    _count,
    _load,
    _new_workspace,
    _observed,
    _ordinary_root,
    _production_patch,
    _remove_workspace,
    _runtime_env,
    _validate_runtime_plan,
)
from .luanti_lua_regression import LuaRegressionExecutionPlan, plan_luanti_lua_regression_execution
from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import ArtifactReference, AuthorizationRecord, StableArtifact, write_stable_artifact


CONFIRMATION = "EXECUTE_LUANTI_LUA_REGRESSION"
_CANDIDATE = CandidateKey("luanti", 9572)
_PROFILE_ID = "luanti-9572-rotation-selector-corrected-v1"
_F2P_CORRECTED = "vector rotate() rotates"
_RETRY_PROFILE = ArtifactReference(
    "candidate_gate57_9572_retry_profile/luanti/9572/lua/v1",
    "candidate_gate57_9572_retry_profile",
    "1.0",
)
_ATTEMPT_KIND = "candidate_gate57_9572_retry_attempt"
_ATTEMPT_VERSION = "1.0"
_AGGREGATE = ArtifactReference(
    "candidate_gate57_9572_retry_aggregate/luanti/9572/lua/v1",
    "candidate_gate57_9572_retry_aggregate",
    "1.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_9572_retry_transition/luanti/9572/lua/v1",
    "candidate_gate57_9572_retry_transition",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_9572_retry_blocker/luanti/9572/lua/v1",
    "candidate_gate57_9572_retry_blocker",
    "1.0",
)
_TERMINAL = ArtifactReference(
    "candidate_gate57_9572_retry_terminal/luanti/9572/lua/v1",
    "candidate_gate57_9572_retry_terminal",
    "1.0",
)


@dataclass(frozen=True)
class RetryResult:
    aggregate: StableArtifact
    followup: StableArtifact
    attempts: int
    qualified: bool


@dataclass(frozen=True)
class _Cell:
    phase: Literal["f2p", "p2p"]
    variant: Literal["fixed", "buggy"]
    repetition: int
    selector: str
    expected: str

    @property
    def reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_gate57_9572_retry_attempt/luanti/9572/lua/"
            f"{self.phase}/{self.variant}/{self.repetition}/v1",
            _ATTEMPT_KIND,
            _ATTEMPT_VERSION,
        )


def execute_luanti_9572_gate57_retry(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> RetryResult:
    """Run the selector-corrected fresh twelve-cell #9572 epoch."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    if authorization.scope != "preannotation-execution":
        raise PermissionError("#9572 retry requires preannotation authorization")
    if authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("#9572 retry authorization is inactive")
    root = _ordinary_root(Path(artifact_root).absolute())
    profile = _load(root, _PROFILE)
    preparation = _load(root, _PREPARATION)
    capsule = _load(root, _CAPSULE)
    _validate_gate14(profile, preparation, capsule)
    retry_profile = _write_retry_profile(root, profile, preparation, capsule, authorization)
    plan = plan_luanti_lua_regression_execution(root, candidate=CandidateKey("luanti", 9722))
    if isinstance(plan, StageDecision):
        raise RuntimeError("registered local Busted runtime is unavailable")
    _validate_runtime_plan(plan)
    workspace = _new_workspace(root)
    attempts: list[StableArtifact] = []
    try:
        fixed, buggy = workspace / "fixed", workspace / "buggy"
        _copy_capsule(root, fixed)
        _copy_capsule(root, buggy)
        _apply_inverse(buggy, _production_patch(root))
        for cell in _cells():
            attempts.append(_execute_cell(root, plan, retry_profile, cell, fixed, buggy, authorization))
        aggregate = _write_aggregate(root, retry_profile, attempts, authorization)
        followup = _write_followup(root, retry_profile, profile, preparation, capsule, aggregate, authorization)
        return RetryResult(aggregate, followup, len(attempts), aggregate.payload["qualified"] is True)
    finally:
        _remove_workspace(workspace, root)


def _validate_gate14(profile: StableArtifact, preparation: StableArtifact, capsule: StableArtifact) -> None:
    if profile.reference != _PROFILE or preparation.reference != _PREPARATION or capsule.reference != _CAPSULE:
        raise RuntimeError("#9572 retry source references are invalid")
    if profile.payload.get("candidate") != _CANDIDATE.to_value() or preparation.payload.get("candidate") != _CANDIDATE.to_value() or capsule.payload.get("candidate") != _CANDIDATE.to_value():
        raise RuntimeError("#9572 retry candidate identity is invalid")
    gates = profile.payload.get("strict_gate_status")
    if type(gates) is not dict or any(gates.get(name) != "passed" for name in ("source_authority", "single_defect_mapping", "strict_inverse_patch", "test_api_compatibility")):
        raise RuntimeError("#9572 retry Gate1--4 chain is not accepted")
    if profile.payload.get("candidate_execution_performed") is not False or preparation.payload.get("candidate_execution_performed") is not False:
        raise RuntimeError("#9572 retry would inherit an executed profile")


def _write_retry_profile(root: Path, profile: StableArtifact, preparation: StableArtifact, capsule: StableArtifact, authorization: AuthorizationRecord) -> StableArtifact:
    _assert_stable_absent(root, _RETRY_PROFILE)
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "candidate_lifecycle": "active",
        "tier": "on_path_candidate",
        "state": "selector_corrected",
        "outcome": "downstream_pending",
        "base_profile_reference": profile.reference.artifact_id,
        "base_preparation_reference": preparation.reference.artifact_id,
        "fixed_capsule_reference": capsule.reference.artifact_id,
        "production_paths": list(_PRODUCTION),
        "preserved_test_paths": [_TEST],
        "f2p_selector": _F2P_CORRECTED,
        "p2p_selector": _P2P,
        "selector_correction": {
            "from": _F2P,
            "to": _F2P_CORRECTED,
            "reason": "registered Busted name filter requires the complete nested test name; the previous short selector selected zero tests",
        },
        "strict_gate_status": profile.payload["strict_gate_status"],
        "downstream_status": "downstream_pending",
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=_RETRY_PROFILE.artifact_id,
        artifact_kind=_RETRY_PROFILE.artifact_kind,
        semantic_version=_RETRY_PROFILE.semantic_version,
        created_at=_now(),
        inputs=(profile.reference, preparation.reference, capsule.reference),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _RETRY_PROFILE)
    if artifact.payload != payload:
        raise RuntimeError("#9572 retry profile typed reload failed")
    return artifact


def _cells() -> tuple[_Cell, ...]:
    return tuple(
        _Cell(phase, variant, repetition, selector, expected)
        for phase, selector, buggy_expected in (("f2p", _F2P_CORRECTED, "failure"), ("p2p", _P2P, "pass"))
        for variant, expected in (("fixed", "pass"), ("buggy", buggy_expected))
        for repetition in range(1, 4)
    )


def _execute_cell(root: Path, plan: LuaRegressionExecutionPlan, retry_profile: StableArtifact, cell: _Cell, fixed: Path, buggy: Path, authorization: AuthorizationRecord) -> StableArtifact:
    _assert_stable_absent(root, cell.reference)
    tree = fixed if cell.variant == "fixed" else buggy
    argv = (str(plan.runtime.runner), _TEST, f"--name={cell.selector}")
    receipt = plan.watchdog.run(argv, cwd=tree, env=_runtime_env(root, plan))
    stdout, out_truncated = _bounded(receipt.stdout)
    stderr, err_truncated = _bounded(receipt.stderr)
    count = _count(receipt)
    observed = _observed(receipt, count)
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "execution_lineage": "candidate-gate57/luanti/9572/retry-v1",
        "phase": cell.phase,
        "variant": cell.variant,
        "repetition": cell.repetition,
        "selector": cell.selector,
        "test_path": _TEST,
        "expected_outcome": cell.expected,
        "observed_outcome": observed,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "completed_test_count": count,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": out_truncated,
        "stderr_truncated": err_truncated,
        "output_encoding": "base64",
        "matched_expected": observed == cell.expected,
        "fresh_receipt": True,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
    }
    write_stable_artifact(root, artifact_id=cell.reference.artifact_id, artifact_kind=cell.reference.artifact_kind, semantic_version=cell.reference.semantic_version, created_at=_now(), inputs=(retry_profile.reference, _CAPSULE), payload=payload, authorization=authorization)
    artifact = _load(root, cell.reference)
    if artifact.payload != payload or artifact.inputs != (retry_profile.reference, _CAPSULE):
        raise RuntimeError("#9572 retry attempt typed reload failed")
    return artifact


def _write_aggregate(root: Path, retry_profile: StableArtifact, attempts: list[StableArtifact], authorization: AuthorizationRecord) -> StableArtifact:
    if len(attempts) != 12 or len({a.reference for a in attempts}) != 12:
        raise RuntimeError("#9572 retry attempt coverage is invalid")
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for attempt in attempts:
        groups.setdefault((attempt.payload["phase"], attempt.payload["variant"]), []).append(attempt)
    expected_groups = {("f2p", "fixed"), ("f2p", "buggy"), ("p2p", "fixed"), ("p2p", "buggy")}
    if set(groups) != expected_groups or any(len(v) != 3 for v in groups.values()):
        raise RuntimeError("#9572 retry repetition coverage is invalid")
    stable = all(len({a.payload["observed_outcome"] for a in v}) == 1 for v in groups.values())
    expected = all(a.payload["matched_expected"] is True for a in attempts)
    infrastructure = any(a.payload["observed_outcome"] in {"timed_out", "launch_failed", "invalid_test_count"} for a in attempts)
    qualified = stable and expected
    outcome = "qualified" if qualified else ("infrastructure_blocked" if infrastructure else "candidate_semantic_mismatch")
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "execution_lineage": "candidate-gate57/luanti/9572/retry-v1",
        "state": "qualified" if qualified else "blocked",
        "outcome": outcome,
        "terminal": outcome == "candidate_semantic_mismatch",
        "resumable": outcome != "candidate_semantic_mismatch",
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "repetitions_per_cell": 3,
        "attempt_count": 12,
        "stable_repetitions": stable,
        "all_expected": expected,
        "qualified": qualified,
        "selector_correction": {"from": _F2P, "to": _F2P_CORRECTED},
        "cells": [
            {
                "phase": phase,
                "variant": variant,
                "observed_outcomes": [a.payload["observed_outcome"] for a in sorted(groups[(phase, variant)], key=lambda x: x.payload["repetition"])],
                "matched_expected": [a.payload["matched_expected"] for a in sorted(groups[(phase, variant)], key=lambda x: x.payload["repetition"])],
            }
            for phase, variant in sorted(expected_groups)
        ],
        "deferred_until_after_gate5_7": ["runtime_world_replay", "semantic_observations", "golden_cross_judgment", "construction_receipt"],
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    write_stable_artifact(root, artifact_id=_AGGREGATE.artifact_id, artifact_kind=_AGGREGATE.artifact_kind, semantic_version=_AGGREGATE.semantic_version, created_at=_now(), inputs=tuple(a.reference for a in attempts), payload=payload, authorization=authorization)
    artifact = _load(root, _AGGREGATE)
    if artifact.payload != payload:
        raise RuntimeError("#9572 retry aggregate typed reload failed")
    return artifact


def _write_followup(root: Path, retry_profile: StableArtifact, profile: StableArtifact, preparation: StableArtifact, capsule: StableArtifact, aggregate: StableArtifact, authorization: AuthorizationRecord) -> StableArtifact:
    qualified = aggregate.payload["qualified"] is True
    infrastructure = aggregate.payload["outcome"] == "infrastructure_blocked"
    reference = _TRANSITION if qualified else (_BLOCKER if infrastructure else _TERMINAL)
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "execution_lineage": "candidate-gate57/luanti/9572/retry-v1",
        "state": "passed" if qualified else "blocked" if infrastructure else "terminal",
        "outcome": "qualified" if qualified else "infrastructure_blocked" if infrastructure else "candidate_semantic_mismatch",
        "terminal": not qualified and not infrastructure,
        "resumable": qualified or infrastructure,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "attempt_count": 12,
        "aggregate_reference": aggregate.reference.artifact_id,
        "selector_correction": {"from": _F2P, "to": _F2P_CORRECTED},
        "deferred_until_after_gate5_7": ["runtime_world_replay", "semantic_observations", "golden_cross_judgment", "construction_receipt"],
    }
    return _write_artifact(root, reference, (profile.reference, preparation.reference, capsule.reference, retry_profile.reference, aggregate.reference), payload, authorization)


def _write_artifact(root: Path, reference: ArtifactReference, inputs: tuple[ArtifactReference, ...], payload: dict[str, object], authorization: AuthorizationRecord) -> StableArtifact:
    write_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    return _load(root, reference)


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["CONFIRMATION", "RetryResult", "execute_luanti_9572_gate57_retry"]
