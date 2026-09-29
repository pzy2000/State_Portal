"""Single public schema for Lazarus generated state, validation and safe repair.

The validator implements only the JSON Schema keywords emitted in this module;
no dependency or remote schema lookup is needed. Python integer checks deliberately
exclude booleans and floats, matching the native adapters' argument contract.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

DRAFT = "https://json-schema.org/draft/2020-12/schema"
REVISION = "lzp-checkpoint-schema-v1"
SPEC_KIND = "p2p_only_state"
STRATEGY = "p2p_only_typed_restore_v3"
STRING = {"type": "string"}
NONEMPTY = {"type": "string", "pattern": r"\S"}
SLUG = {"type": "string", "pattern": r"^[A-Za-z0-9_-]+$(?![\s\S])"}
WORLD_NAME = {
    "type": "string",
    "pattern": r"^(?=.*[A-Za-z0-9_-])[A-Za-z0-9_ -]+$(?![\s\S])",
    "description": "Safe world directory name. Spaces are allowed; separators, dot navigation and control characters are not.",
}
INTEGER = {"type": "integer", "minimum": 0}
TABS = ["about", "local", "online", "content", "settings", "credits"]
COMPONENT_PROPS = {
    "toolbar_button": {"label": STRING, "pressed": {"type": "boolean"}},
    "variables": {
        "scope": STRING,
        "variables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": STRING, "value": STRING},
                "required": ["name", "value"],
                "additionalProperties": False,
            },
        },
    },
    "html_select": {"label": STRING},
    "shortcut_chip": {"text": STRING, "class_name": STRING},
    "themed_button": {"text": STRING, "class_name": STRING},
    "selection": {"text": STRING, "class_name": STRING},
}
ACTION_FIELDS = {
    "jupyterlab": {
        "click": {"selector": STRING},
        "set_class": {"selector": STRING, "class_name": STRING},
        "select_text": {"selector": STRING, "start": INTEGER, "end": INTEGER},
        "append_button": {"text": STRING, "class_name": STRING},
        "add_variable": {"name": STRING, "value": STRING},
        "open_panel": {"value": STRING, "options": {"type": "array", "items": STRING}},
    },
    "luanti": {
        "set_tab": {"tab": STRING},
        "menu_event": {
            "fields": {
                "type": "object",
                "additionalProperties": {"anyOf": [STRING, {"type": "boolean"}]},
            }
        },
        "open_settings": {"section": STRING},
        "configure_world": {"world": STRING},
        "pointer_move": {"x": INTEGER, "y": INTEGER},
    },
    "openra": {
        "widget_event": {
            "id": STRING,
            "event": {"type": "string", "enum": ["click", "mouse_down"]},
        },
        "set_widget_text": {"id": STRING, "text": STRING},
    },
}


def object_schema(
    properties: dict, *, optional: tuple[str, ...] = (), extra: Any = False
) -> dict:
    return {
        "type": "object",
        "properties": copy.deepcopy(properties),
        "required": [key for key in properties if key not in optional],
        "additionalProperties": extra,
    }


def state_schema(benchmark: str) -> dict:
    if benchmark == "jupyterlab":
        return {
            "oneOf": [
                object_schema(
                    {
                        "component": {"const": name, "type": "string"},
                        "theme": {
                            "type": "string",
                            "enum": ["light", "dark", "high_contrast"],
                        },
                        "props": object_schema(props),
                    }
                )
                for name, props in COMPONENT_PROPS.items()
            ]
        }
    if benchmark == "luanti":
        mod = object_schema(
            {"name": SLUG, "description": {**STRING, "not": {"pattern": "\n"}}}
        )
        world = object_schema(
            {
                "name": WORLD_NAME,
                "gameid": SLUG,
                "mods": {"type": "array", "items": mod},
            }
        )
        return object_schema(
            {
                "tab": {"type": "string", "enum": TABS},
                "settings": {
                    "type": "object",
                    "propertyNames": {
                        "not": {"pattern": r"^(warptest|secure\.|screenshot_)"}
                    },
                    "additionalProperties": STRING,
                },
                "worlds": {"type": "array", "items": world},
            }
        )
    if benchmark == "openra":
        return {
            "oneOf": [
                object_schema({"screen": {"const": "mainmenu", "type": "string"}}),
                object_schema(
                    {
                        "screen": {"const": "editor", "type": "string"},
                        "map_index": INTEGER,
                        "editor_tab": {
                            "type": "string",
                            "enum": ["overlays", "tiles"],
                            "default": "overlays",
                        },
                    },
                    optional=("editor_tab",),
                ),
            ]
        }
    raise ValueError(f"unsupported benchmark: {benchmark}")


def action_schema(benchmark: str) -> dict:
    return {
        "oneOf": [
            object_schema({"type": {"const": op, "type": "string"}, **fields})
            for op, fields in ACTION_FIELDS[benchmark].items()
        ]
    }


def spec_schema(benchmark: str) -> dict:
    return {
        "$schema": DRAFT,
        **object_schema(
            {
                "schema_version": {"const": 1, "type": "integer"},
                "benchmark": {"const": benchmark, "type": "string"},
                "target": object_schema(
                    {"kind": {"const": SPEC_KIND}, "state": state_schema(benchmark)}
                ),
                "checkpoint": {
                    "const": {
                        "allowed_strategies": [STRATEGY],
                        "expected_validity": "injection-only",
                    }
                },
                "actions": {
                    "type": "array",
                    "maxItems": 100,
                    "items": action_schema(benchmark),
                },
                "assertions": {
                    "type": "array",
                    "description": "Diagnostic only. Never authoritative grading evidence.",
                },
            }
        ),
    }


def repository_evidence_schema() -> dict:
    return object_schema(
        {
            "source_path": NONEMPTY,
            "matched_symbol": NONEMPTY,
            "rationale": NONEMPTY,
            "kind": {"const": "repository"},
        },
        optional=("kind",),
        extra=True,
    )


def checkpoint_schema(task_id: str, benchmark: str) -> dict:
    repository = repository_evidence_schema()
    public = object_schema(
        {
            "kind": {"const": "public_task"},
            "field": {"const": "issue"},
            "rationale": NONEMPTY,
            "matched_symbol": NONEMPTY,
        },
        optional=("matched_symbol",),
    )
    return {
        "$schema": DRAFT,
        "title": REVISION,
        **object_schema(
            {
                "task_id": {"const": task_id},
                "benchmark": {"const": benchmark},
                "generated_spec": spec_schema(benchmark),
                "source_evidence": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"oneOf": [repository, public]},
                    "contains": repository,
                    "description": "At least one repository citation is required and must name a file actually read. Public task text uses kind=public_task, field=issue; it is not a repository file.",
                },
                "payload": object_schema(
                    {"kind": {"const": "state_injection"}}, extra=True
                ),
                "runtime_validation": object_schema(
                    {"status": NONEMPTY, "detail": STRING}, extra=True
                ),
                "validity_class": {"const": "injection-only"},
            }
        ),
    }


@dataclass(frozen=True)
class Violation:
    path: tuple
    message: str

    def render(self) -> str:
        return (
            "$"
            + "".join(
                f"[{x}]" if isinstance(x, int) else "." + str(x) for x in self.path
            )
            + ": "
            + self.message
        )


def _branch(value: Any, choices: list[dict]) -> dict:
    # A valid discriminator wins even if its payload is malformed. Repair must
    # not change an operation/component into a different schema variant.
    if isinstance(value, dict):
        for choice in choices:
            constants = {
                k: v["const"]
                for k, v in choice.get("properties", {}).items()
                if "const" in v
            }
            if constants and all(value.get(k) == v for k, v in constants.items()):
                return choice
    return min(choices, key=lambda schema: len(validate(value, schema)))


def validate(value: Any, schema: dict, path: tuple = ()) -> list[Violation]:
    supported = {
        "$schema",
        "title",
        "description",
        "default",
        "type",
        "oneOf",
        "anyOf",
        "not",
        "const",
        "enum",
        "pattern",
        "minimum",
        "properties",
        "required",
        "additionalProperties",
        "propertyNames",
        "minItems",
        "maxItems",
        "items",
        "contains",
    }
    unknown = set(schema) - supported
    if unknown:
        raise ValueError(f"unsupported schema keywords: {sorted(unknown)}")
    errors = []

    def fail(message):
        errors.append(Violation(path, message))

    for union in ("oneOf", "anyOf"):
        if union in schema:
            matches = [s for s in schema[union] if not validate(value, s)]
            if (union == "oneOf" and len(matches) != 1) or (
                union == "anyOf" and not matches
            ):
                return validate(value, _branch(value, schema[union]), path) or [
                    Violation(path, "ambiguous schema variant")
                ]
    if "not" in schema and not validate(value, schema["not"]):
        fail("forbidden value or name")
    expected = schema.get("type")
    accepted = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "boolean": type(value) is bool,
    }
    if expected is not None and not accepted[expected]:
        return [Violation(path, f"must be {expected}")]
    if "const" in schema and value != schema["const"]:
        fail("must equal " + repr(schema["const"]))
    if "enum" in schema and value not in schema["enum"]:
        fail("must be one of " + repr(schema["enum"]))
    if isinstance(value, str):
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            fail("must match " + schema["pattern"])
    if type(value) is int and "minimum" in schema and value < schema["minimum"]:
        fail(f"must be >= {schema['minimum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(Violation((*path, key), "required field is missing"))
        for key, item in value.items():
            if not isinstance(key, str):
                fail("object keys must be strings")
                continue
            if "propertyNames" in schema:
                errors.extend(validate(key, schema["propertyNames"], (*path, key)))
            child = props.get(key, schema.get("additionalProperties", True))
            if child is False:
                errors.append(Violation((*path, key), "unknown field"))
            elif isinstance(child, dict):
                errors.extend(validate(item, child, (*path, key)))
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get(
            "maxItems", float("inf")
        ):
            fail("array length outside allowed range")
        if "contains" in schema and not any(
            not validate(item, schema["contains"]) for item in value
        ):
            fail("must contain a repository-file citation")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(validate(item, schema["items"], (*path, index)))
    return errors


def preserve_valid_fields(
    original: Any, repaired: Any, schema: dict
) -> tuple[Any, list[str]]:
    """Accept repairs only inside invalid fields; preserve valid values and list order."""
    restored = []

    def merge(old, new, rule, path):
        if not validate(old, rule):
            if old != new:
                restored.append(
                    Violation(path, "original valid value preserved").render()
                )
            return copy.deepcopy(old)
        for union in ("oneOf", "anyOf"):
            if union in rule:
                rule = _branch(old, rule[union])
        if isinstance(old, dict) and rule.get("type") == "object":
            result = copy.deepcopy(new) if isinstance(new, dict) else {}
            for key, value in old.items():
                child = rule.get("properties", {}).get(
                    key, rule.get("additionalProperties", True)
                )
                if child is True:
                    child = {}
                if isinstance(child, dict):
                    result[key] = merge(value, result.get(key), child, (*path, key))
            return result
        if isinstance(old, list) and rule.get("type") == "array":
            new_list = new if isinstance(new, list) else []
            needs_members = len(old) < rule.get("minItems", 0) or (
                "contains" in rule
                and not any(not validate(item, rule["contains"]) for item in old)
            )
            if len(new_list) != len(old):
                restored.append(
                    Violation(
                        path,
                        "original entries preserved; missing membership may be appended"
                        if needs_members
                        else "original array shape preserved",
                    ).render()
                )
            result = [
                merge(
                    item,
                    new_list[i] if i < len(new_list) else None,
                    rule.get("items", {}),
                    (*path, i),
                )
                for i, item in enumerate(old)
            ]
            if needs_members:
                # Empty/missing evidence membership can be repaired by appending
                # citations. Existing valid citations still cannot be replaced.
                result.extend(copy.deepcopy(new_list[len(old) :]))
            return result
        return copy.deepcopy(new)

    return merge(original, repaired, schema, ()), restored
