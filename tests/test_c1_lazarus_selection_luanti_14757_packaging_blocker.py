from __future__ import annotations

import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_14757_packaging_blocker import (
    load_luanti_14757_packaging_binding,
    write_luanti_14757_packaging_blocker,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    AuthorizationRecord,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 14757}
_PIDS = [20946, 21627, 30865, 70361, 60839]


def _auth(scope: str = "preannotation-execution") -> AuthorizationRecord:
    return AuthorizationRecord(
        "local-test/luanti-14757-packaging-blocker",
        scope,
        "2026-08-14T00:00:00Z",
        os.getuid(),
    )


def _write(
    root: Path,
    artifact_id: str,
    artifact_kind: str,
    version: str,
    payload: dict[str, object],
) -> None:
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        semantic_version=version,
        created_at="2026-08-14T00:00:01Z",
        inputs=(),
        payload=payload,
        authorization=_auth(),
    )


def _seed(root: Path) -> None:
    _write(
        root,
        "lua_regression_aggregate/luanti/14757/fixed-buggy/v3",
        "lua_regression_aggregate",
        "3.0",
        {
            "candidate": _CANDIDATE,
            "qualified": True,
            "all_expected": True,
            "attempt_count": 12,
            "repetitions_per_cell": 3,
            "stable_repetitions": True,
            "cells": [
                {
                    "observed_outcomes": ["failure", "failure", "failure"],
                    "phase": "f2p",
                    "variant": "buggy",
                },
                {
                    "observed_outcomes": ["pass", "pass", "pass"],
                    "phase": "f2p",
                    "variant": "fixed",
                },
                {
                    "observed_outcomes": ["pass", "pass", "pass"],
                    "phase": "p2p",
                    "variant": "buggy",
                },
                {
                    "observed_outcomes": ["pass", "pass", "pass"],
                    "phase": "p2p",
                    "variant": "fixed",
                },
            ],
        },
    )
    _write(
        root,
        "luanti_lua_regression_transition/luanti/14757/test-repetitions/v2",
        "luanti_lua_regression_transition",
        "2.0",
        {
            "candidate": _CANDIDATE,
            "qualified": True,
            "truth_table": {
                "buggy": {"f2p": False, "p2p": True},
                "fixed": {"f2p": True, "p2p": True},
            },
        },
    )
    _write(
        root,
        "candidate_downstream_status/luanti/14757/current/v2",
        "candidate_downstream_status",
        "2.0",
        {"candidate": _CANDIDATE, "gate_1_7_status": "qualified"},
    )
    _write(
        root,
        "lazarus_task_mapping/luanti/14757/lz-luanti-14757-math-round/v4",
        "lazarus_task_mapping",
        "4.0",
        {
            "candidate": _CANDIDATE,
            "complete": True,
            "candidate_execution_performed": False,
            "registered_start": {"world_nodes": []},
            "ordered_replay": [{"type": "restore_registered_start"}],
            "semantic_oracle": {"goal": "round boundary"},
            "golden_contract": {"common_before_fixed_after_buggy_after": True},
        },
    )
    _write(
        root,
        "downstream_preflight/luanti/14757/runtime-visual/v4",
        "downstream_preflight",
        "4.0",
        {
            "candidate": _CANDIDATE,
            "application_pair_status": "complete_launch_unverified",
            "missing_application_versions": [],
            "candidate_execution_performed": False,
            "permanent_terminal_written": False,
        },
    )
    _write(
        root,
        "candidate_downstream_recovery_audit/luanti/14757/runtime-visual/v1",
        "candidate_downstream_recovery_audit",
        "1.0",
        {
            "candidate": _CANDIDATE,
            "admission_gate_1_7_status": "qualified",
            "state": "downstream_pending",
            "outcome": "infrastructure_blocked",
            "resumable": True,
            "terminal": False,
            "permanent_terminal_written": False,
            "inherit_prior_attempts": False,
            "do_not_touch_pids": _PIDS,
            "observed_probe": {
                "process_outcome": "timed_out",
                "process_state": "UE",
                "stack_signature": "dyld4::JustInTimeLoader::loadDependents",
            },
            "static_bundle_checks": {
                "codesign_check": "code has no resources but signature indicates they must be present",
                "dyld_dependency_listing": "completed_without_launch",
            },
        },
    )
    _write(
        root,
        "candidate_sprint_terminal/luanti/14757/runtime-visual-closure/v2",
        "candidate_sprint_terminal",
        "2.0",
        {
            "candidate": _CANDIDATE,
            "admissible": False,
            "strict_freeze_eligible": False,
            "legacy_conclusions_reused": False,
        },
    )
    for variant in ("fixed", "buggy"):
        bundle = (
            root
            / f"private/luanti-14757-formal-materialization/{variant}/build-app/out/minetest.app"
        )
        binary = bundle / "Contents/MacOS/minetest"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"registered arm64 binary")
        binary.chmod(0o700)
        (bundle.parent.parent.parent / "build-app.formal.log").write_text(
            "install_name_tool invalidates code signature\n", encoding="utf-8"
        )
        (bundle.parent.parent.parent / "doc/compiling").mkdir(
            parents=True, exist_ok=True
        )
        (bundle.parent.parent.parent / "doc/compiling/macos.md").write_text(
            "codesign --force --deep -s - macos/minetest.app\n", encoding="utf-8"
        )


def test_static_binding_does_not_launch_or_reuse_old_pids(tmp_path: Path) -> None:
    _seed(tmp_path)
    binding = load_luanti_14757_packaging_binding(tmp_path)
    assert binding.fixed_binary.is_file()
    assert binding.buggy_binary.is_file()
    assert not (tmp_path / "private/new_candidate_launch").exists()

    artifact = write_luanti_14757_packaging_blocker(tmp_path, authorization=_auth())
    assert artifact.payload["state"] == "downstream_pending"
    assert artifact.payload["terminal"] is False
    assert artifact.payload["candidate_execution_performed"] is False
    assert artifact.payload["do_not_touch_pids"] == _PIDS
    assert artifact.payload["next_action"]["launch_performed_by_this_plan"] is False
    assert artifact.payload["repackage_feasibility"]["status"] == (
        "candidate_specific_path_available_not_executed"
    )
    assert (
        artifact.payload["repackage_feasibility"]["repackage_or_resign_performed"]
        is False
    )
    assert (
        "registered_start_and_ordered_replay"
        in artifact.payload["missing_required_evidence"]
    )


def test_packaging_blocker_requires_preannotation_authorization(tmp_path: Path) -> None:
    _seed(tmp_path)
    with pytest.raises(PermissionError, match="preannotation execution"):
        write_luanti_14757_packaging_blocker(
            tmp_path, authorization=_auth("candidate-screening")
        )


def test_static_binding_fails_closed_if_pair_is_missing(tmp_path: Path) -> None:
    _seed(tmp_path)
    (
        tmp_path
        / "private/luanti-14757-formal-materialization/buggy/build-app/out/minetest.app/Contents/MacOS/minetest"
    ).unlink()
    with pytest.raises(RuntimeError, match="required file"):
        load_luanti_14757_packaging_binding(tmp_path)
