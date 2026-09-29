from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_lua_regression import (
    profile_for_candidate,
)
from warptest.c1_lazarus_selection.luanti_lua_visual_closure import (
    REQUIRED_RUNTIME_VISUAL_EVIDENCE,
    evaluate_luanti_lua_version_materialization,
)
from warptest.c1_lazarus_selection.luanti_lua_downstream_closure import (
    load_luanti_lua_downstream_preflight,
    prepare_luanti_lua_downstream_closure,
)
from warptest.c1_lazarus_selection.models import CandidateKey
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/lua-visual-closure",
        scope="preannotation-execution",
        granted_at="2026-08-13T12:00:00Z",
        local_user_id=os.getuid(),
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    *,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    second: int,
) -> None:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=f"2026-08-13T12:00:{second:02d}Z",
        inputs=inputs,
        payload=payload,
        authorization=_authorization(),
    )


def _attempt_reference(number: int, phase: str, variant: str, repetition: int):
    return ArtifactReference(
        f"lua_regression_attempt/luanti/{number}/{phase}/{variant}/{repetition}/v3",
        "lua_regression_attempt",
        "3.0",
    )


def _seed_closed_unit_evidence(root: Path, number: int = 14757) -> CandidateKey:
    candidate = CandidateKey("luanti", number)
    profile = profile_for_candidate(candidate)
    source_tree = root / profile.fixed_source_tree_path
    for relative in (
        *profile.production_paths,
        *profile.support_paths,
        *profile.test_paths,
    ):
        target = source_tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"fixed {relative}\n", encoding="utf-8")

    _write(
        root,
        profile.fixed_capsule_reference,
        inputs=(profile.source_packet_reference,),
        payload={
            "candidate": candidate.to_value(),
            "profile_id": profile.profile_id,
            "source_tree_path": profile.fixed_source_tree_path,
            "support_paths": list(profile.support_paths),
            "test_paths": list(profile.test_paths),
        },
        second=1,
    )
    manifest = ArtifactReference(
        f"candidate_stage/luanti/{number}/test_manifest/v2",
        "candidate_stage",
        "2.0",
    )
    patch = ArtifactReference(
        f"candidate_test_patch/luanti/{number}/screening/v2",
        "candidate_test_patch",
        "2.0",
    )
    _write(
        root,
        manifest,
        inputs=(patch,),
        payload={
            "candidate": candidate.to_value(),
            "stage": "test_manifest",
            "state": "passed",
            "outcome": None,
            "details": {"executable_test_paths": list(profile.test_paths)},
            "complete": True,
        },
        second=2,
    )

    attempts: list[ArtifactReference] = []
    cells: list[dict[str, object]] = []
    second = 3
    for phase in ("f2p", "p2p"):
        for variant in ("fixed", "buggy"):
            outcomes: list[str] = []
            for repetition in range(1, 4):
                reference = _attempt_reference(number, phase, variant, repetition)
                attempts.append(reference)
                if phase == "f2p" and variant == "buggy":
                    observed = "timed_out" if number == 13132 else "failure"
                else:
                    observed = "pass"
                expected = observed
                if observed == "timed_out":
                    process, returncode, count = "timed_out", -9, None
                    stdout = b""
                elif observed == "failure":
                    process, returncode, count = "completed", 1, 1
                    stdout = b"0 successes / 1 failure / 0 errors / 0 pending\n"
                else:
                    process, returncode, count = "completed", 0, 1
                    stdout = b"1 success / 0 failures / 0 errors / 0 pending\n"
                _write(
                    root,
                    reference,
                    inputs=(profile.fixed_capsule_reference,),
                    payload={
                        "candidate": candidate.to_value(),
                        "profile_id": profile.profile_id,
                        "phase": phase,
                        "variant": variant,
                        "repetition": repetition,
                        "selector": (
                            profile.f2p_selector
                            if phase == "f2p"
                            else profile.p2p_selector
                        ),
                        "test_path": profile.test_paths[0],
                        "expected_outcome": expected,
                        "observed_outcome": observed,
                        "process_outcome": process,
                        "returncode": returncode,
                        "completed_test_count": count,
                        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
                        "stderr_base64": "",
                        "stdout_truncated": False,
                        "stderr_truncated": False,
                        "output_encoding": "base64",
                        "matched_expected": True,
                    },
                    second=second,
                )
                second += 1
                outcomes.append(observed)
            cells.append(
                {"phase": phase, "variant": variant, "observed_outcomes": outcomes}
            )
    aggregate = ArtifactReference(
        f"lua_regression_aggregate/luanti/{number}/fixed-buggy/v3",
        "lua_regression_aggregate",
        "3.0",
    )
    aggregate_payload = {
        "candidate": candidate.to_value(),
        "profile_id": profile.profile_id,
        "repetitions_per_cell": 3,
        "attempt_count": 12,
        "stable_repetitions": True,
        "all_expected": True,
        "qualified": True,
        "cells": sorted(cells, key=lambda item: (item["phase"], item["variant"])),
    }
    _write(
        root, aggregate, inputs=tuple(attempts), payload=aggregate_payload, second=15
    )
    transition = ArtifactReference(
        f"luanti_lua_regression_transition/luanti/{number}/test-repetitions/v2",
        "luanti_lua_regression_transition",
        "2.0",
    )
    _write(
        root,
        transition,
        inputs=(manifest, profile.fixed_capsule_reference, *attempts, aggregate),
        payload={
            "candidate": candidate.to_value(),
            "profile_id": profile.profile_id,
            "repetitions_per_cell": 3,
            "attempt_count": 12,
            "stable_repetitions": True,
            "all_expected": True,
            "qualified": True,
            "truth_table": {
                "fixed": {"f2p": True, "p2p": True},
                "buggy": {"f2p": False, "p2p": True},
            },
            "complete": True,
        },
        second=16,
    )
    test_stage = ArtifactReference(
        f"candidate_stage/luanti/{number}/test_repetitions/v2",
        "candidate_stage",
        "2.0",
    )
    _write(
        root,
        test_stage,
        inputs=(manifest, transition),
        payload={
            "candidate": candidate.to_value(),
            "stage": "test_repetitions",
            "state": "passed",
            "outcome": None,
            "details": {
                "next_gate": "version_materialization",
                "evidence_artifacts": [
                    {
                        "artifact_id": transition.artifact_id,
                        "artifact_kind": transition.artifact_kind,
                        "semantic_version": transition.semantic_version,
                    }
                ],
            },
            "complete": True,
        },
        second=17,
    )
    checkpoint = ArtifactReference(
        f"preannotation_checkpoint/luanti/{number}/current/v2",
        "preannotation_checkpoint",
        "2.0",
    )
    _write(
        root,
        checkpoint,
        inputs=(test_stage,),
        payload={
            "candidate": candidate.to_value(),
            "completed_stages": ["test_manifest", "test_repetitions"],
            "state": "active",
            "outcome": None,
            "current_artifact_id": test_stage.artifact_id,
        },
        second=18,
    )
    return candidate


@pytest.mark.parametrize("number", [9722, 12703, 13132, 14757])
def test_closed_unit_evidence_cannot_substitute_for_runtime_visual_closure(
    tmp_path: Path, number: int
) -> None:
    candidate = _seed_closed_unit_evidence(tmp_path, number)

    decision = evaluate_luanti_lua_version_materialization(
        tmp_path, candidate=candidate
    )

    assert decision.ready is False
    assert decision.reason == "registered_runtime_world_visual_closure_missing"
    assert decision.missing_required_evidence == REQUIRED_RUNTIME_VISUAL_EVIDENCE
    assert decision.golden_disposition == "required_no_not_applicable"
    assert decision.unit_evidence_qualified is True
    assert decision.unit_transition == ArtifactReference(
        (f"luanti_lua_regression_transition/luanti/{number}/test-repetitions/v2"),
        "luanti_lua_regression_transition",
        "2.0",
    )


def test_tampered_attempt_fails_before_the_missing_closure_decision(
    tmp_path: Path,
) -> None:
    candidate = _seed_closed_unit_evidence(tmp_path)
    reference = _attempt_reference(14757, "f2p", "buggy", 1)
    attempt = load_stable_artifact(
        tmp_path,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    payload = dict(attempt.payload)
    payload["observed_outcome"] = "pass"
    _write(
        tmp_path,
        reference,
        inputs=attempt.inputs,
        payload=payload,
        second=30,
    )

    with pytest.raises(RuntimeError, match="attempt result is invalid"):
        evaluate_luanti_lua_version_materialization(tmp_path, candidate=candidate)


def test_missing_test_repetition_stage_fails_closed(tmp_path: Path) -> None:
    candidate = _seed_closed_unit_evidence(tmp_path)
    stage_dir = (
        tmp_path / "artifacts/candidate_stage/luanti/14757/test_repetitions/v2/2.0"
    )
    (stage_dir / "head.json").unlink()

    with pytest.raises(RuntimeError, match="artifact current head is missing"):
        evaluate_luanti_lua_version_materialization(tmp_path, candidate=candidate)


def test_no_runtime_visual_evidence_is_written_or_invented(tmp_path: Path) -> None:
    candidate = _seed_closed_unit_evidence(tmp_path)
    before = {path for path in tmp_path.rglob("head.json")}

    evaluate_luanti_lua_version_materialization(tmp_path, candidate=candidate)

    assert {path for path in tmp_path.rglob("head.json")} == before
    assert not any("not_applicable" in path.read_text() for path in before)


def _seed_application_pair(root: Path, number: int) -> None:
    profile = profile_for_candidate(CandidateKey("luanti", number))
    for version in ("fixed", "buggy"):
        tree = root / f"private/luanti-{number}-formal-materialization/{version}"
        for relative in profile.production_paths:
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{version} {relative}\n", encoding="utf-8")
        binary = tree / "bin/minetest"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"registered application binary\n")
        binary.chmod(0o700)
        contents = tree / "build-app/out/minetest.app/Contents"
        (contents / "MacOS").mkdir(parents=True, exist_ok=True)
        (contents / "Resources/builtin").mkdir(parents=True, exist_ok=True)
        (contents / "Info.plist").write_text("registered plist\n", encoding="utf-8")
        app_binary = contents / "MacOS/minetest"
        app_binary.write_bytes(b"registered application binary\n")
        app_binary.chmod(0o700)


@pytest.mark.parametrize("number", [9722, 12703, 13132, 14757])
def test_downstream_preflight_registers_start_replay_oracle_without_demoting(
    tmp_path: Path, number: int
) -> None:
    candidate = _seed_closed_unit_evidence(tmp_path, number)
    if number == 14757:
        _seed_application_pair(tmp_path, number)

    result = prepare_luanti_lua_downstream_closure(
        tmp_path,
        candidate=candidate,
        authorization=_authorization(),
    )
    loaded = load_luanti_lua_downstream_preflight(
        tmp_path,
        candidate=candidate,
    )

    assert loaded.reference == result.preflight.reference
    assert loaded.payload["admission_state"] == "active_qualified"
    assert loaded.payload["downstream_state"] == "pending"
    assert loaded.payload["candidate_execution_performed"] is False
    assert loaded.payload["permanent_terminal_written"] is False
    assert loaded.payload["golden_disposition"] == "required_no_not_applicable"
    start = result.mapping.payload["registered_start"]
    assert start
    if number == 12703:
        assert start["surface"] == "mainmenu_contentdb"
    else:
        assert start["world_nodes"]
    assert (
        result.mapping.payload["ordered_replay"][0]["type"]
        == "restore_registered_start"
    )
    assert result.mapping.payload["semantic_oracle"]["goal"]
    assert (
        result.mapping.payload["golden_contract"]["two_by_two_after_cross_matrix"]
        is True
    )
    fixture = tmp_path / result.mapping.payload["fixture_root"]
    if number == 12703:
        assert (fixture / "client/menu/start.json").is_file()
    else:
        assert (fixture / "mods/c1_lazarus/init.lua").is_file()
    assert (fixture / "replay.json").is_file()
    assert (fixture / "oracle.json").is_file()
    assert (fixture / "golden-contract.json").is_file()

    if number == 14757:
        assert result.mapping.reference.semantic_version == "4.0"
        assert result.mapping.payload["fixture_root"].endswith("/v2")
        assert result.preflight.reference.semantic_version == "4.0"
        assert loaded.payload["application_pair_status"] == "complete_launch_unverified"
        assert loaded.payload["next_action"] == "validate_bounded_application_launch"
        assert (
            "complete_fixed_buggy_application_version_pair"
            not in loaded.payload["missing_required_evidence"]
        )
    else:
        assert loaded.payload["application_pair_status"] == "missing"
        assert loaded.payload["next_action"] == "materialize_complete_application_pair"
        assert (
            "complete_fixed_buggy_application_version_pair"
            in loaded.payload["missing_required_evidence"]
        )


def test_downstream_preflight_rejects_symlinked_application_root(
    tmp_path: Path,
) -> None:
    candidate = _seed_closed_unit_evidence(tmp_path, 14757)
    external = tmp_path / "external"
    external.mkdir()
    materialization = tmp_path / "private/luanti-14757-formal-materialization"
    materialization.mkdir(parents=True)
    (materialization / "fixed").symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="application version root is unsafe"):
        prepare_luanti_lua_downstream_closure(
            tmp_path,
            candidate=candidate,
            authorization=_authorization(),
        )
