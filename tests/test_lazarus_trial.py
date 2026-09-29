from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from warptest import lazarus_trial
from warptest.lazarus_grading import VisualJudgement
from warptest.lazarus_orchestrator import LazarusTrialKey
from warptest.lazarus_trial import (
    LazarusRuntimeObservation,
    LazarusVisualEvidence,
    judge_lazarus_visual_evidence,
    run_lazarus_trial,
)
from warptest.lazarus_versions import LazarusVersionArtifact, tree_fingerprint


def _artifact(
    tmp_path: Path, *, version: str = "buggy", task_id: str = "lz-example"
) -> LazarusVersionArtifact:
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "source.txt").write_text("artifact\n", encoding="utf-8")
    metadata = tmp_path / "artifact.git"
    metadata.mkdir()
    return LazarusVersionArtifact(
        task_id=task_id,
        version=version,
        root=root,
        git_metadata=metadata,
        source_revision="a" * 40,
        patch_sha256="b" * 64,
        tree_sha256=tree_fingerprint(root),
        changed_files=("source.txt",),
        build_identity={},
        cache_hit=True,
    )


def _runtime_observation(tmp_path: Path) -> LazarusRuntimeObservation:
    initial = tmp_path / "initial.png"
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    initial.write_bytes(b"initial-frame")
    before.write_bytes(b"before-frame")
    after.write_bytes(b"after-frame")
    return LazarusRuntimeObservation(
        complete=True,
        runtime_restored=True,
        before_semantic_ok=True,
        goal_checks_ok=False,
        defect_signature_ok=True,
        invariants_ok=True,
        before_screenshot=before,
        after_screenshot=after,
        checkpoint_sha256="c" * 64,
        synthesis_cache_hit=False,
        actual_synthesis_calls=1,
        checkpoint_reuse_success=False,
        treatment_tokens=17,
        treatment_time_ms=23.0,
        trace={"kind": "raw-runtime-observation"},
        initial_screenshot=initial,
        start_construction_mode="state_checkpoint_from_cold_start",
        start_semantic_evidence={"passed": True, "checks": ["test"]},
        start_evaluated=True,
    )


def _visuals(
    observation: LazarusRuntimeObservation, evaluate_after: bool = True
) -> LazarusVisualEvidence:
    assert evaluate_after is True
    assert observation.before_screenshot is not None
    assert observation.after_screenshot is not None
    before_sha = hashlib.sha256(observation.before_screenshot.read_bytes()).hexdigest()
    after_sha = hashlib.sha256(observation.after_screenshot.read_bytes()).hexdigest()
    return LazarusVisualEvidence(
        before=VisualJudgement(True, before_sha, "d" * 64),
        fixed_after=VisualJudgement(False, after_sha, "e" * 64),
        buggy_after=VisualJudgement(True, after_sha, "f" * 64),
        audits={"backend": "test"},
    )


def _checkpoint_restore(
    checkpoint_sha256: str, *, checkpoint_kind: str = "semantic_state"
) -> dict[str, object]:
    payload = {
        "schema": "c1-lazarus-p2p-checkpoint-restore-v1",
        "schema_version": 1,
        "checkpoint_sha256": checkpoint_sha256,
        "checkpoint_kind": checkpoint_kind,
        "runtime_restored": True,
    }
    encoded = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "applied": True,
        "checkpoint_sha256": checkpoint_sha256,
        "checkpoint_kind": checkpoint_kind,
        "runtime_restored": True,
        "path": ".warptest/lazarus/checkpoint-restore.json",
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
        "state_restore": {
            "schema_version": 1,
            "state_restore_applied": True,
            "adapter": "test-isolated-checkpoint-restorer-v1",
            "checkpoint_sha256": checkpoint_sha256,
            "checkpoint_kind": checkpoint_kind,
            "receipt_sha256": hashlib.sha256(encoded).hexdigest(),
        },
    }


def test_lazarus_trial_runs_p2p_after_runtime_and_recomputes_dual_oracle_grade(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path)
    key = LazarusTrialKey(
        task_id="lz-example",
        arm="lazarus_state",
        model="gpt-5.6-terra@relay",
        seed=0,
        version="buggy",
    )
    order: list[str] = []

    def runtime_runner():
        order.append("runtime")
        return _runtime_observation(tmp_path)

    def visual_runner(observation, evaluate_after):
        order.append("visual")
        assert evaluate_after is True
        return _visuals(observation)

    def p2p_runner():
        order.append("p2p")
        return {
            "schema_version": 1,
            "task_id": key.task_id,
            "version": key.version,
            "completed": True,
            "passed": True,
            "timeout_s": 300.0,
            "isolation": "apfs_cow_clone",
            "duration_ms": 2.0,
            "checkpoint_restore": _checkpoint_restore("c" * 64),
            "results": [
                {
                    "id": "p2p",
                    "passed": True,
                    "timed_out": False,
                    "duration_ms": 2.0,
                }
            ],
        }

    report = run_lazarus_trial(
        key=key,
        task_id=key.task_id,
        defect_locus="at_goal",
        artifact=artifact,
        per_trial_p2p_ids=("p2p",),
        version_switch_ms=12.5,
        runtime_runner=runtime_runner,
        visual_runner=visual_runner,
        p2p_runner=p2p_runner,
    )

    assert order == ["runtime", "visual", "p2p"]
    assert report["complete"] is True
    assert report["start_success"] is True
    assert report["goal_success"] is False
    assert report["defect_exposed"] is True
    assert report["false_alarm"] is False
    assert report["grading"]["result"]["defect_exposed"] is True
    assert report["per_trial_p2p_evidence"]["results"][0]["id"] == "p2p"
    assert report["version_switch_ms"] == 12.5
    assert "goal_success" not in report["runtime"]


def test_lzp_trial_requires_and_records_visual_only_grading(tmp_path: Path) -> None:
    task_id = "lzp-jupyterlab-example"
    artifact = _artifact(tmp_path, task_id=task_id)
    key = LazarusTrialKey(
        task_id=task_id,
        arm="lazarus_state",
        model="gpt-5.6-terra@relay",
        seed=0,
        version="buggy",
    )

    def p2p_runner():
        return {
            "schema_version": 1,
            "task_id": task_id,
            "version": "buggy",
            "completed": True,
            "passed": True,
            "timeout_s": 300.0,
            "isolation": "apfs_cow_clone",
            "duration_ms": 2.0,
            "checkpoint_restore": _checkpoint_restore("c" * 64),
            "results": [
                {
                    "id": "p2p",
                    "passed": True,
                    "timed_out": False,
                    "duration_ms": 2.0,
                }
            ],
        }

    report = run_lazarus_trial(
        key=key,
        task_id=task_id,
        defect_locus="at_goal",
        artifact=artifact,
        per_trial_p2p_ids=("p2p",),
        version_switch_ms=0,
        runtime_runner=lambda: _runtime_observation(tmp_path),
        visual_runner=_visuals,
        p2p_runner=p2p_runner,
        grading_mode="p2p_only_visual",
    )
    assert report["complete"] is True
    assert report["semantic_evidence"] == "not_applicable_p2p_only"
    assert report["defect_evidence_source"] == "dual_after_visual_goldens"
    assert report["defect_exposed"] is True


def test_lzp_trial_rejects_semantic_grading_mode(tmp_path: Path) -> None:
    task_id = "lzp-jupyterlab-example"
    artifact = _artifact(tmp_path, task_id=task_id)
    key = LazarusTrialKey(
        task_id=task_id,
        arm="lazarus_state",
        model="gpt-5.6-terra@relay",
        seed=0,
        version="buggy",
    )
    try:
        run_lazarus_trial(
            key=key,
            task_id=task_id,
            defect_locus="at_goal",
            artifact=artifact,
            per_trial_p2p_ids=(),
            version_switch_ms=0,
            runtime_runner=lambda: _runtime_observation(tmp_path),
            visual_runner=_visuals,
            p2p_runner=lambda: {},
        )
    except ValueError as error:
        assert "P2P-only visual grading" in str(error)
    else:
        raise AssertionError("lzp-* trial accepted semantic grading mode")


def test_bound_judge_accepts_deterministic_normalized_equality() -> None:
    lazarus_trial._validate_bound_judge_model(
        {
            "before": {
                "verdict_source": "normalized_image_equality_v1",
                "judge_model": None,
                "normalized_image_equality": {"match": True},
            }
        },
        expected="qwen3-vl-flash",
        label="deterministic audit",
    )


def test_bound_judge_rejects_judge_free_non_deterministic_verdict() -> None:
    try:
        lazarus_trial._validate_bound_judge_model(
            {"before": {"verdict_source": "llm_visual_judge", "judge_model": None}},
            expected="qwen3-vl-flash",
            label="LLM audit",
        )
    except ValueError as error:
        assert "judge model drift" in str(error)
    else:
        raise AssertionError("judge-free LLM verdict was accepted")


def test_lazarus_trial_rejects_missing_total_p2p_duration(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path)
    key = LazarusTrialKey(
        task_id="lz-example",
        arm="lazarus_state",
        model="gpt-5.6-terra@relay",
        seed=0,
        version="buggy",
    )

    report = run_lazarus_trial(
        key=key,
        task_id=key.task_id,
        defect_locus="at_goal",
        artifact=artifact,
        per_trial_p2p_ids=("p2p",),
        version_switch_ms=12.5,
        runtime_runner=lambda: _runtime_observation(tmp_path),
        visual_runner=_visuals,
        p2p_runner=lambda: {
            "schema_version": 1,
            "task_id": key.task_id,
            "version": key.version,
            "completed": True,
            "passed": True,
            "timeout_s": 300.0,
            "isolation": "apfs_cow_clone",
            "checkpoint_restore": _checkpoint_restore("c" * 64),
            "results": [
                {"id": "p2p", "passed": True, "timed_out": False, "duration_ms": 2.0}
            ],
        },
    )

    assert report["complete"] is False
    assert report["per_trial_p2p_ok"] is False
    assert "per-trial P2P duration is invalid" in report["failures"]


def test_lazarus_trial_persists_usage_from_a_failed_runtime_attempt(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path)
    key = LazarusTrialKey(
        task_id="lz-example",
        arm="lazarus_state",
        model="gpt-5.6-terra@relay",
        seed=0,
        version="buggy",
    )
    error = RuntimeError("checkpoint rejected")
    error.lazarus_usage = {
        "llm_calls": 1,
        "total_tokens": 41,
        "llm_latency_ms": 17,
        "usage_complete": True,
    }

    def failed_runtime():
        raise error

    report = run_lazarus_trial(
        key=key,
        task_id=key.task_id,
        defect_locus="at_goal",
        artifact=artifact,
        per_trial_p2p_ids=("p2p",),
        version_switch_ms=12.5,
        runtime_runner=failed_runtime,
        visual_runner=_visuals,
        p2p_runner=lambda: {},
    )

    assert report["complete"] is False
    assert report["actual_synthesis_calls"] == 1
    assert report["treatment_tokens"] == 41
    assert report["treatment_time_ms"] == 17
    assert report["runtime"]["failure_usage"]["usage_complete"] is True


def test_lazarus_trial_fails_closed_when_runtime_was_not_restored_or_visuals_drift(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path, version="fixed")
    key = LazarusTrialKey(
        task_id="lz-example",
        arm="lazarus_agent_s",
        model="gpt-5.6-terra@relay",
        seed=0,
        version="fixed",
    )
    observation = _runtime_observation(tmp_path)
    observation = replace(observation, runtime_restored=False)

    report = run_lazarus_trial(
        key=key,
        task_id=key.task_id,
        defect_locus="on_path",
        artifact=artifact,
        per_trial_p2p_ids=("p2p",),
        version_switch_ms=12.5,
        runtime_runner=lambda: observation,
        visual_runner=_visuals,
        p2p_runner=lambda: {
            "schema_version": 1,
            "task_id": key.task_id,
            "version": key.version,
            "completed": True,
            "passed": True,
            "timeout_s": 300.0,
            "results": [],
        },
    )

    assert report["complete"] is False
    assert report["per_trial_p2p_ok"] is False
    assert any("runtime was not restored" in item for item in report["failures"])


def test_lazarus_trial_short_circuits_after_evidence_when_start_semantics_fail(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path)
    key = LazarusTrialKey(
        task_id="lz-example",
        arm="lazarus_state",
        model="gpt-5.6-terra@relay",
        seed=0,
        version="buggy",
    )
    observation = replace(
        _runtime_observation(tmp_path),
        before_semantic_ok=False,
        goal_checks_ok=None,
        defect_signature_ok=None,
        invariants_ok=None,
        after_screenshot=None,
    )
    calls: list[str] = []

    def visual_runner(
        incoming: LazarusRuntimeObservation, *, evaluate_after: bool
    ) -> LazarusVisualEvidence:
        calls.append("visual")
        assert incoming is observation
        assert evaluate_after is False
        assert incoming.before_screenshot is not None
        before_sha = hashlib.sha256(incoming.before_screenshot.read_bytes()).hexdigest()
        return LazarusVisualEvidence(
            before=VisualJudgement(True, before_sha, "d" * 64),
            fixed_after=VisualJudgement(None, None, "e" * 64),
            buggy_after=VisualJudgement(None, None, "f" * 64),
            audits={"after": "not_executed_start_failed"},
        )

    def p2p_runner():
        calls.append("p2p")
        raise AssertionError("P2P must not run after a failed Start")

    report = run_lazarus_trial(
        key=key,
        task_id=key.task_id,
        defect_locus="at_goal",
        artifact=artifact,
        per_trial_p2p_ids=("p2p",),
        version_switch_ms=12.5,
        runtime_runner=lambda: observation,
        visual_runner=visual_runner,
        p2p_runner=p2p_runner,
    )

    assert calls == ["visual"]
    assert report["failures"] == []
    assert report["complete"] is True
    assert report["start_success"] is False
    assert report["grading"]["result"]["after_statuses"] == {
        "goal": "not_executed_start_failed",
        "defect": "not_executed_start_failed",
    }
    assert report["per_trial_p2p_ok"] is False
    assert report["per_trial_p2p_evidence"]["isolation"] == "not_executed_start_failed"


@pytest.mark.parametrize(
    ("arm", "start_mode"),
    [
        ("lazarus_agent_s", "agent_s_phase_a_from_cold_start"),
        ("lazarus_scalecua", "scalecua_phase_a_from_cold_start"),
    ],
)
def test_p2p_only_failed_phase_a_may_end_on_the_unchanged_cold_start_frame(
    tmp_path: Path, arm: str, start_mode: str,
) -> None:
    task_id = "lzp-jupyterlab-failed-start"
    artifact = _artifact(tmp_path, task_id=task_id)
    key = LazarusTrialKey(
        task_id=task_id,
        arm=arm,
        model="gpt-5.6-terra@relay",
        seed=0,
        version="buggy",
    )
    observation = _runtime_observation(tmp_path)
    assert observation.initial_screenshot is not None
    observation = replace(
        observation,
        before_semantic_ok=False,
        before_screenshot=observation.initial_screenshot,
        after_screenshot=None,
        goal_checks_ok=None,
        defect_signature_ok=None,
        invariants_ok=None,
        start_construction_mode=start_mode,
        start_semantic_evidence={"passed": False, "phase_complete": True},
    )

    def visual_runner(
        incoming: LazarusRuntimeObservation, *, evaluate_after: bool
    ) -> LazarusVisualEvidence:
        assert evaluate_after is False
        assert incoming.before_screenshot is not None
        before_sha = hashlib.sha256(incoming.before_screenshot.read_bytes()).hexdigest()
        return LazarusVisualEvidence(
            before=VisualJudgement(False, before_sha, "d" * 64),
            fixed_after=VisualJudgement(None, None, "e" * 64),
            buggy_after=VisualJudgement(None, None, "f" * 64),
            audits={"after": "not_executed_start_failed"},
        )

    report = run_lazarus_trial(
        key=key,
        task_id=task_id,
        defect_locus="at_goal",
        artifact=artifact,
        per_trial_p2p_ids=("p2p",),
        version_switch_ms=0,
        runtime_runner=lambda: observation,
        visual_runner=visual_runner,
        p2p_runner=lambda: (_ for _ in ()).throw(
            AssertionError("P2P must not run after failed Phase A")
        ),
        grading_mode="p2p_only_visual",
    )
    assert report["complete"] is True
    assert report["failures"] == []
    assert report["start_success"] is False
    assert report["goal_success"] is False


@pytest.mark.parametrize("mode", [
    "state_checkpoint_from_cold_start", "shared_generated_state_from_cold_start",
    "agent_s_phase_a_from_cold_start", "agent_s_already_satisfied_cold_start",
    "scalecua_phase_a_from_cold_start", "scalecua_already_satisfied_cold_start",
])
def test_real_start_observation_recognizes_registered_policy_receipts(tmp_path, mode):
    observation = replace(_runtime_observation(tmp_path), start_construction_mode=mode)
    failures = []
    lazarus_trial._validate_observation(observation, failures, require_real_start=True)
    assert failures == []
    failures = []
    lazarus_trial._validate_observation(
        replace(observation, start_construction_mode="unregistered_start"),
        failures, require_real_start=True,
    )
    assert failures == ["runtime Start construction mode is invalid"]


def test_lazarus_trial_does_not_run_p2p_when_start_visual_fails(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path, version="fixed")
    key = LazarusTrialKey(
        task_id="lz-example",
        arm="lazarus_state",
        model="gpt-5.6-terra@relay",
        seed=0,
        version="fixed",
    )
    observation = _runtime_observation(tmp_path)
    calls: list[str] = []

    def visual_runner(
        incoming: LazarusRuntimeObservation, *, evaluate_after: bool
    ) -> LazarusVisualEvidence:
        calls.append("visual")
        assert incoming is observation
        assert evaluate_after is True
        before_sha = hashlib.sha256(incoming.before_screenshot.read_bytes()).hexdigest()
        return LazarusVisualEvidence(
            before=VisualJudgement(False, before_sha, "d" * 64),
            fixed_after=VisualJudgement(None, None, "e" * 64),
            buggy_after=VisualJudgement(None, None, "f" * 64),
            audits={"after": "not_executed_start_failed"},
        )

    def p2p_runner():
        calls.append("p2p")
        raise AssertionError("P2P must not run after a failed visual Start")

    report = run_lazarus_trial(
        key=key,
        task_id=key.task_id,
        defect_locus="at_goal",
        artifact=artifact,
        per_trial_p2p_ids=("p2p",),
        version_switch_ms=12.5,
        runtime_runner=lambda: observation,
        visual_runner=visual_runner,
        p2p_runner=p2p_runner,
    )

    assert calls == ["visual"]
    assert report["failures"] == []
    assert report["complete"] is True
    assert report["start_success"] is False
    assert report["per_trial_p2p_ok"] is False
    assert report["per_trial_p2p_evidence"]["isolation"] == "not_executed_start_failed"


def test_lazarus_trial_rejects_visual_evidence_that_reuses_different_after_frames(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path)
    key = LazarusTrialKey(
        task_id="lz-example",
        arm="lazarus_agent_s",
        model="gpt-5.6-terra@relay",
        seed=0,
        version="buggy",
    )

    def drifting_visuals(
        observation: LazarusRuntimeObservation, evaluate_after: bool
    ) -> LazarusVisualEvidence:
        assert evaluate_after is True
        evidence = _visuals(observation)
        return LazarusVisualEvidence(
            before=evidence.before,
            fixed_after=evidence.fixed_after,
            buggy_after=VisualJudgement(True, "0" * 64, "f" * 64),
            audits={},
        )

    report = run_lazarus_trial(
        key=key,
        task_id=key.task_id,
        defect_locus="at_goal",
        artifact=artifact,
        per_trial_p2p_ids=("p2p",),
        version_switch_ms=12.5,
        runtime_runner=lambda: _runtime_observation(tmp_path),
        visual_runner=drifting_visuals,
        p2p_runner=lambda: {
            "schema_version": 1,
            "task_id": key.task_id,
            "version": key.version,
            "completed": True,
            "passed": True,
            "timeout_s": 300.0,
            "duration_ms": 1.0,
            "checkpoint_restore": _checkpoint_restore(
                "c" * 64, checkpoint_kind="agent_s_from_start"
            ),
            "results": [{"id": "p2p", "passed": True, "timed_out": False, "duration_ms": 1}],
        },
    )

    assert report["complete"] is False
    assert "visual judgements did not use the same screenshot" in report["failures"]


def test_lazarus_trial_rejects_a_p2p_receipt_without_real_state_restore_evidence(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path)
    key = LazarusTrialKey(
        task_id="lz-example",
        arm="lazarus_state",
        model="gpt-5.6-terra@relay",
        seed=0,
        version="buggy",
    )
    receipt = _checkpoint_restore("c" * 64)
    receipt.pop("state_restore")

    report = run_lazarus_trial(
        key=key,
        task_id=key.task_id,
        defect_locus="at_goal",
        artifact=artifact,
        per_trial_p2p_ids=("p2p",),
        version_switch_ms=12.5,
        runtime_runner=lambda: _runtime_observation(tmp_path),
        visual_runner=_visuals,
        p2p_runner=lambda: {
            "schema_version": 1,
            "task_id": key.task_id,
            "version": key.version,
            "completed": True,
            "passed": True,
            "timeout_s": 300.0,
            "duration_ms": 1.0,
            "checkpoint_restore": receipt,
            "results": [
                {"id": "p2p", "passed": True, "timed_out": False, "duration_ms": 1.0}
            ],
        },
    )

    assert report["complete"] is False
    assert report["per_trial_p2p_ok"] is False
    assert "per-trial P2P checkpoint restore evidence is invalid" in report["failures"]


def test_lazarus_visual_judge_uses_registered_evaluation_before_oracle(
    tmp_path: Path, monkeypatch
) -> None:
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    before.write_bytes(b"before-frame")
    after.write_bytes(b"after-frame")
    visual = tmp_path / "visual"
    visual.mkdir()
    for name in ("before.png", "fixed.png", "buggy.png"):
        (visual / name).write_bytes(name.encode("utf-8"))
    spec_path = tmp_path / "oracle.yaml"
    spec_path.write_text("name: test\n", encoding="utf-8")
    calls: list[dict[str, object]] = []

    def fake_audit(**kwargs):
        calls.append(kwargs)
        return {
            "before": {"label": "correct"},
            "after": {"label": "correct"},
        }

    monkeypatch.setattr(lazarus_trial, "run_visual_pair_audit", fake_audit)
    observation = _runtime_observation(tmp_path)
    evidence = judge_lazarus_visual_evidence(
        observation,
        spec={
            "evaluation": {
                "visual_oracle": {
                    "description": "registered before state",
                    "rubric": {"must": ["before"]},
                },
                "lazarus": {
                    "goldens": {
                        "before": "visual/before.png",
                        "fixed": {
                            "after": "visual/fixed.png",
                            "after_description": "fixed after",
                            "after_rubric": {"must": ["fixed"]},
                        },
                        "buggy": {
                            "after": "visual/buggy.png",
                            "after_description": "buggy after",
                            "after_rubric": {"must": ["buggy"]},
                        },
                    }
                },
            }
        },
        spec_path=spec_path,
    )

    assert evidence.before.ok is True
    assert len(calls) == 2
    assert calls[0]["before_description"] == "registered before state"
    assert calls[0]["before_rubric"] == {"must": ["before"]}


def test_lazarus_visual_judge_binds_the_manifest_judge_model_per_trial(
    tmp_path: Path, monkeypatch
) -> None:
    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    before.write_bytes(b"before-frame")
    after.write_bytes(b"after-frame")
    visual = tmp_path / "visual"
    visual.mkdir()
    for name in ("before.png", "fixed.png", "buggy.png"):
        (visual / name).write_bytes(name.encode("utf-8"))
    spec_path = tmp_path / "oracle.yaml"
    spec_path.write_text("name: test\n", encoding="utf-8")
    seen_models: list[str | None] = []

    def fake_audit(**_kwargs):
        seen_models.append(os.environ.get("WARPTEST_VISUAL_JUDGE_MODEL"))
        return {
            "before": {"label": "correct", "judge_model": seen_models[-1]},
            "after": {"label": "correct", "judge_model": seen_models[-1]},
        }

    monkeypatch.setattr(lazarus_trial, "run_visual_pair_audit", fake_audit)
    monkeypatch.setenv("WARPTEST_VISUAL_JUDGE_MODEL", "wrong-model")
    monkeypatch.setenv("WARPTEST_VISUAL_JUDGE_FAKE", "correct")
    evidence = judge_lazarus_visual_evidence(
        _runtime_observation(tmp_path),
        spec={
            "evaluation": {
                "lazarus": {
                    "goldens": {
                        "before": "visual/before.png",
                        "fixed": {"after": "visual/fixed.png", "after_description": "fixed"},
                        "buggy": {"after": "visual/buggy.png", "after_description": "buggy"},
                    }
                }
            }
        },
        spec_path=spec_path,
        judge_model="qwen3-vl-flash@relay",
    )

    assert seen_models == ["qwen3-vl-flash@relay", "qwen3-vl-flash@relay"]
    assert evidence.audits["registered_judge_model"] == "qwen3-vl-flash@relay"
    assert os.environ["WARPTEST_VISUAL_JUDGE_MODEL"] == "wrong-model"


def test_lazarus_visual_judge_accepts_the_provider_clean_model_bound_by_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    """The routed manifest tag and client-emitted clean model are both bound."""

    before = tmp_path / "before.png"
    after = tmp_path / "after.png"
    before.write_bytes(b"before-frame")
    after.write_bytes(b"after-frame")
    visual = tmp_path / "visual"
    visual.mkdir()
    for name in ("before.png", "fixed.png", "buggy.png"):
        (visual / name).write_bytes(name.encode("utf-8"))
    spec_path = tmp_path / "oracle.yaml"
    spec_path.write_text("name: test\n", encoding="utf-8")

    def clean_model_audit(**_kwargs):
        return {
            "before": {"label": "correct", "judge_model": "qwen3-vl-flash"},
            "after": {"label": "correct", "judge_model": "qwen3-vl-flash"},
        }

    monkeypatch.delenv("WARPTEST_VISUAL_JUDGE_FAKE", raising=False)
    monkeypatch.setattr(lazarus_trial, "run_visual_pair_audit", clean_model_audit)
    evidence = judge_lazarus_visual_evidence(
        _runtime_observation(tmp_path),
        spec={
            "evaluation": {
                "lazarus": {
                    "goldens": {
                        "before": "visual/before.png",
                        "fixed": {"after": "visual/fixed.png", "after_description": "fixed"},
                        "buggy": {"after": "visual/buggy.png", "after_description": "buggy"},
                    }
                }
            }
        },
        spec_path=spec_path,
        judge_model="qwen3-vl-flash@relay",
    )

    assert evidence.audits["registered_judge_identity"] == {
        "model_spec": "qwen3-vl-flash@relay",
        "resolved_model": "qwen3-vl-flash",
        "provider": "relay",
    }


def test_bound_judge_accepts_only_explicit_negative_capture_quality():
    audit = {"after": {"verdict_source":"non_informative_frame_rejection_v1", "judge_model":None,
        "label":"incorrect", "quality":{"informative":False}}}
    lazarus_trial._validate_bound_judge_model(audit, expected="qwen3-vl-flash", label="blank frame")
    audit["after"]["label"] = "correct"
    import pytest
    with pytest.raises(ValueError, match="judge model drift"):
        lazarus_trial._validate_bound_judge_model(audit, expected="qwen3-vl-flash", label="forged positive")


def test_bound_judge_serializes_environment_binding(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import os
    monkeypatch.delenv("WARPTEST_VISUAL_JUDGE_MODEL", raising=False)
    entered = Event()
    second_ready = Event()
    release = Event()
    def first():
        with lazarus_trial._bound_visual_judge_model("judge-a"):
            entered.set()
            assert second_ready.wait(2)
            assert release.wait(2)
            assert os.environ["WARPTEST_VISUAL_JUDGE_MODEL"] == "judge-a"
    def second():
        assert entered.wait(2)
        second_ready.set()
        with lazarus_trial._bound_visual_judge_model("judge-b"):
            assert os.environ["WARPTEST_VISUAL_JUDGE_MODEL"] == "judge-b"
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(first)
        b=pool.submit(second)
        assert second_ready.wait(2)
        release.set()
        a.result()
        b.result()
    assert "WARPTEST_VISUAL_JUDGE_MODEL" not in os.environ


def test_bound_judge_preserves_retryable_provider_failure():
    import pytest
    from warptest.lazarus_p2p_only_recovery import classify_failure
    audit = {"before":{"judge_failure":True,"judge_error":"HTTP 429",
        "judge_failure_details":{"retryable":True,"category":"provider_transient","error":"HTTP 429"}},
        "after":{"evaluation_status":"not_evaluated_before_failed"}}
    with pytest.raises(RuntimeError, match="visual judge unavailable") as error:
        lazarus_trial._validate_bound_judge_model(audit, expected="qwen3-vl-flash", label="outage")
    assert classify_failure(error.value)["retryable"]


def test_policy_exit_before_start_is_a_complete_negative_without_a_stale_frame(tmp_path):
    from dataclasses import replace
    import pytest
    task_id = "lzp-luanti-example"
    artifact = _artifact(tmp_path, task_id=task_id)
    key = LazarusTrialKey("lazarus_agent_s", "test", 0, task_id, "buggy")
    trace = {"benchmark":"luanti", "policy_exit":{"phase":"phase-a", "exit_code":0,
        "target_pid":123, "accepted_action":{"kind":"key", "key":"escape", "execution_receipt":{"accepted":True}}}}
    observation = replace(_runtime_observation(tmp_path), before_semantic_ok=False, invariants_ok=False,
        before_screenshot=None, after_screenshot=None, actual_synthesis_calls=0,
        start_construction_mode="agent_s_phase_a_from_cold_start", start_semantic_evidence={"passed":False},
        trace=trace)
    visuals = LazarusVisualEvidence(VisualJudgement(False,None,None), VisualJudgement(None,None,None), VisualJudgement(None,None,None), {"status":"not_evaluated_semantic_start_failed"})
    def run(obs):
        return run_lazarus_trial(key=key, task_id=task_id, defect_locus="at_goal", artifact=artifact,
            per_trial_p2p_ids=("p2p",), version_switch_ms=0, runtime_runner=lambda: obs,
            visual_runner=lambda *args, **kwargs: visuals, p2p_runner=lambda: pytest.fail("P2P must not run"), grading_mode="p2p_only_visual")
    report = run(observation)
    assert report["complete"] is True, report["failures"]
    assert report["start_success"] is False and report["goal_success"] is False
    assert report["visual"]["before"]["screenshot_sha256"] is None
    trace["policy_exit"]["exit_code"] = 1
    assert run(observation)["complete"] is False


@pytest.mark.parametrize('kind', ['semantic_state', 'agent_s_from_start', 'scalecua_from_start'])
def test_coordinator_accepts_registered_restore_kinds_and_rejects_tampering(kind):
    import copy
    from warptest.lazarus_upstream import LazarusP2PCheckpointRestore
    from warptest.lazarus_trial import lazarus_p2p_checkpoint_restore_is_valid
    sha = 'c' * 64
    assert LazarusP2PCheckpointRestore(sha, kind).validated()
    receipt = _checkpoint_restore(sha, checkpoint_kind=kind)
    assert lazarus_p2p_checkpoint_restore_is_valid(receipt, checkpoint_sha256=sha, checkpoint_kind=kind)
    changed = copy.deepcopy(receipt)
    changed['state_restore']['checkpoint_kind'] = 'unregistered'
    assert not lazarus_p2p_checkpoint_restore_is_valid(changed, checkpoint_sha256=sha, checkpoint_kind=kind)
    changed = copy.deepcopy(receipt)
    changed['payload_sha256'] = '0' * 64
    assert not lazarus_p2p_checkpoint_restore_is_valid(changed, checkpoint_sha256=sha, checkpoint_kind=kind)
    changed = copy.deepcopy(receipt)
    changed['state_restore']['state_restore_applied'] = False
    assert not lazarus_p2p_checkpoint_restore_is_valid(changed, checkpoint_sha256=sha, checkpoint_kind=kind)


def test_runtime_canary_checks_coordinator_receipts_not_only_passed_flag():
    from scripts.run_lazarus_p2p_only_runtime_canary import require_canary_p2p
    sha = 'c' * 64
    p2p = {'schema_version':1,'task_id':'lzp-test','version':'buggy','passed':True,'completed':True,'timeout_s':300.0,'duration_ms':1,'results':[{'id':'control','passed':True,'timed_out':False,'duration_ms':1}],'checkpoint_restore': _checkpoint_restore(sha, checkpoint_kind='scalecua_from_start')}
    require_canary_p2p(p2p, arm='lazarus_scalecua', checkpoint_sha256=sha, task_id='lzp-test', version='buggy', test_ids=['control'])
    p2p['checkpoint_restore']['state_restore']['state_restore_applied'] = False
    with pytest.raises(RuntimeError, match='coordinator admission'):
        require_canary_p2p(p2p, arm='lazarus_scalecua', checkpoint_sha256=sha, task_id='lzp-test', version='buggy', test_ids=['control'])


@pytest.mark.parametrize('invariants_ok', [True, False])
def test_scalecua_trial_accepts_its_real_p2p_receipt_kind(tmp_path, invariants_ok):
    key = LazarusTrialKey(task_id='lzp-example', arm='lazarus_scalecua', model='gpt-6-astra@relay', seed=0, version='buggy')
    observation = replace(_runtime_observation(tmp_path), actual_synthesis_calls=0, start_construction_mode='scalecua_phase_a_from_cold_start', invariants_ok=invariants_ok)
    report = run_lazarus_trial(
        key=key, task_id=key.task_id, defect_locus='at_goal', artifact=_artifact(tmp_path, task_id=key.task_id),
        per_trial_p2p_ids=('p2p',), version_switch_ms=0, grading_mode='p2p_only_visual',
        runtime_runner=lambda: observation, visual_runner=_visuals,
        p2p_runner=lambda: {
            'schema_version':1, 'task_id':key.task_id, 'version':key.version,
            'completed':True, 'passed':True, 'timeout_s':300.0, 'duration_ms':1,
            'isolation':'apfs_cow_clone',
            'checkpoint_restore':_checkpoint_restore('c'*64, checkpoint_kind='scalecua_from_start'),
            'results':[{'id':'p2p','passed':True,'timed_out':False,'duration_ms':1}],
        },
    )
    assert report['complete'], report['failures']
    assert report['per_trial_p2p_ok'] is True


def test_checkpoint_receipt_rejects_unregistered_kind_even_with_matching_hash():
    sha = 'c' * 64
    receipt = _checkpoint_restore(sha, checkpoint_kind='unregistered')
    assert not lazarus_trial.lazarus_p2p_checkpoint_restore_is_valid(receipt, checkpoint_sha256=sha, checkpoint_kind='unregistered')
