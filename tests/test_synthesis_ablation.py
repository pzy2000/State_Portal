from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import yaml

from warptest.repo_tools import RepoSandbox
from warptest.spec_schema import validate_spec_schema
from warptest.synthesis import (
    ExploratoryLLMCheckpointSynthesisAgent,
    ModelCallResult,
    SynthesisError,
    build_checkpoint_format_repair_prompt,
    build_explore_system_prompt,
    build_synthesis_agent,
    formal_c1_checkpoint_contract_errors,
)
from warptest.synthesis_ablation import ablation_scope, project_contract
from warptest.task_schema import PublicIssue, validate_method_checkpoint


def task_and_candidate(tmp_path, *, empty_evidence=False, empty_assertions=False):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "game.js").write_text("function move() { return 4; }\n")
    task = PublicIssue(id="ablation-task", benchmark="2048", repo=str(repo),
                       issue="Merge two tiles with a left move.", tags=["c1"])
    candidate = {
        "task_id": task.id,
        "benchmark": "2048",
        "generated_spec": {
            "name": "Candidate", "benchmark": "2048",
            "target": {"kind": "board", "board": [[2, 2, 0, 0], [0]*4, [0]*4, [0]*4], "score": 0, "bestScore": 0},
            "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
            "actions": [{"type": "move", "direction": "left"}],
            "assertions": [] if empty_assertions else [{"type": "tile_value_present", "value": 4}],
        },
        "source_evidence": [] if empty_evidence else [{"source_path": "game.js", "matched_symbol": "move", "rationale": "Public move implementation"}],
        "payload": {"kind": "localStorage"},
        "runtime_validation": {"status": "pending", "detail": "not executed"},
        "validity_class": "save-restorable",
    }
    return task, candidate


def final(candidate):
    return "FINAL\n```yaml\n" + yaml.safe_dump(candidate) + "```"


class FakeClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.messages = []

    def converse(self, model, messages, **kwargs):
        self.messages.append(copy.deepcopy(messages))
        return ModelCallResult(content=next(self.replies), latency_ms=1)


def test_no_source_final_is_accepted_without_a_tool_or_fabricated_citation(tmp_path, monkeypatch):
    task, candidate = task_and_candidate(tmp_path, empty_evidence=True)
    def forbidden(*args, **kwargs):
        pytest.fail("no-source policy dispatched a repository tool")
    monkeypatch.setattr(RepoSandbox, "dispatch", forbidden)
    client = FakeClient(['ACTION: read_file\n{"path":"game.js"}', final(candidate)])
    agent = ExploratoryLLMCheckpointSynthesisAgent(client=client, max_steps=3, ablation_variant="no_source_exploration")
    output = agent.synthesize(task, tmp_path, tmp_path / "artifact")
    assert output.source_evidence == []
    assert output.model_generated_spec == candidate["generated_spec"]
    metadata = json.loads((tmp_path / "artifact/synthesis-meta.json").read_text())
    assert metadata["tool_calls"] == 0 and metadata["files_read"] == []
    assert "Begin now with one ACTION" not in client.messages[0][1]["content"]
    assert "ACTION: read_file" not in client.messages[0][0]["content"]
    assert validate_method_checkpoint(output, tmp_path)  # Legacy/default gate stays strict.
    assert not validate_method_checkpoint(output, tmp_path, allow_empty_source_evidence=True)


def test_full_still_rejects_zero_tool_final(tmp_path):
    task, candidate = task_and_candidate(tmp_path)
    agent = ExploratoryLLMCheckpointSynthesisAgent(client=FakeClient([final(candidate)]), max_steps=1)
    with pytest.raises(SynthesisError, match="no FINAL|did not produce"):
        agent.synthesize(task, tmp_path)


def test_full_still_rejects_unread_source_citation(tmp_path):
    task, candidate = task_and_candidate(tmp_path)
    candidate["source_evidence"][0]["source_path"] = "never-read.js"
    agent = ExploratoryLLMCheckpointSynthesisAgent(client=FakeClient([
        'ACTION: read_file\n{"path":"game.js"}', final(candidate)
    ]), max_steps=3)
    with pytest.raises(SynthesisError, match="did not read"):
        agent.synthesize(task, tmp_path)


def test_no_source_format_repair_does_not_reintroduce_source_requirement(tmp_path):
    task, candidate = task_and_candidate(tmp_path, empty_evidence=True)
    client = FakeClient(['FINAL\n```yaml\nnot yaml: [\n```', yaml.safe_dump(candidate)])
    agent = ExploratoryLLMCheckpointSynthesisAgent(client=client, max_steps=2, ablation_variant="no_source_exploration")
    output = agent.synthesize(task, tmp_path)
    assert output.source_evidence == []
    repair = client.messages[-1][-1]["content"]
    assert "source_evidence must be []" in repair
    assert "source_evidence a non-empty list" not in repair


def test_no_assertion_policy_repairs_once_instead_of_silently_dropping_output(tmp_path):
    task, candidate = task_and_candidate(tmp_path)
    repaired = copy.deepcopy(candidate)
    repaired["generated_spec"]["assertions"] = []
    client = FakeClient(['ACTION: read_file\n{"path":"game.js"}', final(candidate), yaml.safe_dump(repaired)])
    agent = ExploratoryLLMCheckpointSynthesisAgent(client=client, max_steps=3, ablation_variant="no_assertion_prediction")
    output = agent.synthesize(task, tmp_path, tmp_path / "artifact")
    assert output.model_generated_spec["assertions"] == []
    assert len(client.messages) == 3
    assert "do not generate predicted" in client.messages[0][0]["content"]
    assert "generated_spec.assertions must be []" in client.messages[-1][-1]["content"]
    assert "tile_value_present" in (tmp_path / "artifact/raw-response.txt").read_text()


def test_contract_projection_keeps_signature_and_bounds_but_removes_semantic_prose():
    original = {"_note": "Put results in assertions", "strategy": ["native_save"],
                "direction": "left|right|up|down", "hp": "int 0..20",
                "value": "concrete scalar goal value", "id": "integer (-1 = current player)",
                "board": "4x4 integer matrix before action", "path": "map title from map.yaml"}
    projected = project_contract(original)
    assert projected == {"strategy": ["native_save"], "direction": "left|right|up|down",
                         "hp": "int 0..20", "value": "scalar", "id": "integer",
                         "board": "4x4 integer matrix", "path": "string"}
    assert "_note" in original


def test_schema_only_does_not_restore_contract_prose_during_repair(tmp_path):
    task, _ = task_and_candidate(tmp_path)
    full = build_explore_system_prompt(task)
    projected = build_explore_system_prompt(task, ablation_variant="schema_only_contract")
    repair = build_checkpoint_format_repair_prompt(task, "broken", [], ablation_variant="schema_only_contract")
    assert "integer tile value that must be visible on the board" in full
    for prompt in (projected, repair):
        assert "integer tile value that must be visible on the board" not in prompt
        assert "localStorage_restore_reload" in prompt
        assert "tile_value_present" in prompt


def openra_spec():
    return {
        "name": "settings", "benchmark": "OpenRA",
        "target": {"kind": "openra_settings", "mod": "ra", "settings": {"Game": {"TargetLines": "Manual"}}},
        "checkpoint": {"allowed_strategies": ["openra_public_settings_restore"], "expected_validity": "save-restorable"},
        "actions": [{"type": "openra_set_setting", "section": "Game", "key": "TargetLines", "value": "Automatic"}],
        "assertions": [],
    }


def test_openra_auxiliary_exception_is_scoped_and_does_not_relax_actions():
    spec = openra_spec()
    assert validate_spec_schema(spec, "spec.yaml")
    with ablation_scope("no_assertion_prediction"):
        assert not validate_spec_schema(spec, "spec.yaml")
        broken = copy.deepcopy(spec)
        broken["actions"] = []
        assert validate_spec_schema(broken, "spec.yaml")
    assert validate_spec_schema(spec, "spec.yaml")


def test_concurrent_variants_do_not_share_auxiliary_validation_policy():
    barrier = Barrier(2)
    def worker(variant):
        with ablation_scope(variant):
            barrier.wait()
            return validate_spec_schema(openra_spec(), "spec.yaml")
    with ThreadPoolExecutor(max_workers=2) as pool:
        full = pool.submit(worker, "full")
        ablated = pool.submit(worker, "no_assertion_prediction")
        assert full.result()
        assert not ablated.result()


def test_model_cannot_select_no_source_policy_with_an_output_field(tmp_path):
    task, candidate = task_and_candidate(tmp_path, empty_evidence=True)
    candidate["ablation_variant"] = "no_source_exploration"
    errors = formal_c1_checkpoint_contract_errors(candidate, task=task)
    assert any("illegal top-level" in error for error in errors)
    assert any("non-empty" in error for error in errors)


def test_unknown_variant_fails_before_client_call():
    with pytest.raises(ValueError, match="Unknown"):
        build_synthesis_agent("llm", explore=True, ablation_variant="typo")
