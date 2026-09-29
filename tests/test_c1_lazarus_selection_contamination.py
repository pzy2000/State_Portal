from __future__ import annotations

import os
from pathlib import Path

from warptest.c1_lazarus_selection.contamination import write_contamination_record
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    write_stable_artifact,
)


def test_contamination_record_reports_dates_urls_searchability_and_overlap(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    authorization = AuthorizationRecord(
        authorization_id="local-report/run-1",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )
    write_stable_artifact(
        root,
        artifact_id="candidate_source_packet/luanti/10/source/v1",
        artifact_kind="candidate_source_packet",
        semantic_version="1.0",
        created_at="2026-08-13T00:01:00Z",
        inputs=(),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 10},
            "issue_created_at": "2026-07-01T00:00:00Z",
            "change_created_at": "2026-07-02T00:00:00Z",
            "issue_url": "https://example.invalid/issues/9",
            "change_url": "https://example.invalid/pulls/10",
            "public_text": "Opening the inventory twice loses the selected stack.",
            "complete": True,
        },
        authorization=authorization,
    )
    write_stable_artifact(
        root,
        artifact_id="public_task/luanti/10/current/v1",
        artifact_kind="public_task",
        semantic_version="1.0",
        created_at="2026-08-13T00:02:00Z",
        inputs=(),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 10},
            "task_text": "Opening inventory twice can lose the selected stack.",
            "complete": True,
        },
        authorization=authorization,
    )

    record = write_contamination_record(
        root,
        repository_key="luanti",
        change_number=10,
        exact_text_readily_searchable=True,
        authorization=authorization,
    )

    assert record.payload["issue_created_at"] == "2026-07-01T00:00:00Z"
    assert record.payload["change_url"] == "https://example.invalid/pulls/10"
    assert record.payload["exact_text_readily_searchable"] is True
    assert 0.0 < record.payload["source_text_overlap"] <= 1.0
    assert record.payload["risk_statement"] == "public_training_exposure_possible"
