"""Bounded stable-ID execution for closed Luanti native regressions."""

from __future__ import annotations

import base64
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Callable, Literal, Protocol

from .luanti_native_regression import (
    NativeRegressionExecutionPlan,
    NativeRegressionRun,
    _is_deleted_test_path,
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


EXECUTION_CONFIRMATION = "EXECUTE_LUANTI_NATIVE_REGRESSION"
_VERSION = "3.0"
_OUTPUT_ENCODING = "base64"
_MAX_STREAM_BYTES = 262_144
_CONFIGURE_TIMEOUT_SECONDS = 600
_BUILD_TIMEOUT_SECONDS = 1_800
_TEST_TIMEOUT_SECONDS = 300
_MODULE_SUMMARY = re.compile(
    rb"(?m)^Unit Test Results: (PASSED|FAILED)\r?\n"
    rb"\s+(\d+) / (\d+) failed tests\.\r?$"
)
_CATCH_SUCCESS = re.compile(
    rb"(?m)^All tests passed \(\d+ assertions? in (\d+) test cases?\)\r?$"
)
_CATCH_TOTAL = re.compile(rb"(?mi)^test cases:\s*(\d+)\b")


@dataclass(frozen=True)
class NativeProcessReceipt:
    outcome: Literal["completed", "timed_out", "launch_failed"]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def __post_init__(self) -> None:
        if (
            self.outcome == "completed"
            and type(self.returncode) is not int
            or self.outcome != "completed"
            and self.returncode is not None
            or type(self.stdout) is not bytes
            or type(self.stderr) is not bytes
            or type(self.stdout_truncated) is not bool
            or type(self.stderr_truncated) is not bool
            or self.stdout_truncated
            and len(self.stdout) != _MAX_STREAM_BYTES
            or self.stderr_truncated
            and len(self.stderr) != _MAX_STREAM_BYTES
        ):
            raise ValueError("native process receipt is invalid")


@dataclass(frozen=True)
class NativeRegressionExecutionResult:
    candidate: CandidateKey
    aggregate: StableArtifact
    executed_build_steps: int
    executed_attempts: int
    resumed_attempts: int
    qualified: bool


@dataclass(frozen=True)
class _AttemptSpec:
    run: NativeRegressionRun
    variant: Literal["fixed", "buggy"]
    repetition: int

    @property
    def expected_outcome(self) -> str:
        if self.variant == "fixed":
            return self.run.fixed_expected_outcome
        return self.run.buggy_expected_outcome


class ProcessRunner(Protocol):
    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> NativeProcessReceipt: ...


Planner = Callable[..., NativeRegressionExecutionPlan | StageDecision]


def execute_luanti_native_regression(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
    confirmation: str,
    planner: Planner = plan_luanti_native_regression_execution,
    process_runner: ProcessRunner | None = None,
) -> NativeRegressionExecutionResult:
    """Build both variants and execute exact 3x fixed/buggy F2P/P2P cells."""

    if confirmation != EXECUTION_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {EXECUTION_CONFIRMATION!r}")
    if type(candidate) is not CandidateKey or candidate.repository_key != "luanti":
        raise ValueError("native regression execution candidate is invalid")
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("native regression execution authorization is invalid")
    if authorization.scope != "preannotation-execution":
        raise PermissionError(
            "native regression execution authorization scope is invalid"
        )
    if authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("native regression execution authorization is inactive")
    root = _ordinary_root(Path(artifact_root).absolute())
    decision = planner(root, candidate=candidate)
    if isinstance(decision, StageDecision):
        raise RuntimeError(
            f"native regression execution is blocked: {decision.details.get('reason')}"
        )
    plan = decision
    if plan.profile.candidate != candidate or plan.profile.repetitions != 3:
        raise RuntimeError("native regression execution plan identity is invalid")
    _validate_capsule(root, plan)
    runner = process_runner or run_bounded_process
    _prepare_closed_home(root, Path(plan.closed_environment["HOME"]))
    workspace = _new_workspace(root, candidate)
    attempts: list[StableArtifact] = []
    executed_attempts = 0
    resumed_attempts = 0
    try:
        trees = {variant: workspace / variant for variant in ("fixed", "buggy")}
        _copy_full_capsule(plan, trees["fixed"])
        _copy_full_capsule(plan, trees["buggy"])
        _apply_production_inverse(plan, trees["fixed"], trees["buggy"])
        build_references: dict[str, ArtifactReference] = {}
        for variant in ("fixed", "buggy"):
            build_references[variant] = _configure_and_build(
                root,
                plan,
                variant=variant,
                tree=trees[variant],
                authorization=authorization,
                runner=runner,
            )
        for run in plan.runs:
            for variant in ("fixed", "buggy"):
                for repetition in range(1, plan.profile.repetitions + 1):
                    spec = _AttemptSpec(run, variant, repetition)
                    attempt = _load_attempt_if_valid(
                        root, plan, spec, build_references[variant]
                    )
                    if attempt is None:
                        executable = trees[variant] / run.executable_path
                        _ordinary_existing_file(executable, trees[variant])
                        argv = (str(executable), *run.argv[1:])
                        receipt = runner(
                            argv,
                            cwd=trees[variant],
                            env=dict(plan.closed_environment),
                            timeout_seconds=_TEST_TIMEOUT_SECONDS,
                        )
                        attempt = _persist_attempt(
                            root,
                            plan,
                            spec,
                            build_references[variant],
                            receipt,
                            authorization,
                        )
                        executed_attempts += 1
                    else:
                        resumed_attempts += 1
                    attempts.append(attempt)
        aggregate = _persist_aggregate(root, plan, attempts, authorization)
        return NativeRegressionExecutionResult(
            candidate=candidate,
            aggregate=aggregate,
            executed_build_steps=4,
            executed_attempts=executed_attempts,
            resumed_attempts=resumed_attempts,
            qualified=aggregate.payload["qualified"] is True,
        )
    finally:
        _remove_workspace(workspace, root)


def run_bounded_process(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> NativeProcessReceipt:
    """Run without a shell while draining and bounding both captured streams."""

    if not argv or timeout_seconds <= 0:
        raise ValueError("native process invocation is invalid")
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        return NativeProcessReceipt("launch_failed", None, b"", str(error).encode())
    outputs = [bytearray(), bytearray()]
    truncated = [False, False]

    def drain(stream: object, destination: bytearray, index: int) -> None:
        assert hasattr(stream, "read")
        while chunk := stream.read(65_536):
            remaining = _MAX_STREAM_BYTES - len(destination)
            if remaining > 0:
                destination.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated[index] = True

    assert process.stdout is not None and process.stderr is not None
    threads = (
        threading.Thread(target=drain, args=(process.stdout, outputs[0], 0)),
        threading.Thread(target=drain, args=(process.stderr, outputs[1], 1)),
    )
    for thread in threads:
        thread.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
        outcome: Literal["completed", "timed_out"] = "completed"
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            pass
        process.wait()
        returncode = None
        outcome = "timed_out"
    for thread in threads:
        thread.join()
    process.stdout.close()
    process.stderr.close()
    return NativeProcessReceipt(
        outcome,
        returncode,
        bytes(outputs[0]),
        bytes(outputs[1]),
        truncated[0],
        truncated[1],
    )


def _configure_and_build(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    *,
    variant: str,
    tree: Path,
    authorization: AuthorizationRecord,
    runner: ProcessRunner,
) -> ArtifactReference:
    last_reference: ArtifactReference | None = None
    for step, raw_argv, timeout in (
        ("configure", plan.configure_argv, _CONFIGURE_TIMEOUT_SECONDS),
        ("build", plan.build_argv, _BUILD_TIMEOUT_SECONDS),
    ):
        argv = tuple(str(value) for value in raw_argv)
        receipt = runner(
            argv,
            cwd=tree,
            env=dict(plan.closed_environment),
            timeout_seconds=timeout,
        )
        reference = _persist_build_receipt(
            root, plan, variant, step, argv, receipt, authorization
        )
        last_reference = reference
        if receipt.outcome != "completed" or receipt.returncode != 0:
            raise RuntimeError(f"{variant} {step} failed")
    assert last_reference is not None
    executable = tree / plan.runs[0].executable_path
    _ordinary_existing_file(executable, tree)
    return last_reference


def _build_reference(
    plan: NativeRegressionExecutionPlan, variant: str, step: str
) -> ArtifactReference:
    return ArtifactReference(
        f"native_regression_build/luanti/{plan.profile.candidate.change_number}/{variant}/{step}/{plan.execution_namespace}",
        "native_regression_build",
        _VERSION,
    )


def _persist_build_receipt(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    variant: str,
    step: str,
    argv: tuple[str, ...],
    receipt: NativeProcessReceipt,
    authorization: AuthorizationRecord,
) -> ArtifactReference:
    reference = _build_reference(plan, variant, step)
    stdout, stdout_truncated = _bounded_output(receipt.stdout)
    stderr, stderr_truncated = _bounded_output(receipt.stderr)
    stdout_truncated = stdout_truncated or receipt.stdout_truncated
    stderr_truncated = stderr_truncated or receipt.stderr_truncated
    inputs = (plan.profile.fixed_capsule_reference, *plan.dependency_references)
    payload = {
        "candidate": plan.profile.candidate.to_value(),
        "profile_id": plan.profile.profile_id,
        "variant": variant,
        "step": step,
        "argv": list(argv),
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "output_encoding": _OUTPUT_ENCODING,
    }
    _write_and_reload(root, reference, inputs, payload, authorization)
    return reference


def _attempt_reference(
    plan: NativeRegressionExecutionPlan, spec: _AttemptSpec
) -> ArtifactReference:
    return ArtifactReference(
        "native_regression_attempt/luanti/"
        f"{plan.profile.candidate.change_number}/{spec.run.phase}/"
        f"{spec.variant}/{spec.repetition}/{plan.execution_namespace}",
        "native_regression_attempt",
        _VERSION,
    )


def _load_attempt_if_valid(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    spec: _AttemptSpec,
    build_reference: ArtifactReference,
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
    _validate_attempt(plan, spec, build_reference, artifact)
    return artifact


def _attempt_identity(
    plan: NativeRegressionExecutionPlan, spec: _AttemptSpec
) -> dict[str, object]:
    return {
        "candidate": plan.profile.candidate.to_value(),
        "profile_id": plan.profile.profile_id,
        "phase": spec.run.phase,
        "variant": spec.variant,
        "repetition": spec.repetition,
        "selector": spec.run.selector,
        "expected_outcome": spec.expected_outcome,
    }


def _persist_attempt(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    spec: _AttemptSpec,
    build_reference: ArtifactReference,
    receipt: NativeProcessReceipt,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    count = completed_native_test_count(
        spec.run, receipt.outcome, receipt.stdout, receipt.stderr
    )
    observed = _observed_outcome(receipt.outcome, receipt.returncode, count)
    stdout, stdout_truncated = _bounded_output(receipt.stdout)
    stderr, stderr_truncated = _bounded_output(receipt.stderr)
    stdout_truncated = stdout_truncated or receipt.stdout_truncated
    stderr_truncated = stderr_truncated or receipt.stderr_truncated
    payload = {
        **_attempt_identity(plan, spec),
        "observed_outcome": observed,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "completed_test_count": count,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "output_encoding": _OUTPUT_ENCODING,
        "matched_expected": observed == spec.expected_outcome,
    }
    reference = _attempt_reference(plan, spec)
    artifact = _write_and_reload(
        root,
        reference,
        (plan.profile.fixed_capsule_reference, build_reference),
        payload,
        authorization,
    )
    _validate_attempt(plan, spec, build_reference, artifact)
    return artifact


def _validate_attempt(
    plan: NativeRegressionExecutionPlan,
    spec: _AttemptSpec,
    build_reference: ArtifactReference,
    artifact: StableArtifact,
) -> None:
    expected_keys = {
        *_attempt_identity(plan, spec),
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
    payload = artifact.payload
    if artifact.inputs != (plan.profile.fixed_capsule_reference, build_reference):
        raise RuntimeError("native regression attempt dependency is invalid")
    if set(payload) != expected_keys or any(
        payload[key] != value for key, value in _attempt_identity(plan, spec).items()
    ):
        raise RuntimeError("native regression attempt identity is invalid")
    decoded: dict[str, bytes] = {}
    for key in ("stdout_base64", "stderr_base64"):
        try:
            value = base64.b64decode(payload[key], validate=True)
        except (TypeError, ValueError) as error:
            raise RuntimeError("native regression attempt output is invalid") from error
        if len(value) > _MAX_STREAM_BYTES:
            raise RuntimeError("native regression attempt output is invalid")
        truncated = payload[f"{key.removesuffix('_base64')}_truncated"]
        if truncated is True and len(value) != _MAX_STREAM_BYTES:
            raise RuntimeError("native regression attempt output is invalid")
        decoded[key] = value
    count = completed_native_test_count(
        spec.run,
        payload["process_outcome"],
        decoded["stdout_base64"],
        decoded["stderr_base64"],
    )
    observed = _observed_outcome(
        payload["process_outcome"], payload["returncode"], count
    )
    if (
        payload["completed_test_count"] != count
        or payload["observed_outcome"] != observed
        or payload["matched_expected"] is not (observed == spec.expected_outcome)
        or payload["output_encoding"] != _OUTPUT_ENCODING
        or type(payload["stdout_truncated"]) is not bool
        or type(payload["stderr_truncated"]) is not bool
    ):
        raise RuntimeError("native regression attempt result is invalid")


def completed_native_test_count(
    run: NativeRegressionRun,
    process_outcome: object,
    stdout: bytes,
    stderr: bytes,
) -> int | None:
    """Strictly count the one selected Catch case or native test module.

    Luanti emits unit-test summaries on stderr in real executions.  Parse both
    captured streams as one evidence set while retaining stream boundaries; a
    mixed or duplicated summary is ambiguous and therefore counts as zero.
    A native module summary represents the one module selected by
    ``--test-module`` even when that module reports several internal tests.
    """

    if type(run) is not NativeRegressionRun:
        raise ValueError("native regression run is invalid")
    if type(stdout) is not bytes or type(stderr) is not bytes:
        raise ValueError("native regression output is invalid")
    if process_outcome != "completed":
        return None
    streams = (stdout, stderr)
    modules = [
        match for stream in streams for match in _MODULE_SUMMARY.finditer(stream)
    ]
    successes = [
        match for stream in streams for match in _CATCH_SUCCESS.finditer(stream)
    ]
    totals = [match for stream in streams for match in _CATCH_TOTAL.finditer(stream)]
    if len(modules) == 1 and not successes and not totals:
        return 1 if int(modules[0].group(3)) > 0 else 0
    if len(successes) == 1 and not modules and not totals:
        return int(successes[0].group(1))
    if len(totals) == 1 and not modules and not successes:
        return int(totals[0].group(1))
    return 0


def _observed_outcome(
    process_outcome: object, returncode: object, completed_test_count: object
) -> str:
    if process_outcome == "timed_out":
        return "timed_out"
    if process_outcome == "launch_failed":
        return "launch_failed"
    if process_outcome != "completed" or type(returncode) is not int:
        raise RuntimeError("native regression process result is invalid")
    if completed_test_count != 1:
        return "invalid_test_count"
    return "pass" if returncode == 0 else "failure"


def _persist_aggregate(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 12 or len({item.reference for item in attempts}) != 12:
        raise RuntimeError("native regression aggregate attempt coverage is invalid")
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
        len(group) != 3 for group in groups.values()
    ):
        raise RuntimeError("native regression aggregate repetition coverage is invalid")
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
                    item.payload["observed_outcome"]
                    for item in sorted(
                        groups[(phase, variant)],
                        key=lambda value: value.payload["repetition"],
                    )
                ],
            }
            for phase, variant in sorted(expected_groups)
        ],
    }
    reference = ArtifactReference(
        f"native_regression_aggregate/luanti/{plan.profile.candidate.change_number}/fixed-buggy/{plan.execution_namespace}",
        "native_regression_aggregate",
        _VERSION,
    )
    return _write_and_reload(
        root,
        reference,
        tuple(attempt.reference for attempt in attempts),
        payload,
        authorization,
    )


def _write_and_reload(
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
        raise RuntimeError("native regression typed reload is invalid")
    return artifact


def _validate_capsule(root: Path, plan: NativeRegressionExecutionPlan) -> None:
    expected = root / PurePosixPath(plan.profile.fixed_source_tree_path)
    if plan.fixed_capsule.source_tree != expected:
        raise RuntimeError("native fixed capsule is outside the artifact root")
    _ordinary_existing_directory(expected, root)
    for relative in (
        *plan.profile.production_paths,
        *plan.profile.support_paths,
        *plan.profile.test_paths,
        *plan.profile.p2p_test_paths,
    ):
        # #11866 removes the obsolete test_player.cpp from the fixed PR
        # capsule.  The planner already treats that deletion-only test path
        # as typed inventory; mirror that narrow exception at execution
        # validation so the registered fixed capsule can be copied without
        # inventing a source file that is not in the exact PR tree.
        if (
            _is_deleted_test_path(plan.profile, relative)
            and not (expected / relative).exists()
        ):
            continue
        _ordinary_existing_file(expected / relative, expected)


def _copy_full_capsule(plan: NativeRegressionExecutionPlan, destination: Path) -> None:
    source = plan.fixed_capsule.source_tree
    _validate_tree_links(source)
    shutil.copytree(source, destination, symlinks=True)
    _ordinary_existing_directory(destination, destination.parent)
    _validate_tree_links(destination)


def _validate_tree_links(root: Path) -> None:
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
            continue
        if not stat.S_ISLNK(info.st_mode):
            raise RuntimeError("native fixed capsule entry is unsafe")
        target = os.readlink(path)
        if os.path.isabs(target):
            raise RuntimeError("native fixed capsule link is unsafe")
        try:
            (path.parent / target).resolve(strict=False).relative_to(root.resolve())
        except ValueError as error:
            raise RuntimeError("native fixed capsule link is unsafe") from error


def _apply_production_inverse(
    plan: NativeRegressionExecutionPlan, fixed_tree: Path, buggy_tree: Path
) -> None:
    fixed_inventory = _tree_inventory(fixed_tree)
    environment = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"):
        environment.pop(name, None)
    environment["GIT_CEILING_DIRECTORIES"] = str(buggy_tree.parent)
    try:
        completed = subprocess.run(
            tuple(str(value) for value in plan.production_inverse.buggy_apply_argv),
            cwd=buggy_tree,
            input=plan.production_inverse.patch,
            capture_output=True,
            timeout=30,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("native production inverse application failed") from error
    if completed.returncode != 0:
        raise RuntimeError("native production inverse application failed")
    buggy_inventory = _tree_inventory(buggy_tree)
    production = set(plan.profile.production_paths)
    inventory_delta = (set(fixed_inventory) ^ set(buggy_inventory))
    if not inventory_delta <= production:
        raise RuntimeError("native production inverse changed non-production inventory")
    for path in set(fixed_inventory) | set(buggy_inventory):
        fixed_entry = fixed_inventory.get(path)
        buggy_entry = buggy_inventory.get(path)
        if path in production:
            # A production hunk may add or remove a file (for example a
            # newly-added regression fixture).  The inverse is still strict:
            # only the registered production path may have an inventory delta,
            # and when present both variants must remain ordinary files.
            if fixed_entry is None or buggy_entry is None:
                if fixed_entry not in {None, ("file", None)} or buggy_entry not in {
                    None,
                    ("file", None),
                }:
                    raise RuntimeError("native production inverse changed production type")
                continue
            if fixed_entry != ("file", None) or buggy_entry != ("file", None):
                raise RuntimeError("native production inverse changed production type")
            if _files_equal(fixed_tree / path, buggy_tree / path):
                raise RuntimeError(
                    "native production inverse did not change production"
                )
        elif fixed_entry != buggy_entry:
            raise RuntimeError(
                "native production inverse changed non-production source"
            )
        elif fixed_entry == ("file", None) and not _files_equal(
            fixed_tree / path, buggy_tree / path
        ):
            raise RuntimeError(
                "native production inverse changed non-production source"
            )


def _tree_inventory(root: Path) -> dict[str, tuple[str, bytes | str | None]]:
    inventory: dict[str, tuple[str, bytes | str | None]] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            inventory[relative] = ("directory", None)
        elif stat.S_ISREG(info.st_mode):
            inventory[relative] = ("file", None)
        elif stat.S_ISLNK(info.st_mode):
            inventory[relative] = ("symlink", os.readlink(path))
        else:
            raise RuntimeError("native fixed capsule entry is unsafe")
    return inventory


def _files_equal(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_stream, right.open("rb") as right_stream:
        while True:
            left_chunk = left_stream.read(65_536)
            right_chunk = right_stream.read(65_536)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _new_workspace(root: Path, candidate: CandidateKey) -> Path:
    parent = root / "private/native-regression-work"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _ordinary_existing_directory(parent, root)
    workspace = Path(
        tempfile.mkdtemp(prefix=f"luanti-{candidate.change_number}-", dir=parent)
    )
    _ordinary_existing_directory(workspace, parent)
    return workspace


def _prepare_closed_home(root: Path, home: Path) -> None:
    _require_under(home, root)
    current = root
    for part in home.relative_to(root).parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("native closed HOME is unsafe")


def _remove_workspace(workspace: Path, root: Path) -> None:
    parent = root / "private/native-regression-work"
    if workspace.parent != parent or not workspace.name.startswith("luanti-"):
        raise RuntimeError("refusing to remove an unregistered native workspace")
    try:
        info = workspace.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("native execution workspace became unsafe")
    shutil.rmtree(workspace)


def _ordinary_root(path: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return resolved


def _ordinary_existing_directory(path: Path, root: Path) -> None:
    _require_under(path, root)
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("native directory is unsafe")


def _ordinary_existing_file(path: Path, root: Path) -> None:
    _require_under(path, root)
    current = root
    for index, part in enumerate(path.relative_to(root).parts):
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("native file path is unsafe")
        if index < len(path.relative_to(root).parts) - 1 and not stat.S_ISDIR(
            info.st_mode
        ):
            raise RuntimeError("native file path is unsafe")
        if index == len(path.relative_to(root).parts) - 1 and not stat.S_ISREG(
            info.st_mode
        ):
            raise RuntimeError("native file path is unsafe")


def _require_under(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise RuntimeError("native path is outside the registered root") from error
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise RuntimeError("native path is unsafe")


def _bounded_output(value: bytes) -> tuple[bytes, bool]:
    return value[:_MAX_STREAM_BYTES], len(value) > _MAX_STREAM_BYTES


def _now() -> str:
    value = datetime.now(UTC) + timedelta(microseconds=1)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "EXECUTION_CONFIRMATION",
    "NativeProcessReceipt",
    "NativeRegressionExecutionResult",
    "execute_luanti_native_regression",
    "run_bounded_process",
]
