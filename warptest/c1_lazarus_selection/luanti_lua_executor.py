"""Bounded stable-ID execution for closed Luanti Lua regression profiles."""

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
from typing import Callable, Literal

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


EXECUTION_CONFIRMATION = "EXECUTE_LUANTI_LUA_REGRESSION"
_ATTEMPT_KIND = "lua_regression_attempt"
_AGGREGATE_KIND = "lua_regression_aggregate"
_VERSION = "3.0"
_OUTPUT_ENCODING = "base64"
_MAX_STREAM_BYTES = 262_144
_BUSTED_SUMMARY = re.compile(
    rb"(?m)^(\d+) success(?:es)? / (\d+) failure(?:s)? / "
    rb"(\d+) error(?:s)? / (\d+) pending\b"
)


@dataclass(frozen=True)
class LuaRegressionExecutionResult:
    candidate: CandidateKey
    aggregate: StableArtifact
    executed_attempts: int
    resumed_attempts: int
    qualified: bool


@dataclass(frozen=True)
class _AttemptSpec:
    run: LuaRegressionRun
    variant: Literal["fixed", "buggy"]
    repetition: int

    @property
    def expected_outcome(self) -> str:
        if self.variant == "fixed":
            return self.run.fixed_expected_outcome
        return self.run.buggy_expected_outcome


Planner = Callable[..., LuaRegressionExecutionPlan | StageDecision]


def execute_luanti_lua_regression(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
    confirmation: str,
    planner: Planner = plan_luanti_lua_regression_execution,
) -> LuaRegressionExecutionResult:
    """Execute exactly three fixed/buggy F2P/P2P repetitions, locally.

    Every process receipt is written and typed-loaded before the next process
    starts. Existing attempts are resumed only when their strict payload and
    stable dependencies match the requested cell.
    """

    if confirmation != EXECUTION_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {EXECUTION_CONFIRMATION!r}")
    if type(candidate) is not CandidateKey or candidate.repository_key != "luanti":
        raise ValueError("Lua regression execution candidate is invalid")
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("Lua regression execution authorization is invalid")
    if authorization.scope != "preannotation-execution":
        raise PermissionError("Lua regression execution authorization scope is invalid")
    if authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("Lua regression execution authorization is inactive")
    root = _ordinary_root(Path(artifact_root).absolute())
    decision = planner(root, candidate=candidate)
    if isinstance(decision, StageDecision):
        raise RuntimeError(
            f"Lua regression execution is blocked: {decision.details.get('reason')}"
        )
    plan = decision
    if plan.profile.candidate != candidate or plan.profile.repetitions != 3:
        raise RuntimeError("Lua regression execution plan identity is invalid")
    _validate_capsule(root, plan)

    workspace = _new_workspace(root, candidate)
    executed = 0
    resumed = 0
    attempts: list[StableArtifact] = []
    try:
        fixed_tree = workspace / "fixed"
        buggy_tree = workspace / "buggy"
        _copy_registered_capsule(plan, fixed_tree)
        _copy_registered_capsule(plan, buggy_tree)
        _apply_production_inverse(plan, fixed_tree, buggy_tree)
        for run in plan.runs:
            for variant, tree in (("fixed", fixed_tree), ("buggy", buggy_tree)):
                for repetition in range(1, plan.profile.repetitions + 1):
                    spec = _AttemptSpec(run, variant, repetition)
                    attempt = _load_attempt_if_valid(root, plan, spec)
                    if attempt is None:
                        receipt = plan.watchdog.run(run.argv, cwd=tree)
                        attempt = _persist_attempt(
                            root,
                            plan,
                            spec,
                            receipt,
                            authorization,
                        )
                        executed += 1
                    else:
                        resumed += 1
                    attempts.append(attempt)
        aggregate = _persist_aggregate(root, plan, attempts, authorization)
        qualified = aggregate.payload["qualified"] is True
        return LuaRegressionExecutionResult(
            candidate=candidate,
            aggregate=aggregate,
            executed_attempts=executed,
            resumed_attempts=resumed,
            qualified=qualified,
        )
    finally:
        _remove_workspace(workspace, root)


def _attempt_reference(
    plan: LuaRegressionExecutionPlan, spec: _AttemptSpec
) -> ArtifactReference:
    number = plan.profile.candidate.change_number
    return ArtifactReference(
        (
            f"lua_regression_attempt/luanti/{number}/{spec.run.phase}/"
            f"{spec.variant}/{spec.repetition}/v3"
        ),
        _ATTEMPT_KIND,
        _VERSION,
    )


def _aggregate_reference(plan: LuaRegressionExecutionPlan) -> ArtifactReference:
    return ArtifactReference(
        (
            "lua_regression_aggregate/luanti/"
            f"{plan.profile.candidate.change_number}/fixed-buggy/v3"
        ),
        _AGGREGATE_KIND,
        _VERSION,
    )


def _load_attempt_if_valid(
    root: Path,
    plan: LuaRegressionExecutionPlan,
    spec: _AttemptSpec,
) -> StableArtifact | None:
    reference = _attempt_reference(plan, spec)
    try:
        artifact = load_stable_artifact(
            root,
            artifact_id=reference.artifact_id,
            artifact_kind=reference.artifact_kind,
            semantic_version=reference.semantic_version,
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise
    _validate_attempt(plan, spec, artifact)
    return artifact


def _attempt_identity(
    plan: LuaRegressionExecutionPlan, spec: _AttemptSpec
) -> dict[str, object]:
    return {
        "candidate": plan.profile.candidate.to_value(),
        "profile_id": plan.profile.profile_id,
        "phase": spec.run.phase,
        "variant": spec.variant,
        "repetition": spec.repetition,
        "selector": spec.run.selector,
        "test_path": spec.run.test_path,
        "expected_outcome": spec.expected_outcome,
    }


def _validate_attempt(
    plan: LuaRegressionExecutionPlan,
    spec: _AttemptSpec,
    artifact: StableArtifact,
) -> None:
    payload = artifact.payload
    expected_keys = {
        "candidate",
        "profile_id",
        "phase",
        "variant",
        "repetition",
        "selector",
        "test_path",
        "expected_outcome",
        "observed_outcome",
        "process_outcome",
        "returncode",
        "completed_test_count",
        "stdout_base64",
        "stderr_base64",
        "stdout_truncated",
        "stderr_truncated",
        "output_encoding",
        "matched_expected",
    }
    if artifact.inputs != (plan.profile.fixed_capsule_reference,):
        raise RuntimeError("Lua regression attempt dependency is invalid")
    if set(payload) != expected_keys or any(
        payload[key] != value for key, value in _attempt_identity(plan, spec).items()
    ):
        raise RuntimeError("Lua regression attempt identity is invalid")
    if (
        payload["observed_outcome"]
        not in {
            "pass",
            "failure",
            "timed_out",
            "launch_failed",
            "invalid_test_count",
        }
        or payload["process_outcome"] not in {"completed", "timed_out", "launch_failed"}
        or payload["returncode"] is not None
        and type(payload["returncode"]) is not int
        or payload["completed_test_count"] is not None
        and type(payload["completed_test_count"]) is not int
        or payload["output_encoding"] != _OUTPUT_ENCODING
        or type(payload["stdout_truncated"]) is not bool
        or type(payload["stderr_truncated"]) is not bool
        or type(payload["matched_expected"]) is not bool
    ):
        raise RuntimeError("Lua regression attempt result is invalid")
    decoded_outputs: dict[str, bytes] = {}
    for key in ("stdout_base64", "stderr_base64"):
        if type(payload[key]) is not str:
            raise RuntimeError("Lua regression attempt output is invalid")
        try:
            decoded = base64.b64decode(payload[key], validate=True)
        except ValueError as error:
            raise RuntimeError("Lua regression attempt output is invalid") from error
        decoded_outputs[key] = decoded
        truncated = payload[f"{key.removesuffix('_base64')}_truncated"]
        if len(decoded) > _MAX_STREAM_BYTES or (
            truncated is True and len(decoded) != _MAX_STREAM_BYTES
        ):
            raise RuntimeError("Lua regression attempt output is invalid")
    expected_count = _completed_test_count(
        payload["process_outcome"], decoded_outputs["stdout_base64"]
    )
    if payload["completed_test_count"] != expected_count:
        raise RuntimeError("Lua regression attempt test count is invalid")
    if payload["matched_expected"] is not (
        payload["observed_outcome"] == payload["expected_outcome"]
    ):
        raise RuntimeError("Lua regression attempt expectation is invalid")
    if payload["observed_outcome"] != _observed_outcome_from_values(
        payload["process_outcome"],
        payload["returncode"],
        completed_test_count=payload["completed_test_count"],
    ):
        raise RuntimeError("Lua regression attempt process result is invalid")


def _persist_attempt(
    root: Path,
    plan: LuaRegressionExecutionPlan,
    spec: _AttemptSpec,
    receipt: WatchdogReceipt,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    reference = _attempt_reference(plan, spec)
    observed = _observed_outcome(receipt)
    stdout, stdout_truncated = _bounded_output(receipt.stdout)
    stderr, stderr_truncated = _bounded_output(receipt.stderr)
    payload = {
        **_attempt_identity(plan, spec),
        "observed_outcome": observed,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "completed_test_count": _completed_test_count(receipt.outcome, receipt.stdout),
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "output_encoding": _OUTPUT_ENCODING,
        "matched_expected": observed == spec.expected_outcome,
    }
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=(plan.profile.fixed_capsule_reference,),
        payload=payload,
        authorization=authorization,
    )
    artifact = load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    _validate_attempt(plan, spec, artifact)
    return artifact


def _observed_outcome(receipt: WatchdogReceipt) -> str:
    return _observed_outcome_from_values(
        receipt.outcome,
        receipt.returncode,
        completed_test_count=_completed_test_count(receipt.outcome, receipt.stdout),
    )


def _observed_outcome_from_values(
    process_outcome: object,
    returncode: object,
    *,
    completed_test_count: object,
) -> str:
    if process_outcome == "timed_out":
        return "timed_out"
    if process_outcome == "launch_failed":
        if returncode is not None:
            raise RuntimeError("Lua regression attempt process result is invalid")
        return "launch_failed"
    if process_outcome != "completed" or type(returncode) is not int:
        raise RuntimeError("Lua regression attempt process result is invalid")
    if completed_test_count != 1:
        return "invalid_test_count"
    return "pass" if returncode == 0 else "failure"


def _completed_test_count(process_outcome: object, stdout: bytes) -> int | None:
    if process_outcome != "completed":
        return None
    matches = list(_BUSTED_SUMMARY.finditer(stdout))
    if len(matches) != 1:
        return 0
    return sum(int(value) for value in matches[0].groups())


def _bounded_output(value: bytes) -> tuple[bytes, bool]:
    if len(value) <= _MAX_STREAM_BYTES:
        return value, False
    return value[:_MAX_STREAM_BYTES], True


def _persist_aggregate(
    root: Path,
    plan: LuaRegressionExecutionPlan,
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 12 or len({item.reference for item in attempts}) != 12:
        raise RuntimeError("Lua regression aggregate attempt coverage is invalid")
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
    if set(groups) != expected_groups or any(
        len(value) != 3 for value in groups.values()
    ):
        raise RuntimeError("Lua regression aggregate repetition coverage is invalid")
    stable = all(
        len({attempt.payload["observed_outcome"] for attempt in group}) == 1
        for group in groups.values()
    )
    all_expected = all(
        attempt.payload["matched_expected"] is True for attempt in attempts
    )
    payload = {
        "candidate": plan.profile.candidate.to_value(),
        "profile_id": plan.profile.profile_id,
        "repetitions_per_cell": 3,
        "attempt_count": 12,
        "stable_repetitions": stable,
        "all_expected": all_expected,
        "qualified": stable and all_expected,
        "cells": [
            {
                "phase": phase,
                "variant": variant,
                "observed_outcomes": [
                    attempt.payload["observed_outcome"]
                    for attempt in sorted(
                        groups[(phase, variant)],
                        key=lambda item: item.payload["repetition"],
                    )
                ],
            }
            for phase, variant in sorted(expected_groups)
        ],
    }
    reference = _aggregate_reference(plan)
    inputs = tuple(attempt.reference for attempt in attempts)
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
    aggregate = load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    if aggregate.inputs != inputs or aggregate.payload != payload:
        raise RuntimeError("Lua regression aggregate typed reload is invalid")
    return aggregate


def _validate_capsule(root: Path, plan: LuaRegressionExecutionPlan) -> None:
    source_tree = plan.fixed_capsule.source_tree
    if source_tree != root / PurePosixPath(plan.profile.fixed_source_tree_path):
        raise RuntimeError("fixed capsule is outside the registered artifact root")
    if (
        plan.fixed_capsule.support_paths != plan.profile.support_paths
        or plan.fixed_capsule.test_paths != plan.profile.test_paths
    ):
        raise RuntimeError("fixed capsule support/test binding is invalid")
    _ordinary_existing_directory(source_tree, root, "fixed capsule path is unsafe")
    for relative in _capsule_paths(plan):
        _ordinary_existing_file(source_tree / PurePosixPath(relative), source_tree)


def _copy_registered_capsule(
    plan: LuaRegressionExecutionPlan, destination: Path
) -> None:
    destination.mkdir(mode=0o700)
    for relative in _capsule_paths(plan):
        source = plan.fixed_capsule.source_tree / PurePosixPath(relative)
        target = destination / PurePosixPath(relative)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with source.open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer)


def _apply_production_inverse(
    plan: LuaRegressionExecutionPlan, fixed_tree: Path, buggy_tree: Path
) -> None:
    fixed_production = {
        relative: (fixed_tree / PurePosixPath(relative)).read_bytes()
        for relative in plan.profile.production_paths
    }
    fixed_nonproduction = {
        relative: (fixed_tree / PurePosixPath(relative)).read_bytes()
        for relative in (*plan.profile.support_paths, *plan.profile.test_paths)
    }
    git_environment = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"):
        git_environment.pop(name, None)
    # The disposable tree lives below the benchmark artifact root, which may
    # itself be inside the StatePortal worktree.  Prevent git-apply from
    # discovering that unrelated parent repository and silently applying the
    # candidate-relative paths against the wrong root.
    git_environment["GIT_CEILING_DIRECTORIES"] = str(buggy_tree.parent)
    try:
        completed = subprocess.run(
            plan.production_inverse.buggy_apply_argv,
            cwd=buggy_tree,
            input=plan.production_inverse.patch,
            capture_output=True,
            check=False,
            timeout=10,
            env=git_environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("production inverse application failed") from error
    if completed.returncode != 0:
        raise RuntimeError("production inverse application failed")
    for relative in plan.profile.production_paths:
        _ordinary_existing_file(buggy_tree / PurePosixPath(relative), buggy_tree)
    for relative in (*plan.profile.support_paths, *plan.profile.test_paths):
        _ordinary_existing_file(buggy_tree / PurePosixPath(relative), buggy_tree)
    actual_files = {
        path.relative_to(buggy_tree).as_posix()
        for path in buggy_tree.rglob("*")
        if path.is_file()
    }
    registered_files = set(_capsule_paths(plan))
    if actual_files != registered_files:
        raise RuntimeError("production inverse changed the registered file inventory")
    if any(
        (buggy_tree / PurePosixPath(relative)).read_bytes() != content
        for relative, content in fixed_nonproduction.items()
    ):
        raise RuntimeError("production inverse changed fixed capsule support or tests")
    if all(
        (buggy_tree / PurePosixPath(relative)).read_bytes() == content
        for relative, content in fixed_production.items()
    ):
        raise RuntimeError("production inverse did not change production source")


def _capsule_paths(plan: LuaRegressionExecutionPlan) -> tuple[str, ...]:
    paths = (
        *plan.profile.production_paths,
        *plan.profile.support_paths,
        *plan.profile.test_paths,
    )
    if len(paths) != len(set(paths)):
        raise RuntimeError("fixed capsule registered paths overlap")
    return paths


def _new_workspace(root: Path, candidate: CandidateKey) -> Path:
    parent = root / "private" / "lua-regression-work"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _ordinary_existing_directory(parent, root, "execution workspace parent is unsafe")
    path = Path(
        tempfile.mkdtemp(prefix=f"luanti-{candidate.change_number}-", dir=parent)
    )
    _ordinary_existing_directory(path, parent, "execution workspace is unsafe")
    return path


def _remove_workspace(workspace: Path, root: Path) -> None:
    parent = root / "private" / "lua-regression-work"
    if workspace.parent != parent or not workspace.name.startswith("luanti-"):
        raise RuntimeError("refusing to remove an unregistered execution workspace")
    try:
        info = workspace.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("execution workspace became unsafe")
    shutil.rmtree(workspace)


def _ordinary_root(root: Path) -> Path:
    try:
        info = root.lstat()
        resolved = root.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return resolved


def _ordinary_existing_directory(path: Path, root: Path, message: str) -> None:
    _require_under(path, root, message)
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise RuntimeError(message) from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(message)


def _ordinary_existing_file(path: Path, root: Path) -> None:
    _require_under(path, root, "fixed capsule path is unsafe")
    current = root
    parts = path.relative_to(root).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise RuntimeError("fixed capsule path is unsafe") from error
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("fixed capsule path is unsafe")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("fixed capsule path is unsafe")
        if index == len(parts) - 1 and (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
        ):
            raise RuntimeError("fixed capsule path is unsafe")


def _require_under(path: Path, root: Path, message: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise RuntimeError(message) from error
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise RuntimeError(message)


def _now() -> str:
    value = datetime.now(UTC) + timedelta(microseconds=1)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "EXECUTION_CONFIRMATION",
    "LuaRegressionExecutionResult",
    "execute_luanti_lua_regression",
]
