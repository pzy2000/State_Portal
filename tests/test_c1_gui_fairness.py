from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from warptest import cost, gui_agent
from warptest.state_loop import StateLoopTask
from warptest.task_schema import PublicIssue


def _task(*, benchmark: str = "2048") -> StateLoopTask:
    issue = PublicIssue(
        id=f"{benchmark}-task",
        benchmark=benchmark,
        repo=f"games/{benchmark}",
        issue="legacy state",
    )
    return StateLoopTask(
        issue=issue,
        oracle_spec="oracle.yaml",
        state_description="legacy state",
        preconditions="seeded public start",
        reference_steps="perform one transition",
        goal_description="INDEPENDENT_SENTINEL_GOAL",
        horizon_class="long" if benchmark == "luanti" else "short",
    )


def _provider() -> SimpleNamespace:
    return SimpleNamespace(
        name="relay",
        base_url="https://relay.example/v1",
        api_key_env="RELAY_API_KEY",
        extra_headers={"X-End-User": "tester"},
    )


def test_phase_b_public_checklist_uses_split_fields_without_state_description() -> None:
    task = _task()
    prompt = gui_agent._public_phase_b_instruction(task)
    assert "c1-public-checklist-v4" in prompt
    assert "perform one transition" in prompt
    assert "INDEPENDENT_SENTINEL_GOAL" not in prompt
    assert "verify that the intended visible state changed" in prompt
    assert "do not repeat the same ineffective coordinate" in prompt
    assert "legacy state" not in prompt


def test_new_c1_result_omits_goal_text() -> None:
    result = gui_agent._new_gui_result(
        _task(),
        start_mode="from_start",
        policy_kind="agent_s",
        model="model",
        seed=0,
        max_steps=20,
    )
    assert "goal_description" not in result
    assert "INDEPENDENT_SENTINEL_GOAL" not in gui_agent.json.dumps(result)


def _spec(*, name: str = "shared") -> dict:
    return {
        "name": name,
        "benchmark": "2048",
        "target": {"kind": "board"},
        "checkpoint": {
            "allowed_strategies": ["localStorage_restore_reload"],
            "expected_validity": "save-restorable",
        },
        "actions": [{"type": "move", "direction": "left"}],
        "assertions": [{"type": "tile_value_present", "value": 4}],
    }


def _synthesis_agent(checkpoint: object) -> MagicMock:
    agent = MagicMock()

    def synthesize(*_args, **_kwargs):
        cost.record_call(
            "model",
            prompt_tokens=7,
            completion_tokens=4,
            reasoning_tokens=2,
            total_tokens=11,
            latency_ms=13,
            kind="checkpoint_synthesis",
        )
        return checkpoint

    agent.synthesize.side_effect = synthesize
    return agent


def test_provider_fallback_is_infrastructure_not_fair_model_failure(
    tmp_path: Path,
) -> None:
    task = _task()
    checkpoint = SimpleNamespace(
        generated_spec=_spec(),
        model_generated_spec=None,
        used_public_scaffold=True,
        payload={
            "model_fallback_reason": (
                "LLM API call failed during source exploration: gateway unavailable"
            )
        },
        runtime_validation={},
    )
    with (
        patch("warptest.gui_agent.resolve_model", return_value=("model", _provider())),
        patch(
            "warptest.gui_agent.build_synthesis_agent",
            return_value=_synthesis_agent(checkpoint),
        ),
        patch.dict("os.environ", {"RELAY_API_KEY": "unused"}, clear=False),
    ):
        outcome = gui_agent._obtain_self_synthesis(
            task,
            base=tmp_path,
            seed=0,
            synth_model="model@relay",
            base_url="https://unused.example/v1",
            api_key_env="RELAY_API_KEY",
            temperature=1.0,
            max_explore_steps=12,
            artifact_dir=tmp_path / "artifacts",
            policy_kind="gui_agent",
            shared_cache_root=tmp_path / "cache",
        )

    assert outcome["status"] == "infrastructure_error"
    assert outcome["synth_status"] == "provider_failure"
    assert outcome["self_synthesis_cache"]["cache_hit"] is False


def test_d_first_then_b_reuses_identical_successful_synthesis_and_cost(
    tmp_path: Path,
) -> None:
    task = _task()
    checkpoint = SimpleNamespace(
        generated_spec=_spec(),
        model_generated_spec=_spec(),
        used_public_scaffold=False,
    )
    synthesis_agent = _synthesis_agent(checkpoint)
    build = MagicMock(return_value=synthesis_agent)
    cache_root = tmp_path / "run" / "c1-self-synthesis-cache"

    def obtain(policy_kind: str, artifact_name: str) -> tuple[dict, dict]:
        with cost.scoped_meter() as task_meter:
            outcome = gui_agent._obtain_self_synthesis(
                task,
                base=tmp_path,
                seed=5,
                synth_model="model@relay",
                base_url="https://unused.example/v1",
                api_key_env="RELAY_API_KEY",
                temperature=0.2,
                max_explore_steps=12,
                artifact_dir=tmp_path / artifact_name,
                policy_kind=policy_kind,
                shared_cache_root=cache_root,
            )
            cost.record_call(
                "model",
                prompt_tokens=3,
                completion_tokens=2,
                total_tokens=5,
                kind="gui_policy",
            )
        result = {
            "self_synthesis_cost": outcome["self_synthesis_cost"],
            "self_synthesis_cache": outcome["self_synthesis_cache"],
            "self_synthesis_outcome_status": outcome["status"],
        }
        attempt = gui_agent._gui_attempt_cost(
            result,
            task_meter.summary(),
            start_mode="synthesized",
        )
        return outcome, attempt

    with (
        patch.object(gui_agent, "resolve_model", return_value=("model", _provider())),
        patch.object(gui_agent, "build_synthesis_agent", build),
    ):
        d_outcome, d_cost = obtain("agent_s", "d-artifacts")
        b_outcome, b_cost = obtain("gui_agent", "b-artifacts")

    assert d_outcome["self_synthesis_cache"]["cache_hit"] is False
    assert b_outcome["self_synthesis_cache"]["cache_hit"] is True
    assert d_outcome["generated_spec"] == b_outcome["generated_spec"]
    assert d_outcome["checkpoint_synthesis_ms"] == b_outcome["checkpoint_synthesis_ms"]
    assert d_outcome["self_synthesis_cost"] == b_outcome["self_synthesis_cost"]
    assert d_cost["total_tokens"] == b_cost["total_tokens"] == 16
    assert d_cost["by_kind"]["checkpoint_synthesis"]["total_tokens"] == 11
    assert b_cost["by_kind"]["checkpoint_synthesis"]["total_tokens"] == 11
    d_accounting = d_cost["synthesis_accounting"]
    b_accounting = b_cost["synthesis_accounting"]
    assert d_accounting["cache_key_fingerprint"] == b_accounting["cache_key_fingerprint"]
    assert d_accounting["actual_incurred"] is True
    assert b_accounting["actual_incurred"] is False
    assert d_accounting["attributed"] is True
    assert b_accounting["attributed"] is True
    assert d_accounting["actual_cost"]["llm_calls"] == 1
    assert b_accounting["actual_cost"]["llm_calls"] == 0
    assert d_accounting["attributed_cost"] == b_accounting["attributed_cost"]
    build.assert_called_once()

    cache_entry = Path(b_outcome["self_synthesis_cache"]["cache_entry"])
    assert cache_entry.exists()
    payload = gui_agent.json.loads(cache_entry.read_text(encoding="utf-8"))
    assert payload["producer"]["policy_kind"] == "agent_s"
    assert payload["cache_key"]["seed"] == 5
    assert "policy_kind" not in payload["cache_key"]
    assert "goal_description" not in payload["cache_key"]["task"]
    assert "INDEPENDENT_SENTINEL_GOAL" not in gui_agent.json.dumps(
        payload["cache_key"]
    )


def test_luanti_fair_failure_is_cached_d_first_and_has_complete_early_schema(
    tmp_path: Path,
) -> None:
    task = _task(benchmark="luanti")
    checkpoint = SimpleNamespace(
        generated_spec={"name": "public-scaffold"},
        model_generated_spec=None,
        used_public_scaffold=True,
    )
    synthesis_agent = _synthesis_agent(checkpoint)
    build = MagicMock(return_value=synthesis_agent)
    cache_root = tmp_path / "run" / "c1-self-synthesis-cache"
    audit = {
        "status": "unsupported",
        "reference_reachable": False,
        "reason": "no scripted replay",
    }

    def run(policy_kind: str, out_name: str) -> dict:
        return gui_agent._run_luanti_agent_task(
            tmp_path,
            task,
            model="model@relay",
            base_url="https://unused.example/v1",
            api_key="test-key",
            api_key_env="RELAY_API_KEY",
            synth_model="model@relay",
            max_explore_steps=12,
            temperature=0.2,
            out_dir=tmp_path / out_name,
            max_steps=100,
            start_mode="synthesized",
            policy_kind=policy_kind,
            policy_client=MagicMock(),
            observer=None,
            seed=9,
            shared_synthesis_cache=cache_root,
        )

    with (
        patch.object(gui_agent, "resolve_task_repo", return_value=tmp_path),
        patch.object(
            gui_agent,
            "load_spec",
            return_value={"checkpoint": {"expected_validity": "injection-only"}},
        ),
        patch.object(gui_agent, "audit_scripted_reference_replay", return_value=audit),
        patch.object(gui_agent, "resolve_model", return_value=("model", _provider())),
        patch.object(gui_agent, "build_synthesis_agent", build),
        patch.object(gui_agent, "LuantiWarmSession") as session_cls,
    ):
        d_result = run("agent_s", "d")
        b_result = run("gui_agent", "b")

    build.assert_called_once()
    session_cls.assert_not_called()
    assert d_result["self_synthesis_cache"]["cache_hit"] is False
    assert b_result["self_synthesis_cache"]["cache_hit"] is True
    assert d_result["checkpoint_synthesis_ms"] == b_result["checkpoint_synthesis_ms"]
    assert d_result["self_synthesis_cost"] == b_result["self_synthesis_cost"]
    assert d_result["initial_state_fingerprint"] == b_result["initial_state_fingerprint"]

    for result in (d_result, b_result):
        assert result["status"] == "failure"
        assert result["incomplete"] is False
        assert result["used_public_scaffold"] is True
        assert result["injected_spec_source"] == "none"
        assert result["decision_budget"] == 100
        assert result["steps"] == {"phase_a": 0, "phase_b": 0, "total": 0}
        assert result["budget_exhausted"] is False
        assert result["runtime_started"] is False
        assert result["prng"] is None
        assert result["seed_evidence"] == {
            "schema_version": 1,
            "source": "runtime_not_started",
            "observed": False,
            "benchmark": "luanti",
        }
        assert result["initial_state_fingerprint"]
        assert result["initial_state_screenshot_hash"] is None
        for field in (
            "result",
            "before_visual_ok",
            "before_semantic_ok",
            "after_visual_ok",
            "after_semantic_ok",
            "start_success",
            "goal_success",
            "e2e_success",
            "stage2_pass",
            "stage2_visual_pass",
            "e2e_pass",
        ):
            assert isinstance(result[field], bool)
        for field in (
            "time_to_start_ms",
            "time_start_to_goal_ms",
            "time_e2e_excluding_synthesis_ms",
            "time_e2e_including_synthesis_ms",
            "termination_time_ms",
            "checkpoint_synthesis_ms",
            "checkpoint_restore_ms",
            "runtime_bootstrap_ms",
            "grader_ms",
        ):
            assert isinstance(result[field], int)


def test_seeded_start_contract_is_stable_while_exact_screenshot_hash_is_diagnostic(
    tmp_path: Path,
) -> None:
    task = _task()
    a = gui_agent._new_gui_result(
        task,
        start_mode="from_start",
        policy_kind="gui_agent",
        model="model",
        seed=2,
        max_steps=20,
    )
    c = gui_agent._new_gui_result(
        task,
        start_mode="from_start",
        policy_kind="agent_s",
        model="model",
        seed=2,
        max_steps=20,
    )
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"animated-frame-1")
    second.write_bytes(b"animated-frame-2")
    a["initial_state_screenshot_hash"] = gui_agent._screenshot_fingerprint(str(first))
    c["initial_state_screenshot_hash"] = gui_agent._screenshot_fingerprint(str(second))

    assert a["initial_state_fingerprint"] == c["initial_state_fingerprint"]
    assert a["initial_state_screenshot_hash"] != c["initial_state_screenshot_hash"]

    b = gui_agent._new_gui_result(
        task,
        start_mode="synthesized",
        policy_kind="gui_agent",
        model="model",
        seed=2,
        max_steps=20,
    )
    d = gui_agent._new_gui_result(
        task,
        start_mode="synthesized",
        policy_kind="agent_s",
        model="model",
        seed=2,
        max_steps=20,
    )
    gui_agent._apply_start_contract(
        b,
        task,
        seed=2,
        start_mode="synthesized",
        generated_spec=_spec(),
    )
    gui_agent._apply_start_contract(
        d,
        task,
        seed=2,
        start_mode="synthesized",
        generated_spec=_spec(),
    )
    assert b["initial_state_fingerprint"] == d["initial_state_fingerprint"]

    changed = dict(_spec())
    changed["name"] = "different"
    gui_agent._apply_start_contract(
        d,
        task,
        seed=2,
        start_mode="synthesized",
        generated_spec=changed,
    )
    assert b["initial_state_fingerprint"] != d["initial_state_fingerprint"]


def test_infrastructure_synthesis_failure_remains_incomplete_and_is_not_cached(
    tmp_path: Path,
) -> None:
    task = _task()
    synthesis_agent = MagicMock()
    synthesis_agent.synthesize.side_effect = TimeoutError("provider timed out")
    build = MagicMock(return_value=synthesis_agent)
    cache_root = tmp_path / "run" / "c1-self-synthesis-cache"

    with (
        patch.object(gui_agent, "resolve_model", return_value=("model", _provider())),
        patch.object(gui_agent, "build_synthesis_agent", build),
    ):
        outcomes = [
            gui_agent._obtain_self_synthesis(
                task,
                base=tmp_path,
                seed=6,
                synth_model="model@relay",
                base_url="https://unused.example/v1",
                api_key_env="RELAY_API_KEY",
                temperature=0.2,
                max_explore_steps=12,
                artifact_dir=tmp_path / f"attempt-{attempt}",
                policy_kind="agent_s" if attempt == 0 else "gui_agent",
                shared_cache_root=cache_root,
            )
            for attempt in range(2)
        ]

    assert build.call_count == 2
    assert all(
        outcome["self_synthesis_cache"]["cache_hit"] is False
        for outcome in outcomes
    )
    assert not list(cache_root.glob("*/*.json"))

    result = gui_agent._new_gui_result(
        task,
        start_mode="synthesized",
        policy_kind="agent_s",
        model="model",
        seed=6,
        max_steps=20,
    )
    assert gui_agent._apply_self_synthesis_result(
        result,
        outcomes[0],
        task,
        seed=6,
    ) is None
    assert result["status"] == "failure"
    assert result["incomplete"] is True
    assert result["initial_state_fingerprint"]
