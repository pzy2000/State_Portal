"""Fresh current-v2 Gate 5--7 execution for Luanti #15519.

The persistent #15519 materialization is a null-client pair: the two
executables are ``bin/luanti`` and are built with ``BUILD_UNITTESTS=TRUE``.
The ordinary native profile intentionally targets ``bin/luantiserver`` and is
therefore not suitable for this pair.  This adapter binds the current v2
source/patch/atomicity/manifest/capsule chain plus the typed persistent pair,
then executes the registered client unit-test selectors in a new artifact
namespace.  It never reads the old native v3/v4 attempts or aggregate.

Only Gate 5--7 is in scope.  No Start/replay/oracle/visual/construction or
paid action is performed here.
"""

from __future__ import annotations

import base64
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal

from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_15519_CLIENT_GATE57"
_CANDIDATE = CandidateKey("luanti", 15519)
_VERSION = "1.0"
_PROFILE_ID = "luanti-15519-gettext-plural-trim-client-v1"
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/15519/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/15519/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/15519/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/15519/test_manifest/v2", "candidate_stage", "2.0"
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/15519/fixed/v2", "fixed_source_capsule", "2.0"
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/15519/fixed-buggy/v1",
    "candidate_materialization",
    "1.0",
)
_PREFLIGHT = ArtifactReference(
    "candidate_gate57_15519_client_preflight/luanti/15519/v1",
    "candidate_gate57_15519_client_preflight",
    _VERSION,
)
_ATTEMPT_KIND = "candidate_gate57_15519_client_attempt"
_AGGREGATE = ArtifactReference(
    "candidate_gate57_15519_client_aggregate/luanti/15519/v1",
    "candidate_gate57_15519_client_aggregate",
    _VERSION,
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_15519_client_transition/luanti/15519/v1",
    "candidate_gate57_15519_client_transition",
    _VERSION,
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_15519_client_blocker/luanti/15519/v1",
    "candidate_gate57_15519_client_blocker",
    _VERSION,
)
_MAX_STREAM = 262_144
_TIMEOUT = 300
_CATCH_SUCCESS = re.compile(
    rb"(?m)^All tests passed \(\d+ assertions? in (\d+) test cases?\)\s*$"
)
_CATCH_TOTAL = re.compile(rb"(?mi)^test cases:\s*(\d+)\b")
_MODULE_SUMMARY = re.compile(
    rb"(?m)^Unit Test Results: (?:PASSED|FAILED)\r?\n\s+(\d+) / (\d+) failed tests\.\s*$"
)


@dataclass(frozen=True)
class _Cell:
    phase: Literal["f2p", "p2p"]
    variant: Literal["fixed", "buggy"]
    repetition: int
    selector: str
    expected: Literal["pass", "failure"]

    @property
    def reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_gate57_15519_client_attempt/luanti/15519/"
            f"{self.phase}/{self.variant}/{self.repetition}/v1",
            _ATTEMPT_KIND,
            _VERSION,
        )


@dataclass(frozen=True)
class Gate57Result:
    aggregate: StableArtifact
    transition: StableArtifact
    attempts: int
    qualified: bool


def execute_luanti_15519_client_gate57(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> Gate57Result:
    """Run exactly twelve fresh fixed/buggy F2P/P2P client cells."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _check_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    chain, materialization = _validate_current_v2(root)
    preflight = _write_preflight(root, chain, materialization, authorization)
    attempts: list[StableArtifact] = []
    for cell in _cells():
        # A fresh namespace may be resumed only with an exactly matching typed
        # receipt.  No old native v3/v4 artifact is ever loaded here.
        existing = _try_load(root, cell.reference)
        if existing is not None:
            _validate_attempt(existing, cell, materialization)
            attempts.append(existing)
            continue
        binary = _binary_for(materialization, root, cell.variant)
        env = _runtime_environment(root, materialization, cell.variant)
        receipt = run_bounded_process(
            (str(binary), "--run-unittests", "--test-module", cell.selector),
            cwd=binary.parent.parent,
            env=env,
            timeout_seconds=_TIMEOUT,
        )
        attempts.append(
            _write_attempt(root, materialization, cell, receipt, authorization)
        )
    aggregate = _write_aggregate(
        root, preflight, materialization, attempts, authorization
    )
    transition = _write_transition(root, preflight, aggregate, authorization)
    return Gate57Result(
        aggregate, transition, len(attempts), bool(aggregate.payload["qualified"])
    )


def _validate_current_v2(
    root: Path,
) -> tuple[tuple[StableArtifact, ...], StableArtifact]:
    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    capsule = _load(root, _CAPSULE)
    materialization = _load(root, _MATERIALIZATION)
    if source.inputs != (
        ArtifactReference(
            "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
        ),
    ):
        raise RuntimeError("#15519 source is not current-v2 bound")
    if (
        source.payload.get("candidate") != _CANDIDATE.to_value()
        or source.payload.get("complete") is not True
    ):
        raise RuntimeError("#15519 source closure is invalid")
    if (
        patch.inputs != (source.inputs[0], source.reference)
        or patch.payload.get("candidate") != _CANDIDATE.to_value()
        or patch.payload.get("outcome") != "patch_eligible"
    ):
        raise RuntimeError("#15519 patch closure is invalid")
    if patch.payload.get("production_paths") != [
        "src/gettext_plural_form.cpp",
        "src/gettext_plural_form.h",
    ] or patch.payload.get("test_paths") != ["src/unittest/test_translations.cpp"]:
        raise RuntimeError("#15519 patch scope is invalid")
    if (
        atomicity.inputs
        != (
            source.inputs[0],
            source.reference,
            patch.reference,
            ArtifactReference(
                "screening_checkpoint/luanti/15519/current/v2",
                "screening_checkpoint",
                "2.0",
            ),
        )
        or atomicity.payload.get("outcome") != "accepted"
    ):
        raise RuntimeError("#15519 atomicity closure is invalid")
    if (
        manifest.inputs != (patch.reference, atomicity.reference)
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("details", {}).get("executable_test_paths")
        != ["src/unittest/test_translations.cpp"]
    ):
        raise RuntimeError("#15519 manifest closure is invalid")
    if (
        capsule.inputs != (source.reference, patch.reference, manifest.reference)
        or capsule.payload.get("profile_id") != "luanti-15519-gettext-plural-trim-v2"
        or capsule.payload.get("test_paths") != ["src/unittest/test_translations.cpp"]
        or capsule.payload.get("p2p_test_paths") != ["src/unittest/test_settings.cpp"]
    ):
        raise RuntimeError("#15519 capsule closure is invalid")
    payload = materialization.payload
    expected_base = "private/materialized/luanti/15519/null-client/v2"
    if (
        materialization.inputs[:1]
        != (
            ArtifactReference(
                "fixed_source_capsule/luanti/15519/fixed/v2",
                "fixed_source_capsule",
                "2.0",
            ),
        )
        or payload.get("candidate") != _CANDIDATE.to_value()
        or payload.get("profile_id") != "luanti-15519-gettext-plural-trim-v2"
        or payload.get("state") != "built"
        or payload.get("complete") is not True
        or payload.get("missing_required_evidence") != []
        or payload.get("fixed_tree_path") != f"{expected_base}/fixed"
        or payload.get("buggy_tree_path") != f"{expected_base}/buggy"
        or payload.get("fixed_binary_path") != f"{expected_base}/fixed/bin/luanti"
        or payload.get("buggy_binary_path") != f"{expected_base}/buggy/bin/luanti"
    ):
        raise RuntimeError("#15519 persistent pair is not current-v2 built")
    for variant in ("fixed", "buggy"):
        tree = root / PurePosixPath(payload[f"{variant}_tree_path"])
        binary = root / PurePosixPath(payload[f"{variant}_binary_path"])
        _ordinary_directory(tree, root)
        _ordinary_file(binary, tree)
        if not os.access(binary, os.X_OK):
            raise RuntimeError(f"#15519 {variant} client binary is not executable")
        cache = tree / "build-luanti-15519" / "CMakeCache.txt"
        _ordinary_file(cache, tree)
        cache_text = cache.read_text(encoding="utf-8", errors="strict")
        if (
            "BUILD_UNITTESTS:BOOL=TRUE" not in cache_text
            or "BUILD_CLIENT:BOOL=TRUE" not in cache_text
        ):
            raise RuntimeError(
                f"#15519 {variant} BUILD_UNITTESTS/client closure is invalid"
            )
        if "BUILD_SERVER:BOOL=FALSE" not in cache_text:
            raise RuntimeError(f"#15519 {variant} server-only build mismatch")
    return (source, patch, atomicity, manifest, capsule), materialization


def _write_preflight(
    root: Path,
    chain: tuple[StableArtifact, ...],
    materialization: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "state": "ready_for_gate5_7",
        "terminal": False,
        "resumable": True,
        "current_v2_only": True,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "binary_kind": "luanti-client-unit-test",
        "f2p_selector": "test translations",
        "p2p_selector": "TestSettings",
        "repetitions": 3,
        "version_materialization": materialization.reference.artifact_id,
        "gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
        },
    }
    inputs = tuple(item.reference for item in chain) + (materialization.reference,)
    existing = _try_load(root, _PREFLIGHT)
    if existing is None:
        write_stable_artifact(
            root,
            artifact_id=_PREFLIGHT.artifact_id,
            artifact_kind=_PREFLIGHT.artifact_kind,
            semantic_version=_PREFLIGHT.semantic_version,
            created_at=_now(),
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
        existing = _load(root, _PREFLIGHT)
    if existing.inputs != inputs or existing.payload != payload:
        raise RuntimeError("#15519 client Gate 5--7 preflight drifted")
    return existing


def _cells() -> tuple[_Cell, ...]:
    cells = tuple(
        _Cell(
            phase,
            variant,
            repetition,
            selector,
            "pass" if variant == "fixed" else buggy_expected,
        )
        for phase, selector, buggy_expected in (
            ("f2p", "test translations", "failure"),
            ("p2p", "TestSettings", "pass"),
        )
        for variant in ("fixed", "buggy")
        for repetition in (1, 2, 3)
    )
    if len(cells) != 12:
        raise RuntimeError("#15519 client Gate 5--7 cell coverage is invalid")
    return cells


def _write_attempt(
    root: Path,
    materialization: StableArtifact,
    cell: _Cell,
    receipt: NativeProcessReceipt,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    stdout, stdout_truncated = _bounded(receipt.stdout)
    stderr, stderr_truncated = _bounded(receipt.stderr)
    count = _completed_count(receipt, stdout, stderr)
    observed = _observed(receipt, count)
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "execution_lineage": "candidate-gate57/luanti/15519/client/v1",
        "phase": cell.phase,
        "variant": cell.variant,
        "repetition": cell.repetition,
        "selector": cell.selector,
        "executable_path": f"private/materialized/luanti/15519/null-client/v2/{cell.variant}/bin/luanti",
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
        "fresh_receipt": True,
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
    }
    artifact = _write_and_reload(
        root, cell.reference, (materialization.reference,), payload, authorization
    )
    _validate_attempt(artifact, cell, materialization)
    return artifact


def _write_aggregate(
    root: Path,
    preflight: StableArtifact,
    materialization: StableArtifact,
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 12 or len({item.reference for item in attempts}) != 12:
        raise RuntimeError("#15519 client Gate 5--7 attempt coverage is invalid")
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
        len(values) != 3 for values in groups.values()
    ):
        raise RuntimeError("#15519 client Gate 5--7 repetition coverage is invalid")
    cells = [
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
        for phase, variant in sorted(groups)
    ]
    stable = all(
        len({a.payload["observed_outcome"] for a in values}) == 1
        for values in groups.values()
    )
    expected = all(a.payload["matched_expected"] is True for a in attempts)
    infrastructure = any(
        a.payload["observed_outcome"]
        in {"timed_out", "launch_failed", "invalid_test_count"}
        for a in attempts
    )
    qualified = stable and expected and not infrastructure
    outcome = (
        "qualified"
        if qualified
        else (
            "infrastructure_blocked"
            if infrastructure
            else "candidate_semantic_mismatch"
        )
    )
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "execution_lineage": "candidate-gate57/luanti/15519/client/v1",
        "state": "qualified" if qualified else "blocked",
        "outcome": outcome,
        "terminal": outcome == "candidate_semantic_mismatch",
        "resumable": outcome != "candidate_semantic_mismatch",
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
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
            "fixed_f2p_3x": "passed"
            if _cell_expected(groups, "f2p", "fixed")
            else "failed",
            "buggy_f2p_3x": "passed"
            if _cell_expected(groups, "f2p", "buggy")
            else "failed",
            "fixed_p2p_3x": "passed"
            if _cell_expected(groups, "p2p", "fixed")
            else "failed",
            "buggy_p2p_3x": "passed"
            if _cell_expected(groups, "p2p", "buggy")
            else "failed",
        },
        "deferred_until_after_gate5_7": [
            "runtime_world_replay",
            "semantic_observations",
            "golden_cross_judgment",
            "construction_receipt",
        ],
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    return _write_and_reload(
        root, _AGGREGATE, tuple(a.reference for a in attempts), payload, authorization
    )


def _write_transition(
    root: Path,
    preflight: StableArtifact,
    aggregate: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    qualified = aggregate.payload["qualified"] is True
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "execution_lineage": "candidate-gate57/luanti/15519/client/v1",
        "state": "passed" if qualified else "blocked",
        "outcome": aggregate.payload["outcome"],
        "qualified": qualified,
        "terminal": aggregate.payload["terminal"],
        "resumable": aggregate.payload["resumable"],
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "attempt_count": 12,
        "gate_status": aggregate.payload["gate_status"],
        "does_not_claim_gate_8_13": True,
    }
    reference = _TRANSITION if qualified else _BLOCKER
    return _write_and_reload(
        root,
        reference,
        (preflight.reference, aggregate.reference),
        payload,
        authorization,
    )


def _validate_attempt(
    artifact: StableArtifact, cell: _Cell, materialization: StableArtifact
) -> None:
    payload = artifact.payload
    if (
        artifact.inputs != (materialization.reference,)
        or payload.get("candidate") != _CANDIDATE.to_value()
        or payload.get("profile_id") != _PROFILE_ID
        or payload.get("phase") != cell.phase
        or payload.get("variant") != cell.variant
        or payload.get("repetition") != cell.repetition
        or payload.get("selector") != cell.selector
        or payload.get("expected_outcome") != cell.expected
        or payload.get("fresh_receipt") is not True
        or payload.get("old_attempts_loaded") is not False
    ):
        raise RuntimeError("#15519 client attempt identity is invalid")
    stdout = _decode(payload, "stdout")
    stderr = _decode(payload, "stderr")
    count = _completed_count(
        NativeProcessReceipt(
            payload["process_outcome"],
            payload["returncode"],
            stdout,
            stderr,
            payload["stdout_truncated"],
            payload["stderr_truncated"],
        ),
        stdout,
        stderr,
    )
    observed = _observed(
        NativeProcessReceipt(
            payload["process_outcome"],
            payload["returncode"],
            stdout,
            stderr,
            payload["stdout_truncated"],
            payload["stderr_truncated"],
        ),
        count,
    )
    if (
        payload.get("completed_test_count") != count
        or payload.get("observed_outcome") != observed
        or payload.get("matched_expected") is not (observed == cell.expected)
    ):
        raise RuntimeError("#15519 client attempt result is invalid")


def _completed_count(
    receipt: NativeProcessReceipt, stdout: bytes, stderr: bytes
) -> int | None:
    if receipt.outcome != "completed":
        return None
    modules = [
        m for stream in (stdout, stderr) for m in _MODULE_SUMMARY.finditer(stream)
    ]
    successes = [
        m for stream in (stdout, stderr) for m in _CATCH_SUCCESS.finditer(stream)
    ]
    totals = [m for stream in (stdout, stderr) for m in _CATCH_TOTAL.finditer(stream)]
    if len(modules) == 1 and not successes and not totals:
        return 1 if int(modules[0].group(2)) > 0 else 0
    if len(successes) == 1 and not modules and not totals:
        return int(successes[0].group(1))
    if len(totals) == 1 and not modules and not successes:
        return int(totals[0].group(1))
    return 0


def _observed(receipt: NativeProcessReceipt, count: int | None) -> str:
    if receipt.outcome == "timed_out":
        return "timed_out"
    if receipt.outcome == "launch_failed":
        return "launch_failed"
    if receipt.outcome != "completed" or not isinstance(receipt.returncode, int):
        return "launch_failed"
    if count != 1:
        return "invalid_test_count"
    return "pass" if receipt.returncode == 0 else "failure"


def _cell_expected(
    groups: dict[tuple[str, str], list[StableArtifact]], phase: str, variant: str
) -> bool:
    return all(a.payload["matched_expected"] is True for a in groups[(phase, variant)])


def _binary_for(materialization: StableArtifact, root: Path, variant: str) -> Path:
    path = root / PurePosixPath(str(materialization.payload[f"{variant}_binary_path"]))
    _ordinary_file(
        path, root / PurePosixPath(str(materialization.payload[f"{variant}_tree_path"]))
    )
    if not os.access(path, os.X_OK):
        raise RuntimeError(f"#15519 {variant} client binary is not executable")
    return path


def _runtime_environment(
    root: Path, materialization: StableArtifact, variant: str
) -> dict[str, str]:
    home = root / "private" / "runtime-home" / "luanti-15519-client-gate57"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    tree = root / PurePosixPath(str(materialization.payload[f"{variant}_tree_path"]))
    return {"HOME": str(home), "LANG": "C", "LC_ALL": "C", "PATH": str(tree / "bin")}


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


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
    return _load(root, reference)


def _decode(payload: dict[str, object], stream: str) -> bytes:
    value = payload.get(f"{stream}_base64")
    if not isinstance(value, str):
        raise RuntimeError("#15519 client attempt output is invalid")
    try:
        data = base64.b64decode(value, validate=True)
    except Exception as error:
        raise RuntimeError("#15519 client attempt output is invalid") from error
    if len(data) > _MAX_STREAM:
        raise RuntimeError("#15519 client attempt output exceeds bound")
    return data


def _bounded(value: bytes) -> tuple[bytes, bool]:
    return (value, False) if len(value) <= _MAX_STREAM else (value[:_MAX_STREAM], True)


def _ordinary_root(path: Path) -> Path:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return path.resolve(strict=True)


def _ordinary_directory(path: Path, parent: Path) -> Path:
    info = path.lstat()
    resolved = path.resolve(strict=True)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or parent.resolve(strict=True) not in resolved.parents
    ):
        raise RuntimeError("materialized tree is unsafe")
    return resolved


def _ordinary_file(path: Path, parent: Path) -> Path:
    info = path.lstat()
    resolved = path.resolve(strict=True)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or parent.resolve(strict=True) not in resolved.parents
    ):
        raise RuntimeError("materialized file is unsafe")
    return resolved


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _check_authorization(authorization: AuthorizationRecord) -> None:
    if (
        authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#15519 client Gate 5--7 authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CONFIRMATION", "Gate57Result", "execute_luanti_15519_client_gate57"]
