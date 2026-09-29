"""Tests for the issue-guided fuzz pipeline: contracts, priors, and wiring."""

from __future__ import annotations

import os
import shutil

import pytest

from scripts.build_luanti_priors import build_recipes
from warptest.bug_cards import (
    axis_effective,
    axis_eligible_for_downstream,
    classify_card,
    new_card,
)
from warptest.fuzz_llm import (
    LLMStateFuzzer,
    build_seed_prompt,
    run_evo_fuzz,
)
from warptest.fuzz_priors import NormalizedPriors, materialize_prior_seeds
from warptest.luanti_replay import (
    _REPLAY_INIT_LUA,
    _normalize_staged_runtime,
    _runtime_binary,
    _server_target,
    run_replay,
    write_fixture,
)
from warptest.upstream_contracts import (
    AXIS_FUZZ_REACHABLE,
    AXIS_HEADLESS_REPLAYABLE,
    VERDICT_BUILD_FAILED,
    VERDICT_HARNESS_INCOMPATIBLE,
    VERDICT_POST_STILL_REPRODUCED,
    VERDICT_PRE_NOT_REPRODUCED,
    VERDICT_PRE_REPRODUCED_POST_FIXED,
    DefectOracle,
    ReplayObservation,
    build_cache_key,
    classify_replay,
    derive_pre_post_sha,
    parse_body_closing_refs,
    parse_body_related_refs,
)
from warptest.fuzz_llm import InjectResult


# --------------------------------------------------------------------------- #
# Contract 2: pre/post SHA
# --------------------------------------------------------------------------- #


def test_pre_post_sha_uses_merge_parent_not_base_ref() -> None:
    merge = {"oid": "POST", "parents": {"nodes": [{"oid": "PRE"}, {"oid": "OTHER"}]}}
    shas = derive_pre_post_sha(merge)
    assert shas.pre_fix_sha == "PRE"
    assert shas.post_fix_sha == "POST"


def test_pre_post_sha_raises_without_merge_or_parent() -> None:
    with pytest.raises(ValueError):
        derive_pre_post_sha(None)
    with pytest.raises(ValueError):
        derive_pre_post_sha({"oid": "x", "parents": {"nodes": []}})


def test_body_ref_parsing_separates_closing_and_related() -> None:
    body = "This fixes #100 and closes #200. Related to #300 (see #100)."
    closing = parse_body_closing_refs(body)
    assert closing == [100, 200]
    related = parse_body_related_refs(body, exclude=set(closing))
    assert related == [300]


# --------------------------------------------------------------------------- #
# Contract 3: replay defect oracle + five states
# --------------------------------------------------------------------------- #


def test_oracle_does_not_flag_plain_status_failure() -> None:
    # A validation mismatch (status=="failure") with a clean exit and no configured
    # signature/semantic assertion must NOT be a defect (Contract 3).
    oracle = DefectOracle(
        abnormal_exit=True, timeout=True, log_signatures=[], semantic_assertions=[]
    )
    obs = ReplayObservation(
        build_ok=True, harness_ok=True, exit_code=0, result_json={"status": "failure"}
    )
    is_defect, _ = oracle.evaluate(obs)
    assert is_defect is False


def test_oracle_semantic_assertion_flags_specific_output() -> None:
    oracle = DefectOracle(
        abnormal_exit=False,
        timeout=False,
        semantic_assertions=[{"path": "detail", "contains": "Unknown node"}],
    )
    obs = ReplayObservation(
        build_ok=True,
        harness_ok=True,
        exit_code=0,
        result_json={"detail": "Unknown node: x"},
    )
    is_defect, reason = oracle.evaluate(obs)
    assert is_defect and "Unknown node" in reason


def test_classify_replay_five_states() -> None:
    oracle = DefectOracle(timeout=True)
    hang = ReplayObservation(build_ok=True, harness_ok=True, timed_out=True)
    clean = ReplayObservation(build_ok=True, harness_ok=True, exit_code=0)
    assert (
        classify_replay(hang, clean, oracle)["verdict"]
        == VERDICT_PRE_REPRODUCED_POST_FIXED
    )
    assert (
        classify_replay(clean, clean, oracle)["verdict"] == VERDICT_PRE_NOT_REPRODUCED
    )
    assert (
        classify_replay(hang, hang, oracle)["verdict"] == VERDICT_POST_STILL_REPRODUCED
    )
    nobuild = ReplayObservation(build_ok=False)
    assert classify_replay(nobuild, clean, oracle)["verdict"] == VERDICT_BUILD_FAILED


def test_build_cache_key_changes_with_inputs() -> None:
    base = dict(
        commit="a", platform="p", compiler="c", cmake_args=["-x"], harness_digest="h"
    )
    k1 = build_cache_key(**base)
    k2 = build_cache_key(**{**base, "commit": "b"})
    assert k1 != k2 and len(k1) == 64


def test_runtime_normalization_stages_binary_builtin_and_devtest(tmp_path) -> None:
    stage = tmp_path / "stage"
    binary = stage / "Luanti.app" / "Contents" / "MacOS" / "luantiserver"
    builtin = stage / "Luanti.app" / "Contents" / "Resources" / "builtin"
    devtest = stage / "Luanti.app" / "Contents" / "Resources" / "games" / "devtest"
    binary.parent.mkdir(parents=True)
    builtin.mkdir(parents=True)
    devtest.mkdir(parents=True)
    binary.write_text("server", encoding="utf-8")
    (builtin / "init.lua").write_text("-- builtin", encoding="utf-8")
    (devtest / "game.conf").write_text("name = devtest", encoding="utf-8")

    runtime = tmp_path / "runtime"
    out = _normalize_staged_runtime(stage, runtime, "luantiserver")
    assert out == runtime / "bin" / "luantiserver"
    assert _runtime_binary(runtime) == out
    assert (runtime / "builtin" / "init.lua").is_file()
    assert (runtime / "games" / "devtest" / "game.conf").is_file()

    shutil.rmtree(runtime / "builtin")
    assert _runtime_binary(runtime) is None


def test_server_target_supports_minetest_and_luanti_names(tmp_path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "CMakeLists.txt").write_text("project(minetest)\n", encoding="utf-8")
    (new / "CMakeLists.txt").write_text("project(luanti)\n", encoding="utf-8")
    assert _server_target(old) == "minetestserver"
    assert _server_target(new) == "luantiserver"


def test_pre_harness_launch_failure_is_incompatible(tmp_path) -> None:
    work = tmp_path / "missing"
    write_fixture(work, {})
    oracle = DefectOracle()
    obs = run_replay(tmp_path / "does-not-exist", work, oracle=oracle, timeout_s=0.1)
    assert obs.build_ok is True
    assert obs.harness_ok is False
    assert "launch failed" in obs.log_text
    assert classify_replay(obs, obs, oracle)["verdict"] == VERDICT_HARNESS_INCOMPATIBLE


def test_abnormal_exit_after_harness_sentinel_reaches_oracle(tmp_path) -> None:
    work = tmp_path / "started"
    write_fixture(work, {})
    fake = tmp_path / "fake-luantiserver"
    fake.write_text(
        '#!/bin/sh\nprintf "started\\n" > "$4/replay-started"\nexit 7\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    oracle = DefectOracle(abnormal_exit=True)
    # Process launch on a loaded macOS runner can occasionally exceed one second;
    # this test covers abnormal-exit classification, not launch latency.
    obs = run_replay(fake, work, oracle=oracle, timeout_s=5)
    assert obs.harness_ok is True
    assert obs.exit_code == 7
    assert oracle.evaluate(obs)[0] is True


def test_replay_harness_rejects_oversized_fill_before_looping() -> None:
    assert "local MAX_FILL_NODES = 100000" in _REPLAY_INIT_LUA
    assert "if volume > MAX_FILL_NODES then" in _REPLAY_INIT_LUA
    assert "replay fixture rejected: fill volume" in _REPLAY_INIT_LUA


# --------------------------------------------------------------------------- #
# Contract 1: dual-axis classification (paths never yield high confidence alone)
# --------------------------------------------------------------------------- #


def test_classifier_axes_and_confidence() -> None:
    card = new_card(
        pr_number=1,
        title="fix ItemStack count off-by-one",
        body="param2 handling",
        changed_files=["src/inventory.cpp", "src/nodedef.cpp"],
    )
    card["classification"] = classify_card(card)
    fuzz = card["classification"][AXIS_FUZZ_REACHABLE]
    assert fuzz["value"] is True
    assert fuzz["confidence"] in {"low", "medium"}  # never "high" from signals alone
    # A pure-shader/client PR should not be fuzz_reachable.
    client_card = new_card(
        pr_number=2,
        title="shadow intensity",
        body="",
        changed_files=["src/client/shadows.cpp"],
    )
    client_card["classification"] = classify_card(client_card)
    assert axis_effective(client_card, AXIS_FUZZ_REACHABLE) is False


def test_manual_override_is_preserved_and_authoritative() -> None:
    card = new_card(pr_number=3, title="x", body="", changed_files=[])
    card["classification"] = classify_card(card)
    card["classification"][AXIS_HEADLESS_REPLAYABLE]["manual_override"] = True
    # Re-running the classifier must keep the override.
    card["classification"] = classify_card(card)
    assert card["classification"][AXIS_HEADLESS_REPLAYABLE]["manual_override"] is True
    assert axis_effective(card, AXIS_HEADLESS_REPLAYABLE) is True


def test_downstream_eligibility_requires_medium_confidence_or_human_override() -> None:
    def card(confidence: str, override=None):
        value = new_card(pr_number=1, title="inventory", changed_files=[])
        value["classification"] = {
            AXIS_FUZZ_REACHABLE: {
                "value": True,
                "confidence": confidence,
                "manual_override": override,
            }
        }
        return value

    assert axis_eligible_for_downstream(card("low"), AXIS_FUZZ_REACHABLE) is False
    assert axis_eligible_for_downstream(card("medium"), AXIS_FUZZ_REACHABLE) is True
    assert axis_eligible_for_downstream(card("low", True), AXIS_FUZZ_REACHABLE) is True
    assert (
        axis_eligible_for_downstream(card("medium", False), AXIS_FUZZ_REACHABLE)
        is False
    )


def test_priors_builder_excludes_low_confidence_screening_hits() -> None:
    low = new_card(pr_number=1, title="inventory spacing")
    low["classification"] = {
        AXIS_FUZZ_REACHABLE: {
            "value": True,
            "confidence": "low",
            "manual_override": None,
        }
    }
    medium = new_card(
        pr_number=2, title="inventory count", changed_files=["src/inventory.cpp"]
    )
    medium["classification"] = {
        AXIS_FUZZ_REACHABLE: {
            "value": True,
            "confidence": "medium",
            "manual_override": None,
        }
    }
    recipes, sources = build_recipes([low, medium])
    assert [source["pr_number"] for source in sources] == [2]
    assert recipes and all(recipe["source_prs"] == [2] for recipe in recipes)


# --------------------------------------------------------------------------- #
# Contract 5: structured priors -> deterministic prior seeds; no raw diff in prompt
# --------------------------------------------------------------------------- #


def _priors() -> NormalizedPriors:
    return NormalizedPriors(
        {
            "benchmark": "luanti",
            "recipes": [
                {
                    "id": "param2",
                    "target_path": "nodes[].param2",
                    "op": "field_value",
                    "values": [-1, 256],
                    "rationale": "u8 wrap",
                    "source_pr": 42,
                },
                {
                    "id": "missing",
                    "target_path": "does_not_exist[].x",
                    "op": "field_value",
                    "values": [1],
                },
            ],
        },
        raw_text='{"benchmark":"luanti"}',
    )


def test_materialize_is_deterministic_and_skips_missing_paths() -> None:
    oracle = {"nodes": [{"param2": 0}, {"param2": 0}]}
    priors = _priors()
    seeds1 = materialize_prior_seeds(oracle, priors)
    seeds2 = materialize_prior_seeds(oracle, priors)
    assert [p for _, p in seeds1] == [p for _, p in seeds2]  # stable order
    # Two values applied to the present path; the missing path produced nothing.
    assert len(seeds1) == 2
    assert all(all(n["param2"] in (-1, 256) for n in s["nodes"]) for s, _ in seeds1)
    assert oracle["nodes"][0]["param2"] == 0  # oracle not mutated


def test_seed_prompt_contains_structured_hint_not_raw_diff() -> None:
    hint = _priors().hint()
    prompt = build_seed_prompt(
        "luanti", {"nodes": [{"param2": 0}]}, "nodes: list", hint
    )
    assert "nodes[].param2" in prompt
    assert "diff --git" not in prompt and "PR #" not in prompt


# --------------------------------------------------------------------------- #
# End-to-end wiring: prior seeds injected in order oracle -> prior -> llm
# --------------------------------------------------------------------------- #


def _stub_fuzzer(priors: NormalizedPriors | None) -> LLMStateFuzzer:
    os.environ.setdefault("MODELSCOPE_API_KEY", "test-key")
    fuzzer = LLMStateFuzzer(
        benchmark="luanti", model="stub-model", client=object(), priors=priors
    )
    fuzzer._chat = lambda prompt: '{"nodes": [{"param2": 7}]}'  # deterministic seed
    return fuzzer


def test_run_evo_fuzz_injects_prior_seeds_and_records_provenance() -> None:
    priors = _priors()
    fuzzer = _stub_fuzzer(priors)
    seed_target = {"nodes": [{"param2": 0}]}

    def inject_fn(target, operator_id, screenshot_name):
        return InjectResult(verdict="robust", detail="ok")

    def make_record(*, target, operator_id, provenance, generation, fitness, result):
        return {
            "mutation": operator_id,
            "provenance": provenance,
            "verdict": result.verdict,
        }

    records, meta = run_evo_fuzz(
        benchmark="luanti",
        seed_target=seed_target,
        inject_fn=inject_fn,
        fuzzer=fuzzer,
        budget_s=0.0,  # skip the mutation loop; only seed phase runs
        num_seeds=1,
        samples_per_op=1,
        top_n=3,
        make_record=make_record,
    )
    assert meta["num_oracle_seeds"] == 1
    assert meta["num_prior_seeds"] == 2
    assert meta["priors"]["recipe_ids"] == ["param2", "missing"]
    provenances = [r["provenance"] for r in records]
    assert provenances[0] == "oracle_seed"
    assert any(p.startswith("prior:param2") for p in provenances)
    # Fixed order: the oracle seed precedes every prior seed, which precede llm seeds.
    first_prior = next(i for i, p in enumerate(provenances) if p.startswith("prior:"))
    first_llm = next(
        (i for i, p in enumerate(provenances) if p == "llm_seed"), len(provenances)
    )
    assert first_prior < first_llm


def test_no_priors_is_a_clean_control() -> None:
    fuzzer = _stub_fuzzer(None)
    assert fuzzer.priors_hint == ""
    assert fuzzer.prior_seeds({"nodes": [{"param2": 0}]}) == []
