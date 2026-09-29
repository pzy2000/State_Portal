from __future__ import annotations

import os
from pathlib import Path

import pytest

from warptest.c1_lazarus_selection.luanti_14757_bounded_launch import (
    EXECUTION_CONFIRMATION,
    BoundedLaunchObservation,
    BoundedLaunchSpec,
    execute_luanti_14757_bounded_launch,
)
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    load_stable_artifact,
    write_stable_artifact,
)


def _authorization(scope: str = "preannotation-execution") -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/luanti-14757-bounded-launch-v1",
        scope=scope,
        granted_at="2026-08-14T00:00:00Z",
        local_user_id=os.getuid(),
    )


def _seed_boundary(root: Path):
    fixture = root / (
        "private/downstream-fixtures/luanti/14757/lz-luanti-14757-math-round/v2"
    )
    mod = fixture / "mods/c1_lazarus"
    mod.mkdir(parents=True)
    (mod / "init.lua").write_text("registered v2 fixture\n", encoding="utf-8")
    (mod / "mod.conf").write_text("name = c1_lazarus\n", encoding="utf-8")
    mapping = ArtifactReference(
        "lazarus_task_mapping/luanti/14757/lz-luanti-14757-math-round/v4",
        "lazarus_task_mapping",
        "4.0",
    )
    write_stable_artifact(
        root,
        artifact_id=mapping.artifact_id,
        artifact_kind=mapping.artifact_kind,
        semantic_version=mapping.semantic_version,
        created_at="2026-08-14T00:00:01Z",
        inputs=(),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 14757},
            "fixture_root": fixture.relative_to(root).as_posix(),
            "fixture_files": [
                "mods/c1_lazarus/init.lua",
                "mods/c1_lazarus/mod.conf",
            ],
            "ordered_replay": [
                {"type": "restore_registered_start"},
                {
                    "type": "luanti_worldmod_math_round_boundary",
                    "command": "/c1_lazarus_round",
                },
            ],
            "candidate_execution_performed": False,
            "complete": True,
        },
        authorization=_authorization(),
    )
    paths = []
    for variant in ("fixed", "buggy"):
        tree = root / f"private/luanti-14757-formal-materialization/{variant}"
        binary = tree / "build-app/out/minetest.app/Contents/MacOS/minetest"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"registered application binary\n")
        binary.chmod(0o700)
        paths.append(tree.relative_to(root).as_posix())
    preflight = ArtifactReference(
        "downstream_preflight/luanti/14757/runtime-visual/v4",
        "downstream_preflight",
        "4.0",
    )
    write_stable_artifact(
        root,
        artifact_id=preflight.artifact_id,
        artifact_kind=preflight.artifact_kind,
        semantic_version=preflight.semantic_version,
        created_at="2026-08-14T00:00:02Z",
        inputs=(mapping,),
        payload={
            "candidate": {"repository_key": "luanti", "change_number": 14757},
            "application_pair_status": "complete_launch_unverified",
            "registered_application_paths": paths,
            "candidate_execution_performed": False,
            "permanent_terminal_written": False,
        },
        authorization=_authorization(),
    )
    return load_stable_artifact(
        root,
        artifact_id=preflight.artifact_id,
        artifact_kind=preflight.artifact_kind,
        semantic_version=preflight.semantic_version,
    )


class _Driver:
    def __init__(self, outcomes: dict[str, BoundedLaunchObservation]) -> None:
        self.outcomes = outcomes
        self.specs: list[BoundedLaunchSpec] = []

    def run(self, spec: BoundedLaunchSpec) -> BoundedLaunchObservation:
        self.specs.append(spec)
        return self.outcomes[spec.variant]


def _observation(variant: str) -> BoundedLaunchObservation:
    return BoundedLaunchObservation(
        process_outcome="completed",
        returncode=0,
        stdout=b"",
        stderr=b"",
        start_marker="basenodes:cobble",
        start_result="",
        action_command="/c1_lazarus_round",
        action_count=1,
        semantic_result="0,0" if variant == "fixed" else "1,-1",
    )


def test_bounded_launch_requires_active_preannotation_authorization(
    tmp_path: Path,
) -> None:
    with pytest.raises(PermissionError, match="preannotation-execution"):
        execute_luanti_14757_bounded_launch(
            tmp_path,
            run_root=tmp_path / "private/bounded-runs/luanti/14757/run-1",
            authorization=_authorization("candidate-screening"),
            confirmation=EXECUTION_CONFIRMATION,
            driver=_Driver({}),
        )


def test_bounded_launch_requires_explicit_confirmation(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=EXECUTION_CONFIRMATION):
        execute_luanti_14757_bounded_launch(
            tmp_path,
            run_root=tmp_path / "private/bounded-runs/luanti/14757/run-1",
            authorization=_authorization(),
            confirmation="not-authorized",
            driver=_Driver({}),
        )


def test_bounded_launch_materializes_independent_worlds_and_typed_receipts(
    tmp_path: Path,
) -> None:
    preflight = _seed_boundary(tmp_path)
    driver = _Driver({"fixed": _observation("fixed"), "buggy": _observation("buggy")})
    run_root = tmp_path / "private/bounded-runs/luanti/14757/run-1"

    result = execute_luanti_14757_bounded_launch(
        tmp_path,
        run_root=run_root,
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        watchdog_seconds=30,
        driver=driver,
        preflight_loader=lambda _root: preflight,
    )

    assert [spec.variant for spec in driver.specs] == ["fixed", "buggy"]
    assert driver.specs[0].run_root != driver.specs[1].run_root
    for spec in driver.specs:
        assert spec.watchdog_seconds == 30
        assert spec.fixture_root.name == "v2"
        assert (spec.world / "worldmods/c1_lazarus/init.lua").is_file()
        assert "gameid = devtest" in (spec.world / "world.mt").read_text()
    assert result.aggregate.payload["state"] == "downstream_pending"
    assert result.aggregate.payload["semantic_pair_observed"] is True
    assert result.aggregate.payload["permanent_terminal_written"] is False
    for attempt in result.attempts:
        assert attempt.payload["start_observation"] == {
            "marker": "basenodes:cobble",
            "result": "",
        }
        assert attempt.payload["action_observation"]["count"] == 1
        raw = tmp_path / attempt.payload["raw_observation_path"]
        assert raw.is_file()


def test_timeout_is_persisted_as_pending_evidence_not_a_terminal(
    tmp_path: Path,
) -> None:
    preflight = _seed_boundary(tmp_path)
    timeout = BoundedLaunchObservation(
        process_outcome="timed_out",
        returncode=None,
        stdout=b"partial",
        stderr=b"",
        start_marker=None,
        start_result=None,
        action_command=None,
        action_count=0,
        semantic_result=None,
    )
    driver = _Driver({"fixed": timeout, "buggy": timeout})

    result = execute_luanti_14757_bounded_launch(
        tmp_path,
        run_root=tmp_path / "private/bounded-runs/luanti/14757/run-timeout",
        authorization=_authorization(),
        confirmation=EXECUTION_CONFIRMATION,
        driver=driver,
        preflight_loader=lambda _root: preflight,
    )

    assert result.aggregate.payload["state"] == "downstream_pending"
    assert result.aggregate.payload["semantic_pair_observed"] is False
    assert result.aggregate.payload["permanent_terminal_written"] is False
    assert all(
        item.payload["process_outcome"] == "timed_out" for item in result.attempts
    )
