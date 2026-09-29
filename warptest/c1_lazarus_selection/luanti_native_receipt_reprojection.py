"""Offline v3-to-v4 reprojection for Luanti native test receipts."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .luanti_native_executor import completed_native_test_count
from .luanti_native_regression import (
    NativeRegressionProfile,
    NativeRegressionRun,
    profile_for_candidate,
)
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


REPROJECTION_CONFIRMATION = "REPROJECT_LUANTI_NATIVE_RECEIPTS"
_SOURCE_VERSION = "3.0"
_TARGET_VERSION = "4.0"
_OUTPUT_ENCODING = "base64"
_MAX_STREAM_BYTES = 262_144
_ATTEMPT_FIELDS = frozenset(
    {
        "candidate",
        "profile_id",
        "phase",
        "variant",
        "repetition",
        "selector",
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
)


@dataclass(frozen=True)
class NativeReceiptReprojectionResult:
    candidate: CandidateKey
    aggregate: StableArtifact
    created_attempts: int
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


@dataclass(frozen=True)
class _SourceReceipt:
    artifact: StableArtifact
    stdout: bytes
    stderr: bytes


def reproject_luanti_native_receipts(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> NativeReceiptReprojectionResult:
    """Mechanically derive v4 results from all twelve persisted v3 receipts.

    This function has no process runner and performs no build, test, or network
    operation.  All twelve source attempts are typed-loaded and validated
    before any v4 artifact is written.
    """

    if confirmation != REPROJECTION_CONFIRMATION:
        raise RuntimeError(
            f"explicit confirmation must be {REPROJECTION_CONFIRMATION!r}"
        )
    if type(candidate) is not CandidateKey or candidate.repository_key != "luanti":
        raise ValueError("native receipt reprojection candidate is invalid")
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("native receipt reprojection authorization is invalid")
    if authorization.scope != "preannotation-reprojection":
        raise PermissionError(
            "native receipt reprojection authorization scope is invalid"
        )
    if authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("native receipt reprojection authorization is inactive")

    root = Path(artifact_root).absolute()
    profile = profile_for_candidate(candidate)
    specs = _attempt_specs(profile)
    sources: list[tuple[_AttemptSpec, _SourceReceipt]] = []
    for spec in specs:
        try:
            source = _load_v3_source(root, profile, spec)
        except RuntimeError as error:
            if str(error) in {
                "artifact directory is missing",
                "artifact current head is missing",
            }:
                raise RuntimeError(
                    "native v3 attempt coverage is incomplete"
                ) from error
            raise
        sources.append((spec, source))
    if len(sources) != 12:
        raise RuntimeError("native v3 attempt coverage is incomplete")

    attempts: list[StableArtifact] = []
    created = 0
    resumed = 0
    for spec, source in sources:
        payload = _v4_payload(profile, spec, source)
        reference = _attempt_reference(profile, spec, version=4)
        artifact = _load_optional(root, reference)
        if artifact is None:
            artifact = _write_and_reload(
                root,
                reference,
                (source.artifact.reference,),
                payload,
                authorization,
            )
            created += 1
        else:
            resumed += 1
        _validate_v4_attempt(profile, spec, source, artifact)
        attempts.append(artifact)

    aggregate = _load_or_write_aggregate(root, profile, attempts, authorization)
    return NativeReceiptReprojectionResult(
        candidate=candidate,
        aggregate=aggregate,
        created_attempts=created,
        resumed_attempts=resumed,
        qualified=aggregate.payload["qualified"] is True,
    )


def _attempt_specs(profile: NativeRegressionProfile) -> tuple[_AttemptSpec, ...]:
    executable = "bin/luantiserver"

    def run(
        phase: Literal["f2p", "p2p"],
        selector: str,
        buggy: Literal["failure", "pass"],
    ) -> NativeRegressionRun:
        return NativeRegressionRun(
            phase=phase,
            selector=selector,
            executable_path=executable,
            argv=(
                executable,
                "--run-unittests",
                "--test-module",
                selector,
            ),
            repetitions=3,
            fixed_expected_outcome="pass",
            buggy_expected_outcome=buggy,
        )

    runs = (
        run("f2p", profile.f2p_selector, "failure"),
        run("p2p", profile.p2p_selector, "pass"),
    )
    return tuple(
        _AttemptSpec(run_value, variant, repetition)
        for run_value in runs
        for variant in ("fixed", "buggy")
        for repetition in range(1, profile.repetitions + 1)
    )


def _attempt_reference(
    profile: NativeRegressionProfile, spec: _AttemptSpec, *, version: int
) -> ArtifactReference:
    return ArtifactReference(
        "native_regression_attempt/luanti/"
        f"{profile.candidate.change_number}/{spec.run.phase}/"
        f"{spec.variant}/{spec.repetition}/v{version}",
        "native_regression_attempt",
        f"{version}.0",
    )


def _attempt_identity(
    profile: NativeRegressionProfile, spec: _AttemptSpec
) -> dict[str, object]:
    return {
        "candidate": profile.candidate.to_value(),
        "profile_id": profile.profile_id,
        "phase": spec.run.phase,
        "variant": spec.variant,
        "repetition": spec.repetition,
        "selector": spec.run.selector,
        "expected_outcome": spec.expected_outcome,
    }


def _load_v3_source(
    root: Path, profile: NativeRegressionProfile, spec: _AttemptSpec
) -> _SourceReceipt:
    reference = _attempt_reference(profile, spec, version=3)
    artifact = load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=_SOURCE_VERSION,
    )
    expected_inputs = (
        profile.fixed_capsule_reference,
        ArtifactReference(
            "native_regression_build/luanti/"
            f"{profile.candidate.change_number}/{spec.variant}/build/v3",
            "native_regression_build",
            _SOURCE_VERSION,
        ),
    )
    payload = artifact.payload
    if artifact.inputs != expected_inputs:
        raise RuntimeError("native v3 attempt dependency is invalid")
    if set(payload) != _ATTEMPT_FIELDS or any(
        payload[key] != value for key, value in _attempt_identity(profile, spec).items()
    ):
        raise RuntimeError("native v3 attempt identity is invalid")
    stdout, stderr = _decode_streams(payload, label="native v3 attempt")
    _validate_process_fields(payload, label="native v3 attempt")
    return _SourceReceipt(artifact, stdout, stderr)


def _v4_payload(
    profile: NativeRegressionProfile, spec: _AttemptSpec, source: _SourceReceipt
) -> dict[str, object]:
    source_payload = source.artifact.payload
    count = completed_native_test_count(
        spec.run,
        source_payload["process_outcome"],
        source.stdout,
        source.stderr,
    )
    observed = _observed_outcome(
        source_payload["process_outcome"], source_payload["returncode"], count
    )
    return {
        **_attempt_identity(profile, spec),
        "observed_outcome": observed,
        "process_outcome": source_payload["process_outcome"],
        "returncode": source_payload["returncode"],
        "completed_test_count": count,
        "stdout_base64": source_payload["stdout_base64"],
        "stderr_base64": source_payload["stderr_base64"],
        "stdout_truncated": source_payload["stdout_truncated"],
        "stderr_truncated": source_payload["stderr_truncated"],
        "output_encoding": _OUTPUT_ENCODING,
        "matched_expected": observed == spec.expected_outcome,
    }


def _validate_v4_attempt(
    profile: NativeRegressionProfile,
    spec: _AttemptSpec,
    source: _SourceReceipt,
    artifact: StableArtifact,
) -> None:
    expected = _v4_payload(profile, spec, source)
    if artifact.inputs != (source.artifact.reference,):
        raise RuntimeError("native v4 attempt dependency is invalid")
    if artifact.payload != expected:
        raise RuntimeError("native v4 attempt reprojection is invalid")


def _decode_streams(payload: object, *, label: str) -> tuple[bytes, bytes]:
    if type(payload) is not dict:
        raise RuntimeError(f"{label} output is invalid")
    values: list[bytes] = []
    for name in ("stdout", "stderr"):
        encoded = payload.get(f"{name}_base64")
        try:
            value = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"{label} output is invalid") from error
        truncated = payload.get(f"{name}_truncated")
        if (
            len(value) > _MAX_STREAM_BYTES
            or type(truncated) is not bool
            or truncated is True
            and len(value) != _MAX_STREAM_BYTES
        ):
            raise RuntimeError(f"{label} output is invalid")
        values.append(value)
    return values[0], values[1]


def _validate_process_fields(payload: object, *, label: str) -> None:
    if type(payload) is not dict:
        raise RuntimeError(f"{label} process result is invalid")
    outcome = payload.get("process_outcome")
    returncode = payload.get("returncode")
    count = payload.get("completed_test_count")
    if (
        outcome not in {"completed", "timed_out", "launch_failed"}
        or outcome == "completed"
        and type(returncode) is not int
        or outcome != "completed"
        and returncode is not None
        or count is not None
        and (type(count) is not int or count < 0)
        or payload.get("observed_outcome")
        not in {"pass", "failure", "timed_out", "launch_failed", "invalid_test_count"}
        or type(payload.get("matched_expected")) is not bool
        or payload.get("output_encoding") != _OUTPUT_ENCODING
    ):
        raise RuntimeError(f"{label} process result is invalid")


def _observed_outcome(
    process_outcome: object, returncode: object, completed_test_count: object
) -> str:
    if process_outcome == "timed_out":
        return "timed_out"
    if process_outcome == "launch_failed":
        return "launch_failed"
    if process_outcome != "completed" or type(returncode) is not int:
        raise RuntimeError("native v3 process result is invalid")
    if completed_test_count != 1:
        return "invalid_test_count"
    return "pass" if returncode == 0 else "failure"


def _load_or_write_aggregate(
    root: Path,
    profile: NativeRegressionProfile,
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 12 or len({item.reference for item in attempts}) != 12:
        raise RuntimeError("native v4 aggregate attempt coverage is invalid")
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
        raise RuntimeError("native v4 aggregate repetition coverage is invalid")
    stable = all(
        len({attempt.payload["observed_outcome"] for attempt in group}) == 1
        for group in groups.values()
    )
    all_expected = all(
        attempt.payload["matched_expected"] is True for attempt in attempts
    )
    payload = {
        "candidate": profile.candidate.to_value(),
        "profile_id": profile.profile_id,
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
        "native_regression_aggregate/luanti/"
        f"{profile.candidate.change_number}/fixed-buggy/v4",
        "native_regression_aggregate",
        _TARGET_VERSION,
    )
    artifact = _load_optional(root, reference)
    inputs = tuple(attempt.reference for attempt in attempts)
    if artifact is None:
        artifact = _write_and_reload(root, reference, inputs, payload, authorization)
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("native v4 aggregate reprojection is invalid")
    return artifact


def _load_optional(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return load_stable_artifact(
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
        created_at=datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
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
        raise RuntimeError("native v4 typed reload is invalid")
    return artifact
