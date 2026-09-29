"""Typed Gate 5--7 verdict for the fresh #11866 historical-suite adapter."""

from __future__ import annotations

import base64
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 11866}
_PROFILE = "luanti-11866-mapgen-limit-v1-native-gate57"
_VERSION = "1.0"
_V5_AGGREGATE = ArtifactReference(
    "native_regression_aggregate/luanti/11866/fixed-buggy/irrlicht-v5",
    "native_regression_aggregate",
    "3.0",
)
_V5_FIXED_BUILD = ArtifactReference(
    "native_regression_build/luanti/11866/fixed/build/irrlicht-v5",
    "native_regression_build",
    "3.0",
)
_V5_BUGGY_BUILD = ArtifactReference(
    "native_regression_build/luanti/11866/buggy/build/irrlicht-v5",
    "native_regression_build",
    "3.0",
)
_ADAPTER = ArtifactReference(
    "candidate_gate57_11866_selector_adapter/luanti/11866/native/v1",
    "candidate_gate57_11866_selector_adapter",
    _VERSION,
)
_SELECTOR_BLOCKER = ArtifactReference(
    "candidate_gate57_11866_selector_blocker/luanti/11866/native/v1",
    "candidate_gate57_11866_selector_blocker",
    _VERSION,
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_11866_selector_reparse_aggregate/luanti/11866/native/v5",
    "candidate_gate57_11866_selector_reparse_aggregate",
    _VERSION,
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_11866_selector_reparse_transition/luanti/11866/native/v5",
    "candidate_gate57_11866_selector_reparse_transition",
    _VERSION,
)
_RAW_MODULE_SUMMARY = re.compile(
    rb"(?m)^======== Module ([^\r\n ]+) (passed|failed) "
    rb"\((\d+) failures / (\d+) tests\) - \d+ms\r?$"
)
_EXPECTED = {
    ("f2p", "fixed"): "pass",
    ("f2p", "buggy"): "failure",
    ("p2p", "fixed"): "pass",
    ("p2p", "buggy"): "pass",
}


def write_luanti_11866_selector_reparse_verdict(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact]:
    """Validate raw named lines and persist the fresh Gate 5--7 transition."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    generic = _load(root, _V5_AGGREGATE)
    builds = (_load(root, _V5_FIXED_BUILD), _load(root, _V5_BUGGY_BUILD))
    adapter = _load(root, _ADAPTER)
    blocker = _load(root, _SELECTOR_BLOCKER)
    attempts = tuple(_load(root, reference) for reference in _attempt_references())
    _validate_inputs(generic, builds, adapter, blocker, attempts)
    cells = _reparse_cells(attempts)
    aggregate_payload = {
        "candidate": _CANDIDATE,
        "profile_id": _PROFILE,
        "state": "qualified",
        "outcome": "qualified",
        "qualified": True,
        "terminal": False,
        "resumable": False,
        "attempt_count": 12,
        "repetitions_per_cell": 3,
        "stable_repetitions": True,
        "all_expected": True,
        "raw_named_line_reparse": "unique_target_module_summary_per_attempt",
        "cells": cells,
        "source_bytes_modified": False,
        "test_bytes_modified": False,
        "dependency_bytes_modified": False,
        "old_attempts_loaded": False,
        "old_attempts_referenced": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    aggregate_inputs = (
        _V5_AGGREGATE,
        *tuple(build.reference for build in builds),
        _ADAPTER,
        _SELECTOR_BLOCKER,
        *tuple(attempt.reference for attempt in attempts),
    )
    aggregate = _write_or_load(
        root, _AGGREGATE, aggregate_inputs, aggregate_payload, authorization
    )
    transition_payload = {
        "candidate": _CANDIDATE,
        "state": "passed",
        "outcome": "gate57_qualified",
        "aggregate_reference": aggregate.reference.artifact_id,
        "gate_1_7_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "passed",
            "gate_3_strict_inverse_patch": "passed_static_production_only",
            "gate_4_fixed_test_api_compatibility": "passed_static_buggy_parent",
            "gate_5_fixed_f2p_3x": "passed",
            "gate_6_buggy_f2p_3x_failure": "passed_clean_failure",
            "gate_7_fixed_p2p_3x": "passed",
            "gate_7_buggy_p2p_3x": "passed",
        },
        "historical_suite_adapter": {
            "command": "--run-unittests",
            "binding": "one_exact_named_raw_module_summary_per_attempt",
            "f2p_selector": "TestMap",
            "p2p_selector": "TestSettings",
        },
        "old_attempts_loaded": False,
        "old_attempts_referenced": False,
        "candidate_execution_performed": True,
        "deferred_until_after_gate5_7": [
            "gate8_full_versions",
            "gate9_start_replay_oracle_observations",
            "gate10_goldens",
            "gate11_visual_cross_judgment",
            "gate12_same_lineage_receipt",
            "gate13_freeze",
        ],
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    transition = _write_or_load(
        root,
        _TRANSITION,
        (aggregate.reference,),
        transition_payload,
        authorization,
    )
    return aggregate, transition


def _attempt_references() -> tuple[ArtifactReference, ...]:
    return tuple(
        ArtifactReference(
            f"native_regression_attempt/luanti/11866/{phase}/{variant}/{repetition}/irrlicht-v5",
            "native_regression_attempt",
            "3.0",
        )
        for phase in ("f2p", "p2p")
        for variant in ("fixed", "buggy")
        for repetition in (1, 2, 3)
    )


def _reparse_cells(attempts: tuple[StableArtifact, ...]) -> list[dict[str, object]]:
    cells: list[dict[str, object]] = []
    for phase, variant in _EXPECTED:
        group = sorted(
            (item for item in attempts if item.payload["phase"] == phase and item.payload["variant"] == variant),
            key=lambda item: item.payload["repetition"],
        )
        if len(group) != 3:
            raise RuntimeError("#11866 v5 cell coverage is incomplete")
        summaries: list[dict[str, object]] = []
        for attempt in group:
            selector = attempt.payload["selector"]
            if not isinstance(selector, str):
                raise RuntimeError("#11866 v5 selector is invalid")
            if attempt.payload.get("stderr_truncated") or attempt.payload.get("stdout_truncated"):
                raise RuntimeError("#11866 v5 raw historical output is truncated")
            raw = _decode_output(attempt.payload.get("stderr_base64"))
            matches = [
                match
                for match in _RAW_MODULE_SUMMARY.finditer(raw)
                if match.group(1).decode("utf-8", errors="strict") == selector
            ]
            if len(matches) != 1 or int(matches[0].group(4)) <= 0:
                raise RuntimeError(
                    "#11866 v5 raw output cannot uniquely bind the target module"
                )
            status = matches[0].group(2).decode("ascii")
            failures = int(matches[0].group(3))
            observed = "failure" if status == "failed" else "pass"
            if observed != attempt.payload["observed_outcome"]:
                raise RuntimeError("#11866 v5 reparse differs from receipt outcome")
            summaries.append(
                {
                    "repetition": attempt.payload["repetition"],
                    "selector": selector,
                    "unique_named_raw_line_count": 1,
                    "raw_named_summary": matches[0].group(0).decode("utf-8"),
                    "observed_outcome": observed,
                    "failure_count": failures,
                    "test_count": int(matches[0].group(4)),
                }
            )
        if any(summary["observed_outcome"] != _EXPECTED[(phase, variant)] for summary in summaries):
            raise RuntimeError("#11866 v5 target module behavior is semantically unexpected")
        cells.append(
            {
                "phase": phase,
                "variant": variant,
                "expected_outcome": _EXPECTED[(phase, variant)],
                "observed_outcomes": [summary["observed_outcome"] for summary in summaries],
                "raw_named_summaries": summaries,
            }
        )
    return cells


def _validate_inputs(
    generic: StableArtifact,
    builds: tuple[StableArtifact, StableArtifact],
    adapter: StableArtifact,
    blocker: StableArtifact,
    attempts: tuple[StableArtifact, ...],
) -> None:
    if generic.payload.get("qualified") is not True or generic.payload.get("attempt_count") != 12:
        raise RuntimeError("#11866 v5 generic aggregate is not qualified")
    if any(build.payload.get("returncode") != 0 for build in builds):
        raise RuntimeError("#11866 v5 build pair did not pass")
    if adapter.payload.get("runner_command") != ["--run-unittests"]:
        raise RuntimeError("#11866 historical-suite adapter drifted")
    if blocker.payload.get("terminal") is not False:
        raise RuntimeError("#11866 selector blocker is not resumable")
    if len(attempts) != 12 or any(item.payload.get("candidate") != _CANDIDATE for item in attempts):
        raise RuntimeError("#11866 v5 attempt candidate binding is invalid")
    if any(item.payload.get("matched_expected") is not True for item in attempts):
        raise RuntimeError("#11866 v5 attempt expectation mismatch")


def _write_or_load(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    try:
        existing = _load(root, reference)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#11866 selector reparse artifact conflicts with exact state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, reference)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#11866 selector reparse typed reload failed")
    return artifact


def _decode_output(value: object) -> bytes:
    if not isinstance(value, str):
        raise RuntimeError("#11866 v5 raw output encoding is invalid")
    return base64.b64decode(value, validate=True)


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
        raise PermissionError("#11866 selector reparse authorization is inactive")


__all__ = ["write_luanti_11866_selector_reparse_verdict"]
