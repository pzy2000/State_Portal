"""Regression for no-assertion Unity candidates at both actual admission gates."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from warptest.c1_spec_acceptance import accept_c1_generated_spec
from warptest.state_loop import (
    CachedSynthesisAgent,
    StateLoopTask,
    _unity_state_model_spec_errors,
)
from warptest.synthesis import SynthesisError
from warptest.synthesis_ablation import ablation_scope
from warptest.task_schema import MethodCheckpoint, PublicIssue


def unity_candidate(benchmark):
    strategy = {"jynew": "jynew_utility_save_validate", "pal3": "pal3_console_save_validate"}[benchmark]
    spec = {
        "name": "saved candidate", "benchmark": benchmark,
        "target": {"kind": "synthesized_state"},
        "checkpoint": {"allowed_strategies": [strategy], "expected_validity": "save-restorable"},
        "actions": [{"type": f"{benchmark}_save", "save_index": 0}],
        "assertions": [],
    }
    issue = PublicIssue("test-unity", benchmark, "games/test", "Save slot zero", tags=["c1"])
    task = StateLoopTask(issue=issue, oracle_spec="hidden.yaml", state_description="save")
    checkpoint = MethodCheckpoint(
        task_id=issue.id, benchmark=benchmark, generated_spec=spec,
        model_generated_spec=copy.deepcopy(spec), source_evidence=[], payload={},
        validity_class="save-restorable", runtime_validation={}, inputs_read=[],
    )
    return task, checkpoint


@pytest.mark.parametrize("benchmark", ["jynew", "pal3"])
def test_unity_no_assertions_passes_shared_cache_and_native_admission_only_in_scope(benchmark):
    task, checkpoint = unity_candidate(benchmark)
    inner = SimpleNamespace(name="saved", synthesize=lambda *a, **kw: copy.deepcopy(checkpoint))
    agent = CachedSynthesisAgent(
        inner, cache_root=None, base=Path.cwd(), seed=0,
        synth_model="fake", provider_name="fake", provider_base_url="http://fake",
        temperature=0.2, max_tokens=8192, explore=True, max_explore_steps=16,
    )
    agent.bind_task(task)
    for variant in ["full", "no_source_exploration", "schema_only_contract"]:
        with ablation_scope(variant), pytest.raises(SynthesisError, match="assertions must be a non-empty list"):
            agent.synthesize(task.issue)
    with ablation_scope("no_assertion_prediction"):
        accepted = agent.synthesize(task.issue)
        assert accepted.model_generated_spec == {**checkpoint.generated_spec, "experiment_seed": 0}
        assert not _unity_state_model_spec_errors(
            accepted.model_generated_spec, task=task,
            oracle_spec={"benchmark": benchmark}, spec_path=Path("candidate.yaml"),
        )
    assert _unity_state_model_spec_errors(
        accepted.model_generated_spec, task=task,
        oracle_spec={"benchmark": benchmark}, spec_path=Path("candidate.yaml"),
    )


@pytest.mark.parametrize("benchmark", ["jynew", "pal3"])
@pytest.mark.parametrize("field,value", [
    ("assertions", None), ("assertions", {}), ("assertions", [{"type": "invented"}]),
    ("actions", []), ("actions", [{"type": "invented"}]),
])
def test_no_assertions_does_not_relax_malformed_data_or_actions(benchmark, field, value):
    task, checkpoint = unity_candidate(benchmark)
    checkpoint.generated_spec[field] = value
    with ablation_scope("no_assertion_prediction"):
        assert not accept_c1_generated_spec(
            checkpoint.generated_spec, issue=task.issue, benchmark=benchmark, seed=0,
        ).accepted


def test_unity_admission_variant_is_thread_local():
    barrier = Barrier(2)
    task, checkpoint = unity_candidate("pal3")

    def check(variant):
        with ablation_scope(variant):
            barrier.wait()
            return accept_c1_generated_spec(
                checkpoint.generated_spec, issue=task.issue, benchmark="pal3", seed=0,
            ).accepted

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(check, ["no_assertion_prediction", "full"])) == [True, False]
