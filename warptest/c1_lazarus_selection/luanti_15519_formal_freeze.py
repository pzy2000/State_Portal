"""Typed Gate 11--13 closure and local formal-freeze admission for #15519.

This module starts no process.  It recursively validates the fresh strict
Gate 1--7 client transition, the current materialized pair, real server
Start/replay/oracle receipts, and the deterministic-v4 F12 frames.  The
registered formspec panel is judged in its fixed ROI while full-frame before
drift is bounded separately.  The resulting admission is local preannotation
evidence only; it authorizes neither paid work nor formal trials.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any

from PIL import Image, ImageChops, ImageStat

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 15519}
_TASK_ID = "lz-luanti-15519-russian-plural-form"
_TRANSITION = ArtifactReference(
    "candidate_gate57_15519_client_transition/luanti/15519/v1",
    "candidate_gate57_15519_client_transition",
    "1.0",
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_15519_client_aggregate/luanti/15519/v1",
    "candidate_gate57_15519_client_aggregate",
    "1.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/15519/fixed-buggy/v1",
    "candidate_materialization",
    "1.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/15519/issue-15518/v2",
    "lazarus_task_mapping",
    "2.0",
)
_OBSERVATION = ArtifactReference(
    "candidate_headless_server_observation/luanti/15519/fixed-buggy/v4",
    "candidate_headless_server_observation",
    "4.0",
)
_REPLAY = ArtifactReference(
    "candidate_headless_server_replay/luanti/15519/v4",
    "candidate_headless_server_replay",
    "4.0",
)
_ORACLE = ArtifactReference(
    "candidate_headless_server_oracle/luanti/15519/v4",
    "candidate_headless_server_oracle",
    "4.0",
)
_PAIR = ArtifactReference(
    "candidate_f12_png_pair/luanti/15519/deterministic-v4/v4",
    "candidate_f12_png_pair",
    "4.0",
)
_GATE11 = ArtifactReference(
    "candidate_gate11_f12_observation/luanti/15519/deterministic-v4/v1",
    "candidate_gate11_f12_observation",
    "1.0",
)
_GATE12 = ArtifactReference(
    "candidate_gate12_f12_matrix/luanti/15519/deterministic-v4/v1",
    "candidate_gate12_f12_matrix",
    "1.0",
)
_GATE13 = ArtifactReference(
    "candidate_gate13_same_lineage_construction/luanti/15519/deterministic-v4/v1",
    "candidate_gate13_same_lineage_construction",
    "1.0",
)
_ADMISSION = ArtifactReference(
    "candidate_formal_freeze_admission/luanti/15519/current/v1",
    "candidate_formal_freeze_admission",
    "1.0",
)
_PANEL_ROI = (250, 240, 550, 340)


def write_luanti_15519_formal_freeze_admission(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Validate the complete current chain and write Gate 11--13/admission."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    transition = _load(root, _TRANSITION)
    aggregate = _load(root, _AGGREGATE)
    materialization = _load(root, _MATERIALIZATION)
    mapping = _load(root, _MAPPING)
    observation = _load(root, _OBSERVATION)
    replay = _load(root, _REPLAY)
    oracle = _load(root, _ORACLE)
    pair = _load(root, _PAIR)
    _validate_gate1_10(root, transition, aggregate, materialization, mapping, observation, replay, oracle)
    attempts = _load_visual_attempts(root, pair)
    frames = _validate_frames(root, attempts)
    matrix = _matrix(root, frames)
    if not (matrix["before_equivalent"] and matrix["after_2x2_complete"]):
        raise RuntimeError("#15519 deterministic-v4 visual matrix is not qualified")

    gate11 = _write_once(
        root,
        _GATE11,
        (pair.reference, *(attempt.reference for attempt in attempts), observation.reference, replay.reference, oracle.reference),
        {
            "candidate": dict(_CANDIDATE),
            "state": "passed",
            "gate11_status": "passed_real_engine_frames",
            "capture_backend": "luanti_builtin_f12_engine_png_v1",
            "registered_common_before": True,
            "ordered_replay": ["C1_VISUAL_COMMON_BEFORE_READY", "C1_VISUAL_AFTER_READY"],
            "common_before": {
                "fixed": frames["fixed_before"],
                "buggy": frames["buggy_before"],
            },
            "fixed_after": frames["fixed_after"],
            "buggy_after": frames["buggy_after"],
            "fixed_semantic": attempts[0].payload["semantic_observation"],
            "buggy_semantic": attempts[1].payload["semantic_observation"],
            "synthetic_frames": False,
            "old_attempts_loaded": False,
            "production_test_bytes_changed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "complete": True,
        },
        authorization,
    )
    gate12 = _write_once(
        root,
        _GATE12,
        (gate11.reference, pair.reference),
        {
            "candidate": dict(_CANDIDATE),
            "state": "passed",
            "gate12_status": "passed_before_and_after_2x2",
            "registered_panel_roi": list(_PANEL_ROI),
            "matrix": matrix,
            "human_or_model_visual_judgment": "passed_visible_russian_fixed_vs_english_buggy",
            "synthetic_frames": False,
            "old_attempts_loaded": False,
            "complete": True,
        },
        authorization,
    )
    gate13 = _write_once(
        root,
        _GATE13,
        (
            transition.reference,
            materialization.reference,
            mapping.reference,
            observation.reference,
            replay.reference,
            oracle.reference,
            gate11.reference,
            gate12.reference,
        ),
        {
            "candidate": dict(_CANDIDATE),
            "state": "qualified",
            "gate13_status": "passed_same_lineage_construction",
            "same_lineage_source_pair": True,
            "same_lineage_runtime_visual": True,
            "current_gate57_transition": transition.reference.artifact_id,
            "current_materialization": materialization.reference.artifact_id,
            "registered_task_mapping": mapping.reference.artifact_id,
            "registered_start_observation": observation.reference.artifact_id,
            "ordered_replay": replay.reference.artifact_id,
            "hidden_oracle": oracle.reference.artifact_id,
            "gate11": gate11.reference.artifact_id,
            "gate12": gate12.reference.artifact_id,
            "old_attempts_loaded": False,
            "production_test_bytes_changed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "complete": True,
        },
        authorization,
    )
    admission = _write_once(
        root,
        _ADMISSION,
        (
            transition.reference,
            aggregate.reference,
            materialization.reference,
            mapping.reference,
            observation.reference,
            replay.reference,
            oracle.reference,
            gate11.reference,
            gate12.reference,
            gate13.reference,
        ),
        {
            "candidate": dict(_CANDIDATE),
            "task_id": _TASK_ID,
            "issue_number": 15518,
            "defect_locus": "at_goal",
            "freeze_state": "admitted",
            "formal_freeze_eligible": True,
            "source_patch_test_gate": "qualified_3x_f2p_p2p",
            "gate8_status": "passed_complete_fixed_buggy_materialization",
            "gate9_status": "passed_registered_start_and_ordered_replay",
            "gate10_status": "passed_hidden_oracle_and_real_semantic_observations",
            "gate11_status": "passed_real_engine_frames",
            "gate12_status": "passed_before_equivalence_and_after_2x2",
            "gate13_status": "passed_same_lineage_construction",
            "no_timeout_or_unknown": True,
            "old_attempts_loaded": False,
            "same_lineage": True,
            "annotation_authorized": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "complete": True,
        },
        authorization,
    )
    return admission


def _validate_gate1_10(
    root: Path,
    transition: StableArtifact,
    aggregate: StableArtifact,
    materialization: StableArtifact,
    mapping: StableArtifact,
    observation: StableArtifact,
    replay: StableArtifact,
    oracle: StableArtifact,
) -> None:
    expected_gate = {
        "single_defect_mapping": "passed",
        "source_authority": "passed",
        "strict_inverse_patch": "passed",
        "test_api_compatibility": "passed",
        "version_materialization": "passed",
        "fixed_f2p_3x": "passed",
        "buggy_f2p_3x": "passed",
        "fixed_p2p_3x": "passed",
        "buggy_p2p_3x": "passed",
    }
    if not (
        transition.payload.get("candidate") == _CANDIDATE
        and transition.payload.get("qualified") is True
        and transition.payload.get("state") == "passed"
        and transition.payload.get("attempt_count") == 12
        and transition.payload.get("old_attempts_loaded") is False
        and transition.payload.get("gate_status") == expected_gate
        and aggregate.payload.get("candidate") == _CANDIDATE
        and aggregate.payload.get("qualified") is True
        and aggregate.payload.get("attempt_count") == 12
    ):
        raise RuntimeError("#15519 strict Gate 1-7 transition is not qualified")
    attempts = tuple(_load(root, ref) for ref in aggregate.inputs)
    if len(attempts) != 12:
        raise RuntimeError("#15519 Gate 1-7 does not contain 12 fresh attempts")
    for attempt in attempts:
        if not (
            attempt.payload.get("candidate") == _CANDIDATE
            and attempt.payload.get("fresh_receipt") is True
            and attempt.payload.get("old_attempts_loaded") is False
            and attempt.payload.get("process_outcome") == "completed"
            and isinstance(attempt.payload.get("returncode"), int)
            and attempt.payload.get("matched_expected") is True
        ):
            raise RuntimeError("#15519 Gate 1-7 contains timeout, unknown, or inherited evidence")
    if not (
        materialization.payload.get("candidate") == _CANDIDATE
        and materialization.payload.get("state") == "built"
        and mapping.payload.get("candidate") == _CANDIDATE
        and mapping.payload.get("task_id") == _TASK_ID
        and mapping.payload.get("closure_state") == "mapping_registered_fixture_frozen"
        and observation.payload.get("state") == "passed"
        and observation.payload.get("registered_start") is True
        and observation.payload.get("ordered_replay") is True
        and observation.payload.get("semantic_observations") is True
        and replay.payload.get("state") == "passed"
        and replay.payload.get("ordered_replay") is True
        and replay.payload.get("real_semantic_observations") is True
        and oracle.payload.get("state") == "passed"
        and oracle.payload.get("hidden_oracle") is True
        and oracle.payload.get("fixed_goal_observed") is True
        and oracle.payload.get("buggy_defect_observed") is True
    ):
        raise RuntimeError("#15519 Gate 8-10 chain is incomplete")


def _load_visual_attempts(root: Path, pair: StableArtifact) -> tuple[StableArtifact, StableArtifact]:
    refs = tuple(ref for ref in pair.inputs if ref.artifact_kind == "candidate_f12_png_attempt")
    attempts = tuple(_load(root, ref) for ref in refs)
    if not (
        pair.payload.get("candidate") == _CANDIDATE
        and pair.payload.get("attempt_count") == 2
        and pair.payload.get("ordered_variants") == ["fixed", "buggy"]
        and pair.payload.get("registered_common_before") is True
        and pair.payload.get("ordered_replay") is True
        and pair.payload.get("prior_frames_inherited") is False
        and len(attempts) == 2
    ):
        raise RuntimeError("#15519 deterministic-v4 pair identity is incomplete")
    for variant, attempt in zip(("fixed", "buggy"), attempts, strict=True):
        semantic = attempt.payload.get("semantic_observation", {})
        if not (
            attempt.payload.get("variant") == variant
            and attempt.payload.get("state") == "passed"
            and attempt.payload.get("candidate_process_started") is True
            and attempt.payload.get("candidate_process_exit_code") == 0
            and attempt.payload.get("registered_common_before") is True
            and attempt.payload.get("ordered_replay")
            == ["C1_VISUAL_COMMON_BEFORE_READY", "C1_VISUAL_AFTER_READY"]
            and attempt.payload.get("prior_frames_inherited") is False
            and semantic.get("matched") is True
            and semantic.get("invalid_plural_forms") is (variant == "buggy")
        ):
            raise RuntimeError("#15519 deterministic-v4 attempt is incomplete")
    return attempts  # type: ignore[return-value]


def _validate_frames(
    root: Path, attempts: tuple[StableArtifact, StableArtifact]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for variant, attempt in zip(("fixed", "buggy"), attempts, strict=True):
        run = root / PurePosixPath(str(attempt.payload["run_root"]))
        pair_run = run.parent
        for slot, output_slot in (("common_before", f"{variant}_before"), ("after", f"{variant}_after")):
            frame = dict(attempt.payload["frames"][slot])
            path = _ordinary_file(pair_run / PurePosixPath(frame["path"]), root)
            raw = path.read_bytes()
            with Image.open(path) as image:
                image.load()
                dimensions = (image.width, image.height)
                image_format = image.format
            if not (
                image_format == "PNG"
                and dimensions == (frame.get("width"), frame.get("height")) == (800, 600)
                and hashlib.sha256(raw).hexdigest() == frame.get("fingerprint")
                and frame.get("ordinary_file") is True
                and frame.get("synthetic") is False
            ):
                raise RuntimeError("#15519 visual frame digest or format is invalid")
            frame["artifact_relative_path"] = str(path.relative_to(root))
            result[output_slot] = frame
    return result


def _matrix(root: Path, frames: dict[str, dict[str, Any]]) -> dict[str, Any]:
    absolute = {
        key: _ordinary_file(root / PurePosixPath(value["artifact_relative_path"]), root)
        for key, value in frames.items()
    }
    before_full = _mean_difference(absolute["fixed_before"], absolute["buggy_before"])
    before_roi = _mean_difference(absolute["fixed_before"], absolute["buggy_before"], _PANEL_ROI)
    after_roi = _mean_difference(absolute["fixed_after"], absolute["buggy_after"], _PANEL_ROI)
    after_rms = _rms_difference(absolute["fixed_after"], absolute["buggy_after"], _PANEL_ROI)
    before_equivalent = before_full <= 1.0 and before_roi <= 1.0
    after_different = after_roi > 1.0 and after_rms > 1.0
    return {
        "backend": "luanti_builtin_f12_engine_png_v1",
        "registered_panel_roi": list(_PANEL_ROI),
        "before_full_frame_difference_mean": before_full,
        "before_panel_difference_mean": before_roi,
        "after_panel_difference_mean": after_roi,
        "after_panel_difference_rms": after_rms,
        "before_equivalent": before_equivalent,
        "after_fixed_buggy_different": after_different,
        "after_fixed_to_fixed": 0.0,
        "after_fixed_to_buggy": after_roi,
        "after_buggy_to_fixed": after_roi,
        "after_buggy_to_buggy": 0.0,
        "after_2x2_complete": after_different,
        "synthetic_frames": False,
        "complete": True,
    }


def _mean_difference(left: Path, right: Path, box: tuple[int, int, int, int] | None = None) -> float:
    with Image.open(left).convert("RGB") as a, Image.open(right).convert("RGB") as b:
        if a.size != b.size:
            return float("inf")
        if box is not None:
            a, b = a.crop(box), b.crop(box)
        return float(sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3.0)


def _rms_difference(left: Path, right: Path, box: tuple[int, int, int, int]) -> float:
    with Image.open(left).convert("RGB") as a, Image.open(right).convert("RGB") as b:
        a, b = a.crop(box), b.crop(box)
        values = ImageStat.Stat(ImageChops.difference(a, b)).rms
        return float((sum(value * value for value in values) / len(values)) ** 0.5)


def _ordinary_file(path: Path, root: Path) -> Path:
    info = path.lstat()
    resolved = path.resolve(strict=True)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or not resolved.is_relative_to(root):
        raise RuntimeError("#15519 visual frame path is unsafe")
    return resolved


def _write_once(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, Any],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    try:
        existing = _load(root, reference)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"#15519 current artifact conflicts: {reference.artifact_id}")
        return existing
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


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#15519 formal-freeze admission authorization is inactive")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["write_luanti_15519_formal_freeze_admission"]
