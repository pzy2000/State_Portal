"""Stable-ID version-materialization receipt for Luanti candidate 15519.

The null-client build receipt only proves that four bounded configure/build
processes completed.  The executor removes its working trees, so this module
keeps the formal version-materialization boundary pending until both client
trees and executable outputs are durably materialized and independently
validated.  It never rebuilds, launches a client, or writes a terminal.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 15519}
_VERSION = "2.0"
_TEST_STAGE = ArtifactReference(
    "candidate_stage/luanti/15519/test_repetitions/v2", "candidate_stage", _VERSION
)
_SERVER = ArtifactReference(
    "native_server_closure/luanti/15519/fixed-buggy/v1",
    "native_server_closure",
    "1.0",
)
_CANDIDATE_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/15519/fixed-buggy/v1",
    "candidate_materialization",
    "1.0",
)
_TASK_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/15519/issue-15518/v2",
    "lazarus_task_mapping",
    "2.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/15519/fixed/v2",
    "fixed_source_capsule",
    "2.0",
)
_CLOSURE = ArtifactReference(
    "client_dependency_closure/luanti/15519/darwin-arm64/v2",
    "client_dependency_closure",
    _VERSION,
)
_MATERIALIZATION = ArtifactReference(
    "candidate_stage/luanti/15519/version_materialization/v2",
    "candidate_stage",
    _VERSION,
)
_BUILD_REFS = tuple(
    ArtifactReference(
        f"null_client_build_step/luanti/15519/{variant}/{step}/v2",
        "null_client_build_step",
        _VERSION,
    )
    for variant in ("fixed", "buggy")
    for step in ("configure", "build")
)
_SERVER_OBSERVERS = (
    ArtifactReference(
        "native_server_closure_step/luanti/15519/fixed/observer/v1",
        "native_server_closure_step",
        "1.0",
    ),
    ArtifactReference(
        "native_server_closure_step/luanti/15519/buggy/observer/v1",
        "native_server_closure_step",
        "1.0",
    ),
)
_FIXED_TREE = PurePosixPath("private/materialized/luanti/15519/null-client/v2/fixed")
_BUGGY_TREE = PurePosixPath("private/materialized/luanti/15519/null-client/v2/buggy")
_MISSING = [
    "persistent_fixed_client_tree",
    "persistent_buggy_client_tree",
    "persistent_fixed_client_binary",
    "persistent_buggy_client_binary",
]
_REPLAY_GAPS = (
    "ordered_reference_replay_with_actions_and_waits",
    "raw_fixed_and_buggy_start_observations",
    "semantic_goal_defect_and_invariant_observations",
    "common_before_fixed_after_and_buggy_after_goldens",
    "pairwise_before_visual_equivalence",
    "two_by_two_after_visual_cross_matrix",
    "same_lineage_construction_receipt",
)


@dataclass(frozen=True)
class Luanti15519ReplayPlanningBinding:
    """Read-only binding for replay planning after pair materialization."""

    materialization: StableArtifact
    server_semantics: StableArtifact
    task_mapping: StableArtifact
    fixed_tree: Path
    buggy_tree: Path
    fixed_binary: Path
    buggy_binary: Path
    remaining_gates: tuple[str, ...]

    @property
    def replay_planning_ready(self) -> bool:
        return True

    @property
    def candidate_execution_performed(self) -> bool:
        return False


def write_luanti_15519_version_materialization_pending(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Record built-input binding while preserving the missing-output boundary."""

    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    test_stage, builds, server = _load_inputs(root)
    reference = _MATERIALIZATION
    payload = _pending_payload()
    inputs = (
        test_stage.reference,
        *(item.reference for item in builds),
        server.reference,
    )
    existing = _try_load(root, reference)
    if existing is not None:
        if existing.inputs != inputs or existing.payload != payload:
            raise RuntimeError("Luanti 15519 materialization receipt conflicts")
        _validate_missing_outputs(root)
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
    return load_luanti_15519_version_materialization(root)


def load_luanti_15519_version_materialization(
    artifact_root: str | Path,
) -> StableArtifact:
    """Strictly reload the stage and all build/server dependencies."""

    root = _ordinary_root(Path(artifact_root).absolute())
    test_stage, builds, server = _load_inputs(root)
    artifact = _load(root, _MATERIALIZATION)
    expected_inputs = (
        test_stage.reference,
        *(item.reference for item in builds),
        server.reference,
    )
    if artifact.inputs != expected_inputs:
        raise RuntimeError("Luanti 15519 materialization binding is invalid")
    if artifact.payload != _pending_payload():
        raise RuntimeError("Luanti 15519 materialization payload is invalid")
    _validate_missing_outputs(root)
    return artifact


def load_luanti_15519_replay_planning_binding(
    artifact_root: str | Path,
) -> Luanti15519ReplayPlanningBinding:
    """Load the new persistent pair without changing the old pending stage head."""

    root = _ordinary_root(Path(artifact_root).absolute())
    materialization = _load(root, _CANDIDATE_MATERIALIZATION)
    fixed_tree, buggy_tree, fixed_binary, buggy_binary = (
        _validate_candidate_materialization(root, materialization)
    )
    server = _load(root, _SERVER)
    _validate_server_semantics(server)
    mapping = _load(root, _TASK_MAPPING)
    _validate_task_mapping(root, mapping)
    return Luanti15519ReplayPlanningBinding(
        materialization=materialization,
        server_semantics=server,
        task_mapping=mapping,
        fixed_tree=fixed_tree,
        buggy_tree=buggy_tree,
        fixed_binary=fixed_binary,
        buggy_binary=buggy_binary,
        remaining_gates=_REPLAY_GAPS,
    )


def _load_inputs(
    root: Path,
) -> tuple[StableArtifact, tuple[StableArtifact, ...], StableArtifact]:
    test_stage = _load(root, _TEST_STAGE)
    if (
        test_stage.payload.get("candidate") != _CANDIDATE
        or test_stage.payload.get("stage") != "test_repetitions"
        or test_stage.payload.get("state") != "passed"
        or test_stage.payload.get("complete") is not True
    ):
        raise RuntimeError("Luanti 15519 test repetitions stage is invalid")
    details = test_stage.payload.get("details")
    if (
        type(details) is not dict
        or details.get("next_gate") != "version_materialization"
    ):
        raise RuntimeError("Luanti 15519 test repetitions stage is invalid")
    builds = tuple(_load(root, reference) for reference in _BUILD_REFS)
    for reference, build in zip(_BUILD_REFS, builds, strict=True):
        variant = reference.artifact_id.split("/")[3]
        step = reference.artifact_id.split("/")[4]
        payload = build.payload
        if (
            payload.get("candidate") != _CANDIDATE
            or payload.get("variant") != variant
            or payload.get("step") != step
            or build.inputs != (_CAPSULE, _CLOSURE)
            or payload.get("process_outcome") != "completed"
            or payload.get("returncode") != 0
            or payload.get("complete") is not True
            or payload.get("watchdog_seconds") != (600 if step == "configure" else 1800)
        ):
            raise RuntimeError("Luanti 15519 client build receipt is invalid")
    server = _load(root, _SERVER)
    server_payload = server.payload
    if (
        server.inputs != _SERVER_OBSERVERS
        or server_payload.get("candidate") != _CANDIDATE
        or server_payload.get("server_only") is not True
        or server_payload.get("fixed_build") != "passed"
        or server_payload.get("buggy_build") != "passed"
        or server_payload.get("semantic_qualified") is not True
        or server_payload.get("complete") is not True
    ):
        raise RuntimeError("Luanti 15519 server semantic aggregate is invalid")
    return test_stage, builds, server


def _validate_candidate_materialization(
    root: Path,
    artifact: StableArtifact,
) -> tuple[Path, Path, Path, Path]:
    if artifact.inputs != (
        _CAPSULE,
        _CLOSURE,
        ArtifactReference(
            "candidate_materialization_step/luanti/15519/fixed/configure/v1",
            "candidate_materialization_step",
            "1.0",
        ),
        ArtifactReference(
            "candidate_materialization_step/luanti/15519/fixed/build/v1",
            "candidate_materialization_step",
            "1.0",
        ),
        ArtifactReference(
            "candidate_materialization_step/luanti/15519/buggy/configure/v1",
            "candidate_materialization_step",
            "1.0",
        ),
        ArtifactReference(
            "candidate_materialization_step/luanti/15519/buggy/build/v1",
            "candidate_materialization_step",
            "1.0",
        ),
    ):
        raise RuntimeError("Luanti 15519 candidate materialization binding is invalid")
    payload = artifact.payload
    if (
        payload.get("candidate") != _CANDIDATE
        or payload.get("profile_id") != "luanti-15519-gettext-plural-trim-v2"
        or payload.get("state") != "built"
        or payload.get("outcome") is not None
        or payload.get("missing_required_evidence") != []
        or payload.get("client_launch_performed") is not False
        or payload.get("permanent_terminal_written") is not False
        or payload.get("complete") is not True
        or payload.get("fixed_tree_path")
        != "private/materialized/luanti/15519/null-client/v2/fixed"
        or payload.get("buggy_tree_path")
        != "private/materialized/luanti/15519/null-client/v2/buggy"
        or payload.get("fixed_binary_path")
        != "private/materialized/luanti/15519/null-client/v2/fixed/bin/luanti"
        or payload.get("buggy_binary_path")
        != "private/materialized/luanti/15519/null-client/v2/buggy/bin/luanti"
    ):
        raise RuntimeError("Luanti 15519 candidate materialization payload is invalid")
    step_receipts = payload.get("step_receipts")
    if type(step_receipts) is not list or len(step_receipts) != 4:
        raise RuntimeError(
            "Luanti 15519 candidate materialization receipts are incomplete"
        )
    expected_steps = artifact.inputs[2:]
    expected_values = [
        {
            "artifact_id": reference.artifact_id,
            "artifact_kind": reference.artifact_kind,
            "semantic_version": reference.semantic_version,
        }
        for reference in expected_steps
    ]
    if step_receipts != expected_values:
        raise RuntimeError(
            "Luanti 15519 candidate materialization receipts are invalid"
        )
    paths = {
        "fixed": (
            root / PurePosixPath(str(payload["fixed_tree_path"])),
            root / PurePosixPath(str(payload["fixed_binary_path"])),
        ),
        "buggy": (
            root / PurePosixPath(str(payload["buggy_tree_path"])),
            root / PurePosixPath(str(payload["buggy_binary_path"])),
        ),
    }
    for variant, (tree, binary) in paths.items():
        _ordinary_directory_within(tree, root)
        _ordinary_file_within(binary, tree)
        if not os.access(binary, os.X_OK):
            raise RuntimeError(
                f"Luanti 15519 {variant} client binary is not executable"
            )
    for reference, variant, step in zip(
        expected_steps,
        ("fixed", "fixed", "buggy", "buggy"),
        ("configure", "build", "configure", "build"),
        strict=True,
    ):
        receipt = _load(root, reference)
        tree, binary = paths[variant]
        receipt_payload = receipt.payload
        if (
            receipt.inputs != (_CAPSULE, _CLOSURE)
            or receipt_payload.get("candidate") != _CANDIDATE
            or receipt_payload.get("variant") != variant
            or receipt_payload.get("step") != step
            or receipt_payload.get("watchdog_seconds")
            != (600 if step == "configure" else 1800)
            or receipt_payload.get("process_outcome") != "completed"
            or receipt_payload.get("returncode") != 0
            or receipt_payload.get("tree_path") != tree.relative_to(root).as_posix()
            or receipt_payload.get("binary_path") != binary.relative_to(root).as_posix()
            or receipt_payload.get("complete") is not True
        ):
            raise RuntimeError("Luanti 15519 candidate materialization step is invalid")
    fixed_tree, fixed_binary = paths["fixed"]
    buggy_tree, buggy_binary = paths["buggy"]
    return fixed_tree, buggy_tree, fixed_binary, buggy_binary


def _validate_server_semantics(artifact: StableArtifact) -> None:
    payload = artifact.payload
    if (
        artifact.reference != _SERVER
        or artifact.inputs != _SERVER_OBSERVERS
        or payload.get("candidate") != _CANDIDATE
        or payload.get("server_only") is not True
        or payload.get("fixed_build") != "passed"
        or payload.get("buggy_build") != "passed"
        or payload.get("semantic_qualified") is not True
        or payload.get("complete") is not True
    ):
        raise RuntimeError("Luanti 15519 server semantic aggregate is invalid")


def _validate_task_mapping(root: Path, artifact: StableArtifact) -> None:
    payload = artifact.payload
    if (
        artifact.reference != _TASK_MAPPING
        or payload.get("candidate") != _CANDIDATE
        or payload.get("closure_state") != "mapping_registered_fixture_frozen"
        or payload.get("visual_closure_state") != "pending"
        or payload.get("complete") is not True
    ):
        raise RuntimeError("Luanti 15519 task mapping is invalid")
    fixture_value = payload.get("fixture_root")
    fixture_paths = payload.get("fixture_paths")
    if type(fixture_value) is not str or type(fixture_paths) is not list:
        raise RuntimeError("Luanti 15519 task mapping fixture is invalid")
    fixture = root / PurePosixPath(fixture_value)
    _ordinary_directory_within(fixture, root)
    for relative in fixture_paths:
        if type(relative) is not str:
            raise RuntimeError("Luanti 15519 task mapping fixture is invalid")
        _ordinary_file_within(fixture / PurePosixPath(relative), fixture)


def _ordinary_directory_within(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError(
            "required materialization directory is missing or unsafe"
        ) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("required materialization directory is missing or unsafe")
    return resolved


def _ordinary_file_within(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError(
            "required materialization file is missing or unsafe"
        ) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("required materialization file is missing or unsafe")
    return resolved


def _pending_payload() -> dict[str, object]:
    return {
        "candidate": _CANDIDATE,
        "stage": "version_materialization",
        "state": "downstream_pending",
        "outcome": None,
        "application_pair_status": "missing_persistent_build_outputs",
        "client_build_state": "built",
        "server_semantic_state": "qualified",
        "fixed_tree_path": _FIXED_TREE.as_posix(),
        "buggy_tree_path": _BUGGY_TREE.as_posix(),
        "fixed_client_binary_path": (_FIXED_TREE / "bin/luanti").as_posix(),
        "buggy_client_binary_path": (_BUGGY_TREE / "bin/luanti").as_posix(),
        "missing_required_evidence": list(_MISSING),
        "next_action": "persist_fixed_buggy_client_trees_and_binaries",
        "client_launch_performed": False,
        "permanent_terminal_written": False,
        "paid_actions_authorized": False,
        "complete": True,
    }


def _validate_missing_outputs(root: Path) -> None:
    for relative in (_FIXED_TREE, _BUGGY_TREE):
        path = root / relative
        if path.exists() or path.is_symlink():
            raise RuntimeError("materialization payload is invalid")


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
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _ordinary_root(path: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return resolved


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


__all__ = [
    "Luanti15519ReplayPlanningBinding",
    "load_luanti_15519_version_materialization",
    "load_luanti_15519_replay_planning_binding",
    "write_luanti_15519_version_materialization_pending",
]
