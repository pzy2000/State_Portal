from __future__ import annotations

import os
from pathlib import Path

from warptest.c1_lazarus_selection.luanti_15519_runtime_closure import (
    RUNTIME_CONFIRMATION,
    _classify,
    execute_luanti_15519_runtime_closure,
    write_luanti_15519_runtime_pending_plan,
)
from warptest.c1_lazarus_selection.luanti_native_admission import (
    _TASK_FIXTURE_FILES,
)
from warptest.c1_lazarus_selection.luanti_15519_version_materialization import (
    Luanti15519ReplayPlanningBinding,
)
from warptest.c1_lazarus_selection.luanti_native_executor import NativeProcessReceipt
from warptest.c1_lazarus_selection.stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
)


def _authorization() -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id="local-test/luanti-15519-runtime-closure",
        scope="preannotation-execution",
        granted_at="2026-08-14T06:00:00Z",
        local_user_id=os.getuid(),
    )


def _artifact(
    root: Path, artifact_id: str, kind: str, version: str, payload: dict[str, object]
) -> StableArtifact:
    reference = ArtifactReference(artifact_id, kind, version)
    return StableArtifact(
        reference=reference,
        created_at="2026-08-14T06:00:01Z",
        inputs=(),
        payload=payload,
        authorization=_authorization(),
        revision_path=root / "unused.json",
        head_path=root / "unused-head.json",
    )


def _binding(root: Path) -> Luanti15519ReplayPlanningBinding:
    references = {
        "materialization": _artifact(
            root,
            "candidate_materialization/luanti/15519/fixed-buggy/v1",
            "candidate_materialization",
            "1.0",
            {"state": "built"},
        ),
        "server": _artifact(
            root,
            "native_server_closure/luanti/15519/fixed-buggy/v1",
            "native_server_closure",
            "1.0",
            {"semantic_qualified": True},
        ),
        "mapping": _artifact(
            root,
            "lazarus_task_mapping/luanti/15519/issue-15518/v2",
            "lazarus_task_mapping",
            "2.0",
            {"fixture_root": "fixture"},
        ),
    }
    fixed = root / "source-fixed"
    buggy = root / "source-buggy"
    for tree in (fixed, buggy):
        (tree / "bin").mkdir(parents=True)
        binary = tree / "bin/luanti"
        binary.write_bytes(b"registered\n")
        binary.chmod(0o700)
    return Luanti15519ReplayPlanningBinding(
        materialization=references["materialization"],
        server_semantics=references["server"],
        task_mapping=references["mapping"],
        fixed_tree=fixed,
        buggy_tree=buggy,
        fixed_binary=fixed / "bin/luanti",
        buggy_binary=buggy / "bin/luanti",
        remaining_gates=(),
    )


class _Runner:
    def __init__(self, outcomes: list[bytes]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str], int]] = []
        self.configs: list[str] = []

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> NativeProcessReceipt:
        self.calls.append((argv, cwd, env, timeout_seconds))
        config_index = argv.index("--config") + 1
        self.configs.append(Path(argv[config_index]).read_text(encoding="utf-8"))
        return NativeProcessReceipt(
            "timed_out",
            None,
            self.outcomes.pop(0),
            b"",
        )


def test_fixed_buggy_replay_is_sequential_and_visual_remains_pending(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    fixture = tmp_path / "fixture"
    (fixture / "mods/c1_plural").mkdir(parents=True)
    (fixture / "mods/c1_plural/init.lua").write_text("fixture\n", encoding="utf-8")
    runner = _Runner(
        [
            "Server created for gameid c1_plural\nC1 plural fixture loaded: яблоко\n".encode(),
            "Server created for gameid c1_plural\nC1 plural fixture loaded: \x1b(T@c1_plural)apple\x1bE\nInvalid Plural-Forms line: bad\n".encode(),
        ]
    )
    result = execute_luanti_15519_runtime_closure(
        tmp_path,
        run_root=tmp_path / "private/luanti-15519-runtime/v1/run-1",
        authorization=_authorization(),
        confirmation=RUNTIME_CONFIRMATION,
        planning_loader=lambda _root: binding,
        process_runner=runner,
    )
    assert result.state == "replay_passed"
    assert result.executed_attempts == 2
    assert [call[1].name for call in runner.calls] == ["fixed", "buggy"]
    assert [call[3] for call in runner.calls] == [60, 60]
    assert "language = ru\n" in runner.configs[0]
    assert result.replay_stage.payload["state"] == "passed"
    assert result.construction_stage.payload["state"] == "downstream_pending"
    assert (
        result.construction_stage.payload["golden_state"]
        == "pending_null_video_frame_capture"
    )
    assert not (tmp_path / "private/luanti-15519-runtime/v1/run-1/fixed").exists()


def test_plural_fixture_requests_russian_translation_and_classifier_distinguishes(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "mods/c1_plural/init.lua"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(
        'local S = minetest.get_translator("c1_plural")\n'
        'local translated = core.get_translated_string("ru", S("apple"))\n'
        'minetest.log("action", "C1 plural fixture loaded: " .. translated)\n',
        encoding="utf-8",
    )
    source = fixture.read_text(encoding="utf-8")
    assert 'core.get_translated_string("ru", S("apple"))' in source
    assert (
        'core.get_translated_string("ru", S("apple"))\n'
        in _TASK_FIXTURE_FILES["mods/c1_plural/init.lua"]
    )

    fixed = _classify(
        "fixed",
        'Server for gameid="c1_plural"\nC1 plural fixture loaded: яблоко\n',
    )
    buggy = _classify(
        "buggy",
        'Server for gameid="c1_plural"\n'
        "C1 plural fixture loaded: \x1b(T@c1_plural)apple\x1bE\n"
        "Invalid Plural-Forms line: bad\n",
    )
    assert fixed["translated_russian_observed"] is True
    assert fixed["invalid_plural_forms_observed"] is False
    assert fixed["semantic_classification"] == (
        "fixed_goal_without_plural_parser_error"
    )
    assert fixed["semantic_match"] is True
    assert buggy["translated_russian_observed"] is False
    assert buggy["invalid_plural_forms_observed"] is True
    assert buggy["semantic_classification"] == "buggy_defect_plural_parser_error"
    assert buggy["semantic_match"] is True


def test_unknown_start_stops_before_buggy_and_writes_pending(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    fixture = tmp_path / "fixture"
    (fixture / "mods/c1_plural").mkdir(parents=True)
    (fixture / "mods/c1_plural/init.lua").write_text("fixture\n", encoding="utf-8")
    runner = _Runner([b"no start marker\n"])
    result = execute_luanti_15519_runtime_closure(
        tmp_path,
        run_root=tmp_path / "private/luanti-15519-runtime/v1/run-2",
        authorization=_authorization(),
        confirmation=RUNTIME_CONFIRMATION,
        planning_loader=lambda _root: binding,
        process_runner=runner,
    )
    assert result.state == "pending"
    assert result.executed_attempts == 1
    assert len(runner.calls) == 1
    assert result.replay_stage.payload["state"] == "downstream_pending"


def test_pending_plan_is_readiness_only_and_does_not_launch(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    plan = write_luanti_15519_runtime_pending_plan(
        tmp_path,
        authorization=_authorization(),
        planning_loader=lambda _root: binding,
    )
    assert plan.payload["pair_state"] == "complete_persistent_pair"
    assert plan.payload["client_launch_performed"] is False
    assert plan.payload["candidate_execution_performed"] is False
    assert [item["gate"] for item in plan.payload["gate_statuses"]] == [
        8,
        9,
        10,
        11,
        12,
        13,
    ]
    assert plan.payload["gate_statuses"][0]["state"] == "ready_persistent_pair"
    assert (
        plan.payload["gate_statuses"][3]["state"] == "blocked_null_video_has_no_frames"
    )
