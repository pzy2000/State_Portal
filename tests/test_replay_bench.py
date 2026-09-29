"""Contract tests for the C4 replay-determinism experiment.

The experiment's whole claim rests on two guarantees that are easy to lose
silently: a replay must issue no provider calls, and the strict visual channel
must not be allowed to decide the programmatic outcome. Both are asserted here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from warptest import cost
from warptest import replay_bench as rb


def _reference_result(**overrides):
    result = {
        "task_id": "2048-merge-left",
        "benchmark": "2048",
        "oracle_spec": "specs/2048/merge-left.yaml",
        "validity_class": "save-restorable",
        "status": "success",
        "start_gate_status": "passed",
        "goal_evaluation_status": "evaluated",
        "before_semantic_ok": True,
        "after_semantic_ok": True,
        "before_visual_ok": True,
        "after_visual_ok": True,
        "start_success": True,
        "goal_success": True,
        "e2e_success": True,
        "checkpoint_restore_ms": 103,
        "time_start_to_goal_ms": 1045,
        "grader_ms": 3944,
        "model": "test-model",
        "seed": 0,
        "oracle_goldens": {},
    }
    result.update(overrides)
    return result


def _write_run(tmp_path: Path, result: dict) -> Path:
    """Lay out a minimal C1 run: an index, a trial report, and its spec."""

    invocation = tmp_path / "c1_state" / "modelhash" / "seed0"
    invocation.mkdir(parents=True)
    report = invocation / "state-loop.json"
    report.write_text(json.dumps({"results": [result]}), encoding="utf-8")

    spec_dir = invocation / "tasks" / result["task_id"] / "v1"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "replayed",
                "benchmark": "2048",
                "target": {"kind": "board", "board": [[2, 2, 0, 0]], "score": 0},
                "checkpoint": {
                    "allowed_strategies": ["localStorage_restore_reload"],
                    "expected_validity": "save-restorable",
                },
                "actions": [{"type": "move", "direction": "left"}],
            }
        ),
        encoding="utf-8",
    )

    index = tmp_path / "p5-run-index.json"
    index.write_text(
        json.dumps(
            {
                "run_id": "test-run",
                "arms": {
                    "c1_state": [
                        {
                            "model": result["model"],
                            "seed": result["seed"],
                            "task_id": result["task_id"],
                            "status": "ok",
                            "report": str(report),
                            "report_sha256": "deadbeef",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    return index


def test_harvest_captures_reference_verdict_and_freezes_spec(tmp_path):
    index = _write_run(tmp_path, _reference_result())
    candidates, ledger = rb.harvest_candidates([index])

    assert ledger["accepted"] == 1
    assert candidates[0]["reference_verdict"]["e2e_success"] is True

    manifest = rb.freeze_population(candidates, tmp_path / "population")
    root, loaded = rb.load_population(manifest)
    assert len(loaded) == 1
    assert (root / loaded[0].spec_relpath).is_file()


def test_load_population_rejects_an_edited_spec(tmp_path):
    index = _write_run(tmp_path, _reference_result())
    candidates, _ = rb.harvest_candidates([index])
    manifest = rb.freeze_population(candidates, tmp_path / "population")

    frozen = json.loads(Path(manifest).read_text(encoding="utf-8"))
    spec_path = Path(manifest).parent / frozen["candidates"][0]["spec_relpath"]
    spec_path.write_text("name: tampered\n", encoding="utf-8")

    with pytest.raises(rb.ReplayPopulationError):
        rb.load_population(manifest)


def test_harvest_falls_back_to_the_trial_snapshot_back_link(tmp_path):
    """Atomic snapshots carry the report only; the spec stays with the invocation."""

    result = _reference_result()
    index = _write_run(tmp_path, result)
    source_report = (
        tmp_path / "c1_state" / "modelhash" / "seed0" / "state-loop.json"
    )

    snapshot_dir = tmp_path / "c1-trial-evidence" / "c1_state" / "digest"
    snapshot_dir.mkdir(parents=True)
    snapshot = snapshot_dir / "state-loop.json"
    snapshot.write_text(
        json.dumps(
            {
                "results": [result],
                "atomic_trial_evidence": {
                    "source_invocation_report": str(source_report)
                },
            }
        ),
        encoding="utf-8",
    )
    payload = json.loads(index.read_text(encoding="utf-8"))
    payload["arms"]["c1_state"][0]["report"] = str(snapshot)
    index.write_text(json.dumps(payload), encoding="utf-8")

    candidates, ledger = rb.harvest_candidates([index])
    assert ledger["accepted"] == 1, ledger["rejections"]


def test_goal_stage_is_not_compared_when_the_reference_never_reached_it():
    """A blocked reference Start records after_semantic_ok=False as 'not evaluated'.

    Comparing that against a replay that did evaluate the goal would report a
    grading-policy difference as checkpoint nondeterminism.
    """

    reference = {
        "before_semantic_ok": True,
        "after_semantic_ok": False,
        "goal_evaluation_status": "not_evaluated_start_failed",
    }
    observed = {
        "before_semantic_ok": True,
        "after_semantic_ok": True,
        "goal_evaluation_status": "evaluated",
    }
    comparison = rb._compare_verdicts(reference, observed)

    assert comparison["goal_stage_comparable"] is False
    assert comparison["programmatic_compared_fields"] == ["before_semantic_ok"]
    assert comparison["programmatic_reproduced"] is True


def test_visual_drift_never_flips_the_programmatic_outcome():
    reference = {
        "before_semantic_ok": True,
        "after_semantic_ok": True,
        "before_visual_ok": True,
        "after_visual_ok": True,
        "goal_evaluation_status": "evaluated",
    }
    observed = dict(reference, before_visual_ok=False, after_visual_ok=False)
    comparison = rb._compare_verdicts(reference, observed)

    assert comparison["programmatic_reproduced"] is True
    assert comparison["visual_identity_reproduced"] is False


def test_no_llm_guard_rejects_and_counts_a_provider_call():
    with pytest.raises(cost.LLMCallForbiddenError):
        with cost.no_llm_guard():
            cost.record_call("some-model", prompt_tokens=1, completion_tokens=1)


def test_no_llm_guard_still_fails_when_the_call_error_is_swallowed():
    """A broad ``except Exception`` inside the block must not hide a leak."""

    with pytest.raises(cost.LLMCallForbiddenError):
        with cost.no_llm_guard():
            try:
                cost.record_call("some-model", prompt_tokens=1)
            except Exception:
                pass


def test_no_llm_guard_attests_a_clean_block():
    with cost.no_llm_guard() as attestation:
        pass
    assert attestation.clean
    assert attestation.to_dict()["observed_calls"] == 0


def test_replay_agent_serves_the_frozen_spec_without_calling_a_model():
    spec = {
        "name": "frozen",
        "benchmark": "2048",
        "target": {"kind": "board"},
        "checkpoint": {
            "allowed_strategies": ["localStorage_restore_reload"],
            "expected_validity": "save-restorable",
        },
        "actions": [],
    }
    agent = rb.FrozenCheckpointReplayAgent({"t1": spec})

    class _Task:
        id = "t1"
        benchmark = "2048"

    with cost.no_llm_guard():
        checkpoint = agent.synthesize(_Task())

    assert checkpoint.model_generated_spec == spec
    assert checkpoint.generated_by == rb.REPLAY_AGENT_NAME
    assert agent.served == ["t1"]


def test_replay_agent_refuses_a_task_it_has_no_checkpoint_for():
    agent = rb.FrozenCheckpointReplayAgent({})

    class _Task:
        id = "missing"
        benchmark = "2048"

    with pytest.raises(rb.ReplayPopulationError):
        agent.synthesize(_Task())


def test_task_subset_is_written_in_the_derived_subset_form(tmp_path):
    source = Path("benchmarks/p5/c1_tasks.yaml")
    out = rb.write_task_subset(
        source, ["2048-merge-left", "2048-merge-up"], tmp_path / "subset.yaml"
    )
    data = yaml.safe_load(out.read_text(encoding="utf-8"))

    assert data["selected_task_ids"] == ["2048-merge-left", "2048-merge-up"]
    assert data["source_task_file"].endswith("c1_tasks.yaml")
    assert [row["id"] for row in data["tasks"]] == data["selected_task_ids"]


def test_infrastructure_blocked_replays_leave_the_flake_rate_alone():
    trials = [
        {
            "checkpoint_id": "a",
            "task_id": "t",
            "benchmark": "2048",
            "environment": "mac-a",
            "repetition": rep,
            "position": 0,
            "outcome": rb.OUTCOME_REPRODUCED,
            "comparison": {"visual_identity_reproduced": True},
            "reference_verdict": {"e2e_success": True},
            "timing": {"checkpoint_restore_ms": 100},
            "cost_summary": {"llm_calls": 0},
            "no_llm_attestation": {"clean": True},
        }
        for rep in range(3)
    ]
    trials.append(
        {
            "checkpoint_id": "b",
            "task_id": "t2",
            "benchmark": "2048",
            "environment": "mac-a",
            "repetition": 0,
            "position": 1,
            "outcome": rb.OUTCOME_INFRASTRUCTURE_BLOCKED,
            "comparison": {},
            "reference_verdict": {"e2e_success": True},
            "timing": {},
            "cost_summary": {"llm_calls": 0},
            "no_llm_attestation": {"clean": True},
        }
    )

    summary = rb.aggregate_trials(trials, bootstrap_iterations=50)

    assert summary["replays_infrastructure_blocked"] == 1
    assert summary["checkpoints_replayed"] == 1
    assert summary["verdict_reproduction_rate"] == 1.0
    assert summary["flake_rate"] == 0.0


def test_consistent_divergence_is_not_counted_as_flake():
    """Disagreeing with the reference the same way every time is deterministic."""

    trials = [
        {
            "checkpoint_id": "a",
            "task_id": "t",
            "benchmark": "jupyterlab",
            "environment": "mac-a",
            "repetition": rep,
            "position": 0,
            "outcome": rb.OUTCOME_DIVERGED,
            "comparison": {
                "programmatic_divergent_fields": ["before_semantic_ok"],
                "visual_identity_reproduced": True,
            },
            "reference_verdict": {"e2e_success": False},
            "observed_verdict": {
                "before_semantic_ok": True,
                "after_semantic_ok": False,
            },
            "timing": {},
            "cost_summary": {"llm_calls": 0},
            "no_llm_attestation": {"clean": True},
        }
        for rep in range(10)
    ]
    summary = rb.aggregate_trials(trials, bootstrap_iterations=50)

    assert summary["verdict_reproduction_rate"] == 0.0
    assert summary["self_consistency_rate"] == 1.0
    assert summary["flake_rate"] == 0.0
    assert summary["inconsistent_checkpoints"] == []
    assert summary["divergent_checkpoints"][0]["distinct_observed_verdicts"] == 1


def test_intermittent_divergence_is_counted_as_flake():
    def _trial(rep, before_ok):
        return {
            "checkpoint_id": "a",
            "task_id": "t",
            "benchmark": "luanti",
            "environment": "mac-a",
            "repetition": rep,
            "position": 0,
            "outcome": rb.OUTCOME_REPRODUCED if before_ok else rb.OUTCOME_DIVERGED,
            "comparison": {"visual_identity_reproduced": True},
            "reference_verdict": {"e2e_success": True},
            "observed_verdict": {
                "before_semantic_ok": before_ok,
                "after_semantic_ok": before_ok,
            },
            "timing": {},
            "cost_summary": {"llm_calls": 0},
            "no_llm_attestation": {"clean": True},
        }

    trials = [_trial(rep, True) for rep in range(9)] + [_trial(9, False)]
    summary = rb.aggregate_trials(trials, bootstrap_iterations=50)

    assert summary["self_consistency_rate"] == 0.0
    assert summary["flake_rate"] == 1.0
    assert summary["inconsistent_checkpoints"][0]["distinct_observed_verdicts"] == 2


def test_contract_drift_is_separated_from_a_faithfully_reproduced_failure():
    """A spec the harness can no longer load is drift only if it once loaded."""

    graded_reference = {"before_semantic_ok": True, "goal_evaluation_status": "evaluated"}
    assert rb._reference_was_graded(graded_reference)

    # Both a rejected spec and a de-registered action program are the harness
    # refusing to execute, not the application behaving differently.
    assert rb._rejected_by_spec_contract(
        {"reason": "config synthesis failed: unknown fields"}
    )
    assert rb._rejected_by_spec_contract(
        {
            "reason": "C1 web pair failed at registered_transition: ValueError: "
            "Unregistered JupyterLab State transition program: ['x']"
        }
    )
    assert not rb._rejected_by_spec_contract({"reason": "session bootstrap failed"})

    never_loaded_reference = {
        "before_semantic_ok": False,
        "goal_evaluation_status": "not_evaluated_start_failed",
    }
    assert not rb._reference_was_graded(never_loaded_reference)


def test_contract_drift_is_excluded_from_reproduction_and_flake():
    trials = [
        {
            "checkpoint_id": "a",
            "task_id": "t",
            "benchmark": "2048",
            "environment": "mac-a",
            "repetition": 0,
            "position": 0,
            "outcome": rb.OUTCOME_REPRODUCED,
            "comparison": {"visual_identity_reproduced": True},
            "reference_verdict": {"e2e_success": True},
            "timing": {},
            "cost_summary": {"llm_calls": 0},
            "no_llm_attestation": {"clean": True},
        },
        {
            "checkpoint_id": "b",
            "task_id": "blender-apply-bevel-modifier",
            "benchmark": "blender",
            "environment": "mac-a",
            "repetition": 0,
            "position": 1,
            "outcome": rb.OUTCOME_CONTRACT_DRIFT,
            "comparison": {},
            "reference_verdict": {"e2e_success": True},
            "timing": {},
            "cost_summary": {"llm_calls": 0},
            "no_llm_attestation": {"clean": True},
        },
    ]
    summary = rb.aggregate_trials(trials, bootstrap_iterations=50)

    assert summary["replays_spec_contract_drift"] == 1
    assert summary["checkpoints_replayed"] == 1
    assert summary["verdict_reproduction_rate"] == 1.0
    assert summary["flake_rate"] == 0.0
    assert summary["spec_contract_drift_checkpoints"] == [
        {"benchmark": "blender", "task_id": "blender-apply-bevel-modifier"}
    ]


def test_amortization_breaks_even_once_the_replay_costs_nothing():
    summary = {"llm_calls_observed": 0}
    curve = rb.amortization_curve(
        summary,
        c1_token_costs={
            "m": {"c1_state": 55550.0, "c1_agent_s": 267300.0},
        },
        max_runs=5,
    )
    entry = curve["per_model"]["m"]

    assert entry["cumulative_tokens"]["checkpoint_replay"][5] == 55550.0
    assert entry["break_even_run"]["c1_agent_s"] == 1
