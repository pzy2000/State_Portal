"""Fresh recovery pair for the Luanti #15519 F12 receipt-ID failure.

The original bounded visual run reached and completed the fixed client, but
the timestamp used in its attempt artifact ID was not valid stable-protocol
syntax.  The original pair is retained as a non-terminal bookkeeping blocker.
This module creates a separate v2 lineage and executes a completely fresh
fixed-then-buggy pair; it never imports frames or observations from v1.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
from typing import Any

from . import luanti_15519_f12_png_runner as base
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 15519}
_PRIOR_PAIR = ArtifactReference(
    "candidate_f12_png_pair/luanti/15519/current-v2/v1",
    "candidate_f12_png_pair",
    "1.0",
)
_REGISTRATION = ArtifactReference(
    "candidate_f12_png_runner_registration/luanti/15519/recovery-v2/v2",
    "candidate_f12_png_runner_registration",
    "2.0",
)
_PAIR = ArtifactReference(
    "candidate_f12_png_pair/luanti/15519/recovery-v2/v2",
    "candidate_f12_png_pair",
    "2.0",
)
_V2_PAIR = _PAIR
_V3_REGISTRATION = ArtifactReference(
    "candidate_f12_png_runner_registration/luanti/15519/deterministic-v3/v3",
    "candidate_f12_png_runner_registration",
    "3.0",
)
_V3_PAIR = ArtifactReference(
    "candidate_f12_png_pair/luanti/15519/deterministic-v3/v3",
    "candidate_f12_png_pair",
    "3.0",
)
_V4_REGISTRATION = ArtifactReference(
    "candidate_f12_png_runner_registration/luanti/15519/deterministic-v4/v4",
    "candidate_f12_png_runner_registration",
    "4.0",
)
_V4_PAIR = ArtifactReference(
    "candidate_f12_png_pair/luanti/15519/deterministic-v4/v4",
    "candidate_f12_png_pair",
    "4.0",
)


def run_luanti_15519_f12_recovery_pair(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Execute one fresh pair after validating the v1 bookkeeping failure."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    if _try_load(root, _PAIR) is not None:
        raise RuntimeError("15519 F12 recovery pair already has a one-shot receipt")

    plan = _load(root, base._PLAN)
    materialization = _load(root, base._MATERIALIZATION)
    mapping = _load(root, base._MAPPING)
    prior = _load(root, _PRIOR_PAIR)
    _validate_prior_bookkeeping_failure(prior)
    preflight = base._native_input_preflight()
    registration = _write_once(
        root,
        _REGISTRATION,
        (plan.reference, materialization.reference, mapping.reference, prior.reference),
        {
            "candidate": dict(_CANDIDATE),
            "state": "registered" if all(preflight.values()) else "blocked",
            "runner": "candidate_specific_builtin_F12_CGEventPostToPid_recovery_v2",
            "preflight": preflight,
            "recovery_reason": "v1_attempt_artifact_id_invalid_after_fixed_before_buggy_start",
            "prior_frames_inherited": False,
            "prior_attempts_loaded": False,
            "fresh_fixed_then_buggy_only": True,
            "network_allowed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "complete": True,
        },
        authorization,
    )
    if not all(preflight.values()):
        return _write_pair(root, registration, (), "blocked", "native_input_preflight_failed", None, authorization)

    run_id = _artifact_safe_timestamp()
    run_root = root / "private/runs/luanti/15519/f12-png/recovery-v2" / run_id
    run_root.mkdir(parents=True, mode=0o700)
    attempts: list[StableArtifact] = []
    for variant in ("fixed", "buggy"):
        try:
            result = base._execute_variant(root, run_root, materialization, mapping, variant)
        except Exception as error:
            result = {
                "state": "blocked",
                "reason": f"{type(error).__name__}: {error}",
                "variant": variant,
                "candidate_process_started": False,
                "protected_pids_touched": False,
                "frames": {},
            }
        attempt_ref = ArtifactReference(
            f"candidate_f12_png_attempt/luanti/15519/recovery-v2/{run_id}/{variant}",
            "candidate_f12_png_attempt",
            "2.0",
        )
        attempt = _write_once(
            root,
            attempt_ref,
            (registration.reference,),
            {
                "candidate": dict(_CANDIDATE),
                "run_id": run_id,
                "recovery_v2": True,
                "prior_frames_inherited": False,
                **result,
                "complete": True,
            },
            authorization,
        )
        attempts.append(attempt)
        if result.get("state") != "passed":
            return _write_pair(
                root,
                registration,
                tuple(attempts),
                "blocked",
                f"{variant}_visual_capture_failed",
                None,
                authorization,
            )

    matrix = base._visual_matrix(run_root)
    return _write_pair(
        root,
        registration,
        tuple(attempts),
        "passed",
        None,
        matrix,
        authorization,
    )


def run_luanti_15519_f12_deterministic_pair(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Run a fresh v3 pair with the deterministic full-screen fixture."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    if _try_load(root, _V3_PAIR) is not None:
        raise RuntimeError("15519 deterministic F12 pair already has a one-shot receipt")
    plan = _load(root, base._PLAN)
    materialization = _load(root, base._MATERIALIZATION)
    mapping = _load(root, base._MAPPING)
    prior = _load(root, _V2_PAIR)
    _validate_v2_visual_nondeterminism(prior)
    preflight = base._native_input_preflight()
    registration = _write_once(
        root,
        _V3_REGISTRATION,
        (plan.reference, materialization.reference, mapping.reference, prior.reference),
        {
            "candidate": dict(_CANDIDATE),
            "state": "registered" if all(preflight.values()) else "blocked",
            "runner": "candidate_specific_builtin_F12_CGEventPostToPid_deterministic_v3",
            "preflight": preflight,
            "recovery_reason": "v2_common_before_world_background_nondeterminism",
            "fixture_determinism": {
                "opaque_fullscreen_formspec": True,
                "fixed_sky_cloud_time_pose": True,
                "chat_and_hud_hidden": True,
                "mapgen_alias_noise_removed": True,
            },
            "prior_frames_inherited": False,
            "prior_attempts_loaded": False,
            "fresh_fixed_then_buggy_only": True,
            "network_allowed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "complete": True,
        },
        authorization,
    )
    if not all(preflight.values()):
        return _write_v3_pair(
            root, registration, (), "blocked", "native_input_preflight_failed", None, authorization
        )

    run_id = _artifact_safe_timestamp()
    run_root = root / "private/runs/luanti/15519/f12-png/deterministic-v3" / run_id
    run_root.mkdir(parents=True, mode=0o700)
    attempts: list[StableArtifact] = []
    for variant in ("fixed", "buggy"):
        try:
            result = base._execute_variant(root, run_root, materialization, mapping, variant)
        except Exception as error:
            result = {
                "state": "blocked",
                "reason": f"{type(error).__name__}: {error}",
                "variant": variant,
                "candidate_process_started": False,
                "protected_pids_touched": False,
                "frames": {},
            }
        attempt = _write_once(
            root,
            ArtifactReference(
                f"candidate_f12_png_attempt/luanti/15519/deterministic-v3/{run_id}/{variant}",
                "candidate_f12_png_attempt",
                "3.0",
            ),
            (registration.reference,),
            {
                "candidate": dict(_CANDIDATE),
                "run_id": run_id,
                "deterministic_v3": True,
                "prior_frames_inherited": False,
                **result,
                "complete": True,
            },
            authorization,
        )
        attempts.append(attempt)
        if result.get("state") != "passed":
            return _write_v3_pair(
                root,
                registration,
                tuple(attempts),
                "blocked",
                f"{variant}_visual_capture_failed",
                None,
                authorization,
            )
    matrix = base._visual_matrix(run_root)
    return _write_v3_pair(
        root, registration, tuple(attempts), "passed", None, matrix, authorization
    )


def run_luanti_15519_f12_deterministic_v4_pair(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Run the fixed mod-prefix variant after the v3 pre-Start failure."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    if _try_load(root, _V4_PAIR) is not None:
        raise RuntimeError("15519 deterministic v4 pair already has a one-shot receipt")
    plan = _load(root, base._PLAN)
    materialization = _load(root, base._MATERIALIZATION)
    mapping = _load(root, base._MAPPING)
    prior = _load(root, _V3_PAIR)
    _validate_v3_fixture_prefix_failure(prior)
    preflight = base._native_input_preflight()
    registration = _write_once(
        root,
        _V4_REGISTRATION,
        (plan.reference, materialization.reference, mapping.reference, prior.reference),
        {
            "candidate": dict(_CANDIDATE),
            "state": "registered" if all(preflight.values()) else "blocked",
            "runner": "candidate_specific_builtin_F12_CGEventPostToPid_deterministic_v4",
            "preflight": preflight,
            "recovery_reason": "v3_fixture_node_prefix_rejected_before_registered_start",
            "fixture_node_prefix": "c1_plural",
            "prior_frames_inherited": False,
            "prior_attempts_loaded": False,
            "fresh_fixed_then_buggy_only": True,
            "network_allowed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "complete": True,
        },
        authorization,
    )
    if not all(preflight.values()):
        return _write_v4_pair(
            root, registration, (), "blocked", "native_input_preflight_failed", None, authorization
        )

    run_id = _artifact_safe_timestamp()
    run_root = root / "private/runs/luanti/15519/f12-png/deterministic-v4" / run_id
    run_root.mkdir(parents=True, mode=0o700)
    attempts: list[StableArtifact] = []
    for variant in ("fixed", "buggy"):
        try:
            result = base._execute_variant(root, run_root, materialization, mapping, variant)
        except Exception as error:
            result = {
                "state": "blocked",
                "reason": f"{type(error).__name__}: {error}",
                "variant": variant,
                "candidate_process_started": False,
                "protected_pids_touched": False,
                "frames": {},
            }
        attempt = _write_once(
            root,
            ArtifactReference(
                f"candidate_f12_png_attempt/luanti/15519/deterministic-v4/{run_id}/{variant}",
                "candidate_f12_png_attempt",
                "4.0",
            ),
            (registration.reference,),
            {
                "candidate": dict(_CANDIDATE),
                "run_id": run_id,
                "deterministic_v4": True,
                "prior_frames_inherited": False,
                **result,
                "complete": True,
            },
            authorization,
        )
        attempts.append(attempt)
        if result.get("state") != "passed":
            return _write_v4_pair(
                root,
                registration,
                tuple(attempts),
                "blocked",
                f"{variant}_visual_capture_failed",
                None,
                authorization,
            )
    matrix = base._visual_matrix(run_root)
    return _write_v4_pair(
        root, registration, tuple(attempts), "passed", None, matrix, authorization
    )


def _validate_prior_bookkeeping_failure(prior: StableArtifact) -> None:
    if prior.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("15519 prior F12 pair is not candidate-bound")
    if not (
        prior.payload.get("state") == "blocked"
        and prior.payload.get("attempt_count") == 1
        and prior.payload.get("ordered_variants") == ["fixed"]
        and prior.payload.get("reason")
        == "runner_receipt_artifact_id_invalid_after_fixed_before_buggy_start"
        and prior.payload.get("gate_11_13_passed") is False
    ):
        raise RuntimeError("15519 prior F12 bookkeeping blocker drifted")


def _validate_v2_visual_nondeterminism(prior: StableArtifact) -> None:
    matrix = prior.payload.get("visual_matrix")
    if not (
        prior.payload.get("candidate") == _CANDIDATE
        and prior.payload.get("state") == "blocked"
        and prior.payload.get("attempt_count") == 2
        and prior.payload.get("ordered_variants") == ["fixed", "buggy"]
        and prior.payload.get("reason") == "visual_matrix_failed"
        and prior.payload.get("prior_frames_inherited") is False
        and isinstance(matrix, dict)
        and matrix.get("before_visual_equivalent") is False
        and matrix.get("two_by_two_complete") is True
    ):
        raise RuntimeError("15519 v2 visual nondeterminism receipt drifted")


def _validate_v3_fixture_prefix_failure(prior: StableArtifact) -> None:
    if not (
        prior.payload.get("candidate") == _CANDIDATE
        and prior.payload.get("state") == "blocked"
        and prior.payload.get("attempt_count") == 1
        and prior.payload.get("ordered_variants") == ["fixed"]
        and prior.payload.get("reason") == "fixed_visual_capture_failed"
        and prior.payload.get("prior_frames_inherited") is False
        and prior.payload.get("visual_matrix") is None
    ):
        raise RuntimeError("15519 v3 fixture-prefix blocker drifted")


def _write_pair(
    root: Path,
    registration: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    state: str,
    reason: str | None,
    matrix: dict[str, Any] | None,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    passed = bool(
        state == "passed"
        and matrix is not None
        and matrix.get("before_visual_equivalent") is True
        and matrix.get("two_by_two_complete") is True
    )
    return _write_once(
        root,
        _PAIR,
        (registration.reference, *(attempt.reference for attempt in attempts)),
        {
            "candidate": dict(_CANDIDATE),
            "state": "passed" if passed else "blocked",
            "outcome": (
                "gate_11_13_visual_pair_passed"
                if passed
                else "candidate_specific_visual_pair_blocked"
            ),
            "reason": None if passed else (reason or "visual_matrix_failed"),
            "attempt_count": len(attempts),
            "ordered_variants": [attempt.payload["variant"] for attempt in attempts],
            "visual_matrix": matrix,
            "gate_11_13_passed": passed,
            "recovery_v2": True,
            "prior_frames_inherited": False,
            "protected_pids_touched": False,
            "synthetic_frames_written": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "rerun_allowed": False,
            "complete": True,
        },
        authorization,
    )


def _write_v3_pair(
    root: Path,
    registration: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    state: str,
    reason: str | None,
    matrix: dict[str, Any] | None,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    passed = bool(
        state == "passed"
        and matrix is not None
        and matrix.get("before_visual_equivalent") is True
        and matrix.get("two_by_two_complete") is True
    )
    return _write_once(
        root,
        _V3_PAIR,
        (registration.reference, *(attempt.reference for attempt in attempts)),
        {
            "candidate": dict(_CANDIDATE),
            "state": "passed" if passed else "blocked",
            "outcome": (
                "gate_11_13_visual_pair_passed"
                if passed
                else "candidate_specific_visual_pair_blocked"
            ),
            "reason": None if passed else (reason or "visual_matrix_failed"),
            "attempt_count": len(attempts),
            "ordered_variants": [attempt.payload["variant"] for attempt in attempts],
            "visual_matrix": matrix,
            "gate_11_13_passed": passed,
            "deterministic_v3": True,
            "registered_common_before": all(
                attempt.payload.get("registered_common_before") is True
                for attempt in attempts
            ),
            "ordered_replay": all(
                attempt.payload.get("ordered_replay")
                == ["C1_VISUAL_COMMON_BEFORE_READY", "C1_VISUAL_AFTER_READY"]
                for attempt in attempts
            ),
            "prior_frames_inherited": False,
            "protected_pids_touched": False,
            "synthetic_frames_written": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "rerun_allowed": False,
            "complete": True,
        },
        authorization,
    )


def _write_v4_pair(
    root: Path,
    registration: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    state: str,
    reason: str | None,
    matrix: dict[str, Any] | None,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    passed = bool(
        state == "passed"
        and matrix is not None
        and matrix.get("before_visual_equivalent") is True
        and matrix.get("two_by_two_complete") is True
    )
    return _write_once(
        root,
        _V4_PAIR,
        (registration.reference, *(attempt.reference for attempt in attempts)),
        {
            "candidate": dict(_CANDIDATE),
            "state": "passed" if passed else "blocked",
            "outcome": (
                "gate_11_13_visual_pair_passed"
                if passed
                else "candidate_specific_visual_pair_blocked"
            ),
            "reason": None if passed else (reason or "visual_matrix_failed"),
            "attempt_count": len(attempts),
            "ordered_variants": [attempt.payload["variant"] for attempt in attempts],
            "visual_matrix": matrix,
            "gate_11_13_passed": passed,
            "deterministic_v4": True,
            "registered_common_before": all(
                attempt.payload.get("registered_common_before") is True
                for attempt in attempts
            ),
            "ordered_replay": all(
                attempt.payload.get("ordered_replay")
                == ["C1_VISUAL_COMMON_BEFORE_READY", "C1_VISUAL_AFTER_READY"]
                for attempt in attempts
            ),
            "prior_frames_inherited": False,
            "protected_pids_touched": False,
            "synthetic_frames_written": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "rerun_allowed": False,
            "complete": True,
        },
        authorization,
    )


def _write_once(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, Any],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if _try_load(root, reference) is not None:
        raise RuntimeError(f"one-shot artifact already exists: {reference.artifact_id}")
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


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "bounded-candidate-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("15519 F12 recovery authorization is inactive")


def _artifact_safe_timestamp() -> str:
    return _now().replace(":", "-").replace(".", "-").lower()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "run_luanti_15519_f12_deterministic_v4_pair",
    "run_luanti_15519_f12_deterministic_pair",
    "run_luanti_15519_f12_recovery_pair",
]
