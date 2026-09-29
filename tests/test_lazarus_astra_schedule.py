from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from warptest.lazarus_p2p_only_experiment import (
    ASTRA_MODELS,
    EXPECTED_MODELS,
    build_p2p_only_plan,
    validate_p2p_only_manifest,
    _validate_api_preflight,
)
from warptest.lazarus_paper_catalog import PAPER_TASK_IDS, PAPER_CATALOG_PATH
from warptest.model_request import (
    model_request_profiles_environment,
    resolve_model_request_profile,
)
from warptest.lazarus_p2p_only_identity import schedule_sha256

ROOT = Path(__file__).resolve().parents[1]


def manifest():
    return yaml.safe_load(
        (ROOT / "benchmarks/p5/lazarus_p2p_only_astra_seed0_manifest.yaml").read_text()
    )


def test_astra_registered_matrix_and_authorization():
    m = manifest()
    contract = validate_p2p_only_manifest(
        m, catalog_path=ROOT / PAPER_CATALOG_PATH, task_ids=PAPER_TASK_IDS
    )
    plan = build_p2p_only_plan(
        PAPER_TASK_IDS, models=contract.models, seeds=contract.seeds
    )
    assert plan.complete and len(set(plan.keys)) == 66 and len(plan.groups) == 11
    assert all(len(group) == 6 for group in plan.groups)
    assert contract.paid_run_authorized and contract.expected_cells == 66
    assert contract.observer_model == "claude-sonnet-5@relay"
    assert contract.judge_model == "qwen3-vl-flash@relay"
    original = yaml.safe_load(
        (ROOT / "benchmarks/p5/lazarus_p2p_only_manifest.yaml").read_text()
    )
    assert schedule_sha256(m) != schedule_sha256(original)
    assert (
        build_p2p_only_plan(
            PAPER_TASK_IDS, models=EXPECTED_MODELS, seeds=(0, 1, 2)
        ).trial_count
        == 594
    )


@pytest.mark.parametrize(
    "models,seeds",
    [
        (ASTRA_MODELS, (1,)),
        (ASTRA_MODELS, (False,)),
        (EXPECTED_MODELS, (0,)),
        (("other@relay",), (0,)),
    ],
)
def test_unregistered_schedule_rejected(models, seeds):
    assert not build_p2p_only_plan(PAPER_TASK_IDS, models=models, seeds=seeds).complete


def test_astra_bad_authorization_and_catalog_rejected():
    m = manifest()
    m["authorization"]["scope"] = "c1-lazarus-p2p-only-11tasks-3models-seeds012-cde-v3"
    with pytest.raises(ValueError, match="scope"):
        validate_p2p_only_manifest(
            m, catalog_path=ROOT / PAPER_CATALOG_PATH, task_ids=PAPER_TASK_IDS
        )
    assert not build_p2p_only_plan(
        PAPER_TASK_IDS[:-1], models=ASTRA_MODELS, seeds=(0,)
    ).complete


def test_astra_wire_profile_and_preflight_validation():
    with model_request_profiles_environment(manifest()["model_request_profiles"]):
        for route in ("gpt-6-astra", "gpt-6-astra@relay"):
            assert resolve_model_request_profile(route).request_kwargs() == {
                "reasoning_effort": "none"
            }
        assert (
            resolve_model_request_profile("claude-sonnet-5@relay").request_kwargs()
            == {}
        )
        assert (
            resolve_model_request_profile("qwen3-vl-flash@relay").request_kwargs()
            == {}
        )
    proof = {
        "schema": "c1-lazarus-p2p-only-api-preflight-v1",
        "provider_profile_sha256": "test",
        "models": [
            {
                "model": ASTRA_MODELS[0],
                "complete": True,
                "thinking_disabled_verified": True,
                "request_overrides": {"reasoning_effort": "none"},
            }
        ],
        "fixed_routes": [{"roles": ["observer", "judge"], "complete": True}],
    }
    failures = []
    _validate_api_preflight(
        proof, failures, expected_provider_profile_sha256="test", models=ASTRA_MODELS
    )
    assert not failures
    wrong = deepcopy(proof)
    wrong["models"][0]["request_overrides"] = {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
    _validate_api_preflight(
        wrong, failures, expected_provider_profile_sha256="test", models=ASTRA_MODELS
    )
    assert any("override drift" in failure for failure in failures)


@pytest.mark.parametrize("kind", ["agent_s_worker", "agent_s_grounding"])
def test_astra_none_reaches_native_sdk_boundary(kind):
    from types import SimpleNamespace
    from warptest.agent_s_worker import (
        ChatCompletionsBackend,
        _NativeUsageRecordingCompletions,
    )

    calls = []
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="ok", reasoning_content=None),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=2,
            completion_tokens=3,
            total_tokens=5,
            prompt_tokens_details=None,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ),
    )

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return response

    usage = []
    backend = ChatCompletionsBackend(
        model="gpt-6-astra",
        base_url="https://provider.invalid/v1",
        api_key_env="TEST_UNUSED_KEY",
        default_headers={},
        temperature=0.2,
        timeout_s=30.0,
        max_retries=0,
        usage_sink=usage,
        client=object(),
    )
    proxy = _NativeUsageRecordingCompletions(Completions(), backend=backend, kind=kind)
    with model_request_profiles_environment(manifest()["model_request_profiles"]):
        proxy.create(model="gpt-6-astra", messages=[{"role": "user", "content": "act"}])
    assert calls[0]["model"] == "gpt-6-astra"
    assert calls[0]["reasoning_effort"] == "none"
    assert "extra_body" not in calls[0]
    assert usage[0]["request_contract_passed"] is True
    assert usage[0]["reasoning_tokens"] == 0


def test_astra_preflight_resume_accepts_registered_none_override():
    from scripts.preflight_lazarus_p2p_only_api import _valid_decision_row
    row = {"model": "gpt-6-astra@relay", "complete": True,
           "thinking_disabled_verified": True, "request_overrides": {"reasoning_effort": "none"}}
    assert _valid_decision_row(row)
    row["request_overrides"] = {"extra_body": {"thinking": {"type": "disabled"}}}
    assert not _valid_decision_row(row)
