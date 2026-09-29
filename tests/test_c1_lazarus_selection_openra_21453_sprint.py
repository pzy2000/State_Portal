from __future__ import annotations

import os
from pathlib import Path

from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.openra_21453_sprint import (
    audit_openra_21453_sprint_admission,
    load_openra_21453_sprint_terminal,
    write_openra_21453_sprint_terminal,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("openra", 21453)
_ENUMERATION = ArtifactReference(
    "repository_enumeration/openra/current/v2",
    "repository_enumeration",
    "2.0",
)
_AUDIT = ArtifactReference(
    "enumeration_audit/openra/21453/current/v2",
    "enumeration_audit",
    "2.0",
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/openra-21453-sprint",
        scope="candidate-screening",
        granted_at="2026-08-13T12:00:00Z",
        local_user_id=os.getuid(),
    )


def _write_current_enumeration(root: Path) -> None:
    authorization = _authorization()
    write_stable_artifact(
        root,
        artifact_id=_ENUMERATION.artifact_id,
        artifact_kind=_ENUMERATION.artifact_kind,
        semantic_version=_ENUMERATION.semantic_version,
        created_at="2026-08-13T11:59:58Z",
        inputs=(),
        payload={
            "repository_key": "openra",
            "merged_change_numbers": [21453],
            "authoritative_issue_link_missing": [],
            "repair_not_in_pinned": [],
            "candidates": [_CANDIDATE.to_value()],
            "infrastructure_blocked": [],
            "complete": True,
        },
        authorization=authorization,
    )
    write_stable_artifact(
        root,
        artifact_id=_AUDIT.artifact_id,
        artifact_kind=_AUDIT.artifact_kind,
        semantic_version=_AUDIT.semantic_version,
        created_at="2026-08-13T11:59:59Z",
        inputs=(_ENUMERATION,),
        payload={"candidate": _CANDIDATE.to_value(), "complete": True},
        authorization=authorization,
    )


def _write_production_only_patch(root: Path) -> Path:
    path = root / "pr-21453.diff"
    path.write_text(
        """diff --git a/OpenRA.Mods.Common/Widgets/ProductionPaletteWidget.cs b/OpenRA.Mods.Common/Widgets/ProductionPaletteWidget.cs
--- a/OpenRA.Mods.Common/Widgets/ProductionPaletteWidget.cs
+++ b/OpenRA.Mods.Common/Widgets/ProductionPaletteWidget.cs
@@ -1 +1 @@
-return oldItem;
+return currentItem;
""",
        encoding="utf-8",
    )
    return path


def test_openra_21453_sprint_fails_closed_before_legacy_construction(
    tmp_path: Path,
) -> None:
    _write_current_enumeration(tmp_path)
    patch = _write_production_only_patch(tmp_path)

    result = audit_openra_21453_sprint_admission(tmp_path, patch_path=patch)

    assert result.candidate == _CANDIDATE
    assert result.current_enumeration_candidate is True
    assert result.current_source_state == "missing"
    assert result.current_patch_state == "rejected"
    assert result.current_patch_reason == "no_regression_test"
    assert result.admissible is False
    assert result.legacy_conclusions_reused is False
    assert result.blockers == (
        "current_authoritative_source_closure_missing",
        "current_patch_has_no_regression_test",
    )


def test_openra_21453_sprint_terminal_round_trips_without_hash_fields(
    tmp_path: Path,
) -> None:
    _write_current_enumeration(tmp_path)
    patch = _write_production_only_patch(tmp_path)

    written = write_openra_21453_sprint_terminal(
        tmp_path,
        patch_path=patch,
        authorization=_authorization(),
    )
    loaded = load_openra_21453_sprint_terminal(tmp_path, patch_path=patch)

    assert loaded == written
    assert loaded.artifact.inputs == (_ENUMERATION, _AUDIT)
    assert loaded.artifact.payload["admissible"] is False
    forbidden = ("sha256", "sha-256", "artifact_sha", "implementation_sha", "hmac")
    serialized = loaded.artifact.revision_path.read_text(encoding="utf-8").casefold()
    assert not any(token in serialized for token in forbidden)


def test_openra_21453_sprint_rejects_non_current_candidate(tmp_path: Path) -> None:
    authorization = _authorization()
    write_stable_artifact(
        tmp_path,
        artifact_id=_ENUMERATION.artifact_id,
        artifact_kind=_ENUMERATION.artifact_kind,
        semantic_version=_ENUMERATION.semantic_version,
        created_at="2026-08-13T11:59:58Z",
        inputs=(),
        payload={
            "repository_key": "openra",
            "merged_change_numbers": [21453],
            "authoritative_issue_link_missing": [],
            "repair_not_in_pinned": [21453],
            "candidates": [],
            "infrastructure_blocked": [],
            "complete": True,
        },
        authorization=authorization,
    )
    patch = _write_production_only_patch(tmp_path)

    try:
        audit_openra_21453_sprint_admission(tmp_path, patch_path=patch)
    except RuntimeError as error:
        assert str(error) == "OpenRA 21453 is not a current v2 candidate"
    else:
        raise AssertionError("non-current candidate was admitted to the sprint audit")
