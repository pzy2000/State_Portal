from __future__ import annotations

import copy
import json
from pathlib import Path

from warptest.c1_minimal_smoke_agent import (
    MinimalSmokeActionAgent,
    build_minimal_smoke_prompt,
    build_minimal_smoke_request,
    parse_and_validate_minimal_actions,
)
from warptest.state_loop import load_state_loop_tasks
from warptest.state_loop import _finalize_minimal_smoke_verdict
from warptest.state_loop import (
    _initialize_minimal_smoke_result,
    _mark_transition_action_execution_started,
    _mark_transition_planner_called,
    _stamp_minimal_core_audit,
    _write_state_loop_incremental,
    run_state_loop_benchmark,
)
from warptest.synthesis import (
    build_checkpoint_format_repair_prompt,
    build_explore_system_prompt,
    public_action_contract_for_task,
    synthesize_spec_from_public_issue,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, object]]] = []

    def converse(self, _model: str, messages: list[dict[str, object]]) -> str:
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError("unexpected planner call")
        return self.responses.pop(0)


def task_and_spec(task_id: str = "2048-merge-left"):
    task = next(
        task
        for task in load_state_loop_tasks(ROOT, "benchmarks/p5/c1_tasks.yaml")
        if task.id == task_id
    )
    return task, synthesize_spec_from_public_issue(task.issue)


def planner(tmp_path: Path, planning: FakeClient, repair: FakeClient):
    return MinimalSmokeActionAgent(
        model="fake-model",
        provider="fake-provider",
        base_url="https://example.invalid/v1",
        api_key_env="UNUSED_TEST_KEY",
        temperature=0.2,
        cache_root=tmp_path / "cache",
        planning_client=planning,
        repair_client=repair,
    )


def test_all_formal_tasks_have_public_action_contract() -> None:
    tasks = load_state_loop_tasks(ROOT, "benchmarks/p5/c1_tasks.yaml")
    assert len(tasks) == 61
    assert all(public_action_contract_for_task(task.issue) for task in tasks)
    gameplay = {
        task.id: public_action_contract_for_task(task.issue)
        for task in tasks
        if task.id
        in {
            "openra-allied-medium-tank-tech-chain",
            "openra-soviet-rocket-infantry-chain",
            "openra-allied-combined-arms-rally",
        }
    }
    assert set(gameplay) == {
        "openra-allied-medium-tank-tech-chain",
        "openra-soviet-rocket-infantry-chain",
        "openra-allied-combined-arms-rally",
    }
    assert all(
        any(action.get("type") == "openra_build_actor" for action in actions)
        for actions in gameplay.values()
    )


def test_jupyter_action_contract_exposes_and_requires_cell_index() -> None:
    task, spec = task_and_spec("jupyterlab-notebook-cell-output")
    contract = public_action_contract_for_task(task.issue)
    run_cell = next(
        action
        for action in contract
        if action.get("type") == "jupyterlab_run_notebook_cell"
    )
    assert set(run_cell) == {"type", "path", "cell_index"}

    actions, errors = parse_and_validate_minimal_actions(
        json.dumps(
            {
                "actions": [
                    {
                        "type": "jupyterlab_run_notebook_cell",
                        "path": "notebooks/cell-output.ipynb",
                    }
                ]
            }
        ),
        task=task,
        accepted_spec=spec,
        max_actions=8,
    )
    assert actions is None
    assert any("cell_index is required" in error for error in errors)


def test_blender_action_contract_is_shared_and_rejects_object_name_alias() -> None:
    task, spec = task_and_spec("blender-scene-cube-material")
    contract = public_action_contract_for_task(task.issue)
    location = next(
        row for row in contract if row.get("type") == "blender_set_object_location"
    )
    assert set(location["required"]) == {"object", "location"}
    assert location["optional"] == {}
    assert location["additional_fields_allowed"] is False

    request = build_minimal_smoke_request(task, spec)
    assert request["allowed_action_schema"] == contract
    explore_prompt = build_explore_system_prompt(task.issue)
    assert "Public semantic action schema" in explore_prompt
    assert "blender_set_object_location" in explore_prompt
    assert "object: non-empty existing Blender object name" in explore_prompt

    actions, errors = parse_and_validate_minimal_actions(
        json.dumps(
            {
                "actions": [
                    {
                        "type": "blender_set_object_location",
                        "object_name": "WarpCube",
                        "location": [1, 2, 3],
                    }
                ]
            }
        ),
        task=task,
        accepted_spec=spec,
        max_actions=8,
    )
    assert actions is None
    assert any("unknown fields: ['object_name']" in error for error in errors)
    assert any(".object is required" in error for error in errors)

    repair_prompt = build_checkpoint_format_repair_prompt(
        task.issue,
        "generated_spec:\n  actions:\n  - type: blender_set_object_location\n    object_name: WarpCube\n    location: [1, 2, 3]\n",
        errors,
    )
    assert "public_action_schema" in repair_prompt
    assert "object: non-empty existing Blender object name" in repair_prompt
    assert "object_name" in repair_prompt


def test_blender_wrong_alias_triggers_one_format_repair(tmp_path: Path) -> None:
    task, spec = task_and_spec("blender-scene-cube-material")
    planning = FakeClient(
        json.dumps(
            {
                "actions": [
                    {
                        "type": "blender_set_object_location",
                        "object_name": "WarpCube",
                        "location": [1, 2, 3],
                    }
                ]
            }
        )
    )
    repair = FakeClient(
        json.dumps(
            {
                "actions": [
                    {
                        "type": "blender_set_object_location",
                        "object": "WarpCube",
                        "location": [1, 2, 3],
                    }
                ]
            }
        )
    )
    agent = planner(tmp_path, planning, repair)
    plan = agent.plan(task, spec, seed=0, artifact_dir=tmp_path / "blender")

    assert plan.status == "repaired"
    assert plan.repair_used is True
    assert plan.actions == (
        {
            "type": "blender_set_object_location",
            "object": "WarpCube",
            "location": [1, 2, 3],
        },
    )
    assert len(planning.calls) == 1
    assert len(repair.calls) == 1
    repair_user = repair.calls[0][-1]["content"]
    assert "object_name" in str(repair_user)
    assert "unknown fields" in str(repair_user)


def test_prompt_contains_only_public_task_start_projection_and_action_contract() -> None:
    task, spec = task_and_spec()
    candidate = copy.deepcopy(spec)
    candidate["actions"] = [
        {"type": "move", "direction": "left", "wait_ms": 123456789}
    ]
    candidate["assertions"][0]["private_sentinel"] = "DO_NOT_LEAK_ASSERTION"
    candidate["setup"] = {"private_sentinel": "DO_NOT_LEAK_SETUP"}
    request = build_minimal_smoke_request(task, candidate)
    prompt = build_minimal_smoke_prompt(request)
    assert request["preconditions"] == task.preconditions
    assert request["reference_steps"] == task.reference_steps
    assert request["start"] == {
        "target": candidate["target"],
        "checkpoint": candidate["checkpoint"],
    }
    assert "123456789" not in prompt
    assert "DO_NOT_LEAK_ASSERTION" not in prompt
    assert "DO_NOT_LEAK_SETUP" not in prompt
    assert "oracle_spec" not in prompt
    assert "golden" not in prompt.lower()


def test_strict_action_parser_rejects_fences_extra_keys_empty_and_overflow() -> None:
    task, spec = task_and_spec()
    for raw in (
        '```json\n{"actions":[{"type":"move","direction":"left"}]}\n```',
        '{"actions":[{"type":"move","direction":"left"}],"notes":"x"}',
        '{"actions":[]}',
        json.dumps(
            {
                "actions": [
                    {"type": "move", "direction": "left"} for _ in range(9)
                ]
            }
        ),
    ):
        actions, errors = parse_and_validate_minimal_actions(
            raw, task=task, accepted_spec=spec, max_actions=8
        )
        assert actions is None
        assert errors


def test_one_format_repair_produces_usable_plan_and_cache_prevents_resample(
    tmp_path: Path,
) -> None:
    task, spec = task_and_spec()
    planning = FakeClient("not-json")
    repair = FakeClient('{"actions":[{"type":"move","direction":"left"}]}')
    agent = planner(tmp_path, planning, repair)
    first = agent.plan(task, spec, seed=0, artifact_dir=tmp_path / "first")
    assert first.status == "repaired"
    assert first.usable
    assert first.repair_used is True
    assert [action["type"] for action in first.actions] == ["move"]
    assert len(planning.calls) == 1
    assert len(repair.calls) == 1
    assert "calls" not in first.to_report()["cost"]

    second = agent.plan(task, spec, seed=0, artifact_dir=tmp_path / "second")
    assert second.status == "repaired"
    assert second.cache_hit is True
    assert second.generation_ms == 0
    assert len(planning.calls) == 1
    assert len(repair.calls) == 1


def test_repair_failure_is_terminal_invalid_without_old_action_fallback(
    tmp_path: Path,
) -> None:
    task, spec = task_and_spec()
    original = copy.deepcopy(spec["actions"])
    agent = planner(tmp_path, FakeClient("not-json"), FakeClient('{"actions":[]}'))
    plan = agent.plan(task, spec, seed=0, artifact_dir=tmp_path / "invalid")
    assert plan.status == "invalid"
    assert not plan.usable
    assert plan.actions == ()
    assert spec["actions"] == original
    assert plan.errors


def test_authoritative_verdict_mapping_keeps_incomplete_and_start_not_evaluated() -> None:
    start_failed = {
        "transition_action_source": "minimal_agent_v1",
        "start_success": False,
        "incomplete": False,
    }
    _finalize_minimal_smoke_verdict(start_failed)
    assert start_failed["smoke_action_verdict"] == "not_evaluated_start_failed"

    passed = {
        "transition_action_source": "minimal_agent_v1",
        "start_success": True,
        "goal_success": True,
        "e2e_success": True,
        "after_semantic_ok": True,
        "after_visual_ok": True,
        "minimal_smoke_agent": {"status": "generated"},
    }
    _finalize_minimal_smoke_verdict(passed)
    assert passed["smoke_action_verdict"] == "pass"
    assert passed["smoke_action_failure_stage"] is None

    incomplete = {
        "transition_action_source": "minimal_agent_v1",
        "start_success": True,
        "incomplete": True,
        "minimal_smoke_agent": {"status": "incomplete"},
    }
    _finalize_minimal_smoke_verdict(incomplete)
    assert incomplete["smoke_action_verdict"] == "incomplete"
    assert incomplete["smoke_action_failure_stage"] == "infrastructure"


def test_minimal_core_audit_records_order_without_sha_dependency() -> None:
    row = {
        "task_id": "t",
        "model": "M",
        "seed": 0,
        "accepted_start_id": "run:M:0:t",
        "transition_action_source": "source_agent_v2",
        "before_semantic_ok": True,
        "before_visual_ok": True,
        "start_success": True,
        "start_grading_started": True,
        "goal_success": False,
        "e2e_success": False,
        "incomplete": False,
        "cost": {"usage_complete": True},
    }
    _initialize_minimal_smoke_result(
        row, transition_action_source="source_agent_v2"
    )
    _mark_transition_planner_called(row)
    row["source_transition_agent"] = {"status": "generated"}
    assert row["action_execution_started"] is False
    _mark_transition_action_execution_started(row)
    _stamp_minimal_core_audit(row)
    assert row["planner_called"] is True
    assert row["planner_called_after_start"] is True
    assert row["action_execution_started"] is True
    assert row["usage_complete"] is True
    assert row["minimal_core_audit"]["accepted_start_id"] == "run:M:0:t"
    assert "original_generated_actions_sha256" not in row["minimal_core_audit"]


def test_minimal_core_audit_does_not_reconstruct_event_order() -> None:
    row = {
        "task_id": "t",
        "transition_action_source": "source_agent_v2",
        "start_success": False,
        "start_grading_started": False,
        "cost": {"usage_complete": True},
    }
    _initialize_minimal_smoke_result(
        row, transition_action_source="source_agent_v2"
    )
    _mark_transition_planner_called(row)
    row["source_transition_agent"] = {"status": "generated"}

    # A later successful Start must not rewrite the already-observed ordering.
    row["start_success"] = True
    row["start_grading_started"] = True
    _stamp_minimal_core_audit(row)

    assert row["planner_called"] is True
    assert row["planner_called_after_start"] is False
    assert row["action_execution_started"] is False


def test_minimal_report_uses_independent_nonformal_envelope(tmp_path: Path) -> None:
    report = tmp_path / "state-minimal-agent.json"
    _write_state_loop_incremental(
        report,
        results=[],
        model="M",
        seed=0,
        complete=True,
        run_meta=None,
        git_root=ROOT,
        synthesis_agent="llm_explore",
        restore_diagnostics={"enabled": False, "task_ids": []},
        task_timeout_s=2400.0,
        arm="c1_state_minimal_agent",
        transition_action_source="minimal_agent_v1",
    )
    payload = json.loads(report.read_text())
    assert payload["schema"] == "c1-state-minimal-agent-v1"
    assert payload["schema_version"] == 1
    assert payload["arm"] == "c1_state_minimal_agent"
    assert payload["formal"] is False
    assert payload["excluded_from_formal"] is True


def test_source_agent_v2_report_uses_versioned_nonformal_envelope(
    tmp_path: Path,
) -> None:
    report = tmp_path / "state-minimal-agent.json"
    _write_state_loop_incremental(
        report,
        results=[],
        model="M",
        seed=0,
        complete=True,
        run_meta=None,
        git_root=ROOT,
        synthesis_agent="llm_explore",
        restore_diagnostics={"enabled": False, "task_ids": []},
        task_timeout_s=2400.0,
        arm="c1_state_minimal_agent",
        transition_action_source="source_agent_v2",
    )
    payload = json.loads(report.read_text())
    assert payload["schema"] == "c1-state-source-agent-v2"
    assert payload["schema_version"] == 2
    assert payload["experiment"] == "state_loop_source_transition_agent"
    assert payload["transition_action_source"] == "source_agent_v2"
    assert payload["formal"] is False
    assert payload["excluded_from_formal"] is True


def test_minimal_mode_requires_llm_or_explicit_fake_planner() -> None:
    try:
        run_state_loop_benchmark(
            ROOT,
            tasks_path="benchmarks/p5/c1_tasks.yaml",
            agent_kind="deterministic",
            arm="c1_state_minimal_agent",
            transition_action_source="minimal_agent_v1",
        )
    except ValueError as error:
        assert "explicit fake planner factory" in str(error)
    else:
        raise AssertionError("minimal diagnostic must reject an implicit fake planner")

    try:
        run_state_loop_benchmark(
            ROOT,
            tasks_path="benchmarks/p5/c1_tasks.yaml",
            agent_kind="deterministic",
            arm="c1_state_minimal_agent",
            transition_action_source="source_agent_v2",
        )
    except ValueError as error:
        assert "explicit fake planner factory" in str(error)
    else:
        raise AssertionError("source-agent v2 must reject an implicit fake planner")


def test_production_clients_use_distinct_planning_and_repair_cost_kinds(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> FakeClient:
        calls.append(dict(kwargs))
        return FakeClient('{"actions":[{"type":"move","direction":"left"}]}')

    agent = MinimalSmokeActionAgent(
        model="M",
        provider="P",
        base_url="https://example.invalid/v1",
        api_key_env="UNUSED",
        temperature=0.2,
        cache_root=tmp_path,
        client_factory=factory,
    )
    assert [call["cost_kind"] for call in calls] == [
        "smoke_action_planning",
        "smoke_action_format_repair",
    ]
    assert all(call["max_tokens"] == 2048 for call in calls)
    assert all(call["reasoning_retry_max_tokens"] is None for call in calls)
    agent.close()
