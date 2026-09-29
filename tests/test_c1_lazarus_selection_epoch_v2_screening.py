from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
import os
from collections import Counter
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.audit_labels import write_old24_audit_labels
from warptest.c1_lazarus_selection.epoch import SELECTION_EPOCH_V2
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.operations import evaluate_test_manifest
from warptest.c1_lazarus_selection.patches import classify_unified_patch
from warptest.c1_lazarus_selection.screening import (
    PatchDecision,
    SourceDecision,
    run_screening_batch,
)
from warptest.c1_lazarus_selection.source_atomicity import (
    load_source_atomicity_batch_context,
    write_source_atomicity_artifact,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


def test_v2_screening_ignores_v1_checkpoint_and_binds_v2_enumeration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    authorization = _authorization()
    _write_enumeration(root, authorization, candidates=(1, 2, 3))
    for number in (2, 3):
        _write_raw_source_manifest(root, CandidateKey("luanti", number))
    write_stable_artifact(
        root,
        artifact_id="screening_checkpoint/luanti/1/current/v1",
        artifact_kind="screening_checkpoint",
        semantic_version="1.0",
        created_at="2026-08-13T00:00:01Z",
        inputs=(),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 1},
            "state": "complete",
            "outcome": "patch_eligible",
        },
        authorization=authorization,
    )
    calls: Counter[tuple[str, int]] = Counter()

    def source(candidate):
        calls["source", candidate.change_number] += 1
        if candidate.change_number == 1:
            return SourceDecision.rejected("authority_missing")
        return SourceDecision.accepted(
            raw_evidence_path=f"raw/luanti/{candidate.change_number}.json"
        )

    def patch(candidate, _source):
        calls["patch", candidate.change_number] += 1
        if candidate.change_number == 2:
            return PatchDecision.rejected("no_regression_test")
        return PatchDecision.eligible(
            production_paths=("src/main.cpp",),
            test_paths=("tests/main.cpp",),
        )

    result = run_screening_batch(
        root,
        repository_key="luanti",
        source_capture=source,
        patch_capture=patch,
        authorization=authorization,
        selection_epoch=SELECTION_EPOCH_V2,
        source_workers=1,
        patch_workers=1,
        persistence_workers=1,
        reload_workers=1,
        queue_limit=3,
    )

    assert result.scheduled_candidates == 3
    assert calls == Counter(
        {
            ("source", 1): 1,
            ("source", 2): 1,
            ("source", 3): 1,
            ("patch", 2): 1,
            ("patch", 3): 1,
        }
    )
    enumeration = ArtifactReference(
        "repository_enumeration/luanti/current/v2",
        "repository_enumeration",
        "2.0",
    )
    source_terminal = _load_v2(
        root,
        "candidate_source_terminal/luanti/1/source/v2",
        "candidate_source_terminal",
    )
    source_packet = _load_v2(
        root,
        "candidate_source_packet/luanti/2/source/v2",
        "candidate_source_packet",
    )
    patch_terminal = _load_v2(
        root,
        "candidate_patch_terminal/luanti/2/patch/v2",
        "candidate_patch_terminal",
    )
    test_patch = _load_v2(
        root,
        "candidate_test_patch/luanti/3/screening/v2",
        "candidate_test_patch",
    )
    checkpoint = _load_v2(
        root,
        "screening_checkpoint/luanti/3/current/v2",
        "screening_checkpoint",
    )

    assert source_terminal.inputs == (enumeration,)
    assert source_packet.inputs == (enumeration,)
    assert patch_terminal.inputs[0] == enumeration
    assert test_patch.inputs[0] == enumeration
    assert test_patch.payload["source_manifest_path"] == "raw/luanti/3.json"
    assert test_patch.payload["raw_patch_path"] == "raw/luanti/3.diff"
    assert checkpoint.inputs[0] == enumeration
    assert (
        _load_v2(
            root,
            "screening_checkpoint/luanti/1/current/v2",
            "screening_checkpoint",
        ).payload["outcome"]
        == "source_rejected"
    )
    resumed = run_screening_batch(
        root,
        repository_key="luanti",
        source_capture=lambda _candidate: (_ for _ in ()).throw(
            AssertionError("valid v2 checkpoint was recaptured")
        ),
        patch_capture=lambda _candidate, _source: (_ for _ in ()).throw(
            AssertionError("valid v2 checkpoint was repatched")
        ),
        authorization=authorization,
        selection_epoch=SELECTION_EPOCH_V2,
        source_workers=1,
        patch_workers=1,
        persistence_workers=1,
        reload_workers=1,
        queue_limit=3,
    )
    assert resumed.scheduled_candidates == 0
    assert resumed.resumed_candidates == 3


def test_v2_patch_binds_the_source_declared_raw_patch_for_manifest_gate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    authorization = _authorization()
    candidate = CandidateKey("luanti", 4)
    _write_enumeration(root, authorization, candidates=(candidate.change_number,))
    _write_raw_source_manifest(root, candidate)

    result = run_screening_batch(
        root,
        repository_key=candidate.repository_key,
        source_capture=lambda _candidate: SourceDecision.accepted(
            raw_evidence_path="raw/luanti/4.json"
        ),
        patch_capture=lambda _candidate, source: classify_unified_patch(
            (root / source.raw_evidence_path).with_name("4.diff").read_bytes()
        ),
        authorization=authorization,
        selection_epoch=SELECTION_EPOCH_V2,
        source_workers=1,
        patch_workers=1,
        persistence_workers=1,
        reload_workers=1,
        queue_limit=1,
    )

    assert result.patch_eligible == 1
    patch = _load_v2(
        root,
        "candidate_test_patch/luanti/4/screening/v2",
        "candidate_test_patch",
    )
    assert patch.payload["source_manifest_path"] == "raw/luanti/4.json"
    assert patch.payload["raw_patch_path"] == "raw/luanti/4.diff"
    # The manifest gate reads the accepted source-atomicity decision, so replay it
    # from the same raw evidence the screening batch just bound.
    write_source_atomicity_artifact(
        batch_context=load_source_atomicity_batch_context(
            root, repository_key=candidate.repository_key
        ),
        candidate=candidate,
        created_at=_after_now(),
        authorization=authorization,
    )
    assert (
        evaluate_test_manifest(
            root, candidate, selection_epoch=SELECTION_EPOCH_V2
        ).state
        == "passed"
    )

    raw_patch = root / "raw" / "luanti" / "4.diff"
    raw_patch.unlink()
    with pytest.raises(RuntimeError, match="source patch file is missing"):
        evaluate_test_manifest(root, candidate, selection_epoch=SELECTION_EPOCH_V2)

    outside = tmp_path / "outside.diff"
    outside.write_bytes(b"not a source patch")
    raw_patch.symlink_to(outside)
    with pytest.raises(RuntimeError, match="source patch path contains a symlink"):
        evaluate_test_manifest(root, candidate, selection_epoch=SELECTION_EPOCH_V2)


def test_v2_resume_rejects_checkpoint_without_v2_enumeration_input(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    authorization = _authorization()
    _write_enumeration(root, authorization, candidates=(1,))
    write_stable_artifact(
        root,
        artifact_id="screening_checkpoint/luanti/1/current/v2",
        artifact_kind="screening_checkpoint",
        semantic_version="2.0",
        created_at="2026-08-13T00:00:01Z",
        inputs=(),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 1},
            "state": "complete",
            "outcome": "source_rejected",
            "stage_artifact_id": "candidate_source_terminal/luanti/1/source/v2",
        },
        authorization=authorization,
    )

    with pytest.raises(RuntimeError, match="checkpoint enumeration input"):
        _resume_v2(root, authorization)


def test_v2_resume_rejects_source_terminal_without_enumeration_prerequisite(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    authorization = _authorization()
    enumeration = _write_enumeration(root, authorization, candidates=(1,))
    source_terminal = ArtifactReference(
        "candidate_source_terminal/luanti/1/source/v2",
        "candidate_source_terminal",
        "2.0",
    )
    _write_v2_artifact(
        root,
        authorization,
        reference=source_terminal,
        inputs=(),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 1},
            "outcome": "source_rejected",
            "reason": "authority_missing",
            "complete": True,
        },
    )
    _write_v2_artifact(
        root,
        authorization,
        reference=ArtifactReference(
            "screening_checkpoint/luanti/1/current/v2",
            "screening_checkpoint",
            "2.0",
        ),
        inputs=(enumeration, source_terminal),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 1},
            "state": "complete",
            "outcome": "source_rejected",
            "stage_artifact_id": source_terminal.artifact_id,
        },
    )

    with pytest.raises(RuntimeError, match="source terminal enumeration input"):
        _resume_v2(root, authorization)


def test_v2_resume_rejects_stage_candidate_and_patch_source_prerequisite_drift(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    authorization = _authorization()
    enumeration = _write_enumeration(root, authorization, candidates=(1,))
    wrong_stage = ArtifactReference(
        "candidate_source_terminal/luanti/2/source/v2",
        "candidate_source_terminal",
        "2.0",
    )
    _write_v2_artifact(
        root,
        authorization,
        reference=wrong_stage,
        inputs=(enumeration,),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 2},
            "outcome": "source_rejected",
            "reason": "authority_missing",
            "complete": True,
        },
    )
    _write_v2_artifact(
        root,
        authorization,
        reference=ArtifactReference(
            "screening_checkpoint/luanti/1/current/v2",
            "screening_checkpoint",
            "2.0",
        ),
        inputs=(enumeration, wrong_stage),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 1},
            "state": "complete",
            "outcome": "source_rejected",
            "stage_artifact_id": wrong_stage.artifact_id,
        },
    )

    with pytest.raises(RuntimeError, match="checkpoint stage reference"):
        _resume_v2(root, authorization)

    source = ArtifactReference(
        "candidate_source_packet/luanti/1/source/v2",
        "candidate_source_packet",
        "2.0",
    )
    patch = ArtifactReference(
        "candidate_patch_terminal/luanti/1/patch/v2",
        "candidate_patch_terminal",
        "2.0",
    )
    _write_v2_artifact(
        root,
        authorization,
        reference=source,
        inputs=(),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 1},
            "raw_evidence_path": "raw/luanti/1.json",
            "complete": True,
        },
    )
    _write_v2_artifact(
        root,
        authorization,
        reference=patch,
        inputs=(enumeration, source),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 1},
            "outcome": "patch_rejected",
            "reason": "no_regression_test",
            "complete": True,
        },
    )
    _write_v2_artifact(
        root,
        authorization,
        reference=ArtifactReference(
            "screening_checkpoint/luanti/1/current/v2",
            "screening_checkpoint",
            "2.0",
        ),
        inputs=(enumeration, patch),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 1},
            "state": "complete",
            "outcome": "patch_rejected",
            "stage_artifact_id": patch.artifact_id,
        },
        created_at="2026-08-13T00:00:02Z",
    )

    with pytest.raises(RuntimeError, match="source packet enumeration input"):
        _resume_v2(root, authorization)


def test_v2_old24_audit_never_resolves_v1_terminal_or_checkpoint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    authorization = _authorization()
    _write_enumeration(
        root,
        authorization,
        candidates=(2,),
        missing_issue_links=(1,),
    )
    write_stable_artifact(
        root,
        artifact_id="enumeration_terminal/luanti/1/issue-link/v1",
        artifact_kind="enumeration_terminal",
        semantic_version="1.0",
        created_at="2026-08-13T00:00:01Z",
        inputs=(),
        payload={"complete": True, "reason": "authoritative_issue_link_missing"},
        authorization=authorization,
    )
    write_stable_artifact(
        root,
        artifact_id="screening_checkpoint/luanti/2/current/v1",
        artifact_kind="screening_checkpoint",
        semantic_version="1.0",
        created_at="2026-08-13T00:00:02Z",
        inputs=(),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 2},
            "state": "complete",
            "outcome": "patch_eligible",
        },
        authorization=authorization,
    )
    source = tmp_path / "old24-v2.yaml"
    source.write_text(
        """candidates:
  - task_id: old-luanti-1
    benchmark: luanti
    pr_number: 1
  - task_id: old-luanti-2
    benchmark: luanti
    pr_number: 2
""",
        encoding="utf-8",
    )

    result = write_old24_audit_labels(
        root,
        source_path=source,
        authorization=authorization,
        expected_count=2,
        selection_epoch=SELECTION_EPOCH_V2,
    )

    assert result.mapped == 2
    assert result.terminal == 0
    for label in ("old-luanti-1", "old-luanti-2"):
        audit = _load_v2(root, f"old24_audit/{label}/current/v2", "old24_audit")
        assert audit.payload["screening_state"] == "pending"
        assert audit.inputs == (
            ArtifactReference(
                "repository_enumeration/luanti/current/v2",
                "repository_enumeration",
                "2.0",
            ),
        )


def test_v1_old24_audit_preserves_missing_terminal_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    authorization = _authorization()
    write_stable_artifact(
        root,
        artifact_id="repository_enumeration/luanti/current/v1",
        artifact_kind="repository_enumeration",
        semantic_version="1.0",
        created_at="2026-08-13T00:00:00Z",
        inputs=(),
        payload={
            "repository_key": "luanti",
            "merged_change_numbers": [1],
            "authoritative_issue_link_missing": [1],
            "repair_not_in_pinned": [],
            "candidates": [],
            "complete": True,
        },
        authorization=authorization,
    )
    source = tmp_path / "old24-v1-missing-terminal.yaml"
    source.write_text(
        """candidates:
  - task_id: old-luanti-1
    benchmark: luanti
    pr_number: 1
""",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="artifact directory is missing"):
        write_old24_audit_labels(
            root,
            source_path=source,
            authorization=authorization,
            expected_count=1,
        )


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-epoch-v2/test",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _after_now() -> str:
    """The screening batch stamps its artifacts with the real clock."""

    moment = datetime.now(UTC) + timedelta(seconds=1)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write_raw_source_manifest(root: Path, candidate: CandidateKey) -> None:
    """Create the same durable manifest/patch shape used by source providers."""

    source_path = (
        root / "raw" / candidate.repository_key / f"{candidate.change_number}.json"
    )
    patch_path = (
        root / "raw" / candidate.repository_key / f"{candidate.change_number}.diff"
    )
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_bytes(
        b"""\\
diff --git a/src/main.cpp b/src/main.cpp
--- a/src/main.cpp
+++ b/src/main.cpp
@@ -1 +1 @@
-return 0;
+return 1;
diff --git a/tests/main.cpp b/tests/main.cpp
--- a/tests/main.cpp
+++ b/tests/main.cpp
@@ -1 +1 @@
-assert(false);
+assert(true);
"""
    )
    repository_name = {
        "jupyterlab": "jupyterlab/jupyterlab",
        "luanti": "luanti-org/luanti",
        "openra": "OpenRA/OpenRA",
    }[candidate.repository_key]
    issue_number = candidate.change_number - 1
    source_path.write_text(
        json.dumps(
            {
                "candidate": candidate.to_value(),
                "provider": "github",
                "authority": {
                    "provider": "github",
                    "api_base": "https://api.github.com",
                    "repository": repository_name,
                },
                "pull": {
                    "number": candidate.change_number,
                    "merged": True,
                    "merged_at": "2026-08-01T00:00:00Z",
                    "title": "Repair the defect",
                    "body": "The authoritative relationship is frozen below.",
                },
                "relationships": [
                    {
                        "event": "cross-referenced",
                        "created_at": "2026-07-02T00:00:00Z",
                        "source": {
                            "issue": {
                                "number": issue_number,
                                "repository_url": (
                                    f"https://api.github.com/repos/{repository_name}"
                                ),
                                "created_at": "2026-07-01T00:00:00Z",
                            }
                        },
                    }
                ],
                "issue_numbers": [issue_number],
                "issues": [
                    {
                        "number": issue_number,
                        "issue": {
                            "number": issue_number,
                            "title": f"Issue {issue_number}",
                            "body": "Public defect report.",
                            "created_at": "2026-07-01T00:00:00Z",
                        },
                        "comments": [],
                        "history": [],
                    }
                ],
                "raw_paths": [patch_path.relative_to(root).as_posix()],
                "request_receipt_paths": [],
                "patch_raw_path": patch_path.relative_to(root).as_posix(),
                "attachment_paths": [],
                "complete": True,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _write_enumeration(
    root: Path,
    authorization: AuthorizationRecord,
    *,
    candidates: tuple[int, ...],
    missing_issue_links: tuple[int, ...] = (),
) -> ArtifactReference:
    merged = tuple(sorted((*candidates, *missing_issue_links)))
    reference = ArtifactReference(
        "repository_enumeration/luanti/current/v2",
        "repository_enumeration",
        "2.0",
    )
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at="2026-08-13T00:00:00Z",
        inputs=(),
        payload={
            "repository_key": "luanti",
            "merged_change_numbers": list(merged),
            "authoritative_issue_link_missing": list(missing_issue_links),
            "repair_not_in_pinned": [],
            "candidates": [
                {"repository_key": "luanti", "change_number": number}
                for number in candidates
            ],
            "infrastructure_blocked": [],
            "complete": True,
        },
        authorization=authorization,
    )
    return reference


def _write_v2_artifact(
    root: Path,
    authorization: AuthorizationRecord,
    *,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    created_at: str = "2026-08-13T00:00:01Z",
) -> None:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=created_at,
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )


def _resume_v2(root: Path, authorization: AuthorizationRecord):
    return run_screening_batch(
        root,
        repository_key="luanti",
        source_capture=lambda _candidate: (_ for _ in ()).throw(
            AssertionError("invalid v2 checkpoint was recaptured")
        ),
        patch_capture=lambda _candidate, _source: (_ for _ in ()).throw(
            AssertionError("invalid v2 checkpoint was repatched")
        ),
        authorization=authorization,
        selection_epoch=SELECTION_EPOCH_V2,
        source_workers=1,
        patch_workers=1,
        persistence_workers=1,
        reload_workers=1,
        queue_limit=1,
    )


def _load_v2(root: Path, artifact_id: str, artifact_kind: str):
    return load_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        semantic_version="2.0",
    )
