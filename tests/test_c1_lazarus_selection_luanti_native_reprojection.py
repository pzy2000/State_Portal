from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_native_receipt_reprojection import (
    REPROJECTION_CONFIRMATION,
    reproject_luanti_native_receipts,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/luanti-native-reprojection",
        scope="preannotation-reprojection",
        granted_at="2026-08-13T14:00:00Z",
        local_user_id=os.getuid(),
    )


def _catch_output(*, passed: bool) -> bytes:
    if passed:
        return b"All tests passed (45 assertions in 1 test case)\n"
    return b"test cases:  1 |  0 passed | 1 failed\n"


def _module_output() -> bytes:
    return b"Unit Test Results: PASSED\n    0 / 3 failed tests.\n"


def _v3_reference(phase: str, variant: str, repetition: int) -> ArtifactReference:
    return ArtifactReference(
        f"native_regression_attempt/luanti/15519/{phase}/{variant}/{repetition}/v3",
        "native_regression_attempt",
        "3.0",
    )


def _write_v3_attempts(root: Path, *, omit_last: bool = False) -> None:
    candidate = CandidateKey("luanti", 15519)
    index = 0
    for phase in ("f2p", "p2p"):
        selector = "test translations" if phase == "f2p" else "TestSettings"
        for variant in ("fixed", "buggy"):
            expected = "failure" if phase == "f2p" and variant == "buggy" else "pass"
            returncode = 1 if expected == "failure" else 0
            stderr = (
                _catch_output(passed=returncode == 0)
                if phase == "f2p"
                else _module_output()
            )
            for repetition in range(1, 4):
                index += 1
                if omit_last and index == 12:
                    continue
                reference = _v3_reference(phase, variant, repetition)
                write_stable_artifact(
                    root,
                    artifact_id=reference.artifact_id,
                    artifact_kind=reference.artifact_kind,
                    semantic_version=reference.semantic_version,
                    created_at=f"2026-08-13T14:00:{index:02d}Z",
                    inputs=(
                        ArtifactReference(
                            "fixed_source_capsule/luanti/15519/fixed/v2",
                            "fixed_source_capsule",
                            "2.0",
                        ),
                        ArtifactReference(
                            f"native_regression_build/luanti/15519/{variant}/build/v3",
                            "native_regression_build",
                            "3.0",
                        ),
                    ),
                    payload={
                        "candidate": candidate.to_value(),
                        "profile_id": "luanti-15519-gettext-plural-trim-v2",
                        "phase": phase,
                        "variant": variant,
                        "repetition": repetition,
                        "selector": selector,
                        "expected_outcome": expected,
                        "observed_outcome": "invalid_test_count",
                        "process_outcome": "completed",
                        "returncode": returncode,
                        "completed_test_count": 0,
                        "stdout_base64": base64.b64encode(b"").decode("ascii"),
                        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
                        "stdout_truncated": False,
                        "stderr_truncated": False,
                        "output_encoding": "base64",
                        "matched_expected": False,
                    },
                    authorization=_authorization(),
                )


def test_reprojects_all_twelve_v3_receipts_without_starting_processes(
    tmp_path: Path,
) -> None:
    _write_v3_attempts(tmp_path)

    result = reproject_luanti_native_receipts(
        tmp_path,
        candidate=CandidateKey("luanti", 15519),
        authorization=_authorization(),
        confirmation=REPROJECTION_CONFIRMATION,
    )

    assert result.created_attempts == 12
    assert result.resumed_attempts == 0
    assert result.qualified is True
    assert result.aggregate.reference == ArtifactReference(
        "native_regression_aggregate/luanti/15519/fixed-buggy/v4",
        "native_regression_aggregate",
        "4.0",
    )
    attempt = load_stable_artifact(
        tmp_path,
        artifact_id="native_regression_attempt/luanti/15519/p2p/fixed/1/v4",
        artifact_kind="native_regression_attempt",
        semantic_version="4.0",
    )
    assert attempt.inputs == (_v3_reference("p2p", "fixed", 1),)
    assert attempt.payload["completed_test_count"] == 1
    assert attempt.payload["observed_outcome"] == "pass"
    assert attempt.payload["matched_expected"] is True
    old = load_stable_artifact(
        tmp_path,
        artifact_id=_v3_reference("p2p", "fixed", 1).artifact_id,
        artifact_kind="native_regression_attempt",
        semantic_version="3.0",
    )
    assert old.payload["completed_test_count"] == 0
    assert old.payload["observed_outcome"] == "invalid_test_count"


def test_reprojection_resumes_valid_v4_attempts(tmp_path: Path) -> None:
    _write_v3_attempts(tmp_path)
    first = reproject_luanti_native_receipts(
        tmp_path,
        candidate=CandidateKey("luanti", 15519),
        authorization=_authorization(),
        confirmation=REPROJECTION_CONFIRMATION,
    )
    resumed = reproject_luanti_native_receipts(
        tmp_path,
        candidate=CandidateKey("luanti", 15519),
        authorization=_authorization(),
        confirmation=REPROJECTION_CONFIRMATION,
    )

    assert first.qualified and resumed.qualified
    assert resumed.created_attempts == 0
    assert resumed.resumed_attempts == 12


def test_reprojection_fails_closed_before_aggregate_when_v3_is_incomplete(
    tmp_path: Path,
) -> None:
    _write_v3_attempts(tmp_path, omit_last=True)

    with pytest.raises(RuntimeError, match="v3 attempt coverage is incomplete"):
        reproject_luanti_native_receipts(
            tmp_path,
            candidate=CandidateKey("luanti", 15519),
            authorization=_authorization(),
            confirmation=REPROJECTION_CONFIRMATION,
        )

    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        load_stable_artifact(
            tmp_path,
            artifact_id="native_regression_aggregate/luanti/15519/fixed-buggy/v4",
            artifact_kind="native_regression_aggregate",
            semantic_version="4.0",
        )
    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        load_stable_artifact(
            tmp_path,
            artifact_id="native_regression_attempt/luanti/15519/f2p/fixed/1/v4",
            artifact_kind="native_regression_attempt",
            semantic_version="4.0",
        )


def test_reprojection_requires_explicit_nonexecution_authorization(
    tmp_path: Path,
) -> None:
    _write_v3_attempts(tmp_path)

    with pytest.raises(RuntimeError, match="explicit confirmation"):
        reproject_luanti_native_receipts(
            tmp_path,
            candidate=CandidateKey("luanti", 15519),
            authorization=_authorization(),
            confirmation="no",
        )
