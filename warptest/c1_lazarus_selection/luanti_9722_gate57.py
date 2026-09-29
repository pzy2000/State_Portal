"""Candidate-specific bounded Gate 5--7 execution for Luanti change 9722.

This module deliberately does not use the shared Lua regression executor's
artifact namespace.  It revalidates the closed plan, materializes a fresh
fixed/buggy pair, and writes one stable receipt before starting the next cell.
Only the twelve Gate 5--7 cells are in scope; replay, oracle, construction,
and paid work remain deferred.
"""

from __future__ import annotations

import base64
import os
import shutil
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal

from .luanti_lua_regression import (
    LuaRegressionExecutionPlan,
    LuaRegressionRun,
    WatchdogReceipt,
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


# Keep the existing explicit local execution confirmation while using an
# independent candidate-specific artifact namespace below.
CONFIRMATION = "EXECUTE_LUANTI_LUA_REGRESSION"
_ROOT_CANDIDATE = CandidateKey("luanti", 9722)
_PREFLIGHT = ArtifactReference(
    "candidate_gate57_preflight/luanti/9722/lua/v1",
    "candidate_gate57_preflight",
    "1.0",
)
_ATTEMPT_KIND = "candidate_gate57_attempt"
_ATTEMPT_VERSION = "1.0"
_AGGREGATE = ArtifactReference(
    "candidate_gate57_aggregate/luanti/9722/lua/v1",
    "candidate_gate57_aggregate",
    "1.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_transition/luanti/9722/lua/v1",
    "candidate_gate57_transition",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_blocker/luanti/9722/lua/v1",
    "candidate_gate57_blocker",
    "1.0",
)
_TERMINAL = ArtifactReference(
    "candidate_gate57_terminal/luanti/9722/lua/v1",
    "candidate_gate57_terminal",
    "1.0",
)
_MAX_STREAM_BYTES = 262_144
_BUSTED_SUMMARY = re.compile(
    rb"(?m)^(?:\x1b\[[0-9;]*m)?(\d+) success(?:es)? / "
    rb"(\d+) failure(?:s)? / (\d+) error(?:s)? / (\d+) pending\b"
)


@dataclass(frozen=True)
class Gate57Result:
    aggregate: StableArtifact
    transition: StableArtifact | None
    attempts: int
    qualified: bool


@dataclass(frozen=True)
class _Cell:
    run: LuaRegressionRun
    variant: Literal["fixed", "buggy"]
    repetition: int

    @property
    def expected(self) -> str:
        return (
            self.run.fixed_expected_outcome
            if self.variant == "fixed"
            else self.run.buggy_expected_outcome
        )

    @property
    def reference(self) -> ArtifactReference:
        return _attempt_reference(self.run.phase, self.variant, self.repetition)


def execute_luanti_9722_gate57(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> Gate57Result:
    """Run exactly twelve fresh #9722 fixed/buggy F2P/P2P cells."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    if authorization.scope != "preannotation-execution":
        raise PermissionError("Gate 5--7 authorization scope is invalid")
    if authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("Gate 5--7 authorization is inactive")
    root = _ordinary_root(Path(artifact_root).absolute())
    preflight = _load_preflight(root)
    decision = plan_luanti_lua_regression_execution(root, candidate=_ROOT_CANDIDATE)
    if isinstance(decision, StageDecision):
        raise RuntimeError(
            "Gate 5--7 plan is blocked: "
            f"{decision.details.get('reason', decision.outcome)}"
        )
    plan = decision
    _validate_plan(plan, preflight)
    workspace = _new_workspace(root)
    attempts: list[StableArtifact] = []
    try:
        fixed = workspace / "fixed"
        buggy = workspace / "buggy"
        _copy_capsule(plan, fixed)
        _copy_capsule(plan, buggy)
        _apply_inverse(plan, buggy)
        for cell in _cells(plan):
            attempts.append(_run_fresh_cell(root, plan, cell, fixed, buggy, authorization))
        aggregate = _write_aggregate(root, plan, attempts, authorization)
        qualified = aggregate.payload["qualified"] is True
        if qualified:
            transition = _write_transition(root, plan, aggregate, preflight, authorization)
        elif _is_infrastructure_only(attempts):
            transition = _write_blocker(root, aggregate, preflight, authorization)
        else:
            transition = _write_terminal(root, aggregate, preflight, authorization)
        return Gate57Result(aggregate, transition, len(attempts), qualified)
    finally:
        _remove_workspace(workspace, root)


def _load_preflight(root: Path) -> StableArtifact:
    preflight = load_stable_artifact(
        root,
        artifact_id=_PREFLIGHT.artifact_id,
        artifact_kind=_PREFLIGHT.artifact_kind,
        semantic_version=_PREFLIGHT.semantic_version,
    )
    payload = preflight.payload
    candidate = payload.get("candidate")
    if (
        preflight.reference != _PREFLIGHT
        or candidate != _ROOT_CANDIDATE.to_value()
        or payload.get("profile_id") != "luanti-9722-reversible-number-serialize-v1"
        or payload.get("state") != "ready_for_gate5_7"
        or payload.get("terminal") is not False
        or payload.get("candidate_execution_performed") is not False
        or payload.get("paid_actions_authorized") is not False
    ):
        raise RuntimeError("#9722 Gate 5--7 preflight is not executable")
    gates = payload.get("gate_status")
    if type(gates) is not dict or any(
        gates.get(name) != "passed"
        for name in (
            "source_authority",
            "single_defect_mapping",
            "strict_inverse_patch",
            "test_api_compatibility",
        )
    ):
        raise RuntimeError("#9722 Gate 1--4 preflight is not accepted")
    return preflight


def _validate_plan(plan: LuaRegressionExecutionPlan, preflight: StableArtifact) -> None:
    if plan.profile.candidate != _ROOT_CANDIDATE or plan.profile.repetitions != 3:
        raise RuntimeError("#9722 execution plan candidate identity is invalid")
    if plan.profile.profile_id != preflight.payload["profile_id"]:
        raise RuntimeError("#9722 execution plan profile drifted")
    if plan.profile.production_paths != ("builtin/common/serialize.lua",):
        raise RuntimeError("#9722 production binding is invalid")
    if plan.profile.support_paths != ("builtin/common/vector.lua",):
        raise RuntimeError("#9722 support binding is invalid")
    if plan.profile.test_paths != ("builtin/common/tests/serialize_spec.lua",):
        raise RuntimeError("#9722 test binding is invalid")
    if plan.profile.f2p_selector != "serialize handles precise numbers":
        raise RuntimeError("#9722 F2P selector is invalid")
    if plan.profile.p2p_selector != "serialize handles characters":
        raise RuntimeError("#9722 P2P selector is invalid")
    if plan.profile.f2p_buggy_outcome != "failure":
        raise RuntimeError("#9722 F2P expected outcome is invalid")
    if plan.fixed_capsule.reference != ArtifactReference(
        "fixed_source_capsule/luanti/9722/fixed/v2", "fixed_source_capsule", "2.0"
    ):
        raise RuntimeError("#9722 fixed capsule binding is invalid")
    if not plan.runtime.runner.is_file() or not os.access(plan.runtime.runner, os.X_OK):
        raise RuntimeError("#9722 registered Busted runner is unavailable")


def _cells(plan: LuaRegressionExecutionPlan) -> tuple[_Cell, ...]:
    cells: list[_Cell] = []
    for run in plan.runs:
        for variant in ("fixed", "buggy"):
            for repetition in range(1, 4):
                cells.append(_Cell(run, variant, repetition))
    if len(cells) != 12:
        raise RuntimeError("#9722 Gate 5--7 cell coverage is invalid")
    return tuple(cells)


def _attempt_reference(
    phase: str, variant: str, repetition: int
) -> ArtifactReference:
    return ArtifactReference(
        f"candidate_gate57_attempt/luanti/9722/lua/{phase}/{variant}/{repetition}/v1",
        _ATTEMPT_KIND,
        _ATTEMPT_VERSION,
    )


def _run_fresh_cell(
    root: Path,
    plan: LuaRegressionExecutionPlan,
    cell: _Cell,
    fixed: Path,
    buggy: Path,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = cell.reference
    _assert_absent(root, reference)
    tree = fixed if cell.variant == "fixed" else buggy
    env = _runtime_environment(root, plan)
    receipt = plan.watchdog.run(cell.run.argv, cwd=tree, env=env)
    stdout, stdout_truncated = _bounded(receipt.stdout)
    stderr, stderr_truncated = _bounded(receipt.stderr)
    completed = _completed_count(receipt)
    observed = _observed(receipt, completed)
    payload = {
        "candidate": _ROOT_CANDIDATE.to_value(),
        "profile_id": plan.profile.profile_id,
        "execution_lineage": "candidate-gate57/luanti/9722/lua/v1",
        "phase": cell.run.phase,
        "variant": cell.variant,
        "repetition": cell.repetition,
        "selector": cell.run.selector,
        "test_path": cell.run.test_path,
        "expected_outcome": cell.expected,
        "observed_outcome": observed,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "completed_test_count": completed,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "output_encoding": "base64",
        "matched_expected": observed == cell.expected,
        "fresh_receipt": True,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=(plan.fixed_capsule.reference,),
        payload=payload,
        authorization=authorization,
    )
    loaded = load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    if loaded.payload != payload or loaded.inputs != (plan.fixed_capsule.reference,):
        raise RuntimeError("#9722 Gate 5--7 attempt typed reload failed")
    return loaded


def _runtime_environment(root: Path, plan: LuaRegressionExecutionPlan) -> dict[str, str]:
    home = root / "private" / "runtime-home" / "luanti-9722-gate57"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(plan.runtime.root / "bin"),
        "LUA_PATH": ";".join(str(path) for path in plan.runtime.lua_paths),
        "LUA_CPATH": ";".join(str(path) for path in plan.runtime.lua_cpaths),
    }


def _completed_count(receipt: WatchdogReceipt) -> int | None:
    if receipt.outcome != "completed":
        return None
    matches = list(_BUSTED_SUMMARY.finditer(receipt.stdout))
    if len(matches) != 1:
        return 0
    return sum(int(value) for value in matches[0].groups())


def _observed(receipt: WatchdogReceipt, completed: int | None) -> str:
    if receipt.outcome == "timed_out":
        return "timed_out"
    if receipt.outcome == "launch_failed":
        return "launch_failed"
    if receipt.outcome != "completed" or not isinstance(receipt.returncode, int):
        return "launch_failed"
    if completed != 1:
        return "invalid_test_count"
    return "pass" if receipt.returncode == 0 else "failure"


def _write_aggregate(
    root: Path,
    plan: LuaRegressionExecutionPlan,
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 12 or len({a.reference for a in attempts}) != 12:
        raise RuntimeError("#9722 Gate 5--7 attempt coverage is invalid")
    by_cell = {(a.payload["phase"], a.payload["variant"]): [] for a in attempts}
    for attempt in attempts:
        by_cell[(attempt.payload["phase"], attempt.payload["variant"])].append(attempt)
    if set(by_cell) != {
        ("f2p", "fixed"),
        ("f2p", "buggy"),
        ("p2p", "fixed"),
        ("p2p", "buggy"),
    } or any(len(value) != 3 for value in by_cell.values()):
        raise RuntimeError("#9722 Gate 5--7 repetition coverage is invalid")
    cells = []
    for phase, variant in sorted(by_cell):
        ordered = sorted(by_cell[(phase, variant)], key=lambda a: a.payload["repetition"])
        cells.append({
            "phase": phase,
            "variant": variant,
            "observed_outcomes": [a.payload["observed_outcome"] for a in ordered],
            "matched_expected": [a.payload["matched_expected"] for a in ordered],
        })
    stable = all(len({a.payload["observed_outcome"] for a in group}) == 1 for group in by_cell.values())
    expected = all(a.payload["matched_expected"] is True for a in attempts)
    infrastructure = any(
        a.payload["observed_outcome"] in {"timed_out", "launch_failed", "invalid_test_count"}
        for a in attempts
    )
    qualified = stable and expected
    outcome = "qualified" if qualified else (
        "infrastructure_blocked" if infrastructure else "candidate_semantic_mismatch"
    )
    payload = {
        "candidate": _ROOT_CANDIDATE.to_value(),
        "profile_id": plan.profile.profile_id,
        "execution_lineage": "candidate-gate57/luanti/9722/lua/v1",
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
        "cells": cells,
        "gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
            "version_materialization": "passed",
            "fixed_f2p_3x": "passed" if _cell_pass(by_cell, "f2p", "fixed") else "failed",
            "buggy_f2p_3x": "passed" if _cell_pass(by_cell, "f2p", "buggy") else "failed",
            "fixed_p2p_3x": "passed" if _cell_pass(by_cell, "p2p", "fixed") else "failed",
            "buggy_p2p_3x": "passed" if _cell_pass(by_cell, "p2p", "buggy") else "failed",
        },
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
    loaded = load_stable_artifact(
        root,
        artifact_id=_AGGREGATE.artifact_id,
        artifact_kind=_AGGREGATE.artifact_kind,
        semantic_version=_AGGREGATE.semantic_version,
    )
    if loaded.payload != payload:
        raise RuntimeError("#9722 Gate 5--7 aggregate typed reload failed")
    return loaded


def _cell_pass(groups: dict[tuple[str, str], list[StableArtifact]], phase: str, variant: str) -> bool:
    return all(a.payload["matched_expected"] is True for a in groups[(phase, variant)])


def _is_infrastructure_only(attempts: list[StableArtifact]) -> bool:
    return any(a.payload["observed_outcome"] in {"timed_out", "launch_failed", "invalid_test_count"} for a in attempts)


def _write_transition(
    root: Path,
    plan: LuaRegressionExecutionPlan,
    aggregate: StableArtifact,
    preflight: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _ROOT_CANDIDATE.to_value(),
        "profile_id": plan.profile.profile_id,
        "execution_lineage": "candidate-gate57/luanti/9722/lua/v1",
        "state": "passed",
        "outcome": "qualified",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "attempt_count": 12,
        "gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
            "version_materialization": "passed",
            "fixed_buggy_f2p_p2p_3x": "passed",
        },
        "deferred_until_after_gate5_7": [
            "runtime_world_replay",
            "semantic_observations",
            "golden_cross_judgment",
            "construction_receipt",
        ],
    }
    return _write_followup(root, _TRANSITION, (preflight.reference, aggregate.reference), payload, authorization)


def _write_blocker(
    root: Path,
    aggregate: StableArtifact,
    preflight: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _ROOT_CANDIDATE.to_value(),
        "execution_lineage": "candidate-gate57/luanti/9722/lua/v1",
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "reason": "one_or_more_gate57_cells_need_infrastructure_followup",
        "aggregate_reference": aggregate.reference.artifact_id,
    }
    return _write_followup(root, _BLOCKER, (preflight.reference, aggregate.reference), payload, authorization)


def _write_terminal(
    root: Path,
    aggregate: StableArtifact,
    preflight: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _ROOT_CANDIDATE.to_value(),
        "execution_lineage": "candidate-gate57/luanti/9722/lua/v1",
        "state": "terminal",
        "outcome": "candidate_semantic_mismatch",
        "terminal": True,
        "resumable": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "reason": "stable_gate57_expectations_not_met",
        "aggregate_reference": aggregate.reference.artifact_id,
    }
    return _write_followup(root, _TERMINAL, (preflight.reference, aggregate.reference), payload, authorization)


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
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _copy_capsule(plan: LuaRegressionExecutionPlan, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    paths = (*plan.profile.production_paths, *plan.profile.support_paths, *plan.profile.test_paths)
    for relative in paths:
        source = plan.fixed_capsule.source_tree / PurePosixPath(relative)
        target = destination / PurePosixPath(relative)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with source.open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer)


def _apply_inverse(plan: LuaRegressionExecutionPlan, buggy: Path) -> None:
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"):
        env.pop(name, None)
    env["GIT_CEILING_DIRECTORIES"] = str(buggy.parent)
    argv = ("git", "apply", "-R", "--whitespace=nowarn", "-")
    completed = subprocess.run(
        argv,
        cwd=buggy,
        input=plan.production_inverse.patch,
        capture_output=True,
        check=False,
        timeout=10,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError("#9722 strict inverse patch failed")


def _new_workspace(root: Path) -> Path:
    parent = root / "private" / "candidate-gate57-work" / "luanti" / "9722"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(prefix="run-", dir=parent))


def _remove_workspace(workspace: Path, root: Path) -> None:
    parent = root / "private" / "candidate-gate57-work" / "luanti" / "9722"
    if workspace.parent != parent or not workspace.name.startswith("run-"):
        raise RuntimeError("refusing to remove unregistered Gate 5--7 workspace")
    try:
        info = workspace.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("Gate 5--7 workspace became unsafe")
    shutil.rmtree(workspace)


def _assert_absent(root: Path, reference: ArtifactReference) -> None:
    directory = root / "artifacts" / Path(reference.artifact_id) / reference.semantic_version
    if directory.exists():
        raise RuntimeError("fresh Gate 5--7 receipt already exists; refusing reuse")


def _bounded(value: bytes) -> tuple[bytes, bool]:
    return (value, False) if len(value) <= _MAX_STREAM_BYTES else (value[:_MAX_STREAM_BYTES], True)


def _ordinary_root(root: Path) -> Path:
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return root.resolve(strict=True)


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["CONFIRMATION", "Gate57Result", "execute_luanti_9722_gate57"]
