"""Fresh Gate9/10 iconv-state semantic closure for Luanti #14464."""

from __future__ import annotations

import base64
import hashlib
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .luanti_native_executor import run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_14464_ICONV_REPLAY_V4"
_CANDIDATE = {"repository_key": "luanti", "change_number": 14464}
_PAIR = PurePosixPath("private/materialized/luanti/14464/gate8-v1")
_BUILD = ArtifactReference(
    "candidate_gate8_14464_native_build/luanti/14464/v1",
    "candidate_gate8_14464_native_build",
    "1.0",
)
_READINESS = ArtifactReference(
    "candidate_gate8_14464_build_readiness/luanti/14464/v1",
    "candidate_gate8_14464_build_readiness",
    "1.0",
)
_ADAPTER = ArtifactReference(
    "candidate_gate8_14464_generated_config_adapter/luanti/14464/v1",
    "candidate_gate8_14464_generated_config_adapter",
    "1.0",
)
_DEPENDENCY = ArtifactReference(
    "candidate_gate8_14464_irrlicht_dependency/luanti/14464/current-v2/v1",
    "candidate_gate8_14464_irrlicht_dependency",
    "1.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/14464/gate8-v1",
    "candidate_materialization",
    "1.0",
)
_PLAN = ArtifactReference(
    "candidate_gate9_10_14464_runtime_plan/luanti/14464/v4",
    "candidate_gate9_10_14464_runtime_plan",
    "4.0",
)
_FIXTURE = ArtifactReference(
    "candidate_gate9_14464_runtime_fixture/luanti/14464/iconv-v4",
    "candidate_gate9_14464_runtime_fixture",
    "4.0",
)
_ORACLE = ArtifactReference(
    "candidate_gate10_14464_iconv_oracle/luanti/14464/iconv-v4",
    "candidate_gate10_14464_iconv_oracle",
    "4.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate11_13_14464_client_blocker/luanti/14464/v4",
    "candidate_gate11_13_14464_client_blocker",
    "4.0",
)
_SUMMARY = re.compile(
    rb"(?m)^======== Module TestUtilities (passed|failed) \((\d+) failures / (\d+) tests\) - \d+ms\r?$"
)
_FAILURE = re.compile(rb"(?m)^\[FAIL\] ([A-Za-z0-9_]+) - \d+ms\r?$")
_BUGGY_FAILURES = {"testUTF8", "testWrapRows", "testSanitizeDirName"}


def execute_luanti_14464_gate9_10(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    build, readiness, adapter, dependency, materialization = (
        _load(root, ref)
        for ref in (_BUILD, _READINESS, _ADAPTER, _DEPENDENCY, _MATERIALIZATION)
    )
    if not (
        build.payload.get("state") == "built"
        and readiness.payload.get("state") == "ready_for_sequential_build"
        and adapter.payload.get("forbidden_flag_used") is False
        and dependency.payload.get("system_dependency_closure_used") is False
        and materialization.payload.get("state") == "materialized"
    ):
        raise RuntimeError("#14464 Gate9/10 input chain drifted")
    inputs = (
        build.reference,
        readiness.reference,
        adapter.reference,
        dependency.reference,
        materialization.reference,
    )
    plan = _write_once(
        root,
        _PLAN,
        inputs,
        {
            "candidate": _CANDIDATE,
            "state": "planned",
            "outcome": "historical_module_selector_iconv_replay_planned",
            "complete": True,
            "supersedes_plans": [
                "candidate_gate9_10_14464_runtime_plan/luanti/14464/v1",
                "candidate_gate9_10_14464_runtime_plan/luanti/14464/v2",
                "candidate_gate9_10_14464_runtime_plan/luanti/14464/v3",
            ],
            "registered_start": {
                "binary_pair": build.reference.artifact_id,
                "generated_config_adapter": adapter.reference.artifact_id,
                "initial_iconv_state": "fresh_process",
                "target_test": "TestUtilities.testUTF8",
            },
            "ordered_replay": [
                {"type": "start_fresh_candidate_binary"},
                {"type": "select_historical_module", "selector": "TestUtilities"},
                {"type": "execute_target_inside_module", "target": "testUTF8"},
                {"type": "parse_unique_raw_module_summary"},
                {"type": "bind_buggy_failure_diagnostic_to_testUTF8"},
            ],
            "command": [
                "bin/minetestserver",
                "--run-unittests",
                "--test-module",
                "TestUtilities",
            ],
            "input_sequence_hex": ["c4", "81"],
            "fresh_namespace": "candidate_gate9_14464_iconv_v4",
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    fixture = _write_once(
        root,
        _FIXTURE,
        (plan.reference,),
        {
            "candidate": _CANDIDATE,
            "state": "registered",
            "complete": True,
            "registered_start": plan.payload["registered_start"],
            "ordered_replay": plan.payload["ordered_replay"],
            "module_selector": "TestUtilities",
            "target_test": "testUTF8",
            "raw_summary_pattern": _SUMMARY.pattern.decode("ascii"),
            "fixed_expected": {"module_state": "passed", "failure_count": 0},
            "buggy_expected": {
                "module_state": "failed",
                "failure_count": 3,
                "failure_names": sorted(_BUGGY_FAILURES),
            },
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    attempts = tuple(
        _run_variant(root, variant, build, fixture, authorization)
        for variant in ("fixed", "buggy")
    )
    by_variant = {str(item.payload["variant"]): item for item in attempts}
    fixed = by_variant["fixed"].payload
    buggy = by_variant["buggy"].payload
    oracle = _write_once(
        root,
        _ORACLE,
        (plan.reference, fixture.reference, *(item.reference for item in attempts)),
        {
            "candidate": _CANDIDATE,
            "state": "passed",
            "outcome": "fresh_iconv_state_hidden_oracle_passed",
            "complete": True,
            "gate9_registered_start": "passed",
            "gate10_ordered_replay_hidden_oracle": "passed",
            "registered_start_equal": (
                fixed["registered_start"]["initial_iconv_state"]
                == buggy["registered_start"]["initial_iconv_state"]
                == "fresh_process"
                and fixed["registered_start"]["module_selector"]
                == buggy["registered_start"]["module_selector"]
                == "TestUtilities"
            ),
            "fixed_start_binary_fingerprint": fixed["registered_start"]["binary_fingerprint"],
            "buggy_start_binary_fingerprint": buggy["registered_start"]["binary_fingerprint"],
            "binary_pair_distinct_and_bound": (
                fixed["registered_start"]["binary_fingerprint"]
                != buggy["registered_start"]["binary_fingerprint"]
            ),
            "ordered_replay_executed": True,
            "fixed_goal_observation": fixed["semantic_observation"],
            "buggy_defect_observation": buggy["semantic_observation"],
            "fixed_module_summary": fixed["raw_module_summary"],
            "buggy_module_summary": buggy["raw_module_summary"],
            "buggy_failure_bound_to_testUTF8": buggy["target_diagnostic_observed"],
            "buggy_failure_names": buggy["failure_names"],
            "no_timeout_or_unknown": fixed["no_timeout_or_unknown"] and buggy["no_timeout_or_unknown"],
            "same_lineage": {
                "build": build.reference.artifact_id,
                "adapter": adapter.reference.artifact_id,
                "plan": plan.reference.artifact_id,
                "fixture": fixture.reference.artifact_id,
            },
            "gate11_to_13": "pending_candidate_owned_real_clients",
            "old_attempts_loaded": False,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    blocker = _write_once(
        root,
        _BLOCKER,
        (oracle.reference,),
        {
            "candidate": _CANDIDATE,
            "state": "blocked",
            "outcome": "infrastructure_blocked",
            "terminal": False,
            "resumable": True,
            "does_not_invalidate_gate_1_7": True,
            "completed_downstream": {
                "gate8_fixed_buggy_server_build": True,
                "gate9_registered_start": True,
                "gate10_ordered_replay_hidden_oracle": True,
            },
            "reason": "candidate-owned fixed and buggy real clients are not built",
            "synthetic_frames_allowed": False,
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "next_action": "build both clients with the same generated-config adapter, then execute the registered real-F12 visual plan",
        },
        authorization,
    )
    return plan, fixture, oracle, blocker


def _run_variant(
    root: Path,
    variant: str,
    build: StableArtifact,
    fixture: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    ref = ArtifactReference(
        f"candidate_gate9_14464_runtime_observation/luanti/14464/{variant}/iconv-v4",
        "candidate_gate9_14464_runtime_observation",
        "4.0",
    )
    try:
        _load(root, ref)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    else:
        raise RuntimeError("#14464 fresh Gate9 observation already exists")
    binary_value = build.payload.get("binaries", {}).get(variant, {}).get("path")
    binary = _ordinary_executable(root / PurePosixPath(str(binary_value)), root)
    tree = _ordinary_dir(root / _PAIR / variant, root)
    argv = (str(binary), "--run-unittests", "--test-module", "TestUtilities")
    env = {
        "HOME": str(root / "private/runtime-home/luanti-14464-iconv-v4" / variant),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = run_bounded_process(argv, cwd=tree, env=env, timeout_seconds=300)
    merged = receipt.stdout + b"\n" + receipt.stderr
    summaries = list(_SUMMARY.finditer(merged))
    if len(summaries) != 1:
        raise RuntimeError(f"#14464 {variant} raw module summary count is invalid")
    summary = summaries[0]
    module_state = summary.group(1).decode("ascii")
    failures = int(summary.group(2))
    tests = int(summary.group(3))
    failure_names = {
        match.group(1).decode("ascii") for match in _FAILURE.finditer(merged)
    }
    diagnostic = "testUTF8" in failure_names
    matched = (
        module_state == "passed" and failures == 0
        if variant == "fixed"
        else module_state == "failed" and failures == 3 and failure_names == _BUGGY_FAILURES
    )
    semantic = "goal_iconv_state_reset" if variant == "fixed" else "defect_iconv_state_regression"
    payload = {
        "candidate": _CANDIDATE,
        "variant": variant,
        "state": "observed",
        "registered_start": {
            "binary_fingerprint": _sha256(binary),
            "initial_iconv_state": "fresh_process",
            "module_selector": "TestUtilities",
        },
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "raw_module_summary": summary.group(0).decode("utf-8"),
        "module_state": module_state,
        "failure_count": failures,
        "test_count": tests,
        "target_diagnostic_observed": diagnostic,
        "failure_names": sorted(failure_names),
        "semantic_observation": semantic,
        "matched_expected": matched,
        "no_timeout_or_unknown": receipt.outcome == "completed",
        "stdout_base64": base64.b64encode(receipt.stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(receipt.stderr).decode("ascii"),
        "fresh_receipt": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    expected_rc = 0 if variant == "fixed" else 1
    if not (
        receipt.outcome == "completed"
        and receipt.returncode == expected_rc
        and payload["matched_expected"]
    ):
        raise RuntimeError(f"#14464 {variant} Gate9 semantic oracle failed: {payload}")
    return _write_once(
        root,
        ref,
        (build.reference, fixture.reference),
        payload,
        authorization,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ordinary_dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError("#14464 runtime directory is unsafe")
    return resolved


def _ordinary_executable(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise RuntimeError("#14464 runtime binary is unsafe")
    return resolved


def _load(root: Path, ref: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=ref.artifact_id,
        artifact_kind=ref.artifact_kind,
        semantic_version=ref.semantic_version,
    )


def _write_once(
    root: Path,
    ref: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    try:
        existing = _load(root, ref)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"#14464 artifact conflicts: {ref.artifact_id}")
        return existing
    write_stable_artifact(
        root,
        artifact_id=ref.artifact_id,
        artifact_kind=ref.artifact_kind,
        semantic_version=ref.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    result = _load(root, ref)
    if result.inputs != inputs or dict(result.payload) != payload:
        raise RuntimeError(f"#14464 typed reload failed: {ref.artifact_id}")
    return result


def _require_auth(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14464 runtime authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CONFIRMATION", "execute_luanti_14464_gate9_10"]
