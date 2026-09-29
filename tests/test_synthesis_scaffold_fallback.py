"""Unit tests for the self-synthesis injection fix.

Regression coverage for two bugs found by log analysis of the same-start
experiment: (1) ``normalize_llm_checkpoint_mapping``/``public_scaffold_checkpoint``
used to crash with ``ValueError: No synthesis template for public task: X`` for any
task id absent from ``_TEMPLATES`` (10/20 of the ss20 subset), even when the model's
own ``generated_spec`` was well-formed; and (2) a well-formed model spec that merely
did not byte-match the hidden scaffold was silently replaced by the scaffold
(``used_public_scaffold``), so callers injecting ``generated_spec`` were never
actually testing model self-synthesis. The fix keeps ``model_generated_spec`` as the
model's untouched output and makes scaffold lookup optional.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from warptest.synthesis import (
    BENCHMARK_CONTRACTS,
    SynthesisError,
    _wrap_type_placeholders,
    build_explore_system_prompt,
    build_llm_checkpoint_prompt,
    find_placeholder_echoes,
    is_usable_generated_spec,
    normalize_llm_checkpoint_mapping,
    public_scaffold_checkpoint,
)
from warptest.task_schema import PublicIssue

TEMPLATED_TASK = SimpleNamespace(id="2048-merge-left", benchmark="2048")
UNTEMPLATED_TASK = SimpleNamespace(id="no-such-task-in-templates", benchmark="2048")
TEMPLATED_ISSUE = PublicIssue(id="2048-merge-left", benchmark="2048", repo="games/2048", issue="test issue")


def _valid_llm_mapping(generated_spec: dict) -> dict:
    return {
        "task_id": generated_spec.get("task_id", "t"),
        "benchmark": "2048",
        "generated_spec": generated_spec,
        "source_evidence": [{"source_path": "games/2048/js/game_manager.js", "matched_symbol": "GameManager", "rationale": "r"}],
        "payload": {"kind": "checkpoint"},
        "runtime_validation": {"status": "pending"},
        "validity_class": "unknown",
    }


class NormalizeMissingTemplateTests(unittest.TestCase):
    def test_well_formed_model_spec_survives_untouched_when_no_template_exists(self) -> None:
        model_spec = {
            "name": "model's own spec",
            "benchmark": "2048",
            "target": {"kind": "board", "board": [[4, 0, 0, 0]] * 4, "score": 8},
            "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
            "actions": [{"type": "move", "direction": "right"}],
            "assertions": [{"type": "score_increased_by_at_least", "amount": 4}],
        }
        data = _valid_llm_mapping(model_spec)

        # Must not raise even though UNTEMPLATED_TASK has no scaffold.
        normalized, warnings = normalize_llm_checkpoint_mapping(data, task=UNTEMPLATED_TASK)

        self.assertEqual(normalized["generated_spec"]["target"], model_spec["target"])
        self.assertEqual(normalized["generated_spec"]["actions"], model_spec["actions"])
        self.assertNotIn("generated_spec_canonicalized_with_public_task_scaffold", warnings)
        self.assertIn("no_public_scaffold_template_for_task", warnings)

    def test_non_mapping_model_spec_with_no_template_raises_synthesis_error(self) -> None:
        data = _valid_llm_mapping({})
        data["generated_spec"] = "prose, not a mapping"

        with self.assertRaises(SynthesisError):
            normalize_llm_checkpoint_mapping(data, task=UNTEMPLATED_TASK)

    def test_non_mapping_model_spec_with_template_falls_back_to_scaffold(self) -> None:
        data = _valid_llm_mapping({})
        data["generated_spec"] = "prose, not a mapping"

        normalized, warnings = normalize_llm_checkpoint_mapping(data, task=TEMPLATED_TASK)

        self.assertIn("generated_spec_string_wrapped_with_public_task_scaffold", warnings)
        self.assertEqual(normalized["generated_spec"]["benchmark"], "2048")

    def test_model_spec_diverging_from_template_is_still_canonicalized_when_template_exists(self) -> None:
        model_spec = {
            "name": "diverging",
            "benchmark": "2048",
            "target": {"kind": "board", "board": [[0, 0, 0, 0]] * 4},
            "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
            "actions": [],
            "assertions": [],
        }
        data = _valid_llm_mapping(model_spec)

        normalized, warnings = normalize_llm_checkpoint_mapping(data, task=TEMPLATED_TASK)

        self.assertIn("generated_spec_canonicalized_with_public_task_scaffold", warnings)
        self.assertEqual(normalized["generated_spec"]["llm_generated_spec_raw"], model_spec)


class PublicScaffoldCheckpointFallbackTests(unittest.TestCase):
    def test_missing_template_returns_unusable_checkpoint_instead_of_crashing(self) -> None:
        checkpoint = public_scaffold_checkpoint(
            UNTEMPLATED_TASK,
            located={"inputs_read": []},
            model="test-model",
            reason="LLM output was not an executable checkpoint mapping",
        )

        self.assertIsNone(checkpoint.model_generated_spec)
        self.assertFalse(checkpoint.used_public_scaffold)
        self.assertEqual(checkpoint.generated_spec, {})
        self.assertIn("no public scaffold template available", checkpoint.payload["model_fallback_reason"])

    def test_existing_template_still_used_for_scaffold_fallback(self) -> None:
        checkpoint = public_scaffold_checkpoint(
            TEMPLATED_TASK,
            located={"inputs_read": []},
            model="test-model",
            reason="LLM output was not an executable checkpoint mapping",
        )

        self.assertIsNone(checkpoint.model_generated_spec)
        self.assertTrue(checkpoint.used_public_scaffold)
        self.assertEqual(checkpoint.generated_spec["benchmark"], "2048")


class WrapTypePlaceholdersTests(unittest.TestCase):
    """Bare type-name hints (e.g. "score": "integer") must render as unmistakable
    placeholders, not values a model could plausibly mistake for the answer. Longer
    descriptive hints and enum-choice strings are left untouched (not the observed
    failure mode, and harder to distinguish safely from real literal values).
    """

    def test_bare_type_words_are_wrapped(self) -> None:
        contract = {"score": "integer", "bestScore": "Integer", "name": "non-empty string", "ok": "bool"}
        wrapped = _wrap_type_placeholders(contract)
        self.assertEqual(wrapped["score"], "<integer>")
        self.assertEqual(wrapped["bestScore"], "<Integer>")
        self.assertEqual(wrapped["name"], "<non-empty string>")
        self.assertEqual(wrapped["ok"], "<bool>")

    def test_descriptive_and_enum_hints_are_left_untouched(self) -> None:
        contract = {
            "board": "4x4 integer matrix before action",
            "direction": "left|right|up|down",
            "benchmark": "2048",
            "system_id": "system id string (discover exact id from source)",
        }
        wrapped = _wrap_type_placeholders(contract)
        self.assertEqual(wrapped, contract)

    def test_recurses_through_nested_dicts_and_lists(self) -> None:
        contract = {"actions": [{"type": "move", "wait_ms": "integer"}], "target": {"score": "integer"}}
        wrapped = _wrap_type_placeholders(contract)
        self.assertEqual(wrapped["actions"][0]["wait_ms"], "<integer>")
        self.assertEqual(wrapped["target"]["score"], "<integer>")
        self.assertEqual(wrapped["actions"][0]["type"], "move")

    def test_every_benchmark_contract_wraps_without_error(self) -> None:
        for benchmark, contract in BENCHMARK_CONTRACTS.items():
            _wrap_type_placeholders(contract)  # must not raise for any real contract


class FindPlaceholderEchoesTests(unittest.TestCase):
    def test_detects_bracketed_and_bare_forms(self) -> None:
        spec = {"target": {"score": "<integer>", "bestScore": "integer", "board": [[2, 2, 0, 0]] * 4}}
        echoes = find_placeholder_echoes(spec)
        self.assertIn("target.score", echoes)
        self.assertIn("target.bestScore", echoes)
        self.assertEqual(len(echoes), 2)

    def test_no_false_positive_on_concrete_values(self) -> None:
        spec = {
            "name": "recovered checkpoint",
            "target": {"kind": "board", "board": [[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]], "score": 0},
            "actions": [{"type": "move", "direction": "left"}],
        }
        self.assertEqual(find_placeholder_echoes(spec), [])

    def test_reports_dotted_path_through_lists(self) -> None:
        spec = {"items": [{"count": "integer"}]}
        echoes = find_placeholder_echoes(spec)
        self.assertEqual(echoes, ["items[0].count"])


class NormalizePlaceholderEchoTests(unittest.TestCase):
    """Regression: gpt-5.4-nano and qwen3.5-omni-flash's self-synthesis runs on the
    2048 same-start subset returned generated_spec verbatim from the prompt's schema
    hint (e.g. ``score: integer``), which used to pass structural validation and fail
    much later at restore time with an opaque ``invalid literal for int() ... 'integer'``.
    """

    def test_placeholder_echo_raises_even_when_template_exists(self) -> None:
        model_spec = {
            "name": "non-empty string",
            "benchmark": "2048",
            "target": {"kind": "board", "board": "4x4 integer matrix before action", "score": "integer", "bestScore": "integer"},
            "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
            "actions": [{"type": "move", "direction": "left", "wait_ms": 0}],
            "assertions": [{"type": "tile_value_present"}],
        }
        data = _valid_llm_mapping(model_spec)

        with self.assertRaises(SynthesisError) as ctx:
            normalize_llm_checkpoint_mapping(data, task=TEMPLATED_TASK)
        self.assertIn("placeholder", str(ctx.exception).lower())
        self.assertIn("target.score", ctx.exception.artifacts.get("placeholder_echo_paths", []))

    def test_placeholder_echo_raises_when_no_template_exists(self) -> None:
        model_spec = {
            "name": "x",
            "benchmark": "2048",
            "target": {"kind": "board", "board": [[0, 0, 0, 0]] * 4, "score": "integer"},
            "checkpoint": {},
            "actions": [],
            "assertions": [],
        }
        data = _valid_llm_mapping(model_spec)

        with self.assertRaises(SynthesisError):
            normalize_llm_checkpoint_mapping(data, task=UNTEMPLATED_TASK)

    def test_concrete_values_do_not_trigger_false_positive(self) -> None:
        # Use the untemplated task so canonicalization-on-mismatch (a separate, existing
        # behavior) cannot obscure whether the placeholder-echo check itself fires.
        model_spec = {
            "name": "concrete",
            "benchmark": "2048",
            "target": {"kind": "board", "board": [[4, 0, 0, 0]] * 4, "score": 8, "bestScore": 8},
            "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
            "actions": [{"type": "move", "direction": "right"}],
            "assertions": [{"type": "score_increased_by_at_least", "amount": 4}],
        }
        data = _valid_llm_mapping(model_spec)

        normalized, warnings = normalize_llm_checkpoint_mapping(data, task=UNTEMPLATED_TASK)
        self.assertEqual(normalized["generated_spec"]["target"]["score"], 8)


class PromptWarnsAgainstPlaceholderEchoTests(unittest.TestCase):
    def test_explore_prompt_wraps_hints_and_warns(self) -> None:
        prompt = build_explore_system_prompt(TEMPLATED_ISSUE)
        self.assertIn("<integer>", prompt)
        self.assertIn("type hints, not literal", prompt)
        self.assertNotIn("score: integer\n", prompt)

    def test_single_shot_prompt_wraps_hints_and_warns(self) -> None:
        located = {"profile": None, "repo": "games/2048", "graph": None, "source_evidence": []}
        prompt = build_llm_checkpoint_prompt(TEMPLATED_ISSUE, located)
        self.assertIn("<integer>", prompt)
        self.assertIn("type hints, not literal", prompt)


class IsUsableGeneratedSpecTests(unittest.TestCase):
    """Regression: an empty ``generated_spec`` dict (``{}``) is not ``None`` and used to
    slip past callers' bare ``is None``/``is not None`` checks (``gui_agent.py``,
    ``state_loop.py``), getting injected as if it were a real checkpoint despite lacking
    every required field (``target``, ``checkpoint``, ``actions``, ``assertions``).
    """

    def test_none_is_not_usable(self) -> None:
        self.assertFalse(is_usable_generated_spec(None))

    def test_empty_dict_is_not_usable(self) -> None:
        self.assertFalse(is_usable_generated_spec({}))

    def test_non_dict_is_not_usable(self) -> None:
        self.assertFalse(is_usable_generated_spec("prose output, not a mapping"))
        self.assertFalse(is_usable_generated_spec(["not", "a", "mapping"]))

    def test_dict_missing_required_fields_is_not_usable(self) -> None:
        self.assertFalse(is_usable_generated_spec({"name": "x", "benchmark": "2048"}))
        self.assertFalse(
            is_usable_generated_spec({"target": {}, "checkpoint": {}, "actions": []})
        )  # missing "assertions"

    def test_well_formed_spec_is_usable(self) -> None:
        spec = {
            "name": "x",
            "benchmark": "2048",
            "target": {"kind": "board", "board": [[0, 0, 0, 0]] * 4, "score": 0},
            "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
            "actions": [{"type": "move", "direction": "left"}],
            "assertions": [{"type": "tile_value_present"}],
        }
        self.assertTrue(is_usable_generated_spec(spec))


class ModelGeneratedSpecEmptyDictTests(unittest.TestCase):
    """Regression: an LLM response with ``generated_spec: {}`` used to produce a
    non-None, empty ``model_generated_spec`` on the resulting ``MethodCheckpoint``,
    which callers gating on ``is None`` would then treat as a real, injectable spec.
    """

    def test_empty_generated_spec_yields_no_usable_model_spec(self) -> None:
        data = _valid_llm_mapping({
            "target": {"kind": "board", "board": [[0, 0, 0, 0]] * 4, "score": 0},
            "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
            "actions": [{"type": "move", "direction": "left"}],
            "assertions": [{"type": "tile_value_present"}],
        })
        # Simulate the LLM having echoed an empty mapping for generated_spec, as
        # happens when the model produces no usable structured output.
        data["generated_spec"] = {}
        raw_generated_spec = data.get("generated_spec")
        model_generated_spec = raw_generated_spec if is_usable_generated_spec(raw_generated_spec) else None
        self.assertIsNone(model_generated_spec)


if __name__ == "__main__":
    unittest.main()
