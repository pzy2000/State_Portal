"""Fresh client-only Gate 5--7 epoch for Luanti #15687.

The upstream ``matrix4`` Catch test is part of ``UNITTEST_CLIENT_SRCS`` and
therefore is absent from a server-only binary.  This adapter binds the current
v2 source/patch/atomicity/profile/preparation chain, the exact fixed capsule,
and the registered offline client dependency closure.  It builds two client
binaries in a new namespace and executes twelve new cells.  It never loads the
earlier server-only v3 attempts.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from .luanti_15519_null_client_build import (
    _build_argv,
    _configure_argv,
    _validate_client_closure,
)
from .luanti_client_dependency_materializer import (
    load_luanti_client_dependency_closure,
)
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


CONFIRMATION = "EXECUTE_LUANTI_15687_CLIENT_GATE57_V1"
_CANDIDATE = CandidateKey("luanti", 15687)
_VALUE = _CANDIDATE.to_value()
_PROFILE_ID = "luanti-15687-matrix-scale-v2-client-gate57-v1"
_VERSION = "1.0"
_BUILD_KIND = "candidate_gate57_15687_client_build"
_ATTEMPT_KIND = "candidate_gate57_15687_client_attempt"
_PREFLIGHT = ArtifactReference(
    "candidate_gate57_15687_client_preflight/luanti/15687/v1",
    "candidate_gate57_15687_client_preflight",
    _VERSION,
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_15687_client_aggregate/luanti/15687/v1",
    "candidate_gate57_15687_client_aggregate",
    _VERSION,
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_15687_client_transition/luanti/15687/v1",
    "candidate_gate57_15687_client_transition",
    _VERSION,
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_15687_client_blocker/luanti/15687/v1",
    "candidate_gate57_15687_client_blocker",
    _VERSION,
)
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_CHAIN = (
    ArtifactReference(
        "candidate_source_packet/luanti/15687/source/v2",
        "candidate_source_packet",
        "2.0",
    ),
    ArtifactReference(
        "candidate_test_patch/luanti/15687/screening/v2",
        "candidate_test_patch",
        "2.0",
    ),
    ArtifactReference(
        "source_atomicity/luanti/15687/preannotation/v2", "source_atomicity", "2.0"
    ),
    ArtifactReference(
        "candidate_stage/luanti/15687/test_manifest/v2", "candidate_stage", "2.0"
    ),
    ArtifactReference(
        "candidate_profile/luanti/15687/preflight/v2", "candidate_profile", "2.0"
    ),
    ArtifactReference(
        "candidate_preparation/luanti/15687/preannotation/v2",
        "candidate_preparation",
        "2.0",
    ),
    ArtifactReference(
        "candidate_downstream_plan/luanti/15687/preannotation/v2",
        "candidate_downstream_plan",
        "2.0",
    ),
    ArtifactReference(
        "fixed_source_capsule/luanti/15687/fixed/v2",
        "fixed_source_capsule",
        "2.0",
    ),
)
_CLIENT_CLOSURE = ArtifactReference(
    "client_dependency_closure/luanti/15519/darwin-arm64/v2",
    "client_dependency_closure",
    "2.0",
)
_F2P = NativeRegressionRun(
    phase="f2p",
    selector="matrix4",
    executable_path="bin/luanti",
    argv=("bin/luanti", "--run-unittests", "--test-module", "matrix4"),
    repetitions=3,
    fixed_expected_outcome="pass",
    buggy_expected_outcome="failure",
)
_P2P = NativeRegressionRun(
    phase="p2p",
    selector="TestSettings",
    executable_path="bin/luanti",
    argv=("bin/luanti", "--run-unittests", "--test-module", "TestSettings"),
    repetitions=3,
    fixed_expected_outcome="pass",
    buggy_expected_outcome="pass",
)


@dataclass(frozen=True)
class ClientGate57Result:
    aggregate: StableArtifact
    transition: StableArtifact
    executed_attempts: int
    qualified: bool


@dataclass(frozen=True)
class _Cell:
    run: NativeRegressionRun
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
        return ArtifactReference(
            f"{_ATTEMPT_KIND}/luanti/15687/{self.run.phase}/"
            f"{self.variant}/{self.repetition}/v1",
            _ATTEMPT_KIND,
            _VERSION,
        )


def execute_luanti_15687_client_gate57(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> ClientGate57Result:
    """Build the client pair and run exactly twelve fresh cells."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    _assert_fresh(root)
    plan = _client_plan(root)
    _validate_capsule(root, plan)
    closure = load_luanti_client_dependency_closure(root)
    dependencies = _validate_client_closure(root, closure)
    preflight = _write_preflight(root, chain, closure, authorization)
    workspace = _new_workspace(root, _CANDIDATE)
    builds: dict[str, ArtifactReference] = {}
    attempts: list[StableArtifact] = []
    try:
        trees = {variant: workspace / variant for variant in ("fixed", "buggy")}
        _copy_full_capsule(plan, trees["fixed"])
        _copy_full_capsule(plan, trees["buggy"])
        _apply_production_inverse(plan, trees["fixed"], trees["buggy"])
        _validate_test_bytes(trees)
        for variant in ("fixed", "buggy"):
            builds[variant] = _build(
                root,
                plan,
                closure,
                dependencies,
                variant,
                trees[variant],
                authorization,
            )
        for cell in _cells():
            attempts.append(
                _run_cell(
                    root,
                    cell,
                    trees[cell.variant],
                    builds[cell.variant],
                    authorization,
                )
            )
    except BaseException as error:
        blocker = _write_blocker(
            root,
            preflight,
            closure,
            builds,
            attempts,
            error,
            authorization,
        )
        return ClientGate57Result(blocker, blocker, len(attempts), False)
    finally:
        _remove_workspace(workspace, root)

    aggregate = _write_aggregate(root, preflight, attempts, authorization)
    transition = _write_transition(root, preflight, aggregate, authorization)
    return ClientGate57Result(
        aggregate, transition, len(attempts), bool(aggregate.payload["qualified"])
    )


def _load_chain(root: Path) -> tuple[StableArtifact, ...]:
    chain = tuple(_load(root, reference) for reference in _CHAIN)
    source, patch, atomicity, manifest, profile, preparation, downstream, capsule = chain
    if source.inputs != (_ENUMERATION,) or any(
        item.payload.get("candidate") != _VALUE for item in chain
    ):
        raise RuntimeError("#15687 current-v2 candidate binding is invalid")
    if (
        source.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths")
        != ["irr/include/matrix4.h", "src/client/content_cao.cpp"]
        or patch.payload.get("test_paths") != ["src/unittest/test_irr_matrix4.cpp"]
        or atomicity.payload.get("outcome") != "accepted"
        or manifest.payload.get("state") != "passed"
        or profile.payload.get("strict_gate_status", {}).get("test_api_compatibility")
        != "passed"
        or preparation.payload.get("state") != "prepared"
        or downstream.payload.get("state") != "planned"
        or capsule.payload.get("profile_id")
        != "luanti-15687-matrix-scale-v2-native-gate57"
    ):
        raise RuntimeError("#15687 current-v2 Gate 1--4 chain is invalid")
    return chain


def _client_plan(root: Path) -> NativeRegressionExecutionPlan:
    decision = plan_luanti_native_regression_execution(root, candidate=_CANDIDATE)
    if isinstance(decision, StageDecision):
        raise RuntimeError(
            f"#15687 native plan is blocked: {decision.details.get('reason')}"
        )
    return decision


def _assert_fresh(root: Path) -> None:
    for reference in (_PREFLIGHT, _AGGREGATE, _TRANSITION, _BLOCKER):
        if (root / "artifacts" / Path(reference.artifact_id) / _VERSION).exists():
            raise RuntimeError(f"#15687 client epoch is not fresh: {reference.artifact_id}")
    for kind in (_BUILD_KIND, _ATTEMPT_KIND):
        if (root / "artifacts" / kind / "luanti" / "15687").exists():
            raise RuntimeError(f"#15687 client epoch is not fresh: {kind}")


def _write_preflight(
    root: Path,
    chain: tuple[StableArtifact, ...],
    closure: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE_ID,
        "state": "ready_for_gate5_7",
        "binary_kind": "luanti-client-unit-test",
        "f2p_selector": "matrix4",
        "p2p_selector": "TestSettings",
        "repetitions": 3,
        "client_dependency_root": "private/dependencies/luanti-client/2.0",
        "old_server_attempts_loaded": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
        },
    }
    return _write(
        root,
        _PREFLIGHT,
        tuple(item.reference for item in chain) + (closure.reference,),
        payload,
        authorization,
    )


def _validate_test_bytes(trees: dict[str, Path]) -> None:
    for relative in (
        "src/unittest/test_irr_matrix4.cpp",
        "src/unittest/test_settings.cpp",
    ):
        fixed = (trees["fixed"] / relative).read_bytes()
        buggy = (trees["buggy"] / relative).read_bytes()
        if fixed != buggy:
            raise RuntimeError(f"#15687 fixed test bytes drifted: {relative}")


def _build(
    root: Path,
    plan: NativeRegressionExecutionPlan,
    closure: StableArtifact,
    dependencies: dict[str, Path],
    variant: str,
    tree: Path,
    authorization: AuthorizationRecord,
) -> ArtifactReference:
    home = root / "private" / "runtime-home" / "luanti-15687-client" / variant
    _prepare_closed_home(root, home)
    env = {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(plan.toolchain.root / "bin"),
    }
    last: ArtifactReference | None = None
    for step, argv, timeout in (
        ("configure", _configure_argv(plan, dependencies), 600),
        ("build", _build_argv(plan), 1800),
    ):
        args = tuple(str(value) for value in argv)
        receipt = run_bounded_process(args, cwd=tree, env=env, timeout_seconds=timeout)
        reference = ArtifactReference(
            f"{_BUILD_KIND}/luanti/15687/{variant}/{step}/v1",
            _BUILD_KIND,
            _VERSION,
        )
        payload = _process_payload(receipt)
        payload.update(
            {
                "candidate": _VALUE,
                "profile_id": _PROFILE_ID,
                "variant": variant,
                "step": step,
                "argv": list(args),
                "watchdog_seconds": timeout,
                "build_client": True,
                "build_server": False,
                "build_unittests": True,
                "old_attempts_loaded": False,
                "complete": True,
            }
        )
        _write(
            root,
            reference,
            (plan.profile.fixed_capsule_reference, closure.reference),
            payload,
            authorization,
        )
        last = reference
        if receipt.outcome != "completed" or receipt.returncode != 0:
            raise RuntimeError(f"#15687 {variant} client {step} failed")
    assert last is not None
    _ordinary_existing_file(tree / "bin/luanti", tree)
    return last


def _cells() -> tuple[_Cell, ...]:
    return tuple(
        _Cell(run, variant, repetition)
        for run in (_F2P, _P2P)
        for variant in ("fixed", "buggy")
        for repetition in (1, 2, 3)
    )


def _run_cell(
    root: Path,
    cell: _Cell,
    tree: Path,
    build: ArtifactReference,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    binary = tree / "bin/luanti"
    _ordinary_existing_file(binary, tree)
    home = root / "private" / "runtime-home" / "luanti-15687-client-tests" / cell.variant
    _prepare_closed_home(root, home)
    receipt = run_bounded_process(
        (str(binary), "--run-unittests", "--test-module", cell.run.selector),
        cwd=tree,
        env={
            "HOME": str(home),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": str(tree / "bin"),
        },
        timeout_seconds=300,
    )
    stdout, stdout_truncated = _bounded_output(receipt.stdout)
    stderr, stderr_truncated = _bounded_output(receipt.stderr)
    count = completed_native_test_count(
        cell.run, receipt.outcome, stdout, stderr
    )
    observed = _observed(receipt, count)
    payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE_ID,
        "execution_lineage": "candidate-gate57/luanti/15687/client/v1",
        "phase": cell.run.phase,
        "variant": cell.variant,
        "repetition": cell.repetition,
        "selector": cell.run.selector,
        "expected_outcome": cell.expected,
        "observed_outcome": observed,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "completed_test_count": count,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": stdout_truncated or receipt.stdout_truncated,
        "stderr_truncated": stderr_truncated or receipt.stderr_truncated,
        "output_encoding": "base64",
        "matched_expected": observed == cell.expected,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "complete": True,
    }
    return _write(root, cell.reference, (build,), payload, authorization)


def _observed(receipt: NativeProcessReceipt, count: int | None) -> str:
    if receipt.outcome == "timed_out":
        return "timed_out"
    if receipt.outcome == "launch_failed":
        return "launch_failed"
    if receipt.outcome != "completed" or type(receipt.returncode) is not int:
        return "invalid_process_result"
    if count != 1:
        return "invalid_test_count"
    return "pass" if receipt.returncode == 0 else "failure"


def _write_aggregate(
    root: Path,
    preflight: StableArtifact,
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 12 or len({item.reference for item in attempts}) != 12:
        raise RuntimeError("#15687 client attempt coverage is invalid")
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for attempt in attempts:
        groups.setdefault(
            (str(attempt.payload["phase"]), str(attempt.payload["variant"])), []
        ).append(attempt)
    expected = {
        ("f2p", "fixed"),
        ("f2p", "buggy"),
        ("p2p", "fixed"),
        ("p2p", "buggy"),
    }
    if set(groups) != expected or any(len(group) != 3 for group in groups.values()):
        raise RuntimeError("#15687 client repetition coverage is invalid")
    stable = all(
        len({item.payload["observed_outcome"] for item in group}) == 1
        for group in groups.values()
    )
    all_expected = all(item.payload["matched_expected"] is True for item in attempts)
    infrastructure = any(
        item.payload["observed_outcome"]
        in {"timed_out", "launch_failed", "invalid_test_count", "invalid_process_result"}
        for item in attempts
    )
    qualified = stable and all_expected and not infrastructure
    cells = [
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
        for phase, variant in sorted(expected)
    ]
    outcome = (
        "qualified"
        if qualified
        else "infrastructure_blocked"
        if infrastructure
        else "candidate_semantic_mismatch"
    )
    payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE_ID,
        "execution_lineage": "candidate-gate57/luanti/15687/client/v1",
        "state": "qualified" if qualified else "blocked",
        "outcome": outcome,
        "terminal": outcome == "candidate_semantic_mismatch",
        "resumable": outcome != "candidate_semantic_mismatch",
        "attempt_count": 12,
        "stable_repetitions": stable,
        "all_expected": all_expected,
        "qualified": qualified,
        "cells": cells,
        "old_server_attempts_loaded": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate_status": {
            "fixed_f2p_3x": _gate(groups[("f2p", "fixed")]),
            "buggy_f2p_3x": _gate(groups[("f2p", "buggy")]),
            "fixed_p2p_3x": _gate(groups[("p2p", "fixed")]),
            "buggy_p2p_3x": _gate(groups[("p2p", "buggy")]),
        },
    }
    return _write(
        root,
        _AGGREGATE,
        (preflight.reference, *(item.reference for item in attempts)),
        payload,
        authorization,
    )


def _gate(group: list[StableArtifact]) -> str:
    return "passed" if all(item.payload["matched_expected"] is True for item in group) else "failed"


def _write_transition(
    root: Path,
    preflight: StableArtifact,
    aggregate: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    qualified = aggregate.payload["qualified"] is True
    payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE_ID,
        "state": "passed" if qualified else "blocked",
        "outcome": aggregate.payload["outcome"],
        "qualified": qualified,
        "terminal": aggregate.payload["terminal"],
        "resumable": aggregate.payload["resumable"],
        "attempt_count": 12,
        "gate_status": aggregate.payload["gate_status"],
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "does_not_claim_gate_8_13": True,
    }
    reference = _TRANSITION if qualified else _BLOCKER
    return _write(
        root,
        reference,
        (preflight.reference, aggregate.reference),
        payload,
        authorization,
    )


def _write_blocker(
    root: Path,
    preflight: StableArtifact,
    closure: StableArtifact,
    builds: dict[str, ArtifactReference],
    attempts: list[StableArtifact],
    error: BaseException,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _VALUE,
        "profile_id": _PROFILE_ID,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": str(error),
        "attempt_count": len(attempts),
        "build_variants_completed": sorted(builds),
        "old_server_attempts_loaded": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "does_not_invalidate_gate_1_4": True,
    }
    inputs = (
        preflight.reference,
        closure.reference,
        *builds.values(),
        *(item.reference for item in attempts),
    )
    return _write(root, _BLOCKER, inputs, payload, authorization)


def _process_payload(receipt: NativeProcessReceipt) -> dict[str, object]:
    stdout, stdout_truncated = _bounded_output(receipt.stdout)
    stderr, stderr_truncated = _bounded_output(receipt.stderr)
    return {
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": stdout_truncated or receipt.stdout_truncated,
        "stderr_truncated": stderr_truncated or receipt.stderr_truncated,
        "output_encoding": "base64",
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
    artifact = _load(root, reference)
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("#15687 client typed reload failed")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#15687 client Gate 5--7 authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "CONFIRMATION",
    "ClientGate57Result",
    "execute_luanti_15687_client_gate57",
]
