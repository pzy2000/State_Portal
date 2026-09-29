from __future__ import annotations

import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection import (
    c1_lazarus_downstream_gap_closure as target,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def _authorization(scope: str = "preannotation-execution") -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=f"test/c1-lazarus-gap-closure/{scope}",
        scope=scope,
        granted_at="2026-08-15T04:30:00Z",
        local_user_id=os.getuid(),
    )


def _seed(
    root: Path, reference: ArtifactReference, payload: dict[str, object]
) -> None:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at="2026-08-15T04:00:00Z",
        inputs=(),
        payload=payload,
        authorization=_authorization("infrastructure-audit"),
    )


def _touch(root: Path, relative: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)


def _fixture_14757(root: Path) -> None:
    namespace = "private/candidate_downstream/luanti/14757/repackage-v3"
    (root / namespace).mkdir(parents=True)
    _seed(
        root,
        target._14757_REPACKAGE,
        {
            "candidate": target._14757,
            "fresh_copy_status": "succeeded",
            "inherit_prior_attempts": False,
            "fresh_namespace": namespace,
            "lineage": {
                "binary": {
                    "fixed": {"uuid_match": True, "repackage_v3_binary_uuid": "A"},
                    "buggy": {"uuid_match": True, "repackage_v3_binary_uuid": "B"},
                }
            },
        },
    )
    frames = {
        key: f"private/bounded-runs/luanti/14757/f12/{key}.png"
        for key in target._FRAME_KEYS
    }
    for relative in frames.values():
        _touch(root, relative)
    _seed(
        root,
        target._14757_OBSERVATION,
        {
            "candidate": target._14757,
            "state": "passed",
            "candidate_execution_performed": True,
            "synthetic_frames": False,
            "old_attempts_loaded": False,
            "capture_backend": "luanti_builtin_f12_engine_png_v1",
            **frames,
        },
    )


def _fixture_14555(
    root: Path, *, buggy_semantics: str = "defect_buggy_placement_absent"
) -> None:
    _seed(
        root,
        target._14555_AGGREGATE,
        {
            "candidate": target._14555,
            "qualified": True,
            "complete": True,
            "rendered_client_lineage": "fresh_session_per_repetition",
            "fixed_expected_placement_repetitions": 3,
            "buggy_missing_placement_repetitions": 3,
            "registered_replay": "luanti-14555-half-boundary-place-v1",
        },
    )
    for variant in ("fixed", "buggy"):
        for index in (1, 2, 3):
            before = f"private/oracle/{variant}/{index}/before.png"
            after = f"private/oracle/{variant}/{index}/after.png"
            _touch(root, before)
            _touch(root, after)
            _seed(
                root,
                ArtifactReference(
                    f"luanti_14555_rendered_observation/{variant}"
                    f"/repetition-{index}/v2",
                    "luanti_14555_rendered_observation",
                    "2.0",
                ),
                {
                    "passed": True,
                    "complete": True,
                    "before_frame_path": before,
                    "after_frame_path": after,
                    "input_receipt": {
                        "status": "success",
                        "input_backend": "luanti_irrlicht_sevent_v1",
                    },
                },
            )
    observations = {
        "fixed": "goal_fixed_placement_present",
        "buggy": buggy_semantics,
    }
    for variant, semantics in observations.items():
        before = f"private/downstream-runs/14555/{variant}/before.png"
        after = f"private/downstream-runs/14555/{variant}/after.png"
        _touch(root, before)
        _touch(root, after)
        _seed(
            root,
            target._14555_ATTEMPTS[variant],
            {
                "candidate": target._14555,
                "variant": variant,
                "state": "passed",
                "synthetic_frames": False,
                "candidate_execution_performed": True,
                "semantic_observation": semantics,
                "before_frame": before,
                "after_frame": after,
            },
        )


def test_14757_gate8_is_recomputed_from_lineage_and_real_frames(tmp_path: Path) -> None:
    _fixture_14757(tmp_path)

    artifact = target.write_luanti_14757_gate8(
        tmp_path, authorization=_authorization()
    )

    assert artifact.payload["state"] == "passed"
    assert artifact.payload["role"] == "version_materialization_and_runtime"
    assert artifact.payload["materialization"][
        "binary_identity_matches_formal_build"
    ] is True
    assert artifact.payload["runtime"]["client_launched"] is True
    assert set(artifact.payload["runtime"]["engine_frames"]) == set(target._FRAME_KEYS)
    assert artifact.payload["gate_1_7_preserved"] is True
    assert len(artifact.inputs) == 2


def test_14757_gate8_fails_closed_on_a_missing_frame(tmp_path: Path) -> None:
    _fixture_14757(tmp_path)
    (tmp_path / "private/bounded-runs/luanti/14757/f12/fixed_after.png").unlink()

    with pytest.raises(RuntimeError, match="required file is missing or unsafe"):
        target.write_luanti_14757_gate8(tmp_path, authorization=_authorization())


def test_14555_gate10_is_recomputed_from_the_registered_replay(tmp_path: Path) -> None:
    _fixture_14555(tmp_path)

    artifact = target.write_luanti_14555_gate10(
        tmp_path, authorization=_authorization()
    )

    assert artifact.payload["state"] == "passed"
    assert artifact.payload["role"] == "hidden_semantic_oracle"
    assert artifact.payload["hidden_until_after_candidate_execution"] is True
    assert artifact.payload["expected_after"] == artifact.payload["observed_after"]
    assert artifact.payload["repetitions_per_variant"] == 3
    assert len(artifact.payload["repetition_sources"]["fixed"]) == 3
    assert set(artifact.payload["retained_gate11_frame_fingerprints"]) == {
        "fixed",
        "buggy",
    }
    assert len(artifact.inputs) == 3


def test_14555_gate10_rejects_a_semantic_mismatch(tmp_path: Path) -> None:
    _fixture_14555(tmp_path, buggy_semantics="goal_fixed_placement_present")

    with pytest.raises(RuntimeError, match="hidden oracle mismatch"):
        target.write_luanti_14555_gate10(tmp_path, authorization=_authorization())


def test_authorization_scope_is_enforced(tmp_path: Path) -> None:
    _fixture_14555(tmp_path)
    with pytest.raises(PermissionError):
        target.write_luanti_14555_gate10(
            tmp_path, authorization=_authorization("infrastructure-audit")
        )
