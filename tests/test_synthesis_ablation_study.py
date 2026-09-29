from __future__ import annotations

import json

import pytest
import yaml

from warptest.synthesis_ablation_study import (
    ABLATION_VARIANTS, EXPANSION, TERRA, audit_stage, expected_repeated_action_labels,
    index_rows, prepare, study_plan, verified_semantic_short_circuit,
)


@pytest.fixture
def root(tmp_path):
    directory = tmp_path / "root/benchmarks/p5"
    directory.mkdir(parents=True)
    tasks = {"tasks": [{"id": f"task-{i}"} for i in range(61)]}
    (directory / "c1_tasks.yaml").write_text(yaml.safe_dump(tasks))
    (directory / "manifest.yaml").write_text(yaml.safe_dump({
        "api": {"credentials_file": "original.py"},
        "arms": {"c1_state": {"max_iters": 1, "max_explore_steps": 16}, "unused": {}},
    }))
    return tmp_path / "root"


def test_counterbalanced_registration_has_every_tuple_once(root):
    plan = study_plan(root)
    pairs = [(t, b["variant"]) for b in plan["batches"] for t in b["task_ids"]]
    assert len(pairs) == len(set(pairs)) == 244
    for i, task in enumerate(plan["task_ids"]):
        order = [b["variant"] for b in plan["batches"] if task in b["task_ids"]]
        assert order == [ABLATION_VARIANTS[(i + r) % 4] for r in range(4)]
    assert plan["stages"][0]["models"] == [TERRA]
    assert plan["stages"][1]["models"] == list(EXPANSION)
    assert plan["seeds"] == [0]


def test_preparation_does_not_copy_credentials_or_modify_source(root, tmp_path):
    output = tmp_path / "output"
    original = (root / "benchmarks/p5/manifest.yaml").read_bytes()
    credentials = tmp_path / "existing-credentials.py"
    plan = prepare(root, output, credentials)
    assert not credentials.exists()
    assert (root / "benchmarks/p5/manifest.yaml").read_bytes() == original
    for variant in ABLATION_VARIANTS:
        manifest = yaml.safe_load((output / "manifests" / f"{variant}.yaml").read_text())
        assert manifest["arms"]["c1_state"]["synthesis_ablation"] == variant
        assert set(manifest["arms"]) == {"c1_state"}
        assert manifest["seeds"] == 1
    assert prepare(root, output, credentials) == plan


def test_preparation_rejects_changed_registration(root, tmp_path):
    output = tmp_path / "output"
    prepare(root, output, tmp_path / "credentials.py")
    path = output / "registration.json"
    plan = json.loads(path.read_text())
    plan["seeds"] = [0, 1]
    path.write_text(json.dumps(plan))
    with pytest.raises(ValueError, match="registration"):
        prepare(root, output, tmp_path / "credentials.py")


def test_missing_trials_cannot_pass_expansion_gate(root, tmp_path):
    plan = study_plan(root)
    audit = audit_stage(tmp_path / "output", plan["stages"][0], plan)
    assert audit["complete"] is False
    assert audit["observed_trials"] == 0
    assert audit["expected_trials"] == 244
    assert audit["expansion_depends_on_effect_direction"] is False
    assert any("Missing 244" in error for error in audit["errors"])


def test_reads_real_p5_arm_envelope_including_completed_model_failures():
    rows = [{"task_id": "one", "status": "ok"}, {"task_id": "two", "status": "pending"}]
    assert index_rows({"arms": {"c1_state": rows}}) == rows
    with pytest.raises(ValueError, match="Invalid"):
        index_rows({"arms": []})


def test_repeated_action_receipts_require_matching_actions_and_unique_assertions(tmp_path):
    from warptest.c1_synthesis_cache import canonical_hash
    config = tmp_path / "spec.yaml"
    candidate = {"actions": [{"type": "jynew_level_up"}] * 2}
    config.write_text(yaml.safe_dump(candidate))
    checks = [{"name": "action[jynew_level_up]"}, {"name": "action[jynew_level_up]"},
              {"name": "assertion[jynew_role_attr].Level"}]
    result = {"config_path": str(config), "generated_spec_hash": canonical_hash(candidate), "seed_evidence": {
        "final": {"semantic_observation": {"checks": checks}}}}
    assert expected_repeated_action_labels(result, "final")
    assert not expected_repeated_action_labels(result, "initial")
    checks.append({"name": "assertion[jynew_role_attr].Level"})
    assert not expected_repeated_action_labels(result, "final")
    checks.pop()
    config.write_text(yaml.safe_dump({"actions": [{"type": "jynew_level_up"}]}))
    assert not expected_repeated_action_labels(result, "final")


def test_default_execution_stops_after_terra_without_expansion_audit_or_calls(tmp_path, monkeypatch):
    import warptest.synthesis_ablation_study as study
    import warptest.c1_experiment as preflight
    import warptest.p5_scale as scale

    monkeypatch.setattr(study, "c1_harness_hash", lambda root: "frozen")
    monkeypatch.setattr(study, "validate_retained_runs", lambda *args: None)
    audits = []
    def audit(output, stage, plan):
        audits.append(stage["id"])
        return {"complete": True, "observed_trials": 244}
    monkeypatch.setattr(study, "audit_stage", audit)
    def forbidden(*args, **kwargs):
        pytest.fail("No provider or trial dispatch is allowed at the stage boundary")
    monkeypatch.setattr(preflight, "run_c1_preflight", forbidden)
    monkeypatch.setattr(scale, "run_p5_scale", forbidden)
    plan = {"stages": [{"id": "terra"}, {"id": "expansion"}], "batches": []}
    study.execute(tmp_path, tmp_path / "out", tmp_path / "credentials.py", plan)
    assert audits == ["terra"]
    receipt = json.loads((tmp_path / "out/awaiting-user-instructions.json").read_text())
    assert receipt["expansion_started"] is False
    assert receipt["authorized_models"] == [TERRA]


def test_semantic_short_circuit_cannot_hide_a_visual_call_or_success():
    import copy
    result = {
        **{key: False for key in ("result", "visual_grading_started", "before_semantic_ok",
            "before_visual_ok", "after_semantic_ok", "after_visual_ok", "start_success",
            "goal_success", "e2e_success")},
        "start_grading_started": True, "start_gate_status": "semantic_failed",
        "goal_evaluation_status": "not_evaluated_start_failed", "before_semantic_status": "failure",
        "before_visual_status": "not_evaluated_semantic_failed",
        "after_semantic_status": "not_evaluated", "after_visual_status": "not_evaluated",
        "pair_verdict": {"result": False, "status": "failure", "after_evaluated": False,
            "before": {"stage": "before", "label": "not_evaluated", "evaluation_status": "not_evaluated_semantic_failed"},
            "after": {"stage": "after", "label": "not_evaluated", "evaluation_status": "not_evaluated_start_failed"}},
        "actual_cost": {"usage_complete": True, "llm_calls": 1,
            "calls": [{"kind": "checkpoint_synthesis", "usage_complete": True}]},
    }
    assert verified_semantic_short_circuit(result)
    changed = copy.deepcopy(result)
    changed["before_semantic_status"] = "settings_mismatch"
    assert verified_semantic_short_circuit(changed)
    changed["before_semantic_status"] = "success"
    assert not verified_semantic_short_circuit(changed)
    changed = copy.deepcopy(result)
    changed["actual_cost"]["calls"][0]["kind"] = "visual_judge"
    assert not verified_semantic_short_circuit(changed)
    changed = copy.deepcopy(result)
    changed["e2e_success"] = True
    assert not verified_semantic_short_circuit(changed)
    changed = copy.deepcopy(result)
    changed["pair_verdict"]["before"]["label"] = "correct"
    assert not verified_semantic_short_circuit(changed)
    changed = copy.deepcopy(result)
    changed["actual_cost"]["calls"] = []
    assert not verified_semantic_short_circuit(changed)
