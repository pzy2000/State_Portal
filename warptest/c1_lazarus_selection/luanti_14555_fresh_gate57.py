"""Fresh native Gate 5--7 epoch for Luanti #14555.

The old #14555 projection used curated repetition receipts and a Lua P2P
control.  This adapter deliberately starts a new namespace and binds every
cell to the current v2 source/patch/atomicity/manifest chain, a candidate
specific native P2P registration, and newly executed native build receipts.
It never loads or resumes a historical attempt.  The P2P control is the
unchanged ``TestSettings`` module already present in both current trees; no
test or production file is changed by this runner.
"""

from __future__ import annotations

import base64
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_14555_FRESH_NATIVE_GATE57"
_CANDIDATE = {"repository_key": "luanti", "change_number": 14555}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14555/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14555/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14555/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14555/test_manifest/v2", "candidate_stage", "2.0"
)
_MATERIALIZATION = ArtifactReference(
    "candidate_stage/luanti/14555/version_materialization/v2", "candidate_stage", "2.0"
)
_REGISTRATION = ArtifactReference(
    "candidate_gate57_14555_p2p_registration/luanti/14555/v1",
    "candidate_gate57_14555_p2p_registration",
    "1.0",
)
_TEST_BINDING = ArtifactReference(
    "candidate_gate57_14555_fresh_test_binding/luanti/14555/v1",
    "candidate_gate57_14555_fresh_test_binding",
    "1.0",
)
_PROFILE = ArtifactReference(
    "candidate_gate57_14555_fresh_profile/luanti/14555/v1",
    "candidate_gate57_14555_fresh_profile",
    "1.0",
)
_PREPARATION = ArtifactReference(
    "candidate_gate57_14555_fresh_preparation/luanti/14555/v1",
    "candidate_gate57_14555_fresh_preparation",
    "1.0",
)
_BUILD_KIND = "candidate_gate57_14555_build"
_ATTEMPT_KIND = "candidate_gate57_14555_fresh_attempt"
_AGGREGATE = ArtifactReference(
    "candidate_gate57_14555_fresh_aggregate/luanti/14555/v1",
    "candidate_gate57_14555_fresh_aggregate",
    "1.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_14555_fresh_transition/luanti/14555/v1",
    "candidate_gate57_14555_fresh_transition",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_14555_fresh_blocker/luanti/14555/v1",
    "candidate_gate57_14555_fresh_blocker",
    "1.0",
)

_F2P_PATH = "src/unittest/test_voxelalgorithms.cpp"
_F2P_SELECTOR = "TestVoxelAlgorithms"
_P2P_PATH = "src/unittest/test_settings.cpp"
_P2P_SELECTOR = "TestSettings"
_PRODUCTION_PATH = "src/voxelalgorithms.cpp"
_TREE_ROOT = Path("private/bounded-runs/luanti")
_FIXED_TREE = _TREE_ROOT / "14555-gate57-fixed"
_BUGGY_TREE = _TREE_ROOT / "14555-gate57-buggy"
_BUILD_ROOT = "build-server-unit"
_TOOLCHAIN = Path("private/dependencies/luanti-native/3.0")
_MAX_OUTPUT = 262_144


@dataclass(frozen=True)
class _Cell:
    phase: Literal["f2p", "p2p"]
    variant: Literal["fixed", "buggy"]
    repetition: int
    selector: str
    test_path: str
    expected: Literal["pass", "failure"]

    @property
    def reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_gate57_14555_fresh_attempt/luanti/14555/{self.phase}/{self.variant}/{self.repetition}/v1",
            _ATTEMPT_KIND,
            "1.0",
        )


@dataclass(frozen=True)
class FreshResult:
    aggregate: StableArtifact
    transition: StableArtifact
    attempts: int


def prepare_luanti_14555_fresh(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    """Register the current-v2 P2P control and fresh execution namespace."""

    _auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    source, patch, atomicity, manifest, materialization = chain
    fixed = root / _FIXED_TREE
    buggy = root / _BUGGY_TREE
    for tree in (fixed, buggy):
        _ordinary(tree / _F2P_PATH)
        _ordinary(tree / _P2P_PATH)
        _ordinary(tree / _PRODUCTION_PATH)
    if (fixed / _P2P_PATH).read_bytes() != (buggy / _P2P_PATH).read_bytes():
        raise RuntimeError("#14555 P2P registration source differs between variants")
    if (
        source.payload.get("candidate") != _CANDIDATE
        or patch.payload.get("candidate") != _CANDIDATE
    ):
        raise RuntimeError("#14555 current-v2 candidate binding drifted")

    registration_payload = {
        "candidate": _CANDIDATE,
        "state": "passed",
        "base_manifest_reference": _MANIFEST.artifact_id,
        "registration_kind": "current_v2_candidate_specific_p2p_extension",
        "registered_tests": [
            {
                "phase": "f2p",
                "path": _F2P_PATH,
                "selector": _F2P_SELECTOR,
                "expected": {"fixed": "pass", "buggy": "failure"},
            },
            {
                "phase": "p2p",
                "path": _P2P_PATH,
                "selector": _P2P_SELECTOR,
                "expected": {"fixed": "pass", "buggy": "pass"},
            },
        ],
        "test_api_compatibility": "passed",
        "fixed_only_api": False,
        "production_or_test_modified": False,
        "same_test_bytes_equal": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    registration_dir = (
        root
        / "artifacts"
        / Path(_REGISTRATION.artifact_id)
        / _REGISTRATION.semantic_version
    )
    if registration_dir.exists():
        registration = _load(root, _REGISTRATION)
        if (
            registration.inputs != tuple(a.reference for a in chain)
            or registration.payload != registration_payload
        ):
            raise RuntimeError("#14555 existing P2P registration drifted")
    else:
        _write(
            root,
            _REGISTRATION,
            tuple(a.reference for a in chain),
            registration_payload,
            authorization,
        )
        registration = _load(root, _REGISTRATION)

    profile_payload = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-14555-native-current-v2-fresh-gate57-v1",
        "current_v2_chain": [artifact.reference.artifact_id for artifact in chain],
        "p2p_registration_reference": registration.reference.artifact_id,
        "fixed_tree_path": _FIXED_TREE.as_posix(),
        "buggy_tree_path": _BUGGY_TREE.as_posix(),
        "binary_relative_path": "bin/luantiserver",
        "build_mode": {
            "BUILD_CLIENT": False,
            "BUILD_SERVER": True,
            "BUILD_UNITTESTS": True,
        },
        "f2p": {
            "path": _F2P_PATH,
            "selector": _F2P_SELECTOR,
            "fixed_expected": "pass",
            "buggy_expected": "failure",
            "repetitions": 3,
        },
        "p2p": {
            "path": _P2P_PATH,
            "selector": _P2P_SELECTOR,
            "fixed_expected": "pass",
            "buggy_expected": "pass",
            "repetitions": 3,
        },
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    _write(
        root,
        _PROFILE,
        tuple(a.reference for a in chain) + (registration.reference,),
        profile_payload,
        authorization,
    )
    profile = _load(root, _PROFILE)
    preparation_payload = {
        "candidate": _CANDIDATE,
        "profile_reference": profile.reference.artifact_id,
        "state": "prepared",
        "outcome": "downstream_pending",
        "fresh_epoch": "candidate_gate57_14555_fresh_native_v1",
        "required_attempt_count": 12,
        "inherit_prior_attempts": False,
        "old_attempts_loaded": False,
        "p2p_registration_reference": registration.reference.artifact_id,
        "paid_actions_authorized": False,
    }
    _write(root, _PREPARATION, (profile.reference,), preparation_payload, authorization)
    preparation = _load(root, _PREPARATION)
    return profile, preparation, registration


def execute_luanti_14555_fresh(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> FreshResult:
    """Execute exactly 12 fresh native cells, then persist aggregate/transition."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    profile = _load(root, _PROFILE)
    preparation = _load(root, _PREPARATION)
    registration = _load(root, _REGISTRATION)
    chain_refs = tuple(a.reference for a in chain) + (
        profile.reference,
        registration.reference,
    )
    builds = tuple(
        _build_variant(root, tree, variant, chain_refs, authorization)
        for variant, tree in (("fixed", _FIXED_TREE), ("buggy", _BUGGY_TREE))
    )
    attempts: list[StableArtifact] = []
    for cell in _cells():
        build = builds[0] if cell.variant == "fixed" else builds[1]
        attempt = _run_cell(
            root,
            cell,
            build,
            tuple(a.reference for a in chain)
            + (profile.reference, preparation.reference, registration.reference),
            authorization,
        )
        attempts.append(attempt)
        if attempt.payload["process_outcome"] != "completed":
            break
    aggregate = _aggregate(
        root, profile, preparation, registration, builds, attempts, authorization
    )
    if aggregate.payload["qualified"]:
        transition_reference = _TRANSITION
        payload = {
            "candidate": _CANDIDATE,
            "state": "passed",
            "outcome": "qualified",
            "candidate_execution_performed": True,
            "old_attempts_loaded": False,
            "gate_1_7_status": {
                "source_authority": "passed",
                "single_defect_mapping": "passed",
                "strict_inverse_patch": "passed",
                "test_api_compatibility": "passed",
                "fixed_f2p_3x": "passed",
                "buggy_f2p_3x": "passed_clean_failure",
                "fixed_p2p_3x": "passed",
                "buggy_p2p_3x": "passed",
            },
            "aggregate_reference": aggregate.reference.artifact_id,
            "deferred_until_after_gate5_7": [
                "gate8_full_versions",
                "gate9_start_replay_oracle_observations",
                "gate10_goldens",
                "gate11_visual_cross_judgment",
                "gate12_same_lineage_receipt",
                "gate13_freeze",
            ],
            "paid_actions_authorized": False,
        }
    else:
        transition_reference = _BLOCKER
        mismatch = any(a.payload.get("matched_expected") is False for a in attempts)
        payload = {
            "candidate": _CANDIDATE,
            "state": "terminal" if mismatch else "blocked",
            "outcome": "candidate_semantic_mismatch"
            if mismatch
            else "infrastructure_blocked",
            "terminal": mismatch,
            "resumable": not mismatch,
            "candidate_execution_performed": bool(attempts),
            "aggregate_reference": aggregate.reference.artifact_id,
            "old_attempts_loaded": False,
            "next_action": "inspect_candidate_specific_failure_without_reusing_attempts",
            "paid_actions_authorized": False,
        }
    _write(
        root,
        transition_reference,
        (
            profile.reference,
            preparation.reference,
            registration.reference,
            *tuple(a.reference for a in builds),
            aggregate.reference,
        ),
        payload,
        authorization,
    )
    transition = _load(root, transition_reference)
    return FreshResult(aggregate, transition, len(attempts))


def write_luanti_14555_test_binding_receipt(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Record the frozen-test equality used by the fresh native epoch."""

    _auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    registration = _load(root, _REGISTRATION)
    fixed = root / _FIXED_TREE
    buggy = root / _BUGGY_TREE
    paths = (_F2P_PATH, _P2P_PATH)
    if any(
        not (fixed / path).is_file() or not (buggy / path).is_file() for path in paths
    ):
        raise RuntimeError("#14555 fresh test binding inputs are incomplete")
    equal = {
        path: (fixed / path).read_bytes() == (buggy / path).read_bytes()
        for path in paths
    }
    if not all(equal.values()):
        raise RuntimeError("#14555 fresh frozen test equality failed")
    payload = {
        "candidate": _CANDIDATE,
        "state": "passed",
        "binding_kind": "frozen_upstream_tests_shared_by_fixed_and_buggy",
        "test_paths": list(paths),
        "fixed_buggy_test_bytes_equal": equal,
        "production_inverse_only": True,
        "fresh_bounded_tree_paths": [_FIXED_TREE.as_posix(), _BUGGY_TREE.as_posix()],
        "p2p_registration_reference": registration.reference.artifact_id,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    inputs = tuple(a.reference for a in chain) + (registration.reference,)
    binding_dir = (
        root
        / "artifacts"
        / Path(_TEST_BINDING.artifact_id)
        / _TEST_BINDING.semantic_version
    )
    if not binding_dir.exists():
        _write(root, _TEST_BINDING, inputs, payload, authorization)
    receipt = _load(root, _TEST_BINDING)
    if receipt.inputs != inputs or receipt.payload != payload:
        raise RuntimeError("#14555 fresh test binding receipt drifted")
    return receipt


def _build_variant(
    root: Path,
    tree_relative: Path,
    variant: str,
    inputs: tuple[ArtifactReference, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    tree = root / tree_relative
    build = tree / _BUILD_ROOT
    reference = ArtifactReference(
        f"candidate_gate57_14555_build/luanti/14555/{variant}/v1",
        _BUILD_KIND,
        "1.0",
    )
    existing_dir = (
        root / "artifacts" / Path(reference.artifact_id) / reference.semantic_version
    )
    if existing_dir.exists():
        existing = _load(root, reference)
        if (
            existing.payload.get("candidate") == _CANDIDATE
            and existing.payload.get("variant") == variant
            and existing.payload.get("old_attempts_loaded") is False
            and (root / existing.payload["binary_path"]).is_file()
        ):
            return existing
        raise RuntimeError(f"#14555 existing {variant} build receipt drifted")
    tool = root / _TOOLCHAIN / "bin"
    env = {
        "PATH": str(tool),
        "HOME": str(root / "private/runtime-home/luanti-14555-gate57"),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_DIR": "/dev/null",
    }
    configure_argv = [
        str(tool / "cmake"),
        "-S",
        str(tree),
        "-B",
        str(build),
        "-G",
        "Ninja",
        "-DBUILD_CLIENT=FALSE",
        "-DBUILD_SERVER=TRUE",
        "-DBUILD_UNITTESTS=TRUE",
    ]
    build_argv = [
        str(tool / "cmake"),
        "--build",
        str(build),
        "--target",
        "luantiserver",
        "--parallel",
        "1",
    ]
    configure = _run_process(configure_argv, tree, env, 180)
    compile_receipt = _run_process(build_argv, tree, env, 300)
    binary = tree / "bin/luantiserver"
    if (
        configure["process_outcome"] != "completed"
        or configure["returncode"] != 0
        or compile_receipt["process_outcome"] != "completed"
        or compile_receipt["returncode"] != 0
        or not binary.is_file()
    ):
        raise RuntimeError(f"#14555 {variant} build did not complete")
    payload = {
        "candidate": _CANDIDATE,
        "variant": variant,
        "tree_path": tree_relative.as_posix(),
        "binary_path": binary.relative_to(root).as_posix(),
        "binary_size_bytes": binary.stat().st_size,
        "configure": configure,
        "build": compile_receipt,
        "build_mode": {
            "BUILD_CLIENT": False,
            "BUILD_SERVER": True,
            "BUILD_UNITTESTS": True,
        },
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    _write(root, reference, inputs, payload, authorization)
    return _load(root, reference)


def _run_cell(
    root: Path,
    cell: _Cell,
    build: StableArtifact,
    inputs: tuple[ArtifactReference, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    tree = root / (_FIXED_TREE if cell.variant == "fixed" else _BUGGY_TREE)
    binary = tree / "bin/luantiserver"
    env = {
        "PATH": str(root / _TOOLCHAIN / "bin"),
        "HOME": str(root / "private/runtime-home/luanti-14555-gate57"),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_DIR": "/dev/null",
    }
    argv = [str(binary), "--run-unittests", "--test-module", cell.selector]
    result = _run_process(argv, tree, env, 120)
    stderr = base64.b64decode(result["stderr_base64"])
    observed = (
        "timed_out"
        if result["process_outcome"] == "timed_out"
        else "launch_failed"
        if result["process_outcome"] == "launch_failed"
        else ("pass" if result["returncode"] == 0 else "failure")
        if result["process_outcome"] == "completed"
        else "unknown"
    )
    payload = {
        "candidate": _CANDIDATE,
        "phase": cell.phase,
        "variant": cell.variant,
        "repetition": cell.repetition,
        "selector": cell.selector,
        "test_path": cell.test_path,
        "expected_outcome": cell.expected,
        "observed_outcome": observed,
        "process_outcome": result["process_outcome"],
        "returncode": result["returncode"],
        "argv": argv,
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_base64": result["stdout_base64"],
        "stderr_contains_selector": cell.selector.encode() in stderr,
        "matched_expected": observed == cell.expected,
        "fresh_receipt": True,
        "old_attempts_loaded": False,
        "build_reference": build.reference.artifact_id,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
    }
    _write(root, cell.reference, inputs + (build.reference,), payload, authorization)
    return _load(root, cell.reference)


def _aggregate(
    root: Path,
    profile: StableArtifact,
    preparation: StableArtifact,
    registration: StableArtifact,
    builds: tuple[StableArtifact, StableArtifact],
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    groups = {
        (phase, variant): sorted(
            [
                a
                for a in attempts
                if a.payload.get("phase") == phase
                and a.payload.get("variant") == variant
            ],
            key=lambda a: a.payload.get("repetition", 0),
        )
        for phase in ("f2p", "p2p")
        for variant in ("fixed", "buggy")
    }
    complete = len(attempts) == 12 and all(len(v) == 3 for v in groups.values())
    stable = complete and all(
        len({a.payload.get("observed_outcome") for a in v}) == 1
        for v in groups.values()
    )
    expected = complete and all(
        a.payload.get("matched_expected") is True
        and a.payload.get("process_outcome") == "completed"
        for a in attempts
    )
    qualified = stable and expected
    outcome = (
        "qualified"
        if qualified
        else "infrastructure_blocked"
        if any(
            a.payload.get("observed_outcome")
            in {"timed_out", "launch_failed", "unknown"}
            for a in attempts
        )
        else "candidate_semantic_mismatch"
    )
    payload = {
        "candidate": _CANDIDATE,
        "state": "qualified" if qualified else "blocked",
        "outcome": outcome,
        "qualified": qualified,
        "terminal": outcome == "candidate_semantic_mismatch",
        "resumable": outcome != "candidate_semantic_mismatch",
        "attempt_count": len(attempts),
        "expected_attempt_count": 12,
        "repetitions_per_cell": 3,
        "stable_repetitions": stable,
        "all_expected": expected,
        "no_timeout_or_unknown": all(
            a.payload.get("observed_outcome")
            not in {"timed_out", "unknown", "launch_failed"}
            for a in attempts
        ),
        "cells": [
            {
                "phase": phase,
                "variant": variant,
                "observed_outcomes": [
                    a.payload.get("observed_outcome") for a in values
                ],
                "matched_expected": [a.payload.get("matched_expected") for a in values],
            }
            for (phase, variant), values in groups.items()
            if values
        ],
        "truth_table": {
            "fixed": {"f2p": "pass", "p2p": "pass"},
            "buggy": {"f2p": "failure", "p2p": "pass"},
        },
        "candidate_execution_performed": bool(attempts),
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
    }
    refs = (
        profile.reference,
        preparation.reference,
        registration.reference,
        *(b.reference for b in builds),
        *(a.reference for a in attempts),
    )
    _write(root, _AGGREGATE, refs, payload, authorization)
    return _load(root, _AGGREGATE)


def _run_process(
    argv: list[str], cwd: Path, env: dict[str, str], timeout: int
) -> dict[str, object]:
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        outcome = "completed"
        returncode: int | None = proc.returncode
        stdout, stderr = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as error:
        outcome, returncode = "timed_out", None
        stdout, stderr = error.stdout or b"", error.stderr or b""
    except OSError as error:
        outcome, returncode, stdout, stderr = (
            "launch_failed",
            None,
            b"",
            str(error).encode(),
        )
    return {
        "process_outcome": outcome,
        "returncode": returncode,
        "stdout_base64": base64.b64encode(stdout[:_MAX_OUTPUT]).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr[:_MAX_OUTPUT]).decode("ascii"),
        "stdout_truncated": len(stdout) > _MAX_OUTPUT,
        "stderr_truncated": len(stderr) > _MAX_OUTPUT,
        "timed_out": outcome == "timed_out",
    }


def _load_chain(root: Path) -> tuple[StableArtifact, ...]:
    chain = tuple(
        _load(root, ref)
        for ref in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _MATERIALIZATION)
    )
    if (
        any(a.payload.get("candidate") != _CANDIDATE for a in chain)
        or chain[0].payload.get("complete") is not True
        or chain[1].payload.get("outcome") != "patch_eligible"
        or chain[2].payload.get("outcome") != "accepted"
        or chain[3].payload.get("state") != "passed"
        or chain[4].payload.get("state") != "passed"
    ):
        raise RuntimeError("#14555 current-v2 Gate 1--4 chain is not passed")
    return chain


def _cells() -> tuple[_Cell, ...]:
    return tuple(
        _Cell(phase, variant, repetition, selector, path, expected)
        for phase, selector, path, buggy_expected in (
            ("f2p", _F2P_SELECTOR, _F2P_PATH, "failure"),
            ("p2p", _P2P_SELECTOR, _P2P_PATH, "pass"),
        )
        for variant, expected in (("fixed", "pass"), ("buggy", buggy_expected))
        for repetition in range(1, 4)
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> None:
    if (
        root / "artifacts" / Path(reference.artifact_id) / reference.semantic_version
    ).exists():
        raise RuntimeError(
            f"#14555 fresh artifact already exists: {reference.artifact_id}"
        )
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


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _ordinary(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"#14555 required ordinary file missing: {path}")


def _auth(value: AuthorizationRecord) -> None:
    if (
        value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("#14555 fresh authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "CONFIRMATION",
    "FreshResult",
    "execute_luanti_14555_fresh",
    "prepare_luanti_14555_fresh",
    "write_luanti_14555_test_binding_receipt",
]
