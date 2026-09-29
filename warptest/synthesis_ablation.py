"""Public-input interventions for the seed-0 checkpoint synthesis study.

These policies affect generation only. They must never change the hidden grader.
No environment variable or model-authored field can select a policy.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import re
from typing import Any

ABLATION_REVISION = "synthesis-components-v1"
ABLATION_VARIANTS = (
    "full",
    "no_source_exploration",
    "schema_only_contract",
    "no_assertion_prediction",
)
_ACTIVE_VARIANT: ContextVar[str] = ContextVar("synthesis_ablation_variant", default="full")


@contextmanager
def ablation_scope(variant: str):
    token = _ACTIVE_VARIANT.set(validate_variant(variant))
    try:
        yield
    finally:
        _ACTIVE_VARIANT.reset(token)


def empty_auxiliary_assertions_allowed() -> bool:
    return _ACTIVE_VARIANT.get() == "no_assertion_prediction"


def scoped_agent_policy(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with ablation_scope(self.ablation_variant):
            return function(self, *args, **kwargs)
    return wrapped


def scoped_output_policy(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with ablation_scope(kwargs.get("ablation_variant", "full")):
            return function(*args, **kwargs)
    return wrapped


def validate_variant(value: str) -> str:
    if value not in ABLATION_VARIANTS:
        raise ValueError(f"Unknown synthesis ablation variant: {value!r}")
    return value


def project_contract(value: Any) -> Any:
    """Remove prose guidance while retaining the public interface vocabulary.

    This is a prompt projection, not a replacement executable schema. The same
    original validator runs in every group. Field names, literal constants,
    enums, nested structure and numeric bounds remain visible. The recursive
    output and the original are archived for a pre-run projection audit.
    """
    if isinstance(value, dict):
        return {
            key: project_contract(item)
            for key, item in value.items()
            if key not in {"_note", "note", "selection_rule", "description", "rationale"}
        }
    if isinstance(value, list):
        return [project_contract(item) for item in value]
    if not isinstance(value, str):
        return copy.deepcopy(value)
    if value.startswith("="):
        return "string"
    optional = bool(re.search(r"\boptional\b", value))
    raw = re.sub(r"\([^)]*\)", "", value).strip()
    raw = re.sub(r"^optional\s+", "", raw)
    prefix = "optional " if optional else ""
    if re.match(r"^4x4 integer matrix\b", raw):
        return prefix + "4x4 integer matrix"
    if re.match(r"^list of .*integers\b", raw):
        return prefix + "list of integers"
    vector = re.match(r"^(\d+)-number\b", raw)
    if vector:
        return prefix + f"{vector.group(1)}-number array"
    scalar = re.match(
        r"^((?:non-negative|positive) integer|non-empty string|integer|int|float|boolean|bool|scalar|number|string)\b"
        r"(?:\s+(\d+\.\.\d+))?", raw
    )
    if scalar:
        return prefix + scalar.group(1) + (" " + scalar.group(2) if scalar.group(2) else "")
    if raw in {"value the array must contain", "expected value at that path"}:
        return prefix + "scalar"
    if "scalar" in raw or raw in {"same concrete goal value used by the action"}:
        return prefix + "scalar"
    if raw.startswith("positional argument"):
        return prefix + "scalar"
    # Enum identifiers and public state-path patterns precede their prose hint.
    if "|" in raw:
        return prefix + raw.split(" when ", 1)[0].split(" node id", 1)[0]
    if " " not in raw:
        return prefix + raw
    return prefix + "string"


def public_contract_view(contract: Any, variant: str) -> Any:
    validate_variant(variant)
    return project_contract(contract) if variant == "schema_only_contract" else copy.deepcopy(contract)


def policy_instruction(variant: str) -> str:
    validate_variant(variant)
    if variant == "no_source_exploration":
        return (
            "Repository queries are unavailable in this configuration. Use only the public task, "
            "initial repository map and public contract. Emit FINAL without requesting ACTION. "
            "source_evidence must be an empty list []; do not invent source citations."
        )
    if variant == "no_assertion_prediction":
        return (
            "Generate the Start checkpoint and transition actions, but do not generate predicted "
            "after-state assertions. Set generated_spec.assertions to the empty list []. "
            "The assertion type vocabulary is provided only for interface consistency."
        )
    return ""
