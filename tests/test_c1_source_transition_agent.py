from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import yaml

from warptest.c1_source_transition_agent import (
    SOURCE_TRANSITION_AGENT_MAX_STEPS,
    SOURCE_TRANSITION_AGENT_MAX_TOKENS,
    SourceTransitionActionAgent,
    build_source_transition_prompt,
    build_source_transition_request,
    parse_and_validate_source_transition,
)
from warptest.c1_evaluation import materialize_legacy_spec
from warptest.runner import load_spec
from warptest.state_loop import (
    _initialize_minimal_smoke_result,
    _plan_minimal_smoke_transition,
    load_state_loop_tasks,
)
from warptest.synthesis import (
    public_action_contract_for_task,
    public_assertion_contract_for_task,
    synthesize_spec_from_public_issue,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, object]]] = []

    def converse(
        self, _model: str, messages: list[dict[str, object]]
    ) -> str:
        self.calls.append(copy.deepcopy(messages))
        if not self.responses:
            raise AssertionError("unexpected source-transition call")
        return self.responses.pop(0)


def task_and_spec(task_id: str = "2048-merge-left"):
    task = next(
        task
        for task in load_state_loop_tasks(ROOT, "benchmarks/p5/c1_tasks.yaml")
        if task.id == task_id
    )
    return task, synthesize_spec_from_public_issue(task.issue)


def final_payload(task, spec, *, actions=None, assertions=None) -> str:
    payload = {
        "task_id": task.id,
        "benchmark": task.benchmark,
        "actions": copy.deepcopy(actions if actions is not None else spec["actions"]),
        "assertions": copy.deepcopy(
            assertions if assertions is not None else spec["assertions"]
        ),
        "source_evidence": [
            {
                "source_path": "js/game_manager.js",
                "matched_symbol": "move",
                "rationale": "GameManager move applies the public directional transition.",
            }
        ],
    }
    return "FINAL\n```yaml\n" + yaml.safe_dump(payload, sort_keys=False) + "```"


def test_all_61_tasks_have_public_action_and_assertion_contracts() -> None:
    tasks = load_state_loop_tasks(ROOT, "benchmarks/p5/c1_tasks.yaml")
    assert len(tasks) == 61
    for task in tasks:
        assert public_action_contract_for_task(task.issue)
        assert public_assertion_contract_for_task(task.issue)
    for task in tasks:
        if task.id.startswith("openra-") and (
            "chain" in task.id or task.id.endswith("combined-arms-rally")
        ):
            assertions = public_assertion_contract_for_task(task.issue)
            assert any(
                row.get("type") == "openra_actor_built"
                for row in assertions
            )


def test_all_61_tasks_route_actions_and_auxiliary_assertions_through_v2_parser() -> None:
    tasks = load_state_loop_tasks(ROOT, "benchmarks/p5/c1_tasks.yaml")
    benchmarks: set[str] = set()
    for task in tasks:
        spec = {
            key: value
            for key, value in materialize_legacy_spec(
                load_spec(ROOT / task.oracle_spec)
            ).items()
            if key not in {"evaluation", "visual_oracle"}
        }
        raw = yaml.safe_dump(
            {
                "task_id": task.id,
                "benchmark": task.benchmark,
                "actions": spec["actions"],
                "assertions": spec["assertions"],
                "source_evidence": [
                    {
                        "source_path": "public-source.txt",
                        "matched_symbol": "public-transition",
                        "rationale": "offline routing fixture",
                    }
                ],
            },
            sort_keys=False,
        )
        actions, assertions, evidence, errors = (
            parse_and_validate_source_transition(
                raw,
                task=task,
                accepted_spec=spec,
                files_read=["public-source.txt"],
            )
        )
        assert not errors, (task.id, errors)
        assert actions and assertions and evidence
        benchmarks.add(task.benchmark)
    assert len(benchmarks) == 10


def test_request_includes_start_setup_source_and_both_contracts_without_old_transition() -> None:
    task, spec = task_and_spec()
    candidate = copy.deepcopy(spec)
    candidate["setup"] = {"public_mode": "mocked"}
    candidate["actions"] = [
        {"type": "move", "direction": "left", "private_action": 991337}
    ]
    candidate["assertions"] = [
        {"type": "tile_at", "private_assertion": "DO_NOT_LEAK"}
    ]
    request = build_source_transition_request(
        task,
        candidate,
        repo=ROOT / "games/2048",
        repo_map="repo/js/game_manager.js",
    )
    prompt = build_source_transition_prompt(request)
    assert request["accepted_start"]["setup"] == {"public_mode": "mocked"}
    assert request["accepted_start"]["target"] == candidate["target"]
    assert request["allowed_action_schema"]
    assert request["allowed_assertion_schema"]
    assert request["budget"] == {
        "max_source_steps": 16,
        "max_response_tokens_per_call": 8192,
        "format_repairs": 1,
        "action_count_upper_bound": None,
    }
    assert "991337" not in prompt
    assert "DO_NOT_LEAK" not in prompt
    for forbidden in ("oracle_spec", "golden", "grader", "screenshot"):
        assert forbidden not in prompt.lower()


def test_parser_has_no_eight_action_cap_and_requires_assertions_and_real_evidence() -> None:
    task, spec = task_and_spec()
    actions = [
        {"type": "move", "direction": "left"} for _ in range(17)
    ]
    raw = yaml.safe_dump(
        {
            "task_id": task.id,
            "benchmark": task.benchmark,
            "actions": actions,
            "assertions": spec["assertions"],
            "source_evidence": [
                {
                    "source_path": "js/game_manager.js",
                    "matched_symbol": "move",
                    "rationale": "move supports the transition",
                }
            ],
        },
        sort_keys=False,
    )
    parsed_actions, assertions, evidence, errors = (
        parse_and_validate_source_transition(
            raw,
            task=task,
            accepted_spec=spec,
            files_read=["js/game_manager.js"],
        )
    )
    assert not errors
    assert parsed_actions is not None and len(parsed_actions) == 17
    assert assertions
    assert evidence

    empty_assertions = yaml.safe_load(raw)
    empty_assertions["assertions"] = []
    parsed_actions, _, _, errors = parse_and_validate_source_transition(
        yaml.safe_dump(empty_assertions, sort_keys=False),
        task=task,
        accepted_spec=spec,
        files_read=["js/game_manager.js"],
    )
    assert parsed_actions is None
    assert "assertions must be a non-empty list" in errors

    unread = yaml.safe_load(raw)
    unread["source_evidence"][0]["source_path"] = "js/unread.js"
    parsed_actions, _, _, errors = parse_and_validate_source_transition(
        yaml.safe_dump(unread, sort_keys=False),
        task=task,
        accepted_spec=spec,
        files_read=["js/game_manager.js"],
    )
    assert parsed_actions is None
    assert any("was not observed" in error for error in errors)


def test_source_loop_generates_actions_assertions_evidence_and_caches(
    tmp_path: Path,
) -> None:
    task, spec = task_and_spec()
    client = FakeClient(
        'ACTION: read_file\n{"path":"js/game_manager.js","start":1,"end":80}',
        final_payload(task, spec),
    )
    agent = SourceTransitionActionAgent(
        model="fake-model",
        provider="fake-provider",
        base_url="https://example.invalid/v1",
        api_key_env="UNUSED",
        temperature=0.2,
        cache_root=tmp_path / "cache",
        client=client,
    )
    first = agent.plan(
        task,
        spec,
        base=ROOT,
        seed=0,
        artifact_dir=tmp_path / "first",
        pair_start_fingerprint="pair",
    )
    assert first.status == "generated"
    assert first.usable
    assert first.tool_call_count == 1
    assert first.actions
    assert first.assertions
    assert first.source_evidence
    assert first.to_report()["assertions_auxiliary_only"] is True
    assert (
        first.to_report()["assertions_ignored_for_authoritative_grading"]
        is True
    )
    assert len(client.calls) == 2

    second = agent.plan(
        task,
        spec,
        base=ROOT,
        seed=0,
        artifact_dir=tmp_path / "second",
        pair_start_fingerprint="pair",
    )
    assert second.cache_hit is True
    assert second.generation_ms == 0
    assert len(client.calls) == 2


def test_source_loop_rejects_zero_tool_final_before_accepting_transition(
    tmp_path: Path,
) -> None:
    task, spec = task_and_spec()
    final = final_payload(task, spec)
    client = FakeClient(
        final,
        'ACTION: read_file\n{"path":"js/game_manager.js","start":1,"end":20}',
        final,
    )
    agent = SourceTransitionActionAgent(
        model="fake-model",
        provider="fake-provider",
        base_url="https://example.invalid/v1",
        api_key_env="UNUSED",
        temperature=0.2,
        cache_root=None,
        client=client,
    )
    plan = agent.plan(
        task,
        spec,
        base=ROOT,
        seed=0,
        artifact_dir=tmp_path / "plan",
    )
    assert plan.status == "generated"
    assert plan.tool_call_count == 1
    assert len(client.calls) == 3
    transcript = Path(plan.transcript_path or "").read_text(encoding="utf-8")
    assert "zero_tool_final_rejected" in transcript


def test_source_loop_runs_exactly_one_public_format_repair(tmp_path: Path) -> None:
    task, spec = task_and_spec()
    invalid = {
        "task_id": task.id,
        "benchmark": task.benchmark,
        "actions": spec["actions"],
        "assertions": [],
        "source_evidence": [
            {
                "source_path": "js/game_manager.js",
                "matched_symbol": "move",
                "rationale": "supports transition",
            }
        ],
    }
    repaired = copy.deepcopy(invalid)
    repaired["assertions"] = spec["assertions"]
    client = FakeClient(
        'ACTION: read_file\n{"path":"js/game_manager.js"}',
        "FINAL\n```yaml\n"
        + yaml.safe_dump(invalid, sort_keys=False)
        + "```",
        yaml.safe_dump(repaired, sort_keys=False),
    )
    agent = SourceTransitionActionAgent(
        model="fake-model",
        provider="fake-provider",
        base_url="https://example.invalid/v1",
        api_key_env="UNUSED",
        temperature=0.2,
        cache_root=None,
        client=client,
    )
    plan = agent.plan(
        task,
        spec,
        base=ROOT,
        seed=0,
        artifact_dir=tmp_path / "repair",
    )
    assert plan.status == "repaired"
    assert plan.repair_used is True
    assert plan.assertions
    assert len(client.calls) == 3
    assert plan.repair_prompt_path is not None
    repair_prompt = Path(plan.repair_prompt_path).read_text(encoding="utf-8")
    assert "assertions must be a non-empty list" in repair_prompt


def test_source_loop_uses_16_steps_and_8192_tokens_by_default() -> None:
    assert SOURCE_TRANSITION_AGENT_MAX_STEPS == 16
    assert SOURCE_TRANSITION_AGENT_MAX_TOKENS == 8192


def test_source_cache_is_sensitive_to_pair_start_fingerprint(tmp_path: Path) -> None:
    task, spec = task_and_spec()
    action = 'ACTION: read_file\n{"path":"js/game_manager.js"}'
    final = final_payload(task, spec)
    client = FakeClient(action, final, action, final)
    agent = SourceTransitionActionAgent(
        model="fake-model",
        provider="fake-provider",
        base_url="https://example.invalid/v1",
        api_key_env="UNUSED",
        temperature=0.2,
        cache_root=tmp_path / "cache",
        client=client,
    )
    first = agent.plan(
        task,
        spec,
        base=ROOT,
        seed=0,
        artifact_dir=tmp_path / "one",
        pair_start_fingerprint="pair-one",
    )
    second = agent.plan(
        task,
        spec,
        base=ROOT,
        seed=0,
        artifact_dir=tmp_path / "two",
        pair_start_fingerprint="pair-two",
    )
    assert first.cache_hit is False
    assert second.cache_hit is False
    assert len(client.calls) == 4


def test_runtime_transition_uses_source_actions_but_not_auxiliary_assertions(
    tmp_path: Path,
) -> None:
    task, spec = task_and_spec()
    actions = ({"type": "move", "direction": "left"},)
    auxiliary = ({"type": "tile_at", "x": 0, "y": 0, "value": 4},)
    report = {
        "status": "generated",
        "actions": list(actions),
        "assertions": list(auxiliary),
        "source_evidence": [
            {
                "source_path": "js/game_manager.js",
                "matched_symbol": "move",
                "rationale": "supports transition",
            }
        ],
        "assertions_auxiliary_only": True,
        "assertions_ignored_for_authoritative_grading": True,
    }
    plan = SimpleNamespace(
        status="generated",
        usable=True,
        actions=actions,
        assertions=auxiliary,
        generation_ms=10,
        to_report=lambda: copy.deepcopy(report),
    )
    planner = SimpleNamespace(
        plan=lambda *_args, **_kwargs: plan,
    )
    result = {
        "start_success": True,
    }
    _initialize_minimal_smoke_result(
        result, transition_action_source="source_agent_v2"
    )
    transition = _plan_minimal_smoke_transition(
        result,
        planner=planner,
        base=ROOT,
        task=task,
        generated_spec=spec,
        task_dir=tmp_path,
        seed=0,
    )
    assert transition is not None
    assert transition["actions"] == list(actions)
    assert transition["assertions"] == []
    assert result["source_transition_agent"]["assertions"] == list(auxiliary)
    assert result["original_generated_actions_ignored"] is True
