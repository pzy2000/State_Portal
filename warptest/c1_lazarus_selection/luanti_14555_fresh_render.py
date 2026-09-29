"""Fresh current-v2 rendered replay for Luanti #14555.

This runner starts a new evidence namespace and binds every observation to the
candidate-specific client closure.  Historical rendered attempts are never
loaded or resumed.
"""

from __future__ import annotations

import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Literal

from warptest.luanti_state import LuantiWarmSession

from .luanti_14555_rendered_oracle import (
    _EXPECTED_PLACEMENT,
    _PLAYER_POSITION,
    _STATIC_WALL_FROM,
    _STATIC_WALL_TO,
    _TARGET_NODE,
    _run_one_observation,
    _visual_cross_matrix,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_14555_CURRENT_V2_FRESH_RENDER"
_CANDIDATE = {"repository_key": "luanti", "change_number": 14555}
_TASK_ID = "lz-luanti-14555-negative-half-pointing"
_V1 = "1.0"
_VERSIONS: tuple[Literal["fixed", "buggy"], ...] = ("fixed", "buggy")
_REPETITIONS = (1, 2, 3)
_TREES = {
    "fixed": Path("private/bounded-runs/luanti/14555-gate57-fixed"),
    "buggy": Path("private/bounded-runs/luanti/14555-gate57-buggy"),
}
_CLIENT_CLOSURE = ArtifactReference(
    "candidate_gate813_14555_client_closure/luanti/14555/v1",
    "candidate_gate813_14555_client_closure",
    _V1,
)
_CLIENT_BINDINGS = {
    variant: ArtifactReference(
        f"candidate_gate813_14555_client_binding/luanti/14555/{variant}/v1",
        "candidate_gate813_14555_client_binding",
        _V1,
    )
    for variant in _VERSIONS
}
_ATTEMPT_KIND = "candidate_gate813_14555_render_attempt"
_AGGREGATE = ArtifactReference(
    "candidate_gate813_14555_render_aggregate/luanti/14555/v1",
    "candidate_gate813_14555_render_aggregate",
    _V1,
)
_LINEAGE = ArtifactReference(
    "candidate_gate813_14555_same_lineage/luanti/14555/v1",
    "candidate_gate813_14555_same_lineage",
    _V1,
)
_BLOCKER = ArtifactReference(
    "candidate_gate813_14555_render_blocker/luanti/14555/v1",
    "candidate_gate813_14555_render_blocker",
    _V1,
)
_ORIGINAL_PLAYER_POSITION = {"x": -10.5, "y": 5, "z": 4}
_ORIGINAL_TARGET_NODE = {"x": -10, "y": 5, "z": 2}
_ORIGINAL_EXPECTED_PLACEMENT = {"x": -10, "y": 5, "z": 3}


@dataclass(frozen=True)
class FreshRenderResult:
    aggregate: StableArtifact
    lineage: StableArtifact
    evidence_run_id: str
    attempt_count: int


def run_luanti_14555_fresh_render(
    artifact_root: str | Path,
    *,
    workspace_root: str | Path,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> FreshRenderResult:
    """Run fixed/buggy x3 real rendered observations in a fresh namespace."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _require_authorization(authorization)
    root = _ordinary_directory(Path(artifact_root).absolute())
    workspace = _ordinary_directory(Path(workspace_root).absolute())
    closure = _load(root, _CLIENT_CLOSURE)
    bindings = {
        variant: _load(root, reference)
        for variant, reference in _CLIENT_BINDINGS.items()
    }
    _validate_client_closure(closure, bindings)
    fixture_transform = _validate_static_view_transform()
    evidence_root = _reserve_evidence_root(root)
    run_id = evidence_root.name
    attempts: list[StableArtifact] = []
    try:
        for version in _VERSIONS:
            tree = _ordinary_directory(root / _TREES[version], root)
            metadata = _metadata(workspace, version)
            for repetition in _REPETITIONS:
                attempt = _run_attempt(
                    root,
                    tree=tree,
                    metadata=metadata,
                    binding=bindings[version],
                    closure=closure,
                    evidence_root=evidence_root,
                    run_id=run_id,
                    version=version,
                    repetition=repetition,
                    authorization=authorization,
                    fixture_transform=fixture_transform,
                )
                attempts.append(attempt)
    except Exception as error:
        _write_blocker(
            root,
            closure=closure,
            bindings=bindings,
            attempts=tuple(attempts),
            run_id=run_id,
            error=error,
            authorization=authorization,
        )
        raise

    aggregate = _write_aggregate(
        root,
        closure=closure,
        attempts=tuple(attempts),
        run_id=run_id,
        authorization=authorization,
        fixture_transform=fixture_transform,
    )
    lineage = _write_lineage(
        root,
        closure=closure,
        bindings=bindings,
        aggregate=aggregate,
        attempts=tuple(attempts),
        run_id=run_id,
        authorization=authorization,
        fixture_transform=fixture_transform,
    )
    return FreshRenderResult(aggregate, lineage, run_id, len(attempts))


def _run_attempt(
    root: Path,
    *,
    tree: Path,
    metadata: Path,
    binding: StableArtifact,
    closure: StableArtifact,
    evidence_root: Path,
    run_id: str,
    version: Literal["fixed", "buggy"],
    repetition: int,
    authorization: AuthorizationRecord,
    fixture_transform: dict[str, object],
) -> StableArtifact:
    raw_dir = evidence_root / version / f"repetition-{repetition}"
    raw_dir.mkdir(parents=True, mode=0o700)
    _ordinary_directory(raw_dir, evidence_root)
    with tempfile.TemporaryDirectory(
        prefix=f"luanti-14555-current-v2-{version}-{repetition}-"
    ) as temporary:
        temp = Path(temporary)
        home = temp / "home"
        home.mkdir(mode=0o700)
        temporary_root = temp / "tmp"
        temporary_root.mkdir(mode=0o700)
        with _isolated_environment(
            metadata, home=home, temporary_root=temporary_root
        ):
            with LuantiWarmSession(
                tree,
                temp / "report",
                cache_root=temp / "cache",
                render=True,
                map_backend="dummy",
                creative_mode=False,
                daylight_mode="fixed_noon",
                deterministic_render=True,
                experiment_seed=0,
            ) as session:
                observation = _run_one_observation(
                    session,
                    artifact_root=root,
                    raw_dir=raw_dir,
                    version=version,
                    repetition=repetition,
                    evidence_run_id=run_id,
                )
    payload = {
        **observation,
        "candidate": dict(_CANDIDATE),
        "client_binding_reference": binding.reference.artifact_id,
        "client_closure_reference": closure.reference.artifact_id,
        "client_source_identity": binding.payload.get("source_identity"),
        "real_native_frames": True,
        "fixture_transform": fixture_transform,
        "old_attempts_loaded": False,
        "inherit_prior_attempts": False,
        "formal_trials_authorized": False,
        "paid_actions_authorized": False,
    }
    reference = ArtifactReference(
        f"candidate_gate813_14555_render_attempt/luanti/14555/{version}/{repetition}/v1",
        _ATTEMPT_KIND,
        _V1,
    )
    return _write(
        root,
        reference,
        (binding.reference, closure.reference),
        payload,
        authorization,
    )


def _write_aggregate(
    root: Path,
    *,
    closure: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    run_id: str,
    authorization: AuthorizationRecord,
    fixture_transform: dict[str, object],
) -> StableArtifact:
    if len(attempts) != 6:
        raise RuntimeError("#14555 fresh rendered aggregate requires six attempts")
    by_version: dict[str, list[StableArtifact]] = {"fixed": [], "buggy": []}
    for attempt in attempts:
        version = attempt.payload.get("version")
        if version not in by_version or attempt.payload.get("evidence_run_id") != run_id:
            raise RuntimeError("#14555 fresh rendered attempt identity drifted")
        if attempt.payload.get("passed") is not True:
            raise RuntimeError("#14555 fresh rendered attempt did not pass")
        by_version[str(version)].append(attempt)
    if any(len(values) != 3 for values in by_version.values()):
        raise RuntimeError("#14555 fresh rendered attempt coverage is incomplete")
    matrix = _visual_cross_matrix(root, by_version)
    payload = {
        "candidate": dict(_CANDIDATE),
        "task_id": _TASK_ID,
        "state": "qualified",
        "outcome": "fresh_rendered_truth_table_passed",
        "evidence_run_id": run_id,
        "registered_replay": "luanti-14555-half-boundary-place-v1",
        "attempt_count": 6,
        "fixed_expected_placement_repetitions": 3,
        "buggy_missing_placement_repetitions": 3,
        "visual_cross_matrix": matrix,
        "before_equivalence": True,
        "after_two_by_two_discriminative": True,
        "real_native_frame_count": 18,
        "fixture_transform": fixture_transform,
        "qualified": True,
        "no_timeout_or_unknown": True,
        "old_attempts_loaded": False,
        "inherit_prior_attempts": False,
        "formal_trials_authorized": False,
        "paid_actions_authorized": False,
        "complete": True,
    }
    return _write(
        root,
        _AGGREGATE,
        (closure.reference, *(attempt.reference for attempt in attempts)),
        payload,
        authorization,
    )


def _write_lineage(
    root: Path,
    *,
    closure: StableArtifact,
    bindings: dict[str, StableArtifact],
    aggregate: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    run_id: str,
    authorization: AuthorizationRecord,
    fixture_transform: dict[str, object],
) -> StableArtifact:
    for attempt in attempts:
        version = str(attempt.payload.get("version"))
        binding = bindings.get(version)
        if binding is None or attempt.inputs != (
            binding.reference,
            closure.reference,
        ):
            raise RuntimeError("#14555 fresh render typed lineage is incomplete")
        if attempt.payload.get("client_source_identity") != binding.payload.get(
            "source_identity"
        ):
            raise RuntimeError("#14555 fresh render source identity drifted")
    payload = {
        "candidate": dict(_CANDIDATE),
        "state": "passed",
        "outcome": "same_lineage_constructed",
        "evidence_run_id": run_id,
        "same_lineage": True,
        "current_v2_client_closure": closure.reference.artifact_id,
        "fixed_binding": bindings["fixed"].reference.artifact_id,
        "buggy_binding": bindings["buggy"].reference.artifact_id,
        "render_aggregate": aggregate.reference.artifact_id,
        "attempt_count": 6,
        "real_native_frames": True,
        "before_equivalence": True,
        "after_two_by_two_discriminative": True,
        "fixture_transform": fixture_transform,
        "gate_1_7_preserved": True,
        "old_attempts_loaded": False,
        "formal_trials_authorized": False,
        "paid_actions_authorized": False,
        "complete": True,
    }
    return _write(
        root,
        _LINEAGE,
        (
            closure.reference,
            bindings["fixed"].reference,
            bindings["buggy"].reference,
            aggregate.reference,
            *(attempt.reference for attempt in attempts),
        ),
        payload,
        authorization,
    )


def _write_blocker(
    root: Path,
    *,
    closure: StableArtifact,
    bindings: dict[str, StableArtifact],
    attempts: tuple[StableArtifact, ...],
    run_id: str,
    error: Exception,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": dict(_CANDIDATE),
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "evidence_run_id": run_id,
        "completed_attempt_count": len(attempts),
        "completed_attempts_are_not_reusable": True,
        "failure_type": type(error).__name__,
        "failure_detail": str(error)[:4096],
        "gate_1_7_preserved": True,
        "old_attempts_loaded": False,
        "formal_trials_authorized": False,
        "paid_actions_authorized": False,
        "complete": True,
    }
    return _write(
        root,
        _BLOCKER,
        (
            closure.reference,
            bindings["fixed"].reference,
            bindings["buggy"].reference,
            *(attempt.reference for attempt in attempts),
        ),
        payload,
        authorization,
    )


def _validate_client_closure(
    closure: StableArtifact, bindings: dict[str, StableArtifact]
) -> None:
    if (
        closure.payload.get("candidate") != _CANDIDATE
        or closure.payload.get("state") != "ready"
        or closure.payload.get("gate_1_7_preserved") is not True
        or closure.payload.get("old_attempts_loaded") is not False
    ):
        raise RuntimeError("#14555 current-v2 client closure is not ready")
    for variant, binding in bindings.items():
        if (
            binding.payload.get("candidate") != _CANDIDATE
            or binding.payload.get("variant") != variant
            or binding.payload.get("state") != "ready"
            or binding.payload.get("client_provenance_ready") is not True
        ):
            raise RuntimeError(f"#14555 {variant} client binding is not ready")


def _validate_static_view_transform() -> dict[str, object]:
    def vector(
        left: dict[str, int | float], right: dict[str, int | float]
    ) -> list[float]:
        return [float(left[axis]) - float(right[axis]) for axis in ("x", "y", "z")]

    delta = vector(_PLAYER_POSITION, _ORIGINAL_PLAYER_POSITION)
    if delta != [0.0, 0.0, 0.0]:
        raise RuntimeError("#14555 static-view player translation drifted")
    for current, original in (
        (_TARGET_NODE, _ORIGINAL_TARGET_NODE),
        (_EXPECTED_PLACEMENT, _ORIGINAL_EXPECTED_PLACEMENT),
    ):
        if vector(current, original) != delta:
            raise RuntimeError("#14555 static-view fixture is not a rigid translation")
    if vector(_TARGET_NODE, _PLAYER_POSITION) != vector(
        _ORIGINAL_TARGET_NODE, _ORIGINAL_PLAYER_POSITION
    ):
        raise RuntimeError("#14555 target relative vector drifted")
    if vector(_EXPECTED_PLACEMENT, _TARGET_NODE) != vector(
        _ORIGINAL_EXPECTED_PLACEMENT, _ORIGINAL_TARGET_NODE
    ):
        raise RuntimeError("#14555 expected-placement relative vector drifted")
    wall_z = {
        int(_STATIC_WALL_FROM["z"]),
        int(_STATIC_WALL_TO["z"]),
    }
    action_z = {
        int(_PLAYER_POSITION["z"]),
        int(_EXPECTED_PLACEMENT["z"]),
        int(_TARGET_NODE["z"]),
    }
    if max(wall_z) >= min(action_z):
        raise RuntimeError("#14555 static wall intersects target/action path")
    return {
        "kind": "original_geometry_with_opaque_static_background_wall",
        "delta": {"x": 0, "y": 0, "z": 0},
        "original_player_position": dict(_ORIGINAL_PLAYER_POSITION),
        "current_player_position": dict(_PLAYER_POSITION),
        "original_target_node": dict(_ORIGINAL_TARGET_NODE),
        "current_target_node": dict(_TARGET_NODE),
        "original_expected_placement": dict(_ORIGINAL_EXPECTED_PLACEMENT),
        "current_expected_placement": dict(_EXPECTED_PLACEMENT),
        "target_relative_vector_unchanged": True,
        "expected_placement_relative_vector_unchanged": True,
        "ordered_input_action_unchanged": True,
        "background_wall_from": dict(_STATIC_WALL_FROM),
        "background_wall_to": dict(_STATIC_WALL_TO),
        "background_wall_node": "basenodes:snowblock",
        "target_restored_after_background_wall": True,
        "background_wall_disjoint_from_target": True,
        "background_wall_disjoint_from_action_path": True,
        "semantic_target_assertion_unchanged": {
            "position": dict(_TARGET_NODE),
            "expected": "basenodes:stone",
        },
        "semantic_placement_assertion_unchanged": {
            "position": dict(_EXPECTED_PLACEMENT),
            "expected": "basenodes:cobble",
        },
    }


def _reserve_evidence_root(root: Path) -> Path:
    parent = root / "private/luanti-14555-current-v2-render/runs"
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    _ordinary_directory(parent, root)
    for _ in range(8):
        candidate = parent / f"run-{uuid.uuid4().hex[:16]}"
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return _ordinary_directory(candidate, parent)
    raise RuntimeError("#14555 could not reserve fresh render evidence root")


def _metadata(workspace: Path, version: str) -> Path:
    return _ordinary_directory(
        workspace / ".warptest-cache/lazarus/metadata" / _TASK_ID / f"{version}.git",
        workspace,
    )


@contextmanager
def _isolated_environment(
    metadata: Path, *, home: Path, temporary_root: Path
) -> Iterator[None]:
    updates = {
        "WARPTEST_LUANTI_TRUSTED_GIT_DIR": str(metadata),
        "HOME": str(home),
        "TMPDIR": str(temporary_root),
    }
    removed = ("LUANTI_BIN", "LUANTI_SERVER_BIN", "WARPTEST_LUANTI_BUILD_DIR")
    previous = {key: os.environ.get(key) for key in (*updates, *removed)}
    os.environ.update(updates)
    for key in removed:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _ordinary_directory(path: Path, parent: Path | None = None) -> Path:
    resolved = path.resolve(strict=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"unsafe directory: {path}")
    if parent is not None:
        registered = parent.resolve(strict=True)
        if resolved != registered and registered not in resolved.parents:
            raise RuntimeError(f"directory escapes registered root: {path}")
    return resolved


def _write(
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
    artifact = _load(root, reference)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#14555 fresh render typed reload failed")
    return artifact


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
        raise PermissionError("preannotation execution authorization is required")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["CONFIRMATION", "FreshRenderResult", "run_luanti_14555_fresh_render"]
