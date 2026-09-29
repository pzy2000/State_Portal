from __future__ import annotations

from pathlib import Path
from typing import Any

from .c1_evaluation.contract import (
    materialize_legacy_spec,
    validate_evaluation_contract,
)
from .synthesis_ablation import empty_auxiliary_assertions_allowed


VALID_CHECKPOINT_VALIDITIES = {"replay-reachable", "save-restorable", "injection-only", "unknown"}
VALID_VISUAL_LABELS = {"correct", "incorrect", "uncertain"}
VALID_2048_DIRECTIONS = {"up", "right", "down", "left"}
OPENRA_C1_SETTING_KEYS = {
    ("Player", "Name"),
    ("Game", "ViewportEdgeScroll"),
    ("Game", "TargetLines"),
    ("Game", "StatusBars"),
    ("Game", "IntroductionPromptVersion"),
}
OPENRA_GAMEPLAY_SETUP_KEYS = {
    "cash",
    "fast_build",
    "build_anywhere",
    "all_tech",
    "delay_ticks",
    "remove_player_actor_types",
}
OPENRA_GAMEPLAY_REMOVABLE_ACTORS = {
    "fact",
    "powr",
    "proc",
    "barr",
    "tent",
    "dome",
    "weap",
    "e1",
    "e3",
    "2tnk",
}


# Public projection of the Blender semantic-action runner contract.  These
# rows are model-visible schema guidance, not task answers: they name only the
# parameters consumed by ``blender_driver._run_action``.  Synthesis and the
# post-Start minimal planner both import this exact projection, while
# ``_validate_blender_action`` below enforces the same required/allowed keys
# before a candidate can reach Blender.
BLENDER_PUBLIC_ACTION_CONTRACT: tuple[dict[str, Any], ...] = (
    {
        "type": "blender_add_primitive",
        "required": {
            "primitive": "cube|uv_sphere|cylinder|cone",
            "name": "non-empty Blender object name",
        },
        "optional": {
            "location": "3-number vector [x,y,z]",
            "scale": "3-number vector [x,y,z]",
            "rotation": "3-number Euler vector [x,y,z]",
            "size": "number",
            "segments": "integer",
            "ring_count": "integer",
            "vertices": "integer",
            "radius": "number",
            "depth": "number",
            "radius1": "number",
            "radius2": "number",
            "material": {
                "name": "non-empty material name",
                "color": "4-number RGBA vector",
            },
        },
        "additional_fields_allowed": False,
    },
    {
        "type": "blender_assign_material",
        "required": {
            "object": "non-empty existing Blender object name",
            "material": {
                "name": "non-empty material name",
                "color": "4-number RGBA vector",
            },
        },
        "optional": {},
        "additional_fields_allowed": False,
    },
    {
        "type": "blender_set_object_location",
        "required": {
            "object": "non-empty existing Blender object name",
            "location": "3-number vector [x,y,z]",
        },
        "optional": {},
        "additional_fields_allowed": False,
    },
    {
        "type": "blender_set_object_scale",
        "required": {
            "object": "non-empty existing Blender object name",
            "scale": "3-number vector [x,y,z]",
        },
        "optional": {},
        "additional_fields_allowed": False,
    },
    {
        "type": "blender_set_object_rotation",
        "required": {
            "object": "non-empty existing Blender object name",
            "rotation": "3-number Euler vector [x,y,z]",
        },
        "optional": {},
        "additional_fields_allowed": False,
    },
    {
        "type": "blender_set_frame",
        "required": {"frame": "integer frame number"},
        "optional": {},
        "additional_fields_allowed": False,
    },
    {
        "type": "blender_add_modifier",
        "required": {
            "object": "non-empty existing Blender object name",
            "name": "non-empty modifier name",
            "modifier_type": "non-empty Blender modifier type, e.g. BEVEL",
        },
        "optional": {
            "width": "number",
            "segments": "integer",
            "levels": "integer",
            "render_levels": "integer",
        },
        "additional_fields_allowed": False,
    },
    {
        "type": "blender_apply_modifier",
        "required": {
            "object": "non-empty existing Blender object name",
            "modifier": "non-empty existing modifier name",
        },
        "optional": {},
        "additional_fields_allowed": False,
    },
    {
        "type": "blender_set_keyframe",
        "required": {
            "object": "non-empty existing Blender object name",
            "data_path": "location|scale|rotation_euler",
            "frame": "integer frame number",
            "value": "3-number vector",
        },
        "optional": {},
        "additional_fields_allowed": False,
    },
    {
        "type": "blender_save_as",
        "required": {"path": "non-empty .blend artifact path"},
        "optional": {},
        "additional_fields_allowed": False,
    },
    {
        "type": "blender_reopen",
        "required": {"path": "non-empty .blend artifact path"},
        "optional": {},
        "additional_fields_allowed": False,
    },
    {
        "type": "wait",
        "required": {"ms": "non-negative integer milliseconds"},
        "optional": {},
        "additional_fields_allowed": False,
    },
)

_BLENDER_PUBLIC_ACTION_BY_TYPE = {
    str(row["type"]): row
    for row in BLENDER_PUBLIC_ACTION_CONTRACT
    if row["type"] != "wait"
}


class SpecSchemaError(ValueError):
    def __init__(self, spec_path: str | Path, errors: list[str]) -> None:
        self.spec_path = str(spec_path)
        self.errors = errors
        detail = "; ".join(errors)
        super().__init__(f"Spec schema validation failed for {self.spec_path}: {detail}")


def validate_spec_schema(
    spec: dict[str, Any],
    spec_path: str | Path,
    visual_required: bool = False,
    *,
    allow_lazarus: bool = False,
) -> list[str]:
    errors: list[str] = validate_evaluation_contract(
        spec, spec_path, allow_lazarus=allow_lazarus
    )
    spec = materialize_legacy_spec(spec)
    path = Path(spec_path).expanduser().resolve()

    _require_string(spec, "name", errors)
    _require_string(spec, "benchmark", errors)
    _require_mapping(spec, "target", errors)
    checkpoint = _require_mapping(spec, "checkpoint", errors)
    actions = _require_list(spec, "actions", errors)
    assertions = _require_list(spec, "assertions", errors)

    if "tags" in spec:
        tags = _require_list(spec, "tags", errors)
        for index, tag in enumerate(tags):
            if not isinstance(tag, str) or not tag:
                errors.append(f"tags[{index}] must be a non-empty string")
    if "setup" in spec and not isinstance(spec["setup"], dict):
        errors.append("setup must be a mapping when present")
    if "timeout_ms" in spec:
        _non_negative_int(spec["timeout_ms"], "timeout_ms", errors, allow_zero=False)

    if isinstance(checkpoint, dict):
        strategies = checkpoint.get("allowed_strategies")
        if not isinstance(strategies, list) or not strategies:
            errors.append("checkpoint.allowed_strategies must be a non-empty list")
        elif any(not isinstance(strategy, str) or not strategy for strategy in strategies):
            errors.append("checkpoint.allowed_strategies entries must be non-empty strings")
        expected = checkpoint.get("expected_validity")
        if expected not in VALID_CHECKPOINT_VALIDITIES:
            errors.append(f"checkpoint.expected_validity must be one of {sorted(VALID_CHECKPOINT_VALIDITIES)}")

    if isinstance(actions, list):
        for index, action in enumerate(actions):
            _validate_action(action, index, errors)
    if isinstance(assertions, list):
        for index, assertion in enumerate(assertions):
            _validate_assertion(assertion, index, errors)
    benchmark = str(spec.get("benchmark") or "").strip().lower()
    if benchmark == "luanti" and isinstance(spec.get("target"), dict):
        _validate_luanti_target(spec["target"], errors)
    if benchmark == "libreoffice" and isinstance(spec.get("target"), dict):
        _validate_libreoffice_target(spec["target"], errors)
    if benchmark == "openra" and isinstance(spec.get("target"), dict):
        _validate_openra_target(spec["target"], errors)
        _validate_openra_settings_contract(spec, errors)
        _validate_openra_gameplay_setup(spec, errors)

    if "replay_from_start" in spec:
        _validate_replay_from_start(spec["replay_from_start"], errors)

    _validate_visual_oracle(spec.get("visual_oracle"), path, visual_required, errors)
    return errors


def ensure_valid_spec_schema(
    spec: dict[str, Any],
    spec_path: str | Path,
    visual_required: bool = False,
    *,
    allow_lazarus: bool = False,
) -> None:
    errors = validate_spec_schema(
        spec,
        spec_path,
        visual_required=visual_required,
        allow_lazarus=allow_lazarus,
    )
    if errors:
        raise SpecSchemaError(spec_path, errors)


def _require_string(spec: dict[str, Any], key: str, errors: list[str]) -> str | None:
    value = spec.get(key)
    if not isinstance(value, str) or not value:
        errors.append(f"{key} must be a non-empty string")
        return None
    return value


def _require_mapping(spec: dict[str, Any], key: str, errors: list[str]) -> dict[str, Any] | None:
    value = spec.get(key)
    if not isinstance(value, dict):
        errors.append(f"{key} must be a mapping")
        return None
    return value


def _require_list(spec: dict[str, Any], key: str, errors: list[str]) -> list[Any]:
    value = spec.get(key)
    if not isinstance(value, list):
        errors.append(f"{key} must be a list")
        return []
    return value


def _validate_action(action: Any, index: int, errors: list[str]) -> None:
    prefix = f"actions[{index}]"
    if not isinstance(action, dict):
        errors.append(f"{prefix} must be a mapping")
        return
    action_type = action.get("type")
    if not isinstance(action_type, str) or not action_type:
        errors.append(f"{prefix}.type must be a non-empty string")
        return
    for key in ("wait_ms", "ms"):
        if key in action:
            _non_negative_int(action[key], f"{prefix}.{key}", errors)
    if action_type == "move":
        if action.get("direction") not in VALID_2048_DIRECTIONS:
            errors.append(f"{prefix}.direction must be one of {sorted(VALID_2048_DIRECTIONS)}")
    elif action_type == "key":
        if not isinstance(action.get("key"), str) or not action.get("key"):
            errors.append(f"{prefix}.key must be a non-empty string")
    elif action_type == "click":
        if not isinstance(action.get("selector"), str) or not action.get("selector"):
            errors.append(f"{prefix}.selector must be a non-empty string")
    elif action_type == "hook_call":
        if not isinstance(action.get("method"), str) or not action.get("method"):
            errors.append(f"{prefix}.method must be a non-empty string")
        if "args" in action and not isinstance(action["args"], list):
            errors.append(f"{prefix}.args must be a list when present")
    elif action_type == "hook_action":
        if not isinstance(action.get("action_id"), str) or not action.get("action_id"):
            errors.append(f"{prefix}.action_id must be a non-empty string")
    elif action_type == "spacerpg_patch_state":
        patch = action.get("patch")
        if not isinstance(patch, dict) or not patch:
            errors.append(f"{prefix}.patch must be a non-empty mapping")
        elif any(
            key
            not in {
                "screen",
                "currentSystemId",
                "currentStationId",
                "stationTab",
                "player",
                "runtime",
                "factionHeat",
            }
            for key in patch
        ):
            errors.append(f"{prefix}.patch contains a non-allowlisted SpaceRPG state key")
    elif action_type == "spacerpg_mine_sortie":
        if action.get("resource") != "iron":
            errors.append(f"{prefix}.resource must be iron")
        if action.get("amount") != 2:
            errors.append(f"{prefix}.amount must be 2")
        if action.get("system_id") != "kuro-belt":
            errors.append(f"{prefix}.system_id must be kuro-belt")
    elif action_type in {"openra_validate_map", "openra_launch_map"}:
        if not isinstance(action.get("mod"), str) or not action.get("mod"):
            errors.append(f"{prefix}.mod must be a non-empty string")
        if not isinstance(action.get("map"), str) or not action.get("map"):
            errors.append(f"{prefix}.map must be a non-empty string")
    elif action_type == "openra_open_settings_panel":
        if action.get("panel") not in {"Input", "Display", "Game"}:
            errors.append(f"{prefix}.panel must be Input, Display, or Game")
    elif action_type == "openra_set_setting":
        if action.get("section") not in {"Player", "Game"}:
            errors.append(f"{prefix}.section must be Player or Game")
        _require_action_string(action, "key", prefix, errors)
        if (
            isinstance(action.get("section"), str)
            and isinstance(action.get("key"), str)
            and (action["section"], action["key"]) not in OPENRA_C1_SETTING_KEYS
        ):
            errors.append(
                f"{prefix}.{action['section']}.{action['key']} is not an allowlisted OpenRA C1 setting"
            )
        if "value" not in action:
            errors.append(f"{prefix}.value is required")
        elif not isinstance(action["value"], (str, int, float, bool)):
            errors.append(f"{prefix}.value must be a scalar")
    elif action_type == "openra_wait_ticks":
        _non_negative_int(action.get("ticks"), f"{prefix}.ticks", errors)
    elif action_type == "openra_deploy_actor":
        _require_action_string(action, "player", prefix, errors)
        _require_action_string(action, "actor_id", prefix, errors)
        if "wait_until_actor" in action:
            _require_action_string(action, "wait_until_actor", prefix, errors)
        if "timeout_ticks" in action:
            _non_negative_int(action["timeout_ticks"], f"{prefix}.timeout_ticks", errors, allow_zero=False)
    elif action_type == "openra_build_actor":
        _require_action_string(action, "player", prefix, errors)
        _require_action_string(action, "actor", prefix, errors)
        if "queue" in action:
            _require_action_string(action, "queue", prefix, errors)
        if "count" in action:
            _non_negative_int(action["count"], f"{prefix}.count", errors, allow_zero=False)
        if "timeout_ticks" in action:
            _non_negative_int(action["timeout_ticks"], f"{prefix}.timeout_ticks", errors, allow_zero=False)
        if "place_at" in action:
            _require_action_string(action, "place_at", prefix, errors)
        if "place_near" in action:
            place_near = action["place_near"]
            if not isinstance(place_near, dict):
                errors.append(f"{prefix}.place_near must be a mapping when present")
            else:
                if not isinstance(place_near.get("anchor"), str) or not place_near.get("anchor"):
                    errors.append(f"{prefix}.place_near.anchor must be a non-empty string")
                if "radius" in place_near:
                    _non_negative_int(place_near["radius"], f"{prefix}.place_near.radius", errors)
    elif action_type == "openra_build_group":
        _require_action_string(action, "player", prefix, errors)
        units = action.get("units")
        if not isinstance(units, list) or not units:
            errors.append(f"{prefix}.units must be a non-empty list")
        else:
            for unit_index, unit in enumerate(units):
                unit_prefix = f"{prefix}.units[{unit_index}]"
                if not isinstance(unit, dict):
                    errors.append(f"{unit_prefix} must be a mapping")
                    continue
                _require_action_string(unit, "actor", unit_prefix, errors)
                _require_action_string(unit, "queue", unit_prefix, errors)
                _non_negative_int(
                    unit.get("count", 1),
                    f"{unit_prefix}.count",
                    errors,
                    allow_zero=False,
                )
        if "timeout_ticks" in action:
            _non_negative_int(action["timeout_ticks"], f"{prefix}.timeout_ticks", errors, allow_zero=False)
    elif action_type == "openra_rally_group":
        _require_action_string(action, "player", prefix, errors)
        actors = action.get("actors")
        if not isinstance(actors, list) or not actors:
            errors.append(f"{prefix}.actors must be a non-empty list")
        else:
            for actor_index, actor in enumerate(actors):
                actor_prefix = f"{prefix}.actors[{actor_index}]"
                if not isinstance(actor, dict):
                    errors.append(f"{actor_prefix} must be a mapping")
                    continue
                _require_action_string(actor, "actor", actor_prefix, errors)
                _non_negative_int(
                    actor.get("count", 1),
                    f"{actor_prefix}.count",
                    errors,
                    allow_zero=False,
                )
        _require_action_string(action, "center", prefix, errors)
        _non_negative_int(action.get("radius", 0), f"{prefix}.radius", errors)
        if "timeout_ticks" in action:
            _non_negative_int(action["timeout_ticks"], f"{prefix}.timeout_ticks", errors, allow_zero=False)
    elif action_type in {"jynew_join_team", "jynew_leave_team"}:
        if "role_id" not in action:
            errors.append(f"{prefix}.role_id is required for {action_type}")
    elif action_type == "jynew_add_item":
        if "item_id" not in action:
            errors.append(f"{prefix}.item_id is required")
        if "item_count" in action:
            _non_negative_int(action["item_count"], f"{prefix}.item_count", errors, allow_zero=False)
    elif action_type == "jynew_use_item":
        if "item_id" not in action:
            errors.append(f"{prefix}.item_id is required")
    elif action_type == "jynew_learn_skill":
        if "skill_id" not in action:
            errors.append(f"{prefix}.skill_id is required")
    elif action_type == "jynew_level_up":
        pass
    elif action_type == "jynew_rest":
        pass
    elif action_type == "jynew_set_key_value":
        if not isinstance(action.get("key"), str) or not action.get("key"):
            errors.append(f"{prefix}.key must be a non-empty string")
        if "value" not in action:
            errors.append(f"{prefix}.value is required")
    elif action_type == "jynew_enter_submap":
        _non_negative_int(action.get("map_id"), f"{prefix}.map_id", errors, allow_zero=False)
    elif action_type == "jynew_add_money":
        if not isinstance(action.get("amount"), int) or isinstance(action.get("amount"), bool):
            errors.append(f"{prefix}.amount must be an integer")
    elif action_type in {"jynew_save", "jynew_load_save"}:
        if "save_index" not in action:
            errors.append(f"{prefix}.save_index is required")
    elif action_type == "pal3_set_story_var":
        if "var_id" not in action:
            errors.append(f"{prefix}.var_id is required")
        if "value" not in action:
            errors.append(f"{prefix}.value is required")
    elif action_type in {"pal3_add_team_member", "pal3_remove_team_member"}:
        if "actor_id" not in action:
            errors.append(f"{prefix}.actor_id is required for {action_type}")
    elif action_type == "pal3_add_money":
        if "amount" not in action:
            errors.append(f"{prefix}.amount is required")
    elif action_type == "pal3_add_item":
        if "item_id" not in action:
            errors.append(f"{prefix}.item_id is required")
        if "item_count" in action:
            _non_negative_int(action["item_count"], f"{prefix}.item_count", errors, allow_zero=False)
    elif action_type == "pal3_enable_world_region":
        if "region" not in action:
            errors.append(f"{prefix}.region is required")
    elif action_type == "pal3_set_favor":
        if "actor_id" not in action:
            errors.append(f"{prefix}.actor_id is required")
        if "amount" not in action:
            errors.append(f"{prefix}.amount is required")
    elif action_type == "pal3_load_scene":
        if not isinstance(action.get("city"), str) or not action.get("city"):
            errors.append(f"{prefix}.city must be a non-empty string")
        if not isinstance(action.get("scene"), str) or not action.get("scene"):
            errors.append(f"{prefix}.scene must be a non-empty string")
    elif action_type in {"pal3_save", "pal3_load_save"}:
        if "save_index" not in action:
            errors.append(f"{prefix}.save_index is required")
    elif action_type == "luanti_set_node":
        _require_pos(action, "pos", prefix, errors)
        _require_action_string(action, "node", prefix, errors)
        _validate_luanti_param2(action, f"{prefix}.param2", errors)
    elif action_type == "luanti_dig_node":
        _require_pos(action, "pos", prefix, errors)
    elif action_type == "luanti_drop_falling_node":
        _require_pos(action, "ground_pos", prefix, errors)
        _require_action_string(action, "node", prefix, errors)
        _validate_luanti_param2(action, f"{prefix}.param2", errors)
    elif action_type == "luanti_import_vector_string":
        _require_action_string(action, "value", prefix, errors)
        _require_pos(action, "marker_pos", prefix, errors)
    elif action_type == "luanti_round_half_boundary":
        _require_pos(action, "marker_pos", prefix, errors)
    elif action_type == "luanti_metadata_set_string_nil":
        _require_pos(action, "marker_pos", prefix, errors)
    elif action_type == "luanti_fill":
        _require_pos(action, "from", prefix, errors)
        _require_pos(action, "to", prefix, errors)
        _require_action_string(action, "node", prefix, errors)
        _validate_luanti_param2(action, f"{prefix}.param2", errors)
    elif action_type == "luanti_set_player_pos":
        _require_pos(action, "pos", prefix, errors)
    elif action_type == "luanti_set_player_hp":
        if "hp" not in action:
            errors.append(f"{prefix}.hp is required")
        else:
            _non_negative_int(action["hp"], f"{prefix}.hp", errors)
    elif action_type == "luanti_give_item":
        _require_action_string(action, "item", prefix, errors)
        if "count" in action:
            _non_negative_int(action["count"], f"{prefix}.count", errors, allow_zero=False)
    elif action_type == "luanti_set_player_inventory":
        if not isinstance(action.get("items"), list):
            errors.append(f"{prefix}.items must be a list")
    elif action_type == "luanti_set_meta_inventory":
        _require_pos(action, "pos", prefix, errors)
        if "items" in action and not isinstance(action["items"], list):
            errors.append(f"{prefix}.items must be a list when present")
    elif action_type == "luanti_set_mod_storage":
        _require_action_string(action, "key", prefix, errors)
        if "value" not in action:
            errors.append(f"{prefix}.value is required")
    elif action_type == "luanti_wait_steps":
        _non_negative_int(action.get("steps"), f"{prefix}.steps", errors, allow_zero=False)
    elif action_type == "luanti_save_world":
        pass
    elif action_type in _BLENDER_PUBLIC_ACTION_BY_TYPE:
        _validate_blender_action(action, prefix, errors)
    elif action_type in {
        "jupyterlab_create_text_file",
        "jupyterlab_open_file",
        "jupyterlab_edit_text_file",
        "jupyterlab_create_notebook",
        "jupyterlab_edit_notebook_cell",
        "jupyterlab_run_notebook_cell",
        "jupyterlab_run_notebook",
        "jupyterlab_export_notebook_html",
        "jupyterlab_start_session",
    }:
        _require_action_string(action, "path", prefix, errors)
        if action_type in {"jupyterlab_create_text_file", "jupyterlab_edit_text_file"} and "content" not in action:
            errors.append(f"{prefix}.content is required for {action_type}")
        if action_type == "jupyterlab_create_notebook" and "cells" in action and not isinstance(action["cells"], list):
            errors.append(f"{prefix}.cells must be a list when present")
        if action_type in {"jupyterlab_edit_notebook_cell", "jupyterlab_run_notebook_cell"}:
            if "cell_index" not in action:
                errors.append(f"{prefix}.cell_index is required for {action_type}")
            else:
                _non_negative_int(action["cell_index"], f"{prefix}.cell_index", errors)
        if action_type == "jupyterlab_edit_notebook_cell":
            _require_action_string(action, "source", prefix, errors)
        if action_type == "jupyterlab_export_notebook_html":
            _require_action_string(action, "output_path", prefix, errors)
    elif action_type == "jupyterlab_set_workspace_layout":
        _require_action_string(action, "workspace", prefix, errors)
        paths = action.get("paths")
        if not isinstance(paths, list) or not paths or any(not isinstance(path, str) or not path for path in paths):
            errors.append(f"{prefix}.paths must be a non-empty list of strings")
    elif action_type == "libreoffice_open_document":
        if "path" in action:
            _require_action_string(action, "path", prefix, errors)
    elif action_type == "libreoffice_replace_text":
        _require_action_string(action, "old", prefix, errors)
        if "new" not in action:
            errors.append(f"{prefix}.new is required")
    elif action_type == "libreoffice_append_paragraph":
        _require_action_string(action, "text", prefix, errors)
    elif action_type == "libreoffice_insert_toc":
        if "title" in action:
            _require_action_string(action, "title", prefix, errors)
        styles = action.get("include_styles")
        if styles is not None and (not isinstance(styles, list) or any(not isinstance(style, str) or not style for style in styles)):
            errors.append(f"{prefix}.include_styles must be a list of non-empty strings when present")
    elif action_type == "libreoffice_update_indexes":
        pass
    elif action_type == "libreoffice_apply_paragraph_style":
        _require_action_string(action, "text", prefix, errors)
        _require_action_string(action, "style", prefix, errors)
    elif action_type == "libreoffice_insert_bookmark":
        _require_action_string(action, "name", prefix, errors)
        _require_action_string(action, "text", prefix, errors)
    elif action_type == "libreoffice_insert_reference_field":
        _require_action_string(action, "bookmark", prefix, errors)
        if "prefix" in action:
            _require_action_string(action, "prefix", prefix, errors)
    elif action_type == "libreoffice_set_table_cell":
        _require_action_string(action, "table", prefix, errors)
        _require_action_string(action, "cell", prefix, errors)
        if "value" not in action:
            errors.append(f"{prefix}.value is required")
    elif action_type == "libreoffice_insert_table_row":
        _require_action_string(action, "table", prefix, errors)
        _non_negative_int(action.get("row"), f"{prefix}.row", errors, allow_zero=False)
        values = action.get("values")
        if not isinstance(values, list):
            errors.append(f"{prefix}.values must be a list")
    elif action_type == "libreoffice_append_table_row":
        _require_action_string(action, "table", prefix, errors)
        values = action.get("values")
        if not isinstance(values, list):
            errors.append(f"{prefix}.values must be a list")
    elif action_type == "libreoffice_save":
        if "path" in action:
            _require_action_string(action, "path", prefix, errors)
    elif action_type == "libreoffice_export_pdf":
        _require_action_string(action, "path", prefix, errors)
    elif action_type == "libreoffice_set_cell":
        _require_action_string(action, "cell", prefix, errors)
        if "sheet" in action:
            _require_action_string(action, "sheet", prefix, errors)
        if "value" not in action:
            errors.append(f"{prefix}.value is required")
    elif action_type == "libreoffice_set_formula":
        _require_action_string(action, "cell", prefix, errors)
        _require_action_string(action, "formula", prefix, errors)
        if "sheet" in action:
            _require_action_string(action, "sheet", prefix, errors)
    elif action_type == "libreoffice_recalculate":
        pass
    elif action_type == "libreoffice_sort_range":
        _require_action_string(action, "range", prefix, errors)
        _require_action_string(action, "key", prefix, errors)
        if "sheet" in action:
            _require_action_string(action, "sheet", prefix, errors)
        if action.get("order", "ascending") not in {"ascending", "descending"}:
            errors.append(f"{prefix}.order must be ascending or descending when present")
    elif action_type == "libreoffice_define_named_range":
        _require_action_string(action, "name", prefix, errors)
        _require_action_string(action, "range", prefix, errors)
        if "sheet" in action:
            _require_action_string(action, "sheet", prefix, errors)
    elif action_type == "libreoffice_create_chart":
        _require_action_string(action, "name", prefix, errors)
        _require_action_string(action, "range", prefix, errors)
        if "sheet" in action:
            _require_action_string(action, "sheet", prefix, errors)
        if "chart_type" in action:
            _require_action_string(action, "chart_type", prefix, errors)
    elif action_type == "libreoffice_set_slide_title":
        _non_negative_int(action.get("slide"), f"{prefix}.slide", errors, allow_zero=False)
        _require_action_string(action, "title", prefix, errors)
    elif action_type == "libreoffice_set_slide_bullets":
        _non_negative_int(action.get("slide"), f"{prefix}.slide", errors, allow_zero=False)
        bullets = action.get("bullets")
        if not isinstance(bullets, list) or any(not isinstance(item, str) or not item for item in bullets):
            errors.append(f"{prefix}.bullets must be a list of non-empty strings")
    elif action_type == "libreoffice_add_shape":
        _non_negative_int(action.get("slide"), f"{prefix}.slide", errors, allow_zero=False)
        _require_action_string(action, "text", prefix, errors)
        if "shape" in action:
            _require_action_string(action, "shape", prefix, errors)
    elif action_type == "libreoffice_move_slide":
        _non_negative_int(action.get("from"), f"{prefix}.from", errors, allow_zero=False)
        _non_negative_int(action.get("to"), f"{prefix}.to", errors, allow_zero=False)
    elif action_type == "libreoffice_reopen":
        if "path" in action:
            _require_action_string(action, "path", prefix, errors)
    elif action_type == "wait" and "ms" not in action:
        errors.append(f"{prefix}.ms is required for wait actions")


def _validate_assertion(assertion: Any, index: int, errors: list[str]) -> None:
    prefix = f"assertions[{index}]"
    if not isinstance(assertion, dict):
        errors.append(f"{prefix} must be a mapping")
        return
    assertion_type = assertion.get("type")
    if not isinstance(assertion_type, str) or not assertion_type:
        errors.append(f"{prefix}.type must be a non-empty string")
        return
    if assertion_type in {"state_equals", "state_gte", "state_contains"}:
        if not isinstance(assertion.get("path"), str) or not assertion.get("path"):
            errors.append(f"{prefix}.path must be a non-empty string")
        if "value" not in assertion:
            errors.append(f"{prefix}.value is required")
    elif assertion_type == "tile_value_present":
        if not isinstance(assertion.get("value"), int):
            errors.append(f"{prefix}.value must be an integer")
    elif assertion_type in {
        "largest_tile_equals",
        "empty_cell_count_at_least",
        "tile_count_at_least",
    }:
        key = "value" if assertion_type == "largest_tile_equals" else "count"
        _non_negative_int(assertion.get(key), f"{prefix}.{key}", errors)
    elif assertion_type == "board_cell_equals":
        for key in ("row", "column"):
            value = assertion.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3:
                errors.append(f"{prefix}.{key} must be an integer from 0 to 3")
        _non_negative_int(assertion.get("value"), f"{prefix}.value", errors)
    elif assertion_type == "score_increased_by_at_least":
        if not isinstance(assertion.get("amount"), int):
            errors.append(f"{prefix}.amount must be an integer")
    elif assertion_type == "localStorage_key_exists":
        if not isinstance(assertion.get("key"), str) or not assertion.get("key"):
            errors.append(f"{prefix}.key must be a non-empty string")
    elif assertion_type == "openra_actor_built":
        _require_action_string(assertion, "player", prefix, errors)
        _require_action_string(assertion, "actor", prefix, errors)
        if "min_delta" in assertion:
            _non_negative_int(assertion["min_delta"], f"{prefix}.min_delta", errors, allow_zero=False)
    elif assertion_type == "openra_actor_exists":
        _require_action_string(assertion, "player", prefix, errors)
        if not assertion.get("actor") and not assertion.get("actor_id"):
            errors.append(f"{prefix}.actor or {prefix}.actor_id is required")
        if "actor" in assertion:
            _require_action_string(assertion, "actor", prefix, errors)
        if "actor_id" in assertion:
            _require_action_string(assertion, "actor_id", prefix, errors)
    elif assertion_type == "openra_production_prerequisite_respected":
        _require_action_string(assertion, "player", prefix, errors)
        _require_action_string(assertion, "actor", prefix, errors)
    elif assertion_type == "openra_actor_group_in_region":
        _require_action_string(assertion, "player", prefix, errors)
        _require_action_string(assertion, "center", prefix, errors)
        _non_negative_int(assertion.get("radius", 0), f"{prefix}.radius", errors)
        actors = assertion.get("actors")
        if not isinstance(actors, list) or not actors:
            errors.append(f"{prefix}.actors must be a non-empty list")
        else:
            for actor_index, actor in enumerate(actors):
                actor_prefix = f"{prefix}.actors[{actor_index}]"
                if not isinstance(actor, dict):
                    errors.append(f"{actor_prefix} must be a mapping")
                    continue
                _require_action_string(actor, "actor", actor_prefix, errors)
                _non_negative_int(
                    actor.get("count", 1),
                    f"{actor_prefix}.count",
                    errors,
                    allow_zero=False,
                )
    elif assertion_type == "openra_setting_equals":
        if assertion.get("section") not in {"Player", "Game"}:
            errors.append(f"{prefix}.section must be Player or Game")
        _require_action_string(assertion, "key", prefix, errors)
        if (
            isinstance(assertion.get("section"), str)
            and isinstance(assertion.get("key"), str)
            and (assertion["section"], assertion["key"])
            not in OPENRA_C1_SETTING_KEYS
        ):
            errors.append(
                f"{prefix}.{assertion['section']}.{assertion['key']} is not an allowlisted OpenRA C1 setting"
            )
        if "expected" not in assertion:
            errors.append(f"{prefix}.expected is required")
        elif not isinstance(assertion["expected"], (str, int, float, bool)):
            errors.append(f"{prefix}.expected must be a scalar")
    elif assertion_type == "no_openra_errors":
        pass
    elif assertion_type == "jynew_role_attr":
        if "role_id" not in assertion:
            errors.append(f"{prefix}.role_id is required")
        if not isinstance(assertion.get("attr"), str) or not assertion.get("attr"):
            errors.append(f"{prefix}.attr must be a non-empty string")
        if "expected" not in assertion:
            errors.append(f"{prefix}.expected is required")
    elif assertion_type == "jynew_team_contains":
        if "role_id" not in assertion:
            errors.append(f"{prefix}.role_id is required")
    elif assertion_type == "jynew_team_count":
        if "expected" not in assertion:
            errors.append(f"{prefix}.expected is required")
    elif assertion_type == "jynew_item_count":
        if "item_id" not in assertion:
            errors.append(f"{prefix}.item_id is required")
        if "expected" not in assertion:
            errors.append(f"{prefix}.expected is required")
    elif assertion_type == "jynew_money_gte":
        if "int_value" not in assertion:
            errors.append(f"{prefix}.int_value is required")
    elif assertion_type == "jynew_money_equals":
        if "int_value" not in assertion:
            errors.append(f"{prefix}.int_value is required")
    elif assertion_type == "jynew_skill_learned":
        if "role_id" not in assertion:
            errors.append(f"{prefix}.role_id is required")
        if "skill_id" not in assertion:
            errors.append(f"{prefix}.skill_id is required")
    elif assertion_type == "jynew_key_value_equals":
        if not isinstance(assertion.get("key"), str) or not assertion.get("key"):
            errors.append(f"{prefix}.key must be a non-empty string")
        if "str_value" not in assertion:
            errors.append(f"{prefix}.str_value is required")
    elif assertion_type == "jynew_event_flag":
        if "scene_id" not in assertion:
            errors.append(f"{prefix}.scene_id is required")
        if "event_id" not in assertion:
            errors.append(f"{prefix}.event_id is required")
    elif assertion_type == "jynew_map_id":
        if "int_value" not in assertion:
            errors.append(f"{prefix}.int_value is required")
    elif assertion_type == "no_jynew_utility_errors":
        pass
    elif assertion_type == "pal3_story_var_equals":
        if "var_id" not in assertion:
            errors.append(f"{prefix}.var_id is required")
        if "expected" not in assertion:
            errors.append(f"{prefix}.expected is required")
    elif assertion_type == "pal3_scene_is":
        if not isinstance(assertion.get("city"), str) or not assertion.get("city"):
            errors.append(f"{prefix}.city must be a non-empty string")
        if not isinstance(assertion.get("scene"), str) or not assertion.get("scene"):
            errors.append(f"{prefix}.scene must be a non-empty string")
    elif assertion_type == "pal3_team_contains":
        if "actor_id" not in assertion:
            errors.append(f"{prefix}.actor_id is required")
    elif assertion_type == "pal3_team_count":
        if "expected" not in assertion:
            errors.append(f"{prefix}.expected is required")
    elif assertion_type == "pal3_item_count":
        if "item_id" not in assertion:
            errors.append(f"{prefix}.item_id is required")
        if "expected" not in assertion:
            errors.append(f"{prefix}.expected is required")
    elif assertion_type == "pal3_money_gte":
        if "int_value" not in assertion:
            errors.append(f"{prefix}.int_value is required")
    elif assertion_type == "pal3_world_region_enabled":
        if "region" not in assertion:
            errors.append(f"{prefix}.region is required")
    elif assertion_type == "pal3_favor_gte":
        if "actor_id" not in assertion:
            errors.append(f"{prefix}.actor_id is required")
        if "int_value" not in assertion:
            errors.append(f"{prefix}.int_value is required")
    elif assertion_type == "no_pal3_utility_errors":
        pass
    elif assertion_type == "luanti_node_is":
        _require_pos(assertion, "pos", prefix, errors)
        _require_action_string(assertion, "expected", prefix, errors)
    elif assertion_type == "luanti_node_count":
        _require_pos(assertion, "from", prefix, errors)
        _require_pos(assertion, "to", prefix, errors)
        _require_action_string(assertion, "node", prefix, errors)
        if "min_count" in assertion:
            _non_negative_int(assertion["min_count"], f"{prefix}.min_count", errors, allow_zero=False)
    elif assertion_type == "luanti_player_pos_near":
        _require_pos(assertion, "pos", prefix, errors)
        if "radius" in assertion and (
            not isinstance(assertion["radius"], (int, float))
            or isinstance(assertion["radius"], bool)
            or assertion["radius"] < 0
        ):
            errors.append(f"{prefix}.radius must be a non-negative number")
    elif assertion_type == "luanti_player_hp_gte":
        if "int_value" not in assertion:
            errors.append(f"{prefix}.int_value is required")
    elif assertion_type == "luanti_inventory_contains":
        _require_action_string(assertion, "item", prefix, errors)
        if "count" in assertion:
            _non_negative_int(assertion["count"], f"{prefix}.count", errors, allow_zero=False)
    elif assertion_type == "luanti_meta_inventory_contains":
        _require_pos(assertion, "pos", prefix, errors)
        _require_action_string(assertion, "item", prefix, errors)
        if "count" in assertion:
            _non_negative_int(assertion["count"], f"{prefix}.count", errors, allow_zero=False)
    elif assertion_type == "luanti_mod_storage_equals":
        _require_action_string(assertion, "key", prefix, errors)
        if "value" not in assertion:
            errors.append(f"{prefix}.value is required")
    elif assertion_type == "luanti_gametime_gte":
        if "value" not in assertion:
            errors.append(f"{prefix}.value is required")
    elif assertion_type == "no_luanti_mod_errors":
        pass
    elif assertion_type in {
        "jupyterlab_file_exists",
        "jupyterlab_file_text_equals",
        "jupyterlab_notebook_output_contains",
        "jupyterlab_notebook_cell_source_equals",
        "jupyterlab_notebook_all_code_cells_executed",
        "jupyterlab_notebook_has_mime",
        "jupyterlab_notebook_has_no_error_outputs",
        "jupyterlab_workspace_contains",
    }:
        _require_action_string(assertion, "path", prefix, errors)
        if assertion_type == "jupyterlab_file_text_equals" and "content" not in assertion:
            errors.append(f"{prefix}.content is required for jupyterlab_file_text_equals")
        if assertion_type == "jupyterlab_notebook_output_contains" and (
            not isinstance(assertion.get("text"), str) or not assertion.get("text")
        ):
            errors.append(f"{prefix}.text must be a non-empty string")
        if assertion_type == "jupyterlab_notebook_cell_source_equals":
            _non_negative_int(assertion.get("cell_index", 0), f"{prefix}.cell_index", errors)
            _require_action_string(assertion, "source", prefix, errors)
        if assertion_type == "jupyterlab_notebook_has_mime":
            _require_action_string(assertion, "mime", prefix, errors)
        if assertion_type == "jupyterlab_workspace_contains" and "workspace" in assertion:
            _require_action_string(assertion, "workspace", prefix, errors)
    elif assertion_type == "jupyterlab_session_started":
        pass
    elif assertion_type == "no_jupyterlab_server_errors":
        pass
    elif assertion_type == "libreoffice_text_contains":
        _require_action_string(assertion, "text", prefix, errors)
    elif assertion_type == "libreoffice_toc_contains":
        _require_action_string(assertion, "text", prefix, errors)
    elif assertion_type == "libreoffice_paragraph_style_is":
        _require_action_string(assertion, "text", prefix, errors)
        _require_action_string(assertion, "style", prefix, errors)
    elif assertion_type == "libreoffice_table_cell_equals":
        _require_action_string(assertion, "table", prefix, errors)
        _require_action_string(assertion, "cell", prefix, errors)
        if "expected" not in assertion:
            errors.append(f"{prefix}.expected is required")
    elif assertion_type == "libreoffice_bookmark_exists":
        _require_action_string(assertion, "name", prefix, errors)
    elif assertion_type == "libreoffice_reference_text_contains":
        _require_action_string(assertion, "text", prefix, errors)
    elif assertion_type == "libreoffice_pdf_exists":
        _require_action_string(assertion, "path", prefix, errors)
    elif assertion_type == "libreoffice_sheet_exists":
        if "sheet" in assertion:
            _require_action_string(assertion, "sheet", prefix, errors)
    elif assertion_type == "libreoffice_cell_value_equals":
        _require_action_string(assertion, "cell", prefix, errors)
        if "sheet" in assertion:
            _require_action_string(assertion, "sheet", prefix, errors)
        if "expected" not in assertion:
            errors.append(f"{prefix}.expected is required")
    elif assertion_type == "libreoffice_formula_equals":
        _require_action_string(assertion, "cell", prefix, errors)
        _require_action_string(assertion, "formula", prefix, errors)
        if "sheet" in assertion:
            _require_action_string(assertion, "sheet", prefix, errors)
    elif assertion_type == "libreoffice_named_range_exists":
        _require_action_string(assertion, "name", prefix, errors)
    elif assertion_type == "libreoffice_chart_exists":
        _require_action_string(assertion, "name", prefix, errors)
        if "sheet" in assertion:
            _require_action_string(assertion, "sheet", prefix, errors)
    elif assertion_type == "libreoffice_slide_count_equals":
        _non_negative_int(assertion.get("expected"), f"{prefix}.expected", errors, allow_zero=False)
    elif assertion_type == "libreoffice_slide_title_equals":
        _non_negative_int(assertion.get("slide"), f"{prefix}.slide", errors, allow_zero=False)
        _require_action_string(assertion, "expected", prefix, errors)
    elif assertion_type == "libreoffice_slide_text_contains":
        _non_negative_int(assertion.get("slide"), f"{prefix}.slide", errors, allow_zero=False)
        _require_action_string(assertion, "text", prefix, errors)
    elif assertion_type == "libreoffice_shape_text_contains":
        _non_negative_int(assertion.get("slide"), f"{prefix}.slide", errors, allow_zero=False)
        _require_action_string(assertion, "text", prefix, errors)
    elif assertion_type == "no_libreoffice_errors":
        pass


def _require_action_string(item: dict[str, Any], key: str, prefix: str, errors: list[str]) -> None:
    if not isinstance(item.get(key), str) or not item.get(key):
        errors.append(f"{prefix}.{key} must be a non-empty string")


def _require_numeric_vector(
    item: dict[str, Any],
    key: str,
    length: int,
    prefix: str,
    errors: list[str],
) -> None:
    value = item.get(key)
    if (
        not isinstance(value, list)
        or len(value) != length
        or any(
            not isinstance(component, (int, float)) or isinstance(component, bool)
            for component in value
        )
    ):
        errors.append(f"{prefix}.{key} must be a {length}-number vector")


def _validate_blender_material(
    value: Any, prefix: str, errors: list[str]
) -> None:
    if not isinstance(value, dict):
        errors.append(f"{prefix} must be a mapping")
        return
    unknown = sorted(set(value) - {"name", "color"})
    if unknown:
        errors.append(f"{prefix} contains unknown fields: {unknown}")
    _require_action_string(value, "name", prefix, errors)
    _require_numeric_vector(value, "color", 4, prefix, errors)


def _validate_blender_action(
    action: dict[str, Any], prefix: str, errors: list[str]
) -> None:
    action_type = str(action.get("type") or "")
    contract = _BLENDER_PUBLIC_ACTION_BY_TYPE[action_type]
    required = contract.get("required")
    optional = contract.get("optional")
    required_keys = set(required) if isinstance(required, dict) else set()
    optional_keys = set(optional) if isinstance(optional, dict) else set()
    allowed = {"type", *required_keys, *optional_keys}
    unknown = sorted(set(action) - allowed)
    if unknown:
        errors.append(f"{prefix} contains unknown fields: {unknown}")
    for key in sorted(required_keys):
        if key not in action:
            errors.append(f"{prefix}.{key} is required for {action_type}")

    if action_type == "blender_add_primitive":
        _require_action_string(action, "name", prefix, errors)
        if action.get("primitive") not in {"cube", "uv_sphere", "cylinder", "cone"}:
            errors.append(
                f"{prefix}.primitive must be cube, uv_sphere, cylinder, or cone"
            )
        for key in ("location", "scale", "rotation"):
            if key in action:
                _require_numeric_vector(action, key, 3, prefix, errors)
        for key in ("size", "radius", "depth", "radius1", "radius2"):
            if key in action and (
                not isinstance(action[key], (int, float))
                or isinstance(action[key], bool)
            ):
                errors.append(f"{prefix}.{key} must be a number")
        for key in ("segments", "ring_count", "vertices"):
            if key in action:
                _non_negative_int(action[key], f"{prefix}.{key}", errors, allow_zero=False)
        if "material" in action:
            _validate_blender_material(action["material"], f"{prefix}.material", errors)
    elif action_type == "blender_assign_material":
        _require_action_string(action, "object", prefix, errors)
        _validate_blender_material(action.get("material"), f"{prefix}.material", errors)
    elif action_type == "blender_set_object_location":
        _require_action_string(action, "object", prefix, errors)
        _require_numeric_vector(action, "location", 3, prefix, errors)
    elif action_type == "blender_set_object_scale":
        _require_action_string(action, "object", prefix, errors)
        _require_numeric_vector(action, "scale", 3, prefix, errors)
    elif action_type == "blender_set_object_rotation":
        _require_action_string(action, "object", prefix, errors)
        _require_numeric_vector(action, "rotation", 3, prefix, errors)
    elif action_type == "blender_set_frame":
        _non_negative_int(action.get("frame"), f"{prefix}.frame", errors)
    elif action_type == "blender_add_modifier":
        _require_action_string(action, "object", prefix, errors)
        _require_action_string(action, "name", prefix, errors)
        _require_action_string(action, "modifier_type", prefix, errors)
        for key in ("width",):
            if key in action and (
                not isinstance(action[key], (int, float))
                or isinstance(action[key], bool)
            ):
                errors.append(f"{prefix}.{key} must be a number")
        for key in ("segments", "levels", "render_levels"):
            if key in action:
                _non_negative_int(action[key], f"{prefix}.{key}", errors)
    elif action_type == "blender_apply_modifier":
        _require_action_string(action, "object", prefix, errors)
        _require_action_string(action, "modifier", prefix, errors)
    elif action_type == "blender_set_keyframe":
        _require_action_string(action, "object", prefix, errors)
        if action.get("data_path") not in {"location", "scale", "rotation_euler"}:
            errors.append(
                f"{prefix}.data_path must be location, scale, or rotation_euler"
            )
        _non_negative_int(action.get("frame"), f"{prefix}.frame", errors)
        _require_numeric_vector(action, "value", 3, prefix, errors)
    elif action_type in {"blender_save_as", "blender_reopen"}:
        _require_action_string(action, "path", prefix, errors)


def _require_pos(item: dict[str, Any], key: str, prefix: str, errors: list[str]) -> None:
    pos = item.get(key)
    if not isinstance(pos, dict):
        errors.append(f"{prefix}.{key} must be a mapping with x, y, z")
        return
    for axis in ("x", "y", "z"):
        value = pos.get(axis)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            errors.append(f"{prefix}.{key}.{axis} must be a number")


def _validate_luanti_target(target: dict[str, Any], errors: list[str]) -> None:
    if target.get("kind") not in {"synthesized_world", "save_restore"}:
        errors.append(
            "target.kind must be synthesized_world or save_restore for luanti"
        )
    nodes = target.get("nodes")
    if nodes is not None:
        if not isinstance(nodes, list):
            errors.append("target.nodes must be a list when present")
        else:
            for index, entry in enumerate(nodes):
                prefix = f"target.nodes[{index}]"
                if not isinstance(entry, dict):
                    errors.append(f"{prefix} must be a mapping")
                    continue
                _require_pos(entry, "pos", prefix, errors)
                _require_action_string(entry, "node", prefix, errors)
                _validate_luanti_param2(entry, f"{prefix}.param2", errors)
    fills = target.get("fill")
    if fills is not None:
        if not isinstance(fills, list):
            errors.append("target.fill must be a list when present")
        else:
            for index, entry in enumerate(fills):
                prefix = f"target.fill[{index}]"
                if not isinstance(entry, dict):
                    errors.append(f"{prefix} must be a mapping")
                    continue
                _require_pos(entry, "from", prefix, errors)
                _require_pos(entry, "to", prefix, errors)
                _require_action_string(entry, "node", prefix, errors)
                _validate_luanti_param2(entry, f"{prefix}.param2", errors)
    validations = target.get("validations")
    if validations is not None:
        if not isinstance(validations, list):
            errors.append("target.validations must be a list when present")
        else:
            for index, validation in enumerate(validations):
                prefix = f"target.validations[{index}]"
                if not isinstance(validation, dict):
                    errors.append(f"{prefix} must be a mapping")
                    continue
                _require_action_string(validation, "path", prefix, errors)
                if "expected" not in validation:
                    errors.append(f"{prefix}.expected is required")
    _validate_luanti_target_consistency(target, errors)


def _luanti_position_tuple(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, dict):
        return None
    coords: list[int] = []
    for axis in ("x", "y", "z"):
        item = value.get(axis)
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            return None
        if int(item) != item:
            return None
        coords.append(int(item))
    return coords[0], coords[1], coords[2]


def _validate_luanti_target_consistency(
    target: dict[str, Any], errors: list[str]
) -> None:
    """Reject start validations that contradict the state being synthesized."""

    explicit_nodes: dict[tuple[int, int, int], str] = {}
    for entry in target.get("nodes") or []:
        if isinstance(entry, dict) and isinstance(entry.get("node"), str):
            pos = _luanti_position_tuple(entry.get("pos"))
            if pos is not None:
                explicit_nodes[pos] = entry["node"]
    fill_entries: list[tuple[tuple[int, int, int], tuple[int, int, int], str]] = []
    for entry in target.get("fill") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("node"), str):
            continue
        start = _luanti_position_tuple(entry.get("from"))
        end = _luanti_position_tuple(entry.get("to"))
        if start is not None and end is not None:
            fill_entries.append((start, end, entry["node"]))
    player = target.get("player") if isinstance(target.get("player"), dict) else {}
    storage = {
        str(entry.get("key")): entry.get("value")
        for entry in target.get("mod_storage") or []
        if isinstance(entry, dict) and isinstance(entry.get("key"), str)
    }

    for index, validation in enumerate(target.get("validations") or []):
        if not isinstance(validation, dict) or "expected" not in validation:
            continue
        path = validation.get("path")
        expected = validation.get("expected")
        synthesized: Any = None
        found = False
        if isinstance(path, str) and path.startswith("node."):
            try:
                pos = tuple(int(part) for part in path[5:].split(","))
            except ValueError:
                pos = ()
            if len(pos) == 3:
                for start, end, node in fill_entries:
                    if all(min(start[i], end[i]) <= pos[i] <= max(start[i], end[i]) for i in range(3)):
                        synthesized, found = node, True
                if pos in explicit_nodes:
                    synthesized, found = explicit_nodes[pos], True
        elif path == "player.hp" and "hp" in player:
            synthesized, found = player["hp"], True
        elif isinstance(path, str) and path.startswith("mod_storage."):
            key = path[len("mod_storage.") :]
            if key in storage:
                synthesized, found = storage[key], True
        if found and str(synthesized) != str(expected):
            errors.append(
                f"target.validations[{index}].expected contradicts target state: "
                f"{path} synthesizes {synthesized!r}, not {expected!r}"
            )


def _validate_openra_target(target: dict[str, Any], errors: list[str]) -> None:
    if target.get("kind") != "openra_settings":
        return
    if target.get("mod") != "ra":
        errors.append("target.mod must be ra for openra_settings")
    settings = target.get("settings")
    if not isinstance(settings, dict) or not settings:
        errors.append("target.settings must be a non-empty mapping for openra_settings")
        return
    for section, values in settings.items():
        if not isinstance(values, dict) or not values:
            errors.append(f"target.settings.{section} must be a non-empty mapping")
            continue
        for key, value in values.items():
            if (section, key) not in OPENRA_C1_SETTING_KEYS:
                errors.append(f"target.settings.{section}.{key} is not an allowlisted OpenRA C1 setting")
            if not isinstance(value, (str, int, float, bool)):
                errors.append(f"target.settings.{section}.{key} must be a scalar")


def _validate_openra_settings_contract(
    spec: dict[str, Any], errors: list[str]
) -> None:
    target = spec.get("target")
    if not isinstance(target, dict) or target.get("kind") != "openra_settings":
        return
    checkpoint = spec.get("checkpoint")
    if isinstance(checkpoint, dict):
        if checkpoint.get("allowed_strategies") != [
            "openra_public_settings_restore"
        ]:
            errors.append(
                "openra_settings checkpoint.allowed_strategies must be exactly "
                "['openra_public_settings_restore']"
            )
        if checkpoint.get("expected_validity") != "save-restorable":
            errors.append(
                "openra_settings checkpoint.expected_validity must be save-restorable"
            )
    actions = spec.get("actions")
    if isinstance(actions, list):
        if not actions:
            errors.append("openra_settings actions must include openra_set_setting")
        if any(
            not isinstance(action, dict)
            or action.get("type") != "openra_set_setting"
            for action in actions
        ):
            errors.append(
                "openra_settings actions may contain only openra_set_setting"
            )
    assertions = spec.get("assertions")
    if isinstance(assertions, list):
        setting_assertions = [
            assertion
            for assertion in assertions
            if isinstance(assertion, dict)
            and assertion.get("type") == "openra_setting_equals"
        ]
        if not setting_assertions and not (
            assertions == [] and empty_auxiliary_assertions_allowed()
        ):
            errors.append(
                "openra_settings assertions must include openra_setting_equals"
            )
        if any(
            not isinstance(assertion, dict)
            or assertion.get("type")
            not in {"openra_setting_equals", "no_openra_errors"}
            for assertion in assertions
        ):
            errors.append(
                "openra_settings assertions may contain only "
                "openra_setting_equals and no_openra_errors"
            )


def _validate_openra_gameplay_setup(
    spec: dict[str, Any], errors: list[str]
) -> None:
    setup = spec.get("setup")
    if not isinstance(setup, dict) or "openra_gameplay_probe" not in setup:
        return
    probe = setup.get("openra_gameplay_probe")
    if not isinstance(probe, dict):
        errors.append("setup.openra_gameplay_probe must be a mapping")
        return
    unknown = sorted(set(probe) - OPENRA_GAMEPLAY_SETUP_KEYS)
    if unknown:
        errors.append(
            "setup.openra_gameplay_probe contains non-allowlisted keys: "
            + ", ".join(unknown)
        )
    if "cash" in probe:
        _non_negative_int(
            probe["cash"],
            "setup.openra_gameplay_probe.cash",
            errors,
        )
    for key in ("fast_build", "build_anywhere", "all_tech"):
        if key in probe and not isinstance(probe[key], bool):
            errors.append(f"setup.openra_gameplay_probe.{key} must be a boolean")
    if "delay_ticks" in probe:
        _non_negative_int(
            probe["delay_ticks"],
            "setup.openra_gameplay_probe.delay_ticks",
            errors,
        )
    removals = probe.get("remove_player_actor_types")
    target = spec.get("target")
    target_players = {
        player.get("id")
        for player in target.get("players", [])
        if isinstance(target, dict)
        and isinstance(target.get("players"), list)
        and isinstance(player, dict)
        and isinstance(player.get("id"), str)
    }
    if removals is not None:
        if not isinstance(removals, dict) or not removals:
            errors.append(
                "setup.openra_gameplay_probe.remove_player_actor_types must be a non-empty mapping"
            )
        else:
            for player, actor_types in removals.items():
                if player not in target_players:
                    errors.append(
                        "setup.openra_gameplay_probe.remove_player_actor_types "
                        f"references undeclared player {player!r}"
                    )
                if not isinstance(actor_types, list) or not actor_types:
                    errors.append(
                        "setup.openra_gameplay_probe.remove_player_actor_types."
                        f"{player} must be a non-empty list"
                    )
                    continue
                for actor_type in actor_types:
                    if actor_type not in OPENRA_GAMEPLAY_REMOVABLE_ACTORS:
                        errors.append(
                            "setup.openra_gameplay_probe.remove_player_actor_types."
                            f"{player} contains non-allowlisted actor {actor_type!r}"
                        )


def _validate_luanti_param2(item: dict[str, Any], field: str, errors: list[str]) -> None:
    if "param2" not in item:
        return
    value = item["param2"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 255:
        errors.append(f"{field} must be an integer between 0 and 255")


def _validate_libreoffice_target(target: dict[str, Any], errors: list[str]) -> None:
    kind = target.get("kind")
    if kind not in {"writer_document", "calc_spreadsheet", "impress_presentation"}:
        errors.append("target.kind must be one of ['calc_spreadsheet', 'impress_presentation', 'writer_document']")
        return
    if "path" in target and (not isinstance(target.get("path"), str) or not target.get("path")):
        errors.append("target.path must be a non-empty string when present")
    if kind == "writer_document":
        if "text" in target and not isinstance(target["text"], str):
            errors.append("target.text must be a string when present")
        paragraphs = target.get("paragraphs")
        if paragraphs is not None:
            if not isinstance(paragraphs, list):
                errors.append("target.paragraphs must be a list when present")
            else:
                for index, paragraph in enumerate(paragraphs):
                    if not isinstance(paragraph, dict):
                        errors.append(f"target.paragraphs[{index}] must be a mapping")
                        continue
                    if not isinstance(paragraph.get("text"), str):
                        errors.append(f"target.paragraphs[{index}].text must be a string")
                    if "style" in paragraph and (not isinstance(paragraph.get("style"), str) or not paragraph.get("style")):
                        errors.append(f"target.paragraphs[{index}].style must be a non-empty string")
        tables = target.get("tables")
        if tables is not None:
            if not isinstance(tables, dict):
                errors.append("target.tables must be a mapping when present")
            else:
                for table_name, rows in tables.items():
                    if not isinstance(table_name, str) or not table_name:
                        errors.append("target.tables keys must be non-empty strings")
                    if not isinstance(rows, list) or any(not isinstance(row, list) for row in rows):
                        errors.append(f"target.tables.{table_name} must be a list of rows")
    if kind == "calc_spreadsheet":
        if "sheet" in target and (not isinstance(target.get("sheet"), str) or not target.get("sheet")):
            errors.append("target.sheet must be a non-empty string when present")
        for key in ("cells", "formulas"):
            value = target.get(key)
            if value is not None and not isinstance(value, dict):
                errors.append(f"target.{key} must be a mapping when present")
        sheets = target.get("sheets")
        if sheets is not None:
            if not isinstance(sheets, dict):
                errors.append("target.sheets must be a mapping when present")
            else:
                for sheet_name, sheet_data in sheets.items():
                    if not isinstance(sheet_name, str) or not sheet_name:
                        errors.append("target.sheets keys must be non-empty strings")
                    if not isinstance(sheet_data, dict):
                        errors.append(f"target.sheets.{sheet_name} must be a mapping")
                        continue
                    for key in ("cells", "formulas"):
                        value = sheet_data.get(key)
                        if value is not None and not isinstance(value, dict):
                            errors.append(f"target.sheets.{sheet_name}.{key} must be a mapping when present")
    if kind == "impress_presentation":
        slides = target.get("slides")
        if not isinstance(slides, list) or not slides:
            errors.append("target.slides must be a non-empty list for impress_presentation")
        else:
            for index, slide in enumerate(slides):
                if not isinstance(slide, dict):
                    errors.append(f"target.slides[{index}] must be a mapping")
                    continue
                for key in ("title", "subtitle"):
                    if key in slide and not isinstance(slide[key], str):
                        errors.append(f"target.slides[{index}].{key} must be a string when present")
                bullets = slide.get("bullets")
                if bullets is not None and (not isinstance(bullets, list) or any(not isinstance(item, str) for item in bullets)):
                    errors.append(f"target.slides[{index}].bullets must be a list of strings when present")


def _validate_replay_from_start(value: Any, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append("replay_from_start must be a mapping when present")
        return
    actions = value.get("actions", [])
    if not isinstance(actions, list):
        errors.append("replay_from_start.actions must be a list when present")
    else:
        for index, action in enumerate(actions):
            _validate_action(action, index, errors)
    random_values = value.get("random_values")
    if random_values is not None:
        if not isinstance(random_values, list) or any(not isinstance(item, int | float) for item in random_values):
            errors.append("replay_from_start.random_values must be a list of numbers when present")


def _validate_visual_oracle(value: Any, spec_path: Path, required: bool, errors: list[str]) -> None:
    if value is None:
        if required:
            errors.append("visual_oracle is required")
        return
    if not isinstance(value, dict):
        errors.append("visual_oracle must be a mapping")
        return
    golden = value.get("golden_image")
    if not isinstance(golden, str) or not golden:
        errors.append("visual_oracle.golden_image must be a non-empty string")
    else:
        golden_path = Path(golden)
        if not golden_path.is_absolute():
            golden_path = spec_path.parent / golden_path
        if not golden_path.exists():
            errors.append(f"visual_oracle.golden_image does not exist: {golden_path}")
    for pair_key in ("golden_before", "golden_after"):
        pair_value = value.get(pair_key)
        if pair_value is None:
            continue
        if not isinstance(pair_value, str) or not pair_value:
            errors.append(f"visual_oracle.{pair_key} must be a non-empty string when present")
    for desc_key in ("before_description", "after_description"):
        if desc_key in value and (not isinstance(value.get(desc_key), str) or not value.get(desc_key)):
            errors.append(f"visual_oracle.{desc_key} must be a non-empty string when present")
    label = value.get("expected_label")
    if label not in VALID_VISUAL_LABELS:
        errors.append(f"visual_oracle.expected_label must be one of {sorted(VALID_VISUAL_LABELS)}")
    for rubric_key in ("rubric", "before_rubric", "after_rubric"):
        rubric = value.get(rubric_key)
        if rubric is None:
            continue
        if not isinstance(rubric, dict):
            errors.append(f"visual_oracle.{rubric_key} must be a mapping when present")
        else:
            for key in ("must", "must_not", "ignore"):
                if key in rubric and (not isinstance(rubric[key], list) or any(not isinstance(item, str) for item in rubric[key])):
                    errors.append(f"visual_oracle.{rubric_key}.{key} must be a list of strings")
    capture = value.get("capture")
    if capture is not None:
        if not isinstance(capture, dict):
            errors.append("visual_oracle.capture must be a mapping when present")
        elif "wait_ms" in capture:
            _non_negative_int(capture["wait_ms"], "visual_oracle.capture.wait_ms", errors)


def _non_negative_int(value: Any, field: str, errors: list[str], allow_zero: bool = True) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        errors.append(f"{field} must be a {qualifier} integer")
