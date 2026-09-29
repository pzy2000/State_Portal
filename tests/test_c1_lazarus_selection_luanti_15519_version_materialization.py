from __future__ import annotations

from datetime import UTC, datetime, timedelta

import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_15519_version_materialization import (
    load_luanti_15519_version_materialization,
    write_luanti_15519_version_materialization_pending,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 15519}
_TEST_STAGE = ArtifactReference(
    "candidate_stage/luanti/15519/test_repetitions/v2", "candidate_stage", "2.0"
)
_SERVER = ArtifactReference(
    "native_server_closure/luanti/15519/fixed-buggy/v1",
    "native_server_closure",
    "1.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/15519/fixed/v2", "fixed_source_capsule", "2.0"
)
_CLOSURE = ArtifactReference(
    "client_dependency_closure/luanti/15519/darwin-arm64/v2",
    "client_dependency_closure",
    "2.0",
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
_BUILD_REFS = tuple(
    ArtifactReference(
        f"null_client_build_step/luanti/15519/{variant}/{step}/v2",
        "null_client_build_step",
        "2.0",
    )
    for variant in ("fixed", "buggy")
    for step in ("configure", "build")
)


def _after_now() -> str:
    """Return a timestamp just after the writer's own real-clock stamp."""

    moment = datetime.now(UTC) + timedelta(seconds=1)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/luanti-15519-version-materialization",
        scope="preannotation-execution",
        granted_at="2026-08-14T05:00:00Z",
        local_user_id=os.getuid(),
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    *,
    inputs: tuple[ArtifactReference, ...] = (),
    payload: dict[str, object],
    created_at: str = "2026-08-14T05:00:01Z",
) -> None:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=created_at,
        inputs=inputs,
        payload=payload,
        authorization=_authorization(),
    )


def _seed_inputs(root: Path) -> None:
    _write(
        root,
        _TEST_STAGE,
        payload={
            "candidate": _CANDIDATE,
            "stage": "test_repetitions",
            "state": "passed",
            "outcome": None,
            "details": {"next_gate": "version_materialization"},
            "complete": True,
        },
    )
    for reference in _BUILD_REFS:
        variant = reference.artifact_id.split("/")[3]
        step = reference.artifact_id.split("/")[4]
        _write(
            root,
            reference,
            inputs=(_CAPSULE, _CLOSURE),
            payload={
                "candidate": _CANDIDATE,
                "variant": variant,
                "step": step,
                "watchdog_seconds": 600 if step == "configure" else 1800,
                "process_outcome": "completed",
                "returncode": 0,
                "complete": True,
            },
        )
    _write(
        root,
        _SERVER,
        inputs=_SERVER_OBSERVERS,
        payload={
            "candidate": _CANDIDATE,
            "server_only": True,
            "fixed_build": "passed",
            "buggy_build": "passed",
            "semantic_qualified": True,
            "complete": True,
        },
    )


def test_pending_materialization_binds_all_builds_and_server_semantics(
    tmp_path: Path,
) -> None:
    _seed_inputs(tmp_path)

    artifact = write_luanti_15519_version_materialization_pending(
        tmp_path, authorization=_authorization()
    )
    loaded = load_luanti_15519_version_materialization(tmp_path)

    assert loaded.reference == artifact.reference
    assert loaded.inputs == (_TEST_STAGE, *_BUILD_REFS, _SERVER)
    assert loaded.payload["state"] == "downstream_pending"
    assert (
        loaded.payload["application_pair_status"] == "missing_persistent_build_outputs"
    )
    assert loaded.payload["client_build_state"] == "built"
    assert loaded.payload["server_semantic_state"] == "qualified"
    assert loaded.payload["client_launch_performed"] is False
    assert loaded.payload["permanent_terminal_written"] is False
    assert loaded.payload["missing_required_evidence"] == [
        "persistent_fixed_client_tree",
        "persistent_buggy_client_tree",
        "persistent_fixed_client_binary",
        "persistent_buggy_client_binary",
    ]


def test_loader_rejects_missing_or_failed_build_receipt(tmp_path: Path) -> None:
    _seed_inputs(tmp_path)
    write_luanti_15519_version_materialization_pending(
        tmp_path, authorization=_authorization()
    )
    build = load_stable_artifact(
        tmp_path,
        artifact_id=_BUILD_REFS[1].artifact_id,
        artifact_kind=_BUILD_REFS[1].artifact_kind,
        semantic_version=_BUILD_REFS[1].semantic_version,
    )
    _write(
        tmp_path,
        build.reference,
        payload={**build.payload, "returncode": 1},
        created_at="2026-08-14T05:00:02Z",
    )
    with pytest.raises(RuntimeError, match="build receipt is invalid"):
        load_luanti_15519_version_materialization(tmp_path)


def test_loader_rejects_claimed_complete_pair_without_safe_outputs(
    tmp_path: Path,
) -> None:
    _seed_inputs(tmp_path)
    write_luanti_15519_version_materialization_pending(
        tmp_path, authorization=_authorization()
    )
    reference = ArtifactReference(
        "candidate_stage/luanti/15519/version_materialization/v2",
        "candidate_stage",
        "2.0",
    )
    current = load_stable_artifact(
        tmp_path,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    _write(
        tmp_path,
        reference,
        inputs=current.inputs,
        payload={
            **current.payload,
            "state": "passed",
            "application_pair_status": "complete_persistent_pair",
        },
        # The pending stage is stamped with the real clock, so the claimed-complete
        # revision has to be newer than that to become the current head.
        created_at=_after_now(),
    )
    with pytest.raises(RuntimeError, match="materialization payload is invalid"):
        load_luanti_15519_version_materialization(tmp_path)
