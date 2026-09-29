from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection import source_atomicity as source_atomicity_module
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.source_atomicity import (
    SourceAtomicityBatchContext,
    load_source_atomicity_artifact,
    load_source_atomicity_batch_context,
    write_source_atomicity_artifact,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    write_stable_artifact,
)


def test_single_authoritative_issue_is_mechanically_selected_and_round_trips(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 17701)
    authorization = _authorization()
    expected_inputs, _manifest_path = _write_v2_source_chain(
        root,
        candidate=candidate,
        authorization=authorization,
        issue_numbers=(12863,),
    )
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )

    written = write_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
        created_at="2026-08-13T00:00:04Z",
        authorization=authorization,
    )
    loaded = load_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
    )

    assert written == loaded
    assert loaded.artifact.reference == ArtifactReference(
        "source_atomicity/jupyterlab/17701/preannotation/v2",
        "source_atomicity",
        "2.0",
    )
    assert loaded.artifact.inputs == expected_inputs
    assert loaded.outcome == "accepted"
    assert loaded.reason is None
    assert loaded.authoritative_issue_numbers == (12863,)
    assert loaded.primary_issue_number == 12863


def test_luanti_legacy_issue_repository_alias_is_current_provider_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("luanti", 12984)
    authorization = _authorization()
    _references, manifest_path = _write_v2_source_chain(
        root,
        candidate=candidate,
        authorization=authorization,
        issue_numbers=(12870,),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pull"]["body"] = (
        "Fixes https://github.com/minetest/minetest/issues/12870."
    )
    manifest["relationships"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )

    receipt = write_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
        created_at="2026-08-13T00:00:04Z",
        authorization=authorization,
    )

    assert receipt.outcome == "accepted"
    assert receipt.primary_issue_number == 12870


def test_explicit_closing_issue_wins_over_retained_late_cross_reference(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 11352)
    authorization = _authorization()
    _references, manifest_path = _write_v2_source_chain(
        root,
        candidate=candidate,
        authorization=authorization,
        issue_numbers=(11351, 11834),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pull"]["body"] = "Fixes #11351."
    manifest["relationships"][1]["created_at"] = "2022-03-01T00:00:00Z"
    manifest["relationships"][1]["source"]["issue"]["created_at"] = (
        "2022-03-01T00:00:00Z"
    )
    manifest["issues"][1]["issue"]["created_at"] = "2022-03-01T00:00:00Z"
    manifest["pull"]["merged_at"] = "2021-10-01T00:00:00Z"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )

    receipt = write_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
        created_at="2026-08-13T00:00:04Z",
        authorization=authorization,
    )

    assert receipt.outcome == "accepted"
    assert receipt.linked_issue_numbers == (11351, 11834)
    assert receipt.authoritative_issue_numbers == (11351,)
    assert receipt.primary_issue_number == 11351


@pytest.mark.parametrize(
    ("pull_body", "issue_numbers", "expected_outcome", "expected_primary"),
    (
        ("Fixes #7 and #8.", (7, 8), "rejected", None),
        ("No closing clause.", (7,), "accepted", 7),
        ("No closing clause.", (7, 8), "rejected", None),
    ),
)
def test_explicit_and_crossref_atomicity_selection(
    tmp_path: Path,
    pull_body: str,
    issue_numbers: tuple[int, ...],
    expected_outcome: str,
    expected_primary: int | None,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 17701)
    authorization = _authorization()
    _references, manifest_path = _write_v2_source_chain(
        root,
        candidate=candidate,
        authorization=authorization,
        issue_numbers=issue_numbers,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pull"]["body"] = pull_body
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )

    receipt = write_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
        created_at="2026-08-13T00:00:04Z",
        authorization=authorization,
    )

    assert receipt.outcome == expected_outcome
    assert receipt.linked_issue_numbers == issue_numbers
    assert receipt.authoritative_issue_numbers == issue_numbers
    assert receipt.primary_issue_number == expected_primary


@pytest.mark.parametrize(
    ("relationship_created_at", "expected_reason"),
    (
        (None, "source_manifest_recapture_required"),
        ("2026-08-01T00:00:00Z", "authoritative_issue_missing"),
        ("2026-09-01T00:00:00Z", "authoritative_issue_missing"),
    ),
)
def test_crossref_fallback_requires_valid_premerge_timestamps(
    tmp_path: Path,
    relationship_created_at: str | None,
    expected_reason: str,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 17701)
    authorization = _authorization()
    _references, manifest_path = _write_v2_source_chain(
        root,
        candidate=candidate,
        authorization=authorization,
        issue_numbers=(7,),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if relationship_created_at is None:
        del manifest["relationships"][0]["created_at"]
    else:
        manifest["relationships"][0]["created_at"] = relationship_created_at
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )

    receipt = write_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
        created_at="2026-10-01T00:00:00Z",
        authorization=authorization,
    )

    assert receipt.outcome == "rejected"
    assert receipt.reason == expected_reason
    assert receipt.linked_issue_numbers == (7,)
    assert receipt.authoritative_issue_numbers == ()


@pytest.mark.parametrize(
    ("issue_numbers", "pull_request_number", "expected_reason"),
    (
        ((), None, "authoritative_issue_missing"),
        ((7, 8), None, "multiple_authoritative_issues"),
        ((7,), 7, "authoritative_issue_is_pull_request"),
        ((17701,), None, "authoritative_issue_is_pull_request"),
    ),
)
def test_missing_multi_or_pull_request_issue_is_mechanically_rejected(
    tmp_path: Path,
    issue_numbers: tuple[int, ...],
    pull_request_number: int | None,
    expected_reason: str,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 17701)
    authorization = _authorization()
    _write_v2_source_chain(
        root,
        candidate=candidate,
        authorization=authorization,
        issue_numbers=issue_numbers,
        pull_request_number=pull_request_number,
    )
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )

    receipt = write_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
        created_at="2026-08-13T00:00:04Z",
        authorization=authorization,
    )

    assert receipt.outcome == "rejected"
    assert receipt.reason == expected_reason
    assert receipt.primary_issue_number is None
    assert receipt.authoritative_issue_numbers == issue_numbers


def test_source_atomicity_has_no_caller_verdict_or_issue_selector() -> None:
    parameters = inspect.signature(write_source_atomicity_artifact).parameters

    assert tuple(parameters) == (
        "batch_context",
        "candidate",
        "created_at",
        "authorization",
    )
    assert not {
        "atomic",
        "outcome",
        "verdict",
        "issue_number",
        "issue_numbers",
        "primary_issue_number",
    }.intersection(parameters)


def test_relationship_parser_drift_writes_clear_recapture_required_receipt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 17701)
    authorization = _authorization()
    _references, manifest_path = _write_v2_source_chain(
        root,
        candidate=candidate,
        authorization=authorization,
        issue_numbers=(7,),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["relationships"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )

    receipt = write_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
        created_at="2026-08-13T00:00:04Z",
        authorization=authorization,
    )

    assert receipt.outcome == "rejected"
    assert receipt.reason == "source_manifest_recapture_required"
    assert receipt.linked_issue_numbers == (7,)
    assert receipt.authoritative_issue_numbers == ()
    assert (
        load_source_atomicity_artifact(
            batch_context=context,
            candidate=candidate,
        )
        == receipt
    )


def test_new_closing_parser_match_fails_closed_without_crashing_batch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 8712)
    authorization = _authorization()
    _references, manifest_path = _write_v2_source_chain(
        root,
        candidate=candidate,
        authorization=authorization,
        issue_numbers=(8704,),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pull"]["body"] = (
        "Fixes #8704. Resolves https://github.com/jupyterlab/jupyterlab/issues/8705."
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )

    receipt = write_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
        created_at="2026-08-13T00:00:04Z",
        authorization=authorization,
    )

    assert receipt.outcome == "rejected"
    assert receipt.reason == "source_manifest_recapture_required"
    assert receipt.linked_issue_numbers == (8704, 8705)
    assert receipt.authoritative_issue_numbers == ()


def test_prevalidated_batch_context_cannot_be_constructed_by_the_caller(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 17701)
    authorization = _authorization()
    _write_v2_source_chain(
        root,
        candidate=candidate,
        authorization=authorization,
        issue_numbers=(7,),
    )
    validated = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )

    with pytest.raises(TypeError):
        SourceAtomicityBatchContext(
            artifact_root=validated.artifact_root,
            enumeration_reference=validated.enumeration_reference,
            enumeration_created_at=validated.enumeration_created_at,
            repository_key=validated.repository_key,
            candidate_numbers=frozenset({candidate.change_number + 1}),
        )


def test_batch_context_loads_large_enumeration_once_not_per_candidate_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 17701)
    authorization = _authorization()
    _write_v2_source_chain(
        root,
        candidate=candidate,
        authorization=authorization,
        issue_numbers=(7,),
    )
    original = source_atomicity_module.load_stable_artifact
    enumeration_loads = 0

    def counted_load(*args, **kwargs):
        nonlocal enumeration_loads
        if kwargs.get("artifact_kind") == "repository_enumeration":
            enumeration_loads += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        source_atomicity_module,
        "load_stable_artifact",
        counted_load,
    )
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )
    write_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
        created_at="2026-08-13T00:00:04Z",
        authorization=authorization,
    )
    load_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
    )

    assert enumeration_loads == 1


def test_loader_rejects_strict_scalar_substitution_in_signed_stable_payload(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 17701)
    authorization = _authorization()
    _write_v2_source_chain(
        root,
        candidate=candidate,
        authorization=authorization,
        issue_numbers=(7,),
    )
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )
    receipt = write_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
        created_at="2026-08-13T00:00:04Z",
        authorization=authorization,
    )
    tampered = dict(receipt.artifact.payload)
    tampered["authoritative_issue_count"] = True
    write_stable_artifact(
        root,
        artifact_id=receipt.artifact.reference.artifact_id,
        artifact_kind=receipt.artifact.reference.artifact_kind,
        semantic_version=receipt.artifact.reference.semantic_version,
        created_at="2026-08-13T00:00:05Z",
        inputs=receipt.artifact.inputs,
        payload=tampered,
        authorization=authorization,
    )

    with pytest.raises(RuntimeError, match="exact type drift"):
        load_source_atomicity_artifact(
            batch_context=context,
            candidate=candidate,
        )


def test_loader_rejects_non_screening_authority_on_current_head(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 17701)
    authorization = _authorization()
    _write_v2_source_chain(
        root,
        candidate=candidate,
        authorization=authorization,
        issue_numbers=(7,),
    )
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )
    receipt = write_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
        created_at="2026-08-13T00:00:04Z",
        authorization=authorization,
    )
    annotation_authorization = AuthorizationRecord(
        authorization_id="local-source-atomicity/wrong-scope",
        scope="paid-annotation",
        granted_at="2026-08-13T00:00:05Z",
        local_user_id=os.getuid(),
    )
    write_stable_artifact(
        root,
        artifact_id=receipt.artifact.reference.artifact_id,
        artifact_kind=receipt.artifact.reference.artifact_kind,
        semantic_version=receipt.artifact.reference.semantic_version,
        created_at="2026-08-13T00:00:05Z",
        inputs=receipt.artifact.inputs,
        payload=dict(receipt.artifact.payload),
        authorization=annotation_authorization,
    )

    with pytest.raises(RuntimeError, match="candidate-screening authorization"):
        load_source_atomicity_artifact(
            batch_context=context,
            candidate=candidate,
        )


def test_loader_replays_manifest_and_current_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 17701)
    authorization = _authorization()
    references, manifest_path = _write_v2_source_chain(
        root,
        candidate=candidate,
        authorization=authorization,
        issue_numbers=(7,),
    )
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )
    write_source_atomicity_artifact(
        batch_context=context,
        candidate=candidate,
        created_at="2026-08-13T00:00:04Z",
        authorization=authorization,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["issue_numbers"] = [7, 8]
    manifest["issues"].append(_issue_row(8))
    manifest["relationships"].append(
        {
            "event": "cross-referenced",
            "created_at": "2026-07-02T00:00:00Z",
            "source": {
                "issue": {
                    "number": 8,
                    "created_at": "2026-07-01T00:00:00Z",
                    "repository_url": (
                        "https://api.github.com/repos/jupyterlab/jupyterlab"
                    ),
                }
            },
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="mechanical replay.*drift"):
        load_source_atomicity_artifact(
            batch_context=context,
            candidate=candidate,
        )

    manifest["issue_numbers"] = [7]
    manifest["issues"] = [_issue_row(7)]
    manifest["relationships"] = manifest["relationships"][:1]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    checkpoint = references[-1]
    write_stable_artifact(
        root,
        artifact_id=checkpoint.artifact_id,
        artifact_kind=checkpoint.artifact_kind,
        semantic_version=checkpoint.semantic_version,
        created_at="2026-08-13T00:00:05Z",
        inputs=references[:1] + references[2:3],
        payload={
            "candidate": candidate.to_value(),
            "state": "complete",
            "outcome": "patch_rejected",
            "stage_artifact_id": references[2].artifact_id,
        },
        authorization=authorization,
    )
    with pytest.raises(RuntimeError, match="checkpoint is not patch eligible"):
        load_source_atomicity_artifact(
            batch_context=context,
            candidate=candidate,
        )


def test_source_atomicity_rejects_unsafe_manifest_and_scalar_substitution(
    tmp_path: Path,
) -> None:
    root = tmp_path / "selection"
    candidate = CandidateKey("jupyterlab", 17701)
    authorization = _authorization()
    _references, manifest_path = _write_v2_source_chain(
        root,
        candidate=candidate,
        authorization=authorization,
        issue_numbers=(7,),
    )
    context = load_source_atomicity_batch_context(
        root,
        repository_key=candidate.repository_key,
    )
    outside = tmp_path / "outside.json"
    outside.write_bytes(manifest_path.read_bytes())
    manifest_path.unlink()
    manifest_path.symlink_to(outside)

    with pytest.raises(RuntimeError, match="source manifest path contains a symlink"):
        write_source_atomicity_artifact(
            batch_context=context,
            candidate=candidate,
            created_at="2026-08-13T00:00:04Z",
            authorization=authorization,
        )

    manifest_path.unlink()
    manifest = json.loads(outside.read_text(encoding="utf-8"))
    manifest["issue_numbers"] = [True]
    manifest["issues"] = [_issue_row(7)]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="issue inventory is invalid"):
        write_source_atomicity_artifact(
            batch_context=context,
            candidate=candidate,
            created_at="2026-08-13T00:00:04Z",
            authorization=authorization,
        )


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-source-atomicity/test",
        scope="candidate-screening",
        granted_at="2026-08-13T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _issue_row(
    number: int,
    *,
    pull_request: bool = False,
    created_at: str = "2026-07-01T00:00:00Z",
) -> dict[str, object]:
    issue: dict[str, object] = {
        "number": number,
        "title": f"Issue {number}",
        "body": "Public defect report.",
        "created_at": created_at,
    }
    if pull_request:
        issue["pull_request"] = {"url": f"https://example.invalid/pulls/{number}"}
    return {
        "number": number,
        "issue": issue,
        "comments": [],
        "history": [],
    }


def _write_v2_source_chain(
    root: Path,
    *,
    candidate: CandidateKey,
    authorization: AuthorizationRecord,
    issue_numbers: tuple[int, ...],
    pull_request_number: int | None = None,
) -> tuple[tuple[ArtifactReference, ...], Path]:
    enumeration = ArtifactReference(
        f"repository_enumeration/{candidate.repository_key}/current/v2",
        "repository_enumeration",
        "2.0",
    )
    source = ArtifactReference(
        (
            f"candidate_source_packet/{candidate.repository_key}/"
            f"{candidate.change_number}/source/v2"
        ),
        "candidate_source_packet",
        "2.0",
    )
    patch = ArtifactReference(
        (
            f"candidate_test_patch/{candidate.repository_key}/"
            f"{candidate.change_number}/screening/v2"
        ),
        "candidate_test_patch",
        "2.0",
    )
    checkpoint = ArtifactReference(
        (
            f"screening_checkpoint/{candidate.repository_key}/"
            f"{candidate.change_number}/current/v2"
        ),
        "screening_checkpoint",
        "2.0",
    )
    write_stable_artifact(
        root,
        artifact_id=enumeration.artifact_id,
        artifact_kind=enumeration.artifact_kind,
        semantic_version=enumeration.semantic_version,
        created_at="2026-08-13T00:00:00Z",
        inputs=(),
        payload={
            "repository_key": candidate.repository_key,
            "merged_change_numbers": [candidate.change_number],
            "authoritative_issue_link_missing": [],
            "repair_not_in_pinned": [],
            "candidates": [candidate.to_value()],
            "infrastructure_blocked": [],
            "complete": True,
        },
        authorization=authorization,
    )
    manifest_relative = (
        f"raw/{candidate.repository_key}/{candidate.change_number}/source-manifest.json"
    )
    patch_relative = (
        f"raw/{candidate.repository_key}/{candidate.change_number}/repair.diff"
    )
    manifest_path = root / manifest_relative
    patch_path = root / patch_relative
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_bytes(b"diff --git a/a.py b/a.py\n")
    github_repositories = {
        "jupyterlab": "jupyterlab/jupyterlab",
        "luanti": "luanti-org/luanti",
        "openra": "OpenRA/OpenRA",
    }
    repository_name = github_repositories[candidate.repository_key]
    repository_url = f"https://api.github.com/repos/{repository_name}"
    manifest_path.write_text(
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
                                "number": number,
                                "repository_url": repository_url,
                                "created_at": "2026-07-01T00:00:00Z",
                            }
                        },
                    }
                    for number in issue_numbers
                ],
                "issue_numbers": list(issue_numbers),
                "issues": [
                    _issue_row(
                        number,
                        pull_request=number == pull_request_number,
                    )
                    for number in issue_numbers
                ],
                "raw_paths": [patch_relative],
                "request_receipt_paths": [],
                "patch_raw_path": patch_relative,
                "attachment_paths": [],
                "complete": True,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    write_stable_artifact(
        root,
        artifact_id=source.artifact_id,
        artifact_kind=source.artifact_kind,
        semantic_version=source.semantic_version,
        created_at="2026-08-13T00:00:01Z",
        inputs=(enumeration,),
        payload={
            "candidate": candidate.to_value(),
            "raw_evidence_path": manifest_relative,
            "complete": True,
        },
        authorization=authorization,
    )
    write_stable_artifact(
        root,
        artifact_id=patch.artifact_id,
        artifact_kind=patch.artifact_kind,
        semantic_version=patch.semantic_version,
        created_at="2026-08-13T00:00:02Z",
        inputs=(enumeration, source),
        payload={
            "candidate": candidate.to_value(),
            "outcome": "patch_eligible",
            "production_paths": ["a.py"],
            "test_paths": ["tests/test_a.py"],
            "source_manifest_path": manifest_relative,
            "raw_patch_path": patch_relative,
            "complete": True,
        },
        authorization=authorization,
    )
    write_stable_artifact(
        root,
        artifact_id=checkpoint.artifact_id,
        artifact_kind=checkpoint.artifact_kind,
        semantic_version=checkpoint.semantic_version,
        created_at="2026-08-13T00:00:03Z",
        inputs=(enumeration, patch),
        payload={
            "candidate": candidate.to_value(),
            "state": "complete",
            "outcome": "patch_eligible",
            "stage_artifact_id": patch.artifact_id,
        },
        authorization=authorization,
    )
    return (enumeration, source, patch, checkpoint), manifest_path
