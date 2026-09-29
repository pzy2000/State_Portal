from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from warptest import cost, p5_aggregate, state_loop
from warptest.c1_budgets import C1_STEP_BUDGET, C1_STEP_BUDGETS


def _cost() -> dict:
    leaf = {
        "calls": 1,
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "reasoning_tokens": 1,
        "cached_prompt_tokens": 0,
        "total_tokens": 5,
        "latency_ms": 7,
        "incomplete_calls": 0,
        "unknown_prompt_token_calls": 0,
        "unknown_completion_token_calls": 0,
        "unknown_reasoning_token_calls": 0,
        "unknown_total_token_calls": 0,
        "usage_complete": True,
    }
    return {
        "llm_calls": 1,
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "reasoning_tokens": 1,
        "cached_prompt_tokens": 0,
        "total_tokens": 5,
        "llm_latency_ms": 7,
        "incomplete_calls": 0,
        "unknown_prompt_token_calls": 0,
        "unknown_completion_token_calls": 0,
        "unknown_reasoning_token_calls": 0,
        "unknown_total_token_calls": 0,
        "usage_complete": True,
        "by_model": {"model": dict(leaf)},
        "by_kind": {"gui_policy": dict(leaf)},
        "by_kind_model": {"gui_policy": {"model": dict(leaf)}},
    }


def _zero_cost() -> dict:
    value = _cost()
    top_fields = (
        "llm_calls",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cached_prompt_tokens",
        "total_tokens",
        "llm_latency_ms",
    )
    leaf_fields = (
        "calls",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "cached_prompt_tokens",
        "total_tokens",
        "latency_ms",
    )
    for field in top_fields:
        value[field] = 0
    for mapping in (
        value["by_model"],
        value["by_kind"],
        value["by_kind_model"]["gui_policy"],
    ):
        for bucket in mapping.values():
            for field in leaf_fields:
                bucket[field] = 0
    return value


def _browser_snapshot(seed: int, calls: int) -> dict:
    return {
        "source": "browser_page_runtime",
        "observed": True,
        "algorithm": "mulberry32",
        "seed": seed,
        "calls": calls,
        "viewport": {"width": 1280, "height": 800},
        "device_scale_factor": 1,
    }


def _openra_snapshot(seed: int, process_id: int) -> dict:
    return {
        "schema_version": 1,
        "source": "openra_live_settings_process",
        "observed": True,
        "algorithm": "not_applicable_public_settings",
        "seed": seed,
        "process_id": process_id,
        "process_alive_at_observation": True,
        "benchmark": "OpenRA",
        "task_id": "task",
        "semantic_fingerprint": {
            "Game": {"TargetLines": "Manual", "ViewportEdgeScroll": True}
        },
    }


def _unity_snapshot(
    seed: int, process_id: int, sequence: int, *, benchmark: str = "jynew"
) -> dict:
    return {
        "schema_version": 1,
        "source": "unity_live_process",
        "observed": True,
        "algorithm": "not_applicable_unity_gui",
        "seed": seed,
        "process_id": process_id,
        "process_alive_at_observation": True,
        "benchmark": benchmark,
        "task_id": "task",
        "protocol_version": "warptest-c1-unity-v3",
        "sequence": sequence,
        "semantic_fingerprint": {
            "source": f"{benchmark}_live_semantic_probe_v1",
            "status": "success",
            "ok": True,
            "checks": [{"name": "live", "status": "success"}],
        },
    }


def _native_result(benchmark: str, *, seed: int = 2) -> dict:
    result = _browser_result(seed=seed)
    normalized = benchmark.lower()
    if normalized == "openra":
        source = "openra_live_settings_process"
        algorithm = "not_applicable_public_settings"
        initial = _openra_snapshot(seed, 401)
        final = _openra_snapshot(seed, 401)
    else:
        source = "unity_live_process"
        algorithm = "not_applicable_unity_gui"
        unity_benchmark = "pal3" if normalized == "pal3" else "jynew"
        initial = _unity_snapshot(seed, 501, 3, benchmark=unity_benchmark)
        final = _unity_snapshot(seed, 501, 7, benchmark=unity_benchmark)
    result.update(
        benchmark=benchmark,
        prng=final,
        seed_evidence={
            "schema_version": 1,
            "source": source,
            "observed": True,
            "algorithm": algorithm,
            "seed": seed,
            "task_id": "task",
            "initial": initial,
            "final": final,
        },
    )
    return result


def _browser_result(*, seed: int = 2) -> dict:
    initial = _browser_snapshot(seed, 3)
    final = _browser_snapshot(seed, 5)
    result = {
        "task_id": "task",
        "benchmark": "2048",
        "horizon_class": "short",
        "seed": seed,
        "incomplete": False,
        "before_visual_ok": True,
        "before_semantic_ok": True,
        "after_visual_ok": True,
        "after_semantic_ok": True,
        "start_success": True,
        "goal_success": True,
        "e2e_success": True,
        "time_to_start_ms": 10,
        "time_start_to_goal_ms": 20,
        "time_e2e_excluding_synthesis_ms": 30,
        "time_e2e_including_synthesis_ms": 30,
        "checkpoint_synthesis_ms": 0,
        "checkpoint_restore_ms": 0,
        "runtime_bootstrap_ms": 2,
        "grader_ms": 3,
        "termination_time_ms": 30,
        "decision_budget": C1_STEP_BUDGET,
        "steps": {"phase_a": 1, "phase_b": 1, "total": 2},
        "budget_exhausted": False,
        "runtime_started": True,
        "policy_phase_started": True,
        "visual_grading_started": True,
        "prng": final,
        "seed_evidence": {
            "schema_version": 1,
            "source": "browser_page_runtime",
            "observed": True,
            "algorithm": "mulberry32",
            "seed": seed,
            "initial": initial,
            "final": final,
        },
        "initial_state_fingerprint": "from-start-contract",
        "cost": _cost(),
        "synthesis_actual_cost": _zero_cost(),
        "synthesis_transient_actual_cost": _zero_cost(),
        "synthesis_attributed_cost": _zero_cost(),
    }
    task_actual = cost.merge_summaries([result["cost"]])
    synthesis_actual = cost.merge_summaries([result["synthesis_actual_cost"]])
    synthesis_transient = cost.merge_summaries(
        [result["synthesis_transient_actual_cost"]]
    )
    synthesis_attributed = cost.merge_summaries(
        [result["synthesis_attributed_cost"]]
    )
    accounting = {
        "actual_cost": synthesis_actual,
        "transient_actual_cost": synthesis_transient,
        "attributed_cost": synthesis_attributed,
    }
    attempt = cost.merge_summaries([task_actual, synthesis_attributed])
    attempt.update(
        {
            "attempt_status": "treatment",
            "retry_category": None,
            "retry_reason": None,
            "task_actual_cost": task_actual,
            "synthesis_accounting": accounting,
        }
    )
    attempts, ledger_hash = state_loop._bind_attempt_ledger(
        [attempt], task_id="task"
    )
    result.update(
        cost=cost.merge_summaries([task_actual, synthesis_attributed]),
        actual_cost=cost.merge_summaries([task_actual, synthesis_actual]),
        transient_infrastructure_cost=cost.merge_summaries(
            [synthesis_transient]
        ),
        synthesis_actual_cost=synthesis_actual,
        synthesis_transient_actual_cost=synthesis_transient,
        synthesis_attributed_cost=synthesis_attributed,
        attempt_costs=attempts,
        attempt_count=1,
        attempt_ledger_hash=ledger_hash,
    )
    return result


def _report(seed: int = 2) -> dict:
    return {
        "schema_version": 2,
        "complete": True,
        "model": "model",
        "seed": seed,
        "step_budgets": dict(C1_STEP_BUDGETS),
    }


def _luanti_result(*, node_name: str = "basenodes:dirt", seed: int = 2) -> dict:
    result = _browser_result(seed=seed)
    snapshot = {
        "source": "luanti_live_semantic_probe",
        "version": 1,
        "task_id": "task",
        "nodes": [
            {
                "pos": {"x": 2, "y": 1, "z": 2},
                "name": node_name,
                "param1": 0,
                "param2": 0,
            }
        ],
        "ranges": [],
        "fields": [{"path": "node.2,1,2", "value": node_name}],
        "player": {"present": True, "name": "warptest"},
        "inventory": [],
        "mod_storage": [],
        "meta_inventory": [],
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            snapshot,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    evidence = {
        "schema_version": 1,
        "source": "luanti_warm_session",
        "observed": True,
        "algorithm": "luanti_fixed_map_seed",
        "seed": seed,
        "map_seed": seed,
        "process_alive_at_observation": True,
        "render": True,
        "frame": {"width": 1280, "height": 720},
        "map_backend": "sqlite3",
        "gameid": "devtest",
        "creative_mode": True,
        "git_commit": "abc",
        "world_path": "/tmp/world",
        "config_path": "/tmp/minetest.conf",
        "config_fingerprint": f"config-{seed}",
        "semantic_observed": True,
        "initial_semantic_source": "luanti_live_semantic_probe",
        "initial_semantic_stage": "entry",
        "initial_semantic_fingerprint": fingerprint,
        "initial_semantic_observation": snapshot,
    }
    result.update(
        benchmark="luanti",
        horizon_class="long",
        decision_budget=100,
        prng=dict(evidence),
        seed_evidence=evidence,
    )
    return result


def test_normalize_requires_live_browser_readback_not_planned_prng() -> None:
    valid, valid_issues = p5_aggregate._normalize_c1_trial(
        "c1_gui", _report(), _browser_result()
    )
    assert valid["complete"], valid_issues
    assert valid["runtime_started"] is True

    planned_only = _browser_result()
    planned_only.pop("seed_evidence")
    planned_only["prng"] = {"algorithm": "mulberry32", "seed": 2}
    invalid, issues = p5_aggregate._normalize_c1_trial(
        "c1_gui", _report(), planned_only
    )
    assert not invalid["complete"]
    assert "missing runtime-observed seed_evidence" in issues


def test_steps_require_started_runtime_but_prelaunch_synthesis_zero_is_allowed() -> None:
    invalid_result = _browser_result()
    invalid_result.update(
        runtime_started=False,
        prng=None,
        seed_evidence={
            "schema_version": 1,
            "source": "runtime_not_started",
            "observed": False,
            "benchmark": "2048",
        },
    )
    _, issues = p5_aggregate._normalize_c1_trial(
        "c1_gui_synth", _report(), invalid_result
    )
    assert "steps.total > 0 requires runtime_started=true" in issues

    early_failure = deepcopy(invalid_result)
    early_failure["steps"] = {"phase_a": 0, "phase_b": 0, "total": 0}
    early_failure.update(
        before_visual_ok=False,
        before_semantic_ok=False,
        after_visual_ok=False,
        after_semantic_ok=False,
        start_success=False,
        goal_success=False,
        e2e_success=False,
        synthesis_cache_fingerprint="synth-fingerprint",
        synthesis_attributed_fingerprints=["synth-fingerprint"],
        synthesis_attributed_burdens=[],
        synthesis_cache_hit=False,
        synthesis_actual_incurred_this_attempt=False,
        policy_phase_started=False,
        visual_grading_started=False,
    )
    trial, early_issues = p5_aggregate._normalize_c1_trial(
        "c1_gui_synth", _report(), early_failure
    )
    assert trial["complete"], early_issues


def test_pairing_uses_observed_runtime_values_and_keeps_synth_contract_check() -> None:
    a, _ = p5_aggregate._normalize_c1_trial("c1_gui", _report(), _browser_result())
    c_result = _browser_result()
    c_result["seed_evidence"]["initial"]["calls"] = 99
    c, _ = p5_aggregate._normalize_c1_trial("c1_agent_s", _report(), c_result)

    not_started = _browser_result()
    not_started.update(
        runtime_started=False,
        policy_phase_started=False,
        visual_grading_started=False,
        prng=None,
        seed_evidence={
            "schema_version": 1,
            "source": "runtime_not_started",
            "observed": False,
            "benchmark": "2048",
        },
        steps={"phase_a": 0, "phase_b": 0, "total": 0},
        initial_state_fingerprint="shared-synth-contract",
    )
    b, _ = p5_aggregate._normalize_c1_trial("c1_gui_synth", _report(), not_started)
    d_result = deepcopy(not_started)
    d_result["initial_state_fingerprint"] = "different-synth-contract"
    d, _ = p5_aggregate._normalize_c1_trial(
        "c1_agent_s_synth", _report(), d_result
    )

    indices = {
        "c1_gui": {"model": {("task", 2): a}},
        "c1_gui_synth": {"model": {("task", 2): b}},
        "c1_agent_s": {"model": {("task", 2): c}},
        "c1_agent_s_synth": {"model": {("task", 2): d}},
    }
    issues = p5_aggregate._seed_evidence_issues(indices, ["task"])
    assert any("runtime-observed seed evidence differs" in issue for issue in issues)
    assert any("self-synthesized start contract differs" in issue for issue in issues)


def test_luanti_requires_task_bound_live_semantic_fingerprint() -> None:
    valid, valid_issues = p5_aggregate._normalize_c1_trial(
        "c1_gui", _report(), _luanti_result()
    )
    assert valid["complete"], valid_issues

    missing = _luanti_result()
    missing["seed_evidence"].pop("initial_semantic_fingerprint")
    invalid, issues = p5_aggregate._normalize_c1_trial("c1_gui", _report(), missing)
    assert not invalid["complete"]
    assert "Luanti initial semantic fingerprint is missing or malformed" in issues

    forged = _luanti_result()
    forged["seed_evidence"]["initial_semantic_observation"]["nodes"][0]["name"] = "air"
    invalid, issues = p5_aggregate._normalize_c1_trial("c1_gui", _report(), forged)
    assert not invalid["complete"]
    assert "Luanti initial semantic fingerprint does not match observation" in issues

    wrong_stage = _luanti_result()
    wrong_stage["seed_evidence"]["initial_semantic_stage"] = "restored"
    invalid, issues = p5_aggregate._normalize_c1_trial(
        "c1_gui", _report(), wrong_stage
    )
    assert not invalid["complete"]
    assert (
        "Luanti initial semantic evidence was captured at the wrong trial stage"
        in issues
    )


def test_luanti_pairing_detects_different_live_initial_semantics() -> None:
    a, a_issues = p5_aggregate._normalize_c1_trial(
        "c1_gui", _report(), _luanti_result(node_name="basenodes:dirt")
    )
    c, c_issues = p5_aggregate._normalize_c1_trial(
        "c1_agent_s", _report(), _luanti_result(node_name="air")
    )
    assert not a_issues
    assert not c_issues
    indices = {
        "c1_gui": {"model": {("task", 2): a}},
        "c1_gui_synth": {},
        "c1_agent_s": {"model": {("task", 2): c}},
        "c1_agent_s_synth": {},
    }
    issues = p5_aggregate._seed_evidence_issues(indices, ["task"])
    assert any("runtime-observed seed evidence differs" in issue for issue in issues)


def test_native_openra_and_unity_seed_evidence_normalize_as_live_runtime() -> None:
    for benchmark in ("OpenRA", "jynew", "PAL3"):
        trial, issues = p5_aggregate._normalize_c1_trial(
            "c1_agent_s", _report(), _native_result(benchmark)
        )
        assert trial["complete"], (benchmark, issues)
        assert trial["runtime_started"] is True


def test_native_seed_evidence_fails_closed_on_wrong_process_or_protocol() -> None:
    unity = _native_result("jynew")
    unity["seed_evidence"]["final"]["process_id"] = 777
    unity["seed_evidence"]["final"]["sequence"] = 1
    unity["seed_evidence"]["initial"]["protocol_version"] = "forged"
    trial, issues = p5_aggregate._normalize_c1_trial(
        "c1_agent_s", _report(), unity
    )
    assert not trial["complete"]
    assert "native final process_id differs from initial process_id" in issues
    assert "Unity final sequence precedes initial sequence" in issues
    assert "Unity initial seed snapshot has invalid protocol version" in issues

    wrong_source = _browser_result()
    wrong_source["benchmark"] = "OpenRA"
    trial, issues = p5_aggregate._normalize_c1_trial(
        "c1_agent_s", _report(), wrong_source
    )
    assert not trial["complete"]
    assert (
        "native seed evidence source must be openra_live_settings_process" in issues
    )


def test_native_seed_evidence_rejects_disguised_types_fields_and_prng_replay() -> None:
    openra = _native_result("OpenRA")
    openra["seed_evidence"]["schema_version"] = "1"
    openra["seed_evidence"]["initial"]["seed"] = "2"
    openra["seed_evidence"]["initial"]["process_id"] = "401"
    openra["seed_evidence"]["final"]["unregistered"] = True
    trial, issues = p5_aggregate._normalize_c1_trial(
        "c1_agent_s", _report(), openra
    )
    assert not trial["complete"]
    assert "native seed evidence schema_version must be integer 1" in issues
    assert "native initial observed seed does not match trial seed 2" in issues
    assert "native initial seed snapshot lacks a positive process_id" in issues
    assert "OpenRA final seed snapshot has invalid fields" in issues

    unity = _native_result("jynew")
    unity["seed_evidence"]["initial"]["sequence"] = "3"
    unity["prng"] = deepcopy(unity["prng"])
    unity["prng"]["task_id"] = "replayed-task"
    trial, issues = p5_aggregate._normalize_c1_trial(
        "c1_agent_s", _report(), unity
    )
    assert not trial["complete"]
    assert "Unity initial seed snapshot lacks a non-negative sequence" in issues
    assert (
        "native prng metadata is not the exact final task-bound snapshot" in issues
    )


def _formal_start_gate_trial(benchmark: str) -> dict:
    native = _native_result(benchmark)
    normalized = benchmark.lower()
    native_pid = 401 if normalized == "openra" else 501
    return {
        "arm": "c1_agent_s",
        "model": "model",
        "task_id": "task",
        "seed": 2,
        "benchmark": benchmark,
        "incomplete": False,
        "start_construction_status": "not_applicable",
        "start_grading_started": True,
        "start_gate_status": "passed",
        "goal_evaluation_status": "evaluated",
        "before_visual_status": "evaluated",
        "after_visual_status": "evaluated",
        "before_semantic_status": "success",
        "after_semantic_status": "success",
        "visual_grading_started": True,
        "seed_evidence": native["seed_evidence"],
        "start_grading_runtime_hold": {
            "required": True,
            "held": True,
            "released": True,
            "backend": "sigstop",
            "held_pid": native_pid,
        },
    }


def test_native_start_gate_requires_sigstop_bound_to_observed_process() -> None:
    for benchmark in ("OpenRA", "jynew", "PAL3"):
        trial = _formal_start_gate_trial(benchmark)
        assert p5_aggregate._c1_formal_start_gate_issues(trial) == []

        wrong_backend = deepcopy(trial)
        wrong_backend["start_grading_runtime_hold"]["backend"] = (
            "chromium_cdp_script_pause"
        )
        issues = p5_aggregate._c1_formal_start_gate_issues(wrong_backend)
        assert any("backend must be 'sigstop'" in issue for issue in issues)

        wrong_pid = deepcopy(trial)
        wrong_pid["start_grading_runtime_hold"]["held_pid"] += 1
        issues = p5_aggregate._c1_formal_start_gate_issues(wrong_pid)
        assert any("held_pid does not match" in issue for issue in issues)


def test_browser_start_gate_keeps_cdp_hold_contract() -> None:
    trial = _formal_start_gate_trial("jynew")
    trial.update(
        benchmark="2048",
        seed_evidence=_browser_result()["seed_evidence"],
        start_grading_runtime_hold={
            "required": True,
            "held": True,
            "released": True,
            "backend": "chromium_cdp_script_pause",
        },
    )
    assert p5_aggregate._c1_formal_start_gate_issues(trial) == []


def test_native_pairing_ignores_pid_but_compares_initial_semantics() -> None:
    a_result = _native_result("jynew")
    c_result = deepcopy(a_result)
    c_result["seed_evidence"]["initial"]["process_id"] = 901
    c_result["seed_evidence"]["final"]["process_id"] = 901
    a = {
        "runtime_started": True,
        "seed_evidence": a_result["seed_evidence"],
    }
    c = {
        "runtime_started": True,
        "seed_evidence": c_result["seed_evidence"],
    }
    indices = {
        "c1_gui": {"model": {("task", 2): a}},
        "c1_gui_synth": {},
        "c1_agent_s": {"model": {("task", 2): c}},
        "c1_agent_s_synth": {},
    }
    assert p5_aggregate._seed_evidence_issues(indices, ["task"]) == []

    c_result["seed_evidence"]["initial"]["semantic_fingerprint"]["checks"][0][
        "name"
    ] = "different-live-start"
    c["seed_evidence"] = c_result["seed_evidence"]
    issues = p5_aggregate._seed_evidence_issues(indices, ["task"])
    assert any("runtime-observed seed evidence differs" in issue for issue in issues)
