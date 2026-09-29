"""Luanti #9572 support-file closure and a fresh bounded Gate 5--7 epoch.

The original #9572 capsule contained ``vector.lua`` and its test, but not the
historical ``misc_helpers.lua`` file that defines ``math.hypot``.  This module
creates a new candidate-specific capsule containing that exact support file.
It never edits the production or test files and never reads prior execution
attempts.  A tiny Busted helper only loads the registered support file before
the unchanged test; it does not provide a replacement implementation.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal

from .luanti_9572_gate57 import (
    _SOURCE,
    _PRODUCTION,
    _TEST,
    _apply_inverse,
    _bounded,
    _count,
    _new_workspace,
    _observed,
    _ordinary_root,
    _production_patch,
    _remove_workspace,
    _validate_chain,
    _validate_runtime_plan,
)
from .luanti_fixed_source_legacy_migration_reader import read_legacy_merged_revision
from .luanti_lua_regression import (
    LuaRegressionExecutionPlan,
    plan_luanti_lua_regression_execution,
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


CONFIRMATION = "EXECUTE_LUANTI_LUA_REGRESSION"
_CANDIDATE = CandidateKey("luanti", 9572)
_PROFILE_ID = "luanti-9572-rotation-support-closure-v1"
_SUPPORT = ("builtin/common/misc_helpers.lua",)
_F2P = "vector rotate() rotates"
_P2P = "vector add()"
_PROFILE = ArtifactReference(
    "candidate_gate57_9572_closure_profile/luanti/9572/lua/v1",
    "candidate_gate57_9572_closure_profile",
    "1.0",
)
_PREPARATION = ArtifactReference(
    "candidate_gate57_9572_closure_preparation/luanti/9572/lua/v1",
    "candidate_gate57_9572_closure_preparation",
    "1.0",
)
_CAPSULE = ArtifactReference(
    "candidate_gate57_9572_closure_capsule/luanti/9572/lua/v1",
    "candidate_gate57_9572_closure_capsule",
    "1.0",
)
_ATTEMPT_KIND = "candidate_gate57_9572_closure_attempt"
_ATTEMPT_VERSION = "1.0"
_AGGREGATE = ArtifactReference(
    "candidate_gate57_9572_closure_aggregate/luanti/9572/lua/v1",
    "candidate_gate57_9572_closure_aggregate",
    "1.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_9572_closure_transition/luanti/9572/lua/v1",
    "candidate_gate57_9572_closure_transition",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_9572_closure_blocker/luanti/9572/lua/v1",
    "candidate_gate57_9572_closure_blocker",
    "1.0",
)
_TERMINAL = ArtifactReference(
    "candidate_gate57_9572_closure_terminal/luanti/9572/lua/v1",
    "candidate_gate57_9572_closure_terminal",
    "1.0",
)
_TREE = PurePosixPath("private/fixed-capsules/luanti/9572/closure-source-v1")
_HELPER = "c1_lazarus_9572_registered_support_loader.lua"
_MAX_STREAM = 262_144
_SUMMARY = re.compile(
    rb"(?m)^(?:\x1b\[[0-9;]*m)?(\d+) success(?:es)? / "
    rb"(\d+) failure(?:s)? / (\d+) error(?:s)? / (\d+) pending\b"
)


@dataclass(frozen=True)
class ClosureResult:
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
            f"candidate_gate57_9572_closure_attempt/luanti/9572/lua/"
            f"{self.phase}/{self.variant}/{self.repetition}/v1",
            _ATTEMPT_KIND,
            _ATTEMPT_VERSION,
        )


def prepare_luanti_9572_support_closure(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    """Reload Gate 1--4 and write an independent support-complete capsule."""

    _check_authorization(
        authorization, {"candidate-screening", "preannotation-execution"}
    )
    root = _ordinary_root(Path(artifact_root).absolute())
    chain, source_manifest, _ = _validate_chain(root)
    profile_payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "candidate_lifecycle": "active",
        "tier": "on_path_candidate",
        "authority": "github:luanti-org/luanti",
        "primary_issue_number": 9798,
        "linked_issue_numbers": [9798, 10650],
        "single_defect_mapping": "passed",
        "source_authority": "passed",
        "strict_inverse_patch": "passed",
        "test_api_compatibility": "passed",
        "production_paths": list(_PRODUCTION),
        "support_paths": list(_SUPPORT),
        "test_paths": [_TEST],
        "support_dependency": {
            "symbol": "math.hypot",
            "defined_in": _SUPPORT[0],
            "same_merged_tree": True,
            "loader_semantics": "registered_support_file_before_test",
        },
        "f2p_selector": _F2P,
        "p2p_selector": _P2P,
        "repetitions": 3,
        "strict_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
            "fixed_f2p_3x": "downstream_pending",
            "buggy_f2p_3x": "downstream_pending",
            "fixed_buggy_p2p_3x": "downstream_pending",
        },
        "downstream_status": "downstream_pending",
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    _assert_stable_absent(root, _PROFILE)
    created = _created_after(*(artifact.created_at for artifact in chain))
    write_stable_artifact(
        root,
        artifact_id=_PROFILE.artifact_id,
        artifact_kind=_PROFILE.artifact_kind,
        semantic_version=_PROFILE.semantic_version,
        created_at=created,
        inputs=tuple(artifact.reference for artifact in chain),
        payload=profile_payload,
        authorization=authorization,
    )
    profile = _load(root, _PROFILE)
    if profile.payload != profile_payload:
        raise RuntimeError("#9572 closure profile reload failed")
    preparation_payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "state": "prepared",
        "outcome": "downstream_pending",
        "profile_reference": profile.reference.artifact_id,
        "production_paths": list(_PRODUCTION),
        "support_paths": list(_SUPPORT),
        "test_paths": [_TEST],
        "f2p": {
            "selector": _F2P,
            "repetitions": 3,
            "fixed_expected": "pass",
            "buggy_expected": "failure",
        },
        "p2p": {
            "selector": _P2P,
            "repetitions": 3,
            "fixed_expected": "pass",
            "buggy_expected": "pass",
        },
        "support_loader": {
            "kind": "registered_support_loader",
            "script": _HELPER,
            "loads": list(_SUPPORT),
            "modifies_test": False,
        },
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    _assert_stable_absent(root, _PREPARATION)
    write_stable_artifact(
        root,
        artifact_id=_PREPARATION.artifact_id,
        artifact_kind=_PREPARATION.artifact_kind,
        semantic_version=_PREPARATION.semantic_version,
        created_at=_created_after(profile.created_at),
        inputs=(profile.reference,),
        payload=preparation_payload,
        authorization=authorization,
    )
    preparation = _load(root, _PREPARATION)
    if preparation.payload != preparation_payload:
        raise RuntimeError("#9572 closure preparation reload failed")
    capsule = _materialize_capsule(root, source_manifest, profile, authorization)
    return profile, preparation, capsule


def execute_luanti_9572_support_closure(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> ClosureResult:
    """Run twelve fresh fixed/buggy F2P/P2P cells from the new capsule."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _check_authorization(authorization, {"preannotation-execution"})
    root = _ordinary_root(Path(artifact_root).absolute())
    profile = _load(root, _PROFILE)
    preparation = _load(root, _PREPARATION)
    capsule = _load(root, _CAPSULE)
    _validate_profile(profile, preparation, capsule)
    _validate_capsule_bytes(root, capsule)
    # The registered Busted runtime is shared as a read-only dependency, but
    # the execution plan must remain bound to this candidate. Reusing the
    # #9722 profile here would make the runner lineage ambiguous even though
    # the support capsule and production inverse are #9572-specific.
    plan = plan_luanti_lua_regression_execution(
        root, candidate=CandidateKey("luanti", 9572)
    )
    if isinstance(plan, StageDecision):
        raise RuntimeError("registered local Busted runtime is unavailable")
    _validate_runtime_plan(plan)
    workspace = _new_workspace(root)
    attempts: list[StableArtifact] = []
    try:
        fixed, buggy = workspace / "fixed", workspace / "buggy"
        _copy_closure_capsule(root, fixed)
        _copy_closure_capsule(root, buggy)
        fixed_support = _regular_bytes(fixed / _SUPPORT[0])
        _apply_inverse(buggy, _production_patch(root))
        if _regular_bytes(buggy / _SUPPORT[0]) != fixed_support:
            raise RuntimeError("#9572 support file changed during inverse patch")
        if _regular_bytes(buggy / _PRODUCTION[0]) == _regular_bytes(
            fixed / _PRODUCTION[0]
        ):
            raise RuntimeError("#9572 production inverse did not change the target")
        _write_loader(fixed)
        _write_loader(buggy)
        for cell in _cells():
            attempts.append(
                _execute_cell(
                    root, plan, profile, capsule, cell, fixed, buggy, authorization
                )
            )
        aggregate = _write_aggregate(root, profile, capsule, attempts, authorization)
        followup_ref, followup_payload = _followup_for(aggregate)
        followup = _write_followup(
            root,
            followup_ref,
            (
                profile.reference,
                preparation.reference,
                capsule.reference,
                aggregate.reference,
            ),
            followup_payload,
            authorization,
        )
        return ClosureResult(
            aggregate, followup, len(attempts), aggregate.payload["qualified"] is True
        )
    finally:
        _remove_workspace(workspace, root)


def _materialize_capsule(
    root: Path,
    source_manifest: dict[str, object],
    profile: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    _assert_stable_absent(root, _CAPSULE)
    raw_path = _load(root, _SOURCE).payload.get("raw_evidence_path")
    if not isinstance(raw_path, str):
        raise RuntimeError("#9572 source manifest path is unavailable")
    revision = read_legacy_merged_revision(
        root, raw_manifest_path=raw_path, candidate=_CANDIDATE
    )
    mirror = root / "private/repository-enumeration-git/luanti/mirror.git"
    destination = root.joinpath(*_TREE.parts)
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("#9572 closure capsule destination already exists")
    files = (*_PRODUCTION, *_SUPPORT, _TEST)
    staging_parent = destination.parent
    staging_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".closure-source-", dir=staging_parent))
    try:
        for relative in files:
            content = _git_show(mirror, revision, relative)
            target = staging.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "source_tree_path": _TREE.as_posix(),
        "production_paths": list(_PRODUCTION),
        "support_paths": list(_SUPPORT),
        "test_paths": [_TEST],
        "support_dependency": {
            "symbol": "math.hypot",
            "path": _SUPPORT[0],
            "same_merged_tree": True,
        },
        "source_manifest_complete": source_manifest.get("complete") is True,
    }
    try:
        write_stable_artifact(
            root,
            artifact_id=_CAPSULE.artifact_id,
            artifact_kind=_CAPSULE.artifact_kind,
            semantic_version=_CAPSULE.semantic_version,
            created_at=_created_after(profile.created_at),
            inputs=(profile.reference,),
            payload=payload,
            authorization=authorization,
        )
        artifact = _load(root, _CAPSULE)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    if artifact.payload != payload:
        raise RuntimeError("#9572 closure capsule reload failed")
    return artifact


def _validate_profile(
    profile: StableArtifact, preparation: StableArtifact, capsule: StableArtifact
) -> None:
    if (
        profile.reference != _PROFILE
        or preparation.reference != _PREPARATION
        or capsule.reference != _CAPSULE
    ):
        raise RuntimeError("#9572 closure identity is invalid")
    if (
        profile.payload.get("candidate") != _CANDIDATE.to_value()
        or preparation.payload.get("candidate") != _CANDIDATE.to_value()
        or capsule.payload.get("candidate") != _CANDIDATE.to_value()
    ):
        raise RuntimeError("#9572 closure candidate identity is invalid")
    if (
        preparation.payload.get("state") != "prepared"
        or profile.payload.get("candidate_execution_performed") is not False
    ):
        raise RuntimeError("#9572 closure profile is not fresh")
    gates = profile.payload.get("strict_gate_status")
    if type(gates) is not dict or any(
        gates.get(name) != "passed"
        for name in (
            "source_authority",
            "single_defect_mapping",
            "strict_inverse_patch",
            "test_api_compatibility",
        )
    ):
        raise RuntimeError("#9572 closure Gate 1--4 chain is not accepted")
    if capsule.payload.get("support_paths") != list(_SUPPORT):
        raise RuntimeError("#9572 closure support path is missing")


def _validate_capsule_bytes(root: Path, capsule: StableArtifact) -> None:
    source = root.joinpath(*PurePosixPath(capsule.payload["source_tree_path"]).parts)
    mirror = root / "private/repository-enumeration-git/luanti/mirror.git"
    raw_path = _load(root, _SOURCE).payload["raw_evidence_path"]
    revision = read_legacy_merged_revision(
        root, raw_manifest_path=raw_path, candidate=_CANDIDATE
    )
    for relative in (*_PRODUCTION, *_SUPPORT, _TEST):
        actual = _regular_bytes(source / relative)
        expected = _git_show(mirror, revision, relative)
        if actual != expected:
            raise RuntimeError(f"#9572 closure source mismatch: {relative}")
    if b"function math.hypot" not in _regular_bytes(source / _SUPPORT[0]):
        raise RuntimeError("#9572 closure does not define math.hypot")


def _copy_closure_capsule(root: Path, destination: Path) -> None:
    source = root.joinpath(*_TREE.parts)
    destination.mkdir(mode=0o700)
    for relative in (*_PRODUCTION, *_SUPPORT, _TEST):
        source_path = source / PurePosixPath(relative)
        target = destination / PurePosixPath(relative)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with source_path.open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
        if not target.is_file() or target.is_symlink():
            raise RuntimeError("#9572 closure copied an unsafe file")


def _write_loader(tree: Path) -> None:
    path = tree / _HELPER
    with path.open("xb") as handle:
        handle.write(
            b'_G.core = _G.core or {}\ndofile("builtin/common/misc_helpers.lua")\n'
        )
        handle.flush()
        os.fsync(handle.fileno())


def _execute_cell(
    root: Path,
    plan: LuaRegressionExecutionPlan,
    profile: StableArtifact,
    capsule: StableArtifact,
    cell: _Cell,
    fixed: Path,
    buggy: Path,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    _assert_stable_absent(root, cell.reference)
    tree = fixed if cell.variant == "fixed" else buggy
    argv = (
        str(plan.runtime.runner),
        _TEST,
        f"--helper={_HELPER}",
        f"--name={cell.selector}",
    )
    receipt = plan.watchdog.run(argv, cwd=tree, env=_runtime_env(root, plan))
    stdout, out_truncated = _bounded(receipt.stdout)
    stderr, err_truncated = _bounded(receipt.stderr)
    count = _count(receipt)
    observed = _observed(receipt, count)
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "execution_lineage": "candidate-gate57/luanti/9572/support-closure-v1",
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
        "support_loader": {
            "kind": "registered_support_loader",
            "script": _HELPER,
            "loads": list(_SUPPORT),
            "modifies_test": False,
        },
        "matched_expected": observed == cell.expected,
        "fresh_receipt": True,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=cell.reference.artifact_id,
        artifact_kind=cell.reference.artifact_kind,
        semantic_version=cell.reference.semantic_version,
        created_at=_now(),
        inputs=(profile.reference, capsule.reference),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, cell.reference)
    if artifact.payload != payload or artifact.inputs != (
        profile.reference,
        capsule.reference,
    ):
        raise RuntimeError("#9572 closure attempt reload failed")
    return artifact


def _cells() -> tuple[_Cell, ...]:
    return tuple(
        _Cell(phase, variant, repetition, selector, expected)
        for phase, selector, buggy_expected in (
            ("f2p", _F2P, "failure"),
            ("p2p", _P2P, "pass"),
        )
        for variant, expected in (("fixed", "pass"), ("buggy", buggy_expected))
        for repetition in range(1, 4)
    )


def _write_aggregate(
    root: Path,
    profile: StableArtifact,
    capsule: StableArtifact,
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 12 or len({a.reference for a in attempts}) != 12:
        raise RuntimeError("#9572 closure attempt coverage is invalid")
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
        len(value) != 3 for value in groups.values()
    ):
        raise RuntimeError("#9572 closure repetition coverage is invalid")
    stable = all(
        len({a.payload["observed_outcome"] for a in value}) == 1
        for value in groups.values()
    )
    expected = all(a.payload["matched_expected"] is True for a in attempts)
    infrastructure = any(
        a.payload["observed_outcome"]
        in {"timed_out", "launch_failed", "invalid_test_count"}
        for a in attempts
    )
    outcome = (
        "qualified"
        if stable and expected
        else (
            "infrastructure_blocked"
            if infrastructure
            else "candidate_semantic_mismatch"
        )
    )
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "execution_lineage": "candidate-gate57/luanti/9572/support-closure-v1",
        "state": "qualified" if outcome == "qualified" else "blocked",
        "outcome": outcome,
        "terminal": outcome == "candidate_semantic_mismatch",
        "resumable": outcome != "candidate_semantic_mismatch",
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "support_closure": {
            "path": _SUPPORT[0],
            "symbol": "math.hypot",
            "loader": _HELPER,
            "same_merged_tree": True,
        },
        "repetitions_per_cell": 3,
        "attempt_count": 12,
        "stable_repetitions": stable,
        "all_expected": expected,
        "qualified": stable and expected,
        "cells": [
            {
                "phase": phase,
                "variant": variant,
                "observed_outcomes": [
                    a.payload["observed_outcome"]
                    for a in sorted(
                        groups[(phase, variant)], key=lambda x: x.payload["repetition"]
                    )
                ],
                "matched_expected": [
                    a.payload["matched_expected"]
                    for a in sorted(
                        groups[(phase, variant)], key=lambda x: x.payload["repetition"]
                    )
                ],
            }
            for phase, variant in sorted(expected_groups)
        ],
        "deferred_until_after_gate5_7": [
            "runtime_world_replay",
            "semantic_observations",
            "golden_cross_judgment",
            "construction_receipt",
        ],
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    write_stable_artifact(
        root,
        artifact_id=_AGGREGATE.artifact_id,
        artifact_kind=_AGGREGATE.artifact_kind,
        semantic_version=_AGGREGATE.semantic_version,
        created_at=_now(),
        inputs=tuple(a.reference for a in attempts),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _AGGREGATE)
    if artifact.payload != payload or artifact.inputs != tuple(
        a.reference for a in attempts
    ):
        raise RuntimeError("#9572 closure aggregate reload failed")
    return artifact


def _followup_for(
    aggregate: StableArtifact,
) -> tuple[ArtifactReference, dict[str, object]]:
    if aggregate.payload["qualified"]:
        return _TRANSITION, {
            "candidate": _CANDIDATE.to_value(),
            "profile_id": _PROFILE_ID,
            "state": "passed",
            "outcome": "qualified",
            "terminal": False,
            "resumable": True,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "aggregate_reference": aggregate.reference.artifact_id,
            "gate_status": {
                "source_authority": "passed",
                "single_defect_mapping": "passed",
                "strict_inverse_patch": "passed",
                "test_api_compatibility": "passed",
                "fixed_buggy_f2p_p2p_3x": "passed",
            },
            "deferred_until_after_gate5_7": [
                "runtime_world_replay",
                "semantic_observations",
                "golden_cross_judgment",
                "construction_receipt",
            ],
        }
    if aggregate.payload["outcome"] == "infrastructure_blocked":
        return _BLOCKER, {
            "candidate": _CANDIDATE.to_value(),
            "profile_id": _PROFILE_ID,
            "state": "blocked",
            "outcome": "infrastructure_blocked",
            "terminal": False,
            "resumable": True,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "aggregate_reference": aggregate.reference.artifact_id,
            "reason": "one_or_more_gate57_cells_need_infrastructure_followup",
        }
    return _TERMINAL, {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "state": "terminal",
        "outcome": "candidate_semantic_mismatch",
        "terminal": True,
        "resumable": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "aggregate_reference": aggregate.reference.artifact_id,
        "reason": "stable_gate57_expectations_not_met",
    }


def _write_followup(
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
    return _load(root, reference)


def _runtime_env(root: Path, plan: LuaRegressionExecutionPlan) -> dict[str, str]:
    home = root / "private/runtime-home/luanti-9572-support-closure"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": f"{plan.runtime.root / 'bin'}:/usr/bin:/bin",
        "LUA_PATH": ";".join(str(path) for path in plan.runtime.lua_paths),
        "LUA_CPATH": ";".join(str(path) for path in plan.runtime.lua_cpaths),
    }


def _git_show(mirror: Path, revision: str, relative: str) -> bytes:
    result = subprocess.run(
        ("git", f"--git-dir={mirror}", "show", f"{revision}:{relative}"),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"#9572 merged source file unavailable: {relative}")
    return result.stdout


def _regular_bytes(path: Path) -> bytes:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("#9572 closure file is unsafe")
    return path.read_bytes()


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _check_authorization(authorization: AuthorizationRecord, scopes: set[str]) -> None:
    if (
        authorization.scope not in scopes
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#9572 closure authorization is inactive")


def _assert_stable_absent(root: Path, reference: ArtifactReference) -> None:
    path = root / "artifacts" / Path(reference.artifact_id) / reference.semantic_version
    if path.exists():
        raise RuntimeError(
            f"#9572 fresh artifact already exists: {reference.artifact_id}"
        )


def _created_after(*values: str) -> str:
    parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values]
    return (
        (max(parsed) + timedelta(microseconds=1))
        .astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "CONFIRMATION",
    "ClosureResult",
    "execute_luanti_9572_support_closure",
    "prepare_luanti_9572_support_closure",
]
