"""Schema/repair regressions are offline; every model reply is a fixture."""

import copy
import json
from pathlib import Path

import pytest
import yaml

from warptest.lazarus_p2p_only_schema import (
    checkpoint_schema,
    preserve_valid_fields,
    spec_schema,
    validate,
)
from warptest.lazarus_p2p_only_spec import (
    action_errors,
    public_contract,
    spec_errors,
    state_errors,
)
from warptest.synthesis import (
    ExploratoryLLMCheckpointSynthesisAgent,
    ModelCallResult,
    SynthesisError,
    _normalize_lazarus_public_envelope,
    _wrap_type_placeholders,
    build_explore_system_prompt,
    finalize_formal_c1_model_output,
    formal_c1_checkpoint_contract_errors,
)
from warptest.task_schema import PublicIssue

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = yaml.safe_load(
    (ROOT / "tests/fixtures/lazarus_p2p_only_v3_specs.yaml").read_text()
)
TASK = "lzp-jupyterlab-17222-toolbar-button-pressed-toggle"


def task(task_id=TASK):
    host = task_id.split("-")[1]
    return PublicIssue(
        id=task_id,
        benchmark=host,
        repo=".",
        issue="Public preconditions and reference steps.",
        tags=["c1", "lazarus", "p2p-only"],
        prompt_revision="lzp-generated-state-v3",
    )


def mapping(task_id=TASK):
    return {
        "task_id": task_id,
        "benchmark": task_id.split("-")[1],
        "generated_spec": copy.deepcopy(SAMPLES[task_id]["spec"]),
        "source_evidence": [
            {
                "source_path": "component.js",
                "matched_symbol": "component",
                "rationale": "Actual implementation read.",
            }
        ],
        "payload": {"kind": "state_injection"},
        "runtime_validation": {"status": "not_run", "detail": "Generated only."},
        "validity_class": "injection-only",
    }


class Replies:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def converse(self, model, messages, **kwargs):
        self.calls.append(
            {"model": model, "messages": copy.deepcopy(messages), "kwargs": kwargs}
        )
        assert self.replies, "unexpected extra model call"
        return ModelCallResult(
            content=self.replies.pop(0),
            latency_ms=1,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )


def finalize(tmp_path, original, proposed):
    client = Replies(yaml.safe_dump(proposed))
    result, meta = finalize_formal_c1_model_output(
        task=task(original["task_id"]),
        raw_output=yaml.safe_dump(original),
        client=client,
        model="fake/model",
        located={"repo": str(tmp_path), "inputs_read": []},
        root=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        source_cost_kind="checkpoint_exploration",
    )
    return result, meta, client


@pytest.mark.parametrize("task_id", list(SAMPLES))
def test_all_registered_states_actions_and_envelopes_follow_exported_schema(task_id):
    value = mapping(task_id)
    host = value["benchmark"]
    assert spec_errors(value["generated_spec"], host) == []
    assert not validate(
        value["generated_spec"], public_contract(host)["generated_spec_json_schema"]
    )
    assert formal_c1_checkpoint_contract_errors(value, task=task(task_id)) == []
    assert _wrap_type_placeholders(
        checkpoint_schema(task_id, host)
    ) == checkpoint_schema(task_id, host)


def test_schema_describes_arrays_enums_patterns_and_component_specific_properties():
    contract = checkpoint_schema(TASK, "jupyterlab")
    generated = contract["properties"]["generated_spec"]["properties"]
    assert generated["assertions"]["type"] == "array"
    variants = generated["target"]["properties"]["state"]["oneOf"]
    button = next(
        s for s in variants if s["properties"]["component"]["const"] == "toolbar_button"
    )
    assert set(button["properties"]["props"]["required"]) == {"label", "pressed"}
    panel = next(
        s
        for s in generated["actions"]["items"]["oneOf"]
        if s["properties"]["type"]["const"] == "open_panel"
    )
    assert panel["properties"]["options"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    prompt = build_explore_system_prompt(task())
    assert "checkpoint_json_schema" in prompt and "public_task" in prompt
    assert "assertions: list;" not in prompt
    assert "type: <string>" not in prompt


@pytest.mark.parametrize(
    "name", ["World With Mods", "Test World", "p2p_world", "world-42"]
)
def test_world_names_accept_safe_spaces_without_changing_game_or_mod_rules(name):
    state = {
        "tab": "local",
        "settings": {},
        "worlds": [{"name": name, "gameid": "devtest", "mods": []}],
    }
    assert not state_errors(state, "luanti")
    state["worlds"][0]["gameid"] = "bad game"
    assert state_errors(state, "luanti")


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/tmp/escape",
        "a/b",
        "a\\b",
        ".",
        "..",
        "",
        "   ",
        "x\n",
        "x\r",
        "x\0",
    ],
)
def test_world_name_safety_is_not_relaxed(name):
    assert state_errors(
        {
            "tab": "local",
            "settings": {},
            "worlds": [{"name": name, "gameid": "devtest", "mods": []}],
        },
        "luanti",
    )


@pytest.mark.parametrize("bad", [None, {}, [], True, 1.5, "1"])
def test_malformed_argument_types_return_errors_instead_of_crashing(bad):
    assert action_errors({"type": bad}, "jupyterlab")
    assert state_errors({"component": bad, "theme": bad, "props": {}}, "jupyterlab")
    assert state_errors({"screen": "editor", "map_index": bad}, "openra")


def test_empty_options_are_not_confused_with_a_string_or_invented_options():
    assert not action_errors(
        {"type": "open_panel", "value": "More", "options": []}, "jupyterlab"
    )
    assert action_errors(
        {"type": "open_panel", "value": "More", "options": ""}, "jupyterlab"
    )
    original = {"type": "open_panel", "value": "More", "options": ""}
    accepted, _ = preserve_valid_fields(
        original,
        {"type": "open_panel", "value": "Invented", "options": []},
        spec_schema("jupyterlab")["properties"]["actions"]["items"],
    )
    assert accepted == {"type": "open_panel", "value": "More", "options": []}


def test_repair_cannot_drop_valid_assertions_change_state_or_replace_actions(tmp_path):
    original = mapping()
    original["runtime_validation"] = None
    original["generated_spec"]["assertions"] = [{"diagnostic": "preserve me"}]
    proposed = mapping()
    proposed["generated_spec"].pop("assertions")
    proposed["generated_spec"]["target"]["state"]["props"]["label"] = (
        "Changed task semantics"
    )
    proposed["generated_spec"]["actions"] = []
    result, meta, client = finalize(tmp_path, original, proposed)
    assert result.model_generated_spec == original["generated_spec"]
    assert len(client.calls) == 1
    assert meta["repair"]["valid_field_preservation"]["restored_fields"]
    assert (
        json.loads(
            (tmp_path / "artifacts/format-repair/accepted-mapping.json").read_text()
        )["generated_spec"]
        == original["generated_spec"]
    )
    assert "repair_policy" in client.calls[0]["messages"][-1]["content"]


def test_partial_action_repair_preserves_other_actions_and_list_shape():
    original = mapping()
    original["generated_spec"]["actions"] = [
        {"type": "click", "selector": "#keep"},
        {"type": "click", "selector": 5},
    ]
    proposed = copy.deepcopy(original)
    proposed["generated_spec"]["actions"] = [
        {"type": "click", "selector": "#changed"},
        {"type": "click", "selector": "#fixed"},
        {"type": "click", "selector": "#injected"},
    ]
    value, _ = preserve_valid_fields(
        original, proposed, checkpoint_schema(TASK, "jupyterlab")
    )
    assert value["generated_spec"]["actions"] == [
        {"type": "click", "selector": "#keep"},
        {"type": "click", "selector": "#fixed"},
    ]
    assert not validate(value, checkpoint_schema(TASK, "jupyterlab"))


def test_public_task_evidence_is_typed_but_never_substitutes_for_a_repository_read():
    value = mapping()
    value["source_evidence"].append(
        {
            "source_path": "Public issue state description",
            "matched_symbol": "state",
            "rationale": "Supplied task text.",
        }
    )
    warnings = _normalize_lazarus_public_envelope(value, task=task())
    assert warnings == ["lazarus_public_task_evidence_typed[1]"]
    assert value["source_evidence"][1]["kind"] == "public_task"
    assert not formal_c1_checkpoint_contract_errors(value, task=task())
    value["source_evidence"] = value["source_evidence"][1:]
    assert formal_c1_checkpoint_contract_errors(value, task=task())


def test_arbitrary_unread_path_is_not_normalized_into_public_evidence():
    value = mapping()
    value["source_evidence"][0]["source_path"] = "not-read.js"
    _normalize_lazarus_public_envelope(value, task=task())
    assert value["source_evidence"][0]["source_path"] == "not-read.js"
    assert "kind" not in value["source_evidence"][0]


def test_empty_evidence_list_can_be_repaired_without_changing_valid_spec():
    original = mapping()
    original["source_evidence"] = []
    repaired, _ = preserve_valid_fields(
        original, mapping(), checkpoint_schema(TASK, "jupyterlab")
    )
    assert not validate(repaired, checkpoint_schema(TASK, "jupyterlab"))


def agent_run(tmp_path, replies, steps):
    (tmp_path / "component.js").write_text("export const component = 1;")
    client = Replies(*replies)
    agent = ExploratoryLLMCheckpointSynthesisAgent(
        model="fake/model", client=client, max_steps=steps
    )
    return agent, client


def test_last_budgeted_turn_accepts_a_bare_checkpoint_without_an_extra_call(tmp_path):
    replies = ['ACTION: read_file\n{"path":"component.js"}', yaml.safe_dump(mapping())]
    agent, client = agent_run(tmp_path, replies, 2)
    result = agent.synthesize(task(), tmp_path, artifact_dir=tmp_path / "out")
    assert (
        len(client.calls) == 2
        and result.model_generated_spec == mapping()["generated_spec"]
    )
    assert "reserved final response" in client.calls[-1]["messages"][-1]["content"]
    meta = json.loads((tmp_path / "out/synthesis-meta.json").read_text())
    assert meta["terminal_mapping_without_final_wrapper"] is True


def test_final_turn_does_not_execute_another_tool_or_exceed_the_budget(tmp_path):
    agent, client = agent_run(
        tmp_path,
        [
            'ACTION: read_file\n{"path":"component.js"}',
            'ACTION: read_file\n{"path":"never-read.js"}',
        ],
        2,
    )
    with pytest.raises(SynthesisError, match="FINAL"):
        agent.synthesize(task(), tmp_path, artifact_dir=tmp_path / "out")
    assert len(client.calls) == 2
    meta = json.loads((tmp_path / "out/synthesis-meta.json").read_text())
    assert meta["terminal_action_not_dispatched"] is True and meta["tool_calls"] == 1


@pytest.mark.parametrize("public_alias", [False, True])
def test_real_file_read_binding_stays_strict_even_with_public_task_evidence(
    tmp_path, public_alias
):
    value = mapping()
    value["source_evidence"][0]["source_path"] = "unread.js"
    if public_alias:
        value["source_evidence"].append(
            {
                "source_path": "Public issue state description",
                "matched_symbol": "task",
                "rationale": "Provided text",
            }
        )
    agent, client = agent_run(
        tmp_path,
        ['ACTION: read_file\n{"path":"component.js"}', yaml.safe_dump(value)],
        2,
    )
    with pytest.raises(SynthesisError, match="not read"):
        agent.synthesize(task(), tmp_path)
    assert len(client.calls) == 2


def test_mixed_actual_file_and_public_text_evidence_survives_end_to_end(tmp_path):
    value = mapping()
    value["source_evidence"].append(
        {
            "source_path": "Public issue state description",
            "matched_symbol": "task",
            "rationale": "Provided text",
        }
    )
    agent, client = agent_run(
        tmp_path,
        ['ACTION: read_file\n{"path":"component.js"}', yaml.safe_dump(value)],
        2,
    )
    result = agent.synthesize(task(), tmp_path)
    assert result.source_evidence[1]["kind"] == "public_task" and len(client.calls) == 2


def test_empty_format_repair_response_remains_a_failure_after_one_attempt(tmp_path):
    value = mapping()
    value["runtime_validation"] = None
    client = Replies("")
    with pytest.raises(SynthesisError):
        finalize_formal_c1_model_output(
            task=task(),
            raw_output=yaml.safe_dump(value),
            client=client,
            model="fake/model",
            located={"repo": str(tmp_path), "inputs_read": []},
            root=tmp_path,
            artifact_dir=tmp_path / "out",
            source_cost_kind="checkpoint_exploration",
        )
    assert len(client.calls) == 1


def test_unknown_schema_keywords_fail_closed():
    with pytest.raises(ValueError, match="unsupported schema keywords"):
        validate({}, {"type": "object", "dependentRequired": {"a": ["b"]}})


def test_failed_repair_does_not_publish_an_accepted_mapping(tmp_path):
    value = mapping()
    value["generated_spec"]["actions"][0]["selector"] = 17
    client = Replies(yaml.safe_dump(value))
    with pytest.raises(SynthesisError):
        finalize_formal_c1_model_output(
            task=task(),
            raw_output=yaml.safe_dump(value),
            client=client,
            model="fake/model",
            located={"repo": str(tmp_path), "inputs_read": []},
            root=tmp_path,
            artifact_dir=tmp_path / "out",
            source_cost_kind="checkpoint_exploration",
        )
    assert (tmp_path / "out/format-repair/protected-candidate.json").is_file()
    assert not (tmp_path / "out/format-repair/accepted-mapping.json").exists()
    assert (
        json.loads((tmp_path / "out/format-repair/meta.json").read_text())["succeeded"]
        is False
    )


def test_plain_mapping_does_not_relax_react_protocol_before_final_slot(tmp_path):
    value = mapping()
    agent, client = agent_run(
        tmp_path,
        [
            'ACTION: read_file\n{"path":"component.js"}',
            yaml.safe_dump(value),
            "FINAL\n```yaml\n" + yaml.safe_dump(value) + "```",
        ],
        3,
    )
    result = agent.synthesize(task(), tmp_path)
    assert len(client.calls) == 3
    assert result.model_generated_spec == value["generated_spec"]


def test_format_repair_cannot_replan_an_over_budget_action_sequence():
    original = mapping()
    original["generated_spec"]["actions"] = [
        {"type": "click", "selector": f"#step-{i}"} for i in range(101)
    ]
    proposed = mapping()
    proposed["generated_spec"]["actions"] = [{"type": "click", "selector": "#new-plan"}]
    accepted, _ = preserve_valid_fields(
        original, proposed, checkpoint_schema(TASK, "jupyterlab")
    )
    assert (
        accepted["generated_spec"]["actions"] == original["generated_spec"]["actions"]
    )
    assert validate(accepted, checkpoint_schema(TASK, "jupyterlab"))
