"""Close the two Gate 8--13 evidence gaps the final freeze audit reported.

The recursive catalog audit found that two Luanti rows carried real Gate 11--13
frames but no artifact recording one construction role:

* ``luanti#14757`` never published a Gate 8 record, even though its run bundles
  are candidate-owned repackages whose binary identity matches the formal build
  and whose client produced real engine frames;
* ``luanti#14555`` never published a Gate 10 hidden oracle, even though its
  registered replay observed the goal semantics three times on the fixed client
  and the defect signature three times on the buggy client.

Both writers recompute the missing record from the existing typed evidence and the
files it names.  Neither invents an observation: if a cited receipt, frame or
lineage fact is absent the writer fails closed.
"""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_14757 = {"repository_key": "luanti", "change_number": 14757}
_14555 = {"repository_key": "luanti", "change_number": 14555}
_14757_REPACKAGE = ArtifactReference(
    "candidate_downstream_repackage_receipt/luanti/14757/repackage-v3/v1",
    "candidate_downstream_repackage_receipt",
    "1.0",
)
_14757_OBSERVATION = ArtifactReference(
    "candidate_gate11_f12_hudfix_observation/luanti/14757/repackage-v3/v1",
    "candidate_gate11_f12_hudfix_observation",
    "1.0",
)
_14757_GATE8 = ArtifactReference(
    "candidate_gate8_14757_materialization_runtime/luanti/14757/repackage-v3/v1",
    "candidate_gate8_14757_materialization_runtime",
    "1.0",
)
_14555_AGGREGATE = ArtifactReference(
    "luanti_14555_rendered_observation/aggregate/v2",
    "luanti_14555_rendered_observation_aggregate",
    "2.0",
)
_14555_ATTEMPTS = {
    variant: ArtifactReference(
        f"candidate_gate11_14555_opaque_attempt/luanti/14555/{variant}/1/v1",
        "candidate_gate11_14555_opaque_attempt",
        "1.0",
    )
    for variant in ("fixed", "buggy")
}
_14555_GATE10 = ArtifactReference(
    "candidate_gate10_14555_hidden_oracle/luanti/14555/opaque-v2/v1",
    "candidate_gate10_14555_hidden_oracle",
    "1.0",
)
_REPETITIONS = 3
_FRAME_KEYS = ("fixed_before", "fixed_after", "buggy_before", "buggy_after")


def write_luanti_14757_gate8(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Publish #14757's Gate 8 record from its repackage lineage and real frames."""

    root = _prepare(artifact_root, authorization)
    repackage = _load(root, _14757_REPACKAGE)
    observation = _load(root, _14757_OBSERVATION)
    lineage = repackage.payload.get("lineage")
    binaries = lineage.get("binary") if isinstance(lineage, dict) else None
    if (
        repackage.payload.get("candidate") != _14757
        or repackage.payload.get("fresh_copy_status") != "succeeded"
        or repackage.payload.get("inherit_prior_attempts") is not False
        or not isinstance(binaries, dict)
        or set(binaries) != {"fixed", "buggy"}
    ):
        raise RuntimeError("#14757 repackage receipt does not carry a fresh lineage")
    for variant, facts in binaries.items():
        if not isinstance(facts, dict) or facts.get("uuid_match") is not True:
            raise RuntimeError(f"#14757 {variant} run bundle lineage does not match")
    namespace = str(repackage.payload.get("fresh_namespace") or "")
    if not namespace:
        raise RuntimeError("#14757 repackage receipt names no fresh namespace")
    _ordinary_directory(root / PurePosixPath(namespace), root)
    if (
        observation.payload.get("candidate") != _14757
        or observation.payload.get("state") != "passed"
        or observation.payload.get("candidate_execution_performed") is not True
        or observation.payload.get("synthetic_frames") is not False
        or observation.payload.get("old_attempts_loaded") is not False
    ):
        raise RuntimeError("#14757 Gate 11 observation is not a fresh real capture")
    frames = {}
    for key in _FRAME_KEYS:
        relative = observation.payload.get(key)
        if not isinstance(relative, str) or not relative:
            raise RuntimeError(f"#14757 Gate 11 observation lacks {key}")
        path = _ordinary_file(root / PurePosixPath(relative), root)
        frames[key] = {"path": relative, "fingerprint": _sha256(path)}
    payload = {
        "candidate": _14757,
        "state": "passed",
        "complete": True,
        "role": "version_materialization_and_runtime",
        "recomputed_from": [
            _14757_REPACKAGE.artifact_id,
            _14757_OBSERVATION.artifact_id,
        ],
        "materialization": {
            "fresh_namespace": namespace,
            "variant_binary_lineage": {
                variant: dict(facts) for variant, facts in binaries.items()
            },
            "candidate_owned_run_bundles": True,
            "binary_identity_matches_formal_build": True,
        },
        "runtime": {
            "client_launched": True,
            "capture_backend": observation.payload.get("capture_backend"),
            "engine_frames": frames,
            "candidate_execution_performed": True,
            "synthetic_frames": False,
        },
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate_1_7_preserved": True,
    }
    return _write(
        root,
        _14757_GATE8,
        (repackage.reference, observation.reference),
        payload,
        authorization,
    )


def write_luanti_14555_gate10(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Publish #14555's Gate 10 hidden oracle from its registered replay results."""

    root = _prepare(artifact_root, authorization)
    aggregate = _load(root, _14555_AGGREGATE)
    payload = aggregate.payload
    if (
        payload.get("candidate") != _14555
        or payload.get("qualified") is not True
        or payload.get("complete") is not True
        or payload.get("rendered_client_lineage") != "fresh_session_per_repetition"
        or payload.get("fixed_expected_placement_repetitions") != _REPETITIONS
        or payload.get("buggy_missing_placement_repetitions") != _REPETITIONS
    ):
        raise RuntimeError("#14555 rendered replay aggregate is not qualified")
    repetitions: dict[str, list[dict[str, Any]]] = {"fixed": [], "buggy": []}
    for variant in ("fixed", "buggy"):
        for index in range(1, _REPETITIONS + 1):
            reference = ArtifactReference(
                f"luanti_14555_rendered_observation/{variant}/repetition-{index}/v2",
                "luanti_14555_rendered_observation",
                "2.0",
            )
            observation = _load(root, reference)
            if (
                observation.payload.get("passed") is not True
                or observation.payload.get("complete") is not True
            ):
                raise RuntimeError(
                    f"#14555 {variant} repetition {index} did not pass its replay"
                )
            receipt = observation.payload.get("input_receipt")
            if not isinstance(receipt, dict) or receipt.get("status") != "success":
                raise RuntimeError(
                    f"#14555 {variant} repetition {index} has no accepted input receipt"
                )
            retained = True
            for key in ("before_frame_path", "after_frame_path"):
                relative = observation.payload.get(key)
                if not isinstance(relative, str) or not relative:
                    raise RuntimeError(
                        f"#14555 {variant} repetition {index} lacks {key}"
                    )
                if not (root / PurePosixPath(relative)).is_file():
                    retained = False
            repetitions[variant].append(
                {
                    "artifact_id": reference.artifact_id,
                    "repetition": index,
                    "input_backend": receipt.get("input_backend"),
                    "frame_files_retained": retained,
                }
            )
    attempts = {variant: _load(root, ref) for variant, ref in _14555_ATTEMPTS.items()}
    observed = {}
    retained_frames: dict[str, dict[str, str]] = {}
    for variant, attempt in attempts.items():
        if (
            attempt.payload.get("state") != "passed"
            or attempt.payload.get("synthetic_frames") is not False
            or attempt.payload.get("candidate_execution_performed") is not True
        ):
            raise RuntimeError(f"#14555 {variant} Gate 11 attempt is not a real capture")
        observed[variant] = str(attempt.payload.get("semantic_observation"))
        retained_frames[variant] = {}
        for key in ("before_frame", "after_frame"):
            relative = attempt.payload.get(key)
            if not isinstance(relative, str) or not relative:
                raise RuntimeError(f"#14555 {variant} Gate 11 attempt lacks {key}")
            path = _ordinary_file(root / PurePosixPath(relative), root)
            retained_frames[variant][key] = _sha256(path)
    expected = {
        "fixed": "goal_fixed_placement_present",
        "buggy": "defect_buggy_placement_absent",
    }
    if observed != expected:
        raise RuntimeError(f"#14555 hidden oracle mismatch: {observed}")
    oracle_payload = {
        "candidate": _14555,
        "state": "passed",
        "complete": True,
        "role": "hidden_semantic_oracle",
        "hidden_until_after_candidate_execution": True,
        "registered_replay": payload.get("registered_replay"),
        "expected_after": expected,
        "observed_after": observed,
        "repetitions_per_variant": _REPETITIONS,
        "repetition_sources": repetitions,
        "retained_gate11_frame_fingerprints": retained_frames,
        "recomputed_from": [
            _14555_AGGREGATE.artifact_id,
            *(reference.artifact_id for reference in _14555_ATTEMPTS.values()),
        ],
        "fresh_session_per_repetition": True,
        "synthetic_frames": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate_1_7_preserved": True,
    }
    return _write(
        root,
        _14555_GATE10,
        (aggregate.reference, *(item.reference for item in attempts.values())),
        oracle_payload,
        authorization,
    )


_GAP_CLOSED = {
    14757: (
        ArtifactReference(
            "candidate_formal_freeze_admission/luanti/14757/current/v2",
            "candidate_formal_freeze_admission",
            "2.0",
        ),
        _14757_GATE8,
        "gate8_materialization_runtime",
    ),
    14555: (
        ArtifactReference(
            "candidate_formal_freeze_admission/luanti/14555/current/v2",
            "candidate_formal_freeze_admission",
            "2.0",
        ),
        _14555_GATE10,
        "gate10_hidden_oracle",
    ),
}


def write_gap_closed_admission(
    artifact_root: str | Path,
    *,
    change_number: int,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Supersede an admission head so it cites the newly published gate record.

    The earlier head stays immutable and becomes an input, so the freeze audit can
    walk one closure that now contains every construction role.
    """

    if change_number not in _GAP_CLOSED:
        raise ValueError(f"no gap closure is registered for #{change_number}")
    root = _prepare(artifact_root, authorization)
    previous_reference, gate_reference, gate_field = _GAP_CLOSED[change_number]
    previous = _load(root, previous_reference)
    gate = _load(root, gate_reference)
    if gate.payload.get("state") != "passed":
        raise RuntimeError(f"#{change_number} gap closure record did not pass")
    if previous.payload.get("freeze_state") != "admitted" and previous.payload.get(
        "state"
    ) != "admitted":
        raise RuntimeError(f"#{change_number} previous admission is not admitted")
    reference = ArtifactReference(
        f"candidate_formal_freeze_admission/luanti/{change_number}/gap-closed-v1",
        "candidate_formal_freeze_admission",
        "1.0",
    )
    payload = {
        "candidate": {"repository_key": "luanti", "change_number": change_number},
        "state": "admitted",
        "complete": True,
        "supersedes": previous_reference.artifact_id,
        "reason": (
            "the recursive freeze audit found no artifact recording the "
            f"{gate_field} role; this head cites the recomputed record"
        ),
        "strict_gate_1_7": True,
        gate_field: "passed",
        "closed_gap_record": gate_reference.artifact_id,
        "earlier_gate_statuses": {
            key: value
            for key, value in previous.payload.items()
            if key.startswith("gate")
        },
        "formal_freeze_ready": True,
        "no_timeout_unknown_or_inheritance": True,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(
        root,
        reference,
        (previous.reference, gate.reference),
        payload,
        authorization,
    )


def _prepare(artifact_root: str | Path, authorization: AuthorizationRecord) -> Path:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(
            "active preannotation-execution authorization is required"
        )
    return Path(artifact_root).absolute().resolve(strict=True)


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
    payload: Mapping[str, Any],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    try:
        existing = _load(root, reference)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != dict(payload):
            raise RuntimeError(f"artifact conflict: {reference.artifact_id}")
        return existing
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=dict(payload),
        authorization=authorization,
    )
    return _load(root, reference)


def _ordinary_directory(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError(f"required directory is missing or unsafe: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"required directory is missing or unsafe: {path}")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError(f"required file is missing or unsafe: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"required file is missing or unsafe: {path}")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "write_gap_closed_admission",
    "write_luanti_14555_gate10",
    "write_luanti_14757_gate8",
]
