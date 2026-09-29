"""Recompute why the remaining historical JupyterLab rows cannot reach Gate 5--7.

The five roster rows below passed Gate 1--4 statically but never executed a fresh
twelve-cell epoch, because each of them needs an exact historical runtime (old
Node, Karma/Mocha, TypeScript, Webpack and Firefox) that is not available offline.
This audit reads every registered JupyterLab ``dependency_preparation`` head and
records that none of them is ready, so the blocker is mechanical evidence rather
than a narrative.  It never rejects a candidate: the rows stay eligible if a
complete historical runtime is ever registered.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATES = (2752, 3310, 3703, 611, 1250)
_KIND = "dependency_preparation"
_READY = "ready"
_BLOCKER = ArtifactReference(
    "candidate_pool_gate57_runtime_blocker/jupyterlab/2752-3310-3703-611-1250"
    "/current-v1",
    "candidate_pool_gate57_runtime_blocker",
    "1.0",
)


def write_historical_runtime_pool_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist the recomputed no-historical-runtime result for the five rows."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("an active infrastructure-audit authorization is required")
    root = Path(artifact_root).absolute().resolve(strict=True)
    preparations = _load_preparations(root)
    if not preparations:
        raise RuntimeError("no JupyterLab historical runtime preparation is registered")
    ready = [
        reference.artifact_id
        for reference, payload in preparations
        if payload.get("outcome") == _READY
    ]
    payload = {
        "candidates": [
            {"repository_key": "jupyterlab", "change_number": number}
            for number in CANDIDATES
        ],
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "complete": True,
        "terminal": False,
        "resumable": True,
        "reason": "no_registered_historical_jupyterlab_runtime_is_ready",
        "gate_status": {
            "gate1_4": "passed_static",
            "fresh_gate57_epoch": "blocked_before_first_attempt",
        },
        "registered_preparations": [
            {
                "artifact_id": reference.artifact_id,
                "outcome": payload.get("outcome"),
                "complete": payload.get("complete"),
            }
            for reference, payload in preparations
        ],
        "ready_preparations": ready,
        "host_runtime_substitution_allowed": False,
        "next_action": (
            "register_a_complete_offline_historical_runtime_or_replace_the_row_with_"
            "a_strict_modern_candidate"
        ),
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate8_13_performed": False,
    }
    if ready:
        raise RuntimeError(
            "a ready historical runtime exists; run the fresh epoch instead"
        )
    write_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
        created_at=_now(),
        inputs=tuple(reference for reference, _ in preparations),
        payload=payload,
        authorization=authorization,
    )
    return load_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
    )


def _load_preparations(
    root: Path,
) -> tuple[tuple[ArtifactReference, dict[str, object]], ...]:
    base = root / "artifacts" / _KIND
    if not base.is_dir():
        return ()
    found: list[tuple[ArtifactReference, dict[str, object]]] = []
    for head in sorted(base.rglob("head.json")):
        relative = head.parent.relative_to(base)
        parts = relative.parts
        if len(parts) < 2:
            continue
        artifact_id = f"{_KIND}/" + "/".join(parts[:-1])
        if "jupyterlab" not in artifact_id:
            continue
        artifact = load_stable_artifact(
            root,
            artifact_id=artifact_id,
            artifact_kind=_KIND,
            semantic_version=parts[-1],
        )
        found.append((artifact.reference, dict(artifact.payload)))
    return tuple(found)


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CANDIDATES", "write_historical_runtime_pool_blocker"]
