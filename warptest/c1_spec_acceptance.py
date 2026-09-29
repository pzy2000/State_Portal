"""One shared accept-or-reject decision for a C1 model-generated start spec.

Both synthesizing arms (``c1_state`` and ``c1_agent_s_synth``) used to seed and
validate the model's checkpoint with their own code.  The GUI arm always ran the
strict C1 contract, while six of the state arm's backend gates called
``is_usable_generated_spec`` without ``task=`` and therefore only checked that
the four required keys existed.  Identical model output could then be accepted by
one arm and rejected by the other, which made the two arms' Start numbers
incomparable for reasons unrelated to either method.

The decision now happens exactly once, here, before the shared synthesis cache
stores the outcome, so both arms restore the same seeded spec or record the same
fair failure.  The two arms also used to seed with duplicate implementations
(``json.loads(json.dumps(...))`` vs ``copy.deepcopy``) that differed on which
inputs they rejected; :func:`seed_generated_spec` is now the only one, and
JSON-serializability is an explicit validation step rather than a side effect of
copying.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, fields
from typing import Any

from .synthesis import generated_spec_contract_errors
from .synthesis_ablation import empty_auxiliary_assertions_allowed
from .task_schema import MethodCheckpoint, PublicIssue


ACCEPTED_STATUS = "accepted"
NO_MODEL_SPEC_STATUS = "no_model_spec"
INVALID_MODEL_SPEC_STATUS = "invalid_model_spec"

# Aliases that name the same registered benchmark. A model that answers ``pal3``
# for a task registered as ``pal3.unity`` is correct, so comparing the raw
# strings would reject a usable spec on one arm only.
_BENCHMARK_ALIASES: dict[str, str] = {
    "pal3.unity": "pal3",
    "pal3unity": "pal3",
    "minetest": "luanti",
}

# Executable operations each Unity C1 task can actually run. The state arm has
# always enforced these; the GUI arm has not, so the same spec could be a fair
# zero on one arm and a graded trial on the other.
UNITY_SPEC_CONTRACTS: dict[str, dict[str, frozenset[str]]] = {
    "jynew": {
        "target_kinds": frozenset({"synthesized_state"}),
        "checkpoint_strategies": frozenset({"jynew_utility_save_validate"}),
        "action_types": frozenset(
            {
                "jynew_join_team",
                "jynew_leave_team",
                "jynew_add_item",
                "jynew_use_item",
                "jynew_learn_skill",
                "jynew_level_up",
                "jynew_rest",
                "jynew_set_key_value",
                "jynew_save",
                "jynew_load_save",
                "jynew_enter_submap",
                "jynew_add_money",
            }
        ),
        "assertion_types": frozenset(
            {
                "jynew_role_attr",
                "jynew_team_contains",
                "jynew_team_count",
                "jynew_item_count",
                "jynew_money_gte",
                "jynew_money_equals",
                "jynew_skill_learned",
                "jynew_key_value_equals",
                "jynew_event_flag",
                "jynew_map_id",
                "no_jynew_utility_errors",
            }
        ),
    },
    "pal3": {
        "target_kinds": frozenset({"synthesized_state"}),
        "checkpoint_strategies": frozenset({"pal3_console_save_validate"}),
        "action_types": frozenset(
            {
                "pal3_set_story_var",
                "pal3_add_team_member",
                "pal3_remove_team_member",
                "pal3_add_money",
                "pal3_add_item",
                "pal3_enable_world_region",
                "pal3_set_favor",
                "pal3_load_scene",
                "pal3_save",
                "pal3_load_save",
            }
        ),
        "assertion_types": frozenset(
            {
                "pal3_story_var_equals",
                "pal3_scene_is",
                "pal3_team_contains",
                "pal3_team_count",
                "pal3_item_count",
                "pal3_money_gte",
                "pal3_world_region_enabled",
                "pal3_favor_gte",
                "no_pal3_utility_errors",
            }
        ),
    },
}


def canonical_c1_benchmark(value: Any) -> str:
    """Normalize a benchmark id so alias spellings compare equal."""

    normalized = str(value or "").strip().lower()
    return _BENCHMARK_ALIASES.get(normalized, normalized)


def seed_generated_spec(
    spec: dict[str, Any], seed: int | None, *, benchmark: str
) -> dict[str, Any]:
    """Attach the formal experiment seed without exposing grader-only state."""

    seeded = copy.deepcopy(spec)
    if seed is None:
        return seeded
    seeded["experiment_seed"] = int(seed)
    target = seeded.get("target")
    if isinstance(target, dict) and canonical_c1_benchmark(benchmark) in {
        "datingsim",
        "love",
    }:
        target["seed"] = int(seed)
    return seeded


def unity_contract_errors(spec: dict[str, Any], *, benchmark: str) -> list[str]:
    """Return Unity executability errors, or ``[]`` for a non-Unity benchmark."""

    normalized = canonical_c1_benchmark(benchmark)
    contract = UNITY_SPEC_CONTRACTS.get(normalized)
    if contract is None:
        return []

    errors: list[str] = []
    target = spec.get("target")
    target_kind = target.get("kind") if isinstance(target, dict) else None
    if target_kind not in contract["target_kinds"]:
        errors.append("generated_spec target kind is not supported by this Unity task")

    checkpoint = spec.get("checkpoint")
    strategies = (
        checkpoint.get("allowed_strategies") if isinstance(checkpoint, dict) else None
    )
    if (
        not isinstance(strategies, list)
        or not strategies
        or any(
            strategy not in contract["checkpoint_strategies"]
            for strategy in strategies
        )
    ):
        errors.append(
            "generated_spec checkpoint strategy is not supported by this Unity task"
        )

    for section_name, allowed in (
        ("actions", contract["action_types"]),
        ("assertions", contract["assertion_types"]),
    ):
        entries = spec.get(section_name)
        # Predicted assertions are auxiliary. Only the registered no-assertion
        # intervention may omit them; executable actions and all other variants
        # retain their existing non-empty requirements.
        allow_empty = (
            section_name == "assertions" and empty_auxiliary_assertions_allowed()
        )
        if not isinstance(entries, list) or (not entries and not allow_empty):
            errors.append(f"generated_spec {section_name} must be a non-empty list")
            continue
        if any(
            not isinstance(entry, dict) or entry.get("type") not in allowed
            for entry in entries
        ):
            errors.append(
                f"generated_spec {section_name} contain an operation "
                "unsupported by this Unity task"
            )
    return errors


def generated_spec_acceptance_errors(
    spec: Any, *, issue: PublicIssue, benchmark: str
) -> list[str]:
    """Union of the contract both arms must agree on.

    The strict C1 contract is the GUI arm's historical gate; the normalized
    benchmark match and the Unity executability allowlists are the state arm's.
    Taking the union means one spec is either accepted by both arms or rejected
    by both.
    """

    errors = list(generated_spec_contract_errors(spec, task=issue))
    if not isinstance(spec, dict) or not spec:
        return errors

    if canonical_c1_benchmark(spec.get("benchmark")) != canonical_c1_benchmark(
        benchmark
    ):
        errors.append("generated_spec benchmark does not match the registered task")
    errors.extend(unity_contract_errors(spec, benchmark=benchmark))
    return list(dict.fromkeys(errors))


@dataclass(frozen=True)
class SpecAcceptance:
    """The single accept-or-reject verdict shared by both synthesizing arms."""

    spec: dict[str, Any] | None
    status: str
    errors: tuple[str, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status == ACCEPTED_STATUS

    @property
    def reason(self) -> str | None:
        if self.accepted:
            return None
        if self.status == INVALID_MODEL_SPEC_STATUS:
            return (
                "self-synthesized model spec could not be canonicalized: "
                + "; ".join(self.errors)
            )
        return (
            "self-synthesis produced no usable model spec (prose, unparseable output, "
            "or a mapping that violates the public executable contract); neither arm "
            "falls back to the public scaffold, so this is a fair 0: "
            + "; ".join(self.errors)
        )


def checkpoint_payload(
    checkpoint: Any, accepted_spec: dict[str, Any]
) -> dict[str, Any]:
    """Serialize a checkpoint carrying the one accepted seeded spec.

    The state arm rebuilds a :class:`MethodCheckpoint` from this mapping, so its
    ``model_generated_spec`` must be the spec both arms restore rather than the
    pre-seeding candidate. Duck-typed synthesis agents that expose the fields but
    not ``to_dict`` are supported, because a success that cannot carry both
    projections would be written to the shared cache and then be unreadable by
    the other arm.
    """

    to_dict = getattr(checkpoint, "to_dict", None)
    if callable(to_dict):
        payload = dict(to_dict())
    else:
        payload = {
            field.name: copy.deepcopy(getattr(checkpoint, field.name, None))
            for field in fields(MethodCheckpoint)
        }
    payload["model_generated_spec"] = copy.deepcopy(accepted_spec)
    return payload


def accept_c1_generated_spec(
    candidate: Any, *, issue: PublicIssue, benchmark: str, seed: int | None
) -> SpecAcceptance:
    """Seed, then validate, one model-generated C1 start spec.

    The order matters: the seeded spec is what both arms actually restore, so it
    is the spec that has to satisfy the contract. The state arm used to validate
    the unseeded candidate instead.
    """

    if not isinstance(candidate, dict) or not candidate:
        return SpecAcceptance(
            None,
            NO_MODEL_SPEC_STATUS,
            ("generated_spec must be a non-empty mapping",),
        )
    try:
        seeded = seed_generated_spec(candidate, seed, benchmark=benchmark)
        # Both the cache entry and the report serialize the spec, so a value the
        # model produced that JSON cannot encode has to fail here rather than
        # mid-run on one arm only.
        json.dumps(seeded, sort_keys=True, ensure_ascii=True)
    except Exception as error:  # noqa: BLE001 - graded as a fair failure
        return SpecAcceptance(
            None,
            INVALID_MODEL_SPEC_STATUS,
            (f"{type(error).__name__}: {error}",),
        )
    errors = generated_spec_acceptance_errors(
        seeded, issue=issue, benchmark=benchmark
    )
    if errors:
        return SpecAcceptance(None, NO_MODEL_SPEC_STATUS, tuple(errors))
    return SpecAcceptance(seeded, ACCEPTED_STATUS)
