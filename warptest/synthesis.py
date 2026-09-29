from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import os
import random
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml

from . import cost, run_context
from .c1_provider_runtime import is_temperature_rejection as _is_temperature_rejection
from .discovery import RepositoryInspector
from .models import CheckpointValidity
from .model_bench import DEFAULT_MODELS, DEFAULT_MODELSCOPE_BASE_URL
from .model_request import (
    REASONING_HIGH,
    canonical_model_name,
    resolve_model_request_profile,
)
from .c1_task_deadline import (
    bounded_task_timeout_s,
    current_task_deadline,
    raise_task_timeout_if_expired,
)
from .repo_tools import (
    LAZARUS_IGNORED_DIRS,
    LAZARUS_SOURCE_TEXT_SUFFIXES,
    RepoSandbox,
)
from .reports import write_json
from .source_graph import build_source_state_graph, locate_issue_evidence
from .lazarus_luanti_start import (
    LUANTI_VECTOR_START,
    LuantiPhase0Start,
    luanti_start_for_task_id,
)
from .spec_schema import BLENDER_PUBLIC_ACTION_CONTRACT, validate_spec_schema
from .task_schema import MethodCheckpoint, PublicIssue, resolve_task_repo, validate_method_checkpoint
from .synthesis_ablation import (
    ABLATION_REVISION, ablation_scope, policy_instruction, public_contract_view, scoped_agent_policy,
    scoped_output_policy,
    validate_variant,
)


DEFAULT_SYNTHESIS_MODEL = DEFAULT_MODELS[0]
ALLOW_INCOMPLETE_USAGE_ENV = "WARPTEST_ALLOW_INCOMPLETE_SYNTHESIS_USAGE"

# This gateway exposes reliable usage for these routes only on non-streaming
# chat completions. Gemini streaming can report an inconsistent total, while
# GPT-5.5/5.6 Terra can omit the usage trailer entirely under bursty concurrent
# traffic.
# Formal accounting must prevent those cases rather than repair or estimate them.
_NON_STREAMING_USAGE_MODELS = frozenset(
    {"gemini-3.5-flash", "gpt-5.5", "gpt-5.6-terra"}
)


@contextmanager
def _wall_clock_timeout(seconds: float, label: str):
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _raise_timeout(_signum: int, _frame: Any) -> None:
        raise TimeoutError(f"{label} timed out after {seconds:.0f}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


# Public, source-derived capability vocabulary surfaced to the synthesis method.
# These identifiers are public facts present in each game's source (system/station
# ids, store action names, key bindings, save slots). They are NOT the hidden
# oracle: the method must still decide which identifiers a given issue needs and
# assemble a coherent checkpoint. Scenario specifics (exact boards, seeds,
# relationship patches, story flags, grader assertion thresholds) are withheld.
SOURCE_VOCABULARY: dict[str, dict[str, Any]] = {
    "2048": {
        "checkpoint_strategies": ["localStorage_restore_reload"],
        "move_directions": ["left", "right", "up", "down"],
        "note": "target.board is the row-major 4x4 matrix BEFORE the move; pick the board that matches the issue.",
    },
    "SpaceRPG": {
        "checkpoint_strategies": ["test_hook_jump_and_validate"],
        "hook": "__SPACERPG_E2E__",
        "store_actions": [
            "setScreen", "jumpToSystem", "saveGame", "loadGame", "sell", "buy",
            "installEquipmentFromInventory", "applyDebugScenario",
        ],
        "screens": ["flight", "station", "galaxyMap"],
        "system_ids": ["helion-reach", "kuro-belt", "mirr-vale"],
        "station_ids": ["helion-prime"],
        "commodity_ids": ["basic-food"],
        "equipment_ids": ["cargo-expansion"],
        "save_slots": ["auto", "manual-1"],
        "throttle_key": "W",
        "helion_gate_position": [1280, 140, -1240],
        "note": "Display names map to ids: 'Helion Reach'=helion-reach, 'Helion Prime'=helion-prime, 'Kuro Belt'=kuro-belt, 'Mirr Vale'=mirr-vale, 'Basic Food'=basic-food.",
    },
    "DatingSim": {
        "checkpoint_strategies": ["test_hook_seeded_state"],
        "ai_mode": "mocked",
        "action_ids": ["work_coding", "date_coffee"],
        "hook_methods": ["startDialogue", "applyRelationshipUpdates", "save"],
        "character_ids": ["char_1"],
        "locations": ["home", "cafe", "restaurant"],
        "note": "Use deterministic_state with player_name WarpTest, gender male; seed and patch values are scenario-specific.",
    },
    "jynew": {
        "checkpoint_strategies": ["jynew_utility_save_validate"],
        "utility_command": "--warptest-checkpoint",
        "mod_ids": ["JYX2", "xiastart_roguelike"],
        "role_ids": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14],
        "role_attrs": ["Level", "Hp", "MaxHp", "Mp", "MaxMp", "Attack", "Defence", "Qinggong", "Tili", "Exp"],
        "action_types": [
            "jynew_join_team", "jynew_leave_team", "jynew_add_item", "jynew_use_item",
            "jynew_learn_skill", "jynew_level_up", "jynew_set_key_value", "jynew_save",
            "jynew_load_save", "jynew_rest", "jynew_enter_submap", "jynew_add_money",
        ],
        "assertion_types": [
            "jynew_role_attr", "jynew_team_contains", "jynew_team_count",
            "jynew_item_count", "jynew_money_gte", "jynew_money_equals", "jynew_skill_learned",
            "jynew_key_value_equals", "jynew_event_flag", "jynew_map_id",
            "no_jynew_utility_errors",
        ],
        "note": "GameRuntimeData is the save root serialized via ES3. Roles keyed by int id (player=0). Use Unity batch mode for checkpoint validation.",
    },
    "pal3": {
        "checkpoint_strategies": ["pal3_console_save_validate"],
        "utility_command": "--warptest-checkpoint",
        "variant": "PAL3",
        "main_story_variable_id": 32768,
        "player_actor_ids": {"JingTian": 0, "XueJian": 1, "LongKui": 2, "ZiXuan": 3, "ChangQing": 4, "HuaYing": 5},
        "scene_examples": [{"city": "q01", "scene": "b"}, {"city": "q02", "scene": "q02"}],
        "action_types": [
            "pal3_set_story_var", "pal3_load_scene", "pal3_add_team_member", "pal3_remove_team_member",
            "pal3_add_money", "pal3_add_item", "pal3_enable_world_region", "pal3_set_favor",
            "pal3_save", "pal3_load_save",
        ],
        "assertion_types": [
            "pal3_story_var_equals", "pal3_scene_is", "pal3_team_contains", "pal3_team_count",
            "pal3_item_count", "pal3_money_gte", "pal3_world_region_enabled", "pal3_favor_gte",
            "no_pal3_utility_errors",
        ],
        "note": (
            "State lives in ServiceLocator managers; checkpoints are synthesized/restored via the game's own "
            "console commands (ScriptVarSetValue/SceneLoad/TeamAddOrRemoveActor/InventoryAddMoney/InventoryAddItem/"
            "WorldMapEnableRegion/FavorAdd), the same text format SaveManager writes. Main story progress variable id is 32768. "
            "Requires Unity play mode and the original PAL3 data."
        ),
    },
    "luanti": {
        "checkpoint_strategies": ["luanti_worldmod_state_validate"],
        "worldmod": "warptest",
        "gameid": "devtest",
        "player_name": "warptest",
        "node_ids": [
            "air", "basenodes:stone", "basenodes:dirt", "basenodes:cobble",
            "basenodes:mossycobble", "basenodes:tree", "basenodes:leaves",
            "basenodes:junglegrass", "basenodes:apple", "basenodes:sand",
            "basenodes:gravel", "basenodes:desert_stone", "basenodes:water_source",
            "basenodes:dirt_with_grass", "stairs:stair_cobble", "stairs:slab_cobble",
            "stairs:slab_stone",
        ],
        "chest_node": "chest:chest",
        "chest_list": "main",
        "player_items": ["basetools:pick_steel", "basetools:pick_mese", "basenodes:cobble"],
        "action_types": [
            "luanti_set_node", "luanti_dig_node", "luanti_fill", "luanti_set_player_pos",
            "luanti_set_player_hp", "luanti_give_item", "luanti_set_player_inventory",
            "luanti_set_meta_inventory",
            "luanti_set_mod_storage", "luanti_wait_steps", "luanti_save_world",
        ],
        "assertion_types": [
            "luanti_node_is", "luanti_node_count", "luanti_player_pos_near", "luanti_player_hp_gte",
            "luanti_inventory_contains", "luanti_meta_inventory_contains", "luanti_mod_storage_equals",
            "luanti_gametime_gte", "no_luanti_mod_errors",
        ],
        "note": (
            "State is driven by a test-gated Lua worldmod (mods/warptest) inside an embedded "
            "single-player session: voxel nodes via core.set_node/get_node, the online player via "
            "ObjectRef (pos/hp/inventory list 'main'), node-meta inventories (chest:chest 'main' "
            "list), and mod storage. Positions are integer {x,y,z}; optional param2 orients stairs/slabs. Use devtest node ids "
            "(basenodes:*, stairs:*, chest:chest). "
            "target.kind synthesized_world (injection) or save_restore (persisted via the engine world)."
        ),
    },
    "OpenRA": {
        "checkpoint_strategies": [
            "openra_utility_map_validate",
            "openra_gameplay_production_validate",
            "openra_public_settings_restore",
        ],
        "utility_command": "--warptest-checkpoint",
        "gameplay_probe": {
            "setup_key": "openra_gameplay_probe",
            "request_hook": "Launch.WarptestGameplayRequestPath",
            "report_hook": "Launch.WarptestGameplayReportPath",
            "options": [
                "cash",
                "fast_build",
                "build_anywhere",
                "all_tech",
                "delay_ticks",
                "remove_player_actor_types",
            ],
        },
        "mods": ["cnc", "ra"],
        "maps": [
            "gdi01", "nod03a", "the-hot-box",
            "allies-03a", "soviet-05", "allies-05a", "allies-07", "allies-08a", "drop-zone",
        ],
        "player_ids": ["GDI", "Nod", "Greece", "USSR", "France", "Multi0", "Multi7", "Multi9", "Neutral"],
        "objective_ids": [
            "eliminate-nod", "establish-beachhead", "capture-prison", "destroy-gdi-forces",
            "destroy-bridges", "tanya-survive", "free-prisoners",
            "capture-radar-dome", "defeat-allied-forces", "defend-radar-dome",
        ],
        "actor_ids": [
            "e1", "e2", "jeep", "apc", "mpspawn", "gun", "2tnk",
            "startmcv", "fact", "powr", "proc", "weap", "fix", "barr", "3tnk",
        ],
        "named_actor_ids": ["USSRWarFactory", "Warfactory2"],
        "production_queues": ["Building", "Vehicle", "Infantry"],
        "gameplay_actions": [
            "openra_wait_ticks", "openra_deploy_actor", "openra_build_actor",
            "openra_build_group", "openra_rally_group",
        ],
        "gameplay_assertions": [
            "openra_actor_built",
            "openra_actor_group_in_region",
            "openra_actor_exists",
            "openra_production_prerequisite_respected",
            "no_openra_gameplay_probe_errors",
        ],
        "settings_target": {
            "kind": "openra_settings",
            "sections": {
                "Player": ["Name"],
                "Game": [
                    "ViewportEdgeScroll",
                    "TargetLines",
                    "StatusBars",
                    "IntroductionPromptVersion",
                ],
            },
            "action": "openra_set_setting",
            "assertion": "openra_setting_equals",
        },
        "note": (
            "Map-loading tasks use target.kind=openra_map with utility validation. "
            "Gameplay production tasks use the native gameplay probe. C1 Settings "
            "tasks use target.kind=openra_settings and the public isolated settings.yaml; "
            "Agent-S alone performs the UI transition."
        ),
    },
    "JupyterLab": {
        "checkpoint_strategies": ["jupyterlab_workspace_api_validate"],
        "server": "Jupyter Server with token/password disabled inside a temporary WarpTest root",
        "target_kinds": ["notebook", "text_file", "workspace_layout", "terminal_session"],
        "action_types": [
            "jupyterlab_create_text_file",
            "jupyterlab_open_file",
            "jupyterlab_edit_text_file",
            "jupyterlab_create_notebook",
            "jupyterlab_edit_notebook_cell",
            "jupyterlab_run_notebook_cell",
            "jupyterlab_run_notebook",
            "jupyterlab_export_notebook_html",
            "jupyterlab_set_workspace_layout",
            "jupyterlab_start_session",
            "wait",
        ],
        "assertion_types": [
            "jupyterlab_file_exists",
            "jupyterlab_file_text_equals",
            "jupyterlab_notebook_output_contains",
            "jupyterlab_notebook_cell_source_equals",
            "jupyterlab_notebook_all_code_cells_executed",
            "jupyterlab_notebook_has_mime",
            "jupyterlab_notebook_has_no_error_outputs",
            "jupyterlab_workspace_contains",
            "jupyterlab_session_started",
            "no_jupyterlab_server_errors",
        ],
        "note": "Use public Jupyter Server Contents, Sessions/kernel, workspace, and Galata-compatible browser surfaces. Do not modify the JupyterLab source checkout.",
    },
    "LibreOffice": {
        "checkpoint_strategies": ["libreoffice_uno_document_validate"],
        "runtime": "Isolated LibreOffice process driven through public UNO APIs with ODF/PDF artifacts and optional before/after desktop screenshots under the report directory",
        "target_kinds": ["writer_document", "calc_spreadsheet", "impress_presentation"],
        "writer_styles": ["Standard", "Heading 1", "Heading 2"],
        "writer_structures": ["paragraphs", "text tables", "bookmarks", "contents/reference text"],
        "calc_structures": ["sheets", "cells", "formulas", "named ranges", "charts"],
        "impress_structures": ["slides", "titles", "bullets", "text shapes"],
        "action_types": [
            "libreoffice_open_document",
            "libreoffice_replace_text",
            "libreoffice_append_paragraph",
            "libreoffice_insert_toc",
            "libreoffice_update_indexes",
            "libreoffice_apply_paragraph_style",
            "libreoffice_insert_bookmark",
            "libreoffice_insert_reference_field",
            "libreoffice_set_table_cell",
            "libreoffice_insert_table_row",
            "libreoffice_append_table_row",
            "libreoffice_save",
            "libreoffice_export_pdf",
            "libreoffice_set_cell",
            "libreoffice_set_formula",
            "libreoffice_recalculate",
            "libreoffice_sort_range",
            "libreoffice_define_named_range",
            "libreoffice_create_chart",
            "libreoffice_set_slide_title",
            "libreoffice_set_slide_bullets",
            "libreoffice_add_shape",
            "libreoffice_move_slide",
            "libreoffice_reopen",
        ],
        "assertion_types": [
            "libreoffice_text_contains",
            "libreoffice_toc_contains",
            "libreoffice_paragraph_style_is",
            "libreoffice_table_cell_equals",
            "libreoffice_bookmark_exists",
            "libreoffice_reference_text_contains",
            "libreoffice_pdf_exists",
            "libreoffice_sheet_exists",
            "libreoffice_cell_value_equals",
            "libreoffice_formula_equals",
            "libreoffice_named_range_exists",
            "libreoffice_chart_exists",
            "libreoffice_slide_count_equals",
            "libreoffice_slide_title_equals",
            "libreoffice_slide_text_contains",
            "libreoffice_shape_text_contains",
            "no_libreoffice_errors",
        ],
        "note": "Use public UNO/ODF/PDF document semantics for Writer, Calc, and Impress. Do not modify games/LibreOffice, add private hooks, or use GUI click replay.",
    },
    "Blender": {
        "checkpoint_strategies": ["blender_python_scene_validate"],
        "runtime": "Visible Blender GUI app bundle driven by bpy/operator Python APIs",
        "target_kinds": ["scene", "blend_file"],
        "primitives": ["cube", "uv_sphere", "cylinder", "cone"],
        "modifier_types": ["BEVEL", "SUBSURF", "SOLIDIFY", "MIRROR", "ARRAY"],
        "action_types": [
            "blender_add_primitive",
            "blender_assign_material",
            "blender_set_object_location",
            "blender_set_object_scale",
            "blender_set_object_rotation",
            "blender_set_frame",
            "blender_add_modifier",
            "blender_apply_modifier",
            "blender_set_keyframe",
            "blender_save_as",
            "blender_reopen",
            "wait",
        ],
        "assertion_types": [
            "blender_object_exists",
            "blender_material_color",
            "blender_object_location",
            "blender_object_scale",
            "blender_object_rotation",
            "blender_current_frame_equals",
            "blender_file_exists",
            "blender_modifier_exists",
            "blender_modifier_applied",
            "blender_mesh_geometry",
            "blender_animation_keyframes",
            "no_blender_python_errors",
        ],
        "note": "Use a visible Blender GUI process plus official bpy/operator APIs. Do not use --background/headless mode, pixel clicks, keyboard GUI replay, or modify games/blender source.",
    },
}


# The original OpenRA C1 settings tasks deliberately use a narrower public
# state-construction surface than the older core-synthesis tasks.  The C1
# deep-chain expansion is explicitly gameplay-backed, so it must retain the
# public map/gameplay vocabulary instead of being coerced into settings.yaml.
C1_SOURCE_VOCABULARY: dict[str, dict[str, Any]] = {
    "OpenRA": {
        "checkpoint_strategies": ["openra_public_settings_restore"],
        "target_kind": "openra_settings",
        "mod": "ra",
        "settings": {
            "Player": ["Name"],
            "Game": [
                "ViewportEdgeScroll",
                "TargetLines",
                "StatusBars",
                "IntroductionPromptVersion",
            ],
        },
        "action_types": ["openra_set_setting"],
        "assertion_types": ["openra_setting_equals", "no_openra_errors"],
        "replay_action_types": ["openra_open_settings_panel"],
        "note": (
            "C1 restores only an isolated public settings.yaml start and uses the "
            "normal foreground OpenRA shell. Do not emit openra_map, map validation, "
            "gameplay probes, production actions, or gameplay assertions."
        ),
    },
}


def _legacy_benchmark_key(value: Any) -> str:
    """Map a canonical v5 id to dictionaries retained for historical tasks."""

    raw = str(value or "")
    return {
        "datingsim": "DatingSim",
        "spacerpg": "SpaceRPG",
        "openra": "OpenRA",
        "jupyterlab": "JupyterLab",
        "libreoffice": "LibreOffice",
        "blender": "Blender",
        # Alias spellings of the same registered benchmark. Without these a
        # ``pal3`` spec written for a ``pal3.unity`` task loses its contract.
        "pal3.unity": "pal3",
        "pal3unity": "pal3",
        "minetest": "luanti",
    }.get(raw.lower(), raw)


def _is_c1_synthesis_task(task: PublicIssue) -> bool:
    tags = getattr(task, "tags", [])
    if not isinstance(tags, (list, tuple, set)):
        return False
    return "c1" in {str(tag).strip().lower() for tag in tags}


def _is_lazarus_synthesis_task(task: PublicIssue) -> bool:
    tags = getattr(task, "tags", [])
    if not isinstance(tags, (list, tuple, set)):
        return False
    normalized = {str(tag).strip().lower() for tag in tags}
    return {"c1", "lazarus"} <= normalized


def _is_c1_openra_gameplay_task(task: PublicIssue) -> bool:
    tags = getattr(task, "tags", [])
    if not isinstance(tags, (list, tuple, set)):
        return False
    normalized = {str(tag).strip().lower() for tag in tags}
    return (
        str(task.benchmark).strip().lower() == "openra"
        and "c1" in normalized
        and "deep-chain" in normalized
    )


_LAZARUS_OPENRA_HOTKEY_TASK_ID = "lz-openra-21453-hotkey-ready-building"
_LAZARUS_LUANTI_VECTOR_TASK_ID = LUANTI_VECTOR_START.task_id


def _is_lazarus_openra_hotkey_task(task: PublicIssue) -> bool:
    return (
        task.id == _LAZARUS_OPENRA_HOTKEY_TASK_ID
        and str(task.benchmark).strip().lower() == "openra"
        and _is_lazarus_synthesis_task(task)
    )


def _lazarus_luanti_start(task: PublicIssue) -> LuantiPhase0Start | None:
    """Return the registered Luanti Lazarus Start this task synthesizes, if any."""

    if (
        str(task.benchmark).strip().lower() != "luanti"
        or not _is_lazarus_synthesis_task(task)
    ):
        return None
    return luanti_start_for_task_id(str(task.id))


def _is_lazarus_luanti_vector_task(task: PublicIssue) -> bool:
    return _lazarus_luanti_start(task) is not None


def _source_vocabulary_for_task(task: PublicIssue) -> dict[str, Any]:
    if getattr(task, "prompt_revision", "") == "lzp-generated-state-v3":
        return {}

    benchmark_key = _legacy_benchmark_key(task.benchmark)
    luanti_start = _lazarus_luanti_start(task)
    if luanti_start is not None:
        return luanti_start.source_vocabulary()
    if _is_lazarus_openra_hotkey_task(task):
        return {
            "checkpoint_strategies": ["openra_gameplay_production_validate"],
            "mods": ["ra"],
            "maps": ["allies-08a"],
            "gameplay_actions": ["openra_build_actor"],
            "production_queues": ["Building"],
            "gameplay_assertions": ["no_openra_gameplay_probe_errors"],
            "note": (
                "This public task restores one completed-structure Buildings "
                "placement Start; generic gameplay setup fields are not used."
            ),
        }
    if (
        _is_c1_synthesis_task(task)
        and not _is_c1_openra_gameplay_task(task)
        and benchmark_key in C1_SOURCE_VOCABULARY
    ):
        return copy.deepcopy(C1_SOURCE_VOCABULARY[benchmark_key])
    return copy.deepcopy(SOURCE_VOCABULARY.get(benchmark_key, {}))


class SynthesisError(RuntimeError):
    def __init__(self, message: str, *, artifacts: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.artifacts = artifacts or {}
        self.usage_accounting_complete = self.artifacts.get(
            "usage_accounting_complete", True
        )


class SynthesisAgent(Protocol):
    name: str

    def synthesize(
        self,
        task: PublicIssue,
        root: str | Path = Path.cwd(),
        artifact_dir: Path | None = None,
        *,
        feedback: dict[str, Any] | None = None,
    ) -> MethodCheckpoint:
        ...


@dataclass
class ModelCallResult:
    content: str
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int | None = 0
    cached_prompt_tokens: int = 0
    total_tokens: int = 0
    # Provider-private thinking is kept only long enough to satisfy APIs such as
    # Kimi's preserved-thinking multi-turn contract.  Callers must echo it in the
    # next assistant message, but must never parse, log, or serialize it.
    reasoning_content: str | None = field(default=None, repr=False, compare=False)


def _is_reasoning_effort_rejection(error: Exception) -> bool:
    """True when a chat-completion error is the model rejecting ``reasoning_effort``.

    Non-reasoning models (and some gateway routes) reject an unknown
    ``reasoning_effort`` parameter with an HTTP 400; when that happens we drop it for
    the rest of the run instead of paying the failed round-trip on every call.
    """
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if isinstance(status, int) and status != 400:
        return False
    text = (str(getattr(error, "message", "") or "") + " " + str(error)).lower()
    return "reasoning_effort" in text or "reasoning effort" in text


class OpenAICompatibleChatClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_MODELSCOPE_BASE_URL,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        extra_headers: dict[str, str] | None = None,
        reasoning_effort: str | None = None,
        cost_kind: str = "chat",
        client: Any | None = None,
        minimum_max_tokens: int = 8192,
        reasoning_retry_max_tokens: int | None = 16384,
    ) -> None:
        if not api_key:
            raise SynthesisError("API key is required for LLM checkpoint synthesis.")
        if client is None:
            from openai import OpenAI

            # Reasoning-style models on the ModelScope/DashScope gateway can take a
            # long time to first token; give a generous read timeout. Keep the SDK's
            # own retry count low: the gateway oscillates between healthy windows and
            # short outages, and aggressive rapid reconnects trip rate limiting that
            # snowballs into connection errors. The patient outer loop in `converse`
            # handles outages by waiting them out gently instead.
            timeout = float(os.environ.get("WARPTEST_SYNTHESIS_TIMEOUT", "300"))
            # The patient outer loop records every request/unknown timeout.  An
            # SDK retry would create an unobservable paid attempt and invalidate
            # formal token accounting.
            max_retries = int(os.environ.get("WARPTEST_SYNTHESIS_MAX_RETRIES", "0"))
            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "base_url": base_url,
                "timeout": timeout,
                "max_retries": max_retries,
            }
            if extra_headers:
                kwargs["default_headers"] = dict(extra_headers)
            client = OpenAI(**kwargs)
        else:
            timeout = float(os.environ.get("WARPTEST_SYNTHESIS_TIMEOUT", "300"))
        self.client = client
        self.base_url = base_url
        self.temperature = temperature
        if isinstance(minimum_max_tokens, bool) or int(minimum_max_tokens) < 1:
            raise ValueError("minimum_max_tokens must be a positive integer")
        self.max_tokens = max(int(minimum_max_tokens), int(max_tokens))
        self.reasoning_retry_max_tokens = (
            None
            if reasoning_retry_max_tokens is None
            else max(self.max_tokens, int(reasoning_retry_max_tokens))
        )
        self.timeout_s = timeout
        # Reasoning effort ("minimal"/"low"/"medium"/"high") for reasoning models; None
        # means "do not send the parameter" (use the gateway/model default). Sent as a
        # top-level chat-completions field, mirroring OpenAI's reasoning models.
        self.reasoning_effort = reasoning_effort or None
        self.cost_kind = str(cost_kind or "chat")
        self._last_request_profile: dict[str, Any] | None = None
        # Some newer models (e.g. certain Claude/OpenAI reasoning models on the
        # gateway) reject a non-default ``temperature`` with an HTTP 400. Once we see
        # that, drop the parameter for the rest of the run so the model is actually
        # exercised instead of falling back to the scaffold with zero exploration.
        self._omit_temperature = False
        # Non-reasoning models reject ``reasoning_effort`` with an HTTP 400; drop it
        # for the rest of the run once we see that, same as temperature above.
        self._omit_reasoning_effort = False

    def _task_bounded_client(self) -> Any:
        timeout_s = bounded_task_timeout_s(self.timeout_s)
        return (
            self.client.with_options(timeout=timeout_s)
            if current_task_deadline() is not None
            and hasattr(self.client, "with_options")
            else self.client
        )

    def _create_chat_completion(self, **kwargs: Any) -> Any:
        # Keep transport exceptions as ordinary Exceptions until ``converse`` has
        # persisted the provider-attempt ledger.  Translating an ambiguous timeout
        # here would bypass that accounting boundary because C1TaskTimeout derives
        # from BaseException.
        return self._task_bounded_client().chat.completions.create(**kwargs)

    def generate(self, model: str, prompt: str, *, max_attempts: int | None = None) -> ModelCallResult:
        messages = [
            {
                "role": "system",
                "content": "You generate WarpTest method checkpoints. Return only one YAML mapping.",
            },
            {"role": "user", "content": prompt},
        ]
        # `max_attempts` is accepted for backward compatibility (e.g. the evo repair
        # path). It maps to the empty-payload retry count; gateway-outage retries are
        # governed by converse's patient backoff regardless.
        if max_attempts is not None:
            return self.converse(model, messages, empty_retries=max(1, max_attempts))
        return self.converse(model, messages)

    def converse(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        empty_retries: int = 1,
        outage_budget_s: float = 1800.0,
    ) -> ModelCallResult:
        """Run one multi-turn chat completion and return the accumulated content.

        Shares the provider-adaptive completion behavior with :meth:`generate` so
        the exploration ReAct loop and the single-shot path behave identically.
        Most models stream; routes whose streaming usage is known to be
        inconsistent use a registered non-streaming completion instead.

        Two distinct retry regimes, because they have different causes:

        * Gateway *outages* (connection errors / rate limiting / timeouts raise an
          exception). The ModelScope gateway oscillates between healthy windows and
          minutes-long outages; hammering it during an outage trips rate limiting
          that snowballs. We therefore wait the outage out *gently* -- a long,
          jittered sleep (~30-60s) between single attempts for up to
          ``outage_budget_s`` -- so the call naturally blocks until the gateway
          heals and then returns real content, instead of silently degrading the
          task to a scaffold fallback (which would deflate strict scores with
          infrastructure noise).
        * Empty payloads with *no* exception are a model outcome.  The default
          formal path makes no same-budget retry; only a quantified
          reasoning/length exhaustion receives the single registered 16k retry.
          Legacy callers may explicitly raise ``empty_retries``.
        """

        start = time.perf_counter()
        outage_budget_s = float(os.environ.get("WARPTEST_SYNTHESIS_OUTAGE_BUDGET", str(outage_budget_s)))
        last_error = "LLM returned empty content."
        failure_mode = "empty_content"
        empty_seen = 0
        outage_attempt = 0
        reasoning_retry_used = False
        response_max_tokens = self.max_tokens
        while True:
            attempt_requested_at = datetime.now(timezone.utc).isoformat()
            attempt_start = time.perf_counter()
            had_exception = False
            request_error: Exception | None = None
            try:
                content = self._completion_content(
                    model,
                    messages,
                    max_tokens=response_max_tokens,
                )
            except Exception as error:  # transient gateway outage (connection/rate/timeout)
                had_exception = True
                request_error = error
                last_error = run_context.safe_error_text(str(error), limit=2000)
                content = ""
            usage = getattr(self, "_last_usage", None)
            finish_reason = getattr(self, "_last_finish_reason", None)
            reasoning_observed = bool(
                getattr(self, "_last_reasoning_observed", False)
            )
            reasoning_content = getattr(self, "_last_reasoning_content", None)
            provider_reported_model = getattr(
                self, "_last_provider_reported_model", None
            )
            provider_response_id = getattr(self, "_last_provider_response_id", None)
            system_fingerprint = getattr(self, "_last_system_fingerprint", None)
            if usage is None and request_error is not None:
                usage = cost.extract_usage(request_error)
            try:
                if had_exception and usage is None and request_error is not None:
                    recorded_error_usage = cost.record_error_usage_call(
                        model,
                        error=request_error,
                        latency_ms=int(
                            (time.perf_counter() - attempt_start) * 1000
                        ),
                        kind=self.cost_kind,
                        messages=messages,
                        requested_at=attempt_requested_at,
                    )
                    usage_data = recorded_error_usage or cost.usage_tokens(None)
                else:
                    usage_data = cost.record_usage_call(
                        model,
                        usage=usage,
                        content=content,
                        finish_reason=finish_reason,
                        reasoning_observed=reasoning_observed,
                        reasoning_expected=(
                            isinstance(self._last_request_profile, dict)
                            and self._last_request_profile.get("profile")
                            in {"reasoning_medium", "reasoning_high"}
                        ),
                        reasoning_disabled=(
                            isinstance(self._last_request_profile, dict)
                            and self._last_request_profile.get("profile")
                            == "thinking_disabled"
                        ),
                        latency_ms=int((time.perf_counter() - attempt_start) * 1000),
                        kind=self.cost_kind,
                        messages=messages,
                        response=content if isinstance(content, str) else None,
                        requested_at=attempt_requested_at,
                        endpoint="chat.completions",
                        provider_reported_model=provider_reported_model,
                        provider_response_id=provider_response_id,
                        system_fingerprint=system_fingerprint,
                    )
            except cost.UsageAccountingError as error:
                if (
                    os.environ.get(ALLOW_INCOMPLETE_USAGE_ENV, "").lower()
                    in {"1", "true", "yes", "on"}
                    and isinstance(content, str)
                    and content.strip()
                ):
                    usage_data = cost.usage_tokens(usage)
                    print(
                        f"[llm] {model}: continuing with incomplete usage accounting "
                        f"for {self.cost_kind}: {error}",
                        flush=True,
                    )
                else:
                    raise SynthesisError(
                        str(error),
                        artifacts={
                            "failure_mode": "usage_accounting_incomplete",
                            "usage_accounting_complete": False,
                        },
                    ) from error
            if request_error is not None:
                # The attempt is now durably represented in the active meter (or
                # was proven to be a definite pre-generation failure), so it is
                # safe to unwind the task deadline without losing paid-call
                # provenance.
                raise_task_timeout_if_expired(request_error)
            if request_error is not None:
                capacity = run_context.provider_capacity_failure(request_error)
                if capacity is not None:
                    raise SynthesisError(
                        "LLM request exceeds the provider's per-request TPM ceiling",
                        artifacts={
                            "failure_mode": "provider_capacity",
                            "status_code": 429,
                            "provider_capacity": capacity,
                        },
                    ) from request_error
                # Non-retryable client errors (bad request/auth/not-found, e.g. an
                # unsupported parameter) will never heal by waiting. Usage, when
                # present, has already been recorded above before failing fast.
                status = getattr(request_error, "status_code", None)
                if (
                    isinstance(status, int)
                    and 400 <= status < 500
                    and status not in (408, 429)
                ):
                    raise SynthesisError(
                        f"LLM request rejected (HTTP {status}), not retrying: {last_error}",
                        artifacts={
                            "failure_mode": "client_error",
                            "status_code": status,
                        },
                    ) from request_error
            # Every completed provider response is billable even if it contains
            # reasoning only and must be retried for an empty answer. A streaming
            # exception is recorded only when the gateway supplied usage before it
            # failed, avoiding fabricated zero-token records for connection errors.
            if isinstance(content, str) and content.strip():
                latency_ms = int((time.perf_counter() - start) * 1000)
                return ModelCallResult(
                    content=content,
                    latency_ms=latency_ms,
                    reasoning_content=(
                        reasoning_content
                        if isinstance(reasoning_content, str)
                        else None
                    ),
                    **usage_data,
                )
            if had_exception:
                if time.perf_counter() - start > outage_budget_s:
                    failure_mode = "gateway_outage"
                    break
                outage_attempt += 1
                wait = min(60.0, 20.0 + 8.0 * outage_attempt) + random.uniform(0.0, 5.0)
                print(
                    f"[llm] {model}: gateway error (attempt {outage_attempt}), "
                    f"waiting {wait:.0f}s to ride it out ({last_error})",
                    flush=True,
                )
                time.sleep(wait)
                continue
            if (
                not reasoning_retry_used
                and self.reasoning_retry_max_tokens is not None
                and response_max_tokens < self.reasoning_retry_max_tokens
                and str(finish_reason or "").lower()
                in {"length", "max_tokens"}
            ):
                reasoning_retry_used = True
                response_max_tokens = self.reasoning_retry_max_tokens
                empty_seen += 1
                continue
            if (
                self.reasoning_retry_max_tokens is not None
                and response_max_tokens >= self.reasoning_retry_max_tokens
            ):
                # The registered 16k reasoning retry is the final empty attempt;
                # never buy additional empty 16k responses.
                empty_seen += 1
                break
            empty_seen += 1
            if empty_seen >= empty_retries:
                break
            time.sleep(1.5 * empty_seen)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if failure_mode == "gateway_outage":
            raise SynthesisError(
                f"LLM gateway unavailable: exhausted outage budget ({outage_budget_s:.0f}s) "
                f"after {outage_attempt} retries: {last_error}",
                artifacts={
                    "latency_ms": latency_ms,
                    "failure_mode": "gateway_outage",
                    "outage_attempts": outage_attempt,
                },
            )
        raise SynthesisError(
            f"LLM returned empty content after retries: {last_error}",
            artifacts={
                "latency_ms": latency_ms,
                "failure_mode": "empty_content",
                "empty_attempts": empty_seen,
            },
        )

    def _completion_content(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> str:
        if canonical_model_name(model) in _NON_STREAMING_USAGE_MODELS:
            return self._non_streaming_content(
                model,
                messages,
                max_tokens=max_tokens,
            )
        return self._stream_content(model, messages, max_tokens=max_tokens)

    def _open_non_streaming_completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> Any:
        """Open one completion where the provider's usage is reliable only non-streamed."""

        request_profile = resolve_model_request_profile(model)
        self._last_request_profile = request_profile.provenance()

        def _create(with_temperature: bool, with_reasoning: bool) -> Any:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": int(max_tokens or self.max_tokens),
            }
            if with_temperature:
                kwargs["temperature"] = self.temperature
            if with_reasoning and self.reasoning_effort:
                kwargs["reasoning_effort"] = self.reasoning_effort
            kwargs.update(request_profile.request_kwargs())
            return self._create_chat_completion(**kwargs)

        for _ in range(3):
            with_temperature = not self._omit_temperature
            with_reasoning = (
                not self._omit_reasoning_effort and bool(self.reasoning_effort)
            )
            try:
                return _create(with_temperature, with_reasoning)
            except Exception as error:
                if with_temperature and _is_temperature_rejection(error):
                    self._omit_temperature = True
                    print(
                        f"[llm] {model}: model rejected 'temperature'; retrying "
                        "without it for the rest of the run.",
                        flush=True,
                    )
                    continue
                if (
                    with_reasoning
                    and request_profile.profile != REASONING_HIGH
                    and _is_reasoning_effort_rejection(error)
                ):
                    self._omit_reasoning_effort = True
                    print(
                        f"[llm] {model}: model rejected 'reasoning_effort'; retrying "
                        "without it for the rest of the run.",
                        flush=True,
                    )
                    continue
                raise
        return _create(
            not self._omit_temperature,
            not self._omit_reasoning_effort and bool(self.reasoning_effort),
        )

    def _non_streaming_content(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> str:
        """Read content and exact usage from one non-streaming completion."""

        self._last_usage = None
        self._last_finish_reason = None
        self._last_reasoning_observed = False
        self._last_reasoning_content = None
        self._last_provider_reported_model = None
        self._last_provider_response_id = None
        self._last_system_fingerprint = None
        response = self._open_non_streaming_completion(
            model,
            messages,
            max_tokens=max_tokens,
        )
        self._last_usage = getattr(response, "usage", None)
        self._last_reasoning_observed = cost.response_has_reasoning_content(response)
        self._last_provider_reported_model = getattr(response, "model", None)
        self._last_provider_response_id = getattr(response, "id", None)
        self._last_system_fingerprint = getattr(
            response, "system_fingerprint", None
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        choice = choices[0]
        self._last_finish_reason = getattr(choice, "finish_reason", None)
        message = getattr(choice, "message", None)
        try:
            reasoning_content = (
                getattr(message, "reasoning_content", None)
                if message is not None
                else None
            )
        except Exception:
            reasoning_content = None
        if isinstance(reasoning_content, str):
            # Retain the exact provider value in memory for the next request.  It
            # is deliberately excluded from the cost ledger and transcript.
            self._last_reasoning_content = reasoning_content
        value = getattr(message, "content", None) if message is not None else None
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                text = (
                    item.get("text")
                    if isinstance(item, dict)
                    else getattr(item, "text", None)
                )
                if isinstance(text, str):
                    parts.append(text)
            return "".join(parts)
        return "" if value is None else str(value)

    def _open_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> Any:
        """Open a streaming chat completion, tolerating gateway/model quirks.

        Falls back on independent axes: gateways that reject ``stream_options``
        (TypeError), models that reject a non-default ``temperature`` (HTTP 400), and
        models that reject ``reasoning_effort`` (HTTP 400). Each rejection is sticky for
        the rest of the run (``self._omit_temperature`` / ``self._omit_reasoning_effort``)
        so we do not pay the failed round-trip on every call.
        """

        request_profile = resolve_model_request_profile(model)
        self._last_request_profile = request_profile.provenance()

        def _create(include_usage: bool, with_temperature: bool, with_reasoning: bool) -> Any:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": int(max_tokens or self.max_tokens),
                "stream": True,
            }
            if with_temperature:
                kwargs["temperature"] = self.temperature
            if with_reasoning and self.reasoning_effort:
                kwargs["reasoning_effort"] = self.reasoning_effort
            if include_usage:
                kwargs["stream_options"] = {"include_usage": True}
            kwargs.update(request_profile.request_kwargs())
            return self._create_chat_completion(**kwargs)

        # Retry, dropping whichever optional parameter the model rejects (sticky), then
        # make one final attempt with the surviving parameters.
        for _ in range(3):
            with_temperature = not self._omit_temperature
            with_reasoning = not self._omit_reasoning_effort and bool(self.reasoning_effort)
            try:
                try:
                    return _create(True, with_temperature, with_reasoning)
                except TypeError:
                    return _create(False, with_temperature, with_reasoning)
            except Exception as error:
                if with_temperature and _is_temperature_rejection(error):
                    self._omit_temperature = True
                    print(
                        f"[llm] {model}: model rejected 'temperature'; retrying without it for the rest of the run.",
                        flush=True,
                    )
                    continue
                if (
                    with_reasoning
                    and request_profile.profile != REASONING_HIGH
                    and _is_reasoning_effort_rejection(error)
                ):
                    self._omit_reasoning_effort = True
                    print(
                        f"[llm] {model}: model rejected 'reasoning_effort'; retrying without it for the rest of the run.",
                        flush=True,
                    )
                    continue
                raise
        with_temperature = not self._omit_temperature
        with_reasoning = not self._omit_reasoning_effort and bool(self.reasoning_effort)
        try:
            return _create(True, with_temperature, with_reasoning)
        except TypeError:
            return _create(False, with_temperature, with_reasoning)

    def _stream_content(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> str:
        """Stream the chat completion and accumulate answer content.

        ``delta.content`` is the public answer and is the only value parsed by the
        harness.  ``delta.reasoning_content`` is accumulated separately and kept
        in memory only, so a later turn can echo it exactly for providers that
        require preserved thinking.
        """

        parts: list[str] = []
        reasoning_parts: list[str] = []
        usage = None
        self._last_usage = None
        self._last_finish_reason = None
        self._last_reasoning_observed = False
        self._last_reasoning_content = None
        self._last_provider_reported_model = None
        self._last_provider_response_id = None
        self._last_system_fingerprint = None
        try:
            with _wall_clock_timeout(self.timeout_s, f"LLM stream for {model}"):
                stream = self._open_stream(model, messages, max_tokens=max_tokens)
                for chunk in stream:
                    if self._last_provider_reported_model is None:
                        self._last_provider_reported_model = getattr(
                            chunk, "model", None
                        )
                    if self._last_provider_response_id is None:
                        self._last_provider_response_id = getattr(chunk, "id", None)
                    if self._last_system_fingerprint is None:
                        self._last_system_fingerprint = getattr(
                            chunk, "system_fingerprint", None
                        )
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        usage = chunk_usage
                    choices = getattr(chunk, "choices", None)
                    if not choices:
                        continue
                    finish_reason = getattr(choices[0], "finish_reason", None)
                    if finish_reason:
                        self._last_finish_reason = finish_reason
                    delta = getattr(choices[0], "delta", None)
                    try:
                        reasoning_piece = (
                            getattr(delta, "reasoning_content", None)
                            if delta is not None
                            else None
                        )
                    except Exception:
                        reasoning_piece = None
                    if isinstance(reasoning_piece, str):
                        reasoning_parts.append(reasoning_piece)
                    if reasoning_piece:
                        self._last_reasoning_observed = True
                    piece = getattr(delta, "content", None) if delta is not None else None
                    if isinstance(piece, str):
                        parts.append(piece)
        finally:
            # Preserve usage even when a stream raises after emitting its usage
            # trailer. Private thinking remains process-local and is never added
            # to the cost ledger or transcript.
            self._last_usage = usage
            if reasoning_parts:
                self._last_reasoning_content = "".join(reasoning_parts)
        return "".join(parts)


class NaturalLanguageTargetLocator:
    """Map a public issue plus source evidence to candidate semantic surfaces."""

    def __init__(self, inspector: RepositoryInspector | None = None) -> None:
        self.inspector = inspector or RepositoryInspector()
        self._source_context_cache: dict[Path, tuple[Any, dict[str, Any]]] = {}

    def locate(self, task: PublicIssue, root: str | Path = Path.cwd()) -> dict[str, Any]:
        repo = resolve_task_repo(root, task).resolve()
        cached = self._source_context_cache.get(repo)
        if cached is None:
            inspect_result = self.inspector.inspect(repo)
            graph = build_source_state_graph(inspect_result)
            self._source_context_cache[repo] = (inspect_result, graph)
        else:
            inspect_result, graph = cached
        evidence = locate_issue_evidence(task.issue, graph)
        for item in evidence:
            item["repo"] = str(repo)
        return {
            "task_id": task.id,
            "benchmark": task.benchmark,
            "repo": str(repo),
            "profile": inspect_result.profile,
            "graph": graph,
            "source_evidence": evidence,
            "inputs_read": [task.source_path or "", str(repo)],
        }


class DeterministicCheckpointSynthesisAgent:
    """Deterministic local baseline for method checkpoint synthesis.

    This agent consumes only the public issue and source inspection result. Hidden
    target/oracle files are intentionally outside its API.
    """

    name = "deterministic"

    def __init__(self, locator: NaturalLanguageTargetLocator | None = None) -> None:
        self.locator = locator or NaturalLanguageTargetLocator()

    def synthesize(
        self,
        task: PublicIssue,
        root: str | Path = Path.cwd(),
        artifact_dir: Path | None = None,
        *,
        feedback: dict[str, Any] | None = None,
    ) -> MethodCheckpoint:
        # Templates are keyed by task id, so prior-iteration feedback cannot change
        # the output; accept it only to satisfy the shared agent interface.
        del feedback
        located = self.locator.locate(task, root)
        generated_spec = synthesize_spec_from_public_issue(task)
        checkpoint = generated_spec.get("checkpoint", {})
        validity: CheckpointValidity = checkpoint.get("expected_validity", "unknown")
        source_evidence = located["source_evidence"] or [
            {
                "repo": located["repo"],
                "source_path": str(Path(located["repo"]) / "package.json"),
                "matched_symbol": task.benchmark,
                "confidence": 0.5,
                "rationale": "Fallback repository metadata evidence.",
            }
        ]
        return MethodCheckpoint(
            task_id=task.id,
            benchmark=task.benchmark,
            generated_spec=generated_spec,
            source_evidence=source_evidence,
            payload=_payload_for_spec(generated_spec),
            validity_class=validity,
            runtime_validation={"status": "pending", "detail": "Runtime validation has not been executed yet.", "required": True},
            inputs_read=[item for item in located["inputs_read"] if item],
            hidden_files_read=[],
            generated_by="warptest-deterministic-synthesizer-v1",
            model_generated_spec=copy.deepcopy(generated_spec),
            used_public_scaffold=True,
        )


CheckpointSynthesisAgent = DeterministicCheckpointSynthesisAgent


@dataclass
class LLMCheckpointSynthesisAgent:
    model: str = DEFAULT_SYNTHESIS_MODEL
    base_url: str = DEFAULT_MODELSCOPE_BASE_URL
    api_key_env: str = "MODELSCOPE_API_KEY"
    temperature: float = 0.2
    max_tokens: int = 4096
    extra_headers: dict[str, str] | None = None
    locator: NaturalLanguageTargetLocator = field(default_factory=NaturalLanguageTargetLocator)
    client: Any | None = None

    name: str = "llm"

    def synthesize(
        self,
        task: PublicIssue,
        root: str | Path = Path.cwd(),
        artifact_dir: Path | None = None,
        *,
        feedback: dict[str, Any] | None = None,
    ) -> MethodCheckpoint:
        located = self.locator.locate(task, root)
        prompt = build_llm_checkpoint_prompt(task, located, feedback=feedback)
        prompt_leaks = detect_private_grader_references(prompt)
        artifacts: dict[str, Any] = {
            "agent": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "prompt_private_reference_count": len(prompt_leaks),
            "prompt_private_references": prompt_leaks,
        }
        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            prompt_path = artifact_dir / "prompt.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            artifacts["prompt_path"] = str(prompt_path)
            write_json(artifact_dir / "prompt-meta.json", {"task_id": task.id, "model": self.model, "private_reference_scan": prompt_leaks})
        if prompt_leaks:
            raise SynthesisError("LLM synthesis prompt references private grader data.", artifacts=artifacts)

        raw_response_path: Path | None = None
        try:
            client = self.client or OpenAICompatibleChatClient(
                api_key=os.environ.get(self.api_key_env, ""),
                base_url=self.base_url,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                extra_headers=self.extra_headers,
                cost_kind="checkpoint_synthesis",
            )
            call = client.generate(self.model, prompt)
            raw = call.content
            artifacts["latency_ms"] = call.latency_ms
            if artifact_dir is not None:
                raw_response_path = artifact_dir / "raw-response.txt"
                raw_response_path.write_text(raw, encoding="utf-8")
                artifacts["raw_response_path"] = str(raw_response_path)
        except SynthesisError as error:
            error.artifacts.update(artifacts)
            if must_raise_llm_error(error):
                raise
            if artifact_dir is not None:
                write_json(artifact_dir / "synthesis-meta.json", {**artifacts, **error.artifacts, "fallback_reason": str(error)})
            if _is_c1_synthesis_task(task):
                raise
            return public_scaffold_checkpoint(
                task,
                located=located,
                model=self.model,
                reason=f"LLM API call failed before producing an executable checkpoint: {error}",
            )
        except Exception as error:
            if artifact_dir is not None and raw_response_path is None:
                write_json(
                    artifact_dir / "raw-response-error.json",
                    {"error": run_context.safe_error_text(str(error), limit=2000)},
                )
                artifacts["raw_response_error_path"] = str(artifact_dir / "raw-response-error.json")
            raise SynthesisError(
                run_context.safe_error_text(str(error), limit=2000), artifacts=artifacts
            ) from error

        if _is_c1_synthesis_task(task):
            try:
                checkpoint, finalization = finalize_formal_c1_model_output(
                    task=task,
                    raw_output=raw,
                    client=client,
                    model=self.model,
                    located=located,
                    root=root,
                    artifact_dir=artifact_dir,
                    source_cost_kind="checkpoint_synthesis",
                )
            except SynthesisError as error:
                if must_raise_llm_error(error):
                    raise
                artifacts.update(error.artifacts)
                artifacts.update(
                    {
                        "fallback_reason": str(error),
                        "repair_attempted": True,
                        "initial_output_sha256": hashlib.sha256(
                            raw.encode("utf-8")
                        ).hexdigest(),
                    }
                )
                if artifact_dir is not None:
                    write_json(
                        artifact_dir / "synthesis-meta.json", artifacts
                    )
                error.artifacts.update(artifacts)
                raise
            artifacts.update(
                {
                    "formal_c1_finalization": finalization,
                    "used_public_scaffold": False,
                    "initial_output_sha256": hashlib.sha256(
                        raw.encode("utf-8")
                    ).hexdigest(),
                }
            )
            if artifact_dir is not None:
                write_json(artifact_dir / "synthesis-meta.json", artifacts)
            return checkpoint

        try:
            parsed = parse_llm_checkpoint_output(raw)
            parse_errors: list[str] = []
        except SynthesisError as error:
            artifacts.update(error.artifacts)
            artifacts["fallback_reason"] = str(error)
            if artifact_dir is not None:
                write_json(artifact_dir / "synthesis-meta.json", artifacts)
            return public_scaffold_checkpoint(
                task,
                located=located,
                model=self.model,
                reason=f"LLM output was not an executable checkpoint mapping: {error}",
                raw_model_output=raw,
            )

        output_leaks = detect_private_grader_references(parsed)
        raw_generated_spec = parsed.get("generated_spec")
        model_generated_spec = (
            raw_generated_spec
            if is_usable_generated_spec(raw_generated_spec, task=task)
            else None
        )
        try:
            normalized, normalization_warnings = normalize_llm_checkpoint_mapping(parsed, task=task)
            used_public_scaffold = any(
                warning.startswith("generated_spec_") for warning in normalization_warnings
            )
            checkpoint = method_checkpoint_from_llm_mapping(
                normalized,
                task=task,
                located=located,
                model=self.model,
                model_generated_spec=copy.deepcopy(model_generated_spec),
                used_public_scaffold=used_public_scaffold,
            )
        except SynthesisError as error:
            artifacts.update(error.artifacts)
            artifacts["fallback_reason"] = str(error)
            if artifact_dir is not None:
                write_json(artifact_dir / "synthesis-meta.json", artifacts)
            return public_scaffold_checkpoint(
                task,
                located=located,
                model=self.model,
                reason=f"LLM checkpoint contract failed: {error}",
                raw_model_output=raw,
            )
        schema_errors = validate_method_checkpoint(checkpoint, root)
        artifacts.update(
            {
                "parse_errors": parse_errors,
                "normalization_warnings": normalization_warnings,
                "schema_errors": schema_errors,
                "output_private_reference_count": len(output_leaks),
                "output_private_references": output_leaks,
            }
        )
        if artifact_dir is not None:
            write_json(artifact_dir / "synthesis-meta.json", artifacts)
        if output_leaks:
            raise SynthesisError("LLM method checkpoint references private grader data.", artifacts=artifacts)
        if schema_errors:
            raise SynthesisError("LLM method checkpoint failed schema validation.", artifacts=artifacts)
        return checkpoint


DEFAULT_MAX_EXPLORE_STEPS = 12


@dataclass
class ExploratoryLLMCheckpointSynthesisAgent:
    """Autonomous source-exploration synthesis agent.

    Instead of receiving a pre-digested semantic graph, evidence list, and source
    vocabulary, this agent gets only the public issue and a minimal repo map, then
    must discover the deep semantic target by issuing read_file/grep/list_dir tool
    calls via a text-based ReAct protocol. Once it emits a FINAL checkpoint, the
    output is parsed, normalized, validated, and graded through the same path as
    the single-shot LLM agent so strict/scaffold grading is unchanged.
    """

    model: str = DEFAULT_SYNTHESIS_MODEL
    base_url: str = DEFAULT_MODELSCOPE_BASE_URL
    api_key_env: str = "MODELSCOPE_API_KEY"
    temperature: float = 0.2
    max_tokens: int = 4096
    max_steps: int = DEFAULT_MAX_EXPLORE_STEPS
    extra_headers: dict[str, str] | None = None
    client: Any | None = None
    ablation_variant: str = "full"

    name: str = "llm_explore"

    @scoped_agent_policy
    def synthesize(
        self,
        task: PublicIssue,
        root: str | Path = Path.cwd(),
        artifact_dir: Path | None = None,
        *,
        feedback: dict[str, Any] | None = None,
    ) -> MethodCheckpoint:
        validate_variant(self.ablation_variant)
        if self.ablation_variant != "full" and (
            not _is_c1_synthesis_task(task) or _is_lazarus_synthesis_task(task)
        ):
            raise ValueError("Synthesis ablations support the performance-study C1 tasks only")
        no_source = self.ablation_variant == "no_source_exploration"
        repo = resolve_task_repo(root, task)
        sandbox = RepoSandbox(
            repo,
            text_suffixes=(
                LAZARUS_SOURCE_TEXT_SUFFIXES
                if _is_lazarus_synthesis_task(task)
                else None
            ),
            ignored_dirs=(
                LAZARUS_IGNORED_DIRS
                if _is_lazarus_synthesis_task(task)
                else None
            ),
        )
        repo_map = sandbox.build_repo_map()
        system_prompt = build_explore_system_prompt(task, ablation_variant=self.ablation_variant)
        initial_prompt = build_explore_initial_prompt(
            task, repo, repo_map, self.max_steps, feedback=feedback,
            ablation_variant=self.ablation_variant,
        )
        prompt_leaks = detect_private_grader_references(system_prompt + "\n" + initial_prompt)
        artifacts: dict[str, Any] = {
            "agent": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "max_steps": self.max_steps,
            "ablation_variant": self.ablation_variant,
            "ablation_revision": ABLATION_REVISION,
            "prompt_private_reference_count": len(prompt_leaks),
            "prompt_private_references": prompt_leaks,
        }
        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            prompt_path = artifact_dir / "prompt.txt"
            prompt_path.write_text(system_prompt + "\n\n" + initial_prompt, encoding="utf-8")
            artifacts["prompt_path"] = str(prompt_path)
            write_json(artifact_dir / "public-contract.json", public_contract_view(
                _benchmark_contract_for_task(task), self.ablation_variant
            ))
        if prompt_leaks:
            raise SynthesisError("Exploration prompt references private grader data.", artifacts=artifacts)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": initial_prompt},
        ]
        transcript: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": initial_prompt},
        ]
        try:
            client = self.client or OpenAICompatibleChatClient(
                api_key=os.environ.get(self.api_key_env, ""),
                base_url=self.base_url,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                extra_headers=self.extra_headers,
                cost_kind="checkpoint_exploration",
            )
        except SynthesisError as error:
            if must_raise_llm_error(error):
                raise
            print(
                f"[react] {self.model} {task.id}: FALLBACK to scaffold (client setup failed: {error})",
                flush=True,
            )
            located = {"repo": str(repo), "inputs_read": [task.source_path or "", str(repo)]}
            return public_scaffold_checkpoint(
                task,
                located=located,
                model=self.model,
                reason=f"LLM API client setup failed before source exploration: {error}",
            )

        final_text: str | None = None
        total_latency = 0
        tool_calls = 0
        try:
            for step in range(1, self.max_steps + 1):
                if step >= self.max_steps - 1:
                    remaining = self.max_steps - step + 1
                    if _is_c1_synthesis_task(task) and tool_calls < 1 and not no_source:
                        nudge = (
                            f"REMINDER: only {remaining} step(s) remain and no repository tool has "
                            "been used. Issue one ACTION now; a zero-tool FINAL is invalid."
                        )
                    else:
                        nudge = (
                            f"REMINDER: only {remaining} step(s) remain in your budget. Do NOT issue another "
                            "ACTION. Emit your FINAL ```yaml ... ``` checkpoint NOW using the evidence gathered "
                            "so far, making your best inference for any value you could not fully confirm."
                        )
                    messages.append({"role": "user", "content": nudge})
                    transcript.append({"role": "user", "content": nudge, "finalize_nudge": True})
                cost_marker = cost.mark_records()
                terminal_checkpoint = (
                    getattr(task, "prompt_revision", "") == "lzp-generated-state-v3"
                    and step == self.max_steps and tool_calls > 0
                )
                if terminal_checkpoint:
                    final_instruction = (
                        "This is the reserved final response within the existing budget. "
                        "Return only a complete checkpoint YAML mapping (a FINAL wrapper is also accepted). "
                        "Do not request another ACTION. Use only the public task and collected source evidence. "
                        "Repository source_path citations must come from these observed files: "
                        + json.dumps(sorted(sandbox.reads))
                    )
                    messages.append({"role": "user", "content": final_instruction})
                    transcript.append({"role": "user", "content": final_instruction, "terminal_checkpoint": True})
                call = client.converse(self.model, messages)
                total_latency += int(getattr(call, "latency_ms", 0) or 0)
                reply = call.content
                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": reply,
                }
                reasoning_content = getattr(call, "reasoning_content", None)
                if isinstance(reasoning_content, str):
                    # Kimi thinking models require this exact field to be echoed
                    # on the next turn. Never copy it to ``transcript`` or treat it
                    # as ACTION/FINAL content.
                    assistant_message["reasoning_content"] = reasoning_content
                messages.append(assistant_message)
                transcript.append({"role": "assistant", "content": reply})
                kind, *rest = parse_react_reply(
                    reply,
                    strict_protocol=_is_lazarus_synthesis_task(task),
                )
                if terminal_checkpoint and kind != "final":
                    try:
                        final_mapping = parse_llm_checkpoint_output(reply)
                    except SynthesisError:
                        final_mapping = None
                    if isinstance(final_mapping, dict) and "generated_spec" in final_mapping:
                        kind, rest = "final", [reply]
                        artifacts["terminal_mapping_without_final_wrapper"] = True
                if kind == "final":
                    if _is_c1_synthesis_task(task) and tool_calls < 1 and not no_source:
                        guidance = (
                            "ERROR: a formal C1 checkpoint requires at least one repository tool "
                            "ACTION before FINAL so source evidence is auditable. Issue one "
                            "read_file, grep, or list_dir ACTION now."
                        )
                        messages.append({"role": "user", "content": guidance})
                        transcript.append(
                            {
                                "role": "user",
                                "content": guidance,
                                "zero_tool_final_rejected": True,
                            }
                        )
                        print(
                            f"[react] {self.model} {task.id} step {step}/{self.max_steps}: "
                            "rejected zero-tool FINAL",
                            flush=True,
                        )
                        continue
                    cost.reclassify_since(
                        cost_marker,
                        model=self.model,
                        from_kind="checkpoint_exploration",
                        to_kind="checkpoint_synthesis",
                    )
                    final_text = rest[0]
                    print(
                        f"[react] {self.model} {task.id} step {step}/{self.max_steps}: FINAL "
                        f"(tool_calls={tool_calls})",
                        flush=True,
                    )
                    break
                if kind == "action":
                    if terminal_checkpoint:
                        artifacts["terminal_action_not_dispatched"] = True
                        transcript.append({"role": "user", "content": "Terminal response requested ACTION; not dispatched beyond the exploration budget."})
                        continue
                    tool, args = rest[0], rest[1]
                    if no_source:
                        guidance = "Repository tools are unavailable. Emit FINAL using the provided public inputs; source_evidence must be []."
                        messages.append({"role": "user", "content": guidance})
                        transcript.append({"role": "user", "content": guidance, "blocked_tool": tool})
                        continue
                    observation = sandbox.dispatch(tool, args)
                    tool_calls += 1
                    target = ""
                    if isinstance(args, dict):
                        target = str(args.get("path") or args.get("pattern") or args.get("dir") or args.get("query") or "")
                    print(
                        f"[react] {self.model} {task.id} step {step}/{self.max_steps}: "
                        f"{tool} {target}".rstrip(),
                        flush=True,
                    )
                    obs_message = f"OBSERVATION:\n{observation}"
                    messages.append({"role": "user", "content": obs_message})
                    transcript.append({"role": "user", "content": obs_message, "tool": tool, "args": args})
                    continue
                print(
                    f"[react] {self.model} {task.id} step {step}/{self.max_steps}: parse-error, retrying",
                    flush=True,
                )
                guidance = (
                    "ERROR: could not parse your reply. Emit FINAL then a ```yaml ... ``` mapping; repository tools are unavailable."
                    if no_source else
                    "ERROR: could not parse your reply. Respond with exactly one ACTION block "
                    "(ACTION: read_file|grep|list_dir followed by a one-line JSON args object) "
                    "or a FINAL block (FINAL then a ```yaml ... ``` mapping)."
                )
                messages.append({"role": "user", "content": guidance})
                transcript.append({"role": "user", "content": guidance, "parse_error": rest[0] if rest else ""})
        except SynthesisError as error:
            error.artifacts.update(artifacts)
            if must_raise_llm_error(error):
                raise
            print(
                f"[react] {self.model} {task.id}: FALLBACK to scaffold after {tool_calls} tool call(s) ({error})",
                flush=True,
            )
            if artifact_dir is not None:
                write_json(artifact_dir / "synthesis-meta.json", {**artifacts, **error.artifacts, "fallback_reason": str(error)})
            located = {
                "repo": str(repo),
                "inputs_read": [task.source_path or "", str(repo)] + [str(repo / rel) for rel in sandbox.reads],
            }
            if _is_c1_synthesis_task(task):
                raise
            return public_scaffold_checkpoint(
                task,
                located=located,
                model=self.model,
                reason=f"LLM API call failed during source exploration: {error}",
            )

        artifacts.update({"latency_ms": total_latency, "tool_calls": tool_calls, "files_read": list(sandbox.reads)})
        if artifact_dir is not None:
            write_json(artifact_dir / "transcript.json", {"task_id": task.id, "model": self.model, "messages": transcript})

        if final_text is None:
            artifacts["reason"] = "no FINAL checkpoint within max_steps"
            print(
                f"[react] {self.model} {task.id}: FALLBACK to scaffold (no FINAL within "
                f"{self.max_steps} steps, {tool_calls} tool call(s))",
                flush=True,
            )
            if artifact_dir is not None:
                write_json(artifact_dir / "synthesis-meta.json", artifacts)
            located = {
                "repo": str(repo),
                "inputs_read": [task.source_path or "", str(repo)] + [str(repo / rel) for rel in sandbox.reads],
            }
            if _is_c1_synthesis_task(task):
                raise SynthesisError(
                    "Exploration agent did not produce a FINAL checkpoint within max_steps.",
                    artifacts=artifacts,
                )
            return public_scaffold_checkpoint(
                task,
                located=located,
                model=self.model,
                reason="Exploration agent did not produce a FINAL checkpoint within max_steps.",
            )

        if artifact_dir is not None:
            (artifact_dir / "raw-response.txt").write_text(final_text, encoding="utf-8")
            artifacts["raw_response_path"] = str(artifact_dir / "raw-response.txt")

        if _is_c1_synthesis_task(task):
            located = {
                "repo": str(repo),
                "inputs_read": [task.source_path or "", str(repo)]
                + [str(repo / rel) for rel in sandbox.reads],
            }
            try:
                checkpoint, finalization = finalize_formal_c1_model_output(
                    task=task,
                    raw_output=final_text,
                    client=client,
                    model=self.model,
                    located=located,
                    root=root,
                    artifact_dir=artifact_dir,
                    source_cost_kind="checkpoint_exploration",
                    ablation_variant=self.ablation_variant,
                )
            except SynthesisError as error:
                if must_raise_llm_error(error):
                    raise
                artifacts.update(error.artifacts)
                artifacts.update(
                    {
                        "fallback_reason": str(error),
                        "repair_attempted": True,
                        "initial_output_sha256": hashlib.sha256(
                            final_text.encode("utf-8")
                        ).hexdigest(),
                    }
                )
                if artifact_dir is not None:
                    write_json(
                        artifact_dir / "synthesis-meta.json", artifacts
                    )
                error.artifacts.update(artifacts)
                raise
            if _is_c1_synthesis_task(task):
                read_paths = set(sandbox.reads)
                unread_evidence = sorted(
                    {
                        str(item.get("source_path") or "")
                        .replace("\\", "/")
                        .removeprefix("./")
                        for item in checkpoint.source_evidence
                        if isinstance(item, dict)
                        and not (
                            getattr(task, "prompt_revision", "") == "lzp-generated-state-v3"
                            and item.get("kind") == "public_task"
                        )
                        and str(item.get("source_path") or "")
                        .replace("\\", "/")
                        .removeprefix("./")
                        not in read_paths
                    }
                )
                if unread_evidence:
                    artifacts.update(
                        {
                            "source_evidence_unread": unread_evidence,
                            "files_read": list(sandbox.reads),
                        }
                    )
                    if artifact_dir is not None:
                        write_json(artifact_dir / "synthesis-meta.json", artifacts)
                    raise SynthesisError(
                        "Exploration source evidence references files the agent did not read: "
                        + ", ".join(unread_evidence),
                        artifacts=artifacts,
                    )
            artifacts.update(
                {
                    "formal_c1_finalization": finalization,
                    "used_public_scaffold": False,
                    "initial_output_sha256": hashlib.sha256(
                        final_text.encode("utf-8")
                    ).hexdigest(),
                }
            )
            if artifact_dir is not None:
                write_json(artifact_dir / "synthesis-meta.json", artifacts)
            return checkpoint

        try:
            parsed = parse_llm_checkpoint_output(final_text)
        except SynthesisError as error:
            artifacts.update(error.artifacts)
            artifacts["fallback_reason"] = str(error)
            print(
                f"[react] {self.model} {task.id}: FALLBACK to scaffold (FINAL not a valid checkpoint mapping: {error})",
                flush=True,
            )
            if artifact_dir is not None:
                write_json(artifact_dir / "synthesis-meta.json", artifacts)
            located = {
                "repo": str(repo),
                "inputs_read": [task.source_path or "", str(repo)] + [str(repo / rel) for rel in sandbox.reads],
            }
            return public_scaffold_checkpoint(
                task,
                located=located,
                model=self.model,
                reason=f"Exploration FINAL was not an executable checkpoint mapping: {error}",
                raw_model_output=final_text,
            )

        output_leaks = detect_private_grader_references(parsed)
        raw_generated_spec = parsed.get("generated_spec")
        model_generated_spec = (
            raw_generated_spec
            if is_usable_generated_spec(raw_generated_spec, task=task)
            else None
        )
        located = {
            "repo": str(repo),
            "inputs_read": [task.source_path or "", str(repo)] + [str(repo / rel) for rel in sandbox.reads],
        }
        try:
            normalized, normalization_warnings = normalize_llm_checkpoint_mapping(parsed, task=task)
            used_public_scaffold = any(warning.startswith("generated_spec_") for warning in normalization_warnings)
            checkpoint = method_checkpoint_from_llm_mapping(
                normalized,
                task=task,
                located=located,
                model=self.model,
                model_generated_spec=copy.deepcopy(model_generated_spec),
                used_public_scaffold=used_public_scaffold,
            )
        except SynthesisError as error:
            artifacts.update(error.artifacts)
            artifacts["fallback_reason"] = str(error)
            if artifact_dir is not None:
                write_json(artifact_dir / "synthesis-meta.json", artifacts)
            return public_scaffold_checkpoint(
                task,
                located=located,
                model=self.model,
                reason=f"Exploration checkpoint contract failed: {error}",
                raw_model_output=final_text,
            )
        schema_errors = validate_method_checkpoint(checkpoint, root)
        artifacts.update(
            {
                "normalization_warnings": normalization_warnings,
                "schema_errors": schema_errors,
                "output_private_reference_count": len(output_leaks),
                "output_private_references": output_leaks,
            }
        )
        if artifact_dir is not None:
            write_json(artifact_dir / "synthesis-meta.json", artifacts)
        if output_leaks:
            raise SynthesisError("Exploration method checkpoint references private grader data.", artifacts=artifacts)
        if schema_errors:
            raise SynthesisError("Exploration method checkpoint failed schema validation.", artifacts=artifacts)
        return checkpoint


def _format_feedback(feedback: dict[str, Any] | None) -> str | None:
    """Render a prior failed iteration's ``pair_verdict`` into a synthesis hint.

    The state-loop feedback-regeneration loop passes the previous iteration's
    before/after visual verdict here so the next synthesis can target the actual
    failure instead of blindly retrying. Returns ``None`` when no usable feedback
    is present so callers can keep the unconditional prompt unchanged.
    """

    if not isinstance(feedback, dict):
        return None

    def _half(half: Any) -> str | None:
        if not isinstance(half, dict):
            return None
        label = half.get("label")
        if not label:
            return None
        rationale = str(half.get("rationale") or "").strip()
        failures = half.get("rubric_failures")
        if isinstance(failures, (list, tuple)):
            failure_text = "; ".join(str(item) for item in failures if str(item).strip())
        else:
            failure_text = str(failures or "").strip()
        parts = [str(label)]
        if rationale:
            parts.append(f"- {rationale}")
        if failure_text:
            parts.append(f"(unmet: {failure_text})")
        return " ".join(parts)

    before = _half(feedback.get("before"))
    after = _half(feedback.get("after"))
    if before is None and after is None:
        return None

    lines = [
        "PRIOR ATTEMPT FAILED the before/after visual check. Use this to fix the config:",
    ]
    if before is not None:
        lines.append(f"- before half: {before}")
    if after is not None:
        lines.append(f"- after half: {after}")
    lines.append(
        "Revise the target/checkpoint/actions to address these specific failures; "
        "do not repeat the same config that just failed."
    )
    return "\n".join(lines)


def build_explore_system_prompt(task: PublicIssue, *, ablation_variant: str = "full") -> str:
    validate_variant(ablation_variant)
    contract = public_contract_view(_benchmark_contract_for_task(task), ablation_variant)
    # Render the schema hint with payload_kind nested under a `payload` mapping key
    # (matching the FINAL mapping's actual required top-level shape) rather than as a
    # bare `payload_kind:` sibling of `generated_spec`. Dumped raw, `generated_spec:`
    # immediately followed by `payload_kind:` visually reads as a template for the
    # FINAL document itself, and models pattern-match it into a flat `payload_kind:`
    # top-level key instead of the required nested `payload: {kind: ...}` mapping --
    # this was observed to be a leading cause of "LLM payload must be a mapping"
    # synthesis fallbacks, independent of model or exploration depth.
    contract_hint = dict(contract)
    if "payload_kind" in contract_hint:
        contract_hint["payload"] = {"kind": contract_hint.pop("payload_kind")}
    # `selection_rule` is coordinator guidance, not a FINAL field.  Dumped inside the
    # schema it reads as a top-level key of the document being requested, and a model
    # that copies it produces the "illegal top-level keys: selection_rule" rejection
    # even when its checkpoint is otherwise exact.  Carry it as prose instead, for the
    # same reason `payload_kind` is nested above.
    selection_rule = contract_hint.pop("selection_rule", None)
    contract_hint = _wrap_type_placeholders(contract_hint)
    prompt = "\n".join(
        [
            "You are WarpTest's checkpoint synthesis agent in autonomous source-exploration mode.",
            ("You receive a public issue, repository map and public contract. Infer the Start and transition from these inputs."
             if ablation_variant == "no_source_exploration" else
             "You receive a public issue and a sandboxed game repository. You must DISCOVER the deep semantic target yourself by reading the source."),
            "Private grader answer keys (the benchmark's reference solutions) are unavailable and must never be referenced.",
            "",
            "On each turn reply with EXACTLY ONE of the following blocks and nothing else.",
            *(["Exception: when the reserved final response is explicitly requested at the budget limit, a plain checkpoint YAML mapping without a FINAL wrapper is also accepted. No ACTION is executed in that final slot."]
              if getattr(task, "prompt_revision", "") == "lzp-generated-state-v3" else []),
            "",
            "Read part of a file (line range is optional but recommended):",
            "ACTION: read_file",
            '{"path": "src/state/gameStore.ts", "start": 1, "end": 160}',
            "",
            "Search the repository with a regular expression (glob is optional):",
            "ACTION: grep",
            '{"pattern": "saveGame", "glob": "*.ts"}',
            "",
            "List a directory:",
            "ACTION: list_dir",
            '{"path": "src"}',
            "",
            "When you have gathered enough evidence, emit the final method checkpoint EXACTLY as:",
            "FINAL",
            "```yaml",
            "<one YAML mapping>",
            "```",
            "",
            "The FINAL mapping top-level keys must be: task_id, benchmark, generated_spec, source_evidence, payload, runtime_validation, validity_class.",
            "The FINAL block must be valid YAML. In free-text fields (e.g. source_evidence "
            "matched_symbol/rationale and any _note), write plain prose and do NOT embed raw "
            "double-quote (\") characters -- referring to code symbols like jumpToSystem or "
            "system ids kuro-belt without surrounding quotes avoids breaking the YAML parse.",
            ("generated_spec must contain exactly schema_version, benchmark, target, checkpoint, actions, assertions."
             if getattr(task,"prompt_revision","") == "lzp-generated-state-v3" else
             "generated_spec must be a nested mapping (never a prose string) with name, benchmark, target, checkpoint, actions, assertions."),
            ("Fairness contract: target encodes ONLY the public Start checkpoint; actions encode the public Transition. Hidden grader assertions are unavailable. Never place inferred after-state values into target."
             if ablation_variant == "no_assertion_prediction" else
             "Fairness contract: target encodes ONLY the public Start checkpoint; actions encode the public Transition; infer assertions from that transition and repository source evidence. Hidden grader assertions are unavailable. Never place inferred after-state values into target."),
            f"payload.kind must be: {contract.get('payload_kind')}.",
            (
                "source_evidence must include a repository citation {source_path, matched_symbol, rationale} for a file actually read. Public task text is a different evidence type: {kind: public_task, field: issue, rationale: ...}. Never put task prose in source_path."
                if getattr(task, "prompt_revision", "") == "lzp-generated-state-v3"
                else ("source_evidence must be an empty list []. Repository tools are unavailable in this configuration."
                      if ablation_variant == "no_source_exploration" else
                      "source_evidence must be a non-empty list of {source_path, matched_symbol, rationale} entries for files you actually read.")
            ),
            "validity_class must be one of: save-restorable, replay-reachable, injection-only, unknown.",
            "Benchmark output contract / schema:",
            *(["checkpoint_json_schema is a JSON Schema, not an output template. Emit a concrete instance. In particular assertions/worlds/options are arrays, never prose strings; preserve every required property."]
              if getattr(task, "prompt_revision", "") == "lzp-generated-state-v3" else []),
            yaml.safe_dump(contract_hint, sort_keys=False, allow_unicode=False),
            "Public semantic action schema (guidance only, not additional output fields). "
            "Every generated_spec.actions entry must use exactly one registered type, "
            "include every required field, and omit fields not allowed for that type:",
            yaml.safe_dump(
                public_contract_view(public_action_contract_for_task(task), ablation_variant),
                sort_keys=False,
                allow_unicode=False,
            ),
            *(
                [f"Selection rule (guidance, not an output field): {selection_rule}"]
                if selection_rule
                else []
            ),
            "Important: the schema above describes field TYPES, not values. Angle-bracket "
            "placeholders like <integer> or <non-empty string> are type hints, not literal "
            "values -- you MUST replace every one of them with the concrete value you actually "
            "recovered from the source or the target state. Writing the placeholder text itself "
            "(e.g. the literal string \"integer\") as a field's value is an error.",
            *([policy_instruction(ablation_variant)] if policy_instruction(ablation_variant) else []),
        ]
    )
    if ablation_variant == "no_source_exploration":
        start = prompt.index("Read part of a file")
        end = prompt.index("When you have gathered enough evidence")
        prompt = prompt[:start] + prompt[end:]
    return prompt


def build_explore_initial_prompt(
    task: PublicIssue,
    repo: Path,
    repo_map: str,
    max_steps: int = DEFAULT_MAX_EXPLORE_STEPS,
    *,
    feedback: dict[str, Any] | None = None,
    ablation_variant: str = "full",
) -> str:
    validate_variant(ablation_variant)
    payload = {"task": task.public_payload(), "repo": str(repo)}
    lines = [
        "Public issue to test:",
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        "Repository map (source tree; node_modules and build output are hidden):",
        repo_map,
        "",
        (f"You have at most {max_steps} response steps. No repository tools are available. "
         "Emit FINAL now using the public task, repository map and contract; source_evidence must be []."
         if ablation_variant == "no_source_exploration" else
        f"You have a strict budget of {max_steps} exploration steps (one tool call per step). "
        f"Spend them efficiently: read only the files needed to recover the exact semantic target, "
        f"then emit FINAL. You MUST emit FINAL before the budget runs out -- aim to finalize by around "
        f"step {max(1, max_steps - 2)}. A FINAL checkpoint built from partial evidence is far better than "
        f"running out of steps with no checkpoint at all."),
    ]
    feedback_hint = _format_feedback(feedback)
    if feedback_hint is not None:
        lines.extend(["", feedback_hint])
    lines.append(
        "Emit FINAL now; do not request repository tools."
        if ablation_variant == "no_source_exploration" else
        "Explore the repository with the tools above to find the exact semantic target, then emit FINAL. Begin now with one ACTION."
    )
    return "\n".join(lines)


def parse_react_reply(
    text: str, *, strict_protocol: bool = False
) -> tuple[str, ...]:
    """Parse one ReAct turn into ("final", yaml_text) | ("action", tool, args) | ("error", detail)."""

    if not isinstance(text, str) or not text.strip():
        return ("error", "empty reply")
    body = text.strip()

    # Unwrap a single outer code fence if it encloses the whole reply.
    outer = re.fullmatch(r"```[a-zA-Z0-9_]*\s*(.*?)```", body, flags=re.DOTALL)
    if outer is not None:
        inner = outer.group(1).strip()
        if re.search(r"(?im)^\s*(FINAL|ACTION)\b", inner):
            body = inner

    if strict_protocol:
        protocol_blocks = re.findall(
            r"(?im)^\s*(?:FINAL\s*:?\s*$|ACTION\s*:\s*(?:read_file|grep|list_dir)\b)",
            body,
        )
        if len(protocol_blocks) != 1:
            return ("error", "reply must contain exactly one ACTION or FINAL block")

    final_match = re.search(r"(?im)^\s*FINAL\s*:?\s*$", body)
    if final_match is not None:
        rest = body[final_match.end():].strip()
        fence = re.search(r"```(?:yaml|yml|json)?\s*(.*?)```", rest, flags=re.IGNORECASE | re.DOTALL)
        yaml_text = fence.group(1).strip() if fence is not None else rest
        if yaml_text:
            return ("final", yaml_text)

    action_match = re.search(r"(?im)^\s*ACTION\s*:\s*(read_file|grep|list_dir)\b", body)
    if action_match is not None:
        tool = action_match.group(1)
        rest = body[action_match.end():]
        json_match = re.search(r"\{.*\}", rest, flags=re.DOTALL)
        args: dict[str, Any] = {}
        if json_match is not None:
            raw = json_match.group(0)
            try:
                args = json.loads(raw)
            except Exception:
                try:
                    loaded = yaml.safe_load(raw)
                    args = loaded if isinstance(loaded, dict) else {}
                except Exception:
                    args = {}
        if not isinstance(args, dict):
            args = {}
        return ("action", tool, args)

    return ("error", "no ACTION or FINAL block found")


def build_synthesis_agent(
    agent: str,
    *,
    model: str = DEFAULT_SYNTHESIS_MODEL,
    base_url: str = DEFAULT_MODELSCOPE_BASE_URL,
    api_key_env: str = "MODELSCOPE_API_KEY",
    temperature: float = 0.2,
    max_tokens: int = 4096,
    explore: bool = False,
    max_steps: int = DEFAULT_MAX_EXPLORE_STEPS,
    extra_headers: dict[str, str] | None = None,
    ablation_variant: str = "full",
) -> SynthesisAgent:
    validate_variant(ablation_variant)
    if ablation_variant != "full" and not (agent in {"llm", "llm_explore"} and (explore or agent == "llm_explore")):
        raise ValueError("Ablations require the exploration protocol, including when tools are disabled")
    if agent == "deterministic":
        return DeterministicCheckpointSynthesisAgent()
    if agent in {"llm", "llm_explore"}:
        if explore or agent == "llm_explore":
            return ExploratoryLLMCheckpointSynthesisAgent(
                model=model,
                base_url=base_url,
                api_key_env=api_key_env,
                temperature=temperature,
                max_tokens=max_tokens,
                max_steps=max_steps,
                extra_headers=extra_headers,
                ablation_variant=ablation_variant,
            )
        return LLMCheckpointSynthesisAgent(
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_headers=extra_headers,
        )
    raise ValueError(f"Unsupported synthesis agent: {agent}")


# Bare type-name words used as leaf hints in BENCHMARK_CONTRACTS below (e.g.
# "score": "integer"). Observed failure mode: models echo these words back
# verbatim as the field's value (e.g. writing the literal string "integer"
# where a real recovered score belongs), which then fails at restore time with
# a low-level error like "invalid literal for int() with base 10: 'integer'"
# instead of a clear, attributable synthesis failure. None of our benchmarks'
# real recovered values are legitimately the bare English word "integer",
# "string", etc., so this exact-match set is safe to both mark in the prompt
# and to detect verbatim in model output (see _wrap_type_placeholders and
# find_placeholder_echoes).
_TYPE_PLACEHOLDER_WORDS = {
    "integer", "int", "string", "str", "boolean", "bool", "float", "number",
    "non-empty string",
}


def _wrap_type_placeholders(value: Any) -> Any:
    """Recursively wrap bare type-name leaf strings in ``<...>`` for display.

    Only exact (case-insensitive, stripped) matches against
    ``_TYPE_PLACEHOLDER_WORDS`` are wrapped -- longer descriptive hints (e.g.
    "system id string (discover exact id from source)") and enum-choice
    strings (e.g. "left|right|up|down") are left untouched, since those were
    not the observed failure mode and are harder to distinguish safely from
    real literal values. Wrapping makes a bare type hint visibly not a valid
    value to copy, and makes an unmodified echo of it unambiguous to detect.
    """
    if isinstance(value, dict):
        if value.get("$schema") == "https://json-schema.org/draft/2020-12/schema":
            # Machine-readable schemas contain literal type names; wrapping
            # them as placeholders corrupts the schema sent to the model.
            return copy.deepcopy(value)
        return {k: _wrap_type_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_wrap_type_placeholders(v) for v in value]
    if isinstance(value, str) and value.strip().lower() in _TYPE_PLACEHOLDER_WORDS:
        return f"<{value}>"
    return value


def find_placeholder_echoes(value: Any, *, path: str = "") -> list[str]:
    """Find leaf values in a parsed generated_spec that echo an unfilled type hint.

    Matches both the wrapped form (``<integer>``) a model may copy from the
    prompt verbatim, and the bare form (``integer``) in case a model infers
    the convention without seeing the bracket. Returns dotted paths of every
    match, e.g. ``["target.score"]``, for an attributable error message.
    """
    found: list[str] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            found.extend(find_placeholder_echoes(sub, path=f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for idx, sub in enumerate(value):
            found.extend(find_placeholder_echoes(sub, path=f"{path}[{idx}]"))
    elif isinstance(value, str):
        stripped = value.strip()
        bare = stripped.lower()
        bracketed = bare.startswith("<") and bare.endswith(">") and bare[1:-1] in _TYPE_PLACEHOLDER_WORDS
        if bare in _TYPE_PLACEHOLDER_WORDS or bracketed:
            found.append(path or "<root>")
    return found


# Public output-contract schema per benchmark. Shared by the single-shot prompt
# and the exploration ReAct prompt. This describes the WarpTest spec shape only;
# it contains no scenario specifics or hidden oracle values.
BENCHMARK_CONTRACTS: dict[str, dict[str, Any]] = {
    "2048": {
        "generated_spec": {
            "name": "non-empty string",
            "benchmark": "2048",
            "target": {"kind": "board", "board": "4x4 integer matrix before action", "score": "integer", "bestScore": "integer"},
            "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
            "actions": [{"type": "move", "direction": "left|right|up|down", "wait_ms": "integer"}],
            # Each assertion "type" needs its own extra field(s); a bare
            # {"type": "a|b|c"} entry (the previous shape here) taught models to
            # omit them entirely, which crashed the grader with a bare
            # KeyError("value")/KeyError("amount") at assertion time instead of a
            # readable synthesis-time schema error. Include only the assertion(s)
            # this task actually needs, each with its required field(s) filled in.
            "assertions": [
                {"type": "tile_value_present", "value": "integer tile value that must be visible on the board"},
                {"type": "score_increased_by_at_least", "amount": "integer minimum score increase"},
                {"type": "no_severe_console"},
            ],
        },
        "payload_kind": "localStorage",
    },
    "SpaceRPG": {
        "generated_spec": {
            "name": "non-empty string",
            "benchmark": "SpaceRPG",
            "setup": {"disable_economy_backend": True},
            "target": {
                "kind": "flight|station|galaxy_map|equipment_loadout|debug_scenario|state_patch",
                "screen": "screen id for the kind, e.g. flight|station|galaxyMap",
                "system_id": "system id string (discover exact id from source)",
                "station_id": "station id string for station/equipment_loadout kinds",
                "station_tab": "active station tab for equipment_loadout, e.g. Hangar",
                "equipment_inventory": {"<equipmentId>": "integer quantity (equipment_loadout kind)"},
                "galaxy_map_mode": "galaxy map mode string (galaxy_map kind)",
                "scenario_id": "debug scenario id (debug_scenario kind)",
                "mission_visible": ["visible mission id (debug_scenario kind)"],
                "completed_mission_visible": ["completed mission id (debug_scenario kind)"],
                "known_system_visible": ["known system id, in order (debug_scenario kind)"],
                "_note": "Include only the fields that apply to this task's kind; use the exact ids you discover from the repository.",
            },
            "checkpoint": {"allowed_strategies": ["test_hook_jump_and_validate"], "expected_validity": "replay-reachable|save-restorable|injection-only"},
            # Each action/assertion type needs its own extra field(s); emit only
            # the ones this task needs, each with its fields FILLED IN (a bare
            # {"type": ...} entry is rejected downstream with a KeyError at
            # restore time). hook_call needs method+args; key needs key; wait
            # needs ms. state_equals/state_contains/state_gte need path+value;
            # localStorage_key_exists needs key.
            "actions": [
                {"type": "hook_call", "method": "store action name (discover from source)", "args": ["positional argument to the method, e.g. a system id"]},
                {"type": "key", "key": "keyboard key to press"},
                {"type": "wait", "ms": "integer milliseconds to wait"},
            ],
            "assertions": [
                {"type": "state_equals", "path": "dotted state path, e.g. screen", "value": "expected value at that path"},
                {"type": "state_contains", "path": "dotted state path to an array", "value": "value the array must contain"},
                {"type": "state_gte", "path": "dotted state path to a number", "value": "integer lower bound"},
                {"type": "localStorage_key_exists", "key": "localStorage key that must exist"},
                {"type": "no_severe_console"},
            ],
        },
        "payload_kind": "test_hook",
    },
    "DatingSim": {
        "generated_spec": {
            "name": "non-empty string",
            "benchmark": "DatingSim",
            "setup": {"ai_mode": "mocked"},
            "target": {
                "kind": "deterministic_state",
                "seed": "integer",
                "player_name": "WarpTest",
                "gender": "male",
                "patch": {
                    "player": {
                        "resources": {"money": "int", "energy": "int", "health": "int", "mood": "int"},
                        "title": "optional player title string",
                        "relationships": {
                            "<characterId>": {
                                "trust": "int", "attraction": "int", "comfort": "int", "respect": "int",
                                "investment": "int", "conflict": "int", "status": "string",
                                "history": ["string"], "memories": ["string"],
                            }
                        },
                    },
                    "world": {"location": "string", "week": "int", "day": "int", "timeSlot": "string", "globalFlags": {"<flag>": "bool"}},
                },
                "expect": {
                    "location": "string", "player_title": "string",
                    "resources": {"money": "int", "energy": "int"},
                    "relationships": {"<characterId>": {"trust": "int", "attraction": "int", "status": "string"}},
                    "global_flags": {"<flag>": "bool"},
                },
                "_note": (
                    "patch = seeded Start state to inject; expect = current Start "
                    "values used to validate that injected state. Keep every "
                    "post-action value in assertions only. The effective Start "
                    "is initialized defaults overlaid by patch, so expect must "
                    "match even for fields omitted from patch. "
                    "Include only the sub-fields this task specifies; discover "
                    "the character id (e.g. for Elena) from the repository."
                ),
            },
            "checkpoint": {"allowed_strategies": ["test_hook_seeded_state"], "expected_validity": "injection-only|replay-reachable|save-restorable"},
            "actions": [{"type": "hook_action|hook_call|spacerpg_patch_state|wait"}],
            "assertions": [{"type": "state_equals|no_real_openai|no_severe_console"}],
        },
        "payload_kind": "zustand_hook",
    },
    "JupyterLab": {
        "generated_spec": {
            "name": "non-empty string",
            "benchmark": "JupyterLab",
            "target": {
                "kind": "notebook|text_file|workspace_layout|terminal_session",
                "path": "path under the isolated Jupyter root for notebook/text/session targets",
                "content": "text file content when target.kind=text_file",
                "cells": [{"cell_type": "code|markdown", "source": "cell source"}],
                "workspace": "workspace id when target.kind=workspace_layout",
                "paths": ["document paths opened in the workspace"],
            },
            "checkpoint": {"allowed_strategies": ["jupyterlab_workspace_api_validate"], "expected_validity": "save-restorable"},
            "actions": [
                {
                    "type": "jupyterlab_create_text_file",
                    "path": "workspace-relative text file path",
                    "content": "exact text content",
                },
                {
                    "type": "jupyterlab_open_file",
                    "path": "workspace-relative file or notebook path",
                    "workspace": "optional workspace id",
                },
                {
                    "type": "jupyterlab_edit_text_file",
                    "path": "workspace-relative text file path",
                    "content": "exact replacement text",
                },
                {
                    "type": "jupyterlab_create_notebook",
                    "path": "workspace-relative notebook path",
                    "cells": [
                        {
                            "cell_type": "code|markdown",
                            "source": "cell source",
                        }
                    ],
                },
                {
                    "type": "jupyterlab_edit_notebook_cell",
                    "path": "workspace-relative notebook path",
                    "cell_index": "non-negative integer",
                    "source": "exact replacement cell source",
                },
                {
                    "type": "jupyterlab_run_notebook_cell",
                    "path": "workspace-relative notebook path",
                    "cell_index": "non-negative integer",
                },
                {
                    "type": "jupyterlab_run_notebook",
                    "path": "workspace-relative notebook path",
                },
                {
                    "type": "jupyterlab_export_notebook_html",
                    "path": "workspace-relative notebook path",
                    "output_path": "workspace-relative HTML path",
                },
                {
                    "type": "jupyterlab_set_workspace_layout",
                    "workspace": "workspace id",
                    "paths": ["document path opened in the workspace"],
                },
                {
                    "type": "jupyterlab_start_session",
                    "path": "workspace-relative notebook path",
                    "kernel": "python3",
                },
                {"type": "wait", "ms": "integer milliseconds to wait"},
            ],
            "assertions": [{"type": "jupyterlab_file_exists|jupyterlab_file_text_equals|jupyterlab_notebook_output_contains|jupyterlab_notebook_cell_source_equals|jupyterlab_notebook_all_code_cells_executed|jupyterlab_notebook_has_mime|jupyterlab_notebook_has_no_error_outputs|jupyterlab_workspace_contains|jupyterlab_session_started|no_jupyterlab_server_errors"}],
        },
        "payload_kind": "jupyterlab_workspace_checkpoint",
    },
    "LibreOffice": {
        "generated_spec": {
            "name": "non-empty string",
            "benchmark": "LibreOffice",
            "target": {
                "kind": "writer_document|calc_spreadsheet|impress_presentation",
                "path": "ODF artifact path under the report artifact directory",
                "text": "Writer body text for writer_document targets",
                "paragraphs": [{"text": "Writer paragraph text", "style": "optional Writer style"}],
                "tables": {"Writer table name": [["header", "cells"]]},
                "sheet": "Calc sheet name",
                "cells": {"A1": "scalar cell value"},
                "formulas": {"A3": "=SUM(A1:A2)"},
                "sheets": {"SheetName": {"cells": {"A1": "scalar"}, "formulas": {"B1": "=A1*2"}}},
                "slides": [{"title": "Impress slide title", "subtitle": "optional subtitle", "bullets": ["bullet text"]}],
            },
            "checkpoint": {"allowed_strategies": ["libreoffice_uno_document_validate"], "expected_validity": "save-restorable"},
            "actions": [{"type": "libreoffice_open_document|libreoffice_replace_text|libreoffice_append_paragraph|libreoffice_insert_toc|libreoffice_update_indexes|libreoffice_apply_paragraph_style|libreoffice_insert_bookmark|libreoffice_insert_reference_field|libreoffice_set_table_cell|libreoffice_insert_table_row|libreoffice_append_table_row|libreoffice_save|libreoffice_export_pdf|libreoffice_set_cell|libreoffice_set_formula|libreoffice_recalculate|libreoffice_sort_range|libreoffice_define_named_range|libreoffice_create_chart|libreoffice_set_slide_title|libreoffice_set_slide_bullets|libreoffice_add_shape|libreoffice_move_slide|libreoffice_reopen"}],
            "assertions": [{"type": "libreoffice_text_contains|libreoffice_toc_contains|libreoffice_paragraph_style_is|libreoffice_table_cell_equals|libreoffice_bookmark_exists|libreoffice_reference_text_contains|libreoffice_pdf_exists|libreoffice_sheet_exists|libreoffice_cell_value_equals|libreoffice_formula_equals|libreoffice_named_range_exists|libreoffice_chart_exists|libreoffice_slide_count_equals|libreoffice_slide_title_equals|libreoffice_slide_text_contains|libreoffice_shape_text_contains|no_libreoffice_errors"}],
        },
        "payload_kind": "libreoffice_uno_document_checkpoint",
        "selection_rule": "Use public UNO/ODF document semantics for Writer, Calc, and Impress. Do not modify games/LibreOffice or use GUI click replay.",
    },
    "Blender": {
        "generated_spec": {
            "name": "non-empty string",
            "benchmark": "Blender",
            "target": {
                "kind": "scene|blend_file",
                "objects": [
                    {
                        "name": "object name",
                        "type": "MESH",
                        "primitive": "cube|uv_sphere|cylinder|cone",
                        "size": "optional float for cube",
                        "location": ["x", "y", "z"],
                        "material": {"name": "material name", "color": ["r", "g", "b", "a"]},
                    }
                ],
                "save_as": "optional .blend path under the report artifact dir",
            },
            "checkpoint": {"allowed_strategies": ["blender_python_scene_validate"], "expected_validity": "save-restorable"},
            "actions": [{"type": "blender_add_primitive|blender_assign_material|blender_set_object_location|blender_set_object_scale|blender_set_object_rotation|blender_set_frame|blender_save_as|blender_reopen|blender_add_modifier|blender_apply_modifier|blender_set_keyframe|wait"}],
            "assertions": [{"type": "blender_object_exists|blender_material_color|blender_object_location|blender_object_scale|blender_object_rotation|blender_current_frame_equals|blender_file_exists|blender_modifier_exists|blender_modifier_applied|blender_mesh_geometry|blender_animation_keyframes|no_blender_python_errors"}],
        },
        "payload_kind": "blender_python_scene_checkpoint",
        "selection_rule": "Use bpy/operator API actions inside the visible Blender GUI process. Do not use --background/headless mode or pixel click/keyboard GUI-agent automation.",
    },
    "jynew": {
        "generated_spec": {
            "name": "non-empty string",
            "benchmark": "jynew",
            "target": {
                "kind": "synthesized_state|save_restore",
                "mod_id": "JYX2",
                "player_level": "integer (optional)",
                "money": "integer (optional)",
                "team_ids": "list of role id integers (optional)",
                "items": [{"id": "integer", "count": "integer"}],
                "skills": [{"id": "integer", "level": "integer"}],
                "map_id": "integer (optional)",
                "key_values": [{"key": "string", "value": "string"}],
            },
            "checkpoint": {"allowed_strategies": ["jynew_utility_save_validate"], "expected_validity": "save-restorable|injection-only"},
            "actions": [{"type": "jynew_join_team|jynew_add_item|jynew_learn_skill|jynew_level_up|jynew_save|jynew_load_save|jynew_use_item|jynew_rest|jynew_set_key_value|jynew_enter_submap|jynew_add_money"}],
            "assertions": [{"type": "jynew_role_attr|jynew_team_contains|jynew_team_count|jynew_item_count|jynew_money_gte|jynew_money_equals|jynew_skill_learned|jynew_key_value_equals|jynew_map_id|no_jynew_utility_errors"}],
        },
        "payload_kind": "jynew_utility_checkpoint",
    },
    "pal3": {
        "generated_spec": {
            "name": "non-empty string",
            "benchmark": "pal3",
            "target": {
                "kind": "synthesized_state|save_restore",
                "story_vars": [{"key": "integer (32768 is main story progress)", "value": "integer"}],
                "scene_city": "scene city id e.g. q01 (optional)",
                "scene_name": "scene name e.g. b (optional)",
                "money": "integer (optional)",
                "team_ids": "list of player actor id integers (JingTian=0..HuaYing=5) (optional)",
                "items": [{"id": "integer", "count": "integer"}],
                "world_regions": [{"region": "integer", "flag": "0|1|2"}],
                "favors": [{"actor_id": "integer", "amount": "integer"}],
                "position": {"set": "boolean", "actor_id": "integer (-1 = current player)", "x": "float", "z": "float", "layer": "integer", "facing": "integer"},
                "validations": [{"path": "story_var.32768|money|team.count|scene.city|scene.name|item.<id>|favor.<actorId>|region.<region>", "expected": "string"}],
            },
            "checkpoint": {"allowed_strategies": ["pal3_console_save_validate"], "expected_validity": "save-restorable|injection-only"},
            "actions": [{"type": "pal3_set_story_var|pal3_load_scene|pal3_add_team_member|pal3_remove_team_member|pal3_add_money|pal3_add_item|pal3_enable_world_region|pal3_set_favor|pal3_save|pal3_load_save"}],
            "assertions": [{"type": "pal3_story_var_equals|pal3_scene_is|pal3_team_contains|pal3_team_count|pal3_item_count|pal3_money_gte|pal3_world_region_enabled|pal3_favor_gte|no_pal3_utility_errors"}],
        },
        "payload_kind": "pal3_console_checkpoint",
    },
    "luanti": {
        "generated_spec": {
            "name": "non-empty string",
            "benchmark": "luanti",
            "target": {
                "kind": "synthesized_world|save_restore",
                "nodes": [{"pos": {"x": "int", "y": "int", "z": "int"}, "node": "basenodes:*|stairs:*|chest:chest node id", "param2": "optional int 0..255"}],
                "fill": [{"from": {"x": "int", "y": "int", "z": "int"}, "to": {"x": "int", "y": "int", "z": "int"}, "node": "node id", "param2": "optional int 0..255"}],
                "player": {
                    "pos": {"x": "int", "y": "int", "z": "int"},
                    "hp": "int 0..20",
                    "inventory": [{"item": "item id", "count": "int"}],
                },
                "meta_inventory": [{"pos": {"x": "int", "y": "int", "z": "int"}, "node": "chest:chest", "list": "main", "items": [{"item": "item id", "count": "int"}]}],
                "mod_storage": [{"key": "string", "value": "string"}],
                "validations": [{"path": "node.<x>,<y>,<z>|player.hp|gametime|mod_storage.<key>|inventory.<item>", "expected": "string"}],
            },
            "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable|injection-only"},
            "actions": [{"type": "luanti_set_node|luanti_dig_node|luanti_fill|luanti_set_player_pos|luanti_set_player_hp|luanti_give_item|luanti_set_player_inventory|luanti_set_meta_inventory|luanti_set_mod_storage|luanti_wait_steps|luanti_save_world"}],
            "assertions": [{"type": "luanti_node_is|luanti_node_count|luanti_player_pos_near|luanti_player_hp_gte|luanti_inventory_contains|luanti_meta_inventory_contains|luanti_mod_storage_equals|luanti_gametime_gte|no_luanti_mod_errors"}],
        },
        "payload_kind": "luanti_worldmod_checkpoint",
    },
    "OpenRA": {
        "schema_variants": {
            "map_validation": {
                "generated_spec": {
                    "name": "non-empty string",
                    "benchmark": "OpenRA",
                    "target": {
                        "kind": "openra_map",
                        "mod": "cnc|ra",
                        "requires_mod": "same value as mod",
                        "map": "OpenRA map directory id",
                        "title": "map title from map.yaml",
                        "visibility": "MissionSelector|Lobby when known",
                        "categories": ["Campaign|Drop Zone when known"],
                        "players": [{"id": "player id", "playable": "boolean", "faction": "faction id"}],
                        "actors": [{"type": "actor id", "owner": "player id", "min_count": "integer"}],
                        "objectives": [{"owner": "player id", "type": "primary|secondary", "id": "objective id"}],
                    },
                    "checkpoint": {"allowed_strategies": ["openra_utility_map_validate"], "expected_validity": "replay-reachable"},
                    "actions": [{"type": "openra_validate_map", "mod": "cnc|ra", "map": "map id"}],
                    "assertions": [{"type": "openra_map_loaded|openra_player_exists|openra_actor_count|openra_objective_declared|no_openra_utility_errors"}],
                },
                "payload_kind": "openra_utility_checkpoint",
            },
            "gameplay_production": {
                "generated_spec": {
                    "name": "non-empty string",
                    "benchmark": "OpenRA",
                    "setup": {
                        "openra_gameplay_probe": {
                            "cash": "integer",
                            "fast_build": "boolean",
                            "build_anywhere": "boolean",
                            "all_tech": "boolean",
                            "delay_ticks": "non-negative integer",
                            "remove_player_actor_types": {
                                "player id": ["actor type"]
                            },
                        }
                    },
                    "target": {
                        "kind": "openra_map",
                        "mod": "ra",
                        "requires_mod": "ra",
                        "map": "OpenRA map directory id",
                        "title": "map title from map.yaml",
                        "visibility": "MissionSelector",
                        "categories": ["Campaign"],
                        "players": [{"id": "player id", "playable": "boolean", "faction": "faction id"}],
                        "actors": [{"id": "named actor id", "type": "actor id", "owner": "player id"}],
                    },
                    "checkpoint": {"allowed_strategies": ["openra_gameplay_production_validate"], "expected_validity": "replay-reachable"},
                    "actions": [
                        {"type": "openra_wait_ticks", "ticks": "integer"},
                        {"type": "openra_deploy_actor", "player": "player id", "actor_id": "named actor id", "wait_until_actor": "actor type", "timeout_ticks": "integer"},
                        {"type": "openra_build_actor", "player": "player id", "actor": "actor type", "queue": "Building|Vehicle|Infantry", "place_at": "optional x,y cell", "place_near": {"anchor": "x,y", "radius": "integer"}, "timeout_ticks": "integer"},
                        {"type": "openra_build_group", "player": "player id", "units": [{"actor": "actor type", "queue": "Vehicle|Infantry", "count": "positive integer"}], "timeout_ticks": "integer"},
                        {"type": "openra_rally_group", "player": "player id", "actors": [{"actor": "actor type", "count": "positive integer"}], "center": "x,y", "radius": "integer", "timeout_ticks": "integer"},
                    ],
                    "assertions": [
                        {"type": "openra_actor_built", "player": "player id", "actor": "actor type", "min_delta": "integer"},
                        {"type": "openra_actor_group_in_region", "player": "player id", "actors": [{"actor": "actor type", "count": "positive integer"}], "center": "x,y", "radius": "integer"},
                        {"type": "openra_actor_exists", "player": "player id", "actor": "actor type OR actor_id: named actor id"},
                        {"type": "openra_production_prerequisite_respected", "player": "player id", "actor": "actor type"},
                        {"type": "no_openra_gameplay_probe_errors"},
                    ],
                },
                "payload_kind": "openra_gameplay_probe",
            },
        },
        "selection_rule": "Use map_validation for map/lobby/objective checks; use gameplay_production when the public issue asks to deploy, build, produce, or validate production queues.",
    },
}


C1_BENCHMARK_CONTRACTS: dict[str, dict[str, Any]] = {
    "OpenRA": {
        "generated_spec": {
            "name": "non-empty string",
            "benchmark": "OpenRA",
            "target": {
                "kind": "openra_settings",
                "mod": "ra",
                "settings": {
                    "Player|Game": {
                        "allowlisted public setting key": "concrete scalar start value"
                    }
                },
                "_note": (
                    "Use only Player.Name or Game.ViewportEdgeScroll, TargetLines, "
                    "StatusBars, IntroductionPromptVersion. The target is the start state."
                ),
            },
            "checkpoint": {
                "allowed_strategies": ["openra_public_settings_restore"],
                "expected_validity": "save-restorable",
            },
            "replay_from_start": {
                "actions": [
                    {
                        "type": "openra_open_settings_panel",
                        "panel": "Input|Display|Game",
                    }
                ]
            },
            "actions": [
                {
                    "type": "openra_set_setting",
                    "section": "Player|Game",
                    "key": "allowlisted public setting key",
                    "value": "concrete scalar goal value",
                }
            ],
            "assertions": [
                {
                    "type": "openra_setting_equals",
                    "section": "Player|Game",
                    "key": "same setting key changed by the action",
                    "expected": "same concrete goal value used by the action",
                },
                {"type": "no_openra_errors"},
            ],
        },
        "payload_kind": "openra_public_settings_checkpoint",
        "selection_rule": (
            "This is the only valid OpenRA C1 shape. Do not emit openra_map, "
            "openra_validate_map, openra_gameplay_probe, or production actions."
        ),
    },
}


LAZARUS_OPENRA_HOTKEY_CONTRACT: dict[str, Any] = {
    "generated_spec": {
        "name": "non-empty string",
        "benchmark": "OpenRA",
        "target": {
            "kind": "openra_map",
            "mod": "ra",
            "map": "allies-08a",
        },
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
    },
    "payload_kind": "openra_gameplay_probe",
    "selection_rule": (
        "This exact Lazarus task has one public semantic Start shape. Emit the "
        "allies-08a target and exactly one completed_structure Buildings action; "
        "do not emit map-validation or generic gameplay setup fields."
    ),
}


def lazarus_luanti_contract(start: LuantiPhase0Start) -> dict[str, Any]:
    """Return the exact checkpoint contract one Luanti Lazarus task must satisfy."""

    return {
        "generated_spec": start.generated_spec(),
        "payload_kind": "luanti_worldmod_checkpoint",
        "selection_rule": start.selection_rule,
    }


LAZARUS_LUANTI_VECTOR_CONTRACT: dict[str, Any] = lazarus_luanti_contract(
    LUANTI_VECTOR_START
)


def _benchmark_contract_for_task(task: PublicIssue) -> dict[str, Any]:
    if getattr(task, "prompt_revision", "") == "lzp-generated-state-v3":
        from .lazarus_p2p_only_spec import public_contract
        from .lazarus_p2p_only_schema import checkpoint_schema
        contract = public_contract(task.benchmark)
        return {"checkpoint_json_schema": checkpoint_schema(task.id, task.benchmark),
                "payload_kind": contract["payload_kind"],
                "public_container_api_guidance": contract["container_api"]}

    benchmark_key = _legacy_benchmark_key(task.benchmark)
    luanti_start = _lazarus_luanti_start(task)
    if luanti_start is not None:
        return lazarus_luanti_contract(luanti_start)
    if _is_lazarus_openra_hotkey_task(task):
        return copy.deepcopy(LAZARUS_OPENRA_HOTKEY_CONTRACT)
    if (
        _is_c1_synthesis_task(task)
        and not _is_c1_openra_gameplay_task(task)
        and benchmark_key in C1_BENCHMARK_CONTRACTS
    ):
        contract = copy.deepcopy(C1_BENCHMARK_CONTRACTS[benchmark_key])
        generated = contract.get("generated_spec")
        if isinstance(generated, dict):
            generated["benchmark"] = task.benchmark
        return contract
    return copy.deepcopy(BENCHMARK_CONTRACTS.get(benchmark_key, {}))


def public_action_contract_for_task(task: PublicIssue) -> list[dict[str, Any]]:
    """Return only the public semantic-action schema for one C1 task.

    The minimal post-Start planner must not receive the checkpoint synthesizer's
    target examples, assertions, source vocabulary, or the task's previously
    generated actions.  This helper deliberately projects the existing public
    benchmark contract down to its action schema.  OpenRA deep-chain tasks are
    the one schema-variant exception: their actions live under the registered
    ``gameplay_production`` variant rather than at the contract top level.
    """

    if getattr(task, "prompt_revision", "") == "lzp-generated-state-v3":
        from .lazarus_p2p_only_schema import DRAFT, action_schema
        return [{"$schema": DRAFT, **item} for item in action_schema(task.benchmark)["oneOf"]]
    benchmark_key = _legacy_benchmark_key(task.benchmark)
    if benchmark_key == "Blender":
        return copy.deepcopy(list(BLENDER_PUBLIC_ACTION_CONTRACT))
    contract = _benchmark_contract_for_task(task)
    if _is_c1_openra_gameplay_task(task):
        variants = contract.get("schema_variants")
        variant = (
            variants.get("gameplay_production")
            if isinstance(variants, dict)
            else None
        )
        contract = variant if isinstance(variant, dict) else {}
    generated = contract.get("generated_spec")
    actions = generated.get("actions") if isinstance(generated, dict) else None
    if not isinstance(actions, list) or not actions or not all(
        isinstance(action, dict) and action for action in actions
    ):
        raise ValueError(
            f"no public semantic-action contract is registered for C1 task {task.id!r}"
        )
    return copy.deepcopy(actions)


def public_assertion_contract_for_task(task: PublicIssue) -> list[dict[str, Any]]:
    """Return only the public auxiliary-assertion schema for one C1 task.

    Source-transition agents may emit assertions as a planning aid, but those
    assertions are never authoritative grading evidence.  As with the action
    projection above, OpenRA gameplay tasks select their registered public
    ``gameplay_production`` schema variant.
    """

    contract = _benchmark_contract_for_task(task)
    if _is_c1_openra_gameplay_task(task):
        variants = contract.get("schema_variants")
        variant = (
            variants.get("gameplay_production")
            if isinstance(variants, dict)
            else None
        )
        contract = variant if isinstance(variant, dict) else {}
    generated = contract.get("generated_spec")
    assertions = (
        generated.get("assertions") if isinstance(generated, dict) else None
    )
    if not isinstance(assertions, list) or not assertions or not all(
        isinstance(assertion, dict) and assertion for assertion in assertions
    ):
        raise ValueError(
            "no public auxiliary-assertion contract is registered for C1 task "
            f"{task.id!r}"
        )
    return copy.deepcopy(assertions)


def build_llm_checkpoint_prompt(
    task: PublicIssue, located: dict[str, Any], *, feedback: dict[str, Any] | None = None
) -> str:
    contract = _benchmark_contract_for_task(task)
    if "payload_kind" in contract:
        # Nest under `payload: {kind: ...}` to match the real required top-level shape
        # (see build_explore_system_prompt for the same fix and why it matters).
        contract["payload"] = {"kind": contract.pop("payload_kind")}
    contract = _wrap_type_placeholders(contract)
    payload = {
        "task": task.public_payload(),
        "source_context": {
            "profile": located.get("profile"),
            "repo": located.get("repo"),
            "graph": located.get("graph"),
            "source_evidence": located.get("source_evidence"),
            "source_vocabulary": _source_vocabulary_for_task(task),
        },
        "output_contract": {
            "task_id": task.id,
            "benchmark": task.benchmark,
            "generated_spec": "WarpTest executable spec mapping for the issue. It must be an object, never a string.",
            "source_evidence": "Non-empty list of source paths/symbols/rationales used.",
            "payload": "Checkpoint payload with kind and concrete hook/save/localStorage details.",
            "runtime_validation": "Mapping with at least status and detail.",
            "validity_class": "One of save-restorable, replay-reachable, injection-only, unknown.",
        },
        "benchmark_contract": contract,
    }
    lines = [
        "Generate one WarpTest method checkpoint from the public issue and source evidence below.",
        "Use only the public task and source context in this prompt. Private grader data is unavailable.",
        "Return YAML only. Do not wrap in Markdown.",
        "Important: generated_spec must be a nested YAML mapping with name, benchmark, target, checkpoint, actions, and assertions.",
        "Fairness contract: target encodes ONLY the public Start checkpoint; actions encode the public Transition; infer assertions from that transition and repository source evidence. Hidden grader assertions are unavailable. Never place inferred after-state values into target.",
        "Important: generated_spec must not be a prose string. payload.kind must be the benchmark payload_kind from benchmark_contract.",
        "Top-level keys must be: task_id, benchmark, generated_spec, source_evidence, payload, runtime_validation, validity_class.",
        "Important: benchmark_contract describes field TYPES, not values. Angle-bracket "
        "placeholders like <integer> or <non-empty string> are type hints, not literal values "
        "-- you MUST replace every one of them with the concrete value you actually recovered. "
        "Writing the placeholder text itself (e.g. the literal string \"integer\") as a field's "
        "value is an error.",
    ]
    feedback_hint = _format_feedback(feedback)
    if feedback_hint is not None:
        lines.append(feedback_hint)
    lines.append(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False))
    return "\n".join(lines)


# Core fields a generated_spec must have to be executable at all (injectable via
# StateSession / graded as a real checkpoint). An empty mapping ``{}`` -- which a
# model can return just as easily as ``None`` when it produces no usable output --
# passes a bare ``isinstance(spec, dict)`` or ``is not None`` check but has none of
# these, so treating it as a real spec fails much later with an opaque low-level
# error instead of a clear, attributable synthesis failure.
REQUIRED_GENERATED_SPEC_KEYS = ("target", "checkpoint", "actions", "assertions")

_C1_GENERATED_SPEC_TARGET_KINDS: dict[str, set[str]] = {
    "2048": {"board"},
    "DatingSim": {"deterministic_state"},
    "SpaceRPG": {
        "flight",
        "station",
        "galaxy_map",
        "equipment_loadout",
        "debug_scenario",
        "state_patch",
    },
    "luanti": {"synthesized_world", "save_restore"},
    "OpenRA": {"openra_map"},
    "JupyterLab": {
        "notebook",
        "text_file",
        "workspace_layout",
        "terminal_session",
    },
    "LibreOffice": {
        "writer_document",
        "calc_spreadsheet",
        "impress_presentation",
    },
    "Blender": {"scene", "blend_file"},
    "jynew": {"synthesized_state"},
    "pal3": {"synthesized_state"},
}

_C1_GENERATED_SPEC_STRATEGIES: dict[str, str] = {
    "2048": "localStorage_restore_reload",
    "DatingSim": "test_hook_seeded_state",
    "SpaceRPG": "test_hook_jump_and_validate",
    "luanti": "luanti_worldmod_state_validate",
    "OpenRA": "openra_gameplay_production_validate",
    "JupyterLab": "jupyterlab_workspace_api_validate",
    "LibreOffice": "libreoffice_uno_document_validate",
    "Blender": "blender_python_scene_validate",
    "jynew": "jynew_utility_save_validate",
    "pal3": "pal3_console_save_validate",
}


def _love_start_projection_errors(target: Any) -> list[str]:
    """Reject Love targets whose validator projection describes the goal.

    ``patch`` is overlaid on DatingSim's initialized state and ``expect`` is
    what the runtime immediately validates after that injection.  Expected
    fields therefore have to match the effective Start, including initialized
    defaults omitted from ``patch``.  This is a public executable-contract
    invariant; it does not require or inspect a hidden oracle.
    """

    if not isinstance(target, dict):
        return []
    expect = target.get("expect")
    if not isinstance(expect, dict):
        return []
    patch = target.get("patch")
    patch = patch if isinstance(patch, dict) else {}

    def merge(base: Any, overlay: Any) -> Any:
        """Mirror DatingSim's object-recursive ``deepMerge`` for Start."""

        if not isinstance(overlay, dict):
            return copy.deepcopy(overlay)
        merged = copy.deepcopy(base) if isinstance(base, dict) else {}
        for key, value in overlay.items():
            merged[key] = merge(merged.get(key), value)
        return merged

    # Keep these public defaults aligned with DatingSim's ``INITIAL_STATE``
    # and its test-gated ``initialize`` hook before applying the model patch.
    effective_start = merge(
        {
            "player": {
                "resources": {
                    "money": 1000,
                    "energy": 100,
                    "health": 100,
                    "mood": 100,
                },
                "relationships": {},
                "title": "Love Novice",
            },
            "world": {
                "location": "home",
                "globalFlags": {
                    "warptestSeed": bool(target.get("seed", 1)),
                },
            },
        },
        patch,
    )
    player = (
        effective_start.get("player")
        if isinstance(effective_start.get("player"), dict)
        else {}
    )
    world = (
        effective_start.get("world")
        if isinstance(effective_start.get("world"), dict)
        else {}
    )
    errors: list[str] = []

    def compare(actual: Any, expected: Any, path: str) -> None:
        if isinstance(expected, dict):
            if not isinstance(actual, dict):
                errors.append(
                    "DatingSim target.expect must describe the effective "
                    f"Start value at {path}"
                )
                return
            for key in sorted(expected):
                compare(actual.get(key), expected[key], f"{path}.{key}")
            return
        if actual != expected:
            errors.append(
                "DatingSim target.expect must describe the effective Start "
                f"value at {path}"
            )

    if "resources" in expect:
        compare(player.get("resources"), expect["resources"], "player.resources")
    if "relationships" in expect:
        compare(
            player.get("relationships"),
            expect["relationships"],
            "player.relationships",
        )
    if "global_flags" in expect:
        compare(
            world.get("globalFlags"),
            expect["global_flags"],
            "world.globalFlags",
        )
    if "location" in expect:
        compare(world.get("location"), expect["location"], "world.location")
    if "player_title" in expect:
        compare(player.get("title"), expect["player_title"], "player.title")
    return errors


def generated_spec_contract_errors(
    spec: Any, *, task: PublicIssue | None = None
) -> list[str]:
    """Return executable-contract errors relevant at synthesis time.

    Historical synthesis tasks retain a permissive structural gate because their
    runtime/profile validator owns the detailed semantics. Formal C1 does not:
    every benchmark is rejected before restore unless its model-produced mapping
    satisfies the public executable schema, target kind, and checkpoint strategy.
    """

    if task is not None and getattr(task, "prompt_revision", "") == "lzp-generated-state-v3":
        from .lazarus_p2p_only_spec import spec_errors
        return spec_errors(spec, task.benchmark)
    if not isinstance(spec, dict) or not spec:
        return ["generated_spec must be a non-empty mapping"]
    missing = [key for key in REQUIRED_GENERATED_SPEC_KEYS if key not in spec]
    if missing:
        return ["generated_spec missing " + ", ".join(missing)]
    if task is None or not _is_c1_synthesis_task(task):
        return []
    contract_benchmark = _legacy_benchmark_key(task.benchmark)
    expected_kinds = _C1_GENERATED_SPEC_TARGET_KINDS.get(contract_benchmark)
    if contract_benchmark == "JupyterLab" and task.id in {
        "jupyterlab-sales-analysis-pipeline",
        "jupyterlab-debug-notebook-rerun",
        "jupyterlab-reproducible-experiment-workspace",
    }:
        expected_kinds = {"notebook"}
    if (
        contract_benchmark == "OpenRA"
        and not _is_c1_openra_gameplay_task(task)
        and not _is_lazarus_openra_hotkey_task(task)
    ):
        expected_kinds = {"openra_settings"}
    if expected_kinds is None:
        return [f"no public C1 generated-spec contract is registered for {task.benchmark!r}"]
    errors = validate_spec_schema(spec, f"<generated:{task.id}>")
    if _legacy_benchmark_key(spec.get("benchmark")) != contract_benchmark:
        errors.append(
            f"generated_spec.benchmark must be {task.benchmark!r} for this C1 task"
        )
    target = spec.get("target")
    actual_kind = target.get("kind") if isinstance(target, dict) else None
    if actual_kind not in expected_kinds:
        errors.append(
            "generated_spec.target.kind must be one of "
            f"{sorted(expected_kinds)!r} for this C1 task"
        )
    expected_strategy = _C1_GENERATED_SPEC_STRATEGIES.get(contract_benchmark)
    if (
        contract_benchmark == "OpenRA"
        and not _is_c1_openra_gameplay_task(task)
        and not _is_lazarus_openra_hotkey_task(task)
    ):
        expected_strategy = "openra_public_settings_restore"
    strategies = (spec.get("checkpoint") or {}).get("allowed_strategies")
    if expected_strategy is not None and strategies != [expected_strategy]:
        errors.append(
            "generated_spec.checkpoint.allowed_strategies must be "
            f"{[expected_strategy]!r} for this C1 task"
        )
    if contract_benchmark == "DatingSim":
        errors.extend(_love_start_projection_errors(target))
    if _is_lazarus_openra_hotkey_task(task):
        expected_target = {"kind": "openra_map", "mod": "ra", "map": "allies-08a"}
        actual_target = {
            key: target.get(key) if isinstance(target, dict) else None
            for key in expected_target
        }
        if actual_target != expected_target:
            errors.append(
                "generated_spec.target must identify the public allies-08a Red Alert map"
            )
        actions = spec.get("actions")
        expected_action = {
            "type": "openra_build_actor",
            "player": "local_player",
            "actor": "completed_structure",
            "queue": "Building",
        }
        if actions != [expected_action]:
            errors.append(
                "generated_spec.actions must contain exactly the public completed-structure "
                "Buildings Start action"
            )
        if "setup" in spec:
            errors.append(
                "generated_spec.setup is not part of the public Lazarus hotkey checkpoint"
            )
    luanti_start = _lazarus_luanti_start(task)
    if luanti_start is not None:
        target_nodes = target.get("nodes") if isinstance(target, dict) else None
        nodes_by_position = {
            (
                (node.get("pos") or {}).get("x"),
                (node.get("pos") or {}).get("y"),
                (node.get("pos") or {}).get("z"),
            ): node.get("node")
            for node in target_nodes or []
            if isinstance(node, dict) and isinstance(node.get("pos"), dict)
        }
        target_validations = (
            target.get("validations") if isinstance(target, dict) else None
        )
        validations_by_path = {
            validation.get("path"): validation.get("expected")
            for validation in target_validations or []
            if isinstance(validation, dict)
        }
        if (
            not isinstance(target, dict)
            or set(target) != {"kind", "nodes", "player", "validations"}
            or target.get("kind") != "synthesized_world"
            or nodes_by_position != dict(luanti_start.nodes)
            or validations_by_path != dict(luanti_start.validations)
            or target.get("player") != dict(luanti_start.player)
        ):
            errors.append(
                "generated_spec.target must reproduce the public fixed-seed Luanti marker Start"
            )
        if spec.get("actions") != [dict(luanti_start.action)]:
            errors.append(
                "generated_spec.actions must contain exactly the public registered action"
            )
    return errors


def is_usable_generated_spec(
    spec: Any, *, task: PublicIssue | None = None
) -> bool:
    """True if ``spec`` is a structurally usable (non-empty, well-shaped) checkpoint spec.

    Callers that gate on "did synthesis produce a usable model spec" must use this
    instead of a bare ``is None``/``is not None`` check, since an empty dict ``{}``
    or a dict missing the core executable fields is not None but is just as unusable
    as one.
    """
    return not generated_spec_contract_errors(spec, task=task)


# A ``key: value`` line whose value STARTS with a quote char. This is the shape
# that breaks YAML when a model writes free text like
# ``matched_symbol: "helion-reach"/"kuro-belt" system ids`` -- the value opens a
# double-quoted scalar but has trailing text after the closing quote, so PyYAML
# raises ``expected <block end>, but found '<scalar>'`` and the whole (otherwise
# perfect) checkpoint is discarded. Restricting the repair to leading-quote
# values keeps it conservative: clean scalars/flow collections never match.
_LEADING_QUOTE_VALUE_LINE = re.compile(r"""^(?P<pre>\s*(?:-\s+)?[^:\n]+:[ \t]+)(?P<val>["'].*\S)[ \t]*$""")


def _looks_like_broken_quoted_scalar(value: str) -> bool:
    """True if ``value`` opens a quote but does not parse as a clean scalar."""
    try:
        yaml.safe_load(value)
    except Exception:
        return True
    return False


def _repair_leading_quote_scalars(text: str) -> str | None:
    """Best-effort: re-quote ``key: "a"/"b" text`` free-text values as valid YAML.

    Only rewrites lines whose value begins with a quote AND fails to parse as a
    standalone scalar, replacing the value with a properly escaped double-quoted
    string (``json.dumps`` emits valid YAML double-quoted syntax). Returns the
    repaired text if anything changed, else ``None`` so the caller keeps the
    original parse error. Never raises.
    """
    changed = False
    out: list[str] = []
    for line in text.splitlines():
        match = _LEADING_QUOTE_VALUE_LINE.match(line)
        if match is not None:
            raw_value = match.group("val").strip()
            if _looks_like_broken_quoted_scalar(raw_value):
                out.append(f"{match.group('pre')}{json.dumps(raw_value)}")
                changed = True
                continue
        out.append(line)
    return "\n".join(out) if changed else None


def parse_llm_checkpoint_output(text: str) -> dict[str, Any]:
    body = text.strip()
    fence = re.search(r"```(?:yaml|yml|json)?\s*(.*?)```", body, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        body = fence.group(1).strip()
    try:
        data = yaml.safe_load(body)
    except Exception as error:
        # Common LLM failure: a free-text value embeds quote characters and
        # breaks YAML even though the executable ``generated_spec`` is fine.
        # Try one conservative repair pass before giving up.
        repaired = _repair_leading_quote_scalars(body)
        if repaired is not None:
            try:
                data = yaml.safe_load(repaired)
            except Exception:
                raise SynthesisError(
                    "LLM output did not parse as YAML.", artifacts={"parse_errors": [str(error)]}
                ) from error
        else:
            raise SynthesisError(
                "LLM output did not parse as YAML.", artifacts={"parse_errors": [str(error)]}
            ) from error
    if not isinstance(data, dict):
        raise SynthesisError("LLM output did not parse to a YAML mapping.", artifacts={"parse_errors": ["not a mapping"]})
    return data


_FORMAL_C1_CHECKPOINT_KEYS = {
    "task_id",
    "benchmark",
    "generated_spec",
    "source_evidence",
    "payload",
    "runtime_validation",
    "validity_class",
}


def formal_c1_checkpoint_contract_errors(
    data: Any, *, task: PublicIssue, ablation_variant: str = "full"
) -> list[str]:
    """Validate only the public, pre-restore C1 output contract."""

    validate_variant(ablation_variant)
    if getattr(task, "prompt_revision", "") == "lzp-generated-state-v3":
        from .lazarus_p2p_only_schema import checkpoint_schema, validate
        return [error.render() for error in validate(data, checkpoint_schema(task.id, task.benchmark))]
    if not isinstance(data, dict):
        return ["checkpoint output must be a mapping"]
    errors: list[str] = []
    extra = sorted(set(data) - _FORMAL_C1_CHECKPOINT_KEYS)
    missing = sorted(_FORMAL_C1_CHECKPOINT_KEYS - set(data))
    if missing:
        errors.append("missing top-level keys: " + ", ".join(missing))
    if extra:
        errors.append("illegal top-level keys: " + ", ".join(extra))
    if data.get("task_id") != task.id:
        errors.append(f"task_id must equal {task.id!r}")
    if data.get("benchmark") != task.benchmark:
        errors.append(f"benchmark must equal {task.benchmark!r}")
    with ablation_scope(ablation_variant):
        errors.extend(
            generated_spec_contract_errors(data.get("generated_spec"), task=task)
        )
    evidence = data.get("source_evidence")
    if ablation_variant == "no_source_exploration" and evidence != []:
        errors.append("source_evidence must be [] when source exploration is disabled")
    if ablation_variant == "no_assertion_prediction" and isinstance(data.get("generated_spec"), dict):
        if data["generated_spec"].get("assertions") != []:
            errors.append("generated_spec.assertions must be [] when assertion prediction is disabled")
    if not isinstance(evidence, list) or (not evidence and ablation_variant != "no_source_exploration"):
        errors.append("source_evidence must be a non-empty list")
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append(f"source_evidence[{index}] must be a mapping")
                continue
            for field in ("source_path", "matched_symbol", "rationale"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    errors.append(
                        f"source_evidence[{index}].{field} must be a non-empty string"
                    )
    payload = data.get("payload")
    expected_payload = (
        _payload_for_spec(data["generated_spec"])
        if isinstance(data.get("generated_spec"), dict)
        else {}
    )
    if not isinstance(payload, dict):
        errors.append("payload must be a mapping")
    elif payload.get("kind") != expected_payload.get("kind"):
        errors.append(
            f"payload.kind must equal {expected_payload.get('kind')!r}"
        )
    runtime_validation = data.get("runtime_validation")
    if not isinstance(runtime_validation, dict):
        errors.append("runtime_validation must be a mapping")
    else:
        if not isinstance(runtime_validation.get("status"), str) or not str(
            runtime_validation.get("status")
        ).strip():
            errors.append("runtime_validation.status must be a non-empty string")
        if not isinstance(runtime_validation.get("detail"), str):
            errors.append("runtime_validation.detail must be a string")
    validity = data.get("validity_class")
    expected_validity = (
        (data.get("generated_spec") or {}).get("checkpoint", {}).get(
            "expected_validity"
        )
        if isinstance(data.get("generated_spec"), dict)
        else None
    )
    if validity != expected_validity:
        errors.append(
            "validity_class must equal "
            "generated_spec.checkpoint.expected_validity"
        )
    return errors


def _normalize_lazarus_public_envelope(
    data: dict[str, Any], *, task: PublicIssue
) -> list[str]:
    """Normalize redundant Lazarus envelope fields from public information.

    Frozen C1 remains strict.  Lazarus uses the same public checkpoint shape,
    but its model occasionally repeats the generated spec's display benchmark
    (``OpenRA``) at the lower-case task envelope or leaves ``payload.kind`` as
    ``None`` even though the selected public schema variant determines it.
    Neither field adds task semantics, so correcting only these two aliases is
    a structure-only repair and is recorded explicitly.
    """

    tags = getattr(task, "tags", ())
    normalized_tags = {
        str(tag).strip().lower()
        for tag in tags
        if isinstance(tag, str)
    }
    if "lazarus" not in normalized_tags:
        return []
    warnings: list[str] = []
    if getattr(task, "prompt_revision", "") == "lzp-generated-state-v3":
        evidence = data.get("source_evidence")
        if isinstance(evidence, list):
            for index, item in enumerate(evidence):
                if (isinstance(item, dict)
                    and set(item) == {"source_path", "matched_symbol", "rationale"}
                    and item["source_path"] == "Public issue state description"):
                    # This exact legacy sentinel identifies the supplied issue,
                    # not a repository file. Never reinterpret arbitrary paths.
                    evidence[index] = {"kind": "public_task", "field": "issue",
                                       "matched_symbol": item["matched_symbol"], "rationale": item["rationale"]}
                    warnings.append(f"lazarus_public_task_evidence_typed[{index}]")
    benchmark = data.get("benchmark")
    if (
        isinstance(benchmark, str)
        and benchmark != task.benchmark
        and benchmark.strip().lower() == str(task.benchmark).strip().lower()
    ):
        data["benchmark"] = task.benchmark
        warnings.append("lazarus_benchmark_case_normalized")
    generated_spec = data.get("generated_spec")
    payload = data.get("payload")
    if isinstance(generated_spec, dict) and isinstance(payload, dict):
        expected_kind = _payload_for_spec(generated_spec).get("kind")
        current_kind = payload.get("kind")
        if (
            isinstance(expected_kind, str)
            and expected_kind
            and (
                current_kind is None
                or (
                    isinstance(current_kind, str)
                    and current_kind.strip().lower() in {"", "none", "null"}
                )
            )
        ):
            payload["kind"] = expected_kind
            warnings.append("lazarus_payload_kind_derived_from_generated_spec")
    if isinstance(generated_spec, dict):
        setup = generated_spec.get("setup")
        gameplay = (
            setup.get("openra_gameplay_probe")
            if isinstance(setup, dict)
            else None
        )
        removals = (
            gameplay.get("remove_player_actor_types")
            if isinstance(gameplay, dict)
            else None
        )
        if isinstance(removals, dict):
            pruned = {
                key: value
                for key, value in removals.items()
                if not (isinstance(value, list) and not value)
            }
            if pruned != removals:
                if pruned:
                    gameplay["remove_player_actor_types"] = pruned
                else:
                    gameplay.pop("remove_player_actor_types", None)
                warnings.append("lazarus_empty_actor_removal_lists_pruned")
    return warnings


def build_checkpoint_format_repair_prompt(
    task: PublicIssue,
    raw_output: str,
    errors: list[str],
    *,
    ablation_variant: str = "full",
) -> str:
    """Build the sole fair format-repair request from public material only."""

    contract = public_contract_view(_benchmark_contract_for_task(task), ablation_variant)
    if "payload_kind" in contract:
        contract = {
            **contract,
            "payload": {"kind": contract["payload_kind"]},
        }
        contract.pop("payload_kind", None)
    payload = {
        "public_task": task.public_payload(),
        "public_output_contract": _wrap_type_placeholders(contract),
        "public_action_schema": public_contract_view(public_action_contract_for_task(task), ablation_variant),
        "structural_errors": list(errors),
        "initial_output": raw_output,
    }
    if getattr(task, "prompt_revision", "") == "lzp-generated-state-v3":
        from .lazarus_p2p_only_schema import checkpoint_schema, preserve_valid_fields
        try:
            original = parse_llm_checkpoint_output(raw_output)
        except SynthesisError:
            protected = []
        else:
            _normalize_lazarus_public_envelope(original, task=task)
            _, protected = preserve_valid_fields(original, {}, checkpoint_schema(task.id, task.benchmark))
        payload["repair_policy"] = {
            "protected_fields": protected,
            "rule": "Change invalid fields only. Keep all already-valid state, actions, assertions and evidence unchanged. Array entries retain their order. The coordinator restores protected values before validation.",
            "schema_kind": "JSON Schema: emit a concrete checkpoint instance, not the schema object or prose descriptions",
        }
    return "\n".join(
        [
            "Repair the initial checkpoint output into one valid YAML mapping.",
            "This is a structure-only repair. Preserve the initial output's public "
            "semantics; do not invent task facts or substitute a canonical answer.",
            "Use only the public task, public schema, structural errors, and initial "
            "output below. Oracle, golden, grader, restore, and runtime feedback are "
            "not available.",
            "Return YAML only with exactly these top-level keys: task_id, benchmark, "
            "generated_spec, source_evidence, payload, runtime_validation, validity_class.",
            ("generated_spec must be a mapping; source_evidence must be []; "
             if ablation_variant == "no_source_exploration" else
             "generated_spec must be a mapping; source_evidence a non-empty list; ") +
            "runtime_validation a mapping with status and detail. Remove Markdown "
            "fences, quote colon-containing prose, and remove illegal fields.",
            "For every generated_spec action, follow public_action_schema exactly: "
            "supply all required fields and remove unknown fields.",
            *([policy_instruction(ablation_variant)] if policy_instruction(ablation_variant) else []),
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        ]
    )


@scoped_output_policy
def repair_formal_c1_checkpoint_once(
    *,
    task: PublicIssue,
    raw_output: str,
    initial_errors: list[str],
    client: Any,
    model: str,
    located: dict[str, Any],
    root: str | Path,
    artifact_dir: Path | None,
    source_cost_kind: str,
    ablation_variant: str = "full",
) -> tuple[MethodCheckpoint, dict[str, Any]]:
    """Run the one allowed same-model, public-only C1 format repair."""

    prompt = build_checkpoint_format_repair_prompt(
        task, raw_output, initial_errors, ablation_variant=ablation_variant
    )
    prompt_leaks = detect_private_grader_references(prompt)
    if prompt_leaks:
        raise SynthesisError(
            "C1 format-repair prompt references private grader data.",
            artifacts={"repair_prompt_private_references": prompt_leaks},
        )
    repair_dir = artifact_dir / "format-repair" if artifact_dir is not None else None
    if repair_dir is not None:
        repair_dir.mkdir(parents=True, exist_ok=True)
        (repair_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    marker = cost.mark_records()
    call = client.converse(
        model,
        [
            {
                "role": "system",
                "content": (
                    "You repair WarpTest checkpoint YAML structure without adding "
                    "task semantics. Return only one YAML mapping."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        empty_retries=1,
    )
    cost.reclassify_since(
        marker,
        model=model,
        from_kind=source_cost_kind,
        to_kind="checkpoint_synthesis_repair",
    )
    repaired_raw = call.content
    if repair_dir is not None:
        (repair_dir / "response.txt").write_text(
            repaired_raw, encoding="utf-8"
        )
    metadata = {
        "attempted": True,
        "succeeded": False,
        "initial_errors": list(initial_errors),
        "remaining_errors": ["repair response has not passed validation"],
        "normalization_warnings": [],
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "initial_output_sha256": hashlib.sha256(
            raw_output.encode("utf-8")
        ).hexdigest(),
        "response_sha256": hashlib.sha256(
            repaired_raw.encode("utf-8")
        ).hexdigest(),
        "latency_ms": int(call.latency_ms),
        "usage": {
            "prompt_tokens": int(call.prompt_tokens),
            "completion_tokens": int(call.completion_tokens),
            "reasoning_tokens": call.reasoning_tokens,
            "cached_prompt_tokens": int(call.cached_prompt_tokens),
            "total_tokens": int(call.total_tokens),
        },
    }
    if repair_dir is not None:
        write_json(repair_dir / "meta.json", metadata)
    try:
        parsed = parse_llm_checkpoint_output(repaired_raw)
        output_leaks = detect_private_grader_references(parsed)
        if output_leaks:
            raise SynthesisError(
                "C1 format-repair output references private grader data.",
                artifacts={"repair_output_private_references": output_leaks},
            )
        envelope_warnings = _normalize_lazarus_public_envelope(parsed, task=task)
        if getattr(task, "prompt_revision", "") == "lzp-generated-state-v3":
            from .lazarus_p2p_only_schema import checkpoint_schema, preserve_valid_fields
            try:
                original = parse_llm_checkpoint_output(raw_output)
            except SynthesisError:
                metadata["valid_field_preservation"] = {"available": False, "reason": "initial YAML is not parseable"}
            else:
                original_warnings = _normalize_lazarus_public_envelope(original, task=task)
                parsed, protected = preserve_valid_fields(original, parsed, checkpoint_schema(task.id, task.benchmark))
                metadata["valid_field_preservation"] = {"available": True, "restored_fields": protected,
                                                        "initial_normalization": original_warnings}
            metadata["protected_candidate_sha256"] = hashlib.sha256(
                json.dumps(parsed, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            if repair_dir is not None:
                write_json(repair_dir / "protected-candidate.json", parsed)
        remaining_errors = formal_c1_checkpoint_contract_errors(
            parsed, task=task, ablation_variant=ablation_variant
        )
        if remaining_errors:
            raise SynthesisError(
                "C1 format repair still violates the public checkpoint contract.",
                artifacts={
                    "repair_contract_errors": remaining_errors,
                    "repair_attempted": True,
                },
            )
        normalized, warnings = normalize_llm_checkpoint_mapping(parsed, task=task)
        checkpoint = method_checkpoint_from_llm_mapping(
            normalized,
            task=task,
            located=located,
            model=model,
            model_generated_spec=copy.deepcopy(parsed["generated_spec"]),
            used_public_scaffold=False,
        )
        schema_errors = validate_method_checkpoint(
            checkpoint, root,
            allow_empty_source_evidence=ablation_variant == "no_source_exploration",
        )
        if schema_errors:
            raise SynthesisError(
                "C1 format repair failed public method-checkpoint validation.",
                artifacts={
                    "repair_schema_errors": schema_errors,
                    "repair_attempted": True,
                },
            )
    except SynthesisError as error:
        remaining = []
        for key in (
            "parse_errors",
            "repair_contract_errors",
            "repair_schema_errors",
            "repair_output_private_references",
        ):
            values = error.artifacts.get(key)
            if isinstance(values, list):
                remaining.extend(str(item) for item in values)
        metadata.update(
            {
                "failure": str(error),
                "remaining_errors": remaining or [str(error)],
            }
        )
        if repair_dir is not None:
            write_json(repair_dir / "meta.json", metadata)
        error.artifacts.update(
            {
                "repair_attempted": True,
                "format_repair": metadata,
            }
        )
        raise
    if getattr(task, "prompt_revision", "") == "lzp-generated-state-v3":
        metadata["accepted_mapping_sha256"] = hashlib.sha256(
            json.dumps(parsed, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        if repair_dir is not None:
            write_json(repair_dir / "accepted-mapping.json", parsed)
    metadata.update(
        {
            "succeeded": True,
            "remaining_errors": [],
            "normalization_warnings": [*envelope_warnings, *warnings],
        }
    )
    if repair_dir is not None:
        write_json(repair_dir / "meta.json", metadata)
    return checkpoint, metadata


@scoped_output_policy
def finalize_formal_c1_model_output(
    *,
    task: PublicIssue,
    raw_output: str,
    client: Any,
    model: str,
    located: dict[str, Any],
    root: str | Path,
    artifact_dir: Path | None,
    source_cost_kind: str,
    ablation_variant: str = "full",
) -> tuple[MethodCheckpoint, dict[str, Any]]:
    """Validate an initial C1 output and invoke at most one public format repair."""

    initial_errors: list[str] = []
    normalization_warnings: list[str] = []
    parsed: dict[str, Any] | None = None
    try:
        parsed = parse_llm_checkpoint_output(raw_output)
    except SynthesisError as error:
        initial_errors.extend(
            str(item)
            for item in error.artifacts.get("parse_errors", [str(error)])
        )
    if parsed is not None:
        leaks = detect_private_grader_references(parsed)
        if leaks:
            raise SynthesisError(
                "Formal C1 checkpoint references private grader data.",
                artifacts={"output_private_references": leaks},
            )
        normalization_warnings.extend(
            _normalize_lazarus_public_envelope(parsed, task=task)
        )
        initial_errors.extend(
            formal_c1_checkpoint_contract_errors(parsed, task=task, ablation_variant=ablation_variant)
        )
    checkpoint: MethodCheckpoint | None = None
    if parsed is not None and not initial_errors:
        try:
            normalized, mapping_warnings = normalize_llm_checkpoint_mapping(
                parsed, task=task
            )
            normalization_warnings.extend(mapping_warnings)
            checkpoint = method_checkpoint_from_llm_mapping(
                normalized,
                task=task,
                located=located,
                model=model,
                model_generated_spec=copy.deepcopy(parsed["generated_spec"]),
                used_public_scaffold=False,
            )
            initial_errors.extend(validate_method_checkpoint(
                checkpoint, root,
                allow_empty_source_evidence=ablation_variant == "no_source_exploration",
            ))
        except SynthesisError as error:
            initial_errors.append(str(error))
            for key in (
                "parse_errors",
                "generated_spec_contract_errors",
                "schema_errors",
            ):
                values = error.artifacts.get(key)
                if isinstance(values, list):
                    initial_errors.extend(str(item) for item in values)
    if checkpoint is not None and not initial_errors:
        return checkpoint, {
            "initial_contract_valid": True,
            "initial_errors": [],
            "normalization_warnings": normalization_warnings,
            "repair": {"attempted": False, "succeeded": None},
        }
    repaired, repair_metadata = repair_formal_c1_checkpoint_once(
        task=task,
        raw_output=raw_output,
        initial_errors=initial_errors or ["initial output was not executable"],
        client=client,
        model=model,
        located=located,
        root=root,
        artifact_dir=artifact_dir,
        source_cost_kind=source_cost_kind,
        ablation_variant=ablation_variant,
    )
    return repaired, {
        "initial_contract_valid": False,
        "initial_errors": initial_errors,
        "normalization_warnings": [],
        "repair": repair_metadata,
    }


def method_checkpoint_from_llm_mapping(
    data: dict[str, Any],
    *,
    task: PublicIssue,
    located: dict[str, Any],
    model: str,
    model_generated_spec: dict[str, Any] | None = None,
    used_public_scaffold: bool = False,
) -> MethodCheckpoint:
    missing = [key for key in ["generated_spec", "source_evidence", "payload", "runtime_validation", "validity_class"] if key not in data]
    if missing:
        raise SynthesisError(f"LLM output is missing method checkpoint keys: {', '.join(missing)}", artifacts={"parse_errors": missing})
    generated_spec = data.get("generated_spec")
    source_evidence = data.get("source_evidence")
    payload = data.get("payload")
    runtime_validation = data.get("runtime_validation")
    if not isinstance(generated_spec, dict):
        raise SynthesisError("LLM generated_spec must be a mapping.", artifacts={"parse_errors": ["generated_spec"]})
    if not isinstance(source_evidence, list):
        raise SynthesisError("LLM source_evidence must be a list.", artifacts={"parse_errors": ["source_evidence"]})
    if not isinstance(payload, dict):
        raise SynthesisError("LLM payload must be a mapping.", artifacts={"parse_errors": ["payload"]})
    if not isinstance(runtime_validation, dict):
        raise SynthesisError("LLM runtime_validation must be a mapping.", artifacts={"parse_errors": ["runtime_validation"]})
    return MethodCheckpoint(
        task_id=str(data.get("task_id") or task.id),
        benchmark=str(data.get("benchmark") or task.benchmark),
        generated_spec=generated_spec,
        source_evidence=source_evidence,
        payload=payload,
        validity_class=str(data.get("validity_class") or "unknown"),
        runtime_validation=runtime_validation,
        inputs_read=[item for item in located.get("inputs_read", []) if item],
        hidden_files_read=[str(item) for item in data.get("hidden_files_read", [])] if isinstance(data.get("hidden_files_read"), list) else [],
        generated_by=model,
        model_generated_spec=model_generated_spec,
        used_public_scaffold=used_public_scaffold,
    )


def public_scaffold_checkpoint(
    task: PublicIssue,
    *,
    located: dict[str, Any],
    model: str,
    reason: str,
    raw_model_output: str | None = None,
) -> MethodCheckpoint:
    """Fallback for unusable model output (unparseable, missing keys, API failure).

    If a public-task scaffold template exists, it is used to validate runtime
    infrastructure (``used_public_scaffold=True``). If no template exists for
    this task, there is nothing to fall back to; the checkpoint carries an
    empty, non-executable ``generated_spec`` and ``model_generated_spec=None``
    so callers correctly treat this as a genuine failed synthesis (fair 0)
    rather than crashing or silently injecting a scaffold that does not exist.
    """
    try:
        generated_spec = synthesize_spec_from_public_issue(task)
        checkpoint = generated_spec.get("checkpoint", {})
        validity: CheckpointValidity = checkpoint.get("expected_validity", "unknown")
        used_public_scaffold = True
        runtime_detail = "Model output was not executable; public scaffold will be used only to validate runtime infrastructure."
    except ValueError:
        generated_spec = {}
        validity = "unknown"
        used_public_scaffold = False
        reason = f"{reason}; no public scaffold template available for public task {task.id}"
        runtime_detail = "Model output was not executable and no public scaffold template exists for this task."
    return MethodCheckpoint(
        task_id=task.id,
        benchmark=task.benchmark,
        generated_spec=generated_spec,
        source_evidence=_fallback_source_evidence(task, located, reason),
        payload={
            **_payload_for_spec(generated_spec),
            "model_fallback_reason": reason,
            **({"raw_model_output_excerpt": raw_model_output[:500]} if raw_model_output else {}),
        },
        validity_class=validity,
        runtime_validation={
            "status": "pending",
            "detail": runtime_detail,
            "model_fallback_reason": reason,
        },
        inputs_read=[item for item in located.get("inputs_read", []) if item],
        hidden_files_read=[],
        generated_by=model,
        model_generated_spec=None,
        used_public_scaffold=used_public_scaffold,
    )


def _fallback_source_evidence(task: PublicIssue, located: dict[str, Any], reason: str) -> list[dict[str, Any]]:
    evidence = located.get("source_evidence")
    if isinstance(evidence, list) and evidence:
        return [item for item in evidence if isinstance(item, dict)] or [{"source_path": located.get("repo", ""), "matched_symbol": task.benchmark, "rationale": reason}]

    repo = str(located.get("repo") or "")
    items = []
    for value in located.get("inputs_read", []):
        if not value:
            continue
        path = str(value)
        if path == task.source_path or path == repo:
            continue
        items.append({"repo": repo, "source_path": path, "matched_symbol": task.benchmark, "confidence": 0.35, "rationale": reason})
        if len(items) >= 5:
            break
    if items:
        return items
    return [{"repo": repo, "source_path": repo, "matched_symbol": task.benchmark, "confidence": 0.25, "rationale": reason}]


def must_raise_llm_error(error: SynthesisError) -> bool:
    failure_mode = error.artifacts.get("failure_mode")
    return (
        "API key is required" in str(error)
        or getattr(error, "usage_accounting_complete", True) is False
        or failure_mode
        in {
            "gateway_outage",
            "usage_accounting_incomplete",
            "client_error",
            "provider_capacity",
        }
    )


def normalize_llm_checkpoint_mapping(data: dict[str, Any], *, task: PublicIssue) -> tuple[dict[str, Any], list[str]]:
    """Repair common LLM format drift without reading hidden grader files.

    ModelScope-compatible models often return a good public explanation plus
    source evidence but put it in a prose generated_spec. Where a public-task
    scaffold exists it is used only to repair unusable output (a prose string,
    or a dict that diverges from the executable contract); a well-formed model
    spec is otherwise kept as-is so callers that want the model's own synthesis
    (rather than the canonicalized scaffold) can use it. Not every public task
    has a scaffold template (``synthesize_spec_from_public_issue`` raises
    ``ValueError`` in that case); this must not crash normalization -- it only
    means scaffold-based repair is unavailable for this task. The raw model
    response remains stored next to the prompt for audit either way.
    """

    normalized = copy.deepcopy(data)
    warnings: list[str] = []
    generated_spec = normalized.get("generated_spec")
    formal_c1 = _is_c1_synthesis_task(task)
    if isinstance(generated_spec, dict):
        # Reject a generated_spec that merely echoes the benchmark-contract's type-name
        # hints (e.g. writing the literal string "integer" where a real recovered score
        # belongs) as unusable, rather than letting it pass structural validation and
        # fail much later at restore time with an opaque low-level error. This must raise
        # (not silently repair) so the caller's SynthesisError handling correctly leaves
        # model_generated_spec as None -- an echoed placeholder is not a real synthesis.
        echoes = find_placeholder_echoes(generated_spec)
        if echoes:
            raise SynthesisError(
                "LLM generated_spec echoes unfilled type-hint placeholder(s) instead of "
                f"concrete values: {', '.join(echoes)}.",
                artifacts={"parse_errors": ["generated_spec"], "placeholder_echo_paths": echoes},
            )
        # Native C1 output is itself the measured synthesis result. Do not hide a
        # malformed Luanti/OpenRA model spec by replacing it with a deterministic
        # public scaffold before the executable contract is checked.
        if formal_c1:
            raw_contract_errors = generated_spec_contract_errors(
                generated_spec, task=task
            )
            if raw_contract_errors:
                raise SynthesisError(
                    "LLM generated_spec is incompatible with the public C1 synthesis contract.",
                    artifacts={
                        "parse_errors": ["generated_spec"],
                        "generated_spec_contract_errors": raw_contract_errors,
                    },
                )
    if formal_c1:
        scaffold = None
    else:
        try:
            scaffold = synthesize_spec_from_public_issue(task)
        except ValueError:
            scaffold = None
            warnings.append("no_public_scaffold_template_for_task")
    if not isinstance(generated_spec, dict):
        if formal_c1:
            raise SynthesisError(
                "Formal C1 generated_spec must be the model's own mapping; public "
                "scaffold substitution is forbidden.",
                artifacts={"parse_errors": ["generated_spec"]},
            )
        if scaffold is None:
            raise SynthesisError(
                "LLM generated_spec is not a mapping and no public scaffold template "
                "exists for this task to repair it.",
                artifacts={"parse_errors": ["generated_spec"]},
            )
        generated_spec = scaffold
        if isinstance(normalized.get("generated_spec"), str):
            generated_spec["llm_generated_spec_text"] = normalized["generated_spec"]
            warnings.append("generated_spec_string_wrapped_with_public_task_scaffold")
        else:
            warnings.append("generated_spec_replaced_with_public_task_scaffold")
        normalized["generated_spec"] = generated_spec
    elif scaffold is not None:
        if not _execution_spec_matches_public_scaffold(generated_spec, scaffold):
            repaired = copy.deepcopy(scaffold)
            repaired["llm_generated_spec_raw"] = generated_spec
            normalized["generated_spec"] = repaired
            warnings.append("generated_spec_canonicalized_with_public_task_scaffold")
    # else: generated_spec is already a well-formed mapping and no scaffold exists
    # to canonicalize against -- keep the model's own spec untouched.

    payload = normalized.get("payload")
    if not isinstance(payload, dict):
        if formal_c1:
            raise SynthesisError(
                "Formal C1 payload must be a mapping.",
                artifacts={"parse_errors": ["payload"]},
            )
        # Common LLM drift: a flat "payload_kind" sibling key instead of the required
        # nested "payload: {kind: ...}" mapping (the benchmark-contract schema hint used
        # to render "payload_kind:" as a bare top-level key, which some models mirror
        # even after the prompt was clarified). Repair it rather than hard-failing.
        stray_kind = normalized.get("payload_kind")
        if isinstance(stray_kind, str) and stray_kind:
            payload = {"kind": stray_kind}
            normalized["payload"] = payload
            warnings.append("payload_wrapped_from_stray_payload_kind_key")
        else:
            raise SynthesisError("LLM payload must be a mapping.", artifacts={"parse_errors": ["payload"]})
    expected_payload = _payload_for_spec(normalized["generated_spec"])
    payload_kind_incompatible = (
        _is_c1_synthesis_task(task)
        and _legacy_benchmark_key(task.benchmark) in C1_BENCHMARK_CONTRACTS
        and payload.get("kind") != expected_payload.get("kind")
    )
    if formal_c1 and (
        payload.get("kind") != expected_payload.get("kind")
    ):
        raise SynthesisError(
            "Formal C1 payload.kind does not match the public benchmark contract.",
            artifacts={"parse_errors": ["payload.kind"]},
        )
    if not formal_c1 and (
        payload.get("kind") in {None, "", "checkpoint", "save", "hook"}
        or payload_kind_incompatible
    ):
        normalized["payload"] = {**expected_payload, "llm_payload_raw": payload}
        warnings.append("payload_kind_normalized_to:" + str(expected_payload.get("kind")))
    if not isinstance(normalized.get("runtime_validation"), dict):
        if formal_c1:
            raise SynthesisError(
                "Formal C1 runtime_validation must be a mapping.",
                artifacts={"parse_errors": ["runtime_validation"]},
            )
        normalized["runtime_validation"] = {
            "status": "pending",
            "detail": "Model supplied runtime_validation in a non-mapping form; normalized before runtime execution.",
            "model_runtime_validation_raw": normalized.get("runtime_validation"),
        }
        warnings.append("runtime_validation_scalar_normalized_to_pending_mapping")
    if not normalized["runtime_validation"].get("status"):
        if formal_c1:
            raise SynthesisError(
                "Formal C1 runtime_validation.status is required.",
                artifacts={"parse_errors": ["runtime_validation.status"]},
            )
        normalized["runtime_validation"]["status"] = "pending"
        warnings.append("runtime_validation_status_defaulted_to_pending")
    if formal_c1 and not isinstance(normalized["runtime_validation"].get("detail"), str):
        raise SynthesisError(
            "Formal C1 runtime_validation.detail must be a string.",
            artifacts={"parse_errors": ["runtime_validation.detail"]},
        )
    if not formal_c1:
        _normalize_generated_spec_schema_drift(normalized["generated_spec"], warnings)
    contract_errors = generated_spec_contract_errors(
        normalized["generated_spec"], task=task
    )
    if contract_errors:
        raise SynthesisError(
            "LLM generated_spec is incompatible with the public C1 synthesis contract.",
            artifacts={
                "parse_errors": ["generated_spec"],
                "generated_spec_contract_errors": contract_errors,
            },
        )
    checkpoint = normalized["generated_spec"].get("checkpoint", {})
    expected_validity = checkpoint.get("expected_validity", "unknown")
    if (
        formal_c1
        and normalized.get("validity_class") != expected_validity
    ):
        raise SynthesisError(
            "Formal C1 validity_class must match generated_spec.checkpoint.expected_validity.",
            artifacts={"parse_errors": ["validity_class"]},
        )
    if expected_validity in {"replay-reachable", "save-restorable", "injection-only", "unknown"} and normalized.get("validity_class") != expected_validity:
        normalized["validity_class"] = expected_validity
        warnings.append("validity_class_normalized_from_generated_spec")
    return normalized, warnings


def _execution_spec_matches_public_scaffold(spec: dict[str, Any], scaffold: dict[str, Any]) -> bool:
    execution_keys = ["benchmark", "setup", "target", "checkpoint", "actions", "assertions"]
    return {key: spec.get(key) for key in execution_keys} == {key: scaffold.get(key) for key in execution_keys}


def _normalize_generated_spec_schema_drift(spec: dict[str, Any], warnings: list[str]) -> None:
    assertions = spec.get("assertions")
    if not isinstance(assertions, list):
        return
    for assertion in assertions:
        if not isinstance(assertion, dict):
            continue
        if assertion.get("type") == "score_increased_by_at_least" and "amount" not in assertion and "min_increase" in assertion:
            assertion["amount"] = assertion["min_increase"]
            warnings.append("assertion_score_min_increase_normalized_to_amount")


def detect_private_grader_references(value: Any) -> list[str]:
    patterns = [
        "benchmarks/core/hidden",
        "benchmarks/segments/hidden",
        "hidden_spec",
        "hidden target",
        "hidden oracle",
        "private oracle",
    ]
    found: list[str] = []

    def scan(item: Any) -> None:
        if isinstance(item, dict):
            for nested in item.values():
                scan(nested)
        elif isinstance(item, list):
            for nested in item:
                scan(nested)
        elif isinstance(item, str):
            lowered = item.lower()
            for pattern in patterns:
                if pattern in lowered:
                    found.append(item)
                    break

    scan(value)
    return found


def synthesize_spec_from_public_issue(task: PublicIssue) -> dict[str, Any]:
    try:
        template = _TEMPLATES[task.id]
    except KeyError as error:
        raise ValueError(f"No synthesis template for public task: {task.id}") from error
    spec = copy.deepcopy(template)
    spec.setdefault("tags", [])
    spec["tags"] = sorted(set(spec["tags"] + ["method-checkpoint", "generated-from-public-issue"]))
    spec["name"] = f"Generated method checkpoint for {task.id}"
    spec["benchmark"] = task.benchmark
    return spec


def _payload_for_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if (spec.get("target") or {}).get("kind") == "p2p_only_state":
        return {"kind": "state_injection", "target": spec["target"]}

    checkpoint = spec.get("checkpoint", {})
    strategy = (checkpoint.get("allowed_strategies") or ["unknown"])[0]
    benchmark = str(spec.get("benchmark") or "").lower()
    if strategy == "localStorage_restore_reload":
        return {"kind": "localStorage", "strategy": strategy, "keys": ["gameState"], "target": spec.get("target", {})}
    if benchmark == "spacerpg":
        return {"kind": "test_hook", "strategy": strategy, "hook": "__SPACERPG_E2E__", "target": spec.get("target", {}), "actions": spec.get("actions", [])}
    if benchmark == "datingsim":
        return {"kind": "zustand_hook", "strategy": strategy, "hook": "__WARPTEST_LOVE__", "target": spec.get("target", {}), "actions": spec.get("actions", [])}
    if benchmark == "openra":
        if strategy == "openra_public_settings_restore":
            kind = "openra_public_settings_checkpoint"
            hook = "isolated public settings.yaml"
        elif strategy == "openra_gameplay_production_validate":
            kind = "openra_gameplay_probe"
            hook = "Launch.WarptestGameplayRequestPath"
        else:
            kind = "openra_utility_checkpoint"
            hook = "--warptest-checkpoint"
        return {
            "kind": kind,
            "strategy": strategy,
            "hook": hook,
            "target": spec.get("target", {}),
            "actions": spec.get("actions", []),
        }
    if benchmark == "jynew":
        return {
            "kind": "jynew_utility_checkpoint",
            "strategy": strategy,
            "hook": "--warptest-checkpoint",
            "target": spec.get("target", {}),
            "actions": spec.get("actions", []),
        }
    if benchmark == "pal3":
        return {
            "kind": "pal3_console_checkpoint",
            "strategy": strategy,
            "hook": "--warptest-checkpoint",
            "target": spec.get("target", {}),
            "actions": spec.get("actions", []),
        }
    if benchmark == "luanti":
        return {
            "kind": "luanti_worldmod_checkpoint",
            "strategy": strategy,
            "hook": "warptest",
            "target": spec.get("target", {}),
            "actions": spec.get("actions", []),
        }
    if benchmark == "jupyterlab":
        return {
            "kind": "jupyterlab_workspace_checkpoint",
            "strategy": strategy,
            "hook": "jupyter server contents/session api",
            "target": spec.get("target", {}),
            "actions": spec.get("actions", []),
        }
    if benchmark == "libreoffice":
        return {
            "kind": "libreoffice_uno_document_checkpoint",
            "strategy": strategy,
            "hook": "uno/odf document api",
            "target": spec.get("target", {}),
            "actions": spec.get("actions", []),
        }
    if benchmark == "blender":
        return {
            "kind": "blender_python_scene_checkpoint",
            "strategy": strategy,
            "hook": "bpy/operator api",
            "target": spec.get("target", {}),
            "actions": spec.get("actions", []),
        }
    return {"kind": "unknown", "strategy": strategy, "target": spec.get("target", {})}


_TEMPLATES: dict[str, dict[str, Any]] = {
    "2048-merge-left": {
        "benchmark": "2048",
        "target": {"kind": "board", "board": [[2, 2, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]], "score": 0, "bestScore": 0},
        "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
        "actions": [{"type": "move", "direction": "left", "wait_ms": 450}],
        "assertions": [{"type": "score_increased_by_at_least", "amount": 4}, {"type": "tile_value_present", "value": 4}, {"type": "no_severe_console"}],
        "tags": ["2048", "merge"],
    },
    "2048-near-win": {
        "benchmark": "2048",
        "target": {"kind": "board", "board": [[1024, 1024, 0, 0], [4, 8, 16, 32], [0, 0, 0, 0], [0, 0, 0, 0]], "score": 4096, "bestScore": 4096},
        "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
        "actions": [{"type": "move", "direction": "left", "wait_ms": 450}],
        "assertions": [{"type": "tile_value_present", "value": 2048}, {"type": "no_severe_console"}],
        "tags": ["2048", "near-win"],
    },
    "2048-near-loss": {
        "benchmark": "2048",
        "target": {"kind": "board", "board": [[2, 4, 2, 4], [4, 2, 4, 2], [2, 4, 2, 4], [4, 2, 4, 2]], "score": 512, "bestScore": 512, "over": True},
        "checkpoint": {"allowed_strategies": ["localStorage_restore_reload"], "expected_validity": "save-restorable"},
        "actions": [{"type": "move", "direction": "left", "wait_ms": 300}],
        "assertions": [{"type": "state_equals", "path": "over", "value": True}, {"type": "no_severe_console"}],
        "tags": ["2048", "near-loss"],
    },
    "spacerpg-playable-flight": {
        "benchmark": "SpaceRPG",
        "setup": {"disable_economy_backend": True},
        "target": {"kind": "flight", "screen": "flight", "system_id": "helion-reach"},
        "checkpoint": {"allowed_strategies": ["test_hook_jump_and_validate"], "expected_validity": "replay-reachable"},
        "actions": [{"type": "key", "key": "W", "wait_ms": 300}],
        "assertions": [{"type": "state_equals", "path": "screen", "value": "flight"}, {"type": "no_severe_console"}],
        "tags": ["spacerpg", "flight"],
    },
    "spacerpg-station-save": {
        "benchmark": "SpaceRPG",
        "setup": {"disable_economy_backend": True},
        "target": {"kind": "station", "screen": "station", "system_id": "helion-reach", "station_id": "helion-prime"},
        "checkpoint": {"allowed_strategies": ["test_hook_jump_and_validate"], "expected_validity": "replay-reachable"},
        "actions": [{"type": "hook_call", "method": "saveGame", "args": ["auto"]}],
        "assertions": [{"type": "state_equals", "path": "screen", "value": "station"}, {"type": "localStorage_key_exists", "key": "spacerpg-save-slot:auto"}, {"type": "no_severe_console"}],
        "tags": ["spacerpg", "station", "save"],
    },
    "spacerpg-save-restore": {
        "benchmark": "SpaceRPG",
        "setup": {"disable_economy_backend": True},
        "target": {"kind": "station", "screen": "station", "system_id": "helion-reach", "station_id": "helion-prime"},
        "checkpoint": {"allowed_strategies": ["test_hook_jump_and_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "hook_call", "method": "saveGame", "args": ["manual-1"]}, {"type": "hook_call", "method": "setScreen", "args": ["flight"]}, {"type": "hook_call", "method": "loadGame", "args": ["manual-1"]}, {"type": "wait", "ms": 300}],
        "assertions": [{"type": "state_equals", "path": "screen", "value": "station"}, {"type": "state_equals", "path": "activeSaveSlotId", "value": "manual-1"}, {"type": "localStorage_key_exists", "key": "spacerpg-save-slot:manual-1"}, {"type": "no_severe_console"}],
        "tags": ["spacerpg", "save", "restore"],
    },
    "spacerpg-galaxy-jump": {
        "benchmark": "SpaceRPG",
        "setup": {"disable_economy_backend": True},
        "target": {"kind": "galaxy_map", "screen": "galaxyMap", "system_id": "helion-reach", "galaxy_map_mode": "gate", "gate_position": [1280, 140, -1240]},
        "checkpoint": {"allowed_strategies": ["test_hook_jump_and_validate"], "expected_validity": "replay-reachable"},
        "actions": [{"type": "hook_call", "method": "jumpToSystem", "args": ["kuro-belt"]}, {"type": "wait", "ms": 300}],
        "assertions": [{"type": "state_equals", "path": "screen", "value": "flight"}, {"type": "state_equals", "path": "currentSystemId", "value": "kuro-belt"}, {"type": "state_contains", "path": "knownSystems", "value": "kuro-belt"}, {"type": "no_severe_console"}],
        "tags": ["spacerpg", "galaxy-map", "jump"],
    },
    "spacerpg-trading-cargo": {
        "benchmark": "SpaceRPG",
        "setup": {"disable_economy_backend": True},
        "target": {"kind": "station", "screen": "station", "system_id": "helion-reach", "station_id": "helion-prime"},
        "checkpoint": {"allowed_strategies": ["test_hook_jump_and_validate"], "expected_validity": "replay-reachable"},
        "actions": [{"type": "hook_call", "method": "sell", "args": ["basic-food", 1]}, {"type": "wait", "ms": 250}],
        "assertions": [{"type": "state_equals", "path": "player.cargo.basic-food", "value": 2}, {"type": "state_gte", "path": "player.credits", "value": 1726}, {"type": "no_severe_console"}],
        "tags": ["spacerpg", "trading", "cargo"],
    },
    "spacerpg-equipment-loadout": {
        "benchmark": "SpaceRPG",
        "setup": {"disable_economy_backend": True},
        "target": {"kind": "equipment_loadout", "screen": "station", "system_id": "helion-reach", "station_id": "helion-prime", "station_tab": "Hangar", "equipment_inventory": {"cargo-expansion": 1}},
        "checkpoint": {"allowed_strategies": ["test_hook_jump_and_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "hook_call", "method": "installEquipmentFromInventory", "args": ["cargo-expansion"]}, {"type": "wait", "ms": 250}],
        "assertions": [{"type": "state_contains", "path": "player.equipment", "value": "cargo-expansion"}, {"type": "state_equals", "path": "player.equipmentInventory.cargo-expansion", "value": 0}, {"type": "no_severe_console"}],
        "tags": ["spacerpg", "equipment", "loadout"],
    },
    "spacerpg-story-mission": {
        "benchmark": "SpaceRPG",
        "setup": {"disable_economy_backend": True},
        "target": {"kind": "debug_scenario", "scenario_id": "glass-wake-hero", "screen": "flight", "system_id": "mirr-vale", "mission_visible": ["story-probe-in-glass"], "completed_mission_visible": ["story-clean-carrier"], "known_system_visible": ["helion-reach", "mirr-vale"]},
        "checkpoint": {"allowed_strategies": ["test_hook_jump_and_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "key", "key": "W", "wait_ms": 300}],
        "assertions": [{"type": "state_equals", "path": "currentSystemId", "value": "mirr-vale"}, {"type": "state_contains", "path": "completedMissionIds", "value": "story-clean-carrier"}, {"type": "state_equals", "path": "targetId", "value": "glass-echo-drone"}, {"type": "no_severe_console"}],
        "tags": ["spacerpg", "story", "mission"],
    },
    "love-resource-action": {
        "benchmark": "DatingSim",
        "setup": {"ai_mode": "mocked"},
        "target": {"kind": "deterministic_state", "seed": 7, "player_name": "WarpTest", "gender": "male", "patch": {"player": {"resources": {"money": 1000, "energy": 100, "health": 100, "mood": 100}}, "world": {"location": "home"}}, "expect": {"resources": {"money": 1000, "energy": 100}}},
        "checkpoint": {"allowed_strategies": ["test_hook_seeded_state"], "expected_validity": "injection-only"},
        "actions": [{"type": "hook_action", "action_id": "work_coding", "wait_ms": 500}],
        "assertions": [{"type": "state_equals", "path": "player.resources.money", "value": 1100}, {"type": "state_equals", "path": "player.resources.energy", "value": 80}, {"type": "no_real_openai"}, {"type": "no_severe_console"}],
        "tags": ["love", "resources"],
    },
    "love-cafe-date": {
        "benchmark": "DatingSim",
        "setup": {"ai_mode": "mocked"},
        "target": {"kind": "deterministic_state", "seed": 7, "player_name": "WarpTest", "gender": "male", "patch": {"world": {"location": "cafe"}}, "expect": {"location": "cafe", "resources": {"money": 1000, "energy": 100}}},
        "checkpoint": {"allowed_strategies": ["test_hook_seeded_state"], "expected_validity": "injection-only"},
        "actions": [{"type": "hook_action", "action_id": "date_coffee", "wait_ms": 1000}],
        "assertions": [{"type": "state_equals", "path": "player.resources.money", "value": 920}, {"type": "state_equals", "path": "player.resources.energy", "value": 90}, {"type": "state_equals", "path": "player.stats.communication", "value": 11}, {"type": "state_equals", "path": "world.location", "value": "cafe"}, {"type": "no_real_openai"}, {"type": "no_severe_console"}],
        "tags": ["love", "cafe", "date"],
    },
    "love-dialogue-open": {
        "benchmark": "DatingSim",
        "setup": {"ai_mode": "mocked"},
        "target": {"kind": "deterministic_state", "seed": 11, "player_name": "WarpTest", "gender": "male", "patch": {"player": {"relationships": {"char_1": {"trust": 45, "attraction": 35, "comfort": 40, "respect": 30, "investment": 15, "conflict": 0, "status": "friend", "history": ["Met Elena through a gallery opening."], "memories": []}}}, "world": {"location": "cafe"}}, "expect": {"location": "cafe", "resources": {"money": 1000, "energy": 100}, "relationships": {"char_1": {"trust": 45, "attraction": 35, "status": "friend"}}}},
        "checkpoint": {"allowed_strategies": ["test_hook_seeded_state"], "expected_validity": "injection-only"},
        "actions": [{"type": "hook_call", "method": "startDialogue", "args": ["char_1"], "wait_ms": 300}],
        "assertions": [{"type": "state_equals", "path": "activeDialogue.isOpen", "value": True}, {"type": "state_equals", "path": "activeDialogue.characterId", "value": "char_1"}, {"type": "state_equals", "path": "player.dailyChatUsed", "value": True}, {"type": "no_real_openai"}, {"type": "no_severe_console"}],
        "tags": ["love", "dialogue"],
    },
    "love-relationship-update": {
        "benchmark": "DatingSim",
        "setup": {"ai_mode": "mocked"},
        "target": {"kind": "deterministic_state", "seed": 13, "player_name": "WarpTest", "gender": "male", "patch": {"player": {"relationships": {"char_1": {"trust": 20, "attraction": 25, "comfort": 15, "respect": 10, "investment": 5, "conflict": 2, "status": "acquaintance", "history": ["Met Elena at a gallery opening."], "memories": []}}}, "world": {"location": "cafe"}}, "expect": {"location": "cafe", "resources": {"money": 1000, "energy": 100}, "relationships": {"char_1": {"trust": 20, "attraction": 25, "status": "acquaintance"}}}},
        "checkpoint": {"allowed_strategies": ["test_hook_seeded_state"], "expected_validity": "injection-only"},
        "actions": [{"type": "hook_call", "method": "applyRelationshipUpdates", "args": ["char_1", {"trust": 10, "comfort": 5, "attraction": 5, "respect": 3}], "wait_ms": 300}],
        "assertions": [{"type": "state_equals", "path": "player.relationships.char_1.trust", "value": 30}, {"type": "state_equals", "path": "player.relationships.char_1.attraction", "value": 30}, {"type": "state_equals", "path": "player.relationships.char_1.comfort", "value": 20}, {"type": "state_equals", "path": "player.relationships.char_1.respect", "value": 13}, {"type": "no_real_openai"}, {"type": "no_severe_console"}],
        "tags": ["love", "relationship"],
    },
    "love-ending-precondition": {
        "benchmark": "DatingSim",
        "setup": {"ai_mode": "mocked"},
        "target": {"kind": "deterministic_state", "seed": 19, "player_name": "WarpTest", "gender": "male", "patch": {"player": {"title": "Committed Partner", "relationships": {"char_1": {"trust": 85, "attraction": 90, "comfort": 80, "respect": 75, "investment": 65, "conflict": 0, "status": "partner", "history": ["Completed Elena gallery arc.", "Committed to a long-term relationship."], "memories": ["Elena trusts WarpTest after the final gallery event."]}}}, "world": {"week": 8, "day": 5, "timeSlot": "evening", "location": "restaurant", "globalFlags": {"elena_gallery_arc_complete": True, "elena_partner_ending_ready": True}}}, "expect": {"location": "restaurant", "player_title": "Committed Partner", "resources": {"money": 1000, "energy": 100}, "relationships": {"char_1": {"trust": 85, "attraction": 90, "status": "partner"}}, "global_flags": {"elena_partner_ending_ready": True}}},
        "checkpoint": {"allowed_strategies": ["test_hook_seeded_state"], "expected_validity": "injection-only"},
        "actions": [{"type": "hook_call", "method": "save", "args": [], "wait_ms": 300}],
        "assertions": [{"type": "state_equals", "path": "player.title", "value": "Committed Partner"}, {"type": "state_equals", "path": "player.relationships.char_1.status", "value": "partner"}, {"type": "state_equals", "path": "world.globalFlags.elena_partner_ending_ready", "value": True}, {"type": "no_real_openai"}, {"type": "no_severe_console"}],
        "tags": ["love", "ending"],
    },
    "cnc-gdi01-beachhead": {
        "benchmark": "OpenRA",
        "target": {"kind": "openra_map", "mod": "cnc", "requires_mod": "cnc", "map": "gdi01", "title": "01:   Storm the Beachhead", "visibility": "MissionSelector", "categories": ["Campaign"], "players": [{"id": "GDI", "playable": True, "required": True, "faction": "gdi"}, {"id": "Nod", "bot": "campaign", "faction": "nod"}], "actors": [{"type": "e1", "owner": "Nod", "min_count": 10}, {"type": "gun", "owner": "Nod", "min_count": 3}], "objectives": [{"owner": "GDI", "type": "primary", "id": "eliminate-nod"}, {"owner": "GDI", "type": "secondary", "id": "establish-beachhead"}]},
        "checkpoint": {"allowed_strategies": ["openra_utility_map_validate"], "expected_validity": "replay-reachable"},
        "actions": [{"type": "openra_validate_map", "mod": "cnc", "map": "gdi01"}],
        "assertions": [{"type": "openra_map_loaded", "mod": "cnc", "map": "gdi01"}, {"type": "openra_player_exists", "id": "GDI", "playable": True, "faction": "gdi"}, {"type": "openra_actor_count", "actor": "e1", "owner": "Nod", "min_count": 10}, {"type": "openra_objective_declared", "owner": "GDI", "objective_type": "primary", "objective": "eliminate-nod"}, {"type": "no_openra_utility_errors"}],
        "tags": ["openra", "cnc", "campaign", "gdi"],
    },
    "cnc-nod03a-prison-break": {
        "benchmark": "OpenRA",
        "target": {"kind": "openra_map", "mod": "cnc", "requires_mod": "cnc", "map": "nod03a", "title": "03a: Sudanese Prison Break", "visibility": "MissionSelector", "categories": ["Campaign"], "players": [{"id": "Nod", "playable": True, "required": True, "faction": "nod"}, {"id": "GDI", "bot": "campaign", "faction": "gdi"}], "actors": [{"type": "jeep", "owner": "GDI", "min_count": 2}, {"type": "e1", "owner": "GDI", "min_count": 7}], "objectives": [{"owner": "Nod", "type": "primary", "id": "capture-prison"}, {"owner": "Nod", "type": "secondary", "id": "destroy-gdi-forces"}]},
        "checkpoint": {"allowed_strategies": ["openra_utility_map_validate"], "expected_validity": "replay-reachable"},
        "actions": [{"type": "openra_validate_map", "mod": "cnc", "map": "nod03a"}],
        "assertions": [{"type": "openra_map_loaded", "mod": "cnc", "map": "nod03a"}, {"type": "openra_player_exists", "id": "Nod", "playable": True, "faction": "nod"}, {"type": "openra_actor_count", "actor": "jeep", "owner": "GDI", "min_count": 2}, {"type": "openra_objective_declared", "owner": "Nod", "objective_type": "primary", "objective": "capture-prison"}, {"type": "no_openra_utility_errors"}],
        "tags": ["openra", "cnc", "campaign", "nod"],
    },
    "cnc-hot-box-dropzone": {
        "benchmark": "OpenRA",
        "target": {"kind": "openra_map", "mod": "cnc", "requires_mod": "cnc", "map": "the-hot-box", "title": "The Hot Box", "visibility": "Lobby", "categories": ["Drop Zone"], "players": [{"id": "Multi0", "playable": True, "faction": "nod"}, {"id": "Multi9", "playable": True, "faction": "nod"}], "actors": [{"type": "apc", "min_count": 10}, {"type": "mpspawn", "owner": "Neutral", "min_count": 10}]},
        "checkpoint": {"allowed_strategies": ["openra_utility_map_validate"], "expected_validity": "replay-reachable"},
        "actions": [{"type": "openra_validate_map", "mod": "cnc", "map": "the-hot-box"}],
        "assertions": [{"type": "openra_map_loaded", "mod": "cnc", "map": "the-hot-box"}, {"type": "openra_player_exists", "id": "Multi0", "playable": True, "faction": "nod"}, {"type": "openra_actor_count", "actor": "apc", "min_count": 10}, {"type": "openra_actor_count", "actor": "mpspawn", "owner": "Neutral", "min_count": 10}, {"type": "no_openra_utility_errors"}],
        "tags": ["openra", "cnc", "lobby", "drop-zone"],
    },
    "ra-allies03a-dead-end": {
        "benchmark": "OpenRA",
        "target": {"kind": "openra_map", "mod": "ra", "requires_mod": "ra", "map": "allies-03a", "title": "03a: Dead End", "visibility": "MissionSelector", "categories": ["Campaign"], "players": [{"id": "Greece", "playable": True, "required": True, "faction": "allies"}, {"id": "USSR", "bot": "campaign", "faction": "soviet"}], "actors": [{"type": "e1", "owner": "USSR", "min_count": 20, "match_prefix": True}, {"type": "e2", "owner": "USSR", "min_count": 8, "match_prefix": True}], "objectives": [{"owner": "Greece", "type": "primary", "id": "destroy-bridges"}, {"owner": "Greece", "type": "primary", "id": "tanya-survive"}, {"owner": "Greece", "type": "secondary", "id": "free-prisoners"}]},
        "checkpoint": {"allowed_strategies": ["openra_utility_map_validate"], "expected_validity": "replay-reachable"},
        "actions": [{"type": "openra_validate_map", "mod": "ra", "map": "allies-03a"}],
        "assertions": [{"type": "openra_map_loaded", "mod": "ra", "map": "allies-03a"}, {"type": "openra_player_exists", "id": "Greece", "playable": True, "faction": "allies"}, {"type": "openra_actor_count", "actor": "e1", "owner": "USSR", "min_count": 20, "match_prefix": True}, {"type": "openra_objective_declared", "owner": "Greece", "objective_type": "primary", "objective": "destroy-bridges"}, {"type": "no_openra_utility_errors"}],
        "tags": ["openra", "ra", "campaign", "allies"],
    },
    "ra-soviet05-distant-thunder": {
        "benchmark": "OpenRA",
        "target": {"kind": "openra_map", "mod": "ra", "requires_mod": "ra", "map": "soviet-05", "title": "05:   Distant Thunder", "visibility": "MissionSelector", "categories": ["Campaign"], "players": [{"id": "USSR", "playable": True, "required": True, "faction": "soviet"}, {"id": "Greece", "bot": "campaign", "faction": "allies"}, {"id": "France", "bot": "campaign", "faction": "allies"}], "actors": [{"type": "gun", "owner": "Greece", "min_count": 2}, {"type": "2tnk", "owner": "Greece", "min_count": 1, "location": "59,43"}, {"type": "e1", "owner": "Greece", "min_count": 8}], "objectives": [{"owner": "USSR", "type": "primary", "id": "capture-radar-dome"}, {"owner": "USSR", "type": "primary", "id": "defeat-allied-forces"}, {"owner": "USSR", "type": "primary", "id": "defend-radar-dome"}]},
        "checkpoint": {"allowed_strategies": ["openra_utility_map_validate"], "expected_validity": "replay-reachable"},
        "actions": [{"type": "openra_validate_map", "mod": "ra", "map": "soviet-05"}],
        "assertions": [{"type": "openra_map_loaded", "mod": "ra", "map": "soviet-05"}, {"type": "openra_player_exists", "id": "USSR", "playable": True, "faction": "soviet"}, {"type": "openra_actor_count", "actor": "2tnk", "owner": "Greece", "location": "59,43", "min_count": 1}, {"type": "openra_objective_declared", "owner": "USSR", "objective_type": "primary", "objective": "capture-radar-dome"}, {"type": "no_openra_utility_errors"}],
        "tags": ["openra", "ra", "campaign", "soviet"],
    },
    "ra-soviet05-war-factory-heavy-tank": {
        "benchmark": "OpenRA",
        "setup": {"openra_gameplay_probe": {"cash": 50000, "fast_build": True, "build_anywhere": False}},
        "target": {"kind": "openra_map", "mod": "ra", "requires_mod": "ra", "map": "soviet-05", "title": "05:   Distant Thunder", "visibility": "MissionSelector", "categories": ["Campaign"], "players": [{"id": "USSR", "playable": True, "required": True, "faction": "soviet"}, {"id": "Greece", "bot": "campaign", "faction": "allies"}]},
        "checkpoint": {"allowed_strategies": ["openra_gameplay_production_validate"], "expected_validity": "replay-reachable"},
        "actions": [{"type": "openra_wait_ticks", "ticks": 100}, {"type": "openra_deploy_actor", "player": "USSR", "actor_id": "startmcv", "wait_until_actor": "fact", "timeout_ticks": 900}, {"type": "openra_wait_ticks", "ticks": 10}, {"type": "openra_build_actor", "player": "USSR", "actor": "powr", "queue": "Building", "place_near": {"anchor": "28,76", "radius": 14}, "timeout_ticks": 900}, {"type": "openra_build_actor", "player": "USSR", "actor": "proc", "queue": "Building", "place_near": {"anchor": "28,76", "radius": 14}, "timeout_ticks": 900}, {"type": "openra_build_actor", "player": "USSR", "actor": "weap", "queue": "Building", "place_near": {"anchor": "28,76", "radius": 16}, "timeout_ticks": 900}, {"type": "openra_build_actor", "player": "USSR", "actor": "fix", "queue": "Building", "place_near": {"anchor": "28,76", "radius": 16}, "timeout_ticks": 900}, {"type": "openra_build_actor", "player": "USSR", "actor": "3tnk", "queue": "Vehicle", "timeout_ticks": 900}],
        "assertions": [{"type": "openra_actor_built", "player": "USSR", "actor": "weap", "min_delta": 1}, {"type": "openra_actor_built", "player": "USSR", "actor": "fix", "min_delta": 1}, {"type": "openra_actor_built", "player": "USSR", "actor": "3tnk", "min_delta": 1}, {"type": "openra_production_prerequisite_respected", "player": "USSR", "actor": "3tnk"}, {"type": "no_openra_gameplay_probe_errors"}],
        "tags": ["openra", "ra", "campaign", "soviet", "production", "vehicle"],
    },
    "ra-soviet05-barracks-rocket-infantry": {
        "benchmark": "OpenRA",
        "setup": {"openra_gameplay_probe": {"cash": 50000, "fast_build": True, "build_anywhere": False}},
        "target": {"kind": "openra_map", "mod": "ra", "requires_mod": "ra", "map": "soviet-05", "title": "05:   Distant Thunder", "visibility": "MissionSelector", "categories": ["Campaign"], "players": [{"id": "USSR", "playable": True, "required": True, "faction": "soviet"}, {"id": "Greece", "bot": "campaign", "faction": "allies"}]},
        "checkpoint": {"allowed_strategies": ["openra_gameplay_production_validate"], "expected_validity": "replay-reachable"},
        "actions": [{"type": "openra_wait_ticks", "ticks": 100}, {"type": "openra_deploy_actor", "player": "USSR", "actor_id": "startmcv", "wait_until_actor": "fact", "timeout_ticks": 900}, {"type": "openra_wait_ticks", "ticks": 10}, {"type": "openra_build_actor", "player": "USSR", "actor": "powr", "queue": "Building", "place_near": {"anchor": "28,76", "radius": 14}, "timeout_ticks": 900}, {"type": "openra_build_actor", "player": "USSR", "actor": "barr", "queue": "Building", "place_near": {"anchor": "28,76", "radius": 14}, "timeout_ticks": 900}, {"type": "openra_build_actor", "player": "USSR", "actor": "e1", "queue": "Infantry", "timeout_ticks": 900}],
        "assertions": [{"type": "openra_actor_built", "player": "USSR", "actor": "barr", "min_delta": 1}, {"type": "openra_actor_built", "player": "USSR", "actor": "e1", "min_delta": 1}, {"type": "openra_production_prerequisite_respected", "player": "USSR", "actor": "e1"}, {"type": "no_openra_gameplay_probe_errors"}],
        "tags": ["openra", "ra", "campaign", "soviet", "production", "infantry"],
    },
    "ra-allies07-soviet-war-factory-produces-heavy-tank": {
        "benchmark": "OpenRA",
        "setup": {"openra_gameplay_probe": {"cash": 50000, "fast_build": True, "build_anywhere": False}},
        "target": {"kind": "openra_map", "mod": "ra", "requires_mod": "ra", "map": "allies-07", "title": "07:   Sunken Treasure", "visibility": "MissionSelector", "categories": ["Campaign"], "players": [{"id": "Greece", "playable": True, "required": True, "faction": "allies"}, {"id": "USSR", "bot": "campaign", "faction": "soviet"}], "actors": [{"id": "USSRWarFactory", "type": "weap", "owner": "USSR"}]},
        "checkpoint": {"allowed_strategies": ["openra_gameplay_production_validate"], "expected_validity": "replay-reachable"},
        "actions": [{"type": "openra_wait_ticks", "ticks": 20}, {"type": "openra_build_actor", "player": "USSR", "actor": "3tnk", "queue": "Vehicle", "timeout_ticks": 900}],
        "assertions": [{"type": "openra_actor_exists", "player": "USSR", "actor_id": "USSRWarFactory"}, {"type": "openra_actor_built", "player": "USSR", "actor": "3tnk", "min_delta": 1}, {"type": "openra_production_prerequisite_respected", "player": "USSR", "actor": "3tnk"}, {"type": "no_openra_gameplay_probe_errors"}],
        "tags": ["openra", "ra", "campaign", "allies", "soviet", "production", "vehicle"],
    },
    "ra-allies05a-soviet-war-factory-produces-vehicle": {
        "benchmark": "OpenRA",
        "setup": {"openra_gameplay_probe": {"cash": 50000, "fast_build": True, "build_anywhere": False}},
        "target": {"kind": "openra_map", "mod": "ra", "requires_mod": "ra", "map": "allies-05a", "title": "05a: Tanya's Tale", "visibility": "MissionSelector", "categories": ["Campaign"], "players": [{"id": "Greece", "playable": True, "required": True, "faction": "allies"}, {"id": "USSR", "bot": "campaign", "faction": "soviet"}], "actors": [{"id": "Warfactory2", "type": "weap", "owner": "USSR"}]},
        "checkpoint": {"allowed_strategies": ["openra_gameplay_production_validate"], "expected_validity": "replay-reachable"},
        "actions": [{"type": "openra_wait_ticks", "ticks": 20}, {"type": "openra_build_actor", "player": "USSR", "actor": "3tnk", "queue": "Vehicle", "timeout_ticks": 900}],
        "assertions": [{"type": "openra_actor_exists", "player": "USSR", "actor_id": "Warfactory2"}, {"type": "openra_actor_built", "player": "USSR", "actor": "3tnk", "min_delta": 1}, {"type": "openra_production_prerequisite_respected", "player": "USSR", "actor": "3tnk"}, {"type": "no_openra_gameplay_probe_errors"}],
        "tags": ["openra", "ra", "campaign", "allies", "soviet", "production", "vehicle"],
    },
    "ra-drop-zone": {
        "benchmark": "OpenRA",
        "target": {"kind": "openra_map", "mod": "ra", "requires_mod": "ra", "map": "drop-zone", "title": "Drop Zone", "visibility": "Lobby", "categories": ["Drop Zone"], "players": [{"id": "Multi0", "playable": True, "faction": "soviet"}, {"id": "Multi7", "playable": True, "faction": "soviet"}], "actors": [{"type": "apc", "min_count": 8}, {"type": "mpspawn", "owner": "Neutral", "min_count": 8}]},
        "checkpoint": {"allowed_strategies": ["openra_utility_map_validate"], "expected_validity": "replay-reachable"},
        "actions": [{"type": "openra_validate_map", "mod": "ra", "map": "drop-zone"}],
        "assertions": [{"type": "openra_map_loaded", "mod": "ra", "map": "drop-zone"}, {"type": "openra_player_exists", "id": "Multi0", "playable": True, "faction": "soviet"}, {"type": "openra_actor_count", "actor": "apc", "min_count": 8}, {"type": "openra_actor_count", "actor": "mpspawn", "owner": "Neutral", "min_count": 8}, {"type": "no_openra_utility_errors"}],
        "tags": ["openra", "ra", "lobby", "drop-zone"],
    },
    "jynew-team-join": {
        "benchmark": "jynew",
        "target": {"kind": "synthesized_state", "mod_id": "JYX2", "player_level": 5, "money": 500},
        "checkpoint": {"allowed_strategies": ["jynew_utility_save_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "jynew_join_team", "role_id": 1}],
        "assertions": [{"type": "jynew_team_contains", "role_id": 1}, {"type": "jynew_team_count", "expected": "2", "comparator": "equals"}, {"type": "no_jynew_utility_errors"}],
        "tags": ["jynew", "team"],
    },
    "jynew-level-up": {
        "benchmark": "jynew",
        "target": {"kind": "synthesized_state", "mod_id": "JYX2", "player_level": 3},
        "checkpoint": {"allowed_strategies": ["jynew_utility_save_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "jynew_level_up"}],
        "assertions": [{"type": "jynew_role_attr", "role_id": 0, "attr": "Level", "expected": "4", "comparator": "equals"}, {"type": "no_jynew_utility_errors"}],
        "tags": ["jynew", "level-up"],
    },
    "jynew-skill-learn": {
        "benchmark": "jynew",
        "target": {"kind": "synthesized_state", "mod_id": "JYX2", "player_level": 5},
        "checkpoint": {"allowed_strategies": ["jynew_utility_save_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "jynew_learn_skill", "skill_id": 1}],
        "assertions": [{"type": "jynew_skill_learned", "role_id": 0, "skill_id": 1}, {"type": "no_jynew_utility_errors"}],
        "tags": ["jynew", "skill"],
    },
    "jynew-save-restore": {
        "benchmark": "jynew",
        "target": {"kind": "synthesized_state", "mod_id": "JYX2", "player_level": 5, "money": 1000},
        "checkpoint": {"allowed_strategies": ["jynew_utility_save_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "jynew_save", "save_index": 0}, {"type": "jynew_load_save", "save_index": 0}],
        "assertions": [{"type": "jynew_role_attr", "role_id": 0, "attr": "Level", "expected": "5", "comparator": "equals"}, {"type": "jynew_money_gte", "int_value": 1000}, {"type": "no_jynew_utility_errors"}],
        "tags": ["jynew", "save", "restore"],
    },
    "jynew-money-trade": {
        "benchmark": "jynew",
        "target": {"kind": "synthesized_state", "mod_id": "JYX2", "money": 2000},
        "checkpoint": {"allowed_strategies": ["jynew_utility_save_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "jynew_add_item", "item_id": 2, "item_count": 3}],
        "assertions": [{"type": "jynew_item_count", "item_id": 2, "expected": "3", "comparator": "equals"}, {"type": "jynew_money_gte", "int_value": 2000}, {"type": "no_jynew_utility_errors"}],
        "tags": ["jynew", "money", "item"],
    },
    "pal3-story-progress": {
        "benchmark": "pal3",
        "target": {"kind": "synthesized_state", "story_vars": [{"key": 32768, "value": 10100}]},
        "checkpoint": {"allowed_strategies": ["pal3_console_save_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "pal3_set_story_var", "var_id": 32768, "value": 10401}],
        "assertions": [{"type": "pal3_story_var_equals", "var_id": 32768, "expected": "10401"}, {"type": "no_pal3_utility_errors"}],
        "tags": ["pal3", "story"],
    },
    "pal3-scene-load": {
        "benchmark": "pal3",
        "target": {"kind": "synthesized_state", "story_vars": [{"key": 32768, "value": 10401}]},
        "checkpoint": {"allowed_strategies": ["pal3_console_save_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "pal3_load_scene", "city": "q01", "scene": "b"}],
        "assertions": [{"type": "pal3_scene_is", "city": "q01", "scene": "b"}, {"type": "no_pal3_utility_errors"}],
        "tags": ["pal3", "scene"],
    },
    "pal3-scene-transition": {
        "benchmark": "pal3",
        "target": {"kind": "synthesized_state", "scene_city": "q01", "scene_name": "b"},
        "checkpoint": {"allowed_strategies": ["pal3_console_save_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "pal3_load_scene", "city": "q02", "scene": "q02"}],
        "assertions": [{"type": "pal3_scene_is", "city": "q02", "scene": "q02"}, {"type": "no_pal3_utility_errors"}],
        "tags": ["pal3", "scene", "transition"],
    },
    "pal3-team-join": {
        "benchmark": "pal3",
        "target": {"kind": "synthesized_state", "team_ids": [0]},
        "checkpoint": {"allowed_strategies": ["pal3_console_save_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "pal3_add_team_member", "actor_id": 1}],
        "assertions": [{"type": "pal3_team_contains", "actor_id": 1}, {"type": "pal3_team_count", "expected": "2"}, {"type": "no_pal3_utility_errors"}],
        "tags": ["pal3", "team"],
    },
    "pal3-team-leave": {
        "benchmark": "pal3",
        "target": {"kind": "synthesized_state", "team_ids": [0, 1]},
        "checkpoint": {"allowed_strategies": ["pal3_console_save_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "pal3_remove_team_member", "actor_id": 1}],
        "assertions": [{"type": "pal3_team_count", "expected": "1"}, {"type": "no_pal3_utility_errors"}],
        "tags": ["pal3", "team", "leave"],
    },
    "pal3-item-add": {
        "benchmark": "pal3",
        "target": {"kind": "synthesized_state", "money": 1000},
        "checkpoint": {"allowed_strategies": ["pal3_console_save_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "pal3_add_item", "item_id": 6001, "item_count": 3}],
        "assertions": [{"type": "pal3_item_count", "item_id": 6001, "expected": "3"}, {"type": "no_pal3_utility_errors"}],
        "tags": ["pal3", "item"],
    },
    "pal3-money-add": {
        "benchmark": "pal3",
        "target": {"kind": "synthesized_state", "money": 500},
        "checkpoint": {"allowed_strategies": ["pal3_console_save_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "pal3_add_money", "amount": 500}],
        "assertions": [{"type": "pal3_money_gte", "int_value": 1000}, {"type": "no_pal3_utility_errors"}],
        "tags": ["pal3", "money"],
    },
    "pal3-world-region": {
        "benchmark": "pal3",
        "target": {"kind": "synthesized_state", "story_vars": [{"key": 32768, "value": 10401}]},
        "checkpoint": {"allowed_strategies": ["pal3_console_save_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "pal3_enable_world_region", "region": 1, "flag": 2}],
        "assertions": [{"type": "pal3_world_region_enabled", "region": 1, "int_value": 2}, {"type": "no_pal3_utility_errors"}],
        "tags": ["pal3", "world-map"],
    },
    "pal3-favor-add": {
        "benchmark": "pal3",
        "target": {"kind": "synthesized_state", "team_ids": [0, 1]},
        "checkpoint": {"allowed_strategies": ["pal3_console_save_validate"], "expected_validity": "injection-only"},
        "actions": [{"type": "pal3_set_favor", "actor_id": 1, "amount": 50}],
        "assertions": [{"type": "pal3_favor_gte", "actor_id": 1, "int_value": 70}, {"type": "no_pal3_utility_errors"}],
        "tags": ["pal3", "favor"],
    },
    "pal3-save-restore": {
        "benchmark": "pal3",
        "target": {"kind": "synthesized_state", "scene_city": "q01", "scene_name": "b", "money": 1000, "team_ids": [0, 1], "story_vars": [{"key": 32768, "value": 10401}]},
        "checkpoint": {"allowed_strategies": ["pal3_console_save_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "pal3_save", "save_index": 0}, {"type": "pal3_load_save", "save_index": 0}],
        "assertions": [{"type": "pal3_money_gte", "int_value": 1000}, {"type": "pal3_team_count", "expected": "2"}, {"type": "pal3_story_var_equals", "var_id": 32768, "expected": "10401"}, {"type": "no_pal3_utility_errors"}],
        "tags": ["pal3", "save", "restore"],
    },
    "luanti-set-node": {
        "benchmark": "luanti",
        "target": {"kind": "synthesized_world", "nodes": [{"pos": {"x": 2, "y": 1, "z": 2}, "node": "basenodes:dirt"}]},
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "luanti_set_node", "pos": {"x": 2, "y": 2, "z": 2}, "node": "basenodes:stone"}],
        "assertions": [{"type": "luanti_node_is", "pos": {"x": 2, "y": 2, "z": 2}, "expected": "basenodes:stone"}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "voxel", "set-node"],
    },
    "luanti-dig-node": {
        "benchmark": "luanti",
        "target": {"kind": "synthesized_world", "nodes": [{"pos": {"x": 3, "y": 2, "z": 3}, "node": "basenodes:cobble"}]},
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "luanti_dig_node", "pos": {"x": 3, "y": 2, "z": 3}}],
        "assertions": [{"type": "luanti_node_is", "pos": {"x": 3, "y": 2, "z": 3}, "expected": "air"}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "voxel", "dig"],
    },
    "luanti-fill-floor": {
        "benchmark": "luanti",
        "target": {"kind": "synthesized_world", "fill": [{"from": {"x": 0, "y": 1, "z": 0}, "to": {"x": 3, "y": 1, "z": 3}, "node": "basenodes:cobble"}]},
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "luanti_set_node", "pos": {"x": 1, "y": 2, "z": 1}, "node": "basenodes:tree"}],
        "assertions": [{"type": "luanti_node_count", "from": {"x": 0, "y": 1, "z": 0}, "to": {"x": 3, "y": 1, "z": 3}, "node": "basenodes:cobble", "min_count": 16}, {"type": "luanti_node_is", "pos": {"x": 1, "y": 2, "z": 1}, "expected": "basenodes:tree"}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "voxel", "fill"],
    },
    "luanti-replace-node": {
        "benchmark": "luanti",
        "target": {"kind": "synthesized_world", "nodes": [{"pos": {"x": 4, "y": 2, "z": 4}, "node": "basenodes:stone"}]},
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "luanti_set_node", "pos": {"x": 4, "y": 2, "z": 4}, "node": "basenodes:cobble"}],
        "assertions": [{"type": "luanti_node_is", "pos": {"x": 4, "y": 2, "z": 4}, "expected": "basenodes:cobble"}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "voxel", "replace"],
    },
    "luanti-build-column": {
        "benchmark": "luanti",
        "target": {"kind": "synthesized_world", "nodes": [{"pos": {"x": 0, "y": 1, "z": 0}, "node": "basenodes:tree"}, {"pos": {"x": 0, "y": 2, "z": 0}, "node": "basenodes:tree"}]},
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "luanti_set_node", "pos": {"x": 0, "y": 3, "z": 0}, "node": "basenodes:tree"}],
        "assertions": [{"type": "luanti_node_count", "from": {"x": 0, "y": 1, "z": 0}, "to": {"x": 0, "y": 3, "z": 0}, "node": "basenodes:tree", "min_count": 3}, {"type": "luanti_node_is", "pos": {"x": 0, "y": 3, "z": 0}, "expected": "basenodes:tree"}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "voxel", "build"],
    },
    "luanti-chest-inventory": {
        "benchmark": "luanti",
        "target": {"kind": "synthesized_world", "meta_inventory": [{"pos": {"x": 5, "y": 1, "z": 5}, "node": "chest:chest", "list": "main", "items": [{"item": "basenodes:cobble", "count": 10}]}]},
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "luanti_set_meta_inventory", "pos": {"x": 5, "y": 1, "z": 5}, "list": "main", "items": [{"item": "basenodes:cobble", "count": 25}, {"item": "basenodes:tree", "count": 5}]}],
        "assertions": [{"type": "luanti_meta_inventory_contains", "pos": {"x": 5, "y": 1, "z": 5}, "item": "basenodes:cobble", "count": 25}, {"type": "luanti_meta_inventory_contains", "pos": {"x": 5, "y": 1, "z": 5}, "item": "basenodes:tree", "count": 5}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "inventory", "chest"],
    },
    "luanti-player-teleport": {
        "benchmark": "luanti",
        "target": {"kind": "synthesized_world", "nodes": [{"pos": {"x": 8, "y": 1, "z": 8}, "node": "basenodes:stone"}], "player": {"pos": {"x": 0, "y": 2, "z": 0}}},
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "luanti_set_player_pos", "pos": {"x": 8, "y": 2, "z": 8}}],
        "assertions": [{"type": "luanti_player_pos_near", "pos": {"x": 8, "y": 2, "z": 8}, "radius": 2.0}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "player", "teleport"],
    },
    "luanti-player-hp": {
        "benchmark": "luanti",
        "target": {"kind": "synthesized_world", "player": {"hp": 20}},
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "luanti_set_player_hp", "hp": 12}],
        "assertions": [{"type": "luanti_player_hp_gte", "int_value": 12}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "player", "hp"],
    },
    "luanti-player-inventory": {
        "benchmark": "luanti",
        "target": {"kind": "synthesized_world", "player": {"inventory": [{"item": "basenodes:cobble", "count": 1}]}},
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "luanti_give_item", "item": "basetools:pick_steel", "count": 1}],
        "assertions": [{"type": "luanti_inventory_contains", "item": "basetools:pick_steel", "count": 1}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "player", "inventory"],
    },
    "luanti-mod-storage": {
        "benchmark": "luanti",
        "target": {"kind": "synthesized_world", "mod_storage": [{"key": "warptest_progress", "value": "intro"}]},
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "luanti_set_mod_storage", "key": "warptest_progress", "value": "chapter-2"}],
        "assertions": [{"type": "luanti_mod_storage_equals", "key": "warptest_progress", "value": "chapter-2"}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "mod-storage"],
    },
    "luanti-save-restore": {
        "benchmark": "luanti",
        "target": {"kind": "save_restore", "nodes": [{"pos": {"x": 6, "y": 1, "z": 6}, "node": "basenodes:stone"}], "player": {"hp": 18, "inventory": [{"item": "basetools:pick_steel", "count": 1}]}, "mod_storage": [{"key": "warptest_saved", "value": "yes"}]},
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [],
        "assertions": [{"type": "luanti_node_is", "pos": {"x": 6, "y": 1, "z": 6}, "expected": "basenodes:stone"}, {"type": "luanti_player_hp_gte", "int_value": 18}, {"type": "luanti_inventory_contains", "item": "basetools:pick_steel", "count": 1}, {"type": "luanti_mod_storage_equals", "key": "warptest_saved", "value": "yes"}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "save", "restore"],
    },
    "luanti-complex-house-workshop": {
        "benchmark": "luanti",
        "target": {
            "kind": "synthesized_world",
            "fill": [
                {"from": {"x": -2, "y": 0, "z": -1}, "to": {"x": 8, "y": 0, "z": 9}, "node": "basenodes:dirt_with_grass"},
                {"from": {"x": 0, "y": 1, "z": 1}, "to": {"x": 6, "y": 1, "z": 7}, "node": "basenodes:cobble"},
                {"from": {"x": 0, "y": 2, "z": 1}, "to": {"x": 0, "y": 4, "z": 7}, "node": "basenodes:tree"},
                {"from": {"x": 6, "y": 2, "z": 1}, "to": {"x": 6, "y": 4, "z": 7}, "node": "basenodes:tree"},
                {"from": {"x": 0, "y": 2, "z": 7}, "to": {"x": 6, "y": 4, "z": 7}, "node": "basenodes:cobble"},
                {"from": {"x": 0, "y": 2, "z": 1}, "to": {"x": 2, "y": 4, "z": 1}, "node": "basenodes:cobble"},
                {"from": {"x": 4, "y": 2, "z": 1}, "to": {"x": 6, "y": 4, "z": 1}, "node": "basenodes:cobble"},
                {"from": {"x": -1, "y": 5, "z": 0}, "to": {"x": 7, "y": 5, "z": 8}, "node": "stairs:slab_cobble", "param2": 0},
            ],
            "meta_inventory": [{"pos": {"x": 2, "y": 2, "z": 5}, "node": "chest:chest", "list": "main", "items": [{"item": "basenodes:cobble", "count": 64}, {"item": "basenodes:tree", "count": 32}, {"item": "basetools:pick_steel", "count": 1}]}],
            "validations": [{"path": "node.0,2,1", "expected": "basenodes:cobble"}, {"path": "node.2,2,5", "expected": "chest:chest"}],
        },
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [
            {"type": "luanti_set_node", "pos": {"x": 3, "y": 2, "z": 1}, "node": "air"},
            {"type": "luanti_set_node", "pos": {"x": 3, "y": 3, "z": 1}, "node": "air"},
            {"type": "luanti_set_node", "pos": {"x": 3, "y": 1, "z": 0}, "node": "stairs:stair_cobble", "param2": 2},
            {"type": "luanti_set_node", "pos": {"x": 7, "y": 6, "z": 6}, "node": "basenodes:cobble"},
        ],
        "assertions": [{"type": "luanti_node_count", "from": {"x": -1, "y": 5, "z": 0}, "to": {"x": 7, "y": 5, "z": 8}, "node": "stairs:slab_cobble", "min_count": 81}, {"type": "luanti_node_count", "from": {"x": 0, "y": 2, "z": 1}, "to": {"x": 6, "y": 4, "z": 7}, "node": "basenodes:tree", "min_count": 30}, {"type": "luanti_node_is", "pos": {"x": 3, "y": 2, "z": 1}, "expected": "air"}, {"type": "luanti_node_is", "pos": {"x": 7, "y": 6, "z": 6}, "expected": "basenodes:cobble"}, {"type": "luanti_meta_inventory_contains", "pos": {"x": 2, "y": 2, "z": 5}, "item": "basetools:pick_steel", "count": 1}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "production", "structure", "house", "workshop"],
    },
    "luanti-irrigated-farmstead": {
        "benchmark": "luanti",
        "target": {
            "kind": "synthesized_world",
            "fill": [
                {"from": {"x": -1, "y": 0, "z": -1}, "to": {"x": 9, "y": 0, "z": 9}, "node": "basenodes:dirt_with_grass"},
                {"from": {"x": 0, "y": 1, "z": 2}, "to": {"x": 8, "y": 1, "z": 2}, "node": "basenodes:dirt"},
                {"from": {"x": 0, "y": 1, "z": 4}, "to": {"x": 8, "y": 1, "z": 4}, "node": "basenodes:dirt"},
                {"from": {"x": 0, "y": 1, "z": 6}, "to": {"x": 8, "y": 1, "z": 6}, "node": "basenodes:dirt"},
                {"from": {"x": 0, "y": 1, "z": 3}, "to": {"x": 8, "y": 1, "z": 3}, "node": "basenodes:water_source"},
                {"from": {"x": 0, "y": 1, "z": 5}, "to": {"x": 8, "y": 1, "z": 5}, "node": "basenodes:water_source"},
                {"from": {"x": 6, "y": 1, "z": 0}, "to": {"x": 8, "y": 1, "z": 1}, "node": "basenodes:cobble"},
            ],
            "nodes": [
                {"pos": {"x": 1, "y": 2, "z": 2}, "node": "basenodes:junglegrass"},
                {"pos": {"x": 3, "y": 2, "z": 2}, "node": "basenodes:junglegrass"},
                {"pos": {"x": 5, "y": 2, "z": 2}, "node": "basenodes:junglegrass"},
                {"pos": {"x": 7, "y": 2, "z": 2}, "node": "basenodes:junglegrass"},
                {"pos": {"x": 1, "y": 2, "z": 4}, "node": "basenodes:junglegrass"},
                {"pos": {"x": 3, "y": 2, "z": 4}, "node": "basenodes:junglegrass"},
                {"pos": {"x": 5, "y": 2, "z": 4}, "node": "basenodes:junglegrass"},
                {"pos": {"x": 7, "y": 2, "z": 4}, "node": "basenodes:junglegrass"},
            ],
            "meta_inventory": [{"pos": {"x": 7, "y": 2, "z": 1}, "node": "chest:chest", "list": "main", "items": [{"item": "basenodes:junglegrass", "count": 16}, {"item": "basenodes:dirt", "count": 40}, {"item": "basenodes:apple", "count": 8}]}],
            "validations": [{"path": "node.4,1,3", "expected": "basenodes:water_source"}],
        },
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "luanti_set_node", "pos": {"x": 4, "y": 2, "z": 6}, "node": "basenodes:apple"}],
        "assertions": [{"type": "luanti_node_count", "from": {"x": 0, "y": 1, "z": 3}, "to": {"x": 8, "y": 1, "z": 5}, "node": "basenodes:water_source", "min_count": 18}, {"type": "luanti_node_count", "from": {"x": 0, "y": 1, "z": 2}, "to": {"x": 8, "y": 1, "z": 6}, "node": "basenodes:dirt", "min_count": 27}, {"type": "luanti_node_count", "from": {"x": 0, "y": 2, "z": 2}, "to": {"x": 8, "y": 2, "z": 4}, "node": "basenodes:junglegrass", "min_count": 8}, {"type": "luanti_node_is", "pos": {"x": 4, "y": 2, "z": 6}, "expected": "basenodes:apple"}, {"type": "luanti_meta_inventory_contains", "pos": {"x": 7, "y": 2, "z": 1}, "item": "basenodes:dirt", "count": 40}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "production", "farm", "water", "inventory"],
    },
    "luanti-storage-workshop": {
        "benchmark": "luanti",
        "target": {
            "kind": "synthesized_world",
            "fill": [
                {"from": {"x": -1, "y": 0, "z": -1}, "to": {"x": 9, "y": 0, "z": 8}, "node": "basenodes:dirt_with_grass"},
                {"from": {"x": 1, "y": 1, "z": 1}, "to": {"x": 8, "y": 1, "z": 6}, "node": "basenodes:stone"},
                {"from": {"x": 1, "y": 2, "z": 1}, "to": {"x": 8, "y": 3, "z": 1}, "node": "basenodes:cobble"},
                {"from": {"x": 1, "y": 2, "z": 6}, "to": {"x": 8, "y": 3, "z": 6}, "node": "basenodes:cobble"},
                {"from": {"x": 1, "y": 2, "z": 1}, "to": {"x": 1, "y": 3, "z": 6}, "node": "basenodes:tree"},
                {"from": {"x": 8, "y": 2, "z": 1}, "to": {"x": 8, "y": 3, "z": 6}, "node": "basenodes:tree"},
                {"from": {"x": 0, "y": 4, "z": 0}, "to": {"x": 9, "y": 4, "z": 7}, "node": "stairs:slab_stone", "param2": 0},
            ],
            "nodes": [{"pos": {"x": 0, "y": 1, "z": 2}, "node": "basenodes:cobble"}, {"pos": {"x": 0, "y": 1, "z": 3}, "node": "basenodes:cobble"}, {"pos": {"x": 0, "y": 1, "z": 4}, "node": "basenodes:cobble"}],
            "meta_inventory": [
                {"pos": {"x": 0, "y": 2, "z": 2}, "node": "chest:chest", "list": "main", "items": [{"item": "basenodes:stone", "count": 80}, {"item": "basenodes:cobble", "count": 40}]},
                {"pos": {"x": 0, "y": 2, "z": 3}, "node": "chest:chest", "list": "main", "items": [{"item": "basenodes:tree", "count": 48}, {"item": "basenodes:leaves", "count": 24}]},
                {"pos": {"x": 0, "y": 2, "z": 4}, "node": "chest:chest", "list": "main", "items": [{"item": "basetools:pick_steel", "count": 2}, {"item": "basenodes:mossycobble", "count": 12}]},
            ],
            "mod_storage": [{"key": "warptest_workshop_state", "value": "stocked"}, {"key": "warptest_order", "value": "roofing"}],
            "validations": [{"path": "mod_storage.warptest_workshop_state", "expected": "stocked"}],
        },
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "luanti_set_meta_inventory", "pos": {"x": 0, "y": 2, "z": 4}, "list": "main", "items": [{"item": "basetools:pick_steel", "count": 1}, {"item": "basenodes:mossycobble", "count": 24}]}, {"type": "luanti_set_mod_storage", "key": "warptest_order", "value": "ready-for-build"}],
        "assertions": [{"type": "luanti_node_count", "from": {"x": 0, "y": 2, "z": 2}, "to": {"x": 0, "y": 2, "z": 4}, "node": "chest:chest", "min_count": 3}, {"type": "luanti_meta_inventory_contains", "pos": {"x": 0, "y": 2, "z": 2}, "item": "basenodes:stone", "count": 80}, {"type": "luanti_meta_inventory_contains", "pos": {"x": 0, "y": 2, "z": 3}, "item": "basenodes:tree", "count": 48}, {"type": "luanti_meta_inventory_contains", "pos": {"x": 0, "y": 2, "z": 4}, "item": "basetools:pick_steel", "count": 1}, {"type": "luanti_meta_inventory_contains", "pos": {"x": 0, "y": 2, "z": 4}, "item": "basenodes:mossycobble", "count": 24}, {"type": "luanti_mod_storage_equals", "key": "warptest_order", "value": "ready-for-build"}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "production", "storage", "workshop", "inventory"],
    },
    "luanti-mine-bridge-outpost": {
        "benchmark": "luanti",
        "target": {
            "kind": "synthesized_world",
            "fill": [
                {"from": {"x": -1, "y": 0, "z": -1}, "to": {"x": 8, "y": 0, "z": 8}, "node": "basenodes:gravel"},
                {"from": {"x": 0, "y": 1, "z": 2}, "to": {"x": 7, "y": 1, "z": 4}, "node": "basenodes:water_source"},
                {"from": {"x": 1, "y": 2, "z": 3}, "to": {"x": 6, "y": 2, "z": 3}, "node": "basenodes:tree"},
                {"from": {"x": 0, "y": 1, "z": 7}, "to": {"x": 7, "y": 4, "z": 7}, "node": "basenodes:stone"},
                {"from": {"x": 2, "y": 1, "z": 6}, "to": {"x": 5, "y": 3, "z": 6}, "node": "basenodes:cobble"},
                {"from": {"x": 2, "y": 5, "z": 6}, "to": {"x": 5, "y": 5, "z": 7}, "node": "stairs:slab_stone", "param2": 0},
            ],
            "nodes": [{"pos": {"x": 1, "y": 3, "z": 3}, "node": "basenodes:tree"}, {"pos": {"x": 6, "y": 3, "z": 3}, "node": "basenodes:tree"}, {"pos": {"x": 3, "y": 2, "z": 6}, "node": "air"}, {"pos": {"x": 4, "y": 2, "z": 6}, "node": "air"}],
            "player": {"pos": {"x": 1, "y": 3, "z": 0}, "hp": 20, "inventory": [{"item": "basetools:pick_steel", "count": 1}, {"item": "basenodes:cobble", "count": 32}]},
            "validations": [{"path": "inventory.basetools:pick_steel", "expected": "1"}],
        },
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "luanti_set_player_pos", "pos": {"x": 3, "y": 3, "z": 3}}, {"type": "luanti_set_node", "pos": {"x": 3, "y": 2, "z": 6}, "node": "air"}, {"type": "luanti_set_node", "pos": {"x": 4, "y": 2, "z": 6}, "node": "air"}, {"type": "luanti_set_node", "pos": {"x": 3, "y": 2, "z": 5}, "node": "stairs:stair_cobble", "param2": 0}],
        "assertions": [{"type": "luanti_player_pos_near", "pos": {"x": 3, "y": 3, "z": 3}, "radius": 2.0}, {"type": "luanti_inventory_contains", "item": "basetools:pick_steel", "count": 1}, {"type": "luanti_node_count", "from": {"x": 1, "y": 2, "z": 3}, "to": {"x": 6, "y": 2, "z": 3}, "node": "basenodes:tree", "min_count": 6}, {"type": "luanti_node_count", "from": {"x": 0, "y": 1, "z": 7}, "to": {"x": 7, "y": 4, "z": 7}, "node": "basenodes:stone", "min_count": 28}, {"type": "luanti_node_is", "pos": {"x": 3, "y": 2, "z": 6}, "expected": "air"}, {"type": "luanti_node_is", "pos": {"x": 3, "y": 2, "z": 5}, "expected": "stairs:stair_cobble"}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "production", "mine", "bridge", "player"],
    },
    "luanti-homestead-save-restore": {
        "benchmark": "luanti",
        "target": {
            "kind": "save_restore",
            "fill": [
                {"from": {"x": -1, "y": 0, "z": -1}, "to": {"x": 8, "y": 0, "z": 8}, "node": "basenodes:dirt_with_grass"},
                {"from": {"x": 0, "y": 1, "z": 1}, "to": {"x": 5, "y": 1, "z": 5}, "node": "basenodes:cobble"},
                {"from": {"x": 0, "y": 2, "z": 1}, "to": {"x": 5, "y": 3, "z": 1}, "node": "basenodes:tree"},
                {"from": {"x": 0, "y": 2, "z": 5}, "to": {"x": 5, "y": 3, "z": 5}, "node": "basenodes:tree"},
                {"from": {"x": 0, "y": 2, "z": 1}, "to": {"x": 0, "y": 3, "z": 5}, "node": "basenodes:cobble"},
                {"from": {"x": 5, "y": 2, "z": 1}, "to": {"x": 5, "y": 3, "z": 5}, "node": "basenodes:cobble"},
                {"from": {"x": -1, "y": 4, "z": 0}, "to": {"x": 6, "y": 4, "z": 6}, "node": "stairs:slab_cobble", "param2": 0},
                {"from": {"x": 6, "y": 1, "z": 2}, "to": {"x": 7, "y": 1, "z": 5}, "node": "basenodes:dirt"},
            ],
            "nodes": [{"pos": {"x": 6, "y": 2, "z": 2}, "node": "basenodes:junglegrass"}, {"pos": {"x": 7, "y": 2, "z": 4}, "node": "basenodes:junglegrass"}, {"pos": {"x": 2, "y": 2, "z": 0}, "node": "stairs:stair_cobble", "param2": 2}],
            "meta_inventory": [{"pos": {"x": 2, "y": 2, "z": 3}, "node": "chest:chest", "list": "main", "items": [{"item": "basenodes:cobble", "count": 64}, {"item": "basenodes:tree", "count": 32}, {"item": "basenodes:apple", "count": 6}]}],
            "player": {"pos": {"x": 2, "y": 3, "z": -2}, "hp": 18, "inventory": [{"item": "basetools:pick_steel", "count": 1}, {"item": "basenodes:cobble", "count": 16}]},
            "mod_storage": [{"key": "warptest_homestead_stage", "value": "winter-ready"}, {"key": "warptest_homestead_chests", "value": "stocked"}],
            "validations": [{"path": "node.2,2,3", "expected": "chest:chest"}, {"path": "mod_storage.warptest_homestead_stage", "expected": "winter-ready"}],
        },
        "checkpoint": {"allowed_strategies": ["luanti_worldmod_state_validate"], "expected_validity": "save-restorable"},
        "actions": [],
        "assertions": [{"type": "luanti_node_count", "from": {"x": -1, "y": 4, "z": 0}, "to": {"x": 6, "y": 4, "z": 6}, "node": "stairs:slab_cobble", "min_count": 49}, {"type": "luanti_node_count", "from": {"x": 6, "y": 2, "z": 2}, "to": {"x": 7, "y": 2, "z": 4}, "node": "basenodes:junglegrass", "min_count": 2}, {"type": "luanti_node_is", "pos": {"x": 2, "y": 2, "z": 3}, "expected": "chest:chest"}, {"type": "luanti_meta_inventory_contains", "pos": {"x": 2, "y": 2, "z": 3}, "item": "basenodes:apple", "count": 6}, {"type": "luanti_player_hp_gte", "int_value": 18}, {"type": "luanti_inventory_contains", "item": "basetools:pick_steel", "count": 1}, {"type": "luanti_mod_storage_equals", "key": "warptest_homestead_chests", "value": "stocked"}, {"type": "no_luanti_mod_errors"}],
        "tags": ["luanti", "production", "homestead", "save", "restore", "player"],
    },
    "jupyterlab-text-file-edit": {
        "benchmark": "JupyterLab",
        "target": {"kind": "text_file", "path": "notes/warptest.txt", "content": "WarpTest initial\n"},
        "checkpoint": {"allowed_strategies": ["jupyterlab_workspace_api_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "jupyterlab_open_file", "path": "notes/warptest.txt"}, {"type": "jupyterlab_edit_text_file", "path": "notes/warptest.txt", "content": "WarpTest edited\n"}],
        "assertions": [{"type": "jupyterlab_file_exists", "path": "notes/warptest.txt"}, {"type": "jupyterlab_file_text_equals", "path": "notes/warptest.txt", "content": "WarpTest edited\n"}, {"type": "no_jupyterlab_server_errors"}],
        "tags": ["jupyterlab", "contents", "text"],
    },
    "jupyterlab-markdown-file-save": {
        "benchmark": "JupyterLab",
        "target": {"kind": "text_file", "path": "notes/analysis.md", "content": "# WarpTest\n\nInitial note.\n"},
        "checkpoint": {"allowed_strategies": ["jupyterlab_workspace_api_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "jupyterlab_open_file", "path": "notes/analysis.md"}, {"type": "jupyterlab_edit_text_file", "path": "notes/analysis.md", "content": "# WarpTest\n\nUpdated note.\n"}],
        "assertions": [{"type": "jupyterlab_file_text_equals", "path": "notes/analysis.md", "content": "# WarpTest\n\nUpdated note.\n"}, {"type": "no_jupyterlab_server_errors"}],
        "tags": ["jupyterlab", "contents", "markdown"],
    },
    "jupyterlab-notebook-cell-output": {
        "benchmark": "JupyterLab",
        "target": {"kind": "notebook", "path": "notebooks/cell-output.ipynb", "cells": [{"cell_type": "code", "source": "print('warptest:42')\n"}]},
        "checkpoint": {"allowed_strategies": ["jupyterlab_workspace_api_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "jupyterlab_open_file", "path": "notebooks/cell-output.ipynb"}, {"type": "jupyterlab_run_notebook_cell", "path": "notebooks/cell-output.ipynb", "cell_index": 0}],
        "assertions": [{"type": "jupyterlab_notebook_output_contains", "path": "notebooks/cell-output.ipynb", "text": "warptest:42"}, {"type": "no_jupyterlab_server_errors"}],
        "tags": ["jupyterlab", "notebook", "kernel"],
    },
    "jupyterlab-notebook-markdown-note": {
        "benchmark": "JupyterLab",
        "target": {"kind": "notebook", "path": "notebooks/markdown-note.ipynb", "cells": [{"cell_type": "markdown", "source": "## WarpTest note\n\nSaved through the Contents API.\n"}]},
        "checkpoint": {"allowed_strategies": ["jupyterlab_workspace_api_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "jupyterlab_open_file", "path": "notebooks/markdown-note.ipynb"}],
        "assertions": [{"type": "jupyterlab_file_exists", "path": "notebooks/markdown-note.ipynb"}, {"type": "no_jupyterlab_server_errors"}],
        "tags": ["jupyterlab", "notebook", "markdown"],
    },
    "jupyterlab-workspace-layout": {
        "benchmark": "JupyterLab",
        "target": {"kind": "workspace_layout", "workspace": "warptest", "paths": ["notes/workspace.md", "notebooks/workspace.ipynb"]},
        "checkpoint": {"allowed_strategies": ["jupyterlab_workspace_api_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "jupyterlab_open_file", "path": "notes/workspace.md", "workspace": "warptest"}, {"type": "jupyterlab_open_file", "path": "notebooks/workspace.ipynb", "workspace": "warptest"}, {"type": "jupyterlab_set_workspace_layout", "workspace": "warptest", "paths": ["notes/workspace.md", "notebooks/workspace.ipynb"]}],
        "assertions": [{"type": "jupyterlab_workspace_contains", "workspace": "warptest", "path": "notes/workspace.md"}, {"type": "jupyterlab_workspace_contains", "workspace": "warptest", "path": "notebooks/workspace.ipynb"}, {"type": "no_jupyterlab_server_errors"}],
        "tags": ["jupyterlab", "workspace", "layout"],
    },
    "jupyterlab-session-smoke": {
        "benchmark": "JupyterLab",
        "target": {"kind": "terminal_session", "path": "notebooks/session-smoke.ipynb"},
        "checkpoint": {"allowed_strategies": ["jupyterlab_workspace_api_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "jupyterlab_create_notebook", "path": "notebooks/session-smoke.ipynb", "cells": []}, {"type": "jupyterlab_start_session", "path": "notebooks/session-smoke.ipynb", "kernel": "python3"}],
        "assertions": [{"type": "jupyterlab_file_exists", "path": "notebooks/session-smoke.ipynb"}, {"type": "jupyterlab_session_started"}, {"type": "no_jupyterlab_server_errors"}],
        "tags": ["jupyterlab", "session", "kernel"],
    },
    "libreoffice-writer-board-report-toc": {'benchmark': 'LibreOffice',
     'target': {'kind': 'writer_document',
                'path': 'writer-board-report-toc.odt',
                'paragraphs': [{'text': 'Executive Operations Review', 'style': 'Heading 1'},
                               {'text': 'Draft status packet for the board meeting.'},
                               {'text': 'Pipeline Recovery', 'style': 'Heading 2'},
                               {'text': 'Initial recovery narrative is pending owner detail.'},
                               {'text': 'Risk Register', 'style': 'Heading 2'},
                               {'text': 'Security and delivery risks need the consolidated decision log.'},
                               {'text': 'Appendix', 'style': 'Heading 1'}],
                'tables': {'Milestones': [['Milestone', 'Owner', 'Status'], ['Platform audit', 'Mira', 'Open'], ['Launch review', 'Dev', 'At risk']]}},
     'checkpoint': {'allowed_strategies': ['libreoffice_uno_document_validate'], 'expected_validity': 'save-restorable'},
     'actions': [{'type': 'libreoffice_open_document'},
                 {'type': 'libreoffice_insert_toc', 'title': 'Board Contents', 'include_styles': ['Heading 1', 'Heading 2']},
                 {'type': 'libreoffice_append_paragraph', 'text': 'Q3 recovery owner: Casey. Decision log is ready for board review.'},
                 {'type': 'libreoffice_set_table_cell', 'table': 'Milestones', 'cell': 'C2', 'value': 'Complete'},
                 {'type': 'libreoffice_update_indexes'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'},
                 {'type': 'libreoffice_export_pdf', 'path': 'writer-board-report-toc.pdf'}],
     'assertions': [{'type': 'libreoffice_toc_contains', 'text': 'Pipeline Recovery'},
                    {'type': 'libreoffice_text_contains', 'text': 'Q3 recovery owner: Casey.'},
                    {'type': 'libreoffice_table_cell_equals', 'table': 'Milestones', 'cell': 'C2', 'expected': 'Complete'},
                    {'type': 'libreoffice_pdf_exists', 'path': 'writer-board-report-toc.pdf'},
                    {'type': 'no_libreoffice_errors'}],
     'tags': ['libreoffice', 'writer', 'deep-state', 'toc']},
    "libreoffice-writer-invoice-table-rollup": {'benchmark': 'LibreOffice',
     'target': {'kind': 'writer_document',
                'path': 'writer-invoice-table-rollup.odt',
                'paragraphs': [{'text': 'WarpTest Services Invoice', 'style': 'Heading 1'},
                               {'text': 'Customer: Northstar Labs'},
                               {'text': 'Billing period: Q3 closeout'}],
                'tables': {'Invoice': [['Item', 'Qty', 'Unit', 'Total'],
                                       ['Platform advisory', 4, 150, 600],
                                       ['Support retainer', 2, 80, 160],
                                       ['Subtotal', '', '', 760],
                                       ['Tax', '', '', 76],
                                       ['Grand Total', '', '', 836]]}},
     'checkpoint': {'allowed_strategies': ['libreoffice_uno_document_validate'], 'expected_validity': 'save-restorable'},
     'actions': [{'type': 'libreoffice_open_document'},
                 {'type': 'libreoffice_insert_table_row', 'table': 'Invoice', 'row': 4, 'values': ['Incident review', 3, 120, 360]},
                 {'type': 'libreoffice_set_table_cell', 'table': 'Invoice', 'cell': 'D5', 'value': 1120},
                 {'type': 'libreoffice_set_table_cell', 'table': 'Invoice', 'cell': 'D6', 'value': 112},
                 {'type': 'libreoffice_set_table_cell', 'table': 'Invoice', 'cell': 'D7', 'value': 1232},
                 {'type': 'libreoffice_append_paragraph', 'text': 'Payment note: revised total includes incident review hours.'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'},
                 {'type': 'libreoffice_export_pdf', 'path': 'writer-invoice-table-rollup.pdf'}],
     'assertions': [{'type': 'libreoffice_table_cell_equals', 'table': 'Invoice', 'cell': 'A4', 'expected': 'Incident review'},
                    {'type': 'libreoffice_table_cell_equals', 'table': 'Invoice', 'cell': 'D7', 'expected': 1232},
                    {'type': 'libreoffice_text_contains', 'text': 'revised total includes incident review hours'},
                    {'type': 'libreoffice_pdf_exists', 'path': 'writer-invoice-table-rollup.pdf'},
                    {'type': 'no_libreoffice_errors'}],
     'tags': ['libreoffice', 'writer', 'deep-state', 'invoice']},
    "libreoffice-writer-policy-cross-reference": {'benchmark': 'LibreOffice',
     'target': {'kind': 'writer_document',
                'path': 'writer-policy-cross-reference.odt',
                'paragraphs': [{'text': 'Incident Response Policy', 'style': 'Heading 1'},
                               {'text': 'Purpose', 'style': 'Heading 2'},
                               {'text': 'This policy coordinates response ownership across teams.'},
                               {'text': 'Escalation Matrix', 'style': 'Heading 2'},
                               {'text': 'Severity one events require executive notification.'},
                               {'text': 'Evidence Handling', 'style': 'Heading 2'},
                               {'text': 'Evidence records must remain auditable after handoff.'}]},
     'checkpoint': {'allowed_strategies': ['libreoffice_uno_document_validate'], 'expected_validity': 'save-restorable'},
     'actions': [{'type': 'libreoffice_open_document'},
                 {'type': 'libreoffice_insert_bookmark', 'name': 'escalation_matrix', 'text': 'Escalation Matrix'},
                 {'type': 'libreoffice_insert_reference_field', 'bookmark': 'escalation_matrix', 'prefix': 'See section: '},
                 {'type': 'libreoffice_apply_paragraph_style', 'text': 'Evidence Handling', 'style': 'Heading 1'},
                 {'type': 'libreoffice_update_indexes'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'}],
     'assertions': [{'type': 'libreoffice_bookmark_exists', 'name': 'escalation_matrix'},
                    {'type': 'libreoffice_reference_text_contains', 'text': 'See section: Escalation Matrix'},
                    {'type': 'libreoffice_paragraph_style_is', 'text': 'Evidence Handling', 'style': 'Heading 1'},
                    {'type': 'no_libreoffice_errors'}],
     'tags': ['libreoffice', 'writer', 'deep-state', 'reference']},
    "libreoffice-calc-quarterly-budget-rollup": {'benchmark': 'LibreOffice',
     'target': {'kind': 'calc_spreadsheet',
                'path': 'calc-quarterly-budget-rollup.ods',
                'sheets': {'January': {'cells': {'A1': 'Category',
                                                 'B1': 'Budget',
                                                 'C1': 'Actual',
                                                 'A2': 'Revenue',
                                                 'B2': 12000,
                                                 'C2': 11800,
                                                 'A3': 'Cost',
                                                 'B3': 7400,
                                                 'C3': 7600}},
                           'February': {'cells': {'A1': 'Category',
                                                  'B1': 'Budget',
                                                  'C1': 'Actual',
                                                  'A2': 'Revenue',
                                                  'B2': 13500,
                                                  'C2': 13200,
                                                  'A3': 'Cost',
                                                  'B3': 7900,
                                                  'C3': 8100}},
                           'March': {'cells': {'A1': 'Category',
                                               'B1': 'Budget',
                                               'C1': 'Actual',
                                               'A2': 'Revenue',
                                               'B2': 14100,
                                               'C2': 13900,
                                               'A3': 'Cost',
                                               'B3': 8200,
                                               'C3': 8400}},
                           'Summary': {'cells': {'A1': 'Metric', 'A2': 'Revenue Actual', 'A3': 'Cost Actual', 'A4': 'Margin'},
                                       'formulas': {'B2': '=SUM(January.C2;February.C2;March.C2)',
                                                    'B3': '=SUM(January.C3;February.C3;March.C3)',
                                                    'B4': '=B2-B3'}}}},
     'checkpoint': {'allowed_strategies': ['libreoffice_uno_document_validate'], 'expected_validity': 'save-restorable'},
     'actions': [{'type': 'libreoffice_set_cell', 'sheet': 'March', 'cell': 'C2', 'value': 15000},
                 {'type': 'libreoffice_set_cell', 'sheet': 'March', 'cell': 'C3', 'value': 8300},
                 {'type': 'libreoffice_recalculate'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'}],
     'assertions': [{'type': 'libreoffice_cell_value_equals', 'sheet': 'Summary', 'cell': 'B2', 'expected': 40000},
                    {'type': 'libreoffice_cell_value_equals', 'sheet': 'Summary', 'cell': 'B4', 'expected': 16000},
                    {'type': 'libreoffice_sheet_exists', 'sheet': 'March'},
                    {'type': 'no_libreoffice_errors'}],
     'tags': ['libreoffice', 'calc', 'deep-state', 'budget']},
    "libreoffice-calc-sales-sort-ranking": {'benchmark': 'LibreOffice',
     'target': {'kind': 'calc_spreadsheet',
                'path': 'calc-sales-sort-ranking.ods',
                'sheet': 'Sales',
                'cells': {'A1': 'Rep',
                          'B1': 'Region',
                          'C1': 'Revenue',
                          'D1': 'Score',
                          'A2': 'Arun',
                          'B2': 'East',
                          'C2': 9200,
                          'D2': 82,
                          'A3': 'Blair',
                          'B3': 'West',
                          'C3': 13400,
                          'D3': 90,
                          'A4': 'Casey',
                          'B4': 'Central',
                          'C4': 11700,
                          'D4': 86,
                          'A5': 'Devon',
                          'B5': 'East',
                          'C5': 15100,
                          'D5': 94}},
     'checkpoint': {'allowed_strategies': ['libreoffice_uno_document_validate'], 'expected_validity': 'save-restorable'},
     'actions': [{'type': 'libreoffice_set_cell', 'sheet': 'Sales', 'cell': 'D4', 'value': 96},
                 {'type': 'libreoffice_sort_range', 'sheet': 'Sales', 'range': 'A2:D5', 'key': 'D', 'order': 'descending'},
                 {'type': 'libreoffice_set_formula', 'sheet': 'Sales', 'cell': 'E2', 'formula': '=ROW()-1'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'}],
     'assertions': [{'type': 'libreoffice_cell_value_equals', 'sheet': 'Sales', 'cell': 'A2', 'expected': 'Casey'},
                    {'type': 'libreoffice_cell_value_equals', 'sheet': 'Sales', 'cell': 'D2', 'expected': 96},
                    {'type': 'libreoffice_formula_equals', 'sheet': 'Sales', 'cell': 'E2', 'formula': '=ROW()-1'},
                    {'type': 'no_libreoffice_errors'}],
     'tags': ['libreoffice', 'calc', 'deep-state', 'ranking']},
    "libreoffice-calc-named-range-forecast": {'benchmark': 'LibreOffice',
     'target': {'kind': 'calc_spreadsheet',
                'path': 'calc-named-range-forecast.ods',
                'sheets': {'Inputs': {'cells': {'A1': 'Metric',
                                                'B1': 'Value',
                                                'A2': 'BaseRevenue',
                                                'B2': 50000,
                                                'A3': 'GrowthRate',
                                                'B3': 0.08,
                                                'A4': 'CostRate',
                                                'B4': 0.62}},
                           'Forecast': {'cells': {'A1': 'Metric', 'A2': 'Forecast Revenue', 'A3': 'Forecast Cost', 'A4': 'Forecast Profit'},
                                        'formulas': {'B2': '=Inputs.B2*(1+Inputs.B3)', 'B3': '=B2*Inputs.B4', 'B4': '=B2-B3'}}}},
     'checkpoint': {'allowed_strategies': ['libreoffice_uno_document_validate'], 'expected_validity': 'save-restorable'},
     'actions': [{'type': 'libreoffice_define_named_range', 'name': 'GrowthRate', 'sheet': 'Inputs', 'range': 'B3'},
                 {'type': 'libreoffice_define_named_range', 'name': 'CostRate', 'sheet': 'Inputs', 'range': 'B4'},
                 {'type': 'libreoffice_set_cell', 'sheet': 'Inputs', 'cell': 'B3', 'value': 0.12},
                 {'type': 'libreoffice_recalculate'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'}],
     'assertions': [{'type': 'libreoffice_named_range_exists', 'name': 'GrowthRate'},
                    {'type': 'libreoffice_cell_value_equals', 'sheet': 'Forecast', 'cell': 'B2', 'expected': 56000},
                    {'type': 'libreoffice_cell_value_equals', 'sheet': 'Forecast', 'cell': 'B4', 'expected': 21280},
                    {'type': 'no_libreoffice_errors'}],
     'tags': ['libreoffice', 'calc', 'deep-state', 'forecast']},
    "libreoffice-calc-chart-refresh-export": {'benchmark': 'LibreOffice',
     'target': {'kind': 'calc_spreadsheet',
                'path': 'calc-chart-refresh-export.ods',
                'sheet': 'Metrics',
                'cells': {'A1': 'Month', 'B1': 'Revenue', 'A2': 'Jan', 'B2': 12000, 'A3': 'Feb', 'B3': 13500, 'A4': 'Mar', 'B4': 14100, 'A5': 'Apr', 'B5': 15200}},
     'checkpoint': {'allowed_strategies': ['libreoffice_uno_document_validate'], 'expected_validity': 'save-restorable'},
     'actions': [{'type': 'libreoffice_set_cell', 'sheet': 'Metrics', 'cell': 'B5', 'value': 16800},
                 {'type': 'libreoffice_create_chart', 'sheet': 'Metrics', 'name': 'RevenueTrend', 'range': 'A1:B5', 'chart_type': 'line'},
                 {'type': 'libreoffice_recalculate'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'},
                 {'type': 'libreoffice_export_pdf', 'path': 'calc-chart-refresh-export.pdf'}],
     'assertions': [{'type': 'libreoffice_chart_exists', 'sheet': 'Metrics', 'name': 'RevenueTrend'},
                    {'type': 'libreoffice_cell_value_equals', 'sheet': 'Metrics', 'cell': 'B5', 'expected': 16800},
                    {'type': 'libreoffice_pdf_exists', 'path': 'calc-chart-refresh-export.pdf'},
                    {'type': 'no_libreoffice_errors'}],
     'tags': ['libreoffice', 'calc', 'deep-state', 'chart']},
    "libreoffice-impress-status-deck-build": {'benchmark': 'LibreOffice',
     'target': {'kind': 'impress_presentation',
                'path': 'impress-status-deck-build.odp',
                'slides': [{'title': 'Q3 Delivery Review', 'subtitle': 'Draft deck'},
                           {'title': 'Agenda', 'bullets': ['Delivery health', 'Budget movement', 'Risks']},
                           {'title': 'Delivery Health', 'bullets': ['Green builds', 'Manual QA pending']},
                           {'title': 'Risk Register', 'bullets': ['Security review open', 'Vendor date at risk']}]},
     'checkpoint': {'allowed_strategies': ['libreoffice_uno_document_validate'], 'expected_validity': 'save-restorable'},
     'actions': [{'type': 'libreoffice_set_slide_title', 'slide': 1, 'title': 'Q3 Delivery Review - Executive Update'},
                 {'type': 'libreoffice_set_slide_bullets', 'slide': 3, 'bullets': ['Green builds', 'Manual QA complete', 'Release candidate signed']},
                 {'type': 'libreoffice_add_shape', 'slide': 4, 'shape': 'callout', 'text': 'Decision needed: approve vendor backup plan'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'}],
     'assertions': [{'type': 'libreoffice_slide_count_equals', 'expected': 4},
                    {'type': 'libreoffice_slide_text_contains', 'slide': 1, 'text': 'Executive Update'},
                    {'type': 'libreoffice_shape_text_contains', 'slide': 4, 'text': 'vendor backup plan'},
                    {'type': 'no_libreoffice_errors'}],
     'tags': ['libreoffice', 'impress', 'deep-state', 'deck']},
    "libreoffice-impress-slide-reorder-summary": {'benchmark': 'LibreOffice',
     'target': {'kind': 'impress_presentation',
                'path': 'impress-slide-reorder-summary.odp',
                'slides': [{'title': 'Appendix Metrics', 'bullets': ['Raw detail follows']},
                           {'title': 'Customer Impact', 'bullets': ['Pilot customers stable', 'Support tickets down']},
                           {'title': 'Executive Summary', 'bullets': ['Draft summary', 'Needs final order']},
                           {'title': 'Mitigation Plan', 'bullets': ['Owner matrix ready']}]},
     'checkpoint': {'allowed_strategies': ['libreoffice_uno_document_validate'], 'expected_validity': 'save-restorable'},
     'actions': [{'type': 'libreoffice_move_slide', 'from': 3, 'to': 1},
                 {'type': 'libreoffice_set_slide_title', 'slide': 1, 'title': 'Executive Summary - Final Brief'},
                 {'type': 'libreoffice_set_slide_bullets',
                  'slide': 1,
                  'bullets': ['Customer impact stable', 'Mitigation owners assigned', 'Appendix moved to back']},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'}],
     'assertions': [{'type': 'libreoffice_slide_count_equals', 'expected': 4},
                    {'type': 'libreoffice_slide_title_equals', 'slide': 1, 'expected': 'Executive Summary - Final Brief'},
                    {'type': 'libreoffice_slide_text_contains', 'slide': 1, 'text': 'Mitigation owners assigned'},
                    {'type': 'no_libreoffice_errors'}],
     'tags': ['libreoffice', 'impress', 'deep-state', 'reorder']},
    "libreoffice-impress-kpi-chart-pdf": {'benchmark': 'LibreOffice',
     'target': {'kind': 'impress_presentation',
                'path': 'impress-kpi-chart-pdf.odp',
                'slides': [{'title': 'KPI Snapshot', 'bullets': ['ARR 4.2M', 'NRR 111%', 'Gross margin 68%']},
                           {'title': 'Expansion Motion', 'bullets': ['Pipeline 1.4M', 'Conversion 34%']}]},
     'checkpoint': {'allowed_strategies': ['libreoffice_uno_document_validate'], 'expected_validity': 'save-restorable'},
     'actions': [{'type': 'libreoffice_set_slide_bullets', 'slide': 1, 'bullets': ['ARR 4.8M', 'NRR 116%', 'Gross margin 70%']},
                 {'type': 'libreoffice_add_shape', 'slide': 1, 'shape': 'kpi-callout', 'text': 'NRR improved to 116% after enterprise renewals'},
                 {'type': 'libreoffice_add_shape', 'slide': 2, 'shape': 'kpi-callout', 'text': 'Pipeline coverage is above 3x target'},
                 {'type': 'libreoffice_save'},
                 {'type': 'libreoffice_reopen'},
                 {'type': 'libreoffice_export_pdf', 'path': 'impress-kpi-chart-pdf.pdf'}],
     'assertions': [{'type': 'libreoffice_slide_text_contains', 'slide': 1, 'text': 'NRR 116%'},
                    {'type': 'libreoffice_shape_text_contains', 'slide': 2, 'text': 'Pipeline coverage'},
                    {'type': 'libreoffice_pdf_exists', 'path': 'impress-kpi-chart-pdf.pdf'},
                    {'type': 'no_libreoffice_errors'}],
     'tags': ['libreoffice', 'impress', 'deep-state', 'pdf']},
    "blender-scene-cube-material": {
        "benchmark": "Blender",
        "target": {"kind": "scene", "objects": [{"name": "WarpCube", "type": "MESH", "primitive": "cube", "size": 2.0, "location": [0.0, 0.0, 0.0], "material": {"name": "WarpRed", "color": [1.0, 0.05, 0.02, 1.0]}}]},
        "checkpoint": {"allowed_strategies": ["blender_python_scene_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "blender_set_object_location", "object": "WarpCube", "location": [1.0, 2.0, 3.0]}],
        "assertions": [{"type": "blender_object_exists", "object": "WarpCube", "object_type": "MESH"}, {"type": "blender_material_color", "object": "WarpCube", "material": "WarpRed", "color": [1.0, 0.05, 0.02, 1.0]}, {"type": "blender_object_location", "object": "WarpCube", "location": [1.0, 2.0, 3.0]}, {"type": "no_blender_python_errors"}],
        "tags": ["blender", "scene", "material"],
    },
    "blender-save-reopen-scene": {
        "benchmark": "Blender",
        "target": {"kind": "scene", "objects": [{"name": "PersistCube", "type": "MESH", "primitive": "cube", "size": 1.5, "location": [-1.0, 0.0, 0.5], "material": {"name": "PersistBlue", "color": [0.05, 0.2, 1.0, 1.0]}}]},
        "checkpoint": {"allowed_strategies": ["blender_python_scene_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "blender_save_as", "path": "save-reopen-scene.blend"}, {"type": "blender_reopen", "path": "save-reopen-scene.blend"}],
        "assertions": [{"type": "blender_file_exists", "path": "save-reopen-scene.blend"}, {"type": "blender_object_exists", "object": "PersistCube", "object_type": "MESH"}, {"type": "blender_material_color", "object": "PersistCube", "material": "PersistBlue", "color": [0.05, 0.2, 1.0, 1.0]}, {"type": "no_blender_python_errors"}],
        "tags": ["blender", "persistence", "blend-file"],
    },
    "blender-modifier-apply": {
        "benchmark": "Blender",
        "target": {"kind": "scene", "objects": [{"name": "BevelCube", "type": "MESH", "primitive": "cube", "size": 2.0, "location": [0.0, 0.0, 0.0]}]},
        "checkpoint": {"allowed_strategies": ["blender_python_scene_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "blender_add_modifier", "object": "BevelCube", "name": "WarpBevel", "modifier_type": "BEVEL", "width": 0.2, "segments": 2}, {"type": "blender_apply_modifier", "object": "BevelCube", "modifier": "WarpBevel"}],
        "assertions": [{"type": "blender_object_exists", "object": "BevelCube", "object_type": "MESH"}, {"type": "blender_modifier_applied", "object": "BevelCube", "modifier": "WarpBevel", "vertex_count_min": 9}, {"type": "no_blender_python_errors"}],
        "tags": ["blender", "modifier"],
    },
    "blender-animation-keyframes": {
        "benchmark": "Blender",
        "target": {"kind": "scene", "objects": [{"name": "AnimatedCube", "type": "MESH", "primitive": "cube", "size": 1.0, "location": [0.0, 0.0, 0.0]}]},
        "checkpoint": {"allowed_strategies": ["blender_python_scene_validate"], "expected_validity": "save-restorable"},
        "actions": [{"type": "blender_set_keyframe", "object": "AnimatedCube", "data_path": "location", "frame": 1, "value": [0.0, 0.0, 0.0]}, {"type": "blender_set_keyframe", "object": "AnimatedCube", "data_path": "location", "frame": 24, "value": [4.0, 0.0, 0.0]}],
        "assertions": [{"type": "blender_object_exists", "object": "AnimatedCube", "object_type": "MESH"}, {"type": "blender_animation_keyframes", "object": "AnimatedCube", "data_path": "location", "frames": [1, 24]}, {"type": "no_blender_python_errors"}],
        "tags": ["blender", "animation"],
    },
}
