"""Terminal Gate 5--7 semantic-mismatch receipt for Luanti #16200.

The source-preserving v3 toolchain repair removes the earlier native-link
blocker.  The resulting fresh twelve-cell epoch is mechanically complete, but
the registered F2P selector ``matrix4`` passes on both fixed and buggy trees.
The three stable buggy F2P passes fail the required discriminator, so this
receipt closes the candidate under the strict Gate 5--7 truth table without
attempting another selector, downstream stage, paid annotation, or formal
trial.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 16200}
_PROFILE_ID = "luanti-16200-rotation-conventions-v1-native-gate57"
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/16200/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/16200/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/16200/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/16200/test_manifest/v2", "candidate_stage", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/16200/current/v2",
    "screening_checkpoint",
    "2.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/16200/fixed/v2", "fixed_source_capsule", "2.0"
)
_DIAGNOSTIC = ArtifactReference(
    "candidate_gate57_link_diagnostic/luanti/16200/native/v1",
    "candidate_gate57_link_diagnostic",
    "1.0",
)
_TOOLCHAIN = ArtifactReference(
    "candidate_gate57_fresh_toolchain/luanti/16200/cpp17/v3",
    "candidate_gate57_fresh_toolchain",
    "1.0",
)
_BUILD_REFS = (
    ArtifactReference(
        "native_regression_build/luanti/16200/fixed/configure/v3",
        "native_regression_build",
        "3.0",
    ),
    ArtifactReference(
        "native_regression_build/luanti/16200/fixed/build/v3",
        "native_regression_build",
        "3.0",
    ),
    ArtifactReference(
        "native_regression_build/luanti/16200/buggy/configure/v3",
        "native_regression_build",
        "3.0",
    ),
    ArtifactReference(
        "native_regression_build/luanti/16200/buggy/build/v3",
        "native_regression_build",
        "3.0",
    ),
)
_AGGREGATE = ArtifactReference(
    "native_regression_aggregate/luanti/16200/fixed-buggy/v3",
    "native_regression_aggregate",
    "3.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_terminal_transition/luanti/16200/native/v3",
    "candidate_gate57_terminal_transition",
    "1.0",
)
_TERMINAL = ArtifactReference(
    "candidate_gate57_terminal/luanti/16200/native/v3",
    "candidate_gate57_terminal",
    "1.0",
)


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, Any],
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
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError(f"#16200 semantic receipt typed reload mismatch: {reference.artifact_id}")
    return artifact


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#16200 semantic receipt authorization is inactive")


def _attempts(root: Path, aggregate: StableArtifact) -> tuple[StableArtifact, ...]:
    references = tuple(
        ArtifactReference(
            value.artifact_id, value.artifact_kind, value.semantic_version
        )
        for value in aggregate.inputs
        if value.artifact_kind == "native_regression_attempt"
    )
    attempts = tuple(_load(root, reference) for reference in references)
    if len(attempts) != 12:
        raise RuntimeError("#16200 v3 aggregate does not bind exactly 12 attempts")
    return tuple(
        sorted(
            attempts,
            key=lambda item: (
                str(item.payload.get("phase")),
                str(item.payload.get("variant")),
                int(item.payload.get("repetition", 0)),
            ),
        )
    )


def write_luanti_16200_gate57_semantic_mismatch(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact]:
    """Persist terminal transition and receipt for the complete v3 epoch."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, reference)
        for reference in (
            _ENUMERATION,
            _SOURCE,
            _PATCH,
            _ATOMICITY,
            _MANIFEST,
            _CHECKPOINT,
            _CAPSULE,
            _DIAGNOSTIC,
            _TOOLCHAIN,
            *_BUILD_REFS,
            _AGGREGATE,
        )
    )
    (
        enumeration,
        source,
        patch,
        atomicity,
        manifest,
        checkpoint,
        capsule,
        diagnostic,
        toolchain,
        fixed_configure,
        fixed_build,
        buggy_configure,
        buggy_build,
        aggregate,
    ) = chain
    if _CANDIDATE not in enumeration.payload.get("candidates", []):
        raise RuntimeError("#16200 is absent from current-v2 enumeration")
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain[1:]):
        raise RuntimeError("#16200 semantic mismatch chain is not candidate-bound")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#16200 source packet is not complete")
    if patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#16200 patch packet is not eligible")
    if (
        atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != 15195
    ):
        raise RuntimeError("#16200 source atomicity is not accepted")
    if (
        manifest.payload.get("state") != "passed"
        or manifest.payload.get("details", {}).get("test_count") != 3
    ):
        raise RuntimeError("#16200 test manifest is not closed")
    if checkpoint.payload.get("state") != "complete":
        raise RuntimeError("#16200 screening checkpoint is not complete")
    if diagnostic.payload.get("outcome") != "source_preserving_lua_archive_repair_proved":
        raise RuntimeError("#16200 v3 source-preserving diagnostic is missing")
    if (
        toolchain.payload.get("fresh_epoch") != "cpp17-v3"
        or toolchain.payload.get("old_attempts_loaded") is not False
    ):
        raise RuntimeError("#16200 v3 toolchain receipt is unsafe")
    for build in (fixed_configure, fixed_build, buggy_configure, buggy_build):
        if (
            build.payload.get("process_outcome") != "completed"
            or build.payload.get("returncode") != 0
        ):
            raise RuntimeError("#16200 v3 build closure is incomplete")
    if (
        aggregate.payload.get("candidate") != _CANDIDATE
        or aggregate.payload.get("profile_id") != _PROFILE_ID
        or aggregate.payload.get("attempt_count") != 12
        or aggregate.payload.get("stable_repetitions") is not True
        or aggregate.payload.get("qualified") is not False
        or aggregate.payload.get("all_expected") is not False
    ):
        raise RuntimeError("#16200 v3 aggregate is not the expected mismatch")
    attempts = _attempts(root, aggregate)
    expected = {
        ("f2p", "fixed"): "pass",
        ("f2p", "buggy"): "failure",
        ("p2p", "fixed"): "pass",
        ("p2p", "buggy"): "pass",
    }
    if any(
        item.payload.get("process_outcome") != "completed"
        or item.payload.get("returncode") != 0
        or item.payload.get("selector")
        != ("matrix4" if item.payload.get("phase") == "f2p" else "k-d-tree")
        for item in attempts
    ):
        raise RuntimeError("#16200 v3 attempts are not clean selector receipts")
    cells: dict[str, dict[str, Any]] = {}
    mismatch_attempts: list[dict[str, Any]] = []
    for item in attempts:
        key = (str(item.payload["phase"]), str(item.payload["variant"]))
        group = cells.setdefault(f"{key[0]}:{key[1]}", {"expected": expected[key], "observed": []})
        group["observed"].append(str(item.payload["observed_outcome"]))
        if item.payload.get("matched_expected") is not True:
            mismatch_attempts.append(
                {
                    "phase": item.payload["phase"],
                    "variant": item.payload["variant"],
                    "repetition": item.payload["repetition"],
                    "selector": item.payload["selector"],
                    "expected": item.payload["expected_outcome"],
                    "observed": item.payload["observed_outcome"],
                    "attempt_reference": item.reference.artifact_id,
                }
            )
    if len(mismatch_attempts) != 3 or any(
        item["phase"] != "f2p" or item["variant"] != "buggy" or item["observed"] != "pass"
        for item in mismatch_attempts
    ):
        raise RuntimeError("#16200 v3 mismatch is not the stable buggy F2P cell")
    revision_map = {artifact.reference.artifact_id: artifact.created_at for artifact in chain}
    transition_inputs = (
        _ENUMERATION,
        _SOURCE,
        _PATCH,
        _ATOMICITY,
        _MANIFEST,
        _CHECKPOINT,
        _CAPSULE,
        _DIAGNOSTIC,
        _TOOLCHAIN,
        *_BUILD_REFS,
        _AGGREGATE,
        *(item.reference for item in attempts),
    )
    transition_payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": _PROFILE_ID,
        "fresh_epoch": "v3",
        "state": "blocked",
        "outcome": "candidate_semantic_mismatch",
        "terminal": True,
        "resumable": False,
        "aggregate_reference": _AGGREGATE.artifact_id,
        "attempt_count": 12,
        "stable_repetitions": True,
        "old_attempts_loaded": False,
        "old_attempts_referenced": False,
        "candidate_execution_performed": True,
        "gate_1_7_status": {
            "gate_1_source_authority": "passed",
            "gate_2_single_defect_mapping": "passed",
            "gate_3_strict_inverse_patch": "passed",
            "gate_4_fixed_test_api_compatibility": "passed",
            "gate_5_fixed_f2p_3x": "passed",
            "gate_6_buggy_f2p_3x_failure": "failed_semantic_discriminator",
            "gate_7_fixed_p2p_3x": "passed",
            "gate_7_buggy_p2p_3x": "passed",
        },
        "truth_table": {
            "fixed": {"f2p": "pass", "p2p": "pass"},
            "buggy": {"f2p": "failure", "p2p": "pass"},
        },
        "cells": cells,
        "mismatch_attempts": mismatch_attempts,
        "reason": "registered_matrix4_f2p_selector_passes_on_buggy_tree_all_3_repetitions",
        "current_v2_chain_revisions": revision_map,
        "deferred_without_execution": [
            "gate8_full_versions",
            "gate9_start_replay_oracle_observations",
            "gate10_goldens",
            "gate11_visual_cross_judgment",
            "gate12_same_lineage_receipt",
            "gate13_freeze",
            "paid_annotation",
            "formal_trials",
        ],
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    transition = _write(
        root, _TRANSITION, transition_inputs, transition_payload, authorization
    )
    terminal_inputs = transition_inputs + (transition.reference,)
    terminal_payload = {
        "candidate": _CANDIDATE,
        "profile_id": _PROFILE_ID,
        "execution_lineage": "candidate-gate57/luanti/16200/native/v3",
        "state": "blocked",
        "outcome": "candidate_semantic_mismatch",
        "terminal": True,
        "resumable": False,
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "aggregate_reference": _AGGREGATE.artifact_id,
        "transition_reference": _TRANSITION.artifact_id,
        "mismatch": {
            "phase": "f2p",
            "selector": "matrix4",
            "expected_buggy_outcome": "failure",
            "observed_buggy_outcomes": ["pass", "pass", "pass"],
            "fixed_outcomes": ["pass", "pass", "pass"],
            "mismatch_attempt_count": 3,
            "selector_is_not_a_valid_bug_discriminator": True,
        },
        "current_gate_status": transition_payload["gate_1_7_status"],
        "current_v2_chain": {
            "source": _SOURCE.artifact_id,
            "patch": _PATCH.artifact_id,
            "atomicity": _ATOMICITY.artifact_id,
            "manifest": _MANIFEST.artifact_id,
            "checkpoint": _CHECKPOINT.artifact_id,
            "capsule": _CAPSULE.artifact_id,
            "diagnostic": _DIAGNOSTIC.artifact_id,
            "toolchain": _TOOLCHAIN.artifact_id,
        },
        "reason": "strict_gate57_requires_buggy_f2p_failure_but_matrix4_passes_on_buggy_tree",
        "next_action": "do_not_retry_this_selector_or_admit_candidate; require_a_new_current_v2_test_contract",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    terminal = _write(root, _TERMINAL, terminal_inputs, terminal_payload, authorization)
    return transition, terminal


__all__ = ["write_luanti_16200_gate57_semantic_mismatch"]
