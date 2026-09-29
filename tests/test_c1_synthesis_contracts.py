from __future__ import annotations

import copy
import json

import pytest

from warptest.spec_schema import validate_spec_schema
from warptest.synthesis import (
    ModelCallResult,
    SynthesisError,
    build_checkpoint_format_repair_prompt,
    build_explore_system_prompt,
    build_llm_checkpoint_prompt,
    detect_private_grader_references,
    finalize_formal_c1_model_output,
    generated_spec_contract_errors,
    is_usable_generated_spec,
    normalize_llm_checkpoint_mapping,
)
from warptest.task_schema import PublicIssue


def _task(benchmark: str, task_id: str, *tags: str) -> PublicIssue:
    return PublicIssue(
        id=task_id,
        benchmark=benchmark,
        repo="games/example",
        issue="Construct the declared C1 start and transition through the public UI.",
        tags=["c1", *tags],
    )


def _checkpoint_mapping(spec: dict, *, payload_kind: str) -> dict:
    return {
        "task_id": "candidate",
        "benchmark": spec["benchmark"],
        "generated_spec": spec,
        "source_evidence": [
            {
                "source_path": "public/source.ts",
                "matched_symbol": "public C1 surface",
                "rationale": "The public runtime declares this target shape.",
            }
        ],
        "payload": {"kind": payload_kind},
        "runtime_validation": {"status": "pending", "detail": "not run"},
        "validity_class": spec["checkpoint"]["expected_validity"],
    }


def _openra_c1_spec() -> dict:
    return {
        "name": "Generated OpenRA public settings start",
        "benchmark": "OpenRA",
        "target": {
            "kind": "openra_settings",
            "mod": "ra",
            "settings": {
                "Game": {
                    "TargetLines": "Manual",
                    "IntroductionPromptVersion": 999,
                }
            },
        },
        "checkpoint": {
            "allowed_strategies": ["openra_public_settings_restore"],
            "expected_validity": "save-restorable",
        },
        "replay_from_start": {
            "actions": [
                {"type": "openra_open_settings_panel", "panel": "Display"}
            ]
        },
        "actions": [
            {
                "type": "openra_set_setting",
                "section": "Game",
                "key": "TargetLines",
                "value": "Automatic",
            }
        ],
        "assertions": [
            {
                "type": "openra_setting_equals",
                "section": "Game",
                "key": "TargetLines",
                "expected": "Automatic",
            },
            {"type": "no_openra_errors"},
        ],
    }


def _legacy_openra_map_spec() -> dict:
    return {
        "name": "Legacy map validation",
        "benchmark": "OpenRA",
        "target": {
            "kind": "openra_map",
            "mod": "ra",
            "requires_mod": "ra",
            "map": "allies-03a",
        },
        "checkpoint": {
            "allowed_strategies": ["openra_utility_map_validate"],
            "expected_validity": "replay-reachable",
        },
        "actions": [
            {"type": "openra_validate_map", "mod": "ra", "map": "allies-03a"}
        ],
        "assertions": [{"type": "no_openra_utility_errors"}],
    }


def _openra_c1_gameplay_spec() -> dict:
    return {
        "name": "Generated OpenRA deep production start",
        "benchmark": "OpenRA",
        "setup": {
            "openra_gameplay_probe": {
                "cash": 50000,
                "fast_build": True,
                "build_anywhere": False,
                "delay_ticks": 100,
            }
        },
        "target": {
            "kind": "openra_map",
            "mod": "ra",
            "requires_mod": "ra",
            "map": "allies-08a",
            "players": [
                {
                    "id": "Greece",
                    "playable": True,
                    "required": True,
                    "faction": "allies",
                }
            ],
            "actors": [{"id": "mcv", "type": "mcv", "owner": "Greece"}],
        },
        "checkpoint": {
            "allowed_strategies": ["openra_gameplay_production_validate"],
            "expected_validity": "replay-reachable",
        },
        "actions": [{"type": "openra_wait_ticks", "ticks": 100}],
        "assertions": [{"type": "no_openra_gameplay_probe_errors"}],
    }


def _luanti_c1_spec() -> dict:
    return {
        "name": "Generated Luanti dig-node start",
        "benchmark": "luanti",
        "target": {
            "kind": "synthesized_world",
            "nodes": [
                {
                    "pos": {"x": 3, "y": 2, "z": 3},
                    "node": "basenodes:cobble",
                }
            ],
            "validations": [
                {
                    "path": "node.3,2,3",
                    "expected": "basenodes:cobble",
                }
            ],
        },
        "checkpoint": {
            "allowed_strategies": ["luanti_worldmod_state_validate"],
            "expected_validity": "save-restorable",
        },
        "actions": [
            {"type": "luanti_dig_node", "pos": {"x": 3, "y": 2, "z": 3}}
        ],
        "assertions": [
            {
                "type": "luanti_node_is",
                "pos": {"x": 3, "y": 2, "z": 3},
                "expected": "air",
            },
            {"type": "no_luanti_mod_errors"},
        ],
    }


def _love_c1_spec() -> dict:
    return {
        "name": "Generated DatingSim gym Start",
        "benchmark": "DatingSim",
        "setup": {"ai_mode": "mocked"},
        "target": {
            "kind": "deterministic_state",
            "seed": 0,
            "player_name": "WarpTest",
            "gender": "male",
            "patch": {
                "player": {
                    "resources": {
                        "money": 1000,
                        "energy": 100,
                        "health": 100,
                        "mood": 100,
                    },
                    "title": "Love Novice",
                },
                "world": {
                    "location": "gym",
                    "week": 1,
                    "day": 0,
                    "timeSlot": "morning",
                    "globalFlags": {},
                },
            },
            "expect": {
                "location": "gym",
                "player_title": "Love Novice",
                "resources": {"money": 1000, "energy": 100},
                "global_flags": {},
            },
        },
        "checkpoint": {
            "allowed_strategies": ["test_hook_seeded_state"],
            "expected_validity": "injection-only",
        },
        "actions": [
            {
                "type": "hook_call",
                "method": "performAction",
                "args": ["gym_workout"],
            }
        ],
        "assertions": [
            {
                "type": "state_equals",
                "path": "player.resources.money",
                "value": 990,
            },
            {
                "type": "state_equals",
                "path": "player.resources.energy",
                "value": 70,
            },
            {"type": "no_real_openai"},
            {"type": "no_severe_console"},
        ],
    }


def test_love_c1_contract_keeps_start_expectations_out_of_goal_assertions() -> None:
    task = _task("DatingSim", "love-gym-workout", "love")
    spec = _love_c1_spec()

    assert validate_spec_schema(spec, "generated-love.yaml") == []
    assert is_usable_generated_spec(spec, task=task)
    for rendered in (
        build_llm_checkpoint_prompt(task, {}),
        build_explore_system_prompt(task),
    ):
        assert "expect = current Start values" in rendered
        assert "post-action value" in rendered
        assert "expect = post-action assertions" not in rendered

    contaminated = copy.deepcopy(spec)
    contaminated["target"]["expect"]["resources"] = {
        "money": 990,
        "energy": 70,
    }
    assert not is_usable_generated_spec(contaminated, task=task)
    with pytest.raises(SynthesisError) as captured:
        normalize_llm_checkpoint_mapping(
            _checkpoint_mapping(contaminated, payload_kind="zustand_hook"),
            task=task,
        )
    assert any(
        "effective Start value at player.resources" in error
        for error in captured.value.artifacts["generated_spec_contract_errors"]
    )

    defaults = copy.deepcopy(spec)
    del defaults["target"]["patch"]["player"]["resources"]
    assert is_usable_generated_spec(defaults, task=task)

    contaminated_defaults = copy.deepcopy(defaults)
    contaminated_defaults["target"]["expect"]["resources"] = {
        "money": 990,
        "energy": 70,
    }
    assert not is_usable_generated_spec(contaminated_defaults, task=task)
    assert any(
        "effective Start value at player.resources" in error
        for error in generated_spec_contract_errors(
            contaminated_defaults,
            task=task,
        )
    )


def test_openra_c1_prompt_normalization_and_usability_use_public_settings_contract() -> None:
    task = _task("OpenRA", "openra-target-lines-automatic", "openra")
    spec = _openra_c1_spec()

    assert validate_spec_schema(spec, "generated-openra.yaml") == []
    assert is_usable_generated_spec(spec, task=task)

    prompt = build_llm_checkpoint_prompt(task, {})
    explore_prompt = build_explore_system_prompt(task)
    for rendered in (prompt, explore_prompt):
        assert "kind: openra_settings" in rendered
        assert "openra_public_settings_restore" in rendered
        assert "openra_set_setting" in rendered
        assert "openra_setting_equals" in rendered
        assert "Do not emit openra_map" in rendered

    with pytest.raises(SynthesisError, match="payload.kind"):
        normalize_llm_checkpoint_mapping(
            _checkpoint_mapping(
                spec, payload_kind="openra_utility_checkpoint"
            ),
            task=task,
        )
    mapping = _checkpoint_mapping(
        spec, payload_kind="openra_public_settings_checkpoint"
    )
    normalized, warnings = normalize_llm_checkpoint_mapping(mapping, task=task)
    assert normalized["generated_spec"] == spec
    assert normalized["payload"]["kind"] == "openra_public_settings_checkpoint"
    assert warnings == []
    assert normalized["validity_class"] == "save-restorable"


def test_openra_c1_rejects_legacy_map_validation_shape() -> None:
    task = _task("OpenRA", "openra-target-lines-automatic", "openra")
    old_spec = _legacy_openra_map_spec()

    assert not is_usable_generated_spec(old_spec, task=task)
    with pytest.raises(SynthesisError) as captured:
        normalize_llm_checkpoint_mapping(
            _checkpoint_mapping(old_spec, payload_kind="openra_utility_checkpoint"),
            task=task,
        )
    assert any(
        "openra_settings" in error
        for error in captured.value.artifacts["generated_spec_contract_errors"]
    )


def test_openra_c1_deep_chain_uses_gameplay_synthesis_contract() -> None:
    task = _task(
        "OpenRA",
        "openra-allied-medium-tank-tech-chain",
        "openra",
        "deep-chain",
        "production",
    )
    spec = _openra_c1_gameplay_spec()

    assert validate_spec_schema(spec, "generated-openra-gameplay.yaml") == []
    assert is_usable_generated_spec(spec, task=task)

    prompt = build_llm_checkpoint_prompt(task, {})
    explore_prompt = build_explore_system_prompt(task)
    for rendered in (prompt, explore_prompt):
        assert "kind: openra_map" in rendered
        assert "openra_gameplay_production_validate" in rendered
        assert "openra_build_actor" in rendered
        assert "remove_player_actor_types" in rendered
        assert "actor type" in rendered
        assert "only valid OpenRA C1 shape" not in rendered

    with pytest.raises(SynthesisError, match="payload.kind"):
        normalize_llm_checkpoint_mapping(
            _checkpoint_mapping(
                spec, payload_kind="openra_public_settings_checkpoint"
            ),
            task=task,
        )
    mapping = _checkpoint_mapping(spec, payload_kind="openra_gameplay_probe")
    normalized, warnings = normalize_llm_checkpoint_mapping(mapping, task=task)
    assert normalized["generated_spec"] == spec
    assert normalized["payload"]["kind"] == "openra_gameplay_probe"
    assert warnings == []


def test_openra_lazarus_uses_the_registered_gameplay_synthesis_contract() -> None:
    task = _task(
        "OpenRA",
        "lz-openra-21453-hotkey-ready-building",
        "lazarus",
        "openra",
    )

    prompt = build_llm_checkpoint_prompt(task, {})
    explore_prompt = build_explore_system_prompt(task)

    for rendered in (prompt, explore_prompt):
        assert "kind: openra_map" in rendered
        assert "openra_gameplay_production_validate" in rendered
        assert "openra_build_actor" in rendered
        assert "map: allies-08a" in rendered
        assert "actor: completed_structure" in rendered
        assert "remove_player_actor_types" not in rendered
        assert "only valid OpenRA C1 shape" not in rendered

    valid = {
        "name": "Restore the public completed-building placement Start",
        "benchmark": "OpenRA",
        "target": {"kind": "openra_map", "mod": "ra", "map": "allies-08a"},
        "checkpoint": {
            "allowed_strategies": ["openra_gameplay_production_validate"],
            "expected_validity": "replay-reachable",
        },
        "actions": [
            {
                "type": "openra_build_actor",
                "player": "local_player",
                "actor": "completed_structure",
                "queue": "Building",
            }
        ],
        "assertions": [{"type": "no_openra_gameplay_probe_errors"}],
    }
    assert is_usable_generated_spec(valid, task=task)
    assert not is_usable_generated_spec({**valid, "actions": []}, task=task)


def test_luanti_lazarus_prompt_exposes_only_public_start_and_transition_values() -> None:
    task = _task(
        "luanti",
        "lz-luanti-11919-vector-from-string",
        "lazarus",
        "luanti",
    )
    task = PublicIssue(
        **{
            **task.__dict__,
            "issue": (
                "Preconditions: basenodes:stone at (0, 1, 0) above "
                "basenodes:sand at (0, 0, 0); hover at (0, 3, 6) and look "
                "at (0, 1, 0). Reference steps: submit "
                "/warp_import_coord (1, 2, 3.14)."
            ),
        }
    )
    prompt = build_llm_checkpoint_prompt(task, {})
    explore_prompt = build_explore_system_prompt(task)

    for rendered in (prompt, explore_prompt):
        assert "basenodes:sand" in rendered
        assert "basenodes:stone" in rendered
        assert "basenodes:water_source" not in rendered
        assert "x: 6" not in rendered
        assert "z: 6" in rendered
        assert "luanti_import_vector_string" in rendered
        assert "basenodes:dirt_with_grass" not in rendered
        assert "basenodes:desert_sand" not in rendered
        assert "hidden post-transition" not in rendered.lower()
        assert "hidden after" not in rendered.lower()

    valid = {
        "name": "Public fixed-seed coordinate-import marker",
        "benchmark": "luanti",
        "target": {
            "kind": "synthesized_world",
            "nodes": [
                {
                    "pos": {"x": 0, "y": 0, "z": 0},
                    "node": "basenodes:sand",
                },
                {
                    "pos": {"x": 0, "y": 1, "z": 0},
                    "node": "basenodes:stone",
                },
            ],
            "player": {
                "pos": {"x": 0, "y": 3, "z": 6},
                "hover": True,
                "look_at": {"x": 0, "y": 1, "z": 0},
            },
            "validations": [
                {"path": "node.0,0,0", "expected": "basenodes:sand"},
                {
                    "path": "node.0,1,0",
                    "expected": "basenodes:stone",
                },
            ],
        },
        "checkpoint": {
            "allowed_strategies": ["luanti_worldmod_state_validate"],
            "expected_validity": "save-restorable",
        },
        "actions": [
            {
                "type": "luanti_import_vector_string",
                "value": "(1, 2, 3.14)",
                "marker_pos": {"x": 0, "y": 1, "z": 0},
            }
        ],
        "assertions": [{"type": "no_luanti_mod_errors"}],
    }
    assert is_usable_generated_spec(valid, task=task)
    mismatched = copy.deepcopy(valid)
    mismatched["target"]["nodes"][1]["node"] = "basenodes:water_source"
    assert not is_usable_generated_spec(mismatched, task=task)
    injected = copy.deepcopy(valid)
    injected["target"]["fill"] = []
    assert not is_usable_generated_spec(injected, task=task)
    wrong_view = copy.deepcopy(valid)
    wrong_view["target"]["player"]["pos"]["z"] = 5
    assert not is_usable_generated_spec(wrong_view, task=task)


def test_ordinary_c1_luanti_prompt_keeps_the_pre_lazarus_action_space() -> None:
    task = _task("luanti", "ordinary-c1-luanti", "luanti")

    prompt = build_llm_checkpoint_prompt(task, {})
    explore_prompt = build_explore_system_prompt(task)

    for rendered in (prompt, explore_prompt):
        assert "luanti_set_node" in rendered
        assert "luanti_dig_node" in rendered
        assert "luanti_import_vector_string" not in rendered
        assert "luanti_drop_falling_node" not in rendered
        assert "testnodes:falling" not in rendered
        assert "testnodes:torchlike" not in rendered


def test_luanti_c1_generated_spec_requires_complete_action_arguments() -> None:
    task = _task("luanti", "luanti-dig-node", "luanti")
    spec = _luanti_c1_spec()
    assert validate_spec_schema(spec, "generated-luanti.yaml") == []
    assert is_usable_generated_spec(spec, task=task)

    malformed = {**spec, "actions": [{"type": "luanti_dig_node"}]}
    assert not is_usable_generated_spec(malformed, task=task)
    with pytest.raises(SynthesisError) as captured:
        normalize_llm_checkpoint_mapping(
            _checkpoint_mapping(malformed, payload_kind="luanti_worldmod_checkpoint"),
            task=task,
        )
    assert any(
        "actions[0].pos" in error
        for error in captured.value.artifacts["generated_spec_contract_errors"]
    )


def test_luanti_c1_generated_spec_rejects_target_validation_contradiction() -> None:
    task = _task("luanti", "luanti-dig-node", "luanti")
    spec = _luanti_c1_spec()
    contradictory = {
        **spec,
        "target": {
            **spec["target"],
            "validations": [
                {"path": "node.3,2,3", "expected": "air"}
            ],
        },
    }
    errors = validate_spec_schema(contradictory, "generated-luanti.yaml")
    assert any("contradicts target state" in error for error in errors)
    assert not is_usable_generated_spec(contradictory, task=task)


def _valid_2048_mapping(task: PublicIssue) -> dict:
    spec = {
        "name": "public format fixture",
        "benchmark": "2048",
        "target": {
            "kind": "board",
            "board": [[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]],
            "score": 0,
            "bestScore": 0,
        },
        "checkpoint": {
            "allowed_strategies": ["localStorage_restore_reload"],
            "expected_validity": "save-restorable",
        },
        "actions": [{"type": "move", "direction": "left"}],
        "assertions": [{"type": "tile_value_present", "value": 4}],
    }
    return {
        "task_id": task.id,
        "benchmark": task.benchmark,
        "generated_spec": spec,
        "source_evidence": [
            {
                "source_path": "src/game_manager.js",
                "matched_symbol": "move",
                "rationale": "Public source defines move directions.",
            }
        ],
        "payload": {"kind": "localStorage"},
        "runtime_validation": {"status": "pending", "detail": "not run"},
        "validity_class": "save-restorable",
    }


class _RepairClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    def converse(self, *_args, **_kwargs) -> ModelCallResult:
        self.calls += 1
        return ModelCallResult(
            content=self.response,
            latency_ms=7,
            prompt_tokens=11,
            completion_tokens=13,
            total_tokens=24,
        )


def test_lazarus_repair_normalizes_only_redundant_public_envelope_fields(
    tmp_path,
) -> None:
    task = _task(
        "openra",
        "lz-openra-envelope-repair",
        "lazarus",
        "openra",
        "deep-chain",
    )
    spec = _openra_c1_gameplay_spec()
    repaired = _checkpoint_mapping(spec, payload_kind="openra_gameplay_probe")
    repaired["task_id"] = task.id
    repaired["benchmark"] = "OpenRA"
    repaired["payload"]["kind"] = None
    client = _RepairClient(
        pytest.importorskip("yaml").safe_dump(repaired, sort_keys=False)
    )

    checkpoint, metadata = finalize_formal_c1_model_output(
        task=task,
        raw_output="not yaml: [",
        client=client,
        model="same-model",
        located={"repo": "games/OpenRA", "inputs_read": []},
        root=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        source_cost_kind="checkpoint_exploration",
    )

    assert checkpoint.benchmark == "openra"
    assert checkpoint.payload["kind"] == "openra_gameplay_probe"
    assert metadata["repair"]["normalization_warnings"] == [
        "lazarus_benchmark_case_normalized",
        "lazarus_payload_kind_derived_from_generated_spec",
    ]


def test_lazarus_initial_output_uses_the_same_public_envelope_normalization(
    tmp_path,
) -> None:
    task = _task(
        "openra",
        "lz-openra-envelope-initial",
        "lazarus",
        "openra",
        "deep-chain",
    )
    spec = _openra_c1_gameplay_spec()
    initial = _checkpoint_mapping(spec, payload_kind="openra_gameplay_probe")
    initial["task_id"] = task.id
    initial["benchmark"] = "OpenRA"
    initial["payload"]["kind"] = None
    client = _RepairClient("must not be called")

    checkpoint, metadata = finalize_formal_c1_model_output(
        task=task,
        raw_output=pytest.importorskip("yaml").safe_dump(initial, sort_keys=False),
        client=client,
        model="same-model",
        located={"repo": "games/OpenRA", "inputs_read": []},
        root=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        source_cost_kind="checkpoint_exploration",
    )

    assert checkpoint.payload["kind"] == "openra_gameplay_probe"
    assert client.calls == 0
    assert metadata["normalization_warnings"][:2] == [
        "lazarus_benchmark_case_normalized",
        "lazarus_payload_kind_derived_from_generated_spec",
    ]


def test_lazarus_repair_prunes_noop_empty_actor_removal_lists(tmp_path) -> None:
    task = _task(
        "openra",
        "lz-openra-empty-removal",
        "lazarus",
        "openra",
        "deep-chain",
    )
    spec = _openra_c1_gameplay_spec()
    spec["setup"]["openra_gameplay_probe"]["remove_player_actor_types"] = {
        "Multi0": []
    }
    repaired = _checkpoint_mapping(spec, payload_kind="openra_gameplay_probe")
    repaired["task_id"] = task.id
    repaired["benchmark"] = task.benchmark
    client = _RepairClient(
        pytest.importorskip("yaml").safe_dump(repaired, sort_keys=False)
    )

    checkpoint, metadata = finalize_formal_c1_model_output(
        task=task,
        raw_output="not yaml: [",
        client=client,
        model="same-model",
        located={"repo": "games/OpenRA", "inputs_read": []},
        root=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        source_cost_kind="checkpoint_exploration",
    )

    setup = checkpoint.generated_spec["setup"]["openra_gameplay_probe"]
    assert "remove_player_actor_types" not in setup
    assert metadata["repair"]["normalization_warnings"] == [
        "lazarus_empty_actor_removal_lists_pruned"
    ]


def test_formal_c1_runs_exactly_one_public_same_model_format_repair(
    tmp_path,
) -> None:
    task = _task("2048", "2048-format-repair", "deep-chain")
    valid = _valid_2048_mapping(task)
    client = _RepairClient(
        pytest.importorskip("yaml").safe_dump(valid, sort_keys=False)
    )
    checkpoint, metadata = finalize_formal_c1_model_output(
        task=task,
        raw_output="```yaml\ntask_id: broken",
        client=client,
        model="same-model",
        located={"repo": "games/2048", "inputs_read": []},
        root=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        source_cost_kind="checkpoint_exploration",
    )
    assert client.calls == 1
    assert checkpoint.model_generated_spec == valid["generated_spec"]
    assert checkpoint.used_public_scaffold is False
    assert metadata["repair"]["attempted"] is True
    assert metadata["repair"]["succeeded"] is True
    assert (tmp_path / "artifacts/format-repair/prompt.txt").is_file()
    assert (tmp_path / "artifacts/format-repair/response.txt").is_file()


def test_formal_c1_repair_failure_never_buys_a_second_repair(tmp_path) -> None:
    task = _task("2048", "2048-format-repair", "deep-chain")
    client = _RepairClient("still: [invalid")
    with pytest.raises(SynthesisError):
        finalize_formal_c1_model_output(
            task=task,
            raw_output="not yaml: [",
            client=client,
            model="same-model",
            located={"repo": "games/2048", "inputs_read": []},
            root=tmp_path,
            artifact_dir=tmp_path / "artifacts",
            source_cost_kind="checkpoint_exploration",
        )
    assert client.calls == 1
    repair_meta = json.loads(
        (tmp_path / "artifacts/format-repair/meta.json").read_text(
            encoding="utf-8"
        )
    )
    assert repair_meta["succeeded"] is False
    assert repair_meta["usage"]["total_tokens"] == 24
    assert repair_meta["initial_output_sha256"]
    assert repair_meta["response_sha256"]


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ("```yaml\na: 1", "incomplete fence"),
        ("rationale: code: path", "unescaped colon"),
        ("generated_spec: prose", "generated_spec is not a mapping"),
        ("source_evidence: null", "source_evidence is missing"),
        ("source_evidence: {}", "source_evidence has wrong shape"),
        ("runtime_validation: pending", "runtime_validation is scalar"),
        ("runtime_validation: []", "runtime_validation is a list"),
        ("payload_kind: localStorage", "payload is flat"),
        ("payload: localStorage", "payload is scalar"),
        ("actions: {}", "actions is not a list"),
        ("assertions: {}", "assertions is not a list"),
        ("target: []", "target is not a mapping"),
        ("checkpoint: []", "checkpoint is not a mapping"),
        ("task_id: wrong", "task id mismatch"),
        ("benchmark: wrong", "benchmark mismatch"),
        ("validity_class: invalid", "invalid validity class"),
        ("extra_private_guess: true", "illegal top-level field"),
        ("actions:\\n- move", "action is not a mapping"),
        ("assertions:\\n- pass", "assertion is not a mapping"),
        ("target:\\n  kind: wrong", "invalid target kind"),
        ("checkpoint:\\n  allowed_strategies: []", "empty strategy"),
        ("source_evidence:\\n- source_path: x", "evidence fields missing"),
        ("runtime_validation:\\n  status: pending", "detail missing"),
    ],
)
def test_repair_prompt_covers_observed_format_failures_without_private_data(
    raw: str, error: str
) -> None:
    task = _task("2048", "2048-format-repair", "deep-chain")
    prompt = build_checkpoint_format_repair_prompt(task, raw, [error])
    assert "initial_output:" in prompt
    assert error in prompt
    assert "structure-only repair" in prompt
    assert "canonical answer" in prompt
    assert detect_private_grader_references(prompt) == []
