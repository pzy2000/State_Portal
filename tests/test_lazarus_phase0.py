from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from warptest import lazarus_phase0, lazarus_runtime
from warptest.lazarus_cell_contract import (
    validate_luanti_agent_s_snapshot_binding,
)
from warptest.lazarus_calibration import (
    build_lazarus_calibration,
    lazarus_backend_binding_from_artifact,
)
from warptest.lazarus_orchestrator import (
    DEFAULT_LAZARUS_MODEL,
    LazarusTrialKey,
)
from warptest.model_request import (
    MODEL_REQUEST_PROFILES_ENV,
    resolve_model_request_profile,
)
from warptest.lazarus_phase0 import (
    AgentVisibleSource,
    LazarusPhase0Preflight,
    build_lazarus_phase0_preflight,
    check_lazarus_phase0_unity_editors,
    launch_lazarus_phase0,
    prepare_lazarus_agent_source,
    run_lazarus_phase0,
    write_lazarus_phase0_preflight,
    write_lazarus_phase0_run,
)
from warptest.lazarus_versions import LazarusVersionArtifact, LazarusVersionPair


TASKS = (
    {
        "id": "lz-luanti-pilot",
        "benchmark": "luanti",
        "defect": {"defect_locus": "at_goal"},
    },
    {
        "id": "lz-openra-pilot",
        "benchmark": "openra",
        "defect": {"defect_locus": "on_path"},
    },
)


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def _source_repo(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "config", "user.name", "Test")
    (source / "README.md").write_text("ordinary source\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "initial")
    return source, _git(source, "rev-parse", "HEAD")


def test_phase0_binds_agent_s_p2p_to_the_achieved_start_snapshot() -> None:
    snapshot_sha256 = "a" * 64
    report = {
        "runtime": {
            "trace": {
                "execution": {"start_snapshot_sha256": snapshot_sha256}
            }
        },
        "per_trial_p2p_evidence": {
            "checkpoint_restore": {
                "state_restore": {"start_snapshot_sha256": snapshot_sha256}
            }
        },
    }

    assert validate_luanti_agent_s_snapshot_binding(report) is None
    report["per_trial_p2p_evidence"]["checkpoint_restore"]["state_restore"][
        "start_snapshot_sha256"
    ] = "b" * 64
    assert (
        validate_luanti_agent_s_snapshot_binding(report)
        == "Agent-S Start snapshot provenance drift"
    )


@pytest.fixture
def _allow_generic_runtime_support(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep generic coordinator tests separate from the real Phase-0 adapter map."""

    monkeypatch.setattr(
        "warptest.lazarus_runtime.validate_lazarus_phase0_runtime_support",
        lambda _cells: None,
    )


def test_prepare_agent_source_moves_git_and_rejects_private_overlay_tokens(
    tmp_path: Path,
) -> None:
    source, revision = _source_repo(tmp_path)
    destination = tmp_path / "agent-view"
    trusted = tmp_path / "trusted.git"

    view = prepare_lazarus_agent_source(
        task_id="lz-openra-pilot",
        source_repo=source,
        source_revision=revision,
        destination=destination,
        trusted_git_dir=trusted,
        forbidden_tokens=("WARPTEST_OPENRA_HOTKEY_AUDIT_PATH",),
    )

    assert isinstance(view, AgentVisibleSource)
    assert view.root == destination.resolve()
    assert not (destination / ".git").exists()
    assert trusted.exists()
    assert view.version_identity_hidden is True

    leaked = tmp_path / "leaked"
    with pytest.raises(ValueError, match="forbidden token"):
        prepare_lazarus_agent_source(
            task_id="lz-openra-pilot",
            source_repo=source,
            source_revision=revision,
            destination=leaked,
            trusted_git_dir=tmp_path / "leaked.git",
            forbidden_tokens=("ordinary source",),
        )


def test_prepare_agent_source_removes_git_ignored_overlays(tmp_path: Path) -> None:
    source, _ = _source_repo(tmp_path)
    (source / ".gitignore").write_text("runtime-overlay/\n", encoding="utf-8")
    _git(source, "add", ".gitignore")
    _git(source, "commit", "-m", "ignore runtime overlay")
    overlay = source / "runtime-overlay"
    overlay.mkdir()
    (overlay / "private.lua").write_text(
        "hidden goal and defect outcomes\n",
        encoding="utf-8",
    )

    destination = tmp_path / "agent-view"
    view = prepare_lazarus_agent_source(
        task_id="lz-luanti-pilot",
        source_repo=source,
        source_revision=_git(source, "rev-parse", "HEAD"),
        destination=destination,
        trusted_git_dir=tmp_path / "trusted.git",
        forbidden_tokens=(),
    )

    assert view.root == destination.resolve()
    assert (destination / "README.md").read_text(encoding="utf-8") == "ordinary source\n"
    assert not (destination / "runtime-overlay").exists()


def test_prepare_agent_source_rejects_a_symlink_that_escapes_its_root(
    tmp_path: Path,
) -> None:
    source, _ = _source_repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (source / "escape").symlink_to(outside)
    _git(source, "add", "escape")
    _git(source, "commit", "-m", "add symlink")

    with pytest.raises(ValueError, match="symlink escapes"):
        prepare_lazarus_agent_source(
            task_id="lz-openra-pilot",
            source_repo=source,
            source_revision=_git(source, "rev-parse", "HEAD"),
            destination=tmp_path / "agent-view",
            trusted_git_dir=tmp_path / "trusted.git",
            forbidden_tokens=(),
        )


def _artifact(
    tmp_path: Path,
    *,
    task_id: str,
    benchmark: str,
    version: str,
    ordinal: int,
) -> LazarusVersionArtifact:
    root = tmp_path / task_id / version
    binary = root / "bin" / f"{benchmark}.bin"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(f"{task_id}:{version}".encode("utf-8"))
    return LazarusVersionArtifact(
        task_id=task_id,
        version=version,
        root=root,
        git_metadata=tmp_path / f"{task_id}-{version}.git",
        source_revision=f"{ordinal + 1:040x}",
        patch_sha256=f"{ordinal + 11:064x}",
        tree_sha256=f"{ordinal + 21:064x}",
        changed_files=() if version == "fixed" else ("source.txt",),
        build_identity={"status": "passed"},
        cache_hit=True,
    )


def _pairs(tmp_path: Path) -> dict[str, LazarusVersionPair]:
    pairs: dict[str, LazarusVersionPair] = {}
    for ordinal, task in enumerate(TASKS):
        task_id = str(task["id"])
        benchmark = str(task["benchmark"])
        pairs[task_id] = LazarusVersionPair(
            buggy=_artifact(
                tmp_path,
                task_id=task_id,
                benchmark=benchmark,
                version="buggy",
                ordinal=ordinal * 10,
            ),
            fixed=_artifact(
                tmp_path,
                task_id=task_id,
                benchmark=benchmark,
                version="fixed",
                ordinal=ordinal * 10 + 1,
            ),
            version_switch_ms=12.5,
        )
    return pairs


def _calibration(pairs: dict[str, LazarusVersionPair]) -> dict:
    bindings = []
    for task in TASKS:
        task_id = str(task["id"])
        benchmark = str(task["benchmark"])
        for artifact in (pairs[task_id].buggy, pairs[task_id].fixed):
            bindings.append(
                lazarus_backend_binding_from_artifact(
                    artifact,
                    benchmark=benchmark,
                    capture_backend=f"{benchmark}_capture_v1",
                    input_backend=f"{benchmark}_input_v1",
                    binary_paths=(f"bin/{benchmark}.bin",),
                    canary_passed=True,
                )
            )
    return build_lazarus_calibration(
        bindings,
        manifest_sha256="a" * 64,
        harness_sha256="b" * 64,
        provider_identity_sha256="c" * 64,
        observer_model="claude-sonnet-5@relay",
        grounder_model=DEFAULT_LAZARUS_MODEL,
        judge_model="qwen3-vl-flash@relay",
    )


def _gate(
    task_id: str,
    pair: LazarusVersionPair,
    *,
    p2p_wrapper_duration_s: float = 2.0,
    p2p_command_duration_s: float | None = None,
    golden_metadata_sha256: str = "f" * 64,
) -> dict:
    command_duration_s = (
        p2p_wrapper_duration_s
        if p2p_command_duration_s is None
        else p2p_command_duration_s
    )
    return {
        "schema_version": 1,
        "task_id": task_id,
        "status": "passed",
        "passed": True,
        "complete": True,
        "checks": {
            "fixed_f2p": True,
            "buggy_f2p": False,
            "fixed_p2p": True,
            "buggy_p2p": True,
            "provenance_verified": True,
            "fixed_before_semantic": True,
            "buggy_before_semantic": True,
            "fixed_before_visual": True,
            "buggy_before_visual": True,
            "before_equivalent": True,
            "fixed_goal_checks": True,
            "fixed_after_visual": True,
            "buggy_defect_signature": True,
            "buggy_after_visual": True,
            "fixed_defect_signature": False,
            "fixed_invariants": True,
            "buggy_invariants": True,
        },
        "evidence": {
            "goldens": {"metadata_sha256": golden_metadata_sha256},
            "versions": {
                "buggy": {
                    "tree_sha256": pair.buggy.tree_sha256,
                    "patch_sha256": pair.buggy.patch_sha256,
                },
                "fixed": {
                    "tree_sha256": pair.fixed.tree_sha256,
                    "patch_sha256": pair.fixed.patch_sha256,
                },
            },
            "upstream": {
                version: {
                    "complete": True,
                    "passed": True,
                    "isolation": "apfs_cow_clone",
                    "duration_ms": p2p_wrapper_duration_s * 1000,
                    "results": [
                        {
                            "id": f"{task_id}-p2p",
                            "passed": True,
                            "timed_out": False,
                            "duration_ms": command_duration_s * 1000,
                        }
                    ]
                }
                for version in ("buggy", "fixed")
            },
        },
    }


def _preflight(
    tmp_path: Path,
    *,
    p2p_wrapper_duration_s: float = 2.0,
    p2p_command_duration_s: float | None = None,
    gate_golden_metadata_sha256: str = "f" * 64,
    unity_editors: bool = True,
) -> LazarusPhase0Preflight:
    pairs = _pairs(tmp_path)
    agent_views = {
        str(task["id"]): AgentVisibleSource(
            task_id=str(task["id"]),
            root=tmp_path / "agent" / str(task["id"]),
            trusted_git_dir=tmp_path / "trusted" / str(task["id"]),
            source_revision="d" * 40,
            source_tree_sha256="e" * 64,
            sanitized_tree_sha256="f" * 64,
            forbidden_tokens=(),
            version_identity_hidden=True,
        )
        for task in TASKS
    }
    return build_lazarus_phase0_preflight(
        tasks=TASKS,
        pairs=pairs,
        calibration=_calibration(pairs),
        manifest_sha256="a" * 64,
        harness_sha256="b" * 64,
        provider_identity_sha256="c" * 64,
        backend_paths={
            "luanti": ("bin/luanti.bin",),
            "openra": ("bin/openra.bin",),
        },
        capture_backends={
            "luanti": "luanti_capture_v1",
            "openra": "openra_capture_v1",
        },
        input_backends={
            "luanti": "luanti_input_v1",
            "openra": "openra_input_v1",
        },
        gate_reports={
            task_id: _gate(
                task_id,
                pair,
                p2p_wrapper_duration_s=p2p_wrapper_duration_s,
                p2p_command_duration_s=p2p_command_duration_s,
                golden_metadata_sha256=gate_golden_metadata_sha256,
            )
            for task_id, pair in pairs.items()
        },
        per_trial_p2p_ids={
            str(task["id"]): (f"{task['id']}-p2p",) for task in TASKS
        },
        agent_sources=agent_views,
        task_evidence={
            str(task["id"]): {
                "oracle_spec_sha256": "d" * 64,
                "upstream_evidence_sha256": "e" * 64,
                "golden_metadata_sha256": "f" * 64,
            }
            for task in TASKS
        },
        environment={
            "apfs": True,
            "submodules_clean": True,
            "capacity_ok": True,
            "unity_editors": unity_editors,
        },
    )


def test_phase0_preflight_binds_gate_calibration_and_p2p_budget(
    tmp_path: Path,
) -> None:
    preflight = _preflight(tmp_path)

    assert preflight.complete is True
    assert preflight.plan.complete is True
    assert preflight.plan.trial_count == 8
    assert preflight.p2p_p95_s == pytest.approx(2.0)
    assert set(preflight.expected_provenance) == {
        (str(task["id"]), version)
        for task in TASKS
        for version in ("buggy", "fixed")
    }
    report = preflight.to_report()
    assert report["schema"] == "c1-lazarus-phase0-preflight-v1"
    assert report["complete"] is True
    assert report["expected_cells"] == 8
    assert "tasks" not in report
    assert "defect" not in json.dumps(report, sort_keys=True)

    destination = write_lazarus_phase0_preflight(
        preflight,
        tmp_path / "reports" / "phase0-preflight.json",
    )
    persisted = json.loads(destination.read_text(encoding="utf-8"))
    assert persisted == report


def test_phase0_preflight_uses_full_wrapper_duration_for_p2p_p95(
    tmp_path: Path,
) -> None:
    preflight = _preflight(
        tmp_path,
        p2p_wrapper_duration_s=301.0,
        p2p_command_duration_s=2.0,
    )

    assert preflight.complete is False
    assert preflight.p2p_p95_s == pytest.approx(301.0)
    assert any("P95" in value for value in preflight.failures)


def test_phase0_preflight_fails_closed_on_calibration_drift_or_slow_p2p(
    tmp_path: Path,
) -> None:
    pairs = _pairs(tmp_path)
    calibration = _calibration(pairs)
    calibration["harness_sha256"] = "0" * 64
    calibration["artifact_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in calibration.items() if key != "artifact_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    agent_views = {
        str(task["id"]): AgentVisibleSource(
            task_id=str(task["id"]),
            root=tmp_path / "agent" / str(task["id"]),
            trusted_git_dir=tmp_path / "trusted" / str(task["id"]),
            source_revision="d" * 40,
            source_tree_sha256="e" * 64,
            sanitized_tree_sha256="f" * 64,
            forbidden_tokens=(),
            version_identity_hidden=True,
        )
        for task in TASKS
    }
    preflight = build_lazarus_phase0_preflight(
        tasks=TASKS,
        pairs=pairs,
        calibration=calibration,
        manifest_sha256="a" * 64,
        harness_sha256="b" * 64,
        provider_identity_sha256="c" * 64,
        backend_paths={"luanti": ("bin/luanti.bin",), "openra": ("bin/openra.bin",)},
        capture_backends={"luanti": "luanti_capture_v1", "openra": "openra_capture_v1"},
        input_backends={"luanti": "luanti_input_v1", "openra": "openra_input_v1"},
        gate_reports={
            task_id: _gate(
                task_id,
                pair,
                p2p_wrapper_duration_s=301.0,
                p2p_command_duration_s=2.0,
            )
            for task_id, pair in pairs.items()
        },
        per_trial_p2p_ids={str(task["id"]): (f"{task['id']}-p2p",) for task in TASKS},
        agent_sources=agent_views,
        task_evidence={
            str(task["id"]): {
                "oracle_spec_sha256": "d" * 64,
                "upstream_evidence_sha256": "e" * 64,
                "golden_metadata_sha256": "f" * 64,
            }
            for task in TASKS
        },
        environment={"apfs": True, "submodules_clean": True, "capacity_ok": True},
    )

    assert preflight.complete is False
    assert any("harness_sha256 drift" in value for value in preflight.failures)
    assert any("P95" in value for value in preflight.failures)


def test_phase0_preflight_rejects_a_gate_bound_to_replaced_golden_metadata(
    tmp_path: Path,
) -> None:
    preflight = _preflight(tmp_path, gate_golden_metadata_sha256="0" * 64)

    assert preflight.complete is False
    assert any("golden metadata provenance drift" in item for item in preflight.failures)


def test_phase0_preflight_requires_the_registered_unity_editors(
    tmp_path: Path,
) -> None:
    preflight = _preflight(tmp_path, unity_editors=False)

    assert preflight.complete is False
    assert "Phase 0 environment check failed: unity_editors" in preflight.failures


def test_phase0_unity_editor_check_requires_each_pinned_project_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for relative, version in (
        ("games/jynew/jyx2", "2020.3.32f1c1"),
        ("games/Pal3.Unity", "6000.1.10f1"),
    ):
        version_file = tmp_path / relative / "ProjectSettings" / "ProjectVersion.txt"
        version_file.parent.mkdir(parents=True)
        version_file.write_text(f"m_EditorVersion: {version}\n", encoding="utf-8")
    editor_2020 = tmp_path / "Unity-2020"
    editor_6000 = tmp_path / "Unity-6000"
    for editor in (editor_2020, editor_6000):
        editor.write_text("editor\n", encoding="utf-8")
        editor.chmod(0o755)

    monkeypatch.setattr(
        lazarus_phase0,
        "_unity_editor_candidates",
        lambda version: (editor_2020, editor_6000),
    )
    monkeypatch.setattr(
        lazarus_phase0,
        "_unity_editor_version",
        lambda editor: "2020.3.32f1c1" if editor == editor_2020 else "6000.1.10f1",
    )

    available = check_lazarus_phase0_unity_editors(tmp_path)
    assert available["complete"] is True
    assert [row["editor_version"] for row in available["editors"]] == [
        "2020.3.32f1c1",
        "6000.1.10f1",
    ]

    monkeypatch.setattr(
        lazarus_phase0,
        "_unity_editor_version",
        lambda _editor: "2020.3.32f1c1",
    )
    unavailable = check_lazarus_phase0_unity_editors(tmp_path)
    assert unavailable["complete"] is False
    assert unavailable["editors"][1]["available"] is False


def _cell_report(context, *, complete: bool = True) -> dict:
    key = context.key
    semantic_goal = key.version == "fixed"
    semantic_defect = key.version == "buggy"
    grading_result = {
        "version": key.version,
        "start_success": True,
        "goal_success": semantic_goal,
        "defect_exposed": semantic_defect,
        "invariants_ok": True,
        "per_trial_p2p_ok": True,
        "false_alarm": False,
        "complete": True,
        "after_statuses": {"goal": "evaluated", "defect": "evaluated"},
        "failures": [],
    }
    checkpoint_kind = (
        "semantic_state"
        if key.arm == "lazarus_state"
        else "agent_s_from_start"
    )
    checkpoint_payload = {
        "schema": "c1-lazarus-p2p-checkpoint-restore-v1",
        "schema_version": 1,
        "checkpoint_sha256": "a" * 64,
        "checkpoint_kind": checkpoint_kind,
        "runtime_restored": True,
    }
    checkpoint_bytes = (
        json.dumps(
            checkpoint_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    receipt_sha256 = hashlib.sha256(checkpoint_bytes).hexdigest()
    return {
        "task_id": key.task_id,
        "arm": key.arm,
        "model": key.model,
        "seed": key.seed,
        "version": key.version,
        "defect_locus": context.defect_locus,
        "start_success": True,
        "goal_success": semantic_goal,
        "defect_exposed": semantic_defect,
        "invariants_ok": True,
        "per_trial_p2p_ok": True,
        "false_alarm": False,
        "complete": complete,
        "provenance_valid": True,
        "provenance_sha256": context.artifact.tree_sha256,
        "treatment_tokens": 0 if key.arm == "lazarus_state" and key.version == "fixed" else 7,
        "treatment_time_ms": 4,
        "version_switch_ms": context.version_switch_ms,
        "checkpoint_sha256": "a" * 64,
        "actual_synthesis_calls": (
            1 if key.arm == "lazarus_state" and key.version == "buggy" else 0
        ),
        "synthesis_cache_hit": key.arm == "lazarus_state" and key.version == "fixed",
        "checkpoint_reuse_success": key.arm == "lazarus_state" and key.version == "fixed",
        "runtime": {
            "observed": True,
            "trace": (
                {
                    "observer": {
                        "model": "claude-sonnet-5@relay",
                        "usage": "registered_not_consumed_native_screenshot",
                        "llm_calls": 0,
                    }
                }
                if key.arm == "lazarus_agent_s"
                else {}
            ),
        },
        "grading": {
            "schema_version": 1,
            "evidence": {
                "version": key.version,
                "before_semantic_ok": True,
                "before_visual_ok": True,
                "goal_checks_ok": semantic_goal,
                "defect_signature_ok": semantic_defect,
                "invariants_ok": True,
                "fixed_after_visual": {
                    "ok": semantic_goal,
                    "screenshot_sha256": "b" * 64,
                    "oracle_sha256": "c" * 64,
                },
                "buggy_after_visual": {
                    "ok": semantic_defect,
                    "screenshot_sha256": "b" * 64,
                    "oracle_sha256": "d" * 64,
                },
                "per_trial_p2p_ok": True,
            },
            "result": grading_result,
        },
        "per_trial_p2p_evidence": {
            "schema_version": 1,
            "task_id": key.task_id,
            "version": key.version,
            "completed": True,
            "timeout_s": 300,
            "duration_ms": 2000,
            "checkpoint_restore": {
                "schema_version": 1,
                "applied": True,
                "checkpoint_sha256": "a" * 64,
                "checkpoint_kind": checkpoint_kind,
                "runtime_restored": True,
                "path": ".warptest/lazarus/checkpoint-restore.json",
                "payload_sha256": receipt_sha256,
                "state_restore": {
                    "schema_version": 1,
                    "state_restore_applied": True,
                    "adapter": "test-isolated-checkpoint-restorer-v1",
                    "checkpoint_sha256": "a" * 64,
                    "checkpoint_kind": checkpoint_kind,
                    "receipt_sha256": receipt_sha256,
                },
            },
            "results": [
                {
                    "id": test_id,
                    "passed": True,
                    "timed_out": False,
                    "duration_ms": 2000,
                }
                for test_id in context.per_trial_p2p_ids
            ],
        },
    }


def test_phase0_runner_stops_when_observed_p2p_p95_exceeds_the_budget(
    tmp_path: Path, _allow_generic_runtime_support: None
) -> None:
    preflight = _preflight(tmp_path)

    def executor(context):
        row = _cell_report(context)
        row["per_trial_p2p_evidence"]["duration_ms"] = 300_001
        return row

    outcome = run_lazarus_phase0(
        preflight,
        executor=executor,
        rng=random.Random(1234),
        bootstrap_iterations=20,
    )

    assert outcome.complete is False
    assert outcome.stopped_early is False
    assert len(outcome.reports) == 8
    assert "observed per-trial P2P P95 exceeds 300 seconds" in str(outcome.stop_reason)
    report = outcome.to_report(preflight)
    assert report["observed_p2p_p95_s"] == pytest.approx(300.001)


def test_phase0_runner_refuses_fake_visual_judging_without_using_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _allow_generic_runtime_support: None,
) -> None:
    preflight = _preflight(tmp_path)
    monkeypatch.setenv("WARPTEST_VISUAL_JUDGE_FAKE", "test-only")

    outcome = run_lazarus_phase0(
        preflight,
        executor=lambda _context: pytest.fail("executor must not run"),
        rng=random.Random(1234),
        bootstrap_iterations=20,
    )

    assert outcome.complete is False
    assert outcome.stopped_early is True
    assert outcome.stop_reason == "Phase 0 cannot run with a fake visual judge"


def test_phase0_runner_rejects_p2p_evidence_without_a_bound_checkpoint_restore(
    tmp_path: Path, _allow_generic_runtime_support: None
) -> None:
    preflight = _preflight(tmp_path)

    def executor(context):
        row = _cell_report(context)
        row["per_trial_p2p_evidence"].pop("checkpoint_restore")
        return row

    outcome = run_lazarus_phase0(
        preflight,
        executor=executor,
        rng=random.Random(1234),
        bootstrap_iterations=20,
    )

    assert outcome.complete is False
    assert outcome.stopped_early is True
    assert "checkpoint restore evidence" in str(outcome.stop_reason)


def test_phase0_runner_rejects_version_switch_measurement_drift(
    tmp_path: Path, _allow_generic_runtime_support: None
) -> None:
    preflight = _preflight(tmp_path)

    def executor(context):
        row = _cell_report(context)
        row["version_switch_ms"] = context.version_switch_ms + 1
        return row

    outcome = run_lazarus_phase0(
        preflight,
        executor=executor,
        rng=random.Random(1234),
        bootstrap_iterations=20,
    )

    assert outcome.complete is False
    assert outcome.stopped_early is True
    assert "version_switch_ms drift" in str(outcome.stop_reason)


def test_phase0_runner_rejects_missing_agent_s_observer_usage_evidence(
    tmp_path: Path, _allow_generic_runtime_support: None
) -> None:
    preflight = _preflight(tmp_path)

    def executor(context):
        row = _cell_report(context)
        if context.key.arm == "lazarus_agent_s":
            row["runtime"]["trace"].pop("observer")
        return row

    outcome = run_lazarus_phase0(
        preflight,
        executor=executor,
        rng=random.Random(1234),
        bootstrap_iterations=20,
    )

    assert outcome.complete is False
    assert outcome.stopped_early is True
    assert "observer evidence" in str(outcome.stop_reason)


def test_phase0_runner_is_serial_reuses_state_checkpoint_and_stops_on_incomplete(
    tmp_path: Path, _allow_generic_runtime_support: None
) -> None:
    preflight = _preflight(tmp_path)
    seen: list[LazarusTrialKey] = []

    def executor(context):
        seen.append(context.key)
        return _cell_report(context, complete=len(seen) < 3)

    outcome = run_lazarus_phase0(
        preflight,
        executor=executor,
        rng=random.Random(1234),
        bootstrap_iterations=20,
    )

    assert outcome.complete is False
    assert outcome.stopped_early is True
    assert len(seen) == 3
    assert "incomplete" in outcome.stop_reason


def test_phase0_runner_rejects_fixed_state_synthesis_or_false_alarm(
    tmp_path: Path, _allow_generic_runtime_support: None
) -> None:
    preflight = _preflight(tmp_path)

    def executor(context):
        row = _cell_report(context)
        if context.key.arm == "lazarus_state" and context.key.version == "fixed":
            row["actual_synthesis_calls"] = 1
        return row

    outcome = run_lazarus_phase0(
        preflight,
        executor=executor,
        rng=random.Random(1234),
        bootstrap_iterations=20,
    )

    assert outcome.complete is False
    assert outcome.stopped_early is True
    assert "zero actual synthesis" in outcome.stop_reason


def test_phase0_runner_rejects_a_self_report_without_independent_grading_evidence(
    tmp_path: Path, _allow_generic_runtime_support: None
) -> None:
    preflight = _preflight(tmp_path)

    def executor(context):
        row = _cell_report(context)
        row.pop("grading", None)
        return row

    outcome = run_lazarus_phase0(
        preflight,
        executor=executor,
        rng=random.Random(1234),
        bootstrap_iterations=20,
    )

    assert outcome.complete is False
    assert outcome.stopped_early is True
    assert "independent grading evidence" in outcome.stop_reason


def test_phase0_run_report_is_atomic_and_keeps_task_payload_out_of_coordinator_record(
    tmp_path: Path, _allow_generic_runtime_support: None
) -> None:
    preflight = _preflight(tmp_path)

    outcome = run_lazarus_phase0(
        preflight,
        executor=_cell_report,
        rng=random.Random(1234),
        bootstrap_iterations=20,
    )
    destination = write_lazarus_phase0_run(
        outcome,
        preflight,
        tmp_path / "reports" / "phase0-run.json",
    )

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["schema"] == "c1-lazarus-phase0-run-v1"
    assert payload["complete"] is True
    assert payload["expected_cells"] == 8
    assert payload["observed_cells"] == 8
    assert "tasks" not in payload
    encoded = json.dumps(payload, sort_keys=True)
    assert "fix_commit" not in encoded
    assert "revert_patch" not in encoded
    assert "upstream_record" not in encoded


def test_phase0_runner_blocks_an_unsupported_population_before_executor(
    tmp_path: Path,
) -> None:
    """No earlier Luanti cell may spend a provider call before H2 support is known."""

    preflight = _preflight(tmp_path)
    dispatched: list[LazarusTrialKey] = []

    def executor(context):
        dispatched.append(context.key)
        return _cell_report(context)

    outcome = run_lazarus_phase0(
        preflight,
        executor=executor,
        rng=random.Random(1234),
        bootstrap_iterations=20,
    )

    assert outcome.complete is False
    assert outcome.stopped_early is True
    assert dispatched == []
    assert "runtime support" in str(outcome.stop_reason).lower()


def test_phase0_launch_builds_the_dedicated_runtime_only_after_authorized_preflight(
    tmp_path: Path, _allow_generic_runtime_support: None
) -> None:
    preflight = replace(
        _preflight(tmp_path),
        contract=SimpleNamespace(
            phase0_paid_pilot_authorized=True,
            formal_paid_run_authorized=False,
            phase0_trial_count=8,
            provider_timeout_s=900.0,
            model=DEFAULT_LAZARUS_MODEL,
            observer_model="claude-sonnet-5@relay",
        ),
    )
    calls: list[str] = []

    def runtime_factory(workspace: Path):
        calls.append(f"runtime:{workspace.name}")
        return object()

    def executor_factory(workspace: Path, *, runtime_runner):
        assert runtime_runner is not None
        calls.append(f"executor:{workspace.name}")
        return _cell_report

    launch = launch_lazarus_phase0(
        tmp_path,
        report_path=tmp_path / "reports" / "phase0-run.json",
        preflight_factory=lambda _workspace: preflight,
        runtime_factory=runtime_factory,
        executor_factory=executor_factory,
        rng=random.Random(1234),
    )

    assert calls == [f"runtime:{tmp_path.name}", f"executor:{tmp_path.name}"]
    assert launch.run.complete is True
    assert launch.report_path.is_file()


def test_phase0_launch_applies_thinking_disabled_to_the_runtime_and_restores_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _allow_generic_runtime_support: None,
) -> None:
    preflight = replace(
        _preflight(tmp_path),
        contract=SimpleNamespace(
            phase0_paid_pilot_authorized=True,
            formal_paid_run_authorized=False,
            phase0_trial_count=8,
            provider_timeout_s=900.0,
            model=DEFAULT_LAZARUS_MODEL,
            observer_model="claude-sonnet-5@relay",
        ),
    )
    monkeypatch.setenv(
        MODEL_REQUEST_PROFILES_ENV,
        json.dumps({"gpt-5.6-terra": "provider_default"}),
    )

    def runtime_factory(_workspace: Path):
        assert (
            resolve_model_request_profile(DEFAULT_LAZARUS_MODEL).profile
            == "thinking_disabled"
        )
        return object()

    launch = launch_lazarus_phase0(
        tmp_path,
        report_path=tmp_path / "reports" / "phase0-profile.json",
        preflight_factory=lambda _workspace: preflight,
        runtime_factory=runtime_factory,
        executor_factory=lambda _workspace, *, runtime_runner: _cell_report,
        rng=random.Random(1234),
    )

    assert launch.run.complete is True
    assert (
        resolve_model_request_profile(DEFAULT_LAZARUS_MODEL).profile
        == "provider_default"
    )


def test_phase0_launch_scopes_relay_credentials_and_revalidates_the_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _allow_generic_runtime_support: None,
) -> None:
    preflight = replace(
        _preflight(tmp_path),
        contract=SimpleNamespace(
            phase0_paid_pilot_authorized=True,
            formal_paid_run_authorized=False,
            phase0_trial_count=8,
            provider_timeout_s=900.0,
            model=DEFAULT_LAZARUS_MODEL,
            observer_model="claude-sonnet-5@relay",
            grounder_model=DEFAULT_LAZARUS_MODEL,
            judge_model="qwen3-vl-flash@relay",
        ),
    )
    credentials = tmp_path / "credentials.py"
    credentials.write_text(
        'SECRET = "test-secret"\n'
        'BASE = "https://relay.example.invalid/v1"\n'
        'END_USER = "phase0-test"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.setattr(
        lazarus_phase0,
        "_phase0_provider_identity_from_contract",
        lambda _contract: preflight.provider_identity_sha256,
    )

    def runtime_factory(_workspace: Path):
        assert os.environ["RELAY_API_KEY"] == "test-secret"
        assert os.environ["RELAY_BASE_URL"] == "https://relay.example.invalid/v1"
        return object()

    def preflight_factory(_workspace: Path):
        assert os.environ["RELAY_API_KEY"] == "test-secret"
        return preflight

    launch = launch_lazarus_phase0(
        tmp_path,
        report_path=tmp_path / "reports" / "phase0-credentials.json",
        preflight_factory=preflight_factory,
        runtime_factory=runtime_factory,
        executor_factory=lambda _workspace, *, runtime_runner: _cell_report,
        credential_source=credentials,
        rng=random.Random(1234),
    )

    assert launch.run.complete is True
    assert "RELAY_API_KEY" not in os.environ
    assert "RELAY_BASE_URL" not in os.environ


def test_phase0_provider_identity_reconstructs_the_manifest_scalar_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def identity(payload):
        captured.update(payload)
        return "a" * 64

    monkeypatch.setattr(lazarus_phase0, "lazarus_provider_identity_sha256", identity)
    contract = SimpleNamespace(
        model=DEFAULT_LAZARUS_MODEL,
        observer_model="claude-sonnet-5@relay",
        judge_model="qwen3-vl-flash@relay",
        provider_timeout_s=900.0,
    )

    assert lazarus_phase0._phase0_provider_identity_from_contract(contract) == "a" * 64
    assert captured["provider_timeout_s"] == 900
    assert type(captured["provider_timeout_s"]) is int


def test_phase0_default_launcher_uses_a_fresh_runtime_artifact_root_per_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _allow_generic_runtime_support: None,
) -> None:
    preflight = replace(
        _preflight(tmp_path),
        contract=SimpleNamespace(
            phase0_paid_pilot_authorized=True,
            formal_paid_run_authorized=False,
            phase0_trial_count=8,
            provider_timeout_s=900.0,
            model=DEFAULT_LAZARUS_MODEL,
            observer_model="claude-sonnet-5@relay",
        ),
    )
    roots: list[Path] = []

    def runtime_factory(
        _workspace: Path,
        *,
        runtime_root: Path,
        observer_model: str,
    ):
        assert observer_model == "claude-sonnet-5@relay"
        roots.append(Path(runtime_root))
        return object()

    monkeypatch.setattr(
        lazarus_runtime,
        "build_lazarus_phase0_runtime_runner",
        runtime_factory,
    )
    for attempt in (1, 2):
        launch_lazarus_phase0(
            tmp_path,
            report_path=tmp_path / "reports" / f"phase0-{attempt}.json",
            preflight_factory=lambda _workspace: preflight,
            executor_factory=lambda _workspace, *, runtime_runner: _cell_report,
            rng=random.Random(1234),
        )

    assert len(roots) == 2
    assert roots[0] != roots[1]
    assert all(root.parent.name == "runs" for root in roots)


def test_phase0_launch_refuses_an_unauthorized_manifest_before_runtime_construction(
    tmp_path: Path,
) -> None:
    preflight = replace(
        _preflight(tmp_path),
        contract=SimpleNamespace(
            phase0_paid_pilot_authorized=False,
            formal_paid_run_authorized=False,
            phase0_trial_count=8,
            provider_timeout_s=900.0,
            model=DEFAULT_LAZARUS_MODEL,
            observer_model="claude-sonnet-5@relay",
        ),
    )
    runtime_called = False

    def runtime_factory(_workspace: Path):
        nonlocal runtime_called
        runtime_called = True
        raise AssertionError("unauthorized launch must not construct a runtime")

    launch = launch_lazarus_phase0(
        tmp_path,
        report_path=tmp_path / "reports" / "unauthorized.json",
        preflight_factory=lambda _workspace: preflight,
        runtime_factory=runtime_factory,
        executor_factory=lambda *_args, **_kwargs: _cell_report,
        rng=random.Random(1234),
    )

    assert runtime_called is False
    assert launch.run.complete is False
    assert launch.run.stopped_early is True
    assert "authorization" in str(launch.run.stop_reason).lower()
    assert launch.report_path.is_file()


def test_phase0_launch_refuses_a_fake_visual_judge_before_runtime_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _allow_generic_runtime_support: None,
) -> None:
    preflight = replace(
        _preflight(tmp_path),
        contract=SimpleNamespace(
            phase0_paid_pilot_authorized=True,
            formal_paid_run_authorized=False,
            phase0_trial_count=8,
            provider_timeout_s=900.0,
            model=DEFAULT_LAZARUS_MODEL,
            observer_model="claude-sonnet-5@relay",
        ),
    )
    runtime_called = False

    def runtime_factory(_workspace: Path):
        nonlocal runtime_called
        runtime_called = True
        raise AssertionError("fake visual judge must stop before runtime construction")

    monkeypatch.setenv("WARPTEST_VISUAL_JUDGE_FAKE", "correct")
    launch = launch_lazarus_phase0(
        tmp_path,
        report_path=tmp_path / "reports" / "fake-visual.json",
        preflight_factory=lambda _workspace: preflight,
        runtime_factory=runtime_factory,
        executor_factory=lambda *_args, **_kwargs: _cell_report,
        rng=random.Random(1234),
    )

    assert runtime_called is False
    assert launch.run.complete is False
    assert "fake visual judge" in str(launch.run.stop_reason).lower()
