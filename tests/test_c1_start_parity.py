"""Start-metric parity between the state-managed arm and the synthesized GUI arm.

Both arms build their start with the same synthesis agent, so their Start
numbers are only comparable when they answer the same question (does the live
runtime satisfy the oracle target?) and pay the same synthesis burden.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from warptest import c1_start_surface_parity, cost, gui_agent, p5_aggregate, state_loop
from warptest.c1_start_semantics import (
    UNSUPPORTED_STATUS,
    merge_start_semantic,
    oracle_target,
    semantic_start_grade,
)
from warptest.c1_synthesis_cache import (
    C1_SYNTHESIS_CONSUMER,
    SELF_SYNTHESIS_CACHE_SCHEMA_VERSION,
    cache_entry_path,
    canonical_hash,
    synthesis_cache_key,
)
from warptest.model_request import model_request_profile_provenance
from warptest.models import PhaseResult
from warptest.synthesis import SynthesisError
from warptest.task_schema import MethodCheckpoint, PublicIssue


ORACLE_SPEC = {"target": {"board": [[2, 2, 0, 0]], "score": 0}}


class _Profile:
    benchmark = "2048"

    def __init__(self, status: str = "success") -> None:
        self.status = status
        self.seen: list[dict[str, object]] = []

    def _validate(self, runtime: object, target: dict[str, object]) -> PhaseResult:
        self.seen.append(dict(target))
        return PhaseResult(self.status, f"validate {self.status}")


# --- the shared predicate -------------------------------------------------


def test_semantic_start_grade_validates_against_the_oracle_target() -> None:
    profile = _Profile()

    ok, status, _ = semantic_start_grade(
        profile, object(), ORACLE_SPEC, prefer_public_surface=False
    )

    assert (ok, status) == (True, "success")
    assert profile.seen == [ORACLE_SPEC["target"]]


def test_semantic_start_grade_fails_when_the_live_state_misses_the_target() -> None:
    ok, status, _ = semantic_start_grade(
        _Profile("failure"), object(), ORACLE_SPEC, prefer_public_surface=False
    )

    assert (ok, status) == (False, "validate_failed")


def test_semantic_start_grade_reports_a_missing_validator_rather_than_passing() -> None:
    ok, status, _ = semantic_start_grade(
        SimpleNamespace(benchmark="whatever"),
        object(),
        ORACLE_SPEC,
        prefer_public_surface=False,
    )

    assert (ok, status) == (False, UNSUPPORTED_STATUS)


def test_oracle_target_tolerates_a_malformed_spec() -> None:
    assert oracle_target(None) == {}
    assert oracle_target({"target": "not-a-mapping"}) == {}


# --- the cross-arm report gate --------------------------------------------


def _parity_trial(**overrides: object) -> dict[str, object]:
    trial: dict[str, object] = {
        "incomplete": False,
        "generated_spec_hash": "spec-hash",
        "synthesis_attributed_cost": cost.CostMeter().summary(),
        # Only a report that recorded explicit stage metrics has a Start verdict
        # to compare; a legacy terminal-only report does not.
        "stage_metrics_source": "explicit_v1",
        "before_semantic_ok": True,
        "before_visual_ok": True,
        "start_success": True,
    }
    trial.update(overrides)
    return trial


def _parity_indices(
    state: dict[str, object], synth: dict[str, object]
) -> dict[str, dict[str, dict[tuple[str, int], dict[str, object]]]]:
    return {
        "c1_state": {"model": {("task-1", 0): state}},
        "c1_agent_s_synth": {"model": {("task-1", 0): synth}},
    }


def test_start_parity_gate_accepts_one_shared_start_and_one_verdict() -> None:
    issues = p5_aggregate._start_parity_issues(
        _parity_indices(_parity_trial(), _parity_trial()), ["task-1"]
    )

    assert issues == []


def test_start_parity_gate_reports_divergent_start_specs() -> None:
    """This is the failure the per-consumer cache produced on every task."""

    issues = p5_aggregate._start_parity_issues(
        _parity_indices(
            _parity_trial(generated_spec_hash="state-spec"),
            _parity_trial(generated_spec_hash="gui-spec"),
        ),
        ["task-1"],
    )

    assert issues == [
        "E_vs_D/model/task-1/seed0: paired arms restored different "
        "self-synthesized start specs"
    ]


def test_start_parity_gate_reports_a_start_verdict_disagreement() -> None:
    issues = p5_aggregate._start_parity_issues(
        _parity_indices(
            _parity_trial(before_visual_ok=False, start_success=False),
            _parity_trial(),
        ),
        ["task-1"],
    )

    assert issues == [
        "E_vs_D/model/task-1/seed0: paired arms disagree on before_visual_ok "
        "for the same start spec",
        "E_vs_D/model/task-1/seed0: paired arms disagree on start_success "
        "for the same start spec",
    ]


def test_start_parity_gate_skips_a_legacy_report_without_stage_metrics() -> None:
    issues = p5_aggregate._start_parity_issues(
        _parity_indices(
            _parity_trial(
                stage_metrics_source="legacy_terminal_only",
                before_visual_ok=False,
                start_success=False,
            ),
            _parity_trial(),
        ),
        ["task-1"],
    )

    assert issues == []


def test_start_parity_gate_accepts_one_shared_synthesis_failure() -> None:
    """A shared cache shares the failure too, so neither arm has a spec.

    The state arm records no hash and the synthesized GUI arm records the
    ``no-usable-model-spec`` sentinel. Both describe the same shared fair
    failure, which is agreement rather than a missing record.
    """

    state = _parity_trial(before_semantic_ok=False, before_visual_ok=False, start_success=False)
    del state["generated_spec_hash"]
    synth = _parity_trial(
        generated_spec_hash="no-usable-model-spec",
        before_semantic_ok=False,
        before_visual_ok=False,
        start_success=False,
    )

    issues = p5_aggregate._start_parity_issues(
        _parity_indices(state, synth), ["task-1"]
    )

    assert issues == []


def test_start_parity_gate_reports_a_one_sided_start_spec() -> None:
    state = _parity_trial()
    del state["generated_spec_hash"]

    issues = p5_aggregate._start_parity_issues(
        _parity_indices(state, _parity_trial()), ["task-1"]
    )

    assert issues == [
        "E_vs_D/model/task-1/seed0: only one paired arm restored a "
        "self-synthesized start spec"
    ]


def test_start_parity_gate_reads_the_gui_arm_fingerprint_field() -> None:
    synth = _parity_trial()
    synth["generated_spec_fingerprint"] = synth.pop("generated_spec_hash")

    issues = p5_aggregate._start_parity_issues(
        _parity_indices(_parity_trial(), synth), ["task-1"]
    )

    assert issues == []


# --- the two semantic surfaces --------------------------------------------


def test_surface_comparison_reports_agreement_between_both_surfaces() -> None:
    """The GUI arms read the production surface, the state arm the hook validator.

    Neither arm can use the other's interface, so the equivalence has to be
    measured rather than assumed.
    """

    with patch.object(
        c1_start_surface_parity,
        "grade_public_start",
        return_value=(True, "success", {"source": "public"}),
    ):
        comparison = c1_start_surface_parity.compare_start_semantic_surfaces(
            _Profile(), object(), ORACLE_SPEC
        )

    assert comparison["comparable"] is True
    assert comparison["agree"] is True
    assert comparison["hook"]["ok"] is True
    assert comparison["public"]["ok"] is True


def test_surface_comparison_names_a_disagreement() -> None:
    with patch.object(
        c1_start_surface_parity,
        "grade_public_start",
        return_value=(False, "validate_failed", {}),
    ):
        comparison = c1_start_surface_parity.compare_start_semantic_surfaces(
            _Profile(), object(), ORACLE_SPEC
        )

    assert comparison["agree"] is False


def test_surface_comparison_marks_a_benchmark_without_a_public_grader() -> None:
    with patch.object(
        c1_start_surface_parity, "grade_public_start", return_value=None
    ):
        comparison = c1_start_surface_parity.compare_start_semantic_surfaces(
            _Profile(), object(), ORACLE_SPEC
        )

    assert comparison["comparable"] is False
    assert comparison["agree"] is None


def test_surface_parity_gate_covers_exactly_the_production_surface_benchmarks() -> None:
    tasks = c1_start_surface_parity._public_surface_tasks(
        Path.cwd(), "benchmarks/p5/c1_tasks.yaml"
    )

    assert tasks, "the gate must cover at least one production-surface benchmark"
    assert {task.benchmark for task in tasks} == {"datingsim", "spacerpg"}


def test_surface_parity_gate_fails_closed_on_an_ungradable_task() -> None:
    rows = [
        {
            "task_id": "love-gym-workout",
            "cases": {
                "entry": {"comparable": True, "agree": True, "hook": {"ok": False}, "expected_ok": False},
            },
        },
        {"task_id": "spacerpg-station-save", "error": "RuntimeError: boom", "cases": {}},
    ]
    with (
        patch.object(c1_start_surface_parity, "_public_surface_tasks", return_value=[]),
        patch.object(c1_start_surface_parity, "_grade_task", side_effect=rows),
    ):
        payload = c1_start_surface_parity.run_surface_parity_gate(Path.cwd())

    # No tasks selected means no evidence, which must not read as parity.
    assert payload["passed"] is False
    assert payload["agreement_rate"] is None


# --- the conjunction ------------------------------------------------------


def test_merge_start_semantic_requires_both_restore_and_oracle_target() -> None:
    ok, status, detail = merge_start_semantic(
        restore_ok=True,
        restore_status="success",
        restore_detail={"phase": "restore"},
        oracle_grade=(False, "validate_failed", {"reason": "score mismatch"}),
    )

    assert (ok, status) == (False, "validate_failed")
    assert detail["oracle_target"]["status"] == "validate_failed"


def test_merge_start_semantic_fails_closed_without_oracle_evidence() -> None:
    ok, status, detail = merge_start_semantic(
        restore_ok=True,
        restore_status="success",
        oracle_grade=None,
    )

    assert (ok, status) == (False, UNSUPPORTED_STATUS)
    assert detail["oracle_target"]["status"] == UNSUPPORTED_STATUS


def test_merge_start_semantic_keeps_a_restore_failure_as_the_reported_status() -> None:
    ok, status, _ = merge_start_semantic(
        restore_ok=False,
        restore_status="restore_failure",
        oracle_grade=(True, "success", {}),
    )

    assert (ok, status) == (False, "restore_failure")


# --- Unity: oracle target expressed in the batch runner's assertions ------


def test_pal3_oracle_start_assertions_mirror_the_target_predicates() -> None:
    assertions = state_loop._unity_oracle_start_assertions(
        "pal3",
        {
            "target": {
                "kind": "synthesized_state",
                "scene_city": "q01",
                "scene_name": "b",
                "money": 500,
                "team_ids": [0, 1],
                "story_vars": [{"key": 32768, "value": 10401}],
            }
        },
    )

    assert assertions == [
        {"type": "pal3_money_gte", "int_value": 500},
        {"type": "pal3_team_contains", "actor_id": 0},
        {"type": "pal3_team_contains", "actor_id": 1},
        {
            "type": "pal3_story_var_equals",
            "var_id": 32768,
            "expected": "10401",
            "comparator": "equals",
        },
        {"type": "pal3_scene_is", "city": "q01", "scene": "b"},
    ]


def test_pal3_zero_world_region_threshold_keeps_registered_gte_semantics() -> None:
    assertions = state_loop._unity_oracle_start_assertions(
        "pal3",
        {
            "target": {
                "kind": "synthesized_state",
                "world_regions": [{"region": 1, "flag": 0}],
            }
        },
    )

    assert assertions == [
        {
            "type": "pal3_world_region_enabled",
            "region": 1,
            "int_value": 0,
        }
    ]


def test_jynew_oracle_start_assertions_mirror_the_target_predicates() -> None:
    assertions = state_loop._unity_oracle_start_assertions(
        "jynew",
        {
            "target": {
                "kind": "synthesized_state",
                "mod_id": "jyx1",
                "player_level": 10,
                "money": 3000,
                "team_ids": [0, 5],
                "items": [{"id": 101, "count": 3}],
            }
        },
    )

    assert assertions == [
        {
            "type": "jynew_role_attr",
            "role_id": 0,
            "attr": "Level",
            "expected": "10",
            "comparator": "equals",
        },
        {"type": "jynew_money_gte", "int_value": 3000},
        # Role 0 is skipped: the runner's target check treats it as always
        # present, so asserting it would be stricter than the target predicate.
        {"type": "jynew_team_contains", "role_id": 5},
        {
            "type": "jynew_item_count",
            "item_id": 101,
            "expected": "3",
            "comparator": "gte",
        },
    ]


def test_pal3_oracle_start_assertions_ignore_synthesis_directives() -> None:
    """Player placement tells the runtime how to build the state, not what to check."""

    assertions = state_loop._unity_oracle_start_assertions(
        "pal3",
        {
            "target": {
                "kind": "synthesized_state",
                "scene_city": "q01",
                "scene_name": "b",
                "team_ids": [0],
                "make_playable": True,
                "player_tile_x": 195,
                "player_tile_y": 212,
                "player_nav_layer": 0,
            }
        },
    )

    assert assertions == [
        {"type": "pal3_team_contains", "actor_id": 0},
        {"type": "pal3_scene_is", "city": "q01", "scene": "b"},
    ]


def test_unity_oracle_start_assertions_fail_closed_on_an_unmappable_field() -> None:
    assert (
        state_loop._unity_oracle_start_assertions(
            "jynew", {"target": {"skills": [{"id": 1, "level": 4}]}}
        )
        is None
    )
    assert state_loop._unity_oracle_start_assertions("pal3", {}) is None
    assert state_loop._unity_oracle_start_assertions("2048", ORACLE_SPEC) is None


def test_unity_check_split_separates_restore_from_oracle_checks() -> None:
    restore, oracle = state_loop._unity_check_split(
        {
            "checks": [
                {"name": "c1.target.scene", "status": "success"},
                {"name": "assertion[pal3_money_gte]", "status": "failure"},
            ]
        }
    )

    assert [check["name"] for check in restore] == ["c1.target.scene"]
    assert [check["name"] for check in oracle] == ["assertion[pal3_money_gte]"]


# --- the state arm's synthesis cache --------------------------------------


def _issue() -> PublicIssue:
    return PublicIssue("task-1", "2048", "games/2048", "issue")


def _checkpoint() -> MethodCheckpoint:
    spec = {
        "name": "fake",
        "benchmark": "2048",
        "target": {"board": [[2, 2, 0, 0]]},
        "checkpoint": {},
        "actions": [],
        "assertions": [],
    }
    return MethodCheckpoint(
        task_id="task-1",
        benchmark="2048",
        generated_spec=spec,
        source_evidence=[],
        payload={},
        validity_class="save-restorable",
        runtime_validation={},
        inputs_read=[],
        model_generated_spec=spec,
    )


def _cached_agent(inner: object, cache_root: Path | None) -> state_loop.CachedSynthesisAgent:
    agent = state_loop.CachedSynthesisAgent(
        inner,
        cache_root=cache_root,
        base=Path.cwd(),
        seed=0,
        synth_model="fake-model",
        provider_name="fake",
        provider_base_url="http://fake",
        temperature=1.0,
        max_tokens=8192,
        explore=True,
        max_explore_steps=16,
    )
    agent.bind_task(
        state_loop.StateLoopTask(
            issue=_issue(),
            oracle_spec="oracle.yaml",
            state_description="issue",
        )
    )
    return agent


def _record_one_call() -> None:
    cost.record_call(
        "fake-model",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        kind="checkpoint_synthesis",
    )


def test_state_cache_hit_reuses_the_checkpoint_and_charges_no_actual_spend(
    tmp_path: Path,
) -> None:
    calls = 0

    def synthesize(*_args, **_kwargs) -> MethodCheckpoint:
        nonlocal calls
        calls += 1
        _record_one_call()
        return _checkpoint()

    inner = SimpleNamespace(name="fake-agent", synthesize=synthesize)

    first = _cached_agent(inner, tmp_path)
    first.synthesize(_issue(), Path.cwd(), tmp_path / "artifacts-1")
    first_ledger = first.drain()

    second = _cached_agent(inner, tmp_path)
    checkpoint = second.synthesize(_issue(), Path.cwd(), tmp_path / "artifacts-2")
    second_ledger = second.drain()

    assert calls == 1
    # The shared acceptance seeds the spec before it is cached, so both arms
    # restore the same seeded spec rather than re-seeding a raw candidate.
    assert checkpoint.model_generated_spec == {
        **_checkpoint().model_generated_spec,
        "experiment_seed": 0,
    }
    assert first_ledger["cache_hit"] is False
    assert first_ledger["synthesis_actual_cost"]["llm_calls"] == 1
    assert second_ledger["cache_hit"] is True
    # A cache hit spends nothing but still carries the full comparison burden.
    assert second_ledger["synthesis_actual_cost"]["llm_calls"] == 0
    assert second_ledger["synthesis_attributed_cost"]["llm_calls"] == 1
    assert (
        second_ledger["checkpoint_synthesis_ms"]
        == first_ledger["checkpoint_synthesis_ms"]
    )


def test_the_two_synthesizing_arms_share_one_cache_key(tmp_path: Path) -> None:
    """One key for both arms, so they restore the same self-synthesized start.

    Keying per arm meant `c1_state` and `c1_agent_s_synth` rolled the synthesis
    model independently, and their Start rates then differed by sampling noise
    rather than by anything either method did.
    """

    task = state_loop.StateLoopTask(
        issue=_issue(),
        oracle_spec="oracle.yaml",
        state_description="issue",
    )
    shared = dict(
        identity_root=str(tmp_path),
        public_task={
            "public_issue": _issue().public_payload(),
            "preconditions": "issue",
            "reference_steps": "issue",
        },
        seed=0,
        synth_model="fake-model",
        model_request_profile=model_request_profile_provenance("fake-model"),
        provider_name="fake",
        provider_base_url="http://fake",
        temperature=1.0,
        max_tokens=8192,
        explore=True,
        max_explore_steps=16,
    )
    state_key = synthesis_cache_key(
        consumer=state_loop.STATE_SYNTHESIS_CACHE_CONSUMER, **shared
    )
    gui_key = synthesis_cache_key(consumer=C1_SYNTHESIS_CONSUMER, **shared)

    assert state_loop.STATE_SYNTHESIS_CACHE_CONSUMER == C1_SYNTHESIS_CONSUMER
    assert state_key == gui_key
    assert canonical_hash(state_key, length=32) == canonical_hash(gui_key, length=32)

    gui_built = gui_agent._self_synthesis_cache_key(
        task,
        base=tmp_path,
        seed=0,
        synth_model="fake-model",
        provider_name="fake",
        provider_base_url="http://fake",
        temperature=1.0,
        max_tokens=8192,
        explore=True,
        max_explore_steps=16,
    )
    assert gui_built["consumer"] == C1_SYNTHESIS_CONSUMER
    # The public-task triple is the only key field the two arms build with
    # separate code, so it has to agree field for field.
    assert gui_built["task"] == shared["public_task"]
    assert gui_built == {**state_key, "root": gui_built["root"]}


def test_state_cache_replays_a_fair_synthesis_failure(tmp_path: Path) -> None:
    calls = 0

    def synthesize(*_args, **_kwargs) -> MethodCheckpoint:
        nonlocal calls
        calls += 1
        _record_one_call()
        raise SynthesisError("C1 format repair still violates the contract")

    inner = SimpleNamespace(name="fake-agent", synthesize=synthesize)

    for _ in range(2):
        agent = _cached_agent(inner, tmp_path)
        with pytest.raises(SynthesisError):
            agent.synthesize(_issue(), Path.cwd(), tmp_path / "artifacts")

    assert calls == 1


def test_state_cache_never_persists_an_infrastructure_failure(tmp_path: Path) -> None:
    calls = 0

    def synthesize(*_args, **_kwargs) -> MethodCheckpoint:
        nonlocal calls
        calls += 1
        _record_one_call()
        raise RuntimeError("connection reset by peer")

    inner = SimpleNamespace(name="fake-agent", synthesize=synthesize)

    for _ in range(2):
        agent = _cached_agent(inner, tmp_path)
        with pytest.raises(RuntimeError, match="connection reset"):
            agent.synthesize(_issue(), Path.cwd(), tmp_path / "artifacts")

    assert calls == 2


def test_state_cache_entry_records_the_current_schema_version(tmp_path: Path) -> None:
    inner = SimpleNamespace(
        name="fake-agent",
        synthesize=lambda *_a, **_k: _checkpoint(),
    )
    agent = _cached_agent(inner, tmp_path)
    agent.synthesize(_issue(), Path.cwd(), tmp_path / "artifacts")

    entries = list(tmp_path.rglob("*.json"))
    assert entries, "a successful synthesis must be cached"
    payload = json.loads(entries[0].read_text(encoding="utf-8"))
    assert payload["schema_version"] == SELF_SYNTHESIS_CACHE_SCHEMA_VERSION
    assert payload["cache_key"]["consumer"] == C1_SYNTHESIS_CONSUMER
    # Both projections of the same checkpoint, so either arm can consume it.
    assert isinstance(payload["outcome"]["generated_spec"], dict)
    assert isinstance(
        payload["outcome"]["checkpoint"]["model_generated_spec"], dict
    )
    assert entries[0] == cache_entry_path(
        tmp_path, payload["cache_key_fingerprint"]
    )


def test_disabled_cache_re_synthesizes_every_time(tmp_path: Path) -> None:
    calls = 0

    def synthesize(*_args, **_kwargs) -> MethodCheckpoint:
        nonlocal calls
        calls += 1
        return _checkpoint()

    inner = SimpleNamespace(name="fake-agent", synthesize=synthesize)
    for _ in range(2):
        agent = _cached_agent(inner, None)
        agent.synthesize(_issue(), Path.cwd(), tmp_path / "artifacts")

    assert calls == 2


def test_feedback_driven_retries_bypass_the_cache(tmp_path: Path) -> None:
    calls = 0

    def synthesize(*_args, **_kwargs) -> MethodCheckpoint:
        nonlocal calls
        calls += 1
        return _checkpoint()

    inner = SimpleNamespace(name="fake-agent", synthesize=synthesize)
    agent = _cached_agent(inner, tmp_path)
    for _ in range(2):
        agent.synthesize(
            _issue(),
            Path.cwd(),
            tmp_path / "artifacts",
            feedback={"before": {"label": "incorrect"}},
        )

    assert calls == 2


def test_llm_state_runs_wrap_the_agent_with_the_run_scoped_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LLM path must reach the producer through the cache, not around it."""

    built: list[object] = []

    def fake_run_single_task(_base, task, agent, **_kwargs):
        built.append(agent)
        return {
            "task_id": task.id,
            "benchmark": task.benchmark,
            "status": "failure",
            "result": False,
            "cost": cost.CostMeter().summary(),
        }

    monkeypatch.setattr(
        state_loop, "build_synthesis_agent", lambda *a, **k: MagicMock()
    )
    monkeypatch.setattr(
        state_loop,
        "resolve_model",
        lambda model, **_k: (
            model,
            SimpleNamespace(
                name="fake",
                base_url="http://fake",
                api_key_env="FAKE_KEY",
                extra_headers=None,
            ),
        ),
    )
    monkeypatch.setattr(
        state_loop,
        "load_state_loop_tasks",
        lambda *_a, **_k: [
            state_loop.StateLoopTask(
                issue=_issue(),
                oracle_spec="oracle.yaml",
                state_description="issue",
            )
        ],
    )
    monkeypatch.setattr(state_loop, "_run_single_task", fake_run_single_task)
    monkeypatch.setattr(
        state_loop.run_context, "stamp_payload", lambda *_a, **_k: None
    )

    cache_root = tmp_path / "c1-self-synthesis-cache"
    state_loop.run_state_loop_benchmark(
        tmp_path,
        tasks_path="tasks.yaml",
        agent_kind="llm",
        report_dir=tmp_path / "report",
        shared_synthesis_cache=cache_root,
    )

    assert built and isinstance(built[0], state_loop.CachedSynthesisAgent)
    assert built[0]._cache_root == cache_root.resolve()


def test_state_trial_carries_the_same_synthesis_ledger_as_the_gui_arm() -> None:
    result: dict[str, object] = {}
    agent = _cached_agent(
        SimpleNamespace(name="fake", synthesize=MagicMock()), None
    )
    agent._record(
        actual_cost=cost.CostMeter().summary(),
        attributed_cost=cost.CostMeter().summary(),
        checkpoint_synthesis_ms=1234,
        cache_hit=True,
        cache_fingerprint="deadbeef",
    )

    state_loop._apply_state_synthesis_accounting(result, agent)

    assert result["synthesis_cache_hit"] is True
    assert result["synthesis_cache_fingerprint"] == "deadbeef"
    # A cache hit costs no wall time, so latency comes from the entry.
    assert result["checkpoint_synthesis_ms"] == 1234
    for field in (
        "synthesis_actual_cost",
        "synthesis_attributed_cost",
        "synthesis_transient_actual_cost",
    ):
        assert isinstance(result[field], dict)
