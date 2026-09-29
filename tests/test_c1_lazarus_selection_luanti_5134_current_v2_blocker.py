from __future__ import annotations

import os

import pytest

from warptest.c1_lazarus_selection.luanti_5134_current_v2_blocker import (
    _ATOMICITY,
    _CHAIN,
    _ENUMERATION,
    _MANIFEST,
    _PATCH,
    _PREANNOTATION_CHECKPOINT,
    _SCREENING_CHECKPOINT,
    _SOURCE,
    write_luanti_5134_current_v2_blocker,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 5134}
_SOURCE_RAW = "raw/github/luanti/5134/source-manifest.json"
_PATCH_RAW = "raw/github/luanti/5134/pull.diff"


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        "local-infrastructure-audit/luanti-5134-test",
        "infrastructure-audit",
        "2026-08-14T00:00:00Z",
        os.getuid(),
    )


def _write_chain(tmp_path):
    root = tmp_path / "artifact-root"
    root.mkdir()
    auth = _authorization()
    source_path = root / _SOURCE_RAW
    patch_path = root / _PATCH_RAW
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        """
        {
          "authority": {"repository": "luanti-org/luanti"},
          "candidate": {"repository_key": "luanti", "change_number": 5134},
          "complete": true,
          "issue_numbers": [3786, 4911],
          "provider": "github",
          "pull": {"merged": true, "number": 5134}
        }
        """,
        encoding="utf-8",
    )
    patch_path.write_text(
        """diff --git a/src/threading/mutex.cpp b/src/threading/mutex.cpp
try_lock USE_WIN_MUTEX pthread_mutex_trylock
diff --git a/src/threading/mutex.h b/src/threading/mutex.h
try_lock
diff --git a/src/threading/thread.cpp b/src/threading/thread.cpp
m_start_finished_mutex
diff --git a/src/threading/thread.h b/src/threading/thread.h
m_start_finished_mutex
diff --git a/src/unittest/test_threading.cpp b/src/unittest/test_threading.cpp
TEST(testStartStopWait)
""",
        encoding="utf-8",
    )

    write_stable_artifact(
        root,
        artifact_id=_ENUMERATION.artifact_id,
        artifact_kind=_ENUMERATION.artifact_kind,
        semantic_version=_ENUMERATION.semantic_version,
        created_at="2026-08-14T00:00:01Z",
        inputs=(),
        payload={"repository_key": "luanti", "complete": True},
        authorization=auth,
    )
    write_stable_artifact(
        root,
        artifact_id=_SOURCE.artifact_id,
        artifact_kind=_SOURCE.artifact_kind,
        semantic_version=_SOURCE.semantic_version,
        created_at="2026-08-14T00:00:02Z",
        inputs=(_ENUMERATION,),
        payload={
            "candidate": _CANDIDATE,
            "complete": True,
            "raw_evidence_path": _SOURCE_RAW,
        },
        authorization=auth,
    )
    write_stable_artifact(
        root,
        artifact_id=_PATCH.artifact_id,
        artifact_kind=_PATCH.artifact_kind,
        semantic_version=_PATCH.semantic_version,
        created_at="2026-08-14T00:00:03Z",
        inputs=(_ENUMERATION, _SOURCE),
        payload={
            "candidate": _CANDIDATE,
            "complete": True,
            "outcome": "patch_eligible",
            "production_paths": [
                "src/threading/mutex.cpp",
                "src/threading/mutex.h",
                "src/threading/thread.cpp",
                "src/threading/thread.h",
            ],
            "raw_patch_path": _PATCH_RAW,
            "source_manifest_path": _SOURCE_RAW,
            "test_paths": ["src/unittest/test_threading.cpp"],
        },
        authorization=auth,
    )
    write_stable_artifact(
        root,
        artifact_id=_SCREENING_CHECKPOINT.artifact_id,
        artifact_kind=_SCREENING_CHECKPOINT.artifact_kind,
        semantic_version=_SCREENING_CHECKPOINT.semantic_version,
        created_at="2026-08-14T00:00:04Z",
        inputs=(_ENUMERATION, _PATCH),
        payload={
            "candidate": _CANDIDATE,
            "outcome": "patch_eligible",
            "stage_artifact_id": _PATCH.artifact_id,
            "state": "complete",
        },
        authorization=auth,
    )
    write_stable_artifact(
        root,
        artifact_id=_ATOMICITY.artifact_id,
        artifact_kind=_ATOMICITY.artifact_kind,
        semantic_version=_ATOMICITY.semantic_version,
        created_at="2026-08-14T00:00:05Z",
        inputs=(_ENUMERATION, _SOURCE, _PATCH, _SCREENING_CHECKPOINT),
        payload={
            "candidate": _CANDIDATE,
            "complete": True,
            "authoritative_issue_numbers": [3786, 4911],
            "linked_issue_numbers": [3786, 4911],
            "outcome": "rejected",
            "reason": "multiple_authoritative_issues",
            "source_manifest_path": _SOURCE_RAW,
        },
        authorization=auth,
    )
    write_stable_artifact(
        root,
        artifact_id=_MANIFEST.artifact_id,
        artifact_kind=_MANIFEST.artifact_kind,
        semantic_version=_MANIFEST.semantic_version,
        created_at="2026-08-14T00:00:06Z",
        inputs=(_PATCH,),
        payload={
            "candidate": _CANDIDATE,
            "complete": True,
            "source_manifest_path": _SOURCE_RAW,
            "state": "passed",
        },
        authorization=auth,
    )
    write_stable_artifact(
        root,
        artifact_id=_PREANNOTATION_CHECKPOINT.artifact_id,
        artifact_kind=_PREANNOTATION_CHECKPOINT.artifact_kind,
        semantic_version=_PREANNOTATION_CHECKPOINT.semantic_version,
        created_at="2026-08-14T00:00:07Z",
        inputs=(_MANIFEST,),
        payload={
            "candidate": _CANDIDATE,
            "current_artifact_id": _MANIFEST.artifact_id,
            "state": "active",
        },
        authorization=auth,
    )
    return root


def test_current_v2_blocker_is_nonterminal_and_idempotent(tmp_path):
    root = _write_chain(tmp_path)
    artifact = write_luanti_5134_current_v2_blocker(
        root, authorization=_authorization()
    )

    assert artifact.inputs == _CHAIN
    assert artifact.payload["state"] == "blocked"
    assert artifact.payload["terminal"] is False
    assert artifact.payload["resumable"] is True
    assert artifact.payload["candidate_eligibility"] == (
        "not_admitted_pending_single_defect_mapping"
    )
    assert artifact.payload["source_atomicity"]["authoritative_issue_numbers"] == [
        3786,
        4911,
    ]
    assert artifact.payload["current_gate_status"]["gate_5_fixed_f2p_3x"] == "not_run"
    assert artifact.payload["exact_closure"]["closure_admissible"] is False
    assert artifact.payload["candidate_execution_performed"] is False
    assert artifact.payload["old_attempts_loaded"] is False
    assert artifact.payload["resource_policy"]["paid"] is False

    same = write_luanti_5134_current_v2_blocker(root, authorization=_authorization())
    assert same.revision_path == artifact.revision_path


def test_current_v2_blocker_refuses_changed_atomicity_state(tmp_path):
    root = _write_chain(tmp_path)
    auth = _authorization()
    write_stable_artifact(
        root,
        artifact_id=_ATOMICITY.artifact_id,
        artifact_kind=_ATOMICITY.artifact_kind,
        semantic_version=_ATOMICITY.semantic_version,
        created_at="2026-08-14T00:00:08Z",
        inputs=(_ENUMERATION, _SOURCE, _PATCH, _SCREENING_CHECKPOINT),
        payload={
            "candidate": _CANDIDATE,
            "complete": True,
            "authoritative_issue_numbers": [3786],
            "linked_issue_numbers": [3786],
            "outcome": "accepted",
            "reason": "single_authoritative_issue",
            "source_manifest_path": _SOURCE_RAW,
        },
        authorization=auth,
    )
    with pytest.raises(RuntimeError, match="state changed"):
        write_luanti_5134_current_v2_blocker(root, authorization=auth)
