import json
from pathlib import Path

import pytest
import yaml

from warptest.lazarus_collection import select_rows, key
from warptest.lazarus_p2p_only_experiment import (
    FORMAL_MODELS, MANIFEST_PATH, build_p2p_only_plan,
    validate_p2p_only_manifest, preflight_p2p_only, build_paid_authorization,
    write_run_report, write_run_index,
)
from warptest.lazarus_paper_catalog import PAPER_TASK_IDS, PAPER_CATALOG_PATH
from warptest.model_request import model_request_profiles_environment, resolve_model_request_profile

ROOT = Path(__file__).resolve().parents[1]


def manifest():
    return yaml.safe_load((ROOT / MANIFEST_PATH).read_text())


def validate(value):
    return validate_p2p_only_manifest(value, catalog_path=ROOT / PAPER_CATALOG_PATH, task_ids=PAPER_TASK_IDS)


def test_formal_seed0_contract_and_exact_rotation():
    contract = validate(manifest())
    assert contract.formal and contract.paid_run_authorized
    assert contract.models == FORMAL_MODELS and contract.seeds == (0,)
    plan = build_p2p_only_plan(PAPER_TASK_IDS, models=contract.models, seeds=contract.seeds)
    assert plan.complete and plan.trial_count == 440 and len(plan.groups) == 44
    for group in plan.groups:
        assert len(group) == 10
        assert [k.arm for k in group[:5]] == list(reversed([k.arm for k in group[5:]]))
        assert {k.version for k in group[:5]} == {"buggy"}
        assert {k.arm for k in group} == {
            "lazarus_agent_s",
            "lazarus_agent_s_synth",
            "lazarus_state",
            "lazarus_scalecua",
            "lazarus_scalecua_synth",
        }
    with model_request_profiles_environment(contract.model_request_profiles):
        for model in FORMAL_MODELS:
            expected = {"reasoning_effort": "none"} if model.startswith("gpt-") else {"extra_body": {"thinking": {"type": "disabled"}}}
            assert resolve_model_request_profile(model).request_kwargs() == expected


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(seeds=[1]),
    lambda m: m.update(seeds=[False]),
    lambda m: m.update(models=list(reversed(m["models"]))),
    lambda m: m.update(formal=False),
    lambda m: m.update(expected_cells=594),
    lambda m: m.update(authorization={"scope": "c1-lazarus-p2p-only-11tasks-astra-seed0-cde-v3", "exploratory_astra_66_paid_run": True}),
    lambda m: m["model_request_profiles"].update({"gpt-6-astra": "default"}),
])
def test_formal_schedule_rejects_drift(mutation):
    m = manifest()
    mutation(m)
    with pytest.raises(ValueError):
        validate(m)


def test_formal_status_propagates_without_claiming_confirmatory(tmp_path):
    from dataclasses import replace
    readiness = preflight_p2p_only(ROOT, require_runtime_canaries=False, materialize=False)
    assert readiness.contract.formal
    readiness = replace(readiness, complete=True, failures=())
    assert readiness.to_report()["formal"] is True
    auth = build_paid_authorization(readiness, granted_at="2026-09-07T00:00:00Z")
    assert auth["formal"] and auth["authorization_id"] == "formal_440_paid_run"
    path = write_run_report([], plan=readiness.plan, readiness=readiness, authorization=auth, path=tmp_path / "run.json")
    report = json.loads(path.read_text())
    assert report["formal"] and report["aggregate"]["formal"]
    assert report["confirmatory"] is False and report["paper_ready"] is False
    index = write_run_index([], plan=readiness.plan, readiness=readiness, path=tmp_path / "run-index.json")
    assert json.loads(index.read_text())["formal"] is True


def row(seed=0):
    return dict(task_id="task", model="model", seed=seed, arm="lazarus_state", version="buggy", complete=True)


def test_collection_requires_unique_complete_source_cells():
    r = row()
    expected = {key(r)}
    assert select_rows([("old", {"reports": [r, row(1)]})], expected)[key(r)][1] == r
    for reports, message in (([r, r], "duplicate"), ([], "missing"), ([{**r, "complete": False}], "incomplete")):
        with pytest.raises(ValueError, match=message):
            select_rows([("old", {"reports": reports})], expected)
