"""Public generated-state schema. Contains capabilities, never task solutions."""

from __future__ import annotations

from typing import Any

from .lazarus_p2p_only_schema import (
    ACTION_FIELDS,
    COMPONENT_PROPS,
    SPEC_KIND as SPEC_KIND,
    STRATEGY as STRATEGY,
    action_schema,
    spec_schema,
    state_schema,
    validate,
)

COMPONENTS = tuple(COMPONENT_PROPS)
OPERATIONS = {
    host: {op: tuple(fields) for op, fields in operations.items()}
    for host, operations in ACTION_FIELDS.items()
}


def public_contract(benchmark: str) -> dict:
    return {
        "generated_spec_json_schema": spec_schema(benchmark),
        "payload_kind": "state_injection",
        "container_api": {
            "jupyterlab": {
                "selectors": {
                    "toolbar_button": "#button-host jp-button",
                    "variables": "#tree-host",
                    "html_select": ".strip button / #panel-host",
                    "shortcut_chip": "#line",
                    "selection": "#line",
                    "themed_button": "#host",
                },
                "layout_resources": "Candidate CSS is loaded; glyph-face is the public monospace third-party font class. Candidate CSS class names are discoverable in source. No class is automatically applied by task id.",
                "controls": "The public workbench exposes explicit state JSON, Add variable, Apply class, Append button and selector options. All inputs start empty.",
            },
            "luanti": {
                "operations": "Menu events call the candidate button handler; dialog sections and field names are discovered in candidate source. World state writes only data and empty mods, never implementation code. A world's gameid must be installed; the development fixture provides devtest. Restoring local selects the first supplied world's game and makes that world visible. Other games are not silently substituted."
            },
            "openra": {
                "operations": "widget_event invokes the named candidate ButtonWidget handler; widget IDs are discovered in chrome YAML. map_index selects from available lobby maps."
            },
        }[benchmark],
    }


def spec_errors(spec: Any, benchmark: str) -> list[str]:
    if benchmark not in OPERATIONS:
        return ["unsupported benchmark"]
    return [error.render() for error in validate(spec, spec_schema(benchmark))]


def state_errors(state: Any, benchmark: str) -> list[str]:
    if benchmark not in OPERATIONS:
        return ["unsupported benchmark"]
    return [error.render() for error in validate(state, state_schema(benchmark))]


def action_errors(action: Any, benchmark: str) -> list[str]:
    if benchmark not in OPERATIONS:
        return ["unsupported benchmark"]
    return [error.render() for error in validate(action, action_schema(benchmark))]
